from django.http import HttpResponse


def healthz(request):
    """Unauthenticated liveness probe for load balancers and PaaS health checks.

    Intentionally performs no database access so it stays cheap and reliable.
    """
    return HttpResponse("ok")
