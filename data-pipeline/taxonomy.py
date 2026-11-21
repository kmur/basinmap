#!/usr/bin/env python3
"""GBIF Backbone checklist for one higher taxon (Stage 7: Cyprinodontiformes, backbone key 547).

Pages through ``/species/search`` on the GBIF Backbone Taxonomy and writes ``checklist.json``:

* every ACCEPTED name at species rank under the taxon (with family / genus / authorship), and
* every SYNONYM whose accepted name is one of those species (for search and alias resolution).

At release build time :func:`build_species_outputs` merges the checklist with per-species
occurrence counts and IUCN categories into the two published files:

* ``species_index.json``   one row per accepted species, sorted by family, genus, name.
* ``taxonomy_aliases.json`` synonym / alternate name (lower-cased) -> accepted species key.

Only public GBIF APIs are used; no external association databases.

    python3 data-pipeline/taxonomy.py fetch --taxon-key 547 --workdir data-pipeline/work/taxonomy
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

BACKBONE_DATASET_KEY = "d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"
PAGE_SIZE = 1000
MAX_OFFSET = 100_000  # GBIF species search paging ceiling
CHECKLIST_SCHEMA = "basinmap-checklist/1"


def _page(client: Any, params: Dict[str, str]) -> Iterator[Dict[str, Any]]:
    offset = 0
    while offset < MAX_OFFSET:
        query = urllib.parse.urlencode({**params, "limit": str(PAGE_SIZE), "offset": str(offset)})
        data = client.get_json(f"/species/search?{query}") or {}
        results = data.get("results") or []
        yield from results
        if data.get("endOfRecords", True) or not results:
            return
        offset += len(results)
    raise RuntimeError(f"/species/search exceeded {MAX_OFFSET} results for {params}; narrow the taxon")


def _usage_key(rec: Dict[str, Any]) -> Optional[int]:
    # species/search returns checklist-local "key" plus "nubKey" (backbone). On the backbone they match.
    for field in ("nubKey", "key"):
        if rec.get(field) is not None:
            return int(rec[field])
    return None


def fetch_checklist(client: Any, taxon_key: int) -> Dict[str, Any]:
    """Accepted species and their synonyms under ``taxon_key`` from the GBIF Backbone."""
    root = client.get_json(f"/species/{int(taxon_key)}") or {}
    base = {"datasetKey": BACKBONE_DATASET_KEY, "highertaxonKey": str(int(taxon_key))}
    species: List[Dict[str, Any]] = []
    for rec in _page(client, {**base, "rank": "SPECIES", "status": "ACCEPTED"}):
        key = _usage_key(rec)
        if key is None:
            continue
        species.append({
            "key": key,
            "name": rec.get("canonicalName") or rec.get("scientificName", ""),
            "scientificName": rec.get("scientificName", ""),
            "authorship": (rec.get("authorship") or "").strip(),
            "family": rec.get("family", ""),
            "familyKey": rec.get("familyKey"),
            "genus": rec.get("genus", ""),
            "genusKey": rec.get("genusKey"),
            "vernacularNames": sorted({v.get("vernacularName", "") for v in rec.get("vernacularNames") or []
                                       if v.get("language") in ("eng", "en", "") and v.get("vernacularName")})[:5],
        })
    accepted = {s["key"] for s in species}
    synonyms: List[Dict[str, Any]] = []
    for status in ("SYNONYM", "HETEROTYPIC_SYNONYM", "HOMOTYPIC_SYNONYM", "PROPARTE_SYNONYM"):
        for rec in _page(client, {**base, "status": status}):
            acc = rec.get("acceptedKey") or rec.get("speciesKey")
            if acc is None or int(acc) not in accepted:
                continue  # synonyms of genera/families, or of names outside the accepted set
            synonyms.append({
                "key": _usage_key(rec),
                "name": rec.get("canonicalName") or rec.get("scientificName", ""),
                "scientificName": rec.get("scientificName", ""),
                "status": rec.get("taxonomicStatus") or status,
                "acceptedKey": int(acc),
            })
    species.sort(key=lambda s: (s["family"], s["genus"], s["name"]))
    synonyms.sort(key=lambda s: (s["name"], s["key"] or 0))
    return {
        "schema": CHECKLIST_SCHEMA,
        "source": "GBIF Backbone Taxonomy",
        "source_dataset_key": BACKBONE_DATASET_KEY,
        "taxon": {"key": int(taxon_key), "name": root.get("canonicalName") or root.get("scientificName", ""),
                  "rank": root.get("rank", "")},
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "species": species,
        "synonyms": synonyms,
    }


def normalize_alias(name: str) -> str:
    """Lower-case, accent-free, single-spaced key for search lookups."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return " ".join(folded.lower().replace("×", "x").split())


