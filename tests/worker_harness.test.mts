/**
 * Executes the real Cloudflare Worker (src/worker/index.ts) under Node's built-in TypeScript
 * stripping, against:
 *   - a D1 shim backed by node:sqlite with migrations/*.sql applied,
 *   - in-memory R2 buckets,
 *   - mocked ORCID (/oauth/token) and Turnstile (/siteverify) endpoints.
 *
 * Run: node --test tests/worker_harness.test.mts   (Node >= 22.18; invoked by tests/test_worker_harness.py)
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import worker from "../src/worker/index.ts";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const ORIGIN = "https://staging.basinmap.org";

// ---------------------------------------------------------------- D1 shim
function normalize(params: unknown[]): unknown[] {
  return params.map((p) => (p === undefined ? null : typeof p === "boolean" ? (p ? 1 : 0) : p));
}
class Stmt {
  db: DatabaseSync;
  sql: string;
  params: unknown[];
  constructor(db: DatabaseSync, sql: string, params: unknown[] = []) {
    this.db = db;
    this.sql = sql;
    this.params = params;
  }
  bind(...params: unknown[]) {
    return new Stmt(this.db, this.sql, normalize(params));
  }
  async first<T>(): Promise<T | null> {
    const row = this.db.prepare(this.sql).get(...(this.params as never[]));
    return (row ? { ...row } : null) as T | null;
  }
  async all<T>(): Promise<{ results: T[] }> {
    return { results: this.db.prepare(this.sql).all(...(this.params as never[])).map((r) => ({ ...r })) as T[] };
  }
  async run() {
    this.db.prepare(this.sql).run(...(this.params as never[]));
    return { success: true };
  }
}
function makeD1() {
  const db = new DatabaseSync(":memory:");
  db.exec("PRAGMA foreign_keys = ON;");
  for (const f of readdirSync(`${ROOT}/migrations`).filter((n) => n.endsWith(".sql")).sort()) {
    db.exec(readFileSync(`${ROOT}/migrations/${f}`, "utf-8"));
  }
  return {
    raw: db,
    prepare: (sql: string) => new Stmt(db, sql),
    batch: async (stmts: Stmt[]) => Promise.all(stmts.map((s) => s.run())),
  };
}

// ---------------------------------------------------------------- R2 shim
function makeR2() {
  const store = new Map<string, { bytes: Uint8Array; httpMetadata: Record<string, string> }>();
  const obj = (key: string, bytes: Uint8Array, meta: Record<string, string>) => ({
    key,
    size: bytes.byteLength,
    httpEtag: `"${bytes.byteLength}"`,
    httpMetadata: meta,
    body: new Blob([bytes]).stream(),
    arrayBuffer: async () => bytes.slice().buffer,
    text: async () => new TextDecoder().decode(bytes),
  });
  return {
    store,
    async put(key: string, value: string | Uint8Array | ArrayBuffer, opts?: { httpMetadata?: Record<string, string> }) {
      const bytes = typeof value === "string" ? new TextEncoder().encode(value) : new Uint8Array(value as ArrayBuffer);
      store.set(key, { bytes, httpMetadata: opts?.httpMetadata || {} });
    },
    async get(key: string, opts?: { range?: { offset: number; length: number } }) {
      const o = store.get(key);
      if (!o) return null;
      const bytes = opts?.range ? o.bytes.slice(opts.range.offset, opts.range.offset + opts.range.length) : o.bytes;
      return obj(key, bytes, o.httpMetadata);
    },
    async head(key: string) {
      const o = store.get(key);
      return o ? obj(key, o.bytes, o.httpMetadata) : null;
    },
    async delete(key: string) {
      store.delete(key);
    },
  };
}

// ---------------------------------------------------------------- external services
const fetchLog: Array<{ url: string; body: string }> = [];
let nextOrcid = { orcid: "0000-0002-1825-0097", name: "Pilot Field Contributor" };
let turnstileSuccess = true;
const gbifCalls: Array<{ kind: string; auth?: string; form?: FormData }> = [];
let gbifIndexed: Array<{ key: number; occurrenceID: string }> = [];
globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input);
  const body = init?.body ? String(init.body) : "";
  fetchLog.push({ url, body });
  if (url.endsWith("/oauth/token")) {
    const p = new URLSearchParams(body);
    if (p.get("code") !== "good-code" || p.get("client_secret") !== "orcid-secret") {
      return new Response(JSON.stringify({ error: "invalid_grant" }), { status: 400 });
    }
    return Response.json({ access_token: "x", token_type: "bearer", scope: "/authenticate", ...nextOrcid });
  }
  if (url.includes("turnstile/v0/siteverify")) return Response.json({ success: turnstileSuccess });
  if (url === "https://api.gbif.org/v1/validation" && init?.method === "POST") {
    gbifCalls.push({ kind: "validate", auth: String((init.headers as any)?.authorization || ""), form: init.body as FormData });
    return Response.json({ key: `val-${gbifCalls.length}`, status: "SUBMITTED" });
  }
  if (url.startsWith("https://api.gbif.org/v1/validation/")) {
    gbifCalls.push({ kind: "poll" });
    return Response.json({ key: url.split("/").pop(), status: "FINISHED", metrics: { indexeable: true } });
  }
  if (url.startsWith("https://api.gbif-uat.org/v1/dataset/") && url.endsWith("/crawl")) {
    gbifCalls.push({ kind: "crawl", auth: String((init?.headers as any)?.authorization || "") });
    return new Response(null, { status: 204 });
  }
  if (url.startsWith("https://api.gbif-uat.org/v1/occurrence/search?")) {
    gbifCalls.push({ kind: "search" });
    return Response.json({ offset: 0, limit: 300, endOfRecords: true, results: gbifIndexed });
  }
  throw new Error(`unexpected outbound fetch ${url}`);
}) as typeof fetch;

function makeEnv(overrides: Record<string, unknown> = {}) {
  return {
    ASSETS: { fetch: async () => new Response("<html>basinmap.org</html>", { headers: { "content-type": "text/html" } }) },
    DB: makeD1(),
    PUBLIC_BUCKET: makeR2(),
    INTAKE_BUCKET: makeR2(),
    ENVIRONMENT: "staging",
    PUBLIC_DATA_URL: "/api/r2",
    ORCID_CLIENT_ID: "APP-TEST",
    ORCID_CLIENT_SECRET: "orcid-secret",
    ORCID_BASE_URL: "https://sandbox.orcid.org",
    SESSION_SECRET: "test-session-secret-0123456789abcdef",
    TURNSTILE_SECRET: "turnstile-secret",
    TURNSTILE_SITE_KEY: "1x00000000000000000000AA",
    ADMIN_ORCID_IDS: "0000-0001-5109-3700",
    ...overrides,
  } as any;
}

async function call(env: any, path: string, init: RequestInit & { headers?: Record<string, string> } = {}) {
  return worker.fetch(new Request(`${ORIGIN}${path}`, init), env);
}

function cookieValue(setCookies: string[], name: string): string {
  const c = setCookies.find((s) => s.startsWith(`${name}=`));
  assert.ok(c, `missing Set-Cookie ${name}`);
  return c.split(";")[0];
}

/** Full ORCID login; returns the Cookie header value for the new session. */
async function signIn(env: any, who: { orcid: string; name: string }): Promise<string> {
  nextOrcid = who;
  const login = await call(env, "/api/auth/login?return_to=/review");
  assert.equal(login.status, 302);
  const state = new URL(login.headers.get("location")!).searchParams.get("state")!;
  const stateCookie = cookieValue(login.headers.getSetCookie(), "__Host-bm_oauth");
  const cb = await call(env, `/api/auth/callback?code=good-code&state=${state}`, { headers: { cookie: stateCookie } });
  assert.equal(cb.status, 302);
  assert.equal(cb.headers.get("location"), "/review");
  return cookieValue(cb.headers.getSetCookie(), "__Host-bm_session");
}

