#!/usr/bin/env python3
"""Manifest-aware R2 release publisher, HTTP 206 verifier, and cleanup tool.

Implements the release publication workflow from Sections 4.1, 4.3, and 5.2:
  - Uploads immutable /releases/<version>/* objects before updating manifest.json
  - Verifies HTTP 206 Partial Content, Content-Range, ETag, Cache-Control, and CORS headers
  - Performs manifest-aware release cleanup retaining only current and previous versions,
    strictly protecting /media/, /exports/, /overlays/, and /releases/manifest.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
import hashlib
import http.client
import http.server
import json
import mimetypes
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pmtiles_writer import parse_pmtiles_header  # noqa: E402

IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
MUTABLE_CACHE_CONTROL = "public, max-age=60, must-revalidate"
PROTECTED_TOP_PREFIXES = {"media", "exports", "overlays"}
RANGE_HEADER_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
# Section 4.3: Cloudflare's CDN skips caching objects above 512 MB; keep a margin.
MAX_ARCHIVE_BYTES = 500_000_000
# `wrangler r2 object put` rejects uploads above ~315 MB; larger objects need the S3 API.
WRANGLER_MAX_UPLOAD_BYTES = 300 * 1024 * 1024


def iter_archive_specs(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every PMTiles/raster archive referenced by a release manifest (partitions included)."""
    layers = manifest.get("layers", {})
    specs: List[Dict[str, Any]] = []
    for name in ("basins", "occurrences"):
        layer = layers.get(name)
        if layer:
            specs.extend(layer.get("partitions") or [layer])
    for raster in layers.get("rasters", []):
        specs.extend(raster.get("partitions") or [raster])
    for lvl in (layers.get("basin_levels") or {}).values():
        specs.extend(lvl.get("pmtiles") or [])  # extra HydroBASINS levels (Stage 8)
    return specs


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_upload_plan(root: Path, version: str) -> List[Dict[str, str]]:
    """Return ordered list of R2 uploads (immutable versioned files first, manifest last)."""
    releases_dir = root / "releases"
    version_dir = releases_dir / version
    if not version_dir.is_dir():
        raise FileNotFoundError(f"Release version directory not found: {version_dir}")

    release_manifest_path = version_dir / "release_manifest.json"
    if not release_manifest_path.exists():
        raise FileNotFoundError(f"Missing release_manifest.json in {version_dir}")

    release_manifest = json.loads(release_manifest_path.read_text(encoding="utf-8"))
    if release_manifest.get("current_version") != version:
        raise ValueError(
            f"release_manifest.json current_version ({release_manifest.get('current_version')!r}) "
            f"does not match target version {version!r}"
        )

    # Verify SHA-256 and the < 500 MB CDN ceiling of every archive before publishing anything.
    for spec in iter_archive_specs(release_manifest):
        layer_file = root / spec["path"].lstrip("/")
        if not layer_file.exists():
            raise FileNotFoundError(f"Missing layer file: {layer_file}")
        actual_sha = sha256_file(layer_file)
        if actual_sha != spec["sha256"]:
            raise ValueError(
                f"Checksum mismatch for {layer_file}: expected {spec['sha256']}, got {actual_sha}"
            )

    root_manifest_path = releases_dir / "manifest.json"
    if not root_manifest_path.exists():
        raise FileNotFoundError(f"Missing root manifest: {root_manifest_path}")
    root_manifest = json.loads(root_manifest_path.read_text(encoding="utf-8"))
    if root_manifest.get("current_version") != version:
        raise ValueError(
            f"Root manifest current_version ({root_manifest.get('current_version')!r}) "
            f"does not match target version {version!r}"
        )

    uploads: List[Dict[str, str]] = []
    for file_path in sorted(p for p in version_dir.rglob("*") if p.is_file()):
        if file_path.name.startswith("."):
            continue
        size = file_path.stat().st_size
        if size >= MAX_ARCHIVE_BYTES:
            raise ValueError(
                f"{file_path} is {size} bytes; objects on data.basinmap.org must stay below "
                f"{MAX_ARCHIVE_BYTES} bytes (Section 4.3). Re-run the build with partitioning."
            )
        rel_key = file_path.relative_to(root).as_posix()
        uploads.append(
            {
                "key": rel_key,
                "local_path": str(file_path),
                "cache_control": IMMUTABLE_CACHE_CONTROL,
                "content_type": _guess_content_type(file_path),
            }
        )

    # Manifest must be published LAST so browsers never read a pointer to unuploaded layers.
    uploads.append(
        {
            "key": "releases/manifest.json",
            "local_path": str(root_manifest_path),
            "cache_control": MUTABLE_CACHE_CONTROL,
            "content_type": "application/json",
        }
    )
    return uploads


