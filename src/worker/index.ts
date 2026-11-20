/**
 * Cloudflare Worker API + Static Assets entrypoint for basinmap.org.
 *
 * Workers Static Assets handles all non-/api/* paths directly at zero Worker cost.
 * All /api/* paths run here via `assets.run_worker_first = ["/api/*"]`.
 *
 * Keep behaviour synchronized with scripts/backend_engine.py + scripts/dev_server.py
 * (AGENTS.md invariant 4); tests/worker_harness.test.mts executes this file under Node.
 */

import type { AuthContext, AuthEnv, UserRow } from "./auth.ts";
import {
  authenticate,
  checkMutationOrigin,
  handleCallback,
  handleLogin,
  handleLogout,
  isDevelopment,
  orcidBaseUrl,
  orcidConfigured,
} from "./auth.ts";
import { detectMagicMime, extractExif, haversineMeters, stripJpegExifSegments } from "./media.ts";

export interface Env extends AuthEnv {
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
  TURNSTILE_SITE_KEY?: string;
}

const MAX_UPLOAD_BYTES = 15 * 1024 * 1024;
const MAX_MEDIA_PER_OBSERVATION = 10;
const MAX_SUBMISSIONS_PER_HOUR = 30;
const ALLOWED_LICENSES = new Set(["CC-BY 4.0", "CC0 1.0"]);
const SENSITIVE_SPECIES = new Set(["Salvelinus confluentus", "Actinemys marmorata"]);
const SENSITIVE_IUCN = new Set(["CR", "EN", "VU"]);
const REVIEW_QUEUE_STATUSES = new Set(["submitted", "needs_changes", "approved"]);

const NO_STORE_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "private, no-store",
  "x-content-type-options": "nosniff",
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

function jsonResponse(body: unknown, status: number, headers: Record<string, string> = NO_STORE_HEADERS): Response {
  return new Response(JSON.stringify(body, null, 2), { status, headers });
}

