// Stage 10 unit tests for src/frontend/report.ts (pure helpers; run with Node >= 22.18 type stripping).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  availableMetrics,
  basinLevels,
  browseCountries,
  checklistCsv,
  choroplethExpression,
  climateChartSvg,
  contextValues,
  countryName,
  envDivisor,
  envValue,
  exportFileName,
  filterChecklist,
  habitatIndicators,
  legendStops,
  levelAtZoom,
  levelZoomRanges,
  metricById,
  metricDomain,
  metresPerPixel,
  pdfFromJpeg,
  scaleBar,
  shardEntry,
  sortChecklist,
} from "../src/frontend/report.ts";

const oldManifest = {
  environment: { scalars: { lake_pc: { scale: null }, human_footprint: { scale: null }, regulation_pc: { scale: null },
    forest_pc: { scale: null }, air_temp_c: { scale: 10 }, wetland_pc: { scale: null }, protected_pc: { scale: null } } },
  layers: {
    basins: { path: "/releases/v0.5.0/basins.pmtiles", min_zoom: 0, max_zoom: 7, source_layer: "basins" },
    basin_levels: {
      "06": { level: 6, primary: true, min_zoom: 0, max_zoom: 7 },
      "03": { level: 3, primary: false, min_zoom: 0, max_zoom: 5, source_layer: "basins", pmtiles: [{ path: "/l3.pmtiles", min_zoom: 0, max_zoom: 5 }] },
      "08": { level: 8, primary: false, min_zoom: 5, max_zoom: 10, source_layer: "basins", pmtiles: [{ path: "/l8.pmtiles", min_zoom: 5, max_zoom: 10 }] },
    },
  },
};
const newManifest = { environment: { scalars: { lake_pc: { scale: 10 }, human_footprint: { scale: 10 } } } };

test("BasinATLAS factors: divide only releases built before the factors were confirmed", () => {
  assert.equal(envDivisor(oldManifest, "lake_pc"), 10);
  assert.equal(envDivisor(oldManifest, "forest_pc"), 1);
  assert.equal(envDivisor(newManifest, "lake_pc"), 1); // already divided by the pipeline
  assert.equal(envValue(oldManifest, "human_footprint", 492), 49.2);
  assert.equal(envValue(newManifest, "human_footprint", 49.2), 49.2);
  assert.equal(envValue(oldManifest, "lake_pc", null), null);
});

test("metrics: environment options only with BasinATLAS; domains and expressions", () => {
  assert.deepEqual(availableMetrics({}).map((m) => m.id), ["occurrence_count", "species_count", "threatened_count"]);
  assert.ok(availableMetrics(oldManifest).some((m) => m.id === "env_lake_pc"));
  assert.ok(!availableMetrics(oldManifest).some((m) => m.id === "env_precip_mm")); // not in this manifest's scalars
  assert.deepEqual(metricDomain([3, 0, 120], "count"), [0, 120]);
  const vals = Array.from({ length: 101 }, (_, i) => i); // 0..100
  assert.deepEqual(metricDomain([...vals, 5000], "value"), [2, 99]); // the 5000 outlier is trimmed
  assert.deepEqual(metricDomain([], "value"), [0, 1]);
  const lake: any = choroplethExpression(metricById("env_lake_pc"), [0, 50], 10);
  assert.equal(lake[0], "case");
  assert.deepEqual(lake[1], ["has", "env_lake_pc"]);
  assert.deepEqual(lake[2][2], ["/", ["to-number", ["get", "env_lake_pc"], 0], 10]);
  const counts: any = choroplethExpression(metricById("species_count"), [0, 100], 1);
  assert.equal(counts[0], "interpolate");
  const stops = legendStops(metricById("species_count"), [0, 100]);
  assert.equal(stops.length, 4);
  assert.equal(stops[0].label, "0");
  assert.equal(stops[3].label, "100");
  assert.equal(legendStops(metricById("env_air_temp_c"), [-6, 24])[1].label, "4");
});

test("HydroBASINS levels: auto switches L3 -> L6 -> L8 by zoom; a chosen level shows at every zoom", () => {
  const levels = basinLevels(oldManifest);
  assert.deepEqual(levels.map((l) => [l.key, l.primary]), [["03", false], ["06", true], ["08", false]]);
  const auto = levelZoomRanges(levels, "auto");
  assert.deepEqual(auto, { "03": [0, 4], "06": [4, 7], "08": [7, 24] });
  assert.equal(levelAtZoom(auto, 2), "03");
  assert.equal(levelAtZoom(auto, 5.5), "06");
  assert.equal(levelAtZoom(auto, 9), "08");
  const fixed = levelZoomRanges(levels, "08");
  assert.deepEqual(fixed["08"], [5, 24]); // no Level 8 tiles below zoom 5
  assert.deepEqual(fixed["03"], [0, 0]);
  const single = basinLevels({ layers: { basins: { path: "/b.pmtiles", min_zoom: 0, max_zoom: 9 } } });
  assert.deepEqual(levelZoomRanges(single, "auto"), { "06": [0, 24] });
});

test("shard lookup handles grouped Level 8 documents", () => {
  const grouped = { release_version: "v", level: 8, basins: { "7080000001": { species: [1] } } };
  assert.deepEqual(shardEntry(grouped, "7080000001"), { species: [1] });
  assert.equal(shardEntry(grouped, "missing"), null);
  const single = { basin: {}, species: [] };
  assert.equal(shardEntry(single, "x"), single);
});