def build_species_outputs(
    checklist: Dict[str, Any],
    occurrences: Dict[int, Dict[str, Any]],
    iucn: Dict[str, str],
    threatened: Sequence[str] = ("CR", "EN", "VU"),
) -> Dict[str, Any]:
    """Merge the checklist with release statistics.

    ``occurrences`` maps speciesKey -> {"count", "name", "family", "firstYear", "lastYear", "basins"}.
    Species seen in the download but absent from the checklist (e.g. newer backbone keys) are kept
    and flagged ``in_checklist: false`` so no record silently disappears from the index.
    """
    rows: List[Dict[str, Any]] = []
    seen = set()
    for sp in checklist.get("species", []):
        key = int(sp["key"])
        seen.add(key)
        occ = occurrences.get(key, {})
        cat = iucn.get(str(key), "")
        rows.append({
            "key": key, "name": sp["name"], "authorship": sp.get("authorship", ""),
            "family": sp.get("family", ""), "genus": sp.get("genus", ""),
            "vernacular": sp.get("vernacularNames", []),
            "iucn": cat, "threatened": cat in threatened,
            "occurrence_count": int(occ.get("count", 0)), "basin_count": len(occ.get("basins", ())),
            "first_year": occ.get("firstYear") or 0, "last_year": occ.get("lastYear") or 0,
            "in_checklist": True,
        })
    for key, occ in sorted(occurrences.items()):
        if key in seen:
            continue
        cat = iucn.get(str(key), "")
        name = occ.get("name", "")
        rows.append({
            "key": int(key), "name": name, "authorship": "", "family": occ.get("family", ""),
            "genus": name.split(" ", 1)[0] if name else "", "vernacular": [],
            "iucn": cat, "threatened": cat in threatened,
            "occurrence_count": int(occ.get("count", 0)), "basin_count": len(occ.get("basins", ())),
            "first_year": occ.get("firstYear") or 0, "last_year": occ.get("lastYear") or 0,
            "in_checklist": False,
        })
    rows.sort(key=lambda r: (r["family"], r["genus"], r["name"]))

    aliases: Dict[str, int] = {}
    conflicts: Dict[str, List[int]] = {}

    def add(alias: str, key: int) -> None:
        norm = normalize_alias(alias)
        if not norm:
            return
        prev = aliases.get(norm)
        if prev is None:
            aliases[norm] = key
        elif prev != key:
            conflicts.setdefault(norm, sorted({prev}))
            if key not in conflicts[norm]:
                conflicts[norm].append(key)

    # Precedence: accepted names, then synonyms, then vernacular names. A lower tier never overrides
    # a higher one; within a tier, one spelling pointing at two species is ambiguous and dropped.
    for r in rows:
        add(r["name"], r["key"])
    row_keys = {r["key"] for r in rows}
    for tier in (
        [(syn["name"], syn["acceptedKey"]) for syn in checklist.get("synonyms", []) if syn["acceptedKey"] in row_keys],
        [(v, r["key"]) for r in rows for v in r["vernacular"]],
    ):
        taken = set(aliases) | set(conflicts)
        for alias, key in tier:
            if normalize_alias(alias) not in taken:
                add(alias, key)
    for norm in conflicts:  # ambiguous synonyms resolve to nothing rather than to a wrong species
        aliases.pop(norm, None)

    families = sorted({r["family"] for r in rows if r["family"]})
    genera = sorted({r["genus"] for r in rows if r["genus"]})
    return {
        "species_index": {
            "schema": "basinmap-species-index/1",
            "taxon": checklist.get("taxon", {}),
            "source": checklist.get("source", "GBIF Backbone Taxonomy"),
            "checklist_fetched_at": checklist.get("fetched_at"),
            "counts": {"species": len(rows), "with_occurrences": sum(1 for r in rows if r["occurrence_count"]),
                       "threatened": sum(1 for r in rows if r["threatened"]),
                       "families": len(families), "genera": len(genera)},
            "families": families,
            "species": rows,
        },
        "taxonomy_aliases": {
            "schema": "basinmap-taxonomy-aliases/1",
            "normalization": "NFKD, ASCII, lower-case, single spaces",
            "aliases": dict(sorted(aliases.items())),
            "ambiguous": {k: v for k, v in sorted(conflicts.items())},
        },
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    from ingest_gbif_download import GBIF_API, client_from_env  # noqa: E402

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-base", default=GBIF_API)
    sub = ap.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch", help="Write checklist.json for a GBIF Backbone taxon")
    f.add_argument("--taxon-key", type=int, default=547, help="GBIF Backbone key (547 = Cyprinodontiformes)")
    f.add_argument("--workdir", type=Path, default=SCRIPT_DIR / "work" / "taxonomy")
    args = ap.parse_args(argv)
    client = client_from_env(args.api_base)
    checklist = fetch_checklist(client, args.taxon_key)
    args.workdir.mkdir(parents=True, exist_ok=True)
    out = args.workdir / "checklist.json"
    out.write_text(json.dumps(checklist, indent=1, ensure_ascii=False) + "\n", "utf-8")
    print(json.dumps({"checklist": str(out), "taxon": checklist["taxon"], "species": len(checklist["species"]),
                      "synonyms": len(checklist["synonyms"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
