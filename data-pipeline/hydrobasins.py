#!/usr/bin/env python3
"""Standard-library HydroBASINS reader, filter, simplifier, and point-in-basin index.

Reads the official HydroSHEDS distribution (``hybas_<region>_lev<NN>_v1c.zip``
containing an ESRI Shapefile + dBASE table), a bare ``.shp``/``.dbf`` pair, or a
GeoJSON FeatureCollection, and normalizes features to the property names used by
the basinmap.org release pipeline:

    hybas_id, pfaf_id, next_down, main_bas, sub_area_km2, up_area_km2, basin_name

No GDAL/Shapely dependency is required, so the pilot pipeline keeps the
"Python 3.9+ standard library only" invariant from AGENTS.md.

HydroBASINS citation: Lehner, B., Grill G. (2013). Global river hydrography and
network routing: baseline data and new approaches to study the world's large river
systems. Hydrological Processes, 27(15): 2171-2186. https://www.hydrosheds.org
"""

from __future__ import annotations

import io
import json
import math
import struct
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

HYDROBASINS_DOWNLOAD_TEMPLATE = (
    "https://data.hydrosheds.org/file/HydroBASINS/standard/hybas_{region}_lev{level:02d}_v1c.zip"
)
HYDROBASINS_REGIONS = ("af", "ar", "as", "au", "eu", "gr", "na", "sa", "si")
HYDROBASINS_CITATION = (
    "Lehner, B., Grill G. (2013): Global river hydrography and network routing: baseline data "
    "and new approaches to study the world's large river systems. Hydrological Processes, "
    "27(15): 2171-2186. Data available at https://www.hydrosheds.org"
)

Ring = List[Tuple[float, float]]


# ---------------------------------------------------------------------------
# dBASE (.dbf) and Shapefile (.shp) parsing
# ---------------------------------------------------------------------------

def read_dbf(raw: bytes) -> List[Dict[str, Any]]:
    """Parse a dBASE III table into a list of {FIELD: value} dicts."""
    if len(raw) < 32:
        raise ValueError("DBF file too short")
    num_records, header_len, record_len = struct.unpack("<IHH", raw[4:12])
    fields: List[Tuple[str, str, int, int]] = []
    pos = 32
    while pos < header_len - 1 and raw[pos] != 0x0D:
        desc = raw[pos : pos + 32]
        name = desc[:11].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        ftype = chr(desc[11])
        flen = desc[16]
        fdec = desc[17]
        fields.append((name, ftype, flen, fdec))
        pos += 32

    records: List[Dict[str, Any]] = []
    pos = header_len
    for _ in range(num_records):
        rec = raw[pos : pos + record_len]
        pos += record_len
        if not rec or rec[0:1] == b"*":  # deleted record marker
            records.append({})
            continue
        out: Dict[str, Any] = {}
        fpos = 1
        for name, ftype, flen, fdec in fields:
            val_raw = rec[fpos : fpos + flen]
            fpos += flen
            text = val_raw.decode("latin-1").strip()
            if ftype in ("N", "F"):
                if not text or set(text) <= {"*"}:
                    out[name] = None
                elif fdec == 0 and "." not in text and "e" not in text.lower():
                    out[name] = int(text)
                else:
                    out[name] = float(text)
            elif ftype == "L":
                out[name] = text.upper() in ("Y", "T")
            else:
                out[name] = text
        records.append(out)
    return records


