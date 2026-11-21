import {
  ApiConfigResponse,
  ApiHealthResponse,
  ApprovedOverlayFeatureCollection,
  BasinScalarProperties,
  BasinShardPayload,
  GeoJsonFeature,
  OccurrenceProperties,
  ReleaseManifest,
  basinShardPath,
  deduplicateApprovedOverlay,
  layerPartitions,
  parsePmtilesHeaderBytes,
  resolveDataUrl,
} from "./types.js";
import { handleMapClickForContrib, initContributions } from "./contrib.js";
import {
  BASEMAPS,
  DEFAULT_BASEMAP,
  DatasetEntry,
  GLYPHS_URL,
  KeyEntry,
  LETTER_FONT,
  SpeciesRow,
  TaxonEntry,
  assignSpeciesKey,
  basemapLayers,
  basemapSources,
  buildTaxonEntries,
  colorExpression,
  groupBySpecies,
  letterExpression,
  normalizeCatalog,
  pickDataset,
  searchTaxa,
  selectionBounds,
  selectionFilter,
  synonymsByKey,
} from "./explore.js";
import {
  BasinLevel,
  MetricDef,
  availableMetrics,
  basinLevels,
  browseCountries,
  checklistCsv,
  choroplethExpression,
  climateChartSvg,
  contextValues,
  countryName,
  envDivisor,
  exportFileName,
  filterChecklist,
  formatNumber,
  habitatIndicators,
  legendStops,
  levelAtZoom,
  levelLabel,
  levelZoomRanges,
  metricById,
  metricDomain,
  metresPerPixel,
  pdfFromJpeg,
  scaleBar,
  shardEntry,
  sortChecklist,
  CONTINENT_NAMES,
} from "./report.js";

interface AppState {
  publicDataUrl: string;
  manifest: ReleaseManifest | null;
  basinsGeojson: GeoJsonFeature[];
  snapshotOccurrences: GeoJsonFeature[];
  approvedOccurrences: GeoJsonFeature[];
  deduplicatedCount: number;
  selectedMetric: string;
  selectedSpecies: string;
  minYear: number;
  maxYear: number;
  showBasins: boolean;
  showOccurrences: boolean;
  showApproved: boolean;
  mapInstance: any;
  usingMapLibre: boolean;
  mapLayerIds: {
    basinsFill: string[];
    basinsOutline: string[];
    occurrences: string[];
    bins: string[];
    letters: string[];
    speciesBins: string[];
  };
  snapshotOwnIds: Set<string>;
  basemap: string;
  datasets: DatasetEntry[];
  dataset: DatasetEntry | null;
  speciesRows: SpeciesRow[];
  taxonEntries: TaxonEntry[];
  synonyms: Map<number, string[]>;
  selection: TaxonEntry | null;
  keyEntries: KeyEntry[];
  hiddenSpecies: Set<number>;
  useSpeciesKey: boolean;
  // Stage 10
  levels: BasinLevel[];
  levelMode: string;
  levelRanges: Record<string, number[]>;
  levelLayers: Record<string, any>;
  metricDomains: Record<string, number[]>;
  countries: any;
  report: any;
}

const state: AppState = {
  publicDataUrl: "",
  manifest: null,
  basinsGeojson: [],
  snapshotOccurrences: [],
  approvedOccurrences: [],
  deduplicatedCount: 0,
  selectedMetric: "occurrence_count",
  selectedSpecies: "",
  minYear: 1900,
  maxYear: new Date().getFullYear(),
  showBasins: true,
  showOccurrences: true,
  showApproved: true,
  mapInstance: null,
  usingMapLibre: false,
  mapLayerIds: { basinsFill: [], basinsOutline: [], occurrences: [], bins: [], letters: [], speciesBins: [] },
  snapshotOwnIds: new Set<string>(),
  basemap: DEFAULT_BASEMAP,
  datasets: [],
  dataset: null,
  speciesRows: [],
  taxonEntries: [],
  synonyms: new Map<number, string[]>(),
  selection: null,
  keyEntries: [],
  hiddenSpecies: new Set<number>(),
  useSpeciesKey: false,
  levels: [],
  levelMode: "auto",
  levelRanges: {},
  levelLayers: {},
  metricDomains: {},
  countries: null,
  report: null,
};

const OCCURRENCE_COLOR = "#0284c7";

/** Choropleth for one HydroBASINS level: colours stretch to the basins in view (Stage 10). */
function levelFillExpression(levelKey: string): any {
  const metric = metricById(state.selectedMetric);
  const domain = state.metricDomains[`${levelKey}:${metric.id}`] || (metric.kind === "count" ? [0, 100] : [0, 1]);
  return choroplethExpression(metric, domain, metric.envKey ? envDivisor(state.manifest, metric.envKey) : 1);
}

function getEl(id: string): any {
  return document.getElementById(id);
}

