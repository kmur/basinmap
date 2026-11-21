#!/usr/bin/env python3
"""Stage 10 tests: country/continent summaries (countries.json) and verified BasinATLAS factors."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for p in (PIPELINE_DIR, TESTS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import countries  # noqa: E402
import hydroatlas  # noqa: E402
import ingest_gbif_download as ingest  # noqa: E402
from test_ingest_and_benchmark import TestGbifDownloadIngest  # noqa: E402


class TestCountrySummary(unittest.TestCase):
    def test_continent_table(self) -> None:
        codes = list(countries.known_codes())
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(countries.continent_for("es"), "EU")
        self.assertEqual(countries.continent_for("NA"), "AF")  # Namibia, not the continent code
        self.assertEqual(countries.continent_for("MX"), "NA")
        self.assertEqual(countries.continent_for("BR"), "SA")
        self.assertEqual(countries.continent_for("ZZ"), "")

    def test_summary(self) -> None:
        stats = {}
        countries.add_record(stats, "es", -0.8, 37.7, 2348049, "Aphanius iberus", "Cyprinodontidae", 2001)
        countries.add_record(stats, "ES", -0.9, 38.1, 2348049, "Aphanius iberus", "Cyprinodontidae", 1999)
        countries.add_record(stats, "ES", -3.0, 40.0, 2350580, "Gambusia affinis", "Poeciliidae", None)
        countries.add_record(stats, "ZZ", 0.0, 0.0, None, "", "", 2020)
        countries.add_record(stats, "MX", -99.1, 19.4, 2350580, "Gambusia affinis", "Poeciliidae", 2010)
        doc = countries.build_country_summary(stats, {"2348049": "EN"}, ["CR", "EN", "VU"], "v-test")
        by = {c["code"]: c for c in doc["countries"]}
        self.assertEqual(doc["countries"][0]["code"], "ES")  # most records first
        es = by["ES"]
        self.assertEqual((es["occurrence_count"], es["species_count"], es["threatened_species"]), (3, 2, 1))
        self.assertEqual(es["species"][0], {"key": 2348049, "name": "Aphanius iberus", "family": "Cyprinodontidae",
                                            "count": 2, "iucn": "EN"})
        self.assertEqual(es["bbox"], [-3.0, 37.7, -0.8, 40.0])
        self.assertEqual((es["min_year"], es["max_year"], es["continent"]), (1999, 2001, "EU"))
        self.assertEqual(by["unknown"]["records_without_species"], 1)
        conts = {c["code"]: c for c in doc["continents"]}
        self.assertEqual(conts["EU"]["species_count"], 2)
        self.assertEqual(conts["NA"]["name"], "North America")
        self.assertIn("unknown", conts)


class TestBasinAtlasFactors(unittest.TestCase):
    def test_verified_scales(self) -> None:
        spec = hydroatlas.field_spec()["scalars"]
        self.assertEqual(spec["lake_pc"]["scale"], 10)
        self.assertEqual(spec["human_footprint"]["scale"], 10)
        self.assertEqual(spec["regulation_pc"]["scale"], 10)
        self.assertEqual(spec["air_temp_c"]["scale"], 10)
        self.assertTrue(all(v["scale"] for v in spec.values()), "every scalar factor is now stated")
        self.assertEqual(hydroatlas.scaled(1000, 10), 100)
        self.assertEqual(hydroatlas.field_spec()["monthly"]["precip_mm"]["scale"], 1)


class TestReleaseCountries(TestGbifDownloadIngest):
    """Runs one build from the shared GBIF fixture and checks countries.json in the release."""

    def test_build_release_from_download(self) -> None:  # replaces the inherited build test
        gbif_dir = self._fetch()
        root = self.tmp_path / "build-r2"
        m = ingest.build_release_from_download(gbif_dir, self.hb_zip, "v0.6.0-test", root, client=self.client,
                                               basin_zooms=(0, 8), occurrence_zooms=(0, 6, 9))
        layer = m["layers"]["countries"]
        self.assertEqual(layer["path"], "/releases/v0.6.0-test/countries.json")
        doc = json.loads((root / layer["path"].lstrip("/")).read_text())
        by = {c["code"]: c for c in doc["countries"]}
        self.assertEqual(by["US"]["continent"], "NA")
        self.assertEqual(by["US"]["occurrence_count"], m["counts"]["occurrences"])
        self.assertGreater(by["US"]["species_count"], 1)


# Only the build test above should run from the inherited class.
for _name in [n for n in dir(TestGbifDownloadIngest) if n.startswith("test_") and n != "test_build_release_from_download"]:
    setattr(TestReleaseCountries, _name, None)

if __name__ == "__main__":
    unittest.main()