function jpegWithGps(latDeg: number, latMin: number, lonDeg: number, lonMin: number): Uint8Array {
  // Same structure as tests/test_prompt3_backend_and_dwca.build_test_jpeg_with_exif.
  const dt = new TextEncoder().encode("2026:11:18 14:22:00\0");
  const ifd0Off = 8;
  const dtOff = ifd0Off + 2 + 3 * 12 + 4;
  const gpsOff = dtOff + dt.length;
  const gpsData = gpsOff + 2 + 4 * 12 + 4;
  const tiff = new DataView(new ArrayBuffer(gpsData + 48));
  const u8 = new Uint8Array(tiff.buffer);
  u8.set([0x49, 0x49]);
  tiff.setUint16(2, 42, true);
  tiff.setUint32(4, ifd0Off, true);
  let p = ifd0Off;
  const entry = (tag: number, type: number, count: number, value: number, inlineShort = false) => {
    tiff.setUint16(p, tag, true);
    tiff.setUint16(p + 2, type, true);
    tiff.setUint32(p + 4, count, true);
    if (inlineShort) tiff.setUint16(p + 8, value, true);
    else tiff.setUint32(p + 8, value, true);
    p += 12;
  };
  tiff.setUint16(p, 3, true);
  p += 2;
  entry(0x0112, 3, 1, 6, true);
  entry(0x0132, 2, dt.length, dtOff);
  entry(0x8825, 4, 1, gpsOff);
  p += 4;
  u8.set(dt, dtOff);
  p = gpsOff;
  tiff.setUint16(p, 4, true);
  p += 2;
  entry(0x0001, 2, 2, 0);
  u8[p - 4] = 0x4e; // 'N'
  entry(0x0002, 5, 3, gpsData);
  entry(0x0003, 2, 2, 0);
  u8[p - 4] = 0x57; // 'W'
  entry(0x0004, 5, 3, gpsData + 24);
  const rat = (off: number, vals: number[]) => vals.forEach((v, i) => tiff.setUint32(off + i * 4, v, true));
  rat(gpsData, [latDeg, 1, latMin, 1, 0, 1]);
  rat(gpsData + 24, [lonDeg, 1, lonMin, 1, 0, 1]);
  const app1Payload = new Uint8Array([...new TextEncoder().encode("Exif\0\0"), ...u8]);
  const seg = (marker: number, payload: Uint8Array) => [0xff, marker, (payload.length + 2) >> 8, (payload.length + 2) & 0xff, ...payload];
  return new Uint8Array([
    0xff, 0xd8,
    ...seg(0xe0, new Uint8Array([0x4a, 0x46, 0x49, 0x46, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0])),
    ...seg(0xe1, app1Payload),
    ...seg(0xc0, new Uint8Array([8, 0, 1, 0, 1, 1, 1, 0x11, 0])),
    0xff, 0xda, 0, 8, 1, 1, 0, 0, 0x3f, 0, 0xd2, 0xcf, 0x20,
    0xff, 0xd9,
  ]);
}

