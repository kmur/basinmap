/**
 * Cloudflare Worker API + Static Assets entrypoint for basinmap.org.
 *
 * Workers Static Assets handles all non-/api/* paths directly at zero Worker cost.
 * All /api/* paths run here via `assets.run_worker_first = ["/api/*"]`.
 */

export interface Env {
  ASSETS: Fetcher;
  DB?: D1Database;
  PUBLIC_BUCKET?: R2Bucket;
  INTAKE_BUCKET?: R2Bucket;
  IMAGES?: {
    input(stream: ReadableStream): {
      transform(opts: Record<string, unknown>): {
        output(opts: Record<string, unknown>): Promise<{ response(): Response }>;
      };
    };
  };
  PUBLIC_DATA_URL?: string;
  ENVIRONMENT?: string;
  TURNSTILE_SECRET?: string;
}

interface UserRow {
  id: string;
  orcid_id: string | null;
  display_name: string;
  email: string | null;
  role: "contributor" | "reviewer" | "admin";
}

const MAX_UPLOAD_BYTES = 15 * 1024 * 1024;
const ALLOWED_LICENSES = new Set(["CC-BY 4.0", "CC0 1.0"]);
const SENSITIVE_SPECIES = new Set(["Salvelinus confluentus", "Actinemys marmorata"]);
const SENSITIVE_IUCN = new Set(["CR", "EN", "VU"]);

const DEV_TOKEN_MAP: Record<string, string> = {
  "dev-contributor-orcid": "usr-contrib-1",
  "dev-reviewer-orcid": "usr-review-1",
};

const NO_STORE_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "private, no-store",
};

const CONFIG_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "public, max-age=60, must-revalidate",
};

const ALLOWED_R2_PREFIXES = ["releases/", "overlays/", "media/", "exports/"];

function guessR2ContentType(key: string): string {
  const lower = key.toLowerCase();
  if (lower.endsWith(".pmtiles")) return "application/vnd.pmtiles";
  if (lower.endsWith(".json") || lower.endsWith(".geojson")) return "application/json; charset=utf-8";
  if (lower.endsWith(".zip")) return "application/zip";
  if (lower.endsWith(".xml")) return "application/xml; charset=utf-8";
  if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "image/jpeg";
  if (lower.endsWith(".png")) return "image/png";
  if (lower.endsWith(".webp")) return "image/webp";
  return "application/octet-stream";
}

function cacheControlForR2Key(key: string): string {
  if (key === "releases/manifest.json" || key.startsWith("overlays/")) {
    return "public, max-age=60, must-revalidate";
  }
  if (key.startsWith("releases/") || key.startsWith("media/")) {
    return "public, max-age=31536000, immutable";
  }
  return "public, max-age=60, must-revalidate";
}

async function handlePublicR2Proxy(request: Request, bucket: R2Bucket, rawKey: string): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return jsonResponse({ error: "Method not allowed" }, 405);
  }
  const key = decodeURIComponent(rawKey).replace(/^\/+/, "");
  if (!key || key.includes("..") || !ALLOWED_R2_PREFIXES.some((p) => key.startsWith(p))) {
    return jsonResponse({ error: "Not found", key }, 404);
  }

  const headObj = await bucket.head(key);
  if (!headObj) {
    return jsonResponse({ error: "Object not found", key }, 404);
  }

  const total = headObj.size;
  const baseHeaders: Record<string, string> = {
    "accept-ranges": "bytes",
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, HEAD",
    "access-control-allow-headers": "Range, If-Match",
    "access-control-expose-headers": "ETag, Content-Length, Content-Range",
    "content-type": headObj.httpMetadata?.contentType || guessR2ContentType(key),
    "cache-control": headObj.httpMetadata?.cacheControl || cacheControlForR2Key(key),
    etag: headObj.httpEtag,
  };

  if (request.method === "HEAD") {
    return new Response(null, {
      status: 200,
      headers: { ...baseHeaders, "content-length": String(total) },
    });
  }

  const rangeHdr = request.headers.get("range");
  if (rangeHdr) {
    const match = rangeHdr.trim().match(/^bytes=(\d*)-(\d*)$/);
    if (!match || (match[1] === "" && match[2] === "")) {
      return new Response(null, {
        status: 416,
        headers: { ...baseHeaders, "content-range": `bytes */${total}` },
      });
    }
    let start: number;
    let end: number;
    if (match[1] === "") {
      const suffix = Number(match[2]);
      start = Math.max(0, total - suffix);
      end = total - 1;
    } else {
      start = Number(match[1]);
      end = match[2] !== "" ? Number(match[2]) : total - 1;
    }
    if (start >= total || end < start) {
      return new Response(null, {
        status: 416,
        headers: { ...baseHeaders, "content-range": `bytes */${total}` },
      });
    }
    end = Math.min(end, total - 1);
    const length = end - start + 1;
    const rangedObj = await bucket.get(key, { range: { offset: start, length } });
    if (!rangedObj) {
      return jsonResponse({ error: "Object not found", key }, 404);
    }
    return new Response(rangedObj.body, {
      status: 206,
      headers: {
        ...baseHeaders,
        "content-length": String(length),
        "content-range": `bytes ${start}-${end}/${total}`,
      },
    });
  }

  const fullObj = await bucket.get(key);
  if (!fullObj) {
    return jsonResponse({ error: "Object not found", key }, 404);
  }
  return new Response(fullObj.body, {
    status: 200,
    headers: { ...baseHeaders, "content-length": String(total) },
  });
}

