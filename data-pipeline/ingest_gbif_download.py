#!/usr/bin/env python3
"""Real GBIF Occurrence Download + HydroBASINS ingestion for basinmap.org releases.

Implements hosting plan Sections 4.1-4.3 for the Stage 4 pilot release:

  count       Pre-flight record count for a query file (GBIF occurrence search, no auth)
  request     Submit an asynchronous GBIF Occurrence Download (needs GBIF_USER / GBIF_PWD)
  fetch       Poll a download key until SUCCEEDED, then save the zip, the verbatim download
              metadata (live DOI, key, created date, license, totalRecords), the query JSON,
              provenance.json, and SHA256SUMS
  fetch-hydrobasins
              Download a HydroBASINS v1c region/level zip from data.hydrosheds.org (+ SHA-256)
  build       Join the download against HydroBASINS polygons and write an immutable release:
              scalar-only basins PMTiles, aggregated/record-level occurrence PMTiles, per-basin
              JSON shards (or regional SQLite for Levels 7-12), every archive < 500 MB
  run         count -> request -> fetch -> build in one go

Credentials are read only from the environment (GBIF_USER, GBIF_PWD, GBIF_EMAIL) and are
never written to disk, logged, or embedded in the published manifest. Python 3.9+ standard
library only.
"""

from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_release import IMMUTABLE_CACHE_CONTROL, MUTABLE_CACHE_CONTROL, sha256_file  # noqa: E402
from hydrobasins import (  # noqa: E402
    HYDROBASINS_CITATION,
    HYDROBASINS_DOWNLOAD_TEMPLATE,
    HYDROBASINS_REGIONS,
    BasinIndex,
    filter_basins,
    geometry_bbox,
    load_hydrobasins,
    round_geometry,
    simplify_geometry,
)
from pmtiles_writer import TILE_TYPE_WEBP  # noqa: E402
from taxonomy import build_species_outputs  # noqa: E402
from release_tiling import (  # noqa: E402
    BASIN_SCALAR_FIELDS,
    CDN_CACHE_CEILING_BYTES,
    MAX_ARCHIVE_BYTES,
    OCCURRENCE_POINT_FIELDS,
    build_basin_tiles,
    build_occurrence_tiles,
    write_partitioned_pmtiles,
)

GBIF_API = "https://api.gbif.org/v1"
USER_AGENT = "basinmap.org-data-pipeline/0.2 (+https://basinmap.org)"
TERMINAL_FAILURES = {"FAILED", "KILLED", "CANCELLED", "FILE_ERASED", "SUSPENDED"}
THREATENED_IUCN = ("CR", "EN", "VU")
DEFAULT_MAX_RECORDS = 2_000_000
MAX_JSON_SHARDS = 20_000
LICENSE_URLS = {
    "CC0_1_0": "http://creativecommons.org/publicdomain/zero/1.0/legalcode",
    "CC_BY_4_0": "http://creativecommons.org/licenses/by/4.0/legalcode",
    "CC_BY_NC_4_0": "http://creativecommons.org/licenses/by-nc/4.0/legalcode",
}
SPECIES_RANKS = {"SPECIES", "SUBSPECIES", "VARIETY", "FORM", "INFRASPECIFIC_NAME"}
PRIVATE_REQUEST_FIELDS = ("creator", "notificationAddresses", "notification_address", "sendNotification")


