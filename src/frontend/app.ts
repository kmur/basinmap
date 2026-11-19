import {
  ApiConfigResponse,
  ApiHealthResponse,
  ApprovedOverlayFeatureCollection,
  BasinScalarProperties,
  BasinShardPayload,
  GeoJsonFeature,
  OccurrenceProperties,
  ReleaseManifest,
  deduplicateApprovedOverlay,
  parsePmtilesHeaderBytes,
  resolveDataUrl,
} from "./types.js";

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
  minYear: 2018,
  maxYear: 2026,
  showBasins: true,
  showOccurrences: true,
  showApproved: true,
  mapInstance: null,
  usingMapLibre: false,
};

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

  shardEl.innerHTML = `<p class="muted-note" style="margin-top:0.6rem;">Loading species shard <code>${escapeHtml(props.hybas_id)}.json</code>...</p>`;

  const template = state.manifest.layers.basin_shards_template;
  const shardPath = template.replace("{hybas_id}", props.hybas_id);
  const shardUrl = resolveDataUrl(state.publicDataUrl, shardPath);

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
      <div><strong>occurrenceID:</strong> <code>${escapeHtml(props.occurrenceID)}</code></div>
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

    const basinsPmtilesUrl = resolveDataUrl(state.publicDataUrl, manifest.layers.basins.path);
    const occPmtilesUrl = resolveDataUrl(state.publicDataUrl, manifest.layers.occurrences.path);
    const approvedUrl = resolveDataUrl(
      state.publicDataUrl,
      manifest.overlays.approved_observations.path
    );

    const map = new maplibre.Map({
      container: "maplibre-container",
      bounds: manifest.spatial_coverage.bbox,
      fitBoundsOptions: { padding: 36 },
      style: {
        version: 8,
        sources: {
          basins: {
            type: "vector",
            url: `pmtiles://${basinsPmtilesUrl}`,
            attribution: manifest.attribution,
          },
          occurrences: {
            type: "vector",
            url: `pmtiles://${occPmtilesUrl}`,
            attribution: manifest.attribution,
          },
          approved: {
            type: "geojson",
            data: {
              type: "FeatureCollection",
              features: state.approvedOccurrences,
            },
          },
        },
        layers: [
          {
            id: "background",
            type: "background",
            paint: { "background-color": "#d9e8f2" },
          },
          {
            id: "basins-fill",
            type: "fill",
            source: "basins",
            "source-layer": "basins",
            paint: {
              "fill-color": [
                "interpolate",
                ["linear"],
                ["get", state.selectedMetric],
                0,
                "#d8f3dc",
                6,
                "#95d5b2",
                10,
                "#52b788",
                14,
                "#1b4332",
              ],
              "fill-opacity": 0.78,
            },
          },
          {
            id: "basins-outline",
            type: "line",
            source: "basins",
            "source-layer": "basins",
            paint: {
              "line-color": "#0b2538",
              "line-width": 1.6,
            },
          },
          {
            id: "occurrences-circle",
            type: "circle",
            source: "occurrences",
            "source-layer": "occurrences",
            paint: {
              "circle-radius": 5,
              "circle-color": "#0284c7",
              "circle-stroke-color": "#ffffff",
              "circle-stroke-width": 1.2,
            },
          },
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

    map.on("click", "basins-fill", (e: any) => {
      const feature = e.features && e.features[0];
      if (feature && feature.properties) {
        loadAndRenderBasinShard(feature.properties as BasinScalarProperties);
      }
    });

    map.on("click", "occurrences-circle", (e: any) => {
      const feature = e.features && e.features[0];
      if (feature && feature.properties) {
        renderOccurrenceDetails(feature.properties as OccurrenceProperties, false);
      }
    });

    map.on("click", "approved-circle", (e: any) => {
      const feature = e.features && e.features[0];
      if (feature && feature.properties) {
        renderOccurrenceDetails(feature.properties as OccurrenceProperties, true);
      }
    });

    map.on("error", () => {
      // Fallback cleanly to interactive SVG if WebGL fails in headless environments.
      switchToSvgFallback("MapLibre runtime error — interactive SVG active");
    });

    state.mapInstance = map;
    state.usingMapLibre = true;
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

function syncMapFiltersAndLayers(): void {
  if (state.usingMapLibre && state.mapInstance) {
    const map = state.mapInstance;
    const basinVis = state.showBasins ? "visible" : "none";
    const occVis = state.showOccurrences ? "visible" : "none";
    const appVis = state.showApproved ? "visible" : "none";

    if (map.getLayer("basins-fill")) {
      map.setLayoutProperty("basins-fill", "visibility", basinVis);
      map.setLayoutProperty("basins-outline", "visibility", basinVis);
      map.setPaintProperty("basins-fill", "fill-color", [
        "interpolate",
        ["linear"],
        ["get", state.selectedMetric],
        0,
        "#d8f3dc",
        6,
        "#95d5b2",
        10,
        "#52b788",
        14,
        "#1b4332",
      ]);
    }

    const filterExpr: any[] = [
      "all",
      [">=", ["get", "year"], state.minYear],
      ["<=", ["get", "year"], state.maxYear],
    ];
    if (state.selectedSpecies) {
      filterExpr.push(["==", ["get", "scientificName"], state.selectedSpecies]);
    }

    if (map.getLayer("occurrences-circle")) {
      map.setLayoutProperty("occurrences-circle", "visibility", occVis);
      map.setFilter("occurrences-circle", filterExpr);
    }
    if (map.getLayer("approved-circle")) {
      map.setLayoutProperty("approved-circle", "visibility", appVis);
      map.setFilter("approved-circle", filterExpr);
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

async function bootstrap(): Promise<void> {
  bindControls();
  const apiStatus = await probeApiStatus();
  state.publicDataUrl = apiStatus.publicDataUrl;

  const manifestUrl = resolveDataUrl(state.publicDataUrl, "/releases/manifest.json");
  const manifest: ReleaseManifest = await fetchJsonWithTimeout(manifestUrl, 5000);
  state.manifest = manifest;

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
  const basinsGeoUrl = resolveDataUrl(
    state.publicDataUrl,
    manifest.layers.basins.geojson_path || "/releases/v0.1.0-demo/basins.geojson"
  );
  const occGeoUrl = resolveDataUrl(
    state.publicDataUrl,
    manifest.layers.occurrences.geojson_path || "/releases/v0.1.0-demo/occurrences.geojson"
  );
  const approvedUrl = resolveDataUrl(
    state.publicDataUrl,
    manifest.overlays.approved_observations.path
  );

  const [basinsFc, occFc, approvedFc] = await Promise.all([
    fetchJsonWithTimeout(basinsGeoUrl, 5000),
    fetchJsonWithTimeout(occGeoUrl, 5000),
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
  const dedup = deduplicateApprovedOverlay(approvedFc.features || [], snapshotIds);
  state.approvedOccurrences = dedup.uniqueFeatures;
  state.deduplicatedCount = dedup.duplicateCount;

  populateSpeciesFilter();

  const mlReady = initMapLibreIfAvailable();
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
