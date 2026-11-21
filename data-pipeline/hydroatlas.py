#!/usr/bin/env python3
"""BasinATLAS v1.0 (HydroATLAS) sub-basin attributes for basinmap releases (Stage 8).

BasinATLAS adds 281 hydro-environmental attributes to every HydroBASINS sub-basin at levels 1-12
(Linke et al. 2019, Scientific Data 6: 283, doi:10.1038/s41597-019-0300-6; CC BY 4.0). Column names
follow ``<variable>_<unit>_<spatial extent><dimension>`` (HydroATLAS TechDoc v1.0, Tables 2-4):
spatial ``s`` = in the sub-basin, ``p`` = at the pour point, ``u`` = whole upstream watershed;
dimension ``01``-``12`` = month, ``yr`` = annual, ``av``/``se``/``mj`` = average / spatial extent /
spatial majority.

Only CC BY 4.0 attributes are extracted (SoilGrids soil texture is ODbL 1.0 and is excluded).
Values are kept exactly as stored, plus a ``scale`` divisor where the TechDoc states one
(temperature is stored in tenths of a degree). Every extract also records the observed min/max of
each column so other storage factors can be checked against the BasinATLAS Catalog before display.

    python3 data-pipeline/hydroatlas.py fetch   --workdir data-pipeline/work/hydroatlas
    python3 data-pipeline/hydroatlas.py extract --source data-pipeline/work/hydroatlas/BasinATLAS_Data_v10_shp.zip \\
        --levels 3 6 8 --out-dir data-pipeline/work/hydroatlas
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent

BASINATLAS_URL = "https://figshare.com/ndownloader/files/20087237"  # BasinATLAS_Data_v10_shp.zip
BASINATLAS_FILENAME = "BasinATLAS_Data_v10_shp.zip"
BASINATLAS_MD5 = "ce5013294bdcb6f884ff6cdf1a1acae0"  # Figshare computed_md5
BASINATLAS_BYTES = 4_276_492_333
BASINATLAS_DOI = "10.6084/m9.figshare.9890531"
BASINATLAS_CITATION = (
    "Linke, S., Lehner, B., Ouellet Dallaire, C., Ariwi, J., Grill, G., Anand, M., Beames, P., "
    "Burchard-Levine, V., Maxwell, S., Moidu, H., Tan, F., Thieme, M. (2019). Global hydro-environmental "
    "sub-basin and river reach characteristics at high spatial resolution. Scientific Data 6: 283. "
    "doi:10.1038/s41597-019-0300-6"
)
BASINATLAS_LICENSE = "CC BY 4.0"
USER_AGENT = "basinmap-pipeline/1.0 (+https://basinmap.org)"

MONTHS = [f"{m:02d}" for m in range(1, 13)]

# (output key, BasinATLAS column, unit, scale divisor or None if the storage factor is unverified,
#  source dataset). Scalars are small enough for vector-tile properties; monthly series go to shards.
SCALAR_FIELDS: List[Tuple[str, str, str, Optional[int], str]] = [
    ("air_temp_c", "tmp_dc_syr", "degrees C", 10, "WorldClim v1.4"),
    ("precip_mm", "pre_mm_syr", "mm per year", None, "WorldClim v1.4"),
    ("discharge_m3s", "dis_m3_pyr", "m3/s at pour point", None, "WaterGAP v2.2"),
    ("elevation_m", "ele_mt_sav", "m a.s.l.", None, "EarthEnv-DEM90"),
    ("forest_pc", "for_pc_sse", "percent cover", None, "GLC2000"),
    ("wetland_pc", "wet_pc_sg1", "percent cover (GLWD class group 1)", None, "GLWD"),
    ("lake_pc", "lka_pc_sse", "percent cover (limnicity)", None, "HydroLAKES"),
    ("protected_pc", "pac_pc_sse", "percent cover", None, "WDPA"),
    ("human_footprint", "hft_ix_s09", "index (2009)", None, "Human Footprint v2"),
    ("population_km2", "ppd_pk_sav", "people per km2", None, "GPW v4"),
    ("regulation_pc", "dor_pc_pva", "degree of regulation at pour point", None, "GRanD / HydroSHEDS"),
]
CLASS_FIELDS: List[Tuple[str, str, str]] = [
    ("land_cover_class", "glc_cl_smj", "GLC2000 class (spatial majority)"),
    ("climate_zone_class", "clz_cl_smj", "GEnS climate zone (spatial majority)"),
    ("biome_class", "tbi_cl_smj", "TEOW terrestrial biome (spatial majority)"),
    ("ecoregion_class", "tec_cl_smj", "TEOW terrestrial ecoregion (spatial majority)"),
]
MONTHLY_FIELDS: List[Tuple[str, str, str, Optional[int]]] = [
    ("air_temp_c", "tmp_dc_s", "degrees C", 10),
    ("precip_mm", "pre_mm_s", "mm per month", None),
]


def wanted_columns() -> List[str]:
    cols = ["HYBAS_ID"] + [c for _, c, _, _, _ in SCALAR_FIELDS] + [c for _, c, _ in CLASS_FIELDS]
    for _, prefix, _, _ in MONTHLY_FIELDS:
        cols += [f"{prefix}{m}" for m in MONTHS]
    return cols


def field_spec() -> Dict[str, Any]:
    """Units and storage factors written into every release that carries BasinATLAS values."""
    return {
        "scalars": {k: {"column": c, "unit": u, "scale": s, "source": src} for k, c, u, s, src in SCALAR_FIELDS},
        "classes": {k: {"column": c, "description": d} for k, c, d in CLASS_FIELDS},
        "monthly": {k: {"columns": f"{p}01..{p}12", "unit": u, "scale": s} for k, p, u, s in MONTHLY_FIELDS},
        "scale_note": ("value / scale gives the unit; scale null means the storage factor is not stated in the "
                       "HydroATLAS TechDoc and must be checked in the BasinATLAS Catalog sheet before display"),
        "spatial_note": "s = whole sub-basin, p = at the sub-basin pour point (HydroATLAS TechDoc v1.0, Table 3)",
        "source": {"name": "BasinATLAS v1.0 (HydroATLAS)", "doi": BASINATLAS_DOI, "license": BASINATLAS_LICENSE,
                   "citation": BASINATLAS_CITATION,
                   "excluded": "SoilGrids1km soil texture (ODbL 1.0) is not extracted"},
    }


# --- Streaming dBASE reader (only the requested columns, constant memory) ---------------------

def iter_dbf_columns(fh: BinaryIO, columns: Sequence[str]) -> Iterator[Dict[str, Any]]:
    """Yield {column: value} for each record of a dBASE III file, parsing only ``columns``.

    Column matching is case-insensitive; numeric fields become int/float, blanks become None.
    """
    head = fh.read(32)
    if len(head) < 32:
        raise ValueError("DBF file too short")
    num_records, header_len, record_len = struct.unpack("<IHH", head[4:12])
    desc = fh.read(header_len - 32)
    want = {c.lower(): c for c in columns}
    picks: List[Tuple[str, str, int, int, int]] = []  # (key, type, offset, length, decimals)
    pos, offset = 0, 1
    while pos + 32 <= len(desc) and desc[pos] != 0x0D:
        d = desc[pos:pos + 32]
        name = d[:11].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        ftype, flen, fdec = chr(d[11]), d[16], d[17]
        if name.lower() in want:
            picks.append((want[name.lower()], ftype, offset, flen, fdec))
        offset += flen
        pos += 32
    missing = set(want.values()) - {p[0] for p in picks}
    if missing:
        raise KeyError(f"DBF is missing columns: {sorted(missing)}")
    for _ in range(num_records):
        rec = fh.read(record_len)
        if len(rec) < record_len:
            break
        if rec[:1] == b"*":
            continue
        out: Dict[str, Any] = {}
        for key, ftype, off, flen, fdec in picks:
            text = rec[off:off + flen].decode("latin-1").strip()
            if ftype in ("N", "F"):
                if not text or set(text) <= {"*"}:
                    out[key] = None
                elif fdec == 0 and "." not in text and "e" not in text.lower():
                    out[key] = int(text)
                else:
                    out[key] = float(text)
            else:
                out[key] = text
        yield out


def _level_member(names: Sequence[str], level: int) -> str:
    pat = re.compile(rf"(^|/)BasinATLAS_v10_lev{level:02d}\.dbf$", re.IGNORECASE)
    hits = [n for n in names if pat.search(n)]
    if not hits:
        raise FileNotFoundError(f"No BasinATLAS_v10_lev{level:02d}.dbf in the archive")
    return hits[0]


def extract_level(source: Path, level: int) -> Dict[str, Any]:
    """Read one level's attributes from the shapefile zip (or a directory holding the .dbf)."""
    cols = wanted_columns()
    rows: Dict[str, Dict[str, Any]] = {}
    ranges: Dict[str, List[float]] = {}

    def consume(stream: BinaryIO) -> None:
        for rec in iter_dbf_columns(stream, cols):
            hid = rec.get("HYBAS_ID")
            if hid is None:
                continue
            scalars = {k: rec[c] for k, c, _, _, _ in SCALAR_FIELDS}
            classes = {k: rec[c] for k, c, _ in CLASS_FIELDS}
            monthly = {k: [rec[f"{p}{m}"] for m in MONTHS] for k, p, _, _ in MONTHLY_FIELDS}
            for k, v in scalars.items():
                if isinstance(v, (int, float)):
                    r = ranges.setdefault(k, [v, v])
                    r[0], r[1] = min(r[0], v), max(r[1], v)
            rows[str(int(hid))] = {"s": scalars, "c": classes, "m": monthly}

    if source.is_dir():
        dbfs = [p for p in source.rglob("*.dbf") if re.search(rf"lev{level:02d}\.dbf$", p.name, re.IGNORECASE)]
        if not dbfs:
            raise FileNotFoundError(f"No lev{level:02d} .dbf under {source}")
        with dbfs[0].open("rb") as fh:
            consume(fh)
    else:
        with zipfile.ZipFile(source) as zf:
            with zf.open(_level_member(zf.namelist(), level)) as fh:
                consume(fh)
    return {"schema": "basinmap-basinatlas/1", "level": level, "fields": field_spec(),
            "observed_ranges": {k: v for k, v in sorted(ranges.items())},
            "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "count": len(rows), "basins": rows}


def load_attributes(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def scaled(value: Any, scale: Optional[int]) -> Any:
    if value is None or not scale:
        return value
    return round(value / scale, 2)


def basin_environment(attrs: Dict[str, Any], hybas_id: str) -> Optional[Dict[str, Any]]:
    """Shard-ready environment block for one basin (scaled where the TechDoc gives the factor)."""
    row = attrs.get("basins", {}).get(str(hybas_id))
    if row is None:
        return None
    spec = attrs["fields"]
    return {
        "scalars": {k: scaled(v, spec["scalars"][k]["scale"]) for k, v in row["s"].items()},
        "classes": row["c"],
        "monthly": {k: [scaled(v, spec["monthly"][k]["scale"]) for v in vals] for k, vals in row["m"].items()},
    }


def tile_scalars(attrs: Optional[Dict[str, Any]], hybas_id: str) -> Dict[str, Any]:
    """Small numeric properties for vector tiles (choropleth metrics)."""
    if not attrs:
        return {}
    env = basin_environment(attrs, hybas_id)
    if not env:
        return {}
    keep = ("air_temp_c", "precip_mm", "elevation_m", "forest_pc", "lake_pc", "protected_pc", "human_footprint")
    return {f"env_{k}": env["scalars"][k] for k in keep if env["scalars"].get(k) is not None}


def download(url: str, dest: Path, expected_md5: Optional[str] = None, expected_bytes: Optional[int] = None,
             urlopen: Any = urllib.request.urlopen, retries: int = 3) -> Dict[str, Any]:
    """Stream ``url`` to ``dest`` with MD5 + SHA-256; reuse an existing file whose MD5 matches."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and expected_md5:
        md5 = hashlib.md5()
        with dest.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                md5.update(chunk)
        if md5.hexdigest() == expected_md5:
            return {"path": str(dest), "reused": True, "md5": expected_md5, "bytes": dest.stat().st_size}
    for attempt in range(retries + 1):
        try:
            md5, sha = hashlib.md5(), hashlib.sha256()
            total, tmp = 0, dest.with_suffix(dest.suffix + ".part")
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=300) as resp, tmp.open("wb") as out:
                last = time.monotonic()
                for chunk in iter(lambda: resp.read(1 << 22), b""):
                    md5.update(chunk)
                    sha.update(chunk)
                    out.write(chunk)
                    total += len(chunk)
                    if time.monotonic() - last > 30:
                        print(f"[hydroatlas] {total / 1e9:.2f} GB", file=sys.stderr, flush=True)
                        last = time.monotonic()
            if expected_bytes and total != expected_bytes:
                raise IOError(f"size {total} != expected {expected_bytes}")
            if expected_md5 and md5.hexdigest() != expected_md5:
                raise IOError(f"md5 {md5.hexdigest()} != expected {expected_md5}")
            tmp.replace(dest)
            return {"path": str(dest), "reused": False, "md5": md5.hexdigest(), "sha256": sha.hexdigest(),
                    "bytes": total}
        except Exception:
            if attempt == retries:
                raise
            time.sleep(min(60, 5 * 2 ** attempt))
    raise RuntimeError("unreachable")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch", help=f"Download {BASINATLAS_FILENAME} (4.3 GB) from Figshare and verify its MD5")
    f.add_argument("--workdir", type=Path, default=SCRIPT_DIR / "work" / "hydroatlas")
    e = sub.add_parser("extract", help="Write basinatlas_levNN.json (selected CC BY columns) per level")
    e.add_argument("--source", type=Path, required=True, help="BasinATLAS shp zip or a folder with the .dbf files")
    e.add_argument("--levels", type=int, nargs="+", default=[3, 6, 8])
    e.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.command == "fetch":
        res = download(BASINATLAS_URL, args.workdir / BASINATLAS_FILENAME, BASINATLAS_MD5, BASINATLAS_BYTES)
        print(json.dumps(res, indent=2))
        return 0
    args.out_dir.mkdir(parents=True, exist_ok=True)
    summary = {}
    for lvl in args.levels:
        data = extract_level(args.source, lvl)
        out = args.out_dir / f"basinatlas_lev{lvl:02d}.json"
        out.write_text(json.dumps(data, separators=(",", ":")) + "\n", "utf-8")
        summary[lvl] = {"file": str(out), "basins": data["count"], "observed_ranges": data["observed_ranges"]}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
