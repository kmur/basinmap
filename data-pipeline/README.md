# basinmap.org Spatial Data Pipeline (`data-pipeline/`)

Reproducible batch pipeline for preparing immutable spatial releases (`releases/<version>/`), hydrological basin pre-aggregations, and manifest-aware R2 uploads for `https://data.basinmap.org`.

## Design Rules Enforced

1. **Zero external runtime dependencies:** every script runs on standard Python 3.9+. `pmtiles_writer.py` builds spec-compliant PMTiles v3 + MVT v2.1 archives (with leaf directories, so the header and root directory always fit in the first 16 KiB), and `hydrobasins.py` reads the HydroSHEDS shapefile distribution directly.
2. **Scalar-only vector tiles:** `basins*.pmtiles` carry only `hybas_id`, `pfaf_id`, `basin_name`, `sub_area_km2`, `up_area_km2`, `occurrence_count`, `species_count`, `threatened_count`, `min_year`, `max_year`, `shard_path` (`release_tiling.build_basin_tiles` rejects anything else). Species checklists, temporal histograms, and dataset breakdowns live in `releases/<version>/basins/<hybas_id>.json`. For HydroBASINS Levels 7–12, or more than 20,000 basins, they go into regional SQLite partitions (`basins/pfaf-<prefix>.sqlite`) instead.
3. **< 500 MB per archive (Section 4.3):** `release_tiling.write_partitioned_pmtiles` splits any archive whose estimated size exceeds 500,000,000 bytes, first by zoom tier and then spatially (Hilbert-ordered ancestor tiles). Each partition is listed in the manifest with its own `min_zoom`, `max_zoom`, `bounds`, `sha256`, and `bytes`. `publish_release.py` refuses to upload any object at or above that size.
4. **Immutable releases & manifest-last promotion:** builds write into `.staging-<version>`, are renamed to `releases/<version>/`, and `releases/manifest.json` is written last. Uploads put every versioned object first and the manifest last.
5. **Manifest-aware cleanup (no age-based R2 lifecycle rules):** `cleanup` deletes only `/releases/<ver>/` directories that are neither `current_version` nor `previous_version`, and refuses to touch `/media/`, `/exports/`, `/overlays/`, or `/releases/manifest.json`.

## Demo Quickstart (`v0.1.0-demo`, synthetic)

```bash
python3 data-pipeline/sample_data/generate_sources.py
python3 data-pipeline/build_release.py --version v0.1.0-demo --output-root public-r2
python3 data-pipeline/publish_release.py verify-http --root public-r2
python3 -m unittest discover -s data-pipeline/tests -v
```

## Stage 4: Real GBIF + HydroBASINS Pilot Release (`v0.2.0`)

