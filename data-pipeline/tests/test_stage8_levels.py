#!/usr/bin/env python3
"""Stage 8 tests: BasinATLAS extraction (streaming DBF, CC BY columns only, temperature scaling)
and releases with HydroBASINS Levels 3 / 6 / 8 plus environment values."""

from __future__ import annotations

import io
import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

PIPELINE_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for p in (PIPELINE_DIR, TESTS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import hydroatlas  # noqa: E402
import hydrobasins  # noqa: E402
import ingest_gbif_download as ingest  # noqa: E402
import publish_release  # noqa: E402
from test_ingest_and_benchmark import FAKE_KEY, SAMPLE, FakeGbif, make_simple_csv_zip  # noqa: E402


def write_dbf(rows: Sequence[Dict[str, Any]], fields: Sequence[Tuple[str, str, int, int]]) -> bytes:
    record_len = 1 + sum(f[2] for f in fields)
    header_len = 32 + 32 * len(fields) + 1
    out = io.BytesIO()
    out.write(struct.pack("<4BIHH20x", 3, 126, 11, 21, len(rows), header_len, record_len))
    for name, ftype, flen, fdec in fields:
        out.write(struct.pack("<11sc4xBB14x", name.encode("ascii"), ftype.encode("ascii"), flen, fdec))
    out.write(b"\x0d")
    for row in rows:
        out.write(b" ")
        for name, ftype, flen, fdec in fields:
            v = row.get(name)
            text = "" if v is None else (f"{v:.{fdec}f}" if fdec else str(v))
            out.write(text.rjust(flen)[:flen].encode("latin-1"))
    out.write(b"\x1a")
    return out.getvalue()


def atlas_fields() -> List[Tuple[str, str, int, int]]:
    cols = [c for c in hydroatlas.wanted_columns() if c != "HYBAS_ID"]
    # BasinATLAS stores lower-case column names; add an unrelated column and an ODbL soil column
    return ([("HYBAS_ID", "N", 11, 0)] + [(c, "N", 10, 0) for c in cols]
            + [("cly_pc_sav", "N", 6, 0), ("riv_tc_ssu", "N", 12, 0)])


def atlas_row(hybas_id: int, seed: int) -> Dict[str, Any]:
    row: Dict[str, Any] = {"HYBAS_ID": hybas_id, "cly_pc_sav": 30, "riv_tc_ssu": 5}
    for _, col, _, _, _ in hydroatlas.SCALAR_FIELDS:
        row[col] = seed
    for _, col, _ in hydroatlas.CLASS_FIELDS:
        row[col] = 3
    for m in range(1, 13):
        row[f"tmp_dc_s{m:02d}"] = 100 + m * 10 + seed  # tenths of a degree
        row[f"pre_mm_s{m:02d}"] = 20 * m
    row["tmp_dc_syr"] = 125
    return row


def write_atlas_zip(path: Path, levels: Dict[int, Sequence[int]]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for lvl, ids in levels.items():
            raw = write_dbf([atlas_row(h, i) for i, h in enumerate(ids)], atlas_fields())
            zf.writestr(f"BasinATLAS_v10_shp/BasinATLAS_v10_lev{lvl:02d}.dbf", raw)
            zf.writestr(f"BasinATLAS_v10_shp/BasinATLAS_v10_lev{lvl:02d}.shp", b"")


def l6_features() -> List[Dict[str, Any]]:
    return hydrobasins.load_hydrobasins(SAMPLE / "hydrobasins_l6.geojson")


def l8_features(l6: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Split every Level 6 polygon's bbox into two Level 8 children (west / east halves)."""
    out = []
    for f in l6:
        w, s, e, n = hydrobasins.geometry_bbox(f["geometry"])
        mid = (w + e) / 2
        for i, (x0, x1) in enumerate(((w, mid), (mid, e)), start=1):
            props = dict(f["properties"])
            props["hybas_id"] = str(int(props["hybas_id"]) * 10 + i)
            props["pfaf_id"] = str(props["pfaf_id"]) + f"1{i}"
            out.append({"type": "Feature", "properties": props, "geometry": {"type": "Polygon", "coordinates": [
                [[x0, s], [x1, s], [x1, n], [x0, n], [x0, s]]]}})
    return out


def l3_features(l6: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bbs = [hydrobasins.geometry_bbox(f["geometry"]) for f in l6]
    w, s, e, n = min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs)
    props = dict(l6[0]["properties"], hybas_id="7030000010", pfaf_id=str(l6[0]["properties"]["pfaf_id"])[:3])
    return [{"type": "Feature", "properties": props, "geometry": {"type": "Polygon", "coordinates": [
        [[w - 0.5, s - 0.5], [e + 0.5, s - 0.5], [e + 0.5, n + 0.5], [w - 0.5, n + 0.5], [w - 0.5, s - 0.5]]]}}]


class TestBasinAtlas(unittest.TestCase):
    def test_extract_selects_cc_by_columns_scales_temperature_and_records_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            z = Path(tmp) / "BasinATLAS_Data_v10_shp.zip"
            write_atlas_zip(z, {6: [7060041230, 7060041240], 8: [70600412301]})
            data = hydroatlas.extract_level(z, 6)
            self.assertEqual((data["level"], data["count"]), (6, 2))
            row = data["basins"]["7060041230"]
            self.assertNotIn("cly_pc_sav", json.dumps(row))
            self.assertEqual(row["m"]["air_temp_c"][0], 110)  # stored value kept
            env = hydroatlas.basin_environment(data, "7060041230")
            self.assertEqual(env["monthly"]["air_temp_c"][:2], [11.0, 12.0])  # tenths of a degree -> degrees
            self.assertEqual(env["scalars"]["air_temp_c"], 12.5)
            self.assertEqual(env["monthly"]["precip_mm"][11], 240)
            self.assertEqual(data["observed_ranges"]["forest_pc"], [0, 1])
            self.assertIn("ODbL", data["fields"]["source"]["excluded"])
            self.assertEqual(set(hydroatlas.tile_scalars(data, "7060041240")),
                             {"env_air_temp_c", "env_precip_mm", "env_elevation_m", "env_forest_pc", "env_lake_pc",
                              "env_protected_pc", "env_human_footprint"})
            self.assertIsNone(hydroatlas.basin_environment(data, "999"))
            out = Path(tmp) / "out"
            self.assertEqual(hydroatlas.main(["extract", "--source", str(z), "--levels", "6", "8", "--out-dir", str(out)]), 0)
            self.assertEqual(json.loads((out / "basinatlas_lev08.json").read_text())["count"], 1)
            with self.assertRaises(FileNotFoundError):
                hydroatlas.extract_level(z, 3)

    def test_missing_column_is_an_error(self) -> None:
        raw = write_dbf([{"HYBAS_ID": 1}], [("HYBAS_ID", "N", 11, 0)])
        with self.assertRaises(KeyError):
            list(hydroatlas.iter_dbf_columns(io.BytesIO(raw), hydroatlas.wanted_columns()))

    def test_download_verifies_md5_and_reuses_file(self) -> None:
        import hashlib
        payload = b"x" * 5000

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()

        calls = []

        def fake(req, timeout=0):
            calls.append(req.full_url)
            return Resp(payload)

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.zip"
            md5 = hashlib.md5(payload).hexdigest()
            res = hydroatlas.download("https://example.org/a", dest, md5, len(payload), urlopen=fake)
            self.assertFalse(res["reused"])
            self.assertTrue(hydroatlas.download("https://example.org/a", dest, md5, urlopen=fake)["reused"])
            self.assertEqual(len(calls), 1)
            with self.assertRaises(IOError):
                hydroatlas.download("https://example.org/a", Path(tmp) / "b.zip", "0" * 32, urlopen=fake, retries=0)


class TestMultiLevelRelease(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        zip_path = t / "fixture.zip"
        info = make_simple_csv_zip(zip_path)
        self.rows = info["rows"]
        self.client = ingest.GbifClient(urlopen=FakeGbif(zip_path.read_bytes(), info["iucn"]), user="u", password="p")
        ingest.request_download(self.client, ingest.load_query(PIPELINE_DIR / "queries" / "pilot_willamette.json"), 10_000)
        self.gbif_dir = ingest.fetch_download(self.client, FAKE_KEY, t / "work", poll_seconds=0, sleep=lambda s: None)
        hb = t / "hb"
        l6 = l6_features()
        self.l6, self.l8, self.l3 = l6, l8_features(l6), l3_features(l6)
        hydrobasins.write_shapefile_zip(l6, hb / "hybas_na_lev06_v1c.zip", "hybas_na_lev06_v1c")
        hydrobasins.write_shapefile_zip(self.l8, hb / "hybas_na_lev08_v1c.zip", "hybas_na_lev08_v1c")
        hydrobasins.write_shapefile_zip(self.l3, hb / "hybas_na_lev03_v1c.zip", "hybas_na_lev03_v1c")
        self.hb = hb
        atlas_zip = t / "BasinATLAS_Data_v10_shp.zip"
        write_atlas_zip(atlas_zip, {6: [int(f["properties"]["hybas_id"]) for f in l6],
                                    8: [int(f["properties"]["hybas_id"]) for f in self.l8],
                                    3: [int(f["properties"]["hybas_id"]) for f in self.l3]})
        self.atlas_dir = t / "atlas"
        hydroatlas.main(["extract", "--source", str(atlas_zip), "--levels", "3", "6", "8", "--out-dir", str(self.atlas_dir)])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_levels_3_6_8_with_environment(self) -> None:
        root = Path(self.tmp.name) / "build-r2"
        extra = ingest.group_hydrobasins_by_level([self.hb / "hybas_na_lev03_v1c.zip", self.hb / "hybas_na_lev08_v1c.zip"])
        self.assertEqual(sorted(extra), [3, 8])
        m = ingest.build_release_from_download(
            self.gbif_dir, self.hb / "hybas_na_lev06_v1c.zip", "v0.4.0-test", root, client=self.client,
            basin_zooms=(0, 8), occurrence_zooms=(0, 6, 9), extra_levels=extra, basinatlas_dir=self.atlas_dir,
            level_zooms={8: (4, 8)})
        rel = root / "releases" / "v0.4.0-test"
        levels = m["layers"]["basin_levels"]
        self.assertEqual(sorted(levels), ["03", "06", "08"])
        self.assertTrue(levels["06"]["primary"])
        self.assertEqual((levels["08"]["min_zoom"], levels["08"]["max_zoom"]), (4, 8))
        self.assertEqual(levels["08"]["shard_grouping"], "parent_level_06")
        self.assertEqual(levels["03"]["shard_grouping"], "basin")
        self.assertEqual((m["counts"]["basins_l03"], m["counts"]["basins_l08"]), (1, len(self.l8)))
        # Level 3 contains every Level 6 basin, so it receives every record assigned at Level 6
        l3_shard = json.loads((rel / "basins_l03" / "7030000010.json").read_text())
        self.assertEqual(l3_shard["basin"]["occurrence_count"], levels["03"]["assigned_occurrences"])
        self.assertGreater(levels["03"]["assigned_occurrences"], 0)
        self.assertGreaterEqual(levels["03"]["assigned_occurrences"], levels["08"]["assigned_occurrences"])
        # Level 8 shards are grouped by Level 6 parent: one file per Level 6 basin, two children each
        l8_files = sorted((rel / "basins_l08").glob("*.json"))
        self.assertEqual(len(l8_files), len(self.l6))
        doc = json.loads(l8_files[0].read_text())
        self.assertEqual(doc["parent_level"], 6)
        self.assertEqual(len(doc["basins"]), 2)
        child = next(iter(doc["basins"].values()))
        self.assertEqual(child["basin"]["parent_hybas_id"], doc["parent_hybas_id"])
        self.assertEqual(len(child["environment"]["monthly"]["air_temp_c"]), 12)
        self.assertEqual(sum(b["basin"]["occurrence_count"] for f in l8_files
                             for b in json.loads(f.read_text())["basins"].values()),
                         levels["08"]["assigned_occurrences"])
        # Primary level gets BasinATLAS values in tiles (scalars) and shards (monthly series)
        primary = json.loads(next((rel / "basins").glob("70*.json")).read_text())
        self.assertIn("environment", primary)
        self.assertIn("env_air_temp_c", primary["basin"])
        self.assertEqual(m["environment"]["levels"], ["03", "06", "08"])
        self.assertEqual(m["environment"]["scalars"]["air_temp_c"]["scale"], 10)
        self.assertIn("BasinATLAS", m["environment"]["source"]["name"])
        # Every archive (extra levels included) is checksummed and uploadable
        specs = publish_release.iter_archive_specs(m)
        self.assertTrue(any("basins_l08" in s["path"] for s in specs))
        plan = publish_release.build_upload_plan(root, "v0.4.0-test")
        keys = [p["key"] for p in plan]
        self.assertEqual(keys[-1], "releases/manifest.json")
        self.assertTrue(any(k.startswith("releases/v0.4.0-test/basins_l03/") for k in keys))
        self.assertTrue(any(k.endswith("basins_l08.pmtiles") for k in keys))

    def test_without_stage8_inputs_manifest_is_unchanged(self) -> None:
        root = Path(self.tmp.name) / "build-r2"
        m = ingest.build_release_from_download(self.gbif_dir, self.hb / "hybas_na_lev06_v1c.zip", "v0.4.0-plain", root,
                                               client=self.client, basin_zooms=(0, 8), occurrence_zooms=(0, 6, 9))
        self.assertNotIn("basin_levels", m["layers"])
        self.assertNotIn("environment", m)

    def test_level_mismatch_and_cli(self) -> None:
        bad = self.hb / "hybas_na_lev08_v1c_bad.zip"
        hydrobasins.write_shapefile_zip(self.l6, bad, "hybas_na_lev08_v1c")  # level-6 codes in a lev08 file
        with self.assertRaises(ValueError):
            ingest.build_release_from_download(self.gbif_dir, self.hb / "hybas_na_lev06_v1c.zip", "v0.4.0-bad",
                                               Path(self.tmp.name) / "r", client=self.client,
                                               extra_levels={8: [bad]})
        args = ingest.parse_args(["build", "--gbif-dir", str(self.gbif_dir), "--hydrobasins", str(self.hb / "hybas_na_lev06_v1c.zip"),
                                  "--extra-hydrobasins", str(self.hb / "hybas_na_lev08_v1c.zip"),
                                  "--basinatlas-dir", str(self.atlas_dir), "--level-zoom", "8", "5", "9"])
        self.assertEqual(args.level_zoom, [[8, 5, 9]])
        with self.assertRaises(ValueError):
            ingest.group_hydrobasins_by_level([Path("basins.zip")])


if __name__ == "__main__":
    unittest.main()
