import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const apiSource = new URL("../src/services/api.ts", import.meta.url);

test("non-JSON gateway failures point to the app origin, not the model provider", async () => {
  const source = await readFile(apiSource, "utf8");
  const gatewayBranch = source.split("if ([502, 503, 504].includes(response.status) && payload === null)", 2)[1]?.split(
    "throw new ApiError(`Request failed",
    1,
  )[0];

  assert.ok(gatewayBranch, "Expected a dedicated non-JSON gateway error branch");
  assert.ok(gatewayBranch.includes("Cloudflare Tunnel"));
  assert.ok(gatewayBranch.includes("Codespaces port forwarding"));
  assert.ok(gatewayBranch.includes("application API"));
  assert.ok(gatewayBranch.includes("does not indicate a model-provider outage"));
  assert.ok(!gatewayBranch.includes("check the provider status"));
});