function escapeHtml(raw: unknown): string {
  return String(raw ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function fetchJsonWithTimeout(url: string, timeoutMs: number): Promise<any> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(url, { signal: controller.signal });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status} for ${url}`);
    }
    return await resp.json();
  } finally {
    clearTimeout(timer);
  }
}

async function probeApiStatus(): Promise<{ online: boolean; publicDataUrl: string }> {
  const apiBadge = getEl("badge-api");
  const offlineBanner = getEl("api-offline-banner");
  let resolvedDataUrl = "";

  try {
    const health: ApiHealthResponse = await fetchJsonWithTimeout("/api/health", 2500);
    if (health.status !== "ok") {
      throw new Error(`Unexpected health status: ${health.status}`);
    }
    const config: ApiConfigResponse = await fetchJsonWithTimeout("/api/config", 2500);
    resolvedDataUrl = config.public_data_url || health.public_data_url || "";

    if (apiBadge) {
      apiBadge.textContent = `API: online (${health.environment})`;
      apiBadge.className = "pill ok";
    }
    if (offlineBanner) {
      offlineBanner.classList.add("hidden");
    }
    return { online: true, publicDataUrl: resolvedDataUrl };
  } catch (_err) {
    if (apiBadge) {
      apiBadge.textContent = "API: offline (map active)";
      apiBadge.className = "pill warn";
    }
    if (offlineBanner) {
      offlineBanner.classList.remove("hidden");
    }
    return { online: false, publicDataUrl: "" };
  }
}

async function verifyRangeProbe(url: string): Promise<{ status: number; tiles: number; maxZoom: number }> {
  const resp = await fetch(url, {
    method: "GET",
    headers: { Range: "bytes=0-126" },
  });
  if (resp.status !== 206 && resp.status !== 200) {
    throw new Error(`Range probe failed with HTTP ${resp.status}`);
  }
  const buf = await resp.arrayBuffer();
  const header = parsePmtilesHeaderBytes(buf.slice(0, 127));
  return {
    status: resp.status,
    tiles: header.numAddressedTiles,
    maxZoom: header.maxZoom,
  };
}

function renderBasinScalars(props: BasinScalarProperties, levelKey: string): void {
  const container = getEl("basin-scalars");
  if (!container) return;
  const lvl = state.levels.find((l) => l.key === levelKey);
  const p: any = props;
  const temp = p.env_air_temp_c;
  const precip = p.env_precip_mm;
  const climate = temp !== undefined && temp !== null && temp !== ""
    ? `<div class="metric-tile"><div class="metric-label">Climate (annual)</div><div class="metric-value">${escapeHtml(formatNumber(Number(temp)))} °C${precip !== undefined && precip !== null && precip !== "" ? ` · ${escapeHtml(formatNumber(Number(precip)))} mm` : ""}</div></div>`
    : "";
  container.innerHTML = `
    <div style="margin-bottom: 0.6rem;">
      <strong style="font-size: 1rem;">${escapeHtml(props.basin_name)}</strong>
      <div class="muted-note">
        ${lvl ? `${escapeHtml(levelLabel(lvl.level))} &middot; ` : ""}HYBAS_ID <code>${escapeHtml(props.hybas_id)}</code> &middot; Pfafstetter <code>${escapeHtml(props.pfaf_id)}</code>
      </div>
    </div>
    <div class="metric-grid">
      <div class="metric-tile">
        <div class="metric-label">Occurrences</div>
        <div class="metric-value">${escapeHtml(Number(props.occurrence_count || 0).toLocaleString("en-US"))}</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Species</div>
        <div class="metric-value">${escapeHtml(props.species_count)}</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Threatened-species records</div>
        <div class="metric-value">${escapeHtml(Number(props.threatened_count || 0).toLocaleString("en-US"))}</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Year span</div>
        <div class="metric-value">${props.min_year ? `${escapeHtml(props.min_year)}&ndash;${escapeHtml(props.max_year)}` : "undated"}</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Sub-basin area</div>
        <div class="metric-value">${escapeHtml(Number(props.sub_area_km2 || 0).toLocaleString("en-US"))} km&sup2;</div>
      </div>
      ${climate}
    </div>
  `;
}

async function loadAndRenderBasinShard(props: BasinScalarProperties, levelKey?: string): Promise<void> {
  const level = levelKey || (state.levels.find((l) => l.primary)?.key ?? "06");
  renderBasinScalars(props, level);
  const shardEl = getEl("basin-shard-details");
  if (!shardEl || !state.manifest) return;

  const shardPath = basinShardPath(state.manifest, props.hybas_id, props.shard_path);
  const shardUrl = resolveDataUrl(state.publicDataUrl, shardPath);
  if (!shardPath.endsWith(".json")) {
    shardEl.innerHTML = `<p class="muted-note" style="margin-top:0.6rem;">Detailed checklist for this sub-basin is in the regional partition <a href="${escapeHtml(shardUrl)}">${escapeHtml(shardPath.split("/").pop())}</a> (SQLite, table <code>basin_shards</code>).</p>`;
    return;
  }
  shardEl.innerHTML = `<p class="muted-note" style="margin-top:0.6rem;">Loading the species list…</p>`;

  try {
    const doc = await fetchJsonWithTimeout(shardUrl, 8000);
    const shard: any = shardEntry(doc, props.hybas_id);
    if (!shard) throw new Error("this basin is not in its shard file");
    state.report = { kind: "basin", props, level, shard, url: shardUrl, sort: "count", query: "", iucn: "" };
    const top = (shard.species || []).slice(0, 8)
      .map((sp: any) => `<li><em>${escapeHtml(sp.scientificName)}</em> <span class="iucn-badge iucn-${escapeHtml(sp.iucnRedListCategory || "NE")}">${escapeHtml(sp.iucnRedListCategory || "NE")}</span> <span class="muted">${escapeHtml(sp.count)}</span></li>`)
      .join("");
    const more = (shard.species || []).length > 8 ? `<p class="muted-note">…and ${(shard.species || []).length - 8} more in the report.</p>` : "";
    shardEl.innerHTML = `
      <div style="margin-top:0.75rem;">
        <button id="btn-open-report" class="btn btn-primary btn-small" type="button">Open basin report</button>
        <h3 style="margin-top:0.75rem;">Most recorded species</h3>
        ${top ? `<ul class="top-species">${top}</ul>${more}` : `<p class="muted-note">No species-level records in this basin.</p>`}
      </div>`;
    getEl("btn-open-report")?.addEventListener("click", () => openBasinReport());
    if (!getEl("report-drawer")?.classList.contains("hidden") && state.report) renderReport();
  } catch (err: any) {
    shardEl.innerHTML = `<p class="muted-note" style="color:#991b1b;">Unable to load the species list: ${escapeHtml(err?.message || err)}</p>`;
  }
}

// ------------------------------------------------------------------------------------------------
// Stage 10: Basin Report drawer, country browser, choropleth legend, map export
// ------------------------------------------------------------------------------------------------

function openReportDrawer(): void {
  const drawer = getEl("report-drawer");
  if (!drawer) return;
  getEl("contrib-drawer")?.classList.add("hidden");
  drawer.classList.remove("hidden");
  renderReport();
  getEl("report-close")?.focus();
}

function closeReportDrawer(): void {
  getEl("report-drawer")?.classList.add("hidden");
}

function openBasinReport(): void {
  if (!state.report) return;
  openReportDrawer();
}

function renderReport(): void {
  const body = getEl("report-body");
  const title = getEl("report-title");
  const r = state.report;
  if (!body || !r) return;
  if (r.kind === "country") {
    renderCountryReport(body, title, r);
    return;
  }
  const props: any = r.props;
  const shard: any = r.shard;
  const manifest: any = state.manifest;
  const lvl = state.levels.find((l) => l.key === r.level);
  if (title) title.textContent = props.basin_name || `Basin ${props.hybas_id}`;
  const env = shard.environment || null;
  const species = sortChecklist(filterChecklist(shard.species || [], r.query, r.iucn), r.sort);
  const totalSpecies = (shard.species || []).length;
  const threatenedSpecies = (shard.species || []).filter((sp: any) => ["CR", "EN", "VU"].includes(sp.iucnRedListCategory)).length;
  const rows = species
    .map((sp: any) => `<tr>
        <td><em>${escapeHtml(sp.scientificName)}</em><div class="muted-note">${escapeHtml(sp.family || "")}</div></td>
        <td><span class="iucn-badge iucn-${escapeHtml(sp.iucnRedListCategory || "NE")}">${escapeHtml(sp.iucnRedListCategory || "NE")}</span></td>
        <td class="num">${escapeHtml(Number(sp.count || 0).toLocaleString("en-US"))}</td>
        <td class="num">${sp.firstYear ? `${escapeHtml(sp.firstYear)}–${escapeHtml(sp.lastYear)}` : "–"}</td>
        <td><a href="https://www.gbif.org/species/${escapeHtml(sp.taxonKey)}" target="_blank" rel="noopener noreferrer" aria-label="GBIF species page for ${escapeHtml(sp.scientificName)}">GBIF</a></td>
      </tr>`)
    .join("");

  let climate = "";
  let habitat = "";
  let context = "";
  if (env) {
    const chart = climateChartSvg(env.monthly || {}, 340, 190);
    climate = chart
      ? `<section class="report-section"><h3>Climate through the year</h3>${chart}
         <p class="legend-line"><span class="legend-swatch" style="background:#93c5fd"></span>Precipitation (mm per month)
         <span class="legend-swatch" style="background:#dc2626;border-radius:50%"></span>Mean air temperature (°C)</p></section>`
      : "";
    const ind = habitatIndicators(manifest, env.scalars || {});
    habitat = `<section class="report-section"><h3>Habitat indicators</h3><ul class="indicator-list">${ind
      .map((i) => `<li><div class="ind-head"><span>${escapeHtml(i.label)}</span><strong>${escapeHtml(i.display)}</strong></div>
        <div class="ind-bar" role="img" aria-label="${escapeHtml(i.label)}: ${escapeHtml(i.display)}"><span style="width:${(i.fraction * 100).toFixed(1)}%"></span></div>
        <small class="muted-note">${escapeHtml(i.note)}</small></li>`)
      .join("")}</ul></section>`;
    const ctx = contextValues(manifest, env.scalars || {});
    context = ctx.length
      ? `<section class="report-section"><h3>Other basin values</h3><dl class="context-list">${ctx
          .map((c: any) => `<dt>${escapeHtml(c.label)}</dt><dd>${escapeHtml(c.display)}</dd>`).join("")}</dl></section>`
      : "";
  }

  const hist = shard.temporal_histogram || {};
  const years = Object.keys(hist).sort();
  const maxYearCount = Math.max(1, ...years.map((y) => Number(hist[y]) || 0));
  const histBars = years
    .map((yr) => `<div class="hist-col" title="${escapeHtml(yr)}: ${escapeHtml(hist[yr])} records"><div class="hist-bar" style="height:${Math.max(6, Math.round(((Number(hist[yr]) || 0) / maxYearCount) * 100))}%"></div></div>`)
    .join("");
  const datasets = (shard.datasets || []).slice(0, 12)
    .map((ds: any) => `<li>${escapeHtml(ds.datasetName)} <span class="muted">(${escapeHtml(ds.count)})</span></li>`).join("");
  const moreDatasets = (shard.datasets || []).length > 12 ? `<li class="muted">…and ${(shard.datasets || []).length - 12} more</li>` : "";
  const doi = manifest?.provenance?.gbif_doi || "";
  const atlasSource = manifest?.environment?.source;

  body.innerHTML = `
    <p class="muted-note">${lvl ? `${escapeHtml(levelLabel(lvl.level))} · ` : ""}HYBAS_ID <code>${escapeHtml(props.hybas_id)}</code> ·
      Pfafstetter <code>${escapeHtml(props.pfaf_id)}</code> · ${escapeHtml(Number(props.sub_area_km2 || 0).toLocaleString("en-US"))} km²</p>
    <div class="metric-grid">
      <div class="metric-tile"><div class="metric-label">Records</div><div class="metric-value">${escapeHtml(Number(props.occurrence_count || 0).toLocaleString("en-US"))}</div></div>
      <div class="metric-tile"><div class="metric-label">Species</div><div class="metric-value">${escapeHtml(totalSpecies)}</div></div>
      <div class="metric-tile"><div class="metric-label">Threatened species</div><div class="metric-value">${escapeHtml(threatenedSpecies)}</div></div>
      <div class="metric-tile"><div class="metric-label">Years</div><div class="metric-value">${props.min_year ? `${escapeHtml(props.min_year)}–${escapeHtml(props.max_year)}` : "undated"}</div></div>
    </div>
    <div class="btn-row">
      <button id="report-zoom" class="btn btn-small btn-ghost" type="button">Zoom to basin</button>
      <button id="report-show-species" class="btn btn-small btn-ghost" type="button" ${totalSpecies && state.speciesRows.length ? "" : "disabled"}>Show these species on the map</button>
      <button id="report-csv" class="btn btn-small btn-ghost" type="button">Download checklist (CSV)</button>
    </div>
    ${climate}
    ${habitat}
    ${context}
    <section class="report-section">
      <h3>Species checklist <span class="muted">(${species.length} of ${totalSpecies})</span></h3>
      <div class="checklist-controls">
        <input id="report-q" type="search" placeholder="Filter by name or family" value="${escapeHtml(r.query)}" aria-label="Filter species" />
        <select id="report-iucn" aria-label="IUCN category">
          <option value="">All categories</option>
          <option value="threatened"${r.iucn === "threatened" ? " selected" : ""}>Threatened (CR, EN, VU)</option>
          ${["CR", "EN", "VU", "NT", "LC", "DD", "NE"].map((c) => `<option value="${c}"${r.iucn === c ? " selected" : ""}>${c}</option>`).join("")}
        </select>
        <select id="report-sort" aria-label="Sort species">
          <option value="count"${r.sort === "count" ? " selected" : ""}>Most records</option>
          <option value="name"${r.sort === "name" ? " selected" : ""}>Name</option>
          <option value="family"${r.sort === "family" ? " selected" : ""}>Family</option>
          <option value="iucn"${r.sort === "iucn" ? " selected" : ""}>Most threatened</option>
        </select>
      </div>
      <div class="table-scroll"><table class="species-table">
        <thead><tr><th>Species</th><th>IUCN</th><th class="num">Records</th><th class="num">Years</th><th></th></tr></thead>
        <tbody>${rows || `<tr><td colspan="5" class="muted-note">No species match.</td></tr>`}</tbody>
      </table></div>
    </section>
    ${years.length ? `<section class="report-section"><h3>Records per year</h3><div class="histogram-bars" role="img" aria-label="Records per year, ${escapeHtml(years[0])} to ${escapeHtml(years[years.length - 1])}">${histBars}</div>
      <div class="hist-axis"><span>${escapeHtml(years[0])}</span><span>${escapeHtml(years[years.length - 1])}</span></div></section>` : ""}
    ${datasets ? `<section class="report-section"><h3>Contributing datasets</h3><ul class="dataset-list">${datasets}${moreDatasets}</ul></section>` : ""}
    <section class="report-section method-notes">
      <h3>About these numbers</h3>
      <ul>
        <li>Records are GBIF occurrences whose coordinates fall inside this HydroBASINS polygon (Lehner &amp; Grill 2013).
          Species are GBIF species keys; records not identified to species are counted as records only.</li>
        <li>Threatened means IUCN Red List categories CR, EN or VU as reported by GBIF. A basin's records reflect
          where people have sampled, not a complete inventory.</li>
        ${env ? `<li>Climate and habitat values describe the <strong>whole sub-basin</strong> (BasinATLAS v1.0, “s” attributes), not the
          places where the fish were recorded. Temperatures and precipitation are 1970–2000 WorldClim averages; outlet values
          (discharge, regulation) are at the basin's pour point.</li>` : ""}
      </ul>
      <p class="muted-note">Cite: GBIF.org occurrence download <a href="https://doi.org/${escapeHtml(doi)}" target="_blank" rel="noopener noreferrer">doi:${escapeHtml(doi)}</a>;
        HydroBASINS v1c (Lehner &amp; Grill 2013)${atlasSource ? `; ${escapeHtml(atlasSource.citation || atlasSource.name)} (${escapeHtml(atlasSource.license || "CC BY 4.0")})` : ""}.
        Release ${escapeHtml(manifest?.current_version || "")}.</p>
    </section>`;

  getEl("report-q")?.addEventListener("input", (e: any) => {
    r.query = e.target.value;
    rerenderReportKeepingFocus("report-q");
  });
  getEl("report-iucn")?.addEventListener("change", (e: any) => {
    r.iucn = e.target.value;
    renderReport();
  });
  getEl("report-sort")?.addEventListener("change", (e: any) => {
    r.sort = e.target.value;
    renderReport();
  });
  getEl("report-zoom")?.addEventListener("click", () => {
    const bb = shard.bbox;
    if (state.mapInstance && Array.isArray(bb)) state.mapInstance.fitBounds([[bb[0], bb[1]], [bb[2], bb[3]]], { padding: 48, duration: 700 });
  });
  getEl("report-show-species")?.addEventListener("click", () => {
    const keys = (shard.species || []).map((sp: any) => Number(sp.taxonKey)).filter((k: number) => k > 0);
    applySelection({ kind: "basin", label: props.basin_name || props.hybas_id, detail: `${keys.length} species`, speciesKeys: keys, norm: "", bbox: shard.bbox } as any);
  });
  getEl("report-csv")?.addEventListener("click", () => {
    const csv = checklistCsv(species, { title: `${props.basin_name || props.hybas_id} (HYBAS_ID ${props.hybas_id})`, version: manifest?.current_version, doi });
    downloadBlob(new Blob([csv], { type: "text/csv;charset=utf-8" }), exportFileName(`checklist ${props.basin_name || props.hybas_id}`, "csv", new Date()));
  });
}

function rerenderReportKeepingFocus(inputId: string): void {
  const before = getEl(inputId);
  const pos = before ? before.selectionStart : null;
  renderReport();
  const after = getEl(inputId);
  if (after) {
    after.focus();
    if (pos !== null) after.setSelectionRange(pos, pos);
  }
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

// --- Country / continent fauna browser ---------------------------------------------------------

async function loadCountries(manifest: ReleaseManifest): Promise<void> {
  const card = getEl("country-card");
  const spec: any = (manifest.layers as any).countries;
  if (!card || !spec || !spec.path) {
    card?.classList.add("hidden");
    return;
  }
  try {
    state.countries = await fetchJsonWithTimeout(resolveDataUrl(state.publicDataUrl, spec.path), 8000);
  } catch (_err) {
    card.classList.add("hidden");
    return;
  }
  card.classList.remove("hidden");
  const sel = getEl("select-continent");
  if (sel) {
    sel.innerHTML = `<option value="">All continents</option>` + (state.countries.continents || [])
      .map((c: any) => `<option value="${escapeHtml(c.code)}">${escapeHtml(CONTINENT_NAMES[c.code] || c.name)} (${escapeHtml(c.species_count)} species)</option>`)
      .join("");
    sel.addEventListener("change", () => renderCountryList());
  }
  getEl("country-search")?.addEventListener("input", () => renderCountryList());
  renderCountryList();
}

function renderCountryList(): void {
  const list = getEl("country-list");
  if (!list || !state.countries) return;
  const continent = getEl("select-continent")?.value || "";
  const q = getEl("country-search")?.value || "";
  const rows = browseCountries(state.countries, continent, q, navigator.language || "en");
  const shown = rows.slice(0, 60);
  list.innerHTML = shown
    .map((c: any) => `<li><button type="button" class="country-row" data-code="${escapeHtml(c.code)}">
        <span>${escapeHtml(c.name)}</span><span class="muted">${escapeHtml(c.species_count)} sp · ${escapeHtml(Number(c.occurrence_count).toLocaleString("en-US"))} rec</span></button></li>`)
    .join("") || `<li class="muted-note">No countries match.</li>`;
  const note = getEl("country-note");
  if (note) note.textContent = rows.length > shown.length ? `Showing ${shown.length} of ${rows.length}; type to find others.` : "";
  list.querySelectorAll(".country-row").forEach((btn: any) => {
    btn.addEventListener("click", () => openCountryReport(btn.getAttribute("data-code")));
  });
}

function openCountryReport(code: string): void {
  const c = (state.countries?.countries || []).find((x: any) => x.code === code);
  if (!c) return;
  state.report = { kind: "country", country: c, sort: "count", query: "", iucn: "" };
  openReportDrawer();
}

function renderCountryReport(body: any, title: any, r: any): void {
  const c = r.country;
  const name = countryName(c.code, navigator.language || "en");
  if (title) title.textContent = name;
  const species = sortChecklist(filterChecklist(c.species || [], r.query, r.iucn), r.sort);
  const rows = species
    .map((sp: any) => `<tr><td><em>${escapeHtml(sp.name)}</em><div class="muted-note">${escapeHtml(sp.family || "")}</div></td>
      <td><span class="iucn-badge iucn-${escapeHtml(sp.iucn || "NE")}">${escapeHtml(sp.iucn || "NE")}</span></td>
      <td class="num">${escapeHtml(Number(sp.count).toLocaleString("en-US"))}</td>
      <td><a href="https://www.gbif.org/species/${escapeHtml(sp.key)}" target="_blank" rel="noopener noreferrer" aria-label="GBIF species page for ${escapeHtml(sp.name)}">GBIF</a></td></tr>`)
    .join("");
  const doi = state.manifest?.provenance?.gbif_doi || "";
  body.innerHTML = `
    <p class="muted-note">${escapeHtml(CONTINENT_NAMES[c.continent || "unknown"] || "")}${c.code !== "unknown" ? ` · ISO code ${escapeHtml(c.code)}` : ""}</p>
    <div class="metric-grid">
      <div class="metric-tile"><div class="metric-label">Records</div><div class="metric-value">${escapeHtml(Number(c.occurrence_count).toLocaleString("en-US"))}</div></div>
      <div class="metric-tile"><div class="metric-label">Species</div><div class="metric-value">${escapeHtml(c.species_count)}</div></div>
      <div class="metric-tile"><div class="metric-label">Threatened species</div><div class="metric-value">${escapeHtml(c.threatened_species)}</div></div>
      <div class="metric-tile"><div class="metric-label">Years</div><div class="metric-value">${c.min_year ? `${escapeHtml(c.min_year)}–${escapeHtml(c.max_year)}` : "undated"}</div></div>
    </div>
    <div class="btn-row">
      <button id="country-show" class="btn btn-small btn-primary" type="button" ${c.species.length ? "" : "disabled"}>Show these species on the map</button>
      <button id="country-csv" class="btn btn-small btn-ghost" type="button">Download list (CSV)</button>
    </div>
    <p class="muted-note">“Show on the map” highlights these species everywhere they were recorded and zooms to ${escapeHtml(name)}.</p>
    <section class="report-section">
      <h3>Species recorded in ${escapeHtml(name)} <span class="muted">(${species.length} of ${c.species.length})</span></h3>
      <div class="checklist-controls">
        <input id="report-q" type="search" placeholder="Filter by name or family" value="${escapeHtml(r.query)}" aria-label="Filter species" />
        <select id="report-iucn" aria-label="IUCN category">
          <option value="">All categories</option>
          <option value="threatened"${r.iucn === "threatened" ? " selected" : ""}>Threatened (CR, EN, VU)</option>
        </select>
        <select id="report-sort" aria-label="Sort species">
          <option value="count"${r.sort === "count" ? " selected" : ""}>Most records</option>
          <option value="name"${r.sort === "name" ? " selected" : ""}>Name</option>
          <option value="family"${r.sort === "family" ? " selected" : ""}>Family</option>
          <option value="iucn"${r.sort === "iucn" ? " selected" : ""}>Most threatened</option>
        </select>
      </div>
      <div class="table-scroll"><table class="species-table">
        <thead><tr><th>Species</th><th>IUCN</th><th class="num">Records</th><th></th></tr></thead>
        <tbody>${rows || `<tr><td colspan="4" class="muted-note">No species match.</td></tr>`}</tbody>
      </table></div>
    </section>
    <section class="report-section method-notes">
      <h3>About these numbers</h3>
      <p class="muted-note">Countries are GBIF's interpreted country code for each record (ISO 3166-1); records GBIF could
        not place in a country are grouped as “Country not recorded”. Counts include only records in this release.
        Cite: GBIF.org occurrence download <a href="https://doi.org/${escapeHtml(doi)}" target="_blank" rel="noopener noreferrer">doi:${escapeHtml(doi)}</a>.</p>
    </section>`;
  getEl("report-q")?.addEventListener("input", (e: any) => {
    r.query = e.target.value;
    rerenderReportKeepingFocus("report-q");
  });
  getEl("report-iucn")?.addEventListener("change", (e: any) => {
    r.iucn = e.target.value;
    renderReport();
  });
  getEl("report-sort")?.addEventListener("change", (e: any) => {
    r.sort = e.target.value;
    renderReport();
  });
  getEl("country-show")?.addEventListener("click", () => {
    const keys = (c.species || []).map((sp: any) => Number(sp.key));
    applySelection({ kind: "country", label: name, detail: `${keys.length} species`, speciesKeys: keys, norm: "", bbox: c.bbox } as any);
  });
  getEl("country-csv")?.addEventListener("click", () => {
    const csv = checklistCsv(species, { title: `Species recorded in ${name}`, version: state.manifest?.current_version, doi });
    downloadBlob(new Blob([csv], { type: "text/csv;charset=utf-8" }), exportFileName(`species ${name}`, "csv", new Date()));
  });
}

// --- Choropleth domain, legend and level note ---------------------------------------------------

function activeLevelKey(): string {
  const map = state.mapInstance;
  const zoom = map ? map.getZoom() : 0;
  return levelAtZoom(state.levelRanges, zoom) || (state.levels.find((l) => l.primary)?.key ?? "");
}

function updateChoropleth(): void {
  const map = state.mapInstance;
  if (!state.usingMapLibre || !map) return;
  const metric = metricById(state.selectedMetric);
  const divisor = metric.envKey ? envDivisor(state.manifest, metric.envKey) : 1;
  for (const levelKey of Object.keys(state.levelLayers)) {
    const layers = state.levelLayers[levelKey].fill.filter((id: string) => map.getLayer(id) && map.getLayoutProperty(id, "visibility") !== "none");
    if (!layers.length) continue;
    let feats: any[] = [];
    try {
      feats = map.queryRenderedFeatures({ layers });
    } catch (_err) {
      feats = [];
    }
    if (!feats.length) continue;
    const seen = new Set<string>();
    const values: number[] = [];
    for (const f of feats) {
      const p = f.properties || {};
      const id = String(p.hybas_id ?? f.id ?? "");
      if (seen.has(id)) continue;
      seen.add(id);
      const raw = p[metric.id];
      if (raw === undefined || raw === null || raw === "") continue;
      values.push(Number(raw) / divisor);
    }
    if (!values.length) continue;
    state.metricDomains[`${levelKey}:${metric.id}`] = metricDomain(values, metric.kind);
    for (const id of state.levelLayers[levelKey].fill) {
      if (map.getLayer(id)) map.setPaintProperty(id, "fill-color", levelFillExpression(levelKey));
    }
  }
  renderChoroplethLegend();
}

function renderChoroplethLegend(): void {
  const el = getEl("hud-legend");
  if (!el) return;
  if (!state.usingMapLibre || !state.showBasins || !state.levels.length) {
    el.classList.add("hidden");
    return;
  }
  const metric = metricById(state.selectedMetric);
  const key = activeLevelKey();
  const domain = state.metricDomains[`${key}:${metric.id}`];
  if (!domain) {
    el.classList.add("hidden");
    return;
  }
  const stops = legendStops(metric, domain);
  el.classList.remove("hidden");
  el.innerHTML = `<div class="legend-title">${escapeHtml(metric.label)}${metric.unit && metric.kind !== "count" ? ` (${escapeHtml(metric.unit)})` : ""}</div>
    <div class="legend-ramp" style="background:linear-gradient(90deg, ${stops.map((st: any) => st.color).join(", ")})"></div>
    <div class="legend-labels">${stops.map((st: any) => `<span>${escapeHtml(st.label)}</span>`).join("")}</div>
    <div class="legend-sub">${metric.kind === "count" ? "log scale, " : ""}basins in view</div>`;
}

function renderLevelNote(): void {
  const note = getEl("basin-level-note");
  if (!note || !state.levels.length) return;
  const key = activeLevelKey();
  const lvl = state.levels.find((l) => l.key === key);
  const range = state.levelRanges[key];
  if (state.levelMode !== "auto" && range && state.mapInstance && state.mapInstance.getZoom() < range[0]) {
    note.textContent = `Zoom in to see ${lvl ? levelLabel(lvl.level).toLowerCase() : "this level"} (from zoom ${range[0]}).`;
    return;
  }
  note.textContent = lvl ? `Showing ${levelLabel(lvl.level).toLowerCase()}.` : "";
}

function populateBasinControls(manifest: ReleaseManifest): void {
  const metricSel = getEl("select-metric");
  if (metricSel) {
    const metrics = availableMetrics(manifest);
    const groups: Record<string, MetricDef[]> = {};
    metrics.forEach((m) => {
      groups[m.group] = [...(groups[m.group] || []), m];
    });
    metricSel.innerHTML = Object.keys(groups)
      .map((g) => `<optgroup label="${escapeHtml(g)}">${groups[g].map((m) => `<option value="${m.id}">${escapeHtml(m.label)}</option>`).join("")}</optgroup>`)
      .join("");
    if (!metrics.some((m) => m.id === state.selectedMetric)) state.selectedMetric = metrics[0].id;
    metricSel.value = state.selectedMetric;
  }
  const levels = basinLevels(manifest);
  const row = getEl("basin-level-row");
  const levelSel = getEl("select-basin-level");
  if (!row || !levelSel) return;
  if (levels.length < 2) {
    row.classList.add("hidden");
    return;
  }
  row.classList.remove("hidden");
  levelSel.innerHTML = `<option value="auto">Automatic (${levels.map((l) => l.level).join(" → ")} as you zoom in)</option>` +
    levels.map((l) => `<option value="${l.key}">${escapeHtml(levelLabel(l.level))}</option>`).join("");
  let saved = "auto";
  try {
    saved = window.localStorage.getItem("bm.levels") || "auto";
  } catch (_err) {
    saved = "auto";
  }
  state.levelMode = saved === "auto" || levels.some((l) => l.key === saved) ? saved : "auto";
  levelSel.value = state.levelMode;
}

// --- Map export (PNG / PDF) ---------------------------------------------------------------------

function openExportModal(): void {
  const modal = getEl("export-modal");
  if (!modal || !state.usingMapLibre) return;
  const titleInput = getEl("export-map-title");
  if (titleInput && !titleInput.value) {
    const ds = state.dataset ? state.dataset.title : "basinmap.org";
    titleInput.value = state.selection ? `${state.selection.label}: ${ds}` : ds;
  }
  const status = getEl("export-status");
  if (status) status.textContent = "";
  modal.classList.remove("hidden");
  titleInput?.focus();
}

function closeExportModal(): void {
  getEl("export-modal")?.classList.add("hidden");
  getEl("btn-export")?.focus();
}

function captureMapCanvas(map: any): Promise<HTMLCanvasElement> {
  return new Promise((resolve) => {
    map.once("render", () => {
      const src = map.getCanvas();
      const copy = document.createElement("canvas");
      copy.width = src.width;
      copy.height = src.height;
      const ctx = copy.getContext("2d");
      if (ctx) ctx.drawImage(src, 0, 0);
      resolve(copy);
    });
    map.triggerRepaint();
  });
}

function waitForIdle(map: any, timeoutMs: number): Promise<void> {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, timeoutMs);
    map.once("idle", () => {
      clearTimeout(t);
      resolve();
    });
    map.triggerRepaint();
  });
}

async function exportMap(): Promise<void> {
  const map = state.mapInstance;
  const manifest: any = state.manifest;
  if (!map || !manifest) return;
  const status = getEl("export-status");
  const go = getEl("export-go");
  const format = (document.querySelector('input[name="export-format"]:checked') as any)?.value || "png";
  const factor = Number(getEl("export-scale")?.value || 1);
  const withLegend = Boolean(getEl("export-legend")?.checked);
  const title = (getEl("export-map-title")?.value || "basinmap.org map").trim();
  if (go) go.disabled = true;
  if (status) status.textContent = "Rendering the map…";
  const originalRatio = typeof map.getPixelRatio === "function" ? map.getPixelRatio() : window.devicePixelRatio || 1;
  try {
    if (factor > 1 && typeof map.setPixelRatio === "function") {
      map.setPixelRatio(Math.min(4, (window.devicePixelRatio || 1) * factor));
      await waitForIdle(map, 15000);
    }
    const mapCanvas = await captureMapCanvas(map);
    const ratio = mapCanvas.width / map.getCanvas().clientWidth;
    const out = composeExport(mapCanvas, ratio, title, withLegend, manifest);
    const date = new Date();
    if (format === "pdf") {
      const blob: Blob = await new Promise((res, rej) => out.toBlob((b: Blob | null) => (b ? res(b) : rej(new Error("JPEG encoding failed"))), "image/jpeg", 0.92));
      const jpeg = new Uint8Array(await blob.arrayBuffer());
      const pdf = pdfFromJpeg(jpeg, out.width, out.height, title);
      downloadBlob(new Blob([pdf as BlobPart], { type: "application/pdf" }), exportFileName(title, "pdf", date));
    } else {
      const blob: Blob = await new Promise((res, rej) => out.toBlob((b: Blob | null) => (b ? res(b) : rej(new Error("PNG encoding failed"))), "image/png"));
      downloadBlob(blob, exportFileName(title, "png", date));
    }
    if (status) status.textContent = "Done. Check your downloads.";
  } catch (err: any) {
    if (status) status.textContent = `Export failed: ${err?.message || err}. A background map provider may block image export; try “No background map”.`;
  } finally {
    if (factor > 1 && typeof map.setPixelRatio === "function") map.setPixelRatio(originalRatio);
    if (go) go.disabled = false;
  }
}

function composeExport(mapCanvas: HTMLCanvasElement, ratio: number, title: string, withLegend: boolean, manifest: any): HTMLCanvasElement {
  const r = Math.max(1, ratio);
  const pad = Math.round(14 * r);
  const headerH = Math.round(46 * r);
  const keyed = withLegend ? state.keyEntries.filter((k) => k.letter && !state.hiddenSpecies.has(k.key)) : [];
  const keyCols = 3;
  const keyRows = Math.ceil(keyed.length / keyCols);
  const legendH = withLegend ? Math.round((54 + keyRows * 15) * r) : 0;
  const footerH = Math.round(40 * r);
  const out = document.createElement("canvas");
  out.width = mapCanvas.width;
  out.height = headerH + mapCanvas.height + legendH + footerH;
  const ctx = out.getContext("2d");
  if (!ctx) throw new Error("canvas unavailable");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, out.width, out.height);
  const font = (px: number, weight: string) => `${weight} ${Math.round(px * r)}px system-ui, -apple-system, "Segoe UI", sans-serif`;
  ctx.fillStyle = "#0b2538";
  ctx.font = font(17, "600");
  ctx.textBaseline = "top";
  ctx.fillText(title, pad, Math.round(9 * r), out.width - 2 * pad);
  ctx.font = font(11, "400");
  ctx.fillStyle = "#4b6173";
  const metric = metricById(state.selectedMetric);
  const sub = `${state.dataset ? state.dataset.title + " · " : ""}release ${manifest.current_version} · basins: ${metric.label}${state.selection ? ` · species: ${state.selection.label}` : ""}`;
  ctx.fillText(sub, pad, Math.round(29 * r), out.width - 2 * pad);
  ctx.drawImage(mapCanvas, 0, headerH);
  ctx.strokeStyle = "#d3dfe8";
  ctx.lineWidth = Math.max(1, r);
  ctx.strokeRect(0, headerH, mapCanvas.width, mapCanvas.height);

  let y = headerH + mapCanvas.height + Math.round(10 * r);
  if (withLegend) {
    // Choropleth ramp
    const domain = state.metricDomains[`${activeLevelKey()}:${metric.id}`];
    const x0 = pad;
    ctx.fillStyle = "#13222e";
    ctx.font = font(11, "600");
    ctx.fillText(`${metric.label}${metric.kind !== "count" && metric.unit ? ` (${metric.unit})` : ""}`, x0, y);
    if (domain && state.showBasins) {
      const stops = legendStops(metric, domain);
      const w = Math.round(180 * r);
      const h = Math.round(9 * r);
      const grad = ctx.createLinearGradient(x0, 0, x0 + w, 0);
      stops.forEach((st: any, i: number) => grad.addColorStop(i / (stops.length - 1), st.color));
      ctx.fillStyle = grad;
      ctx.fillRect(x0, y + Math.round(16 * r), w, h);
      ctx.fillStyle = "#4b6173";
      ctx.font = font(9.5, "400");
      stops.forEach((st: any, i: number) => {
        const tx = x0 + (w * i) / (stops.length - 1);
        ctx.textAlign = i === 0 ? "left" : i === stops.length - 1 ? "right" : "center";
        ctx.fillText(st.label, tx, y + Math.round(28 * r));
      });
      ctx.textAlign = "left";
    }
    // Scale bar
    const center = state.mapInstance.getCenter();
    const mpp = metresPerPixel(center.lat, state.mapInstance.getZoom()) / r;
    const bar = scaleBar(mpp, Math.round(120 * r));
    const bx = out.width - pad - bar.px;
    const by = y + Math.round(18 * r);
    ctx.strokeStyle = "#13222e";
    ctx.lineWidth = Math.max(1.5, 1.5 * r);
    ctx.beginPath();
    ctx.moveTo(bx, by - 5 * r);
    ctx.lineTo(bx, by);
    ctx.lineTo(bx + bar.px, by);
    ctx.lineTo(bx + bar.px, by - 5 * r);
    ctx.stroke();
    ctx.fillStyle = "#13222e";
    ctx.font = font(10, "400");
    ctx.textAlign = "center";
    ctx.fillText(`${bar.label} (at map centre)`, bx + bar.px / 2, by + Math.round(4 * r));
    ctx.textAlign = "left";
    // Species key
    let ky = y + Math.round(46 * r);
    const colW = (out.width - 2 * pad) / keyCols;
    keyed.forEach((k, i) => {
      const cx = pad + (i % keyCols) * colW;
      const cy = ky + Math.floor(i / keyCols) * Math.round(15 * r);
      ctx.fillStyle = k.color;
      ctx.beginPath();
      ctx.arc(cx + 6 * r, cy + 6 * r, 6 * r, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#ffffff";
      ctx.font = font(8.5, "700");
      ctx.textAlign = "center";
      ctx.fillText(k.letter, cx + 6 * r, cy + 1.5 * r);
      ctx.textAlign = "left";
      ctx.fillStyle = "#13222e";
      ctx.font = font(10, "italic 400");
      ctx.fillText(k.name, cx + 16 * r, cy + 1 * r, colW - 20 * r);
    });
    y += legendH;
  }
  // Attribution footer
  ctx.fillStyle = "#4b6173";
  ctx.font = font(9, "400");
  const bm = state.basemap !== "none" ? BASEMAPS.find((b) => b.id === state.basemap) : null;
  const bmAttr = bm ? String((bm as any).attribution || "").replace(/<[^>]+>/g, "") : "";
  const line1 = `Data: GBIF.org occurrence download doi:${manifest.provenance?.gbif_doi || ""} · HydroBASINS (Lehner & Grill 2013)${manifest.environment ? " · BasinATLAS (Linke et al. 2019, CC BY 4.0)" : ""}`;
  const line2 = `${bmAttr ? `Background: ${bmAttr} · ` : ""}Made with basinmap.org on ${new Date().toISOString().slice(0, 10)}`;
  ctx.fillText(line1, pad, y, out.width - 2 * pad);
  ctx.fillText(line2, pad, y + Math.round(13 * r), out.width - 2 * pad);
  return out;
}

function bindStage10Controls(): void {
  getEl("report-close")?.addEventListener("click", () => closeReportDrawer());
  getEl("report-print")?.addEventListener("click", () => {
    document.body.classList.add("printing-report");
    window.print();
    setTimeout(() => document.body.classList.remove("printing-report"), 500);
  });
  getEl("btn-export")?.addEventListener("click", () => openExportModal());
  getEl("export-cancel")?.addEventListener("click", () => closeExportModal());
  getEl("export-form")?.addEventListener("submit", (e: any) => {
    e.preventDefault();
    exportMap();
  });
  document.addEventListener("keydown", (e: any) => {
    if (e.key !== "Escape") return;
    if (!getEl("export-modal")?.classList.contains("hidden")) closeExportModal();
    else if (!getEl("report-drawer")?.classList.contains("hidden")) closeReportDrawer();
  });
}

function renderOccurrenceDetails(props: OccurrenceProperties, isProvisional: boolean): void {
  const el = getEl("occurrence-details");
  if (!el) return;

  const badge = isProvisional
    ? `<span class="provisional-badge">Approved Local Observation (Pending GBIF Indexing)</span>`
    : `<span class="snapshot-badge">GBIF Snapshot (${escapeHtml(state.manifest?.current_version || "")})</span>`;

  el.innerHTML = `
    <div style="margin-bottom:0.5rem;">${badge}</div>
    <p style="margin:0.35rem 0;"><strong><em>${escapeHtml(props.scientificName)}</em></strong></p>
    <div class="muted-note">
      ${Number((props as any).count || 1) > 1 ? `<div><strong>${escapeHtml((props as any).count)} records at this location</strong> (showing one)</div>` : ""}
      <div><strong>occurrenceID:</strong> <code>${escapeHtml(props.occurrenceID)}</code></div>
      ${props.gbifID ? `<div><a href="https://www.gbif.org/occurrence/${escapeHtml(props.gbifID)}" target="_blank" rel="noopener noreferrer">View on GBIF</a></div>` : ""}
      <div><strong>Event Date:</strong> ${escapeHtml(props.eventDate)}</div>
      <div><strong>Basis of Record:</strong> ${escapeHtml(props.basisOfRecord)}</div>
      <div><strong>Institution:</strong> ${escapeHtml(props.institutionCode || "Community contributor")}</div>
      <div><strong>Basin ID:</strong> <code>${escapeHtml(props.hybas_id || "—")}</code></div>
      <div><strong>License:</strong> ${escapeHtml(props.license)}</div>
    </div>
  `;
}

function getBasinFillColor(props: BasinScalarProperties, metric: string, maxVal: number): string {
  const raw = Number((props as any)[metric] || 0);
  const ratio = maxVal > 0 ? Math.min(1, raw / maxVal) : 0;
  const stops = ["#d8f3dc", "#95d5b2", "#52b788", "#2d6a4f", "#1b4332"];
  const idx = Math.min(stops.length - 1, Math.floor(ratio * stops.length));
  return stops[idx];
}

function filterOccurrenceFeatures(features: GeoJsonFeature[]): GeoJsonFeature[] {
  return features.filter((feat) => {
    const p: any = feat.properties || {};
    const yr = Number(p.year || 0);
    if (yr && (yr < state.minYear || yr > state.maxYear)) {
      return false;
    }
    if (state.selectedSpecies && p.scientificName !== state.selectedSpecies) {
      return false;
    }
    return true;
  });
}

function renderSvgMapFallback(): void {
  const container = getEl("svg-map-container");
  if (!container || !state.manifest) return;

  const [minLon, minLat, maxLon, maxLat] = state.manifest.spatial_coverage.bbox;
  const padLon = (maxLon - minLon) * 0.08;
  const padLat = (maxLat - minLat) * 0.08;
  const x0 = minLon - padLon;
  const x1 = maxLon + padLon;
  const y0 = minLat - padLat;
  const y1 = maxLat + padLat;
  const width = 900;
  const height = 650;

  const projX = (lon: number) => ((lon - x0) / (x1 - x0)) * width;
  const projY = (lat: number) => height - ((lat - y0) / (y1 - y0)) * height;

  const maxMetricVal = Math.max(
    1,
    ...state.basinsGeojson.map((f) => Number((f.properties as any)[state.selectedMetric] || 0))
  );

  const basinPaths = state.showBasins
    ? state.basinsGeojson
        .map((feat, idx) => {
          const props = feat.properties as unknown as BasinScalarProperties;
          const rings: number[][][] =
            feat.geometry.type === "Polygon"
              ? (feat.geometry.coordinates as number[][][])
              : (feat.geometry.coordinates as number[][][][])[0];
          const d = rings
            .map((ring) =>
              ring
                .map(
                  (pt, ptIdx) =>
                    `${ptIdx === 0 ? "M" : "L"}${projX(pt[0]).toFixed(1)},${projY(pt[1]).toFixed(1)}`
                )
                .join(" ") + " Z"
            )
            .join(" ");
          const fill = getBasinFillColor(props, state.selectedMetric, maxMetricVal);
          return `<path class="svg-basin" data-basin-idx="${idx}" d="${d}" fill="${fill}" fill-opacity="0.82" stroke="#0b2538" stroke-width="1.5" style="cursor:pointer;" />`;
        })
        .join("")
    : "";

  const snapPoints = state.showOccurrences
    ? filterOccurrenceFeatures(state.snapshotOccurrences)
        .map((feat) => {
          const coords = feat.geometry.coordinates as number[];
          const cx = projX(coords[0]).toFixed(1);
          const cy = projY(coords[1]).toFixed(1);
          const occId = escapeHtml((feat.properties as any).occurrenceID);
          return `<circle class="svg-occ" data-occ-id="${occId}" data-provisional="false" cx="${cx}" cy="${cy}" r="4.5" fill="#0284c7" stroke="#ffffff" stroke-width="1.2" style="cursor:pointer;" />`;
        })
        .join("")
    : "";

  const approvedPoints = state.showApproved
    ? filterOccurrenceFeatures(state.approvedOccurrences)
        .map((feat) => {
          const coords = feat.geometry.coordinates as number[];
          const cx = projX(coords[0]).toFixed(1);
          const cy = projY(coords[1]).toFixed(1);
          const occId = escapeHtml((feat.properties as any).occurrenceID);
          return `<circle class="svg-occ" data-occ-id="${occId}" data-provisional="true" cx="${cx}" cy="${cy}" r="6" fill="#d97706" stroke="#ffffff" stroke-width="1.5" style="cursor:pointer;" />`;
        })
        .join("")
    : "";

  container.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" width="100%" height="100%" preserveAspectRatio="xMidYMid meet" style="background:#d9e8f2;">
      <g>${basinPaths}</g>
      <g>${snapPoints}</g>
      <g>${approvedPoints}</g>
    </svg>
  `;

  container.querySelectorAll(".svg-basin").forEach((el: Element) => {
    el.addEventListener("click", () => {
      const idx = Number(el.getAttribute("data-basin-idx"));
      const feat = state.basinsGeojson[idx];
      if (feat) {
        loadAndRenderBasinShard(feat.properties as unknown as BasinScalarProperties);
      }
    });
  });

  container.querySelectorAll(".svg-occ").forEach((el: Element) => {
    el.addEventListener("click", (evt: Event) => {
      evt.stopPropagation();
      const occId = el.getAttribute("data-occ-id");
      const isProv = el.getAttribute("data-provisional") === "true";
      const pool = isProv ? state.approvedOccurrences : state.snapshotOccurrences;
      const found = pool.find((f) => (f.properties as any).occurrenceID === occId);
      if (found) {
        renderOccurrenceDetails(found.properties as unknown as OccurrenceProperties, isProv);
      }
    });
  });
}

