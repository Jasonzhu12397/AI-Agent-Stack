import unittest
from types import SimpleNamespace

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
            "properties": {
                "query": {"type": "string", "maxLength": 40},
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 12},
                    "maxItems": 2,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "string"}}},
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

    def test_rejects_arrays_outside_item_schema_and_bounds(self):
        installation, tool = fixture()
        router = ToolRouter()
        router.register("sample", "lookup", lambda args, context: {"items": []})
        with self.assertRaisesRegex(ToolExecutionError, "array shape"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments={"query": "x", "tags": ["a", "b", "c"]},
                granted_permissions=["sample:read"],
            )
        with self.assertRaisesRegex(ToolExecutionError, "invalid item"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments={"query": "x", "tags": ["a", 3]},
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

    def test_bounds_serialized_arguments_and_results(self):
        installation, tool = fixture()
        tool.input_schema = {
            "type": "object",
            "properties": {"payload": {"type": "string", "maxLength": 100000}},
            "required": ["payload"],
            "additionalProperties": False,
        }
        tool.output_schema = {
            "type": "object",
            "properties": {"payload": {"type": "string", "maxLength": 100000}},
            "required": ["payload"],
            "additionalProperties": False,
        }
        router = ToolRouter()
        router.register("sample", "lookup", lambda args, context: {"ok": True})
        with self.assertRaisesRegex(ToolExecutionError, "arguments exceed 64 KiB"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments={"payload": "x" * 70000},
                granted_permissions=["sample:read"],
            )

        tool.input_schema = {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
        tool.output_schema = {
            "type": "object",
            "properties": {"payload": {"type": "string", "maxLength": 100000}},
            "required": ["payload"],
            "additionalProperties": False,
        }
        router = ToolRouter()
        router.register(
            "sample",
            "lookup",
            lambda args, context: {"payload": "x" * 70000},
        )
        with self.assertRaisesRegex(ToolExecutionError, "result exceeds 64 KiB"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments={},
                granted_permissions=["sample:read"],
            )

    def test_dispatches_version_pinned_handler(self):
        installation, tool = fixture()
        router = ToolRouter()
        router.register(
            "sample",
            "lookup",
            lambda args, context: {"items": ["v1"]},
            plugin_version="1.0.0",
        )
        router.register(
            "sample",
            "lookup",
            lambda args, context: {"items": ["v2"]},
            plugin_version="2.0.0",
        )
        installation.plugin.version = "2.0.0"
        installation.plugin_version = "2.0.0"
        result = router.execute(
            installation=installation,
            tool=tool,
            arguments={"query": "version"},
            granted_permissions=["sample:read"],
        )
        self.assertEqual(result["result"]["items"], ["v2"])


    def test_validates_constraints_inside_array_items(self):
        installation, tool = fixture()
        tool.input_schema = {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 3, "enum": ["ok", "yes"]},
                    "maxItems": 4,
                }
            },
            "required": ["tags"],
            "additionalProperties": False,
        }
        router = ToolRouter()
        router.register("sample", "lookup", lambda args, context: {"items": []})
        with self.assertRaisesRegex(ToolExecutionError, "invalid length|allowed value"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments={"tags": ["valid", "bad"]},
                granted_permissions=["sample:read"],
            )

    def test_validates_numeric_bounds_and_nested_arrays(self):
        installation, tool = fixture()
        tool.input_schema = {
            "type": "object",
            "properties": {
                "matrix": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1, "maximum": 5},
                        "minItems": 1,
                        "maxItems": 2,
                    },
                    "maxItems": 2,
                }
            },
            "required": ["matrix"],
            "additionalProperties": False,
        }
        router = ToolRouter()
        router.register("sample", "lookup", lambda args, context: {"items": []})
        with self.assertRaisesRegex(ToolExecutionError, "below its minimum"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments={"matrix": [[0]]},
                granted_permissions=["sample:read"],
            )

    def test_rejects_non_finite_numbers_inside_arrays(self):
        installation, tool = fixture()
        tool.input_schema = {
            "type": "object",
            "properties": {
                "values": {"type": "array", "items": {"type": "number"}, "maxItems": 3}
            },
            "required": ["values"],
            "additionalProperties": False,
        }
        router = ToolRouter()
        router.register("sample", "lookup", lambda args, context: {"items": []})
        with self.assertRaisesRegex(ToolExecutionError, "finite"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments={"values": [float("nan")]},
                granted_permissions=["sample:read"],
            )


    def test_enforces_enum_constraints_for_objects_and_arrays(self):
        installation, tool = fixture()
        router = ToolRouter()
        router.register("sample", "lookup", lambda args, context: {"items": []})

        tool.input_schema = {
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                    "enum": [{"name": "allowed"}],
                }
            },
            "required": ["payload"],
            "additionalProperties": False,
        }
        with self.assertRaisesRegex(ToolExecutionError, "allowed value"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments={"payload": {"name": "denied"}},
                granted_permissions=["sample:read"],
            )

        tool.input_schema = {
            "type": "object",
            "properties": {
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 2,
                    "enum": [["allowed"]],
                }
            },
            "required": ["values"],
            "additionalProperties": False,
        }
        with self.assertRaisesRegex(ToolExecutionError, "allowed value"):
            router.execute(
                installation=installation,
                tool=tool,
                arguments={"values": ["denied"]},
                granted_permissions=["sample:read"],
            )

if __name__ == "__main__":
    unittest.main()
