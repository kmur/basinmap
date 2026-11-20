/**
 * Contributor & reviewer workflows (hosting plan Sections 5.1 and 6):
 *   - ORCID sign-in state (session cookie) / dev tokens in local development
 *   - "Submit Observation" drawer: map coordinate picking, Darwin Core fields, explicit
 *     dataset-wide license acceptance, Cloudflare Turnstile, idempotent POST, and
 *     per-photo streaming uploads with progress (XHR) and retry
 *   - "My submissions" with honest review/publication status (never implies GBIF indexing)
 *   - "Reviewer queue": verbatim coordinates vs private EXIF GPS, corrections (PATCH),
 *     Approve / Request changes
 */

export interface ContribHooks {
  getMap: () => any;
  onReviewDecision: () => void;
}

interface ContribUser {
  id: string;
  orcid_id: string | null;
  display_name: string;
  role: string;
}

interface ContribConfig {
  supported_licenses: string[];
  submissions_enabled: boolean;
  environment: string;
  max_upload_bytes: number;
  max_media_per_observation: number;
  turnstile_site_key: string;
  auth: { orcid_login_enabled: boolean; orcid_base_url: string; dev_tokens_enabled: boolean };
}

const DEV_TOKEN_KEY = "bm_dev_token";
const ACCEPTED_TYPES = ["image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"];

const cs: any = {
  hooks: null,
  config: null,
  user: null,
  apiOnline: false,
  picking: false,
  idempotencyKey: "",
  savedOccurrenceId: "",
  turnstileToken: "",
  turnstileWidgetId: null,
  files: [],
  uploads: [],
  activeTab: "submit",
  queueStatus: "submitted",
  queue: [],
  selectedReviewId: "",
  objectUrls: [],
};

function el(id: string): any {
  return document.getElementById(id);
}

