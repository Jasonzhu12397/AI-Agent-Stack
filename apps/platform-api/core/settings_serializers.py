"""Server-side validation and code-owned defaults for the settings center.

The UI never invents a default: every section/namespace exposes its defaults
from the constants here. Unknown keys are rejected with
``SETTINGS_UNKNOWN_KEY``; wrong type or out-of-range values are rejected with
``SETTINGS_INVALID_VALUE`` and a ``details`` map naming the offending field.
"""

from __future__ import annotations

TENANT_SECTIONS = {
    "general",
    "notifications",
    "work-mode",
    "data-controls",
    "privacy",
}

PREFERENCE_NAMESPACES = {
    "appearance",
    "shortcuts",
    "notification-delivery",
}

# Code-owned defaults, one entry per known key.
SECTION_DEFAULTS: dict[str, dict[str, object]] = {
    "general": {
        "default_landing_page": "dashboard",
        "items_per_page": 20,
        "compact_mode": False,
    },
    "notifications": {
        "email_digest": "off",
        "mention_alerts": True,
        "system_alerts": True,
        "quiet_hours_enabled": False,
    },
    "work-mode": {
        "confirm_destructive": True,
        "auto_save_draft": True,
        "execution_timeout_seconds": 300,
        "run_parallelism": 4,
    },
    "data-controls": {
        "retention_days": 365,
        "allow_cloud_sync": False,
        "export_enabled": True,
    },
    "privacy": {
        "profile_visible": False,
        "analytics_opt_out": False,
        "telemetry_enabled": True,
    },
}

NAMESPACE_DEFAULTS: dict[str, dict[str, object]] = {
    "appearance": {
        "theme": "system",
        "density": "comfortable",
        "font_size": "medium",
        "reduce_motion": False,
    },
    "shortcuts": {},
    "notification-delivery": {
        "in_app": True,
        "email": False,
        "push": False,
        "digest_frequency": "off",
    },
}

# Schema entries: type in {"bool", "int", "str"}; optional choices/min/max.
SECTION_SCHEMAS: dict[str, dict[str, dict[str, object]]] = {
    "general": {
        "default_landing_page": {
            "type": "str",
            "choices": ["dashboard", "agents", "workflows", "fleet"],
        },
        "items_per_page": {"type": "int", "min": 5, "max": 200},
        "compact_mode": {"type": "bool"},
    },
    "notifications": {
        "email_digest": {"type": "str", "choices": ["off", "daily", "weekly"]},
        "mention_alerts": {"type": "bool"},
        "system_alerts": {"type": "bool"},
        "quiet_hours_enabled": {"type": "bool"},
    },
    "work-mode": {
        "confirm_destructive": {"type": "bool"},
        "auto_save_draft": {"type": "bool"},
        "execution_timeout_seconds": {"type": "int", "min": 30, "max": 3600},
        "run_parallelism": {"type": "int", "min": 1, "max": 16},
    },
    "data-controls": {
        "retention_days": {"type": "int", "min": 1, "max": 3650},
        "allow_cloud_sync": {"type": "bool"},
        "export_enabled": {"type": "bool"},
    },
    "privacy": {
        "profile_visible": {"type": "bool"},
        "analytics_opt_out": {"type": "bool"},
        "telemetry_enabled": {"type": "bool"},
    },
}

NAMESPACE_SCHEMAS: dict[str, dict[str, dict[str, object]]] = {
    "appearance": {
        "theme": {"type": "str", "choices": ["light", "dark", "system"]},
        "density": {"type": "str", "choices": ["comfortable", "compact"]},
        "font_size": {"type": "str", "choices": ["small", "medium", "large"]},
        "reduce_motion": {"type": "bool"},
    },
    "shortcuts": {},
    "notification-delivery": {
        "in_app": {"type": "bool"},
        "email": {"type": "bool"},
        "push": {"type": "bool"},
        "digest_frequency": {"type": "str", "choices": ["off", "daily", "weekly"]},
    },
}

