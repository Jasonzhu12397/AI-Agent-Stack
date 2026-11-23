"""Per-page backend acceptance matrix for the 14 settings-center pages.

Every page is driven over real HTTP (DRF ``APIClient``) against the trunk
backend. For each page the matrix runs three acceptance checks:

1. ``test_page_unauthenticated_rejected`` — an anonymous request is rejected.
2. ``test_page_non_member_isolation`` — an authenticated non-member of the
   tenant/project scope receives 404 and the resource is not disclosed.
3. ``test_page_member_payload`` — an authenticated member receives a 2xx
   response carrying the page's primary payload fields. Empty data returns an
   empty list or server-owned defaults; nothing is fabricated here.

Two matrix-integrity tests guard the registry itself:

4. ``test_slug_set_matches_contract_doc`` — the slugs parsed from
   ``docs/settings-center-contract.md`` match the registry exactly.
5. ``test_matrix_has_one_case_per_slug`` — exactly one parametrized acceptance
   case per slug; a missing page fails this test.

Only tests are added; no production code is modified.
"""

import re
from pathlib import Path

import pytest
from core.models import Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

CONTRACT_DOC = Path(__file__).resolve().parent.parent / "docs" / "settings-center-contract.md"

# Views inheriting ``SettingsBaseView`` answer anonymous requests with the
# contract envelope ``{"error": {"code", "message", "details"}}`` (401
# AUTH_REQUIRED under this base view).
ENVELOPE_AUTH_STYLE = "envelope"
# Plain ``APIView`` subclasses rely on DRF's default rejection. Under the
# project's Session/Basic authentication this answers anonymous requests 403
# with ``{"detail": ...}``; the contract error envelope is not emitted there
# yet. This is a backend contract gap recorded for upstream follow-up; the
# matrix asserts the actual rejection without touching production code.
DRF_AUTH_STYLE = "drf"


def _tenant_path(tenant_id: str, path: str) -> str:
    return f"/api/v1/tenants/{tenant_id}/{path}/"


# Registry order matches the navigation order in the contract document.
PAGES = [
    {
        "slug": "account",
        "scope": "user",
        "auth_style": ENVELOPE_AUTH_STYLE,
        "url": lambda tenant, project: "/api/v1/users/me/",
        "member_keys": ["id", "username", "display_name", "timezone", "locale"],
    },
    {
        "slug": "plan",
        "scope": "tenant",
        "auth_style": DRF_AUTH_STYLE,
        "url": lambda tenant, project: _tenant_path(tenant.id, "billing/plans"),
        "member_keys": ["results"],
    },
    {
        "slug": "subscription",
        "scope": "tenant",
        "auth_style": DRF_AUTH_STYLE,
        "url": lambda tenant, project: _tenant_path(
            tenant.id, "billing/subscription"
        ),
        "member_keys": ["subscription", "source"],
    },
    {
        "slug": "general",
        "scope": "tenant",
        "auth_style": ENVELOPE_AUTH_STYLE,
        "url": lambda tenant, project: _tenant_path(tenant.id, "settings/general"),
        "member_keys": ["section", "scope", "effective", "defaults", "sources"],
    },
    {
        "slug": "connectors",
        "scope": "tenant",
        "auth_style": DRF_AUTH_STYLE,
        "url": lambda tenant, project: _tenant_path(tenant.id, "connectors"),
        "member_keys": ["results"],
    },
    {
        "slug": "appearance",
        "scope": "user",
        "auth_style": ENVELOPE_AUTH_STYLE,
        "url": lambda tenant, project: "/api/v1/users/me/preferences/appearance/",
        "member_keys": ["namespace", "scope", "effective", "defaults", "sources"],
    },
    {
        "slug": "notifications",
        "scope": "tenant",
        "auth_style": ENVELOPE_AUTH_STYLE,
        "url": lambda tenant, project: _tenant_path(
            tenant.id, "settings/notifications"
        ),
        "member_keys": ["section", "scope", "effective", "defaults", "sources"],
    },
    {
        "slug": "work-mode",
        "scope": "tenant",
        "auth_style": ENVELOPE_AUTH_STYLE,
        "url": lambda tenant, project: _tenant_path(
            tenant.id, "settings/work-mode"
        ),
        "member_keys": ["section", "scope", "effective", "defaults", "sources"],
    },
    {
        "slug": "shortcuts",
        "scope": "meta",
        "auth_style": ENVELOPE_AUTH_STYLE,
        "url": lambda tenant, project: "/api/v1/meta/shortcuts/",
        "member_keys": ["results"],
    },
    {
        "slug": "data-controls",
        "scope": "tenant",
        "auth_style": DRF_AUTH_STYLE,
        "url": lambda tenant, project: _tenant_path(
            tenant.id, "data-controls/exports"
        ),
        "member_keys": ["results"],
    },
    {
        "slug": "archive",
        "scope": "project",
        "auth_style": DRF_AUTH_STYLE,
        "url": lambda tenant, project: (
            f"/api/v1/tenants/{tenant.id}/projects/{project.id}/archive-items/"
        ),
        "member_keys": ["count", "page", "results"],
    },
    {
        "slug": "privacy",
        "scope": "tenant",
        "auth_style": DRF_AUTH_STYLE,
        "url": lambda tenant, project: _tenant_path(tenant.id, "privacy/grants"),
        "member_keys": ["results"],
    },
    {
        "slug": "help",
        "scope": "tenant",
        "auth_style": ENVELOPE_AUTH_STYLE,
        "url": lambda tenant, project: _tenant_path(tenant.id, "feedback"),
        "member_keys": ["results"],
    },
    {
        "slug": "about",
        "scope": "meta",
        "auth_style": ENVELOPE_AUTH_STYLE,
        "url": lambda tenant, project: "/api/v1/meta/about/",
        "member_keys": ["version", "build", "licenses"],
    },
]

