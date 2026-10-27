"""Where is this request coming from? Used to enforce 'withdrawals only from the USA'.

Cloudflare adds a `CF-IPCountry` header to every proxied request. That header is only trustworthy if
the request really came through Cloudflare, so a Cloudflare Transform Rule also adds a secret
header (X-Origin-Verify) that the app checks. A request without the right secret (for example one
sent straight to the Render address, possibly with a faked country) has an UNKNOWN country, and
unknown is treated as not allowed.
"""
import hmac

from django.conf import settings

BLOCKED_MESSAGE = 'Withdrawals can only be initiated from within the United States.'
SECRET_HEADER = 'HTTP_X_ORIGIN_VERIFY'
COUNTRY_HEADER = 'HTTP_CF_IPCOUNTRY'


def detect_country(request):
    """Two-letter country code, or None when it can't be established reliably."""
    if settings.DEBUG and settings.DEV_COUNTRY:
        # Local development has no Cloudflare in front; DEV_COUNTRY=GB lets you see the blocked state.
        return settings.DEV_COUNTRY.upper()
    expected = settings.CF_ORIGIN_SECRET
    supplied = request.META.get(SECRET_HEADER, '')
    if not expected or not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        return None
    code = request.META.get(COUNTRY_HEADER, '').strip().upper()
    # XX = Cloudflare could not tell; T1 = Tor. Neither is a usable country.
    if len(code) != 2 or not code.isalpha() or code in {'XX', 'T1'}:
        return None
    return code


def withdrawals_allowed(request):
    """(allowed, country_or_None)."""
    country = detect_country(request)
    return (country in settings.WITHDRAWAL_ALLOWED_COUNTRIES, country)
