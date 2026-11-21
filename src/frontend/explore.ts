// Stage 9: basemaps, dataset selector, family/genus/species/synonym search and the lettered species key.
// Pure helpers only (no DOM, no window access at import) so they can be unit-tested; app.ts does the DOM wiring.

export interface Basemap {
  id: string;
  label: string;
  tiles: string[];
  tileSize: number;
  maxzoom: number;
  attribution: string;
}

/** Raster basemaps. Kept in one table so URLs can change without touching map code (OSMF tile policy). */
export const BASEMAPS: Basemap[] = [
  {
    id: "terrain",
    label: "Terrain (light)",
    tiles: ["https://tiles.maps.eox.at/wmts/1.0.0/terrain-light_3857/default/g/{z}/{y}/{x}.jpg"],
    tileSize: 256,
    maxzoom: 16,
    attribution:
      'Terrain Light { Data &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors and others, Rendering &copy; <a href="https://maps.eox.at">EOX</a> }',
  },
  {
    id: "streets",
    label: "Streets (OpenStreetMap)",
    tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
    tileSize: 256,
    maxzoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  },
  {
    id: "satellite",
    label: "Satellite (Sentinel-2 cloudless 2016)",
    tiles: ["https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg"],
    tileSize: 256,
    maxzoom: 15,
    attribution:
      '<a href="https://s2maps.eu">Sentinel-2 cloudless - https://s2maps.eu</a> by <a href="https://eox.at">EOX IT Services GmbH</a> (Contains modified Copernicus Sentinel data 2016 &amp; 2017), CC BY 4.0',
  },
];
export const DEFAULT_BASEMAP = "terrain";
export const GLYPHS_URL = "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf";
export const LETTER_FONT = ["Open Sans Semibold"];

/** Okabe-Ito + Tableau-style palette: 13 colours distinguishable on light basemaps; letters disambiguate repeats. */
export const KEY_PALETTE = [
  "#d55e00", "#0072b2", "#009e73", "#cc79a7", "#e69f00", "#56b4e9", "#8c564b",
  "#7f3c8d", "#11a579", "#3969ac", "#e73f74", "#80ba5a", "#4b4b8f",
];
export const KEY_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
export const MAX_KEY_SPECIES = 26;

export function basemapSources(): Record<string, any> {
  const out: Record<string, any> = {};
  for (const b of BASEMAPS) {
    out[`basemap-${b.id}`] = { type: "raster", tiles: b.tiles, tileSize: b.tileSize, maxzoom: b.maxzoom, attribution: b.attribution };
  }
  return out;
}

export function basemapLayers(active: string): any[] {
  return BASEMAPS.map((b) => ({
    id: `basemap-${b.id}`,
    type: "raster",
    source: `basemap-${b.id}`,
    layout: { visibility: b.id === active ? "visible" : "none" },
    paint: { "raster-opacity": b.id === "satellite" ? 0.95 : 1 },
  }));
}

// ---------------------------------------------------------------------------------------------
// Dataset catalog (releases/datasets.json)
// ---------------------------------------------------------------------------------------------

export interface DatasetEntry {
  id: string;
  title: string;
  description: string;
  manifest_path: string;
  version: string;
  default: boolean;
}

/** Catalog entries with the default dataset always present and first. */
export function normalizeCatalog(catalog: any): DatasetEntry[] {
  const raw: any[] = catalog && Array.isArray(catalog.datasets) ? catalog.datasets : [];
  const out: DatasetEntry[] = [];
  const seen = new Set<string>();
  for (const e of raw) {
    if (!e || typeof e.id !== "string" || typeof e.manifest_path !== "string" || seen.has(e.id)) continue;
    if (!/^\/releases\/[A-Za-z0-9_./-]+\.json$/.test(e.manifest_path) || e.manifest_path.includes("..")) continue;
    seen.add(e.id);
    out.push({
      id: e.id,
      title: String(e.title || e.id),
      description: String(e.description || ""),
      manifest_path: e.manifest_path,
      version: String(e.version || ""),
      default: e.id === "default",
    });
  }
  if (!seen.has("default")) {
    out.unshift({ id: "default", title: "Main map", description: "", manifest_path: "/releases/manifest.json", version: "", default: true });
  }
  out.sort((a, b) => (a.default === b.default ? a.title.localeCompare(b.title) : a.default ? -1 : 1));
  return out;
}

