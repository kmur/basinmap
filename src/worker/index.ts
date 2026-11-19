/**
 * Cloudflare Worker entrypoint for basinmap.org.
 *
 * Workers Static Assets handles all non-/api/* requests directly at zero
 * Worker invocation cost. Requests matching /api/* execute here first
 * via `assets.run_worker_first = ["/api/*"]`.
 */

export interface Env {
  ASSETS: Fetcher;
  DB?: D1Database;
  PUBLIC_BUCKET?: R2Bucket;
  INTAKE_BUCKET?: R2Bucket;
  PUBLIC_DATA_URL?: string;
  ENVIRONMENT?: string;
}

const NO_STORE_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "private, no-store",
};

const CONFIG_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "public, max-age=60, must-revalidate",
};

function jsonResponse(body: unknown, status: number, headers: Record<string, string>): Response {
  return new Response(JSON.stringify(body, null, 2), { status, headers });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const { pathname } = url;

    if (pathname.startsWith("/api/")) {
      if (pathname === "/api/health" && request.method === "GET") {
        return jsonResponse(
          {
            status: "ok",
            service: "basinmap-worker-api",
            environment: env.ENVIRONMENT ?? "production",
            public_data_url: env.PUBLIC_DATA_URL ?? "",
            bindings: {
              d1: Boolean(env.DB),
              public_r2: Boolean(env.PUBLIC_BUCKET),
              intake_r2: Boolean(env.INTAKE_BUCKET),
            },
            timestamp: new Date().toISOString(),
          },
          200,
          NO_STORE_HEADERS
        );
      }

      if (pathname === "/api/config" && request.method === "GET") {
        return jsonResponse(
          {
            public_data_url: env.PUBLIC_DATA_URL ?? "",
            manifest_path: "/releases/manifest.json",
            approved_overlay_path: "/overlays/approved.geojson",
            supported_licenses: ["CC-BY 4.0", "CC0 1.0"],
            submissions_enabled: true,
          },
          200,
          CONFIG_HEADERS
        );
      }

      return jsonResponse(
        {
          error: "Not found",
          path: pathname,
        },
        404,
        NO_STORE_HEADERS
      );
    }

    return env.ASSETS.fetch(request);
  },
};