def _ring_signed_area(ring: Sequence[Tuple[float, float]]) -> float:
    area = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def point_in_ring(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _rings_to_geometry(rings: List[Ring]) -> Optional[Dict[str, Any]]:
    """Group shapefile rings (outer = clockwise, holes = counter-clockwise) into GeoJSON."""
    polygons: List[List[Ring]] = []
    holes: List[Ring] = []
    for ring in rings:
        if len(ring) < 4:
            continue
        if _ring_signed_area(ring) <= 0:  # clockwise in y-up lon/lat space -> outer ring
            polygons.append([ring])
        else:
            holes.append(ring)
    if not polygons and holes:  # tolerate inverted winding from non-ESRI writers
        polygons = [[h] for h in holes]
        holes = []
    for hole in holes:
        hx, hy = hole[0]
        target = next((p for p in polygons if point_in_ring(hx, hy, p[0])), None)
        if target is not None:
            target.append(hole)
    if not polygons:
        return None
    as_lists = [[[list(pt) for pt in ring] for ring in poly] for poly in polygons]
    if len(as_lists) == 1:
        return {"type": "Polygon", "coordinates": as_lists[0]}
    return {"type": "MultiPolygon", "coordinates": as_lists}


def read_shp_polygons(raw: bytes) -> List[Optional[Dict[str, Any]]]:
    """Parse Polygon (5) / PolygonZ (15) / PolygonM (25) shapefile records to GeoJSON geometries."""
    if len(raw) < 100 or struct.unpack(">i", raw[0:4])[0] != 9994:
        raise ValueError("Not an ESRI Shapefile (bad file code)")
    pos = 100
    out: List[Optional[Dict[str, Any]]] = []
    while pos + 8 <= len(raw):
        _rec_no, content_words = struct.unpack(">ii", raw[pos : pos + 8])
        pos += 8
        content = raw[pos : pos + content_words * 2]
        pos += content_words * 2
        shape_type = struct.unpack("<i", content[0:4])[0]
        if shape_type == 0:
            out.append(None)
            continue
        if shape_type not in (5, 15, 25):
            raise ValueError(f"Unsupported shapefile geometry type {shape_type} (expected polygons)")
        num_parts, num_points = struct.unpack("<ii", content[36:44])
        parts = list(struct.unpack(f"<{num_parts}i", content[44 : 44 + 4 * num_parts]))
        pts_off = 44 + 4 * num_parts
        coords = struct.unpack(f"<{2 * num_points}d", content[pts_off : pts_off + 16 * num_points])
        parts.append(num_points)
        rings: List[Ring] = []
        for i in range(num_parts):
            start, end = parts[i], parts[i + 1]
            rings.append([(coords[2 * k], coords[2 * k + 1]) for k in range(start, end)])
        out.append(_rings_to_geometry(rings))
    return out


def _normalize_properties(props: Dict[str, Any]) -> Dict[str, Any]:
    lower = {str(k).lower(): v for k, v in props.items()}
    hybas_id = str(int(lower["hybas_id"])) if isinstance(lower.get("hybas_id"), (int, float)) else str(lower["hybas_id"])
    pfaf = lower.get("pfaf_id", "")
    pfaf_id = str(int(pfaf)) if isinstance(pfaf, (int, float)) else str(pfaf)
    sub_area = lower.get("sub_area_km2", lower.get("sub_area", 0.0)) or 0.0
    up_area = lower.get("up_area_km2", lower.get("up_area", 0.0)) or 0.0
    next_down = lower.get("next_down", 0) or 0
    main_bas = lower.get("main_bas", 0) or 0
    return {
        "hybas_id": hybas_id,
        "pfaf_id": pfaf_id,
        "next_down": str(int(next_down)) if isinstance(next_down, (int, float)) else str(next_down),
        "main_bas": str(int(main_bas)) if isinstance(main_bas, (int, float)) else str(main_bas),
        "sub_area_km2": float(sub_area),
        "up_area_km2": float(up_area),
        "basin_name": str(lower.get("basin_name") or f"HydroBASINS {pfaf_id or hybas_id}"),
    }


def load_hydrobasins(path: Path) -> List[Dict[str, Any]]:
    """Load HydroBASINS polygons from a HydroSHEDS .zip, a .shp (+ sibling .dbf), or GeoJSON."""
    suffix = path.suffix.lower()
    if suffix in (".geojson", ".json"):
        fc = json.loads(path.read_text(encoding="utf-8"))
        return [
            {"type": "Feature", "properties": _normalize_properties(f["properties"]), "geometry": f["geometry"]}
            for f in fc.get("features", [])
            if f.get("geometry")
        ]

    if suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            shp_name = next((n for n in names if n.lower().endswith(".shp")), None)
            if shp_name is None:
                raise FileNotFoundError(f"No .shp member found in {path}")
            dbf_name = shp_name[:-4] + ".dbf"
            dbf_match = next((n for n in names if n.lower() == dbf_name.lower()), None)
            if dbf_match is None:
                raise FileNotFoundError(f"No .dbf member matching {shp_name} in {path}")
            shp_raw = zf.read(shp_name)
            dbf_raw = zf.read(dbf_match)
    elif suffix == ".shp":
        shp_raw = path.read_bytes()
        dbf_path = path.with_suffix(".dbf")
        if not dbf_path.exists():
            dbf_path = path.with_suffix(".DBF")
        dbf_raw = dbf_path.read_bytes()
    else:
        raise ValueError(f"Unsupported HydroBASINS source format: {path}")

    geoms = read_shp_polygons(shp_raw)
    records = read_dbf(dbf_raw)
    if len(geoms) != len(records):
        raise ValueError(f"Shapefile/DBF record count mismatch ({len(geoms)} vs {len(records)})")
    features = []
    for geom, rec in zip(geoms, records):
        if geom is None or not rec:
            continue
        features.append({"type": "Feature", "properties": _normalize_properties(rec), "geometry": geom})
    return features


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def iter_polygons(geometry: Dict[str, Any]) -> Iterable[List[Sequence[Sequence[float]]]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if gtype == "Polygon":
        yield coords
    elif gtype == "MultiPolygon":
        for poly in coords:
            yield poly


def geometry_bbox(geometry: Dict[str, Any]) -> List[float]:
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    for poly in iter_polygons(geometry):
        for pt in poly[0] if poly else []:
            x, y = float(pt[0]), float(pt[1])
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
    return [min_x, min_y, max_x, max_y]


def bbox_intersects(a: Sequence[float], b: Sequence[float]) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def point_in_geometry(lon: float, lat: float, geometry: Dict[str, Any]) -> bool:
    for poly in iter_polygons(geometry):
        if poly and point_in_ring(lon, lat, poly[0]):
            if not any(point_in_ring(lon, lat, hole) for hole in poly[1:]):
                return True
    return False


def filter_basins(
    features: List[Dict[str, Any]],
    bbox: Optional[Sequence[float]] = None,
    pfaf_prefixes: Optional[Sequence[str]] = None,
    hybas_ids: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Keep basins intersecting bbox and/or matching Pfafstetter prefixes / explicit IDs."""
    id_set = {str(h) for h in hybas_ids} if hybas_ids else None
    out = []
    for feat in features:
        props = feat["properties"]
        if id_set is not None and props["hybas_id"] not in id_set:
            continue
        if pfaf_prefixes and not any(props["pfaf_id"].startswith(p) for p in pfaf_prefixes):
            continue
        if bbox is not None and not bbox_intersects(geometry_bbox(feat["geometry"]), bbox):
            continue
        out.append(feat)
    return out


def _perp_dist_sq(p: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2


def simplify_ring(ring: Sequence[Sequence[float]], tolerance: float) -> List[List[float]]:
    """Iterative Douglas-Peucker simplification keeping a closed ring of >= 4 points."""
    pts = [(float(p[0]), float(p[1])) for p in ring]
    if tolerance <= 0 or len(pts) <= 5:
        return [list(p) for p in pts]
    closed = pts[0] == pts[-1]
    work = pts[:-1] if closed else pts
    n = len(work)
    # Split the ring at the vertex farthest from the first one so DP has two anchored halves.
    far = max(range(n), key=lambda i: (work[i][0] - work[0][0]) ** 2 + (work[i][1] - work[0][1]) ** 2)
    keep = [False] * n
    keep[0] = keep[far] = True
    tol_sq = tolerance * tolerance
    stack = [(0, far), (far, n)]
    while stack:
        s, e = stack.pop()
        end_pt = work[e % n]
        max_d, idx = -1.0, -1
        for i in range(s + 1, e):
            d = _perp_dist_sq(work[i], work[s], end_pt)
            if d > max_d:
                max_d, idx = d, i
        if idx != -1 and max_d > tol_sq:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    out = [list(work[i]) for i in range(n) if keep[i]]
    if len(out) < 3:
        return [list(p) for p in pts]
    out.append(list(out[0]))
    return out


def simplify_geometry(geometry: Dict[str, Any], tolerance: float) -> Dict[str, Any]:
    polys = []
    for poly in iter_polygons(geometry):
        if not poly:
            continue
        outer = simplify_ring(poly[0], tolerance)
        if len(outer) < 4:
            continue
        rings = [outer]
        for hole in poly[1:]:
            hb = geometry_bbox({"type": "Polygon", "coordinates": [hole]})
            # Drop holes smaller than the tolerance; they are invisible at this zoom.
            if (hb[2] - hb[0]) > tolerance and (hb[3] - hb[1]) > tolerance:
                simplified = simplify_ring(hole, tolerance)
                if len(simplified) >= 4:
                    rings.append(simplified)
        polys.append(rings)
    if not polys:
        return geometry
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


def round_geometry(geometry: Dict[str, Any], ndigits: int = 5) -> Dict[str, Any]:
    def rnd(ring: Sequence[Sequence[float]]) -> List[List[float]]:
        return [[round(float(p[0]), ndigits), round(float(p[1]), ndigits)] for p in ring]

    if geometry["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [rnd(r) for r in geometry["coordinates"]]}
    return {
        "type": "MultiPolygon",
        "coordinates": [[rnd(r) for r in poly] for poly in geometry["coordinates"]],
    }


class BasinIndex:
    """Uniform-grid index for fast point-in-basin assignment over many points."""

    def __init__(self, features: List[Dict[str, Any]], cell_deg: float = 0.25) -> None:
        self.cell = cell_deg
        self.features = features
        self.bboxes = [geometry_bbox(f["geometry"]) for f in features]
        self.grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for idx, bb in enumerate(self.bboxes):
            for gx in range(int(math.floor(bb[0] / cell_deg)), int(math.floor(bb[2] / cell_deg)) + 1):
                for gy in range(int(math.floor(bb[1] / cell_deg)), int(math.floor(bb[3] / cell_deg)) + 1):
                    self.grid[(gx, gy)].append(idx)

    def lookup(self, lon: float, lat: float) -> Optional[int]:
        key = (int(math.floor(lon / self.cell)), int(math.floor(lat / self.cell)))
        for idx in self.grid.get(key, ()):
            bb = self.bboxes[idx]
            if bb[0] <= lon <= bb[2] and bb[1] <= lat <= bb[3]:
                if point_in_geometry(lon, lat, self.features[idx]["geometry"]):
                    return idx
        return None


# ---------------------------------------------------------------------------
# Minimal shapefile writer (used by tests to emulate the HydroSHEDS distribution)
# ---------------------------------------------------------------------------

def write_shapefile_zip(features: List[Dict[str, Any]], zip_path: Path, stem: str) -> None:
    """Write Polygon features to a HydroSHEDS-style zip (stem.shp/.shx/.dbf/.prj)."""
    field_defs = [
        ("HYBAS_ID", "N", 11, 0),
        ("NEXT_DOWN", "N", 11, 0),
        ("MAIN_BAS", "N", 11, 0),
        ("SUB_AREA", "N", 12, 1),
        ("UP_AREA", "N", 12, 1),
        ("PFAF_ID", "N", 12, 0),
    ]
    shp_records: List[bytes] = []
    for rec_no, feat in enumerate(features, start=1):
        rings: List[Ring] = []
        for poly in iter_polygons(feat["geometry"]):
            for r_idx, ring in enumerate(poly):
                pts = [(float(p[0]), float(p[1])) for p in ring]
                if pts[0] != pts[-1]:
                    pts.append(pts[0])
                is_cw = _ring_signed_area(pts) < 0
                if (r_idx == 0) != is_cw:  # outer rings clockwise, holes counter-clockwise
                    pts.reverse()
                rings.append(pts)
        all_pts = [p for r in rings for p in r]
        bb = (
            min(p[0] for p in all_pts),
            min(p[1] for p in all_pts),
            max(p[0] for p in all_pts),
            max(p[1] for p in all_pts),
        )
        parts, acc = [], 0
        for r in rings:
            parts.append(acc)
            acc += len(r)
        content = struct.pack("<i4d2i", 5, *bb, len(rings), len(all_pts))
        content += struct.pack(f"<{len(parts)}i", *parts)
        content += struct.pack(f"<{2 * len(all_pts)}d", *[c for p in all_pts for c in p])
        shp_records.append(struct.pack(">ii", rec_no, len(content) // 2) + content)

    all_bb = [geometry_bbox(f["geometry"]) for f in features]
    file_bb = (
        min(b[0] for b in all_bb),
        min(b[1] for b in all_bb),
        max(b[2] for b in all_bb),
        max(b[3] for b in all_bb),
    )
    body = b"".join(shp_records)
    shp_header = struct.pack(">7i", 9994, 0, 0, 0, 0, 0, (100 + len(body)) // 2)
    shp_header += struct.pack("<2i8d", 1000, 5, *file_bb, 0, 0, 0, 0)
    shp = shp_header + body

    shx_body = b""
    offset = 50
    for rec in shp_records:
        shx_body += struct.pack(">ii", offset, (len(rec) - 8) // 2)
        offset += len(rec) // 2
    shx = struct.pack(">7i", 9994, 0, 0, 0, 0, 0, (100 + len(shx_body)) // 2)
    shx += struct.pack("<2i8d", 1000, 5, *file_bb, 0, 0, 0, 0) + shx_body

    record_len = 1 + sum(f[2] for f in field_defs)
    header_len = 32 + 32 * len(field_defs) + 1
    dbf = io.BytesIO()
    dbf.write(struct.pack("<4BIHH20x", 3, 126, 11, 19, len(features), header_len, record_len))
    for name, ftype, flen, fdec in field_defs:
        dbf.write(struct.pack("<11sc4xBB14x", name.encode("ascii"), ftype.encode("ascii"), flen, fdec))
    dbf.write(b"\x0d")
    for feat in features:
        p = feat["properties"]
        values = [
            int(p["hybas_id"]),
            int(p.get("next_down") or 0),
            int(p.get("main_bas") or 0),
            float(p.get("sub_area_km2", 0.0)),
            float(p.get("up_area_km2", 0.0)),
            int(p["pfaf_id"]),
        ]
        dbf.write(b" ")
        for (name, ftype, flen, fdec), val in zip(field_defs, values):
            text = f"{val:.{fdec}f}" if fdec else str(int(val))
            dbf.write(text.rjust(flen).encode("ascii"))
    dbf.write(b"\x1a")

    prj = (
        'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
        'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
    )
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{stem}.shp", shp)
        zf.writestr(f"{stem}.shx", shx)
        zf.writestr(f"{stem}.dbf", dbf.getvalue())
        zf.writestr(f"{stem}.prj", prj)
