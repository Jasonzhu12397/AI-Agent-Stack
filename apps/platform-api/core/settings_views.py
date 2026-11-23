"""Settings center HTTP surface: scoped settings, preferences, account, meta."""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import NotAuthenticated, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_audit_event
from core.membership_views import (
    project_for_user,
    require_project_admin,
    require_tenant_admin,
    tenant_for_user,
)
from core.settings_models import (
    FeedbackEntry,
    ProjectSetting,
    TenantSetting,
    UserAccountProfile,
    UserPreference,
)
from core.settings_serializers import (
    NAMESPACE_DEFAULTS,
    NAMESPACE_SCHEMAS,
    PREFERENCE_NAMESPACES,
    SECTION_DEFAULTS,
    SECTION_SCHEMAS,
    SHORTCUT_IDS,
    SHORTCUT_REGISTRY,
    TENANT_SECTIONS,
    normalize_combination,
    validate_combination,
    validate_values,
)


def _error(code: str, message: str, details: dict | None = None, http_status: int = 400):
    return Response(
        {"error": {"code": code, "message": message, "details": details or {}}},
        status=http_status,
    )


def _fold(defaults: dict[str, object], *layers: tuple[str, dict[str, object]]):
    effective = dict(defaults)
    sources = {key: "default" for key in defaults}
    for layer_name, layer_values in layers:
        for key, value in layer_values.items():
            effective[key] = value
            sources[key] = layer_name
    return effective, sources


class SettingsBaseView(APIView):
    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc):
        if isinstance(exc, Http404):
            return _error(
                "NOT_FOUND", "Resource not found.", {}, status.HTTP_404_NOT_FOUND
            )
        if isinstance(exc, PermissionDenied):
            return _error(
                "FORBIDDEN", "Permission denied.", {}, status.HTTP_403_FORBIDDEN
            )
        if isinstance(exc, NotAuthenticated):
            return _error(
                "AUTH_REQUIRED",
                "Authentication required.",
                {},
                status.HTTP_401_UNAUTHORIZED,
            )
        return super().handle_exception(exc)


def _settings_payload(scope: str, section: str, stored: dict, defaults: dict, row):
    effective, sources = _fold(defaults, (scope, stored))
    return {
        "section": section,
        "scope": scope,
        "values": stored,
        "effective": effective,
        "defaults": defaults,
        "sources": sources,
        "updated_at": row.updated_at if row else None,
    }


class TenantSettingsView(SettingsBaseView):
    def _tenant(self):
        return tenant_for_user(self.request, self.kwargs["tenant_id"])

    def get(self, request, tenant_id, section):
        if section not in TENANT_SECTIONS:
            raise Http404
        tenant = self._tenant()
        row = TenantSetting.objects.filter(tenant=tenant, section=section).first()
        stored = row.values if row else {}
        return Response(
            _settings_payload(
                "tenant", section, stored, SECTION_DEFAULTS[section], row
            )
        )

    def patch(self, request, tenant_id, section):
        if section not in TENANT_SECTIONS:
            raise Http404
        tenant = self._tenant()
        if not require_tenant_admin(request, tenant):
            return _error(
                "FORBIDDEN",
                "Tenant administrator permission required.",
                {},
                status.HTTP_403_FORBIDDEN,
            )
        values = request.data.get("values", request.data)
        cleaned, error = validate_values(SECTION_SCHEMAS[section], values)
        if error is not None:
            return _error(
                error["code"], error["message"], error["details"], status.HTTP_400_BAD_REQUEST
            )
        row, _ = TenantSetting.objects.select_for_update().get_or_create(
            tenant=tenant,
            section=section,
            defaults={"values": {}, "updated_by": request.user},
        )
        row.values = {**row.values, **cleaned}
        row.updated_by = request.user
        row.save(update_fields=["values", "updated_at", "updated_by"])
        record_audit_event(
            request,
            tenant,
            "settings.tenant.updated",
            "tenant_setting",
            str(row.id),
            metadata={"section": section, "changed_keys": sorted(cleaned)},
        )
        return Response(
            _settings_payload(
                "tenant", section, row.values, SECTION_DEFAULTS[section], row
            )
        )


