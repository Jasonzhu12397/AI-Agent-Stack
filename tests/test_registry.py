"""Registry catalog contract: immutable digests, gated promotion, internal pulls."""

import pytest
from core.models import Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def scope(db):
    user = get_user_model().objects.create_user(username="registry-admin")
    tenant = Tenant.objects.create(name="Registry tenant", slug="registry-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    project = Project.objects.create(
        tenant=tenant, name="Registry project", slug="registry-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return {"user": user, "tenant": tenant, "project": project, "client": client}


def base(scope):
    return f"/api/v1/tenants/{scope['tenant'].id}/projects/{scope['project'].id}/registry"


def register(scope, name="ai-agent-stack/eph-agent", digest=DIGEST, category="agent"):
    response = scope["client"].post(
        f"{base(scope)}/images/",
        {"name": name, "digest": digest, "category": category},
        format="json",
    )
    assert response.status_code == 201, response.data
    return response.data


def test_register_requires_an_immutable_digest(scope):
    response = scope["client"].post(
        f"{base(scope)}/images/",
        {"name": "ai-agent-stack/eph-agent", "digest": "latest", "category": "agent"},
        format="json",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "REGISTRY_DIGEST_REQUIRED"


def test_register_rejects_unknown_category_and_duplicate(scope):
    bad = scope["client"].post(
        f"{base(scope)}/images/",
        {"name": "ai-agent-stack/eph-agent", "digest": DIGEST, "category": "nonsense"},
        format="json",
    )
    assert bad.status_code == 400
    register(scope)
    dup = scope["client"].post(
        f"{base(scope)}/images/",
        {"name": "ai-agent-stack/eph-agent", "digest": DIGEST, "category": "agent"},
        format="json",
    )
    assert dup.status_code == 409


def test_promotion_walks_the_ladder_and_gates_production(scope):
    image = register(scope)
    image_id = image["id"]
    # A fresh image is scanning, so promotion is refused.
    early = scope["client"].post(
        f"{base(scope)}/images/{image_id}/promote/", {"stage": "dev"}, format="json"
    )
    assert early.status_code == 409

    scan = scope["client"].post(
        f"{base(scope)}/images/{image_id}/scan/",
        {"scan_status": "passed", "findings": 0, "sbom_ref": "sbom/eph-agent.json"},
        format="json",
    )
    assert scan.status_code == 200

    staging_first = scope["client"].post(
        f"{base(scope)}/images/{image_id}/promote/", {"stage": "staging"}, format="json"
    )
    assert staging_first.status_code == 200
    assert staging_first.data["stage"] == "staging"

    # Production demands a signature; staging is not signed yet.
    prod = scope["client"].post(
        f"{base(scope)}/images/{image_id}/promote/", {"stage": "production"}, format="json"
    )
    assert prod.status_code == 409
    assert prod.data["error"]["code"] == "REGISTRY_SIGNATURE_REQUIRED"

    signed = scope["client"].post(f"{base(scope)}/images/{image_id}/sign/", {}, format="json")
    assert signed.status_code == 200
    prod_ok = scope["client"].post(
        f"{base(scope)}/images/{image_id}/promote/", {"stage": "production"}, format="json"
    )
    assert prod_ok.status_code == 200
    assert prod_ok.data["stage"] == "production"


def test_promotion_cannot_skip_a_stage(scope):
    image = register(scope, name="ai-agent-stack/target-agent", digest=OTHER_DIGEST)
    scope["client"].post(
        f"{base(scope)}/images/{image['id']}/scan/", {"scan_status": "passed"}, format="json"
    )
    skip = scope["client"].post(
        f"{base(scope)}/images/{image['id']}/promote/", {"stage": "production"}, format="json"
    )
    assert skip.status_code == 409
    assert skip.data["error"]["code"] == "REGISTRY_PROMOTION_ORDER"


def test_failed_scan_blocks_the_image(scope):
    image = register(scope, name="ai-agent-stack/ironic", category="infrastructure")
    scan = scope["client"].post(
        f"{base(scope)}/images/{image['id']}/scan/",
        {"scan_status": "failed", "findings": 3},
        format="json",
    )
    assert scan.status_code == 200
    assert scan.data["status"] == "blocked"
    promote = scope["client"].post(
        f"{base(scope)}/images/{image['id']}/promote/", {"stage": "staging"}, format="json"
    )
    assert promote.status_code == 409


def test_pull_plan_only_lists_ready_images_on_the_internal_host(scope):
    ready = register(scope)
    scope["client"].post(
        f"{base(scope)}/images/{ready['id']}/scan/", {"scan_status": "passed"}, format="json"
    )
    pending = register(scope, name="ai-agent-stack/eph-agent", digest=OTHER_DIGEST)
    plan = scope["client"].get(f"{base(scope)}/pull-plan/?category=agent")
    assert plan.status_code == 200
    data = plan.data
    assert data["digest_pinned"] is True
    assert data["registry_host"]
    refs = [item["internal_ref"] for item in data["images"]]
    assert all(ref.startswith(data["registry_host"] + "/") for ref in refs)
    assert all("@" in ref for ref in refs)
    names = [item["name"] for item in data["images"]]
    assert "ai-agent-stack/eph-agent" in names
    assert len(data["images"]) == 1, pending


def test_summary_counts_by_category_stage_and_status(scope):
    image = register(scope)
    scope["client"].post(
        f"{base(scope)}/images/{image['id']}/scan/", {"scan_status": "passed"}, format="json"
    )
    register(scope, name="ai-agent-stack/control-plane", category="kubernetes")
    summary = scope["client"].get(f"{base(scope)}/summary/")
    assert summary.status_code == 200
    assert summary.data["total"] == 2
    assert summary.data["by_category"]["agent"] == 1
    assert summary.data["by_status"]["ready"] == 1
    assert summary.data["scan_passed"] == 1


def test_events_trail_every_lifecycle_step(scope):
    image = register(scope)
    scope["client"].post(
        f"{base(scope)}/images/{image['id']}/scan/", {"scan_status": "passed"}, format="json"
    )
    scope["client"].post(f"{base(scope)}/images/{image['id']}/sign/", {}, format="json")
    events = scope["client"].get(f"{base(scope)}/images/{image['id']}/events/")
    assert events.status_code == 200
    actions = [event["action"] for event in events.data["events"]]
    assert set(actions) >= {"registered", "scanned", "signed"}