function initMapLibreIfAvailable(): boolean {
  const win: any = window;
  const maplibre = win.maplibregl;
  const pmtilesLib = win.pmtiles;
  const manifest = state.manifest;
  if (!maplibre || !pmtilesLib || !manifest) {
    return false;
  }

  try {
    const protocol = new pmtilesLib.Protocol();
    maplibre.addProtocol("pmtiles", protocol.tile);

    const approvedUrl = resolveDataUrl(
      state.publicDataUrl,
      manifest.overlays.approved_observations.path
    );

    // One vector source per CDN-size partition (< 500 MB each, see release manifest).
    const sources: Record<string, any> = {};
    const rasterLayers: any[] = [];
    const basinLayers: any[] = [];
    const occLayers: any[] = [];
    const ids = { basinsFill: [] as string[], basinsOutline: [] as string[], occurrences: [] as string[], bins: [] as string[], letters: [] as string[], speciesBins: [] as string[] };
    const speciesBinsSpec: any = (manifest.layers as any).species_bins;
    const sourceSpec = (path: string, part: { min_zoom: number; max_zoom: number; bounds?: number[] }) => {
      const spec: any = {
        url: `pmtiles://${resolveDataUrl(state.publicDataUrl, path)}`,
        minzoom: part.min_zoom,
        maxzoom: part.max_zoom,
        attribution: manifest.attribution,
      };
      if (part.bounds) spec.bounds = part.bounds;
      return spec;
    };

    (manifest.layers.rasters || []).forEach((raster) => {
      layerPartitions(raster).forEach((part, i) => {
        const sid = `raster-${raster.id}-${i}`;
        sources[sid] = { type: "raster", tileSize: raster.tile_size || 256, ...sourceSpec(part.path, part), attribution: raster.attribution };
        rasterLayers.push({ id: `${sid}-layer`, type: "raster", source: sid, paint: { "raster-opacity": 0.55 } });
      });
    });

    // HydroBASINS levels (Stage 10): the primary level from layers.basins plus any extra levels
    // (3 and 8), each with its own fill/outline layers so they can switch by zoom.
    state.levels = basinLevels(manifest);
    state.levelLayers = {};
    state.levels.forEach((lvl) => {
      const entry = { fill: [] as string[], outline: [] as string[] };
      state.levelLayers[lvl.key] = entry;
      const parts = lvl.primary ? layerPartitions(manifest.layers.basins) : lvl.pmtiles;
      parts.forEach((part: any, i: number) => {
        const sid = lvl.primary ? `basins-${i}` : `basins-l${lvl.key}-${i}`;
        sources[sid] = { type: "vector", ...sourceSpec(part.path, part) };
        const fillId = lvl.primary ? (i === 0 ? "basins-fill" : `basins-fill-${i}`) : `basins-l${lvl.key}-fill-${i}`;
        const outlineId = lvl.primary ? (i === 0 ? "basins-outline" : `basins-outline-${i}`) : `basins-l${lvl.key}-outline-${i}`;
        ids.basinsFill.push(fillId);
        ids.basinsOutline.push(outlineId);
        entry.fill.push(fillId);
        entry.outline.push(outlineId);
        basinLayers.push(
          {
            id: fillId,
            type: "fill",
            source: sid,
            "source-layer": lvl.sourceLayer || "basins",
            metadata: { level: lvl.key },
            paint: { "fill-color": levelFillExpression(lvl.key), "fill-opacity": 0.72 },
          },
          {
            id: outlineId,
            type: "line",
            source: sid,
            "source-layer": lvl.sourceLayer || "basins",
            metadata: { level: lvl.key },
            paint: {
              "line-color": "#0b2538",
              "line-width": ["interpolate", ["linear"], ["zoom"], 2, lvl.level <= 3 ? 0.8 : 0.3, 10, lvl.level <= 3 ? 2.2 : 1.4],
            },
          }
        );
      });
    });

    layerPartitions(manifest.layers.occurrences).forEach((part, i) => {
      const sid = `occurrences-${i}`;
      sources[sid] = { type: "vector", ...sourceSpec(part.path, part) };
      const binId = `occurrence-bins-${i}`;
      const occId = i === 0 ? "occurrences-circle" : `occurrences-circle-${i}`;
      const letterId = `occurrences-letter-${i}`;
      ids.bins.push(binId);
      ids.occurrences.push(occId);
      ids.letters.push(letterId);
      if (speciesBinsSpec) {
        // Per-species overview points (Stage 9 releases): shown instead of count bins while searching.
        const sbId = `species-bins-${i}`;
        ids.speciesBins.push(sbId);
        occLayers.push({
          id: sbId,
          type: "circle",
          source: sid,
          "source-layer": speciesBinsSpec.source_layer || "species_bins",
          maxzoom: (speciesBinsSpec.max_zoom ?? 8) + 1,
          layout: { visibility: "none" },
          paint: {
            "circle-radius": ["interpolate", ["linear"], ["ln", ["+", 1, ["get", "count"]]], 0, 3.5, 8, 11],
            "circle-color": OCCURRENCE_COLOR,
            "circle-opacity": 0.85,
            "circle-stroke-color": "#ffffff",
            "circle-stroke-width": 0.8,
          },
        });
      }
      occLayers.push(
        {
          id: binId,
          type: "circle",
          source: sid,
          "source-layer": "occurrence_bins",
          paint: {
            "circle-radius": ["interpolate", ["linear"], ["ln", ["+", 1, ["get", "count"]]], 0, 2.5, 10, 14],
            "circle-color": "#0284c7",
            "circle-opacity": 0.55,
            "circle-stroke-color": "#ffffff",
            "circle-stroke-width": 0.6,
          },
        },
        {
          id: occId,
          type: "circle",
          source: sid,
          "source-layer": "occurrences",
          paint: {
            "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 2.5, 13, 5],
            "circle-color": OCCURRENCE_COLOR,
            "circle-stroke-color": "#ffffff",
            "circle-stroke-width": 1.2,
          },
        },
        {
          // Species-key letters (A, B, C...) over points once zoomed in; hidden until a selection is active.
          id: letterId,
          type: "symbol",
          source: sid,
          "source-layer": "occurrences",
          minzoom: 9,
          layout: {
            visibility: "none",
            "text-field": "",
            "text-font": LETTER_FONT,
            "text-size": 9,
            "text-allow-overlap": true,
            "text-ignore-placement": true,
          },
          paint: { "text-color": "#ffffff" },
        }
      );
    });
    state.mapLayerIds = ids;

    sources.approved = {
      type: "geojson",
      data: {
        type: "FeatureCollection",
        features: state.approvedOccurrences,
      },
    };

    const map = new maplibre.Map({
      container: "maplibre-container",
      bounds: manifest.spatial_coverage.bbox,
      fitBoundsOptions: { padding: 36 },
      style: {
        version: 8,
        glyphs: GLYPHS_URL,
        sources: { ...basemapSources(), ...sources },
        layers: [
          {
            id: "background",
            type: "background",
            paint: { "background-color": "#d9e8f2" },
          },
          ...basemapLayers(state.basemap),
          ...rasterLayers,
          ...basinLayers,
          ...occLayers,
          {
            id: "approved-circle",
            type: "circle",
            source: "approved",
            paint: {
              "circle-radius": 6.5,
              "circle-color": "#d97706",
              "circle-stroke-color": "#ffffff",
              "circle-stroke-width": 1.5,
            },
          },
        ],
      },
    });

    // Registered first so a coordinate-picking click is consumed before layer handlers run.
    map.on("click", (e: any) => {
      if (handleMapClickForContrib(e) && e.originalEvent) e.originalEvent.bmConsumed = true;
    });
    const consumed = (e: any) => Boolean(e.originalEvent && e.originalEvent.bmConsumed);

    Object.keys(state.levelLayers).forEach((levelKey) => {
      state.levelLayers[levelKey].fill.forEach((layerId: string) => {
        map.on("click", layerId, (e: any) => {
          if (consumed(e)) return;
          const feature = e.features && e.features[0];
          if (feature && feature.properties) {
            loadAndRenderBasinShard(feature.properties as BasinScalarProperties, levelKey);
          }
        });
      });
    });

    ids.occurrences.forEach((layerId) => {
      map.on("click", layerId, (e: any) => {
        if (consumed(e)) return;
        const feature = e.features && e.features[0];
        if (feature && feature.properties) {
          renderOccurrenceDetails(feature.properties as OccurrenceProperties, false);
        }
      });
    });

    ids.bins.forEach((layerId) => {
      map.on("click", layerId, (e: any) => {
        if (consumed(e)) return;
        map.easeTo({ center: e.lngLat, zoom: Math.min(map.getZoom() + 2, (manifest.layers.occurrence_bins_max_zoom ?? 8) + 1) });
      });
    });

    map.on("click", "approved-circle", (e: any) => {
      if (consumed(e)) return;
      const feature = e.features && e.features[0];
      if (feature && feature.properties) {
        renderOccurrenceDetails(feature.properties as OccurrenceProperties, true);
      }
    });

    // Only a failure before the first render (e.g. no WebGL) switches to the SVG fallback. Tile and
    // glyph errors (a basemap provider being slow or offline) must not tear down a working map.
    let mapLoaded = false;
    map.on("load", () => {
      mapLoaded = true;
      syncMapFiltersAndLayers();
    });
    // Recolour basins to the values in view and keep the level note current.
    let choroplethTimer: any = null;
    const scheduleChoropleth = () => {
      if (choroplethTimer) clearTimeout(choroplethTimer);
      choroplethTimer = setTimeout(() => updateChoropleth(), 250);
    };
    map.on("moveend", scheduleChoropleth);
    let idleCount = 0;
    map.on("idle", () => {
      // The first idle events come once tiles have loaded: compute the initial colour domain then.
      if (idleCount < 2) scheduleChoropleth();
      idleCount += 1;
    });
    map.on("zoomend", () => renderLevelNote());
    map.on("error", (e: any) => {
      if (mapLoaded || (e && (e.sourceId || e.tile))) return;
      switchToSvgFallback("MapLibre runtime error — interactive SVG active");
    });

    map.addControl(new maplibre.NavigationControl({ visualizePitch: false }), "top-right");
    map.addControl(new maplibre.FullscreenControl(), "top-right");
    map.addControl(new maplibre.ScaleControl({ unit: "metric", maxWidth: 120 }), "bottom-left");
    attachSpeciesTooltip(map, maplibre, [...ids.occurrences, ...ids.speciesBins, "approved-circle"]);

    state.mapInstance = map;
    state.usingMapLibre = true;
    // Read-only handle for diagnostics and browser tests (e.g. basinmap.map.queryRenderedFeatures()).
    (window as any).basinmap = { map, state };
    const hud = getEl("hud-engine");
    if (hud) {
      hud.textContent = `Map engine: MapLibre GL JS + PMTiles (${approvedUrl ? "live" : "ready"})`;
    }
    return true;
  } catch (_err) {
    return false;
  }
}

