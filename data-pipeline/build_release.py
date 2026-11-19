#!/usr/bin/env python3
"""Reproducible spatial release builder for basinmap.org.

Reads GBIF download metadata, HydroBASINS polygons, and GBIF occurrence records,
performs the basin spatial join and pre-aggregation, builds validated PMTiles v3
vector archives and per-basin JSON shards into an immutable release directory,
and updates releases/manifest.json last.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pmtiles_writer import (  # noqa: E402
    DEFAULT_EXTENT,
    GEOM_POINT,
    GEOM_POLYGON,
    encode_mvt_tile,
    inspect_pmtiles,
    lat_to_tile_y,
    lon_to_tile_x,
    project_polygon_to_tile,
    write_pmtiles,
)

THREATENED_CATEGORIES = {"CR", "EN", "VU", "NT"}
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
MUTABLE_CACHE_CONTROL = "public, max-age=60, must-revalidate"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def point_in_ring(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        intersects = ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) if (yj - yi) != 0 else 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def point_in_geometry(lon: float, lat: float, geometry: Dict[str, Any]) -> bool:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if gtype == "Polygon":
        if not coords or not point_in_ring(lon, lat, coords[0]):
            return False
        for hole in coords[1:]:
            if point_in_ring(lon, lat, hole):
                return False
        return True
    if gtype == "MultiPolygon":
        for poly in coords:
            if poly and point_in_ring(lon, lat, poly[0]):
                if not any(point_in_ring(lon, lat, hole) for hole in poly[1:]):
                    return True
        return False
    return False


def geometry_bbox(geometry: Dict[str, Any]) -> List[float]:
    xs: List[float] = []
    ys: List[float] = []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if gtype == "Polygon":
        for ring in coords:
            for pt in ring:
                xs.append(float(pt[0]))
                ys.append(float(pt[1]))
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                for pt in ring:
                    xs.append(float(pt[0]))
                    ys.append(float(pt[1]))
    return [min(xs), min(ys), max(xs), max(ys)]


def _polygon_rings_for_tile(
    geometry: Dict[str, Any],
    zoom: int,
    tx: int,
    ty: int,
) -> List[List[Tuple[int, int]]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])
    polygons = [coords] if gtype == "Polygon" else (coords if gtype == "MultiPolygon" else [])
    rings_out: List[List[Tuple[int, int]]] = []
    for poly in polygons:
        if not poly:
            continue
        clipped_ext = project_polygon_to_tile(poly[0], zoom, tx, ty, DEFAULT_EXTENT)
        if clipped_ext is not None:
            rings_out.append(clipped_ext)
            for hole in poly[1:]:
                clipped_hole = project_polygon_to_tile(hole, zoom, tx, ty, DEFAULT_EXTENT)
                if clipped_hole is not None:
                    rings_out.append(clipped_hole)
    return rings_out


def build_basins_pmtiles(
    output_path: Path,
    basin_features: List[Dict[str, Any]],
    bounds: List[float],
    center: List[float],
    attribution: str,
    min_zoom: int = 0,
    max_zoom: int = 8,
) -> Dict[str, Any]:
    """Generate basins.pmtiles containing scalar summary attributes only."""
    tiles_by_zxy: Dict[Tuple[int, int, int], bytes] = {}
    min_lon, min_lat, max_lon, max_lat = bounds

    for z in range(min_zoom, max_zoom + 1):
        n = 1 << z
        x_start = max(0, min(n - 1, int(math.floor(lon_to_tile_x(min_lon, z)))))
        x_end = max(0, min(n - 1, int(math.floor(lon_to_tile_x(max_lon, z)))))
        y_start = max(0, min(n - 1, int(math.floor(lat_to_tile_y(max_lat, z)))))
        y_end = max(0, min(n - 1, int(math.floor(lat_to_tile_y(min_lat, z)))))

        for tx in range(x_start, x_end + 1):
            for ty in range(y_start, y_end + 1):
                tile_features: List[Dict[str, Any]] = []
                for idx, feat in enumerate(basin_features, start=1):
                    rings = _polygon_rings_for_tile(feat["geometry"], z, tx, ty)
                    if not rings:
                        continue
                    tile_features.append(
                        {
                            "id": idx,
                            "geom_type": GEOM_POLYGON,
                            "geometry": rings,
                            "properties": dict(feat["properties"]),
                        }
                    )
                if tile_features:
                    tiles_by_zxy[(z, tx, ty)] = encode_mvt_tile({"basins": tile_features})

    metadata = {
        "name": "HydroBASINS Level 6 Summary",
        "description": "Hydrological basins with scalar GBIF occurrence and species counts",
        "version": "2",
        "attribution": attribution,
        "format": "pbf",
        "vector_layers": [
            {
                "id": "basins",
                "description": "HydroBASINS L6 polygons with scalar biodiversity totals",
                "minzoom": min_zoom,
                "maxzoom": max_zoom,
                "fields": {
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
                },
            }
        ],
    }

    stats = write_pmtiles(
        output_path=output_path,
        tiles_by_zxy=tiles_by_zxy,
        metadata=metadata,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        bounds=bounds,
        center=center,
        center_zoom=6,
    )
    inspected = inspect_pmtiles(output_path)
    if not inspected["tiles"]:
        raise RuntimeError("Generated basins.pmtiles contains zero readable tiles")
    return stats


def build_occurrences_pmtiles(
    output_path: Path,
    occurrence_features: List[Dict[str, Any]],
    bounds: List[float],
    center: List[float],
    attribution: str,
    min_zoom: int = 0,
    max_zoom: int = 10,
) -> Dict[str, Any]:
    """Generate occurrences.pmtiles containing point records."""
    tiles_by_zxy: Dict[Tuple[int, int, int], bytes] = {}

    for z in range(min_zoom, max_zoom + 1):
        n = 1 << z
        by_tile: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
        for idx, feat in enumerate(occurrence_features, start=1):
            lon, lat = feat["geometry"]["coordinates"]
            fx = lon_to_tile_x(lon, z)
            fy = lat_to_tile_y(lat, z)
            tx = max(0, min(n - 1, int(math.floor(fx))))
            ty = max(0, min(n - 1, int(math.floor(fy))))
            lx = max(0, min(DEFAULT_EXTENT - 1, round((fx - tx) * DEFAULT_EXTENT)))
            ly = max(0, min(DEFAULT_EXTENT - 1, round((fy - ty) * DEFAULT_EXTENT)))
            by_tile[(tx, ty)].append(
                {
                    "id": idx,
                    "geom_type": GEOM_POINT,
                    "geometry": [(lx, ly)],
                    "properties": dict(feat["properties"]),
                }
            )
        for (tx, ty), feats in by_tile.items():
            tiles_by_zxy[(z, tx, ty)] = encode_mvt_tile({"occurrences": feats})

    metadata = {
        "name": "GBIF Occurrences",
        "description": "Scoped GBIF occurrence points linked to HydroBASINS IDs",
        "version": "2",
        "attribution": attribution,
        "format": "pbf",
        "vector_layers": [
            {
                "id": "occurrences",
                "description": "Point occurrences with snapshot metadata",
                "minzoom": min_zoom,
                "maxzoom": max_zoom,
                "fields": {
                    "occurrenceID": "String",
                    "gbifID": "Number",
                    "scientificName": "String",
                    "taxonKey": "Number",
                    "eventDate": "String",
                    "year": "Number",
                    "basisOfRecord": "String",
                    "hybas_id": "String",
                    "license": "String",
                    "institutionCode": "String",
                },
            }
        ],
    }

    stats = write_pmtiles(
        output_path=output_path,
        tiles_by_zxy=tiles_by_zxy,
        metadata=metadata,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        bounds=bounds,
        center=center,
        center_zoom=7,
    )
    inspected = inspect_pmtiles(output_path)
    if not inspected["tiles"]:
        raise RuntimeError("Generated occurrences.pmtiles contains zero readable tiles")
    return stats


def build_release(
    version: str,
    source_dir: Path,
    output_root: Path,
    force: bool = False,
) -> Dict[str, Any]:
    """Build an immutable release under <output_root>/releases/<version>/ and update manifest.json."""
    if not version or "/" in version or version.startswith("."):
        raise ValueError(f"Invalid release version name: {version!r}")

    meta_path = source_dir / "gbif_download_metadata.json"
    basins_path = source_dir / "hydrobasins_l6.geojson"
    occ_path = source_dir / "gbif_occurrences.tsv"

    for required in (meta_path, basins_path, occ_path):
        if not required.exists():
            raise FileNotFoundError(f"Missing required source file: {required}")

    gbif_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    basins_fc = json.loads(basins_path.read_text(encoding="utf-8"))
    with occ_path.open("r", encoding="utf-8", newline="") as f:
        occurrences_raw = list(csv.DictReader(f, delimiter="\t"))

    releases_dir = output_root / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)
    target_dir = releases_dir / version
    if target_dir.exists() and not force:
        raise FileExistsError(
            f"Release directory {target_dir} already exists (releases are immutable; pass --force to rebuild)"
        )

    staging_dir = releases_dir / f".staging-{version}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    (staging_dir / "basins").mkdir(parents=True, exist_ok=True)

    # Spatial join: assign every occurrence to a HydroBASINS polygon.
    basin_by_id: Dict[str, Dict[str, Any]] = {}
    basin_bboxes: List[List[float]] = []
    for bfeat in basins_fc.get("features", []):
        props = bfeat.get("properties", {})
        hybas_id = str(props["hybas_id"])
        basin_by_id[hybas_id] = bfeat
        basin_bboxes.append(geometry_bbox(bfeat["geometry"]))

    all_min_lon = min(b[0] for b in basin_bboxes)
    all_min_lat = min(b[1] for b in basin_bboxes)
    all_max_lon = max(b[2] for b in basin_bboxes)
    all_max_lat = max(b[3] for b in basin_bboxes)
    overall_bounds = [
        round(all_min_lon, 5),
        round(all_min_lat, 5),
        round(all_max_lon, 5),
        round(all_max_lat, 5),
    ]
    overall_center = [
        round((all_min_lon + all_max_lon) / 2.0, 5),
        round((all_min_lat + all_max_lat) / 2.0, 5),
    ]

    basin_records: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    occurrence_features: List[Dict[str, Any]] = []
    unassigned_count = 0
    all_species_keys = set()
    all_licenses = set()

    for row in occurrences_raw:
        lat = float(row["decimalLatitude"])
        lon = float(row["decimalLongitude"])
        year = int(row["year"])
        taxon_key = int(row["taxonKey"])
        gbif_id = int(row["gbifID"])

        matched_hybas_id: Optional[str] = None
        for hybas_id, bfeat in basin_by_id.items():
            if point_in_geometry(lon, lat, bfeat["geometry"]):
                matched_hybas_id = hybas_id
                break

        if matched_hybas_id is None:
            unassigned_count += 1
            continue

        row_normalized = {
            "gbifID": gbif_id,
            "occurrenceID": row["occurrenceID"],
            "scientificName": row["scientificName"],
            "taxonKey": taxon_key,
            "family": row["family"],
            "decimalLatitude": lat,
            "decimalLongitude": lon,
            "eventDate": row["eventDate"],
            "year": year,
            "basisOfRecord": row["basisOfRecord"],
            "institutionCode": row["institutionCode"],
            "datasetKey": row["datasetKey"],
            "datasetName": row["datasetName"],
            "license": row["license"],
            "iucnRedListCategory": row.get("iucnRedListCategory", ""),
            "hybas_id": matched_hybas_id,
        }
        basin_records[matched_hybas_id].append(row_normalized)
        all_species_keys.add(row["scientificName"])
        if row["license"]:
            all_licenses.add(row["license"])

        occurrence_features.append(
            {
                "type": "Feature",
                "id": gbif_id,
                "properties": {
                    "occurrenceID": row["occurrenceID"],
                    "gbifID": gbif_id,
                    "scientificName": row["scientificName"],
                    "taxonKey": taxon_key,
                    "eventDate": row["eventDate"],
                    "year": year,
                    "basisOfRecord": row["basisOfRecord"],
                    "hybas_id": matched_hybas_id,
                    "license": row["license"],
                    "institutionCode": row["institutionCode"],
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [lon, lat],
                },
            }
        )

    if unassigned_count > 0:
        raise ValueError(f"{unassigned_count} occurrence records fell outside all HydroBASINS polygons")

    # Pre-aggregate each basin: scalar properties only in vector tiles; detailed lists in shards.
    aggregated_basin_features: List[Dict[str, Any]] = []
    basin_shard_files: List[Dict[str, Any]] = []

    for hybas_id, bfeat in sorted(basin_by_id.items()):
        props = bfeat["properties"]
        recs = basin_records.get(hybas_id, [])
        species_map: Dict[str, Dict[str, Any]] = {}
        temporal_hist: Dict[str, int] = defaultdict(int)
        basis_counts: Dict[str, int] = defaultdict(int)
        dataset_counts: Dict[str, Dict[str, Any]] = {}
        threatened_count = 0
        years: List[int] = []

        for r in recs:
            sname = r["scientificName"]
            yr = r["year"]
            years.append(yr)
            temporal_hist[str(yr)] += 1
            basis_counts[r["basisOfRecord"]] += 1
            if r["iucnRedListCategory"] in THREATENED_CATEGORIES:
                threatened_count += 1

            if sname not in species_map:
                species_map[sname] = {
                    "scientificName": sname,
                    "taxonKey": r["taxonKey"],
                    "family": r["family"],
                    "iucnRedListCategory": r["iucnRedListCategory"],
                    "count": 0,
                    "firstYear": yr,
                    "lastYear": yr,
                }
            sp_entry = species_map[sname]
            sp_entry["count"] += 1
            sp_entry["firstYear"] = min(sp_entry["firstYear"], yr)
            sp_entry["lastYear"] = max(sp_entry["lastYear"], yr)

            dkey = r["datasetKey"]
            if dkey not in dataset_counts:
                dataset_counts[dkey] = {
                    "datasetKey": dkey,
                    "datasetName": r["datasetName"],
                    "license": r["license"],
                    "count": 0,
                }
            dataset_counts[dkey]["count"] += 1

        scalar_props = {
            "hybas_id": hybas_id,
            "pfaf_id": str(props["pfaf_id"]),
            "basin_name": str(props["basin_name"]),
            "sub_area_km2": float(props["sub_area_km2"]),
            "up_area_km2": float(props["up_area_km2"]),
            "occurrence_count": len(recs),
            "species_count": len(species_map),
            "threatened_count": threatened_count,
            "min_year": min(years) if years else 0,
            "max_year": max(years) if years else 0,
            "shard_path": f"basins/{hybas_id}.json",
        }
        aggregated_basin_features.append(
            {
                "type": "Feature",
                "id": hybas_id,
                "properties": scalar_props,
                "geometry": bfeat["geometry"],
            }
        )

        species_list = sorted(
            species_map.values(),
            key=lambda item: (-item["count"], item["scientificName"]),
        )
        shard_payload = {
            "release_version": version,
            "basin": scalar_props,
            "bbox": props.get("bbox", geometry_bbox(bfeat["geometry"])),
            "species": species_list,
            "temporal_histogram": {k: temporal_hist[k] for k in sorted(temporal_hist.keys())},
            "basis_of_record": dict(sorted(basis_counts.items())),
            "datasets": sorted(dataset_counts.values(), key=lambda d: (-d["count"], d["datasetKey"])),
            "provenance": {
                "gbif_doi": gbif_meta["doi"],
                "download_key": gbif_meta["downloadKey"],
            },
        }
        shard_rel = f"basins/{hybas_id}.json"
        shard_file = staging_dir / shard_rel
        shard_file.write_text(json.dumps(shard_payload, indent=2) + "\n", encoding="utf-8")
        basin_shard_files.append(
            {
                "hybas_id": hybas_id,
                "path": f"/releases/{version}/{shard_rel}",
                "sha256": sha256_file(shard_file),
                "bytes": shard_file.stat().st_size,
            }
        )

    # Write GeoJSON mirror outputs.
    basins_geojson_path = staging_dir / "basins.geojson"
    basins_geojson_path.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": aggregated_basin_features},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    occ_geojson_path = staging_dir / "occurrences.geojson"
    occ_geojson_path.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": occurrence_features},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    attribution = (
        f"GBIF.org ({gbif_meta['doi']}); HydroSHEDS / HydroBASINS Level 6"
    )
    basins_pmtiles_path = staging_dir / "basins.pmtiles"
    basins_pm_stats = build_basins_pmtiles(
        output_path=basins_pmtiles_path,
        basin_features=aggregated_basin_features,
        bounds=overall_bounds,
        center=overall_center,
        attribution=attribution,
        min_zoom=0,
        max_zoom=8,
    )

    occ_pmtiles_path = staging_dir / "occurrences.pmtiles"
    occ_pm_stats = build_occurrences_pmtiles(
        output_path=occ_pmtiles_path,
        occurrence_features=occurrence_features,
        bounds=overall_bounds,
        center=overall_center,
        attribution=attribution,
        min_zoom=0,
        max_zoom=10,
    )

    # Preserve previous_version from existing manifest.json if distinct.
    root_manifest_path = releases_dir / "manifest.json"
    previous_version: Optional[str] = None
    if root_manifest_path.exists():
        existing_manifest = json.loads(root_manifest_path.read_text(encoding="utf-8"))
        existing_current = existing_manifest.get("current_version")
        if existing_current and existing_current != version:
            previous_version = existing_current
        elif existing_manifest.get("previous_version") and existing_manifest.get("previous_version") != version:
            previous_version = existing_manifest.get("previous_version")

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "schema_version": "1.0.0",
        "current_version": version,
        "previous_version": previous_version,
        "generated_at": generated_at,
        "provenance": {
            "gbif_doi": gbif_meta["doi"],
            "gbif_download_key": gbif_meta["downloadKey"],
            "gbif_download_created": gbif_meta["created"],
            "gbif_query": gbif_meta["query"],
            "citations": gbif_meta.get("citations", []),
            "sources": {
                "gbif_download_metadata.json": {
                    "sha256": sha256_file(meta_path),
                    "bytes": meta_path.stat().st_size,
                },
                "hydrobasins_l6.geojson": {
                    "sha256": sha256_file(basins_path),
                    "bytes": basins_path.stat().st_size,
                },
                "gbif_occurrences.tsv": {
                    "sha256": sha256_file(occ_path),
                    "bytes": occ_path.stat().st_size,
                },
            },
        },
        "spatial_coverage": {
            "crs": "EPSG:4326",
            "bbox": overall_bounds,
            "center": overall_center,
        },
        "counts": {
            "occurrences": len(occurrence_features),
            "species": len(all_species_keys),
            "basins": len(aggregated_basin_features),
        },
        "attribution": attribution,
        "licenses": sorted(all_licenses),
        "layers": {
            "basins": {
                "format": "pmtiles",
                "source_layer": "basins",
                "path": f"/releases/{version}/basins.pmtiles",
                "geojson_path": f"/releases/{version}/basins.geojson",
                "sha256": sha256_file(basins_pmtiles_path),
                "bytes": basins_pmtiles_path.stat().st_size,
                "min_zoom": 0,
                "max_zoom": 8,
                "tiles": basins_pm_stats["num_addressed_tiles"],
                "cache_control": IMMUTABLE_CACHE_CONTROL,
            },
            "occurrences": {
                "format": "pmtiles",
                "source_layer": "occurrences",
                "path": f"/releases/{version}/occurrences.pmtiles",
                "geojson_path": f"/releases/{version}/occurrences.geojson",
                "sha256": sha256_file(occ_pmtiles_path),
                "bytes": occ_pmtiles_path.stat().st_size,
                "min_zoom": 0,
                "max_zoom": 10,
                "tiles": occ_pm_stats["num_addressed_tiles"],
                "cache_control": IMMUTABLE_CACHE_CONTROL,
            },
            "basin_shards_template": f"/releases/{version}/basins/{{hybas_id}}.json",
            "basin_shards": basin_shard_files,
        },
        "overlays": {
            "approved_observations": {
                "format": "geojson",
                "path": "/overlays/approved.geojson",
                "cache_control": MUTABLE_CACHE_CONTROL,
            }
        },
    }

    (staging_dir / "release_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    # Atomically promote staging_dir -> target_dir, then update releases/manifest.json last.
    if target_dir.exists() and force:
        shutil.rmtree(target_dir)
    staging_dir.rename(target_dir)
    root_manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    return manifest


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an immutable spatial release for basinmap.org.")
    parser.add_argument("--version", default="v0.1.0-demo", help="Immutable release version tag")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=SCRIPT_DIR / "sample_data",
        help="Directory containing source GBIF and HydroBASINS files",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=SCRIPT_DIR.parent / "public-r2",
        help="Root directory representing the public R2 bucket layout",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite target release directory if it already exists (use only in dev/demo rebuilds)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    manifest = build_release(
        version=args.version,
        source_dir=args.source_dir,
        output_root=args.output_root,
        force=args.force,
    )
    print(
        f"Built release {manifest['current_version']} in {args.output_root / 'releases' / manifest['current_version']} "
        f"({manifest['counts']['occurrences']} occurrences, {manifest['counts']['species']} species, "
        f"{manifest['counts']['basins']} basins)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
