export const PUBLIC_PATH = "/v1/installations/:installationId/judgments/:eventId";
export const FUNCTION_PATH = "/api/v1/installations/:installationId/judgments/:eventId";

export function matchJudgmentRoute(urlPath) {
  const path = String(urlPath || "").split("?")[0];
  const match = path.match(/^\/(?:api\/)?v1\/installations\/([^/]+)\/judgments\/([^/]+)$/);
  if (!match) return null;
  return {
    installationId: decodeURIComponent(match[1]),
    eventId: decodeURIComponent(match[2]),
  };
}
