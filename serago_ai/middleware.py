"""
Security middleware for SeraGo-AI.

- ApiKeyMiddleware: Validates X-Api-Key header on webhook endpoints.
- RateLimitMiddleware: Simple in-memory rate limiter for sensitive endpoints.
- RequestLoggingMiddleware: Logs every request with timing.
- DebugRequestMiddleware: Temporary debug logging for malformed HTTP requests.
"""

import logging
import time
from collections import defaultdict
from threading import Lock

from django.conf import settings
from django.http import JsonResponse

logger = logging.getLogger("serago_ai.security")
debug_logger = logging.getLogger("serago_ai.debug")


# ══════════════════════════════════════════════════════════════════════
#  API Key Authentication
# ══════════════════════════════════════════════════════════════════════

# Paths that require the API key (webhook + batch read endpoints called by .NET).
# Health checks and API docs are public.
_PROTECTED_PREFIXES = (
    "/api/matching/webhook/",
    "/api/matching/refresh-for-you",
    "/api/matching/applications/",
    # Batch read endpoints used by .NET to annotate pages
    "/api/matching/scores/",
    "/api/matching/application-scores/",
    # Resume parsing — called by .NET with a presigned R2 URL for a private
    # resume. Without this entry the endpoint would be OPEN to the internet.
    "/api/matching/parse-resume",
)

# Paths that are always public (no API key, no CSRF concern).
# NOTE: everything here short-circuits the API-key check in __call__ before
# `requires_auth` is computed, so a path must not appear in BOTH tuples — the
# entry below wins and silently disables the key check.
_PUBLIC_PATHS = (
    "/api/matching/health",
    "/api/matching/docs",
    "/api/matching/redoc",
    "/api/matching/schema",
    "/admin/",
    "/api/ai/health",
)


class ApiKeyMiddleware:
    """Validate X-Api-Key on protected webhook/endpoint paths.

    The .NET backend sends this header with every webhook/batch/parse call.
    The AI classify endpoint is also protected by this middleware when
    MATCHING_API_KEY is configured (it must stay OUT of _PUBLIC_PATHS for
    that to hold — that tuple short-circuits before this check runs).
    """

    # AI service paths that Django calls from the .NET backend via X-Api-Key.
    _AI_CLASSIFY_PATH = "/api/ai/classify"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Skip public paths
        for path in _PUBLIC_PATHS:
            if request.path.startswith(path):
                return self.get_response(request)

        # AI classify is machine-to-machine (X-Api-Key), not a browser form.
        requires_auth = (
            any(request.path.startswith(p) for p in _PROTECTED_PREFIXES)
            or request.path == self._AI_CLASSIFY_PATH
        )

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
    "/api/matching/webhook/application": (100, 60),    # 100 req/min (applications)
    "/api/matching/webhook/job-published": (60, 60),   # 60 req/min (job approvals)
}


class RateLimitMiddleware:
    """Simple in-memory sliding-window rate limiter.

    Limits per IP. Resets automatically as old entries expire.
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


class DebugRequestMiddleware:
    """Temporary debug middleware to capture malformed HTTP requests.

    Logs the raw request line / first bytes seen by Django before the HTTP
    parser rejects them (e.g. 'Bad request syntax'). This is for local
    debugging only and should not be left enabled in production.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        meta = getattr(request, "META", {})
        method = meta.get("REQUEST_METHOD", "")
        path = meta.get("PATH_INFO", "")
        protocol = meta.get("SERVER_PROTOCOL", "")
        body_preview = b""

        try:
            body = request.body
            if isinstance(body, (bytes, bytearray)):
                body_preview = bytes(body)[:200]
            else:
                body_preview = str(body).encode("utf-8", errors="replace")[:200]
        except Exception as exc:
            body_preview = str(exc).encode("utf-8", errors="replace")[:200]

        debug_logger.debug(
            "DEBUG_HTTP_IN %(METHOD)s %(PATH)s %(PROTOCOL)s | body_bytes=%(LEN)d | body_head=%(BODY)s",
            {
                "METHOD": method,
                "PATH": path,
                "PROTOCOL": protocol,
                "LEN": len(body_preview),
                "BODY": body_preview,
            },
        )

        return self.get_response(request)


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

