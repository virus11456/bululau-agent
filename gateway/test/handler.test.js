import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { ATOMIC_CREATE, officialBlobStore } from "../lib/blob-store.js";
import { canonical, sha256Hex } from "../lib/contract.js";
import { handle } from "../lib/handler.js";
import { FUNCTION_PATH, PUBLIC_PATH, matchJudgmentRoute } from "../lib/route.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const INSTALL = "inst-local-1";
const EVENT = `c-${"ab".repeat(16)}`;
const NOW = new Date("2026-11-07T04:22:00Z");
const PUBLISHER_SECRET = "publisher-secret";
const READER_SECRET = "reader-secret";

function sha(value) {
  return createHash("sha256").update(value).digest("hex");
}

const credentials = {
  installationId: INSTALL,
  publisher: { keyId: "pub-key", secretSha256: sha(PUBLISHER_SECRET) },
  reader: { keyId: "read-key", secretSha256: sha(READER_SECRET) },
};

function auth(role) {
  const secret = role === "publisher" ? PUBLISHER_SECRET : READER_SECRET;
  const keyId = role === "publisher" ? "pub-key" : "read-key";
  return `Bearer ${keyId}:${secret}`;
}

function document(decision = "keep_original_ai") {
  const judgment = {
    schema_version: 1,
    event_type: "dry_run_judgment",
    event_id: EVENT,
    evaluation_id: "s1",
    envelope_sha256: "cd".repeat(32),
    decision,
    basis: {
      reason: "orders_open",
      as_of: "2026-11-07T12:19:30+08:00",
      positions: 0,
      orders: 1,
      freshness_status: "fresh",
      candidate_count: 0,
    },
    completed_at: "2026-11-07T04:22:00Z",
    orders_placed: false,
    ai_execution_proven: false,
  };
  return {
    schema_version: 1,
    event_type: "judgment_put",
    installation_id: INSTALL,
    event_id: EVENT,
    evaluation_id: "s1",
    envelope_sha256: judgment.envelope_sha256,
    judgment_sha256: sha256Hex(canonical(judgment)),
    judgment,
  };
}

function memoryStore() {
  const records = new Map();
  let gate = null;
  const calls = [];
  return {
    calls,
    arm() {
      let release;
      gate = new Promise((resolve) => {
        release = resolve;
      });
      return release;
    },
    async create(pathname, body) {
      calls.push("put");
      if (gate) await gate;
      if (records.has(pathname)) {
        throw Object.assign(new Error("Vercel Blob: This blob already exists"), { status: 409 });
      }
      records.set(pathname, Buffer.from(body));
      return { pathname };
    },
    async read(pathname) {
      calls.push("get");
      const found = records.get(pathname);
      return found ? Buffer.from(found) : null;
    },
    list() {
      throw new Error("list_forbidden");
    },
    del() {
      throw new Error("del_forbidden");
    },
  };
}

function put(store, doc = document()) {
  return handle({
    method: "PUT",
    installationId: INSTALL,
    eventId: EVENT,
    authorization: auth("publisher"),
    credentials,
    raw: Buffer.from(JSON.stringify(doc)),
    now: NOW,
    store,
  });
}

test("atomic create evidence is the conditional put, not a local link", () => {
  assert.equal(ATOMIC_CREATE.sdkOption, "allowOverwrite: false");
  assert.equal(ATOMIC_CREATE.requestHeader, "x-allow-overwrite: 0");
  assert.equal(ATOMIC_CREATE.serverCondition, "If-None-Match: *");
  assert.equal(ATOMIC_CREATE.getThenPutIsNotThisMechanism, true);
  assert.equal(ATOMIC_CREATE.localLinkIsNotThisMechanism, true);
});

test("publisher creates once and a different body conflicts without a leading get", async () => {
  const store = memoryStore();
  const created = await put(store);
  assert.equal(created.status, 201);
  assert.equal(created.headers["Cache-Control"], "private, no-store");
  assert.equal(store.calls.join(","), "put");
  const other = document("no_trade");
  const conflict = await put(store, other);
  assert.equal(conflict.status, 409);
  assert.deepEqual(store.calls, ["put", "put", "get"]);
  const saved = JSON.parse((await store.read("returns/" + INSTALL + "/" + EVENT + ".json")).toString("utf8"));
  assert.equal(saved.judgment.decision, "keep_original_ai");
});

test("same digest retry returns the stored object", async () => {
  const store = memoryStore();
  const doc = document();
  assert.equal((await put(store, doc)).status, 201);
  const again = await put(store, doc);
  assert.equal(again.status, 200);
  assert.equal(again.body.judgment_sha256, doc.judgment_sha256);
});

