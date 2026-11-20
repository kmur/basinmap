/**
 * Authentication for basinmap.org (hosting plan Section 6).
 *
 * - ORCID OAuth 2.0 authorization-code flow (`/authenticate` scope) with a confidential
 *   client: the client secret stays in the Worker, the browser never sees a token.
 * - Server-side sessions in D1 (`sessions`, migration 0002). The browser holds only
 *   `__Host-bm_session=<random token>.<HMAC-SHA256>` (HttpOnly; Secure; SameSite=Lax;
 *   Path=/); D1 stores SHA-256(token), so logout/role changes apply immediately and a
 *   leaked table cannot be replayed.
 * - Cookie-authenticated mutations must carry a same-origin `Origin` header (CSRF
 *   defence in depth on top of SameSite=Lax).
 * - Dev bearer tokens (`dev-contributor-orcid`, `dev-reviewer-orcid`, `user:<id>`) are
 *   accepted ONLY when ENVIRONMENT=development; staging and production reject them.
 */

export interface AuthEnv {
  DB?: D1Database;
  ENVIRONMENT?: string;
  ORCID_CLIENT_ID?: string;
  ORCID_CLIENT_SECRET?: string;
  ORCID_BASE_URL?: string;
  SESSION_SECRET?: string;
  ADMIN_ORCID_IDS?: string;
}

export interface UserRow {
  id: string;
  orcid_id: string | null;
  display_name: string;
  email: string | null;
  role: "contributor" | "reviewer" | "admin";
}

export interface AuthContext {
  user: UserRow;
  via: "cookie" | "dev-bearer";
}

export const SESSION_COOKIE = "__Host-bm_session";
export const STATE_COOKIE = "__Host-bm_oauth";
const STATE_TTL_SECONDS = 600;
const CONTRIBUTOR_SESSION_SECONDS = 7 * 24 * 3600;
// Section 6: stronger controls for reviewers/administrators -> shorter sessions.
const PRIVILEGED_SESSION_SECONDS = 12 * 3600;
const ORCID_ID_RE = /^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$/;

const DEV_TOKEN_MAP: Record<string, string> = {
  "dev-contributor-orcid": "usr-contrib-1",
  "dev-reviewer-orcid": "usr-review-1",
};

const enc = new TextEncoder();

export function isDevelopment(env: AuthEnv): boolean {
  return (env.ENVIRONMENT || "production") === "development";
}

export function orcidBaseUrl(env: AuthEnv): string {
  return (env.ORCID_BASE_URL || "https://orcid.org").replace(/\/+$/, "");
}

export function orcidConfigured(env: AuthEnv): boolean {
  return Boolean(env.ORCID_CLIENT_ID && env.ORCID_CLIENT_SECRET && env.SESSION_SECRET);
}

function b64url(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlDecode(text: string) {
  const norm = text.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(norm + "=".repeat((4 - (norm.length % 4)) % 4));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function randomToken(bytes = 32): string {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return b64url(buf);
}

async function hmacKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [
    "sign",
    "verify",
  ]);
}

export async function signValue(secret: string, value: string): Promise<string> {
  const sig = await crypto.subtle.sign("HMAC", await hmacKey(secret), enc.encode(value));
  return `${value}.${b64url(new Uint8Array(sig))}`;
}

/** Returns the signed value if the HMAC verifies (constant-time via subtle.verify), else null. */
export async function verifySignedValue(secret: string, signed: string): Promise<string | null> {
  const dot = signed.lastIndexOf(".");
  if (dot <= 0) return null;
  const value = signed.slice(0, dot);
  const sig = (() => {
    try {
      return b64urlDecode(signed.slice(dot + 1));
    } catch (_err) {
      return null;
    }
  })();
  if (!sig) return null;
  const ok = await crypto.subtle.verify("HMAC", await hmacKey(secret), sig, enc.encode(value));
  return ok ? value : null;
}

export async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", enc.encode(text));
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export function parseCookies(request: Request): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of (request.headers.get("cookie") || "").split(";")) {
    const eq = part.indexOf("=");
    if (eq <= 0) continue;
    out[part.slice(0, eq).trim()] = part.slice(eq + 1).trim();
  }
  return out;
}

export function buildCookie(name: string, value: string, maxAgeSeconds: number): string {
  return `${name}=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${Math.max(0, Math.floor(maxAgeSeconds))}`;
}