function switchToSvgFallback(reason: string): void {
  state.usingMapLibre = false;
  const ml = getEl("maplibre-container");
  const svg = getEl("svg-map-container");
  const hud = getEl("hud-engine");
  if (ml) ml.classList.add("hidden");
  if (svg) svg.classList.remove("hidden");
  if (hud) hud.textContent = `Map engine: ${reason}`;
  renderSvgMapFallback();
}

/** Default the year filter to the release's own span (all records visible on first load). Releases
 * built before temporal_coverage existed fall back to 1900 through the current year. */
function applyReleaseYearRange(manifest: ReleaseManifest): void {
  const span = manifest.temporal_coverage;
  const thisYear = new Date().getFullYear();
  state.minYear = span && span.min_year > 0 ? span.min_year : 1900;
  state.maxYear = span && span.max_year > 0 ? span.max_year : thisYear;
  const yearMin = getEl("filter-year-min");
  const yearMax = getEl("filter-year-max");
  if (yearMin) yearMin.value = String(state.minYear);
  if (yearMax) yearMax.value = String(state.maxYear);
}

function syncMapFiltersAndLayers(): void {
  if (state.usingMapLibre && state.mapInstance) {
    const map = state.mapInstance;
    const occVis = state.showOccurrences ? "visible" : "none";
    const appVis = state.showApproved ? "visible" : "none";

    state.levelRanges = levelZoomRanges(state.levels, state.levelMode);
    for (const levelKey of Object.keys(state.levelLayers)) {
      const range = state.levelRanges[levelKey] || [0, 24];
      const shown = state.showBasins && range[1] > range[0];
      const entry = state.levelLayers[levelKey];
      for (const layerId of [...entry.fill, ...entry.outline]) {
        if (!map.getLayer(layerId)) continue;
        map.setLayoutProperty(layerId, "visibility", shown ? "visible" : "none");
        if (shown) map.setLayerZoomRange(layerId, range[0], Math.min(24, range[1]));
      }
      for (const layerId of entry.fill) {
        if (map.getLayer(layerId)) map.setPaintProperty(layerId, "fill-color", levelFillExpression(levelKey));
      }
    }
    renderLevelNote();
    renderChoroplethLegend();

    // Undated records (year 0, common for old museum specimens) are never hidden by the year range,
    // matching the list filter.
    const filterExpr: any[] = [
      "all",
      [
        "any",
        ["==", ["get", "year"], 0],
        ["all", [">=", ["get", "year"], state.minYear], ["<=", ["get", "year"], state.maxYear]],
      ],
    ];
    if (state.selectedSpecies) {
      filterExpr.push(["==", ["get", "scientificName"], state.selectedSpecies]);
    }
    const taxonFilter = selectionFilter(state.keyEntries, state.hiddenSpecies, state.useSpeciesKey);
    // Approved overlay points carry scientificName only, so they always use name matching.
    const approvedTaxonFilter = selectionFilter(state.keyEntries, state.hiddenSpecies, false);
    const filtering = Boolean(state.selectedSpecies || taxonFilter);
    const occFilter = taxonFilter ? [...filterExpr, taxonFilter] : filterExpr;
    const approvedFilter = approvedTaxonFilter ? [...filterExpr, approvedTaxonFilter] : filterExpr;
    const keyed = state.keyEntries.length > 0;

    for (const layerId of state.mapLayerIds.occurrences) {
      if (map.getLayer(layerId)) {
        map.setLayoutProperty(layerId, "visibility", occVis);
        map.setFilter(layerId, occFilter);
        map.setPaintProperty(layerId, "circle-color", colorExpression(state.keyEntries, state.useSpeciesKey, OCCURRENCE_COLOR));
        map.setPaintProperty(layerId, "circle-radius", keyed
          ? ["interpolate", ["linear"], ["zoom"], 4, 3.5, 9, 6, 13, 8]
          : ["interpolate", ["linear"], ["zoom"], 8, 2.5, 13, 5]);
      }
    }
    for (const layerId of state.mapLayerIds.letters) {
      if (map.getLayer(layerId)) {
        map.setLayoutProperty(layerId, "visibility", keyed && state.showOccurrences ? "visible" : "none");
        map.setLayoutProperty(layerId, "text-field", letterExpression(state.keyEntries, state.useSpeciesKey));
        map.setFilter(layerId, occFilter);
      }
    }
    const speciesBinFilter = selectionFilter(state.keyEntries, state.hiddenSpecies, true);
    for (const layerId of state.mapLayerIds.speciesBins) {
      if (map.getLayer(layerId)) {
        map.setLayoutProperty(layerId, "visibility", keyed && state.showOccurrences ? "visible" : "none");
        if (speciesBinFilter) map.setFilter(layerId, speciesBinFilter);
        map.setPaintProperty(layerId, "circle-color", colorExpression(state.keyEntries, true, OCCURRENCE_COLOR));
      }
    }
    // Aggregated bins carry counts only; hide them while a species/taxon filter is active.
    for (const layerId of state.mapLayerIds.bins) {
      if (map.getLayer(layerId)) {
        map.setLayoutProperty(layerId, "visibility", filtering ? "none" : occVis);
      }
    }
    if (map.getLayer("approved-circle")) {
      map.setLayoutProperty("approved-circle", "visibility", appVis);
      map.setFilter("approved-circle", approvedFilter);
    }
    for (const b of BASEMAPS) {
      const id = `basemap-${b.id}`;
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", b.id === state.basemap ? "visible" : "none");
    }
  } else {
    renderSvgMapFallback();
  }
}

