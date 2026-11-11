# ruff: noqa: I001
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from core.models import EPHSeedArtifact
from tests.test_eph_seed_artifacts import create_seed

pytest_plugins = ["tests.test_eph_seed_artifacts"]


@pytest.mark.django_db
def test_cleanup_erases_only_expired_undownloaded_seed_payloads(seed_workspace):
    tenant, project, client, enrollment = seed_workspace
    created = create_seed(client, tenant, project, enrollment)
    assert created.status_code == 201
    expired = EPHSeedArtifact.objects.get(pk=created.data["artifact_id"])
    expired.expires_at = timezone.now() - timedelta(seconds=1)
    expired.save(update_fields=["expires_at"])

    output = StringIO()
    call_command("cleanup_expired_eph_seed_artifacts", stdout=output)

    expired.refresh_from_db()
    assert expired.encrypted_payload == ""
    assert expired.revoked_at is None
    assert expired.download_token_hash
    assert "artifact metadata retained" in output.getvalue()


@pytest.mark.django_db
def test_cleanup_dry_run_does_not_erase_seed_payload(seed_workspace):
    tenant, project, client, enrollment = seed_workspace
    created = create_seed(client, tenant, project, enrollment)
    artifact = EPHSeedArtifact.objects.get(pk=created.data["artifact_id"])
    artifact.expires_at = timezone.now() - timedelta(seconds=1)
    artifact.save(update_fields=["expires_at"])

    output = StringIO()
    call_command("cleanup_expired_eph_seed_artifacts", dry_run=True, stdout=output)

    artifact.refresh_from_db()
    assert artifact.encrypted_payload
    assert "DRY_RUN" in output.getvalue()


@pytest.mark.django_db
def test_cleanup_preserves_downloaded_and_unexpired_seed_artifacts(seed_workspace):
    tenant, project, client, enrollment = seed_workspace
    created = create_seed(client, tenant, project, enrollment)
    artifact = EPHSeedArtifact.objects.get(pk=created.data["artifact_id"])
    artifact.expires_at = timezone.now() - timedelta(seconds=1)
    artifact.downloaded_at = timezone.now() - timedelta(seconds=2)
    artifact.save(update_fields=["expires_at", "downloaded_at"])

    output = StringIO()
    call_command("cleanup_expired_eph_seed_artifacts", stdout=output)

    artifact.refresh_from_db()
    assert artifact.encrypted_payload


@pytest.mark.django_db
def test_cleanup_rejects_invalid_batch_size():
    with pytest.raises(CommandError, match="batch-size"):
        call_command("cleanup_expired_eph_seed_artifacts", batch_size=0)