def _guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pmtiles":
        return "application/vnd.pmtiles"
    if suffix in (".geojson", ".json"):
        return "application/json"
    if suffix == ".zip":
        return "application/zip"
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _run_with_retries(cmd: List[str], retries: int, sleep: Callable[[float], None] = time.sleep) -> None:
    """Run one upload command, retrying transient failures (Cloudflare 5xx, network) with backoff."""
    for attempt in range(retries + 1):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return
        if attempt == retries:
            sys.stderr.write(proc.stdout[-2000:] + proc.stderr[-4000:])
            raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout, proc.stderr)
        sleep(min(60.0, 2.0 * 2 ** attempt))


def run_upload_commands(commands: List[List[str]], jobs: int = 1, retries: int = 4,
                        sleep: Callable[[float], None] = time.sleep) -> None:
    """Upload every object; the last command (the root ``releases/manifest.json``) runs only after
    all others succeeded, so the live site never points at a partially uploaded release."""
    if not commands:
        return
    body, last = commands[:-1], commands[-1]
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = [pool.submit(_run_with_retries, cmd, retries, sleep) for cmd in body]
        try:
            for fut in concurrent.futures.as_completed(futures):
                fut.result()
                done += 1
                if done % 250 == 0 or done == len(body):
                    print(f"[publish] uploaded {done}/{len(body)} release objects", flush=True)
        except BaseException:
            for f in futures:
                f.cancel()
            raise
    _run_with_retries(last, retries, sleep)
    print("[publish] switched releases/manifest.json", flush=True)


def r2_s3_client(account_id: Optional[str] = None, jobs: int = 32) -> Any:
    """boto3 S3 client for Cloudflare R2 (credentials: R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY,
    account: CLOUDFLARE_ACCOUNT_ID). One pooled HTTPS connection set for every object, with
    botocore's adaptive retries for transient 5xx/throttling."""
    import boto3  # type: ignore
    from botocore.config import Config  # type: ignore

    account = account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    key_id = os.environ.get("R2_ACCESS_KEY_ID", "")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY", "")
    if not (account and key_id and secret):
        raise PermissionError("--mode s3 needs CLOUDFLARE_ACCOUNT_ID, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY")
    return boto3.client(
        "s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com", region_name="auto",
        aws_access_key_id=key_id, aws_secret_access_key=secret,
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"}, max_pool_connections=max(10, jobs + 4)),
    )


def upload_plan_s3(plan: List[Dict[str, Any]], bucket: str, client: Any, jobs: int = 32,
                   retries: int = 4, sleep: Callable[[float], None] = time.sleep) -> None:
    """Upload a release plan through the S3 API; the root manifest (last item) goes only after
    every other object succeeded. Large files use multipart automatically (no 300 MiB limit)."""
    if not plan:
        return

    def put(item: Dict[str, Any]) -> None:
        for attempt in range(retries + 1):
            try:
                client.upload_file(item["local_path"], bucket, item["key"], ExtraArgs={
                    "CacheControl": item["cache_control"], "ContentType": item["content_type"]})
                return
            except Exception:  # botocore already retried transient errors; this covers the rest
                if attempt == retries:
                    raise
                sleep(min(60.0, 2.0 * 2 ** attempt))

    body, last = plan[:-1], plan[-1]
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = [pool.submit(put, item) for item in body]
        try:
            for fut in concurrent.futures.as_completed(futures):
                fut.result()
                done += 1
                if done % 1000 == 0 or done == len(body):
                    print(f"[publish] uploaded {done}/{len(body)} release objects", flush=True)
        except BaseException:
            for f in futures:
                f.cancel()
            raise
    put(last)
    print(f"[publish] switched {last['key']}", flush=True)


