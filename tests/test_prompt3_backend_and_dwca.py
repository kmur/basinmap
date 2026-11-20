#!/usr/bin/env python3
"""Automated tests for Prompt 3: D1 schema, media intake, review workflow, overlay, and DwC-A."""

from __future__ import annotations

import functools
import http.client
import json
import socket
import struct
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import backend_engine  # noqa: E402
import dev_server  # noqa: E402
import export_dwca  # noqa: E402


def build_test_jpeg_with_exif(lat_deg: int, lat_min: int, lon_deg: int, lon_min: int) -> bytes:
    """Build a minimal valid binary JPEG containing a real APP1 TIFF GPS & Orientation EXIF block."""
    # Construct little-endian TIFF payload:
    # Header (8 bytes): II, 42, IFD0 offset=8
    ifd0_offset = 8
    # IFD0: 3 entries (Orientation, DateTime, GPSInfoIFDPointer) -> 2 + 3*12 + 4 = 42 bytes
    ifd0_size = 2 + 3 * 12 + 4
    dt_bytes = b"2026:11:18 14:22:00\x00"  # 20 bytes
    dt_offset = ifd0_offset + ifd0_size
    gps_ifd_offset = dt_offset + len(dt_bytes)
    # GPS IFD: 4 entries (LatRef, Lat, LonRef, Lon) -> 2 + 4*12 + 4 = 54 bytes
    gps_ifd_size = 2 + 4 * 12 + 4
    gps_data_offset = gps_ifd_offset + gps_ifd_size

    lat_rationals = struct.pack("<6I", lat_deg, 1, lat_min, 1, 0, 1)  # 24 bytes
    lon_rationals = struct.pack("<6I", lon_deg, 1, lon_min, 1, 0, 1)  # 24 bytes
    lon_data_offset = gps_data_offset + len(lat_rationals)

    ifd0 = bytearray()
    ifd0.extend(struct.pack("<H", 3))
    # 0x0112 Orientation (SHORT, count=1, value=6 inline)
    ifd0.extend(struct.pack("<HHIHH", 0x0112, 3, 1, 6, 0))
    # 0x0132 DateTime (ASCII, count=20, offset=dt_offset)
    ifd0.extend(struct.pack("<HHII", 0x0132, 2, len(dt_bytes), dt_offset))
    # 0x8825 GPSInfoIFDPointer (LONG, count=1, offset=gps_ifd_offset)
    ifd0.extend(struct.pack("<HHII", 0x8825, 4, 1, gps_ifd_offset))
    ifd0.extend(struct.pack("<I", 0))  # next IFD = 0

    gps_ifd = bytearray()
    gps_ifd.extend(struct.pack("<H", 4))
    # 0x0001 GPSLatitudeRef 'N\x00'
    gps_ifd.extend(struct.pack("<HHI4s", 0x0001, 2, 2, b"N\x00\x00\x00"))
    # 0x0002 GPSLatitude (RATIONAL, count=3)
    gps_ifd.extend(struct.pack("<HHII", 0x0002, 5, 3, gps_data_offset))
    # 0x0003 GPSLongitudeRef 'W\x00'
    gps_ifd.extend(struct.pack("<HHI4s", 0x0003, 2, 2, b"W\x00\x00\x00"))
    # 0x0004 GPSLongitude (RATIONAL, count=3)
    gps_ifd.extend(struct.pack("<HHII", 0x0004, 5, 3, lon_data_offset))
    gps_ifd.extend(struct.pack("<I", 0))

    tiff = (
        b"II"
        + struct.pack("<HI", 42, ifd0_offset)
        + bytes(ifd0)
        + dt_bytes
        + bytes(gps_ifd)
        + lat_rationals
        + lon_rationals
    )
    app1_payload = b"Exif\x00\x00" + tiff
    app1_seg = b"\xFF\xE1" + struct.pack(">H", len(app1_payload) + 2) + app1_payload

    app0_payload = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    app0_seg = b"\xFF\xE0" + struct.pack(">H", len(app0_payload) + 2) + app0_payload

    sof0_payload = b"\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    sof0_seg = b"\xFF\xC0" + struct.pack(">H", len(sof0_payload) + 2) + sof0_payload

    sos_seg = b"\xFF\xDA\x00\x08\x01\x01\x00\x00\x3F\x00\xD2\xCF\x20"
    return b"\xFF\xD8" + app0_seg + app1_seg + sof0_seg + sos_seg + b"\xFF\xD9"