function bindControls(): void {
  const toggleBasins = getEl("toggle-basins");
  const toggleOcc = getEl("toggle-occurrences");
  const toggleApp = getEl("toggle-approved");
  const selectMetric = getEl("select-metric");
  const filterSpecies = getEl("filter-species");
  const yearMin = getEl("filter-year-min");
  const yearMax = getEl("filter-year-max");

  if (toggleBasins) {
    toggleBasins.addEventListener("change", () => {
      state.showBasins = Boolean(toggleBasins.checked);
      syncMapFiltersAndLayers();
    });
  }
  if (toggleOcc) {
    toggleOcc.addEventListener("change", () => {
      state.showOccurrences = Boolean(toggleOcc.checked);
      syncMapFiltersAndLayers();
    });
  }
  if (toggleApp) {
    toggleApp.addEventListener("change", () => {
      state.showApproved = Boolean(toggleApp.checked);
      syncMapFiltersAndLayers();
    });
  }
  if (selectMetric) {
    selectMetric.addEventListener("change", () => {
      state.selectedMetric = selectMetric.value;
      syncMapFiltersAndLayers();
      updateChoropleth();
    });
  }
  const selectLevel = getEl("select-basin-level");
  if (selectLevel) {
    selectLevel.addEventListener("change", () => {
      state.levelMode = selectLevel.value || "auto";
      try {
        window.localStorage.setItem("bm.levels", state.levelMode);
      } catch (_err) {
        // Storage unavailable: the choice lasts for this visit.
      }
      syncMapFiltersAndLayers();
      updateChoropleth();
    });
  }
  if (filterSpecies) {
    filterSpecies.addEventListener("change", () => {
      state.selectedSpecies = filterSpecies.value;
      syncMapFiltersAndLayers();
    });
  }
  if (yearMin) {
    yearMin.addEventListener("change", () => {
      state.minYear = Number(yearMin.value || 1900);
      syncMapFiltersAndLayers();
    });
  }
  if (yearMax) {
    yearMax.addEventListener("change", () => {
      state.maxYear = Number(yearMax.value || 2030);
      syncMapFiltersAndLayers();
    });
  }
}

