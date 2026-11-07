import { authenticate } from "./auth.js";
import { isAlreadyExists } from "./blob-store.js";
import { judgmentPath, validatePut, validateStored } from "./contract.js";

const FIXED = {
  rejected_id: 400,
  rejected_schema: 400,
  binding_mismatch: 400,
  digest_mismatch: 400,
  secret_rejected: 400,
  non_finite: 400,
  too_large: 400,
  expired: 400,
  unauthorized: 401,
  scope_rejected: 403,
  credentials_unconfigured: 503,
  blob_auth_unconfigured: 503,
  method_not_allowed: 405,
  not_found: 404,
  conflict: 409,
  store_failed: 502,
};

function response(status, body) {
  return {
    status,
    headers: { "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff" },
    body,
  };
}

function redacted(error) {
  const reason = error && Object.prototype.hasOwnProperty.call(FIXED, error.message) ? error.message : "store_failed";
  return response(FIXED[reason], { reason });
}

async function readBytes(store, pathname) {
  try {
    return await store.read(pathname);
  } catch {
    return redacted(Object.assign(new Error("store_failed"), { status: 502 }));
  }
}

function checked(raw, installationId, eventId) {
  try {
    return { parsed: validateStored(raw, installationId, eventId) };
  } catch {
    return { failed: response(502, { reason: "store_failed" }) };
  }
}

async function dispatch(input) {
  const method = input.method || "";
  if (method !== "PUT" && method !== "GET") {
    return response(405, { reason: "method_not_allowed" });
  }
  let pathname;
  try {
    pathname = judgmentPath(input.installationId, input.eventId);
  } catch (error) {
    return redacted(error);
  }
  if (!input.credentials) return response(503, { reason: "credentials_unconfigured" });
  const auth = authenticate(input.authorization, input.credentials, input.installationId);
  if (!auth.ok) return response(auth.status, { reason: auth.reason });
  if (method === "PUT" && auth.role !== "publisher") return response(403, { reason: "scope_rejected" });
  if (method === "GET" && auth.role !== "reader") return response(403, { reason: "scope_rejected" });
  if (method === "GET") {
    const stored = await readBytes(input.store, pathname);
    if (stored && stored.status) return stored;
    if (!stored) return response(404, { reason: "not_found" });
    const result = checked(stored, input.installationId, input.eventId);
    if (result.failed) return result.failed;
    return response(200, result.parsed);
  }
  let parsed;
  try {
    parsed = validatePut({ raw: input.raw }, input.installationId, input.eventId, input.now || new Date());
  } catch (error) {
    return redacted(error);
  }
  const raw = Buffer.from(JSON.stringify(parsed));
  try {
    await input.store.create(pathname, raw);
    return response(201, parsed);
  } catch (error) {
    if (!isAlreadyExists(error)) return response(502, { reason: "store_failed" });
  }
  const stored = await readBytes(input.store, pathname);
  if (stored && stored.status) return stored;
  if (!stored) return response(409, { reason: "conflict" });
  const existing = checked(stored, input.installationId, input.eventId);
  if (existing.failed) return existing.failed;
  if (existing.parsed.judgment_sha256 === parsed.judgment_sha256 && existing.parsed.envelope_sha256 === parsed.envelope_sha256) {
    return response(200, existing.parsed);
  }
  return response(409, { reason: "conflict" });
}

export async function handle(input) {
  try {
    return await dispatch(input);
  } catch {
    return response(502, { reason: "store_failed" });
  }
}