test("climate chart and habitat indicators", () => {
  const monthly = { air_temp_c: [12.4, 14.3, 18.1, 22, 25.1, 27.7, 28.9, 29, 27, 22.8, 18.1, 13.9],
    precip_mm: [50, 53, 33, 55, 96, 95, 74, 78, 141, 89, 52, 43] };
  const svg = climateChartSvg(monthly, 340, 190);
  assert.match(svg, /^<svg /);
  assert.equal((svg.match(/<rect /g) || []).length, 12);
  assert.match(svg, /precipitation 859 mm per year/);
  assert.match(svg, /<polyline /);
  assert.equal(climateChartSvg({}, 340, 190), "");
  const cold = climateChartSvg({ air_temp_c: [-20, -18, -10, 0, 6, 12, 15, 13, 7, -2, -12, -18], precip_mm: Array(12).fill(20) }, 340, 190);
  assert.match(cold, /stroke-dasharray/); // zero line when temperatures go below 0
  const ind = habitatIndicators(oldManifest, { forest_pc: 21, wetland_pc: 16, lake_pc: 50, protected_pc: 5, human_footprint: 88 });
  assert.deepEqual(ind.map((i) => i.key), ["forest_pc", "wetland_pc", "lake_pc", "protected_pc", "human_footprint"]);
  assert.equal(ind[2].display, "5%");
  assert.equal(ind[4].display, "8.8 of 50");
  assert.ok(Math.abs(ind[4].fraction - 0.176) < 1e-9);
  assert.equal(habitatIndicators(oldManifest, {})[0].display, "no data");
  const ctx = contextValues(oldManifest, { air_temp_c: 21.6, regulation_pc: 120 });
  assert.deepEqual(ctx.map((c) => c.display), ["21.6 °C", "12 %"]);
});

test("checklist filter, sort and CSV", () => {
  const sp = [
    { scientificName: "Fundulus similis", family: "Fundulidae", iucnRedListCategory: "LC", count: 10, taxonKey: 1 },
    { scientificName: "Aphanius iberus", family: "Cyprinodontidae", iucnRedListCategory: "EN", count: 3, taxonKey: 2, firstYear: 1990, lastYear: 2020 },
    { scientificName: "Cyprinodon, sp.", family: "Cyprinodontidae", iucnRedListCategory: "", count: 5, taxonKey: 3 },
  ];
  assert.deepEqual(filterChecklist(sp, "", "threatened").map((s) => s.taxonKey), [2]);
  assert.deepEqual(filterChecklist(sp, "cyprino", "").map((s) => s.taxonKey), [2, 3]);
  assert.deepEqual(sortChecklist(sp, "name").map((s) => s.taxonKey), [2, 3, 1]);
  assert.deepEqual(sortChecklist(sp, "iucn").map((s) => s.taxonKey), [2, 1, 3]);
  assert.deepEqual(sortChecklist(sp, "count").map((s) => s.taxonKey), [1, 3, 2]);
  const csv = checklistCsv(sp, { title: "Basin 771", version: "v0.5.0", doi: "10.15468/dl.x" });
  const lines = csv.trim().split("\n");
  assert.match(lines[0], /https:\/\/doi.org\/10.15468\/dl.x/);
  assert.equal(lines.length, 5);
  assert.equal(lines[3], "Aphanius iberus,2,Cyprinodontidae,EN,3,1990,2020");
  assert.match(lines[4], /^"Cyprinodon, sp\.",3,/);
});

test("country browser", () => {
  const doc = { countries: [
    { code: "US", continent: "NA", species_count: 120 },
    { code: "MX", continent: "NA", species_count: 300 },
    { code: "ES", continent: "EU", species_count: 6 },
    { code: "unknown", continent: "", species_count: 2 },
  ] };
  assert.deepEqual(browseCountries(doc, "NA", "", "en").map((c: any) => c.code), ["MX", "US"]);
  assert.deepEqual(browseCountries(doc, "", "spa", "en").map((c: any) => c.code), ["ES"]);
  assert.deepEqual(browseCountries(doc, "unknown", "", "en").map((c: any) => c.code), ["unknown"]);
  assert.equal(countryName("ES", "en"), "Spain");
  assert.equal(countryName("unknown", "en"), "Country not recorded");
});

test("export: scale bar, PDF structure, file names", () => {
  const mpp = metresPerPixel(0, 0);
  assert.ok(Math.abs(mpp - 78271.517) < 1);
  const bar = scaleBar(12.3, 120); // up to 1476 m
  assert.deepEqual([bar.metres, bar.label], [1000, "1 km"]);
  assert.ok(Math.abs(bar.px - 1000 / 12.3) < 1e-9);
  assert.equal(scaleBar(0.5, 100).label, "50 m");
  const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3, 0xff, 0xd9]);
  const pdf = pdfFromJpeg(jpeg, 1600, 900, "Killifish (worldwide)");
  const text = Buffer.from(pdf).toString("latin1");
  assert.ok(text.startsWith("%PDF-1.4"));
  assert.ok(text.trimEnd().endsWith("%%EOF"));
  assert.match(text, /\/MediaBox \[0 0 841.89 595.28\]/); // landscape A4 for a wide map
  assert.match(text, /\/Width 1600 \/Height 900/);
  const xrefAt = Number(/startxref\n(\d+)/.exec(text)![1]);
  assert.ok(text.slice(xrefAt).startsWith("xref"));
  const offsets = [...text.slice(xrefAt).matchAll(/^(\d{10}) 00000 n $/gm)].map((m) => Number(m[1]));
  assert.equal(offsets.length, 6);
  offsets.forEach((off, i) => assert.ok(text.slice(off).startsWith(`${i + 1} 0 obj`), `object ${i + 1} offset`));
  assert.match(text, /\/Title \(Killifish worldwide\)/);
  assert.equal(exportFileName("Killifish: worldwide!", "png", new Date("2026-11-21T12:00:00Z")), "killifish-worldwide-2026-11-21.png");
});
