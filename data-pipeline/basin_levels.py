#!/usr/bin/env python3
"""Extra HydroBASINS levels for a release (Stage 8): Level 3 (large river systems) and Level 8
(small tributary catchments) alongside the primary Level 6.

For each extra level the release gets its own ``basins_lNN.pmtiles`` (scalar properties only) and
pre-aggregated shards. Coarser levels write one JSON shard per basin. Finer levels are grouped by
their parent basin at the primary level (Pfafstetter codes nest: the first 6 digits of a Level 8
code are its Level 6 parent), so Level 8 adds ~16k files instead of ~190k and one click on a Level 6
basin can load all of its Level 8 children at once.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from hydroatlas import basin_environment, tile_scalars
from hydrobasins import BasinIndex, geometry_bbox, load_hydrobasins, simplify_geometry

# (lon, lat, speciesKey, species, family, iucn-from-download, year)
LevelRow = Tuple[float, float, int, str, str, str, int]

DEFAULT_LEVEL_ZOOMS = {3: (0, 5), 4: (0, 6), 5: (0, 7), 6: (0, 7), 7: (4, 9), 8: (5, 9), 9: (6, 10)}


def index_cell_for_level(level: int) -> float:
    """Grid cell size for the point-in-basin index: big basins need coarse cells to stay small."""
    return {1: 5.0, 2: 3.0, 3: 1.0, 4: 0.5}.get(level, 0.25)


def simplify_tolerance_for_level(level: int) -> float:
    """Degrees; ~1 px at the level's max zoom so tiles look identical while memory stays bounded."""
    return {1: 0.02, 2: 0.02, 3: 0.01, 4: 0.005, 5: 0.003, 6: 0.0, 7: 0.002, 8: 0.002}.get(level, 0.001)


def load_level(paths: Sequence[Path], level: int, simplify_tol: Optional[float] = None) -> List[Dict[str, Any]]:
    """Load one level region by region, simplifying each region before the next is read."""
    tol = simplify_tolerance_for_level(level) if simplify_tol is None else simplify_tol
    feats: List[Dict[str, Any]] = []
    for path in paths:
        region = load_hydrobasins(Path(path))
        if tol > 0:
            for f in region:
                f["geometry"] = simplify_geometry(f["geometry"], tol)
        feats.extend(region)
        del region
    for f in feats:
        lvl = len(str(f["properties"].get("pfaf_id", "")))
        if lvl and lvl != level:
            raise ValueError(f"{f['properties']['hybas_id']} has a level-{lvl} Pfafstetter code, expected level {level}")
    return feats


def aggregate_level(
    basins: List[Dict[str, Any]],
    rows: Sequence[LevelRow],
    level: int,
    iucn_map: Dict[str, str],
    threatened: Sequence[str],
    progress: Callable[[str], None] = lambda m: None,
) -> Dict[str, Any]:
    """Assign rows to basins and build scalar properties + shard payloads for one level."""
    index = BasinIndex(basins, cell_deg=index_cell_for_level(level))
    agg: Dict[int, Dict[str, Any]] = {}
    assigned = 0
    for i, (lon, lat, sk, species, family, iucn, yr) in enumerate(rows):
        if i and i % 100_000 == 0:
            progress(f"level {level}: {i:,}/{len(rows):,} records assigned")
        bidx = index.lookup(lon, lat)
        if bidx is None:
            continue
        assigned += 1
        a = agg.setdefault(bidx, {"n": 0, "species": {}, "years": defaultdict(int), "min_year": None, "max_year": None})
        a["n"] += 1
        if yr:
            a["years"][str(yr)] += 1
            a["min_year"] = yr if a["min_year"] is None else min(a["min_year"], yr)
            a["max_year"] = yr if a["max_year"] is None else max(a["max_year"], yr)
        if sk and species:
            sp = a["species"].get(sk)
            if sp is None:
                sp = a["species"][sk] = {"scientificName": species, "taxonKey": sk, "family": family,
                                         "iucnRedListCategory": iucn or iucn_map.get(str(sk), ""), "count": 0}
            sp["count"] += 1
    return {"index": index, "agg": agg, "assigned": assigned}


def build_level_outputs(
    basins: List[Dict[str, Any]],
    agg: Dict[int, Dict[str, Any]],
    level: int,
    version: str,
    threatened: Sequence[str],
    atlas: Optional[Dict[str, Any]] = None,
    parent_by_pfaf: Optional[Dict[str, str]] = None,
    parent_level: Optional[int] = None,
) -> Dict[str, Any]:
    """Scalar features (for tiles) and shard files {relative path: payload} for one level."""
    folder = f"basins_l{level:02d}"
    group = parent_by_pfaf is not None and parent_level is not None and level > parent_level
    features: List[Dict[str, Any]] = []
    shards: Dict[str, Dict[str, Any]] = {}
    for bidx, feat in enumerate(basins):
        props = feat["properties"]
        hybas_id = str(props["hybas_id"])
        a = agg.get(bidx)
        species = sorted(a["species"].values(), key=lambda s: (-s["count"], s["scientificName"])) if a else []
        threatened_records = sum(s["count"] for s in species if s["iucnRedListCategory"] in threatened)
        pfaf = str(props.get("pfaf_id", ""))
        if group:
            parent = parent_by_pfaf.get(pfaf[:parent_level], "unassigned")
            shard_rel = f"{folder}/{parent}.json"
        else:
            parent = None
            shard_rel = f"{folder}/{hybas_id}.json"
        scalar = {
            "hybas_id": hybas_id, "pfaf_id": pfaf, "basin_name": props.get("basin_name", ""),
            "sub_area_km2": round(float(props.get("sub_area_km2") or 0), 1),
            "up_area_km2": round(float(props.get("up_area_km2") or 0), 1),
            "occurrence_count": a["n"] if a else 0, "species_count": len(species),
            "threatened_count": threatened_records,
            "min_year": (a["min_year"] or 0) if a else 0, "max_year": (a["max_year"] or 0) if a else 0,
            "shard_path": shard_rel, **tile_scalars(atlas, hybas_id),
        }
        if parent is not None:
            scalar["parent_hybas_id"] = parent
        features.append({"type": "Feature", "id": hybas_id, "properties": scalar, "geometry": feat["geometry"]})
        payload = {
            "basin": scalar, "hydrobasins": {"level": level, "next_down": props.get("next_down"),
                                             "main_bas": props.get("main_bas")},
            "bbox": [round(v, 5) for v in geometry_bbox(feat["geometry"])],
            "species": species,
            "temporal_histogram": dict(sorted(a["years"].items())) if a else {},
            **({"environment": basin_environment(atlas, hybas_id)} if atlas else {}),
        }
        if group:
            doc = shards.setdefault(shard_rel, {"release_version": version, "level": level,
                                                "parent_level": parent_level, "parent_hybas_id": parent, "basins": {}})
            doc["basins"][hybas_id] = payload
        else:
            shards[shard_rel] = {"release_version": version, **payload}
    return {"features": features, "shards": shards, "grouped_by_parent": group}


def write_shards(staging_dir: Path, shards: Dict[str, Dict[str, Any]], version: str,
                 sha256_file: Callable[[Path], str]) -> List[Dict[str, Any]]:
    refs = []
    for rel, payload in sorted(shards.items()):
        f = staging_dir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(payload, separators=(",", ":")) + "\n", "utf-8")
        refs.append({"path": f"/releases/{version}/{rel}", "sha256": sha256_file(f), "bytes": f.stat().st_size})
    return refs
