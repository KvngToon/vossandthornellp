from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.cache import add_never_cache_headers


def investor_required(view=None, *, allow_pending_password_change=False):
    """Allow only authenticated, active, non-staff investors.

    While the investor still has a temporary password every page redirects to the change-password
    page. Sets request.investor to the InvestorProfile. Anyone else who is signed in
    gets a 404 so the portal's existence isn't confirmed to them. Views must
    scope every query by request.investor, never by an ID in the URL alone.
    """
    if view is None:  # used as @investor_required(allow_pending_password_change=True)
        return lambda v: investor_required(v, allow_pending_password_change=allow_pending_password_change)

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect_to_login(request.get_full_path(), reverse('investors:login'))
        profile = getattr(user, 'investor_profile', None)
        if user.is_staff or profile is None or not profile.is_active:
            raise Http404
        request.investor = profile
        if profile.must_change_password and not allow_pending_password_change:
            return redirect('investors:change_password')
        response = view(request, *args, **kwargs)
        # Financial data: never keep it in browser/proxy caches (back button after logout).
        add_never_cache_headers(response)
        response['Cache-Control'] += ', private, no-store'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response
    return wrapped
