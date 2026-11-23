"""The marketplace document must not drift from the code it describes.

Every capability, credential and installation error code the document names is
treated as a promise to the operator: if the code no longer exists, or the
rotation endpoint it documents is not routed, the document is lying. This test
fails on that instead of letting the prose rot.
"""

import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "agent-marketplace.md"
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "apps" / "platform-api"

# Tokens that look like error codes but are prose, enum values or field names.
PROSE_TOKENS = {
    "GET",
    "POST",
    "HTTP",
    "HTTPS",
    "API",
    "ID",
    "OK",
    "URI",
    "URL",
    "JSON",
    "SQL",
    "ONE",
}

ROTATION_ENDPOINT = "/api/v1/agent-plugin-installations/{installation_id}/credential/"


def _document() -> str:
    return DOC.read_text(encoding="utf-8")


def _source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
    )


def test_the_document_mentions_the_rotation_endpoint():
    text = _document()
    assert ROTATION_ENDPOINT in text
    assert "Credential rotation" in text


def test_every_code_named_in_the_document_exists_in_the_source():
    text = _document()
    source = _source()
    named = {
        token
        for token in re.findall(r"`([A-Z][A-Z0-9_]{3,})`", text)
        if token not in PROSE_TOKENS
    }
    assert named, "the document is expected to name error codes"
    missing = sorted(token for token in named if token not in source)
    assert missing == [], f"documented codes missing from the source: {missing}"


def test_the_rotation_audit_event_matches_the_code():
    text = _document()
    source = _source()
    event = "agent_plugin.credential_rotated"
    assert event in text
    assert event in source


def test_the_document_states_that_a_reference_is_never_echoed():
    text = _document().lower()
    # Stated as a guarantee, not merely absent from the prose.
    assert "never echoes the reference" in text
    assert "returns the credential_ref" not in text