// ------------------------------------------------------------------------------------------------
// Stage 9: taxon search, species key, grouped tooltips, basemaps, datasets
// ------------------------------------------------------------------------------------------------

function attachSpeciesTooltip(map: any, maplibre: any, layerIds: string[]): void {
  const popup = new maplibre.Popup({ closeButton: false, closeOnClick: false, offset: 10, className: "species-tip" });
  const onMove = (e: any) => {
    const box = [[e.point.x - 6, e.point.y - 6], [e.point.x + 6, e.point.y + 6]];
    const layers = layerIds.filter((id) => map.getLayer(id));
    const feats = layers.length ? map.queryRenderedFeatures(box, { layers }) : [];
    if (!feats.length) {
      popup.remove();
      map.getCanvas().style.cursor = "";
      return;
    }
    map.getCanvas().style.cursor = "pointer";
    const groups = groupBySpecies(feats, state.keyEntries).slice(0, 8);
    const total = groups.reduce((acc: number, g: any) => acc + g.count, 0);
    const rows = groups
      .map((g: any) => `<li><span class="key-dot" style="background:${g.color}">${escapeHtml(g.letter)}</span><em>${escapeHtml(g.label)}</em> <span class="muted">×${g.count}</span></li>`)
      .join("");
    popup
      .setLngLat(e.lngLat)
      .setHTML(`<div class="tip-head">${total} record${total === 1 ? "" : "s"} here</div><ul class="tip-list">${rows}</ul>`)
      .addTo(map);
  };
  map.on("mousemove", onMove);
  map.on("mouseout", () => popup.remove());
}