def http_call(
    backend: backend_engine.BasinmapBackend,
    public_r2_dir: Path,
    method: str,
    path: str,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    include_content_length: bool = True,
) -> Tuple[int, Dict[str, str], bytes]:
    handler_cls = functools.partial(
        dev_server.LocalBasinmapHandler,
        dist_dir=ROOT_DIR / "dist",
        r2_dir=public_r2_dir,
        backend=backend,
        simulate_api_outage=False,
    )
    client_sock, server_sock = socket.socketpair()
    err_holder: List[BaseException] = []

    def serve() -> None:
        try:
            handler_cls(server_sock, ("127.0.0.1", 0), None)
        except BaseException as exc:  # pragma: no cover
            err_holder.append(exc)
        finally:
            try:
                server_sock.close()
            except OSError:
                pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        req_headers = dict(headers or {})
        payload = body or b""
        if body is not None and include_content_length and "Content-Length" not in req_headers:
            req_headers["Content-Length"] = str(len(payload))
        lines = [f"{method} {path} HTTP/1.0", "Host: basinmap.org"]
        for k, v in req_headers.items():
            lines.append(f"{k}: {v}")
        raw_req = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload
        client_sock.sendall(raw_req)

        resp = http.client.HTTPResponse(client_sock, method=method)
        resp.begin()
        resp_hdrs = {k.lower(): v for k, v in resp.getheaders()}
        resp_body = resp.read()
        status = resp.status
    finally:
        client_sock.close()
        t.join(timeout=5)
    if err_holder:
        raise err_holder[0]
    return status, resp_hdrs, resp_body


