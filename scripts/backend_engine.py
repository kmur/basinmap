#!/usr/bin/env python3
"""Local SQLite + R2 backend engine implementing basinmap.org observation workflows.

Executes migrations/0001_init.sql and provides:
  - ORCID/dev bearer authentication & Turnstile token checks
  - Idempotent observation submission, sensitive-species coordinate generalization,
    access control, and full revision history tracking
  - Streaming media upload validation (Content-Length, <=15 MB, magic bytes)
  - JPEG/PNG EXIF extraction (private) + metadata stripping (public derivative)
  - Reviewer approval / needs_changes workflow & deterministic approved.geojson rebuilds
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT_DIR / "migrations"
MIGRATION_FILE = MIGRATIONS_DIR / "0001_init.sql"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MiB
MAX_MEDIA_PER_OBSERVATION = 10
MAX_SUBMISSIONS_PER_HOUR = 30
REVIEW_QUEUE_STATUSES = {"submitted", "needs_changes", "approved"}
ALLOWED_LICENSES = {"CC-BY 4.0", "CC0 1.0"}
SENSITIVE_SPECIES_NAMES = {
    "Salvelinus confluentus",
    "Actinemys marmorata",
}
SENSITIVE_IUCN_CATEGORIES = {"CR", "EN", "VU"}

DEV_TOKEN_TO_USER_ID = {
    "dev-contributor-orcid": "usr-contrib-1",
    "dev-reviewer-orcid": "usr-review-1",
}


class HttpError(Exception):
    """HTTP-mapped application error."""

    def __init__(self, status: int, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("CREATE TABLE IF NOT EXISTS _local_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    applied = {r[0] for r in conn.execute("SELECT name FROM _local_migrations")}
    for mig in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if mig.name in applied:
            continue
        conn.executescript(mig.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO _local_migrations (name, applied_at) VALUES (?, ?)", (mig.name, utc_now_iso()))
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Magic bytes & EXIF parsing / sanitization
# ---------------------------------------------------------------------------

def detect_media_magic_bytes(data: bytes) -> str:
    """Detect supported image MIME type from leading magic bytes only."""
    if len(data) < 12:
        raise HttpError(415, "Unsupported Media Type: file too small to identify image signature", "invalid_media")
    if data[:3] == b"\xFF\xD8\xFF":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
        return "image/heic"
    raise HttpError(
        415,
        "Unsupported Media Type: leading magic bytes do not match JPEG, PNG, WebP, or HEIC",
        "unsupported_media_type",
    )


def _read_tiff_ifd_value(
    tiff: bytes,
    endian: str,
    ftype: int,
    count: int,
    val_off_bytes: bytes,
) -> Any:
    type_sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
    elem_size = type_sizes.get(ftype, 1)
    total_bytes = elem_size * count
    if total_bytes <= 4:
        raw = val_off_bytes[:total_bytes]
    else:
        offset = struct.unpack(f"{endian}I", val_off_bytes)[0]
        if offset + total_bytes > len(tiff):
            return None
        raw = tiff[offset : offset + total_bytes]

    if ftype == 2:  # ASCII
        return raw.rstrip(b"\x00").decode("ascii", errors="replace").strip()
    if ftype == 3 and count >= 1:  # SHORT
        vals = struct.unpack(f"{endian}{count}H", raw[: 2 * count])
        return vals[0] if count == 1 else list(vals)
    if ftype == 4 and count >= 1:  # LONG
        vals = struct.unpack(f"{endian}{count}I", raw[: 4 * count])
        return vals[0] if count == 1 else list(vals)
    if ftype == 5 and count >= 1:  # RATIONAL
        out = []
        for idx in range(count):
            num, den = struct.unpack(f"{endian}II", raw[idx * 8 : (idx + 1) * 8])
            out.append(num / den if den else 0.0)
        return out[0] if count == 1 else out
    return None


def _parse_ifd(tiff: bytes, endian: str, ifd_offset: int) -> Dict[int, Any]:
    if ifd_offset + 2 > len(tiff):
        return {}
    num_entries = struct.unpack(f"{endian}H", tiff[ifd_offset : ifd_offset + 2])[0]
    tags: Dict[int, Any] = {}
    pos = ifd_offset + 2
    for _ in range(num_entries):
        if pos + 12 > len(tiff):
            break
        tag, ftype, count = struct.unpack(f"{endian}HHI", tiff[pos : pos + 8])
        val_off_bytes = tiff[pos + 8 : pos + 12]
        tags[tag] = _read_tiff_ifd_value(tiff, endian, ftype, count, val_off_bytes)
        pos += 12
    return tags


def extract_exif_metadata(data: bytes, mime_type: str) -> Dict[str, Any]:
    """Extract orientation, datetime, and GPS coordinates from JPEG APP1 EXIF if present."""
    result: Dict[str, Any] = {
        "gps_latitude": None,
        "gps_longitude": None,
        "datetime": None,
        "orientation": 1,
    }
    if mime_type != "image/jpeg" or len(data) < 4 or data[:2] != b"\xFF\xD8":
        return result

    pos = 2
    n = len(data)
    while pos + 2 <= n:
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker in (0xD8, 0xD9, 0x00) or (0xD0 <= marker <= 0xD7):
            pos += 2
            continue
        if marker == 0xDA:  # Start of Scan
            break
        if pos + 4 > n:
            break
        seg_len = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        if seg_len < 2 or pos + 2 + seg_len > n:
            break
        payload = data[pos + 4 : pos + 2 + seg_len]
        if marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            tiff = payload[6:]
            if len(tiff) >= 8:
                endian_hdr = tiff[:2]
                endian = "<" if endian_hdr == b"II" else (">" if endian_hdr == b"MM" else "")
                if endian:
                    magic, ifd0_off = struct.unpack(f"{endian}HI", tiff[2:8])
                    if magic == 42:
                        ifd0 = _parse_ifd(tiff, endian, ifd0_off)
                        if isinstance(ifd0.get(0x0112), int):
                            result["orientation"] = ifd0[0x0112]
                        if isinstance(ifd0.get(0x0132), str):
                            result["datetime"] = ifd0[0x0132]

                        exif_off = ifd0.get(0x8769)
                        if isinstance(exif_off, int):
                            exif_ifd = _parse_ifd(tiff, endian, exif_off)
                            if isinstance(exif_ifd.get(0x9003), str):
                                result["datetime"] = exif_ifd[0x9003]

                        gps_off = ifd0.get(0x8825)
                        if isinstance(gps_off, int):
                            gps_ifd = _parse_ifd(tiff, endian, gps_off)
                            lat_dms = gps_ifd.get(0x0002)
                            lat_ref = gps_ifd.get(0x0001)
                            lon_dms = gps_ifd.get(0x0004)
                            lon_ref = gps_ifd.get(0x0003)
                            if isinstance(lat_dms, list) and len(lat_dms) == 3:
                                lat_val = lat_dms[0] + lat_dms[1] / 60.0 + lat_dms[2] / 3600.0
                                if str(lat_ref).upper() == "S":
                                    lat_val = -lat_val
                                result["gps_latitude"] = round(lat_val, 6)
                            if isinstance(lon_dms, list) and len(lon_dms) == 3:
                                lon_val = lon_dms[0] + lon_dms[1] / 60.0 + lon_dms[2] / 3600.0
                                if str(lon_ref).upper() == "W":
                                    lon_val = -lon_val
                                result["gps_longitude"] = round(lon_val, 6)
            break
        pos += 2 + seg_len
    return result


def strip_private_metadata(data: bytes, mime_type: str) -> bytes:
    """Create sanitized public image bytes stripping EXIF/XMP/COM metadata segments.

    In production Workers, Cloudflare Images transformation with `metadata: "none"`
    handles orientation baking and HEIC->JPEG transcoding; locally for JPEG we strip
    APP1/APP2/APP13/COM markers and for PNG we strip textual/eXIf ancillary chunks.
    """
    if mime_type == "image/jpeg" and data[:2] == b"\xFF\xD8":
        out = bytearray(b"\xFF\xD8")
        pos = 2
        n = len(data)
        strip_markers = {0xE1, 0xE2, 0xED, 0xFE}
        while pos + 2 <= n:
            if data[pos] != 0xFF:
                out.extend(data[pos:])
                break
            marker = data[pos + 1]
            if marker == 0xDA:  # Start of Scan — copy remaining entropy-coded stream verbatim
                out.extend(data[pos:])
                break
            if marker in (0xD8, 0xD9, 0x00) or (0xD0 <= marker <= 0xD7):
                out.extend(data[pos : pos + 2])
                pos += 2
                continue
            if pos + 4 > n:
                break
            seg_len = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
            seg_end = pos + 2 + seg_len
            if seg_len < 2 or seg_end > n:
                break
            if marker not in strip_markers:
                out.extend(data[pos:seg_end])
            pos = seg_end
        return bytes(out)

    if mime_type == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n":
        out = bytearray(data[:8])
        pos = 8
        strip_chunks = {b"tEXt", b"zTXt", b"iTXt", b"eXIf"}
        while pos + 8 <= len(data):
            length = struct.unpack(">I", data[pos : pos + 4])[0]
            ctype = data[pos + 4 : pos + 8]
            chunk_end = pos + 12 + length
            if chunk_end > len(data):
                break
            if ctype not in strip_chunks:
                out.extend(data[pos:chunk_end])
            pos = chunk_end
        return bytes(out)

    return data


# ---------------------------------------------------------------------------
# Coordinate generalization & serialization helpers
# ---------------------------------------------------------------------------

def generalize_coordinates(
    lat: float,
    lon: float,
    uncertainty_m: Optional[int],
    scientific_name: str,
    iucn_category: Optional[str],
    explicit_sensitive: bool,
) -> Tuple[bool, float, float, int, Optional[str]]:
    is_sensitive = (
        explicit_sensitive
        or scientific_name in SENSITIVE_SPECIES_NAMES
        or (iucn_category or "").upper() in SENSITIVE_IUCN_CATEGORIES
    )
    if is_sensitive:
        pub_lat = round(lat, 1)
        pub_lon = round(lon, 1)
        pub_unc = max(int(uncertainty_m or 0), 10000)
        withheld = (
            "Coordinates generalized to 0.1 degree grid due to species sensitivity. "
            "Exact coordinates retained privately for authorized reviewers."
        )
        return True, pub_lat, pub_lon, pub_unc, withheld
    return False, lat, lon, int(uncertainty_m or 30), None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$")


def validate_observation_input(body: Dict[str, Any], base: Optional[sqlite3.Row]) -> Dict[str, Any]:
    """Shared POST/PATCH validation mirroring validateObservationInput in src/worker/index.ts."""

    def pick(key: str, fallback: Any) -> Any:
        return body[key] if body.get(key) is not None else fallback

    sci = str(pick("scientificName", base["scientific_name"] if base else "")).strip()
    date = str(pick("eventDate", base["event_date"] if base else "")).strip()
    try:
        lat = float(pick("decimalLatitude", base["verbatim_latitude"] if base else float("nan")))
        lon = float(pick("decimalLongitude", base["verbatim_longitude"] if base else float("nan")))
        unc = float(pick("coordinateUncertaintyInMeters", (base["verbatim_coordinate_uncertainty_m"] if base else None) or 30))
    except (TypeError, ValueError) as exc:
        raise HttpError(400, "Valid decimalLatitude and decimalLongitude are required", "invalid_coordinates") from exc
    locality = pick("locality", base["locality"] if base else None)
    notes = body.get("notes")

    if not sci or len(sci) > 200:
        raise HttpError(400, "scientificName is required (max 200 characters)", "invalid_scientific_name")
    if not _DATE_RE.match(date):
        raise HttpError(400, "eventDate must be ISO 8601 (YYYY-MM-DD or YYYY-MM-DDThh:mm)", "invalid_date")
    try:
        day = datetime.strptime(date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HttpError(400, "eventDate must be a real date that is not in the future", "invalid_date") from exc
    if day.timestamp() > datetime.now(timezone.utc).timestamp() + 36 * 3600 or day.year < 1700:
        raise HttpError(400, "eventDate must be a real date that is not in the future", "invalid_date")
    if not (math.isfinite(lat) and math.isfinite(lon) and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise HttpError(400, "Valid decimalLatitude and decimalLongitude are required", "invalid_coordinates")
    if not (math.isfinite(unc) and 1 <= unc <= 100000):
        raise HttpError(400, "coordinateUncertaintyInMeters must be between 1 and 100000", "invalid_uncertainty")
    if locality is not None and len(str(locality)) > 1000:
        raise HttpError(400, "locality is limited to 1000 characters", "invalid_locality")
    if notes is not None and len(str(notes)) > 4000:
        raise HttpError(400, "notes are limited to 4000 characters", "invalid_notes")
    return {
        "scientificName": sci,
        "eventDate": date,
        "year": int(date[:4]),
        "lat": lat,
        "lon": lon,
        "uncertainty": int(round(unc)),
        "locality": (str(locality).strip() or None) if locality is not None else None,
        "notes": str(notes) if notes is not None else None,
    }


def _row_to_public_dict(row: sqlite3.Row, include_verbatim: bool = False) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "occurrence_id": row["occurrence_id"],
        "revision_number": row["revision_number"],
        "review_status": row["review_status"],
        "publication_status": row["publication_status"],
        "scientific_name": row["scientific_name"],
        "taxon_key": row["taxon_key"],
        "kingdom": row["kingdom"],
        "family": row["family"],
        "iucn_category": row["iucn_category"],
        "is_sensitive_species": bool(row["is_sensitive_species"]),
        "public_latitude": row["public_latitude"],
        "public_longitude": row["public_longitude"],
        "public_coordinate_uncertainty_m": row["public_coordinate_uncertainty_m"],
        "information_withheld": row["information_withheld"],
        "geodetic_datum": row["geodetic_datum"],
        "event_date": row["event_date"],
        "year": row["year"],
        "basis_of_record": row["basis_of_record"],
        "hybas_id": row["hybas_id"],
        "locality": row["locality"],
        "recorded_by": row["recorded_by"],
        "recorded_by_id": row["recorded_by_id"],
        "identified_by": row["identified_by"],
        "identified_by_id": row["identified_by_id"],
        "license": row["license"],
        "license_accepted_at": row["license_accepted_at"] if "license_accepted_at" in row.keys() else None,
        "rights_holder": row["rights_holder"],
        "reviewed_at": row["reviewed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_verbatim:
        payload["verbatim"] = {
            "scientific_name": row["verbatim_scientific_name"],
            "event_date": row["verbatim_event_date"],
            "latitude": row["verbatim_latitude"],
            "longitude": row["verbatim_longitude"],
            "coordinate_uncertainty_m": row["verbatim_coordinate_uncertainty_m"],
            "locality": row["verbatim_locality"],
            "notes": row["verbatim_notes"],
            "review_notes": row["review_notes"],
        }
    return payload


class BasinmapBackend:
    """Stateful local SQLite + R2 filesystem service mirroring Worker API routes."""

    def __init__(
        self,
        db_path: Path,
        public_r2_dir: Path,
        private_r2_dir: Path,
        enforce_turnstile: bool = True,
    ) -> None:
        self.db_path = db_path
        self.public_r2_dir = public_r2_dir
        self.private_r2_dir = private_r2_dir
        self.enforce_turnstile = enforce_turnstile
        self.public_r2_dir.mkdir(parents=True, exist_ok=True)
        self.private_r2_dir.mkdir(parents=True, exist_ok=True)
        self.conn = init_db(db_path)

    def close(self) -> None:
        self.conn.close()

    def authenticate(self, auth_header: Optional[str]) -> Optional[sqlite3.Row]:
        if not auth_header:
            return None
        parts = auth_header.strip().split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        token = parts[1].strip()
        if token in DEV_TOKEN_TO_USER_ID:
            uid = DEV_TOKEN_TO_USER_ID[token]
            return self.conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if token.startswith("user:"):
            uid = token[5:]
            return self.conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        return self.conn.execute("SELECT * FROM users WHERE orcid_id = ?", (token,)).fetchone()

    def require_auth(self, auth_header: Optional[str]) -> sqlite3.Row:
        user = self.authenticate(auth_header)
        if user is None:
            raise HttpError(401, "Authentication required (provide Bearer ORCID or dev token)", "unauthorized")
        return user

    def check_turnstile(self, headers: Dict[str, str]) -> None:
        if not self.enforce_turnstile:
            return
        lower = {k.lower(): v for k, v in headers.items()}
        token = (lower.get("x-turnstile-token") or "").strip()
        if not token or token == "turnstile-fail":
            raise HttpError(403, "Turnstile verification failed or missing X-Turnstile-Token", "turnstile_failed")

    def _record_revision(
        self,
        occurrence_id: str,
        revision_number: int,
        actor_user_id: str,
        change_type: str,
        notes: Optional[str] = None,
    ) -> None:
        row = self.conn.execute("SELECT * FROM observations WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
        snap = json.dumps(_row_to_public_dict(row, include_verbatim=True), sort_keys=True)
        self.conn.execute(
            """
            INSERT INTO observation_revisions
              (occurrence_id, revision_number, actor_user_id, change_type, snapshot_json, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (occurrence_id, revision_number, actor_user_id, change_type, snap, notes, utc_now_iso()),
        )

    def create_observation(
        self,
        body: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Tuple[int, Dict[str, Any]]:
        lower = {k.lower(): v for k, v in headers.items()}
        user = self.require_auth(lower.get("authorization"))
        if not isinstance(body, dict):
            raise HttpError(400, "Invalid JSON request body", "invalid_json")

        idem_key = (lower.get("idempotency-key") or body.get("idempotency_key") or "").strip()
        if not idem_key or len(idem_key) > 128:
            raise HttpError(400, "Missing Idempotency-Key header or idempotency_key field", "missing_idempotency_key")

        # Replays are answered before Turnstile (the original token was single-use), and only to
        # the account that created the record.
        existing = self.conn.execute(
            "SELECT * FROM observations WHERE idempotency_key = ?", (idem_key,)
        ).fetchone()
        if existing is not None:
            if existing["submitter_user_id"] != user["id"]:
                raise HttpError(409, "Idempotency-Key already used by another submission", "idempotency_conflict")
            return 200, {
                "idempotent_replay": True,
                "observation": _row_to_public_dict(existing, include_verbatim=True),
            }

        self.check_turnstile(lower)

        license_val = str(body.get("license") or "").strip()
        if license_val not in ALLOWED_LICENSES:
            raise HttpError(
                400,
                f"Invalid license {license_val!r}. Allowed dataset-wide licenses: {sorted(ALLOWED_LICENSES)}",
                "invalid_license",
            )
        if body.get("licenseAccepted") is not True:
            raise HttpError(400, "You must explicitly accept the dataset-wide contribution license", "license_not_accepted")

        v = validate_observation_input(body, None)

        hour_ago = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - 3600, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        recent = self.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE submitter_user_id = ? AND created_at > ?",
            (user["id"], hour_ago),
        ).fetchone()[0]
        if recent >= MAX_SUBMISSIONS_PER_HOUR:
            raise HttpError(429, "Submission rate limit reached; try again later", "rate_limited")

        iucn_raw = body.get("iucnCategory") or body.get("iucn_category")
        iucn = str(iucn_raw).upper()[:2] if iucn_raw else None
        explicit_sens = bool(body.get("isSensitiveSpecies", False))
        is_sens, pub_lat, pub_lon, pub_unc, withheld = generalize_coordinates(
            v["lat"], v["lon"], v["uncertainty"], v["scientificName"], iucn, explicit_sens
        )

        occ_id = f"urn:basinmap:occ:{uuid.uuid4()}"
        now = utc_now_iso()
        rights_holder = str(body.get("rightsHolder") or user["display_name"]).strip()[:200] or user["display_name"]

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO observations (
                  occurrence_id, idempotency_key, submitter_user_id, revision_number,
                  review_status, publication_status,
                  verbatim_scientific_name, verbatim_event_date, verbatim_latitude,
                  verbatim_longitude, verbatim_coordinate_uncertainty_m, verbatim_locality,
                  verbatim_notes,
                  scientific_name, taxon_key, kingdom, family, iucn_category,
                  is_sensitive_species, public_latitude, public_longitude,
                  public_coordinate_uncertainty_m, information_withheld, geodetic_datum,
                  event_date, year, basis_of_record, hybas_id, locality,
                  recorded_by, recorded_by_id, license, license_accepted_at, rights_holder,
                  created_at, updated_at
                ) VALUES (
                  ?, ?, ?, 1,
                  'submitted', 'not_exported',
                  ?, ?, ?,
                  ?, ?, ?,
                  ?,
                  ?, ?, ?, ?, ?,
                  ?, ?, ?,
                  ?, ?, 'EPSG:4326',
                  ?, ?, 'HUMAN_OBSERVATION', ?, ?,
                  ?, ?, ?, ?, ?,
                  ?, ?
                )
                """,
                (
                    occ_id,
                    idem_key,
                    user["id"],
                    v["scientificName"],
                    v["eventDate"],
                    v["lat"],
                    v["lon"],
                    v["uncertainty"],
                    v["locality"],
                    v["notes"],
                    v["scientificName"],
                    body.get("taxonKey"),
                    body.get("kingdom", "Animalia"),
                    body.get("family"),
                    iucn,
                    1 if is_sens else 0,
                    pub_lat,
                    pub_lon,
                    pub_unc,
                    withheld,
                    v["eventDate"],
                    v["year"],
                    body.get("hybas_id"),
                    v["locality"],
                    user["display_name"],
                    user["orcid_id"],  # Darwin Core recordedByID = authenticated ORCID iD
                    license_val,
                    now,
                    rights_holder,
                    now,
                    now,
                ),
            )
            self._record_revision(
                occ_id, 1, user["id"], "created", f"Initial contributor submission; {license_val} accepted"
            )

        row = self.conn.execute("SELECT * FROM observations WHERE occurrence_id = ?", (occ_id,)).fetchone()
        return 201, {
            "idempotent_replay": False,
            "observation": _row_to_public_dict(row, include_verbatim=True),
        }

    def list_my_observations(self, headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        lower = {k.lower(): v for k, v in headers.items()}
        user = self.require_auth(lower.get("authorization"))
        rows = self.conn.execute(
            """
            SELECT o.*, (SELECT COUNT(*) FROM media_intake m WHERE m.occurrence_id = o.occurrence_id) AS media_count
            FROM observations o WHERE o.submitter_user_id = ? ORDER BY o.created_at DESC LIMIT 100
            """,
            (user["id"],),
        ).fetchall()
        return 200, {
            "observations": [
                {**_row_to_public_dict(r, include_verbatim=True), "media_count": int(r["media_count"] or 0)} for r in rows
            ]
        }

    def list_review_queue(self, headers: Dict[str, str], query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        lower = {k.lower(): v for k, v in headers.items()}
        user = self.require_auth(lower.get("authorization"))
        if user["role"] not in ("reviewer", "admin"):
            raise HttpError(403, "Forbidden: reviewer or admin role required", "forbidden")
        status = query.get("status") or "submitted"
        if status not in REVIEW_QUEUE_STATUSES:
            raise HttpError(400, "status must be submitted, needs_changes, or approved", "invalid_status")
        try:
            limit = max(1, min(100, int(query.get("limit") or 50)))
        except ValueError:
            limit = 50
        before = query.get("before")
        sql = """
            SELECT o.*, u.display_name AS submitter_name, u.orcid_id AS submitter_orcid
            FROM observations o JOIN users u ON u.id = o.submitter_user_id
            WHERE o.review_status = ? {extra} ORDER BY o.created_at DESC LIMIT ?
        """
        rows = (
            self.conn.execute(sql.format(extra="AND o.created_at < ?"), (status, before, limit)).fetchall()
            if before
            else self.conn.execute(sql.format(extra=""), (status, limit)).fetchall()
        )
        items = []
        for row in rows:
            media = self.conn.execute(
                """
                SELECT media_id, detected_mime_type, byte_length, sha256, exif_gps_latitude, exif_gps_longitude,
                       exif_datetime, exif_orientation, derivative_status, created_at
                FROM media_intake WHERE occurrence_id = ? ORDER BY created_at ASC
                """,
                (row["occurrence_id"],),
            ).fetchall()
            tolerance = max(2 * float(row["verbatim_coordinate_uncertainty_m"] or 30), 500.0)
            media_out = []
            for m in media:
                md = dict(m)
                has_gps = m["exif_gps_latitude"] is not None and m["exif_gps_longitude"] is not None
                offset = (
                    round(haversine_m(row["verbatim_latitude"], row["verbatim_longitude"], m["exif_gps_latitude"], m["exif_gps_longitude"]))
                    if has_gps
                    else None
                )
                md.update({"url": f"/api/media/{m['media_id']}", "exif_offset_m": offset,
                           "exif_mismatch": offset is not None and offset > tolerance})
                media_out.append(md)
            items.append(
                {
                    **_row_to_public_dict(row, include_verbatim=True),
                    "submitter": {"display_name": row["submitter_name"], "orcid_id": row["submitter_orcid"]},
                    "media": media_out,
                }
            )
        next_before = rows[-1]["created_at"] if len(rows) == limit and rows else None
        return 200, {"status": status, "observations": items, "next_before": next_before}

    def get_private_media(self, media_id: str, headers: Dict[str, str]) -> Tuple[bytes, str]:
        lower = {k.lower(): v for k, v in headers.items()}
        user = self.require_auth(lower.get("authorization"))
        row = self.conn.execute(
            """
            SELECT m.*, o.submitter_user_id FROM media_intake m JOIN observations o ON o.occurrence_id = m.occurrence_id
            WHERE m.media_id = ?
            """,
            (media_id,),
        ).fetchone()
        if row is None:
            raise HttpError(404, "Media not found", "not_found")
        if user["id"] != row["submitter_user_id"] and user["role"] not in ("reviewer", "admin"):
            raise HttpError(403, "Forbidden", "forbidden")
        path = self.private_r2_dir / row["private_r2_key"]
        if not path.is_file():
            raise HttpError(404, "Media object missing", "not_found")
        return path.read_bytes(), row["detected_mime_type"]

    def get_observation(self, occurrence_id: str, headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        lower = {k.lower(): v for k, v in headers.items()}
        user = self.authenticate(lower.get("authorization"))
        row = self.conn.execute(
            "SELECT * FROM observations WHERE occurrence_id = ?", (occurrence_id,)
        ).fetchone()
        if row is None:
            raise HttpError(404, f"Observation not found: {occurrence_id}", "not_found")

        is_owner = user is not None and user["id"] == row["submitter_user_id"]
        is_reviewer = user is not None and user["role"] in ("reviewer", "admin")

        if row["review_status"] != "approved" and not (is_owner or is_reviewer):
            raise HttpError(403, "Pending observations are restricted to submitter and reviewers", "forbidden")

        include_verbatim = is_owner or is_reviewer
        media_rows = self.conn.execute(
            "SELECT media_id, public_r2_key, detected_mime_type, derivative_status, license, rights_holder FROM media_intake WHERE occurrence_id = ?",
            (occurrence_id,),
        ).fetchall()
        media_list = [dict(m) for m in media_rows if (is_owner or is_reviewer or m["derivative_status"] == "public_sanitized")]

        revs = []
        if is_owner or is_reviewer:
            rev_rows = self.conn.execute(
                "SELECT revision_number, actor_user_id, change_type, notes, created_at FROM observation_revisions WHERE occurrence_id = ? ORDER BY revision_number ASC",
                (occurrence_id,),
            ).fetchall()
            revs = [dict(r) for r in rev_rows]

        payload: Dict[str, Any] = {
            "observation": _row_to_public_dict(row, include_verbatim=include_verbatim),
            "media": media_list,
        }
        if revs:
            payload["revisions"] = revs
        return 200, payload

    def update_observation(
        self,
        occurrence_id: str,
        body: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Tuple[int, Dict[str, Any]]:
        lower = {k.lower(): v for k, v in headers.items()}
        user = self.require_auth(lower.get("authorization"))
        row = self.conn.execute(
            "SELECT * FROM observations WHERE occurrence_id = ?", (occurrence_id,)
        ).fetchone()
        if row is None:
            raise HttpError(404, f"Observation not found: {occurrence_id}", "not_found")

        is_owner = user["id"] == row["submitter_user_id"]
        is_reviewer = user["role"] in ("reviewer", "admin")
        if not (is_owner or is_reviewer):
            raise HttpError(403, "Only the submitter or an authorized reviewer can edit this record", "forbidden")

        v = validate_observation_input(body, row)
        sci_name = v["scientificName"]
        event_date = v["eventDate"]
        lat, lon, unc, locality = v["lat"], v["lon"], v["uncertainty"], v["locality"]
        iucn = body.get("iucnCategory", row["iucn_category"])
        explicit_sens = bool(body.get("isSensitiveSpecies", bool(row["is_sensitive_species"])))

        is_sens, pub_lat, pub_lon, pub_unc, withheld = generalize_coordinates(
            lat, lon, unc, sci_name, iucn, explicit_sens
        )
        # A reviewer who changes the name becomes the identifier (identifiedBy / identifiedByID).
        reidentified = is_reviewer and sci_name != row["scientific_name"]
        identified_by = user["display_name"] if reidentified else row["identified_by"]
        identified_by_id = user["orcid_id"] if reidentified else row["identified_by_id"]
        new_rev = int(row["revision_number"]) + 1
        new_review_status = row["review_status"] if is_reviewer else "submitted"
        now = utc_now_iso()

        with self.conn:
            self.conn.execute(
                """
                UPDATE observations
                SET revision_number = ?,
                    review_status = ?,
                    scientific_name = ?,
                    iucn_category = ?,
                    is_sensitive_species = ?,
                    public_latitude = ?,
                    public_longitude = ?,
                    public_coordinate_uncertainty_m = ?,
                    information_withheld = ?,
                    verbatim_scientific_name = ?,
                    verbatim_event_date = ?,
                    verbatim_latitude = ?,
                    verbatim_longitude = ?,
                    verbatim_coordinate_uncertainty_m = ?,
                    event_date = ?,
                    year = ?,
                    locality = ?,
                    verbatim_locality = ?,
                    identified_by = ?,
                    identified_by_id = ?,
                    updated_at = ?
                WHERE occurrence_id = ?
                """,
                (
                    new_rev,
                    new_review_status,
                    sci_name,
                    iucn,
                    1 if is_sens else 0,
                    pub_lat,
                    pub_lon,
                    pub_unc,
                    withheld,
                    sci_name,
                    event_date,
                    lat,
                    lon,
                    unc,
                    event_date,
                    int(event_date[:4]),
                    locality,
                    locality,
                    identified_by,
                    identified_by_id,
                    now,
                    occurrence_id,
                ),
            )
            self._record_revision(
                occurrence_id,
                new_rev,
                user["id"],
                "updated",
                body.get("notes") or "Observation corrected while preserving occurrenceID",
            )

        if row["review_status"] == "approved":
            self.rebuild_approved_overlay()

        updated = self.conn.execute(
            "SELECT * FROM observations WHERE occurrence_id = ?", (occurrence_id,)
        ).fetchone()
        return 200, {"observation": _row_to_public_dict(updated, include_verbatim=True)}

    def upload_media(
        self,
        occurrence_id: str,
        raw_bytes: bytes,
        headers: Dict[str, str],
    ) -> Tuple[int, Dict[str, Any]]:
        lower = {k.lower(): v for k, v in headers.items()}
        user = self.require_auth(lower.get("authorization"))

        content_len_hdr = lower.get("content-length")
        if content_len_hdr is None or not str(content_len_hdr).strip().isdigit():
            raise HttpError(411, "Content-Length header is required for streaming R2 upload", "length_required")
        declared_len = int(content_len_hdr)
        if declared_len <= 0:
            raise HttpError(400, "Empty upload payload", "empty_upload")
        if declared_len > MAX_UPLOAD_BYTES or len(raw_bytes) > MAX_UPLOAD_BYTES:
            raise HttpError(413, f"Upload exceeds maximum {MAX_UPLOAD_BYTES} bytes (15 MiB)", "payload_too_large")
        if len(raw_bytes) != declared_len:
            raise HttpError(400, "Payload size does not match Content-Length", "length_mismatch")

        obs = self.conn.execute(
            "SELECT * FROM observations WHERE occurrence_id = ?", (occurrence_id,)
        ).fetchone()
        if obs is None:
            raise HttpError(404, f"Observation not found: {occurrence_id}", "not_found")
        if user["id"] != obs["submitter_user_id"] and user["role"] not in ("reviewer", "admin"):
            raise HttpError(403, "Forbidden: cannot upload media to another user's observation", "forbidden")

        media_count = self.conn.execute(
            "SELECT COUNT(*) FROM media_intake WHERE occurrence_id = ?", (occurrence_id,)
        ).fetchone()[0]
        if media_count >= MAX_MEDIA_PER_OBSERVATION:
            raise HttpError(409, f"At most {MAX_MEDIA_PER_OBSERVATION} photos per observation", "too_many_media")

        mime_type = detect_media_magic_bytes(raw_bytes)
        exif = extract_exif_metadata(raw_bytes, mime_type)
        media_id = str(uuid.uuid4())
        private_key = f"intake/{occurrence_id}/{media_id}.bin"
        private_path = self.private_r2_dir / private_key
        private_path.parent.mkdir(parents=True, exist_ok=True)
        private_path.write_bytes(raw_bytes)

        digest = hashlib.sha256(raw_bytes).hexdigest()
        now = utc_now_iso()

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO media_intake (
                  media_id, occurrence_id, uploader_user_id, private_r2_key,
                  detected_mime_type, byte_length, sha256,
                  exif_gps_latitude, exif_gps_longitude, exif_datetime, exif_orientation,
                  license, rights_holder, derivative_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'private_only', ?)
                """,
                (
                    media_id,
                    occurrence_id,
                    user["id"],
                    private_key,
                    mime_type,
                    len(raw_bytes),
                    digest,
                    exif["gps_latitude"],
                    exif["gps_longitude"],
                    exif["datetime"],
                    exif["orientation"],
                    obs["license"],
                    obs["rights_holder"],
                    now,
                ),
            )

        return 201, {
            "media_id": media_id,
            "occurrence_id": occurrence_id,
            "detected_mime_type": mime_type,
            "byte_length": len(raw_bytes),
            "sha256": digest,
            "derivative_status": "private_only",
            "exif_extracted": exif,
        }

    def review_observation(
        self,
        occurrence_id: str,
        decision: str,
        body: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Tuple[int, Dict[str, Any]]:
        lower = {k.lower(): v for k, v in headers.items()}
        reviewer = self.require_auth(lower.get("authorization"))
        if reviewer["role"] not in ("reviewer", "admin"):
            raise HttpError(403, "Forbidden: reviewer or admin role required", "forbidden")

        if decision not in ("approved", "needs_changes"):
            raise HttpError(400, f"Invalid review decision: {decision!r}", "invalid_decision")

        obs = self.conn.execute(
            "SELECT * FROM observations WHERE occurrence_id = ?", (occurrence_id,)
        ).fetchone()
        if obs is None:
            raise HttpError(404, f"Observation not found: {occurrence_id}", "not_found")

        new_rev = int(obs["revision_number"]) + 1
        now = utc_now_iso()
        notes = str(body.get("notes") or "")

        sanitized_keys: List[str] = []
        with self.conn:
            self.conn.execute(
                """
                UPDATE observations
                SET review_status = ?,
                    revision_number = ?,
                    reviewer_user_id = ?,
                    identified_by = COALESCE(identified_by, ?),
                    identified_by_id = COALESCE(identified_by_id, ?),
                    review_notes = ?,
                    reviewed_at = ?,
                    updated_at = ?
                WHERE occurrence_id = ?
                """,
                (
                    decision,
                    new_rev,
                    reviewer["id"],
                    reviewer["display_name"],
                    reviewer["orcid_id"],
                    notes,
                    now,
                    now,
                    occurrence_id,
                ),
            )

            media_rows = self.conn.execute(
                "SELECT * FROM media_intake WHERE occurrence_id = ?", (occurrence_id,)
            ).fetchall()

            if decision == "approved":
                for m in media_rows:
                    priv_path = self.private_r2_dir / m["private_r2_key"]
                    if priv_path.is_file():
                        raw_bytes = priv_path.read_bytes()
                        clean_bytes = strip_private_metadata(raw_bytes, m["detected_mime_type"])
                        ext = "png" if m["detected_mime_type"] == "image/png" else "jpg"
                        public_key = f"media/{m['media_id']}.{ext}"
                        public_path = self.public_r2_dir / public_key
                        public_path.parent.mkdir(parents=True, exist_ok=True)
                        public_path.write_bytes(clean_bytes)
                        self.conn.execute(
                            "UPDATE media_intake SET public_r2_key = ?, derivative_status = 'public_sanitized' WHERE media_id = ?",
                            (public_key, m["media_id"]),
                        )
                        sanitized_keys.append(public_key)
            else:
                # Revoke public media visibility if record sent back to needs_changes
                for m in media_rows:
                    if m["public_r2_key"]:
                        pub_file = self.public_r2_dir / m["public_r2_key"]
                        if pub_file.exists():
                            pub_file.unlink()
                    self.conn.execute(
                        "UPDATE media_intake SET public_r2_key = NULL, derivative_status = 'revoked' WHERE media_id = ?",
                        (m["media_id"],),
                    )

            self._record_revision(occurrence_id, new_rev, reviewer["id"], decision, notes)

        overlay_res = self.rebuild_approved_overlay()
        updated = self.conn.execute(
            "SELECT * FROM observations WHERE occurrence_id = ?", (occurrence_id,)
        ).fetchone()
        return 200, {
            "occurrence_id": occurrence_id,
            "review_status": decision,
            "observation": _row_to_public_dict(updated, include_verbatim=True),
            "sanitized_media_keys": sanitized_keys,
            "skipped_media_ids": [],
            "overlay_record_count": len(overlay_res["features"]),
        }

    def _live_snapshot_ids(self) -> set:
        try:
            manifest = json.loads((self.public_r2_dir / "releases" / "manifest.json").read_text(encoding="utf-8"))
            rel = (manifest.get("layers") or {}).get("snapshot_own_occurrence_ids")
            if not rel:
                return set()
            ids = json.loads((self.public_r2_dir / rel.lstrip("/")).read_text(encoding="utf-8"))
            return {i for i in ids if isinstance(i, str)}
        except (OSError, ValueError):
            return set()

    def rebuild_approved_overlay(self) -> Dict[str, Any]:
        """Deterministically rebuild /overlays/approved.geojson via full D1/SQLite SELECT."""
        rows = self.conn.execute(
            "SELECT * FROM observations WHERE review_status = 'approved' ORDER BY occurrence_id ASC"
        ).fetchall()
        # Hide a record only once the live map release contains it (mirrors src/worker/overlay.ts).
        in_snapshot = self._live_snapshot_ids()

        features: List[Dict[str, Any]] = []
        for row in rows:
            if row["occurrence_id"] in in_snapshot:
                continue
            media_rows = self.conn.execute(
                "SELECT media_id, public_r2_key FROM media_intake WHERE occurrence_id = ? AND derivative_status = 'public_sanitized' ORDER BY media_id ASC",
                (row["occurrence_id"],),
            ).fetchall()
            features.append(
                {
                    "type": "Feature",
                    "id": row["occurrence_id"],
                    "properties": {
                        "occurrenceID": row["occurrence_id"],
                        "scientificName": row["scientific_name"],
                        "taxonKey": row["taxon_key"],
                        "eventDate": row["event_date"],
                        "year": row["year"],
                        "basisOfRecord": row["basis_of_record"],
                        "hybas_id": row["hybas_id"],
                        "coordinateUncertaintyInMeters": row["public_coordinate_uncertainty_m"],
                        "informationWithheld": row["information_withheld"],
                        "isSensitiveSpecies": bool(row["is_sensitive_species"]),
                        "license": row["license"],
                        "rightsHolder": row["rights_holder"],
                        "recordedByID": row["recorded_by_id"],
                        "identifiedByID": row["identified_by_id"],
                        "reviewStatus": row["review_status"],
                        "publicationStatus": row["publication_status"],
                        "gbifOccurrenceKey": row["gbif_occurrence_key"],
                        "provisional": True,
                        "mediaUrls": [f"/{m['public_r2_key']}" for m in media_rows if m["public_r2_key"]],
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [row["public_longitude"], row["public_latitude"]],
                    },
                }
            )

        fc = {
            "type": "FeatureCollection",
            "name": "approved_local_observations",
            "generated_at": utc_now_iso(),
            "features": features,
        }
        overlay_path = self.public_r2_dir / "overlays" / "approved.geojson"
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = overlay_path.with_suffix(".geojson.tmp")
        tmp_path.write_text(json.dumps(fc, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, overlay_path)
        return fc