class ProjectSettingsView(SettingsBaseView):
    def _scope(self):
        tenant = tenant_for_user(self.request, self.kwargs["tenant_id"])
        project = project_for_user(self.request, tenant, self.kwargs["project_id"])
        return tenant, project

    def get(self, request, tenant_id, project_id, section):
        if section not in TENANT_SECTIONS:
            raise Http404
        tenant, project = self._scope()
        tenant_row = TenantSetting.objects.filter(tenant=tenant, section=section).first()
        project_row = ProjectSetting.objects.filter(project=project, section=section).first()
        tenant_values = tenant_row.values if tenant_row else {}
        project_values = project_row.values if project_row else {}
        defaults = SECTION_DEFAULTS[section]
        effective, sources = _fold(
            defaults, ("tenant", tenant_values), ("project", project_values)
        )
        return Response(
            {
                "section": section,
                "scope": "project",
                "values": project_values,
                "effective": effective,
                "defaults": defaults,
                "sources": sources,
                "updated_at": project_row.updated_at if project_row else None,
            }
        )

    def patch(self, request, tenant_id, project_id, section):
        if section not in TENANT_SECTIONS:
            raise Http404
        tenant, project = self._scope()
        if not require_project_admin(request, tenant, project):
            return _error(
                "FORBIDDEN",
                "Project administrator permission required.",
                {},
                status.HTTP_403_FORBIDDEN,
            )
        values = request.data.get("values", request.data)
        cleaned, error = validate_values(SECTION_SCHEMAS[section], values)
        if error is not None:
            return _error(
                error["code"], error["message"], error["details"], status.HTTP_400_BAD_REQUEST
            )
        row, _ = ProjectSetting.objects.select_for_update().get_or_create(
            project=project,
            section=section,
            defaults={"tenant": tenant, "values": {}, "updated_by": request.user},
        )
        row.values = {**row.values, **cleaned}
        row.updated_by = request.user
        row.save(update_fields=["values", "updated_at", "updated_by"])
        record_audit_event(
            request,
            tenant,
            "settings.project.updated",
            "project_setting",
            str(row.id),
            project=project,
            metadata={"section": section, "changed_keys": sorted(cleaned)},
        )
        tenant_row = TenantSetting.objects.filter(tenant=tenant, section=section).first()
        tenant_values = tenant_row.values if tenant_row else {}
        defaults = SECTION_DEFAULTS[section]
        effective, sources = _fold(
            defaults, ("tenant", tenant_values), ("project", row.values)
        )
        return Response(
            {
                "section": section,
                "scope": "project",
                "values": row.values,
                "effective": effective,
                "defaults": defaults,
                "sources": sources,
                "updated_at": row.updated_at,
            }
        )


def _shortcut_defaults() -> dict[str, str]:
    return {entry["id"]: entry["default_combination"] for entry in SHORTCUT_REGISTRY}


def _validate_shortcuts(values: object, existing: dict[str, object]):
    if not isinstance(values, dict):
        return None, {
            "code": "SETTINGS_INVALID_VALUE",
            "message": "Values must be an object.",
            "details": {"values": ["must be an object"]},
        }
    unknown: dict[str, list[str]] = {}
    invalid: dict[str, list[str]] = {}
    cleaned: dict[str, str] = {}
    for shortcut_id, combination in values.items():
        if shortcut_id not in SHORTCUT_IDS:
            unknown[shortcut_id] = ["unknown shortcut id"]
            continue
        problem = validate_combination(combination)
        if problem is not None:
            invalid[shortcut_id] = [problem]
        else:
            cleaned[shortcut_id] = combination
    if unknown:
        return None, {
            "code": "SETTINGS_UNKNOWN_KEY",
            "message": "One or more shortcut ids are not recognized.",
            "details": unknown,
        }
    if invalid:
        return None, {
            "code": "SHORTCUT_INVALID_COMBINATION",
            "message": "One or more shortcut combinations are invalid.",
            "details": invalid,
        }
    merged = {**existing, **cleaned}
    seen: dict[str, str] = {}
    conflicts: dict[str, list[str]] = {}
    for shortcut_id, combination in merged.items():
        normalized = normalize_combination(str(combination))
        if normalized in seen:
            conflicts.setdefault(shortcut_id, []).append(
                f"conflicts with {seen[normalized]}"
            )
            conflicts.setdefault(seen[normalized], []).append(
                f"conflicts with {shortcut_id}"
            )
        else:
            seen[normalized] = shortcut_id
    if conflicts:
        return None, {
            "code": "SHORTCUT_CONFLICT",
            "message": "Two shortcuts are bound to the same combination.",
            "details": conflicts,
        }
    return cleaned, None