/** Only same-site absolute paths are allowed as post-login destinations (no open redirects). */
export function safeReturnTo(raw: string | null): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\") || raw.startsWith("/api/")) {
    return "/";
  }
  return raw.slice(0, 512);
}

function redirect(location: string, cookies: string[] = []): Response {
  const headers = new Headers({ location, "cache-control": "private, no-store" });
  for (const c of cookies) headers.append("set-cookie", c);
  return new Response(null, { status: 302, headers });
}

function authJson(body: unknown, status: number, cookies: string[] = []): Response {
  const headers = new Headers({
    "content-type": "application/json; charset=utf-8",
    "cache-control": "private, no-store",
    "x-content-type-options": "nosniff",
  });
  for (const c of cookies) headers.append("set-cookie", c);
  return new Response(JSON.stringify(body, null, 2), { status, headers });
}

/** Resolve the caller: session cookie first; dev bearer tokens only in development. */
export async function authenticate(request: Request, env: AuthEnv): Promise<AuthContext | null> {
  const db = env.DB;
  if (!db) return null;

  if (isDevelopment(env)) {
    const match = (request.headers.get("authorization") || "").match(/^Bearer\s+(.+)$/i);
    if (match) {
      const token = match[1].trim();
      const mapped = DEV_TOKEN_MAP[token] || (token.startsWith("user:") ? token.slice(5) : null);
      const user = mapped
        ? await db.prepare("SELECT * FROM users WHERE id = ?").bind(mapped).first<UserRow>()
        : await db.prepare("SELECT * FROM users WHERE orcid_id = ?").bind(token).first<UserRow>();
      return user ? { user, via: "dev-bearer" } : null;
    }
  }

  const raw = parseCookies(request)[SESSION_COOKIE];
  if (!raw || !env.SESSION_SECRET) return null;
  const token = await verifySignedValue(env.SESSION_SECRET, raw);
  if (!token) return null;
  const row = await db
    .prepare(
      `SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
       WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?`
    )
    .bind(await sha256Hex(token), new Date().toISOString())
    .first<UserRow>();
  return row ? { user: row, via: "cookie" } : null;
}

/**
 * CSRF defence for cookie sessions: state-changing requests must come from this origin.
 * Returns an error Response, or null when the request may proceed.
 */
export function checkMutationOrigin(request: Request, ctx: AuthContext): Response | null {
  if (ctx.via !== "cookie" || request.method === "GET" || request.method === "HEAD") return null;
  const origin = request.headers.get("origin");
  if (!origin || origin !== new URL(request.url).origin) {
    return authJson({ error: "Cross-origin request rejected", code: "bad_origin" }, 403);
  }
  return null;
}

export async function handleLogin(request: Request, env: AuthEnv): Promise<Response> {
  if (!orcidConfigured(env)) {
    return authJson(
      {
        error: "ORCID sign-in is not configured for this environment",
        code: "orcid_not_configured",
        dev_tokens_enabled: isDevelopment(env),
      },
      503
    );
  }
  const url = new URL(request.url);
  const state = randomToken(24);
  const payload = b64url(
    enc.encode(
      JSON.stringify({
        s: state,
        r: safeReturnTo(url.searchParams.get("return_to")),
        e: Math.floor(Date.now() / 1000) + STATE_TTL_SECONDS,
      })
    )
  );
  const stateCookie = buildCookie(STATE_COOKIE, await signValue(env.SESSION_SECRET as string, payload), STATE_TTL_SECONDS);
  const authorize = new URL(`${orcidBaseUrl(env)}/oauth/authorize`);
  authorize.searchParams.set("client_id", env.ORCID_CLIENT_ID as string);
  authorize.searchParams.set("response_type", "code");
  authorize.searchParams.set("scope", "/authenticate");
  authorize.searchParams.set("redirect_uri", `${url.origin}/api/auth/callback`);
  authorize.searchParams.set("state", state);
  return redirect(authorize.toString(), [stateCookie]);
}

