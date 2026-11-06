import test from "node:test";
import assert from "node:assert/strict";
import {
  connectorTemplates,
  buildConnectorTemplatePayload,
} from "../src/domain/connectorTemplates.ts";

const byId = (id) => {
  const template = connectorTemplates.find((item) => item.id === id);
  assert.ok(template, `missing connector template: ${id}`);
  return template;
};

const toolByName = (template, name) => {
  const tool = template.payload.tools.find((item) => item.name === name);
  assert.ok(tool, `missing tool ${name} in ${template.id}`);
  return tool;
};

test("connector template identifiers and plugin keys are unique", () => {
  const ids = connectorTemplates.map((item) => item.id);
  const pluginKeys = connectorTemplates.map((item) => item.payload.plugin_key);
  assert.equal(new Set(ids).size, ids.length);
  assert.equal(new Set(pluginKeys).size, pluginKeys.length);
  for (const template of connectorTemplates) {
    assert.equal(template.payload.plugin_key, template.id);
    assert.equal(template.payload.connector_type, "http");
  }
});

test("HTTP operation mappings reference declared tool input properties", () => {
  for (const template of connectorTemplates) {
    const http = template.payload.manifest.http;
    assert.ok(http.base_url.startsWith("https://"));
    assert.equal(new URL(http.base_url).hostname, template.host);
    for (const [operationName, operation] of Object.entries(http.operations)) {
      const tool = toolByName(template, operationName);
      const properties = tool.input_schema.properties ?? {};
      for (const argumentName of Object.values(operation.query_params ?? {})) {
        assert.ok(
          Object.hasOwn(properties, argumentName),
          `${template.id}/${operationName} maps undeclared query argument ${argumentName}`,
        );
      }
      for (const argumentName of Object.values(operation.path_params ?? {})) {
        assert.ok(
          Object.hasOwn(properties, argumentName),
          `${template.id}/${operationName} maps undeclared path argument ${argumentName}`,
        );
      }
      if (operation.body_param) {
        assert.ok(
          Object.hasOwn(properties, operation.body_param),
          `${template.id}/${operationName} maps undeclared body argument ${operation.body_param}`,
        );
      }
    }
  }
});

test("Baidu nearby search uses the correct endpoint and server-side AK", () => {
  const template = byId("baidu-map-places");
  const http = template.payload.manifest.http;
  const operation = http.operations["search-baidu-places-around"];
  const tool = toolByName(template, "search-baidu-places-around");
  assert.equal(template.host, "api.map.baidu.com");
  assert.equal(operation.method, "GET");
  assert.equal(operation.path, "/place/v3/around");
  assert.equal(operation.auth_query_parameter, "ak");
  assert.equal(http.auth_required, true);
  assert.deepEqual(tool.required_permissions, ["baidu:places:read"]);
  assert.equal(tool.risk_level, "low");
  assert.equal(tool.idempotency_mode, "read_only");
});

test("Baidu nearby search validates coordinate order and bounded pagination", () => {
  const tool = toolByName(byId("baidu-map-places"), "search-baidu-places-around");
  const schema = tool.input_schema;
  assert.deepEqual(schema.required, ["query", "location", "output"]);
  assert.match(schema.properties.location.pattern, /\\d\{1,2\}/);
  assert.equal(schema.properties.radius.minimum, 1);
  assert.equal(schema.properties.radius.maximum, 50000);
  assert.equal(schema.properties.page_size.minimum, 1);
  assert.equal(schema.properties.page_size.maximum, 20);
  assert.equal(schema.properties.page_num.minimum, 0);
  assert.equal(schema.properties.page_num.maximum, 100);
  assert.deepEqual(schema.properties.coord_type.enum, [1, 2, 3, 4]);
  assert.deepEqual(schema.properties.output.enum, ["json"]);
});

test("Baidu place details requires either uid or uids", () => {
  const tool = toolByName(byId("baidu-map-places"), "get-baidu-place-details");
  assert.deepEqual(tool.input_schema.anyOf, [
    { required: ["uid"] },
    { required: ["uids"] },
  ]);
});

test("template payload builder returns an isolated deep copy", () => {
  const first = buildConnectorTemplatePayload("baidu-map-places");
  first.manifest.http.base_url = "https://invalid.example";
  const second = buildConnectorTemplatePayload("baidu-map-places");
  assert.equal(second.manifest.http.base_url, "https://api.map.baidu.com");
  assert.throws(() => buildConnectorTemplatePayload("missing-template"), /Unknown connector template/);
});