def make_root_manifest(release_manifest: Dict[str, Any], live_manifest: Optional[Dict[str, Any]],
                       version: str) -> Dict[str, Any]:
    """Root ``releases/manifest.json`` that makes an already-published release live again.

    Mirrors the builder: a copy of the release's own manifest with only one sample basin shard
    (the root manifest is fetched on every visit), and ``previous_version`` set to whatever was live
    so the switch itself can be undone.
    """
    if release_manifest.get("current_version") != version:
        raise ValueError(f"release_manifest.json is for {release_manifest.get('current_version')!r}, not {version!r}")
    root = json.loads(json.dumps(release_manifest))
    shards = root.get("layers", {}).get("basin_shards")
    if isinstance(shards, list):
        root["layers"]["basin_shards"] = shards[:1]
    live = (live_manifest or {}).get("current_version")
    if live and live != version:
        root["previous_version"] = live
    return root


DATASET_CATALOG_KEY = "releases/datasets.json"


def dataset_manifest_key(dataset_id: str) -> str:
    """Root manifest key for a dataset; the default dataset keeps ``releases/manifest.json``."""
    if dataset_id in ("", "default"):
        return "releases/manifest.json"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", dataset_id):
        raise ValueError(f"dataset id {dataset_id!r} must be lower-case letters, digits and dashes")
    return f"releases/datasets/{dataset_id}/manifest.json"


