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
};

const OCCURRENCE_COLOR = "#0284c7";

/** Log-scaled basin colour ramp so real release counts (1 to 100k+) stay legible. */
function basinFillExpression(): any[] {
  const maxVal = Math.max(
    14,
    ...state.basinsGeojson.map((f) => Number((f.properties as any)[state.selectedMetric] || 0))
  );
  const top = Math.log(maxVal + 1);
  return [
    "interpolate",
    ["linear"],
    ["ln", ["+", 1, ["to-number", ["get", state.selectedMetric], 0]]],
    0,
    "#d8f3dc",
    top * 0.4,
    "#95d5b2",
    top * 0.7,
    "#52b788",
    top,
    "#1b4332",
  ];
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

function renderBasinScalars(props: BasinScalarProperties): void {
  const container = getEl("basin-scalars");
  if (!container) return;

  container.innerHTML = `
    <div style="margin-bottom: 0.6rem;">
      <strong style="font-size: 1rem;">${escapeHtml(props.basin_name)}</strong>
      <div class="muted-note">
        HYBAS_ID: <code>${escapeHtml(props.hybas_id)}</code> &middot; Pfaf: <code>${escapeHtml(props.pfaf_id)}</code>
      </div>
    </div>
    <div class="metric-grid">
      <div class="metric-tile">
        <div class="metric-label">Occurrences</div>
        <div class="metric-value">${escapeHtml(props.occurrence_count)}</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Species Richness</div>
        <div class="metric-value">${escapeHtml(props.species_count)}</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Threatened (CR/EN/VU/NT)</div>
        <div class="metric-value">${escapeHtml(props.threatened_count)}</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Year Span</div>
        <div class="metric-value">${escapeHtml(props.min_year)}&ndash;${escapeHtml(props.max_year)}</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Sub-basin Area</div>
        <div class="metric-value">${escapeHtml(props.sub_area_km2)} km&sup2;</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Upstream Area</div>
        <div class="metric-value">${escapeHtml(props.up_area_km2)} km&sup2;</div>
      </div>
    </div>
  `;
}

async function loadAndRenderBasinShard(props: BasinScalarProperties): Promise<void> {
  renderBasinScalars(props);
  const shardEl = getEl("basin-shard-details");
  if (!shardEl || !state.manifest) return;

  const shardPath = basinShardPath(state.manifest, props.hybas_id, props.shard_path);
  const shardUrl = resolveDataUrl(state.publicDataUrl, shardPath);
  if (!shardPath.endsWith(".json")) {
    shardEl.innerHTML = `<p class="muted-note" style="margin-top:0.6rem;">Detailed checklist for this sub-basin is in the regional partition <a href="${escapeHtml(shardUrl)}">${escapeHtml(shardPath.split("/").pop())}</a> (SQLite, table <code>basin_shards</code>).</p>`;
    return;
  }
  shardEl.innerHTML = `<p class="muted-note" style="margin-top:0.6rem;">Loading species shard <code>${escapeHtml(props.hybas_id)}.json</code>...</p>`;

  try {
    const shard: BasinShardPayload = await fetchJsonWithTimeout(shardUrl, 5000);
    const maxYearCount = Math.max(
      1,
      ...Object.values(shard.temporal_histogram || { "2020": 1 })
    );

    const speciesRows = shard.species
      .map((sp) => {
        const badgeCls = `iucn-badge iucn-${escapeHtml(sp.iucnRedListCategory || "LC")}`;
        return `
          <tr>
            <td>
              <em>${escapeHtml(sp.scientificName)}</em>
              <div class="muted-note" style="font-size:0.7rem;">${escapeHtml(sp.family)}</div>
            </td>
            <td><span class="${badgeCls}">${escapeHtml(sp.iucnRedListCategory || "NE")}</span></td>
            <td>${escapeHtml(sp.count)}</td>
            <td>${escapeHtml(sp.firstYear)}&ndash;${escapeHtml(sp.lastYear)}</td>
          </tr>
        `;
      })
      .join("");

    const histBars = Object.entries(shard.temporal_histogram || {})
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([yr, cnt]) => {
        const heightPct = Math.max(8, Math.round((cnt / maxYearCount) * 100));
        return `
          <div class="hist-col" title="${escapeHtml(yr)}: ${escapeHtml(cnt)} records">
            <div class="hist-bar" style="height:${heightPct}%"></div>
            <span class="hist-lbl">${escapeHtml(yr.slice(-2))}</span>
          </div>
        `;
      })
      .join("");

    const datasetItems = shard.datasets
      .map(
        (ds) =>
          `<li>${escapeHtml(ds.datasetName)} (<strong>${escapeHtml(ds.count)}</strong>)</li>`
      )
      .join("");

    shardEl.innerHTML = `
      <div style="margin-top:0.85rem;">
        <h3>Species Checklist (${shard.species.length})</h3>
        <table class="species-table">
          <thead>
            <tr><th>Species</th><th>IUCN</th><th>Obs</th><th>Years</th></tr>
          </thead>
          <tbody>${speciesRows}</tbody>
        </table>
      </div>
      <div style="margin-top:0.85rem;">
        <h3>Temporal Histogram</h3>
        <div class="histogram-bars">${histBars}</div>
      </div>
      <div style="margin-top:0.85rem;">
        <h3>Contributing Datasets</h3>
        <ul class="muted-note" style="padding-left:1.1rem;margin:0;">${datasetItems}</ul>
      </div>
    `;
  } catch (err: any) {
    shardEl.innerHTML = `<p class="muted-note" style="color:#991b1b;">Unable to load basin shard: ${escapeHtml(err?.message || err)}</p>`;
  }
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

    layerPartitions(manifest.layers.basins).forEach((part, i) => {
      const sid = `basins-${i}`;
      sources[sid] = { type: "vector", ...sourceSpec(part.path, part) };
      const fillId = i === 0 ? "basins-fill" : `basins-fill-${i}`;
      const outlineId = i === 0 ? "basins-outline" : `basins-outline-${i}`;
      ids.basinsFill.push(fillId);
      ids.basinsOutline.push(outlineId);
      basinLayers.push(
        {
          id: fillId,
          type: "fill",
          source: sid,
          "source-layer": "basins",
          paint: { "fill-color": basinFillExpression(), "fill-opacity": 0.78 },
        },
        {
          id: outlineId,
          type: "line",
          source: sid,
          "source-layer": "basins",
          paint: { "line-color": "#0b2538", "line-width": ["interpolate", ["linear"], ["zoom"], 4, 0.4, 10, 1.6] },
        }
      );
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

    ids.basinsFill.forEach((layerId) => {
      map.on("click", layerId, (e: any) => {
        if (consumed(e)) return;
        const feature = e.features && e.features[0];
        if (feature && feature.properties) {
          loadAndRenderBasinShard(feature.properties as BasinScalarProperties);
        }
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
    const basinVis = state.showBasins ? "visible" : "none";
    const occVis = state.showOccurrences ? "visible" : "none";
    const appVis = state.showApproved ? "visible" : "none";

    for (const layerId of [...state.mapLayerIds.basinsFill, ...state.mapLayerIds.basinsOutline]) {
      if (map.getLayer(layerId)) map.setLayoutProperty(layerId, "visibility", basinVis);
    }
    for (const layerId of state.mapLayerIds.basinsFill) {
      if (map.getLayer(layerId)) map.setPaintProperty(layerId, "fill-color", basinFillExpression());
    }

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
  const bb = state.selection ? selectionBounds(selectedRows()) : null;
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
      ${selectionBounds(rows) ? `<button type="button" class="btn btn-ghost" id="key-fit">Zoom to results</button>` : ""}
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
