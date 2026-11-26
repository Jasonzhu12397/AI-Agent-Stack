"""Cloud VM orchestration: Nova request shape and the console contract."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from core import cloud_vm
from core.cloud_vm import CloudVmError, OpenStackProvider, load_provider_configs
from core.models import (
    CloudVmInstance,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()

PROVIDER_CONFIG = {
    "key": "openstack-lab",
    "type": "openstack",
    "display_name": "OpenStack lab",
    "auth_url": "https://keystone.example.com/v3",
    "username": "nova-user",
    "password": "super-secret",
    "project_name": "platform",
    "domain_name": "Default",
    "region": "RegionOne",
    "verify_tls": True,
    "timeout": 20,
}
CATALOG = {
    "token": {
        "expires_at": "2030-01-01T00:00:00.000000Z",
        "catalog": [
            {
                "type": "compute",
                "endpoints": [
                    {
                        "interface": "public",
                        "region_id": "RegionOne",
                        "url": "https://nova.example.com/v2.1",
                    }
                ],
            }
        ],
    }
}


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class RecordingSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected provider call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


def test_provider_config_is_validated_and_secrets_are_not_public(monkeypatch):
    monkeypatch.setenv(
        cloud_vm.PROVIDER_ENV, json.dumps([{**PROVIDER_CONFIG, "password": "super-secret"}])
    )
    configs = load_provider_configs()
    assert configs["openstack-lab"]["password"] == "super-secret"
    assert cloud_vm.public_providers() == [
        {
            "key": "openstack-lab",
            "type": "openstack",
            "display_name": "OpenStack lab",
            "region": "RegionOne",
        }
    ]
    assert "super-secret" not in json.dumps(cloud_vm.public_providers())


def test_provider_config_rejects_insecure_tls_without_the_override(monkeypatch):
    monkeypatch.setenv(
        cloud_vm.PROVIDER_ENV, json.dumps([{**PROVIDER_CONFIG, "verify_tls": False}])
    )
    monkeypatch.delenv(cloud_vm.ALLOW_INSECURE_ENV, raising=False)
    with pytest.raises(CloudVmError) as caught:
        load_provider_configs()
    assert caught.value.code == "CLOUD_VM_PROVIDER_CONFIG_INVALID"


def test_create_server_uses_keystone_catalog_and_nova_payload():
    session = RecordingSession(
        [
            FakeResponse(headers={"X-Subject-Token": "token-1"}, payload=CATALOG),
            FakeResponse(
                status=202,
                payload={
                    "server": {
                        "id": "server-1",
                        "name": "worker-a",
                        "status": "BUILD",
                        "addresses": {
                            "private": [
                                {"version": 4, "addr": "10.0.0.9", "OS-EXT-IPS:type": "fixed"}
                            ]
                        },
                    }
                },
            ),
        ]
    )
    provider = OpenStackProvider(PROVIDER_CONFIG, session=session)
    server = provider.create_server(
        name="worker-a",
        flavor_ref="flavor-1",
        image_ref="image-1",
        network_ids=["network-1"],
    )
    assert server["server_id"] == "server-1"
    assert server["status"] == "BUILD"
    assert server["ip_address"] == "10.0.0.9"
    token_call, create_call = session.calls
    assert token_call["url"] == "https://keystone.example.com/v3/auth/tokens"
    assert token_call["json"]["auth"]["scope"]["project"]["name"] == "platform"
    assert create_call["url"] == "https://nova.example.com/v2.1/servers"
    assert create_call["headers"]["X-Auth-Token"] == "token-1"
    assert create_call["json"] == {
        "server": {
            "name": "worker-a",
            "flavorRef": "flavor-1",
            "imageRef": "image-1",
            "networks": [{"uuid": "network-1"}],
        }
    }


def test_provider_conflict_maps_to_a_409():
    session = RecordingSession(
        [
            FakeResponse(headers={"X-Subject-Token": "token-1"}, payload=CATALOG),
            FakeResponse(status=409, payload={"error": {"message": "Flavor is exhausted"}}),
        ]
    )
    provider = OpenStackProvider(PROVIDER_CONFIG, session=session)
    with pytest.raises(CloudVmError) as caught:
        provider.create_server(
            name="worker-a", flavor_ref="flavor-1", image_ref="image-1", network_ids=["n-1"]
        )
    assert caught.value.code == "CLOUD_VM_CONFLICT"
    assert caught.value.http_status == 409


def test_standard_library_transport_reaches_a_live_http_endpoint():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status, payload, headers=None):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            seen.append((self.path, dict(self.headers), payload))
            if self.path == "/auth/tokens":
                self._reply(
                    201,
                    {"token": {"expires_at": "2030-01-01T00:00:00Z"}},
                    {"X-Subject-Token": "token-live"},
                )
            elif self.path == "/v2.1/servers":
                self._reply(
                    202,
                    {
                        "server": {
                            "id": "server-live",
                            "name": "worker-live",
                            "status": "BUILD",
                            "addresses": {},
                        }
                    },
                )
            else:
                self._reply(404, {"error": {"message": "not found"}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        config = {
            **PROVIDER_CONFIG,
            "auth_url": f"http://127.0.0.1:{port}",
            "compute_url": f"http://127.0.0.1:{port}/v2.1",
            "verify_tls": True,
        }
        provider = OpenStackProvider(config)
        created = provider.create_server(
            name="worker-live",
            flavor_ref="flavor-1",
            image_ref="image-1",
            network_ids=["network-1"],
        )
        provider.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert created["server_id"] == "server-live"
    assert [path for path, _headers, _body in seen] == ["/auth/tokens", "/v2.1/servers"]
    assert seen[1][1]["X-Auth-Token"] == "token-live"
    assert seen[1][2]["server"]["networks"] == [{"uuid": "network-1"}]


@pytest.fixture
def workspace(db, monkeypatch):
    monkeypatch.setenv(cloud_vm.PROVIDER_ENV, json.dumps([PROVIDER_CONFIG]))
    user = User.objects.create_user(username="cloud-owner", password="pw")
    tenant = Tenant.objects.create(name="Cloud tenant", slug="cloud-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="Cloud project", slug="cloud-project")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


class FakeProvider:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.actions = []
        self.servers = []

    def create_server(self, *, name, flavor_ref, image_ref, network_ids):
        self.created.append(
            {
                "name": name,
                "flavor_ref": flavor_ref,
                "image_ref": image_ref,
                "network_ids": network_ids,
            }
        )
        return {
            "server_id": "server-1",
            "name": name,
            "status": "BUILD",
            "ip_address": "10.0.0.9",
            "metadata": {"flavor_id": flavor_ref, "image_id": image_ref},
        }

    def list_servers(self):
        return self.servers

    def delete_server(self, server_id):
        self.deleted.append(server_id)

    def server_action(self, server_id, action):
        self.actions.append((server_id, action))

    def close(self):
        pass


def test_console_lists_configured_providers_without_secrets(workspace):
    _user, tenant, project, client = workspace
    response = client.get(f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cloud-vms/providers/")
    assert response.status_code == 200
    assert response.data == {
        "count": 1,
        "results": [
            {
                "key": "openstack-lab",
                "type": "openstack",
                "display_name": "OpenStack lab",
                "region": "RegionOne",
            }
        ],
    }
    assert "super-secret" not in json.dumps(response.data)


def test_console_creates_a_vm_and_reconciles_it(workspace, monkeypatch):
    user, tenant, project, client = workspace
    provider = FakeProvider()
    monkeypatch.setattr("core.cloud_vm_views.build_provider", lambda key: provider)
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cloud-vms/",
        {
            "provider": "openstack-lab",
            "name": "worker-a",
            "flavor_ref": "flavor-1",
            "image_ref": "image-1",
            "network_ids": ["network-1"],
        },
        format="json",
    )
    assert response.status_code == 201
    assert response.data["status"] == "BUILD"
    assert response.data["ip_address"] == "10.0.0.9"
    vm = CloudVmInstance.objects.get(pk=response.data["id"])
    assert vm.created_by == user
    assert provider.created == [
        {
            "name": "worker-a",
            "flavor_ref": "flavor-1",
            "image_ref": "image-1",
            "network_ids": ["network-1"],
        }
    ]
    provider.servers = [
        {
            "server_id": "server-1",
            "name": "worker-a",
            "status": "ACTIVE",
            "ip_address": "10.0.0.10",
            "metadata": {"created": "2030-01-01T00:00:00Z"},
        }
    ]
    refreshed = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cloud-vms/?refresh=1"
    )
    assert refreshed.status_code == 200
    assert refreshed.data["results"][0]["status"] == "ACTIVE"
    assert refreshed.data["results"][0]["ip_address"] == "10.0.0.10"
    assert refreshed.data["warnings"] == []


def test_console_keeps_last_known_state_when_the_provider_fails(workspace, monkeypatch):
    _user, tenant, project, client = workspace
    CloudVmInstance.objects.create(
        tenant=tenant,
        project=project,
        created_by=_user,
        provider_key="openstack-lab",
        server_id="server-1",
        name="worker-a",
        flavor_ref="flavor-1",
        image_ref="image-1",
        network_ids=["network-1"],
        status="ACTIVE",
        ip_address="10.0.0.9",
    )

    class BrokenProvider(FakeProvider):
        def list_servers(self):
            raise CloudVmError("CLOUD_VM_PROVIDER_UNAVAILABLE", "Provider is down.", 503)

    monkeypatch.setattr("core.cloud_vm_views.build_provider", lambda key: BrokenProvider())
    response = client.get(f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cloud-vms/?refresh=1")
    assert response.status_code == 200
    assert response.data["results"][0]["status"] == "ACTIVE"
    assert response.data["warnings"] == ["Provider is down."]


def test_console_power_action_and_delete(workspace, monkeypatch):
    user, tenant, project, client = workspace
    vm = CloudVmInstance.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        provider_key="openstack-lab",
        server_id="server-1",
        name="worker-a",
        flavor_ref="flavor-1",
        image_ref="image-1",
        network_ids=["network-1"],
        status="ACTIVE",
    )
    provider = FakeProvider()
    monkeypatch.setattr("core.cloud_vm_views.build_provider", lambda key: provider)
    action = client.post(
        f"/api/v1/cloud-vms/{vm.id}/actions/",
        {"action": "hard-reboot"},
        format="json",
    )
    assert action.status_code == 202
    assert action.data["status"] == "REBOOTING"
    assert provider.actions == [("server-1", "hard-reboot")]
    deleted = client.delete(f"/api/v1/cloud-vms/{vm.id}/")
    assert deleted.status_code == 202
    assert deleted.data["status"] == "DELETING"
    assert provider.deleted == ["server-1"]


def test_console_rejects_a_duplicate_network(workspace, monkeypatch):
    _user, tenant, project, client = workspace
    monkeypatch.setattr("core.cloud_vm_views.build_provider", lambda key: FakeProvider())
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cloud-vms/",
        {
            "provider": "openstack-lab",
            "name": "worker-a",
            "flavor_ref": "flavor-1",
            "image_ref": "image-1",
            "network_ids": ["n-1", "n-1"],
        },
        format="json",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "CLOUD_VM_REQUEST_INVALID"


def test_console_hides_vms_from_a_foreign_tenant(workspace):
    owner, tenant, project, _client = workspace
    vm = CloudVmInstance.objects.create(
        tenant=tenant,
        project=project,
        created_by=owner,
        provider_key="openstack-lab",
        server_id="server-1",
        name="worker-a",
        flavor_ref="flavor-1",
        image_ref="image-1",
        network_ids=["network-1"],
        status="ACTIVE",
    )
    outsider = User.objects.create_user(username="outsider", password="pw")
    client = APIClient()
    client.force_authenticate(user=outsider)
    assert client.get(f"/api/v1/cloud-vms/{vm.id}/").status_code == 404
    assert client.delete(f"/api/v1/cloud-vms/{vm.id}/").status_code == 404