function esc(raw: unknown): string {
  return String(raw ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function newKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `k-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  if (cs.config && cs.config.auth && cs.config.auth.dev_tokens_enabled) {
    const tok = sessionStorage.getItem(DEV_TOKEN_KEY);
    if (tok) headers["Authorization"] = `Bearer ${tok}`;
  }
  return headers;
}

async function api(path: string, init: any): Promise<{ status: number; body: any }> {
  const opts: any = Object.assign({ credentials: "same-origin" }, init || {});
  opts.headers = Object.assign({}, authHeaders(), (init && init.headers) || {});
  const resp = await fetch(path, opts);
  let body: any = null;
  try {
    body = await resp.json();
  } catch (_err) {
    body = null;
  }
  return { status: resp.status, body };
}

function errorText(res: { status: number; body: any }): string {
  const msg = res.body && res.body.error ? res.body.error : `HTTP ${res.status}`;
  if (res.status === 401) return "Your session has expired. Sign in with ORCID again; your entries are kept.";
  if (res.status === 429) return "Too many submissions in the last hour. Please try again later.";
  return msg;
}

const REVIEW_LABELS: Record<string, string> = {
  draft: "Draft",
  submitted: "Awaiting review",
  needs_changes: "Changes requested",
  approved: "Approved (shown on map as provisional)",
};

const PUBLICATION_LABELS: Record<string, string> = {
  not_exported: "Not yet published to GBIF",
  exported: "Exported in Darwin Core Archive; not yet on GBIF",
  published_by_provider: "Published by provider; awaiting GBIF indexing",
  indexed_by_gbif: "Indexed by GBIF",
  publication_error: "Publication error (team notified)",
};

function statusBadges(obs: any): string {
  return `<span class="status-chip review-${esc(obs.review_status)}">${esc(REVIEW_LABELS[obs.review_status] || obs.review_status)}</span>
    <span class="status-chip pub-${esc(obs.publication_status)}">${esc(PUBLICATION_LABELS[obs.publication_status] || obs.publication_status)}</span>`;
}

// ---------------------------------------------------------------------------
// Auth area
// ---------------------------------------------------------------------------

function renderAuthArea(): void {
  const area = el("auth-area");
  if (!area) return;
  const cfg = cs.config;
  if (cs.user) {
    const role = cs.user.role === "contributor" ? "" : ` · ${esc(cs.user.role)}`;
    const orcid = cs.user.orcid_id
      ? `<a class="orcid-link" href="${esc(cs.user.orcid_id)}" target="_blank" rel="noopener noreferrer" title="ORCID iD">iD</a>`
      : "";
    area.innerHTML = `
      <button id="btn-open-drawer" class="btn btn-primary" type="button">Submit Observation</button>
      ${cs.user.role === "reviewer" || cs.user.role === "admin" ? `<button id="btn-open-review" class="btn btn-ghost" type="button">Review queue</button>` : ""}
      <span class="user-chip">${orcid}<span>${esc(cs.user.display_name)}${role}</span></span>
      <button id="btn-sign-out" class="btn btn-ghost" type="button">Sign out</button>`;
  } else if (cfg && cfg.auth && cfg.auth.dev_tokens_enabled) {
    area.innerHTML = `
      <label class="dev-signin">Dev sign-in
        <select id="dev-token-select" aria-label="Development identity">
          <option value="">— choose —</option>
          <option value="dev-contributor-orcid">Pilot contributor</option>
          <option value="dev-reviewer-orcid">Pilot reviewer</option>
        </select>
      </label>`;
  } else if (cfg && cfg.auth && cfg.auth.orcid_login_enabled) {
    const back = encodeURIComponent(location.pathname + location.search);
    area.innerHTML = `
      <button id="btn-open-drawer" class="btn btn-ghost" type="button">Submit Observation</button>
      <a id="btn-sign-in" class="btn btn-orcid" href="/api/auth/login?return_to=${back}">Sign in with ORCID</a>`;
  } else {
    area.innerHTML = `<span class="pill warn" title="ORCID sign-in is not configured in this environment">Sign-in unavailable</span>`;
  }

  const openBtn = el("btn-open-drawer");
  if (openBtn) openBtn.addEventListener("click", () => openDrawer(cs.user ? "submit" : "signin"));
  const reviewBtn = el("btn-open-review");
  if (reviewBtn) reviewBtn.addEventListener("click", () => openDrawer("review"));
  const outBtn = el("btn-sign-out");
  if (outBtn) outBtn.addEventListener("click", signOut);
  const devSel = el("dev-token-select");
  if (devSel) {
    devSel.addEventListener("change", async () => {
      if (devSel.value) sessionStorage.setItem(DEV_TOKEN_KEY, devSel.value);
      await refreshUser();
    });
  }
  const reviewTab = el("tab-review");
  if (reviewTab) reviewTab.classList.toggle("hidden", !(cs.user && (cs.user.role === "reviewer" || cs.user.role === "admin")));
}

async function refreshUser(): Promise<void> {
  try {
    const res = await api("/api/auth/me", { method: "GET" });
    cs.user = res.status === 200 && res.body ? res.body.user : null;
  } catch (_err) {
    cs.user = null;
  }
  renderAuthArea();
  if (!el("contrib-drawer").classList.contains("hidden")) showTab(cs.activeTab);
}

async function signOut(): Promise<void> {
  sessionStorage.removeItem(DEV_TOKEN_KEY);
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (_err) {
    // Session cookie also expires on its own; the UI state is what matters here.
  }
  cs.user = null;
  closeDrawer();
  renderAuthArea();
}

// ---------------------------------------------------------------------------
// Drawer & tabs
// ---------------------------------------------------------------------------

function placeDrawer(): void {
  // Sit directly under the header, whose height changes with the sign-in controls and wrapping.
  const header = document.querySelector(".top-bar");
  if (header) document.documentElement.style.setProperty("--header-bottom", `${Math.round(header.getBoundingClientRect().bottom)}px`);
}

function openDrawer(tab: string): void {
  placeDrawer();
  el("contrib-drawer").classList.remove("hidden");
  showTab(tab === "signin" ? "submit" : tab);
}

function closeDrawer(): void {
  el("contrib-drawer").classList.add("hidden");
  stopPicking();
  clearCompareLayer();
}

function showTab(tab: string): void {
  cs.activeTab = tab;
  for (const name of ["submit", "mine", "review"]) {
    const btn = el(`tab-${name}`);
    const panel = el(`panel-${name}`);
    if (btn) btn.setAttribute("aria-selected", String(name === tab));
    if (panel) panel.classList.toggle("hidden", name !== tab);
  }
  if (tab !== "review") clearCompareLayer();
  if (tab === "submit") renderSubmitPanel();
  if (tab === "mine") loadMine();
  if (tab === "review") loadQueue();
}

function signInPrompt(): string {
  const cfg = cs.config;
  if (cfg && cfg.auth && cfg.auth.orcid_login_enabled) {
    const back = encodeURIComponent(location.pathname + location.search);
    return `<div class="notice">Contributions are attributed to your ORCID iD (Darwin Core <code>recordedByID</code>).
      <a class="btn btn-orcid" href="/api/auth/login?return_to=${back}">Sign in with ORCID</a></div>`;
  }
  if (cfg && cfg.auth && cfg.auth.dev_tokens_enabled) {
    return `<div class="notice">Choose a development identity in the header to submit.</div>`;
  }
  return `<div class="notice warn">Sign-in is not available in this environment.</div>`;
}

// ---------------------------------------------------------------------------
// Submission form
// ---------------------------------------------------------------------------

function renderSubmitPanel(): void {
  const panel = el("panel-submit");
  if (!panel) return;
  if (!cs.apiOnline) {
    panel.innerHTML = `<div class="notice warn">The submission service is unavailable right now. The map keeps working; please try again later.</div>`;
    return;
  }
  if (!cs.user) {
    panel.innerHTML = signInPrompt();
    return;
  }
  const cfg = cs.config;
  if (!cfg.submissions_enabled) {
    panel.innerHTML = `<div class="notice warn">Submissions are temporarily disabled in this environment (spam protection is not configured).</div>`;
    return;
  }
  if (panel.getAttribute("data-ready") === "1") return; // keep the user's entries when switching tabs
  if (!cs.idempotencyKey) cs.idempotencyKey = newKey();
  const now = new Date();
  // Local calendar date (toISOString would give tomorrow's UTC date in the evening in the Americas).
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const licenses = (cfg.supported_licenses || ["CC-BY 4.0", "CC0 1.0"])
    .map(
      (lic: string, i: number) =>
        `<label class="radio"><input type="radio" name="obs-license" value="${esc(lic)}" ${i === 0 ? "checked" : ""}/> ${esc(lic)}</label>`
    )
    .join("");
  const maxMb = Math.round((cfg.max_upload_bytes || 15728640) / 1048576);
  panel.setAttribute("data-ready", "1");
  panel.innerHTML = `
    <form id="obs-form" novalidate>
      <div class="form-row">
        <label for="obs-name">Scientific name *</label>
        <input id="obs-name" list="obs-name-suggest" maxlength="200" autocomplete="off" required placeholder="e.g. Oncorhynchus kisutch" />
        <datalist id="obs-name-suggest"></datalist>
        <small id="obs-taxon-note" class="muted-note"></small>
      </div>
      <div class="form-row">
        <label for="obs-date">Date observed *</label>
        <input id="obs-date" type="date" max="${today}" value="${today}" required />
      </div>
      <fieldset class="form-row">
        <legend>Location *</legend>
        <div class="coord-pair">
          <input id="obs-lat" type="number" step="0.00001" min="-90" max="90" placeholder="Latitude" aria-label="Latitude" required />
          <input id="obs-lon" type="number" step="0.00001" min="-180" max="180" placeholder="Longitude" aria-label="Longitude" required />
        </div>
        <div class="btn-row">
          <button id="btn-pick" class="btn btn-ghost" type="button">Pick on map</button>
          <button id="btn-geolocate" class="btn btn-ghost" type="button">Use my location</button>
        </div>
        <label for="obs-unc">Coordinate uncertainty (metres)</label>
        <input id="obs-unc" type="number" min="1" max="100000" value="30" />
        <label class="check"><input id="obs-sensitive" type="checkbox" /> Sensitive species location: publish only a generalised (0.1°) position</label>
      </fieldset>
      <div class="form-row">
        <label for="obs-locality">Locality</label>
        <input id="obs-locality" maxlength="1000" placeholder="e.g. McKenzie River at Hendricks Bridge" />
      </div>
      <div class="form-row">
        <label for="obs-notes">Notes for reviewers</label>
        <textarea id="obs-notes" rows="3" maxlength="4000"></textarea>
      </div>
      <div class="form-row">
        <label for="obs-photos">Photos (JPEG, PNG, WebP or HEIC; up to ${esc(cfg.max_media_per_observation)} × ${maxMb} MB)</label>
        <input id="obs-photos" type="file" multiple accept="image/jpeg,image/png,image/webp,image/heic,.heic,.heif" />
        <ul id="obs-file-list" class="file-list"></ul>
      </div>
      <fieldset class="form-row license-box">
        <legend>Dataset licence *</legend>
        <div class="radio-row">${licenses}</div>
        <label for="obs-rights">Rights holder</label>
        <input id="obs-rights" maxlength="200" value="${esc(cs.user.display_name)}" />
        <label class="check"><input id="obs-accept" type="checkbox" required />
          I agree that this record and every photo I attach are published under the selected licence as part of the
          basinmap.org dataset (one licence applies to the whole dataset on GBIF).</label>
      </fieldset>
      <div class="form-row">
        <div id="turnstile-box"></div>
        <small id="turnstile-note" class="muted-note"></small>
      </div>
      <div id="obs-error" class="notice error hidden" role="alert"></div>
      <button id="btn-submit-obs" class="btn btn-primary" type="submit">Submit for review</button>
    </form>
    <div id="obs-result" class="hidden"></div>`;

  el("obs-form").addEventListener("submit", (e: any) => {
    e.preventDefault();
    submitObservation();
  });
  el("btn-pick").addEventListener("click", togglePicking);
  el("btn-geolocate").addEventListener("click", useGeolocation);
  el("obs-photos").addEventListener("change", onFilesChosen);
  el("obs-name").addEventListener("input", onNameInput);
  el("obs-lat").addEventListener("change", updatePickMarker);
  el("obs-lon").addEventListener("change", updatePickMarker);
  mountTurnstile();
}

function resetSubmitPanel(): void {
  const panel = el("panel-submit");
  panel.removeAttribute("data-ready");
  cs.idempotencyKey = "";
  cs.savedOccurrenceId = "";
  cs.files = [];
  cs.uploads = [];
  cs.turnstileToken = "";
  cs.turnstileWidgetId = null;
  removePickMarker();
  renderSubmitPanel();
}

// ---- taxon suggestions (GBIF species API; optional, failure tolerant)
let suggestTimer: any = null;
let suggestions: any[] = [];

function onNameInput(): void {
  const q = String(el("obs-name").value || "").trim();
  const match = suggestions.find((s: any) => s.canonicalName === q || s.scientificName === q);
  el("obs-taxon-note").textContent = match
    ? `GBIF taxon ${match.key} · ${match.rank || ""}${match.family ? " · " + match.family : ""}`
    : "";
  if (suggestTimer) clearTimeout(suggestTimer);
  if (q.length < 3 || match) return;
  suggestTimer = setTimeout(async () => {
    try {
      const resp = await fetch(`https://api.gbif.org/v1/species/suggest?limit=8&q=${encodeURIComponent(q)}`);
      if (!resp.ok) return;
      suggestions = await resp.json();
      el("obs-name-suggest").innerHTML = suggestions
        .map((s: any) => `<option value="${esc(s.canonicalName || s.scientificName)}">${esc(s.rank || "")} ${esc(s.family || "")}</option>`)
        .join("");
    } catch (_err) {
      // The GBIF backbone lookup is a convenience; free-text names are still accepted.
    }
  }, 300);
}

