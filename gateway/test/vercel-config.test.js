import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const DOCS = {
  nodeVersions: "https://vercel.com/docs/functions/runtimes/node-js/node-js-versions",
  vercelJson: "https://vercel.com/docs/project-configuration/vercel-json",
};

test("node 22 comes from engines and not functions.runtime", async () => {
  const vercel = JSON.parse(await readFile(new URL("../vercel.json", import.meta.url), "utf8"));
  const pkg = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(pkg.engines.node, "22.x");
  assert.notEqual(pkg.engines.node, ">=22.0.0");
  const functions = vercel.functions || {};
  assert.ok(Object.keys(functions).length > 0);
  for (const [pattern, value] of Object.entries(functions)) {
    assert.equal(Object.hasOwn(value, "runtime"), false, pattern);
    assert.equal(value.maxDuration, 15);
  }
  assert.equal(JSON.stringify(vercel).includes("nodejs22.x"), false);
  assert.equal(vercel.rewrites[0].source, "/v1/installations/:installationId/judgments/:eventId");
  assert.equal(vercel.rewrites[0].destination, "/api/v1/installations/:installationId/judgments/:eventId");
  assert.ok(DOCS.nodeVersions.startsWith("https://vercel.com/docs/functions/runtimes/node-js/"));
  assert.ok(DOCS.vercelJson.endsWith("/vercel-json"));
});
