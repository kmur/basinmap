# basinmap.org — Open Hydrological Basin Web-GIS & GBIF Portal

Open-source Web-GIS application and community biodiversity observation publishing platform built for the **GBIF Ebbe Nielsen Challenge**.

- **Live Production:** https://basinmap.org (Spatial R2 endpoint: https://data.basinmap.org)
- **Live Staging:** https://staging.basinmap.org
- **Source Repository:** https://github.com/kmur/basinmap
- **Architecture & Cost Specification:** [hosting-and-gbif-publishing-plan.md](hosting-and-gbif-publishing-plan.md)
- **AI Agent Handoff Guide:** [AGENTS.md](AGENTS.md)

---

## Public Data Policy (GBIF & HydroSHEDS)

`basinmap.org` is an independent open-source project built exclusively on **public scientific datasets** (no proprietary or external association databases):

- **Biodiversity & Taxonomy (GBIF Preferred):** Species checklists, accepted names, synonyms, IUCN Red List categories, and scoped occurrence records are sourced directly from the **GBIF Backbone Taxonomy** and **GBIF Occurrence Downloads** (with preserved download DOIs, query JSON, and SHA-256 checksums).
- **Hydrological Boundaries & Environment:** Watershed polygons, monthly climate averages, and sub-basin habitat attributes are sourced directly from public **HydroSHEDS / HydroBASINS v1c** and **BasinATLAS v1.0** releases under their original CC BY 4.0 and ODbL 1.0 licenses.

---

## Architecture Overview

`basinmap.org` separates read-heavy spatial delivery from transactional observation workflows to keep infrastructure open, portable, and low cost:

1. **Static Frontend (`https://basinmap.org`):** TypeScript + MapLibre GL JS + `pmtiles` served from Cloudflare Workers Static Assets at zero Worker invocation cost.
2. **Same-Origin Worker API (`https://basinmap.org/api/*`):** Routed via `run_worker_first = ["/api/*"]` in `wrangler.jsonc`, backed by Cloudflare D1 (`basinmap-db`), private media intake R2 (`basinmap-intake-media`), and public R2 (`basinmap-public-data`).
3. **Public Spatial & Publication Object Store (`https://data.basinmap.org`):**
   - `/releases/<version>/` — immutable PMTiles v3 archives and pre-aggregated hydrological basin JSON shards (`Cache-Control: public, max-age=31536000, immutable`).
   - `/releases/manifest.json` — release pointer with GBIF download DOI provenance (`Cache-Control: public, max-age=60, must-revalidate`).
   - `/overlays/approved.geojson` — deterministically rebuilt GeoJSON overlay of approved local observations awaiting GBIF snapshot reconciliation (`Cache-Control: public, max-age=60, must-revalidate`).
   - `/media/<uuid>.jpg` — permanent EXIF-stripped public observation images (never deleted by map-release cleanup).
   - `/exports/` — Darwin Core Archive (`dwca-latest.zip`, `eml.xml`) endpoints for GBIF ingestion.
4. **Isolated Staging (`https://staging.basinmap.org`):** Triggered by pushes to the `staging` branch using isolated `basinmap-db-staging`, `basinmap-public-data-staging` (served via `/api/r2/*` with HTTP `206` ranges), and `basinmap-intake-media-staging`.

---

## Repository Structure

