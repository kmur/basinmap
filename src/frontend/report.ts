// Stage 10 pure helpers: zoom-adaptive HydroBASINS levels, choropleth metrics and legends,
// BasinATLAS display factors, the Basin Report (climate chart, habitat indicators, checklist),
// the country/continent fauna browser, and PNG/PDF map export. No DOM access here, so everything
// is unit-tested in tests/report.test.mts.

// ---------------------------------------------------------------------------------------------
// BasinATLAS display factors
// ---------------------------------------------------------------------------------------------

/**
 * Storage factors confirmed after the v0.4.0/v0.5.0 builds (observed maxima 1000 / 492 / 9734).
 * Releases built before Stage 10 wrote these with scale null and unscaled values; releases built
 * after it divide in the pipeline and record the factor, so the browser divides only when the
 * manifest says the factor was unknown.
 */
export const KNOWN_ENV_SCALES: Record<string, number> = { lake_pc: 10, human_footprint: 10, regulation_pc: 10 };

export function envDivisor(manifest: any, key: string): number {
  const spec = manifest && manifest.environment && manifest.environment.scalars ? manifest.environment.scalars[key] : null;
  if (spec && spec.scale == null && KNOWN_ENV_SCALES[key]) return KNOWN_ENV_SCALES[key];
  return 1;
}

export function envValue(manifest: any, key: string, raw: any): number | null {
  if (raw === null || raw === undefined || raw === "") return null;
  const n = Number(raw);
  if (!Number.isFinite(n)) return null;
  return Math.round((n / envDivisor(manifest, key)) * 100) / 100;
}

// ---------------------------------------------------------------------------------------------
// Choropleth metrics
// ---------------------------------------------------------------------------------------------

export interface MetricDef {
  id: string; // vector-tile property
  envKey: string; // BasinATLAS scalar key ("" for record counts)
  label: string;
  unit: string;
  kind: string; // "count" (log ramp from 0) or "value" (linear ramp over the values in view)
  ramp: string[];
  group: string;
}

const GREENS = ["#d8f3dc", "#95d5b2", "#52b788", "#1b4332"];
const REDS = ["#fde2e4", "#f4978e", "#e5383b", "#7f1d1d"];

export const METRICS: MetricDef[] = [
  { id: "occurrence_count", envKey: "", label: "Total occurrences", unit: "records", kind: "count", ramp: GREENS, group: "GBIF records" },
  { id: "species_count", envKey: "", label: "Species richness", unit: "species", kind: "count", ramp: GREENS, group: "GBIF records" },
  { id: "threatened_count", envKey: "", label: "Threatened-species records (CR/EN/VU)", unit: "records", kind: "count", ramp: REDS, group: "GBIF records" },
  { id: "env_air_temp_c", envKey: "air_temp_c", label: "Air temperature, annual mean", unit: "°C", kind: "value", ramp: ["#2c7bb6", "#abd9e9", "#fdae61", "#d7191c"], group: "BasinATLAS environment" },
  { id: "env_precip_mm", envKey: "precip_mm", label: "Precipitation, annual", unit: "mm", kind: "value", ramp: ["#f7fbff", "#9ecae1", "#4292c6", "#08306b"], group: "BasinATLAS environment" },
  { id: "env_elevation_m", envKey: "elevation_m", label: "Elevation, mean", unit: "m", kind: "value", ramp: ["#e5f5e0", "#c7b98a", "#8c6d31", "#4a3311"], group: "BasinATLAS environment" },
  { id: "env_forest_pc", envKey: "forest_pc", label: "Forest cover", unit: "%", kind: "value", ramp: GREENS, group: "BasinATLAS environment" },
  { id: "env_lake_pc", envKey: "lake_pc", label: "Lake area (limnicity)", unit: "%", kind: "value", ramp: ["#eff3ff", "#9ecae1", "#3182bd", "#08519c"], group: "BasinATLAS environment" },
  { id: "env_protected_pc", envKey: "protected_pc", label: "Protected area", unit: "%", kind: "value", ramp: ["#f2f0f7", "#bcbddc", "#807dba", "#4a1486"], group: "BasinATLAS environment" },
  { id: "env_human_footprint", envKey: "human_footprint", label: "Human footprint (0–50)", unit: "index", kind: "value", ramp: ["#ffffcc", "#fd8d3c", "#e31a1c", "#800026"], group: "BasinATLAS environment" },
];

