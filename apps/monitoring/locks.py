"""Cross-process locks for long-running monitoring operations."""

import hashlib
import logging
import uuid
from contextlib import contextmanager

from django.core.cache import cache
from django.db import DatabaseError, connection

logger = logging.getLogger(__name__)

# Cloud inventory passes have a 20-minute hard task limit. The fallback cache
# lock outlives that limit so another worker cannot start while the first task
# is being terminated.
CLOUD_SYNC_LOCK_TIMEOUT_SECONDS = 25 * 60


def cloud_sync_lock_id(cloud_uuid):
    """Return a stable signed 64-bit PostgreSQL advisory-lock identifier."""
    digest = hashlib.blake2b(str(cloud_uuid).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


@contextmanager
def cloud_sync_lock(cloud_uuid):
    """Try to acquire a process-safe lock for one cloud inventory pass.

    PostgreSQL session advisory locks cover every web and worker process using
    the database without holding a transaction open during provider API calls.
    A tokenized cache lock keeps development/test databases functional.
    """
    if connection.vendor == "postgresql":
        lock_id = cloud_sync_lock_id(cloud_uuid)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
            acquired = bool(cursor.fetchone()[0])

        try:
            yield acquired
        finally:
            if acquired:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
                except DatabaseError:
                    # A lost database connection releases session advisory
                    # locks automatically. Keep the original task outcome.
                    logger.warning(
                        "Could not explicitly release cloud sync lock for %s",
                        cloud_uuid,
                    )
        return

    cache_key = f"cloud-sync-lock:{cloud_uuid}"
    token = uuid.uuid4().hex
    acquired = cache.add(
        cache_key,
        token,
        timeout=CLOUD_SYNC_LOCK_TIMEOUT_SECONDS,
    )
    try:
        yield acquired
    finally:
        # Never delete a newer worker's lock if this fallback lock expired and
        # was reacquired while the original operation was finishing.
        if acquired and cache.get(cache_key) == token:
            cache.delete(cache_key)
