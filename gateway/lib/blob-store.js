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

export function officialBlobStore() {
  return {
    async create(pathname, body) {
      let result;
      try {
        result = await put(pathname, body, {
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
        throw error;
      }
      if (!result || result.pathname !== pathname) {
        throw Object.assign(new Error("pathname_rewritten"), { status: 500 });
      }
      return { pathname: result.pathname };
    },
    async read(pathname) {
      const result = await get(pathname, { access: "private", useCache: false });
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