test("concurrent creates keep one body and never read before put", async () => {
  const store = memoryStore();
  const release = store.arm();
  const first = put(store, document());
  const second = put(store, document("no_trade"));
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(store.calls, ["put", "put"]);
  release();
  const results = await Promise.all([first, second]);
  const statuses = results.map((item) => item.status).sort();
  assert.deepEqual(statuses, [201, 409]);
  const saved = JSON.parse((await store.read("returns/" + INSTALL + "/" + EVENT + ".json")).toString("utf8"));
  assert.equal(saved.judgment.decision, "keep_original_ai");
});

test("roles, installation, schema, size, and expiry stay closed", async () => {
  const store = memoryStore();
  const base = {
    installationId: INSTALL,
    eventId: EVENT,
    credentials,
    now: NOW,
    store,
  };
  assert.equal((await handle({ ...base, method: "GET", authorization: auth("publisher") })).status, 403);
  assert.equal((await handle({ ...base, method: "PUT", authorization: auth("reader"), raw: Buffer.from("{}") })).status, 403);
  assert.equal((await handle({ ...base, method: "DELETE", authorization: auth("publisher") })).status, 405);
  assert.equal((await handle({ ...base, method: "PUT", installationId: "other-inst", authorization: auth("publisher"), raw: Buffer.from("{}") })).status, 403);
  const leaked = document();
  leaked.pathname = "returns/other/x.json";
  assert.equal((await put(store, leaked)).status, 400);
  leaked.judgment.totalWalletBalance = "1";
  delete leaked.pathname;
  assert.equal((await put(store, leaked)).status, 400);
  const huge = Buffer.alloc(MAX_TOO);
  assert.equal((await handle({ ...base, method: "PUT", authorization: auth("publisher"), raw: huge })).status, 400);
  const stale = document();
  stale.judgment.completed_at = "2026-11-07T03:00:00Z";
  stale.judgment_sha256 = sha256Hex(canonical(stale.judgment));
  assert.equal((await put(store, stale)).status, 400);
  assert.deepEqual(store.calls, []);
});

const MAX_TOO = 64 * 1024 + 1;

test("reader gets only an existing private object and misses stay 404", async () => {
  const store = memoryStore();
  const missing = await handle({
    method: "GET",
    installationId: INSTALL,
    eventId: EVENT,
    authorization: auth("reader"),
    credentials,
    now: NOW,
    store,
  });
  assert.equal(missing.status, 404);
  await put(store);
  const found = await handle({
    method: "GET",
    installationId: INSTALL,
    eventId: EVENT,
    authorization: auth("reader"),
    credentials,
    now: NOW,
    store,
  });
  assert.equal(found.status, 200);
  assert.equal(found.body.event_id, EVENT);
  assert.equal(found.headers["Cache-Control"], "private, no-store");
});

test("gateway sources do not import the trading programs", async () => {
  const files = [];
  async function walk(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      if (entry.name === "node_modules") continue;
      const full = path.join(directory, entry.name);
      if (entry.isDirectory()) await walk(full);
      else if (entry.name.endsWith(".js")) files.push(full);
    }
  }
  await walk(path.join(ROOT, "lib"));
  await walk(path.join(ROOT, "api"));
  for (const file of files) {
    const text = await readFile(file, "utf8");
    assert.equal(text.includes("live_trader"), false, file);
    assert.equal(text.includes("prefilter"), false, file);
    assert.equal(text.includes("BLOB_READ_WRITE_TOKEN="), false, file);
  }
});

function mockRes() {
  return {
    statusCode: 0,
    payload: null,
    setHeader() {},
    status(code) {
      this.statusCode = code;
      return this;
    },
    json(body) {
      this.payload = body;
    },
  };
}