export function metricById(id: string): MetricDef {
  return METRICS.find((m) => m.id === id) || METRICS[0];
}

/** Metrics this release can show (environment metrics need BasinATLAS values in the tiles). */
export function availableMetrics(manifest: any): MetricDef[] {
  const hasEnv = Boolean(manifest && manifest.environment && manifest.environment.scalars);
  return METRICS.filter((m) => !m.envKey || (hasEnv && manifest.environment.scalars[m.envKey]));
}

/** Colour domain for the values in view: counts from 0 to the maximum, others from the 2nd to 98th percentile. */
export function metricDomain(values: number[], kind: string): number[] {
  const vals = values.filter((v) => Number.isFinite(v));
  if (!vals.length) return kind === "count" ? [0, 10] : [0, 1];
  vals.sort((a, b) => a - b);
  if (kind === "count") return [0, Math.max(1, vals[vals.length - 1])];
  const pick = (q: number) => vals[Math.min(vals.length - 1, Math.max(0, Math.round(q * (vals.length - 1))))];
  let lo = pick(0.02);
  let hi = pick(0.98);
  if (hi <= lo) hi = lo + 1;
  return [lo, hi];
}

const NO_DATA = "#e5e7eb";

/** MapLibre fill-color expression; ``divisor`` corrects BasinATLAS values stored without their factor. */
export function choroplethExpression(metric: MetricDef, domain: number[], divisor: number): any {
  const raw: any = ["to-number", ["get", metric.id], 0];
  const val: any = divisor && divisor !== 1 ? ["/", raw, divisor] : raw;
  const r = metric.ramp;
  let ramp: any = null;
  if (metric.kind === "count") {
    const top = Math.log(1 + Math.max(1, domain[1]));
    ramp = ["interpolate", ["linear"], ["ln", ["+", 1, ["max", 0, val]]], 0, r[0], top * 0.4, r[1], top * 0.7, r[2], top, r[3]];
  } else {
    const lo = domain[0];
    const hi = domain[1] > lo ? domain[1] : lo + 1;
    const step = (hi - lo) / 3;
    ramp = ["interpolate", ["linear"], val, lo, r[0], lo + step, r[1], lo + 2 * step, r[2], hi, r[3]];
  }
  if (metric.kind === "count") return ramp;
  return ["case", ["has", metric.id], ramp, NO_DATA];
}

export function formatNumber(v: number): string {
  if (!Number.isFinite(v)) return "–";
  const a = Math.abs(v);
  if (a >= 10000) return Math.round(v).toLocaleString("en-US");
  if (a >= 100) return String(Math.round(v));
  if (a >= 10) return String(Math.round(v * 10) / 10);
  return String(Math.round(v * 100) / 100);
}

/** Four legend stops matching ``choroplethExpression``. */
export function legendStops(metric: MetricDef, domain: number[]): any[] {
  const r = metric.ramp;
  if (metric.kind === "count") {
    const top = Math.log(1 + Math.max(1, domain[1]));
    return [0, 0.4, 0.7, 1].map((f, i) => ({ color: r[i], label: formatNumber(Math.round(Math.exp(top * f) - 1)) }));
  }
  const lo = domain[0];
  const hi = domain[1] > lo ? domain[1] : lo + 1;
  return [0, 1, 2, 3].map((i) => ({ color: r[i], label: formatNumber(lo + ((hi - lo) * i) / 3) }));
}

// ---------------------------------------------------------------------------------------------
// Zoom-adaptive HydroBASINS levels (3 -> 6 -> 8)
// ---------------------------------------------------------------------------------------------

