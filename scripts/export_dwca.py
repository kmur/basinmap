#!/usr/bin/env python3
"""Darwin Core Archive (.zip) exporter, structural validator, and GBIF UAT CLI.

Follows Sections 5.1 and 5.2 of hosting-and-gbif-publishing-plan.md:
  - Exports approved observations (using generalized coordinates for sensitive taxa)
    and permanent /media/<uuid>.jpg public derivatives into dwca-latest.zip
  - Maps Simple Multimedia terms (license, rightsHolder, identifier)
  - Validates archive XML, column mapping, and referential integrity
  - GBIF Data Validator (authenticated POST /v1/validation + polling), registry crawl requests,
    reconciliation of GBIF-indexed occurrenceIDs, exports/status.json, UAT registration
  - Python mirror of src/worker/publication.ts (keep both in sync)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from xml.sax.saxutils import escape

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend_engine import init_db, utc_now_iso  # noqa: E402

LICENSE_URI_MAP = {
    "CC-BY 4.0": "http://creativecommons.org/licenses/by/4.0/legalcode",
    "CC0 1.0": "http://creativecommons.org/publicdomain/zero/1.0/legalcode",
}

OCCURRENCE_FIELDS = [
    ("occurrenceID", "http://rs.tdwg.org/dwc/terms/occurrenceID"),
    ("basisOfRecord", "http://rs.tdwg.org/dwc/terms/basisOfRecord"),
    ("scientificName", "http://rs.tdwg.org/dwc/terms/scientificName"),
    ("taxonKey", "http://rs.gbif.org/terms/1.0/taxonKey"),
    ("kingdom", "http://rs.tdwg.org/dwc/terms/kingdom"),
    ("family", "http://rs.tdwg.org/dwc/terms/family"),
    ("eventDate", "http://rs.tdwg.org/dwc/terms/eventDate"),
    ("decimalLatitude", "http://rs.tdwg.org/dwc/terms/decimalLatitude"),
    ("decimalLongitude", "http://rs.tdwg.org/dwc/terms/decimalLongitude"),
    ("geodeticDatum", "http://rs.tdwg.org/dwc/terms/geodeticDatum"),
    ("coordinateUncertaintyInMeters", "http://rs.tdwg.org/dwc/terms/coordinateUncertaintyInMeters"),
    ("informationWithheld", "http://rs.tdwg.org/dwc/terms/informationWithheld"),
    ("locality", "http://rs.tdwg.org/dwc/terms/locality"),
    ("recordedBy", "http://rs.tdwg.org/dwc/terms/recordedBy"),
    ("recordedByID", "http://rs.tdwg.org/dwc/terms/recordedByID"),
    ("identifiedBy", "http://rs.tdwg.org/dwc/terms/identifiedBy"),
    ("identifiedByID", "http://rs.tdwg.org/dwc/terms/identifiedByID"),
    ("license", "http://purl.org/dc/terms/license"),
    ("rightsHolder", "http://purl.org/dc/terms/rightsHolder"),
    ("modified", "http://purl.org/dc/terms/modified"),
]

MULTIMEDIA_FIELDS = [
    ("coreid", None),
    ("identifier", "http://purl.org/dc/terms/identifier"),
    ("type", "http://purl.org/dc/terms/type"),
    ("format", "http://purl.org/dc/terms/format"),
    ("created", "http://purl.org/dc/terms/created"),
    ("creator", "http://purl.org/dc/terms/creator"),
    ("license", "http://purl.org/dc/terms/license"),
    ("rightsHolder", "http://purl.org/dc/terms/rightsHolder"),
]


def _tsv_cell(val: Any) -> str:
    if val is None:
        return ""
    return str(val).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def _build_meta_xml() -> str:
    occ_field_lines = []
    for idx, (_, term) in enumerate(OCCURRENCE_FIELDS):
        if idx == 0:
            occ_field_lines.append(f'    <id index="0" />')
        occ_field_lines.append(f'    <field index="{idx}" term="{term}" />')

    mm_field_lines = ['    <coreid index="0" />']
    for idx, (_, term) in enumerate(MULTIMEDIA_FIELDS):
        if term is not None:
            mm_field_lines.append(f'    <field index="{idx}" term="{term}" />')

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<archive xmlns="http://rs.tdwg.org/dwc/text/" metadata="eml.xml">\n'
        '  <core encoding="UTF-8" fieldsTerminatedBy="\\t" linesTerminatedBy="\\n" '
        'fieldsEnclosedBy="" ignoreHeaderLines="1" rowType="http://rs.tdwg.org/dwc/terms/Occurrence">\n'
        '    <files>\n'
        '      <location>occurrence.txt</location>\n'
        '    </files>\n'
        + "\n".join(occ_field_lines)
        + "\n  </core>\n"
        '  <extension encoding="UTF-8" fieldsTerminatedBy="\\t" linesTerminatedBy="\\n" '
        'fieldsEnclosedBy="" ignoreHeaderLines="1" rowType="http://rs.gbif.org/terms/1.0/Multimedia">\n'
        '    <files>\n'
        '      <location>multimedia.txt</location>\n'
        '    </files>\n'
        + "\n".join(mm_field_lines)
        + "\n  </extension>\n"
        "</archive>\n"
    )


def _build_eml_xml(
    export_id: str,
    dataset_license: str,
    bbox: Sequence[float],
    min_date: str,
    max_date: str,
    pub_date: str,
) -> str:
    license_uri = LICENSE_URI_MAP[dataset_license]
    west, south, east, north = bbox
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<eml:eml xmlns:eml="eml://ecoinformatics.org/eml-2.1.1"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="eml://ecoinformatics.org/eml-2.1.1 http://rs.gbif.org/schema/eml-gbif-profile/1.2/eml.xsd"
         packageId="{escape(export_id)}"
         system="https://basinmap.org"
         scope="system"
         xml:lang="eng">
  <dataset>
    <alternateIdentifier>{escape(export_id)}</alternateIdentifier>
    <title xml:lang="eng">basinmap.org Community Hydrological Basin Biodiversity Observations</title>
    <creator>
      <organizationName>basinmap.org Open Web-GIS Project</organizationName>
      <positionName>Data Steward</positionName>
      <electronicMailAddress>stewardship@basinmap.org</electronicMailAddress>
      <onlineUrl>https://basinmap.org</onlineUrl>
    </creator>
    <metadataProvider>
      <organizationName>basinmap.org Open Web-GIS Project</organizationName>
      <positionName>Data Steward</positionName>
      <electronicMailAddress>stewardship@basinmap.org</electronicMailAddress>
      <onlineUrl>https://basinmap.org</onlineUrl>
    </metadataProvider>
    <pubDate>{escape(pub_date)}</pubDate>
    <language>eng</language>
    <abstract>
      <para>Reviewed biodiversity observations contributed via basinmap.org with hydrological basin context, preserved verbatim metadata, and coordinate generalization for sensitive taxa.</para>
    </abstract>
    <intellectualRights>
      <para>This work is licensed under <ulink url="{escape(license_uri)}"><citetitle>{escape(dataset_license)}</citetitle></ulink>.</para>
    </intellectualRights>
    <coverage>
      <geographicCoverage>
        <geographicDescription>Hydrological basins mapped by basinmap.org</geographicDescription>
        <boundingCoordinates>
          <westBoundingCoordinate>{west:.4f}</westBoundingCoordinate>
          <eastBoundingCoordinate>{east:.4f}</eastBoundingCoordinate>
          <northBoundingCoordinate>{north:.4f}</northBoundingCoordinate>
          <southBoundingCoordinate>{south:.4f}</southBoundingCoordinate>
        </boundingCoordinates>
      </geographicCoverage>
      <temporalCoverage>
        <rangeOfDates>
          <beginDate><calendarDate>{escape(min_date)}</calendarDate></beginDate>
          <endDate><calendarDate>{escape(max_date)}</calendarDate></endDate>
        </rangeOfDates>
      </temporalCoverage>
    </coverage>
    <contact>
      <organizationName>basinmap.org Data Stewardship</organizationName>
      <positionName>Data Steward</positionName>
      <electronicMailAddress>stewardship@basinmap.org</electronicMailAddress>
      <onlineUrl>https://basinmap.org</onlineUrl>
    </contact>
  </dataset>
</eml:eml>
"""