function selectedTaxon(name: string): any {
  return suggestions.find((s: any) => s.canonicalName === name || s.scientificName === name) || null;
}

// ---- coordinates
export function isPickingCoordinates(): boolean {
  return cs.picking;
}

function togglePicking(): void {
  if (cs.picking) {
    stopPicking();
    return;
  }
  const map = cs.hooks.getMap();
  if (!map) {
    el("obs-error").textContent = "Map picking needs the interactive map; please type coordinates instead.";
    el("obs-error").classList.remove("hidden");
    return;
  }
  cs.picking = true;
  el("btn-pick").textContent = "Click the map… (cancel)";
  map.getCanvas().style.cursor = "crosshair";
  document.body.classList.add("picking");
  // On narrow screens the map sits below the sidebar: bring it into view for the tap.
  const container = map.getContainer();
  const rect = container.getBoundingClientRect();
  if (rect.top < 0 || rect.bottom > window.innerHeight) container.scrollIntoView({ block: "center", behavior: "smooth" });
}

function stopPicking(): void {
  cs.picking = false;
  const btn = el("btn-pick");
  if (btn) btn.textContent = "Pick on map";
  const map = cs.hooks && cs.hooks.getMap();
  if (map) map.getCanvas().style.cursor = "";
  document.body.classList.remove("picking");
}

function onMapPick(e: any): void {
  if (!cs.picking) return;
  el("obs-lat").value = e.lngLat.lat.toFixed(5);
  el("obs-lon").value = e.lngLat.lng.toFixed(5);
  stopPicking();
  updatePickMarker();
}

function pointFc(lon: number, lat: number): any {
  return { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [lon, lat] } }] };
}

function updatePickMarker(): void {
  const map = cs.hooks.getMap();
  const lat = Number(el("obs-lat").value);
  const lon = Number(el("obs-lon").value);
  if (!map || !Number.isFinite(lat) || !Number.isFinite(lon) || el("obs-lat").value === "") return;
  const data = pointFc(lon, lat);
  if (map.getSource("pick-point")) {
    map.getSource("pick-point").setData(data);
  } else {
    map.addSource("pick-point", { type: "geojson", data });
    map.addLayer({
      id: "pick-point",
      type: "circle",
      source: "pick-point",
      paint: { "circle-radius": 8, "circle-color": "#7c3aed", "circle-stroke-color": "#ffffff", "circle-stroke-width": 2 },
    });
  }
}

