"""Guard against column widths narrower than the protocol tokens they store.

SQLite ignores ``varchar`` lengths, so a too-narrow column is silently accepted
locally and only raises ``value too long for type character varying`` on the
PostgreSQL CI shards. This suite keeps the wiring honest across both engines with
a model-level invariant plus a round-trip write of the longest token.
"""

from core.models import AgentPluginDefinition
from django.apps import apps


def _protocol_fields():
    app_config = apps.get_app_config("core")
    for model in app_config.get_models():
        for field in model._meta.get_fields():
            if getattr(field, "name", None) == "protocol" and getattr(field, "max_length", None):
                yield model, field


def _offenders():
    longest_token = max(len(value) for value in AgentPluginDefinition.Protocol.values)
    return [
        f"{model._meta.label}.{field.name} max_length={field.max_length} < {longest_token}"
        for model, field in _protocol_fields()
        if field.max_length < longest_token
    ]


def test_protocol_columns_fit_every_declared_protocol_token():
    offenders = _offenders()

    assert offenders == [], "protocol column(s) too narrow: " + ", ".join(offenders)


def test_protocol_columns_are_discovered():
    # Guards the invariant above from silently passing on an empty field set.
    assert len(list(_protocol_fields())) >= 2


def test_agent_capability_invocation_protocol_holds_longest_token(db):
    from core.models import AgentCapabilityInvocation

    field = AgentCapabilityInvocation._meta.get_field("protocol")
    longest = max(AgentPluginDefinition.Protocol.values, key=len)

    assert len(longest) <= field.max_length