class UserPreferenceView(SettingsBaseView):
    def _namespace(self, namespace: str):
        if namespace not in PREFERENCE_NAMESPACES:
            raise Http404
        return namespace

    def _defaults(self, namespace: str) -> dict[str, object]:
        if namespace == "shortcuts":
            return dict(_shortcut_defaults())
        return dict(NAMESPACE_DEFAULTS[namespace])

    def get(self, request, namespace):
        self._namespace(namespace)
        row = UserPreference.objects.filter(user=request.user, namespace=namespace).first()
        stored = row.values if row else {}
        defaults = self._defaults(namespace)
        effective, sources = _fold(defaults, ("user", stored))
        return Response(
            {
                "namespace": namespace,
                "scope": "user",
                "values": stored,
                "effective": effective,
                "defaults": defaults,
                "sources": sources,
                "updated_at": row.updated_at if row else None,
            }
        )

    def _update(self, request, namespace, replace: bool):
        self._namespace(namespace)
        row, _ = UserPreference.objects.select_for_update().get_or_create(
            user=request.user,
            namespace=namespace,
            defaults={"values": {}, "updated_by": request.user},
        )
        values = request.data.get("values", request.data)
        if namespace == "shortcuts":
            cleaned, error = _validate_shortcuts(values, row.values if replace else {})
        else:
            cleaned, error = validate_values(NAMESPACE_SCHEMAS[namespace], values)
        if error is not None:
            status_code = (
                status.HTTP_409_CONFLICT
                if error["code"] == "SHORTCUT_CONFLICT"
                else status.HTTP_400_BAD_REQUEST
            )
            return _error(error["code"], error["message"], error["details"], status_code)
        merged = cleaned if replace else {**row.values, **cleaned}
        row.values = merged
        row.updated_by = request.user
        row.save(update_fields=["values", "updated_at", "updated_by"])
        record_audit_event(
            request,
            None,
            "preference.updated",
            "user_preference",
            str(row.id),
            metadata={"namespace": namespace, "changed_keys": sorted(cleaned)},
        )
        defaults = self._defaults(namespace)
        effective, sources = _fold(defaults, ("user", row.values))
        return Response(
            {
                "namespace": namespace,
                "scope": "user",
                "values": row.values,
                "effective": effective,
                "defaults": defaults,
                "sources": sources,
                "updated_at": row.updated_at,
            }
        )

    def put(self, request, namespace):
        return self._update(request, namespace, replace=True)

    def patch(self, request, namespace):
        return self._update(request, namespace, replace=False)


class ShortcutRegistryView(SettingsBaseView):
    def get(self, request):
        return Response({"results": SHORTCUT_REGISTRY})


class AboutView(SettingsBaseView):
    def get(self, request):
        from packages.version import project_version

        return Response(
            {
                "version": project_version(),
                "build": {
                    "commit": os.environ.get("GIT_COMMIT", "")
                    or os.environ.get("CI_COMMIT_SHA", ""),
                    "built_at": os.environ.get("BUILD_DATE", ""),
                },
                "licenses": {
                    "file": "LICENSE",
                    "notice": "Third-party licenses are listed in the repository LICENSE file.",
                },
            }
        )


def _feedback_payload(entry: FeedbackEntry) -> dict:
    return {
        "id": str(entry.id),
        "category": entry.category,
        "severity": entry.severity,
        "body": entry.body,
        "created_at": entry.created_at,
    }


class FeedbackListCreateView(SettingsBaseView):
    def get(self, request, tenant_id):
        tenant = tenant_for_user(request, tenant_id)
        entries = FeedbackEntry.objects.filter(tenant=tenant).select_related("user")
        return Response({"results": [_feedback_payload(entry) for entry in entries]})

    def post(self, request, tenant_id):
        tenant = tenant_for_user(request, tenant_id)
        category = request.data.get("category")
        severity = request.data.get("severity")
        body = request.data.get("body")
        details: dict[str, list[str]] = {}
        if category not in FeedbackEntry.Category.values:
            details["category"] = ["must be one of: bug, feature, usability, other"]
        if severity not in FeedbackEntry.Severity.values:
            details["severity"] = ["must be one of: low, medium, high"]
        if not isinstance(body, str) or not body.strip():
            details["body"] = ["must be a non-empty string"]
        if details:
            return _error(
                "FEEDBACK_INVALID",
                "Feedback payload is invalid.",
                details,
                status.HTTP_400_BAD_REQUEST,
            )
        entry = FeedbackEntry.objects.create(
            tenant=tenant,
            user=request.user,
            category=category,
            severity=severity,
            body=body.strip(),
        )
        record_audit_event(
            request,
            tenant,
            "feedback.created",
            "feedback",
            str(entry.id),
            metadata={"category": category, "severity": severity},
        )
        return Response(_feedback_payload(entry), status=status.HTTP_201_CREATED)


