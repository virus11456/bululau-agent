import { get, put } from "@vercel/blob";

export const ATOMIC_CREATE = {
  provenBy: "s3-if-none-match",
  sdkOption: "allowOverwrite: false",
  requestHeader: "x-allow-overwrite: 0",
  serverCondition: "If-None-Match: *",
  localLinkIsNotThisMechanism: true,
  getThenPutIsNotThisMechanism: true,
};

export function isAlreadyExists(error) {
  const status = error && (error.status || error.statusCode);
  if (status === 409 || status === 412) return true;
  const message = String((error && error.message) || "").toLowerCase();
  return message.includes("already exists") || message.includes("blob_already_exists");
}

export function officialBlobStore(env = process.env) {
  const storeId = typeof env.BLOB_STORE_ID === "string" ? env.BLOB_STORE_ID.trim() : "";
  const oidcToken = typeof env.VERCEL_OIDC_TOKEN === "string" ? env.VERCEL_OIDC_TOKEN.trim() : "";
  if (!storeId || !oidcToken) {
    return {
      async create() {
        throw Object.assign(new Error("blob_auth_unconfigured"), { status: 503 });
      },
      async read() {
        throw Object.assign(new Error("blob_auth_unconfigured"), { status: 503 });
      },
    };
  }
  const auth = { storeId, oidcToken };
  return {
    async create(pathname, body) {
      let result;
      try {
        result = await put(pathname, body, {
          ...auth,
          access: "private",
          allowOverwrite: false,
          addRandomSuffix: false,
          cacheControlMaxAge: 60,
          contentType: "application/json",
          multipart: false,
        });
      } catch (error) {
        if (isAlreadyExists(error)) {
          throw Object.assign(new Error("blob_already_exists"), { status: 409 });
        }
        throw Object.assign(new Error("store_failed"), { status: 502 });
      }
      if (!result || result.pathname !== pathname) {
        throw Object.assign(new Error("pathname_rewritten"), { status: 500 });
      }
      return { pathname: result.pathname };
    },
    async read(pathname) {
      const result = await get(pathname, { ...auth, access: "private", useCache: false });
      if (!result || result.statusCode !== 200 || !result.stream) return null;
      const reader = result.stream.getReader();
      const chunks = [];
      while (true) {
        const step = await reader.read();
        if (step.done) break;
        chunks.push(Buffer.from(step.value));
      }
      return Buffer.concat(chunks);
    },
  };
}
