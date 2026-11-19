#!/usr/bin/env python3
"""Darwin Core Archive (.zip) exporter, structural validator, and GBIF UAT CLI.

Follows Sections 5.1 and 5.2 of hosting-and-gbif-publishing-plan.md:
  - Exports approved observations (using generalized coordinates for sensitive taxa)
    and permanent /media/<uuid>.jpg public derivatives into dwca-latest.zip
  - Maps Simple Multimedia terms (license, rightsHolder, identifier)
  - Validates archive XML, column mapping, and referential integrity
  - Supports GBIF Data Validator API (api.gbif.org) and UAT registration (api.gbif-uat.org)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sqlite3
import sys
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
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
      <onlineUrl>https://basinmap.org</onlineUrl>
    </creator>
    <metadataProvider>
      <organizationName>basinmap.org Open Web-GIS Project</organizationName>
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
      <electronicMailAddress>stewardship@basinmap.org</electronicMailAddress>
      <onlineUrl>https://basinmap.org</onlineUrl>
    </contact>
  </dataset>
</eml:eml>
"""


def export_dwca(
    conn: sqlite3.Connection,
    public_r2_dir: Path,
    public_base_url: str = "https://data.basinmap.org",
    dataset_license: str = "CC-BY 4.0",
) -> Dict[str, Any]:
    if dataset_license not in LICENSE_URI_MAP:
        raise ValueError(f"Unsupported dataset_license: {dataset_license!r}")

    license_uri = LICENSE_URI_MAP[dataset_license]
    rows = conn.execute(
        """
        SELECT * FROM observations
        WHERE review_status = 'approved' AND license = ?
        ORDER BY occurrence_id ASC
        """,
        (dataset_license,),
    ).fetchall()

    occ_lines = ["\t".join(col for col, _ in OCCURRENCE_FIELDS)]
    mm_lines = ["\t".join(col for col, _ in MULTIMEDIA_FIELDS)]

    lats: List[float] = []
    lons: List[float] = []
    dates: List[str] = []
    occ_ids: List[str] = []
    media_count = 0

    clean_base = public_base_url.rstrip("/")

    for row in rows:
        occ_ids.append(row["occurrence_id"])
        lats.append(float(row["public_latitude"]))
        lons.append(float(row["public_longitude"]))
        dates.append(str(row["event_date"])[:10])

        occ_row = [
            row["occurrence_id"],
            row["basis_of_record"],
            row["scientific_name"],
            row["taxon_key"],
            row["kingdom"],
            row["family"],
            row["event_date"],
            row["public_latitude"],
            row["public_longitude"],
            row["geodetic_datum"],
            row["public_coordinate_uncertainty_m"],
            row["information_withheld"],
            row["locality"],
            row["recorded_by"],
            row["recorded_by_id"],
            row["identified_by"],
            row["identified_by_id"],
            license_uri,
            row["rights_holder"],
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
            media_url = f"{clean_base}/{m['public_r2_key'].lstrip('/')}"
            mm_row = [
                row["occurrence_id"],
                media_url,
                "StillImage",
                "image/jpeg" if m["public_r2_key"].endswith(".jpg") else m["detected_mime_type"],
                m["created_at"],
                row["recorded_by"],
                LICENSE_URI_MAP.get(m["license"], license_uri),
                m["rights_holder"],
            ]
            mm_lines.append("\t".join(_tsv_cell(v) for v in mm_row))

    bbox = [
        min(lons) if lons else -123.8,
        min(lats) if lats else 43.5,
        max(lons) if lons else -121.5,
        max(lats) if lats else 46.2,
    ]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    min_date = min(dates) if dates else today
    max_date = max(dates) if dates else today
    export_id = f"basinmap-dwca-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"

    eml_xml = _build_eml_xml(export_id, dataset_license, bbox, min_date, max_date, today)
    meta_xml = _build_meta_xml()
    occ_txt = "\n".join(occ_lines) + "\n"
    mm_txt = "\n".join(mm_lines) + "\n"

    exports_dir = public_r2_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    zip_path = exports_dir / "dwca-latest.zip"
    tmp_zip = exports_dir / "dwca-latest.zip.tmp"

    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fname, content in (
            ("eml.xml", eml_xml),
            ("meta.xml", meta_xml),
            ("occurrence.txt", occ_txt),
            ("multimedia.txt", mm_txt),
        ):
            zi = zipfile.ZipInfo(fname, date_time=(2026, 11, 19, 12, 0, 0))
            zf.writestr(zi, content.encode("utf-8"))

    os.replace(tmp_zip, zip_path)
    (exports_dir / "eml.xml").write_text(eml_xml, encoding="utf-8")

    zip_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    now = utc_now_iso()

    with conn:
        for oid in occ_ids:
            row = conn.execute("SELECT * FROM observations WHERE occurrence_id = ?", (oid,)).fetchone()
            new_rev = int(row["revision_number"]) + 1
            conn.execute(
                """
                UPDATE observations
                SET publication_status = 'exported',
                    exported_dwca_version = ?,
                    revision_number = ?,
                    updated_at = ?
                WHERE occurrence_id = ?
                """,
                (export_id, new_rev, now, oid),
            )
            snap = json.dumps({"occurrence_id": oid, "exported_dwca_version": export_id})
            actor_id = row["reviewer_user_id"] or row["submitter_user_id"]
            conn.execute(
                """
                INSERT INTO observation_revisions
                  (occurrence_id, revision_number, actor_user_id, change_type, snapshot_json, notes, created_at)
                VALUES (?, ?, ?, 'exported', ?, ?, ?)
                """,
                (oid, new_rev, actor_id, snap, f"Exported in DwC-A {export_id}", now),
            )

        conn.execute(
            """
            INSERT INTO publication_exports
              (export_id, archive_r2_key, dataset_license, record_count, media_count, sha256, validator_status, created_at)
            VALUES (?, 'exports/dwca-latest.zip', ?, ?, ?, ?, 'local_validated', ?)
            """,
            (export_id, dataset_license, len(occ_ids), media_count, zip_sha, now),
        )

    validation = validate_dwca_zip(zip_path)
    return {
        "export_id": export_id,
        "zip_path": str(zip_path),
        "archive_r2_key": "exports/dwca-latest.zip",
        "dataset_license": dataset_license,
        "record_count": len(occ_ids),
        "media_count": media_count,
        "sha256": zip_sha,
        "validation": validation,
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


def build_gbif_validator_request(zip_path: Path) -> Dict[str, Any]:
    val = validate_dwca_zip(zip_path)
    return {
        "endpoint": "https://api.gbif.org/v1/validation",
        "method": "POST",
        "file_path": str(zip_path),
        "file_bytes": zip_path.stat().st_size,
        "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
        "local_validation": val,
    }


def submit_to_gbif_validator(zip_path: Path, dry_run: bool = False) -> Dict[str, Any]:
    req_info = build_gbif_validator_request(zip_path)
    if dry_run:
        return {"dry_run": True, **req_info}

    boundary = f"----BasinmapBoundary{uuid.uuid4().hex}"
    data = zip_path.read_bytes()
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode("ascii"))
    body.write(
        b'Content-Disposition: form-data; name="file"; filename="dwca-latest.zip"\r\n'
        b"Content-Type: application/zip\r\n\r\n"
    )
    body.write(data)
    body.write(f"\r\n--{boundary}--\r\n".encode("ascii"))

    req = urllib.request.Request(
        req_info["endpoint"],
        data=body.getvalue(),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return {
            "dry_run": False,
            "status": resp.status,
            "response": json.loads(resp.read().decode("utf-8")),
        }


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
    parser = argparse.ArgumentParser(description="Export and validate Darwin Core Archives for GBIF.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_exp = sub.add_parser("export", help="Build and validate dwca-latest.zip from SQLite/D1")
    p_exp.add_argument("--db", type=Path, default=ROOT_DIR / ".local-data" / "basinmap.sqlite3")
    p_exp.add_argument("--public-r2", type=Path, default=ROOT_DIR / "public-r2")
    p_exp.add_argument("--public-base-url", default="https://data.basinmap.org")
    p_exp.add_argument("--license", default="CC-BY 4.0", choices=sorted(LICENSE_URI_MAP))

    p_val = sub.add_parser("validate-gbif", help="Validate dwca-latest.zip against GBIF Data Validator API")
    p_val.add_argument("--zip", type=Path, default=ROOT_DIR / "public-r2" / "exports" / "dwca-latest.zip")
    p_val.add_argument("--dry-run", action="store_true")

    p_uat = sub.add_parser("register-uat", help="Register dataset + DwC-A endpoint on api.gbif-uat.org")
    p_uat.add_argument("--org-key", default="00000000-0000-0000-0000-000000000001")
    p_uat.add_argument("--installation-key", default="00000000-0000-0000-0000-000000000002")
    p_uat.add_argument("--dwca-url", default="https://data.basinmap.org/exports/dwca-latest.zip")
    p_uat.add_argument("--dry-run", action="store_true")

    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.command == "export":
        conn = init_db(args.db)
        try:
            res = export_dwca(conn, args.public_r2, args.public_base_url, args.license)
        finally:
            conn.close()
        print(json.dumps(res, indent=2))
        return 0
    if args.command == "validate-gbif":
        res = submit_to_gbif_validator(args.zip, dry_run=args.dry_run)
        print(json.dumps(res, indent=2))
        return 0
    if args.command == "register-uat":
        res = register_on_gbif_uat(args.org_key, args.installation_key, args.dwca_url, dry_run=args.dry_run)
        print(json.dumps(res, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