```text
.
├── .github/workflows/deploy.yml    # Automated tests, resource provisioning, migrations & deploy (main + staging)
├── AGENTS.md                       # Architectural invariants & handoff instructions for coding agents
├── hosting-and-gbif-publishing-plan.md
├── migrations/
│   ├── 0001_init.sql               # D1 SQLite schema & pilot ORCID users
│   └── 0002_auth_sessions.sql      # Server-side sessions, last login, licence acceptance
├── data-pipeline/                  # Reproducible pure-Python spatial release pipeline
│   ├── pmtiles_writer.py           # PMTiles v3 (leaf dirs, MVT + WebP raster) writer/inspector/range reader
│   ├── build_release.py            # Demo release builder (synthetic v0.1.0-demo)
│   ├── ingest_gbif_download.py     # Real GBIF async download + HydroBASINS join -> v0.2.0 release
│   ├── hydrobasins.py              # Stdlib HydroSHEDS shapefile reader, filter, simplifier, basin index
│   ├── release_tiling.py           # Scalar basin tiles, binned/record occurrence tiles, < 500 MB partitioning
│   ├── benchmark_range_pilot.py    # Cold/warm HTTP 206 + CF-Cache-Status benchmark & R2 cost projection
│   ├── publish_release.py          # R2 publisher (wrangler/rclone), HTTP 206 verifier & manifest-aware cleanup
│   ├── queries/pilot_willamette.json  # Scoped GBIF download predicate for the Stage 4 pilot
│   ├── sample_data/                # Deterministic Willamette Basin demo GBIF + HydroBASINS sources
│   └── tests/                      # test_pipeline.py, test_ingest_and_benchmark.py
├── public-r2/                      # Sample v0.1.0-demo R2 release & overlays/approved.geojson
├── scripts/
│   ├── build_dist.py               # Compiles src/frontend/*.ts to ES2022 JS in ./dist
│   ├── backend_engine.py           # Local SQLite + R2 backend mirroring src/worker/index.ts
│   ├── dev_server.py               # Local HTTP dev server with 206 ranges & API outage simulation
│   └── export_dwca.py              # DwC-A (.zip) exporter, validator, and GBIF Validator / UAT CLI
├── src/
│   ├── frontend/                   # MapLibre GL JS + PMTiles SPA (index.html, styles.css, types.ts, app.ts,
│   │                               #   contrib.ts = submission drawer, uploads, My submissions, reviewer queue)
│   └── worker/                     # index.ts (API & Static Assets), auth.ts (ORCID OAuth + sessions), media.ts (EXIF)
├── tests/                          # Frontend, backend, media, DwC-A, Worker harness (worker_harness.test.mts) & browser E2E
├── package.json
├── r2-cors.json
├── tsconfig.json
└── wrangler.jsonc
```

---

## Local Development & Testing

All local builds, spatial releases, and unit/integration tests run with Python 3.9+ standard library only (no external GIS binaries required for the demo dataset):

```bash
# 1. Build ./dist frontend bundle
python3 scripts/build_dist.py

# 2. Run all automated tests (spatial pipeline + frontend + backend + DwC-A)
python3 -m unittest discover -s data-pipeline/tests -v
python3 -m unittest discover -s tests -v

# 3. Verify HTTP 206 range delivery, ETag, Cache-Control, and CORS headers
python3 data-pipeline/publish_release.py verify-http --root public-r2

# 4. Run local dev server (http://127.0.0.1:8787)
python3 scripts/dev_server.py --port 8787
```

When Node.js is installed, you can also run `npm install`, `npx tsc --noEmit`, `npm run test:worker` (executes the real Worker against an in-memory D1/R2), and `npm run dev`.

Locally (`scripts/dev_server.py`, `ENVIRONMENT=development`) there is no ORCID client: use the **Dev sign-in** menu in the header (pilot contributor / pilot reviewer) to exercise submission and review. Turnstile is bypassed in development only.

---

## Accounts & Spam Protection (Stage 5)

Contributors and reviewers sign in with **ORCID**; their ORCID iD URI becomes Darwin Core `recordedByID` (and `identifiedByID` when a reviewer re-identifies a record). Sessions are stored in D1 and carried in an `HttpOnly; Secure; SameSite=Lax` cookie. Submissions are protected by **Cloudflare Turnstile**, idempotency keys, a 30-per-hour limit, and a 10-photo / 15 MiB-per-photo cap.

One-time setup per environment (values are secrets; never commit them):

