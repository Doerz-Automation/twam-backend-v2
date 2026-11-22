"""
Rate limits for the public sign-in / sign-up / OTP endpoints.

Counts live in the shared database cache (settings.CACHES), so every gunicorn
worker and every server sees the same numbers. Each counter is a fixed window:
it starts at the first attempt and expires `window` seconds later.

Limits are keyed mainly by the account (email or signed-in user), which a caller
cannot fake. The IP limit is a looser second line: the forwarded IP header can be
spoofed, so it is never the only protection.
"""
import math
import time
from functools import wraps

from django.core.cache import cache
from graphql import GraphQLError

TOO_MANY = "Too many attempts. Please try again in {minutes} minute{s}."


# --- what to count by ---------------------------------------------------------

def client_ip(info):
    request = info.context.request
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        # nginx and the load balancer append to the list; the first entry is the
        # address the request came from
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def by_ip(info, kwargs):
    return client_ip(info)


def by_email(info, kwargs):
    """Email from an `email` argument or from `input.email`."""
    email = kwargs.get("email")
    if email is None and kwargs.get("input") is not None:
        email = getattr(kwargs["input"], "email", None)
    return (email or "").strip().lower() or None


def by_user(info, kwargs):
    user = info.context.request.user
    return str(user.id) if getattr(user, "is_authenticated", False) else None


# --- counters -----------------------------------------------------------------

def _key(scope, ident):
    return f"rl:{scope}:{ident}"


def _read(key):
    """(count, expires_at) or (0, None) when there is no live window."""
    entry = cache.get(key)
    if not entry:
        return 0, None
    count, expires_at = entry
    if expires_at <= time.time():
        return 0, None
    return count, expires_at


def _hit(key, window):
    count, expires_at = _read(key)
    if expires_at is None:
        expires_at = time.time() + window
    # keep the original window end: re-setting with the remaining time stops a
    # steady stream of attempts from pushing the window forward forever
    remaining = max(1, int(math.ceil(expires_at - time.time())))
    cache.set(key, (count + 1, expires_at), remaining)


def _deny_if_over(key, limit):
    count, expires_at = _read(key)
    if expires_at is not None and count >= limit:
        minutes = max(1, int(math.ceil((expires_at - time.time()) / 60)))
        raise GraphQLError(TOO_MANY.format(minutes=minutes, s="" if minutes == 1 else "s"))


def rate_limit(scope, *, key, limit, window, failures_only=False, reset_on_success=False):
    """
    Refuse a resolver call once `limit` attempts were made in `window` seconds.

    failures_only:    count only calls that raise (a wrong password or code), not
                      successful ones
    reset_on_success: clear the counter after a successful call; use it for the
                      per-account limit, never for the IP limit (otherwise an
                      attacker could reset it with their own valid account)

    Put it directly above `def` so the API secret / login checks run first.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(self, info, *args, **kwargs):
            ident = key(info, kwargs)
            if ident is None:
                return func(self, info, *args, **kwargs)
            k = _key(scope, ident)
            _deny_if_over(k, limit)
            if not failures_only:
                _hit(k, window)
            try:
                result = func(self, info, *args, **kwargs)
            except Exception:
                if failures_only:
                    _hit(k, window)
                raise
            if reset_on_success:
                cache.delete(k)
            return result
        return wrapper
    return decorator


MINUTE = 60
HOUR = 60 * MINUTE