def log(msg: str) -> None:
    print(f"[ingest] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# GBIF API client
# ---------------------------------------------------------------------------

class GbifClient:
    """Thin GBIF REST client. ``urlopen`` is injectable for offline tests."""

    def __init__(
        self,
        api_base: str = GBIF_API,
        urlopen: Optional[Callable[..., Any]] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 120.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self._urlopen = urlopen or urllib.request.urlopen
        self._user = user
        self._password = password
        self.timeout = timeout

    def _open(self, url: str, method: str = "GET", body: Optional[bytes] = None, auth: bool = False,
              content_type: Optional[str] = None) -> Any:
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("User-Agent", USER_AGENT)
        if content_type:
            req.add_header("Content-Type", content_type)
        if auth:
            if not self._user or not self._password:
                raise PermissionError("GBIF_USER and GBIF_PWD must be set in the environment for this step")
            token = base64.b64encode(f"{self._user}:{self._password}".encode("utf-8")).decode("ascii")
            req.add_header("Authorization", f"Basic {token}")
        return self._urlopen(req, timeout=self.timeout)

    def get_json(self, path: str, retries: int = 3) -> Any:
        """GET JSON; empty bodies (e.g. HTTP 204 No Content) return None; transient errors are retried."""
        for attempt in range(retries + 1):
            try:
                with self._open(f"{self.api_base}{path}") as resp:
                    raw = resp.read().decode("utf-8").strip()
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as err:
                if err.code not in (429, 500, 502, 503, 504) or attempt == retries:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == retries:
                    raise
            time.sleep(min(30.0, 2.0 ** attempt))
        return None

    def count_predicate(self, predicate: Dict[str, Any]) -> int:
        """Exact record count for a download predicate (POST /occurrence/search/predicate, no auth)."""
        body = json.dumps({"limit": 0, "predicate": predicate}).encode("utf-8")
        with self._open(f"{self.api_base}/occurrence/search/predicate", "POST", body,
                        content_type="application/json") as resp:
            return int(json.loads(resp.read().decode("utf-8"))["count"])

    def count(self, search_params: Dict[str, Any]) -> int:
        params: List[Tuple[str, str]] = [("limit", "0")]
        for key, val in search_params.items():
            for item in val if isinstance(val, list) else [val]:
                params.append((key, str(item).lower() if isinstance(item, bool) else str(item)))
        return int(self.get_json(f"/occurrence/search?{urllib.parse.urlencode(params)}")["count"])

    def submit_download(self, request_body: Dict[str, Any]) -> str:
        body = json.dumps(request_body).encode("utf-8")
        with self._open(f"{self.api_base}/occurrence/download/request", "POST", body, auth=True,
                        content_type="application/json") as resp:
            key = resp.read().decode("utf-8").strip()
        if not re.fullmatch(r"\d{7}-\d{15}", key):
            raise RuntimeError(f"Unexpected GBIF download key response: {key[:120]!r}")
        return key

    def download_metadata(self, key: str) -> Dict[str, Any]:
        return self.get_json(f"/occurrence/download/{urllib.parse.quote(key)}")

    def stream_to_file(self, url: str, dest: Path, chunk: int = 1 << 20) -> Tuple[str, int]:
        h = hashlib.sha256()
        total = 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self._open(url) as resp, tmp.open("wb") as out:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                h.update(buf)
                out.write(buf)
                total += len(buf)
        tmp.replace(dest)
        return h.hexdigest(), total

    def species_iucn(self, species_key: int) -> str:
        try:
            data = self.get_json(f"/species/{int(species_key)}/iucnRedListCategory")
        except urllib.error.HTTPError as err:
            if err.code in (204, 404):
                return ""
            raise
        return str((data or {}).get("code") or "")

    def dataset_title(self, dataset_key: str) -> str:
        try:
            return str(self.get_json(f"/dataset/{urllib.parse.quote(dataset_key)}").get("title") or "")
        except urllib.error.HTTPError:
            return ""


def client_from_env(api_base: str = GBIF_API) -> GbifClient:
    return GbifClient(api_base=api_base, user=os.environ.get("GBIF_USER"), password=os.environ.get("GBIF_PWD"))


# ---------------------------------------------------------------------------
# Query handling, request, fetch
# ---------------------------------------------------------------------------

def load_query(path: Path) -> Dict[str, Any]:
    query = json.loads(path.read_text(encoding="utf-8"))
    if "request" not in query or "predicate" not in query["request"]:
        raise ValueError(f"{path} must contain request.predicate (GBIF download predicate JSON)")
    return query


def public_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Strip creator/notification fields before publishing a query in a public manifest."""
    return {k: v for k, v in request.items() if k not in PRIVATE_REQUEST_FIELDS}


def predicate_bbox(predicate: Dict[str, Any]) -> Optional[List[float]]:
    """Bounding box of the first WITHIN polygon in a predicate tree (WKT POLYGON/MULTIPOLYGON)."""
    stack = [predicate]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if str(node.get("type", "")).lower() == "within" and node.get("geometry"):
            nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", node["geometry"])]
            xs, ys = nums[0::2], nums[1::2]
            return [min(xs), min(ys), max(xs), max(ys)]
        stack.extend(node.get("predicates", []))
        if isinstance(node.get("predicate"), dict):
            stack.append(node["predicate"])
    return None


def request_download(client: GbifClient, query: Dict[str, Any], max_records: int, force: bool = False,
                     dry_run: bool = False) -> Dict[str, Any]:
    body = dict(query["request"])
    body.setdefault("format", "SIMPLE_CSV")
    body["creator"] = client._user or "<GBIF_USER>"
    email = os.environ.get("GBIF_EMAIL")
    if email:
        body["notificationAddresses"] = [email]
        body["sendNotification"] = True
    expected = None
    if not dry_run:
        expected = client.count_predicate(body["predicate"])
    if expected is not None:
        if expected > max_records and not force:
            raise RuntimeError(
                f"Query matches {expected:,} records (> --max-records {max_records:,}). Narrow the scope "
                "or pass --force and use the tippecanoe/DuckDB path described in data-pipeline/README.md"
            )
    if dry_run:
        redacted = dict(body, creator="<GBIF_USER>")
        redacted.pop("notificationAddresses", None)
        return {"dry_run": True, "request": redacted}
    key = client.submit_download(body)
    return {"key": key, "expected_records": expected}


def gbif_citation(meta: Dict[str, Any]) -> str:
    created = str(meta.get("created", ""))[:10]
    try:
        date_txt = datetime.strptime(created, "%Y-%m-%d").strftime("%d %B %Y").lstrip("0")
    except ValueError:
        date_txt = created
    return f"GBIF.org ({date_txt}) GBIF Occurrence Download https://doi.org/{meta['doi']}"


def _write_sha256sums(directory: Path, names: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    lines = []
    for name in names:
        p = directory / name
        if p.exists():
            digest = sha256_file(p)
            out[name] = {"sha256": digest, "bytes": p.stat().st_size}
            lines.append(f"{digest}  {name}")
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def fetch_download(client: GbifClient, key: str, workdir: Path, poll_seconds: float = 60.0,
                   timeout_seconds: float = 6 * 3600, sleep: Callable[[float], None] = time.sleep) -> Path:
    """Wait for a GBIF download, then save zip + metadata + query + provenance + SHA256SUMS."""
    out_dir = workdir / f"gbif-{key}"
    out_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    while True:
        meta = client.download_metadata(key)
        status = meta.get("status")
        log(f"download {key}: status={status} records={meta.get('totalRecords')}")
        if status == "SUCCEEDED":
            break
        if status in TERMINAL_FAILURES:
            raise RuntimeError(f"GBIF download {key} ended with status {status}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"GBIF download {key} not ready after {timeout_seconds}s")
        sleep(poll_seconds)

    if not meta.get("doi"):
        raise RuntimeError(f"GBIF download {key} has no DOI in its metadata")
    (out_dir / "download_metadata.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", "utf-8")
    (out_dir / "query.json").write_text(
        json.dumps(public_request(meta.get("request", {})), indent=2, sort_keys=True) + "\n", "utf-8"
    )

    zip_path = out_dir / f"{key}.zip"
    link = meta.get("downloadLink") or f"{client.api_base}/occurrence/download/request/{key}.zip"
    if zip_path.exists() and meta.get("size") and zip_path.stat().st_size == int(meta["size"]):
        log(f"reusing existing {zip_path.name}")
        digest = sha256_file(zip_path)
        size = zip_path.stat().st_size
    else:
        log(f"downloading {link}")
        digest, size = client.stream_to_file(link, zip_path)
    if meta.get("size") and int(meta["size"]) != size:
        raise RuntimeError(f"Zip size {size} does not match GBIF metadata size {meta['size']}")
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            raise RuntimeError(f"Corrupt member {bad} in {zip_path}")

    checksums = _write_sha256sums(out_dir, [zip_path.name, "download_metadata.json", "query.json"])
    provenance = {
        "gbif_doi": meta["doi"],
        "gbif_download_key": key,
        "gbif_download_created": meta.get("created"),
        "gbif_download_modified": meta.get("modified"),
        "gbif_download_url": link,
        "gbif_download_format": meta.get("request", {}).get("format"),
        "gbif_total_records": meta.get("totalRecords"),
        "gbif_number_datasets": meta.get("numberDatasets"),
        "gbif_license": meta.get("license"),
        "gbif_query": public_request(meta.get("request", {})),
        "citation": gbif_citation(meta),
        "zip_sha256": digest,
        "zip_bytes": size,
        "files": checksums,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (out_dir / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", "utf-8")
    return out_dir


def fetch_hydrobasins(client: GbifClient, region: str, level: int, workdir: Path,
                      url_template: str = HYDROBASINS_DOWNLOAD_TEMPLATE) -> Path:
    if region not in HYDROBASINS_REGIONS:
        raise ValueError(f"Unknown HydroBASINS region {region!r}; expected one of {HYDROBASINS_REGIONS}")
    if not 1 <= level <= 12:
        raise ValueError("HydroBASINS level must be 1-12")
    out_dir = workdir / "hydrobasins"
    out_dir.mkdir(parents=True, exist_ok=True)
    url = url_template.format(region=region, level=level)
    dest = out_dir / url.rsplit("/", 1)[-1]
    digest, size = client.stream_to_file(url, dest)
    (out_dir / f"{dest.name}.sha256").write_text(f"{digest}  {dest.name}\n", "utf-8")
    (out_dir / f"{dest.name}.source.json").write_text(
        json.dumps({"url": url, "sha256": digest, "bytes": size, "region": region, "level": level,
                    "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, indent=2) + "\n",
        "utf-8",
    )
    return dest


# ---------------------------------------------------------------------------
# Reading the download zip
# ---------------------------------------------------------------------------

def iter_download_rows(zip_path: Path) -> Iterator[Dict[str, str]]:
    """Stream rows from a SIMPLE_CSV (``<key>.csv``) or DWCA (``occurrence.txt``) download zip."""
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        member = next((n for n in names if n.lower() == "occurrence.txt"), None)
        if member is None:
            member = next((n for n in names if n.lower().endswith(".csv")), None)
        if member is None:
            raise FileNotFoundError(f"No occurrence.txt or *.csv found in {zip_path}")
        with zf.open(member) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            reader = csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE)
            header = next(reader)
            for row in reader:
                if len(row) < len(header):
                    row = row + [""] * (len(header) - len(row))
                yield dict(zip(header, row))


def _to_int(val: Optional[str]) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except ValueError:
        return None


def normalize_license(raw: str) -> str:
    raw = (raw or "").strip()
    return LICENSE_URLS.get(raw, raw)


def normalize_row(row: Dict[str, str]) -> Optional[Dict[str, Any]]:
    try:
        lat = float(row.get("decimalLatitude") or "nan")
        lon = float(row.get("decimalLongitude") or "nan")
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    rank = (row.get("taxonRank") or "").upper()
    species_name = row.get("species") or (row.get("scientificName") if rank in SPECIES_RANKS else "")
    species_key = _to_int(row.get("speciesKey")) or (_to_int(row.get("taxonKey")) if species_name else None)
    return {
        "gbifID": _to_int(row.get("gbifID")) or 0,
        "occurrenceID": row.get("occurrenceID", ""),
        "scientificName": row.get("scientificName", ""),
        "taxonKey": _to_int(row.get("taxonKey")) or 0,
        "speciesKey": species_key,
        "species": species_name or "",
        "family": row.get("family", ""),
        "lat": lat,
        "lon": lon,
        "eventDate": row.get("eventDate", ""),
        "year": _to_int(row.get("year")),
        "basisOfRecord": row.get("basisOfRecord", ""),
        "institutionCode": row.get("institutionCode", ""),
        "datasetKey": row.get("datasetKey", ""),
        "datasetName": row.get("datasetName", ""),
        "license": normalize_license(row.get("license", "")),
        "iucnRedListCategory": row.get("iucnRedListCategory", ""),
    }


# ---------------------------------------------------------------------------
# Enrichment caches (IUCN categories, dataset titles)
# ---------------------------------------------------------------------------

def _load_cache(path: Path) -> Dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def enrich(client: Optional[GbifClient], species_keys: Sequence[int], dataset_keys: Sequence[str],
           cache_dir: Path, workers: int = 8) -> Tuple[Dict[str, str], Dict[str, str]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    iucn_path = cache_dir / "iucn_by_species_key.json"
    ds_path = cache_dir / "dataset_titles.json"
    iucn = _load_cache(iucn_path)
    titles = _load_cache(ds_path)
    if client is not None:
        todo = [k for k in species_keys if str(k) not in iucn]
        if todo:
            log(f"IUCN lookups: {len(todo)} species to fetch ({len(iucn)} cached), {workers} in parallel")
        done = 0
        # Bounded concurrency keeps this polite to api.gbif.org while cutting wall time ~8x.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(client.species_iucn, key): key for key in todo}
            for fut in as_completed(futures):
                iucn[str(futures[fut])] = fut.result()
                done += 1
                if done % 250 == 0 or done == len(todo):
                    log(f"IUCN lookups {done}/{len(todo)}")
                    iucn_path.write_text(json.dumps(iucn, sort_keys=True), "utf-8")
        for key in dataset_keys:
            if key and key not in titles:
                titles[key] = client.dataset_title(key)
        iucn_path.write_text(json.dumps(iucn, sort_keys=True, indent=0) + "\n", "utf-8")
        ds_path.write_text(json.dumps(titles, sort_keys=True, indent=0) + "\n", "utf-8")
    return iucn, titles


# ---------------------------------------------------------------------------
# Shards
# ---------------------------------------------------------------------------

def _write_sqlite_shards(staging_dir: Path, shards: Dict[str, Dict[str, Any]], prefix_len: int) -> Dict[str, str]:
    """Write per-Pfafstetter-prefix SQLite files for fine HydroBASINS levels (7-12)."""
    by_prefix: Dict[str, List[Tuple[str, Dict[str, Any]]]] = defaultdict(list)
    for hybas_id, payload in shards.items():
        by_prefix[payload["basin"]["pfaf_id"][:prefix_len]].append((hybas_id, payload))
    paths: Dict[str, str] = {}
    for prefix, items in sorted(by_prefix.items()):
        rel = f"basins/pfaf-{prefix}.sqlite"
        db_path = staging_dir / rel
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA page_size=4096")
        con.execute("CREATE TABLE basin_shards (hybas_id TEXT PRIMARY KEY, pfaf_id TEXT, payload_json TEXT NOT NULL)")
        con.executemany(
            "INSERT INTO basin_shards VALUES (?,?,?)",
            [(h, p["basin"]["pfaf_id"], json.dumps(p, separators=(",", ":"))) for h, p in sorted(items)],
        )
        con.commit()
        con.execute("VACUUM")
        con.close()
        for hybas_id, _ in items:
            paths[hybas_id] = rel
    return paths


# ---------------------------------------------------------------------------
# Release build
# ---------------------------------------------------------------------------

def _infer_level(path: Path, explicit: Optional[int]) -> int:
    if explicit:
        return explicit
    m = re.search(r"lev(\d{2})", path.name)
    return int(m.group(1)) if m else 6


def _tile_layer_metadata(name: str, description: str, attribution: str, layer_id: str,
                         fields: Dict[str, str]) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "version": "2",
        "attribution": attribution,
        "format": "pbf",
        "vector_layers": [{"id": layer_id, "description": description, "fields": fields}],
    }


def build_release_from_download(
    gbif_dir: Path,
    hydrobasins_path: Union[Path, Sequence[Path]],
    version: str,
    output_root: Path,
    *,
    previous_version: Optional[str] = None,
    basin_level: Optional[int] = None,
    bbox: Optional[Sequence[float]] = None,
    pfaf_prefixes: Optional[Sequence[str]] = None,
    client: Optional[GbifClient] = None,
    enrichment_dir: Optional[Path] = None,
    own_dataset_key: Optional[str] = None,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
    basin_zooms: Tuple[int, int] = (0, 10),
    occurrence_zooms: Tuple[int, int, int] = (0, 9, 14),
    max_points_per_tile: int = 4000,
    shard_format: str = "auto",
    raster_tiles_dir: Optional[Path] = None,
    raster_name: str = "hillshade",
    raster_attribution: str = "",
    force: bool = False,
    keep_unassigned: bool = False,
    checklist_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build an immutable release directory from a fetched GBIF download.

    ``hydrobasins_path`` may list several regional files of one level (e.g. all nine regions for a
    global taxon). With ``keep_unassigned`` records outside every basin polygon (coasts, estuaries,
    small islands) stay in the occurrence tiles with an empty ``hybas_id`` instead of being dropped.
    ``checklist_path`` (from ``taxonomy.py fetch``) adds ``species_index.json`` and
    ``taxonomy_aliases.json`` to the release.
    """
    hydrobasins_paths = [Path(hydrobasins_path)] if isinstance(hydrobasins_path, (str, Path)) else [Path(x) for x in hydrobasins_path]
    if not hydrobasins_paths:
        raise ValueError("At least one HydroBASINS file is required")
    if not re.fullmatch(r"v[0-9A-Za-z.\-]+", version):
        raise ValueError(f"Invalid release version name: {version!r}")
    meta_path = gbif_dir / "download_metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("status") != "SUCCEEDED" or not meta.get("doi"):
        raise ValueError(f"{meta_path} is not a SUCCEEDED GBIF download with a DOI")
    key = meta["key"]
    zip_path = gbif_dir / f"{key}.zip"
    query_path = gbif_dir / "query.json"
    sums_path = gbif_dir / "SHA256SUMS"
    if sums_path.exists():
        for line in sums_path.read_text(encoding="utf-8").splitlines():
            digest, name = line.split(None, 1)
            if (gbif_dir / name).exists() and sha256_file(gbif_dir / name) != digest:
                raise ValueError(f"SHA-256 mismatch for {gbif_dir / name} (source changed since fetch)")

    level = _infer_level(hydrobasins_paths[0], basin_level)
    for extra in hydrobasins_paths[1:]:
        if _infer_level(extra, basin_level) != level:
            raise ValueError(f"{extra.name} is not HydroBASINS level {level}; all regions must share one level")
    query_request = public_request(meta.get("request", {}))
    scope_bbox = list(bbox) if bbox else predicate_bbox(query_request.get("predicate", {}))
    all_basins = [f for hb_path in hydrobasins_paths for f in load_hydrobasins(hb_path)]
    basins = filter_basins(all_basins, bbox=scope_bbox, pfaf_prefixes=pfaf_prefixes)
    if not basins:
        raise ValueError("No HydroBASINS polygons intersect the requested scope")
    log(f"{len(basins)} of {len(all_basins)} level-{level} basins in scope {scope_bbox}")
    index = BasinIndex(basins)

    releases_dir = output_root / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)
    target_dir = releases_dir / version
    if target_dir.exists() and not force:
        raise FileExistsError(f"Release directory {target_dir} already exists (releases are immutable)")
    staging_dir = releases_dir / f".staging-{version}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    (staging_dir / "basins").mkdir(parents=True)

    # --- Spatial join & aggregation --------------------------------------------------------
    agg: Dict[int, Dict[str, Any]] = {}
    points: List[Tuple[float, float, Dict[str, Any]]] = []
    unassigned = 0
    skipped = 0
    total_rows = 0
    species_keys: set = set()
    dataset_keys: set = set()
    licenses: Dict[str, int] = defaultdict(int)
    own_ids: List[str] = []
    has_iucn_column = False
    species_totals: Dict[int, Dict[str, Any]] = {}

    for raw in iter_download_rows(zip_path):
        total_rows += 1
        rec = normalize_row(raw)
        if rec is None:
            skipped += 1
            continue
        if rec["iucnRedListCategory"]:
            has_iucn_column = True
        bidx = index.lookup(rec["lon"], rec["lat"])
        sk = rec["speciesKey"]
        if bidx is None:
            unassigned += 1
            if not keep_unassigned:
                continue
        hybas_id = basins[bidx]["properties"]["hybas_id"] if bidx is not None else ""
        yr = rec["year"]
        if sk and rec["species"]:
            tot = species_totals.get(sk)
            if tot is None:
                tot = species_totals[sk] = {"count": 0, "name": rec["species"], "family": rec["family"],
                                            "firstYear": None, "lastYear": None, "basins": set(),
                                            "iucn": rec["iucnRedListCategory"]}
            tot["count"] += 1
            if hybas_id:
                tot["basins"].add(hybas_id)
            if yr:
                tot["firstYear"] = yr if not tot["firstYear"] else min(tot["firstYear"], yr)
                tot["lastYear"] = yr if not tot["lastYear"] else max(tot["lastYear"], yr)
        licenses[rec["license"]] += 1
        dataset_keys.add(rec["datasetKey"])
        if sk and rec["species"]:
            species_keys.add(sk)
        if own_dataset_key and rec["datasetKey"] == own_dataset_key and rec["occurrenceID"]:
            own_ids.append(rec["occurrenceID"])
        points.append((rec["lon"], rec["lat"], {
            "occurrenceID": rec["occurrenceID"], "gbifID": rec["gbifID"], "scientificName": rec["scientificName"],
            "taxonKey": rec["taxonKey"], "eventDate": rec["eventDate"], "year": yr or 0,
            "basisOfRecord": rec["basisOfRecord"], "hybas_id": hybas_id, "license": rec["license"],
            "institutionCode": rec["institutionCode"],
        }))
        if bidx is None:
            continue
        a = agg.setdefault(bidx, {"n": 0, "species": {}, "years": defaultdict(int), "basis": defaultdict(int),
                                  "datasets": defaultdict(int), "iucn_records": defaultdict(int),
                                  "no_species": 0, "min_year": None, "max_year": None})
        a["n"] += 1
        if yr:
            a["years"][str(yr)] += 1
            a["min_year"] = yr if a["min_year"] is None else min(a["min_year"], yr)
            a["max_year"] = yr if a["max_year"] is None else max(a["max_year"], yr)
        a["basis"][rec["basisOfRecord"]] += 1
        a["datasets"][(rec["datasetKey"], rec["datasetName"], rec["license"])] += 1
        if sk and rec["species"]:
            sp = a["species"].get(sk)
            if sp is None:
                sp = a["species"][sk] = {"scientificName": rec["species"], "taxonKey": sk, "family": rec["family"],
                                         "iucnRedListCategory": rec["iucnRedListCategory"], "count": 0,
                                         "firstYear": yr, "lastYear": yr}
            sp["count"] += 1
            if yr:
                sp["firstYear"] = yr if not sp["firstYear"] else min(sp["firstYear"], yr)
                sp["lastYear"] = yr if not sp["lastYear"] else max(sp["lastYear"], yr)
        else:
            a["no_species"] += 1

    if not points:
        raise ValueError("No GBIF records fell inside the selected HydroBASINS polygons")
    kept = "kept in occurrence tiles" if keep_unassigned else "dropped"
    log(f"{len(points):,} records ({unassigned:,} outside basins, {kept}; {skipped:,} without valid coordinates)")

    # --- Enrichment -------------------------------------------------------------------------
    if has_iucn_column:
        iucn_source = "download column iucnRedListCategory"
        iucn_map, titles = enrich(client, [], sorted(dataset_keys), enrichment_dir or gbif_dir / "enrichment")
    elif client is not None or (enrichment_dir and (enrichment_dir / "iucn_by_species_key.json").exists()):
        iucn_source = "GBIF API /species/{speciesKey}/iucnRedListCategory"
        iucn_map, titles = enrich(client, sorted(species_keys), sorted(dataset_keys),
                                  enrichment_dir or gbif_dir / "enrichment")
    else:
        iucn_source = "unavailable (run build without --no-enrich to populate threatened_count)"
        iucn_map, titles = {}, {}

    # --- Species index & taxonomy aliases (Stage 7) -----------------------------------------
    species_index_path = taxonomy_aliases_path = None
    species_index_counts: Dict[str, Any] = {}
    checklist: Optional[Dict[str, Any]] = None
    if checklist_path is not None:
        checklist = json.loads(Path(checklist_path).read_text(encoding="utf-8"))
        merged_iucn = {str(k): v for k, v in iucn_map.items()}
        for sk, tot in species_totals.items():
            if tot["iucn"] and not merged_iucn.get(str(sk)):
                merged_iucn[str(sk)] = tot["iucn"]
        outputs = build_species_outputs(checklist, species_totals, merged_iucn, THREATENED_IUCN)
        (staging_dir / "species_index.json").write_text(
            json.dumps(outputs["species_index"], separators=(",", ":"), ensure_ascii=False) + "\n", "utf-8")
        (staging_dir / "taxonomy_aliases.json").write_text(
            json.dumps(outputs["taxonomy_aliases"], separators=(",", ":"), ensure_ascii=False) + "\n", "utf-8")
        species_index_path = f"/releases/{version}/species_index.json"
        taxonomy_aliases_path = f"/releases/{version}/taxonomy_aliases.json"
        species_index_counts = outputs["species_index"]["counts"]
        log(f"species index: {species_index_counts}")

    # --- Basin features & shards ------------------------------------------------------------
    use_sqlite = shard_format == "sqlite" or (shard_format == "auto" and (level >= 7 or len(basins) > MAX_JSON_SHARDS))
    basin_features: List[Dict[str, Any]] = []
    shard_payloads: Dict[str, Dict[str, Any]] = {}
    threatened_species_total: set = set()
    for bidx, feat in enumerate(basins):
        props = feat["properties"]
        a = agg.get(bidx)
        species_list: List[Dict[str, Any]] = []
        threatened_records = 0
        if a:
            for sk, sp in a["species"].items():
                cat = sp["iucnRedListCategory"] or iucn_map.get(str(sk), "")
                sp["iucnRedListCategory"] = cat
                if cat in THREATENED_IUCN:
                    threatened_records += sp["count"]
                    threatened_species_total.add(sk)
                species_list.append(sp)
        species_list.sort(key=lambda s: (-s["count"], s["scientificName"]))
        hybas_id = props["hybas_id"]
        scalar = {
            "hybas_id": hybas_id,
            "pfaf_id": props["pfaf_id"],
            "basin_name": props["basin_name"],
            "sub_area_km2": round(float(props["sub_area_km2"]), 1),
            "up_area_km2": round(float(props["up_area_km2"]), 1),
            "occurrence_count": a["n"] if a else 0,
            "species_count": len(species_list),
            "threatened_count": threatened_records,
            "min_year": (a["min_year"] or 0) if a else 0,
            "max_year": (a["max_year"] or 0) if a else 0,
            "shard_path": f"basins/{hybas_id}.json",
        }
        basin_features.append({"type": "Feature", "id": hybas_id, "properties": scalar, "geometry": feat["geometry"]})
        datasets = []
        if a:
            for (dkey, dname, lic), cnt in a["datasets"].items():
                datasets.append({"datasetKey": dkey, "datasetName": dname or titles.get(dkey, "") or dkey,
                                 "license": lic, "count": cnt})
        datasets.sort(key=lambda d: (-d["count"], d["datasetKey"]))
        shard_payloads[hybas_id] = {
            "release_version": version,
            "basin": scalar,
            "hydrobasins": {"level": level, "next_down": props.get("next_down"), "main_bas": props.get("main_bas")},
            "bbox": [round(v, 5) for v in geometry_bbox(feat["geometry"])],
            "species": species_list,
            "records_without_species_rank": a["no_species"] if a else 0,
            "temporal_histogram": dict(sorted(a["years"].items())) if a else {},
            "basis_of_record": dict(sorted(a["basis"].items())) if a else {},
            "datasets": datasets,
            "provenance": {"gbif_doi": meta["doi"], "download_key": key},
        }

    shard_refs: List[Dict[str, Any]] = []
    if use_sqlite:
        sqlite_paths = _write_sqlite_shards(staging_dir, shard_payloads, prefix_len=max(2, level - 4))
        for feat in basin_features:
            feat["properties"]["shard_path"] = sqlite_paths[feat["properties"]["hybas_id"]]
            shard_payloads[feat["properties"]["hybas_id"]]["basin"]["shard_path"] = feat["properties"]["shard_path"]
        for rel in sorted(set(sqlite_paths.values())):
            f = staging_dir / rel
            shard_refs.append({"path": f"/releases/{version}/{rel}", "sha256": sha256_file(f), "bytes": f.stat().st_size,
                               "format": "sqlite"})
    else:
        for hybas_id, payload in sorted(shard_payloads.items()):
            rel = f"basins/{hybas_id}.json"
            f = staging_dir / rel
            f.write_text(json.dumps(payload, separators=(",", ":")) + "\n", "utf-8")
            shard_refs.append({"hybas_id": hybas_id, "path": f"/releases/{version}/{rel}",
                               "sha256": sha256_file(f), "bytes": f.stat().st_size})
    (staging_dir / "basins" / "index.json").write_text(json.dumps(shard_refs, separators=(",", ":")) + "\n", "utf-8")

    # --- Tiles ------------------------------------------------------------------------------
    citation = gbif_citation(meta)
    attribution = f"GBIF.org (doi:{meta['doi']}); HydroSHEDS / HydroBASINS v1c Level {level}"
    url_prefix = f"/releases/{version}"
    log("tiling basins")
    basin_tiles = build_basin_tiles(basin_features, basin_zooms[0], basin_zooms[1])
    basin_parts = write_partitioned_pmtiles(
        basin_tiles, staging_dir, "basins", url_prefix,
        _tile_layer_metadata(f"HydroBASINS Level {level} Summary",
                             "HydroBASINS polygons with scalar GBIF occurrence and species counts",
                             attribution, "basins", BASIN_SCALAR_FIELDS),
        max_bytes=max_archive_bytes, center_zoom=6,
    )
    del basin_tiles
    log("tiling occurrences")
    occ_tiles = build_occurrence_tiles(points, occurrence_zooms[0], occurrence_zooms[1], occurrence_zooms[2],
                                       max_points_per_tile=max_points_per_tile)
    occ_md = _tile_layer_metadata("GBIF Occurrences", "Aggregated bins (overview) and GBIF records (detail)",
                                  attribution, "occurrences", OCCURRENCE_POINT_FIELDS)
    occ_md["vector_layers"].append({"id": "occurrence_bins", "description": "Record counts per grid cell",
                                    "fields": {"count": "Number", "max_year": "Number"}})
    occ_parts = write_partitioned_pmtiles(occ_tiles, staging_dir, "occurrences", url_prefix, occ_md,
                                          max_bytes=max_archive_bytes, center_zoom=occurrence_zooms[1])
    del occ_tiles

    rasters = []
    if raster_tiles_dir is not None:
        rasters.append(pack_raster_layer(raster_tiles_dir, staging_dir, raster_name, url_prefix,
                                         raster_attribution, max_archive_bytes))

    # --- GeoJSON mirrors (only when small enough for the SVG fallback) -----------------------
    # Busiest basin first: the frontend opens the first basin, and verify-http samples it.
    ranked = sorted(basin_features, key=lambda f: (-f["properties"]["occurrence_count"], f["properties"]["hybas_id"]))
    basins_geo = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": f["id"], "properties": f["properties"],
         "geometry": round_geometry(simplify_geometry(f["geometry"], 0.003), 4)} for f in ranked]}
    basins_geo_bytes = json.dumps(basins_geo, separators=(",", ":")).encode("utf-8")
    basins_geojson_path = None
    if len(basins_geo_bytes) <= 10_000_000:
        (staging_dir / "basins.geojson").write_bytes(basins_geo_bytes)
        basins_geojson_path = f"{url_prefix}/basins.geojson"
    occ_geojson_path = None
    if len(points) <= 5000:
        occ_fc = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": p["gbifID"], "properties": p,
             "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]}} for lon, lat, p in points]}
        (staging_dir / "occurrences.geojson").write_text(json.dumps(occ_fc, separators=(",", ":")), "utf-8")
        occ_geojson_path = f"{url_prefix}/occurrences.geojson"
    own_ids_path = None
    if own_dataset_key:
        (staging_dir / "snapshot_own_occurrence_ids.json").write_text(json.dumps(sorted(set(own_ids))) + "\n", "utf-8")
        own_ids_path = f"{url_prefix}/snapshot_own_occurrence_ids.json"

    # --- Manifests --------------------------------------------------------------------------
    root_manifest_path = releases_dir / "manifest.json"
    if previous_version is None and root_manifest_path.exists():
        existing = json.loads(root_manifest_path.read_text(encoding="utf-8"))
        cur = existing.get("current_version")
        previous_version = cur if cur and cur != version else existing.get("previous_version")
    if previous_version == version:
        previous_version = None

    all_lon = [b for f in basin_features for b in geometry_bbox(f["geometry"])[0::2]]
    all_lat = [b for f in basin_features for b in geometry_bbox(f["geometry"])[1::2]]
    cov_bbox = [round(min(all_lon), 5), round(min(all_lat), 5), round(max(all_lon), 5), round(max(all_lat), 5)]
    hb_sources = {hb.name: {"sha256": sha256_file(hb), "bytes": hb.stat().st_size} for hb in hydrobasins_paths}
    if checklist_path is not None:
        hb_sources["checklist.json"] = {"sha256": sha256_file(Path(checklist_path)), "bytes": Path(checklist_path).stat().st_size}
    sources = {
        zip_path.name: {"sha256": sha256_file(zip_path), "bytes": zip_path.stat().st_size},
        "download_metadata.json": {"sha256": sha256_file(meta_path), "bytes": meta_path.stat().st_size},
        **({"query.json": {"sha256": sha256_file(query_path), "bytes": query_path.stat().st_size}} if query_path.exists() else {}),
        **hb_sources,
    }
    edir = enrichment_dir or gbif_dir / "enrichment"
    for cache_name in ("iucn_by_species_key.json", "dataset_titles.json"):
        if (edir / cache_name).exists():
            sources[cache_name] = {"sha256": sha256_file(edir / cache_name), "bytes": (edir / cache_name).stat().st_size}

    def layer_spec(parts: List[Dict[str, Any]], source_layer: str, geojson_path: Optional[str],
                   z: Tuple[int, int]) -> Dict[str, Any]:
        first = parts[0]
        spec = {"format": "pmtiles", "source_layer": source_layer, "path": first["path"], "sha256": first["sha256"],
                "bytes": first["bytes"], "min_zoom": z[0], "max_zoom": z[1], "tiles": sum(p["tiles"] for p in parts),
                "cache_control": IMMUTABLE_CACHE_CONTROL, "partitions": parts}
        if geojson_path:
            spec["geojson_path"] = geojson_path
        return spec

    shard_template = f"{url_prefix}/basins/{{hybas_id}}.json" if not use_sqlite else f"{url_prefix}/{{shard_path}}"
    release_manifest: Dict[str, Any] = {
        "schema_version": "1.1.0",
        "current_version": version,
        "previous_version": previous_version,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provenance": {
            "gbif_doi": meta["doi"],
            "gbif_download_key": key,
            "gbif_download_created": meta.get("created"),
            "gbif_download_url": meta.get("downloadLink"),
            "gbif_download_format": query_request.get("format"),
            "gbif_total_records": meta.get("totalRecords"),
            "gbif_license": meta.get("license"),
            "gbif_query": query_request,
            "citations": [{"citation": citation, "doi": meta["doi"]}],
            "hydrobasins": {"version": "v1c", "level": level, "file": hydrobasins_paths[0].name,
                            "files": [hb.name for hb in hydrobasins_paths],
                            "scope_bbox": scope_bbox, "pfaf_prefixes": list(pfaf_prefixes or []),
                            "citation": HYDROBASINS_CITATION},
            "iucn_source": iucn_source,
            **({"taxonomy": {"source": checklist.get("source"), "taxon": checklist.get("taxon"),
                             "fetched_at": checklist.get("fetched_at"),
                             "source_dataset_key": checklist.get("source_dataset_key")}} if checklist else {}),
            "sources": sources,
        },
        "spatial_coverage": {"crs": "EPSG:4326", "bbox": cov_bbox,
                             "center": [round((cov_bbox[0] + cov_bbox[2]) / 2, 5), round((cov_bbox[1] + cov_bbox[3]) / 2, 5)]},
        "counts": {
            "occurrences": len(points),
            "species": len(species_keys),
            "basins": len(basin_features),
            "download_rows": total_rows,
            "unassigned_occurrences": unassigned,
            "invalid_coordinates": skipped,
            "threatened_species": len({*threatened_species_total,
                                       *(k for k, v in species_totals.items()
                                         if (iucn_map.get(str(k)) or v["iucn"]) in THREATENED_IUCN)}),
            "unassigned_kept": unassigned if keep_unassigned else 0,
            **({"checklist_species": species_index_counts.get("species", 0)} if species_index_counts else {}),
        },
        "field_definitions": {
            "occurrence_count": "GBIF records whose coordinates fall inside the basin polygon",
            "species_count": "Distinct GBIF speciesKey values (records identified to species rank or below)",
            "threatened_count": f"Records of species with IUCN Red List category in {list(THREATENED_IUCN)}",
            "min_year": "Earliest record year (0 when no dated records)",
            "max_year": "Latest record year (0 when no dated records)",
        },
        "attribution": attribution,
        "licenses": sorted(l for l in licenses if l),
        "limits": {"max_archive_bytes": max_archive_bytes, "cdn_cache_ceiling_bytes": CDN_CACHE_CEILING_BYTES},
        "layers": {
            "basins": layer_spec(basin_parts, "basins", basins_geojson_path, basin_zooms),
            "occurrences": layer_spec(occ_parts, "occurrences", occ_geojson_path, (occurrence_zooms[0], occurrence_zooms[2])),
            "occurrence_bins_max_zoom": occurrence_zooms[1] - 1,
            "basin_shards_format": "sqlite" if use_sqlite else "json",
            "basin_shards_template": shard_template,
            "basin_shards_index": f"{url_prefix}/basins/index.json",
            "basin_shards_count": len(shard_refs),
            "basin_shards": shard_refs,
            **({"rasters": rasters} if rasters else {}),
            **({"snapshot_own_occurrence_ids": own_ids_path} if own_ids_path else {}),
            **({"species_index": species_index_path, "taxonomy_aliases": taxonomy_aliases_path}
               if species_index_path else {}),
        },
        "overlays": {"approved_observations": {"format": "geojson", "path": "/overlays/approved.geojson",
                                               "cache_control": MUTABLE_CACHE_CONTROL}},
    }
    (staging_dir / "release_manifest.json").write_text(json.dumps(release_manifest, indent=2) + "\n", "utf-8")

    # The root manifest is fetched on every visit: keep it small (sample shard only).
    root_manifest = json.loads(json.dumps(release_manifest))
    busiest = ranked[0]["properties"]["shard_path"] if ranked else ""
    sample = [r for r in shard_refs if r["path"].endswith("/" + busiest)] or shard_refs[:1]
    root_manifest["layers"]["basin_shards"] = sample[:1]

    if target_dir.exists() and force:
        shutil.rmtree(target_dir)
    staging_dir.rename(target_dir)
    root_manifest_path.write_text(json.dumps(root_manifest, indent=2) + "\n", "utf-8")
    return release_manifest


