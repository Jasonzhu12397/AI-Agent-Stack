from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        # Register the protocol adapter implementations. Imports are performed
        # directly here: a missing adapter or registration failure must surface
        # during startup instead of being swallowed by a fallback handler.
        from core.agent_a2a_adapter import register as register_a2a_adapter
        from core.agent_mcp_adapter import register as register_mcp_adapter
        from core.agent_openai_compatible_adapter import (
            register as register_openai_compatible_adapter,
        )

        register_mcp_adapter()
        register_openai_compatible_adapter()
        register_a2a_adapter()

        # Importing the module connects the login/logout receivers and defines
        # the SessionActivity model; a broken import must fail startup loudly.
        from core import session_activity  # noqa: F401
