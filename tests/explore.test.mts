// Stage 9 unit tests for src/frontend/explore.ts (pure helpers; run with Node >= 22.18 type stripping).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  BASEMAPS,
  KEY_LETTERS,
  MAX_KEY_SPECIES,
  assignSpeciesKey,
  basemapLayers,
  basemapSources,
  buildTaxonEntries,
  colorExpression,
  groupBySpecies,
  letterExpression,
  normalizeAlias,
  normalizeCatalog,
  pickDataset,
  searchTaxa,
  selectionBounds,
  selectionFilter,
  synonymsByKey,
} from "../src/frontend/explore.ts";

const index = {
  species: [
    { key: 1, name: "Aphanius fasciatus", family: "Aphaniidae", genus: "Aphanius", iucn: "LC", occurrence_count: 40, vernacular: ["Mediterranean killifish"], bbox: [0, 30, 20, 45] },
    { key: 2, name: "Aphanius iberus", family: "Aphaniidae", genus: "Aphanius", iucn: "EN", occurrence_count: 90, vernacular: [], bbox: [-5, 36, 3, 42] },
    { key: 3, name: "Gambusia affinis", family: "Poeciliidae", genus: "Gambusia", iucn: "LC", occurrence_count: 1000, vernacular: ["Western mosquitofish"] },
    { key: 4, name: "Gambusia holbrooki", family: "Poeciliidae", genus: "Gambusia", iucn: "LC", occurrence_count: 1000, vernacular: [] },
  ],
};
const aliases = {
  aliases: {
    "aphanius fasciatus": 1, "lebias fasciata": 1, "mediterranean killifish": 1, "aphanius iberus": 2,
    "gambusia affinis": 3, "heterandria affinis": 3, "western mosquitofish": 3, "gambusia holbrooki": 4,
  },
};

test("normalizeAlias folds accents, case and spacing", () => {
  assert.equal(normalizeAlias("  Aphanius  FASCIÁTUS "), "aphanius fasciatus");
  assert.equal(normalizeAlias("Xiphophorus ×hybrid"), "xiphophorus xhybrid");
});

test("search ranks exact and prefix matches, families and genera first", () => {
  const entries = buildTaxonEntries(index, aliases);
  const kinds = new Set(entries.map((e) => e.kind));
  assert.deepEqual([...kinds].sort(), ["common", "family", "genus", "species", "synonym"]);
  const aph = searchTaxa(entries, "aph", 10);
  assert.equal(aph[0].kind, "family"); // Aphaniidae
  assert.equal(aph[0].label, "Aphaniidae");
  assert.deepEqual(aph[0].speciesKeys.sort(), [1, 2]);
  assert.equal(aph[1].kind, "genus");
  const syn = searchTaxa(entries, "lebias", 5);
  assert.equal(syn[0].kind, "synonym");
  assert.equal(syn[0].label, "Lebias fasciata");
  assert.match(syn[0].detail, /synonym of Aphanius fasciatus/);
  const common = searchTaxa(entries, "mosquitofish", 5);
  assert.equal(common[0].kind, "common");
  assert.deepEqual(common[0].speciesKeys, [3]);
  assert.deepEqual(searchTaxa(entries, "a", 5), []); // too short
  assert.equal(searchTaxa(entries, "gambusia affinis", 5)[0].kind, "species");
});

test("species key: deterministic letters/colours, most-recorded first, ties by name", () => {
  const syn = synonymsByKey(aliases, index);
  assert.deepEqual(syn.get(1), ["Lebias fasciata"]); // vernacular names are not synonyms
  const key = assignSpeciesKey(index.species, syn);
  assert.deepEqual(key.map((k) => [k.letter, k.name]), [
    ["A", "Gambusia affinis"], ["B", "Gambusia holbrooki"], ["C", "Aphanius iberus"], ["D", "Aphanius fasciatus"],
  ]);
  assert.deepEqual(key[3].names, ["Aphanius fasciatus", "Lebias fasciata"]);
  assert.notEqual(key[0].color, key[1].color);
  const many = Array.from({ length: 40 }, (_, i) => ({ ...index.species[0], key: 100 + i, name: `Sp ${i}`, occurrence_count: i }));
  assert.equal(assignSpeciesKey(many, new Map()).length, MAX_KEY_SPECIES);
  assert.equal(KEY_LETTERS.length, MAX_KEY_SPECIES);
});