SLUGS = [entry["slug"] for entry in PAGES]


@pytest.fixture
def actor_factory(db):
    user_model = get_user_model()

    def create(username, email=None):
        return user_model.objects.create_user(
            username=username,
            password="test-password",
            email=email or f"{username}@example.test",
        )

    return create


@pytest.fixture
def acceptance_workspace(db, actor_factory):
    insider = actor_factory("pages-insider")
    outsider = actor_factory("pages-outsider")
    tenant = Tenant.objects.create(name="Pages tenant", slug="pages-tenant")
    foreign = Tenant.objects.create(name="Pages foreign", slug="pages-foreign")
    TenantMembership.objects.create(tenant=tenant, user=insider, role="owner")
    TenantMembership.objects.create(tenant=foreign, user=outsider, role="owner")
    project = Project.objects.create(
        tenant=tenant, name="Pages project", slug="pages-project"
    )
    ProjectMembership.objects.create(project=project, user=insider, role="admin")
    return {
        "insider": insider,
        "outsider": outsider,
        "tenant": tenant,
        "foreign": foreign,
        "project": project,
    }


def _entry(slug: str) -> dict:
    return next(entry for entry in PAGES if entry["slug"] == slug)


def _url(entry: dict, workspace: dict) -> str:
    return entry["url"](workspace["tenant"], workspace["project"])


def _assert_error_envelope(response):
    assert "error" in response.data, response.data
    error = response.data["error"]
    assert {"code", "message", "details"} <= set(error)


@pytest.mark.django_db
@pytest.mark.parametrize("slug", SLUGS)
def test_page_unauthenticated_rejected(slug, acceptance_workspace):
    entry = _entry(slug)
    client = APIClient()
    response = client.get(_url(entry, acceptance_workspace))

    # Project convention (see tests/test_tenant_access.py and
    # tests/test_settings_store.py): anonymous requests are rejected with the
    # DRF default 403, or 401 AUTH_REQUIRED on SettingsBaseView subclasses.
    # No global authentication configuration is changed to force 401.
    assert response.status_code in {401, 403}, response.data
    if entry["auth_style"] == ENVELOPE_AUTH_STYLE:
        _assert_error_envelope(response)
    else:
        # Plain APIViews emit the DRF default {"detail": ...} instead of the
        # contract error envelope; tracked as a backend gap in the report.
        assert "detail" in response.data or "error" in response.data


@pytest.mark.django_db
@pytest.mark.parametrize("slug", SLUGS)
def test_page_non_member_isolation(slug, acceptance_workspace):
    entry = _entry(slug)
    client = APIClient()
    client.force_authenticate(user=acceptance_workspace["outsider"])

    if entry["scope"] in {"tenant", "project"}:
        response = client.get(_url(entry, acceptance_workspace))
        assert response.status_code == 404, response.data
        # No existence disclosure: contract envelope or DRF detail shape.
        assert "detail" in response.data or "error" in response.data
    elif entry["scope"] == "user":
        response = client.get(_url(entry, acceptance_workspace))
        assert response.status_code == 200, response.data
        if slug == "account":
            # Own resource only: the outsider must not see the insider's identity.
            assert response.data["username"] == acceptance_workspace["outsider"].username
    else:  # meta scope: registry/about are user-independent but auth-gated.
        response = client.get(_url(entry, acceptance_workspace))
        assert response.status_code == 200, response.data


@pytest.mark.django_db
@pytest.mark.parametrize("slug", SLUGS)
def test_page_member_payload(slug, acceptance_workspace):
    entry = _entry(slug)
    client = APIClient()
    client.force_authenticate(user=acceptance_workspace["insider"])
    response = client.get(_url(entry, acceptance_workspace))
    assert response.status_code in {200, 201}, response.data

    for key in entry["member_keys"]:
        assert key in response.data, (slug, key, response.data)

    if "results" in response.data:
        assert isinstance(response.data["results"], list)

    if slug == "account":
        assert response.data["username"] == acceptance_workspace["insider"].username
        identities = client.get("/api/v1/users/me/identities/")
        assert identities.status_code == 200
        assert "has_password" in identities.data
    elif slug == "shortcuts":
        for item in response.data["results"]:
            assert {"id", "default_combination", "scope", "description"} <= set(item)
    elif slug == "subscription":
        # Usage is read-only authoritative state; per the parallel branch note,
        # assert only key presence and top-level types, never history row fields.
        usage = client.get(
            _tenant_path(acceptance_workspace["tenant"].id, "billing/usage")
        )
        assert usage.status_code == 200, usage.data
        for key in ("daily_limit", "daily_used", "monthly_used", "history"):
            assert key in usage.data, usage.data
        assert isinstance(usage.data["history"], list)


def _contract_doc_slugs() -> set:
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    start = text.index("Slugs (fixed")
    window = text[start : text.index("Groups:", start)]
    return set(re.findall(r"`([a-z][a-z0-9-]*)`", window))


def test_slug_set_matches_contract_doc():
    assert set(SLUGS) == _contract_doc_slugs()


def test_matrix_has_one_case_per_slug():
    assert len(PAGES) == 14
    assert len(SLUGS) == 14
    assert len(set(SLUGS)) == 14
    registered = {entry["slug"] for entry in PAGES}
    for entry in PAGES:
        assert entry["slug"] in registered
        assert entry["scope"] in {"tenant", "project", "user", "meta"}
        assert entry["member_keys"]
        assert callable(entry["url"])
