"""Compatibility wrappers for the hardened Hetzner status checks.

Older callers import this module directly for Server and Volume checks. Keep
those imports working while ensuring they use the same bounded, redacted,
read-only implementation as every other Hetzner resource family.
"""

from apps.monitoring.checks.hetzner_resources import (
    check_hetzner_server_status,
    check_hetzner_volume_status,
)

__all__ = [
    "check_hetzner_server_status",
    "check_hetzner_volume_status",
]