function selectedRows(): SpeciesRow[] {
  if (!state.selection) return [];
  const keys = new Set<number>(state.selection.speciesKeys);
  return state.speciesRows.filter((r) => keys.has(r.key));
}

function applySelection(entry: TaxonEntry | null): void {
  state.selection = entry;
  state.hiddenSpecies = new Set<number>();
  const rows = selectedRows();
  state.keyEntries = entry ? assignSpeciesKey(rows, state.synonyms) : [];
  const input = getEl("taxon-search");
  if (input) input.value = entry ? entry.label : "";
  renderSpeciesKey();
  syncMapFiltersAndLayers();
  if (entry) fitToSelection();
}

function fitToSelection(): void {
  const map = state.mapInstance;
  if (!state.usingMapLibre || !map) return;
  // Only move when the release knows where the selection occurs (species_index bbox, Stage 9 builds);
  // falling back to the whole release extent would zoom the user out of the area they are studying.
  const own: any = state.selection ? (state.selection as any).bbox : null; // country or basin selections
  const bb = Array.isArray(own) && own.length === 4 ? own : state.selection ? selectionBounds(selectedRows()) : null;
  if (!bb) return;
  const pad = bb[0] === bb[2] && bb[1] === bb[3] ? 0.5 : 0;
  map.fitBounds([[bb[0] - pad, bb[1] - pad], [bb[2] + pad, bb[3] + pad]], { padding: 48, maxZoom: 9, duration: 800 });
}

function renderSpeciesKey(): void {
  const card = getEl("species-key-card");
  const body = getEl("species-key");
  if (!card || !body) return;
  if (!state.selection) {
    card.classList.add("hidden");
    body.innerHTML = "";
    return;
  }
  card.classList.remove("hidden");
  const rows = selectedRows();
  const lettered = state.keyEntries.filter((k) => k.letter);
  const others = state.keyEntries.filter((k) => !k.letter);
  const truncated = others.length > 0;
  const items = lettered
    .map((k) => {
      const off = state.hiddenSpecies.has(k.key);
      return `<li><button type="button" class="key-row${off ? " off" : ""}" data-key="${k.key}" aria-pressed="${!off}">
        <span class="key-dot" style="background:${k.color}">${k.letter}</span>
        <em>${escapeHtml(k.name)}</em><span class="muted">${k.count.toLocaleString("en-US")}</span></button></li>`;
    })
    .join("") + (others.length
      ? `<li><div class="key-row key-other"><span class="key-dot" style="background:${others[0].color}"></span>
        <span>${others.length} other species</span><span class="muted">${others.reduce((n, k) => n + k.count, 0).toLocaleString("en-US")}</span></div></li>`
      : "");
  body.innerHTML = `
    <p class="muted-note"><strong>${escapeHtml(state.selection.label)}</strong> · ${escapeHtml(state.selection.kind)} ·
      ${rows.length} species${truncated ? ` (letters for the ${lettered.length} most recorded; the rest in grey)` : ""}</p>
    ${state.mapLayerIds.speciesBins.length ? "" : `<p class="muted-note">This release shows individual records from zoom ${(state.manifest?.layers.occurrence_bins_max_zoom ?? 8) + 1}; zoom in to see them.</p>`}
    <ul class="species-key">${items}</ul>
    <div class="key-actions">
      <button type="button" class="btn btn-ghost" id="key-show-all">Show all</button>
      ${selectionBounds(rows) || (state.selection as any).bbox ? `<button type="button" class="btn btn-ghost" id="key-fit">Zoom to results</button>` : ""}
      <button type="button" class="btn btn-ghost" id="key-clear">Clear</button>
    </div>`;
  body.querySelectorAll(".key-row").forEach((btn: any) => {
    btn.addEventListener("click", () => {
      const key = Number(btn.getAttribute("data-key"));
      if (state.hiddenSpecies.has(key)) state.hiddenSpecies.delete(key);
      else state.hiddenSpecies.add(key);
      renderSpeciesKey();
      syncMapFiltersAndLayers();
    });
  });
  getEl("key-show-all")?.addEventListener("click", () => {
    state.hiddenSpecies = new Set<number>();
    renderSpeciesKey();
    syncMapFiltersAndLayers();
  });
  getEl("key-fit")?.addEventListener("click", () => fitToSelection());
  getEl("key-clear")?.addEventListener("click", () => applySelection(null));
}

function initTaxonSearch(): void {
  const input = getEl("taxon-search");
  const list = getEl("taxon-results");
  const row = getEl("taxon-search-row");
  if (!input || !list) return;
  if (!state.taxonEntries.length) {
    if (row) row.classList.add("hidden");
    return;
  }
  if (row) row.classList.remove("hidden");
  let results: TaxonEntry[] = [];
  let active = -1;
  const close = () => {
    list.classList.add("hidden");
    list.innerHTML = "";
    input.setAttribute("aria-expanded", "false");
    active = -1;
  };
  const choose = (i: number) => {
    if (results[i]) applySelection(results[i]);
    close();
  };
  const render = () => {
    if (!results.length) {
      close();
      return;
    }
    list.innerHTML = results
      .map((r, i) => `<li role="option" id="taxon-opt-${i}" aria-selected="${i === active}" data-i="${i}" class="${i === active ? "active" : ""}">
          <span class="kind kind-${r.kind}">${escapeHtml(r.kind)}</span>
          <span class="label">${r.kind === "family" || r.kind === "common" ? escapeHtml(r.label) : `<em>${escapeHtml(r.label)}</em>`}</span>
          <span class="detail">${escapeHtml(r.detail)}</span></li>`)
      .join("");
    list.classList.remove("hidden");
    input.setAttribute("aria-expanded", "true");
    list.querySelectorAll("li").forEach((li: any) => {
      li.addEventListener("mousedown", (ev: any) => {
        ev.preventDefault();
        choose(Number(li.getAttribute("data-i")));
      });
    });
  };
  input.addEventListener("input", () => {
    results = searchTaxa(state.taxonEntries, input.value, 12);
    active = results.length ? 0 : -1;
    render();
    if (!input.value.trim() && state.selection) applySelection(null);
  });
  input.addEventListener("keydown", (ev: any) => {
    if (ev.key === "ArrowDown" && results.length) {
      active = (active + 1) % results.length;
      render();
      ev.preventDefault();
    } else if (ev.key === "ArrowUp" && results.length) {
      active = (active - 1 + results.length) % results.length;
      render();
      ev.preventDefault();
    } else if (ev.key === "Enter" && active >= 0) {
      choose(active);
      ev.preventDefault();
    } else if (ev.key === "Escape") {
      close();
    }
  });
  input.addEventListener("blur", () => setTimeout(close, 120));
}