def compatible_licenses(dataset_license: str) -> List[str]:
    """Licences publishable inside a dataset under ``dataset_license`` (CC0 may be relicensed as CC-BY)."""
    return ["CC-BY 4.0", "CC0 1.0"] if dataset_license == "CC-BY 4.0" else [dataset_license]


def _bump_revision(conn: sqlite3.Connection, oid: str, actor: str, change_type: str, snapshot: Dict[str, Any],
                   notes: str, now: str, new_status: str) -> None:
    row = conn.execute("SELECT revision_number FROM observations WHERE occurrence_id = ?", (oid,)).fetchone()
    new_rev = int(row["revision_number"]) + 1
    # updated_at is Darwin Core `modified` (record content). Status-only changes must not touch it,
    # otherwise every export would change the archive and trigger another GBIF crawl.
    conn.execute(
        "UPDATE observations SET publication_status = ?, revision_number = ? WHERE occurrence_id = ?",
        (new_status, new_rev, oid),
    )
    conn.execute(
        """
        INSERT INTO observation_revisions
          (occurrence_id, revision_number, actor_user_id, change_type, snapshot_json, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (oid, new_rev, actor, change_type, json.dumps(snapshot), notes, now),
    )


def export_dwca(
    conn: sqlite3.Connection,
    public_r2_dir: Path,
    public_base_url: str = "https://data.basinmap.org",
    dataset_license: str = "CC-BY 4.0",
    force: bool = True,
) -> Dict[str, Any]:
    """Build the archive from approved records (mirrors exportDwca in src/worker/publication.ts).

    * CC0 records are included in a CC-BY 4.0 dataset (CC0 permits relicensing); other
      licence mismatches are counted in ``excluded_license_count``.
    * ``publication_status`` only moves ``not_exported -> exported``; records already
      published/indexed keep their status. Records exported earlier but no longer approved are
      withdrawn (status back to ``not_exported``) and omitted, so GBIF removes them on re-crawl.
    * With ``force=False`` nothing is written when occurrence/multimedia content is unchanged.
    * Writes ``exports/dwca-latest.zip`` (crawled URL), an immutable
      ``exports/archive/<export_id>.zip`` snapshot, and ``exports/eml.xml``.
    """
    if dataset_license not in LICENSE_URI_MAP:
        raise ValueError(f"Unsupported dataset_license: {dataset_license!r}")

    license_uri = LICENSE_URI_MAP[dataset_license]
    allowed = compatible_licenses(dataset_license)
    marks = ",".join("?" for _ in allowed)
    rows = conn.execute(
        f"SELECT * FROM observations WHERE review_status = 'approved' AND license IN ({marks}) ORDER BY occurrence_id ASC",
        allowed,
    ).fetchall()
    excluded = conn.execute(
        f"SELECT COUNT(*) FROM observations WHERE review_status = 'approved' AND license NOT IN ({marks})", allowed
    ).fetchone()[0]

    occ_lines = ["\t".join(col for col, _ in OCCURRENCE_FIELDS)]
    mm_lines = ["\t".join(col for col, _ in MULTIMEDIA_FIELDS)]
    lats: List[float] = []
    lons: List[float] = []
    dates: List[str] = []
    media_count = 0
    clean_base = public_base_url.rstrip("/")

    for row in rows:
        lats.append(float(row["public_latitude"]))
        lons.append(float(row["public_longitude"]))
        dates.append(str(row["event_date"])[:10])
        occ_row = [
            row["occurrence_id"], row["basis_of_record"], row["scientific_name"], row["taxon_key"], row["kingdom"],
            row["family"], row["event_date"], row["public_latitude"], row["public_longitude"], row["geodetic_datum"],
            row["public_coordinate_uncertainty_m"], row["information_withheld"], row["locality"], row["recorded_by"],
            row["recorded_by_id"], row["identified_by"], row["identified_by_id"], license_uri, row["rights_holder"],
            row["updated_at"],
        ]
        occ_lines.append("\t".join(_tsv_cell(v) for v in occ_row))
        media_rows = conn.execute(
            """
            SELECT * FROM media_intake
            WHERE occurrence_id = ? AND derivative_status = 'public_sanitized'
            ORDER BY media_id ASC
            """,
            (row["occurrence_id"],),
        ).fetchall()
        for m in media_rows:
            if not m["public_r2_key"]:
                continue
            media_count += 1
            mm_row = [
                row["occurrence_id"],
                f"{clean_base}/{m['public_r2_key'].lstrip('/')}",
                "StillImage",
                "image/jpeg" if m["public_r2_key"].endswith(".jpg") else m["detected_mime_type"],
                m["created_at"],
                row["recorded_by"],
                LICENSE_URI_MAP.get(m["license"], license_uri),
                m["rights_holder"],
            ]
            mm_lines.append("\t".join(_tsv_cell(v) for v in mm_row))

    occ_txt = "\n".join(occ_lines) + "\n"
    mm_txt = "\n".join(mm_lines) + "\n"
    content_sha = hashlib.sha256(f"{occ_txt}\0{mm_txt}".encode("utf-8")).hexdigest()
    last = conn.execute("SELECT content_sha256 FROM publication_exports ORDER BY created_at DESC LIMIT 1").fetchone()
    withdrawn = conn.execute(
        """
        SELECT occurrence_id, COALESCE(reviewer_user_id, submitter_user_id) AS actor FROM observations
        WHERE review_status != 'approved' AND publication_status != 'not_exported'
        """
    ).fetchall()

    base_result = {
        "dataset_license": dataset_license,
        "record_count": len(rows),
        "media_count": media_count,
        "excluded_license_count": excluded,
        "withdrawn_count": len(withdrawn),
        "content_sha256": content_sha,
    }
    if not force and last is not None and last["content_sha256"] == content_sha and not withdrawn:
        return {"changed": False, "export_id": None, "newly_exported": 0, **base_result}

    now_dt = datetime.now(timezone.utc)
    today = now_dt.strftime("%Y-%m-%d")
    export_id = f"basinmap-dwca-{now_dt.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    bbox = [min(lons), min(lats), max(lons), max(lats)] if lons else [-123.8, 43.5, -121.5, 46.2]
    eml_xml = _build_eml_xml(export_id, dataset_license, bbox, min(dates) if dates else today,
                             max(dates) if dates else today, today)

    exports_dir = public_r2_dir / "exports"
    (exports_dir / "archive").mkdir(parents=True, exist_ok=True)
    zip_path = exports_dir / "dwca-latest.zip"
    tmp_zip = exports_dir / "dwca-latest.zip.tmp"
    stamp = now_dt.timetuple()[:6]
    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fname, content in (("eml.xml", eml_xml), ("meta.xml", _build_meta_xml()),
                               ("occurrence.txt", occ_txt), ("multimedia.txt", mm_txt)):
            zf.writestr(zipfile.ZipInfo(fname, date_time=stamp), content.encode("utf-8"))
    snapshot_key = f"exports/archive/{export_id}.zip"
    shutil.copyfile(tmp_zip, public_r2_dir / snapshot_key)  # immutable snapshot first
    os.replace(tmp_zip, zip_path)
    (exports_dir / "eml.xml").write_text(eml_xml, encoding="utf-8")
    zip_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    now = utc_now_iso()

    newly = 0
    with conn:
        for row in rows:
            conn.execute("UPDATE observations SET exported_dwca_version = ? WHERE occurrence_id = ?",
                         (export_id, row["occurrence_id"]))
            if row["publication_status"] == "not_exported":
                newly += 1
                _bump_revision(conn, row["occurrence_id"], row["reviewer_user_id"] or row["submitter_user_id"],
                               "exported", {"occurrence_id": row["occurrence_id"], "exported_dwca_version": export_id},
                               f"Exported in DwC-A {export_id}", now, "exported")
        for w in withdrawn:
            # change_type CHECK (0001) has no 'withdrawn': record it as 'updated' with an explicit note.
            _bump_revision(conn, w["occurrence_id"], w["actor"], "updated",
                           {"occurrence_id": w["occurrence_id"], "withdrawn_from": export_id},
                           f"Withdrawn: omitted from DwC-A {export_id} (no longer approved)", now, "not_exported")
        conn.execute(
            """
            INSERT INTO publication_exports
              (export_id, archive_r2_key, dataset_license, record_count, media_count, sha256, validator_status,
               created_at, content_sha256, archive_snapshot_r2_key, excluded_license_count)
            VALUES (?, 'exports/dwca-latest.zip', ?, ?, ?, ?, 'not_submitted', ?, ?, ?, ?)
            """,
            (export_id, dataset_license, len(rows), media_count, zip_sha, now, content_sha, snapshot_key, excluded),
        )

    return {
        "changed": True,
        "export_id": export_id,
        "zip_path": str(zip_path),
        "archive_r2_key": "exports/dwca-latest.zip",
        "archive_snapshot_r2_key": snapshot_key,
        "newly_exported": newly,
        "sha256": zip_sha,
        "validation": validate_dwca_zip(zip_path),
        **base_result,
    }



def validate_dwca_zip(zip_path: Path) -> Dict[str, Any]:
    """Perform strict structural validation on a Darwin Core Archive zip."""
    if not zip_path.is_file():
        raise FileNotFoundError(f"DwC-A zip not found: {zip_path}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        required = {"eml.xml", "meta.xml", "occurrence.txt", "multimedia.txt"}
        missing = required - names
        if missing:
            raise AssertionError(f"DwC-A missing required entries: {sorted(missing)}")

        eml_root = ET.fromstring(zf.read("eml.xml").decode("utf-8"))
        meta_root = ET.fromstring(zf.read("meta.xml").decode("utf-8"))

        ns = {"dwc": "http://rs.tdwg.org/dwc/text/"}
        core = meta_root.find("dwc:core", ns)
        ext = meta_root.find("dwc:extension", ns)
        if core is None or ext is None:
            raise AssertionError("meta.xml must define both <core> and <extension>")

        core_fields = core.findall("dwc:field", ns)
        ext_fields = ext.findall("dwc:field", ns)

        occ_lines = [line for line in zf.read("occurrence.txt").decode("utf-8").splitlines() if line]
        mm_lines = [line for line in zf.read("multimedia.txt").decode("utf-8").splitlines() if line]

        if not occ_lines or not mm_lines:
            raise AssertionError("occurrence.txt and multimedia.txt must include header rows")

        occ_headers = occ_lines[0].split("\t")
        mm_headers = mm_lines[0].split("\t")
        if len(occ_headers) != len(core_fields):
            raise AssertionError(f"occurrence.txt header count {len(occ_headers)} != meta.xml fields {len(core_fields)}")
        if len(mm_headers) != len(ext_fields) + 1:  # +1 for coreid column at index 0
            raise AssertionError(f"multimedia.txt header count {len(mm_headers)} != meta.xml fields+coreid {len(ext_fields) + 1}")

        occ_ids = set()
        occ_id_idx = occ_headers.index("occurrenceID")
        occ_lic_idx = occ_headers.index("license")
        for line in occ_lines[1:]:
            cols = line.split("\t")
            if len(cols) != len(occ_headers):
                raise AssertionError("occurrence.txt row column count mismatch")
            occ_ids.add(cols[occ_id_idx])
            if cols[occ_lic_idx] not in LICENSE_URI_MAP.values():
                raise AssertionError(f"Invalid occurrence license URI: {cols[occ_lic_idx]!r}")

        mm_id_idx = mm_headers.index("identifier")
        mm_lic_idx = mm_headers.index("license")
        mm_rh_idx = mm_headers.index("rightsHolder")
        for line in mm_lines[1:]:
            cols = line.split("\t")
            if len(cols) != len(mm_headers):
                raise AssertionError("multimedia.txt row column count mismatch")
            if cols[0] not in occ_ids:
                raise AssertionError(f"multimedia.txt coreid {cols[0]!r} not found in occurrence.txt")
            if "/media/" not in cols[mm_id_idx] or "/releases/" in cols[mm_id_idx]:
                raise AssertionError(f"Media identifier must use permanent /media/ prefix: {cols[mm_id_idx]!r}")
            if cols[mm_lic_idx] not in LICENSE_URI_MAP.values():
                raise AssertionError(f"Invalid multimedia license URI: {cols[mm_lic_idx]!r}")
            if not cols[mm_rh_idx]:
                raise AssertionError("multimedia.txt rightsHolder must not be empty")

        title_el = eml_root.find(".//title")
        return {
            "valid": True,
            "eml_title": title_el.text if title_el is not None else "",
            "occurrence_rows": len(occ_lines) - 1,
            "multimedia_rows": len(mm_lines) - 1,
        }



VALIDATOR_URL = "https://api.gbif.org/v1/validation"
VALIDATOR_PENDING = {"submitted", "queued", "running", "downloading"}
MAX_RECONCILE_PAGES = 40


def _gbif_auth() -> Optional[str]:
    user, pwd = os.environ.get("GBIF_USER", ""), os.environ.get("GBIF_PWD", "")
    if not user or not pwd:
        return None
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")


def build_gbif_validator_request(zip_path: Path) -> Dict[str, Any]:
    val = validate_dwca_zip(zip_path)
    return {
        "endpoint": VALIDATOR_URL,
        "method": "POST",
        "auth": "HTTP Basic (GBIF_USER / GBIF_PWD)",
        "file_path": str(zip_path),
        "file_bytes": zip_path.stat().st_size,
        "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
        "local_validation": val,
    }


def submit_to_gbif_validator(
    zip_path: Path,
    dry_run: bool = False,
    urlopen: Optional[Callable[..., Any]] = None,
    conn: Optional[sqlite3.Connection] = None,
    export_id: Optional[str] = None,
) -> Dict[str, Any]:
    """POST the archive to the GBIF Data Validator (multipart `file` + `validationRequest`, Basic auth)."""
    req_info = build_gbif_validator_request(zip_path)
    if dry_run:
        return {"dry_run": True, **req_info}
    auth = _gbif_auth()
    if not auth:
        raise RuntimeError("Set GBIF_USER and GBIF_PWD (a GBIF.org account) to use the GBIF Data Validator")

    boundary = f"----BasinmapBoundary{uuid.uuid4().hex}"
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode("ascii"))
    body.write(f'Content-Disposition: form-data; name="file"; filename="{zip_path.name}"\r\n'.encode("utf-8"))
    body.write(b"Content-Type: application/zip\r\n\r\n")
    body.write(zip_path.read_bytes())
    body.write(f"\r\n--{boundary}\r\n".encode("ascii"))
    body.write(b'Content-Disposition: form-data; name="validationRequest"\r\nContent-Type: application/json\r\n\r\n')
    body.write(json.dumps({"sourceId": f"basinmap.org/{export_id or zip_path.stem}"}).encode("utf-8"))
    body.write(f"\r\n--{boundary}--\r\n".encode("ascii"))
    req = urllib.request.Request(
        VALIDATOR_URL,
        data=body.getvalue(),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Authorization": auth,
                 "User-Agent": "basinmap.org"},
    )
    with (urlopen or urllib.request.urlopen)(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode("utf-8") or "{}")
    status = str(payload.get("status") or "submitted").lower()
    if conn is not None and export_id:
        with conn:
            conn.execute(
                "UPDATE publication_exports SET validator_job_id = ?, validator_status = ?, validator_checked_at = ? WHERE export_id = ?",
                (payload.get("key"), status, utc_now_iso(), export_id),
            )
    return {"dry_run": False, "key": payload.get("key"), "status": status, "response": payload}


def poll_gbif_validator(key: str, urlopen: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    auth = _gbif_auth()
    if not auth:
        raise RuntimeError("Set GBIF_USER and GBIF_PWD to read GBIF Data Validator results")
    req = urllib.request.Request(f"{VALIDATOR_URL}/{key}", headers={"Authorization": auth, "User-Agent": "basinmap.org"})
    with (urlopen or urllib.request.urlopen)(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def poll_pending_validations(conn: sqlite3.Connection, urlopen: Optional[Callable[..., Any]] = None) -> List[Dict[str, str]]:
    if not _gbif_auth():
        return []
    out = []
    pending = conn.execute(
        """
        SELECT export_id, validator_job_id FROM publication_exports
        WHERE validator_job_id IS NOT NULL AND validator_status IN ('submitted','queued','running','downloading')
        """
    ).fetchall()
    for p in pending:
        body = poll_gbif_validator(p["validator_job_id"], urlopen)
        status = str(body.get("status") or "unknown").lower()
        summary = None if status in VALIDATOR_PENDING else json.dumps(body)[:20000]
        with conn:
            conn.execute(
                """
                UPDATE publication_exports SET validator_status = ?, validator_summary = COALESCE(?, validator_summary),
                       validator_checked_at = ? WHERE export_id = ?
                """,
                (status, summary, utc_now_iso(), p["export_id"]),
            )
        out.append({"export_id": p["export_id"], "status": status})
    return out


def request_crawl(conn: sqlite3.Connection, export_id: str, dataset_key: Optional[str], api_base: str,
                  urlopen: Optional[Callable[..., Any]] = None) -> str:
    if not dataset_key:
        status = "no_dataset_key"
    elif not _gbif_auth():
        status = "no_credentials"
    else:
        req = urllib.request.Request(
            f"{api_base.rstrip('/')}/dataset/{dataset_key}/crawl", data=b"", method="POST",
            headers={"Authorization": _gbif_auth() or "", "User-Agent": "basinmap.org"},
        )
        try:
            with (urlopen or urllib.request.urlopen)(req, timeout=60):
                status = "requested"
        except urllib.error.HTTPError as err:
            status = "not_authorized" if err.code in (401, 403) else f"error_{err.code}"
    with conn:
        conn.execute("UPDATE publication_exports SET crawl_status = ? WHERE export_id = ?", (status, export_id))
    return status


def reconcile_with_gbif(conn: sqlite3.Connection, dataset_key: Optional[str], api_base: str,
                        urlopen: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Mark approved records GBIF has indexed from our dataset as ``indexed_by_gbif``."""
    if not dataset_key:
        return {"checked": 0, "newly_indexed": 0, "complete": True}
    opener = urlopen or urllib.request.urlopen
    offset = checked = newly = 0
    complete = False
    now = utc_now_iso()
    for _ in range(MAX_RECONCILE_PAGES):
        url = f"{api_base.rstrip('/')}/occurrence/search?datasetKey={dataset_key}&limit=300&offset={offset}"
        with opener(urllib.request.Request(url, headers={"User-Agent": "basinmap.org"}), timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
        results = body.get("results") or []
        with conn:
            for r in results:
                if not r.get("occurrenceID") or not r.get("key"):
                    continue
                checked += 1
                cur = conn.execute(
                    """
                    UPDATE observations SET publication_status = 'indexed_by_gbif', gbif_occurrence_key = ?, gbif_indexed_at = ?
                    WHERE occurrence_id = ? AND review_status = 'approved'
                      AND (publication_status != 'indexed_by_gbif' OR gbif_occurrence_key IS NULL OR gbif_occurrence_key != ?)
                    """,
                    (r["key"], now, r["occurrenceID"], r["key"]),
                )
                newly += cur.rowcount
        offset += len(results)
        if body.get("endOfRecords") or not results:
            complete = True
            break
    return {"checked": checked, "newly_indexed": newly, "complete": complete}


def write_status(conn: sqlite3.Connection, public_r2_dir: Path, public_base_url: str, dataset_license: str,
                 dataset_key: Optional[str], api_base: str) -> Dict[str, Any]:
    base = public_base_url.rstrip("/")
    latest = conn.execute(
        """
        SELECT export_id, created_at, record_count, media_count, sha256, archive_snapshot_r2_key, validator_status,
               validator_checked_at, validator_summary, crawl_status, excluded_license_count
        FROM publication_exports ORDER BY created_at DESC LIMIT 1
        """
    ).fetchone()
    counts = conn.execute(
        "SELECT publication_status AS s, COUNT(*) AS n FROM observations WHERE review_status = 'approved' GROUP BY publication_status"
    ).fetchall()
    indexeable = None
    if latest is not None and latest["validator_summary"]:
        try:
            indexeable = json.loads(latest["validator_summary"]).get("indexeable")
        except ValueError:
            indexeable = None
    status = {
        "generated_at": utc_now_iso(),
        "dataset_license": dataset_license,
        "archive_url": f"{base}/exports/dwca-latest.zip",
        "latest_export": None if latest is None else {
            "export_id": latest["export_id"],
            "created_at": latest["created_at"],
            "record_count": latest["record_count"],
            "media_count": latest["media_count"],
            "sha256": latest["sha256"],
            "snapshot_url": f"{base}/{latest['archive_snapshot_r2_key']}" if latest["archive_snapshot_r2_key"] else None,
            "excluded_license_count": latest["excluded_license_count"] or 0,
            "gbif_validator": {"status": latest["validator_status"], "checked_at": latest["validator_checked_at"],
                               "indexeable": indexeable},
            "gbif_crawl": latest["crawl_status"],
        },
        "gbif": {"api_base": api_base.rstrip("/"), "dataset_key": dataset_key or None},
        "approved_records_by_publication_status": {r["s"]: r["n"] for r in counts},
        "note": "Approved records appear on GBIF only after the registered dataset is crawled; see publication_status.",
    }
    out = public_r2_dir / "exports" / "status.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    return status


def run_publication(
    conn: sqlite3.Connection,
    public_r2_dir: Path,
    trigger: str = "manual",
    public_base_url: str = "https://data.basinmap.org",
    dataset_license: str = "CC-BY 4.0",
    dataset_key: Optional[str] = None,
    api_base: str = "https://api.gbif.org/v1",
    force: bool = False,
    urlopen: Optional[Callable[..., Any]] = None,
    rebuild_overlay: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Mirror of runPublication in src/worker/publication.ts (local/dev and manual operations)."""
    run_id = str(uuid.uuid4())
    started = utc_now_iso()
    with conn:
        conn.execute("INSERT INTO publication_runs (run_id, trigger, started_at) VALUES (?, ?, ?)", (run_id, trigger, started))
    summary: Dict[str, Any] = {"run_id": run_id, "trigger": trigger, "started_at": started}
    try:
        summary["validations"] = poll_pending_validations(conn, urlopen)
        rec = reconcile_with_gbif(conn, dataset_key, api_base, urlopen)
        summary["reconcile"] = rec
        if rec["newly_indexed"] and rebuild_overlay is not None:
            summary["overlay_records"] = len(rebuild_overlay()["features"])
        exp = export_dwca(conn, public_r2_dir, public_base_url, dataset_license, force=force)
        summary["export"] = exp
        if exp["changed"] and not exp.get("record_count"):
            # GBIF rejects an empty occurrence core (OCCURRENCE_CORE_EMPTY); wait for records.
            summary["validator"] = summary["crawl"] = "skipped_empty"
        elif exp["changed"]:
            zip_path = Path(exp["zip_path"])
            summary["validator"] = (
                submit_to_gbif_validator(zip_path, urlopen=urlopen, conn=conn, export_id=exp["export_id"])["status"]
                if _gbif_auth() and os.environ.get("GBIF_VALIDATE", "true") != "false"
                else "not_submitted"
            )
            summary["crawl"] = request_crawl(conn, exp["export_id"], dataset_key, api_base, urlopen)
        summary["status"] = write_status(conn, public_r2_dir, public_base_url, dataset_license, dataset_key, api_base)
        with conn:
            conn.execute(
                "UPDATE publication_runs SET finished_at = ?, export_id = ?, summary_json = ? WHERE run_id = ?",
                (utc_now_iso(), exp.get("export_id"), json.dumps(summary, default=str)[:20000], run_id),
            )
        return summary
    except Exception as err:
        with conn:
            conn.execute(
                "UPDATE publication_runs SET finished_at = ?, error = ?, summary_json = ? WHERE run_id = ?",
                (utc_now_iso(), str(err)[:2000], json.dumps(summary, default=str)[:20000], run_id),
            )
        raise



def build_gbif_uat_registration(
    publishing_org_key: str,
    installation_key: str,
    dwca_url: str = "https://data.basinmap.org/exports/dwca-latest.zip",
) -> Dict[str, Any]:
    return {
        "dataset_endpoint": "https://api.gbif-uat.org/v1/dataset",
        "dataset_payload": {
            "publishingOrganizationKey": publishing_org_key,
            "installationKey": installation_key,
            "title": "basinmap.org Community Hydrological Basin Biodiversity Observations (UAT)",
            "type": "OCCURRENCE",
            "homepage": "https://basinmap.org",
            "license": "CC_BY_4_0",
        },
        "endpoint_payload": {
            "type": "DWC_ARCHIVE",
            "url": dwca_url,
        },
    }


def register_on_gbif_uat(
    publishing_org_key: str,
    installation_key: str,
    dwca_url: str = "https://data.basinmap.org/exports/dwca-latest.zip",
    dry_run: bool = False,
) -> Dict[str, Any]:
    plan = build_gbif_uat_registration(publishing_org_key, installation_key, dwca_url)
    if dry_run:
        return {"dry_run": True, **plan}

    user = os.environ.get("GBIF_UAT_USER", "")
    password = os.environ.get("GBIF_UAT_PASSWORD", "")
    if not user or not password:
        raise RuntimeError("Set GBIF_UAT_USER and GBIF_UAT_PASSWORD environment variables for live UAT registration")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")

    req = urllib.request.Request(
        plan["dataset_endpoint"],
        data=json.dumps(plan["dataset_payload"]).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        dataset_key = json.loads(resp.read().decode("utf-8"))

    ep_req = urllib.request.Request(
        f"{plan['dataset_endpoint']}/{dataset_key}/endpoint",
        data=json.dumps(plan["endpoint_payload"]).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {token}",
        },
    )
    with urllib.request.urlopen(ep_req, timeout=30) as ep_resp:
        ep_res = ep_resp.read().decode("utf-8")

    return {"dry_run": False, "dataset_key": dataset_key, "endpoint_response": ep_res}




def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export, validate, publish and reconcile basinmap.org Darwin Core Archives.")
    parser.add_argument("--db", type=Path, default=ROOT_DIR / ".local-data" / "basinmap.sqlite3")
    parser.add_argument("--public-r2", type=Path, default=ROOT_DIR / "public-r2")
    parser.add_argument("--public-base-url", default="https://data.basinmap.org")
    parser.add_argument("--license", default="CC-BY 4.0", choices=sorted(LICENSE_URI_MAP))
    parser.add_argument("--gbif-api-base", default=os.environ.get("GBIF_API_BASE", "https://api.gbif.org/v1"))
    parser.add_argument("--dataset-key", default=os.environ.get("GBIF_DATASET_KEY") or None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_exp = sub.add_parser("export", help="Build and validate dwca-latest.zip from SQLite")
    p_exp.add_argument("--if-changed", action="store_true", help="Skip when record content is unchanged")

    sub.add_parser("run", help="Full pipeline: poll validator, reconcile, export-if-changed, validate, crawl, status.json")

    p_val = sub.add_parser("validate-gbif", help="Submit dwca-latest.zip to the GBIF Data Validator (GBIF_USER/GBIF_PWD)")
    p_val.add_argument("--zip", type=Path, default=ROOT_DIR / "public-r2" / "exports" / "dwca-latest.zip")
    p_val.add_argument("--dry-run", action="store_true")
    p_val.add_argument("--wait", action="store_true", help="Poll until the validator finishes")
    p_val.add_argument("--poll-seconds", type=float, default=20.0)

    sub.add_parser("reconcile", help="Mark records GBIF has indexed from --dataset-key as indexed_by_gbif")

    p_uat = sub.add_parser("register-uat", help="Register dataset + DwC-A endpoint on api.gbif-uat.org")
    p_uat.add_argument("--org-key", default="00000000-0000-0000-0000-000000000001")
    p_uat.add_argument("--installation-key", default="00000000-0000-0000-0000-000000000002")
    p_uat.add_argument("--dwca-url", default="https://data.basinmap.org/exports/dwca-latest.zip")
    p_uat.add_argument("--dry-run", action="store_true")

    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.command == "validate-gbif":
        res = submit_to_gbif_validator(args.zip, dry_run=args.dry_run)
        if args.wait and not args.dry_run and res.get("key"):
            while res["status"] in VALIDATOR_PENDING:
                time.sleep(args.poll_seconds)
                body = poll_gbif_validator(res["key"])
                res = {**res, "status": str(body.get("status") or "unknown").lower(), "response": body}
        print(json.dumps(res, indent=2, default=str))
        return 0
    if args.command == "register-uat":
        res = register_on_gbif_uat(args.org_key, args.installation_key, args.dwca_url, dry_run=args.dry_run)
        print(json.dumps(res, indent=2))
        return 0
    conn = init_db(args.db)
    try:
        if args.command == "export":
            res = export_dwca(conn, args.public_r2, args.public_base_url, args.license, force=not args.if_changed)
        elif args.command == "reconcile":
            res = reconcile_with_gbif(conn, args.dataset_key, args.gbif_api_base)
        else:
            res = run_publication(conn, args.public_r2, "manual", args.public_base_url, args.license,
                                  args.dataset_key, args.gbif_api_base)
    finally:
        conn.close()
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
