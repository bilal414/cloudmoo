import logging

from django.db import connection
from django.http import HttpResponse, JsonResponse

logger = logging.getLogger(__name__)


def healthz(request):
    """Unauthenticated liveness probe for load balancers and PaaS health checks.

    Intentionally performs no database access so it stays cheap and reliable.
    """
    return HttpResponse("ok")


def readyz(request):
    """Database readiness probe for deployment and load-balancer checks."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        logger.exception("Readiness probe failed")
        return JsonResponse(
            {'status': 'not_ready', 'checks': {'database': 'failed'}},
            status=503,
        )

    return JsonResponse(
        {'status': 'ready', 'checks': {'database': 'ok'}},
    )