function errorResponse(status: number, error: string, code: string): Response {
  return jsonResponse({ error, code }, status);
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
        "Coordinates generalized to 0.1 degree grid due to species sensitivity. Exact coordinates retained privately for authorized reviewers.",
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

/** Same shape as backend_engine._row_to_public_dict so the frontend works against both. */
function serializeObservation(row: Record<string, unknown>, includeVerbatim: boolean): Record<string, unknown> {
  const out: Record<string, unknown> = {
    occurrence_id: row.occurrence_id,
    revision_number: row.revision_number,
    review_status: row.review_status,
    publication_status: row.publication_status,
    scientific_name: row.scientific_name,
    taxon_key: row.taxon_key,
    kingdom: row.kingdom,
    family: row.family,
    iucn_category: row.iucn_category,
    is_sensitive_species: Boolean(row.is_sensitive_species),
    public_latitude: row.public_latitude,
    public_longitude: row.public_longitude,
    public_coordinate_uncertainty_m: row.public_coordinate_uncertainty_m,
    information_withheld: row.information_withheld,
    geodetic_datum: row.geodetic_datum,
    event_date: row.event_date,
    year: row.year,
    basis_of_record: row.basis_of_record,
    hybas_id: row.hybas_id,
    locality: row.locality,
    recorded_by: row.recorded_by,
    recorded_by_id: row.recorded_by_id,
    identified_by: row.identified_by,
    identified_by_id: row.identified_by_id,
    license: row.license,
    license_accepted_at: row.license_accepted_at ?? null,
    rights_holder: row.rights_holder,
    reviewed_at: row.reviewed_at,
    created_at: row.created_at,
    updated_at: row.updated_at,
  };
  if (includeVerbatim) {
    out.verbatim = {
      scientific_name: row.verbatim_scientific_name,
      event_date: row.verbatim_event_date,
      latitude: row.verbatim_latitude,
      longitude: row.verbatim_longitude,
      coordinate_uncertainty_m: row.verbatim_coordinate_uncertainty_m,
      locality: row.verbatim_locality,
      notes: row.verbatim_notes,
      review_notes: row.review_notes,
    };
  }
  return out;
}

interface ObservationInput {
  scientificName: string;
  eventDate: string;
  year: number;
  lat: number;
  lon: number;
  uncertainty: number;
  locality: string | null;
  notes: string | null;
}

/** Shared validation for POST and PATCH. `base` supplies existing values for partial PATCH bodies. */
function validateObservationInput(
  body: Record<string, unknown>,
  base: Record<string, unknown> | null
): { ok: true; value: ObservationInput } | { ok: false; response: Response } {
  const pick = (key: string, fallback: unknown) => (body[key] !== undefined && body[key] !== null ? body[key] : fallback);
  const sci = String(pick("scientificName", base?.scientific_name ?? "")).trim();
  const date = String(pick("eventDate", base?.event_date ?? "")).trim();
  const lat = Number(pick("decimalLatitude", base?.verbatim_latitude ?? NaN));
  const lon = Number(pick("decimalLongitude", base?.verbatim_longitude ?? NaN));
  const unc = Number(pick("coordinateUncertaintyInMeters", base?.verbatim_coordinate_uncertainty_m ?? 30));
  const locality = pick("locality", base?.locality ?? null);
  const notes = body.notes !== undefined && body.notes !== null ? String(body.notes) : null;
  const bad = (msg: string, code: string) => ({ ok: false as const, response: errorResponse(400, msg, code) });

  if (!sci || sci.length > 200) return bad("scientificName is required (max 200 characters)", "invalid_scientific_name");
  if (!/^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$/.test(date)) {
    return bad("eventDate must be ISO 8601 (YYYY-MM-DD or YYYY-MM-DDThh:mm)", "invalid_date");
  }
  const parsed = Date.parse(date.slice(0, 10) + "T00:00:00Z");
  if (Number.isNaN(parsed) || parsed > Date.now() + 36 * 3600 * 1000 || Number(date.slice(0, 4)) < 1700) {
    return bad("eventDate must be a real date that is not in the future", "invalid_date");
  }
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
    return bad("Valid decimalLatitude and decimalLongitude are required", "invalid_coordinates");
  }
  if (!Number.isFinite(unc) || unc < 1 || unc > 100000) {
    return bad("coordinateUncertaintyInMeters must be between 1 and 100000", "invalid_uncertainty");
  }
  if (locality !== null && String(locality).length > 1000) return bad("locality is limited to 1000 characters", "invalid_locality");
  if (notes !== null && notes.length > 4000) return bad("notes are limited to 4000 characters", "invalid_notes");
  return {
    ok: true,
    value: {
      scientificName: sci,
      eventDate: date,
      year: Number(date.slice(0, 4)),
      lat,
      lon,
      uncertainty: Math.round(unc),
      locality: locality === null ? null : String(locality).trim() || null,
      notes,
    },
  };
}

/** Turnstile siteverify. Fails closed outside development when no secret is configured. */
async function verifyTurnstile(request: Request, env: Env): Promise<Response | null> {
  const token = (request.headers.get("x-turnstile-token") || "").trim();
  if (!token || token === "turnstile-fail") {
    return errorResponse(403, "Turnstile verification failed or missing X-Turnstile-Token", "turnstile_failed");
  }
  if (!env.TURNSTILE_SECRET) {
    return isDevelopment(env)
      ? null
      : errorResponse(503, "Turnstile is not configured for this environment", "turnstile_not_configured");
  }
  const form = new URLSearchParams({ secret: env.TURNSTILE_SECRET, response: token });
  const ip = request.headers.get("cf-connecting-ip");
  if (ip) form.set("remoteip", ip);
  const resp = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: form,
  });
  const outcome = (await resp.json().catch(() => ({}))) as { success?: boolean };
  return resp.ok && outcome.success
    ? null
    : errorResponse(403, "Turnstile verification failed", "turnstile_failed");
}

async function requireUser(request: Request, env: Env): Promise<AuthContext | Response> {
  const ctx = await authenticate(request, env);
  if (!ctx) return errorResponse(401, "Sign in with ORCID to continue", "unauthorized");
  return checkMutationOrigin(request, ctx) || ctx;
}

function isReviewer(user: UserRow): boolean {
  return user.role === "reviewer" || user.role === "admin";
}

async function loadObservation(db: D1Database, occId: string): Promise<Record<string, unknown> | null> {
  return db.prepare("SELECT * FROM observations WHERE occurrence_id = ?").bind(occId).first<Record<string, unknown>>();
}