def pack_raster_layer(tiles_dir: Path, staging_dir: Path, name: str, url_prefix: str, attribution: str,
                      max_bytes: int) -> Dict[str, Any]:
    """Pack a ``{z}/{x}/{y}.webp`` tree (gdal2tiles / rio-rgbify output) into WebP raster PMTiles."""
    tiles: Dict[Tuple[int, int, int], bytes] = {}
    for f in tiles_dir.rglob("*.webp"):
        rel = f.relative_to(tiles_dir).parts
        if len(rel) != 3:
            continue
        data = f.read_bytes()
        if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise ValueError(f"{f} is not a WebP image")
        tiles[(int(rel[0]), int(rel[1]), int(Path(rel[2]).stem))] = data
    if not tiles:
        raise ValueError(f"No z/x/y.webp tiles found under {tiles_dir}")
    md = {"name": name, "format": "webp", "type": "baselayer", "attribution": attribution, "version": "2"}
    parts = write_partitioned_pmtiles(tiles, staging_dir, name, url_prefix, md, max_bytes=max_bytes,
                                      tile_type=TILE_TYPE_WEBP)
    return {"id": name, "format": "pmtiles", "tile_type": "webp", "tile_size": 256,
            "path": parts[0]["path"], "sha256": parts[0]["sha256"], "bytes": parts[0]["bytes"],
            "min_zoom": min(p["min_zoom"] for p in parts), "max_zoom": max(p["max_zoom"] for p in parts),
            "attribution": attribution, "cache_control": IMMUTABLE_CACHE_CONTROL, "partitions": parts}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--api-base", default=GBIF_API)
    p.add_argument("--workdir", type=Path, default=SCRIPT_DIR / "work")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("count", help="Pre-flight record count via occurrence search")
    s.add_argument("--query", type=Path, default=SCRIPT_DIR / "queries" / "pilot_willamette.json")

    s = sub.add_parser("request", help="Submit an asynchronous GBIF Occurrence Download")
    s.add_argument("--query", type=Path, default=SCRIPT_DIR / "queries" / "pilot_willamette.json")
    s.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    s.add_argument("--force", action="store_true")
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("fetch", help="Wait for a download key and save zip + DOI metadata + checksums")
    s.add_argument("--key", required=True)
    s.add_argument("--poll-seconds", type=float, default=60.0)

    s = sub.add_parser("fetch-hydrobasins", help="Download a HydroBASINS v1c region/level zip")
    s.add_argument("--region", default="na", choices=(*HYDROBASINS_REGIONS, "all"),
                   help="HydroBASINS region, or 'all' for the nine regions (global taxa)")
    s.add_argument("--level", type=int, default=6)

    for name in ("build", "run"):
        s = sub.add_parser(name)
        if name == "build":
            s.add_argument("--gbif-dir", type=Path, required=True, help="Directory written by `fetch`")
        else:
            s.add_argument("--query", type=Path, default=SCRIPT_DIR / "queries" / "pilot_willamette.json")
            s.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
            s.add_argument("--region", default="na", choices=(*HYDROBASINS_REGIONS, "all"))
        s.add_argument("--hydrobasins", type=Path, nargs="+", required=(name == "build"),
                       help="HydroSHEDS hybas_*_levNN_v1c.zip, .shp, or GeoJSON (several regions allowed)")
        s.add_argument("--keep-unassigned", action="store_true",
                       help="Keep records outside every basin in the occurrence tiles (global taxa, coasts)")
        s.add_argument("--checklist", type=Path, default=None,
                       help="checklist.json from taxonomy.py fetch: adds species_index.json + taxonomy_aliases.json")
        s.add_argument("--level", type=int, default=None)
        s.add_argument("--version", default="v0.2.0")
        s.add_argument("--previous-version", default=None,
                       help="Live current_version to retain for rollback (e.g. v0.1.0-demo)")
        s.add_argument("--output-root", type=Path, default=SCRIPT_DIR.parent / "build-r2")
        s.add_argument("--bbox", type=float, nargs=4, default=None, metavar=("W", "S", "E", "N"))
        s.add_argument("--pfaf-prefix", action="append", default=None)
        s.add_argument("--no-enrich", action="store_true", help="Skip GBIF IUCN/dataset-title lookups")
        s.add_argument("--own-dataset-key", default=None, help="basinmap.org GBIF dataset key for overlay dedup")
        s.add_argument("--max-archive-mb", type=float, default=MAX_ARCHIVE_BYTES / 1e6)
        s.add_argument("--basin-zooms", type=int, nargs=2, default=(0, 10))
        s.add_argument("--occurrence-zooms", type=int, nargs=3, default=(0, 9, 14),
                       metavar=("MIN", "POINT_MIN", "MAX"))
        s.add_argument("--max-points-per-tile", type=int, default=4000)
        s.add_argument("--shard-format", choices=("auto", "json", "sqlite"), default="auto")
        s.add_argument("--raster-tiles-dir", type=Path, default=None, help="{z}/{x}/{y}.webp tree to pack")
        s.add_argument("--raster-name", default="hillshade")
        s.add_argument("--raster-attribution", default="")
        s.add_argument("--force", action="store_true")
    return p.parse_args(argv)