function removePickMarker(): void {
  const map = cs.hooks && cs.hooks.getMap();
  if (map && map.getLayer("pick-point")) {
    map.removeLayer("pick-point");
    map.removeSource("pick-point");
  }
}

function useGeolocation(): void {
  if (!navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    (pos: any) => {
      el("obs-lat").value = pos.coords.latitude.toFixed(5);
      el("obs-lon").value = pos.coords.longitude.toFixed(5);
      el("obs-unc").value = String(Math.max(1, Math.round(pos.coords.accuracy || 30)));
      updatePickMarker();
    },
    () => {
      el("obs-error").textContent = "Location permission was denied; pick the point on the map or type it.";
      el("obs-error").classList.remove("hidden");
    },
    { enableHighAccuracy: true, timeout: 15000 }
  );
}

// ---- files
function onFilesChosen(): void {
  const input = el("obs-photos");
  const cfg = cs.config;
  const chosen: any[] = Array.from(input.files || []);
  const problems: string[] = [];
  cs.files = [];
  for (const f of chosen) {
    const typeOk = ACCEPTED_TYPES.includes(f.type) || /\.(heic|heif)$/i.test(f.name);
    if (!typeOk) problems.push(`${f.name}: unsupported type`);
    else if (f.size > cfg.max_upload_bytes) problems.push(`${f.name}: larger than ${Math.round(cfg.max_upload_bytes / 1048576)} MB`);
    else if (cs.files.length >= cfg.max_media_per_observation) problems.push(`${f.name}: more than ${cfg.max_media_per_observation} photos`);
    else cs.files.push(f);
  }
  el("obs-file-list").innerHTML =
    cs.files.map((f: any) => `<li>${esc(f.name)} <small>${(f.size / 1048576).toFixed(1)} MB</small></li>`).join("") +
    problems.map((p) => `<li class="error">${esc(p)} (skipped)</li>`).join("");
}

// ---- Turnstile
function mountTurnstile(): void {
  const cfg = cs.config;
  const note = el("turnstile-note");
  if (!cfg.turnstile_site_key) {
    cs.turnstileToken = cfg.auth.dev_tokens_enabled ? "dev-local" : "";
    note.textContent = cfg.auth.dev_tokens_enabled ? "Turnstile is bypassed in local development." : "";
    return;
  }
  const render = () => {
    const ts = (window as any).turnstile;
    if (!ts || !el("turnstile-box")) return;
    cs.turnstileWidgetId = ts.render("#turnstile-box", {
      sitekey: cfg.turnstile_site_key,
      action: "submit-observation",
      callback: (token: string) => {
        cs.turnstileToken = token;
      },
      "expired-callback": () => {
        cs.turnstileToken = "";
      },
      "error-callback": () => {
        cs.turnstileToken = "";
        note.textContent = "Spam check failed to load. Check your connection and reload.";
      },
    });
  };
  if ((window as any).turnstile) {
    render();
    return;
  }
  const script = document.createElement("script");
  script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
  script.async = true;
  script.onload = render;
  script.onerror = () => {
    note.textContent = "Spam check could not load; submissions need it.";
  };
  document.head.appendChild(script);
}

function resetTurnstile(): void {
  const ts = (window as any).turnstile;
  if (ts && cs.turnstileWidgetId !== null) ts.reset(cs.turnstileWidgetId);
  if (cs.config.turnstile_site_key) cs.turnstileToken = "";
}

// ---- submit + uploads
function formError(msg: string): void {
  const box = el("obs-error");
  box.textContent = msg;
  box.classList.toggle("hidden", !msg);
}

async function submitObservation(): Promise<void> {
  formError("");
  const name = String(el("obs-name").value || "").trim();
  const date = String(el("obs-date").value || "");
  const latRaw = String(el("obs-lat").value || "");
  const lonRaw = String(el("obs-lon").value || "");
  if (!name) return formError("Enter a scientific name.");
  if (!date) return formError("Enter the date of the observation.");
  if (!latRaw || !lonRaw) return formError("Pick the location on the map or enter latitude and longitude.");
  if (!el("obs-accept").checked) return formError("Accept the dataset licence to submit.");
  if (!cs.turnstileToken && !cs.savedOccurrenceId) return formError("Please complete the spam check.");

  const license = (document.querySelector('input[name="obs-license"]:checked') as any).value;
  const taxon = selectedTaxon(name);
  const payload: any = {
    scientificName: name,
    eventDate: date,
    decimalLatitude: Number(latRaw),
    decimalLongitude: Number(lonRaw),
    coordinateUncertaintyInMeters: Number(el("obs-unc").value || 30),
    locality: String(el("obs-locality").value || "").trim() || null,
    notes: String(el("obs-notes").value || "").trim() || null,
    isSensitiveSpecies: Boolean(el("obs-sensitive").checked),
    license,
    licenseAccepted: true,
    rightsHolder: String(el("obs-rights").value || "").trim(),
  };
  if (taxon) {
    payload.taxonKey = taxon.key;
    payload.family = taxon.family || null;
  }

  const btn = el("btn-submit-obs");
  btn.disabled = true;
  btn.textContent = "Saving…";
  let res: any = null;
  try {
    res = await api("/api/observations", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "idempotency-key": cs.idempotencyKey,
        "x-turnstile-token": cs.turnstileToken,
      },
      body: JSON.stringify(payload),
    });
  } catch (_err) {
    btn.disabled = false;
    btn.textContent = "Retry submission";
    return formError(
      "Not saved: the connection failed. Your entries are kept; retrying sends the same submission and cannot create a duplicate."
    );
  }
  if (res.status !== 200 && res.status !== 201) {
    btn.disabled = false;
    btn.textContent = "Submit for review";
    resetTurnstile();
    return formError(`Not saved: ${errorText(res)}`);
  }
  const obs = res.body.observation;
  cs.savedOccurrenceId = obs.occurrence_id;
  el("obs-form").classList.add("hidden");
  cs.uploads = cs.files.map((f: any) => ({ file: f, status: "queued", progress: 0, error: "" }));
  renderResult(obs);
  await runUploads();
}

