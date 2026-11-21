#!/usr/bin/env python3
"""Vector/raster tiling and CDN-size-aware PMTiles partitioning for basinmap.org releases.

Enforces the hosting plan's Section 4.3 rule that every ``.pmtiles``/COG object on
``data.basinmap.org`` stays below 500 MB (Cloudflare's CDN skips caching objects
above 512 MB and forwards every range request to R2). Archives over the budget are
split first by zoom tier and then spatially (Hilbert-ordered ancestor tiles), and
each partition is described in the release manifest with its own zoom range and
bounds so MapLibre only requests tiles from the partition that covers the view.
"""

from __future__ import annotations

import gzip
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from hydrobasins import geometry_bbox, iter_polygons, simplify_geometry
from pmtiles_writer import (
    COMPRESSION_GZIP,
    COMPRESSION_NONE,
    DEFAULT_EXTENT,
    GEOM_POINT,
    GEOM_POLYGON,
    TILE_TYPE_MVT,
    encode_mvt_tile,
    lat_to_tile_y,
    lon_to_tile_x,
    project_polygon_to_tile,
    write_pmtiles,
    zxy_to_tile_id,
)

# Section 4.3: keep each archive below 500 MB (decimal) so Cloudflare's 512 MB
# ordinary CDN cacheable-file ceiling is never reached.
MAX_ARCHIVE_BYTES = 500_000_000
CDN_CACHE_CEILING_BYTES = 512 * 1024 * 1024
# Bytes reserved per tile for directory entries when estimating archive size.
_DIR_OVERHEAD_PER_TILE = 6
_PARTITION_SAFETY = 0.98

BASIN_SCALAR_FIELDS = {
    "hybas_id": "String",
    "pfaf_id": "String",
    "basin_name": "String",
    "sub_area_km2": "Number",
    "up_area_km2": "Number",
    "occurrence_count": "Number",
    "species_count": "Number",
    "threatened_count": "Number",
    "min_year": "Number",
    "max_year": "Number",
    "shard_path": "String",
    "parent_hybas_id": "String",
    # BasinATLAS v1.0 scalars (Stage 8); present only when the release carries environment data
    "env_air_temp_c": "Number",
    "env_precip_mm": "Number",
    "env_elevation_m": "Number",
    "env_forest_pc": "Number",
    "env_lake_pc": "Number",
    "env_protected_pc": "Number",
    "env_human_footprint": "Number",
}

OCCURRENCE_POINT_FIELDS = {
    "occurrenceID": "String",
    "gbifID": "Number",
    "scientificName": "String",
    "taxonKey": "Number",
    "speciesKey": "Number",  # accepted species (synonyms resolved); Stage 9 search filters on it
    "eventDate": "String",
    "year": "Number",
    "basisOfRecord": "String",
    "hybas_id": "String",
    "license": "String",
    "institutionCode": "String",
    "count": "Number",
}


def tile_bounds(z: int, x: int, y: int) -> List[float]:
    n = 1 << z

    def lat_of(ty: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))

    return [x / n * 360.0 - 180.0, lat_of(y + 1), (x + 1) / n * 360.0 - 180.0, lat_of(y)]


def tile_range(bbox: Sequence[float], z: int) -> Tuple[int, int, int, int]:
    n = 1 << z
    x0 = max(0, min(n - 1, int(math.floor(lon_to_tile_x(bbox[0], z)))))
    x1 = max(0, min(n - 1, int(math.floor(lon_to_tile_x(bbox[2], z)))))
    y0 = max(0, min(n - 1, int(math.floor(lat_to_tile_y(bbox[3], z)))))
    y1 = max(0, min(n - 1, int(math.floor(lat_to_tile_y(bbox[1], z)))))
    return x0, x1, y0, y1


def _assert_scalar(props: Dict[str, Any]) -> None:
    for key, val in props.items():
        if key not in BASIN_SCALAR_FIELDS:
            raise ValueError(f"Non-whitelisted basin tile attribute {key!r} (AGENTS.md invariant 2)")
        if isinstance(val, (list, dict, tuple, set)):
            raise ValueError(f"Basin tile attribute {key!r} must be scalar, got {type(val).__name__}")


# ---------------------------------------------------------------------------
# Tile generation
# ---------------------------------------------------------------------------