1. **ORCID client.** Staging uses the ORCID sandbox (<https://sandbox.orcid.org/developer-tools>, test accounts only); production uses <https://orcid.org/developer-tools>. Register the redirect URI `https://staging.basinmap.org/api/auth/callback` (staging) or `https://basinmap.org/api/auth/callback` (production).
2. **Turnstile.** Staging ships with Cloudflare's always-pass test site key (`1x00000000000000000000AA` in `wrangler.jsonc`); pair it with the matching test secret `1x0000000000000000000000000000000AA`, or create a real widget for `staging.basinmap.org`. Production: create a widget for `basinmap.org`, put its site key in `wrangler.jsonc` → `vars.TURNSTILE_SITE_KEY`, and its secret below.
3. **Secrets** (drop `--env staging` for production):
   ```bash
   npx wrangler secret put ORCID_CLIENT_ID --env staging
   npx wrangler secret put ORCID_CLIENT_SECRET --env staging
   openssl rand -base64 48 | npx wrangler secret put SESSION_SECRET --env staging
   npx wrangler secret put TURNSTILE_SECRET --env staging
   npx wrangler secret put ADMIN_ORCID_IDS --env staging   # comma-separated bare iDs, e.g. 0000-0002-1825-0097
   ```
4. **Reviewers.** IDs in `ADMIN_ORCID_IDS` become admins at sign-in. Promote others after their first sign-in:
   ```bash
   npx wrangler d1 execute basinmap-db-staging --remote --command \
     "UPDATE users SET role = 'reviewer' WHERE orcid_id = 'https://sandbox.orcid.org/0000-0000-0000-0000'"
   ```

Until these are set, the API fails closed: `/api/auth/login` returns `503 orcid_not_configured`, submissions are disabled (`/api/config` → `submissions_enabled: false`), and the map stays fully usable.

---

## Deployment Workflow (`staging` -> `main`)

Saving files locally does not alter live sites until committed and pushed to GitHub:

1. **Test changes on Staging (`https://staging.basinmap.org`):**
   ```bash
   git checkout staging
   git add -A
   git commit -m "Describe your change"
   git push origin staging
   ```
2. **Promote verified changes to Production (`https://basinmap.org`):**
   ```bash
   git checkout main
   git merge staging
   git push origin main
   ```

GitHub Actions ([.github/workflows/deploy.yml](.github/workflows/deploy.yml)) automatically runs all unit tests, `npx tsc --noEmit`, `scripts/build_dist.py`, D1 migrations, R2 release sync, and `wrangler deploy`.

---

## Project Roadmap

| Stage | Status | Scope |
|---|---|---|
| **Stage 1** | Complete & Live | Reproducible spatial pipeline, `v0.1.0-demo` PMTiles & HydroBASINS shards, HTTP `206` check, manifest cleanup |
| **Stage 2** | Complete & Live | Cloudflare Workers Static Assets scaffold, MapLibre GL JS + PMTiles SPA, basin shard inspector, outage resilience |
| **Stage 3** | Complete & Live | D1 observation/revision/media schema, streaming uploads, EXIF stripping, review workflow, DwC-A `.zip` exporter |
| **Stage 4** | Code complete; live `v0.2.0` pending | `ingest_gbif_download.py` (real GBIF DOI download + HydroBASINS), `< 500 MB` partitioning, WebP raster PMTiles, `benchmark_range_pilot.py`; operator steps in [data-pipeline/README.md](data-pipeline/README.md#stage-4-real-gbif--hydrobasins-pilot-release-v020) |
| **Stage 5** | Code complete; secrets pending | ORCID OAuth + D1 sessions, Turnstile, submission drawer with map picking & photo upload progress, My submissions, reviewer queue with EXIF GPS checks (see [Accounts & spam protection](#accounts--spam-protection-stage-5)) |
| **Stage 6** | Planned | In-Worker / scheduled DwC-A `.zip` generation, live GBIF Data Validator + `gbif-uat.org` registration, quiet-hours D1 backups |
| **Stage 7** | Planned | Public GBIF *Cyprinodontiformes* (Killifish) Backbone Taxonomy, IUCN status & GBIF Occurrence Download pipeline |
| **Stage 8** | Planned | Public HydroBASINS v1c (L3/L6/L8) & BasinATLAS v1.0 climate/habitat ingestion + spatial join into `< 500 MB` PMTiles & shards |
| **Stage 9** | Planned | Real OSM/satellite basemaps, genus/synonym/date search, color-coded lettered Species Key & grouped-species tooltips |
| **Stage 10** | Planned | Zoom-adaptive L3→L6→L8 basins, Basin Report (climate seasonality + habitat metrics), Country Browser & static PNG/PDF map export |