function jsonResponse(body: unknown, status: number, headers = NO_STORE_HEADERS): Response {
  return new Response(JSON.stringify(body, null, 2), { status, headers });
}

function detectMagicMime(head: Uint8Array): string | null {
  if (head.byteLength < 12) return null;
  if (head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff) return "image/jpeg";
  if (
    head[0] === 0x89 &&
    head[1] === 0x50 &&
    head[2] === 0x4e &&
    head[3] === 0x47 &&
    head[4] === 0x0d &&
    head[5] === 0x0a &&
    head[6] === 0x1a &&
    head[7] === 0x0a
  ) {
    return "image/png";
  }
  const riff = String.fromCharCode(head[0], head[1], head[2], head[3]);
  const webp = String.fromCharCode(head[8], head[9], head[10], head[11]);
  if (riff === "RIFF" && webp === "WEBP") return "image/webp";
  const ftyp = String.fromCharCode(head[4], head[5], head[6], head[7]);
  const brand = String.fromCharCode(head[8], head[9], head[10], head[11]);
  if (ftyp === "ftyp" && ["heic", "heix", "hevc", "mif1", "msf1"].includes(brand)) {
    return "image/heic";
  }
  return null;
}

function stripJpegExifSegments(bytes: Uint8Array): Uint8Array {
  if (bytes.byteLength < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) {
    return bytes;
  }
  const chunks: Uint8Array[] = [bytes.slice(0, 2)];
  let pos = 2;
  const stripMarkers = new Set([0xe1, 0xe2, 0xed, 0xfe]);
  while (pos + 2 <= bytes.byteLength) {
    if (bytes[pos] !== 0xff) {
      chunks.push(bytes.slice(pos));
      break;
    }
    const marker = bytes[pos + 1];
    if (marker === 0xda) {
      chunks.push(bytes.slice(pos));
      break;
    }
    if (marker === 0xd8 || marker === 0xd9 || marker === 0x00 || (marker >= 0xd0 && marker <= 0xd7)) {
      chunks.push(bytes.slice(pos, pos + 2));
      pos += 2;
      continue;
    }
    if (pos + 4 > bytes.byteLength) break;
    const segLen = (bytes[pos + 2] << 8) | bytes[pos + 3];
    const segEnd = pos + 2 + segLen;
    if (segLen < 2 || segEnd > bytes.byteLength) break;
    if (!stripMarkers.has(marker)) {
      chunks.push(bytes.slice(pos, segEnd));
    }
    pos = segEnd;
  }
  const total = chunks.reduce((acc, c) => acc + c.byteLength, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return out;
}

function generalizeCoordinates(
  lat: number,
  lon: number,
  uncertaintyM: number | null | undefined,
  scientificName: string,
  iucnCategory: string | null | undefined,
  explicitSensitive: boolean
) {
  const isSensitive =
    explicitSensitive ||
    SENSITIVE_SPECIES.has(scientificName) ||
    SENSITIVE_IUCN.has((iucnCategory || "").toUpperCase());
  if (isSensitive) {
    return {
      isSensitive: 1,
      publicLat: Math.round(lat * 10) / 10,
      publicLon: Math.round(lon * 10) / 10,
      publicUncertaintyM: Math.max(Number(uncertaintyM || 0), 10000),
      informationWithheld:
        "Coordinates generalized to 0.1 degree grid due to species sensitivity. Exact coordinates retained privately for reviewers.",
    };
  }
  return {
    isSensitive: 0,
    publicLat: lat,
    publicLon: lon,
    publicUncertaintyM: Number(uncertaintyM || 30),
    informationWithheld: null as string | null,
  };
}

async function authenticateUser(request: Request, db: D1Database): Promise<UserRow | null> {
  const auth = request.headers.get("authorization") || "";
  const match = auth.match(/^Bearer\s+(.+)$/i);
  if (!match) return null;
  const token = match[1].trim();
  const mappedId = DEV_TOKEN_MAP[token] || (token.startsWith("user:") ? token.slice(5) : null);
  if (mappedId) {
    return await db.prepare("SELECT * FROM users WHERE id = ?").bind(mappedId).first<UserRow>();
  }
  return await db.prepare("SELECT * FROM users WHERE orcid_id = ?").bind(token).first<UserRow>();
}

async function verifyTurnstile(request: Request, env: Env): Promise<boolean> {
  const token = (request.headers.get("x-turnstile-token") || "").trim();
  if (!token || token === "turnstile-fail") return false;
  if (!env.TURNSTILE_SECRET) return true;
  const resp = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ secret: env.TURNSTILE_SECRET, response: token }),
  });
  if (!resp.ok) return false;
  const outcome = (await resp.json()) as { success?: boolean };
  return Boolean(outcome.success);
}

