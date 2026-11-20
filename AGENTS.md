# AGENTS.md — Coding Agent Handoff & Architecture Invariants

This repository implements **basinmap.org** (production) and **staging.basinmap.org** (staging). Read this file alongside `README.md` and `hosting-and-gbif-publishing-plan.md` before making changes.

---

## 1. Current State of the Repository

Stages 1–3 are implemented, tested, and deployed live on Cloudflare (`main` -> `https://basinmap.org`, `staging` -> `https://staging.basinmap.org`, public R2 -> `https://data.basinmap.org`):

- **Infrastructure & CI/CD:** `wrangler.jsonc`, `r2-cors.json`, `.github/workflows/deploy.yml`, `package.json`, `tsconfig.json`.
- **Spatial Pipeline (`data-pipeline/`):** Pure-Python PMTiles v3 & MVT v2.1 builder (`pmtiles_writer.py`), reproducible release builder (`build_release.py`), manifest-aware R2 publisher/verifier/cleanup (`publish_release.py`), and sample Willamette Basin `v0.1.0-demo` release in `public-r2/`.
- **Frontend (`src/frontend/`):** MapLibre GL JS + `pmtiles` SPA with interactive SVG fallback, manifest-driven layer loading, basin click -> scalar summary + `/releases/<version>/basins/<hybas_id>.json` shard loading, overlay deduplication by `occurrenceID`, and non-blocking API outage fallback.
- **Worker API & Migrations (`src/worker/index.ts`, `migrations/0001_init.sql`):**
  - `GET /api/health`, `GET /api/config`, `GET /api/r2/*` (used by `staging` with HTTP `206` byte ranges), `GET /api/auth/me`
  - `POST /api/observations` (idempotent, Turnstile check, dataset-wide `CC-BY 4.0` / `CC0 1.0`, sensitive-species 0.1° coordinate generalization)
  - `GET /api/observations/:id` and `PATCH /api/observations/:id` (role-based verbatim filtering, stable `occurrence_id`, revision audit trail)
  - `PUT /api/uploads/:id` (streaming R2 put requiring `Content-Length <= 15 MiB` and magic-byte validation for JPEG/PNG/WebP/HEIC)
  - `POST /api/review/:id/approve` and `/needs_changes` (reviewer/admin role check, EXIF-stripped `/media/<media_id>.jpg` derivative creation, deterministic `overlays/approved.geojson` rebuild)
- **Local Emulator & DwC-A Exporter (`scripts/`):** `build_dist.py`, `backend_engine.py`, `dev_server.py`, `export_dwca.py`.

---

## 2. Mandatory Architectural Invariants (Do Not Violate)

1. **R2 Prefix Isolation & Cleanup (`public-r2/` / `basinmap-public-data`):**
   - Never add age-based R2 Object Lifecycle rules to `/releases/`.
   - Release cleanup must run via `data-pipeline/publish_release.py cleanup`, which reads `releases/manifest.json`, retains only `current_version` and `previous_version`, and refuses to touch `/media/`, `/exports/`, `/overlays/`, or `/releases/manifest.json`.
   - Public observation media referenced by GBIF (`multimedia.txt`) must live permanently under `/media/<media_id>.jpg`, never under `/releases/<version>/`.
2. **Vector Tile & CDN Size Discipline:**
   - Keep `basins.pmtiles` attributes strictly scalar (`occurrence_count`, `species_count`, `threatened_count`, `min_year`, `max_year`, `shard_path`). Never embed species lists inside vector tiles.
   - Keep individual `.pmtiles` and COG objects on `data.basinmap.org` **under 500 MB** (Cloudflare CDN bypasses caching above 512 MB), or route multi-GB layers through a range/tile Worker and benchmark cost.
   - Keep `r2-cors.json` `origins: ["*"]` on `data.basinmap.org` (Cloudflare CDN cache does not partition by `Vary: Origin` by default).
3. **Privacy, Licensing & Overlay Consistency:**
   - Only `CC-BY 4.0` and `CC0 1.0` are accepted so GBIF dataset-level `eml.xml` licensing stays consistent across `occurrence.txt` and Simple Multimedia `multimedia.txt` (`license` and `rightsHolder`).
   - Never leak unapproved observations, raw intake media, or private EXIF GPS coordinates to unauthenticated endpoints or `PUBLIC_BUCKET`.
   - Always rebuild `/overlays/approved.geojson` deterministically via a full `SELECT ... ORDER BY occurrence_id ASC` query (never read-modify-write).
4. **Dual Runtime & CI Compatibility:**
   - Keep `src/frontend/*.ts` valid under **both** `npx tsc --noEmit` (run in GitHub Actions) and `python3 scripts/build_dist.py` (which strips TS syntax to ES2022 modules in `./dist` and checks syntax via JavaScriptCore `jsc -m` when on macOS).
   - Keep `src/worker/index.ts` and `scripts/backend_engine.py` behavior synchronized when adding or modifying API endpoints.
   - Do not remove the `wrangler.jsonc` `"workers_dev": false` / `"preview_urls": false` settings or the `getWorkerSubdomain` `10007` patch in `.github/workflows/deploy.yml` until `wrangler` upstream resolves the `/workers/workers/` 10007 error on new custom-domain Workers.

---

## 3. Required Verification Before Any Commit

Run these commands from the repository root and confirm all checks pass:

```bash
python3 scripts/build_dist.py
python3 -m unittest discover -s data-pipeline/tests -v
python3 -m unittest discover -s tests -v
python3 data-pipeline/publish_release.py verify-http --root public-r2
```

If Node.js is installed locally, also run:

```bash
npx tsc --noEmit
```

Always develop and test on the `staging` branch first (`https://staging.basinmap.org`) before merging into `main` (`https://basinmap.org`).

---

## 4. Remaining Implementation Roadmap

- **Stage 4 — Real GBIF + HydroBASINS Release & Pilot Benchmarks (code complete, live release pending):**
  `data-pipeline/ingest_gbif_download.py` (GBIF async download with DOI/key/query/SHA-256 provenance + HydroBASINS join), `release_tiling.py` (`< 500 MB` partitioning recorded as `layers.*.partitions`), `benchmark_range_pilot.py` (cold/warm 206, `CF-Cache-Status`, Section 7 costs). Remaining: run `count → request → fetch → build` on a machine with GBIF access, publish `v0.2.0` with `publish_release.py --mode rclone` to `basinmap-public-data-staging`, benchmark, then production. Real releases are built into git-ignored `build-r2/`, never committed. Keep new manifests backward compatible: the frontend reads `layers.<name>.partitions` when present and falls back to `layers.<name>.path`.
- **Stage 5 — Contributor Submission & Reviewer UI + Live ORCID OAuth & Turnstile:**
  Build the frontend observation submission modal, drag-and-drop streaming media uploader, ORCID OAuth PKCE/code flow (`src/worker/index.ts`), live Cloudflare Turnstile widget, and reviewer approval queue UI wired to `/api/observations`, `/api/uploads/:id`, and `/api/review/:id/*`.
- **Stage 6 — Automated DwC-A Publishing, GBIF Validation & Off-Provider Backups:**
  Wire DwC-A `.zip` generation into the Worker (or scheduled GitHub Action) writing `/exports/dwca-latest.zip`, validate live against `https://api.gbif.org/v1/validation` and `https://api.gbif-uat.org/v1/dataset`, reconcile GBIF-indexed `occurrenceID`s back into D1 (`publication_status = 'indexed_by_gbif'`), and add a quiet-hours scheduled GitHub Action running `wrangler d1 export` + encrypted off-Cloudflare backup retention.
