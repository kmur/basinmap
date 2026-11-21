#!/usr/bin/env python3
"""Country and continent summaries for the public fauna browser (Stage 10).

GBIF interprets ``countryCode`` (ISO 3166-1 alpha-2) for every occurrence. A release groups its
records by that code into ``countries.json``: per country, the record count, a bounding box, the
year range and the species seen there (with counts and IUCN category), plus the continent the
country belongs to. Country names are left to the browser (``Intl.DisplayNames``) so no name table
has to be maintained here; ``ZZ`` and blank codes are reported as "unknown".

Continents follow the UN M49 macro-regions, with the Americas split at Panama/Colombia into North
America (including Central America and the Caribbean) and South America. Transcontinental
countries are assigned to one continent, as GBIF's own country pages do (Russia and Turkey to
Europe/Asia as listed below).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Sequence

CONTINENT_CODES: Dict[str, str] = {
    "AF": "Africa", "AN": "Antarctica", "AS": "Asia", "EU": "Europe",
    "NA": "North America", "OC": "Oceania", "SA": "South America",
}

_BY_CONTINENT: Dict[str, str] = {
    "AF": "AO BF BI BJ BW CD CF CG CI CM CV DJ DZ EG EH ER ET GA GH GM GN GQ GW IO KE KM LR LS LY MA MG ML MR "
          "MU MW MZ NA NE NG RE RW SC SD SH SL SN SO SS ST SZ TD TF TG TN TZ UG YT ZA ZM ZW",
    "AN": "AQ BV GS HM",
    "AS": "AE AF AM AZ BD BH BN BT CC CN CX CY GE HK ID IL IN IQ IR JO JP KG KH KP KR KW KZ LA LB LK MM MN "
          "MO MV MY NP OM PH PK PS QA SA SG SY TH TJ TL TM TR TW UZ VN YE",
    "EU": "AD AL AT AX BA BE BG BY CH CZ DE DK EE ES FI FO FR GB GG GI GR HR HU IE IM IS IT JE LI LT LU LV "
          "MC MD ME MK MT NL NO PL PT RO RS RU SE SI SJ SK SM UA VA XK",
    "NA": "AG AI AW BB BL BM BQ BS BZ CA CR CU CW DM DO GD GL GP GT HN HT JM KN KY LC MF MQ MS MX NI PA PM "
          "PR SV SX TC TT US VC VG VI",
    "OC": "AS AU CK FJ FM GU KI MH MP NC NF NR NU NZ PF PG PN PW SB TK TO TV UM VU WF WS",
    "SA": "AR BO BR CL CO EC FK GF GY PE PY SR UY VE",
}

CONTINENT_OF: Dict[str, str] = {cc: cont for cont, codes in _BY_CONTINENT.items() for cc in codes.split()}


def continent_for(country_code: str) -> str:
    """Continent code (AF, AN, AS, EU, NA, OC, SA) for an ISO 3166-1 alpha-2 code, or '' if unknown."""
    return CONTINENT_OF.get((country_code or "").strip().upper(), "")


def new_country_stats() -> Dict[str, Any]:
    return {"count": 0, "species": {}, "bbox": None, "min_year": None, "max_year": None, "no_species": 0}


def add_record(stats: Dict[str, Dict[str, Any]], country_code: str, lon: float, lat: float,
               species_key: Optional[int], species: str, family: str, year: Optional[int]) -> None:
    """Accumulate one occurrence into ``stats`` (keyed by upper-case ISO code, '' for unknown)."""
    cc = (country_code or "").strip().upper()
    if cc == "ZZ":
        cc = ""
    s = stats.get(cc)
    if s is None:
        s = stats[cc] = new_country_stats()
    s["count"] += 1
    bb = s["bbox"]
    if bb is None:
        s["bbox"] = [lon, lat, lon, lat]
    else:
        bb[0], bb[1] = min(bb[0], lon), min(bb[1], lat)
        bb[2], bb[3] = max(bb[2], lon), max(bb[3], lat)
    if year:
        s["min_year"] = year if s["min_year"] is None else min(s["min_year"], year)
        s["max_year"] = year if s["max_year"] is None else max(s["max_year"], year)
    if species_key and species:
        sp = s["species"].get(species_key)
        if sp is None:
            sp = s["species"][species_key] = {"key": species_key, "name": species, "family": family, "count": 0}
        sp["count"] += 1
    else:
        s["no_species"] += 1


def build_country_summary(stats: Dict[str, Dict[str, Any]], iucn_by_key: Dict[str, str],
                          threatened: Sequence[str], version: str, source: str = "GBIF countryCode") -> Dict[str, Any]:
    """The ``countries.json`` document for a release."""
    countries = []
    for cc, s in sorted(stats.items(), key=lambda kv: (-kv[1]["count"], kv[0])):
        species = sorted(s["species"].values(), key=lambda x: (-x["count"], x["name"]))
        rows = []
        for sp in species:
            iucn = iucn_by_key.get(str(sp["key"]), "") or ""
            rows.append({"key": sp["key"], "name": sp["name"], "family": sp["family"], "count": sp["count"],
                         **({"iucn": iucn} if iucn else {})})
        countries.append({
            "code": cc or "unknown",
            "continent": continent_for(cc),
            "occurrence_count": s["count"],
            "species_count": len(rows),
            "threatened_species": sum(1 for r in rows if r.get("iucn") in threatened),
            "records_without_species": s["no_species"],
            "bbox": [round(v, 4) for v in s["bbox"]] if s["bbox"] else None,
            "min_year": s["min_year"] or 0,
            "max_year": s["max_year"] or 0,
            "species": rows,
        })
    continents: Dict[str, Dict[str, Any]] = {}
    for c in countries:
        key = c["continent"] or "unknown"
        cont = continents.setdefault(key, {"code": key, "name": CONTINENT_CODES.get(key, "Unknown"),
                                           "countries": 0, "occurrence_count": 0, "species": set()})
        cont["countries"] += 1
        cont["occurrence_count"] += c["occurrence_count"]
        cont["species"].update(r["key"] for r in c["species"])
    return {
        "schema": "basinmap-countries/1",
        "release_version": version,
        "source": source,
        "note": ("Countries are GBIF's interpreted countryCode (ISO 3166-1 alpha-2); records GBIF could not "
                 "assign are listed as 'unknown'. Continents follow UN M49 regions with the Americas split "
                 "into North (incl. Central America and the Caribbean) and South America."),
        "continents": [
            {"code": v["code"], "name": v["name"], "countries": v["countries"],
             "occurrence_count": v["occurrence_count"], "species_count": len(v["species"])}
            for v in sorted(continents.values(), key=lambda v: -v["occurrence_count"])
        ],
        "countries": countries,
    }


def known_codes() -> Iterable[str]:
    return CONTINENT_OF.keys()