def update_dataset_catalog(catalog: Optional[Dict[str, Any]], dataset_id: str, title: str, version: str,
                           description: str = "", now: Optional[str] = None) -> Dict[str, Any]:
    """Upsert one dataset in ``releases/datasets.json`` (Stage 9 dataset selector).

    The default dataset (``releases/manifest.json``) is always listed first; others follow by title.
    """
    did = dataset_id or "default"
    entries = {e["id"]: e for e in (catalog or {}).get("datasets", []) if isinstance(e, dict) and e.get("id")}
    prev = entries.get(did, {})
    entries[did] = {
        "id": did,
        "title": title or prev.get("title") or ("Live release" if did == "default" else did),
        "description": description or prev.get("description", ""),
        "manifest_path": "/" + dataset_manifest_key(did),
        "version": version,
        "default": did == "default",
        "updated_at": now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    ordered = sorted(entries.values(), key=lambda e: (not e["default"], e["title"].lower()))
    return {"schema": "basinmap-datasets/1", "datasets": ordered}


def publish_release(
    root: Path,
    version: Optional[str],
    mode: str = "local",
    bucket: str = "basinmap-public-data",
    dest: Optional[Path] = None,
    dry_run: bool = False,
    rclone_remote: str = "r2",
    jobs: int = 1,
    retries: int = 4,
    s3_client: Any = None,
) -> Dict[str, Any]:
    if not version:
        manifest_path = root / "releases" / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"Cannot infer version without {manifest_path}")
        version = json.loads(manifest_path.read_text(encoding="utf-8"))["current_version"]

    plan = build_upload_plan(root, version)

    if mode == "local":
        if dest is not None:
            dest_resolved = dest.resolve()
            if dest_resolved == root.resolve():
                raise ValueError("--dest must be a separate target directory from --root")
            for item in plan:
                target = dest_resolved / item["key"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if not dry_run:
                    shutil.copy2(item["local_path"], target)
            if not dry_run:
                (dest_resolved / ".r2-publish-plan.json").write_text(
                    json.dumps(plan, indent=2) + "\n", encoding="utf-8"
                )
        return {"mode": "local", "version": version, "object_count": len(plan), "plan": plan}

    if mode == "wrangler":
        too_big = [i["key"] for i in plan if Path(i["local_path"]).stat().st_size > WRANGLER_MAX_UPLOAD_BYTES]
        if too_big:
            raise ValueError(
                "wrangler r2 object put cannot upload objects above 300 MiB; use --mode rclone for: "
                + ", ".join(too_big)
            )
        commands: List[List[str]] = []
        for item in plan:
            cmd = [
                "npx",
                "wrangler",
                "r2",
                "object",
                "put",
                f"{bucket}/{item['key']}",
                "--remote",
                "--file",
                item["local_path"],
                "--cache-control",
                item["cache_control"],
                "--content-type",
                item["content_type"],
            ]
            commands.append(cmd)
        if not dry_run:
            run_upload_commands(commands, jobs=jobs, retries=retries)
        return {
            "mode": "wrangler",
            "version": version,
            "bucket": bucket,
            "object_count": len(plan),
            "dry_run": dry_run,
            "commands": [" ".join(c) for c in commands],
        }

    if mode == "s3":
        if not dry_run:
            upload_plan_s3(plan, bucket, s3_client or r2_s3_client(jobs=jobs), jobs=jobs, retries=retries)
        return {"mode": "s3", "version": version, "bucket": bucket, "object_count": len(plan),
                "dry_run": dry_run, "keys": [i["key"] for i in plan]}

    if mode == "rclone":
        # S3-compatible upload via an rclone remote configured for R2 (no size limit below 5 GB
        # single-part; rclone switches to multipart automatically).
        commands = []
        for item in plan:
            cmd = [
                "rclone", "copyto", item["local_path"], f"{rclone_remote}:{bucket}/{item['key']}",
                "--s3-no-check-bucket",
                "--header-upload", f"Cache-Control: {item['cache_control']}",
                "--header-upload", f"Content-Type: {item['content_type']}",
            ]
            commands.append(cmd)
        if not dry_run:
            run_upload_commands(commands, jobs=jobs, retries=retries)
        return {
            "mode": "rclone",
            "version": version,
            "bucket": bucket,
            "object_count": len(plan),
            "dry_run": dry_run,
            "commands": [" ".join(c) for c in commands],
        }

    raise ValueError(f"Unsupported publish mode: {mode!r}")


# ---------------------------------------------------------------------------
# Ephemeral R2-compatible HTTP Range & CORS server for local/CI verification
# ---------------------------------------------------------------------------

class R2SimulatedRangeHandler(http.server.BaseHTTPRequestHandler):
    """HTTP server emulating R2 Custom Domain Range, ETag, Cache-Control, and CORS headers."""

    def __init__(self, *args: Any, bucket_root: Path, **kwargs: Any) -> None:
        self.bucket_root = bucket_root.resolve()
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Keep unit test and CLI output clean.
        return

    def _resolve_target(self) -> Optional[Path]:
        req_path = self.path.split("?", 1)[0].lstrip("/")
        if not req_path:
            return None
        candidate = (self.bucket_root / req_path).resolve()
        try:
            candidate.relative_to(self.bucket_root)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        return candidate

    def _cache_control_for(self, target: Path) -> str:
        rel = target.relative_to(self.bucket_root).as_posix()
        if rel == "releases/manifest.json" or rel.startswith("overlays/"):
            return MUTABLE_CACHE_CONTROL
        if rel.startswith("releases/") or rel.startswith("media/"):
            return IMMUTABLE_CACHE_CONTROL
        return MUTABLE_CACHE_CONTROL

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Range, If-Match")
        self.send_header("Access-Control-Expose-Headers", "ETag, Content-Length, Content-Range")
        self.send_header("Accept-Ranges", "bytes")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle_get(send_body=False)

    def do_GET(self) -> None:  # noqa: N802
        self._handle_get(send_body=True)

    def _handle_get(self, send_body: bool) -> None:
        target = self._resolve_target()
        if target is None:
            self.send_response(404)
            self._send_cors_headers()
            self.end_headers()
            return

        data = target.read_bytes()
        total_len = len(data)
        etag = f'"{hashlib.md5(data).hexdigest()}"'
        cache_control = self._cache_control_for(target)
        content_type = _guess_content_type(target)

        range_header = self.headers.get("Range")
        if range_header:
            match = RANGE_HEADER_RE.match(range_header.strip())
            if not match:
                self.send_response(416)
                self._send_cors_headers()
                self.send_header("Content-Range", f"bytes */{total_len}")
                self.end_headers()
                return

            start_s, end_s = match.groups()
            if start_s == "" and end_s == "":
                self.send_response(416)
                self._send_cors_headers()
                self.send_header("Content-Range", f"bytes */{total_len}")
                self.end_headers()
                return

            if start_s == "":
                suffix_len = int(end_s)
                start = max(0, total_len - suffix_len)
                end = total_len - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else total_len - 1

            if start >= total_len or end < start:
                self.send_response(416)
                self._send_cors_headers()
                self.send_header("Content-Range", f"bytes */{total_len}")
                self.end_headers()
                return

            end = min(end, total_len - 1)
            slice_bytes = data[start : end + 1]
            self.send_response(206)
            self._send_cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(slice_bytes)))
            self.send_header("Content-Range", f"bytes {start}-{end}/{total_len}")
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            if send_body:
                self.wfile.write(slice_bytes)
            return

        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(total_len))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        if send_body:
            self.wfile.write(data)


