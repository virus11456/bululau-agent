import { authenticate } from "./auth.js";
import { isAlreadyExists } from "./blob-store.js";
import { judgmentPath, validatePut } from "./contract.js";

function response(status, body) {
  return {
    status,
    headers: { "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff" },
    body,
  };
}

function failure(error) {
  const status = error.status || 400;
  return response(status, { reason: error.message || "rejected" });
}

export async function handle(input) {
  const method = input.method || "";
  if (method !== "PUT" && method !== "GET") {
    return response(405, { reason: "method_not_allowed" });
  }
  let pathname;
  try {
    pathname = judgmentPath(input.installationId, input.eventId);
  } catch (error) {
    return failure(error);
  }
  if (!input.credentials) return response(503, { reason: "credentials_unconfigured" });
  const auth = authenticate(input.authorization, input.credentials, input.installationId);
  if (!auth.ok) return response(auth.status, { reason: auth.reason });
  if (method === "PUT" && auth.role !== "publisher") return response(403, { reason: "scope_rejected" });
  if (method === "GET" && auth.role !== "reader") return response(403, { reason: "scope_rejected" });
  if (method === "GET") {
    const stored = await input.store.read(pathname);
    if (!stored) return response(404, { reason: "not_found" });
    if (stored.length > 64 * 1024) return response(400, { reason: "too_large" });
    try {
      return response(200, JSON.parse(stored.toString("utf8")));
    } catch {
      return response(502, { reason: "store_failed" });
    }
  }
  let parsed;
  try {
    parsed = validatePut({ raw: input.raw }, input.installationId, input.eventId, input.now || new Date());
  } catch (error) {
    return failure(error);
  }
  const raw = Buffer.from(JSON.stringify(parsed));
  try {
    await input.store.create(pathname, raw);
    return response(201, parsed);
  } catch (error) {
    if (!isAlreadyExists(error)) return response(503, { reason: "store_failed" });
    const stored = await input.store.read(pathname);
    if (!stored) return response(409, { reason: "conflict" });
    try {
      const existing = JSON.parse(stored.toString("utf8"));
      if (existing.judgment_sha256 === parsed.judgment_sha256 && existing.envelope_sha256 === parsed.envelope_sha256) {
        return response(200, existing);
      }
    } catch {
      return response(409, { reason: "conflict" });
    }
    return response(409, { reason: "conflict" });
  }
}