async function loadTaxonomy(manifest: ReleaseManifest): Promise<void> {
  const layers: any = manifest.layers;
  state.useSpeciesKey = Array.isArray(layers.occurrence_point_fields) && layers.occurrence_point_fields.includes("speciesKey");
  if (!layers.species_index) return;
  const [index, aliases] = await Promise.all([
    fetchJsonWithTimeout(resolveDataUrl(state.publicDataUrl, layers.species_index), 15000).catch(() => null),
    layers.taxonomy_aliases
      ? fetchJsonWithTimeout(resolveDataUrl(state.publicDataUrl, layers.taxonomy_aliases), 15000).catch(() => null)
      : Promise.resolve(null),
  ]);
  if (!index) return;
  state.speciesRows = Array.isArray(index.species) ? index.species : [];
  state.synonyms = synonymsByKey(aliases, index);
  state.taxonEntries = buildTaxonEntries(index, aliases);
}

function renderDatasetSelector(): void {
  const row = getEl("dataset-row");
  const select = getEl("select-dataset");
  if (!row || !select) return;
  if (state.datasets.length < 2) {
    row.classList.add("hidden");
    return;
  }
  row.classList.remove("hidden");
  select.innerHTML = state.datasets
    .map((d) => `<option value="${escapeHtml(d.id)}"${state.dataset && d.id === state.dataset.id ? " selected" : ""}>${escapeHtml(d.title)}</option>`)
    .join("");
  const desc = getEl("dataset-description");
  if (desc) desc.textContent = state.dataset ? state.dataset.description : "";
  select.addEventListener("change", () => {
    const url = new URL(window.location.href);
    if (select.value === "default") url.searchParams.delete("dataset");
    else url.searchParams.set("dataset", select.value);
    window.location.assign(url.toString());
  });
}

function renderBasemapSelector(): void {
  const select = getEl("select-basemap");
  if (!select) return;
  select.innerHTML = BASEMAPS.map((b) => `<option value="${b.id}"${b.id === state.basemap ? " selected" : ""}>${escapeHtml(b.label)}</option>`).join("")
    + `<option value="none"${state.basemap === "none" ? " selected" : ""}>None (plain)</option>`;
  select.addEventListener("change", () => {
    state.basemap = select.value;
    try {
      window.localStorage.setItem("bm.basemap", state.basemap);
    } catch (_err) {
      // Storage can be unavailable (private mode); the choice still applies for this visit.
    }
    syncMapFiltersAndLayers();
  });
}

function populateSpeciesFilter(): void {
  const select = getEl("filter-species");
  if (!select) return;
  const names = new Set<string>();
  for (const f of state.snapshotOccurrences) {
    const s = (f.properties as any).scientificName;
    if (typeof s === "string" && s) names.add(s);
  }
  for (const f of state.approvedOccurrences) {
    const s = (f.properties as any).scientificName;
    if (typeof s === "string" && s) names.add(s);
  }
  Array.from(names)
    .sort()
    .forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    });
}

/** Refresh the provisional overlay after a review decision (bypasses the 60 s edge cache once). */
async function reloadApprovedOverlay(): Promise<void> {
  if (!state.manifest) return;
  const url = resolveDataUrl(state.publicDataUrl, state.manifest.overlays.approved_observations.path);
  try {
    const fc = await fetchJsonWithTimeout(`${url}?t=${Date.now()}`, 5000);
    const dedup = deduplicateApprovedOverlay(fc.features || [], state.snapshotOwnIds);
    state.approvedOccurrences = dedup.uniqueFeatures;
    state.deduplicatedCount = dedup.duplicateCount;
    const map = state.mapInstance;
    if (state.usingMapLibre && map && map.getSource("approved")) {
      map.getSource("approved").setData({ type: "FeatureCollection", features: state.approvedOccurrences });
    } else {
      renderSvgMapFallback();
    }
  } catch (_err) {
    // The overlay refreshes on the next page load; the review itself is already saved.
  }
}

async function bootstrap(): Promise<void> {
  bindControls();
  bindStage10Controls();
  const apiStatus = await probeApiStatus();
  state.publicDataUrl = apiStatus.publicDataUrl;

  // Contributor/reviewer UI; independent of map data so it works even if the release fails to load.
  initContributions(
    { getMap: () => (state.usingMapLibre ? state.mapInstance : null), onReviewDecision: reloadApprovedOverlay },
    apiStatus.online
  ).catch(() => undefined);

  try {
    const saved = window.localStorage.getItem("bm.basemap");
    if (saved && (saved === "none" || BASEMAPS.some((b) => b.id === saved))) state.basemap = saved;
  } catch (_err) {
    // Storage unavailable: keep the default basemap.
  }
  renderBasemapSelector();

  // Dataset catalog (optional): several datasets can be offered side by side.
  const catalog = await fetchJsonWithTimeout(resolveDataUrl(state.publicDataUrl, "/releases/datasets.json"), 4000).catch(() => null);
  state.datasets = normalizeCatalog(catalog);
  state.dataset = pickDataset(state.datasets, new URLSearchParams(window.location.search).get("dataset"));
  renderDatasetSelector();

  const manifestUrl = resolveDataUrl(state.publicDataUrl, state.dataset ? state.dataset.manifest_path : "/releases/manifest.json");
  const manifest: ReleaseManifest = await fetchJsonWithTimeout(manifestUrl, 5000);
  state.manifest = manifest;
  applyReleaseYearRange(manifest);
  const taxonomyReady = loadTaxonomy(manifest).then(() => initTaxonSearch()).catch(() => undefined);
  populateBasinControls(manifest);
  loadCountries(manifest).catch(() => undefined);

  const versionBadge = getEl("badge-version");
  const doiBadge = getEl("badge-doi");
  const provText = getEl("provenance-text");
  if (versionBadge) {
    versionBadge.textContent = `Release: ${manifest.current_version}`;
    versionBadge.className = "pill ok";
  }
  if (doiBadge) {
    doiBadge.textContent = `GBIF DOI: ${manifest.provenance.gbif_doi}`;
    doiBadge.href = `https://doi.org/${manifest.provenance.gbif_doi}`;
  }
  if (provText) {
    provText.innerHTML = `
      <strong>Snapshot:</strong> ${escapeHtml(manifest.current_version)} (${escapeHtml(manifest.counts.occurrences)} records,
      ${escapeHtml(manifest.counts.species)} species, ${escapeHtml(manifest.counts.basins)} basins)<br/>
      <strong>Attribution:</strong> ${escapeHtml(manifest.attribution)}<br/>
      <strong>Generated:</strong> ${escapeHtml(manifest.generated_at)}
    `;
  }

  // Probe PMTiles HTTP 206 byte-range delivery directly from R2 / static layer URL.
  const rangeBadge = getEl("badge-range");
  try {
    const basinsProbe = await verifyRangeProbe(
      resolveDataUrl(state.publicDataUrl, manifest.layers.basins.path)
    );
    const occProbe = await verifyRangeProbe(
      resolveDataUrl(state.publicDataUrl, manifest.layers.occurrences.path)
    );
    if (rangeBadge) {
      rangeBadge.textContent = `Range ${basinsProbe.status}: ${basinsProbe.tiles}+${occProbe.tiles} tiles`;
      rangeBadge.className = "pill ok";
    }
  } catch (err: any) {
    if (rangeBadge) {
      rangeBadge.textContent = `Range check: ${err?.message || "failed"}`;
      rangeBadge.className = "pill warn";
    }
  }

  // Load GeoJSON inspection mirrors + approved overlay for filtering, deduplication, and SVG fallback.
  // GeoJSON mirrors are only published for small releases (SVG fallback + species filter);
  // large real releases rely on PMTiles alone.
  const emptyFc = { type: "FeatureCollection", features: [] as GeoJsonFeature[] };
  const basinsGeoPath = manifest.layers.basins.geojson_path;
  const occGeoPath = manifest.layers.occurrences.geojson_path;
  const ownIdsPath = manifest.layers.snapshot_own_occurrence_ids;
  const approvedUrl = resolveDataUrl(
    state.publicDataUrl,
    manifest.overlays.approved_observations.path
  );

  const [basinsFc, occFc, ownIds, approvedFc] = await Promise.all([
    basinsGeoPath
      ? fetchJsonWithTimeout(resolveDataUrl(state.publicDataUrl, basinsGeoPath), 8000).catch(() => emptyFc)
      : Promise.resolve(emptyFc),
    occGeoPath
      ? fetchJsonWithTimeout(resolveDataUrl(state.publicDataUrl, occGeoPath), 8000).catch(() => emptyFc)
      : Promise.resolve(emptyFc),
    ownIdsPath
      ? fetchJsonWithTimeout(resolveDataUrl(state.publicDataUrl, ownIdsPath), 5000).catch(() => [])
      : Promise.resolve([]),
    fetchJsonWithTimeout(approvedUrl, 5000).catch(() => ({
      type: "FeatureCollection",
      features: [],
    } as ApprovedOverlayFeatureCollection)),
  ]);

  state.basinsGeojson = basinsFc.features || [];
  state.snapshotOccurrences = occFc.features || [];

  const snapshotIds = new Set<string>(
    state.snapshotOccurrences
      .map((f) => (f.properties as any).occurrenceID)
      .filter((id): id is string => typeof id === "string")
  );
  // Large releases publish only basinmap.org's own occurrenceIDs found in the GBIF snapshot.
  for (const id of Array.isArray(ownIds) ? ownIds : []) {
    if (typeof id === "string") snapshotIds.add(id);
  }
  state.snapshotOwnIds = snapshotIds;
  const dedup = deduplicateApprovedOverlay(approvedFc.features || [], snapshotIds);
  state.approvedOccurrences = dedup.uniqueFeatures;
  state.deduplicatedCount = dedup.duplicateCount;

  populateSpeciesFilter();
  const speciesRow = getEl("filter-species-row");
  if (speciesRow && state.snapshotOccurrences.length === 0 && state.approvedOccurrences.length === 0) {
    speciesRow.classList.add("hidden");
  }

  const mlReady = initMapLibreIfAvailable();
  await taxonomyReady;
  // The taxon search replaces the old name dropdown when the release has a species index.
  if (speciesRow && state.taxonEntries.length) speciesRow.classList.add("hidden");
  if (!mlReady) {
    switchToSvgFallback("Interactive Vector SVG (PMTiles-backed)");
  }

  // Auto-select the first basin so the inspector immediately demonstrates scalar + shard loading.
  if (state.basinsGeojson.length > 0) {
    loadAndRenderBasinShard(
      state.basinsGeojson[0].properties as unknown as BasinScalarProperties
    );
  }
}

if (typeof document !== "undefined") {
  bootstrap().catch((err) => {
    const hud = getEl("hud-engine");
    if (hud) {
      hud.textContent = `Initialization error: ${err?.message || err}`;
    }
  });
}
