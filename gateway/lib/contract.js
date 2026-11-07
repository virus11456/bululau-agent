import { createHash } from "node:crypto";

export const MAX_BYTES = 64 * 1024;
export const ID_RE = /^[A-Za-z0-9_-]{1,128}$/;
export const SHA_RE = /^[a-f0-9]{64}$/;
const PUT_FIELDS = [
  "schema_version",
  "event_type",
  "installation_id",
  "event_id",
  "evaluation_id",
  "envelope_sha256",
  "judgment_sha256",
  "judgment",
];
const JUDGMENT_FIELDS = [
  "schema_version",
  "event_type",
  "event_id",
  "evaluation_id",
  "envelope_sha256",
  "decision",
  "basis",
  "completed_at",
  "orders_placed",
  "ai_execution_proven",
];
const SECRET_KEYS = new Set([
  "authorization",
  "api_key",
  "api_secret",
  "token",
  "secret",
  "BLOB_READ_WRITE_TOKEN",
  "totalWalletBalance",
  "totalUnrealizedProfit",
  "day_start_equity",
]);

export function assertId(value) {
  if (typeof value !== "string" || !ID_RE.test(value)) {
    throw Object.assign(new Error("rejected_id"), { status: 400 });
  }
  return value;
}

export function judgmentPath(installationId, eventId) {
  assertId(installationId);
  assertId(eventId);
  return `returns/${installationId}/${eventId}.json`;
}

export function rejectUnsafeIntegerTokens(text) {
  const source = String(text);
  let index = 0;
  while (index < source.length) {
    const char = source[index];
    if (char === '"') {
      index += 1;
      while (index < source.length) {
        if (source[index] === "\\") {
          index += 2;
          continue;
        }
        if (source[index] === '"') {
          index += 1;
          break;
        }
        index += 1;
      }
      continue;
    }
    if (char === "-" || (char >= "0" && char <= "9")) {
      const start = index;
      if (source[index] === "-") index += 1;
      const digits = index;
      while (index < source.length && source[index] >= "0" && source[index] <= "9") index += 1;
      const marker = source[index];
      if (marker === "." || marker === "e" || marker === "E") {
        index += 1;
        while (index < source.length && /[0-9eE+.-]/.test(source[index])) index += 1;
        continue;
      }
      const token = source.slice(start, index);
      if (digits === index) {
        throw Object.assign(new Error("unsafe_integer"), { status: 400 });
      }
      let wide;
      let narrow;
      try {
        wide = BigInt(token);
        narrow = BigInt(Number(token));
      } catch {
        throw Object.assign(new Error("unsafe_integer"), { status: 400 });
      }
      if (wide !== narrow) {
        throw Object.assign(new Error("unsafe_integer"), { status: 400 });
      }
      continue;
    }
    index += 1;
  }
}

export function canonical(value) {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw Object.assign(new Error("non_finite"), { status: 400 });
    }
    return JSON.stringify(value);
  }
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonical(item)).join(",")}]`;
  }
  if (typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  throw Object.assign(new Error("rejected_schema"), { status: 400 });
}

export function sha256Hex(text) {
  return createHash("sha256").update(text).digest("hex");
}

function sameKeys(value, fields) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value);
  return keys.length === fields.length && fields.every((field) => keys.includes(field));
}

function rejectSecrets(value) {
  if (!value || typeof value !== "object") return;
  for (const key of Object.keys(value)) {
    if (SECRET_KEYS.has(key)) {
      throw Object.assign(new Error("secret_rejected"), { status: 400 });
    }
    rejectSecrets(value[key]);
  }
}

function hasTimezone(value) {
  return typeof value === "string" && /(Z|[+-]\d{2}:\d{2})$/.test(value);
}

export function validateStored(raw, installationId, eventId) {
  const bytes = Buffer.isBuffer(raw) ? raw : Buffer.from(String(raw));
  if (bytes.length > MAX_BYTES) {
    throw Object.assign(new Error("too_large"), { status: 400 });
  }
  let parsed;
  try {
    rejectUnsafeIntegerTokens(bytes.toString("utf8"));
    parsed = JSON.parse(bytes.toString("utf8"));
  } catch (error) {
    if (error && (error.message === "unsafe_integer" || error.message === "non_finite")) {
      throw error;
    }
    throw Object.assign(new Error("rejected_schema"), { status: 400 });
  }
  rejectSecrets(parsed);
  if (!sameKeys(parsed, PUT_FIELDS) || parsed.schema_version !== 1 || parsed.event_type !== "judgment_put") {
    throw Object.assign(new Error("rejected_schema"), { status: 400 });
  }
  if (parsed.installation_id !== installationId || parsed.event_id !== eventId) {
    throw Object.assign(new Error("binding_mismatch"), { status: 400 });
  }
  assertId(parsed.evaluation_id);
  if (!SHA_RE.test(parsed.envelope_sha256) || !SHA_RE.test(parsed.judgment_sha256)) {
    throw Object.assign(new Error("digest_mismatch"), { status: 400 });
  }
  const judgment = parsed.judgment;
  if (!sameKeys(judgment, JUDGMENT_FIELDS) || judgment.schema_version !== 1 || judgment.event_type !== "dry_run_judgment") {
    throw Object.assign(new Error("rejected_schema"), { status: 400 });
  }
  if (judgment.event_id !== eventId || judgment.evaluation_id !== parsed.evaluation_id) {
    throw Object.assign(new Error("binding_mismatch"), { status: 400 });
  }
  if (judgment.envelope_sha256 !== parsed.envelope_sha256) {
    throw Object.assign(new Error("digest_mismatch"), { status: 400 });
  }
  if (judgment.ai_execution_proven !== false || judgment.orders_placed !== false) {
    throw Object.assign(new Error("rejected_schema"), { status: 400 });
  }
  if (judgment.decision !== "keep_original_ai" && judgment.decision !== "no_trade") {
    throw Object.assign(new Error("rejected_schema"), { status: 400 });
  }
  if (!judgment.basis || typeof judgment.basis !== "object" || Array.isArray(judgment.basis)) {
    throw Object.assign(new Error("rejected_schema"), { status: 400 });
  }
  if (sha256Hex(canonical(judgment)) !== parsed.judgment_sha256) {
    throw Object.assign(new Error("digest_mismatch"), { status: 400 });
  }
  if (!hasTimezone(judgment.completed_at) || !Number.isFinite(Date.parse(judgment.completed_at))) {
    throw Object.assign(new Error("expired"), { status: 400 });
  }
  return parsed;
}

export function validatePut(document, installationId, eventId, now) {
  const parsed = validateStored(document.raw, installationId, eventId);
  const completed = Date.parse(parsed.judgment.completed_at);
  const current = now instanceof Date ? now.getTime() : Date.parse(now);
  if (!Number.isFinite(completed) || !Number.isFinite(current)) {
    throw Object.assign(new Error("expired"), { status: 400 });
  }
  if (completed > current + 60 * 1000 || current - completed > 15 * 60 * 1000) {
    throw Object.assign(new Error("expired"), { status: 400 });
  }
  return parsed;
}