function renderResult(obs: any): void {
  const box = el("obs-result");
  box.classList.remove("hidden");
  const rows = cs.uploads
    .map((u: any, i: number) => {
      const pct = Math.round(u.progress * 100);
      const state =
        u.status === "done"
          ? `<span class="ok-text">Uploaded</span>`
          : u.status === "error"
            ? `<span class="error">${esc(u.error)}</span> <button class="btn btn-ghost btn-small" data-retry="${i}" type="button">Retry</button>`
            : u.status === "uploading"
              ? `${pct}%`
              : "Waiting";
      return `<li><span>${esc(u.file.name)}</span><progress max="100" value="${u.status === "done" ? 100 : u.status === "error" ? 0 : pct}"></progress><span>${state}</span></li>`;
    })
    .join("");
  const pending = cs.uploads.some((u: any) => u.status === "queued" || u.status === "uploading");
  const failed = cs.uploads.filter((u: any) => u.status === "error").length;
  box.innerHTML = `
    <div class="notice ok">
      <strong>Saved: awaiting review.</strong> Your record is stored with identifier <code>${esc(obs.occurrence_id)}</code>.
      It is <em>not yet published to GBIF</em>: a reviewer checks it first, then approved records are exported in the
      basinmap.org Darwin Core Archive and appear on GBIF only after the publisher's next ingestion.
      ${obs.is_sensitive_species ? "<br/>Public coordinates will be generalised to 0.1°." : ""}
    </div>
    <div>${statusBadges(obs)}</div>
    ${cs.uploads.length ? `<h3>Photos</h3><ul class="upload-list">${rows}</ul>` : ""}
    ${pending ? "" : failed ? `<p class="error">${failed} photo(s) failed. Retry them, or submit without them; the record is already saved.</p>` : ""}
    <div class="btn-row">
      <button id="btn-new-obs" class="btn btn-primary" type="button" ${pending ? "disabled" : ""}>Submit another</button>
      <button id="btn-view-mine" class="btn btn-ghost" type="button">My submissions</button>
    </div>`;
  box.querySelectorAll("[data-retry]").forEach((b: any) =>
    b.addEventListener("click", async () => {
      const idx = Number(b.getAttribute("data-retry"));
      cs.uploads[idx].status = "queued";
      cs.uploads[idx].error = "";
      renderResult(obs);
      await runUploads();
    })
  );
  el("btn-new-obs").addEventListener("click", resetSubmitPanel);
  el("btn-view-mine").addEventListener("click", () => showTab("mine"));
  cs.lastSavedObs = obs;
}

