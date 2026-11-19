#!/usr/bin/env python3
"""Unit and integration tests for the basinmap.org spatial data pipeline."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parents[1]
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import build_release  # noqa: E402
import pmtiles_writer  # noqa: E402
import publish_release  # noqa: E402


class TestPMTilesAndMVT(unittest.TestCase):
    def test_hilbert_roundtrip_and_continuity(self) -> None:
        self.assertEqual(pmtiles_writer.zxy_to_tile_id(0, 0, 0), 0)
        self.assertEqual(pmtiles_writer.tile_id_to_zxy(0), (0, 0, 0))

        for z in range(1, 5):
            n = 1 << z
            acc = ((1 << (2 * z)) - 1) // 3
            seen = set()
            coords_by_d = []
            for d in range(n * n):
                tid = acc + d
                rz, rx, ry = pmtiles_writer.tile_id_to_zxy(tid)
                self.assertEqual(rz, z)
                self.assertEqual(pmtiles_writer.zxy_to_tile_id(rz, rx, ry), tid)
                seen.add((rx, ry))
                coords_by_d.append((rx, ry))
            self.assertEqual(len(seen), n * n)

            # Every adjacent step along the Hilbert curve must move by Manhattan distance 1.
            for idx in range(len(coords_by_d) - 1):
                x1, y1 = coords_by_d[idx]
                x2, y2 = coords_by_d[idx + 1]
                self.assertEqual(abs(x1 - x2) + abs(y1 - y2), 1)

    def test_mvt_encode_decode_and_pmtiles_roundtrip(self) -> None:
        mvt_bytes = pmtiles_writer.encode_mvt_tile(
            {
                "basins": [
                    {
                        "id": 1,
                        "geom_type": pmtiles_writer.GEOM_POLYGON,
                        "geometry": [[(100, 100), (100, 500), (500, 500), (500, 100)]],
                        "properties": {
                            "hybas_id": "7060041230",
                            "occurrence_count": 12,
                            "sub_area_km2": 4820.5,
                        },
                    }
                ],
                "occurrences": [
                    {
                        "id": 42,
                        "geom_type": pmtiles_writer.GEOM_POINT,
                        "geometry": [(250, 250)],
                        "properties": {"scientificName": "Oncorhynchus tshawytscha"},
                    }
                ],
            }
        )
        decoded = pmtiles_writer.decode_mvt_tile(mvt_bytes)
        self.assertIn("basins", decoded)
        self.assertIn("occurrences", decoded)
        self.assertEqual(decoded["basins"]["features"][0]["properties"]["hybas_id"], "7060041230")
        self.assertEqual(decoded["basins"]["features"][0]["properties"]["occurrence_count"], 12)
        self.assertEqual(
            decoded["occurrences"]["features"][0]["properties"]["scientificName"],
            "Oncorhynchus tshawytscha",
        )

        with tempfile.TemporaryDirectory() as tmp:
            out_pm = Path(tmp) / "test.pmtiles"
            stats = pmtiles_writer.write_pmtiles(
                output_path=out_pm,
                tiles_by_zxy={(0, 0, 0): mvt_bytes, (4, 2, 5): mvt_bytes},
                metadata={"name": "test"},
                min_zoom=0,
                max_zoom=4,
                bounds=[-123.6, 43.6, -121.6, 46.02],
                center=[-122.6, 44.8],
            )
            self.assertEqual(stats["num_addressed_tiles"], 2)
            # Identical tile content must be deduplicated into 1 blob.
            self.assertEqual(stats["num_tile_contents"], 1)

            inspected = pmtiles_writer.inspect_pmtiles(out_pm)
            self.assertEqual(inspected["header"]["magic"], "PMTiles\x03")
            self.assertEqual(inspected["metadata"]["name"], "test")
            self.assertIn((0, 0, 0), inspected["tiles"])
            self.assertIn((4, 2, 5), inspected["tiles"])


class TestBuildPublishAndCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self.source_dir = PIPELINE_DIR / "sample_data"

    def test_build_release_aggregation_and_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "r2"
            manifest = build_release.build_release(
                version="v0.1.0-test",
                source_dir=self.source_dir,
                output_root=root,
            )
            self.assertEqual(manifest["current_version"], "v0.1.0-test")
            self.assertIsNone(manifest["previous_version"])
            self.assertEqual(manifest["counts"], {"occurrences": 72, "species": 10, "basins": 6})
            self.assertEqual(manifest["provenance"]["gbif_doi"], "10.15468/dl.basinmap-demo-2026")

            # Confirm immutable release protection unless --force.
            with self.assertRaises(FileExistsError):
                build_release.build_release(
                    version="v0.1.0-test",
                    source_dir=self.source_dir,
                    output_root=root,
                    force=False,
                )

            # Inspect basins.pmtiles to ensure scalar properties only (no embedded species lists).
            basins_pm = root / "releases" / "v0.1.0-test" / "basins.pmtiles"
            inspected = pmtiles_writer.inspect_pmtiles(basins_pm)
            sample_tile = next(iter(inspected["tiles"].values()))
            basin_feats = sample_tile["basins"]["features"]
            self.assertGreater(len(basin_feats), 0)
            for feat in basin_feats:
                props = feat["properties"]
                self.assertIn("occurrence_count", props)
                self.assertIn("species_count", props)
                self.assertIn("shard_path", props)
                self.assertNotIn("species", props)
                self.assertNotIn("species_list", props)

            # Inspect all 6 basin JSON shards.
            shard_total_occ = 0
            for shard_meta in manifest["layers"]["basin_shards"]:
                shard_file = root / shard_meta["path"].lstrip("/")
                self.assertTrue(shard_file.exists())
                shard = json.loads(shard_file.read_text(encoding="utf-8"))
                shard_total_occ += shard["basin"]["occurrence_count"]
                self.assertGreater(len(shard["species"]), 0)
                self.assertTrue(shard["temporal_histogram"])
            self.assertEqual(shard_total_occ, 72)

    def test_publish_order_http_206_and_manifest_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "r2"

            # Create permanent non-release assets that cleanup MUST NEVER touch.
            media_file = root / "media" / "550e8400-e29b-41d4-a716-446655440000.jpg"
            export_file = root / "exports" / "dwca-latest.zip"
            overlay_file = root / "overlays" / "approved.geojson"
            for fpath, body in (
                (media_file, b"fake-jpeg"),
                (export_file, b"fake-dwca"),
                (overlay_file, b'{"type":"FeatureCollection","features":[]}'),
            ):
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_bytes(body)

            # Build three sequential releases.
            build_release.build_release("v0.1.0", self.source_dir, root)
            build_release.build_release("v0.2.0", self.source_dir, root)
            manifest = build_release.build_release("v0.3.0", self.source_dir, root)
            self.assertEqual(manifest["current_version"], "v0.3.0")
            self.assertEqual(manifest["previous_version"], "v0.2.0")

            # Verify upload plan order: immutable versioned assets first, manifest.json last.
            staged = tmp_path / "staged-r2"
            pub_res = publish_release.publish_release(
                root=root,
                version="v0.3.0",
                mode="local",
                dest=staged,
            )
            plan = pub_res["plan"]
            self.assertGreater(len(plan), 5)
            self.assertEqual(plan[-1]["key"], "releases/manifest.json")
            self.assertEqual(plan[-1]["cache_control"], publish_release.MUTABLE_CACHE_CONTROL)
            for item in plan[:-1]:
                self.assertTrue(item["key"].startswith("releases/v0.3.0/"))
                self.assertEqual(item["cache_control"], publish_release.IMMUTABLE_CACHE_CONTROL)

            # Verify wrangler dry-run output.
            wrangler_res = publish_release.publish_release(
                root=root,
                version="v0.3.0",
                mode="wrangler",
                dry_run=True,
            )
            self.assertTrue(wrangler_res["commands"][-1].startswith("npx wrangler r2 object put basinmap-public-data/releases/manifest.json"))

            # Verify HTTP 206 byte-range, ETag, Cache-Control, and CORS on v0.3.0.
            http_res = publish_release.verify_http_delivery(root=root)
            self.assertEqual(http_res["current_version"], "v0.3.0")
            self.assertEqual(http_res["verified_layers"]["basins"]["status"], 206)
            self.assertEqual(http_res["verified_layers"]["occurrences"]["status"], 206)

            # Verify manifest-aware cleanup removes v0.1.0, keeps v0.2.0 + v0.3.0, and spares /media, /exports, /overlays.
            clean_res = publish_release.cleanup_releases(root=root)
            self.assertEqual(clean_res["deleted"], ["v0.1.0"])
            self.assertEqual(set(clean_res["retained"]), {"v0.2.0", "v0.3.0"})
            self.assertFalse((root / "releases" / "v0.1.0").exists())
            self.assertTrue((root / "releases" / "v0.2.0").is_dir())
            self.assertTrue((root / "releases" / "v0.3.0").is_dir())
            self.assertTrue((root / "releases" / "manifest.json").is_file())
            self.assertTrue(media_file.is_file())
            self.assertTrue(export_file.is_file())
            self.assertTrue(overlay_file.is_file())


if __name__ == "__main__":
    unittest.main()