def _build_from_args(args: argparse.Namespace, gbif_dir: Path, hydrobasins: Sequence[Path]) -> Dict[str, Any]:
    client = None if args.no_enrich else client_from_env(args.api_base)
    max_bytes = int(args.max_archive_mb * 1e6)
    if max_bytes > MAX_ARCHIVE_BYTES:
        raise ValueError("--max-archive-mb may not exceed 500 (hosting plan Section 4.3)")
    return build_release_from_download(
        gbif_dir, hydrobasins, args.version, args.output_root,
        previous_version=args.previous_version, basin_level=args.level, bbox=args.bbox,
        pfaf_prefixes=args.pfaf_prefix, client=client, own_dataset_key=args.own_dataset_key,
        max_archive_bytes=max_bytes, basin_zooms=tuple(args.basin_zooms),
        occurrence_zooms=tuple(args.occurrence_zooms), max_points_per_tile=args.max_points_per_tile,
        shard_format=args.shard_format, raster_tiles_dir=args.raster_tiles_dir, raster_name=args.raster_name,
        raster_attribution=args.raster_attribution, force=args.force,
        keep_unassigned=args.keep_unassigned, checklist_path=args.checklist,
    )


def _summary(manifest: Dict[str, Any]) -> Dict[str, Any]:
    layers = manifest["layers"]
    archives = [p for name in ("basins", "occurrences") for p in layers[name]["partitions"]]
    archives += [p for r in layers.get("rasters", []) for p in r["partitions"]]
    return {"version": manifest["current_version"], "doi": manifest["provenance"]["gbif_doi"],
            "counts": manifest["counts"], "archives": [{"path": a["path"], "bytes": a["bytes"],
                                                        "zooms": [a["min_zoom"], a["max_zoom"]]} for a in archives]}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    client = client_from_env(args.api_base)
    if args.command == "count":
        query = load_query(args.query)
        print(json.dumps({"query": str(args.query), "count": client.count_predicate(query["request"]["predicate"])}))
    elif args.command == "request":
        print(json.dumps(request_download(client, load_query(args.query), args.max_records, args.force,
                                          args.dry_run), indent=2))
    elif args.command == "fetch":
        out = fetch_download(client, args.key, args.workdir, poll_seconds=args.poll_seconds)
        print(json.loads((out / "provenance.json").read_text())["citation"])
        print(out)
    elif args.command == "fetch-hydrobasins":
        regions = HYDROBASINS_REGIONS if args.region == "all" else (args.region,)
        for region in regions:
            print(fetch_hydrobasins(client, region, args.level, args.workdir))
    elif args.command == "build":
        print(json.dumps(_summary(_build_from_args(args, args.gbif_dir, args.hydrobasins)), indent=2))
    elif args.command == "run":
        res = request_download(client, load_query(args.query), args.max_records, args.force)
        log(f"submitted GBIF download {res['key']} (expected ~{res['expected_records']} records)")
        gbif_dir = fetch_download(client, res["key"], args.workdir)
        regions = HYDROBASINS_REGIONS if args.region == "all" else (args.region,)
        hb = args.hydrobasins or [fetch_hydrobasins(client, r, args.level or 6, args.workdir) for r in regions]
        print(json.dumps(_summary(_build_from_args(args, gbif_dir, hb)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