function uploadOne(u: any, onProgress: any): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/uploads/${encodeURIComponent(cs.savedOccurrenceId)}`);
    xhr.withCredentials = true;
    const headers = authHeaders();
    for (const k of Object.keys(headers)) xhr.setRequestHeader(k, headers[k]);
    xhr.setRequestHeader("content-type", u.file.type || "application/octet-stream");
    xhr.upload.onprogress = (ev: any) => {
      if (ev.lengthComputable) onProgress(ev.loaded / ev.total);
    };
    xhr.onload = () => {
      if (xhr.status === 201) return resolve();
      let msg = `HTTP ${xhr.status}`;
      try {
        msg = JSON.parse(xhr.responseText).error || msg;
      } catch (_err) {
        // keep HTTP status text
      }
      reject(new Error(xhr.status === 415 ? "Not a supported image file" : xhr.status === 413 ? "File too large" : msg));
    };
    xhr.onerror = () => reject(new Error("Connection failed"));
    xhr.send(u.file); // Blob body: the browser sets Content-Length for the Worker's streaming put
  });
}

async function runUploads(): Promise<void> {
  for (const u of cs.uploads) {
    if (u.status !== "queued") continue;
    u.status = "uploading";
    u.progress = 0;
    renderResult(cs.lastSavedObs);
    try {
      await uploadOne(u, (f: number) => {
        u.progress = f;
        const bars = el("obs-result").querySelectorAll("progress");
        const idx = cs.uploads.indexOf(u);
        if (bars[idx]) bars[idx].value = Math.round(f * 100);
      });
      u.status = "done";
    } catch (err: any) {
      u.status = "error";
      u.error = err && err.message ? err.message : "Upload failed";
    }
    renderResult(cs.lastSavedObs);
  }
}

// ---------------------------------------------------------------------------
// My submissions
// ---------------------------------------------------------------------------

async function loadMine(): Promise<void> {
  const panel = el("panel-mine");
  if (!cs.user) {
    panel.innerHTML = signInPrompt();
    return;
  }
  panel.innerHTML = `<p class="muted-note">Loading your submissions…</p>`;
  let res: any = null;
  try {
    res = await api("/api/observations/mine", { method: "GET" });
  } catch (_err) {
    panel.innerHTML = `<div class="notice warn">Could not reach the submission service.</div>`;
    return;
  }
  if (res.status !== 200) {
    panel.innerHTML = `<div class="notice warn">${esc(errorText(res))}</div>`;
    return;
  }
  const items: any[] = res.body.observations || [];
  if (!items.length) {
    panel.innerHTML = `<p class="muted-note">No submissions yet.</p>`;
    return;
  }
  panel.innerHTML = `<ul class="obs-list">${items
    .map(
      (o: any) => `
      <li class="obs-card">
        <div><strong><em>${esc(o.scientific_name)}</em></strong> · ${esc(o.event_date)} · ${esc(o.media_count)} photo(s)</div>
        <div>${statusBadges(o)}</div>
        ${o.review_status === "needs_changes" && o.verbatim && o.verbatim.review_notes ? `<div class="notice warn">Reviewer: ${esc(o.verbatim.review_notes)}</div>` : ""}
        ${o.review_status !== "approved" ? `<button class="btn btn-ghost btn-small" data-edit="${esc(o.occurrence_id)}" type="button">Edit</button>` : ""}
        <div class="edit-slot" data-slot="${esc(o.occurrence_id)}"></div>
      </li>`
    )
    .join("")}</ul>`;
  panel.querySelectorAll("[data-edit]").forEach((b: any) =>
    b.addEventListener("click", () => {
      const id = b.getAttribute("data-edit");
      const obs = items.find((o: any) => o.occurrence_id === id);
      const slot = panel.querySelector(`[data-slot="${CSS.escape(id)}"]`);
      renderEditForm(slot, obs, false, () => loadMine());
    })
  );
}

// ---------------------------------------------------------------------------
// Shared correction form (contributor edit or reviewer correction -> PATCH)
// ---------------------------------------------------------------------------

function renderEditForm(slot: any, obs: any, asReviewer: boolean, onSaved: any): void {
  const v = obs.verbatim || {};
  slot.innerHTML = `
    <form class="edit-form" novalidate>
      <label>Scientific name<input name="scientificName" maxlength="200" value="${esc(obs.scientific_name)}" /></label>
      <label>Date<input name="eventDate" type="date" value="${esc(String(obs.event_date || "").slice(0, 10))}" /></label>
      <div class="coord-pair">
        <label>Latitude<input name="decimalLatitude" type="number" step="0.00001" value="${esc(v.latitude)}" /></label>
        <label>Longitude<input name="decimalLongitude" type="number" step="0.00001" value="${esc(v.longitude)}" /></label>
      </div>
      <label>Uncertainty (m)<input name="coordinateUncertaintyInMeters" type="number" min="1" max="100000" value="${esc(v.coordinate_uncertainty_m || 30)}" /></label>
      <label>Locality<input name="locality" maxlength="1000" value="${esc(obs.locality || "")}" /></label>
      <label class="check"><input name="isSensitiveSpecies" type="checkbox" ${obs.is_sensitive_species ? "checked" : ""}/> Sensitive location (generalise public coordinates)</label>
      <label>Reason for change<input name="notes" maxlength="4000" placeholder="${asReviewer ? "e.g. Re-identified from photo 2" : "e.g. Corrected location"}" /></label>
      <div class="error edit-error" role="alert"></div>
      <button class="btn btn-primary btn-small" type="submit">${asReviewer ? "Save correction" : "Save and resubmit"}</button>
    </form>`;
  const form = slot.querySelector("form");
  form.addEventListener("submit", async (e: any) => {
    e.preventDefault();
    const fd = new FormData(form);
    const body: any = {
      scientificName: String(fd.get("scientificName") || "").trim(),
      eventDate: String(fd.get("eventDate") || ""),
      decimalLatitude: Number(fd.get("decimalLatitude")),
      decimalLongitude: Number(fd.get("decimalLongitude")),
      coordinateUncertaintyInMeters: Number(fd.get("coordinateUncertaintyInMeters") || 30),
      locality: String(fd.get("locality") || "").trim() || null,
      isSensitiveSpecies: fd.get("isSensitiveSpecies") === "on",
    };
    const notes = String(fd.get("notes") || "").trim();
    if (notes) body.notes = notes;
    const errBox = slot.querySelector(".edit-error");
    errBox.textContent = "Saving…";
    try {
      const res = await api(`/api/observations/${encodeURIComponent(obs.occurrence_id)}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.status !== 200) {
        errBox.textContent = `Not saved: ${errorText(res)}`;
        return;
      }
      errBox.textContent = "";
      onSaved();
    } catch (_err) {
      errBox.textContent = "Not saved: connection failed.";
    }
  });
}

// ---------------------------------------------------------------------------
// Reviewer queue
// ---------------------------------------------------------------------------

async function loadQueue(): Promise<void> {
  const panel = el("panel-review");
  if (!cs.user || (cs.user.role !== "reviewer" && cs.user.role !== "admin")) {
    panel.innerHTML = `<div class="notice warn">The reviewer queue is available to reviewers and administrators.</div>`;
    return;
  }
  panel.innerHTML = `
    <div class="queue-toolbar">
      <label>Status
        <select id="queue-status">
          <option value="submitted">Awaiting review</option>
          <option value="needs_changes">Changes requested</option>
          <option value="approved">Approved</option>
        </select>
      </label>
      <button id="queue-refresh" class="btn btn-ghost btn-small" type="button">Refresh</button>
    </div>
    <div id="queue-body"><p class="muted-note">Loading queue…</p></div>`;
  el("queue-status").value = cs.queueStatus;
  el("queue-status").addEventListener("change", () => {
    cs.queueStatus = el("queue-status").value;
    cs.selectedReviewId = "";
    loadQueue();
  });
  el("queue-refresh").addEventListener("click", loadQueue);
  let res: any = null;
  try {
    res = await api(`/api/review/pending?status=${encodeURIComponent(cs.queueStatus)}&limit=50`, { method: "GET" });
  } catch (_err) {
    el("queue-body").innerHTML = `<div class="notice warn">Could not reach the review service.</div>`;
    return;
  }
  if (res.status !== 200) {
    el("queue-body").innerHTML = `<div class="notice warn">${esc(errorText(res))}</div>`;
    return;
  }
  cs.queue = res.body.observations || [];
  renderQueue();
}

