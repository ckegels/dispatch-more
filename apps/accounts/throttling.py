"""Shared login rate limiting for JWT and Django admin."""

from django.http import HttpResponse
from rest_framework.throttling import AnonRateThrottle

from dispatcharr.utils import get_client_ip


class LoginRateThrottle(AnonRateThrottle):
    """Per-IP login throttle (scope ``login``, see REST_FRAMEWORK rates).

    Identity uses the same trusted-proxy-aware ``get_client_ip`` helper as
    ACLs and audit logs, instead of DRF's default ``get_ident``. DRF's
    default trusts a raw, client-suppliable ``X-Forwarded-For`` verbatim
    (nginx appends rather than replaces it), so a caller varying that header
    on every request would otherwise mint a fresh throttle bucket each time
    and never get rate limited.
    """

    scope = "login"

    def get_ident(self, request):
        return get_client_ip(request) or super().get_ident(request)

    def allow_request(self, request, view):
        if not failures_only():
            return super().allow_request(request, view)
        # Dispatch More: only failed logins use up the budget (note_failed_login). Every
        # device behind one address shares it -- a house behind the VPN is one address -- and
        # with correct logins counted, two TVs and a phone restarting after an update locked
        # each other out of their live sockets (2026-10-18). Three wrong passwords a minute
        # from an address are still all it gets.
        if self.rate is None:
            return True
        self.key = self.get_cache_key(request, view)
        if self.key is None:
            return True
        self.now = self.timer()
        self.history = [t for t in self.cache.get(self.key, []) if t > self.now - self.duration]
        if len(self.history) >= self.num_requests:
            return self.throttle_failure()
        return True


def failures_only():
    """
    Dispatch More: while Watch together is on (Settings -> arrTV, apps.proxy.live_proxy
    app_links) only failed logins count -- the devices watching together depend on their live
    sockets, and their logins share one address's budget (the user: "just auto apply this if
    these settings for watch together are on"). Off: stock, every attempt counts.
    """
    try:
        from apps.proxy.live_proxy import app_links

        return app_links.enabled()
    except Exception:
        return False


def note_failed_login(request):
    """A login that failed, counted against the address's budget (only with failures_only)."""
    if not failures_only():
        return
    throttle = LoginRateThrottle()
    key = throttle.get_cache_key(request, None)
    if key is None:
        return
    now = throttle.timer()
    history = [t for t in throttle.cache.get(key, []) if t > now - throttle.duration]
    history.insert(0, now)
    throttle.cache.set(key, history, throttle.duration)


def enforce_login_rate_limit(request):
    """Return a 429 response when the shared login throttle denies *request*.

    Allowed checks consume a slot in the same ``login`` scope used by
    ``TokenObtainPairView``, so JWT and admin login attempts share one budget.
    Returns ``None`` when the request may proceed.
    """
    throttle = LoginRateThrottle()
    if throttle.allow_request(request, view=None):
        return None

    wait = throttle.wait()
    response = HttpResponse(
        "Too many login attempts. Try again later.",
        status=429,
        content_type="text/plain; charset=utf-8",
    )
    if wait is not None:
        response["Retry-After"] = str(max(1, int(wait)))
    return response
