"""HTTP surface for the account-authorization flow."""

from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import provider_accounts
from core.model_serializers import ModelProviderReadSerializer
from core.model_views import get_tenant
from core.provider_oauth import OAuthFlowError, complete_flow, start_flow


class ProviderOAuthStartView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, preset_key):
        tenant = get_tenant(request, tenant_id)
        redirect_uri = (request.data.get("redirect_uri") or "").strip()
        try:
            attempt = start_flow(
                user=request.user,
                tenant=tenant,
                preset_key=preset_key,
                redirect_uri=redirect_uri,
            )
        except OAuthFlowError as exc:
            return Response(
                {"code": exc.code, "detail": str(exc)},
                status=exc.http_status,
            )
        return Response(
            {"authorization_url": attempt.authorization_url, "state": attempt.state},
            status=status.HTTP_201_CREATED,
        )


class ProviderOAuthExchangeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, preset_key):
        tenant = get_tenant(request, tenant_id)
        try:
            provider, link = complete_flow(
                user=request.user,
                tenant=tenant,
                preset_key=preset_key,
                code=request.data.get("code") or "",
                state=request.data.get("state") or "",
            )
        except OAuthFlowError as exc:
            return Response({"code": exc.code, "detail": str(exc)}, status=exc.http_status)
        except provider_accounts.ProviderAccountError as exc:
            return Response({"code": exc.code, "detail": str(exc)}, status=exc.http_status)
        return Response(
            {
                "provider": ModelProviderReadSerializer(
                    provider, context={"account_user": request.user}
                ).data,
                "account": {
                    "linked": True,
                    "account_label": link.account_label,
                    "linked_at": link.linked_at,
                },
            },
            status=status.HTTP_201_CREATED,
        )
