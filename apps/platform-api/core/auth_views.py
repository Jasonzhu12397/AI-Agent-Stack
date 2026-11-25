import re
import secrets

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_auth_event
from core.models import (
    AuditEvent,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)


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
        if request.session.session_key is None:
            request.session.save()
        return Response(
            {
                "id": user.pk,
                "username": user.get_username(),
                "email": user.email,
                # The same server-side session key, usable as a header when the
                # cookie is dropped by an access proxy or a partitioned webview.
                "session_token": request.session.session_key,
            }
        )


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9]{6,15}$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def _normalized_identity(value: str) -> tuple[str, str, str]:
    """Return ``(kind, username, email)`` for an email or phone signup."""

    identity = value.strip()
    if "@" in identity:
        email = identity.lower()
        if not EMAIL_RE.fullmatch(email) or len(email) > 254:
            raise ValueError("Enter a valid email address.")
        return "email", email, email
    phone = re.sub(r"[\s()\-]", "", identity)
    if not PHONE_RE.fullmatch(phone):
        raise ValueError("Enter a valid phone number with 6 to 15 digits.")
    return "phone", phone, ""


def _unique_tenant_slug(name: str) -> str:
    base = slugify(name)[:60] or "workspace"
    slug = base
    while Tenant.objects.filter(slug=slug).exists():
        slug = f"{base[:70]}-{secrets.token_hex(2)}"
    return slug[:80]


class RegisterView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    @method_decorator(csrf_protect)
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    @transaction.atomic
    def post(self, request):
        if not getattr(settings, "PLATFORM_ALLOW_SELF_REGISTRATION", True):
            return Response({"detail": "Self registration is disabled."}, status=403)
        identifier = request.data.get("identifier")
        password = request.data.get("password")
        display_name = request.data.get("display_name", "")
        if not isinstance(identifier, str) or not isinstance(password, str):
            return Response(
                {"detail": "An email address or phone number and a password are required."},
                status=400,
            )
        if len(password) < MIN_PASSWORD_LENGTH:
            return Response(
                {"detail": f"Password must be at least {MIN_PASSWORD_LENGTH} characters."},
                status=400,
            )
        if len(password) > MAX_PASSWORD_LENGTH:
            return Response({"detail": "Password is too long."}, status=400)
        try:
            kind, username, email = _normalized_identity(identifier)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        if display_name and not isinstance(display_name, str):
            return Response({"detail": "Display name must be text."}, status=400)
        display_name = (display_name.strip() or username.split("@")[0])[:120]
        user_model = get_user_model()
        if user_model.objects.filter(username__iexact=username).exists():
            return Response(
                {"detail": "An account with this email or phone number already exists."},
                status=409,
            )
        user = user_model.objects.create_user(
            username=username,
            email=email,
            password=password,
        )
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])
        tenant = Tenant.objects.create(
            name=f"{display_name}'s workspace"[:120],
            slug=_unique_tenant_slug(display_name or username),
        )
        TenantMembership.objects.create(
            tenant=tenant,
            user=user,
            role=TenantMembership.Role.OWNER,
        )
        project = Project.objects.create(
            tenant=tenant,
            name="Default project",
            slug="default",
        )
        ProjectMembership.objects.create(
            project=project,
            user=user,
            role=ProjectMembership.Role.ADMIN,
        )
        record_auth_event(
            request,
            "auth.register.succeeded",
            AuditEvent.Outcome.SUCCESS,
            f"{kind}_self_registration",
            actor=user,
        )
        login(request, user)
        if request.session.session_key is None:
            request.session.save()
        return Response(
            {
                "id": user.pk,
                "username": user.get_username(),
                "email": user.email,
                "session_token": request.session.session_key,
                "tenant_id": str(tenant.id),
                "project_id": str(project.id),
            },
            status=201,
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
