#!/usr/bin/env python3
"""Generate deterministic sample GBIF and HydroBASINS source files for the pilot.

Produces three source artifacts in data-pipeline/sample_data/:
  1. gbif_download_metadata.json
  2. hydrobasins_l6.geojson
  3. gbif_occurrences.tsv
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

SAMPLE_DIR = Path(__file__).resolve().parent

SPECIES_CATALOG: List[Dict[str, Any]] = [
    {
        "scientificName": "Oncorhynchus tshawytscha",
        "taxonKey": 5204029,
        "kingdom": "Animalia",
        "phylum": "Chordata",
        "class": "Actinopterygii",
        "order": "Salmoniformes",
        "family": "Salmonidae",
        "genus": "Oncorhynchus",
        "iucnRedListCategory": "LC",
    },
    {
        "scientificName": "Oncorhynchus mykiss",
        "taxonKey": 5204019,
        "kingdom": "Animalia",
        "phylum": "Chordata",
        "class": "Actinopterygii",
        "order": "Salmoniformes",
        "family": "Salmonidae",
        "genus": "Oncorhynchus",
        "iucnRedListCategory": "LC",
    },
    {
        "scientificName": "Salvelinus confluentus",
        "taxonKey": 2351283,
        "kingdom": "Animalia",
        "phylum": "Chordata",
        "class": "Actinopterygii",
        "order": "Salmoniformes",
        "family": "Salmonidae",
        "genus": "Salvelinus",
        "iucnRedListCategory": "VU",
    },
    {
        "scientificName": "Rana aurora",
        "taxonKey": 2426776,
        "kingdom": "Animalia",
        "phylum": "Chordata",
        "class": "Amphibia",
        "order": "Anura",
        "family": "Ranidae",
        "genus": "Rana",
        "iucnRedListCategory": "LC",
    },
    {
        "scientificName": "Actinemys marmorata",
        "taxonKey": 2443326,
        "kingdom": "Animalia",
        "phylum": "Chordata",
        "class": "Reptilia",
        "order": "Testudines",
        "family": "Emydidae",
        "genus": "Actinemys",
        "iucnRedListCategory": "VU",
    },
    {
        "scientificName": "Margaritifera falcata",
        "taxonKey": 2289460,
        "kingdom": "Animalia",
        "phylum": "Mollusca",
        "class": "Bivalvia",
        "order": "Unionida",
        "family": "Margaritiferidae",
        "genus": "Margaritifera",
        "iucnRedListCategory": "NT",
    },
    {
        "scientificName": "Cinclus mexicanus",
        "taxonKey": 2495054,
        "kingdom": "Animalia",
        "phylum": "Chordata",
        "class": "Aves",
        "order": "Passeriformes",
        "family": "Cinclidae",
        "genus": "Cinclus",
        "iucnRedListCategory": "LC",
    },
    {
        "scientificName": "Lontra canadensis",
        "taxonKey": 2433745,
        "kingdom": "Animalia",
        "phylum": "Chordata",
        "class": "Mammalia",
        "order": "Carnivora",
        "family": "Mustelidae",
        "genus": "Lontra",
        "iucnRedListCategory": "LC",
    },
    {
        "scientificName": "Dicamptodon tenebrosus",
        "taxonKey": 2431954,
        "kingdom": "Animalia",
        "phylum": "Chordata",
        "class": "Amphibia",
        "order": "Urodela",
        "family": "Dicamptodontidae",
        "genus": "Dicamptodon",
        "iucnRedListCategory": "LC",
    },
    {
        "scientificName": "Entosphenus tridentatus",
        "taxonKey": 2421296,
        "kingdom": "Animalia",
        "phylum": "Chordata",
        "class": "Petromyzonti",
        "order": "Petromyzontiformes",
        "family": "Petromyzontidae",
        "genus": "Entosphenus",
        "iucnRedListCategory": "NT",
    },
]

BASINS: List[Dict[str, Any]] = [
    {
        "hybas_id": "7060041230",
        "pfaf_id": "782911",
        "basin_name": "Upper Willamette",
        "sub_area_km2": 4820.5,
        "up_area_km2": 4820.5,
        "ring": [
            [-123.60, 43.60],
            [-122.45, 43.60],
            [-122.40, 44.00],
            [-122.50, 44.32],
            [-123.55, 44.30],
            [-123.60, 43.95],
            [-123.60, 43.60],
        ],
    },
    {
        "hybas_id": "7060041240",
        "pfaf_id": "782912",
        "basin_name": "Middle Willamette",
        "sub_area_km2": 5140.2,
        "up_area_km2": 13410.7,
        "ring": [
            [-123.55, 44.30],
            [-122.50, 44.32],
            [-122.45, 44.75],
            [-122.55, 45.08],
            [-123.52, 45.10],
            [-123.58, 44.70],
            [-123.55, 44.30],
        ],
    },
    {
        "hybas_id": "7060041250",
        "pfaf_id": "782913",
        "basin_name": "McKenzie River",
        "sub_area_km2": 3450.0,
        "up_area_km2": 3450.0,
        "ring": [
            [-122.45, 43.60],
            [-121.65, 43.60],
            [-121.62, 44.10],
            [-121.68, 44.40],
            [-122.48, 44.38],
            [-122.50, 44.32],
            [-122.40, 44.00],
            [-122.45, 43.60],
        ],
    },
    {
        "hybas_id": "7060041260",
        "pfaf_id": "782914",
        "basin_name": "Santiam River",
        "sub_area_km2": 4690.8,
        "up_area_km2": 4690.8,
        "ring": [
            [-122.48, 44.38],
            [-121.68, 44.40],
            [-121.64, 44.78],
            [-121.70, 45.02],
            [-122.55, 45.08],
            [-122.45, 44.75],
            [-122.50, 44.32],
            [-122.48, 44.38],
        ],
    },
    {
        "hybas_id": "7060041270",
        "pfaf_id": "782915",
        "basin_name": "Clackamas River",
        "sub_area_km2": 2440.3,
        "up_area_km2": 2440.3,
        "ring": [
            [-122.55, 45.08],
            [-121.70, 45.02],
            [-121.66, 45.36],
            [-121.72, 45.62],
            [-122.58, 45.58],
            [-122.62, 45.32],
            [-122.55, 45.08],
        ],
    },
    {
        "hybas_id": "7060041280",
        "pfaf_id": "782916",
        "basin_name": "Lower Willamette & Columbia Confluence",
        "sub_area_km2": 4110.4,
        "up_area_km2": 29750.0,
        "ring": [
            [-123.52, 45.10],
            [-122.55, 45.08],
            [-122.62, 45.32],
            [-122.58, 45.58],
            [-121.72, 45.62],
            [-121.78, 45.98],
            [-123.48, 46.02],
            [-123.55, 45.55],
            [-123.52, 45.10],
        ],
    },
]

DATASETS = [
    (
        "50c9509d-22c7-4a22-a47d-8c48425ef4a7",
        "iNaturalist Research-grade Observations",
        "iNaturalist",
        "HUMAN_OBSERVATION",
        "http://creativecommons.org/licenses/by/4.0/legalcode",
    ),
    (
        "7e380070-f762-11e1-a439-00145eb45e9a",
        "Oregon Biodiversity Information Center Freshwater Survey",
        "ORBIC",
        "HUMAN_OBSERVATION",
        "http://creativecommons.org/licenses/by/4.0/legalcode",
    ),
    (
        "82b1d700-f762-11e1-a439-00145eb45e9a",
        "Oregon State University Ichthyology Collection",
        "OS",
        "PRESERVED_SPECIMEN",
        "http://creativecommons.org/publicdomain/zero/1.0/legalcode",
    ),
]

OBSERVERS = [
    "https://orcid.org/0000-0002-1825-0097",
    "https://orcid.org/0000-0001-5109-3700",
    "M. Alvarez",
    "K. Lindholm",
    "R. Takahashi",
    "J. Whitmore",
]

TSV_COLUMNS = [
    "gbifID",
    "occurrenceID",
    "scientificName",
    "taxonKey",
    "kingdom",
    "phylum",
    "class",
    "order",
    "family",
    "genus",
    "decimalLatitude",
    "decimalLongitude",
    "coordinateUncertaintyInMeters",
    "eventDate",
    "year",
    "month",
    "basisOfRecord",
    "institutionCode",
    "datasetKey",
    "datasetName",
    "recordedBy",
    "license",
    "iucnRedListCategory",
]


def point_in_ring(lon: float, lat: float, ring: List[List[float]]) -> bool:
    """Return True when (lon, lat) lies strictly inside the polygon ring."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        intersects = ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) if (yj - yi) != 0 else 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def ring_bbox(ring: List[List[float]]) -> List[float]:
    xs = [pt[0] for pt in ring]
    ys = [pt[1] for pt in ring]
    return [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)]