`ingest_gbif_download.py` replaces the synthetic inputs with a real, scoped [GBIF Occurrence Download](https://techdocs.gbif.org/en/data-use/api-downloads) and real [HydroBASINS v1c](https://www.hydrosheds.org/products/hydrobasins) polygons. Run it on a batch machine with outbound access to `api.gbif.org` and `data.hydrosheds.org`. The default output root, `build-r2/`, is git-ignored; real releases never go in the repository.

### 1. Scope the query and check its size

The pilot query is in `queries/pilot_willamette.json`. It covers the same extent as the demo (Willamette basin, Oregon), with georeferenced, present, non-fossil records from 2015 to 2025. `request.predicate` is the exact download predicate. `search_params` is the matching occurrence-search query, used only for the record count.

```bash
python3 data-pipeline/ingest_gbif_download.py count
```

`count` POSTs the exact download predicate to `/v1/occurrence/search/predicate`, so it matches what the download will contain. The pilot scope excludes the eBird Observation Dataset, which is 16.4M of the 17.5M records in this extent (1,112,502 remain as of 2026-11-19). If the count is above 2,000,000 (`--max-records`), narrow `YEAR` or the polygon. The pure-Python builder holds the joined records in memory (about 2 KB per record). For larger scopes, use the tippecanoe/DuckDB path below.

### 2. Request and fetch the download (preserves the live DOI)

```bash
export GBIF_USER=... GBIF_PWD=...      # GBIF.org account; read from the environment only
export GBIF_EMAIL=you@example.org      # optional: GBIF emails you when the download is ready
python3 data-pipeline/ingest_gbif_download.py request          # prints {"key": "00xxxxx-26111..."}
python3 data-pipeline/ingest_gbif_download.py fetch --key <KEY>
python3 data-pipeline/ingest_gbif_download.py fetch-hydrobasins --region na --level 6
```

`fetch` polls `GET /v1/occurrence/download/<key>` until `SUCCEEDED` and streams the zip. It checks the zip's size against GBIF's metadata and runs a zip integrity check. Into `data-pipeline/work/gbif-<key>/` it writes:

| File | Contents |
|---|---|
| `<key>.zip` | The GBIF download (SIMPLE_CSV `<key>.csv`, or DWCA `occurrence.txt`) |
| `download_metadata.json` | Verbatim GBIF download metadata: **DOI**, key, created/modified, license, totalRecords, request |
| `query.json` | The download request/predicate with `creator` and `notificationAddresses` removed |
| `provenance.json` | DOI, key, citation string (`GBIF.org (<date>) GBIF Occurrence Download https://doi.org/<doi>`), zip SHA-256 |
| `SHA256SUMS` | `sha256sum -c`-compatible checksums of the three files above |

`fetch-hydrobasins` downloads `https://data.hydrosheds.org/file/HydroBASINS/standard/hybas_<region>_lev<NN>_v1c.zip` into `work/hydrobasins/`, next to a `.sha256` and a `.source.json`.

### 3. Build the release

```bash
python3 data-pipeline/ingest_gbif_download.py build \
  --gbif-dir data-pipeline/work/gbif-<KEY> \
  --hydrobasins data-pipeline/work/hydrobasins/hybas_na_lev06_v1c.zip \
  --version v0.2.0 --previous-version v0.1.0-demo
```

Pass `--previous-version` set to the version currently live in the target bucket, so that rollback and `cleanup` keep it. The build:

1. Re-verifies `SHA256SUMS` and refuses a source that changed since `fetch`.
2. Keeps HydroBASINS polygons that intersect the query's `WITHIN` polygon (or `--bbox` / `--pfaf-prefix`), then assigns each record with a grid-indexed point-in-polygon join. Records outside every basin are counted as `unassigned_occurrences` in the manifest.
3. Looks up the IUCN Red List category for each `speciesKey` (`GET /v1/species/<key>/iucnRedListCategory`) and dataset titles (`GET /v1/dataset/<key>`). Results are cached in `work/gbif-<key>/enrichment/` and checksummed into the manifest. `threatened_count` counts records of CR/EN/VU species. Pass `--no-enrich` to skip the lookups: `threatened_count` is then 0 and `provenance.iucn_source` says why.
4. Writes these tiles:
   - `basins*.pmtiles`: zoom 0–10, simplified to about 1 px per zoom
   - `occurrences*.pmtiles`: zoom 0–8 `occurrence_bins` (a record count per 1/64-tile cell), and zoom 9–14 record-level `occurrences`. Records on the same tile pixel become one feature with a `count` (stacked site-level coordinates otherwise produce multi-MB tiles). Zooms 9–13 are thinned deterministically to 4,000 locations per tile; zoom 14 keeps every distinct location. For the Willamette pilot, the largest tile is 316 KB.
   Every archive is partitioned below 500 MB.
5. Writes basin shards plus `basins/index.json`. GeoJSON mirrors for the SVG fallback are written only when small (basins ≤ 10 MB, occurrences ≤ 5,000 records). The root `manifest.json` lists a single sample shard so the per-visit manifest stays small. The full list is in `release_manifest.json`.
6. Optional: `--own-dataset-key <basinmap GBIF dataset key>` writes `snapshot_own_occurrence_ids.json`, which the frontend uses to remove approved local observations that already appear in the GBIF snapshot.
7. Optional raster layer: `--raster-tiles-dir <z/x/y.webp tree> --raster-name hillshade --raster-attribution "..."` packs pre-rendered WebP tiles (for example from `gdal_translate`/`gdal2tiles` + `cwebp`) into a WebP raster PMTiles (`tile_type=4`, partitioned the same way). The frontend draws it under the basins. Purely visual layers should use this rather than a browser COG. A numeric/analytical layer would need a COG plus a MapLibre COG protocol, which is not wired up yet.

`run` chains `request → fetch → fetch-hydrobasins → build` in one command.

### 4. Publish to staging first

`wrangler r2 object put` cannot upload objects larger than about 300 MiB. `publish_release.py --mode wrangler` refuses such files, and `--mode rclone` uploads through R2's S3 API (rclone remote named `r2` by default, configured with an R2 API token):

```bash
python3 data-pipeline/publish_release.py publish --root build-r2 --version v0.2.0 \
  --mode rclone --bucket basinmap-public-data-staging --dry-run   # review, then drop --dry-run
python3 data-pipeline/publish_release.py verify-http --base-url https://staging.basinmap.org/api/r2
python3 data-pipeline/benchmark_range_pilot.py --base-url https://staging.basinmap.org/api/r2 \
  --json-out bench-staging.json --markdown-out bench-staging.md
```

`staging.basinmap.org` picks up the new `releases/manifest.json` within 60 s, with no code deploy. Push the frontend changes in this branch to `staging` first so the partition-aware layers are live. After the staging check, publish the same `build-r2/` to `basinmap-public-data` and benchmark `https://data.basinmap.org`. Apply `stage4-deploy-yml.patch` (`git apply stage4-deploy-yml.patch`) so the GitHub Actions deploy step stops republishing the repo's demo release over a bucket that already serves a different `current_version`; without it, the next push to `staging` would reset the bucket manifest to `v0.1.0-demo`.

### Scaling past the pure-Python path

For scopes above about 2 M records, or continental/global builds: run the join in DuckDB Spatial (or GDAL/PostGIS), emit GeoJSONSeq to `tippecanoe` with `--maximum-tile-bytes` and per-zoom-tier output files, and check each `.pmtiles` is below 500 MB. Then write the same `release_manifest.json` schema (`layers.*.partitions`) so that `publish_release.py`, `benchmark_range_pilot.py`, and the frontend work unchanged.

## Stage 7: Global Cyprinodontiformes Release (`v0.3.0`)

Killifishes, livebearers and pupfishes worldwide: GBIF Backbone order **547**, about 1,530 accepted
species and ~694k clean georeferenced records (2026-11-20), so one GBIF download (one DOI) covers it.

What is new in the pipeline:

* `queries/cyprinodontiformes_global.json`: order 547, present, clean coordinates; no fossils or
  living (aquarium) specimens; no year filter, so historical museum specimens are kept.
* `taxonomy.py fetch --taxon-key 547`: pages the GBIF Backbone (`/species/search`) into
  `checklist.json` (accepted species with family/genus/authorship/English names, plus species synonyms).
* `ingest_gbif_download.py build` now accepts several `--hydrobasins` files of one level (all nine
  regions for a global taxon), `--keep-unassigned` (records off every basin polygon such as coasts and
  estuaries stay in the occurrence tiles with an empty `hybas_id`), and `--checklist`, which adds:
  * `species_index.json`: one row per species (key, name, authorship, family, genus, English names,
    IUCN category, occurrence/basin counts, first/last year). Species seen in the download but not
    in the checklist are kept with `"in_checklist": false`.
  * `taxonomy_aliases.json`: normalised name -> accepted species key (accepted names, then synonyms,
    then English names; spellings that point at two species are listed under `ambiguous` instead).
  Both are referenced from the manifest as `layers.species_index` and `layers.taxonomy_aliases`.

### Building it: GitHub Actions (no local commands)

`.github/workflows/build-release.yml` runs count, request, fetch, HydroBASINS, checklist, build,
publish and verify in the cloud. One-time setup: add the repository secrets `GBIF_USER` and
`GBIF_PWD`. Then **Actions -> Build data release -> Run workflow** (defaults: `v0.3.0`, this query,
taxon 547, HydroBASINS level 6 for all regions, basin tiles to zoom 7, target `staging`).

* The release is also kept as a 7-day workflow artifact; untick **publish** to inspect it first.
* Releases are immutable: the job refuses a version already in the bucket.
* `download_key` reuses a finished GBIF download (same DOI) instead of requesting a new one.
* Archives are capped at 290 MB so `wrangler r2 object put` can upload them.
* Run with target `production` only after checking the staging result.

## Benchmark: `benchmark_range_pilot.py`

This script replays the data-origin requests a MapLibre + pmtiles.js browser makes for one visit. The visit follows the same steps as `src/frontend/app.ts`:

1. Bootstrap: manifest, 127-byte range probes, GeoJSON mirrors, approved overlay.
2. Initial fit.
3. Zoom in by 2.
4. Pan.
5. Detail zoom, then detail zoom + 1.
6. Basin shard click.

Each step is run for a desktop (1440×900) and a mobile (390×844) viewport. Vector tiles are 512 px, the first PMTiles read is 16 KiB, leaf directories are cached, and tiles already seen are cached.

Each run has three passes:

- **natural:** URLs as the app sends them.
- **cold:** a unique `?bench=<token>` on every URL, which forces a Cloudflare MISS and an R2 read.
- **warm:** the same URLs again, which should HIT.

For every request it records status, `Content-Range`, bytes, TTFB, total latency, `CF-Cache-Status`, and `Age`. Requests that are not HIT/STALE/UPDATING/REVALIDATED count as R2 Class B operations. These are projected with the Section 7.1 formulas (`Read cost = ceil(max(0, B − 10M)/1M) × $0.36`, `Storage = max(0, ceil(S) − 10) × $0.015`). For `/api/r2` base URLs, the projection also includes Workers Paid request costs.

```bash
python3 data-pipeline/benchmark_range_pilot.py --base-url https://data.basinmap.org \
  --runs 5 --visits 20000 100000 --storage-gb 50 100 --json-out bench.json --markdown-out bench.md
# Above/below the 512 MB CDN ceiling: add --probe /path/to/a-600MB-test-object.bin
python3 data-pipeline/benchmark_range_pilot.py --root build-r2 --runs 1   # offline, no CDN
```

Each cold pass causes about one R2 Class B read per request (roughly 50–60 per visit), which is negligible against the 10 M/month free allowance.

## Stage 4 Results (2026-11-19)

**Status:** code, tests, and staging/production publish commands are complete. The live `v0.2.0` GBIF download and the staging publish have **not run yet**: the build environment used for this change could not reach `api.gbif.org`, `data.hydrosheds.org`, or the R2 S3 endpoint. Steps 1–4 above are the remaining operator actions.

**Verification:**
- `scripts/build_dist.py` passes.
- `npx tsc --noEmit` passes.
- `data-pipeline/tests`: 17 tests pass, including offline GBIF API fakes (async request → RUNNING → SUCCEEDED → zip), a real HydroSHEDS-format shapefile zip, SHA-256 tamper rejection, forced partitioning, leaf directories, WebP raster packing, SQLite shards, and the Section 7.2 cost table.
- `tests/`: 9 tests pass.
- `publish_release.py verify-http --root public-r2` passes.

**Synthetic scale check** (300,000 SIMPLE_CSV records, 400 densified basin polygons, pure Python 3.13):

| Measure | Result |
|---|---|
| Build time | 41.5 s |
| Peak RSS | 581 MB |
| `basins.pmtiles` | 0.55 MB |
| Occurrences total | 27 MB |
| Forced 20 MB budget | Split into `occurrences-p00` (z0–5, 4 KB), `-p01` (z6–12, 12.6 MB), `-p02` (z6–12, 14.6 MB) |
| Offline benchmark of that release | 60 requests/visit (desktop), 47 (mobile); 2.3 MB / 1.6 MB transferred |
| Projected cost, every request reaching R2 | 1.07 M Class B ops/month at 20k visits → $0.60 (50 GB) / $1.35 (100 GB); 5.35 M at 100k visits → same totals, inside the free read allowance |

**Live baseline on the current `v0.1.0-demo`** (`https://data.basinmap.org`, browser `fetch` from the operator's Mac, 3 runs × 3 objects, 16 KiB ranges):

| Pass | CF-Cache-Status | Median latency | Range |
|---|---|---|---|
| natural | 9/9 HIT (`Age` ≈ 2,186 s) | 59 ms | 19–193 ms |
| cold (cache-busted) | 9/9 MISS | 162 ms | 127–240 ms |
| warm (repeat) | 18/18 HIT | 28 ms | 20–148 ms |

All range responses were `206` with correct `Content-Range` and `Cache-Control: public, max-age=31536000, immutable`. This confirms that the cache-busting method yields a true R2 MISS followed by an edge HIT.

**Staging `/api/r2` (Worker proxy):** 4/4 `206` responses at 158–295 ms, with **no `CF-Cache-Status`**. Every staging map request is a Worker invocation plus an R2 read and is never edge-cached. Staging latency is therefore an upper bound and not representative of production. To benchmark staging under production-like caching, attach a custom domain (for example `data-staging.basinmap.org`) to `basinmap-public-data-staging`, and run the same Cache Rule / `r2-cors.json` against it.