const contributor = { orcid: "0000-0002-1825-0097", name: "Field Contributor" };
const reviewer = { orcid: "0000-0001-5109-3700", name: "Taxonomic Reviewer" };
const json = (body: unknown) => JSON.stringify(body);
const baseObs = {
  scientificName: "Oncorhynchus kisutch",
  eventDate: "2026-11-10",
  decimalLatitude: 44.05,
  decimalLongitude: -123.08,
  coordinateUncertaintyInMeters: 25,
  locality: "McKenzie River at Hendricks Bridge",
  notes: "Spawning pair",
  license: "CC-BY 4.0",
  licenseAccepted: true,
};

test("config advertises auth + Turnstile; ORCID unconfigured fails closed", async () => {
  const env = makeEnv({ ORCID_CLIENT_SECRET: undefined, TURNSTILE_SECRET: undefined });
  const cfg = await (await call(env, "/api/config")).json();
  assert.equal(cfg.auth.orcid_login_enabled, false);
  assert.equal(cfg.auth.dev_tokens_enabled, false);
  assert.equal(cfg.submissions_enabled, false);
  assert.equal(cfg.turnstile_site_key, "1x00000000000000000000AA");
  assert.equal((await call(env, "/api/auth/login")).status, 503);
});

test("ORCID OAuth login sets hardened session cookie and maps ORCID iD", async () => {
  const env = makeEnv();
  const login = await call(env, "/api/auth/login?return_to=//evil.example/x");
  const loc = new URL(login.headers.get("location")!);
  assert.equal(loc.origin + loc.pathname, "https://sandbox.orcid.org/oauth/authorize");
  assert.equal(loc.searchParams.get("scope"), "/authenticate");
  assert.equal(loc.searchParams.get("redirect_uri"), `${ORIGIN}/api/auth/callback`);
  const stateCookieHeader = login.headers.getSetCookie().find((c) => c.startsWith("__Host-bm_oauth="))!;
  assert.match(stateCookieHeader, /HttpOnly; Secure; SameSite=Lax; Max-Age=600/);

  // Tampered / mismatched state is rejected.
  const stateCookie = stateCookieHeader.split(";")[0];
  const bad = await call(env, "/api/auth/callback?code=good-code&state=wrong", { headers: { cookie: stateCookie } });
  assert.equal(bad.headers.get("location"), "/?auth_error=state_mismatch");
  const noCookie = await call(env, `/api/auth/callback?code=good-code&state=${loc.searchParams.get("state")}`);
  assert.equal(noCookie.headers.get("location"), "/?auth_error=missing_state");

  const ok = await call(env, `/api/auth/callback?code=good-code&state=${loc.searchParams.get("state")}`, {
    headers: { cookie: stateCookie },
  });
  assert.equal(ok.status, 302);
  assert.equal(ok.headers.get("location"), "/", "open redirect in return_to must fall back to /");
  const session = ok.headers.getSetCookie().find((c) => c.startsWith("__Host-bm_session="))!;
  assert.match(session, /; Path=\/; HttpOnly; Secure; SameSite=Lax; Max-Age=604800$/);

  const me = await (await call(env, "/api/auth/me", { headers: { cookie: session.split(";")[0] } })).json();
  assert.equal(me.user.orcid_id, "https://sandbox.orcid.org/0000-0002-1825-0097");
  assert.equal(me.user.role, "contributor");
  assert.equal(me.auth_method, "cookie");
  // The session token itself is never stored, only its SHA-256.
  const raw = session.split(";")[0].split("=")[1];
  const rows = env.DB.raw.prepare("SELECT token_hash FROM sessions").all();
  assert.equal(rows.length, 1);
  assert.ok(!raw.includes(rows[0].token_hash as string));

  // Forged cookie signature is rejected.
  const forged = session.split(";")[0].replace(/.$/, (c: string) => (c === "A" ? "B" : "A"));
  assert.equal((await call(env, "/api/auth/me", { headers: { cookie: forged } })).status, 401);

  // Admin allow-list gets a short privileged session.
  const adminCookie = await signIn(env, reviewer);
  const admin = await (await call(env, "/api/auth/me", { headers: { cookie: adminCookie } })).json();
  assert.equal(admin.user.role, "admin");
});

