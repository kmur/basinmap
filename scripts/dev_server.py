#!/usr/bin/env python3
"""Local dev server emulating Workers Static Assets + /api/* + R2 HTTP 206 ranges."""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import mimetypes
import re
import socketserver
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

ROOT_DIR = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT_DIR / "dist"
PUBLIC_R2_DIR = ROOT_DIR / "public-r2"
LOCAL_DATA_DIR = ROOT_DIR / ".local-data"

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend_engine import BasinmapBackend, HttpError, MAX_UPLOAD_BYTES  # noqa: E402
from export_dwca import export_dwca  # noqa: E402

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pmtiles":
        return "application/vnd.pmtiles"
    if ext in (".json", ".geojson"):
        return "application/json; charset=utf-8"
    if ext == ".js":
        return "application/javascript; charset=utf-8"
    if ext == ".css":
        return "text/css; charset=utf-8"
    if ext == ".html":
        return "text/html; charset=utf-8"
    if ext == ".zip":
        return "application/zip"
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


class LocalBasinmapHandler(http.server.BaseHTTPRequestHandler):
    """Serves /api/* (or 503 outage), R2 /releases/* + /overlays/* + /media/* with 206 ranges, and ./dist SPA."""

    def __init__(
        self,
        *args: Any,
        dist_dir: Path,
        r2_dir: Path,
        backend: Optional[BasinmapBackend] = None,
        simulate_api_outage: bool = False,
        **kwargs: Any,
    ) -> None:
        self.dist_dir = dist_dir.resolve()
        self.r2_dir = r2_dir.resolve()
        self.backend = backend
        self.simulate_api_outage = simulate_api_outage
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _read_body_bytes(self) -> bytes:
        hdr = self.headers.get("Content-Length")
        if hdr is None or not hdr.strip().isdigit():
            return b""
        length = int(hdr)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _headers_dict(self) -> dict:
        return {k: v for k, v in self.headers.items()}

    def _send_json(self, status: int, payload: Any, cache_control: str) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle_api(self, path: str) -> None:
        if self.simulate_api_outage:
            self._send_json(
                503,
                {"error": "Simulated API outage", "path": path},
                "private, no-store",
            )
            return

        if path == "/api/health" and self.command in ("GET", "HEAD"):
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "basinmap-worker-api",
                    "environment": "development",
                    "public_data_url": "",
                    "bindings": {
                        "d1": True,
                        "public_r2": True,
                        "intake_r2": True,
                    },
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                "private, no-store",
            )
            return

        if path == "/api/config" and self.command in ("GET", "HEAD"):
            self._send_json(
                200,
                {
                    "public_data_url": "",
                    "manifest_path": "/releases/manifest.json",
                    "approved_overlay_path": "/overlays/approved.geojson",
                    "supported_licenses": ["CC-BY 4.0", "CC0 1.0"],
                    "submissions_enabled": True,
                },
                "public, max-age=60, must-revalidate",
            )
            return

        if path.startswith("/api/r2/") and self.command in ("GET", "HEAD"):
            rel_path = path[len("/api/r2") :]
            if any(rel_path.startswith(p) for p in ("/releases/", "/overlays/", "/media/", "/exports/")):
                if self._serve_r2_object(rel_path):
                    return
            self._send_json(404, {"error": "Object not found", "path": path}, "private, no-store")
            return

        if self.backend is None:
            self._send_json(404, {"error": "Not found", "path": path}, "private, no-store")
            return

        headers = self._headers_dict()
        try:
            if path == "/api/auth/me" and self.command == "GET":
                user = self.backend.require_auth(headers.get("Authorization"))
                self._send_json(200, {"user": dict(user)}, "private, no-store")
                return

            if path == "/api/observations" and self.command == "POST":
                raw = self._read_body_bytes()
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception as exc:
                    raise HttpError(400, "Invalid JSON request body", "invalid_json") from exc
                status, payload = self.backend.create_observation(body, headers)
                self._send_json(status, payload, "private, no-store")
                return

            obs_match = re.match(r"^/api/observations/([^/]+)$", path)
            if obs_match:
                occ_id = unquote(obs_match.group(1))
                if self.command == "GET":
                    status, payload = self.backend.get_observation(occ_id, headers)
                    self._send_json(status, payload, "private, no-store")
                    return
                if self.command == "PATCH":
                    raw = self._read_body_bytes()
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except Exception as exc:
                        raise HttpError(400, "Invalid JSON request body", "invalid_json") from exc
                    status, payload = self.backend.update_observation(occ_id, body, headers)
                    self._send_json(status, payload, "private, no-store")
                    return

            upload_match = re.match(r"^/api/uploads/([^/]+)$", path)
            if upload_match and self.command == "PUT":
                occ_id = unquote(upload_match.group(1))
                # Reject missing/oversized Content-Length before reading any stream bytes.
                cl_hdr = self.headers.get("Content-Length")
                if cl_hdr is None or not cl_hdr.strip().isdigit():
                    raise HttpError(411, "Content-Length header is required", "length_required")
                declared_len = int(cl_hdr)
                if declared_len <= 0:
                    raise HttpError(400, "Empty upload payload", "empty_upload")
                if declared_len > MAX_UPLOAD_BYTES:
                    raise HttpError(413, f"Upload exceeds maximum {MAX_UPLOAD_BYTES} bytes (15 MiB)", "payload_too_large")
                raw = self._read_body_bytes()
                status, payload = self.backend.upload_media(occ_id, raw, headers)
                self._send_json(status, payload, "private, no-store")
                return

            review_match = re.match(r"^/api/review/([^/]+)/(approve|needs_changes)$", path)
            if review_match and self.command == "POST":
                occ_id = unquote(review_match.group(1))
                decision = "approved" if review_match.group(2) == "approve" else "needs_changes"
                raw = self._read_body_bytes()
                body = json.loads(raw.decode("utf-8")) if raw else {}
                status, payload = self.backend.review_observation(occ_id, decision, body, headers)
                self._send_json(status, payload, "private, no-store")
                return

            if path == "/api/exports/dwca" and self.command == "POST":
                reviewer = self.backend.require_auth(headers.get("Authorization"))
                if reviewer["role"] not in ("reviewer", "admin"):
                    raise HttpError(403, "Forbidden: reviewer role required", "forbidden")
                raw = self._read_body_bytes()
                body = json.loads(raw.decode("utf-8")) if raw else {}
                lic = body.get("license", "CC-BY 4.0")
                res = export_dwca(self.backend.conn, self.r2_dir, dataset_license=lic)
                self._send_json(200, res, "private, no-store")
                return

            self._send_json(404, {"error": "Not found", "path": path}, "private, no-store")
        except HttpError as err:
            self._send_json(err.status, {"error": err.message, "code": err.code}, "private, no-store")

    def _serve_r2_object(self, rel_path: str) -> bool:
        target = (self.r2_dir / rel_path.lstrip("/")).resolve()
        try:
            target.relative_to(self.r2_dir)
        except ValueError:
            return False
        if not target.is_file():
            return False

        data = target.read_bytes()
        total = len(data)
        etag = f'"{hashlib.md5(data).hexdigest()}"'
        rel = target.relative_to(self.r2_dir).as_posix()
        cache_ctrl = (
            "public, max-age=60, must-revalidate"
            if (rel == "releases/manifest.json" or rel.startswith("overlays/"))
            else "public, max-age=31536000, immutable"
        )
        ctype = _guess_mime(target)

        range_hdr = self.headers.get("Range")
        if range_hdr:
            m = RANGE_RE.match(range_hdr.strip())
            if not m or (m.group(1) == "" and m.group(2) == ""):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return True
            start_s, end_s = m.groups()
            if start_s == "":
                start = max(0, total - int(end_s))
                end = total - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else total - 1
            if start >= total or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return True
            end = min(end, total - 1)
            chunk = data[start : end + 1]
            self.send_response(206)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "ETag, Content-Length, Content-Range")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_ctrl)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(chunk)
            return True

        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "ETag, Content-Length, Content-Range")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(total))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache_ctrl)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        return True

    def _serve_static_asset(self, path: str) -> None:
        clean = path.lstrip("/") or "index.html"
        target = (self.dist_dir / clean).resolve()
        try:
            target.relative_to(self.dist_dir)
        except ValueError:
            target = self.dist_dir / "index.html"
        if not target.is_file():
            target = self.dist_dir / "index.html"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _guess_mime(target))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        req_path = self.path.split("?", 1)[0]
        if req_path.startswith("/api/"):
            self._handle_api(req_path)
            return
        if (
            req_path.startswith("/releases/")
            or req_path.startswith("/overlays/")
            or req_path.startswith("/media/")
            or req_path.startswith("/exports/")
        ):
            if self._serve_r2_object(req_path):
                return
            self._send_json(404, {"error": "Object not found", "path": req_path}, "private, no-store")
            return
        self._serve_static_asset(req_path)


class ReusableThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local basinmap.org dev server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--simulate-api-outage", action="store_true")
    args = parser.parse_args()

    backend = BasinmapBackend(
        db_path=LOCAL_DATA_DIR / "basinmap.sqlite3",
        public_r2_dir=PUBLIC_R2_DIR,
        private_r2_dir=LOCAL_DATA_DIR / "private-r2",
        enforce_turnstile=False,
    )
    handler = functools.partial(
        LocalBasinmapHandler,
        dist_dir=DIST_DIR,
        r2_dir=PUBLIC_R2_DIR,
        backend=backend,
        simulate_api_outage=args.simulate_api_outage,
    )
    server = ReusableThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving basinmap.org locally at http://{args.host}:{args.port} (outage={args.simulate_api_outage})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