def build_basin_tiles(
    features: List[Dict[str, Any]],
    min_zoom: int,
    max_zoom: int,
    simplify_px: float = 1.0,
) -> Dict[Tuple[int, int, int], bytes]:
    """Return gzip-compressed MVT tiles {(z,x,y): bytes} with per-zoom simplified polygons."""
    for feat in features:
        _assert_scalar(feat["properties"])
    bboxes = [geometry_bbox(f["geometry"]) for f in features]
    tiles: Dict[Tuple[int, int, int], bytes] = {}
    for z in range(min_zoom, max_zoom + 1):
        # Tolerance of ``simplify_px`` screen pixels on a 512 px MapLibre vector tile.
        tol_deg = simplify_px * 360.0 / (512.0 * (1 << z))
        per_tile: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
        for idx, feat in enumerate(features):
            geom = simplify_geometry(feat["geometry"], tol_deg) if z < max_zoom else feat["geometry"]
            x0, x1, y0, y1 = tile_range(bboxes[idx], z)
            for tx in range(x0, x1 + 1):
                for ty in range(y0, y1 + 1):
                    rings: List[List[Tuple[int, int]]] = []
                    for poly in iter_polygons(geom):
                        if not poly:
                            continue
                        outer = project_polygon_to_tile(poly[0], z, tx, ty, DEFAULT_EXTENT)
                        if outer is None:
                            continue
                        rings.append(outer)
                        for hole in poly[1:]:
                            ph = project_polygon_to_tile(hole, z, tx, ty, DEFAULT_EXTENT)
                            if ph is not None:
                                rings.append(ph)
                    if rings:
                        per_tile[(tx, ty)].append(
                            {
                                "id": idx + 1,
                                "geom_type": GEOM_POLYGON,
                                "geometry": rings,
                                "properties": dict(feat["properties"]),
                            }
                        )
        for (tx, ty), feats in per_tile.items():
            tiles[(z, tx, ty)] = gzip.compress(encode_mvt_tile({"basins": feats}), mtime=0)
    return tiles


def _thin_key(gbif_id: int) -> int:
    return (gbif_id * 2654435761) & 0xFFFFFFFF