async function recordRevision(
  db: D1Database,
  occId: string,
  revision: number,
  actorId: string,
  changeType: string,
  notes: string | null,
  now: string
): Promise<void> {
  const row = await loadObservation(db, occId);
  await db
    .prepare(
      `INSERT INTO observation_revisions
        (occurrence_id, revision_number, actor_user_id, change_type, snapshot_json, notes, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)`
    )
    .bind(occId, revision, actorId, changeType, JSON.stringify(row ? serializeObservation(row, true) : {}), notes, now)
    .run();
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

// ---------------------------------------------------------------------------
// Route handlers
// ---------------------------------------------------------------------------

async function createObservation(request: Request, env: Env, db: D1Database): Promise<Response> {
  const auth = await requireUser(request, env);
  if (auth instanceof Response) return auth;
  const user = auth.user;

  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null;
  if (!body || typeof body !== "object") return errorResponse(400, "Invalid JSON body", "invalid_json");

  const idemKey = (request.headers.get("idempotency-key") || String(body.idempotency_key || "")).trim();
  if (!idemKey || idemKey.length > 128) return errorResponse(400, "Idempotency-Key header required", "missing_idempotency_key");

  // Replays are answered before Turnstile: the original token was single-use, and a client retry
  // after a dropped response must get the saved record back rather than a captcha failure.
  const existing = await db.prepare("SELECT * FROM observations WHERE idempotency_key = ?").bind(idemKey).first<Record<string, unknown>>();
  if (existing) {
    if (existing.submitter_user_id !== user.id) {
      return errorResponse(409, "Idempotency-Key already used by another submission", "idempotency_conflict");
    }
    return jsonResponse({ idempotent_replay: true, observation: serializeObservation(existing, true) }, 200);
  }

  const turnstileError = await verifyTurnstile(request, env);
  if (turnstileError) return turnstileError;

  const license = String(body.license || "").trim();
  if (!ALLOWED_LICENSES.has(license)) {
    return errorResponse(400, "License must be CC-BY 4.0 or CC0 1.0 (dataset-wide license)", "invalid_license");
  }
  if (body.licenseAccepted !== true) {
    return errorResponse(400, "You must explicitly accept the dataset-wide contribution license", "license_not_accepted");
  }
  const checked = validateObservationInput(body, null);
  if (!checked.ok) return checked.response;
  const v = checked.value;

  const hourAgo = new Date(Date.now() - 3600 * 1000).toISOString();
  const recent = await db
    .prepare("SELECT COUNT(*) AS n FROM observations WHERE submitter_user_id = ? AND created_at > ?")
    .bind(user.id, hourAgo)
    .first<{ n: number }>();
  if (recent && Number(recent.n) >= MAX_SUBMISSIONS_PER_HOUR) {
    return errorResponse(429, "Submission rate limit reached; try again later", "rate_limited");
  }

  const iucn = body.iucnCategory ? String(body.iucnCategory).toUpperCase().slice(0, 2) : null;
  const gen = generalizeCoordinates(v.lat, v.lon, v.uncertainty, v.scientificName, iucn, Boolean(body.isSensitiveSpecies));
  const occId = `urn:basinmap:occ:${crypto.randomUUID()}`;
  const now = new Date().toISOString();
  const rightsHolder = String(body.rightsHolder || user.display_name).trim().slice(0, 200) || user.display_name;

  await db
    .prepare(
      `INSERT INTO observations (
        occurrence_id, idempotency_key, submitter_user_id, revision_number,
        review_status, publication_status,
        verbatim_scientific_name, verbatim_event_date, verbatim_latitude,
        verbatim_longitude, verbatim_coordinate_uncertainty_m, verbatim_locality, verbatim_notes,
        scientific_name, taxon_key, family, iucn_category,
        is_sensitive_species, public_latitude, public_longitude,
        public_coordinate_uncertainty_m, information_withheld,
        event_date, year, basis_of_record, hybas_id, locality,
        recorded_by, recorded_by_id, license, license_accepted_at, rights_holder, created_at, updated_at
      ) VALUES (?, ?, ?, 1, 'submitted', 'not_exported', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    )
    .bind(
      occId,
      idemKey,
      user.id,
      v.scientificName,
      v.eventDate,
      v.lat,
      v.lon,
      v.uncertainty,
      v.locality,
      v.notes,
      v.scientificName,
      body.taxonKey ?? null,
      body.family ?? null,
      iucn,
      gen.isSensitive,
      gen.publicLat,
      gen.publicLon,
      gen.publicUncertaintyM,
      gen.informationWithheld,
      v.eventDate,
      v.year,
      "HUMAN_OBSERVATION",
      body.hybas_id ?? null,
      v.locality,
      user.display_name,
      user.orcid_id, // Darwin Core recordedByID = authenticated ORCID iD, never client-supplied
      license,
      now,
      rightsHolder,
      now,
      now
    )
    .run();
  await recordRevision(db, occId, 1, user.id, "created", `Initial contributor submission; ${license} accepted`, now);

  const inserted = await loadObservation(db, occId);
  return jsonResponse({ idempotent_replay: false, observation: serializeObservation(inserted || {}, true) }, 201);
}

async function listMyObservations(request: Request, env: Env, db: D1Database): Promise<Response> {
  const auth = await requireUser(request, env);
  if (auth instanceof Response) return auth;
  const { results } = await db
    .prepare(
      `SELECT o.*, (SELECT COUNT(*) FROM media_intake m WHERE m.occurrence_id = o.occurrence_id) AS media_count
       FROM observations o WHERE o.submitter_user_id = ? ORDER BY o.created_at DESC LIMIT 100`
    )
    .bind(auth.user.id)
    .all<Record<string, unknown>>();
  return jsonResponse(
    { observations: results.map((r) => ({ ...serializeObservation(r, true), media_count: Number(r.media_count || 0) })) },
    200
  );
}

async function getObservation(request: Request, env: Env, db: D1Database, occId: string): Promise<Response> {
  const ctx = await authenticate(request, env);
  const row = await loadObservation(db, occId);
  if (!row) return errorResponse(404, "Observation not found", "not_found");
  const isOwner = Boolean(ctx && ctx.user.id === row.submitter_user_id);
  const reviewer = Boolean(ctx && isReviewer(ctx.user));
  if (row.review_status !== "approved" && !isOwner && !reviewer) {
    return errorResponse(403, "Pending observations are restricted to submitter and reviewers", "forbidden");
  }
  const media = await db
    .prepare(
      "SELECT media_id, public_r2_key, detected_mime_type, derivative_status, license, rights_holder FROM media_intake WHERE occurrence_id = ? ORDER BY created_at ASC"
    )
    .bind(occId)
    .all<Record<string, unknown>>();
  const payload: Record<string, unknown> = {
    observation: serializeObservation(row, isOwner || reviewer),
    media: media.results.filter((m) => isOwner || reviewer || m.derivative_status === "public_sanitized"),
  };
  if (isOwner || reviewer) {
    const revs = await db
      .prepare(
        "SELECT revision_number, actor_user_id, change_type, notes, created_at FROM observation_revisions WHERE occurrence_id = ? ORDER BY revision_number ASC"
      )
      .bind(occId)
      .all();
    payload.revisions = revs.results;
  }
  return jsonResponse(payload, 200);
}

async function updateObservation(request: Request, env: Env, db: D1Database, occId: string): Promise<Response> {
  const auth = await requireUser(request, env);
  if (auth instanceof Response) return auth;
  const user = auth.user;
  const row = await loadObservation(db, occId);
  if (!row) return errorResponse(404, "Observation not found", "not_found");
  const isOwner = user.id === row.submitter_user_id;
  const reviewer = isReviewer(user);
  if (!isOwner && !reviewer) return errorResponse(403, "Only the submitter or an authorized reviewer can edit this record", "forbidden");

  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null;
  if (!body || typeof body !== "object") return errorResponse(400, "Invalid JSON body", "invalid_json");
  const checked = validateObservationInput(body, row);
  if (!checked.ok) return checked.response;
  const v = checked.value;

  const iucn = (body.iucnCategory ?? row.iucn_category) as string | null;
  const gen = generalizeCoordinates(v.lat, v.lon, v.uncertainty, v.scientificName, iucn, Boolean(body.isSensitiveSpecies ?? row.is_sensitive_species));
  const newRev = Number(row.revision_number) + 1;
  const nextStatus = reviewer ? String(row.review_status) : "submitted";
  // A reviewer who changes the name becomes the identifier (Darwin Core identifiedBy/identifiedByID).
  const reidentified = reviewer && v.scientificName !== row.scientific_name;
  const now = new Date().toISOString();

  await db
    .prepare(
      `UPDATE observations
       SET revision_number = ?, review_status = ?, scientific_name = ?,
           iucn_category = ?, is_sensitive_species = ?, public_latitude = ?,
           public_longitude = ?, public_coordinate_uncertainty_m = ?,
           information_withheld = ?, verbatim_scientific_name = ?,
           verbatim_event_date = ?, verbatim_latitude = ?, verbatim_longitude = ?,
           verbatim_coordinate_uncertainty_m = ?, event_date = ?, year = ?,
           locality = ?, verbatim_locality = ?,
           identified_by = ?, identified_by_id = ?, updated_at = ?
       WHERE occurrence_id = ?`
    )
    .bind(
      newRev,
      nextStatus,
      v.scientificName,
      iucn ?? null,
      gen.isSensitive,
      gen.publicLat,
      gen.publicLon,
      gen.publicUncertaintyM,
      gen.informationWithheld,
      v.scientificName,
      v.eventDate,
      v.lat,
      v.lon,
      v.uncertainty,
      v.eventDate,
      v.year,
      v.locality,
      v.locality,
      reidentified ? user.display_name : row.identified_by ?? null,
      reidentified ? user.orcid_id : row.identified_by_id ?? null,
      now,
      occId
    )
    .run();
  await recordRevision(db, occId, newRev, user.id, "updated", v.notes ?? "Observation corrected while preserving occurrenceID", now);

  if (row.review_status === "approved" && env.PUBLIC_BUCKET) {
    await rebuildApprovedOverlay(db, env.PUBLIC_BUCKET);
  }
  const updated = await loadObservation(db, occId);
  return jsonResponse({ observation: serializeObservation(updated || {}, true) }, 200);
}

async function uploadMedia(request: Request, env: Env, db: D1Database, occId: string): Promise<Response> {
  const auth = await requireUser(request, env);
  if (auth instanceof Response) return auth;
  const user = auth.user;
  if (!env.INTAKE_BUCKET) return errorResponse(503, "INTAKE_BUCKET not bound", "storage_unavailable");

  const lenHdr = request.headers.get("content-length");
  if (!lenHdr || !/^\d+$/.test(lenHdr)) {
    return errorResponse(411, "Content-Length header required for streaming R2 put", "length_required");
  }
  const byteLength = Number(lenHdr);
  if (byteLength <= 0) return errorResponse(400, "Empty upload payload", "empty_upload");
  if (byteLength > MAX_UPLOAD_BYTES) return errorResponse(413, "Upload exceeds maximum 15 MiB", "payload_too_large");
  if (!request.body) return errorResponse(400, "Empty request body", "empty_upload");

  const obs = await loadObservation(db, occId);
  if (!obs) return errorResponse(404, "Observation not found", "not_found");
  if (user.id !== obs.submitter_user_id && !isReviewer(user)) {
    return errorResponse(403, "Forbidden: cannot upload media to another user's observation", "forbidden");
  }
  const count = await db.prepare("SELECT COUNT(*) AS n FROM media_intake WHERE occurrence_id = ?").bind(occId).first<{ n: number }>();
  if (count && Number(count.n) >= MAX_MEDIA_PER_OBSERVATION) {
    return errorResponse(409, `At most ${MAX_MEDIA_PER_OBSERVATION} photos per observation`, "too_many_media");
  }

  // Inspect magic bytes from the leading chunk before accepting the rest of the stream.
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  let mimeType: string | null = null;
  while (true) {
    const next = await reader.read();
    if (next.done) break;
    chunks.push(next.value);
    total += next.value.byteLength;
    if (total > MAX_UPLOAD_BYTES || total > byteLength) {
      await reader.cancel();
      return errorResponse(413, "Upload exceeded declared Content-Length / 15 MiB ceiling", "payload_too_large");
    }
    if (mimeType === null && total >= 12) {
      const head = new Uint8Array(12);
      let filled = 0;
      for (const c of chunks) {
        const take = Math.min(12 - filled, c.byteLength);
        head.set(c.subarray(0, take), filled);
        filled += take;
        if (filled === 12) break;
      }
      mimeType = detectMagicMime(head);
      if (!mimeType) {
        await reader.cancel();
        return errorResponse(415, "Unsupported Media Type: magic bytes do not match JPEG, PNG, WebP, or HEIC", "unsupported_media_type");
      }
    }
  }
  if (!mimeType) return errorResponse(415, "Unsupported Media Type: file too small to identify", "unsupported_media_type");
  if (total !== byteLength) return errorResponse(400, "Payload size does not match Content-Length", "length_mismatch");

  const fullBytes = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    fullBytes.set(c, off);
    off += c.byteLength;
  }
  const exif = extractExif(fullBytes, mimeType);
  const mediaId = crypto.randomUUID();
  const privateKey = `intake/${occId}/${mediaId}.bin`;
  await env.INTAKE_BUCKET.put(privateKey, fullBytes, { httpMetadata: { contentType: mimeType } });

  const hashBuf = await crypto.subtle.digest("SHA-256", fullBytes);
  const shaHex = Array.from(new Uint8Array(hashBuf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  const now = new Date().toISOString();
  await db
    .prepare(
      `INSERT INTO media_intake (
        media_id, occurrence_id, uploader_user_id, private_r2_key,
        detected_mime_type, byte_length, sha256,
        exif_gps_latitude, exif_gps_longitude, exif_datetime, exif_orientation,
        license, rights_holder, derivative_status, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'private_only', ?)`
    )
    .bind(
      mediaId,
      occId,
      user.id,
      privateKey,
      mimeType,
      total,
      shaHex,
      exif.gps_latitude,
      exif.gps_longitude,
      exif.datetime,
      exif.orientation,
      obs.license,
      obs.rights_holder,
      now
    )
    .run();

  return jsonResponse(
    {
      media_id: mediaId,
      occurrence_id: occId,
      detected_mime_type: mimeType,
      byte_length: total,
      sha256: shaHex,
      derivative_status: "private_only",
      exif_extracted: exif,
    },
    201
  );
}

/** Private intake media for the submitter and reviewers only (never cached, never public). */
async function getPrivateMedia(request: Request, env: Env, db: D1Database, mediaId: string): Promise<Response> {
  const auth = await requireUser(request, env);
  if (auth instanceof Response) return auth;
  if (!env.INTAKE_BUCKET) return errorResponse(503, "INTAKE_BUCKET not bound", "storage_unavailable");
  const media = await db
    .prepare(
      `SELECT m.*, o.submitter_user_id FROM media_intake m JOIN observations o ON o.occurrence_id = m.occurrence_id
       WHERE m.media_id = ?`
    )
    .bind(mediaId)
    .first<Record<string, unknown>>();
  if (!media) return errorResponse(404, "Media not found", "not_found");
  if (auth.user.id !== media.submitter_user_id && !isReviewer(auth.user)) return errorResponse(403, "Forbidden", "forbidden");
  const obj = await env.INTAKE_BUCKET.get(String(media.private_r2_key));
  if (!obj) return errorResponse(404, "Media object missing", "not_found");
  return new Response(obj.body, {
    status: 200,
    headers: {
      "content-type": String(media.detected_mime_type),
      "content-length": String(media.byte_length),
      "cache-control": "private, no-store",
      "content-disposition": "inline",
      "x-content-type-options": "nosniff",
      "content-security-policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
    },
  });
}

/** Reviewer queue: verbatim coordinates + private EXIF evidence for verification. */
async function listReviewQueue(request: Request, env: Env, db: D1Database): Promise<Response> {
  const auth = await requireUser(request, env);
  if (auth instanceof Response) return auth;
  if (!isReviewer(auth.user)) return errorResponse(403, "Forbidden: reviewer or admin role required", "forbidden");

  const url = new URL(request.url);
  const status = url.searchParams.get("status") || "submitted";
  if (!REVIEW_QUEUE_STATUSES.has(status)) return errorResponse(400, "status must be submitted, needs_changes, or approved", "invalid_status");
  const limit = Math.max(1, Math.min(100, Number(url.searchParams.get("limit") || 50) || 50));
  const before = url.searchParams.get("before");

  const rows = before
    ? await db
        .prepare(
          `SELECT o.*, u.display_name AS submitter_name, u.orcid_id AS submitter_orcid
           FROM observations o JOIN users u ON u.id = o.submitter_user_id
           WHERE o.review_status = ? AND o.created_at < ? ORDER BY o.created_at DESC LIMIT ?`
        )
        .bind(status, before, limit)
        .all<Record<string, unknown>>()
    : await db
        .prepare(
          `SELECT o.*, u.display_name AS submitter_name, u.orcid_id AS submitter_orcid
           FROM observations o JOIN users u ON u.id = o.submitter_user_id
           WHERE o.review_status = ? ORDER BY o.created_at DESC LIMIT ?`
        )
        .bind(status, limit)
        .all<Record<string, unknown>>();

  const items: Array<Record<string, unknown>> = [];
  for (const row of rows.results) {
    const media = await db
      .prepare(
        `SELECT media_id, detected_mime_type, byte_length, sha256, exif_gps_latitude, exif_gps_longitude,
                exif_datetime, exif_orientation, derivative_status, created_at
         FROM media_intake WHERE occurrence_id = ? ORDER BY created_at ASC`
      )
      .bind(row.occurrence_id)
      .all<Record<string, unknown>>();
    const lat = Number(row.verbatim_latitude);
    const lon = Number(row.verbatim_longitude);
    const tolerance = Math.max(2 * Number(row.verbatim_coordinate_uncertainty_m || 30), 500);
    items.push({
      ...serializeObservation(row, true),
      submitter: { display_name: row.submitter_name, orcid_id: row.submitter_orcid },
      media: media.results.map((m) => {
        const hasGps = m.exif_gps_latitude !== null && m.exif_gps_longitude !== null;
        const offset = hasGps ? Math.round(haversineMeters(lat, lon, Number(m.exif_gps_latitude), Number(m.exif_gps_longitude))) : null;
        return {
          ...m,
          url: `/api/media/${m.media_id}`,
          exif_offset_m: offset,
          exif_mismatch: offset !== null && offset > tolerance,
        };
      }),
    });
  }
  const last = rows.results[rows.results.length - 1];
  return jsonResponse(
    {
      status,
      observations: items,
      next_before: rows.results.length === limit && last ? last.created_at : null,
    },
    200
  );
}

async function reviewObservation(request: Request, env: Env, db: D1Database, occId: string, decision: string): Promise<Response> {
  const auth = await requireUser(request, env);
  if (auth instanceof Response) return auth;
  const reviewer = auth.user;
  if (!isReviewer(reviewer)) return errorResponse(403, "Forbidden: reviewer role required", "forbidden");
  if (!env.INTAKE_BUCKET || !env.PUBLIC_BUCKET) return errorResponse(503, "R2 bindings unavailable", "storage_unavailable");

  const obs = await loadObservation(db, occId);
  if (!obs) return errorResponse(404, "Observation not found", "not_found");

  const body = (await request.json().catch(() => ({}))) as { notes?: string };
  const notes = String(body.notes ?? "").slice(0, 4000);
  const now = new Date().toISOString();
  const newRev = Number(obs.revision_number) + 1;

  await db
    .prepare(
      `UPDATE observations
       SET review_status = ?, revision_number = ?, reviewer_user_id = ?,
           identified_by = COALESCE(identified_by, ?), identified_by_id = COALESCE(identified_by_id, ?),
           review_notes = ?, reviewed_at = ?, updated_at = ?
       WHERE occurrence_id = ?`
    )
    .bind(decision, newRev, reviewer.id, reviewer.display_name, reviewer.orcid_id, notes, now, now, occId)
    .run();

  const mediaRows = await db.prepare("SELECT * FROM media_intake WHERE occurrence_id = ?").bind(occId).all<Record<string, unknown>>();
  const sanitizedKeys: string[] = [];
  const skippedMedia: string[] = [];
  if (decision === "approved") {
    for (const m of mediaRows.results) {
      // Without the Images binding only JPEG can be stripped safely; other formats stay private.
      if (!env.IMAGES && m.detected_mime_type !== "image/jpeg") {
        skippedMedia.push(String(m.media_id));
        continue;
      }
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
        httpMetadata: { contentType: "image/jpeg", cacheControl: "public, max-age=31536000, immutable" },
      });
      await db
        .prepare("UPDATE media_intake SET public_r2_key = ?, derivative_status = 'public_sanitized' WHERE media_id = ?")
        .bind(publicKey, m.media_id)
        .run();
      sanitizedKeys.push(publicKey);
    }
  } else {
    for (const m of mediaRows.results) {
      if (m.public_r2_key) await env.PUBLIC_BUCKET.delete(String(m.public_r2_key));
      await db
        .prepare("UPDATE media_intake SET public_r2_key = NULL, derivative_status = 'revoked' WHERE media_id = ?")
        .bind(m.media_id)
        .run();
    }
  }
  await recordRevision(db, occId, newRev, reviewer.id, decision, notes, now);

  const overlayCount = await rebuildApprovedOverlay(db, env.PUBLIC_BUCKET);
  const updated = await loadObservation(db, occId);
  return jsonResponse(
    {
      occurrence_id: occId,
      review_status: decision,
      observation: serializeObservation(updated || {}, true),
      sanitized_media_keys: sanitizedKeys,
      skipped_media_ids: skippedMedia,
      overlay_record_count: overlayCount,
    },
    200
  );
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
          submissions_enabled: Boolean(env.DB && env.INTAKE_BUCKET) && (isDevelopment(env) || Boolean(env.TURNSTILE_SECRET)),
          environment: env.ENVIRONMENT ?? "production",
          max_upload_bytes: MAX_UPLOAD_BYTES,
          max_media_per_observation: MAX_MEDIA_PER_OBSERVATION,
          turnstile_site_key: env.TURNSTILE_SITE_KEY || "",
          auth: {
            orcid_login_enabled: orcidConfigured(env),
            orcid_base_url: orcidBaseUrl(env),
            dev_tokens_enabled: isDevelopment(env),
          },
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
    const db = env.DB;

    if (pathname === "/api/auth/login" && request.method === "GET") return handleLogin(request, env);
    if (pathname === "/api/auth/callback" && request.method === "GET") return handleCallback(request, env);
    if (pathname === "/api/auth/logout" && request.method === "POST") return handleLogout(request, env);

    if (pathname === "/api/auth/me" && request.method === "GET") {
      const ctx = await authenticate(request, env);
      if (!ctx) return errorResponse(401, "Unauthorized", "unauthorized");
      const u = ctx.user;
      return jsonResponse(
        { user: { id: u.id, orcid_id: u.orcid_id, display_name: u.display_name, role: u.role }, auth_method: ctx.via },
        200
      );
    }

    if (pathname === "/api/observations" && request.method === "POST") return createObservation(request, env, db);
    if (pathname === "/api/observations/mine" && request.method === "GET") return listMyObservations(request, env, db);

    const obsMatch = pathname.match(/^\/api\/observations\/([^/]+)$/);
    if (obsMatch && request.method === "GET") return getObservation(request, env, db, decodeURIComponent(obsMatch[1]));
    if (obsMatch && request.method === "PATCH") return updateObservation(request, env, db, decodeURIComponent(obsMatch[1]));

    const uploadMatch = pathname.match(/^\/api\/uploads\/([^/]+)$/);
    if (uploadMatch && request.method === "PUT") return uploadMedia(request, env, db, decodeURIComponent(uploadMatch[1]));

    const mediaMatch = pathname.match(/^\/api\/media\/([0-9a-fA-F-]{36})$/);
    if (mediaMatch && request.method === "GET") return getPrivateMedia(request, env, db, mediaMatch[1]);

    if (pathname === "/api/review/pending" && request.method === "GET") return listReviewQueue(request, env, db);

    const reviewMatch = pathname.match(/^\/api\/review\/([^/]+)\/(approve|needs_changes)$/);
    if (reviewMatch && request.method === "POST") {
      const decision = reviewMatch[2] === "approve" ? "approved" : "needs_changes";
      return reviewObservation(request, env, db, decodeURIComponent(reviewMatch[1]), decision);
    }

    return jsonResponse({ error: "Not found", path: pathname }, 404);
  },
};
