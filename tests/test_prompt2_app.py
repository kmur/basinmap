#!/usr/bin/env python3
"""Automated tests for Prompt 2: Cloudflare config, frontend bundle, Worker shell, and resilience."""

from __future__ import annotations

import functools
import http.client
import json
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
PIPELINE_DIR = ROOT_DIR / "data-pipeline"
for p in (str(SCRIPTS_DIR), str(PIPELINE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import build_dist  # noqa: E402
import dev_server  # noqa: E402
import pmtiles_writer  # noqa: E402


def _request_handler(
    path: str,
    simulate_outage: bool = False,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict[str, str], bytes]:
    """Execute an HTTP request against LocalBasinmapHandler via TCP or socketpair."""
    handler_cls = functools.partial(
        dev_server.LocalBasinmapHandler,
        dist_dir=ROOT_DIR / "dist",
        r2_dir=ROOT_DIR / "public-r2",
        simulate_api_outage=simulate_outage,
    )

    client_sock, server_sock = socket.socketpair()
    err_holder: List[BaseException] = []

    def serve_one() -> None:
        try:
            handler_cls(server_sock, ("127.0.0.1", 0), None)
        except BaseException as exc:  # pragma: no cover
            err_holder.append(exc)
        finally:
            try:
                server_sock.close()
            except OSError:
                pass

    t = threading.Thread(target=serve_one, daemon=True)
    t.start()
    try:
        req_lines = [f"{method} {path} HTTP/1.0", "Host: basinmap.org"]
        for k, v in (headers or {}).items():
            req_lines.append(f"{k}: {v}")
        client_sock.sendall(("\r\n".join(req_lines) + "\r\n\r\n").encode("ascii"))

        resp = http.client.HTTPResponse(client_sock, method=method)
        resp.begin()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        body = b"" if method == "HEAD" else resp.read()
        status = resp.status
    finally:
        client_sock.close()
        t.join(timeout=5)

    if err_holder:
        raise err_holder[0]
    return status, resp_headers, body


class TestPrompt2App(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        build_dist.build_dist()

    def test_wrangler_and_project_config(self) -> None:
        raw_jsonc = (ROOT_DIR / "wrangler.jsonc").read_text(encoding="utf-8")
        # Strip // comments if present while preserving URLs
        cleaned = re.sub(r"^\s*//.*$", "", raw_jsonc, flags=re.MULTILINE)
        cfg = json.loads(cleaned)

        self.assertEqual(cfg["name"], "basinmap")
        self.assertEqual(cfg["main"], "src/worker/index.ts")
        self.assertEqual(cfg["routes"][0]["pattern"], "basinmap.org")
        self.assertEqual(cfg["assets"]["directory"], "./dist")
        self.assertEqual(cfg["assets"]["run_worker_first"], ["/api/*"])
        self.assertEqual(cfg["d1_databases"][0]["binding"], "DB")
        self.assertEqual(cfg["d1_databases"][0]["database_name"], "basinmap-db")

        r2_bindings = {b["binding"]: b["bucket_name"] for b in cfg["r2_buckets"]}
        self.assertEqual(r2_bindings["PUBLIC_BUCKET"], "basinmap-public-data")
        self.assertEqual(r2_bindings["INTAKE_BUCKET"], "basinmap-intake-media")
        self.assertEqual(cfg["vars"]["PUBLIC_DATA_URL"], "https://data.basinmap.org")

        staging = cfg["env"]["staging"]
        self.assertEqual(staging["name"], "basinmap-staging")
        self.assertEqual(staging["routes"][0]["pattern"], "staging.basinmap.org")
        self.assertEqual(staging["d1_databases"][0]["database_name"], "basinmap-db-staging")
        staging_r2 = {b["binding"]: b["bucket_name"] for b in staging["r2_buckets"]}
        self.assertEqual(staging_r2["PUBLIC_BUCKET"], "basinmap-public-data-staging")
        self.assertEqual(staging_r2["INTAKE_BUCKET"], "basinmap-intake-media-staging")
        self.assertEqual(staging["vars"]["PUBLIC_DATA_URL"], "/api/r2")

        worker_ts = (ROOT_DIR / "src" / "worker" / "index.ts").read_text(encoding="utf-8")
        self.assertIn("/api/health", worker_ts)
        self.assertIn("/api/config", worker_ts)
        self.assertIn("/api/r2/", worker_ts)
        self.assertIn("env.ASSETS.fetch(request)", worker_ts)

    def test_build_dist_and_frontend_bundle(self) -> None:
        dist = ROOT_DIR / "dist"
        for rel in (
            "index.html",
            "assets/styles.css",
            "assets/types.js",
            "assets/app.js",
            "releases/manifest.json",
            "releases/v0.1.0-demo/basins.pmtiles",
            "releases/v0.1.0-demo/occurrences.pmtiles",
            "overlays/approved.geojson",
        ):
            self.assertTrue((dist / rel).is_file(), f"Missing built artifact: {rel}")

        html = (dist / "index.html").read_text(encoding="utf-8")
        self.assertIn("basinmap.org", html)
        self.assertIn("/assets/app.js", html)

    def test_worker_api_and_static_range_delivery(self) -> None:
        status, headers, body = _request_handler("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "private, no-store")
        health = json.loads(body.decode("utf-8"))
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["bindings"]["d1"])
        self.assertTrue(health["bindings"]["public_r2"])
        self.assertTrue(health["bindings"]["intake_r2"])

        cfg_status, _, cfg_body = _request_handler("/api/config")
        self.assertEqual(cfg_status, 200)
        cfg = json.loads(cfg_body.decode("utf-8"))
        self.assertEqual(cfg["manifest_path"], "/releases/manifest.json")
        self.assertEqual(cfg["approved_overlay_path"], "/overlays/approved.geojson")

        miss_status, miss_headers, _ = _request_handler("/api/nonexistent")
        self.assertEqual(miss_status, 404)
        self.assertEqual(miss_headers.get("cache-control"), "private, no-store")

        idx_status, idx_headers, idx_body = _request_handler("/")
        self.assertEqual(idx_status, 200)
        self.assertIn("text/html", idx_headers.get("content-type", ""))
        self.assertIn(b"basinmap.org", idx_body)

        rng_status, rng_headers, rng_body = _request_handler(
            "/releases/v0.1.0-demo/basins.pmtiles",
            headers={"Range": "bytes=0-126"},
        )
        self.assertEqual(rng_status, 206)
        self.assertEqual(rng_headers.get("content-length"), "127")
        self.assertTrue(rng_headers.get("content-range", "").startswith("bytes 0-126/"))
        parsed = pmtiles_writer.parse_pmtiles_header(rng_body)
        self.assertEqual(parsed["magic"], "PMTiles\x03")
        self.assertEqual(parsed["num_addressed_tiles"], 16)

        # Verify staging /api/r2/* proxy also honors 206 byte-range reads
        st_r2_status, st_r2_headers, st_r2_body = _request_handler(
            "/api/r2/releases/v0.1.0-demo/basins.pmtiles",
            headers={"Range": "bytes=0-126"},
        )
        self.assertEqual(st_r2_status, 206)
        self.assertEqual(st_r2_headers.get("content-length"), "127")
        self.assertTrue(st_r2_headers.get("content-range", "").startswith("bytes 0-126/"))
        st_parsed = pmtiles_writer.parse_pmtiles_header(st_r2_body)
        self.assertEqual(st_parsed["magic"], "PMTiles\x03")
        self.assertEqual(st_parsed["num_addressed_tiles"], 16)

    def test_graceful_degradation_during_api_outage(self) -> None:
        h_status, _, _ = _request_handler("/api/health", simulate_outage=True)
        c_status, _, _ = _request_handler("/api/config", simulate_outage=True)
        self.assertEqual(h_status, 503)
        self.assertEqual(c_status, 503)

        # Static frontend, manifest, PMTiles range reads, basin shard, and approved overlay stay live.
        idx_status, _, _ = _request_handler("/", simulate_outage=True)
        man_status, _, man_body = _request_handler("/releases/manifest.json", simulate_outage=True)
        pm_status, _, pm_body = _request_handler(
            "/releases/v0.1.0-demo/basins.pmtiles",
            simulate_outage=True,
            headers={"Range": "bytes=0-126"},
        )
        shard_status, _, shard_body = _request_handler(
            "/releases/v0.1.0-demo/basins/7060041230.json",
            simulate_outage=True,
        )
        ov_status, _, _ = _request_handler("/overlays/approved.geojson", simulate_outage=True)

        self.assertEqual(idx_status, 200)
        self.assertEqual(man_status, 200)
        self.assertEqual(json.loads(man_body.decode("utf-8"))["current_version"], "v0.1.0-demo")
        self.assertEqual(pm_status, 206)
        self.assertEqual(len(pm_body), 127)
        self.assertEqual(shard_status, 200)
        self.assertGreater(len(json.loads(shard_body.decode("utf-8"))["species"]), 0)
        self.assertEqual(ov_status, 200)

    def test_overlay_deduplication_and_js_helpers_in_jsc(self) -> None:
        if not build_dist.JSC_BIN.exists():
            self.skipTest("JavaScriptCore CLI not available")

        occ_fc = json.loads(
            (ROOT_DIR / "public-r2" / "releases" / "v0.1.0-demo" / "occurrences.geojson").read_text(
                encoding="utf-8"
            )
        )
        snapshot_ids = [f["properties"]["occurrenceID"] for f in occ_fc["features"]]
        duplicate_id = snapshot_ids[0]
        pm_header_bytes = list(
            (ROOT_DIR / "public-r2" / "releases" / "v0.1.0-demo" / "basins.pmtiles").read_bytes()[:127]
        )

        # Write a temporary ES module importing dist/assets/types.js and asserting behavior.
        with tempfile.TemporaryDirectory() as tmp:
            test_mjs = Path(tmp) / "verify_types.mjs"
            types_js_uri = (ROOT_DIR / "dist" / "assets" / "types.js").as_uri()
            test_mjs.write_text(
                f"""
import {{
  resolveDataUrl,
  parsePmtilesHeaderBytes,
  deduplicateApprovedOverlay,
}} from "{types_js_uri}";

function assertEq(actual, expected, msg) {{
  if (actual !== expected) {{
    throw new Error(msg + ": expected " + expected + ", got " + actual);
  }}
}}

assertEq(resolveDataUrl("https://data.basinmap.org/", "/releases/manifest.json"),
  "https://data.basinmap.org/releases/manifest.json", "resolveDataUrl prod");
assertEq(resolveDataUrl("", "overlays/approved.geojson"),
  "/overlays/approved.geojson", "resolveDataUrl dev");

const hdrBytes = new Uint8Array({json.dumps(pm_header_bytes)});
const hdr = parsePmtilesHeaderBytes(hdrBytes.buffer);
assertEq(hdr.version, 3, "pmtiles version");
assertEq(hdr.minZoom, 0, "pmtiles minZoom");
assertEq(hdr.maxZoom, 8, "pmtiles maxZoom");
assertEq(hdr.numAddressedTiles, 16, "pmtiles tile count");

const snapshotSet = new Set({json.dumps(snapshot_ids)});
const overlay = [
  {{ type: "Feature", properties: {{ occurrenceID: {json.dumps(duplicate_id)} }}, geometry: {{ type: "Point", coordinates: [-123.0, 44.0] }} }},
  {{ type: "Feature", properties: {{ occurrenceID: "urn:basinmap:local:2026:9999" }}, geometry: {{ type: "Point", coordinates: [-122.9, 44.1] }} }}
];
const res = deduplicateApprovedOverlay(overlay, snapshotSet);
assertEq(res.duplicateCount, 1, "duplicateCount");
assertEq(res.uniqueFeatures.length, 1, "uniqueFeatures length");
assertEq(res.uniqueFeatures[0].properties.occurrenceID, "urn:basinmap:local:2026:9999", "kept unique id");
print("JSC_VERIFY_OK");
""",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [str(build_dist.JSC_BIN), "-m", str(test_mjs)],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("JSC_VERIFY_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