class _ReusableThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def start_range_test_server(bucket_root: Path) -> Tuple[_ReusableThreadingHTTPServer, str, threading.Thread]:
    handler = functools.partial(R2SimulatedRangeHandler, bucket_root=bucket_root)
    server = _ReusableThreadingHTTPServer(("127.0.0.1", 0), handler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{host}:{port}", thread


HTTP_USER_AGENT = "basinmap-publish-verifier/1.0 (+https://basinmap.org)"


def _http_request(url: str, method: str = "GET", headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], bytes]:
    # Cloudflare bot rules on data.basinmap.org refuse Python's default "Python-urllib" identity.
    req = urllib.request.Request(url, method=method, headers={"User-Agent": HTTP_USER_AGENT, **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if url.startswith("http://127.0.0.1") else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=15) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read()
            return resp.status, resp_headers, body
    except urllib.error.HTTPError as err:
        resp_headers = {k.lower(): v for k, v in err.headers.items()}
        return err.code, resp_headers, err.read()


def _socketpair_http_request(
    bucket_root: Path,
    path: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict[str, str], bytes]:
    """Execute a real HTTP/1.0 wire-protocol request over socketpair against R2SimulatedRangeHandler."""
    client_sock, server_sock = socket.socketpair()
    err_holder: List[BaseException] = []

    def serve_one() -> None:
        try:
            R2SimulatedRangeHandler(server_sock, ("127.0.0.1", 0), None, bucket_root=bucket_root)
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
        hdr_lines = [f"{method} {path} HTTP/1.0", "Host: data.basinmap.org"]
        for k, v in (headers or {}).items():
            hdr_lines.append(f"{k}: {v}")
        raw_req = ("\r\n".join(hdr_lines) + "\r\n\r\n").encode("ascii")
        client_sock.sendall(raw_req)

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


def verify_http_delivery(
    base_url: Optional[str] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Verify manifest, 206 byte-range reads, ETag, Cache-Control, and CORS headers."""
    server = None
    use_socketpair = False
    resolved_base = base_url

    def do_req(path: str, method: str = "GET", headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], bytes]:
        if use_socketpair:
            assert root is not None
            return _socketpair_http_request(root, path, method=method, headers=headers)
        assert resolved_base is not None
        return _http_request(f"{resolved_base.rstrip('/')}{path}", method=method, headers=headers)

    try:
        if resolved_base is None:
            if root is None:
                raise ValueError("Provide either --base-url or --root")
            try:
                server, resolved_base, _ = start_range_test_server(root)
                # Probe TCP loopback; restricted sandboxes disallow 127.0.0.1 sockets.
                manifest_status, manifest_headers, manifest_body = do_req("/releases/manifest.json")
            except (OSError, urllib.error.URLError):
                if server is not None:
                    server.shutdown()
                    server.server_close()
                    server = None
                use_socketpair = True
                resolved_base = "http://socketpair-r2.local"
                manifest_status, manifest_headers, manifest_body = do_req("/releases/manifest.json")
        else:
            manifest_status, manifest_headers, manifest_body = do_req("/releases/manifest.json")

        base = resolved_base.rstrip("/")
        if manifest_status != 200:
            raise AssertionError(f"Expected 200 for manifest.json, got {manifest_status}")
        if "max-age=60" not in manifest_headers.get("cache-control", ""):
            raise AssertionError(
                f"Expected short max-age=60 Cache-Control on manifest.json, got {manifest_headers.get('cache-control')!r}"
            )
        manifest = json.loads(manifest_body.decode("utf-8"))

        verified_layers: Dict[str, Any] = {}
        archive_checks: List[Tuple[str, str]] = [
            ("basins", manifest["layers"]["basins"]["path"]),
            ("occurrences", manifest["layers"]["occurrences"]["path"]),
        ]
        for spec in iter_archive_specs(manifest):
            if spec["path"] not in {p for _, p in archive_checks}:
                archive_checks.append((spec["path"].rsplit("/", 1)[-1], spec["path"]))
        for layer_name, layer_path in archive_checks:
            layer_url = f"{base}{layer_path}"

            head_status, head_headers, _ = do_req(layer_path, method="HEAD")
            if head_status != 200:
                raise AssertionError(f"HEAD {layer_url} returned {head_status}")

            status, headers, body = do_req(
                layer_path,
                method="GET",
                headers={"Range": "bytes=0-126", "Origin": "https://basinmap.org"},
            )
            if status != 206:
                raise AssertionError(f"Expected 206 Partial Content for {layer_url}, got {status}")
            if headers.get("content-length") != "127" or len(body) != 127:
                raise AssertionError(
                    f"Expected 127 bytes for header range on {layer_url}, got len={len(body)}, "
                    f"Content-Length={headers.get('content-length')!r}"
                )
            content_range = headers.get("content-range", "")
            if not content_range.startswith("bytes 0-126/"):
                raise AssertionError(f"Invalid Content-Range on {layer_url}: {content_range!r}")
            if not headers.get("etag"):
                raise AssertionError(f"Missing ETag header on {layer_url}")
            if headers.get("access-control-allow-origin") != "*":
                raise AssertionError(
                    f"Expected Access-Control-Allow-Origin: *, got {headers.get('access-control-allow-origin')!r}"
                )
            expose = {h.strip().lower() for h in headers.get("access-control-expose-headers", "").split(",")}
            for req_hdr in ("etag", "content-length", "content-range"):
                if req_hdr not in expose:
                    raise AssertionError(
                        f"Missing {req_hdr} in Access-Control-Expose-Headers on {layer_url}: {expose}"
                    )
            if "immutable" not in headers.get("cache-control", ""):
                raise AssertionError(
                    f"Expected immutable Cache-Control on {layer_url}, got {headers.get('cache-control')!r}"
                )

            parsed_header = parse_pmtiles_header(body)
            verified_layers[layer_name] = {
                "url": layer_url,
                "status": status,
                "content_range": content_range,
                "etag": headers["etag"],
                "pmtiles_min_zoom": parsed_header["min_zoom"],
                "pmtiles_max_zoom": parsed_header["max_zoom"],
                "num_addressed_tiles": parsed_header["num_addressed_tiles"],
            }

        # Verify at least one basin shard is readable.
        first_shard = manifest["layers"]["basin_shards"][0]
        shard_status, shard_headers, shard_body = do_req(first_shard["path"])
        if shard_status != 200:
            raise AssertionError(f"Expected 200 for basin shard {first_shard['path']}, got {shard_status}")
        if "immutable" not in shard_headers.get("cache-control", ""):
            raise AssertionError(f"Expected immutable Cache-Control on shard {first_shard['path']}")
        shard_json = json.loads(shard_body.decode("utf-8"))
        if "basin" not in shard_json or "species" not in shard_json:
            raise AssertionError(f"Basin shard {first_shard['path']} is missing basin/species keys")
        if shard_json["basin"].get("occurrence_count", 0) > 0 and not shard_json["species"]:
            raise AssertionError(f"Basin shard {first_shard['path']} has records but an empty species list")

        return {
            "base_url": base,
            "transport": "socketpair" if use_socketpair else "http",
            "current_version": manifest["current_version"],
            "verified_layers": verified_layers,
            "verified_shard": first_shard["path"],
        }
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


# ---------------------------------------------------------------------------
# Manifest-aware release cleanup (never touches /media, /exports, /overlays)
# ---------------------------------------------------------------------------

def cleanup_releases(root: Path, dry_run: bool = False) -> Dict[str, Any]:
    """Delete /releases/<ver>/ directories not referenced as current or previous in manifest.json."""
    root_resolved = root.resolve()
    releases_dir = (root_resolved / "releases").resolve()
    manifest_path = releases_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Cannot run cleanup without manifest: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    current_ver = manifest.get("current_version")
    previous_ver = manifest.get("previous_version")
    if not current_ver:
        raise ValueError("manifest.json is missing current_version; aborting cleanup")

    keep_versions = {v for v in (current_ver, previous_ver) if v}
    retained: List[str] = []
    deleted: List[str] = []

    for entry in sorted(releases_dir.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith("."):
            continue
        if name in PROTECTED_TOP_PREFIXES:
            raise RuntimeError(f"Refusing to evaluate protected prefix inside releases/: {entry}")

        resolved_entry = entry.resolve()
        if resolved_entry.parent != releases_dir:
            raise RuntimeError(f"Unsafe path escape detected during cleanup: {resolved_entry}")

        if name in keep_versions:
            retained.append(name)
        else:
            if not dry_run:
                shutil.rmtree(resolved_entry)
            deleted.append(name)

    return {
        "current_version": current_ver,
        "previous_version": previous_ver,
        "retained": retained,
        "deleted": deleted,
        "dry_run": dry_run,
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish, verify, and clean up basinmap.org releases.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_pub = subparsers.add_parser("publish", help="Stage or upload a release to R2")
    p_pub.add_argument("--root", type=Path, default=SCRIPT_DIR.parent / "public-r2")
    p_pub.add_argument("--version", default=None, help="Release version (defaults to manifest.json current_version)")
    p_pub.add_argument("--mode", choices=["local", "wrangler", "rclone", "s3"], default="local",
                       help="s3 = R2 S3 API via boto3 (fastest; needs R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY)")
    p_pub.add_argument("--rclone-remote", default="r2", help="rclone remote configured for R2 (--mode rclone)")
    p_pub.add_argument("--bucket", default="basinmap-public-data")
    p_pub.add_argument("--dest", type=Path, default=None, help="Target local directory for --mode local")
    p_pub.add_argument("--dry-run", action="store_true")
    p_pub.add_argument("--jobs", type=int, default=1, help="Parallel uploads (manifest.json is always last)")
    p_pub.add_argument("--retries", type=int, default=4, help="Retries per object on transient failures")

    p_root = subparsers.add_parser("make-root", help="Write a root manifest.json that switches the live release")
    p_root.add_argument("--release-manifest", type=Path, required=True, help="releases/<version>/release_manifest.json")
    p_root.add_argument("--live-manifest", type=Path, default=None, help="current releases/manifest.json (optional)")
    p_root.add_argument("--version", required=True)
    p_root.add_argument("--out", type=Path, required=True)

    p_cat = subparsers.add_parser("catalog", help="Upsert a dataset in releases/datasets.json")
    p_cat.add_argument("--catalog", type=Path, default=None, help="current datasets.json (optional)")
    p_cat.add_argument("--dataset-id", default="default")
    p_cat.add_argument("--title", default="")
    p_cat.add_argument("--description", default="")
    p_cat.add_argument("--version", required=True)
    p_cat.add_argument("--out", type=Path, required=True)

    p_ver = subparsers.add_parser("verify-http", help="Verify 206 byte-range, ETag, Cache-Control, and CORS")
    p_ver.add_argument("--root", type=Path, default=SCRIPT_DIR.parent / "public-r2")
    p_ver.add_argument("--base-url", default=None, help="Live R2 custom domain URL (e.g. https://data.basinmap.org)")

    p_cln = subparsers.add_parser("cleanup", help="Delete releases older than current and previous")
    p_cln.add_argument("--root", type=Path, default=SCRIPT_DIR.parent / "public-r2")
    p_cln.add_argument("--dry-run", action="store_true")

    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.command == "publish":
        result = publish_release(
            root=args.root,
            version=args.version,
            mode=args.mode,
            bucket=args.bucket,
            dest=args.dest,
            dry_run=args.dry_run,
            rclone_remote=args.rclone_remote,
            jobs=args.jobs,
            retries=args.retries,
        )
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "catalog":
        current = None
        if args.catalog and args.catalog.exists():
            try:
                current = json.loads(args.catalog.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                current = None
        cat = update_dataset_catalog(current, args.dataset_id, args.title, args.version, args.description)
        args.out.write_text(json.dumps(cat, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(cat, indent=2))
        return 0

    if args.command == "make-root":
        live = None
        if args.live_manifest and args.live_manifest.exists():
            try:
                live = json.loads(args.live_manifest.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                live = None  # missing object downloaded as an empty file: treat as a new dataset
        root = make_root_manifest(json.loads(args.release_manifest.read_text(encoding="utf-8")), live, args.version)
        args.out.write_text(json.dumps(root, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"current_version": root["current_version"], "previous_version": root.get("previous_version"),
                          "out": str(args.out)}, indent=2))
        return 0

    if args.command == "verify-http":
        result = verify_http_delivery(base_url=args.base_url, root=args.root)
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "cleanup":
        result = cleanup_releases(root=args.root, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
