import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Pools 0-3 belong to the network layout, so the first assignable pool is
// pool4. A fresh SR-IOV configuration must open on that name rather than start
// empty and later emit "pool0", which would collide with the reserved range.
const source = readFileSync(new URL("../src/components/SriovDevicePluginEditor.vue", import.meta.url), "utf8");

test("test_sriov_pool_numbering_starts_at_four", () => {
  const starts = source.match(/let index = \d+;/g) ?? [];
  assert.equal(starts.length, 2);
  for (const start of starts) assert.equal(start, "let index = 4;");
});

test("test_empty_configuration_opens_on_pool4", () => {
  assert.match(source, /if \(count === 0\) addPool\(\)/);
});

test("test_new_pool_is_named_pool4", () => {
  assert.match(source, /const name = "pool" \+ index;/);
  assert.match(source, /sriov_network_device_plugin_configmap_name: "sriov-dp-configmap-" \+ name/);
});
