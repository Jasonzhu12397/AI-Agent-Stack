from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_auth_event
from core.models import AuditEvent


@require_GET
@ensure_csrf_cookie
def csrf_cookie(request):
    """Hand out a CSRF token that is never reused from a cache.

    The token is tied to the cookie in the same response, so any intermediary
    that serves a cached body pairs a stale token with a live cookie and every
    write request fails with "CSRF token incorrect". Proxies in front of the
    console (Codespaces, tunnels) do cache GETs, hence these headers.
    """

    response = JsonResponse({"csrf_token": get_token(request)})
    response["Cache-Control"] = "no-store, private, max-age=0"
    response["Pragma"] = "no-cache"
    response["Vary"] = "Cookie"
    return response


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    @method_decorator(csrf_protect)
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            record_auth_event(
                request,
                "auth.login.denied",
                AuditEvent.Outcome.DENIED,
                "invalid_payload",
            )
            return Response({"detail": "Username and password are required."}, status=400)
        user = authenticate(request=request, username=username, password=password)
        if user is None or not user.is_active:
            record_auth_event(
                request,
                "auth.login.denied",
                AuditEvent.Outcome.DENIED,
                "invalid_credentials",
            )
            return Response({"detail": "Invalid username or password."}, status=400)
        record_auth_event(
            request,
            "auth.login.succeeded",
            AuditEvent.Outcome.SUCCESS,
            "authenticated",
            actor=user,
        )
        login(request, user)
        return Response(
            {
                "id": user.pk,
                "username": user.get_username(),
                "email": user.email,
            }
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        actor = request.user
        record_auth_event(
            request,
            "auth.logout.succeeded",
            AuditEvent.Outcome.SUCCESS,
            "signed_out",
            actor=actor,
        )
        logout(request)
        return Response(status=204)


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            {
                "id": request.user.pk,
                "username": request.user.get_username(),
                "email": request.user.email,
                "is_staff": request.user.is_staff,
            }
        )