# Server-authoritative shortcut registry. The browser may remap a combination
# per user, but it cannot invent a shortcut id or a scope.
SHORTCUT_REGISTRY: list[dict[str, str]] = [
    {
        "id": "sidebar.toggle",
        "default_combination": "Mod+B",
        "scope": "global",
        "description": "Toggle the navigation sidebar.",
    },
    {
        "id": "command.palette",
        "default_combination": "Mod+K",
        "scope": "global",
        "description": "Open the command palette.",
    },
    {
        "id": "agent.new",
        "default_combination": "Mod+N",
        "scope": "studio",
        "description": "Create a new agent.",
    },
    {
        "id": "workflow.run",
        "default_combination": "Mod+Enter",
        "scope": "studio",
        "description": "Run the current workflow.",
    },
    {
        "id": "settings.open",
        "default_combination": "Mod+,",
        "scope": "global",
        "description": "Open the settings center.",
    },
]

SHORTCUT_IDS = {entry["id"] for entry in SHORTCUT_REGISTRY}

_COMBINATION_MODIFIERS = {"mod", "ctrl", "alt", "shift"}
_COMBINATION_KEYS = {
    "enter",
    "space",
    "tab",
    "escape",
    "backspace",
    "delete",
    "home",
    "end",
    "pageup",
    "pagedown",
    "comma",
    "period",
    "slash",
    "backslash",
    "bracketleft",
    "bracketright",
}


def _type_check(spec: dict[str, object], value: object) -> str | None:
    expected = spec["type"]
    if expected == "bool":
        if not isinstance(value, bool):
            return "must be a boolean"
    elif expected == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return "must be an integer"
        minimum = spec.get("min")
        maximum = spec.get("max")
        if minimum is not None and value < minimum:
            return f"must be at least {minimum}"
        if maximum is not None and value > maximum:
            return f"must be at most {maximum}"
    elif expected == "str":
        if not isinstance(value, str) or not value.strip():
            return "must be a non-empty string"
        choices = spec.get("choices")
        if choices and value not in choices:
            return f"must be one of: {', '.join(str(choice) for choice in choices)}"
    return None


def validate_combination(combination: object) -> str | None:
    """Return an error message when the shortcut combination is malformed."""

    if not isinstance(combination, str) or not combination.strip():
        return "combination must be a non-empty string"
    tokens = [token.strip().lower() for token in combination.split("+") if token.strip()]
    if len(tokens) < 2:
        return "combination must include at least one modifier and a key"
    *modifiers, key = tokens
    if any(modifier not in _COMBINATION_MODIFIERS for modifier in modifiers):
        return f"modifiers must be one of: {', '.join(sorted(_COMBINATION_MODIFIERS))}"
    if not (len(key) == 1 or key in _COMBINATION_KEYS or key.isalnum()):
        return f"unsupported key: {key}"
    return None


def normalize_combination(combination: str) -> str:
    tokens = [token.strip().lower() for token in combination.split("+") if token.strip()]
    modifiers, key = tokens[:-1], tokens[-1]
    ordered = "+".join(sorted(modifiers))
    return f"{ordered}+{key}" if ordered else key


def validate_values(
    schema: dict[str, dict[str, object]],
    values: object,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Validate a values object against a section/namespace schema.

    Returns ``(cleaned, None)`` on success or ``(None, error)`` where error
    follows the contract envelope's ``error`` payload.
    """

    if not isinstance(values, dict):
        return None, {
            "code": "SETTINGS_INVALID_VALUE",
            "message": "Values must be an object.",
            "details": {"values": ["must be an object"]},
        }

    unknown: dict[str, list[str]] = {}
    invalid: dict[str, list[str]] = {}
    cleaned: dict[str, object] = {}

    for key, value in values.items():
        spec = schema.get(key)
        if spec is None:
            unknown[key] = ["unknown setting for this scope"]
            continue
        problem = _type_check(spec, value)
        if problem is not None:
            invalid[key] = [problem]
        else:
            cleaned[key] = value

    if unknown:
        return None, {
            "code": "SETTINGS_UNKNOWN_KEY",
            "message": "One or more settings keys are not recognized.",
            "details": unknown,
        }
    if invalid:
        return None, {
            "code": "SETTINGS_INVALID_VALUE",
            "message": "One or more settings values are invalid.",
            "details": invalid,
        }
    return cleaned, None