test("dev bearer tokens only work in development", async () => {
  const staging = makeEnv();
  const r1 = await call(staging, "/api/auth/me", { headers: { authorization: "Bearer dev-reviewer-orcid" } });
  assert.equal(r1.status, 401);
  const dev = makeEnv({ ENVIRONMENT: "development", TURNSTILE_SECRET: undefined });
  const r2 = await (await call(dev, "/api/auth/me", { headers: { authorization: "Bearer dev-reviewer-orcid" } })).json();
  assert.equal(r2.user.id, "usr-review-1");
  assert.equal(r2.auth_method, "dev-bearer");
  // Development without a Turnstile secret still requires a (dummy) token.
  const noTok = await call(dev, "/api/observations", {
    method: "POST",
    headers: { authorization: "Bearer dev-contributor-orcid", "idempotency-key": "dev-1", "content-type": "application/json" },
    body: json(baseObs),
  });
  assert.equal(noTok.status, 403);
  const withTok = await call(dev, "/api/observations", {
    method: "POST",
    headers: { authorization: "Bearer dev-contributor-orcid", "idempotency-key": "dev-1", "x-turnstile-token": "dev", "content-type": "application/json" },
    body: json(baseObs),
  });
  assert.equal(withTok.status, 201);
});

test("submission: CSRF origin check, Turnstile, license, validation, idempotency, ORCID recordedByID", async () => {
  const env = makeEnv();
  const cookie = await signIn(env, contributor);
  const headers = (extra: Record<string, string> = {}) => ({
    cookie,
    origin: ORIGIN,
    "content-type": "application/json",
    "idempotency-key": "idem-1",
    "x-turnstile-token": "tok",
    ...extra,
  });

  assert.equal((await call(env, "/api/observations", { method: "POST", headers: headers({ origin: "https://evil.example" }), body: json(baseObs) })).status, 403);
  const noOrigin = headers();
  delete (noOrigin as any).origin;
  assert.equal((await call(env, "/api/observations", { method: "POST", headers: noOrigin, body: json(baseObs) })).status, 403);

  turnstileSuccess = false;
  assert.equal((await call(env, "/api/observations", { method: "POST", headers: headers(), body: json(baseObs) })).status, 403);
  turnstileSuccess = true;

  const noAccept = await call(env, "/api/observations", { method: "POST", headers: headers(), body: json({ ...baseObs, licenseAccepted: false }) });
  assert.equal((await noAccept.json()).code, "license_not_accepted");
  const badLicense = await call(env, "/api/observations", { method: "POST", headers: headers(), body: json({ ...baseObs, license: "CC-BY-NC 4.0" }) });
  assert.equal((await badLicense.json()).code, "invalid_license");
  const future = await call(env, "/api/observations", { method: "POST", headers: headers(), body: json({ ...baseObs, eventDate: "2099-01-01" }) });
  assert.equal((await future.json()).code, "invalid_date");
  const badLat = await call(env, "/api/observations", { method: "POST", headers: headers(), body: json({ ...baseObs, decimalLatitude: 91 }) });
  assert.equal((await badLat.json()).code, "invalid_coordinates");

  const verifyCallsBefore = fetchLog.filter((f) => f.url.includes("siteverify")).length;
  const created = await call(env, "/api/observations", {
    method: "POST",
    headers: headers({ "cf-connecting-ip": "203.0.113.5" }),
    body: json({ ...baseObs, recordedByID: "https://orcid.org/9999-9999-9999-9999" }),
  });
  assert.equal(created.status, 201);
  const obs = (await created.json()).observation;
  assert.equal(obs.review_status, "submitted");
  assert.equal(obs.publication_status, "not_exported");
  assert.equal(obs.recorded_by_id, "https://sandbox.orcid.org/0000-0002-1825-0097");
  assert.equal(obs.license, "CC-BY 4.0");
  assert.ok(obs.license_accepted_at);
  assert.equal(obs.verbatim.notes, "Spawning pair");
  const sv = fetchLog.filter((f) => f.url.includes("siteverify")).pop()!;
  assert.equal(new URLSearchParams(sv.body).get("remoteip"), "203.0.113.5");

  // Retry with the same key: replayed WITHOUT another Turnstile call (token was single-use).
  const callsAfterCreate = fetchLog.filter((f) => f.url.includes("siteverify")).length;
  const replay = await call(env, "/api/observations", { method: "POST", headers: headers({ "x-turnstile-token": "" }), body: json(baseObs) });
  assert.equal(replay.status, 200);
  assert.equal((await replay.json()).idempotent_replay, true);
  assert.equal(fetchLog.filter((f) => f.url.includes("siteverify")).length, callsAfterCreate);
  assert.ok(callsAfterCreate > verifyCallsBefore);

  // Same key from another account -> conflict, never another user's record.
  const other = await signIn(env, { orcid: "0000-0003-0000-0001", name: "Someone Else" });
  const conflict = await call(env, "/api/observations", { method: "POST", headers: { ...headers(), cookie: other }, body: json(baseObs) });
  assert.equal(conflict.status, 409);

  const mine = await (await call(env, "/api/observations/mine", { headers: { cookie } })).json();
  assert.equal(mine.observations.length, 1);
  assert.equal((await (await call(env, "/api/observations/mine", { headers: { cookie: other } })).json()).observations.length, 0);
});

