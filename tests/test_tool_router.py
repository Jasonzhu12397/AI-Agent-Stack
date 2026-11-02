from types import SimpleNamespace
import unittest

from core.tool_router import ToolExecutionError, ToolRouter


def fixture(risk="low"):
    plugin = SimpleNamespace(
        id="plugin-1",
        plugin_key="sample",
        version="1.0.0",
        enabled=True,
        review_status="approved",
    )
    installation = SimpleNamespace(
        id="install-1",
        tenant_id="tenant-1",
        project_id="project-1",
        plugin_id=plugin.id,
        plugin=plugin,
        plugin_version="1.0.0",
        status="installed",
        granted_permissions=["sample:read"],
    )
    tool = SimpleNamespace(
        plugin_id=plugin.id,
        name="lookup",
        enabled=True,
        risk_level=risk,
        required_permissions=["sample:read"],
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 40}},
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"items": {"type": "array"}},
            "required": ["items"],
            "additionalProperties": False,
        },
    )
    return installation, tool


class ToolRouterTests(unittest.TestCase):
    def test_executes_registered_handler_with_scoped_context(self):
        installation, tool = fixture()
        router = ToolRouter()
        observed = {}

        def handler(arguments, context):
            observed["arguments"] = arguments
            observed["context"] = context
            return {"items": [arguments["query"]]}

        router.register("sample", "lookup", handler)
        result = router.execute(
            installation=installation,
            tool=tool,
            arguments={"query": "cluster"},
            granted_permissions=["sample:read"],
        )
        self.assertEqual(result, {"ok": True, "tool": "lookup", "result": {"items": ["cluster"]}})
        self.assertEqual(observed["context"].tenant_id, "tenant-1")
        self.assertEqual(observed["context"].project_id, "project-1")

    def test_rejects_missing_permission(self):
        installation, tool = fixture()
        router = ToolRouter()
        router.register("sample", "lookup", lambda args, context: {"items": []})
        with self.assertRaisesRegex(ToolExecutionError, "permissions"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments={"query": "x"},
                granted_permissions=[],
            )

    def test_rejects_high_risk_without_approval(self):
        installation, tool = fixture(risk="high")
        router = ToolRouter()
        router.register("sample", "lookup", lambda args, context: {"items": []})
        with self.assertRaisesRegex(ToolExecutionError, "approval"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments={"query": "x"},
                granted_permissions=["sample:read"],
            )

    def test_rejects_unknown_arguments(self):
        installation, tool = fixture()
        router = ToolRouter()
        router.register("sample", "lookup", lambda args, context: {"items": []})
        with self.assertRaisesRegex(ToolExecutionError, "Unknown tool arguments"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments={"query": "x", "shell": "id"},
                granted_permissions=["sample:read"],
            )

    def test_hides_handler_exception_details(self):
        installation, tool = fixture()
        router = ToolRouter()

        def handler(arguments, context):
            raise RuntimeError("secret-token=do-not-leak")

        router.register("sample", "lookup", handler)
        with self.assertRaises(ToolExecutionError) as raised:
            router.execute(
                installation=installation,
                tool=tool,
                arguments={"query": "x"},
                granted_permissions=["sample:read"],
            )
        self.assertNotIn("do-not-leak", str(raised.exception))

    def test_rejects_unapproved_connector(self):
        installation, tool = fixture()
        installation.plugin.enabled = False
        router = ToolRouter()
        router.register("sample", "lookup", lambda args, context: {"items": []})
        with self.assertRaisesRegex(ToolExecutionError, "not approved"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments={"query": "x"},
                granted_permissions=["sample:read"],
            )

    def test_rejects_non_object_arguments(self):
        installation, tool = fixture()
        router = ToolRouter()
        router.register("sample", "lookup", lambda args, context: {"items": []})
        with self.assertRaisesRegex(ToolExecutionError, "must be an object"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments=["x"],
                granted_permissions=["sample:read"],
            )


if __name__ == "__main__":
    unittest.main()
