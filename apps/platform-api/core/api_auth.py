"""Authentication classes shared by every API view.

Kept in a module that imports nothing from ``core.auth_views`` so the DRF
settings can load it while Django is still starting up.
"""

from rest_framework.authentication import SessionAuthentication


class SessionAuthentication401(SessionAuthentication):
    """Session auth that answers 401 instead of 403 when credentials are absent.

    DRF turns ``NotAuthenticated`` into 403 whenever the authenticator cannot
    produce a challenge header. That makes an expired cookie look identical to
    a permission refusal, so the console showed one 403 per page instead of one
    sign-in prompt. A challenge header keeps the two cases apart.
    """

    def authenticate_header(self, request):
        return "Session"