function renderQueue(): void {
  const body = el("queue-body");
  if (!cs.queue.length) {
    body.innerHTML = `<p class="muted-note">Nothing in this queue.</p>`;
    clearCompareLayer();
    return;
  }
  body.innerHTML = `<ul class="obs-list">${cs.queue
    .map((o: any) => {
      const flags = (o.media || []).filter((m: any) => m.exif_mismatch).length;
      return `<li class="obs-card selectable ${o.occurrence_id === cs.selectedReviewId ? "selected" : ""}" data-review="${esc(o.occurrence_id)}" tabindex="0">
        <div><strong><em>${esc(o.scientific_name)}</em></strong> · ${esc(o.event_date)}</div>
        <div class="muted-note">${esc(o.submitter && o.submitter.display_name)} · ${esc((o.media || []).length)} photo(s)
          ${flags ? `<span class="status-chip flag">${flags} GPS mismatch</span>` : ""}</div>
      </li>
      ${o.occurrence_id === cs.selectedReviewId ? `<li class="review-detail" id="review-detail"></li>` : ""}`;
    })
    .join("")}</ul>`;
  body.querySelectorAll("[data-review]").forEach((card: any) => {
    const open = () => {
      const id = card.getAttribute("data-review");
      cs.selectedReviewId = cs.selectedReviewId === id ? "" : id;
      renderQueue();
    };
    card.addEventListener("click", open);
    card.addEventListener("keydown", (e: any) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        open();
      }
    });
  });
  const selected = cs.queue.find((o: any) => o.occurrence_id === cs.selectedReviewId);
  if (selected) renderReviewDetail(selected);
  else clearCompareLayer();
}

function fmtCoord(v: unknown): string {
  return v === null || v === undefined ? "—" : Number(v).toFixed(5);
}

function exifDate(raw: string): string {
  const m = /^(\d{4}):(\d{2}):(\d{2})/.exec(raw || "");
  return m ? `${m[1]}-${m[2]}-${m[3]}` : "";
}

function renderReviewDetail(o: any): void {
  const box = el("review-detail");
  const v = o.verbatim || {};
  const submitterOrcid = o.submitter && o.submitter.orcid_id;
  const mediaRows = (o.media || [])
    .map((m: any, i: number) => {
      const exDay = exifDate(m.exif_datetime);
      const dateFlag = exDay && exDay !== String(o.event_date).slice(0, 10);
      const gps =
        m.exif_gps_latitude === null || m.exif_gps_latitude === undefined
          ? `<span class="muted-note">No EXIF GPS</span>`
          : `${fmtCoord(m.exif_gps_latitude)}, ${fmtCoord(m.exif_gps_longitude)} · <strong>${esc(m.exif_offset_m)} m</strong> from submitted point
             ${m.exif_mismatch ? `<span class="status-chip flag">beyond tolerance</span>` : `<span class="status-chip ok">consistent</span>`}`;
      return `<li class="media-row">
        <div class="thumb" data-media="${esc(m.media_id)}" data-mime="${esc(m.detected_mime_type)}">Photo ${i + 1}</div>
        <div class="media-meta">
          <div>${esc(m.detected_mime_type)} · ${(Number(m.byte_length) / 1048576).toFixed(2)} MB · ${esc(m.derivative_status)}</div>
          <div>EXIF GPS: ${gps}</div>
          <div>EXIF time: ${esc(m.exif_datetime || "—")} ${dateFlag ? `<span class="status-chip flag">date differs from ${esc(o.event_date)}</span>` : ""}</div>
        </div>
      </li>`;
    })
    .join("");
  box.innerHTML = `
    <div class="review-grid">
      <div><span class="k">occurrenceID</span><code>${esc(o.occurrence_id)}</code></div>
      <div><span class="k">Submitted by</span>${esc(o.submitter && o.submitter.display_name)}
        ${submitterOrcid ? `<a href="${esc(submitterOrcid)}" target="_blank" rel="noopener noreferrer">${esc(submitterOrcid)}</a>` : ""}</div>
      <div><span class="k">Verbatim name</span><em>${esc(v.scientific_name)}</em></div>
      <div><span class="k">Verbatim position</span>${fmtCoord(v.latitude)}, ${fmtCoord(v.longitude)} ± ${esc(v.coordinate_uncertainty_m)} m</div>
      <div><span class="k">Public position</span>${fmtCoord(o.public_latitude)}, ${fmtCoord(o.public_longitude)}${o.is_sensitive_species ? " (generalised)" : ""}</div>
      <div><span class="k">Date</span>${esc(o.event_date)}</div>
      <div><span class="k">Locality</span>${esc(v.locality || "—")}</div>
      <div><span class="k">Notes</span>${esc(v.notes || "—")}</div>
      <div><span class="k">Licence</span>${esc(o.license)} · ${esc(o.rights_holder)}</div>
      <div><span class="k">Revision</span>${esc(o.revision_number)} · ${statusBadges(o)}</div>
    </div>
    <h3>Evidence</h3>
    ${mediaRows ? `<ul class="media-list">${mediaRows}</ul>` : `<p class="muted-note">No photos attached.</p>`}
    <p class="muted-note">On the map: <span class="legend-dot submitted"></span> submitted point, <span class="legend-dot exif"></span> photo EXIF positions.</p>
    <details>
      <summary>Correct this record</summary>
      <div id="review-edit-slot"></div>
    </details>
    <label for="review-notes">Review notes (sent to the contributor)</label>
    <textarea id="review-notes" rows="2" maxlength="4000"></textarea>
    <div id="review-error" class="error" role="alert"></div>
    <div class="btn-row">
      <button id="btn-approve" class="btn btn-primary" type="button">Approve</button>
      <button id="btn-needs-changes" class="btn btn-warn" type="button">Request changes</button>
    </div>
    <p class="muted-note">Approval publishes EXIF-stripped photos and adds the record to the public provisional overlay (refreshes within about a minute). GBIF publication happens later through the Darwin Core Archive export.</p>`;

  renderEditForm(el("review-edit-slot"), o, true, async () => {
    await loadQueue();
  });
  el("btn-approve").addEventListener("click", () => decide(o, "approve"));
  el("btn-needs-changes").addEventListener("click", () => decide(o, "needs_changes"));
  loadThumbnails(box);
  showCompareLayer(o);
}