def ring_interior_points(ring: List[List[float]], count: int, seed_offset: int) -> List[Tuple[float, float]]:
    """Deterministically generate `count` points guaranteed inside `ring`."""
    min_x, min_y, max_x, max_y = ring_bbox(ring)
    pts: List[Tuple[float, float]] = []
    # Probe a deterministic quasi-random grid inside the bbox, keeping points in ring.
    idx = 0
    while len(pts) < count:
        u = ((idx * 37 + seed_offset * 13 + 11) % 97) / 98.0 + 0.01
        v = ((idx * 53 + seed_offset * 29 + 17) % 89) / 90.0 + 0.01
        lon = round(min_x + (max_x - min_x) * u, 5)
        lat = round(min_y + (max_y - min_y) * v, 5)
        if point_in_ring(lon, lat, ring):
            pts.append((lon, lat))
        idx += 1
        if idx > 5000:
            raise RuntimeError("Unable to find sufficient interior points for basin polygon")
    return pts


def generate_all(output_dir: Path = SAMPLE_DIR) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "doi": "10.15468/dl.basinmap-demo-2026",
        "downloadKey": "0012345-261118120000000",
        "created": "2026-11-18T12:00:00Z",
        "status": "SUCCEEDED",
        "format": "SIMPLE_CSV",
        "license": "http://creativecommons.org/licenses/by/4.0/legalcode",
        "query": {
            "geometry": "POLYGON((-123.8 43.5, -121.5 43.5, -121.5 46.2, -123.8 46.2, -123.8 43.5))",
            "hasCoordinate": True,
            "hasGeospatialIssue": False,
            "occurrenceStatus": "PRESENT",
            "year": "2018,2026",
        },
        "totalRecords": 72,
        "citations": [
            {
                "citation": (
                    "GBIF.org (18 November 2026) GBIF Occurrence Download "
                    "https://doi.org/10.15468/dl.basinmap-demo-2026"
                ),
                "doi": "10.15468/dl.basinmap-demo-2026",
            }
        ],
        "notes": "Self-contained synthetic demonstration dataset for the basinmap.org spatial pilot.",
    }
    (output_dir / "gbif_download_metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )

    basin_features = []
    for basin in BASINS:
        bbox = ring_bbox(basin["ring"])
        basin_features.append(
            {
                "type": "Feature",
                "id": basin["hybas_id"],
                "properties": {
                    "hybas_id": basin["hybas_id"],
                    "pfaf_id": basin["pfaf_id"],
                    "basin_name": basin["basin_name"],
                    "sub_area_km2": basin["sub_area_km2"],
                    "up_area_km2": basin["up_area_km2"],
                    "bbox": bbox,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [basin["ring"]],
                },
            }
        )

    hydrobasins_fc = {
        "type": "FeatureCollection",
        "name": "hydrobasins_l6_willamette_demo",
        "features": basin_features,
    }
    (output_dir / "hydrobasins_l6.geojson").write_text(
        json.dumps(hydrobasins_fc, indent=2) + "\n", encoding="utf-8"
    )

    # Generate 12 deterministic records per basin = 72 records total across 10 species.
    rows: List[Dict[str, Any]] = []
    rec_num = 1
    for b_idx, basin in enumerate(BASINS):
        coords = ring_interior_points(basin["ring"], count=12, seed_offset=b_idx + 1)
        for i in range(12):
            species = SPECIES_CATALOG[(b_idx * 3 + i) % len(SPECIES_CATALOG)]
            dataset_key, dataset_name, inst_code, basis, rec_license = DATASETS[
                (b_idx + i) % len(DATASETS)
            ]
            observer = OBSERVERS[(b_idx + i * 2) % len(OBSERVERS)]
            year = 2019 + ((b_idx * 2 + i) % 8)  # 2019..2026
            month = 1 + ((b_idx * 5 + i * 3) % 12)
            day = 1 + ((b_idx * 7 + i * 5) % 28)
            lon, lat = coords[i]
            gbif_id = 4800000000 + rec_num
            occ_id = f"urn:basinmap:demo:{basin['hybas_id']}:{rec_num:04d}"
            uncertainty = [15, 25, 50, 100, 250][(rec_num) % 5]
            rows.append(
                {
                    "gbifID": gbif_id,
                    "occurrenceID": occ_id,
                    "scientificName": species["scientificName"],
                    "taxonKey": species["taxonKey"],
                    "kingdom": species["kingdom"],
                    "phylum": species["phylum"],
                    "class": species["class"],
                    "order": species["order"],
                    "family": species["family"],
                    "genus": species["genus"],
                    "decimalLatitude": f"{lat:.5f}",
                    "decimalLongitude": f"{lon:.5f}",
                    "coordinateUncertaintyInMeters": uncertainty,
                    "eventDate": f"{year:04d}-{month:02d}-{day:02d}",
                    "year": year,
                    "month": month,
                    "basisOfRecord": basis,
                    "institutionCode": inst_code,
                    "datasetKey": dataset_key,
                    "datasetName": dataset_name,
                    "recordedBy": observer,
                    "license": rec_license,
                    "iucnRedListCategory": species["iucnRedListCategory"],
                }
            )
            rec_num += 1

    tsv_path = output_dir / "gbif_occurrences.tsv"
    with tsv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TSV_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    generate_all()
    print(f"Generated sample source files in {SAMPLE_DIR}")