def build_occurrence_tiles(
    points: Sequence[Tuple[float, float, Dict[str, Any]]],
    min_zoom: int,
    point_min_zoom: int,
    max_zoom: int,
    max_points_per_tile: int = 4000,
    bin_shift: int = 6,
    species_bins: bool = False,
    species_bin_shift: int = 4,
) -> Dict[Tuple[int, int, int], bytes]:
    """Aggregated ``occurrence_bins`` below ``point_min_zoom``; record-level ``occurrences`` above.

    Overview zooms carry one point per 1/(2**bin_shift) tile cell with a ``count``
    property, so dense regions never ship every record at low zoom. Detail zooms emit
    one feature per occupied tile pixel (``count`` = records at that pixel); below
    ``max_zoom`` locations are thinned deterministically (by hashed gbifID) to
    ``max_points_per_tile``; ``max_zoom`` keeps every distinct location.

    With ``species_bins`` the overview zooms also carry a ``species_bins`` layer: one point per
    (speciesKey, 1/(2**species_bin_shift) tile cell) with ``speciesKey``, ``scientificName``,
    ``count`` and ``max_year``, so a species search can show every species' footprint at world
    scale (Stage 9). Records not identified to species are left out of this layer.
    """
    tiles: Dict[Tuple[int, int, int], bytes] = {}
    world = [(lon_to_tile_x(lon, 0), lat_to_tile_y(lat, 0)) for lon, lat, _ in points]

    for z in range(min_zoom, min(point_min_zoom, max_zoom + 1)):
        cz = z + bin_shift
        scale = float(1 << cz)
        bins: Dict[Tuple[int, int], List[int]] = defaultdict(lambda: [0, 0])
        for (u, v), (_, _, props) in zip(world, points):
            key = (min(int(u * scale), (1 << cz) - 1), min(int(v * scale), (1 << cz) - 1))
            b = bins[key]
            b[0] += 1
            b[1] = max(b[1], int(props.get("year") or 0))
        per_tile: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
        cell_px = DEFAULT_EXTENT >> bin_shift
        for (cx, cy), (count, max_year) in bins.items():
            tx, ty = cx >> bin_shift, cy >> bin_shift
            lx = (cx - (tx << bin_shift)) * cell_px + cell_px // 2
            ly = (cy - (ty << bin_shift)) * cell_px + cell_px // 2
            per_tile[(tx, ty)].append(
                {
                    "id": cx * (1 << cz) + cy + 1,
                    "geom_type": GEOM_POINT,
                    "geometry": [(lx, ly)],
                    "properties": {"count": count, "max_year": max_year},
                }
            )
        layers_by_tile: Dict[Tuple[int, int], Dict[str, List[Dict[str, Any]]]] = defaultdict(dict)
        for key, feats in per_tile.items():
            layers_by_tile[key]["occurrence_bins"] = feats
        if species_bins:
            sz = z + species_bin_shift
            sscale = float(1 << sz)
            sbins: Dict[Tuple[int, int, int], List[Any]] = {}
            for (u, v), (_, _, props) in zip(world, points):
                sk = int(props.get("speciesKey") or 0)
                if not sk:
                    continue
                cell = (sk, min(int(u * sscale), (1 << sz) - 1), min(int(v * sscale), (1 << sz) - 1))
                b = sbins.get(cell)
                if b is None:
                    sbins[cell] = [1, int(props.get("year") or 0), props.get("scientificName", "")]
                else:
                    b[0] += 1
                    b[1] = max(b[1], int(props.get("year") or 0))
            scell_px = DEFAULT_EXTENT >> species_bin_shift
            sper_tile: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
            for (sk, cx, cy), (count, max_year, name) in sbins.items():
                tx, ty = cx >> species_bin_shift, cy >> species_bin_shift
                lx = (cx - (tx << species_bin_shift)) * scell_px + scell_px // 2
                ly = (cy - (ty << species_bin_shift)) * scell_px + scell_px // 2
                sper_tile[(tx, ty)].append({
                    "id": len(sper_tile[(tx, ty)]) + 1,
                    "geom_type": GEOM_POINT,
                    "geometry": [(lx, ly)],
                    "properties": {"speciesKey": sk, "scientificName": name, "count": count, "max_year": max_year},
                })
            for key, feats in sper_tile.items():
                layers_by_tile[key]["species_bins"] = feats
        for (tx, ty), layers in layers_by_tile.items():
            tiles[(z, tx, ty)] = gzip.compress(encode_mvt_tile(layers), mtime=0)

    for z in range(max(min_zoom, point_min_zoom), max_zoom + 1):
        scale = float(1 << z)
        n = 1 << z
        per_tile_idx: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for i, (u, v) in enumerate(world):
            per_tile_idx[(min(int(u * scale), n - 1), min(int(v * scale), n - 1))].append(i)
        for (tx, ty), idxs in per_tile_idx.items():
            # Records on the same tile pixel are drawn on top of each other: emit one feature per
            # pixel (deterministic representative = lowest hashed gbifID) carrying `count`. This keeps
            # stacked site-level coordinates (e.g. 28k records at one point) from bloating tiles.
            by_pixel: Dict[Tuple[int, int], List[int]] = defaultdict(list)
            for i in idxs:
                u, v = world[i]
                lx = max(0, min(DEFAULT_EXTENT - 1, round((u * scale - tx) * DEFAULT_EXTENT)))
                ly = max(0, min(DEFAULT_EXTENT - 1, round((v * scale - ty) * DEFAULT_EXTENT)))
                by_pixel[(lx, ly)].append(i)
            reps = []
            for (lx, ly), members in by_pixel.items():
                rep = min(members, key=lambda i: _thin_key(int(points[i][2]["gbifID"])))
                reps.append((rep, len(members), lx, ly))
            if z < max_zoom and len(reps) > max_points_per_tile:
                reps = sorted(reps, key=lambda r: _thin_key(int(points[r[0]][2]["gbifID"])))[:max_points_per_tile]
            feats = []
            for rep, count, lx, ly in reps:
                props = {k: v for k, v in points[rep][2].items() if k in OCCURRENCE_POINT_FIELDS and v not in (None, "")}
                props["count"] = count
                feats.append(
                    {
                        "id": int(points[rep][2]["gbifID"]),
                        "geom_type": GEOM_POINT,
                        "geometry": [(lx, ly)],
                        "properties": props,
                    }
                )
            tiles[(z, tx, ty)] = gzip.compress(encode_mvt_tile({"occurrences": feats}), mtime=0)
    return tiles


# ---------------------------------------------------------------------------
# Size-budgeted partitioning
# ---------------------------------------------------------------------------

def _estimate_bytes(keys: Iterable[Tuple[int, int, int]], tiles: Dict[Tuple[int, int, int], bytes]) -> int:
    return sum(len(tiles[k]) + _DIR_OVERHEAD_PER_TILE for k in keys) + 16384


