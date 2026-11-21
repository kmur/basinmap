# AGENTS.md — Coding Agent Handoff & Architecture Invariants

This repository implements **basinmap.org** (production) and **staging.basinmap.org** (staging). Read this file alongside `README.md` and `hosting-and-gbif-publishing-plan.md` before making changes.

---

## 1. Current State of the Repository

Stages 1–3 are implemented, tested, and deployed live (Stage 4's `v0.2.0` release is live on staging; Stages 5–6 are code complete) on Cloudflare (`main` -> `https://basinmap.org`, `staging` -> `https://staging.basinmap.org`, public R2 -> `https://data.basinmap.org`):

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
- **Stage 5 — Auth, Submission & Review (code complete; needs ORCID/Turnstile secrets per environment):**
  - `src/worker/auth.ts`: ORCID OAuth (`GET /api/auth/login`, `GET /api/auth/callback`, `POST /api/auth/logout`), D1-backed sessions (`migrations/0002_auth_sessions.sql`), `__Host-bm_session` cookie.
  - `src/worker/media.ts`: magic bytes, private EXIF extraction, JPEG stripping.
  - New routes: `GET /api/observations/mine`, `GET /api/review/pending`, `GET /api/media/:media_id` (private intake media).
  - `src/frontend/contrib.ts`: Submit drawer, uploads with progress, My submissions, Reviewer queue.
  - `tests/worker_harness.test.mts` executes the real Worker under Node (D1 via `node:sqlite`).

---

## 2. Mandatory Architectural Invariants (Do Not Violate)

1. **R2 Prefix Isolation & Cleanup (`public-r2/` / `basinmap-public-data`):**
   - Never add age-based R2 Object Lifecycle rules to `/releases/`.
   - Release cleanup must run via `data-pipeline/publish_release.py cleanup`, which reads `releases/manifest.json`, retains only `current_version` and `previous_version`, and refuses to touch `/media/`, `/exports/`, `/overlays/`, or `/releases/manifest.json`.
   - Public observation media referenced by GBIF (`multimedia.txt`) must live permanently under `/media/<media_id>.jpg`, never under `/releases/<version>/`.
   - `/exports/archive/<export_id>.zip` snapshots are immutable and never deleted; `dwca-latest.zip`, `eml.xml` and `status.json` are overwritten by the publication run.
2. **Vector Tile & CDN Size Discipline:**
   - Keep `basins.pmtiles` attributes strictly scalar (`occurrence_count`, `species_count`, `threatened_count`, `min_year`, `max_year`, `shard_path`). Never embed species lists inside vector tiles.
   - Keep individual `.pmtiles` and COG objects on `data.basinmap.org` **under 500 MB** (Cloudflare CDN bypasses caching above 512 MB), or route multi-GB layers through a range/tile Worker and benchmark cost.
   - Keep `r2-cors.json` `origins: ["*"]` on `data.basinmap.org` (Cloudflare CDN cache does not partition by `Vary: Origin` by default).
3. **Privacy, Licensing & Overlay Consistency:**
   - Only `CC-BY 4.0` and `CC0 1.0` are accepted so GBIF dataset-level `eml.xml` licensing stays consistent across `occurrence.txt` and Simple Multimedia `multimedia.txt` (`license` and `rightsHolder`).
   - Never leak unapproved observations, raw intake media, or private EXIF GPS coordinates to unauthenticated endpoints or `PUBLIC_BUCKET`.
   - Always rebuild `/overlays/approved.geojson` deterministically via a full `SELECT ... ORDER BY occurrence_id ASC` query (never read-modify-write).
4. **Authentication, Abuse Protection & Attribution:**
   - Dev bearer tokens (`dev-contributor-orcid`, `dev-reviewer-orcid`, `user:<id>`) are accepted **only** when `ENVIRONMENT=development` (wrangler dev env and `scripts/dev_server.py`). Staging/production authenticate solely via the `__Host-bm_session` cookie (`HttpOnly; Secure; SameSite=Lax; Path=/`).
   - Sessions live in D1 (`sessions.token_hash` = SHA-256 of the random token; the cookie carries `token.HMAC(SESSION_SECRET)`). Never store raw session tokens or ORCID access tokens.
   - Cookie-authenticated `POST`/`PATCH`/`PUT` must pass the same-origin `Origin` check (`checkMutationOrigin`).
   - Turnstile fails **closed** outside development (no `TURNSTILE_SECRET` → 503). Idempotent replays are answered **before** Turnstile, and only to the submitting account (otherwise 409).
   - `recordedByID` comes only from the authenticated user's ORCID iD URI; reviewer re-identification sets `identifiedBy`/`identifiedByID`. Never accept these from the request body.
   - Submissions require `licenseAccepted: true` (stored as `license_accepted_at`). UI copy must never imply GBIF indexing on submission; show `review_status` and `publication_status` separately.
5. **Independent Project & Public Data Sourcing Only (GBIF Preferred):**
   - `basinmap.org` is a standalone, independent open-source project.
   - **Never** copy, import, or bundle external association species databases, KMI locality catalogues, or third-party plugin data/assets.
   - **Never** reference `AKA`, `American Killifish Association`, `KMI`, `JAKA`, or `BNL` in filenames, code identifiers, schemas, comments, data files, or UI labels.
   - Source all species checklists, taxonomic synonyms, IUCN categories, and occurrence records strictly from **public scientific datasets — primarily the GBIF Backbone Taxonomy API (`/v1/species`) and GBIF Occurrence Downloads (`/v1/occurrence/download`)** — paired with public **HydroSHEDS / HydroBASINS / BasinATLAS** watershed and environmental datasets.
6. **Dual Runtime & CI Compatibility:**
   - Keep `src/frontend/*.ts` valid under **both** `npx tsc --noEmit` (run in GitHub Actions) and `python3 scripts/build_dist.py` (which strips TS syntax to ES2022 modules in `./dist` and checks syntax via JavaScriptCore `jsc -m` when on macOS).
   - Keep `src/worker/index.ts` and `scripts/backend_engine.py` behavior synchronized when adding or modifying API endpoints (same JSON shapes via `serializeObservation` / `_row_to_public_dict`), and cover Worker changes in `tests/worker_harness.test.mts`.
   - Worker modules import siblings with explicit `.ts` extensions and use `import type` for interfaces (required for Node type stripping in the harness; esbuild/wrangler accept both).
   - `scripts/build_dist.py` uses a small custom TypeScript stripper (output is syntax-checked with Node/JSC). In `src/frontend/*.ts` avoid: `let x: T;` without an initializer, function-typed parameters (`cb: () => void`; use `any`), `type` aliases, enums, parameter properties, and `import type`.
   - Publication-status transitions (`exported`, `indexed_by_gbif`) must never change `observations.updated_at` (Darwin Core `modified`) and must never regress `indexed_by_gbif`. Keep `src/worker/publication.ts` and `scripts/export_dwca.py` producing identical `meta.xml` and column order (enforced by `tests/test_prompt6_publication.py`).
   - Backups are encrypted before leaving the runner; never upload a plaintext D1 dump anywhere.
   - `migrations/*.sql` are applied in order by wrangler (D1) and by `backend_engine.init_db` (tracked in `_local_migrations`); add new files, never edit applied ones.
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

`tests/test_prompt5_auth_review.py` runs `node --test tests/worker_harness.test.mts` (Node ≥ 22.18). The browser end-to-end test `tests/test_prompt5_ui_e2e.py` runs when Python `playwright` and Chrome/Chromium are installed (after `npm install`); otherwise it is skipped.

If Node.js is installed locally, also run:

```bash
npx tsc --noEmit
```

Always develop and test on the `staging` branch first (`https://staging.basinmap.org`) before merging into `main` (`https://basinmap.org`).

---

## 4. Remaining Implementation Roadmap

- **Stage 4 — Real GBIF + HydroBASINS Release & Pilot Benchmarks (code complete, live release pending):**
  `data-pipeline/ingest_gbif_download.py` (GBIF async download with DOI/key/query/SHA-256 provenance + HydroBASINS join), `release_tiling.py` (`< 500 MB` partitioning recorded as `layers.*.partitions`), `benchmark_range_pilot.py` (cold/warm 206, `CF-Cache-Status`, Section 7 costs). Remaining: run `count → request → fetch → build` on a machine with GBIF access, publish `v0.2.0` with `publish_release.py --mode rclone` to `basinmap-public-data-staging`, benchmark, then production. Real releases are built into git-ignored `build-r2/`, never committed. Keep new manifests backward compatible: the frontend reads `layers.<name>.partitions` when present and falls back to `layers.<name>.path`.
- **Stage 5 — Contributor Submission & Reviewer UI + Live ORCID OAuth & Turnstile (code complete):**
  Remaining: register ORCID sandbox (staging) and production clients, set the Worker secrets listed in README.md, promote reviewer accounts, verify sign-in → submit → review on `https://staging.basinmap.org`, then merge to `main`. Non-JPEG photos stay private on approval until an Images binding (`IMAGES`) is configured.
- **Stage 6 — Automated DwC-A Publishing, GBIF Validation & Off-Provider Backups (code complete):**
  `src/worker/publication.ts` (cron `17 10 * * *` + `POST /api/admin/publication/run`), `src/worker/overlay.ts`, `migrations/0003_publication_pipeline.sql`, Python mirror in `scripts/export_dwca.py run`, restore drill `scripts/backup_restore_check.py`, backup Action in `ops/github-workflows/d1-backup.yml`. Remaining: set `GBIF_USER`/`GBIF_PWD` Worker secrets, register the UAT dataset and set `env.staging.vars.GBIF_DATASET_KEY`, copy the backup workflow into `.github/workflows/` with `BACKUP_AGE_RECIPIENT`, confirm a crawl on UAT, then production registration.
- **Stage 7 — Public GBIF *Cyprinodontiformes* (Killifish) Taxonomy, IUCN & Occurrence Pipeline (code complete):**
  `data-pipeline/taxonomy.py` (GBIF Backbone checklist for order 547 -> `species_index.json` + `taxonomy_aliases.json`), `queries/cyprinodontiformes_global.json`, multi-region `--hydrobasins`, `--keep-unassigned` and `--checklist` in `ingest_gbif_download.py build`, and `.github/workflows/build-release.yml` (cloud build + publish; needs `GBIF_USER`/`GBIF_PWD` repository secrets). Remaining: run the workflow for `v0.3.0` to staging, review, then production. The frontend does not read `species_index.json` yet (Stage 9 search).
- **Stage 8 — Public HydroSHEDS / HydroBASINS Level 3/6/8 & BasinATLAS Environment Pipeline (code complete):**
  Ingest authoritative public **HydroBASINS v1c** and **BasinATLAS v1.0** datasets directly from HydroSHEDS (`hydrosheds.org` / Figshare DOIs), preserving proper attribution (`CC BY 4.0` for BasinATLAS/WorldClim/EarthEnv/GLC2000/Human Footprint and `ODbL 1.0` for SoilGrids1km), and spatially join GBIF *Cyprinodontiformes* records into `< 500 MB` Level 3/6/8 `.pmtiles` + pre-aggregated basin shards.
  Implemented in `data-pipeline/hydroatlas.py` (Figshare download + MD5, streaming column-selective DBF extract, CC BY columns only), `data-pipeline/basin_levels.py` (per-level join; Level 8 shards grouped by Level 6 parent) and `ingest_gbif_download.py build --extra-hydrobasins --basinatlas-dir`; `build-release.yml` inputs `extra_levels`/`basinatlas`. Remaining: run `v0.4.0` to staging, confirm BasinATLAS storage factors from `environment.observed_ranges` against the BasinATLAS Catalog, then production. Frontend display of levels and environment is Stage 10.
- **Stage 9 — Real Basemaps, Dataset Selector, Genus/Synonym Search & Lettered Species Key (code complete):**
  Add OpenStreetMap street + optional satellite/terrain backgrounds in MapLibre GL JS, scale bar, fit-to-results and fullscreen controls, family/genus/species/GBIF-synonym search (from `species_index.json` / `taxonomy_aliases.json`), and deterministic color-coded/lettered species markers with an interactive Species Key and grouped-species tooltips.
  **Dataset selector:** let visitors choose among several published datasets side by side (e.g. "Killifish: worldwide" `v0.3.x` and "All species: Willamette" `v0.2.0`) instead of one live release at a time. A small `releases/datasets.json` catalog (id, title, taxon/geographic scope, current version, manifest path, default flag) is written by `build-release.yml`/`switch-release.yml` per dataset; the frontend lists it in the Layers panel, remembers the choice in the URL (`?dataset=`), and loads that dataset's manifest. `releases/manifest.json` stays as the default dataset for backward compatibility. Each dataset keeps its own immutable release history.
  Implemented in `src/frontend/explore.ts` (pure, unit-tested in `tests/explore.test.mts`) and wired in `app.ts`: basemaps (EOX Terrain Light default, OpenStreetMap, Sentinel-2 cloudless 2016 CC BY 4.0, plain), navigation/fullscreen/scale controls, taxon search with keyboard support, up-to-26-species lettered key with show/hide, zoom-to-results from per-species bboxes, grouped hover tooltips, `releases/datasets.json` selector (`?dataset=`), and an error handler that no longer drops to the SVG fallback on tile/glyph errors. Pipeline: points carry `speciesKey`, overview tiles carry a `species_bins` layer, and `species_index.json` rows carry `bbox` (all from the next release build). `switch-release.yml` registers extra datasets; `build-release.yml` updates the catalog. Remaining: build `v0.5.0` so search works at world zoom with exact synonym matching, and register Willamette (`v0.2.0`) as a second production dataset.

- **Stage 10 — Interactive Basin Report, Monthly Climate/Habitat Charts, Country Browser & Static Export:**
  Add zoom-adaptive HydroBASINS Level 3 → 6 → 8 display toggles, a Basin Report drawer (species checklist, 12-month BasinATLAS air temperature/precipitation seasonality chart with clear whole-basin methodological notes, and 5 habitat indicators), a public country/continent GBIF fauna browser, and a printable PNG/PDF map export modal.