test("MapLibre expressions: speciesKey mode and name-prefix mode", () => {
  const key = assignSpeciesKey(index.species.slice(0, 2), synonymsByKey(aliases, index));
  const byKey = selectionFilter(key, new Set(), true);
  assert.deepEqual(byKey, ["any", ["==", ["get", "speciesKey"], 2], ["==", ["get", "speciesKey"], 1]]);
  const byName: any = selectionFilter(key, new Set([2]), false);
  // Aphanius fasciatus via accepted name or synonym, matched as a whole-word prefix of scientificName
  assert.equal(byName[0], "any");
  assert.equal(byName.length, 2); // only Aphanius fasciatus is shown
  assert.deepEqual(byName[1], ["any",
    ["==", ["index-of", "Aphanius fasciatus ", ["concat", ["get", "scientificName"], " "]], 0],
    ["==", ["index-of", "Lebias fasciata ", ["concat", ["get", "scientificName"], " "]], 0]]);
  assert.deepEqual(selectionFilter(key, new Set([1, 2]), true), ["==", 1, 0]);
  assert.equal(selectionFilter([], new Set(), true), null);
  const color: any = colorExpression(key, true, "#000");
  assert.equal(color[0], "case");
  assert.equal(color[color.length - 1], "#000");
  assert.equal(colorExpression([], true, "#000"), "#000");
  const letters: any = letterExpression(key, true);
  assert.equal(letters[2], "A");
});

test("bounds union and grouped tooltip", () => {
  assert.deepEqual(selectionBounds(index.species.slice(0, 2)), [-5, 30, 20, 45]);
  assert.equal(selectionBounds(index.species.slice(2)), null);
  const key = assignSpeciesKey(index.species, synonymsByKey(aliases, index));
  const groups = groupBySpecies([
    { properties: { speciesKey: 3, scientificName: "Gambusia affinis (Baird & Girard, 1853)", count: 2 } },
    { properties: { scientificName: "Heterandria affinis Baird & Girard", count: 1 } },
    { properties: { scientificName: "Lebias fasciata Valenciennes" } },
    { properties: { scientificName: "Poecilia reticulata" } },
  ], key);
  assert.deepEqual(groups.map((g) => [g.label, g.letter, g.count]), [
    ["Gambusia affinis", "A", 3], // accepted name + synonym grouped together
    ["Aphanius fasciatus", "D", 1],
    ["Poecilia reticulata", "", 1],
  ]);
  const gaff = groups.find((g) => g.label === "Gambusia affinis");
  assert.equal(gaff?.count, 3); // accepted name + synonym grouped together
});

test("dataset catalog: default first, unsafe paths dropped, URL choice honoured", () => {
  const entries = normalizeCatalog({ datasets: [
    { id: "willamette", title: "All species: Willamette", manifest_path: "/releases/datasets/willamette/manifest.json", version: "v0.2.0" },
    { id: "evil", title: "x", manifest_path: "https://evil.example/manifest.json" },
    { id: "dots", title: "y", manifest_path: "/releases/../secret.json" },
  ] });
  assert.deepEqual(entries.map((e) => e.id), ["default", "willamette"]);
  assert.equal(entries[0].manifest_path, "/releases/manifest.json");
  assert.equal(pickDataset(entries, "willamette").id, "willamette");
  assert.equal(pickDataset(entries, "nope").id, "default");
  assert.equal(pickDataset(normalizeCatalog(null), null).id, "default");
});

test("basemaps: every layer has a source and exactly one is visible", () => {
  const sources = basemapSources();
  const layers = basemapLayers("satellite");
  assert.equal(layers.length, BASEMAPS.length);
  for (const l of layers) assert.ok(sources[l.source], l.source);
  assert.deepEqual(layers.filter((l) => l.layout.visibility === "visible").map((l) => l.id), ["basemap-satellite"]);
  assert.equal(basemapLayers("none").filter((l) => l.layout.visibility === "visible").length, 0);
  for (const b of BASEMAPS) assert.ok(b.attribution.length > 20 && b.tiles[0].startsWith("https://"), b.id);
});
