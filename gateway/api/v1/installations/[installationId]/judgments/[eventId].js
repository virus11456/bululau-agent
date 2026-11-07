import { credentialsFromEnv } from "../../../../../../lib/auth.js";
import { officialBlobStore } from "../../../../../../lib/blob-store.js";
import { MAX_BYTES } from "../../../../../../lib/contract.js";
import { handle } from "../../../../../../lib/handler.js";

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

export default async function judgmentRoute(req, res) {
  const read = req.method === "PUT" ? await readLimited(req) : { tooLarge: false, raw: Buffer.alloc(0) };
  const result = read.tooLarge
    ? { status: 400, headers: { "Cache-Control": "private, no-store" }, body: { reason: "too_large" } }
    : await handle({
        method: req.method,
        installationId: req.query.installationId,
        eventId: req.query.eventId,
        authorization: req.headers.authorization,
        credentials: credentialsFromEnv(),
        raw: read.raw,
        now: new Date(),
        store: officialBlobStore(),
      });
  res.setHeader("Cache-Control", result.headers["Cache-Control"]);
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.status(result.status).json(result.body);
}
