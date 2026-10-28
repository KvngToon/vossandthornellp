"""Where is this request coming from? Used to enforce 'withdrawals only from the allowed countries' (settings.WITHDRAWAL_ALLOWED_COUNTRIES).

Cloudflare adds a `CF-IPCountry` header to every proxied request. That header is only trustworthy if
the request really came through Cloudflare, so a Cloudflare Transform Rule also adds a secret
header (X-Origin-Verify) that the app checks. A request without the right secret (for example one
sent straight to the Render address, possibly with a faked country) has an UNKNOWN country, and
unknown is treated as not allowed.
"""
import hmac

from django.conf import settings

# Names for the message shown to investors; any allowed country missing here falls back to its code.
COUNTRY_NAMES = {'US': 'the United States', 'GB': 'the United Kingdom'}


def _blocked_message(extra=()):
    codes = list(settings.WITHDRAWAL_ALLOWED_COUNTRIES) + [c for c in extra if c not in settings.WITHDRAWAL_ALLOWED_COUNTRIES]
    names = [COUNTRY_NAMES.get(code, code) for code in codes]
    joined = names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]
    return f'Withdrawals can only be initiated from within {joined}.'


BLOCKED_MESSAGE = _blocked_message()


def blocked_message(profile=None):
    """The refusal text for this investor: the standard countries plus any extras staff gave them."""
    return _blocked_message(profile.extra_countries if profile is not None else ())
SECRET_HEADER = 'HTTP_X_ORIGIN_VERIFY'
COUNTRY_HEADER = 'HTTP_CF_IPCOUNTRY'


def detect_country(request):
    """Two-letter country code, or None when it can't be established reliably."""
    if settings.DEBUG and settings.DEV_COUNTRY:
        # Local development has no Cloudflare in front; DEV_COUNTRY=FR lets you see the blocked state.
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


def withdrawal_location(request, profile=None):
    """(allowed, country_or_None, overridden) for this request and investor.

    Standard rule first. Only when that refuses can an investor-specific override apply: extra countries
    need a verified country that is on their list; 'any location' skips the check entirely. `overridden`
    is True when the override, not the standard rule, is what allowed it (recorded on the request)."""
    allowed, country = withdrawals_allowed(request)
    if allowed:
        return True, country, False
    if profile is not None:
        if profile.withdrawal_any_location:
            return True, country, True
        if country is not None and country in profile.extra_countries:
            return True, country, True
    return False, country, False
