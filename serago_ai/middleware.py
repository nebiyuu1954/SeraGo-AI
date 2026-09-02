"""
Security middleware for SeraGo-AI.

- ApiKeyMiddleware: Validates X-Api-Key header on webhook endpoints.
- RateLimitMiddleware: Simple in-memory rate limiter for sensitive endpoints.
- RequestLoggingMiddleware: Logs every request with timing.
"""

import logging
import time
from collections import defaultdict
from threading import Lock

from django.conf import settings
from django.http import JsonResponse

logger = logging.getLogger("serago_ai.security")


# ══════════════════════════════════════════════════════════════════════
#  API Key Authentication
# ══════════════════════════════════════════════════════════════════════

# Paths that require the API key (webhook endpoints called by .NET).
# Read endpoints (GET) and health checks are public.
_PROTECTED_PREFIXES = (
    "/api/matching/webhook/",
    "/api/matching/score-application",
)

# Paths that are always public (no API key needed).
_PUBLIC_PATHS = (
    "/api/matching/health",
    "/api/matching/classify-sector/health",
    "/admin/",
)


class ApiKeyMiddleware:
    """Validate X-Api-Key on protected webhook endpoints.

    The .NET backend sends this header with every webhook call.
    The scraper sends it when calling classify-sector.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Skip public paths
        for path in _PUBLIC_PATHS:
            if request.path.startswith(path):
                return self.get_response(request)

        # Check if this path requires API key auth
        requires_auth = any(request.path.startswith(p) for p in _PROTECTED_PREFIXES)

        if requires_auth:
            expected_key = getattr(settings, "MATCHING_API_KEY", "")
            if not expected_key:
                # No key configured — allow in DEBUG, block in production
                if not settings.DEBUG:
                    logger.error("MATCHING_API_KEY not configured — blocking request")
                    return JsonResponse(
                        {"error": "Server configuration error"}, status=500
                    )
                # In DEBUG mode, allow without key (local dev convenience)
                logger.warning(
                    "No MATCHING_API_KEY set — allowing in DEBUG mode"
                )
            else:
                provided_key = request.headers.get("X-Api-Key", "")
                if not provided_key:
                    logger.warning(
                        "Missing X-Api-Key on %s from %s",
                        request.path,
                        _get_client_ip(request),
                    )
                    return JsonResponse(
                        {"error": "Missing X-Api-Key header"}, status=401
                    )
                if provided_key != expected_key:
                    logger.warning(
                        "Invalid X-Api-Key on %s from %s",
                        request.path,
                        _get_client_ip(request),
                    )
                    return JsonResponse(
                        {"error": "Invalid API key"}, status=403
                    )

        return self.get_response(request)


# ══════════════════════════════════════════════════════════════════════
#  Rate Limiting
# ══════════════════════════════════════════════════════════════════════

# Rate limits: (max_requests, window_seconds)
_RATE_LIMITS = {
    "/api/matching/classify-sector": (60, 60),        # 60 req/min per IP
    "/api/matching/on-demand-match": (10, 60),         # 10 req/min per user
    "/api/matching/score-application": (20, 60),       # 20 req/min per IP
    "/api/matching/webhook/application": (100, 60),    # 100 req/min (batch)
    "/api/matching/webhook/talent-login": (30, 60),    # 30 req/min
}


class RateLimitMiddleware:
    """Simple in-memory sliding-window rate limiter.

    Limits per IP for most endpoints, per userId for on-demand-match.
    Resets automatically as old entries expire.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def __call__(self, request):
        # Find matching rate limit
        limit_config = None
        for path, config in _RATE_LIMITS.items():
            if request.path == path or request.path.startswith(path + "/"):
                limit_config = config
                break

        if limit_config:
            max_requests, window = limit_config
            # Use userId for on-demand endpoints, IP for everything else
            if "on-demand" in request.path:
                key = _get_user_id(request) or _get_client_ip(request)
            else:
                key = _get_client_ip(request)

            now = time.time()
            cutoff = now - window

            with self._lock:
                # Clean old entries
                self._requests[key] = [
                    t for t in self._requests[key] if t > cutoff
                ]

                if len(self._requests[key]) >= max_requests:
                    retry_after = int(self._requests[key][0] + window - now) + 1
                    logger.warning(
                        "Rate limit exceeded: %s from %s (%d/%d in %ds)",
                        request.path,
                        key[:20],
                        len(self._requests[key]),
                        max_requests,
                        window,
                    )
                    return JsonResponse(
                        {
                            "error": "Rate limit exceeded",
                            "retry_after_seconds": retry_after,
                        },
                        status=429,
                        headers={"Retry-After": str(retry_after)},
                    )

                self._requests[key].append(now)

        return self.get_response(request)


# ══════════════════════════════════════════════════════════════════════
#  Request Logging
# ══════════════════════════════════════════════════════════════════════


class RequestLoggingMiddleware:
    """Log every request with method, path, status code, and timing."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.time()
        response = self.get_response(request)
        elapsed = (time.time() - start) * 1000

        # Only log API requests (skip static files)
        if request.path.startswith("/api/"):
            logger.info(
                "%s %s → %d (%.0fms) [%s]",
                request.method,
                request.path,
                response.status_code,
                elapsed,
                _get_client_ip(request)[:15],
            )

        return response


# ══════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════


def _get_client_ip(request) -> str:
    """Get the real client IP (handles reverse proxies)."""
    x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _get_user_id(request) -> str | None:
    """Extract userId from request body (for rate limiting on-demand endpoints)."""
    try:
        import json
        body = json.loads(request.body)
        return body.get("userId")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None