async function loadThumbnails(box: any): Promise<void> {
  for (const url of cs.objectUrls) URL.revokeObjectURL(url);
  cs.objectUrls = [];
  const thumbs = box.querySelectorAll("[data-media]");
  for (const t of thumbs) {
    const id = t.getAttribute("data-media");
    try {
      const resp = await fetch(`/api/media/${encodeURIComponent(id)}`, { credentials: "same-origin", headers: authHeaders() });
      if (!resp.ok) {
        t.textContent = `Unavailable (${resp.status})`;
        continue;
      }
      const blobUrl = URL.createObjectURL(await resp.blob());
      cs.objectUrls.push(blobUrl);
      const mime = t.getAttribute("data-mime");
      t.innerHTML = `<a href="${blobUrl}" target="_blank" rel="noopener">${
        mime === "image/heic" ? "Open HEIC original" : `<img src="${blobUrl}" alt="Submitted evidence photo" loading="lazy" />`
      }</a>`;
    } catch (_err) {
      t.textContent = "Could not load photo";
    }
  }
}

function showCompareLayer(o: any): void {
  const map = cs.hooks.getMap();
  if (!map) return;
  const v = o.verbatim || {};
  const sub = [Number(v.longitude), Number(v.latitude)];
  const features: any[] = [{ type: "Feature", properties: { kind: "submitted" }, geometry: { type: "Point", coordinates: sub } }];
  for (const m of o.media || []) {
    if (m.exif_gps_latitude === null || m.exif_gps_latitude === undefined) continue;
    const p = [Number(m.exif_gps_longitude), Number(m.exif_gps_latitude)];
    features.push({ type: "Feature", properties: { kind: "exif" }, geometry: { type: "Point", coordinates: p } });
    features.push({ type: "Feature", properties: { kind: "link" }, geometry: { type: "LineString", coordinates: [sub, p] } });
  }
  const data = { type: "FeatureCollection", features };
  if (map.getSource("review-compare")) {
    map.getSource("review-compare").setData(data);
  } else {
    map.addSource("review-compare", { type: "geojson", data });
    map.addLayer({
      id: "review-compare-link",
      type: "line",
      source: "review-compare",
      filter: ["==", ["get", "kind"], "link"],
      paint: { "line-color": "#7c3aed", "line-width": 2, "line-dasharray": [2, 2] },
    });
    map.addLayer({
      id: "review-compare-points",
      type: "circle",
      source: "review-compare",
      filter: ["!=", ["get", "kind"], "link"],
      paint: {
        "circle-radius": 7,
        "circle-color": ["match", ["get", "kind"], "submitted", "#d97706", "#7c3aed"],
        "circle-stroke-color": "#ffffff",
        "circle-stroke-width": 2,
      },
    });
  }
  const lons = features.filter((f: any) => f.geometry.type === "Point").map((f: any) => f.geometry.coordinates[0]);
  const lats = features.filter((f: any) => f.geometry.type === "Point").map((f: any) => f.geometry.coordinates[1]);
  map.fitBounds(
    [
      [Math.min(...lons) - 0.01, Math.min(...lats) - 0.01],
      [Math.max(...lons) + 0.01, Math.max(...lats) + 0.01],
    ],
    { padding: 60, maxZoom: 13, duration: 600 }
  );
}

function clearCompareLayer(): void {
  const map = cs.hooks && cs.hooks.getMap();
  if (!map || !map.getSource("review-compare")) return;
  map.removeLayer("review-compare-points");
  map.removeLayer("review-compare-link");
  map.removeSource("review-compare");
}

async function decide(o: any, decision: string): Promise<void> {
  const notes = String(el("review-notes").value || "").trim();
  const errBox = el("review-error");
  if (decision === "needs_changes" && !notes) {
    errBox.textContent = "Explain what the contributor should change.";
    return;
  }
  errBox.textContent = "Saving…";
  el("btn-approve").disabled = true;
  el("btn-needs-changes").disabled = true;
  try {
    const res = await api(`/api/review/${encodeURIComponent(o.occurrence_id)}/${decision}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ notes }),
    });
    if (res.status !== 200) {
      errBox.textContent = `Not saved: ${errorText(res)}`;
      el("btn-approve").disabled = false;
      el("btn-needs-changes").disabled = false;
      return;
    }
    const skipped = (res.body.skipped_media_ids || []).length;
    cs.selectedReviewId = "";
    await loadQueue();
    const body = el("queue-body");
    if (body) {
      body.insertAdjacentHTML(
        "afterbegin",
        `<div class="notice ok">${decision === "approve" ? "Approved" : "Returned to contributor"}: <em>${esc(o.scientific_name)}</em>.
          ${skipped ? `${skipped} non-JPEG photo(s) kept private (image conversion not configured).` : ""}</div>`
      );
    }
    if (cs.hooks.onReviewDecision) cs.hooks.onReviewDecision();
  } catch (_err) {
    errBox.textContent = "Not saved: connection failed.";
    el("btn-approve").disabled = false;
    el("btn-needs-changes").disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

export async function initContributions(hooks: ContribHooks, apiOnline: boolean): Promise<void> {
  cs.hooks = hooks;
  cs.apiOnline = apiOnline;

  const params = new URLSearchParams(location.search);
  const authError = params.get("auth_error");
  if (authError) {
    params.delete("auth_error");
    history.replaceState(null, "", location.pathname + (params.toString() ? `?${params}` : "") + location.hash);
    const banner = el("auth-banner");
    if (banner) {
      banner.textContent = `ORCID sign-in did not complete (${authError}). Please try again.`;
      banner.classList.remove("hidden");
    }
  }

  el("drawer-close").addEventListener("click", closeDrawer);
  for (const name of ["submit", "mine", "review"]) {
    el(`tab-${name}`).addEventListener("click", () => showTab(name));
  }
  window.addEventListener("resize", placeDrawer);
  document.addEventListener("keydown", (e: any) => {
    if (e.key === "Escape" && cs.picking) stopPicking();
  });

  if (!apiOnline) {
    renderAuthArea();
    return;
  }
  try {
    const res = await api("/api/config", { method: "GET" });
    cs.config = res.status === 200 ? res.body : null;
  } catch (_err) {
    cs.config = null;
  }
  if (!cs.config) {
    cs.apiOnline = false;
    renderAuthArea();
    return;
  }
  await refreshUser();
}

/** Called by app.ts on every MapLibre map click before layer handlers run. */
export function handleMapClickForContrib(e: any): boolean {
  if (!cs.picking) return false;
  onMapPick(e);
  return true;
}