async function rebuildApprovedOverlay(db: D1Database, publicBucket: R2Bucket): Promise<number> {
  const { results } = await db
    .prepare(
      `SELECT * FROM observations
       WHERE review_status = 'approved'
         AND publication_status != 'indexed_by_gbif'
       ORDER BY occurrence_id ASC`
    )
    .all<Record<string, unknown>>();

  const features: Array<Record<string, unknown>> = [];
  for (const row of results) {
    const mediaRes = await db
      .prepare(
        `SELECT public_r2_key FROM media_intake
         WHERE occurrence_id = ? AND derivative_status = 'public_sanitized'
         ORDER BY media_id ASC`
      )
      .bind(row.occurrence_id)
      .all<{ public_r2_key: string }>();

    features.push({
      type: "Feature",
      id: row.occurrence_id,
      properties: {
        occurrenceID: row.occurrence_id,
        scientificName: row.scientific_name,
        taxonKey: row.taxon_key,
        eventDate: row.event_date,
        year: row.year,
        basisOfRecord: row.basis_of_record,
        hybas_id: row.hybas_id,
        coordinateUncertaintyInMeters: row.public_coordinate_uncertainty_m,
        informationWithheld: row.information_withheld,
        isSensitiveSpecies: Boolean(row.is_sensitive_species),
        license: row.license,
        rightsHolder: row.rights_holder,
        recordedByID: row.recorded_by_id,
        identifiedByID: row.identified_by_id,
        reviewStatus: row.review_status,
        publicationStatus: row.publication_status,
        provisional: true,
        mediaUrls: mediaRes.results.map((m) => `/${m.public_r2_key}`),
      },
      geometry: {
        type: "Point",
        coordinates: [row.public_longitude, row.public_latitude],
      },
    });
  }

  const fc = {
    type: "FeatureCollection",
    name: "approved_local_observations",
    generated_at: new Date().toISOString(),
    features,
  };

  await publicBucket.put("overlays/approved.geojson", JSON.stringify(fc, null, 2) + "\n", {
    httpMetadata: {
      contentType: "application/json; charset=utf-8",
      cacheControl: "public, max-age=60, must-revalidate",
    },
  });
  return features.length;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const { pathname } = url;

    if (!pathname.startsWith("/api/")) {
      return env.ASSETS.fetch(request);
    }

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
        200
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

    if (pathname.startsWith("/api/r2/")) {
      if (!env.PUBLIC_BUCKET) {
        return jsonResponse({ error: "PUBLIC_BUCKET binding unavailable" }, 503);
      }
      return handlePublicR2Proxy(request, env.PUBLIC_BUCKET, pathname.slice("/api/r2/".length));
    }

    if (!env.DB) {
      return jsonResponse({ error: "D1 database binding unavailable" }, 503);
    }

    if (pathname === "/api/auth/me" && request.method === "GET") {
      const user = await authenticateUser(request, env.DB);
      if (!user) return jsonResponse({ error: "Unauthorized" }, 401);
      return jsonResponse({ user }, 200);
    }

    if (pathname === "/api/observations" && request.method === "POST") {
      const user = await authenticateUser(request, env.DB);
      if (!user) return jsonResponse({ error: "Unauthorized" }, 401);
      if (!(await verifyTurnstile(request, env))) {
        return jsonResponse({ error: "Turnstile verification failed" }, 403);
      }

      const body = (await request.json().catch(() => null)) as Record<string, unknown> | null;
      if (!body) return jsonResponse({ error: "Invalid JSON body" }, 400);

      const idemKey = (request.headers.get("idempotency-key") || String(body.idempotency_key || "")).trim();
      if (!idemKey) return jsonResponse({ error: "Idempotency-Key required" }, 400);

      const existing = await env.DB.prepare("SELECT * FROM observations WHERE idempotency_key = ?")
        .bind(idemKey)
        .first<Record<string, unknown>>();
      if (existing) {
        return jsonResponse({ idempotent_replay: true, observation: existing }, 200);
      }

      const license = String(body.license || "").trim();
      if (!ALLOWED_LICENSES.has(license)) {
        return jsonResponse({ error: "License must be CC-BY 4.0 or CC0 1.0" }, 400);
      }

      const sciName = String(body.scientificName || "").trim();
      const eventDate = String(body.eventDate || "").trim();
      const lat = Number(body.decimalLatitude);
      const lon = Number(body.decimalLongitude);
      if (!sciName || !eventDate || Number.isNaN(lat) || Number.isNaN(lon)) {
        return jsonResponse({ error: "scientificName, eventDate, decimalLatitude, decimalLongitude required" }, 400);
      }

      const gen = generalizeCoordinates(
        lat,
        lon,
        body.coordinateUncertaintyInMeters as number | undefined,
        sciName,
        body.iucnCategory as string | undefined,
        Boolean(body.isSensitiveSpecies)
      );
      const occId = `urn:basinmap:occ:${crypto.randomUUID()}`;
      const now = new Date().toISOString();
      const rightsHolder = String(body.rightsHolder || user.display_name);

      await env.DB.batch([
        env.DB.prepare(
          `INSERT INTO observations (
            occurrence_id, idempotency_key, submitter_user_id, revision_number,
            review_status, publication_status,
            verbatim_scientific_name, verbatim_event_date, verbatim_latitude,
            verbatim_longitude, verbatim_coordinate_uncertainty_m, verbatim_locality,
            scientific_name, taxon_key, family, iucn_category,
            is_sensitive_species, public_latitude, public_longitude,
            public_coordinate_uncertainty_m, information_withheld,
            event_date, year, basis_of_record, hybas_id, locality,
            recorded_by, recorded_by_id, license, rights_holder, created_at, updated_at
          ) VALUES (?, ?, ?, 1, 'submitted', 'not_exported', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
        ).bind(
          occId,
          idemKey,
          user.id,
          sciName,
          eventDate,
          lat,
          lon,
          Number(body.coordinateUncertaintyInMeters || 30),
          body.locality ?? null,
          sciName,
          body.taxonKey ?? null,
          body.family ?? null,
          body.iucnCategory ?? null,
          gen.isSensitive,
          gen.publicLat,
          gen.publicLon,
          gen.publicUncertaintyM,
          gen.informationWithheld,
          eventDate,
          Number(eventDate.slice(0, 4)),
          body.basisOfRecord ?? "HUMAN_OBSERVATION",
          body.hybas_id ?? null,
          body.locality ?? null,
          user.display_name,
          user.orcid_id,
          license,
          rightsHolder,
          now,
          now
        ),
        env.DB.prepare(
          `INSERT INTO observation_revisions
            (occurrence_id, revision_number, actor_user_id, change_type, snapshot_json, notes, created_at)
           VALUES (?, 1, ?, 'created', ?, 'Initial contributor submission', ?)`
        ).bind(occId, user.id, JSON.stringify({ occurrence_id: occId, scientificName: sciName }), now),
      ]);

      const inserted = await env.DB.prepare("SELECT * FROM observations WHERE occurrence_id = ?")
        .bind(occId)
        .first();
      return jsonResponse({ idempotent_replay: false, observation: inserted }, 201);
    }

    const obsMatch = pathname.match(/^\/api\/observations\/([^/]+)$/);
    if (obsMatch && request.method === "GET") {
      const occId = decodeURIComponent(obsMatch[1]);
      const user = await authenticateUser(request, env.DB);
      const row = await env.DB.prepare("SELECT * FROM observations WHERE occurrence_id = ?")
        .bind(occId)
        .first<Record<string, unknown>>();
      if (!row) return jsonResponse({ error: "Observation not found" }, 404);

      const isOwner = Boolean(user && user.id === row.submitter_user_id);
      const isReviewer = Boolean(user && (user.role === "reviewer" || user.role === "admin"));
      if (row.review_status !== "approved" && !isOwner && !isReviewer) {
        return jsonResponse({ error: "Forbidden" }, 403);
      }

      const responseRow: Record<string, unknown> = { ...row };
      if (!isOwner && !isReviewer) {
        delete responseRow.verbatim_latitude;
        delete responseRow.verbatim_longitude;
        delete responseRow.verbatim_notes;
      }
      return jsonResponse({ observation: responseRow }, 200);
    }

    if (obsMatch && request.method === "PATCH") {
      const occId = decodeURIComponent(obsMatch[1]);
      const user = await authenticateUser(request, env.DB);
      if (!user) return jsonResponse({ error: "Unauthorized" }, 401);

      const row = await env.DB.prepare("SELECT * FROM observations WHERE occurrence_id = ?")
        .bind(occId)
        .first<Record<string, unknown>>();
      if (!row) return jsonResponse({ error: "Observation not found" }, 404);

      const isOwner = user.id === row.submitter_user_id;
      const isReviewer = user.role === "reviewer" || user.role === "admin";
      if (!isOwner && !isReviewer) return jsonResponse({ error: "Forbidden" }, 403);

      const body = (await request.json().catch(() => null)) as Record<string, unknown> | null;
      if (!body) return jsonResponse({ error: "Invalid JSON body" }, 400);

      const sciName = String(body.scientificName ?? row.scientific_name).trim();
      const eventDate = String(body.eventDate ?? row.event_date).trim();
      const lat = body.decimalLatitude !== undefined ? Number(body.decimalLatitude) : Number(row.verbatim_latitude);
      const lon = body.decimalLongitude !== undefined ? Number(body.decimalLongitude) : Number(row.verbatim_longitude);
      const unc = body.coordinateUncertaintyInMeters !== undefined
        ? Number(body.coordinateUncertaintyInMeters)
        : (row.verbatim_coordinate_uncertainty_m as number | undefined);
      const iucn = (body.iucnCategory ?? row.iucn_category) as string | undefined;
      const gen = generalizeCoordinates(
        lat,
        lon,
        unc,
        sciName,
        iucn,
        Boolean(body.isSensitiveSpecies ?? row.is_sensitive_species)
      );
      const newRev = Number(row.revision_number) + 1;
      const nextStatus = isReviewer ? String(row.review_status) : "submitted";
      const now = new Date().toISOString();

      await env.DB.batch([
        env.DB.prepare(
          `UPDATE observations
           SET revision_number = ?, review_status = ?, scientific_name = ?,
               iucn_category = ?, is_sensitive_species = ?, public_latitude = ?,
               public_longitude = ?, public_coordinate_uncertainty_m = ?,
               information_withheld = ?, verbatim_scientific_name = ?,
               verbatim_event_date = ?, verbatim_latitude = ?, verbatim_longitude = ?,
               verbatim_coordinate_uncertainty_m = ?, event_date = ?, year = ?,
               locality = ?, verbatim_locality = ?, updated_at = ?
           WHERE occurrence_id = ?`
        ).bind(
          newRev,
          nextStatus,
          sciName,
          iucn ?? null,
          gen.isSensitive,
          gen.publicLat,
          gen.publicLon,
          gen.publicUncertaintyM,
          gen.informationWithheld,
          sciName,
          eventDate,
          lat,
          lon,
          unc ?? null,
          eventDate,
          Number(eventDate.slice(0, 4)),
          body.locality ?? row.locality ?? null,
          body.locality ?? row.verbatim_locality ?? null,
          now,
          occId
        ),
        env.DB.prepare(
          `INSERT INTO observation_revisions
            (occurrence_id, revision_number, actor_user_id, change_type, snapshot_json, notes, created_at)
           VALUES (?, ?, ?, 'updated', ?, ?, ?)`
        ).bind(
          occId,
          newRev,
          user.id,
          JSON.stringify({ occurrence_id: occId, scientificName: sciName, revision_number: newRev }),
          String(body.notes ?? "Observation corrected while preserving occurrenceID"),
          now
        ),
      ]);

      if (row.review_status === "approved" && env.PUBLIC_BUCKET) {
        await rebuildApprovedOverlay(env.DB, env.PUBLIC_BUCKET);
      }

      const updated = await env.DB.prepare("SELECT * FROM observations WHERE occurrence_id = ?")
        .bind(occId)
        .first();
      return jsonResponse({ observation: updated }, 200);
    }

    const uploadMatch = pathname.match(/^\/api\/uploads\/([^/]+)$/);
    if (uploadMatch && request.method === "PUT") {
      const occId = decodeURIComponent(uploadMatch[1]);
      const user = await authenticateUser(request, env.DB);
      if (!user) return jsonResponse({ error: "Unauthorized" }, 401);
      if (!env.INTAKE_BUCKET) return jsonResponse({ error: "INTAKE_BUCKET not bound" }, 503);

      const lenHdr = request.headers.get("content-length");
      if (!lenHdr || !/^\d+$/.test(lenHdr)) {
        return jsonResponse({ error: "Content-Length header required for streaming R2 put" }, 411);
      }
      const byteLength = Number(lenHdr);
      if (byteLength <= 0 || byteLength > MAX_UPLOAD_BYTES) {
        return jsonResponse({ error: "Upload must be between 1 byte and 15 MiB" }, 413);
      }
      if (!request.body) return jsonResponse({ error: "Empty request body" }, 400);

      const obs = await env.DB.prepare("SELECT * FROM observations WHERE occurrence_id = ?")
        .bind(occId)
        .first<Record<string, unknown>>();
      if (!obs) return jsonResponse({ error: "Observation not found" }, 404);
      if (user.id !== obs.submitter_user_id && user.role !== "reviewer" && user.role !== "admin") {
        return jsonResponse({ error: "Forbidden" }, 403);
      }

      // Inspect magic bytes from the leading chunk before streaming to R2.
      const reader = request.body.getReader();
      const first = await reader.read();
      if (first.done || !first.value) return jsonResponse({ error: "Empty stream" }, 400);
      const mimeType = detectMagicMime(first.value);
      if (!mimeType) {
        await reader.cancel();
        return jsonResponse({ error: "Unsupported Media Type based on magic bytes" }, 415);
      }

      const chunks: Uint8Array[] = [first.value];
      let total = first.value.byteLength;
      while (true) {
        const next = await reader.read();
        if (next.done) break;
        total += next.value.byteLength;
        if (total > MAX_UPLOAD_BYTES) {
          await reader.cancel();
          return jsonResponse({ error: "Upload exceeded 15 MiB ceiling" }, 413);
        }
        chunks.push(next.value);
      }
      const fullBytes = new Uint8Array(total);
      let off = 0;
      for (const c of chunks) {
        fullBytes.set(c, off);
        off += c.byteLength;
      }

      const mediaId = crypto.randomUUID();
      const privateKey = `intake/${occId}/${mediaId}.bin`;
      await env.INTAKE_BUCKET.put(privateKey, fullBytes, {
        httpMetadata: { contentType: mimeType },
      });

      const hashBuf = await crypto.subtle.digest("SHA-256", fullBytes);
      const shaHex = Array.from(new Uint8Array(hashBuf))
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("");

      const now = new Date().toISOString();
      await env.DB.prepare(
        `INSERT INTO media_intake (
          media_id, occurrence_id, uploader_user_id, private_r2_key,
          detected_mime_type, byte_length, sha256, license, rights_holder,
          derivative_status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'private_only', ?)`
      )
        .bind(mediaId, occId, user.id, privateKey, mimeType, total, shaHex, obs.license, obs.rights_holder, now)
        .run();

      return jsonResponse(
        {
          media_id: mediaId,
          occurrence_id: occId,
          detected_mime_type: mimeType,
          byte_length: total,
          sha256: shaHex,
          derivative_status: "private_only",
        },
        201
      );
    }

    const reviewMatch = pathname.match(/^\/api\/review\/([^/]+)\/(approve|needs_changes)$/);
    if (reviewMatch && request.method === "POST") {
      const occId = decodeURIComponent(reviewMatch[1]);
      const decision = reviewMatch[2] === "approve" ? "approved" : "needs_changes";
      const reviewer = await authenticateUser(request, env.DB);
      if (!reviewer) return jsonResponse({ error: "Unauthorized" }, 401);
      if (reviewer.role !== "reviewer" && reviewer.role !== "admin") {
        return jsonResponse({ error: "Forbidden: reviewer role required" }, 403);
      }
      if (!env.INTAKE_BUCKET || !env.PUBLIC_BUCKET) {
        return jsonResponse({ error: "R2 bindings unavailable" }, 503);
      }

      const obs = await env.DB.prepare("SELECT * FROM observations WHERE occurrence_id = ?")
        .bind(occId)
        .first<Record<string, unknown>>();
      if (!obs) return jsonResponse({ error: "Observation not found" }, 404);

      const body = (await request.json().catch(() => ({}))) as { notes?: string };
      const now = new Date().toISOString();
      const newRev = Number(obs.revision_number) + 1;

      await env.DB.prepare(
        `UPDATE observations
         SET review_status = ?, revision_number = ?, reviewer_user_id = ?,
             identified_by = ?, identified_by_id = ?, review_notes = ?,
             reviewed_at = ?, updated_at = ?
         WHERE occurrence_id = ?`
      )
        .bind(
          decision,
          newRev,
          reviewer.id,
          reviewer.display_name,
          reviewer.orcid_id,
          body.notes ?? "",
          now,
          now,
          occId
        )
        .run();

      const mediaRows = await env.DB.prepare("SELECT * FROM media_intake WHERE occurrence_id = ?")
        .bind(occId)
        .all<Record<string, unknown>>();

      if (decision === "approved") {
        for (const m of mediaRows.results) {
          const obj = await env.INTAKE_BUCKET.get(String(m.private_r2_key));
          if (!obj) continue;
          const publicKey = `media/${m.media_id}.jpg`;
          let sanitizedBytes: Uint8Array;
          if (env.IMAGES) {
            const transformed = await env.IMAGES.input(obj.body)
              .transform({ rotate: 0 })
              .output({ format: "image/jpeg", quality: 88, metadata: "none" });
            sanitizedBytes = new Uint8Array(await transformed.response().arrayBuffer());
          } else {
            sanitizedBytes = stripJpegExifSegments(new Uint8Array(await obj.arrayBuffer()));
          }
          await env.PUBLIC_BUCKET.put(publicKey, sanitizedBytes, {
            httpMetadata: {
              contentType: "image/jpeg",
              cacheControl: "public, max-age=31536000, immutable",
            },
          });
          await env.DB.prepare(
            "UPDATE media_intake SET public_r2_key = ?, derivative_status = 'public_sanitized' WHERE media_id = ?"
          )
            .bind(publicKey, m.media_id)
            .run();
        }
      } else {
        for (const m of mediaRows.results) {
          if (m.public_r2_key) {
            await env.PUBLIC_BUCKET.delete(String(m.public_r2_key));
          }
          await env.DB.prepare(
            "UPDATE media_intake SET public_r2_key = NULL, derivative_status = 'revoked' WHERE media_id = ?"
          )
            .bind(m.media_id)
            .run();
        }
      }

      await env.DB.prepare(
        `INSERT INTO observation_revisions
          (occurrence_id, revision_number, actor_user_id, change_type, snapshot_json, notes, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      )
        .bind(occId, newRev, reviewer.id, decision, JSON.stringify({ review_status: decision }), body.notes ?? "", now)
        .run();

      const overlayCount = await rebuildApprovedOverlay(env.DB, env.PUBLIC_BUCKET);
      return jsonResponse({ occurrence_id: occId, review_status: decision, overlay_record_count: overlayCount }, 200);
    }

    return jsonResponse({ error: "Not found", path: pathname }, 404);
  },
};
