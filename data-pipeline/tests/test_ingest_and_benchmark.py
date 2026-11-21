#!/usr/bin/env python3
"""Tests for the Stage 4 GBIF download ingestion, CDN-size partitioning, and range benchmark.

All network access is replaced by an in-memory fake of the GBIF REST API, and the
HydroBASINS source is written as a real HydroSHEDS-style shapefile zip, so the suite runs
offline in CI.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import random
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile
from pathlib import Path
from typing import Any, Dict, List

PIPELINE_DIR = Path(__file__).resolve().parents[1]
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import benchmark_range_pilot as bench  # noqa: E402
import hydrobasins  # noqa: E402
import ingest_gbif_download as ingest  # noqa: E402
import pmtiles_writer  # noqa: E402
import publish_release  # noqa: E402
import release_tiling  # noqa: E402

SAMPLE = PIPELINE_DIR / "sample_data"
FAKE_KEY = "0099999-261119000000001"
FAKE_DOI = "10.15468/dl.testfx"

SIMPLE_CSV_COLUMNS = [
    "gbifID", "datasetKey", "occurrenceID", "kingdom", "phylum", "class", "order", "family", "genus", "species",
    "infraspecificEpithet", "taxonRank", "scientificName", "verbatimScientificName",
    "verbatimScientificNameAuthorship", "countryCode", "locality", "stateProvince", "occurrenceStatus",
    "individualCount", "publishingOrgKey", "decimalLatitude", "decimalLongitude", "coordinateUncertaintyInMeters",
    "coordinatePrecision", "elevation", "elevationAccuracy", "depth", "depthAccuracy", "eventDate", "day", "month",
    "year", "taxonKey", "speciesKey", "basisOfRecord", "institutionCode", "collectionCode", "catalogNumber",
    "recordNumber", "identifiedBy", "dateIdentified", "license", "rightsHolder", "recordedBy", "typeStatus",
    "establishmentMeans", "lastInterpreted", "mediaType", "issue",
]
LICENSE_TO_ENUM = {
    "http://creativecommons.org/licenses/by/4.0/legalcode": "CC_BY_4_0",
    "http://creativecommons.org/publicdomain/zero/1.0/legalcode": "CC0_1_0",
}


def make_simple_csv_zip(dest: Path, extra_outside: int = 3) -> Dict[str, Any]:
    """Convert the demo TSV into a GBIF SIMPLE_CSV download zip (``<key>.csv``)."""
    with (SAMPLE / "gbif_occurrences.tsv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    out = io.StringIO()
    out.write("\t".join(SIMPLE_CSV_COLUMNS) + "\n")
    species_iucn = {}
    for r in rows:
        rec = {c: "" for c in SIMPLE_CSV_COLUMNS}
        rec.update({k: v for k, v in r.items() if k in rec})
        rec["species"] = r["scientificName"]
        rec["taxonRank"] = "SPECIES"
        rec["speciesKey"] = r["taxonKey"]
        rec["license"] = LICENSE_TO_ENUM.get(r["license"], r["license"])
        rec["occurrenceStatus"] = "PRESENT"
        rec["countryCode"] = r.get("countryCode") or "US"  # demo records are all in Oregon
        species_iucn[r["taxonKey"]] = r["iucnRedListCategory"]
        out.write("\t".join(rec[c] for c in SIMPLE_CSV_COLUMNS) + "\n")
    for i in range(extra_outside):  # Pacific Ocean records: outside every basin
        rec = {c: "" for c in SIMPLE_CSV_COLUMNS}
        rec.update({"gbifID": str(9900000000 + i), "decimalLatitude": "44.5", "decimalLongitude": "-125.9",
                    "scientificName": "Phoca vitulina", "species": "Phoca vitulina", "taxonRank": "SPECIES",
                    "taxonKey": "2434793", "speciesKey": "2434793", "year": "2021", "basisOfRecord": "HUMAN_OBSERVATION",
                    "license": "CC_BY_4_0", "datasetKey": "50c9509d-22c7-4a22-a47d-8c48425ef4a7"})
        out.write("\t".join(rec[c] for c in SIMPLE_CSV_COLUMNS) + "\n")
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{FAKE_KEY}.csv", out.getvalue())
    return {"rows": len(rows), "iucn": species_iucn}


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class FakeGbif:
    """In-memory stand-in for api.gbif.org used through GbifClient(urlopen=...)."""

    def __init__(self, zip_bytes: bytes, iucn: Dict[str, str]) -> None:
        self.zip_bytes = zip_bytes
        self.iucn = iucn
        self.polls = 0
        self.calls: List[str] = []
        self.auth_headers: List[str] = []
        self.submitted: Dict[str, Any] = {}

    def metadata(self) -> Dict[str, Any]:
        status = "RUNNING" if self.polls < 2 else "SUCCEEDED"
        return {
            "key": FAKE_KEY, "doi": FAKE_DOI, "status": status, "license": "http://creativecommons.org/licenses/by/4.0/legalcode",
            "created": "2026-11-19T18:02:11.000+00:00", "modified": "2026-11-19T18:09:40.000+00:00",
            "downloadLink": f"https://api.gbif.org/v1/occurrence/download/request/{FAKE_KEY}.zip",
            "size": len(self.zip_bytes), "totalRecords": 75, "numberDatasets": 4,
            "request": {"creator": "kenmurch", "notificationAddresses": ["private@example.org"], "sendNotification": True,
                        "format": "SIMPLE_CSV", "predicate": self.submitted.get("predicate", {"type": "and", "predicates": [
                            {"type": "within", "geometry": "POLYGON((-123.8 43.5,-121.5 43.5,-121.5 46.2,-123.8 46.2,-123.8 43.5))"}]})},
        }

    def __call__(self, req: Any, timeout: float = 0) -> FakeResponse:
        url = req.full_url
        self.calls.append(f"{req.get_method()} {url}")
        if req.get_header("Authorization"):
            self.auth_headers.append(req.get_header("Authorization"))
        path = url.split("/v1", 1)[1]
        if path.startswith(f"/occurrence/download/{FAKE_KEY}?_="):  # cache-busted status poll
            assert req.get_header("Cache-control") == "no-cache"
            path = path.split("?", 1)[0]
        if path == "/occurrence/search/predicate" and req.get_method() == "POST":
            body = json.loads(req.data.decode())
            assert body["limit"] == 0 and body["predicate"]["type"] == "and"
            return FakeResponse(json.dumps({"count": 75, "results": []}).encode())
        if path.startswith("/occurrence/search"):
            return FakeResponse(json.dumps({"count": 75, "results": []}).encode())
        if path == "/occurrence/download/request" and req.get_method() == "POST":
            self.submitted = json.loads(req.data.decode())
            return FakeResponse(FAKE_KEY.encode())
        if path == f"/occurrence/download/request/{FAKE_KEY}.zip":
            return FakeResponse(self.zip_bytes)
        if path == f"/occurrence/download/{FAKE_KEY}":
            self.polls += 1
            return FakeResponse(json.dumps(self.metadata()).encode())
        if path.startswith("/species/") and path.endswith("/iucnRedListCategory"):
            key = path.split("/")[2]
            if self.iucn.get(key) == "":
                return FakeResponse(b"", status=204)  # GBIF: species without a Red List category
            if key not in self.iucn:
                raise urllib.error.HTTPError(url, 404, "not found", {}, None)
            return FakeResponse(json.dumps({"code": self.iucn[key]}).encode())
        if path.startswith("/dataset/"):
            return FakeResponse(json.dumps({"title": "Fixture dataset " + path[-4:]}).encode())
        raise AssertionError(f"Unexpected fake GBIF call {url}")


class TestHydroBasinsReader(unittest.TestCase):
    def test_shapefile_zip_roundtrip_and_index(self) -> None:
        feats = hydrobasins.load_hydrobasins(SAMPLE / "hydrobasins_l6.geojson")
        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "hybas_na_lev06_v1c.zip"
            hydrobasins.write_shapefile_zip(feats, zpath, "hybas_na_lev06_v1c")
            back = hydrobasins.load_hydrobasins(zpath)
        self.assertEqual([f["properties"]["hybas_id"] for f in back], [f["properties"]["hybas_id"] for f in feats])
        self.assertEqual(back[0]["properties"]["pfaf_id"], "782911")
        self.assertAlmostEqual(back[0]["properties"]["sub_area_km2"], 4820.5)
        idx = hydrobasins.BasinIndex(back)
        self.assertEqual(back[idx.lookup(-123.29412, 43.97520)]["properties"]["hybas_id"], "7060041230")
        self.assertIsNone(idx.lookup(-125.9, 44.5))
        self.assertEqual(len(hydrobasins.filter_basins(back, pfaf_prefixes=["78291"])), len(back))
        self.assertEqual(hydrobasins.filter_basins(back, bbox=[0, 0, 1, 1]), [])

    def test_simplify_keeps_closed_ring(self) -> None:
        ring = [[math_x / 100.0, (math_x % 7) / 1000.0] for math_x in range(200)] + [[2, 1], [0, 1], [0, 0]]
        out = hydrobasins.simplify_ring(ring, 0.01)
        self.assertLess(len(out), len(ring))
        self.assertEqual(out[0], out[-1])
        self.assertGreaterEqual(len(out), 4)


class TestPMTilesLeafDirectoriesAndPartitions(unittest.TestCase):
    def test_leaf_directories_keep_root_in_first_16k(self) -> None:
        rng = random.Random(7)
        tiles = {}
        for _ in range(30000):
            z, x, y = 14, rng.randrange(1 << 14), rng.randrange(1 << 14)
            tiles[(z, x, y)] = rng.randbytes(rng.randrange(20, 90)) if hasattr(rng, "randbytes") else bytes(
                rng.randrange(256) for _ in range(rng.randrange(20, 90)))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "leafy.pmtiles"
            stats = pmtiles_writer.write_pmtiles(out, tiles, {"name": "leafy"}, 14, 14, [-180, -85, 180, 85], [0, 0],
                                                 tile_type=pmtiles_writer.TILE_TYPE_WEBP)
            raw = out.read_bytes()
        self.assertGreater(stats["num_leaf_directories"], 0)
        header = pmtiles_writer.parse_pmtiles_header(raw[:127])
        self.assertLessEqual(header["root_dir_offset"] + header["root_dir_bytes"], 16384)
        self.assertEqual(header["tile_type"], pmtiles_writer.TILE_TYPE_WEBP)
        self.assertEqual(header["tile_compression"], pmtiles_writer.COMPRESSION_NONE)
        calls = []
        reader = pmtiles_writer.PMTilesRangeReader(lambda o, n: (calls.append(n), raw[o:o + n])[1])
        reader.open()
        for zxy in list(tiles)[:200]:
            self.assertEqual(reader.get_tile(*zxy), tiles[zxy])
        self.assertEqual(calls[0], 16384)

    def test_partitioning_respects_budget(self) -> None:
        rng = random.Random(3)
        tiles = {}
        for z in range(0, 9):
            for x in range(min(1 << z, 24)):
                for y in range(min(1 << z, 24)):
                    mvt = pmtiles_writer.encode_mvt_tile({"l": [{
                        "id": 1, "geom_type": pmtiles_writer.GEOM_POINT, "geometry": [(x, y)],
                        "properties": {"noise": "".join(chr(65 + rng.randrange(58)) for _ in range(400))}}]})
                    tiles[(z, x, y)] = gzip.compress(mvt, mtime=0)
        total = sum(len(v) for v in tiles.values())
        budget = 200_000
        self.assertGreater(total, budget)
        with tempfile.TemporaryDirectory() as tmp:
            parts = release_tiling.write_partitioned_pmtiles(tiles, Path(tmp), "occ", "/releases/vX", {"name": "occ"},
                                                             max_bytes=budget)
            self.assertGreater(len(parts), 1)
            covered = set()
            for part in parts:
                self.assertLess(part["bytes"], budget)
                inspected = pmtiles_writer.inspect_pmtiles(Path(tmp) / Path(part["path"]).name)
                self.assertEqual(inspected["header"]["min_zoom"], part["min_zoom"])
                covered |= set(inspected["tiles"].keys())
            self.assertEqual(covered, set(tiles))

    def test_basin_tiles_reject_non_scalar_attributes(self) -> None:
        feat = {"type": "Feature", "properties": {"hybas_id": "1", "species": ["a"]},
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}
        with self.assertRaises(ValueError):
            release_tiling.build_basin_tiles([feat], 0, 1)


class TestGbifClientResilience(unittest.TestCase):
    def test_empty_204_body_means_no_category_and_transient_errors_retry(self) -> None:
        calls = {"n": 0}

        def urlopen(req: Any, timeout: float = 0) -> FakeResponse:
            calls["n"] += 1
            if "/species/1/" in req.full_url:
                return FakeResponse(b"", status=204)  # what api.gbif.org returns for unassessed taxa
            if "/species/2/" in req.full_url and calls["n"] < 4:
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
            return FakeResponse(b'{"code": "VU"}')

        client = ingest.GbifClient(urlopen=urlopen)
        with mock.patch.object(ingest.time, "sleep", lambda s: None):  # no real backoff in tests
            self.assertEqual(client.species_iucn(1), "")
            self.assertEqual(client.species_iucn(2), "VU")
            with tempfile.TemporaryDirectory() as tmp:
                iucn, _ = ingest.enrich(client, [1, 2, 3], [], Path(tmp))
                self.assertEqual(iucn, {"1": "", "2": "VU", "3": "VU"})


class TestGbifDownloadIngest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        zip_path = self.tmp_path / "fixture.zip"
        info = make_simple_csv_zip(zip_path)
        self.demo_rows = info["rows"]
        self.fake = FakeGbif(zip_path.read_bytes(), info["iucn"])
        self.client = ingest.GbifClient(urlopen=self.fake, user="kenmurch", password="s3cret")
        feats = hydrobasins.load_hydrobasins(SAMPLE / "hydrobasins_l6.geojson")
        self.hb_zip = self.tmp_path / "hydrobasins" / "hybas_na_lev06_v1c.zip"
        hydrobasins.write_shapefile_zip(feats, self.hb_zip, "hybas_na_lev06_v1c")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fetch(self) -> Path:
        query = ingest.load_query(PIPELINE_DIR / "queries" / "pilot_willamette.json")
        res = ingest.request_download(self.client, query, max_records=1000)
        self.assertEqual(res, {"key": FAKE_KEY, "expected_records": 75})
        self.assertEqual(self.fake.submitted["creator"], "kenmurch")
        self.assertTrue(self.fake.auth_headers and self.fake.auth_headers[0].startswith("Basic "))
        return ingest.fetch_download(self.client, FAKE_KEY, self.tmp_path / "work", poll_seconds=0, sleep=lambda s: None)

    def test_request_guard_and_dry_run(self) -> None:
        query = ingest.load_query(PIPELINE_DIR / "queries" / "pilot_willamette.json")
        with self.assertRaises(RuntimeError):
            ingest.request_download(self.client, query, max_records=10)
        dry = ingest.request_download(ingest.GbifClient(urlopen=self.fake), query, 10, dry_run=True)
        self.assertEqual(dry["request"]["creator"], "<GBIF_USER>")
        with self.assertRaises(PermissionError):
            ingest.request_download(ingest.GbifClient(urlopen=self.fake), query, 1000)

    def test_fetch_preserves_doi_query_and_checksums(self) -> None:
        gbif_dir = self._fetch()
        self.assertGreaterEqual(self.fake.polls, 2)  # polled through RUNNING -> SUCCEEDED
        prov = json.loads((gbif_dir / "provenance.json").read_text())
        self.assertEqual(prov["gbif_doi"], FAKE_DOI)
        self.assertEqual(prov["gbif_download_key"], FAKE_KEY)
        self.assertEqual(prov["citation"], f"GBIF.org (19 November 2026) GBIF Occurrence Download https://doi.org/{FAKE_DOI}")
        self.assertNotIn("notificationAddresses", prov["gbif_query"])
        self.assertNotIn("creator", prov["gbif_query"])
        sums = (gbif_dir / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(sums), 3)
        self.assertEqual(sums[0].split()[0], prov["zip_sha256"])
        meta = json.loads((gbif_dir / "download_metadata.json").read_text())
        self.assertEqual(meta["doi"], FAKE_DOI)  # verbatim GBIF metadata kept locally

    def test_build_release_from_download(self) -> None:
        gbif_dir = self._fetch()
        root = self.tmp_path / "build-r2"
        manifest = ingest.build_release_from_download(
            gbif_dir, self.hb_zip, "v0.2.0-test", root, previous_version="v0.1.0-demo", client=self.client,
            basin_zooms=(0, 9), occurrence_zooms=(0, 8, 11))
        rel = root / "releases" / "v0.2.0-test"
        self.assertEqual(manifest["previous_version"], "v0.1.0-demo")
        self.assertEqual(manifest["provenance"]["gbif_doi"], FAKE_DOI)
        self.assertEqual(manifest["provenance"]["gbif_download_key"], FAKE_KEY)
        self.assertIn("predicate", manifest["provenance"]["gbif_query"])
        self.assertNotIn("creator", manifest["provenance"]["gbif_query"])
        self.assertIn(f"{FAKE_KEY}.zip", manifest["provenance"]["sources"])
        self.assertIn("hybas_na_lev06_v1c.zip", manifest["provenance"]["sources"])
        self.assertEqual(manifest["counts"]["occurrences"], self.demo_rows)
        self.assertEqual(manifest["counts"]["unassigned_occurrences"], 3)
        self.assertEqual(manifest["counts"]["basins"], 6)
        self.assertTrue(manifest["provenance"]["iucn_source"].startswith("GBIF API"))
        self.assertEqual(manifest["provenance"]["hydrobasins"]["level"], 6)

        # Every archive sits under the CDN budget and is checksummed.
        for name in ("basins", "occurrences"):
            for part in manifest["layers"][name]["partitions"]:
                f = root / part["path"].lstrip("/")
                self.assertLess(f.stat().st_size, release_tiling.MAX_ARCHIVE_BYTES)
                self.assertEqual(publish_release.sha256_file(f), part["sha256"])

        # Scalar-only basin tiles.
        insp = pmtiles_writer.inspect_pmtiles(rel / "basins.pmtiles")
        for tile in insp["tiles"].values():
            for feat in tile["basins"]["features"]:
                self.assertTrue(set(feat["properties"]) <= set(release_tiling.BASIN_SCALAR_FIELDS))

        # Overview zooms are aggregated bins, detail zooms are records.
        occ = pmtiles_writer.inspect_pmtiles(rel / "occurrences.pmtiles")
        low = [t for (z, _, _), t in occ["tiles"].items() if z < 8]
        high = [t for (z, _, _), t in occ["tiles"].items() if z >= 8]
        self.assertTrue(all("occurrence_bins" in t and "occurrences" not in t for t in low))
        self.assertTrue(all("occurrences" in t for t in high))
        self.assertEqual(sum(f["properties"]["count"] for t in low[:1] for f in t["occurrence_bins"]["features"]),
                         self.demo_rows)

        # Shards hold the species checklists; threatened counts derived from IUCN lookups.
        total, threatened = 0, 0
        for ref in json.loads((rel / "basins" / "index.json").read_text()):
            shard = json.loads((root / ref["path"].lstrip("/")).read_text())
            total += shard["basin"]["occurrence_count"]
            threatened += shard["basin"]["threatened_count"]
            self.assertTrue(all(isinstance(s["count"], int) for s in shard["species"]))
            self.assertTrue(all(d["datasetName"] for d in shard["datasets"]))
        self.assertEqual(total, self.demo_rows)
        self.assertGreater(threatened, 0)

        # Root manifest stays small; publish plan verifies checksums and puts manifest last.
        root_manifest = json.loads((root / "releases" / "manifest.json").read_text())
        self.assertEqual(len(root_manifest["layers"]["basin_shards"]), 1)
        plan = publish_release.build_upload_plan(root, "v0.2.0-test")
        self.assertEqual(plan[-1]["key"], "releases/manifest.json")
        res = publish_release.publish_release(root, "v0.2.0-test", mode="wrangler",
                                              bucket="basinmap-public-data-staging", dry_run=True)
        self.assertIn("basinmap-public-data-staging/releases/manifest.json", res["commands"][-1])
        rc = publish_release.publish_release(root, "v0.2.0-test", mode="rclone",
                                             bucket="basinmap-public-data-staging", dry_run=True)
        self.assertTrue(rc["commands"][-1].startswith("rclone copyto"))
        self.assertIn("r2:basinmap-public-data-staging/releases/manifest.json", rc["commands"][-1])
        self.assertIn("Cache-Control: public, max-age=60, must-revalidate", rc["commands"][-1])
        orig = publish_release.WRANGLER_MAX_UPLOAD_BYTES
        publish_release.WRANGLER_MAX_UPLOAD_BYTES = 1000
        try:
            with self.assertRaises(ValueError):
                publish_release.publish_release(root, "v0.2.0-test", mode="wrangler", dry_run=True)
        finally:
            publish_release.WRANGLER_MAX_UPLOAD_BYTES = orig
        http_res = publish_release.verify_http_delivery(root=root)
        self.assertEqual(http_res["current_version"], "v0.2.0-test")

        # Immutable releases.
        with self.assertRaises(FileExistsError):
            ingest.build_release_from_download(gbif_dir, self.hb_zip, "v0.2.0-test", root, client=None)

    def test_tampered_source_is_rejected(self) -> None:
        gbif_dir = self._fetch()
        with (gbif_dir / f"{FAKE_KEY}.zip").open("ab") as f:
            f.write(b"tamper")
        with self.assertRaises(ValueError):
            ingest.build_release_from_download(gbif_dir, self.hb_zip, "v0.2.0-bad", self.tmp_path / "r2")

    def test_sqlite_shards_and_raster_pack(self) -> None:
        gbif_dir = self._fetch()
        raster_dir = self.tmp_path / "raster"
        webp = b"RIFF\x1a\x00\x00\x00WEBPVP8L\x0d\x00\x00\x00\x2f\x00\x00\x00\x10\x07\x10\x11\x11\x88\x88\xfe\x07\x00"
        for z, x, y in ((0, 0, 0), (1, 0, 0), (1, 0, 1)):
            p = raster_dir / str(z) / str(x) / f"{y}.webp"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(webp)
        root = self.tmp_path / "r2s"
        manifest = ingest.build_release_from_download(
            gbif_dir, self.hb_zip, "v0.2.0-sq", root, shard_format="sqlite", raster_tiles_dir=raster_dir,
            basin_zooms=(0, 6), occurrence_zooms=(0, 6, 8))
        self.assertEqual(manifest["layers"]["basin_shards_format"], "sqlite")
        import sqlite3
        db = root / manifest["layers"]["basin_shards"][0]["path"].lstrip("/")
        con = sqlite3.connect(db)
        n = con.execute("SELECT COUNT(*) FROM basin_shards").fetchone()[0]
        con.close()
        self.assertEqual(n, 6)
        raster = manifest["layers"]["rasters"][0]
        hdr = pmtiles_writer.parse_pmtiles_header((root / raster["path"].lstrip("/")).read_bytes()[:127])
        self.assertEqual(hdr["tile_type"], pmtiles_writer.TILE_TYPE_WEBP)
        self.assertTrue(manifest["provenance"]["iucn_source"].startswith("unavailable"))


class TestBenchmark(unittest.TestCase):
    def test_cost_formulas_match_section_7_table(self) -> None:
        # Section 7.2 rows at 20,000 visits/month.
        self.assertEqual(bench.r2_monthly_cost(50, 2_000_000)["total"], 0.60)
        self.assertEqual(bench.r2_monthly_cost(100, 10_000_000)["total"], 1.35)
        self.assertEqual(bench.r2_monthly_cost(50, 20_000_000)["total"], 4.20)
        self.assertEqual(bench.r2_monthly_cost(100, 40_000_000)["total"], 12.15)
        self.assertEqual(bench.r2_monthly_cost(50, 100_000_000)["total"], 33.00)
        self.assertEqual(round(bench.r2_monthly_cost(100, 100_000_000)["total"] + 5, 2), 38.75)

    def test_viewport_tile_math(self) -> None:
        vp = bench.Viewport("t", -122.6, 44.8, 7.5)
        desk = bench.tiles_for_viewport(vp, 1440, 900, 0, 12)
        mob = bench.tiles_for_viewport(vp, 390, 844, 0, 12)
        self.assertTrue(all(z == 7 for z, _, _ in desk))
        self.assertGreater(len(desk), len(mob))
        self.assertEqual(bench.tiles_for_viewport(vp, 1440, 900, 9, 12), [])
        over = bench.tiles_for_viewport(bench.Viewport("o", -122.6, 44.8, 15), 390, 844, 0, 12)
        self.assertTrue(all(z == 12 for z, _, _ in over))

    def test_benchmark_against_local_release_root(self) -> None:
        root = PIPELINE_DIR.parent / "public-r2"
        transport = bench.socketpair_transport(root)
        result = bench.run_benchmark(transport, "local", profiles=("desktop", "mobile"), runs=1,
                                     visits=(20_000,), storage_gb=(50.0,), probe_paths=())
        for prof in ("desktop", "mobile"):
            for pname in ("natural", "cold", "warm"):
                s = result["profiles"][prof][pname]
                self.assertEqual(s["errors"], 0)
                self.assertEqual(s["range_206_ok"], s["range_requests"])
                self.assertGreater(s["range_requests"], 2)
                # No CDN locally: every request is an R2 read.
                self.assertEqual(s["r2_reads"], s["requests"])
        self.assertTrue(result["cost_projection"])
        self.assertIn("Projected monthly R2 cost", bench.markdown_report(result))


if __name__ == "__main__":
    unittest.main()