def plan_partitions(
    tiles: Dict[Tuple[int, int, int], bytes],
    max_bytes: int = MAX_ARCHIVE_BYTES,
) -> List[List[Tuple[int, int, int]]]:
    """Group tile keys into archives that each stay below ``max_bytes``."""
    budget = int(max_bytes * _PARTITION_SAFETY)
    keys = list(tiles.keys())
    if _estimate_bytes(keys, tiles) <= budget:
        return [keys]
    for k in keys:
        if len(tiles[k]) + 16384 > budget:
            raise ValueError(f"Tile {k} alone exceeds the {max_bytes}-byte archive budget")

    zooms = sorted({k[0] for k in keys})
    for split_z in range(zooms[0] + 1, zooms[-1] + 1):
        low = [k for k in keys if k[0] < split_z]
        if _estimate_bytes(low, tiles) > budget:
            break  # even the overview tier is too big; fall through to error below
        groups: Dict[Tuple[int, int], List[Tuple[int, int, int]]] = defaultdict(list)
        for k in keys:
            if k[0] >= split_z:
                shift = k[0] - split_z
                groups[(k[1] >> shift, k[2] >> shift)].append(k)
        sizes = {anc: _estimate_bytes(g, tiles) - 16384 for anc, g in groups.items()}
        if any(s + 16384 > budget for s in sizes.values()):
            continue
        parts: List[List[Tuple[int, int, int]]] = [low] if low else []
        current: List[Tuple[int, int, int]] = []
        current_size = 16384
        for anc in sorted(groups, key=lambda a: zxy_to_tile_id(split_z, a[0], a[1])):
            if current and current_size + sizes[anc] > budget:
                parts.append(current)
                current, current_size = [], 16384
            current.extend(groups[anc])
            current_size += sizes[anc]
        if current:
            parts.append(current)
        return parts
    raise ValueError(
        "Unable to partition archive below the CDN budget; lower max zoom, thin points, or "
        "split the source by region before tiling"
    )


def write_partitioned_pmtiles(
    tiles: Dict[Tuple[int, int, int], bytes],
    out_dir: Path,
    base_name: str,
    url_prefix: str,
    metadata: Dict[str, Any],
    max_bytes: int = MAX_ARCHIVE_BYTES,
    tile_type: int = TILE_TYPE_MVT,
    center_zoom: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Write ``base_name.pmtiles`` (or ``base_name-pNN.pmtiles`` parts) and return partition specs."""
    from build_release import sha256_file  # local import avoids a cycle at module import

    groups = plan_partitions(tiles, max_bytes)
    specs: List[Dict[str, Any]] = []
    for idx, keys in enumerate(groups):
        name = f"{base_name}.pmtiles" if len(groups) == 1 else f"{base_name}-p{idx:02d}.pmtiles"
        sub = {k: tiles[k] for k in keys}
        z_min = min(k[0] for k in keys)
        z_max = max(k[0] for k in keys)
        tb = [tile_bounds(*k) for k in keys]
        bounds = [
            round(max(-180.0, min(b[0] for b in tb)), 5),
            round(max(-85.0511, min(b[1] for b in tb)), 5),
            round(min(180.0, max(b[2] for b in tb)), 5),
            round(min(85.0511, max(b[3] for b in tb)), 5),
        ]
        center = [round((bounds[0] + bounds[2]) / 2, 5), round((bounds[1] + bounds[3]) / 2, 5)]
        md = dict(metadata)
        if "vector_layers" in md:
            md["vector_layers"] = [dict(vl, minzoom=z_min, maxzoom=z_max) for vl in md["vector_layers"]]
        out_path = out_dir / name
        stats = write_pmtiles(
            output_path=out_path,
            tiles_by_zxy=sub,
            metadata=md,
            min_zoom=z_min,
            max_zoom=z_max,
            bounds=bounds,
            center=center,
            center_zoom=max(z_min, min(z_max, center_zoom if center_zoom is not None else z_min)),
            tile_type=tile_type,
            tile_compression=COMPRESSION_GZIP if tile_type == TILE_TYPE_MVT else COMPRESSION_NONE,
            tiles_precompressed=True,
        )
        size = out_path.stat().st_size
        if size >= max_bytes:
            raise RuntimeError(f"{out_path} is {size} bytes, above the {max_bytes}-byte CDN budget")
        specs.append(
            {
                "path": f"{url_prefix}/{name}",
                "sha256": sha256_file(out_path),
                "bytes": size,
                "min_zoom": z_min,
                "max_zoom": z_max,
                "bounds": bounds,
                "tiles": stats["num_addressed_tiles"],
                "leaf_directories": stats["num_leaf_directories"],
            }
        )
    return specs