def _profile_payload(user, profile: UserAccountProfile) -> dict:
    return {
        "id": str(user.pk),
        "username": user.get_username(),
        "display_name": profile.display_name,
        "email": user.email,
        "timezone": profile.timezone,
        "locale": profile.locale,
    }


class AccountProfileView(SettingsBaseView):
    def _profile(self):
        profile, _ = UserAccountProfile.objects.select_for_update().get_or_create(
            user=self.request.user
        )
        return profile

    def get(self, request):
        return Response(_profile_payload(request.user, self._profile()))

    def patch(self, request):
        profile = self._profile()
        allowed = {"display_name", "email", "timezone", "locale"}
        updates = {key: value for key, value in request.data.items() if key in allowed}
        if not updates:
            return _error(
                "ACCOUNT_NO_FIELDS",
                "No updatable fields supplied.",
                {},
                status.HTTP_400_BAD_REQUEST,
            )
        user = request.user
        if "email" in updates:
            email = updates["email"]
            if not isinstance(email, str) or "@" not in email.strip():
                return _error(
                    "ACCOUNT_INVALID_EMAIL",
                    "A valid email address is required.",
                    {"email": ["must be a valid email address"]},
                    status.HTTP_400_BAD_REQUEST,
                )
            normalized = email.strip().lower()
            if (
                get_user_model()
                .objects.filter(email__iexact=normalized)
                .exclude(pk=user.pk)
                .exists()
            ):
                return _error(
                    "ACCOUNT_EMAIL_TAKEN",
                    "This email address is already in use.",
                    {"email": ["already taken"]},
                    status.HTTP_409_CONFLICT,
                )
            user.email = normalized
        if "display_name" in updates:
            profile.display_name = str(updates["display_name"])[:160]
        if "timezone" in updates:
            timezone_value = str(updates["timezone"])[:64]
            if not timezone_value.strip():
                return _error(
                    "ACCOUNT_INVALID_VALUE",
                    "Timezone must be a non-empty string.",
                    {"timezone": ["must be a non-empty string"]},
                    status.HTTP_400_BAD_REQUEST,
                )
            profile.timezone = timezone_value
        if "locale" in updates:
            locale_value = str(updates["locale"])[:16]
            if not locale_value.strip():
                return _error(
                    "ACCOUNT_INVALID_VALUE",
                    "Locale must be a non-empty string.",
                    {"locale": ["must be a non-empty string"]},
                    status.HTTP_400_BAD_REQUEST,
                )
            profile.locale = locale_value
        profile.updated_by = user
        with transaction.atomic():
            if "email" in updates:
                user.save(update_fields=["email"])
            profile.save()
        record_audit_event(
            request,
            None,
            "account.profile.updated",
            "user_profile",
            str(profile.pk),
            metadata={"fields": sorted(updates)},
        )
        return Response(_profile_payload(user, profile))


class AccountIdentitiesView(SettingsBaseView):
    def get(self, request):
        user = request.user
        has_password = user.has_usable_password()
        return Response(
            {
                "has_password": has_password,
                "methods": ["password"] if has_password else [],
                "bound_identities": 0,
                "providers": [],
            }
        )


class AccountSessionsView(SettingsBaseView):
    @staticmethod
    def _user_sessions(user):
        from django.contrib.sessions.models import Session

        user_id = str(user.pk)
        for session in Session.objects.all().iterator():
            try:
                data = session.get_decoded()
            except Exception:
                continue
            if str(data.get("_auth_user_id")) == user_id:
                yield session

    def get(self, request):
        current_key = request.session.session_key
        results = []
        for session in self._user_sessions(request.user):
            results.append(
                {
                    "session_key": session.session_key,
                    "is_current": session.session_key == current_key,
                    "expire_date": session.expire_date,
                    "created_at": None,
                    "last_activity": None,
                }
            )
        return Response({"results": results})


class AccountSessionRevokeView(SettingsBaseView):
    def post(self, request, session_key):
        from django.contrib.sessions.models import Session

        user_id = str(request.user.pk)
        session = Session.objects.filter(pk=session_key).first()
        revoked = False
        if session is not None:
            try:
                data = session.get_decoded()
            except Exception:
                data = {}
            if str(data.get("_auth_user_id")) == user_id:
                session.delete()
                revoked = True
                record_audit_event(
                    request,
                    None,
                    "session.revoked",
                    "session",
                    session_key,
                    metadata={
                        "current": session.session_key == request.session.session_key
                    },
                )
        return Response({"revoked": revoked, "idempotent": True})
