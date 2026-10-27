"""Superusers stepping into a DEMO investor's portal and back.

Only ever for flagged demo investors; real clients can never be impersonated. The admin session is
replaced by the demo investor's (they share one session cookie), and the superuser's id is kept
server-side in the session so a banner button can switch back. Nothing here is client-controllable:
the marker lives in the server-side session and is cleared whenever the session changes user."""
from django.contrib.auth import get_user_model, login, logout

SESSION_KEY = 'impersonator_id'
BACKEND = 'django.contrib.auth.backends.ModelBackend'


def start(request, target_user):
    admin_id = request.user.pk
    login(request, target_user, backend=BACKEND)  # swaps the session user (and clears the session)
    request.session[SESSION_KEY] = admin_id


def stop(request):
    """Switch back to the superuser who started. Returns that user, or None (everyone signed out)
    when the session isn't a valid demo impersonation."""
    current = request.user
    profile = getattr(current, 'investor_profile', None) if current.is_authenticated else None
    admin_id = request.session.get(SESSION_KEY)
    admin_user = (get_user_model().objects.filter(pk=admin_id, is_active=True, is_staff=True, is_superuser=True).first()
                  if admin_id else None)
    if admin_user is None or profile is None or not profile.is_demo:
        logout(request)
        return None
    login(request, admin_user, backend=BACKEND)
    return admin_user


def is_active(request):
    return bool(request.session.get(SESSION_KEY))