test("function entry imports and both urls reach the same ids", async () => {
  const entryPath = path.join(ROOT, "api/v1/installations/[installationId]/judgments/[eventId].js");
  const source = await readFile(entryPath, "utf8");
  assert.match(source, /from "\.\.\/\.\.\/\.\.\/\.\.\/\.\.\/lib\//);
  assert.doesNotMatch(source, /\.\.\/\.\.\/\.\.\/\.\.\/\.\.\/\.\.\//);
  assert.equal(path.resolve(path.dirname(entryPath), "../../../../../lib/handler.js"), path.join(ROOT, "lib/handler.js"));
  const loaded = await import(pathToFileURL(entryPath).href);
  assert.equal(typeof loaded.default, "function");
  assert.equal(typeof loaded.judgmentRoute, "function");
  const config = JSON.parse(await readFile(path.join(ROOT, "vercel.json"), "utf8"));
  assert.equal(config.rewrites[0].source, PUBLIC_PATH);
  assert.equal(config.rewrites[0].destination, FUNCTION_PATH);
  const event = `c-${"ab".repeat(16)}`;
  for (const prefix of ["", "/api"]) {
    const matched = matchJudgmentRoute(`${prefix}/v1/installations/${INSTALL}/judgments/${event}`);
    assert.equal(matched.installationId, INSTALL);
    assert.equal(matched.eventId, event);
    const res = mockRes();
    const store = memoryStore();
    await loaded.judgmentRoute({
      method: "GET",
      url: `${prefix}/v1/installations/${INSTALL}/judgments/${event}`,
      query: {},
      headers: { authorization: auth("reader") },
    }, res, { store, credentials, now: NOW });
    assert.equal(res.statusCode, 404);
    assert.equal(res.payload.reason, "not_found");
  }
});

test("store failures stay redacted on get and conflict readback", async () => {
  const exploding = memoryStore();
  exploding.read = async () => {
    throw new Error("SYNTHETIC_SECRET_MARKER");
  };
  const missing = await handle({
    method: "GET",
    installationId: INSTALL,
    eventId: EVENT,
    authorization: auth("reader"),
    credentials,
    now: NOW,
    store: exploding,
  });
  assert.equal(missing.status, 502);
  assert.equal(JSON.stringify(missing), '{"status":502,"headers":{"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"},"body":{"reason":"store_failed"}}');
  const conflict = memoryStore();
  conflict.create = async () => {
    throw Object.assign(new Error("Vercel Blob: This blob already exists"), { status: 409 });
  };
  conflict.read = exploding.read;
  const denied = await put(conflict);
  assert.equal(denied.status, 502);
  assert.equal(denied.body.reason, "store_failed");
  assert.equal(JSON.stringify(denied).includes("SYNTHETIC_SECRET_MARKER"), false);
});

test("get and conflict readback reject unbound or non finite objects", async () => {
  const cases = [
    Buffer.from('{"x":1e309}'),
    Buffer.from(JSON.stringify({ ...document(), installation_id: "other-inst" })),
    Buffer.from(JSON.stringify({ ...document(), event_id: "c-" + "cd".repeat(16) })),
  ];
  for (const raw of cases) {
    const store = memoryStore();
    store.read = async () => raw;
    const got = await handle({
      method: "GET",
      installationId: INSTALL,
      eventId: EVENT,
      authorization: auth("reader"),
      credentials,
      now: NOW,
      store,
    });
    assert.notEqual(got.status, 200);
    assert.equal(got.body.reason, "store_failed");
    const racing = memoryStore();
    racing.create = async () => {
      throw Object.assign(new Error("Vercel Blob: This blob already exists"), { status: 409 });
    };
    racing.read = async () => raw;
    const again = await put(racing);
    assert.notEqual(again.status, 200);
    assert.equal(again.body.reason, "store_failed");
    assert.equal(JSON.stringify(again).includes("1e309"), false);
  }
});

test("oidc prerequisites do not fall back to a store token", async () => {
  const source = await readFile(path.join(ROOT, "lib/blob-store.js"), "utf8");
  assert.equal(source.includes("BLOB_READ_WRITE_TOKEN"), false);
  const previous = process.env.BLOB_READ_WRITE_TOKEN;
  const previousStore = process.env.BLOB_STORE_ID;
  const previousOidc = process.env.VERCEL_OIDC_TOKEN;
  process.env.BLOB_READ_WRITE_TOKEN = "SYNTHETIC_SECRET_MARKER";
  delete process.env.BLOB_STORE_ID;
  delete process.env.VERCEL_OIDC_TOKEN;
  try {
    await assert.rejects(officialBlobStore().create("returns/a/b.json", Buffer.from("{}")), (error) => {
      assert.equal(error.message, "blob_auth_unconfigured");
      assert.equal(JSON.stringify(error.message).includes("SYNTHETIC_SECRET_MARKER"), false);
      return true;
    });
  } finally {
    if (previous === undefined) delete process.env.BLOB_READ_WRITE_TOKEN;
    else process.env.BLOB_READ_WRITE_TOKEN = previous;
    if (previousStore === undefined) delete process.env.BLOB_STORE_ID;
    else process.env.BLOB_STORE_ID = previousStore;
    if (previousOidc === undefined) delete process.env.VERCEL_OIDC_TOKEN;
    else process.env.VERCEL_OIDC_TOKEN = previousOidc;
  }
});

test("zod override stays on the patched release", async () => {
  const lock = JSON.parse(await readFile(path.join(ROOT, "package-lock.json"), "utf8"));
  const zod = lock.packages["node_modules/zod"];
  assert.ok(zod);
  const parts = zod.version.split(".").map(Number);
  assert.ok(parts[0] > 4 || (parts[0] === 4 && (parts[1] > 6 || (parts[1] === 6 && parts[2] >= 6))));
});
