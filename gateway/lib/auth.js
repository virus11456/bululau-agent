import { createHash, timingSafeEqual } from "node:crypto";

function digest(value) {
  return createHash("sha256").update(value).digest();
}

function matches(secret, expectedHex) {
  if (typeof secret !== "string" || typeof expectedHex !== "string" || !/^[a-f0-9]{64}$/.test(expectedHex)) {
    return false;
  }
  const actual = digest(secret);
  const expected = Buffer.from(expectedHex, "hex");
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

export function authenticate(header, credentials, installationId) {
  if (!credentials || credentials.installationId !== installationId) {
    return { ok: false, status: 403, reason: "scope_rejected" };
  }
  if (credentials.publisher.keyId === credentials.reader.keyId) {
    return { ok: false, status: 503, reason: "credentials_unconfigured" };
  }
  if (typeof header !== "string" || !header.startsWith("Bearer ")) {
    return { ok: false, status: 401, reason: "unauthorized" };
  }
  const token = header.slice("Bearer ".length);
  const split = token.indexOf(":");
  if (split <= 0) return { ok: false, status: 401, reason: "unauthorized" };
  const keyId = token.slice(0, split);
  const secret = token.slice(split + 1);
  if (keyId === credentials.publisher.keyId && matches(secret, credentials.publisher.secretSha256)) {
    return { ok: true, role: "publisher" };
  }
  if (keyId === credentials.reader.keyId && matches(secret, credentials.reader.secretSha256)) {
    return { ok: true, role: "reader" };
  }
  return { ok: false, status: 403, reason: "scope_rejected" };
}

export function credentialsFromEnv(env = process.env) {
  const names = [
    "GATEWAY_INSTALLATION_ID",
    "GATEWAY_PUBLISHER_KEY_ID",
    "GATEWAY_PUBLISHER_SECRET_SHA256",
    "GATEWAY_READER_KEY_ID",
    "GATEWAY_READER_SECRET_SHA256",
  ];
  if (names.some((name) => typeof env[name] !== "string" || env[name].length === 0)) return null;
  return {
    installationId: env.GATEWAY_INSTALLATION_ID,
    publisher: {
      keyId: env.GATEWAY_PUBLISHER_KEY_ID,
      secretSha256: env.GATEWAY_PUBLISHER_SECRET_SHA256,
    },
    reader: {
      keyId: env.GATEWAY_READER_KEY_ID,
      secretSha256: env.GATEWAY_READER_SECRET_SHA256,
    },
  };
}
