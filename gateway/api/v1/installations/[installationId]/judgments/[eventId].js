import { credentialsFromEnv } from "../../../../../lib/auth.js";
import { officialBlobStore } from "../../../../../lib/blob-store.js";
import { MAX_BYTES } from "../../../../../lib/contract.js";
import { handle } from "../../../../../lib/handler.js";
import { matchJudgmentRoute } from "../../../../../lib/route.js";

async function readLimited(req) {
  const declared = Number(req.headers["content-length"] || 0);
  if (Number.isFinite(declared) && declared > MAX_BYTES) return { tooLarge: true, raw: Buffer.alloc(0) };
  const chunks = [];
  let total = 0;
  for await (const chunk of req) {
    total += chunk.length;
    if (total > MAX_BYTES) return { tooLarge: true, raw: Buffer.alloc(0) };
    chunks.push(chunk);
  }
  return { tooLarge: false, raw: Buffer.concat(chunks) };
}

function requestPath(req) {
  const raw = req.url || "";
  if (raw.startsWith("http://") || raw.startsWith("https://")) return new URL(raw).pathname;
  return raw.split("?")[0];
}

export async function judgmentRoute(req, res, deps = {}) {
  let result;
  try {
    const matched = matchJudgmentRoute(requestPath(req));
    const query = req.query || {};
    if (matched && query.installationId && query.installationId !== matched.installationId) {
      result = { status: 400, headers: { "Cache-Control": "private, no-store" }, body: { reason: "binding_mismatch" } };
    } else if (matched && query.eventId && query.eventId !== matched.eventId) {
      result = { status: 400, headers: { "Cache-Control": "private, no-store" }, body: { reason: "binding_mismatch" } };
    } else {
      const read = req.method === "PUT" ? await readLimited(req) : { tooLarge: false, raw: Buffer.alloc(0) };
      result = read.tooLarge
        ? { status: 400, headers: { "Cache-Control": "private, no-store" }, body: { reason: "too_large" } }
        : await handle({
            method: req.method,
            installationId: query.installationId || (matched && matched.installationId),
            eventId: query.eventId || (matched && matched.eventId),
            authorization: req.headers.authorization,
            credentials: deps.credentials || credentialsFromEnv(),
            raw: read.raw,
            now: deps.now || new Date(),
            store: deps.store || officialBlobStore(),
          });
    }
  } catch {
    result = { status: 502, headers: { "Cache-Control": "private, no-store" }, body: { reason: "store_failed" } };
  }
  res.setHeader("Cache-Control", "private, no-store");
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.status(result.status).json(result.body);
}

export default function route(req, res) {
  return judgmentRoute(req, res);
}
