"""DingTalk, WeCom and Feishu group robots receive tenant notifications."""

import base64
import hashlib
import hmac
import json

import httpx
import pytest
from core.models import Tenant, TenantMembership
from core.notification_channels import build_request, deliver, dingtalk_sign, feishu_sign
from core.settings_models import NotificationEndpoint
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

DING_URL = "https://oapi.dingtalk.com/robot/send?access_token=abc"
WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xyz"
FEISHU_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/token"


@pytest.fixture
def tenant_admin(db):
    user = get_user_model().objects.create_user(username="im-admin", password="pw-im-admin")
    tenant = Tenant.objects.create(name="IM tenant", slug="im-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    return user, tenant


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _create(client, tenant, channel, name, url, secret=""):
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/settings/notifications/endpoints/",
        {"channel": channel, "name": name, "webhook_url": url, "secret": secret},
        format="json",
    )
    assert response.status_code == 201, response.content
    return response.json()


@pytest.mark.django_db
def test_endpoints_store_the_url_sealed(tenant_admin):
    user, tenant = tenant_admin
    client = _client(user)
    payload = _create(client, tenant, "dingtalk", "Ops robot", DING_URL, "SEC123")
    assert "webhook_url" not in payload
    record = NotificationEndpoint.objects.get(pk=payload["id"])
    assert DING_URL not in record.sealed_url
    listed = client.get(f"/api/v1/tenants/{tenant.id}/settings/notifications/endpoints/").json()
    assert listed["results"][0]["channel"] == "dingtalk"


@pytest.mark.django_db
def test_wecom_payload_carries_no_signature(tenant_admin):
    user, tenant = tenant_admin
    client = _client(user)
    endpoint_id = _create(client, tenant, "wecom", "WeCom group", WECOM_URL)["id"]
    endpoint = NotificationEndpoint.objects.get(pk=endpoint_id)
    url, payload = build_request(endpoint, "Run failed", "agent-run-42 failed")
    assert url == WECOM_URL
    assert payload["msgtype"] == "markdown"
    assert "Run failed" in payload["markdown"]["content"]


@pytest.mark.django_db
def test_dingtalk_request_is_signed_and_verifiable(tenant_admin):
    user, tenant = tenant_admin
    client = _client(user)
    endpoint_id = _create(client, tenant, "dingtalk", "Ops robot", DING_URL, "SEC123")["id"]
    endpoint = NotificationEndpoint.objects.get(pk=endpoint_id)
    url, payload = build_request(endpoint, "Budget", "80% used", timestamp_ms="1700000000000")
    assert "timestamp=1700000000000" in url and "sign=" in url
    sign = url.split("sign=")[-1]
    expected = base64.b64encode(
        hmac.new(
            b"SEC123",
            b"1700000000000\nSEC123",
            hashlib.sha256,
        ).digest()
    ).decode()
    assert sign == expected
    assert dingtalk_sign("SEC123", "1700000000000") == expected
    assert payload["msgtype"] == "markdown"


@pytest.mark.django_db
def test_feishu_request_is_signed_and_verifiable(tenant_admin):
    user, tenant = tenant_admin
    client = _client(user)
    endpoint_id = _create(client, tenant, "feishu", "Feishu group", FEISHU_URL, "FE123")["id"]
    endpoint = NotificationEndpoint.objects.get(pk=endpoint_id)
    url, payload = build_request(endpoint, "Approval", "cluster.provision waits")
    assert url == FEISHU_URL
    timestamp = payload["timestamp"]
    expected = base64.b64encode(
        hmac.new(f"{timestamp}\nFE123".encode(), b"", hashlib.sha256).digest()
    ).decode()
    assert payload["sign"] == expected
    assert feishu_sign("FE123", timestamp) == expected


@pytest.mark.django_db
def test_delivery_reports_upstream_rejection(tenant_admin):
    user, tenant = tenant_admin
    client = _client(user)
    endpoint_id = _create(client, tenant, "wecom", "WeCom group", WECOM_URL)["id"]
    endpoint = NotificationEndpoint.objects.get(pk=endpoint_id)

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["msgtype"] == "markdown"
        return httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid webhook url"})

    result = deliver(endpoint, "t", "b", transport=httpx.MockTransport(handler))
    assert result.ok is False
    assert "invalid webhook url" in result.message


@pytest.mark.django_db
def test_test_endpoint_records_a_failed_delivery(tenant_admin, monkeypatch):
    user, tenant = tenant_admin
    client = _client(user)
    endpoint_id = _create(client, tenant, "feishu", "Feishu group", FEISHU_URL)["id"]

    from core.notification_channels import DeliveryResult
    from core.notification_views import NotificationEndpointTestView

    monkeypatch.setattr(
        "core.notification_views.deliver",
        lambda endpoint, title, body: DeliveryResult(False, "sign match fail"),
    )
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/settings/notifications/endpoints/{endpoint_id}/test/"
    )
    assert response.status_code == 502
    assert "sign match fail" in response.json()["message"]
    assert NotificationEndpoint.objects.get(pk=endpoint_id).last_error == "sign match fail"
    assert NotificationEndpointTestView is not None


@pytest.mark.django_db
def test_member_cannot_create_endpoints(tenant_admin):
    user, tenant = tenant_admin
    member = get_user_model().objects.create_user(username="im-member", password="pw-im-member")
    TenantMembership.objects.create(tenant=tenant, user=member, role="member")
    response = _client(member).post(
        f"/api/v1/tenants/{tenant.id}/settings/notifications/endpoints/",
        {"channel": "wecom", "name": "n", "webhook_url": WECOM_URL},
        format="json",
    )
    assert response.status_code == 403