export async function handleCallback(request: Request, env: AuthEnv): Promise<Response> {
  const clearState = buildCookie(STATE_COOKIE, "", 0);
  if (!orcidConfigured(env) || !env.DB) {
    return authJson({ error: "ORCID sign-in is not configured", code: "orcid_not_configured" }, 503, [clearState]);
  }
  const url = new URL(request.url);
  const fail = (code: string) => redirect(`/?auth_error=${encodeURIComponent(code)}`, [clearState]);

  if (url.searchParams.get("error")) return fail(url.searchParams.get("error") || "access_denied");
  const code = url.searchParams.get("code") || "";
  const state = url.searchParams.get("state") || "";
  const signedState = parseCookies(request)[STATE_COOKIE];
  if (!code || !state || !signedState) return fail("missing_state");

  const statePayload = await verifySignedValue(env.SESSION_SECRET as string, signedState);
  if (!statePayload) return fail("bad_state");
  let parsed: { s?: string; r?: string; e?: number };
  try {
    parsed = JSON.parse(new TextDecoder().decode(b64urlDecode(statePayload)));
  } catch (_err) {
    return fail("bad_state");
  }
  if (parsed.s !== state || !parsed.e || parsed.e < Math.floor(Date.now() / 1000)) return fail("state_mismatch");

  const tokenResp = await fetch(`${orcidBaseUrl(env)}/oauth/token`, {
    method: "POST",
    headers: { accept: "application/json", "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: env.ORCID_CLIENT_ID as string,
      client_secret: env.ORCID_CLIENT_SECRET as string,
      grant_type: "authorization_code",
      code,
      redirect_uri: `${url.origin}/api/auth/callback`,
    }),
  });
  if (!tokenResp.ok) return fail("token_exchange_failed");
  const token = (await tokenResp.json().catch(() => ({}))) as { orcid?: string; name?: string };
  const orcid = String(token.orcid || "");
  if (!ORCID_ID_RE.test(orcid)) return fail("invalid_orcid");

  // ORCID iD as a resolvable URI -> Darwin Core recordedByID / identifiedByID.
  const orcidUri = `${orcidBaseUrl(env)}/${orcid}`;
  const displayName = String(token.name || "").trim().slice(0, 200) || orcid;
  const admins = new Set(
    (env.ADMIN_ORCID_IDS || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
  );
  const now = new Date().toISOString();
  const db = env.DB;
  let user = await db.prepare("SELECT * FROM users WHERE orcid_id = ?").bind(orcidUri).first<UserRow>();
  if (!user) {
    const id = `usr-${crypto.randomUUID()}`;
    await db
      .prepare(
        "INSERT INTO users (id, orcid_id, display_name, email, role, created_at, last_login_at) VALUES (?, ?, ?, NULL, ?, ?, ?)"
      )
      .bind(id, orcidUri, displayName, admins.has(orcid) ? "admin" : "contributor", now, now)
      .run();
  } else {
    await db
      .prepare(
        "UPDATE users SET display_name = ?, last_login_at = ?, role = CASE WHEN ? THEN 'admin' ELSE role END WHERE id = ?"
      )
      .bind(displayName, now, admins.has(orcid) ? 1 : 0, user.id)
      .run();
  }
  user = await db.prepare("SELECT * FROM users WHERE orcid_id = ?").bind(orcidUri).first<UserRow>();
  if (!user) return fail("user_upsert_failed");

  const ttl = user.role === "contributor" ? CONTRIBUTOR_SESSION_SECONDS : PRIVILEGED_SESSION_SECONDS;
  const sessionToken = randomToken(32);
  await db
    .prepare("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)")
    .bind(await sha256Hex(sessionToken), user.id, now, new Date(Date.now() + ttl * 1000).toISOString())
    .run();
  const sessionCookie = buildCookie(SESSION_COOKIE, await signValue(env.SESSION_SECRET as string, sessionToken), ttl);
  return redirect(safeReturnTo(parsed.r || "/"), [sessionCookie, clearState]);
}

export async function handleLogout(request: Request, env: AuthEnv): Promise<Response> {
  const clear = buildCookie(SESSION_COOKIE, "", 0);
  const ctx = await authenticate(request, env);
  if (ctx) {
    const bad = checkMutationOrigin(request, ctx);
    if (bad) return bad;
  }
  const raw = parseCookies(request)[SESSION_COOKIE];
  if (raw && env.SESSION_SECRET && env.DB) {
    const token = await verifySignedValue(env.SESSION_SECRET, raw);
    if (token) {
      await env.DB.prepare("UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL")
        .bind(new Date().toISOString(), await sha256Hex(token))
        .run();
    }
  }
  return authJson({ signed_out: true }, 200, [clear]);
}
