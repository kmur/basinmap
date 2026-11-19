# basinmap.org Spatial Data Pipeline (`data-pipeline/`)

Reproducible batch pipeline for preparing immutable spatial releases (`releases/<version>/`), hydrological basin pre-aggregations, and manifest-aware R2 uploads for `https://data.basinmap.org`.

## Design Rules Enforced

1. **Zero external runtime dependencies for the pilot:** All scripts run on standard Python 3.9+ (`pmtiles_writer.py` builds spec-compliant PMTiles v3 + MVT v2.1 archives directly).
2. **Pre-aggregated basins without tile bloat:** `basins.pmtiles` stores only scalar counts (`occurrence_count`, `species_count`, `threatened_count`, `min_year`, `max_year`, `shard_path`). Detailed species lists and temporal histograms live in `releases/<version>/basins/<hybas_id>.json`.
3. **Immutable releases & manifest-last promotion:** Builds validate into `.staging-<version>` first, promote to `/releases/<version>/`, and update `/releases/manifest.json` last.
4. **Manifest-aware cleanup (no age-based R2 lifecycle rules):** `cleanup` deletes only `/releases/<ver>/` directories not referenced as `current_version` or `previous_version` in `manifest.json`, and strictly refuses to touch `/media/`, `/exports/`, `/overlays/`, or `/releases/manifest.json`.

## Local Quickstart

```bash
# 1. Generate deterministic sample GBIF + HydroBASINS source files
python3 data-pipeline/sample_data/generate_sources.py

# 2. Build the immutable v0.1.0-demo release into public-r2/
python3 data-pipeline/build_release.py --version v0.1.0-demo --output-root public-r2

# 3. Verify HTTP 206 byte-range responses, ETag, Cache-Control, and CORS headers
python3 data-pipeline/publish_release.py verify-http --root public-r2

# 4. Run the test suite
python3 -m unittest discover -s data-pipeline/tests -v
```

## Publishing to Cloudflare R2 (`basinmap-public-data`)

```bash
# Preview exact Wrangler upload commands (immutable /releases/v0.1.0-demo/* first, manifest.json last)
python3 data-pipeline/publish_release.py publish --root public-r2 --version v0.1.0-demo --mode wrangler --dry-run

# Upload to live R2 bucket
python3 data-pipeline/publish_release.py publish --root public-r2 --version v0.1.0-demo --mode wrangler

# Verify live HTTP 206 delivery on data.basinmap.org
python3 data-pipeline/publish_release.py verify-http --base-url https://data.basinmap.org

# Dry-run manifest-aware cleanup of releases older than current and previous
python3 data-pipeline/publish_release.py cleanup --root public-r2 --dry-run
```

## Scaling to the Full 50–100 GB Production Dataset

For production GBIF downloads and full global/regional HydroBASINS builds:
1. Run on a dedicated batch machine (not a default CI runner) with sufficient NVMe scratch disk.
2. Perform the point-in-polygon join in DuckDB Spatial or GDAL/PostGIS, emitting GeoJSONSeq into `tippecanoe` (partitioning `.pmtiles` archives below **500 MB** per file by region/zoom tier so Cloudflare's CDN caches range requests directly).
3. Feed the generated `.pmtiles` archives and `basins/*.json` (or partitioned GeoParquet for Levels 7–12) into the same `release_manifest.json` + `publish_release.py` promotion and verification workflow.