class TestPrompt3BackendAndDwCA(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.db_path = self.tmp_path / "test.sqlite3"
        self.public_r2 = self.tmp_path / "public-r2"
        self.private_r2 = self.tmp_path / "private-r2"
        (self.public_r2 / "overlays").mkdir(parents=True, exist_ok=True)
        (self.public_r2 / "overlays" / "approved.geojson").write_text(
            '{"type": "FeatureCollection", "features": []}\n',
            encoding="utf-8",
        )
        self.backend = backend_engine.BasinmapBackend(
            db_path=self.db_path,
            public_r2_dir=self.public_r2,
            private_r2_dir=self.private_r2,
            enforce_turnstile=True,
        )
        self.contrib_headers = {
            "Authorization": "Bearer dev-contributor-orcid",
            "X-Turnstile-Token": "turnstile-ok-token",
            "Content-Type": "application/json",
        }
        self.reviewer_headers = {
            "Authorization": "Bearer dev-reviewer-orcid",
            "X-Turnstile-Token": "turnstile-ok-token",
            "Content-Type": "application/json",
        }

    def tearDown(self) -> None:
        self.backend.close()
        self.tmp_dir.cleanup()

    def test_d1_schema_idempotency_license_and_sensitive_generalization(self) -> None:
        # Reject unsupported license (e.g. CC-BY-NC 4.0)
        bad_payload = json.dumps(
            {
                "scientificName": "Oncorhynchus tshawytscha",
                "eventDate": "2026-10-05",
                "decimalLatitude": 44.51234,
                "decimalLongitude": -123.26123,
                "license": "CC-BY-NC 4.0",
            }
        ).encode("utf-8")
        st, _, _ = http_call(
            self.backend,
            self.public_r2,
            "POST",
            "/api/observations",
            headers={**self.contrib_headers, "Idempotency-Key": "idem-bad-lic"},
            body=bad_payload,
        )
        self.assertEqual(st, 400)

        # Reject failed Turnstile token
        st_ts, _, _ = http_call(
            self.backend,
            self.public_r2,
            "POST",
            "/api/observations",
            headers={
                "Authorization": "Bearer dev-contributor-orcid",
                "X-Turnstile-Token": "turnstile-fail",
                "Idempotency-Key": "idem-ts-fail",
            },
            body=bad_payload,
        )
        self.assertEqual(st_ts, 403)

        # Submit sensitive species (Salvelinus confluentus, VU)
        sens_payload = json.dumps(
            {
                "scientificName": "Salvelinus confluentus",
                "taxonKey": 2351283,
                "family": "Salmonidae",
                "iucnCategory": "VU",
                "eventDate": "2026-09-14",
                "decimalLatitude": 44.14892,
                "decimalLongitude": -122.17431,
                "coordinateUncertaintyInMeters": 20,
                "hybas_id": "7060041250",
                "locality": "Upper McKenzie tributary",
                "license": "CC-BY 4.0",
                "licenseAccepted": True,
            }
        ).encode("utf-8")

        st1, _, body1 = http_call(
            self.backend,
            self.public_r2,
            "POST",
            "/api/observations",
            headers={**self.contrib_headers, "Idempotency-Key": "idem-bull-trout-1"},
            body=sens_payload,
        )
        self.assertEqual(st1, 201)
        res1 = json.loads(body1.decode("utf-8"))
        self.assertFalse(res1["idempotent_replay"])
        obs = res1["observation"]
        occ_id = obs["occurrence_id"]

        # Exact coordinates in verbatim, 0.1-deg generalized coordinates in public
        self.assertTrue(obs["is_sensitive_species"])
        self.assertAlmostEqual(obs["verbatim"]["latitude"], 44.14892)
        self.assertAlmostEqual(obs["verbatim"]["longitude"], -122.17431)
        self.assertAlmostEqual(obs["public_latitude"], 44.1)
        self.assertAlmostEqual(obs["public_longitude"], -122.2)
        self.assertGreaterEqual(obs["public_coordinate_uncertainty_m"], 10000)
        self.assertIsNotNone(obs["information_withheld"])

        # Replay exact Idempotency-Key -> same occurrence_id, no duplicate row
        st2, _, body2 = http_call(
            self.backend,
            self.public_r2,
            "POST",
            "/api/observations",
            headers={**self.contrib_headers, "Idempotency-Key": "idem-bull-trout-1"},
            body=sens_payload,
        )
        self.assertEqual(st2, 200)
        res2 = json.loads(body2.decode("utf-8"))
        self.assertTrue(res2["idempotent_replay"])
        self.assertEqual(res2["observation"]["occurrence_id"], occ_id)

        # Unauthenticated caller cannot inspect pending submission
        st_unauth, _, _ = http_call(
            self.backend,
            self.public_r2,
            "GET",
            f"/api/observations/{quote(occ_id)}",
        )
        self.assertEqual(st_unauth, 403)

    def test_streaming_upload_magic_bytes_content_length_and_exif_stripping(self) -> None:
        obs_payload = json.dumps(
            {
                "scientificName": "Oncorhynchus mykiss",
                "taxonKey": 5204019,
                "family": "Salmonidae",
                "eventDate": "2026-11-01",
                "decimalLatitude": 44.502,
                "decimalLongitude": -123.251,
                "hybas_id": "7060041240",
                "license": "CC-BY 4.0",
                "licenseAccepted": True,
            }
        ).encode("utf-8")
        _, _, obs_body = http_call(
            self.backend,
            self.public_r2,
            "POST",
            "/api/observations",
            headers={**self.contrib_headers, "Idempotency-Key": "idem-media-1"},
            body=obs_payload,
        )
        occ_id = json.loads(obs_body.decode("utf-8"))["observation"]["occurrence_id"]

        # 1. Missing Content-Length must fail with 411
        st_nolen, _, _ = http_call(
            self.backend,
            self.public_r2,
            "PUT",
            f"/api/uploads/{quote(occ_id)}",
            headers={"Authorization": "Bearer dev-contributor-orcid"},
            body=b"1234",
            include_content_length=False,
        )
        self.assertEqual(st_nolen, 411)

        # 2. Fake bytes claiming Content-Type: image/jpeg must fail with 415
        st_fake, _, _ = http_call(
            self.backend,
            self.public_r2,
            "PUT",
            f"/api/uploads/{quote(occ_id)}",
            headers={
                "Authorization": "Bearer dev-contributor-orcid",
                "Content-Type": "image/jpeg",
            },
            body=b"<html>not actually a jpeg file at all</html>",
        )
        self.assertEqual(st_fake, 415)

        # 3. Declared Content-Length > 15 MiB rejected with 413
        st_big, _, _ = http_call(
            self.backend,
            self.public_r2,
            "PUT",
            f"/api/uploads/{quote(occ_id)}",
            headers={
                "Authorization": "Bearer dev-contributor-orcid",
                "Content-Length": str(16 * 1024 * 1024),
            },
            body=b"",
            include_content_length=False,
        )
        self.assertEqual(st_big, 413)

        # 4. Valid JPEG with embedded APP1 GPS EXIF (44.5 N, 123.25 W)
        jpeg_bytes = build_test_jpeg_with_exif(44, 30, 123, 15)
        self.assertIn(b"\xFF\xE1", jpeg_bytes)

        st_ok, _, up_body = http_call(
            self.backend,
            self.public_r2,
            "PUT",
            f"/api/uploads/{quote(occ_id)}",
            headers={
                "Authorization": "Bearer dev-contributor-orcid",
                "Content-Type": "application/octet-stream",
            },
            body=jpeg_bytes,
        )
        self.assertEqual(st_ok, 201)
        up_res = json.loads(up_body.decode("utf-8"))
        self.assertEqual(up_res["detected_mime_type"], "image/jpeg")
        self.assertEqual(up_res["derivative_status"], "private_only")
        self.assertAlmostEqual(up_res["exif_extracted"]["gps_latitude"], 44.5)
        self.assertAlmostEqual(up_res["exif_extracted"]["gps_longitude"], -123.25)
        self.assertEqual(up_res["exif_extracted"]["orientation"], 6)

        # Approve as reviewer -> public derivative created under /media/<media_id>.jpg with EXIF stripped
        st_rev, _, rev_body = http_call(
            self.backend,
            self.public_r2,
            "POST",
            f"/api/review/{quote(occ_id)}/approve",
            headers=self.reviewer_headers,
            body=b'{"notes": "Verified photo and ID"}',
        )
        self.assertEqual(st_rev, 200)
        rev_res = json.loads(rev_body.decode("utf-8"))
        self.assertEqual(len(rev_res["sanitized_media_keys"]), 1)
        pub_key = rev_res["sanitized_media_keys"][0]
        self.assertTrue(pub_key.startswith("media/"))
        pub_bytes = (self.public_r2 / pub_key).read_bytes()
        self.assertEqual(pub_bytes[:3], b"\xFF\xD8\xFF")
        self.assertNotIn(b"\xFF\xE1", pub_bytes)
        exif_after = backend_engine.extract_exif_metadata(pub_bytes, "image/jpeg")
        self.assertIsNone(exif_after["gps_latitude"])
        self.assertIsNone(exif_after["gps_longitude"])

    def test_reviewer_permissions_correction_and_deterministic_overlay(self) -> None:
        # Submit two observations
        ids: List[str] = []
        for idx, sp in enumerate(("Oncorhynchus tshawytscha", "Salvelinus confluentus"), start=1):
            payload = json.dumps(
                {
                    "scientificName": sp,
                    "taxonKey": 5204029 if idx == 1 else 2351283,
                    "family": "Salmonidae",
                    "eventDate": f"2026-10-0{idx}",
                    "decimalLatitude": 44.4567 + idx * 0.1,
                    "decimalLongitude": -123.1234,
                    "hybas_id": "7060041240",
                    "license": "CC-BY 4.0",
                    "licenseAccepted": True,
                }
            ).encode("utf-8")
            _, _, b = http_call(
                self.backend,
                self.public_r2,
                "POST",
                "/api/observations",
                headers={**self.contrib_headers, "Idempotency-Key": f"idem-rev-{idx}"},
                body=payload,
            )
            ids.append(json.loads(b.decode("utf-8"))["observation"]["occurrence_id"])

        # Contributor cannot approve
        st_forbid, _, _ = http_call(
            self.backend,
            self.public_r2,
            "POST",
            f"/api/review/{quote(ids[0])}/approve",
            headers=self.contrib_headers,
            body=b"{}",
        )
        self.assertEqual(st_forbid, 403)

        # Reviewer approves both
        for oid in ids:
            st, _, _ = http_call(
                self.backend,
                self.public_r2,
                "POST",
                f"/api/review/{quote(oid)}/approve",
                headers=self.reviewer_headers,
                body=b'{"notes": "Approved"}',
            )
            self.assertEqual(st, 200)

        overlay = json.loads((self.public_r2 / "overlays" / "approved.geojson").read_text(encoding="utf-8"))
        self.assertEqual(len(overlay["features"]), 2)

        # Apply correction to first record -> occurrence_id stays stable, revision_number increments to 3
        st_patch, _, patch_body = http_call(
            self.backend,
            self.public_r2,
            "PATCH",
            f"/api/observations/{quote(ids[0])}",
            headers=self.reviewer_headers,
            body=json.dumps({"locality": "Corrected Santiam Confluence", "notes": "Refined locality"}).encode("utf-8"),
        )
        self.assertEqual(st_patch, 200)
        patched = json.loads(patch_body.decode("utf-8"))["observation"]
        self.assertEqual(patched["occurrence_id"], ids[0])
        self.assertEqual(patched["revision_number"], 3)

        # Public GET on approved record hides verbatim coords
        st_pub, _, pub_body = http_call(
            self.backend,
            self.public_r2,
            "GET",
            f"/api/observations/{quote(ids[1])}",
        )
        self.assertEqual(st_pub, 200)
        pub_obs = json.loads(pub_body.decode("utf-8"))["observation"]
        self.assertNotIn("verbatim", pub_obs)
        self.assertAlmostEqual(pub_obs["public_longitude"], -123.1)

        # Mark second observation needs_changes -> overlay deterministically drops to 1 feature
        st_nc, _, nc_body = http_call(
            self.backend,
            self.public_r2,
            "POST",
            f"/api/review/{quote(ids[1])}/needs_changes",
            headers=self.reviewer_headers,
            body=b'{"notes": "Missing dorsal fin photo"}',
        )
        self.assertEqual(st_nc, 200)
        self.assertEqual(json.loads(nc_body.decode("utf-8"))["overlay_record_count"], 1)

    def test_dwca_export_validation_and_gbif_uat_payloads(self) -> None:
        # Submit, attach EXIF JPEG, and approve one sensitive observation
        payload = json.dumps(
            {
                "scientificName": "Salvelinus confluentus",
                "taxonKey": 2351283,
                "family": "Salmonidae",
                "iucnCategory": "VU",
                "eventDate": "2026-10-12",
                "decimalLatitude": 44.3377,
                "decimalLongitude": -122.4488,
                "locality": "McKenzie headwaters",
                "license": "CC-BY 4.0",
                "licenseAccepted": True,
            }
        ).encode("utf-8")
        _, _, b = http_call(
            self.backend,
            self.public_r2,
            "POST",
            "/api/observations",
            headers={**self.contrib_headers, "Idempotency-Key": "idem-dwca-1"},
            body=payload,
        )
        occ_id = json.loads(b.decode("utf-8"))["observation"]["occurrence_id"]

        jpeg_bytes = build_test_jpeg_with_exif(44, 20, 122, 26)
        http_call(
            self.backend,
            self.public_r2,
            "PUT",
            f"/api/uploads/{quote(occ_id)}",
            headers={"Authorization": "Bearer dev-contributor-orcid", "Content-Type": "image/jpeg"},
            body=jpeg_bytes,
        )
        http_call(
            self.backend,
            self.public_r2,
            "POST",
            f"/api/review/{quote(occ_id)}/approve",
            headers=self.reviewer_headers,
            body=b'{"notes": "Ready for GBIF"}',
        )

        # Export via API endpoint
        st_exp, _, exp_body = http_call(
            self.backend,
            self.public_r2,
            "POST",
            "/api/exports/dwca",
            headers=self.reviewer_headers,
            body=b'{"license": "CC-BY 4.0"}',
        )
        self.assertEqual(st_exp, 200)
        exp_res = json.loads(exp_body.decode("utf-8"))
        self.assertEqual(exp_res["record_count"], 1)
        self.assertEqual(exp_res["media_count"], 1)
        self.assertTrue(exp_res["validation"]["valid"])

        zip_path = self.public_r2 / "exports" / "dwca-latest.zip"
        with zipfile.ZipFile(zip_path, "r") as zf:
            occ_txt = zf.read("occurrence.txt").decode("utf-8")
            mm_txt = zf.read("multimedia.txt").decode("utf-8")
            self.assertIn(occ_id, occ_txt)
            # Sensitive coords must remain generalized (44.3, -122.4) in occurrence.txt
            self.assertIn("44.3\t-122.4", occ_txt)
            self.assertNotIn("44.3377", occ_txt)
            self.assertIn("https://data.basinmap.org/media/", mm_txt)
            self.assertIn("http://creativecommons.org/licenses/by/4.0/legalcode", mm_txt)

        # Verify dry-run GBIF Data Validator & gbif-uat.org registration payloads
        val_req = export_dwca.submit_to_gbif_validator(zip_path, dry_run=True)
        self.assertEqual(val_req["endpoint"], "https://api.gbif.org/v1/validation")
        self.assertTrue(val_req["local_validation"]["valid"])

        uat_req = export_dwca.register_on_gbif_uat(
            publishing_org_key="11111111-1111-1111-1111-111111111111",
            installation_key="22222222-2222-2222-2222-222222222222",
            dry_run=True,
        )
        self.assertEqual(uat_req["dataset_endpoint"], "https://api.gbif-uat.org/v1/dataset")
        self.assertEqual(uat_req["endpoint_payload"]["type"], "DWC_ARCHIVE")
        self.assertEqual(uat_req["endpoint_payload"]["url"], "https://data.basinmap.org/exports/dwca-latest.zip")


if __name__ == "__main__":
    unittest.main()
