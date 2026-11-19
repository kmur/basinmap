# basinmap.org — Open Hydrological Basin Web-GIS & GBIF Portal

Open-source Web-GIS application and community biodiversity observation publishing platform built for the **GBIF Ebbe Nielsen Challenge**.

- **Live Production:** https://basinmap.org (Spatial R2 endpoint: https://data.basinmap.org)
- **Live Staging:** https://staging.basinmap.org
- **Source Repository:** https://github.com/kmur/basinmap
- **Architecture & Cost Specification:** [hosting-and-gbif-publishing-plan.md](hosting-and-gbif-publishing-plan.md)
- **AI Agent Handoff Guide:** [AGENTS.md](AGENTS.md)

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
│   └── 0001_init.sql               # D1 SQLite schema & pilot ORCID users
├── data-pipeline/                  # Reproducible pure-Python spatial release pipeline
│   ├── pmtiles_writer.py           # PMTiles v3 & MVT v2.1 protobuf encoder/inspector
│   ├── build_release.py            # Spatial join, HydroBASINS pre-aggregation & manifest builder
│   ├── publish_release.py          # R2 publisher, HTTP 206 verifier & manifest-aware cleanup
│   ├── sample_data/                # Deterministic Willamette Basin demo GBIF + HydroBASINS sources
│   └── tests/test_pipeline.py
├── public-r2/                      # Sample v0.1.0-demo R2 release & overlays/approved.geojson
├── scripts/
│   ├── build_dist.py               # Compiles src/frontend/*.ts to ES2022 JS in ./dist
│   ├── backend_engine.py           # Local SQLite + R2 backend mirroring src/worker/index.ts
│   ├── dev_server.py               # Local HTTP dev server with 206 ranges & API outage simulation
│   └── export_dwca.py              # DwC-A (.zip) exporter, validator, and GBIF Validator / UAT CLI
├── src/
│   ├── frontend/                   # MapLibre GL JS + PMTiles SPA (index.html, styles.css, types.ts, app.ts)
│   └── worker/index.ts             # Cloudflare Worker API & Static Assets entrypoint
├── tests/                          # Automated frontend, backend, media & DwC-A test suites
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

When Node.js is installed, you can also run `npm install`, `npx tsc --noEmit`, and `npm run dev`.

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
| **Stage 4** | Planned | Real scoped GBIF download + HydroBASINS dataset (+ optional raster `.pmtiles`), `< 500 MB` partitioning & cold/warm pilot benchmarks |
| **Stage 5** | Planned | Contributor submission & photo upload UI, reviewer queue UI, live ORCID OAuth & Cloudflare Turnstile integration |
| **Stage 6** | Planned | In-Worker / scheduled DwC-A `.zip` generation, live GBIF Data Validator + `gbif-uat.org` registration, quiet-hours D1 backups |