export interface BasinLevel {
  key: string; // "03", "06", "08"
  level: number;
  primary: boolean;
  minZoom: number;
  maxZoom: number;
  sourceLayer: string;
  pmtiles: any[]; // partitions with path/min_zoom/max_zoom/bounds (primary level: from layers.basins)
}

export function basinLevels(manifest: any): BasinLevel[] {
  const layers = (manifest && manifest.layers) || {};
  const out: BasinLevel[] = [];
  const levels = layers.basin_levels || {};
  for (const key of Object.keys(levels)) {
    const l = levels[key];
    if (!l || typeof l.level !== "number") continue;
    if (l.primary) continue;
    if (!Array.isArray(l.pmtiles) || !l.pmtiles.length) continue;
    out.push({ key, level: l.level, primary: false, minZoom: Number(l.min_zoom ?? 0), maxZoom: Number(l.max_zoom ?? 14),
      sourceLayer: l.source_layer || "basins", pmtiles: l.pmtiles });
  }
  const primaryEntry: any = Object.keys(levels).map((k) => levels[k]).find((l: any) => l && l.primary);
  const primaryLevel = primaryEntry ? primaryEntry.level : 6;
  const basins = layers.basins;
  if (basins) {
    const parts = Array.isArray(basins.partitions) && basins.partitions.length ? basins.partitions : [basins];
    out.push({ key: String(primaryLevel).padStart(2, "0"), level: primaryLevel, primary: true,
      minZoom: Number(basins.min_zoom ?? 0), maxZoom: Number(basins.max_zoom ?? 14), sourceLayer: basins.source_layer || "basins",
      pmtiles: parts });
  }
  out.sort((a, b) => a.level - b.level);
  return out;
}

/** Preferred switch zoom between a coarser and a finer level (major basins to z4, sub-catchments from z7). */
function preferredBreak(finer: BasinLevel): number {
  return finer.level <= 6 ? 4 : 7;
}

/**
 * Zoom range for each level in "auto" mode, or for the one chosen level (shown at every zoom; MapLibre
 * over-zooms coarse tiles, and a finer level only appears from its first tile zoom).
 */
export function levelZoomRanges(levels: BasinLevel[], mode: string): Record<string, number[]> {
  const out: Record<string, number[]> = {};
  if (!levels.length) return out;
  if (mode !== "auto") {
    for (const l of levels) out[l.key] = l.key === mode ? [Math.max(0, l.minZoom), 24] : [0, 0];
    return out;
  }
  let start = 0;
  for (let i = 0; i < levels.length; i++) {
    const next = levels[i + 1];
    const end = next ? Math.max(preferredBreak(next), next.minZoom) : 24;
    out[levels[i].key] = [start, Math.max(start, end)];
    start = Math.max(start, end);
  }
  return out;
}

export function levelAtZoom(ranges: Record<string, number[]>, zoom: number): string {
  for (const key of Object.keys(ranges)) {
    const r = ranges[key];
    if (r[1] > r[0] && zoom >= r[0] && zoom < r[1]) return key;
  }
  return "";
}

export function levelLabel(level: number): string {
  if (level <= 3) return `Level ${level}: major river basins`;
  if (level <= 6) return `Level ${level}: sub-basins`;
  return `Level ${level}: small catchments`;
}

/** Shard lookup: Level 8 shards group every catchment of one Level 6 parent under ``basins``. */
export function shardEntry(doc: any, hybasId: string): any {
  if (doc && doc.basins && typeof doc.basins === "object" && !doc.species) return doc.basins[String(hybasId)] || null;
  return doc || null;
}

// ---------------------------------------------------------------------------------------------
// Basin Report: climate chart, habitat indicators, checklist
// ---------------------------------------------------------------------------------------------

export const MONTH_LETTERS = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"];

function esc(s: any): string {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" } as any)[c]);
}

function niceStep(range: number, ticks: number): number {
  const raw = range / Math.max(1, ticks);
  const mag = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-9))));
  const f = raw / mag;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * mag;
}

