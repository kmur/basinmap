#!/usr/bin/env python3
"""Cold/warm HTTP 206 range benchmark and R2 cost projection for basinmap.org (Section 11).

Replays the data requests a MapLibre GL JS + pmtiles.js browser makes during a scripted
map visit (bootstrap, initial fit, zoom in, pan, detail zoom, basin click) against
``https://data.basinmap.org`` (or staging / a local release root) and records, per request:

  * HTTP status, Content-Range validity, bytes transferred
  * time-to-first-byte and total latency
  * ``CF-Cache-Status`` / ``Age`` (Cloudflare CDN cache outcome)

Three passes are measured per run:

  natural  URLs exactly as the app requests them (whatever is in cache right now)
  cold     every URL gets a unique ``?bench=<token>`` query string, so Cloudflare treats it as a
           new cache key and must read R2 (MISS) -- the first-visitor / cache-evicted case
  warm     the cold URLs repeated immediately, so the edge should answer from cache (HIT)

Requests that reach R2 (any ``CF-Cache-Status`` other than HIT/STALE/UPDATING, or a missing
header) are counted as R2 Class B operations and projected with the hosting plan's Section 7.1
formulas across visit volumes and storage sizes. Cold passes deliberately cause R2 reads:
roughly ``requests_per_visit x runs`` Class B operations per invocation, far inside the
10 million/month free allowance.

Standard library only.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import statistics
import sys
import time
import urllib.parse
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pmtiles_writer import PMTilesRangeReader, lat_to_tile_y, lon_to_tile_x  # noqa: E402

DEFAULT_BASE_URL = "https://data.basinmap.org"
USER_AGENT = "basinmap-range-benchmark/0.2 (+https://basinmap.org)"
CACHE_HIT_STATUSES = {"HIT", "STALE", "UPDATING", "REVALIDATED"}
PROFILES = {
    # name: (css width, css height) -- vector tiles are 512 px, independent of DPR
    "desktop": (1440, 900),
    "mobile": (390, 844),
}

# Hosting plan Section 7.1 published rates.
R2_FREE_STORAGE_GB = 10
R2_STORAGE_PER_GB = 0.015
R2_FREE_CLASS_B = 10_000_000
R2_CLASS_B_PER_MILLION = 0.36
R2_FREE_CLASS_A = 1_000_000
R2_CLASS_A_PER_MILLION = 4.50
WORKERS_PAID_BASE = 5.00
WORKERS_INCLUDED_REQUESTS = 10_000_000
WORKERS_REQUEST_PER_MILLION = 0.30


# ---------------------------------------------------------------------------
# Cost model (Section 7.1)
# ---------------------------------------------------------------------------

def r2_monthly_cost(storage_gb: float, class_b_ops: float, class_a_ops: float = 0.0) -> Dict[str, float]:
    storage = max(0, math.ceil(storage_gb) - R2_FREE_STORAGE_GB) * R2_STORAGE_PER_GB
    reads = math.ceil(max(0.0, class_b_ops - R2_FREE_CLASS_B) / 1_000_000) * R2_CLASS_B_PER_MILLION
    writes = math.ceil(max(0.0, class_a_ops - R2_FREE_CLASS_A) / 1_000_000) * R2_CLASS_A_PER_MILLION
    return {"storage": round(storage, 2), "reads": round(reads, 2), "writes": round(writes, 2),
            "total": round(storage + reads + writes, 2)}


def workers_monthly_cost(requests: float) -> float:
    extra = math.ceil(max(0.0, requests - WORKERS_INCLUDED_REQUESTS) / 1_000_000) * WORKERS_REQUEST_PER_MILLION
    return round(WORKERS_PAID_BASE + extra, 2)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

@dataclass
class RequestRecord:
    pass_name: str
    step: str
    path: str
    range: Optional[str]
    status: int
    bytes: int
    ttfb_ms: float
    total_ms: float
    cf_cache_status: Optional[str]
    age: Optional[str]
    content_range: Optional[str]
    ok: bool
    error: Optional[str] = None


class HttpTransport:
    """Keep-alive HTTP(S) client (one connection per host, like a browser's pooled connection)."""

    def __init__(self, base_url: str, timeout: float = 30.0, origin: str = "https://basinmap.org") -> None:
        parsed = urllib.parse.urlsplit(base_url)
        self.scheme = parsed.scheme
        self.host = parsed.netloc
        self.prefix = parsed.path.rstrip("/")
        self.timeout = timeout
        self.origin = origin
        self._conn: Optional[http.client.HTTPConnection] = None

    def _connection(self) -> http.client.HTTPConnection:
        if self._conn is None:
            cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
            self._conn = cls(self.host, timeout=self.timeout)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __call__(self, path: str, headers: Dict[str, str]) -> Tuple[int, Dict[str, str], bytes, float, float]:
        hdrs = {"User-Agent": USER_AGENT, "Origin": self.origin, "Accept-Encoding": "identity", **headers}
        for attempt in (1, 2):
            conn = self._connection()
            t0 = time.perf_counter()
            try:
                conn.request("GET", f"{self.prefix}{path}", headers=hdrs)
                resp = conn.getresponse()
                t1 = time.perf_counter()
                body = resp.read()
                t2 = time.perf_counter()
                return (resp.status, {k.lower(): v for k, v in resp.getheaders()}, body,
                        (t1 - t0) * 1000.0, (t2 - t0) * 1000.0)
            except (http.client.HTTPException, ConnectionError, OSError):
                self.close()
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")


def socketpair_transport(root: Path) -> Callable[[str, Dict[str, str]], Tuple[int, Dict[str, str], bytes, float, float]]:
    from publish_release import _socketpair_http_request

    def call(path: str, headers: Dict[str, str]) -> Tuple[int, Dict[str, str], bytes, float, float]:
        t0 = time.perf_counter()
        status, hdrs, body = _socketpair_http_request(root, path, headers=headers)
        ms = (time.perf_counter() - t0) * 1000.0
        return status, hdrs, body, ms, ms

    return call


# ---------------------------------------------------------------------------
# Visit simulation
# ---------------------------------------------------------------------------

@dataclass
class Viewport:
    name: str
    lon: float
    lat: float
    zoom: float


def fit_zoom(bbox: Sequence[float], width: int, height: int, padding: int = 36) -> float:
    dx = abs(lon_to_tile_x(bbox[2], 0) - lon_to_tile_x(bbox[0], 0))
    dy = abs(lat_to_tile_y(bbox[1], 0) - lat_to_tile_y(bbox[3], 0))
    zx = math.log2(max(1.0, width - 2 * padding) / (512.0 * max(dx, 1e-9)))
    zy = math.log2(max(1.0, height - 2 * padding) / (512.0 * max(dy, 1e-9)))
    return max(0.0, min(zx, zy))


def map_journey(manifest: Dict[str, Any], width: int, height: int) -> List[Viewport]:
    """Scripted Section 11 journey: open map, zoom in, pan, zoom to record detail."""
    bbox = manifest["spatial_coverage"]["bbox"]
    lon, lat = manifest["spatial_coverage"]["center"]
    z0 = fit_zoom(bbox, width, height)
    detail = float(manifest["layers"].get("occurrence_bins_max_zoom", 8)) + 1.0
    # Pan half a viewport east, but stay inside the release extent so detail zooms hit data.
    span_lon = min(width / 512.0 / (2 ** (z0 + 2)) * 360.0 / 2.0, (bbox[2] - bbox[0]) * 0.25)
    return [
        Viewport("initial-fit", lon, lat, z0),
        Viewport("zoom-in", lon, lat, z0 + 2),
        Viewport("pan-east", lon + span_lon, lat, z0 + 2),
        Viewport("detail-zoom", lon + span_lon, lat, max(detail, z0 + 3)),
        Viewport("detail-zoom+1", lon + span_lon, lat, max(detail, z0 + 3) + 1),
    ]


def tiles_for_viewport(vp: Viewport, width: int, height: int, min_zoom: int, max_zoom: int,
                       tile_size: int = 512, bounds: Optional[Sequence[float]] = None) -> List[Tuple[int, int, int]]:
    """Tiles MapLibre requests for a viewport (integer tile zoom, overzoom past max_zoom)."""
    zoom_for_tiles = vp.zoom + math.log2(512.0 / tile_size)
    tz = int(math.floor(zoom_for_tiles))
    if tz < min_zoom:
        return []
    tz = min(tz, max_zoom)
    n = 1 << tz
    scale = 2 ** (zoom_for_tiles - tz)
    cx, cy = lon_to_tile_x(vp.lon, tz), lat_to_tile_y(vp.lat, tz)
    half_w = width / (tile_size * scale) / 2.0
    half_h = height / (tile_size * scale) / 2.0
    x0, x1 = int(math.floor(cx - half_w)), int(math.floor(cx + half_w))
    y0, y1 = max(0, int(math.floor(cy - half_h))), min(n - 1, int(math.floor(cy + half_h)))
    if bounds is not None:
        bx0, bx1 = int(math.floor(lon_to_tile_x(bounds[0], tz))), int(math.floor(lon_to_tile_x(bounds[2], tz)))
        by0, by1 = int(math.floor(lat_to_tile_y(bounds[3], tz))), int(math.floor(lat_to_tile_y(bounds[1], tz)))
        x0, x1, y0, y1 = max(x0, bx0), min(x1, bx1), max(y0, by0), min(y1, by1)
    return [(tz, x % n, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


@dataclass
class Archive:
    path: str
    min_zoom: int
    max_zoom: int
    bounds: Optional[List[float]]
    tile_size: int = 512


def archives_from_manifest(manifest: Dict[str, Any]) -> List[Archive]:
    out: List[Archive] = []
    layers = manifest["layers"]
    for name in ("basins", "occurrences"):
        layer = layers[name]
        for part in layer.get("partitions") or [layer]:
            out.append(Archive(part["path"], part["min_zoom"], part["max_zoom"], part.get("bounds")))
    for raster in layers.get("rasters", []):
        for part in raster.get("partitions") or [raster]:
            out.append(Archive(part["path"], part["min_zoom"], part["max_zoom"], part.get("bounds"),
                               raster.get("tile_size", 256)))
    return out


class VisitRunner:
    def __init__(self, transport: Callable[..., Any], manifest: Dict[str, Any], width: int, height: int,
                 pass_name: str, token: Optional[str]) -> None:
        self.transport = transport
        self.manifest = manifest
        self.width = width
        self.height = height
        self.pass_name = pass_name
        self.token = token
        self.records: List[RequestRecord] = []
        self.step = "bootstrap"
        self.readers: Dict[str, PMTilesRangeReader] = {}
        self.seen_tiles: set = set()

    def _url(self, path: str) -> str:
        if not self.token:
            return path
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}bench={self.token}"

    def get(self, path: str, rng: Optional[Tuple[int, int]] = None) -> bytes:
        headers: Dict[str, str] = {}
        rng_txt = None
        if rng is not None:
            rng_txt = f"bytes={rng[0]}-{rng[0] + rng[1] - 1}"
            headers["Range"] = rng_txt
        try:
            status, hdrs, body, ttfb, total = self.transport(self._url(path), headers)
        except Exception as err:  # noqa: BLE001 - record and continue the visit
            self.records.append(RequestRecord(self.pass_name, self.step, path, rng_txt, 0, 0, 0.0, 0.0, None, None,
                                              None, False, f"{type(err).__name__}: {err}"))
            return b""
        cr = hdrs.get("content-range")
        ok = status == 200 if rng is None else (status == 206 and bool(cr) and len(body) <= rng[1])
        self.records.append(RequestRecord(self.pass_name, self.step, path, rng_txt, status, len(body),
                                          round(ttfb, 2), round(total, 2), hdrs.get("cf-cache-status"),
                                          hdrs.get("age"), cr, ok))
        return body

    def _reader(self, path: str) -> PMTilesRangeReader:
        if path not in self.readers:
            reader = PMTilesRangeReader(lambda off, ln, p=path: self.get(p, (off, ln)))
            reader.open()
            self.readers[path] = reader
        return self.readers[path]

    def run(self) -> List[RequestRecord]:
        m = self.manifest
        layers = m["layers"]
        # 1. Bootstrap exactly like src/frontend/app.ts.
        self.get("/releases/manifest.json")
        for name in ("basins", "occurrences"):
            self.get(layers[name]["path"], (0, 127))  # verifyRangeProbe
        for name in ("basins", "occurrences"):
            if layers[name].get("geojson_path"):
                self.get(layers[name]["geojson_path"])
        if layers.get("snapshot_own_occurrence_ids"):
            self.get(layers["snapshot_own_occurrence_ids"])
        self.get(m["overlays"]["approved_observations"]["path"])
        # 2. Map journey.
        archives = archives_from_manifest(m)
        for vp in map_journey(m, self.width, self.height):
            self.step = vp.name
            for arc in archives:
                for zxy in tiles_for_viewport(vp, self.width, self.height, arc.min_zoom, arc.max_zoom,
                                              arc.tile_size, arc.bounds):
                    key = (arc.path, zxy)
                    if key in self.seen_tiles:
                        continue  # browser/MapLibre tile cache
                    self.seen_tiles.add(key)
                    self._reader(arc.path).get_tile(*zxy)
        # 3. Inspect a basin (shard click).
        self.step = "basin-click"
        shards = layers.get("basin_shards") or []
        if shards and str(shards[0]["path"]).endswith(".json"):
            self.get(shards[0]["path"])
        return self.records


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

def _pct(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, int(math.ceil(q * len(vals))) - 1))
    return round(vals[idx], 1)


def reaches_r2(rec: RequestRecord) -> bool:
    status = (rec.cf_cache_status or "").upper()
    return status not in CACHE_HIT_STATUSES


def summarize(records: List[RequestRecord]) -> Dict[str, Any]:
    totals = [r.total_ms for r in records if r.status]
    ttfb = [r.ttfb_ms for r in records if r.status]
    cache: Dict[str, int] = {}
    for r in records:
        key = r.cf_cache_status or "(none)"
        cache[key] = cache.get(key, 0) + 1
    range_recs = [r for r in records if r.range]
    return {
        "requests": len(records),
        "range_requests": len(range_recs),
        "range_206_ok": sum(1 for r in range_recs if r.ok),
        "errors": sum(1 for r in records if not r.ok),
        "bytes": sum(r.bytes for r in records),
        "r2_reads": sum(1 for r in records if reaches_r2(r)),
        "cf_cache_status": dict(sorted(cache.items())),
        "latency_ms": {"p50": _pct(totals, 0.5), "p75": _pct(totals, 0.75), "p95": _pct(totals, 0.95),
                       "max": round(max(totals), 1) if totals else None,
                       "mean": round(statistics.mean(totals), 1) if totals else None},
        "ttfb_ms": {"p50": _pct(ttfb, 0.5), "p95": _pct(ttfb, 0.95)},
        "sequential_time_ms": round(sum(totals), 1),
        "by_step": {s: sum(1 for r in records if r.step == s) for s in dict.fromkeys(r.step for r in records)},
    }


def project_costs(per_visit_r2_reads: Dict[str, float], visits: Sequence[int], storage_gb: Sequence[float],
                  via_worker: bool, requests_per_visit: float) -> List[Dict[str, Any]]:
    rows = []
    for scenario, reads in per_visit_r2_reads.items():
        for v in visits:
            b = reads * v
            for s in storage_gb:
                cost = r2_monthly_cost(s, b)
                row = {"scenario": scenario, "r2_reads_per_visit": round(reads, 1), "visits_per_month": v,
                       "class_b_ops": int(round(b)), "storage_gb": s, "r2": cost}
                if via_worker:
                    row["worker_requests"] = int(round(requests_per_visit * v))
                    row["workers_paid_total"] = workers_monthly_cost(requests_per_visit * v)
                    row["total_with_workers"] = round(cost["total"] + row["workers_paid_total"], 2)
                rows.append(row)
    return rows


def markdown_report(result: Dict[str, Any]) -> str:
    lines = [f"# Range pilot benchmark — {result['base_url']}", "",
             f"Release `{result['release']}` · GBIF DOI `{result['gbif_doi']}` · {result['started_at']}", ""]
    lines += ["| Profile | Pass | Requests | 206 OK | R2 reads | Bytes | p50 ms | p95 ms | CF-Cache-Status |",
              "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for prof, passes in result["profiles"].items():
        for pname, s in passes.items():
            cache = ", ".join(f"{k}:{v}" for k, v in s["cf_cache_status"].items())
            lines.append(f"| {prof} | {pname} | {s['requests']} | {s['range_206_ok']}/{s['range_requests']} | "
                         f"{s['r2_reads']} | {s['bytes']:,} | {s['latency_ms']['p50']} | {s['latency_ms']['p95']} | {cache} |")
    lines += ["", "## Projected monthly R2 cost (Section 7.1 formulas)", "",
              "| Scenario | R2 reads/visit | Visits/month | Class B ops | Storage GB | R2 total |"
              + (" Workers ($5 plan) | Total |" if result["via_worker"] else ""),
              "|---|---:|---:|---:|---:|---:|" + ("---:|---:|" if result["via_worker"] else "")]
    for r in result["cost_projection"]:
        line = (f"| {r['scenario']} | {r['r2_reads_per_visit']} | {r['visits_per_month']:,} | {r['class_b_ops']:,} | "
                f"{r['storage_gb']} | ${r['r2']['total']:.2f} |")
        if result["via_worker"]:
            line += f" ${r['workers_paid_total']:.2f} | ${r['total_with_workers']:.2f} |"
        lines.append(line)
    if result.get("probes"):
        lines += ["", "## Object-size probes (above/below the 512 MB CDN ceiling)", "",
                  "| Object | Pass | Status | CF-Cache-Status | ms |", "|---|---|---:|---|---:|"]
        for p in result["probes"]:
            lines.append(f"| {p['path']} | {p['pass_name']} | {p['status']} | {p['cf_cache_status']} | {p['total_ms']} |")
    lines += ["", "Notes: R2 reads = requests whose CF-Cache-Status is not HIT/STALE/UPDATING/REVALIDATED "
              "(or absent). Cold passes add a unique query string to force a cache MISS."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_benchmark(
    transport: Callable[..., Any],
    base_url: str,
    profiles: Sequence[str] = ("desktop", "mobile"),
    runs: int = 3,
    visits: Sequence[int] = (20_000,),
    storage_gb: Sequence[float] = (50.0, 100.0),
    probe_paths: Sequence[str] = (),
    passes: Sequence[str] = ("natural", "cold", "warm"),
) -> Dict[str, Any]:
    status, _, body, _, _ = transport("/releases/manifest.json", {})
    if status != 200:
        raise RuntimeError(f"GET /releases/manifest.json returned {status}")
    manifest = json.loads(body.decode("utf-8"))
    result: Dict[str, Any] = {
        "base_url": base_url,
        "release": manifest["current_version"],
        "gbif_doi": manifest["provenance"]["gbif_doi"],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "via_worker": "/api/r2" in base_url,
        "runs": runs,
        "profiles": {},
        "requests": [],
    }
    per_visit: Dict[str, List[float]] = {}
    req_counts: List[int] = []
    for prof in profiles:
        width, height = PROFILES[prof]
        result["profiles"][prof] = {}
        prof_records: Dict[str, List[RequestRecord]] = {p: [] for p in passes}
        for run in range(runs):
            token = uuid.uuid4().hex[:12]
            for pname in passes:
                tok = None if pname == "natural" else token
                recs = VisitRunner(transport, manifest, width, height, pname, tok).run()
                prof_records[pname].extend(recs)
                per_visit.setdefault(pname, []).append(sum(1 for r in recs if reaches_r2(r)))
                req_counts.append(len(recs))
                for r in recs:
                    result["requests"].append(dict(asdict(r), profile=prof, run=run))
        for pname, recs in prof_records.items():
            s = summarize(recs)
            # Report per-visit averages for counts.
            for k in ("requests", "range_requests", "range_206_ok", "errors", "bytes", "r2_reads"):
                s[k] = round(s[k] / runs, 1) if runs > 1 else s[k]
            s["sequential_time_ms"] = round(s["sequential_time_ms"] / runs, 1)
            result["profiles"][prof][pname] = s

    scenarios = {f"{p} (measured)": statistics.mean(v) for p, v in per_visit.items()}
    scenarios["all requests reach R2 (worst case)"] = statistics.mean(req_counts)
    result["cost_projection"] = project_costs(scenarios, visits, storage_gb, result["via_worker"],
                                              statistics.mean(req_counts))
    probes = []
    for path in probe_paths:
        token = uuid.uuid4().hex[:12]
        for pname, tok in (("cold", token), ("warm", token), ("warm", token)):
            url = f"{path}{'&' if '?' in path else '?'}bench={tok}"
            st, hdrs, b, ttfb, total = transport(url, {"Range": "bytes=0-16383"})
            probes.append({"path": path, "pass_name": pname, "status": st, "bytes": len(b),
                           "cf_cache_status": hdrs.get("cf-cache-status"), "total_ms": round(total, 1),
                           "ttfb_ms": round(ttfb, 1)})
    result["probes"] = probes
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cold/warm PMTiles range benchmark + R2 cost projection")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL,
                   help="data origin, e.g. https://data.basinmap.org or https://staging.basinmap.org/api/r2")
    p.add_argument("--root", type=Path, default=None, help="Benchmark a local release root instead (offline)")
    p.add_argument("--profile", choices=("desktop", "mobile", "both"), default="both")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--passes", nargs="+", default=["natural", "cold", "warm"], choices=["natural", "cold", "warm"])
    p.add_argument("--visits", type=int, nargs="+", default=[20_000, 100_000])
    p.add_argument("--storage-gb", type=float, nargs="+", default=[50.0, 100.0])
    p.add_argument("--probe", action="append", default=[],
                   help="Extra object path for above/below-512 MB cache probes (repeatable)")
    p.add_argument("--json-out", type=Path, default=None)
    p.add_argument("--markdown-out", type=Path, default=None)
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    profiles = ("desktop", "mobile") if args.profile == "both" else (args.profile,)
    server = None
    if args.root is not None:
        from publish_release import start_range_test_server

        try:
            server, base, _ = start_range_test_server(args.root)
            transport: Callable[..., Any] = HttpTransport(base)
            transport("/releases/manifest.json", {})
        except OSError:
            if server is not None:
                server.shutdown()
                server = None
            base = f"file://{args.root}"
            transport = socketpair_transport(args.root)
    else:
        base = args.base_url
        transport = HttpTransport(base)
    try:
        result = run_benchmark(transport, base, profiles, args.runs, args.visits, args.storage_gb, args.probe,
                               args.passes)
    finally:
        if isinstance(transport, HttpTransport):
            transport.close()
        if server is not None:
            server.shutdown()
            server.server_close()
    report = markdown_report(result)
    print(report)
    if args.json_out:
        args.json_out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.write_text(report, encoding="utf-8")
    bad = sum(s["errors"] for passes in result["profiles"].values() for s in passes.values())
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