test("upload -> reviewer queue with EXIF comparison -> correction -> approve / needs changes -> logout", async () => {
  const env = makeEnv();
  const cookie = await signIn(env, contributor);
  const reviewerCookie = await signIn(env, reviewer);
  const created = await call(env, "/api/observations", {
    method: "POST",
    headers: { cookie, origin: ORIGIN, "content-type": "application/json", "idempotency-key": "idem-up", "x-turnstile-token": "t" },
    body: json(baseObs),
  });
  const occId = (await created.json()).observation.occurrence_id as string;
  const up = (bytes: Uint8Array, who = cookie, extra: Record<string, string> = {}) =>
    call(env, `/api/uploads/${encodeURIComponent(occId)}`, {
      method: "PUT",
      headers: { cookie: who, origin: ORIGIN, "content-length": String(bytes.byteLength), ...extra },
      body: bytes,
    });

  // EXIF GPS 44°03'N 123°05'W ~ submitted point; a second photo taken ~40 km away.
  const near = jpegWithGps(44, 3, 123, 5);
  const far = jpegWithGps(44, 25, 123, 5);
  const r1 = await up(near);
  assert.equal(r1.status, 201);
  const m1 = await r1.json();
  assert.equal(m1.detected_mime_type, "image/jpeg");
  assert.equal(m1.exif_extracted.gps_latitude, 44.05);
  assert.equal(m1.exif_extracted.gps_longitude, -123.083333);
  assert.equal(m1.exif_extracted.datetime, "2026:11:18 14:22:00");
  assert.equal((await up(far)).status, 201);
  assert.equal((await up(new TextEncoder().encode("<html>not an image</html>"))).status, 415);

  // Private media: reviewer yes, contributor-owner yes, anonymous no, other contributor no.
  const media = await call(env, `/api/media/${m1.media_id}`, { headers: { cookie: reviewerCookie } });
  assert.equal(media.status, 200);
  assert.equal(media.headers.get("cache-control"), "private, no-store");
  assert.equal((await call(env, `/api/media/${m1.media_id}`)).status, 401);
  const stranger = await signIn(env, { orcid: "0000-0003-0000-0002", name: "Stranger" });
  assert.equal((await call(env, `/api/media/${m1.media_id}`, { headers: { cookie: stranger } })).status, 403);

  // Review queue is reviewer-only and includes verbatim coordinates + EXIF evidence.
  assert.equal((await call(env, "/api/review/pending", { headers: { cookie } })).status, 403);
  const queue = await (await call(env, "/api/review/pending", { headers: { cookie: reviewerCookie } })).json();
  assert.equal(queue.observations.length, 1);
  const item = queue.observations[0];
  assert.equal(item.verbatim.latitude, 44.05);
  assert.equal(item.submitter.orcid_id, "https://sandbox.orcid.org/0000-0002-1825-0097");
  const [nearM, farM] = item.media;
  assert.ok(nearM.exif_offset_m < 300, `near offset ${nearM.exif_offset_m}`);
  assert.equal(nearM.exif_mismatch, false);
  assert.ok(farM.exif_offset_m > 30000);
  assert.equal(farM.exif_mismatch, true);
  assert.equal(nearM.url, `/api/media/${m1.media_id}`);

  // Reviewer correction keeps occurrenceID and records the reviewer as identifier.
  const patched = await call(env, `/api/observations/${encodeURIComponent(occId)}`, {
    method: "PATCH",
    headers: { cookie: reviewerCookie, origin: ORIGIN, "content-type": "application/json" },
    body: json({ scientificName: "Oncorhynchus tshawytscha", notes: "Re-identified from photo 1" }),
  });
  const pObs = (await patched.json()).observation;
  assert.equal(pObs.occurrence_id, occId);
  assert.equal(pObs.revision_number, 2);
  assert.equal(pObs.review_status, "submitted");
  assert.equal(pObs.identified_by_id, "https://sandbox.orcid.org/0000-0001-5109-3700");

  // Contributors cannot approve; reviewers can, producing EXIF-free public JPEGs + overlay.
  assert.equal((await call(env, `/api/review/${encodeURIComponent(occId)}/approve`, { method: "POST", headers: { cookie, origin: ORIGIN } })).status, 403);
  const appr = await (
    await call(env, `/api/review/${encodeURIComponent(occId)}/approve`, {
      method: "POST",
      headers: { cookie: reviewerCookie, origin: ORIGIN, "content-type": "application/json" },
      body: json({ notes: "Verified" }),
    })
  ).json();
  assert.equal(appr.review_status, "approved");
  assert.equal(appr.sanitized_media_keys.length, 2);
  for (const key of appr.sanitized_media_keys) {
    const bytes = env.PUBLIC_BUCKET.store.get(key).bytes as Uint8Array;
    assert.ok(!Buffer.from(bytes).includes(Buffer.from("Exif")), "public derivative must not contain EXIF");
  }
  const overlay = JSON.parse(new TextDecoder().decode(env.PUBLIC_BUCKET.store.get("overlays/approved.geojson").bytes));
  assert.equal(overlay.features.length, 1);
  assert.equal(overlay.features[0].properties.recordedByID, "https://sandbox.orcid.org/0000-0002-1825-0097");
  assert.equal(overlay.features[0].properties.publicationStatus, "not_exported");

  const nc = await (
    await call(env, `/api/review/${encodeURIComponent(occId)}/needs_changes`, {
      method: "POST",
      headers: { cookie: reviewerCookie, origin: ORIGIN, "content-type": "application/json" },
      body: json({ notes: "Photo 2 location disagrees" }),
    })
  ).json();
  assert.equal(nc.review_status, "needs_changes");
  assert.equal(env.PUBLIC_BUCKET.store.has(appr.sanitized_media_keys[0]), false);

  // Logout revokes the server-side session immediately.
  const out = await call(env, "/api/auth/logout", { method: "POST", headers: { cookie: reviewerCookie, origin: ORIGIN } });
  assert.equal(out.status, 200);
  assert.match(out.headers.getSetCookie()[0], /^__Host-bm_session=; .*Max-Age=0$/);
  assert.equal((await call(env, "/api/auth/me", { headers: { cookie: reviewerCookie } })).status, 401);
});

