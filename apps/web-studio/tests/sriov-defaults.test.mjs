import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const editor = readFileSync(
  new URL("../src/components/SriovDevicePluginEditor.vue", import.meta.url),
  "utf8",
);
const fleet = readFileSync(
  new URL("../src/components/Metal3FleetView.vue", import.meta.url),
  "utf8",
);

test("test_sriov_pool_name_is_editable_in_the_ui", () => {
  assert.match(editor, /<el-form-item label="Pool name">/);
  assert.match(editor, /@change="renamePool"/);
  assert.match(editor, /function renamePool/);
  assert.match(editor, /draft\[next\] = pool/);
});

test("test_sriov_editor_has_no_reserved_pool4_default", () => {
  assert.doesNotMatch(editor, /pool4/);
  assert.doesNotMatch(editor, /let index = 4;/);
});

test("test_empty_configuration_uses_the_editable_starter_pool", () => {
  assert.match(editor, /if \(count === 0\) addPool\(\)/);
  assert.match(editor, /const name = "pool-" \+ index;/);
});

test("test_fleet_view_does_not_preload_pool4", () => {
  assert.doesNotMatch(fleet, /pool4/);
  assert.match(fleet, /const sriovPools = ref<Record<string, any>>\(\{\}\);/);
});