export function pickDataset(entries: DatasetEntry[], requested: string | null): DatasetEntry {
  const hit = requested ? entries.find((e) => e.id === requested) : undefined;
  return hit || entries.find((e) => e.default) || entries[0];
}

// ---------------------------------------------------------------------------------------------
// Taxon search over species_index.json + taxonomy_aliases.json
// ---------------------------------------------------------------------------------------------

export interface SpeciesRow {
  key: number;
  name: string;
  family: string;
  genus: string;
  iucn: string;
  occurrence_count: number;
  vernacular: string[];
  bbox?: number[];
}

export interface TaxonEntry {
  kind: string; // family | genus | species | synonym | common
  label: string;
  detail: string;
  speciesKeys: number[];
  norm: string;
}

export function normalizeAlias(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/×/g, "x")
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .join(" ");
}

function capitalize(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

export function buildTaxonEntries(index: any, aliases: any): TaxonEntry[] {
  const rows: SpeciesRow[] = (index && Array.isArray(index.species) ? index.species : []).filter(
    (r: any) => r && typeof r.key === "number" && typeof r.name === "string"
  );
  const byKey = new Map<number, SpeciesRow>();
  const families = new Map<string, number[]>();
  const genera = new Map<string, number[]>();
  for (const r of rows) {
    byKey.set(r.key, r);
    if (r.family) families.set(r.family, [...(families.get(r.family) || []), r.key]);
    if (r.genus) genera.set(r.genus, [...(genera.get(r.genus) || []), r.key]);
  }
  const entries: TaxonEntry[] = [];
  families.forEach((keys, fam) =>
    entries.push({ kind: "family", label: fam, detail: `${keys.length} species`, speciesKeys: keys, norm: normalizeAlias(fam) })
  );
  genera.forEach((keys, gen) => {
    const fam = byKey.get(keys[0])?.family || "";
    entries.push({ kind: "genus", label: gen, detail: `${fam} · ${keys.length} species`, speciesKeys: keys, norm: normalizeAlias(gen) });
  });
  const accepted = new Set<string>();
  for (const r of rows) {
    accepted.add(normalizeAlias(r.name));
    const recs = r.occurrence_count ? `${r.occurrence_count.toLocaleString("en-US")} records` : "no records";
    entries.push({
      kind: "species",
      label: r.name,
      detail: [r.family, r.iucn ? `IUCN ${r.iucn}` : "", recs].filter(Boolean).join(" · "),
      speciesKeys: [r.key],
      norm: normalizeAlias(r.name),
    });
  }
  const vernacular = new Set<string>();
  for (const r of rows) for (const v of r.vernacular || []) vernacular.add(normalizeAlias(v));
  const map = aliases && aliases.aliases && typeof aliases.aliases === "object" ? aliases.aliases : {};
  for (const norm of Object.keys(map)) {
    const key = map[norm];
    const target = byKey.get(key);
    if (!target || accepted.has(norm)) continue;
    const isCommon = vernacular.has(norm);
    entries.push({
      kind: isCommon ? "common" : "synonym",
      label: isCommon ? norm : capitalize(norm),
      detail: `${isCommon ? "common name" : "synonym"} of ${target.name}`,
      speciesKeys: [key],
      norm,
    });
  }
  return entries;
}

const KIND_RANK: Record<string, number> = { family: 0, genus: 1, species: 2, synonym: 3, common: 4 };

export function searchTaxa(entries: TaxonEntry[], query: string, limit: number): TaxonEntry[] {
  const q = normalizeAlias(query);
  if (q.length < 2) return [];
  const scored: any[] = [];
  for (const e of entries) {
    let score = -1;
    if (e.norm === q) score = 0;
    else if (e.norm.startsWith(q)) score = 1;
    else if (e.norm.split(" ").some((w) => w.startsWith(q))) score = 2;
    else if (e.norm.includes(q)) score = 3;
    if (score >= 0) scored.push([score * 10 + (KIND_RANK[e.kind] ?? 5), e]);
  }
  scored.sort((a, b) => a[0] - b[0] || a[1].label.localeCompare(b[1].label));
  return scored.slice(0, limit).map((s) => s[1]);
}

// ---------------------------------------------------------------------------------------------
// Species key and MapLibre expressions
// ---------------------------------------------------------------------------------------------

export interface KeyEntry {
  key: number;
  name: string;
  letter: string;
  color: string;
  count: number;
  names: string[]; // accepted name + synonyms, for releases whose tiles lack speciesKey
}

/** Deterministic letters/colours: most-recorded species first, ties by name. */
export function assignSpeciesKey(rows: SpeciesRow[], synonymsByKey: Map<number, string[]>): KeyEntry[] {
  const sorted = [...rows].sort((a, b) => (b.occurrence_count || 0) - (a.occurrence_count || 0) || a.name.localeCompare(b.name));
  return sorted.slice(0, MAX_KEY_SPECIES).map((r, i) => ({
    key: r.key,
    name: r.name,
    letter: KEY_LETTERS[i],
    color: KEY_PALETTE[i % KEY_PALETTE.length],
    count: r.occurrence_count || 0,
    names: [r.name, ...(synonymsByKey.get(r.key) || [])],
  }));
}

export function synonymsByKey(aliases: any, index: any): Map<number, string[]> {
  const out = new Map<number, string[]>();
  const skip = new Set<string>(); // vernacular names and accepted names are not synonyms
  for (const r of (index && index.species) || []) {
    skip.add(normalizeAlias(r.name || ""));
    for (const v of r.vernacular || []) skip.add(normalizeAlias(v));
  }
  const map = aliases && aliases.aliases ? aliases.aliases : {};
  for (const norm of Object.keys(map)) {
    if (skip.has(norm)) continue;
    const key = map[norm];
    out.set(key, [...(out.get(key) || []), capitalize(norm)]);
  }
  return out;
}

/** True when the point matches one species: by speciesKey when tiles carry it, else by name prefix. */
function speciesMatch(entry: KeyEntry, useSpeciesKey: boolean): any[] {
  if (useSpeciesKey) return ["==", ["get", "speciesKey"], entry.key];
  // GBIF scientificName includes authorship ("Gambusia affinis (Baird & Girard, 1853)"): match the prefix.
  const tests = entry.names.map((n) => ["==", ["index-of", `${n} `, ["concat", ["get", "scientificName"], " "]], 0]);
  return tests.length === 1 ? tests[0] : ["any", ...tests];
}

export function selectionFilter(entries: KeyEntry[], hidden: Set<number>, useSpeciesKey: boolean): any[] | null {
  const shown = entries.filter((e) => !hidden.has(e.key));
  if (!entries.length) return null;
  if (!shown.length) return ["==", 1, 0];
  return ["any", ...shown.map((e) => speciesMatch(e, useSpeciesKey))];
}

export function colorExpression(entries: KeyEntry[], useSpeciesKey: boolean, fallback: string): any {
  if (!entries.length) return fallback;
  const expr: any[] = ["case"];
  for (const e of entries) expr.push(speciesMatch(e, useSpeciesKey), e.color);
  expr.push(fallback);
  return expr;
}

export function letterExpression(entries: KeyEntry[], useSpeciesKey: boolean): any {
  if (!entries.length) return "";
  const expr: any[] = ["case"];
  for (const e of entries) expr.push(speciesMatch(e, useSpeciesKey), e.letter);
  expr.push("");
  return expr;
}

/** Union of species bboxes (from species_index.json) for fit-to-results. */
export function selectionBounds(rows: SpeciesRow[]): number[] | null {
  const boxes: any[] = rows.map((r) => r.bbox).filter((b) => Array.isArray(b) && b.length === 4);
  if (!boxes.length) return null;
  return [
    Math.min(...boxes.map((b) => b[0])), Math.min(...boxes.map((b) => b[1])),
    Math.max(...boxes.map((b) => b[2])), Math.max(...boxes.map((b) => b[3])),
  ];
}

/** Group features under the pointer by species for the hover tooltip. */
export function groupBySpecies(features: any[], entries: KeyEntry[]): any[] {
  const groups = new Map<string, any>();
  for (const f of features) {
    const p = (f && f.properties) || {};
    const sk = Number(p.speciesKey || 0);
    const sci = String(p.scientificName || "Unidentified");
    const entry = entries.find((e) => (sk && e.key === sk) || e.names.some((n) => sci === n || sci.startsWith(`${n} `)));
    const label = entry ? entry.name : sci;
    const g = groups.get(label) || { label, color: entry ? entry.color : "#0284c7", letter: entry ? entry.letter : "", count: 0 };
    g.count += Number(p.count || 1);
    groups.set(label, g);
  }
  return Array.from(groups.values()).sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}