test("upload guards: Content-Length required, size cap, per-observation limit", async () => {
  const env = makeEnv();
  const cookie = await signIn(env, contributor);
  const created = await call(env, "/api/observations", {
    method: "POST",
    headers: { cookie, origin: ORIGIN, "content-type": "application/json", "idempotency-key": "idem-g", "x-turnstile-token": "t" },
    body: json(baseObs),
  });
  const occId = encodeURIComponent((await created.json()).observation.occurrence_id);
  const big = await call(env, `/api/uploads/${occId}`, { method: "PUT", headers: { cookie, origin: ORIGIN, "content-length": String(16 * 1024 * 1024) }, body: new Uint8Array(10) });
  assert.equal(big.status, 413);
  const img = jpegWithGps(44, 3, 123, 5);
  for (let i = 0; i < 10; i++) {
    assert.equal((await call(env, `/api/uploads/${occId}`, { method: "PUT", headers: { cookie, origin: ORIGIN, "content-length": String(img.byteLength) }, body: img })).status, 201);
  }
  const eleventh = await call(env, `/api/uploads/${occId}`, { method: "PUT", headers: { cookie, origin: ORIGIN, "content-length": String(img.byteLength) }, body: img });
  assert.equal(eleventh.status, 409);
});


test("publication pipeline: DwC-A export, validator, crawl, reconciliation, overlay, withdrawal, cron", async () => {
  const env = makeEnv({
    PUBLICATION_BASE_URL: "https://staging.basinmap.org/api/r2",
    GBIF_API_BASE: "https://api.gbif-uat.org/v1",
    GBIF_DATASET_KEY: "11111111-2222-3333-4444-555555555555",
    GBIF_USER: "basinmap-bot",
    GBIF_PWD: "bot-password",
  });
  const cookie = await signIn(env, contributor);
  const admin = await signIn(env, reviewer); // ADMIN_ORCID_IDS -> admin
  const post = (path: string, who: string, body: unknown, extra: Record<string, string> = {}) =>
    call(env, path, { method: "POST", headers: { cookie: who, origin: ORIGIN, "content-type": "application/json", ...extra }, body: json(body) });
  const create = async (key: string, over: Record<string, unknown>) =>
    (await (await post("/api/observations", cookie, { ...baseObs, ...over }, { "idempotency-key": key, "x-turnstile-token": "t" })).json()).observation;
  const a = await create("pub-a", { scientificName: "Oncorhynchus kisutch" });
  const b = await create("pub-b", { scientificName: "Castor canadensis", license: "CC0 1.0" });
  const c = await create("pub-c", { scientificName: "Ardea herodias" });
  const img = jpegWithGps(44, 3, 123, 5);
  await call(env, `/api/uploads/${encodeURIComponent(a.occurrence_id)}`, {
    method: "PUT", headers: { cookie, origin: ORIGIN, "content-length": String(img.byteLength) }, body: img,
  });
  for (const o of [a, b]) assert.equal((await post(`/api/review/${encodeURIComponent(o.occurrence_id)}/approve`, admin, { notes: "ok" })).status, 200);

  // Contributors cannot trigger publication.
  assert.equal((await post("/api/admin/publication/run", cookie, {})).status, 403);

  const run1 = await (await post("/api/admin/publication/run", admin, {})).json();
  assert.equal(run1.export.changed, true);
  assert.equal(run1.export.record_count, 2, "CC0 record is publishable inside the CC-BY dataset; unapproved excluded");
  assert.equal(run1.export.media_count, 1);
  assert.equal(run1.export.newly_exported, 2);
  assert.equal(run1.validator, "submitted");
  assert.equal(run1.crawl, "requested");
  const v = gbifCalls.find((g) => g.kind === "validate")!;
  assert.equal(v.auth, `Basic ${btoa("basinmap-bot:bot-password")}`);
  assert.ok(v.form instanceof FormData && v.form.get("file") instanceof Blob && v.form.get("validationRequest"));
  const store = env.PUBLIC_BUCKET.store;
  const latest = store.get("exports/dwca-latest.zip");
  assert.equal(latest.httpMetadata.cacheControl, "public, max-age=300, must-revalidate");
  assert.ok(store.get(`exports/archive/${run1.export.export_id}.zip`));
  assert.equal(store.get(`exports/archive/${run1.export.export_id}.zip`).httpMetadata.cacheControl, "public, max-age=31536000, immutable");
  if (process.env.DWCA_OUT) writeFileSync(process.env.DWCA_OUT, latest.bytes);
  const st1 = JSON.parse(new TextDecoder().decode(store.get("exports/status.json").bytes));
  assert.equal(st1.latest_export.record_count, 2);
  assert.equal(st1.archive_url, "https://staging.basinmap.org/api/r2/exports/dwca-latest.zip");
  assert.equal(st1.approved_records_by_publication_status.exported, 2);
  assert.ok(!JSON.stringify(st1).includes("verbatim"), "status.json must not contain private data");
  const statusOf = (id: string) =>
    (env.DB.raw.prepare("SELECT publication_status, gbif_occurrence_key FROM observations WHERE occurrence_id = ?").get(id) as any);
  assert.equal(statusOf(a.occurrence_id).publication_status, "exported");
  assert.equal(statusOf(c.occurrence_id).publication_status, "not_exported");

  // Unchanged content -> no new archive and no new crawl; validator job polled to completion.
  const crawlsBefore = gbifCalls.filter((g) => g.kind === "crawl").length;
  const run2 = await (await post("/api/admin/publication/run", admin, {})).json();
  assert.equal(run2.export.changed, false);
  assert.equal(gbifCalls.filter((g) => g.kind === "crawl").length, crawlsBefore);
  assert.deepEqual(run2.validations, [{ export_id: run1.export.export_id, status: "finished" }]);
  assert.equal(run2.status.latest_export.gbif_validator.indexeable, true);

  // GBIF indexes record A -> reconciliation marks it and never regresses it on export.
  gbifIndexed = [{ key: 4400000123, occurrenceID: a.occurrence_id }, { key: 1, occurrenceID: "urn:someone-else:1" }];
  const run3 = await (await post("/api/admin/publication/run", admin, { force: true })).json();
  assert.deepEqual({ checked: run3.reconcile.checked, newly: run3.reconcile.newly_indexed }, { checked: 2, newly: 1 });
  assert.equal(statusOf(a.occurrence_id).publication_status, "indexed_by_gbif");
  assert.equal(statusOf(a.occurrence_id).gbif_occurrence_key, 4400000123);
  assert.equal(run3.export.changed, true, "force=true always writes a new archive");
  assert.equal(run3.export.newly_exported, 0, "indexed record keeps its status");

  // Overlay hides a record only when the live map release already contains it.
  let overlay = JSON.parse(new TextDecoder().decode(store.get("overlays/approved.geojson").bytes));
  assert.deepEqual(overlay.features.map((f: any) => f.id).sort(), [a.occurrence_id, b.occurrence_id].sort());
  await env.PUBLIC_BUCKET.put("releases/manifest.json", json({ current_version: "v9", layers: { snapshot_own_occurrence_ids: "/releases/v9/snapshot_own_occurrence_ids.json" } }));
  await env.PUBLIC_BUCKET.put("releases/v9/snapshot_own_occurrence_ids.json", json([a.occurrence_id]));

  // Withdrawal: B no longer approved -> omitted from the next archive, status back to not_exported.
  await post(`/api/review/${encodeURIComponent(b.occurrence_id)}/needs_changes`, admin, { notes: "check id" });
  overlay = JSON.parse(new TextDecoder().decode(store.get("overlays/approved.geojson").bytes));
  assert.deepEqual(overlay.features.map((f: any) => f.id), [], "A is in the live snapshot, B is no longer approved");
  const pending: Promise<unknown>[] = [];
  await worker.scheduled({} as any, env, { waitUntil: (p: Promise<unknown>) => pending.push(p) } as any);
  await Promise.all(pending);
  assert.equal(statusOf(b.occurrence_id).publication_status, "not_exported");
  const lastRun = env.DB.raw.prepare("SELECT trigger, error, export_id FROM publication_runs ORDER BY started_at DESC, rowid DESC LIMIT 1").get() as any;
  assert.equal(lastRun.trigger, "cron");
  assert.equal(lastRun.error, null);
  const lastExport = env.DB.raw.prepare("SELECT record_count FROM publication_exports WHERE export_id = ?").get(lastRun.export_id) as any;
  assert.equal(lastExport.record_count, 1);
  const revs = env.DB.raw.prepare("SELECT change_type, notes FROM observation_revisions WHERE occurrence_id = ? ORDER BY revision_number").all(b.occurrence_id) as any[];
  assert.equal(revs[revs.length - 2].change_type, "needs_changes");
  assert.match(revs[revs.length - 1].notes, /^Withdrawn: omitted from DwC-A /);

  // Admin status endpoint.
  const statusRes = await (await call(env, "/api/admin/publication/status", { headers: { cookie: admin } })).json();
  assert.ok(statusRes.runs.length >= 4 && statusRes.exports.length >= 3);
});
