#!/usr/bin/env python3
"""Stage 7 tests: GBIF Backbone checklist paging, species index / alias outputs, the global
Cyprinodontiformes query, and multi-region release builds that keep off-basin records.

Offline: GBIF is replaced by in-memory fakes; HydroBASINS regions are written as real
shapefile zips from the demo polygons.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

PIPELINE_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for p in (PIPELINE_DIR, TESTS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import hydrobasins  # noqa: E402
import ingest_gbif_download as ingest  # noqa: E402
import taxonomy  # noqa: E402
from test_ingest_and_benchmark import FAKE_KEY, SAMPLE, FakeGbif, FakeResponse, make_simple_csv_zip  # noqa: E402

QUERY = PIPELINE_DIR / "queries" / "cyprinodontiformes_global.json"


class FakeBackbone:
    """/species/{key} and paged /species/search on the GBIF Backbone."""

    def __init__(self, species: List[Dict[str, Any]], synonyms: List[Dict[str, Any]]) -> None:
        self.species = species
        self.synonyms = synonyms
        self.searches: List[Dict[str, str]] = []

    def __call__(self, req: Any, timeout: float = 0) -> FakeResponse:
        url = req.full_url
        path, _, qs = url.split("/v1", 1)[1].partition("?")
        if path == "/species/547":
            return FakeResponse(json.dumps({"key": 547, "canonicalName": "Cyprinodontiformes", "rank": "ORDER"}).encode())
        if path == "/species/search":
            q = dict(urllib.parse.parse_qsl(qs))
            self.searches.append(q)
            assert q["datasetKey"] == taxonomy.BACKBONE_DATASET_KEY and q["highertaxonKey"] == "547"
            pool = self.species if q.get("status") == "ACCEPTED" else [s for s in self.synonyms if s["taxonomicStatus"] == q["status"]]
            off, lim = int(q["offset"]), int(q["limit"])
            page = pool[off:off + lim]
            return FakeResponse(json.dumps({"count": len(pool), "endOfRecords": off + lim >= len(pool),
                                            "results": page}).encode())
        raise AssertionError(f"Unexpected call {url}")


def demo_species() -> List[Dict[str, Any]]:
    with (SAMPLE / "gbif_occurrences.tsv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    seen: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        seen.setdefault(r["taxonKey"], {"key": int(r["taxonKey"]), "nubKey": int(r["taxonKey"]),
                                         "canonicalName": r["scientificName"], "scientificName": r["scientificName"] + " (Author, 1900)",
                                         "authorship": "(Author, 1900) ", "rank": "SPECIES", "taxonomicStatus": "ACCEPTED",
                                         "family": r.get("family", "") or "Testidae", "genus": r["scientificName"].split()[0],
                                         "vernacularNames": [{"vernacularName": "Demo " + r["scientificName"], "language": "eng"}]})
    return sorted(seen.values(), key=lambda s: s["key"])


class TestChecklist(unittest.TestCase):
    def test_fetch_pages_species_and_keeps_only_species_synonyms(self) -> None:
        species = [
            {"key": 2347630, "nubKey": 2347630, "canonicalName": "Ilyodon whitei", "scientificName": "Ilyodon whitei (Meek, 1904)",
             "authorship": "(Meek, 1904) ", "rank": "SPECIES", "taxonomicStatus": "ACCEPTED", "family": "Goodeidae",
             "familyKey": 7657, "genus": "Ilyodon", "genusKey": 2347629,
             "vernacularNames": [{"vernacularName": "Balsas Splitfin", "language": "eng"},
                                 {"vernacularName": "Mexclapique", "language": "spa"}]},
            {"key": 2347627, "nubKey": 2347627, "canonicalName": "Alloophorus robustus", "scientificName": "Alloophorus robustus (Bean, 1892)",
             "authorship": "(Bean, 1892) ", "rank": "SPECIES", "taxonomicStatus": "ACCEPTED", "family": "Goodeidae",
             "genus": "Alloophorus"},
            {"key": 5, "nubKey": 5, "canonicalName": "Fundulus heteroclitus", "scientificName": "Fundulus heteroclitus (Linnaeus, 1766)",
             "authorship": "(Linnaeus, 1766)", "rank": "SPECIES", "taxonomicStatus": "ACCEPTED", "family": "Fundulidae", "genus": "Fundulus"},
        ]
        synonyms = [
            {"key": 2346614, "nubKey": 2346614, "canonicalName": "Hydrargira", "rank": "GENUS", "taxonomicStatus": "SYNONYM",
             "acceptedKey": 2347676},  # genus synonym: not a species alias
            {"key": 2347632, "nubKey": 2347632, "canonicalName": "Ilydon whitei", "rank": "SPECIES", "taxonomicStatus": "SYNONYM",
             "acceptedKey": 2347630, "speciesKey": 2347630},
            {"key": 77, "nubKey": 77, "canonicalName": "Fundulus pisculentus", "rank": "SPECIES",
             "taxonomicStatus": "HETEROTYPIC_SYNONYM", "acceptedKey": 5},
        ]
        fake = FakeBackbone(species, synonyms)
        client = ingest.GbifClient(urlopen=fake)
        with mock.patch.object(taxonomy, "PAGE_SIZE", 2):  # force several pages
            checklist = taxonomy.fetch_checklist(client, 547)
        self.assertEqual(checklist["taxon"], {"key": 547, "name": "Cyprinodontiformes", "rank": "ORDER"})
        self.assertEqual([s["name"] for s in checklist["species"]],
                         ["Fundulus heteroclitus", "Alloophorus robustus", "Ilyodon whitei"])  # family, genus, name
        ilyodon = checklist["species"][2]
        self.assertEqual(ilyodon["authorship"], "(Meek, 1904)")
        self.assertEqual(ilyodon["vernacularNames"], ["Balsas Splitfin"])
        self.assertEqual(sorted(s["name"] for s in checklist["synonyms"]), ["Fundulus pisculentus", "Ilydon whitei"])
        offsets = [q["offset"] for q in fake.searches if q.get("status") == "ACCEPTED"]
        self.assertEqual(offsets, ["0", "2"])

    def test_species_outputs_aliases_and_uncatalogued_species(self) -> None:
        checklist = {"taxon": {"key": 547}, "species": [
            {"key": 1, "name": "Aphanius fasciatus", "family": "Aphaniidae", "genus": "Aphanius", "vernacularNames": ["Mediterranean Killifish"]},
            {"key": 2, "name": "Nothobranchius furzeri", "family": "Nothobranchiidae", "genus": "Nothobranchius", "vernacularNames": []},
            {"key": 3, "name": "Zeta one", "family": "Zidae", "genus": "Zeta", "vernacularNames": ["Shared Name"]},
            {"key": 4, "name": "Zeta two", "family": "Zidae", "genus": "Zeta", "vernacularNames": ["Shared Name"]},
        ], "synonyms": [
            {"name": "Lebias fasciata", "acceptedKey": 1},
            {"name": "Aphanius fasciatus", "acceptedKey": 2},  # cannot override an accepted spelling
            {"name": "Ambiguous name", "acceptedKey": 3},
            {"name": "Ambiguous  NAME", "acceptedKey": 4},
        ]}
        occ = {1: {"count": 10, "basins": {"a", "b"}, "firstYear": 1950, "lastYear": 2020},
               99: {"count": 2, "name": "Novus speciosus", "family": "Aphaniidae", "basins": set()}}
        out = taxonomy.build_species_outputs(checklist, occ, {"1": "EN", "2": "LC"})
        idx = out["species_index"]
        by_key = {r["key"]: r for r in idx["species"]}
        self.assertEqual((by_key[1]["occurrence_count"], by_key[1]["basin_count"], by_key[1]["threatened"]), (10, 2, True))
        self.assertEqual((by_key[2]["occurrence_count"], by_key[2]["iucn"]), (0, "LC"))
        self.assertFalse(by_key[99]["in_checklist"])
        self.assertEqual(by_key[99]["genus"], "Novus")
        self.assertEqual(idx["counts"], {"species": 5, "with_occurrences": 2, "threatened": 1, "families": 3, "genera": 4})
        aliases = out["taxonomy_aliases"]["aliases"]
        self.assertEqual(aliases["lebias fasciata"], 1)
        self.assertEqual(aliases["aphanius fasciatus"], 1)
        self.assertEqual(aliases["mediterranean killifish"], 1)
        self.assertNotIn("ambiguous name", aliases)
        self.assertNotIn("shared name", aliases)
        self.assertEqual(out["taxonomy_aliases"]["ambiguous"]["ambiguous name"], [3, 4])
        self.assertEqual(taxonomy.normalize_alias("  Aphanius  FASCIÁTUS "), "aphanius fasciatus")


class TestGlobalQuery(unittest.TestCase):
    def test_query_is_global_order_547_without_fossils_or_aquaria(self) -> None:
        query = ingest.load_query(QUERY)
        preds = query["request"]["predicate"]["predicates"]
        by_key = {p.get("key"): p for p in preds}
        self.assertEqual(by_key["TAXON_KEY"]["value"], "547")
        self.assertEqual(by_key["OCCURRENCE_STATUS"]["value"], "PRESENT")
        self.assertNotIn("FOSSIL_SPECIMEN", by_key["BASIS_OF_RECORD"]["values"])
        self.assertNotIn("LIVING_SPECIMEN", by_key["BASIS_OF_RECORD"]["values"])
        self.assertNotIn("YEAR", by_key)  # historical museum records matter for killifish
        self.assertIsNone(ingest.predicate_bbox(query["request"]["predicate"]))
        fake = FakeGbif(b"", {})
        dry = ingest.request_download(ingest.GbifClient(urlopen=fake), query, 2_000_000, dry_run=True)
        self.assertEqual(dry["request"]["format"], "SIMPLE_CSV")


class TestMultiRegionBuild(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        zip_path = self.tmp_path / "fixture.zip"
        info = make_simple_csv_zip(zip_path, extra_outside=3)
        self.demo_rows = info["rows"]
        self.fake = FakeGbif(zip_path.read_bytes(), info["iucn"])
        self.client = ingest.GbifClient(urlopen=self.fake, user="u", password="p")
        feats = hydrobasins.load_hydrobasins(SAMPLE / "hydrobasins_l6.geojson")
        half = len(feats) // 2
        self.hb = [self.tmp_path / "hb" / "hybas_na_lev06_v1c.zip", self.tmp_path / "hb" / "hybas_ar_lev06_v1c.zip"]
        hydrobasins.write_shapefile_zip(feats[:half], self.hb[0], "hybas_na_lev06_v1c")
        hydrobasins.write_shapefile_zip(feats[half:], self.hb[1], "hybas_ar_lev06_v1c")
        self.n_basins = len(feats)
        query = ingest.load_query(QUERY)
        ingest.request_download(self.client, query, max_records=1000)
        self.gbif_dir = ingest.fetch_download(self.client, FAKE_KEY, self.tmp_path / "work", poll_seconds=0, sleep=lambda s: None)
        fake_backbone = FakeBackbone(demo_species(), [])
        self.checklist = taxonomy.fetch_checklist(ingest.GbifClient(urlopen=fake_backbone), 547)
        self.checklist_path = self.tmp_path / "checklist.json"
        self.checklist_path.write_text(json.dumps(self.checklist))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_two_regions_keep_unassigned_and_species_index(self) -> None:
        root = self.tmp_path / "build-r2"
        manifest = ingest.build_release_from_download(
            self.gbif_dir, self.hb, "v0.3.0-test", root, client=self.client, basin_zooms=(0, 8),
            occurrence_zooms=(0, 6, 9), keep_unassigned=True, checklist_path=self.checklist_path)
        rel = root / "releases" / "v0.3.0-test"
        counts = manifest["counts"]
        self.assertEqual(counts["basins"], self.n_basins)
        self.assertEqual(counts["occurrences"], self.demo_rows + 3)  # Pacific records kept
        self.assertEqual((counts["unassigned_occurrences"], counts["unassigned_kept"]), (3, 3))
        self.assertEqual(manifest["provenance"]["hydrobasins"]["files"], [p.name for p in self.hb])
        for p in self.hb:
            self.assertIn(p.name, manifest["provenance"]["sources"])
        self.assertIn("checklist.json", manifest["provenance"]["sources"])
        self.assertEqual(manifest["provenance"]["taxonomy"]["taxon"]["key"], 547)
        self.assertEqual(manifest["layers"]["species_index"], "/releases/v0.3.0-test/species_index.json")
        idx = json.loads((rel / "species_index.json").read_text())
        aliases = json.loads((rel / "taxonomy_aliases.json").read_text())
        in_checklist = [r for r in idx["species"] if r["in_checklist"]]
        self.assertEqual(len(in_checklist), len(self.checklist["species"]))
        seal = [r for r in idx["species"] if r["name"] == "Phoca vitulina"]
        self.assertEqual(len(seal), 1)  # off-basin species outside the checklist still indexed
        self.assertEqual((seal[0]["occurrence_count"], seal[0]["basin_count"], seal[0]["in_checklist"]), (3, 0, False))
        self.assertEqual(sum(r["occurrence_count"] for r in idx["species"]), self.demo_rows + 3)
        first = self.checklist["species"][0]["name"]
        self.assertEqual(aliases["aliases"][taxonomy.normalize_alias(first)], self.checklist["species"][0]["key"])
        root_manifest = json.loads((root / "releases" / "manifest.json").read_text())
        self.assertEqual(root_manifest["layers"]["taxonomy_aliases"], "/releases/v0.3.0-test/taxonomy_aliases.json")

    def test_default_build_still_drops_unassigned_and_rejects_mixed_levels(self) -> None:
        root = self.tmp_path / "build-r2"
        manifest = ingest.build_release_from_download(
            self.gbif_dir, self.hb[0], "v0.3.0-a", root, client=self.client, basin_zooms=(0, 8), occurrence_zooms=(0, 6, 9))
        self.assertEqual(manifest["counts"]["unassigned_kept"], 0)
        self.assertNotIn("species_index", manifest["layers"])
        lev5 = self.tmp_path / "hb" / "hybas_eu_lev05_v1c.zip"
        hydrobasins.write_shapefile_zip(hydrobasins.load_hydrobasins(SAMPLE / "hydrobasins_l6.geojson")[:1], lev5, "hybas_eu_lev05_v1c")
        with self.assertRaises(ValueError):
            ingest.build_release_from_download(self.gbif_dir, [self.hb[0], lev5], "v0.3.0-b", root, client=self.client)

    def test_cli_accepts_several_hydrobasins_and_region_all(self) -> None:
        args = ingest.parse_args(["build", "--gbif-dir", str(self.gbif_dir), "--hydrobasins", *map(str, self.hb),
                                  "--keep-unassigned", "--checklist", str(self.checklist_path), "--version", "v0.3.0"])
        self.assertEqual(args.hydrobasins, self.hb)
        self.assertTrue(args.keep_unassigned)
        self.assertEqual(ingest.parse_args(["fetch-hydrobasins", "--region", "all", "--level", "3"]).region, "all")


class TestParallelPublish(unittest.TestCase):
    def test_retries_transient_failures_and_switches_manifest_last(self) -> None:
        import subprocess
        import publish_release
        calls: List[str] = []
        failures = {"obj-3": 2}  # obj-3 fails twice (Cloudflare 500) then succeeds

        def fake_run(cmd, capture_output=True, text=True):
            name = cmd[-1]
            calls.append(name)
            if failures.get(name, 0) > 0:
                failures[name] -= 1
                return subprocess.CompletedProcess(cmd, 1, "", "500: Internal Server Error")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        cmds = [["put", f"obj-{i}"] for i in range(10)] + [["put", "manifest"]]
        with mock.patch.object(publish_release.subprocess, "run", side_effect=fake_run):
            publish_release.run_upload_commands(cmds, jobs=4, retries=4, sleep=lambda s: None)
        self.assertEqual(calls[-1], "manifest")
        self.assertEqual(calls.count("obj-3"), 3)
        self.assertEqual(len(calls), 13)

    def test_permanent_failure_never_switches_manifest(self) -> None:
        import subprocess
        import publish_release
        calls: List[str] = []

        def fake_run(cmd, capture_output=True, text=True):
            calls.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 1 if cmd[-1] == "obj-1" else 0, "", "boom")

        cmds = [["put", "obj-0"], ["put", "obj-1"], ["put", "manifest"]]
        with mock.patch.object(publish_release.subprocess, "run", side_effect=fake_run), \
                mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                publish_release.run_upload_commands(cmds, jobs=2, retries=2, sleep=lambda s: None)
        self.assertNotIn("manifest", calls)


if __name__ == "__main__":
    unittest.main()
