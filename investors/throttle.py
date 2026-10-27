"""Failed-login throttling for the portal.

Uses Django's cache. The default cache is per-process, so with N gunicorn
workers the effective limit is up to N times higher; switch CACHES to a shared
backend if tighter limits are needed.
"""
from django.core.cache import cache

MAX_FAILURES_PER_USER = 5
MAX_FAILURES_PER_IP = 20
WINDOW_SECONDS = 15 * 60


def client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def _keys(request, username):
    return (f'inv-login-user:{username.strip().lower()}', f'inv-login-ip:{client_ip(request)}')


def is_throttled(request, username):
    user_key, ip_key = _keys(request, username)
    return (cache.get(user_key, 0) >= MAX_FAILURES_PER_USER
            or cache.get(ip_key, 0) >= MAX_FAILURES_PER_IP)


def record_failure(request, username):
    for key in _keys(request, username):
        cache.add(key, 0, WINDOW_SECONDS)
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, WINDOW_SECONDS)


def clear(request, username):
    cache.delete(_keys(request, username)[0])


# Password-reset emails: stop the form being used to flood an investor's inbox.
RESET_MAX_PER_EMAIL = 3
RESET_WINDOW_SECONDS = 60 * 60


def allow_reset_email(email):
    """Count a reset request for this address; False once it is over the limit."""
    key = f'inv-reset:{email.strip().lower()}'
    cache.add(key, 0, RESET_WINDOW_SECONDS)
    try:
        return cache.incr(key) <= RESET_MAX_PER_EMAIL
    except ValueError:
        cache.set(key, 1, RESET_WINDOW_SECONDS)
        return True