/**
 * Climograph: monthly precipitation bars (left axis, mm) and mean air temperature line (right axis, °C)
 * for the whole sub-basin. Returns an SVG string, or "" when the release has no monthly values.
 */
export function climateChartSvg(monthly: any, width: number, height: number): string {
  const temp: any[] = monthly && Array.isArray(monthly.air_temp_c) ? monthly.air_temp_c : [];
  const prec: any[] = monthly && Array.isArray(monthly.precip_mm) ? monthly.precip_mm : [];
  if (temp.length !== 12 && prec.length !== 12) return "";
  const w = width || 340;
  const h = height || 190;
  const m = { l: 40, r: 38, t: 14, b: 26 };
  const pw = w - m.l - m.r;
  const ph = h - m.t - m.b;
  const pVals = prec.length === 12 ? prec.map((v: any) => Math.max(0, Number(v) || 0)) : [];
  const tVals = temp.length === 12 ? temp.map((v: any) => Number(v)) : [];
  const pStep = niceStep(Math.max(10, ...pVals), 4);
  const pMax = pStep * Math.max(1, Math.ceil(Math.max(10, ...pVals) / pStep));
  const tLoRaw = tVals.length ? Math.min(0, ...tVals) : 0;
  const tHiRaw = tVals.length ? Math.max(...tVals) : 30;
  const tStep = niceStep(Math.max(5, tHiRaw - tLoRaw), 4);
  const tLo = Math.floor(tLoRaw / tStep) * tStep;
  const tHi = Math.max(tLo + tStep, Math.ceil(tHiRaw / tStep) * tStep);
  const bw = pw / 12;
  const x = (i: number) => m.l + bw * i + bw / 2;
  const yP = (v: number) => m.t + ph - (v / pMax) * ph;
  const yT = (v: number) => m.t + ph - ((v - tLo) / (tHi - tLo)) * ph;
  const parts: string[] = [];
  parts.push(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" class="climate-chart">`);
  const total = pVals.reduce((a: number, b: number) => a + b, 0);
  const meanT = tVals.length ? tVals.reduce((a: number, b: number) => a + b, 0) / 12 : NaN;
  parts.push(`<title>Monthly climate: precipitation ${Math.round(total)} mm per year${tVals.length ? `, mean air temperature ${formatNumber(meanT)} °C` : ""}</title>`);
  for (let v = 0; v <= pMax + 1e-9; v += pStep) {
    const y = yP(v).toFixed(1);
    parts.push(`<line x1="${m.l}" x2="${m.l + pw}" y1="${y}" y2="${y}" stroke="#e2e8f0" stroke-width="1"/>`);
    if (pVals.length) parts.push(`<text x="${m.l - 4}" y="${y}" dy="3" text-anchor="end" font-size="9" fill="#2563eb">${formatNumber(v)}</text>`);
  }
  pVals.forEach((v: number, i: number) => {
    const y = yP(v);
    parts.push(`<rect x="${(m.l + bw * i + bw * 0.15).toFixed(1)}" y="${y.toFixed(1)}" width="${(bw * 0.7).toFixed(1)}" height="${(m.t + ph - y).toFixed(1)}" fill="#93c5fd"><title>${MONTH_LETTERS[i]}: ${formatNumber(v)} mm</title></rect>`);
  });
  if (tVals.length) {
    for (let v = tLo; v <= tHi + 1e-9; v += tStep) {
      parts.push(`<text x="${m.l + pw + 4}" y="${yT(v).toFixed(1)}" dy="3" font-size="9" fill="#dc2626">${formatNumber(v)}</text>`);
    }
    if (tLo < 0) parts.push(`<line x1="${m.l}" x2="${m.l + pw}" y1="${yT(0).toFixed(1)}" y2="${yT(0).toFixed(1)}" stroke="#fca5a5" stroke-dasharray="3 3"/>`);
    const pts = tVals.map((v: number, i: number) => `${x(i).toFixed(1)},${yT(v).toFixed(1)}`).join(" ");
    parts.push(`<polyline points="${pts}" fill="none" stroke="#dc2626" stroke-width="2"/>`);
    tVals.forEach((v: number, i: number) => parts.push(`<circle cx="${x(i).toFixed(1)}" cy="${yT(v).toFixed(1)}" r="2.6" fill="#dc2626"><title>${MONTH_LETTERS[i]}: ${formatNumber(v)} °C</title></circle>`));
  }
  MONTH_LETTERS.forEach((l, i) => parts.push(`<text x="${x(i).toFixed(1)}" y="${h - m.b + 14}" text-anchor="middle" font-size="9" fill="#475569">${l}</text>`));
  parts.push(`<text x="4" y="${m.t - 4}" font-size="9" fill="#2563eb">mm</text>`);
  if (tVals.length) parts.push(`<text x="${w - 4}" y="${m.t - 4}" text-anchor="end" font-size="9" fill="#dc2626">°C</text>`);
  parts.push("</svg>");
  return parts.join("");
}

export interface Indicator {
  key: string;
  label: string;
  value: number | null;
  display: string;
  fraction: number; // 0..1 for the bar
  note: string;
}

/** The five habitat indicators of the Basin Report (whole sub-basin values from BasinATLAS). */
export const HABITAT_INDICATORS = [
  { key: "forest_pc", label: "Forest cover", unit: "%", max: 100, note: "GLC2000 tree cover classes" },
  { key: "wetland_pc", label: "Wetland extent", unit: "%", max: 100, note: "GLWD wetland class group 1" },
  { key: "lake_pc", label: "Lake area (limnicity)", unit: "%", max: 100, note: "HydroLAKES" },
  { key: "protected_pc", label: "Protected area", unit: "%", max: 100, note: "WDPA, 2014" },
  { key: "human_footprint", label: "Human footprint", unit: "of 50", max: 50, note: "Human Footprint v2, 2009" },
];

export function habitatIndicators(manifest: any, scalars: any): Indicator[] {
  const s = scalars || {};
  return HABITAT_INDICATORS.map((d) => {
    const v = envValue(manifest, d.key, s[d.key]);
    const shown = v === null ? "no data" : `${formatNumber(v)}${d.unit === "%" ? "%" : ` ${d.unit}`}`;
    return { key: d.key, label: d.label, value: v, display: shown,
      fraction: v === null ? 0 : Math.max(0, Math.min(1, v / d.max)), note: d.note };
  });
}

/** Secondary context values (not habitat): elevation, discharge, regulation, population, climate. */
export function contextValues(manifest: any, scalars: any): any[] {
  const s = scalars || {};
  const rows = [
    ["air_temp_c", "Air temperature, annual mean", "°C"],
    ["precip_mm", "Precipitation, annual", "mm"],
    ["elevation_m", "Elevation, mean", "m"],
    ["discharge_m3s", "Discharge at the outlet", "m³/s"],
    ["regulation_pc", "Degree of regulation at the outlet", "%"],
    ["population_km2", "Population density", "per km²"],
  ];
  const out: any[] = [];
  for (const r of rows) {
    const v = envValue(manifest, r[0], s[r[0]]);
    if (v !== null) out.push({ key: r[0], label: r[1], display: `${formatNumber(v)} ${r[2]}` });
  }
  return out;
}

export const THREATENED = ["CR", "EN", "VU"];

export function filterChecklist(species: any[], query: string, iucnFilter: string): any[] {
  const q = (query || "").trim().toLowerCase();
  return (species || []).filter((s: any) => {
    const cat = s.iucnRedListCategory || s.iucn || "";
    if (iucnFilter === "threatened" && !THREATENED.includes(cat)) return false;
    if (iucnFilter && iucnFilter !== "threatened" && iucnFilter !== cat) return false;
    if (!q) return true;
    return `${s.scientificName || s.name || ""} ${s.family || ""}`.toLowerCase().includes(q);
  });
}

const IUCN_ORDER: Record<string, number> = { EX: 0, EW: 1, CR: 2, EN: 3, VU: 4, NT: 5, LC: 6, DD: 7, NE: 8, "": 9 };

export function sortChecklist(species: any[], by: string): any[] {
  const rows = [...(species || [])];
  const name = (s: any) => String(s.scientificName || s.name || "");
  if (by === "name") rows.sort((a, b) => name(a).localeCompare(name(b)));
  else if (by === "family") rows.sort((a, b) => String(a.family || "").localeCompare(String(b.family || "")) || name(a).localeCompare(name(b)));
  else if (by === "iucn") rows.sort((a, b) => (IUCN_ORDER[a.iucnRedListCategory || a.iucn || ""] ?? 9) - (IUCN_ORDER[b.iucnRedListCategory || b.iucn || ""] ?? 9) || name(a).localeCompare(name(b)));
  else rows.sort((a, b) => (b.count || 0) - (a.count || 0) || name(a).localeCompare(name(b)));
  return rows;
}

function csvCell(v: any): string {
  const s = String(v ?? "");
  const quote = String.fromCharCode(34);
  const needs = s.indexOf(quote) >= 0 || s.indexOf(",") >= 0 || s.indexOf("\n") >= 0;
  return needs ? quote + s.split(quote).join(quote + quote) + quote : s;
}

/** Checklist CSV with a provenance header line (GBIF DOI must be cited when the data are reused). */
export function checklistCsv(species: any[], meta: any): string {
  const lines = [
    `# ${meta.title || "basinmap.org checklist"}; release ${meta.version || ""}; GBIF occurrence download https://doi.org/${meta.doi || ""}`,
    "scientificName,taxonKey,family,iucnRedListCategory,records,firstYear,lastYear",
  ];
  for (const s of species || []) {
    lines.push([s.scientificName || s.name, s.taxonKey || s.key, s.family, s.iucnRedListCategory || s.iucn || "", s.count,
      s.firstYear || "", s.lastYear || ""].map(csvCell).join(","));
  }
  return lines.join("\n") + "\n";
}

// ---------------------------------------------------------------------------------------------
// Country / continent fauna browser
// ---------------------------------------------------------------------------------------------

export const CONTINENT_NAMES: Record<string, string> = {
  AF: "Africa", AN: "Antarctica", AS: "Asia", EU: "Europe", NA: "North America", OC: "Oceania", SA: "South America",
  unknown: "Unknown",
};

export function countryName(code: string, locale: string): string {
  if (!code || code === "unknown") return "Country not recorded";
  try {
    const dn = new (Intl as any).DisplayNames([locale || "en"], { type: "region" });
    const name = dn.of(code);
    if (name && name !== code) return name;
  } catch (_err) {
    // Older browsers: fall back to the code.
  }
  return code;
}

/** Countries of one continent ("" = all), most species first, optionally filtered by a name query. */
export function browseCountries(doc: any, continent: string, query: string, locale: string): any[] {
  const q = normalizeQuery(query);
  const rows = (doc && Array.isArray(doc.countries) ? doc.countries : [])
    .filter((c: any) => !continent || (c.continent || "unknown") === continent)
    .map((c: any) => ({ ...c, name: countryName(c.code, locale) }))
    .filter((c: any) => !q || normalizeQuery(c.name).includes(q) || String(c.code).toLowerCase() === q);
  rows.sort((a: any, b: any) => (b.species_count || 0) - (a.species_count || 0) || a.name.localeCompare(b.name));
  return rows;
}

function normalizeQuery(s: string): string {
  return String(s || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();
}

// ---------------------------------------------------------------------------------------------
// Map export
// ---------------------------------------------------------------------------------------------

/** A round scale-bar length (metres) and its width in pixels for ``metresPerPixel``, at most ``maxPx`` wide. */
export function scaleBar(metresPerPixel: number, maxPx: number): any {
  const maxM = metresPerPixel * maxPx;
  const mag = Math.pow(10, Math.floor(Math.log10(Math.max(maxM, 1))));
  const f = maxM / mag;
  const nice = (f >= 5 ? 5 : f >= 2 ? 2 : 1) * mag;
  const label = nice >= 1000 ? `${formatNumber(nice / 1000)} km` : `${formatNumber(nice)} m`;
  return { metres: nice, px: nice / metresPerPixel, label };
}

/** Web Mercator ground resolution at a latitude and zoom (512-px MapLibre tiles), metres per CSS pixel. */
export function metresPerPixel(lat: number, zoom: number): number {
  return (40075016.686 * Math.cos((lat * Math.PI) / 180)) / (512 * Math.pow(2, zoom));
}

function bytesOf(s: string): number[] {
  const out: number[] = [];
  for (let i = 0; i < s.length; i++) out.push(s.charCodeAt(i) & 0xff);
  return out;
}

/**
 * A one-page PDF (A4, orientation from the image) holding one JPEG, scaled to fit inside 10 mm margins.
 * Written by hand (no PDF library): five objects, the JPEG embedded with DCTDecode.
 */
export function pdfFromJpeg(jpeg: Uint8Array, widthPx: number, heightPx: number, title: string): Uint8Array {
  const landscape = widthPx >= heightPx;
  const pageW = landscape ? 841.89 : 595.28;
  const pageH = landscape ? 595.28 : 841.89;
  const margin = 28.35; // 10 mm
  const scale = Math.min((pageW - 2 * margin) / widthPx, (pageH - 2 * margin) / heightPx);
  const drawW = widthPx * scale;
  const drawH = heightPx * scale;
  const x = (pageW - drawW) / 2;
  const y = (pageH - drawH) / 2;
  const content = `q ${drawW.toFixed(2)} 0 0 ${drawH.toFixed(2)} ${x.toFixed(2)} ${y.toFixed(2)} cm /Im0 Do Q`;
  const safeTitle = String(title || "basinmap.org map").replace(/[()\\]/g, "").replace(/[^\x20-\x7e]/g, "?");
  const chunks: number[][] = [];
  const offsets: number[] = [];
  let length = 0;
  const push = (arr: number[]) => {
    chunks.push(arr);
    length += arr.length;
  };
  push(bytesOf("%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"));
  const obj = (n: number, body: string) => {
    offsets[n] = length;
    push(bytesOf(`${n} 0 obj\n${body}\nendobj\n`));
  };
  obj(1, "<< /Type /Catalog /Pages 2 0 R >>");
  obj(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>");
  obj(3, `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${pageW} ${pageH}] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>`);
  offsets[4] = length;
  push(bytesOf(`4 0 obj\n<< /Type /XObject /Subtype /Image /Width ${widthPx} /Height ${heightPx} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${jpeg.length} >>\nstream\n`));
  push(Array.from(jpeg));
  push(bytesOf("\nendstream\nendobj\n"));
  obj(5, `<< /Length ${content.length} >>\nstream\n${content}\nendstream`);
  obj(6, `<< /Title (${safeTitle}) /Producer (basinmap.org) >>`);
  const xrefAt = length;
  let xref = `xref\n0 7\n0000000000 65535 f \n`;
  for (let n = 1; n <= 6; n++) xref += `${String(offsets[n]).padStart(10, "0")} 00000 n \n`;
  push(bytesOf(`${xref}trailer\n<< /Size 7 /Root 1 0 R /Info 6 0 R >>\nstartxref\n${xrefAt}\n%%EOF\n`));
  const out = new Uint8Array(length);
  let p = 0;
  for (const c of chunks) {
    out.set(c, p);
    p += c.length;
  }
  return out;
}

export function exportFileName(title: string, ext: string, date: Date): string {
  const slug = String(title || "basinmap").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "basinmap";
  const d = date.toISOString().slice(0, 10);
  return `${slug}-${d}.${ext}`;
}

export function escapeText(s: any): string {
  return esc(s);
}
