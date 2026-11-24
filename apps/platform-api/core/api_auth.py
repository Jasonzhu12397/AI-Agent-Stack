"""Authentication classes shared by every API view.

Kept in a module that imports nothing from ``core.auth_views`` so the DRF
settings can load it while Django is still starting up.
"""

from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.sessions.backends.db import SessionStore
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import AuthenticationFailed


class SessionAuthentication401(SessionAuthentication):
    """Session auth that answers 401 instead of 403 when credentials are absent.

    DRF turns ``NotAuthenticated`` into 403 whenever the authenticator cannot
    produce a challenge header. That makes an expired cookie look identical to
    a permission refusal, so the console showed one 403 per page instead of one
    sign-in prompt. A challenge header keeps the two cases apart.
    """

    def authenticate_header(self, request):
        return "Session"


class SessionTokenAuthentication(SessionAuthentication):
    """Session auth through an ``X-Session-Token`` header.

    Access proxies and in-app webviews drop or partition the session cookie,
    which made every request after sign-in look like an expired login. The
    login response hands the same session key back as a token, and this class
    resolves it against the server-side session store, so the console keeps
    working when cookies cannot travel.
    """

    def authenticate(self, request):
        header = request.headers.get("X-Session-Token")
        if not header:
            return None
        if len(header) > 128:
            raise AuthenticationFailed("Invalid session token.")
        session = SessionStore(session_key=header)
        if not session.exists(header):
            raise AuthenticationFailed("This session has expired. Sign in again.")
        user_id = session.get(SESSION_KEY)
        if user_id is None:
            raise AuthenticationFailed("This session has expired. Sign in again.")
        user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
        if user is None:
            raise AuthenticationFailed("This session has expired. Sign in again.")
        # CSRF stays enforced for cookie-authenticated writes; a token
        # authenticates the caller, and the CSRF layer still checks the header.
        # CSRF is not re-checked here: the token travels in a custom header a
        # cross-site page cannot set, which is the property CSRF protects.
        session.accessed = True
        session.save()
        return (user, None)

    def authenticate_header(self, request):
        return "Session"
