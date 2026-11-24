import uuid

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import provider_accounts
from core.api_errors import legacy_error_response
from core.audit import record_audit_event
from core.model_gateway import ModelGateway, ModelGatewayError
from core.model_serializers import (
    ModelConfigSerializer,
    ModelProviderReadSerializer,
    ModelProviderWriteSerializer,
)
from core.models import (
    AuditEvent,
    ModelConfig,
    ModelProvider,
    Tenant,
    TenantMembership,
)
from core.provider_accounts import AccountTokenGuard, ProviderAccountError
from core.token_budget import TenantTokenBudgetExceeded
from core.token_guard import AdHocTokenGuard


def get_tenant(request, tenant_id):
    return get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
    )


def is_tenant_admin(request, tenant):
    return TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()


class ModelProviderListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id):
        tenant = get_tenant(request, tenant_id)
        providers = ModelProvider.objects.filter(
            Q(tenant=tenant) | Q(tenant__isnull=True),
            enabled=True,
        )
        return Response(
            ModelProviderReadSerializer(
                providers,
                many=True,
                context={"account_user": request.user},
            ).data
        )

    def post(self, request, tenant_id):
        tenant = get_tenant(request, tenant_id)
        serializer = ModelProviderWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account_mode = (
            serializer.validated_data.get("auth_mode") == ModelProvider.AuthMode.USER_ACCOUNT
        )
        # Attaching your own account buys nothing from the tenant, so any
        # member may do it; only a paid provider needs an administrator.
        if not account_mode and not is_tenant_admin(request, tenant):
            return legacy_error_response(
                "Tenant administrator permission required.",
                http_status=status.HTTP_403_FORBIDDEN,
                code="TENANT_ADMIN_REQUIRED",
            )
        name = serializer.validated_data["name"]
        if ModelProvider.objects.filter(tenant=tenant, name=name).exists():
            if not account_mode:
                return legacy_error_response(
                    "A model provider with this name already exists.",
                    http_status=status.HTTP_409_CONFLICT,
                    code="MODEL_PROVIDER_NAME_TAKEN",
                )
            # Two members may both add "My OpenAI"; keep the shared namespace
            # unique by qualifying the later one with its owner.
            base = f"{name} ({request.user.username})"
            candidate, suffix = base, 2
            while ModelProvider.objects.filter(tenant=tenant, name=candidate).exists():
                candidate = f"{base} {suffix}"
                suffix += 1
            serializer.validated_data["name"] = candidate
        with transaction.atomic():
            provider = serializer.save(tenant=tenant, created_by=request.user)
            if provider.auth_mode == ModelProvider.AuthMode.USER_ACCOUNT:
                _seed_preset_models(provider)
        record_audit_event(
            request,
            tenant,
            "model.provider.created",
            "model_provider",
            provider.id,
            metadata={"name": provider.name, "auth_mode": provider.auth_mode},
        )
        return Response(
            ModelProviderReadSerializer(
                provider,
                context={"account_user": request.user},
            ).data,
            status=status.HTTP_201_CREATED,
        )


def _seed_preset_models(provider):
    """Publish the preset's models so a linked member has something to call."""

    preset = provider_accounts.PROVIDER_PRESETS.get(provider.preset_key) or {"models": []}
    for item in preset["models"]:
        ModelConfig.objects.get_or_create(
            provider=provider,
            model_name=item["model_name"],
            version=1,
            defaults={
                "display_name": item["display_name"],
                "capabilities": ["chat", "tools"],
                "input_cost_per_million": 0,
                "output_cost_per_million": 0,
            },
        )


class ModelConfigListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get_provider(self, request, tenant, provider_id):
        return get_object_or_404(
            ModelProvider.objects.filter(
                Q(tenant=tenant) | Q(tenant__isnull=True),
                enabled=True,
            ),
            pk=provider_id,
        )

    def get(self, request, tenant_id, provider_id):
        tenant = get_tenant(request, tenant_id)
        provider = self.get_provider(request, tenant, provider_id)
        configs = ModelConfig.objects.filter(provider=provider, enabled=True)
        return Response(ModelConfigSerializer(configs, many=True).data)

    def post(self, request, tenant_id, provider_id):
        tenant = get_tenant(request, tenant_id)
        if not is_tenant_admin(request, tenant):
            return Response(
                {"detail": "Tenant administrator permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        with transaction.atomic():
            provider = self.get_provider(request, tenant, provider_id)
            if provider.tenant_id != tenant.id:
                return Response(
                    {"detail": "Only tenant-owned providers can be modified by tenant admins."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            provider = ModelProvider.objects.select_for_update().get(pk=provider.pk)
            serializer = ModelConfigSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            latest_version = (
                ModelConfig.objects.filter(
                    provider=provider,
                    model_name=serializer.validated_data["model_name"],
                )
                .order_by("-version")
                .values_list("version", flat=True)
                .first()
            )
            model_config = serializer.save(
                provider=provider,
                version=(latest_version or 0) + 1,
            )
        record_audit_event(
            request,
            tenant,
            "model.config.created",
            "model_config",
            model_config.id,
            metadata={
                "provider_id": str(provider.id),
                "model_name": model_config.model_name,
                "version": model_config.version,
            },
        )
        return Response(
            ModelConfigSerializer(model_config).data,
            status=status.HTTP_201_CREATED,
        )


class ModelTestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, model_config_id):
        tenant = get_tenant(request, tenant_id)
        model_config = get_object_or_404(
            ModelConfig.objects.select_related("provider").filter(
                Q(provider__tenant=tenant) | Q(provider__tenant__isnull=True),
                provider__enabled=True,
                enabled=True,
            ),
            pk=model_config_id,
        )
        provider = model_config.provider
        if provider.auth_mode == ModelProvider.AuthMode.USER_ACCOUNT:
            return _test_with_linked_account(request, tenant, model_config)
        if not is_tenant_admin(request, tenant):
            return legacy_error_response(
                "Tenant administrator permission required.",
                http_status=status.HTTP_403_FORBIDDEN,
                code="TENANT_ADMIN_REQUIRED",
            )
        messages = request.data.get(
            "messages",
            [{"role": "user", "content": "Reply with a short connection test."}],
        )
        request_id = str(request.headers.get("X-Request-ID") or uuid.uuid4().hex)[:160]
        guard = AdHocTokenGuard(tenant=tenant, scope_ref=f"model-test:{model_config.id}")
        try:
            result = ModelGateway(guard=guard).complete(
                model_config,
                messages,
                idempotency_key=f"model-test:{request_id}",
            )
        except TenantTokenBudgetExceeded as exc:
            record_audit_event(
                request,
                tenant,
                "model.config.tested",
                "model_config",
                model_config.id,
                outcome=AuditEvent.Outcome.FAILURE,
                metadata={"error_code": "TENANT_TOKEN_BUDGET_EXCEEDED", "period": exc.period},
            )
            return Response(
                {
                    "error": {
                        "code": "TENANT_TOKEN_BUDGET_EXCEEDED",
                        "message": f"Tenant {exc.period} token budget would be exceeded.",
                        "details": {"period": exc.period},
                    }
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except ModelGatewayError as exc:
            record_audit_event(
                request,
                tenant,
                "model.config.tested",
                "model_config",
                model_config.id,
                outcome=AuditEvent.Outcome.FAILURE,
                metadata={"error_code": exc.code, "retryable": exc.retryable},
            )
            return Response(
                {
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "retryable": exc.retryable,
                    }
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        record_audit_event(
            request,
            tenant,
            "model.config.tested",
            "model_config",
            model_config.id,
            metadata={
                "provider_id": str(model_config.provider_id),
                "model_name": model_config.model_name,
            },
        )
        return Response(result.as_dict())


class ModelProviderCatalogView(APIView):
    """Model platforms a member can attach their own account to.

    A member who has not purchased any balance still gets the free allowance,
    so the catalogue and the allowance are readable by every tenant member
    rather than only by administrators who configure paid providers.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id):
        get_tenant(request, tenant_id)
        return Response(
            {
                "presets": provider_accounts.catalogue(),
                "free_daily_tokens": provider_accounts.free_daily_tokens(),
            }
        )


class ModelProviderAccountView(APIView):
    """Link or revoke the caller's own account on an account-billed provider."""

    permission_classes = [IsAuthenticated]

    def _provider(self, request, tenant_id, provider_id):
        tenant = get_tenant(request, tenant_id)
        return get_object_or_404(
            ModelProvider.objects.filter(
                Q(tenant=tenant) | Q(tenant__isnull=True),
                enabled=True,
            ),
            pk=provider_id,
        )

    def get(self, request, tenant_id, provider_id):
        provider = self._provider(request, tenant_id, provider_id)
        link = provider_accounts.active_link(request.user, provider)
        payload = provider_accounts.describe_account(link)
        payload.update(provider_accounts.quota_snapshot(request.user, provider))
        payload["auth_mode"] = provider.auth_mode
        return Response(payload)

    def post(self, request, tenant_id, provider_id):
        provider = self._provider(request, tenant_id, provider_id)
        credential_ref = request.data.get("credential_ref", "")
        account_label = request.data.get("account_label", "")
        if not isinstance(credential_ref, str) or not isinstance(account_label, str):
            return legacy_error_response(
                "credential_ref and account_label must be strings.",
                http_status=400,
                code="PROVIDER_ACCOUNT_INPUT_INVALID",
            )
        try:
            link = provider_accounts.link_account(
                user=request.user,
                provider=provider,
                credential_ref=credential_ref,
                account_label=account_label,
            )
        except ProviderAccountError as exc:
            return legacy_error_response(str(exc), http_status=exc.http_status, code=exc.code)
        record_audit_event(
            request,
            provider.tenant or get_tenant(request, tenant_id),
            "model.provider.account.linked",
            "model_provider",
            provider.id,
            metadata={"account_label": link.account_label, "auth_mode": provider.auth_mode},
        )
        payload = provider_accounts.describe_account(link)
        payload.update(provider_accounts.quota_snapshot(request.user, provider))
        return Response(payload, status=status.HTTP_201_CREATED)

    def delete(self, request, tenant_id, provider_id):
        provider = self._provider(request, tenant_id, provider_id)
        try:
            link = provider_accounts.unlink_account(user=request.user, provider=provider)
        except ProviderAccountError as exc:
            return legacy_error_response(str(exc), http_status=exc.http_status, code=exc.code)
        record_audit_event(
            request,
            provider.tenant or get_tenant(request, tenant_id),
            "model.provider.account.unlinked",
            "model_provider",
            provider.id,
            metadata={"account_label": link.account_label},
        )
        payload = provider_accounts.describe_account(None)
        payload.update(provider_accounts.quota_snapshot(request.user, provider))
        return Response(payload)


def _test_with_linked_account(request, tenant, model_config):
    """Run a member's own-account model test against their free allowance.

    No tenant balance is involved, so the path skips the purchased-pool guard
    entirely: the linked account is the authority and the free allowance is
    the only limit. Members do not need an administrator role to try the
    account they just linked.
    """

    provider = model_config.provider
    link = provider_accounts.active_link(request.user, provider)
    if link is None:
        record_audit_event(
            request,
            tenant,
            "model.config.tested",
            "model_config",
            model_config.id,
            outcome=AuditEvent.Outcome.FAILURE,
            metadata={"error_code": "PROVIDER_ACCOUNT_NOT_LINKED"},
        )
        return legacy_error_response(
            "Link your own account on this platform before testing its models.",
            http_status=403,
            code="PROVIDER_ACCOUNT_NOT_LINKED",
        )
    messages = request.data.get(
        "messages",
        [{"role": "user", "content": "Reply with a short connection test."}],
    )
    request_id = str(request.headers.get("X-Request-ID") or uuid.uuid4().hex)[:160]
    try:
        result = ModelGateway(account=AccountTokenGuard(link=link)).complete(
            model_config,
            messages,
            idempotency_key=f"model-test:{request_id}",
        )
    except ProviderAccountError as exc:
        record_audit_event(
            request,
            tenant,
            "model.config.tested",
            "model_config",
            model_config.id,
            outcome=AuditEvent.Outcome.FAILURE,
            metadata={"error_code": exc.code},
        )
        return legacy_error_response(str(exc), http_status=exc.http_status, code=exc.code)
    except ModelGatewayError as exc:
        record_audit_event(
            request,
            tenant,
            "model.config.tested",
            "model_config",
            model_config.id,
            outcome=AuditEvent.Outcome.FAILURE,
            metadata={"error_code": exc.code, "retryable": exc.retryable},
        )
        return legacy_error_response(str(exc), http_status=502, code=exc.code)
    record_audit_event(
        request,
        tenant,
        "model.config.tested",
        "model_config",
        model_config.id,
        metadata={
            "provider_id": str(provider.id),
            "model_name": model_config.model_name,
            "billing": "linked_account_free_quota",
        },
    )
    payload = result.as_dict()
    payload["billing"] = "linked_account_free_quota"
    payload["free_quota"] = provider_accounts.quota_snapshot(request.user, provider)
    return Response(payload)
