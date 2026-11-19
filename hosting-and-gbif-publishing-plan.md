# Hosting and GBIF publishing plan

**Project:** Open Web-GIS application for a GBIF Ebbe Nielsen Challenge entry  
**Prepared:** 18 November 2026  
**Status:** Proposed architecture for external review; no infrastructure has been deployed or benchmarked  
**Currency:** USD, before taxes; published prices checked on the preparation date

## 1. Recommendation and review purpose

Build an open-source mapping application with two distinct workloads: predominantly static map delivery and a small transactional service for new observations. Use GitHub for source control, Cloudflare Workers Static Assets for the frontend, R2 Standard storage for prepared spatial layers and media, and a Worker API with D1 for submissions. Resolve the GBIF publishing arrangement in parallel with application development.

Use the Workers Free plan during the prototype if its limits are sufficient. For an initial public service, budget for the $5/month Workers Paid plan and its higher database and execution limits. This is a planning choice, rather than a claim that the free plan cannot handle the traffic.

Cloudflare is the proposed initial host because the workload suits inexpensive static delivery and object storage. The application, data formats, and build process should remain portable. Cloudflare is a proprietary hosting service; the openness of this project will come from its code, licenses, documentation, data provenance, and ability to run elsewhere.

The external reviewer should assess whether this design can meet the stated workload, whether the cost model captures the important variables, and whether the publication and operational arrangements are sufficient for launch. All performance targets below are proposed acceptance criteria, not measured results.

## 2. Requirements, assumptions, and unresolved inputs

| Item | Current basis | Planning implication |
|---|---|---|
| Priorities | Open, fast, and low cost; no provider preference | Favor standard formats, reproducible builds, and measured costs |
| Initial spatial data | Approximately 50–100 GB | Store large layers in object storage |
| Monthly users | Approximately 1,000 | No per-user hosting charge is assumed; identity pricing is separate |
| Map visits | Approximately 20,000/month | Model requests and transferred bytes per complete visit |
| Data change rate | Owner estimates 99% remains unchanged | Use versioned snapshots with periodic refreshes |
| Contributions | Users should submit new records through the app | Provide persistent storage, validation, review, and publication tracking |
| GBIF publishing organization | Not yet identified | Treat a confirmed publishing route as a launch dependency for GBIF publication |
| Contributor volume and media | Unknown | Use scenarios; measure during the pilot |
| Geography and peak concurrency | Unknown | Test multiple regions and agree a representative peak |
| Heavy spatial analysis | Not yet required | Keep GDAL processing and large joins in a batch pipeline initially |

For costing, 50–100 GB means the live, prepared serving dataset. Original downloads, historical releases, staging copies, media, and backups are additional unless the owner intended that figure to cover everything. Confirm this interpretation before procurement.

The 99% figure is a project assumption, not a guarantee about GBIF. Corrections, deletions, taxonomic changes, and new occurrences still require a refresh process. A map visit includes initial display and subsequent layer selection, panning, and zooming; it is not one HTTP request.

## 3. Proposed architecture

```mermaid
flowchart TD
    GH[Public GitHub repository] --> BUILD[Frontend build and deployment]
    BUILD --> FE[Workers Static Assets]
    FE --> APP[Browser mapping application]
    APP -->|Direct range reads| DATA[Public R2 spatial layers]
    APP -->|Submission and status requests| API[Worker API]
    API --> DB[Private D1 observation database]
    API --> MEDIA[Private R2 incoming media]
    DB --> REVIEW[Contributor and reviewer workflow]
    REVIEW --> EXPORT[Approved Darwin Core export]
    EXPORT --> PUB[Registered publisher and publication endpoint]
    PUB --> GBIF[GBIF ingestion and discovery]
    SNAP[Versioned source downloads] --> BATCH[Reproducible batch preparation]
    BATCH --> DATA
```

| Component | Responsibility | Initial implementation |
|---|---|---|
| Frontend | Map, filters, record display, contribution forms | TypeScript and MapLibre GL JS; static build output |
| Spatial delivery | Serve only the relevant tiles or raster blocks | R2 custom domain with HTTP range support and deliberate cache rules |
| Submission API | Authentication checks, input validation, writes, status lookup | Small Worker with a documented HTTP API |
| Transactional database | Observations, identifiers, revisions, review state, publication jobs | D1; bulk GBIF records stay outside this database |
| Media storage | Evidence files, approved public derivatives, publication archives | Separate private and public R2 buckets or clearly separated stores |
| Batch preparation | Download, transform, tile, summarize, validate, and release data | Versioned scripts on a suitable local machine or batch runner |
| Publishing integration | Deliver approved data to the GBIF network | Prefer a partner's hosted IPT; support other agreed dataset endpoints |

Cloudflare recommends Workers Static Assets for new projects. Ordinary asset requests are free, but invoking Worker code introduces a different usage model. Keep static frontend paths out of unnecessary Worker execution. [Deployment guidance][S01] [Asset billing][S02]

`basinmap.org` is registered on a Cloudflare-managed zone. Configure the domain and bucket layout as follows, preserving any existing website and email DNS records during setup:

- `https://basinmap.org`: Workers Static Assets serving the frontend SPA with zero Worker invocation cost.
- `https://basinmap.org/api/*`: Same-origin Worker API routed with `run_worker_first = ["/api/*"]`, avoiding CORS preflight overhead on submissions and keeping `HttpOnly; Secure; SameSite=Lax` session cookies simple.
- `https://data.basinmap.org`: Public R2 Custom Domain (`basinmap-public-data`) for spatial layers, overlays, approved public media, and Darwin Core exports. A production R2 endpoint must use a custom domain rather than `r2.dev`, which is a rate-limited development endpoint. [R2 public access][S03]
- Private R2 bucket (`basinmap-intake-media`): Bound exclusively to the Worker (`env.INTAKE_BUCKET`) with no public hostname and no public CORS policy.

Within the public R2 bucket, enforce strict top-level key prefix separation so automated map-release cleanup never threatens permanent GBIF publication assets:

- `/releases/<version>/`: Immutable spatial layers and basin summaries (`Cache-Control: public, max-age=31536000, immutable`).
- `/releases/manifest.json`: Mutable pointer to the active release (`Cache-Control: public, max-age=60, must-revalidate`).
- `/overlays/approved.geojson`: Mutable public overlay of approved local observations awaiting GBIF snapshot reconciliation (`Cache-Control: public, max-age=60, must-revalidate`).
- `/media/<uuid>.<ext>`: Permanent approved public observation media referenced by GBIF records; never touched by release cleanup.
- `/exports/`: Darwin Core Archive (`dwca.zip`) and `eml.xml` endpoints; never touched by release cleanup.

## 4. Spatial data preparation and delivery

### 4.1 Versioned, reproducible releases

Obtain a suitably scoped GBIF download rather than repeatedly paging through occurrence search results to reconstruct the full dataset. Preserve the download DOI, query, download date, source files needed for rebuilding, transformation parameters, and checksums. GBIF's download API provides asynchronous downloads and associated citation information. [GBIF downloads][S04]

Each map release should have an immutable version directory (`/releases/<version>/`) and a small manifest (`/releases/manifest.json`) that records source versions, spatial coverage, counts, licenses, attribution, and layer URLs. Build into a new directory, validate it, and update the manifest only after completion. Retain the previous release so rollback is a manifest change. Do not depend on overwriting a large archive while browsers are reading its byte ranges.

Perform release retention cleanup using a **manifest-aware post-release script** that inspects `manifest.json`, keeps the `current` and `previous` version directories, and explicitly deletes only older `/releases/<version>/` prefixes (never touching `/media/`, `/exports/`, or `/overlays/`). Do **not** rely on age-based R2 Object Lifecycle rules to purge `/releases/`: lifecycle rules evaluate object age rather than release count, so pausing monthly builds would risk deleting the live production release.

Proposed initial refresh policy: inspect for relevant updates weekly and publish a refreshed snapshot monthly, with an exceptional rebuild for important corrections or withdrawals. Confirm the cadence with the scientific owner. Do not assume a reliable incremental-change feed exists for every source; the first pipeline should support a complete scoped rebuild.

### 4.2 Vector and raster formats

Use PMTiles for suitable vector layers, including occurrence visualization and reference boundaries. Choose zoom ranges and generalization rules that prevent dense areas from overwhelming the browser. Broad-scale occurrence display should use appropriate aggregation; preserve record-level access and source data for detailed inspection.

Use tiled, overview-equipped COGs when browser raster access is appropriate, such as layers where the browser must inspect raw floating-point or multi-band pixel values. MapLibre requires a compatible COG integration; hosting a TIFF alone is insufficient. Validate the actual coordinate system, compression, bands, nodata values, and mobile decoding behavior. For purely visual raster layers (such as hillshade, land cover, or display surfaces), evaluate WebP-encoded raster `.pmtiles` archives alongside COGs during the pilot: prepared raster PMTiles reduce granular range-request amplification and mobile decoding overhead compared to client-side COG decoding. [MapLibre COG example][S05]

Generalized display geometry and aggregated tiles are presentation products. Analyses requiring original coordinates, geometry, or raster precision must use appropriate source data. Document that distinction in the application's methods.

Define the supported filters and analyses before accepting the static design. Precomputed layers can support many display filters and prepared summaries, but arbitrary polygon counts, buffers, spatial joins, or unrestricted combinations may need additional indexing or computation. A low data change rate does not establish that every query can be answered cheaply. Confirm these interactions in the pilot.

Because `basinmap.org` organizes data around hydrological basins, pre-aggregate basin statistics during the batch pipeline: store scalar summary counts directly in PMTiles vector feature properties for immediate map styling and tooltips, while keeping variable-length species lists and temporal summaries out of the vector tiles to avoid tile bloat. Serve those detailed summaries either as individual static JSON shards for coarser basin levels (global HydroBASINS counts are roughly 292 at Level 3, 1.3k at Level 4, 4.7k at Level 5, and 16k at Level 6—substantially fewer if regionally scoped) or inside regionally partitioned GeoParquet/SQLite files for fine sub-basins (Levels 7–12) to avoid generating hundreds of thousands of small objects and consuming Class A write allowances.

For record inspection, either retain sufficient snapshot fields in accessible prepared products or provide a documented lookup strategy. If current GBIF API details are used, distinguish them from the versioned map snapshot. Core map display should continue during an external record-detail service outage, with the affected detail view explaining its availability.

### 4.3 Range requests, CORS, and caching

Large map archives belong in R2. Workers Static Assets has a 25 MiB per-file limit and does not natively honor byte-range requests with partial responses. Its ordinary response to a range request is the full asset. [Workers limits][S06] [Range behavior][S02]

PMTiles depends on correct range and cross-origin behavior. Because `https://basinmap.org` and `https://data.basinmap.org` are separate origins, configure the public R2 bucket CORS policy explicitly with:

- `AllowedOrigins: ["*"]` — Cloudflare's CDN cache does not partition cached responses by `Vary: Origin` by default; listing multiple explicit origins (such as production plus `http://localhost`) risks serving a cached response with the wrong `Access-Control-Allow-Origin` header.
- `AllowedMethods: ["GET", "HEAD"]`
- `AllowedHeaders: ["Range", "If-Match"]`
- `ExposeHeaders: ["ETag", "Content-Length", "Content-Range"]`

Verify this configuration using browser requests from deployed production and local/preview origins. [PMTiles storage guidance][S07] [R2 CORS][S08]

Set explicit `Cache-Control` metadata on every R2 object at upload time: long-lived immutable headers (`public, max-age=31536000, immutable`) on `/releases/<version>/*` and permanent `/media/*`, short TTLs (`public, max-age=60, must-revalidate`) on `/releases/manifest.json` and `/overlays/approved.geojson`, and `private, no-store` responses on authenticated API endpoints. Enable **Cloudflare Smart Tiered Cache** (free) to improve regional cache hit rates on long-tail tile requests, and create an explicit **Cloudflare Cache Rule** for `data.basinmap.org` marking `.pmtiles`, `.tif`, `.fgb`, `.geojson`, and `.json` as eligible for cache while honoring origin `Cache-Control` headers. Do not assume unfamiliar extensions are cached automatically.

Cloudflare's ordinary CDN cacheable-file ceiling is 512 MB on Free, Pro, and Business plans. When a browser issues a byte-range request for a file larger than 512 MB on an R2 custom domain, the edge skips caching and forwards every range request to R2. [CDN behavior and limits][S09]

Test both warm and cold delivery in the pilot and evaluate the two concrete strategies for staying within budget and latency targets:

1. **Partition archives below 500 MB:** Split large `.pmtiles` or COG layers by region, basin, or zoom tier under 500 MB so Cloudflare's standard CDN caches them directly on `data.basinmap.org` with zero Worker invocations (trading off extra client-side source/seam management).
2. **Tile/Range Worker for single multi-GB archives:** Route tile requests through a stateless Worker backed by R2 bindings when a unified archive is preferred. Note that standard `caches.default` lookups inside a Worker still invoke—and bill—the Worker on every cache hit (roughly $9/month overage on Workers Paid at 40 million tile requests/month). Test Cloudflare's opt-in cache-in-front-of-Worker setting during the pilot to determine whether cached tile responses can bypass Worker invocations. Do not proxy map reads through the submission API.

## 5. Observation collection and GBIF publication

### 5.1 Collection model

Give each observation a stable project identifier suitable for use as `occurrenceID`. Store original contributor input, the interpreted values, revision history, attribution, license choice, evidence links, and workflow status. Keep the observation identifier stable when a record is corrected.

Because GBIF only accepts `CC0 1.0`, `CC-BY 4.0`, or `CC-BY-NC 4.0` and applies a single license at the dataset level in `eml.xml` (mixed record licenses either force the entire dataset to the most restrictive choice or require separate datasets per license), adopt a single project-wide contribution license (recommended: `CC-BY 4.0` or `CC0 1.0`) that contributors explicitly accept for both occurrence records and uploaded media. Record this cleanly in the Darwin Core `license` field and the Simple Multimedia extension (`multimedia.txt`) `license` and `rightsHolder` fields.

Capture the appropriate Darwin Core fields for the agreed record type: taxon/name information, event date, basis of record, coordinates, datum, uncertainty, locality, observer, identification information, and supporting evidence as relevant. Preserve verbatim values alongside normalized values. Define required fields with the eventual publisher rather than treating every possible Darwin Core term as mandatory.

Separate review status from publication status. Suggested values include `draft`, `submitted`, `needs_changes`, and `approved` for review; and `not_exported`, `exported`, `published_by_provider`, `indexed_by_gbif`, and `publication_error` for delivery. A successful form submission must not be presented as confirmation of GBIF indexing.

Pending records should be visible to their contributor and authorized reviewers. Serve the public overlay of approved local observations statically from R2 (`https://data.basinmap.org/overlays/approved.geojson`, `Cache-Control: public, max-age=60, must-revalidate`) rather than querying D1 on every public map visit, and label its provisional status accurately. Rebuild `approved.geojson` deterministically from a full `SELECT` query in D1—debounced via a Queue or scheduled cron job—rather than using read-modify-write updates, preventing concurrent reviewer approvals from overwriting each other. Reconcile exported records with later GBIF versions using stable source identifiers so a local overlay and a GBIF snapshot do not display the same occurrence twice.

### 5.2 Publishing route

GBIF's normal ingestion model consumes datasets supplied through publishing organizations. Its public occurrence API is not a general insert endpoint for arbitrary observations. IPT is a common publishing mechanism, and custom systems can also serve standards-compliant archives from a web server. [GBIF publication model][S10]

Preferred route: build automated Darwin Core Archive generation (`.zip` containing `eml.xml`, `meta.xml`, `occurrence.txt`, and `multimedia.txt`) directly to `https://data.basinmap.org/exports/` and validate it continuously against the **GBIF Data Validator API** so technical readiness and Ebbe Nielsen Challenge demonstration do not stall on external server access. In parallel, establish a relationship with a registered, endorsed publishing organization—either to import that archive into an existing hosted IPT in the relevant country or community, or to register the custom R2 DwC-A endpoint directly under their organization. The IPT manual recommends hosted arrangements to reduce operating effort, and a dataset discoverable on GBIF must carry the required organizational endorsement and registered installation regardless of delivery mechanism. [IPT prerequisites][S11]

Begin with a documented, manually supervised export/import or registration process. Automate recurring updates only after the partner confirms supported import mechanisms, update schedules, validation rules, and responsibility for failed publications. Do not assume every IPT exposes a record-writing API suitable for the app.

For end-to-end engineering testing without polluting production GBIF data, validate registration and ingestion against the GBIF test environment (`gbif-uat.org` / `api.gbif-uat.org`, or `gbif-test.org`, using a sandbox test organization and installation obtained via `helpdesk@gbif.org`). Operating a self-hosted IPT server remains a last-resort third route that introduces separate server maintenance and cost and is not inherent to the recommended hosting design.

Publication acceptance requires an end-to-end demonstration with an authorized publisher: submit, review, export, publish, confirm GBIF discovery, and process a correction while retaining the source identifier. The partner agreement should identify who manages updates, withdrawals, licensing, and continuity of the dataset endpoint. No GBIF publishing partner has yet been arranged.

Keep published archive and media URLs stable for as long as their records reference them. Storing approved public media under `/media/<uuid>.<ext>` and archives under `/exports/`—completely separate from `/releases/<version>/`—ensures map-release cleanup rules never delete objects referenced by a published dataset. Check that any proposed hosting or publisher migration preserves those references and the organization's ability to maintain the dataset.

## 6. Backend, database, and access controls

Use the database for the relatively small volume of project contributions and workflow metadata. D1 is not the proposed store for the entire 50–100 GB map dataset, nor a substitute for PostGIS when complex spatial computation is required. Keep database access behind the API, use indexed queries, paginate results, and limit query scope.

Relevant current database allowances are:

| Metric | Workers Free / D1 | Workers Paid / D1 |
|---|---:|---:|
| Maximum individual database | 500 MB | 10 GB |
| Included storage across the account | 5 GB | 5 GB |
| Included row reads | 5 million/day | 25 billion/month |
| Included row writes | 100,000/day | 50 million/month |
| Point-in-time recovery window | 7 days | 30 days |

These are different limits: a free account's 5 GB allowance does not make a single free database 5 GB. Row scans and index maintenance affect consumption; one observation is not necessarily one billed write. [D1 limits][S12] [D1 pricing][S13]

Select a mature authentication implementation after confirming contributor access needs. Prefer a standard identity integration with a replaceable adapter; **ORCID OAuth** is a strong fit for biodiversity contributions because authenticated ORCID iDs map directly to Darwin Core `recordedByID` and `identifiedByID`. Require stronger authentication for reviewers and administrators. If passwordless magic links or account verification emails are used, budget for an external transactional email provider (such as Resend or Postmark), since Cloudflare Email Routing does not send outbound email to arbitrary addresses. Identity, verification email, and account recovery costs are not yet included in the hosting estimate.

Enforce ownership and reviewer permissions server-side. Protect submission and upload endpoints with **Cloudflare Turnstile**, request throttling, input validation, and idempotency keys to avoid abuse and duplicate submissions on retries. For incoming evidence media, stream uploads directly through the Worker (`PUT /api/uploads/:id` → `env.INTAKE_BUCKET.put(key, request.body)`) rather than issuing S3 presigned URLs to browsers; this avoids exposing CORS on the private intake bucket and reliably enforces upload policies. Reject streaming uploads that lack a valid `Content-Length` header (required by R2 stream puts) or exceed the size cap (for example, 15 MB), verify the true file format from leading magic bytes rather than trusting the declared `Content-Type`, and prevent permanent storage credentials from reaching browsers.

Define handling for sensitive species locations and personal information before enabling public contributions. Retain original uploaded files (such as HEIC, JPEG, PNG, or WebP) and extracted EXIF GPS/timestamp evidence in the private intake bucket for reviewer verification. Upon approval, generate sanitized public derivatives under `/media/<uuid>.jpg` (or `.webp`) using **Cloudflare Images transformations with `metadata: "none"`** (or an equivalent WebAssembly image pipeline) so EXIF `Orientation` is baked into upright pixel data, mobile formats such as HEIC are converted cleanly for browsers, and all private EXIF tags are stripped. Public mapping may also need generalized coordinates while restricted reviewers retain originals. Pending records, private media, authentication material, and database backups must never enter the public GitHub repository or a public bucket.

## 7. Cost model

### 7.1 Published rates and calculation basis

R2 Standard currently includes 10 GB-month, 1 million Class A operations, and 10 million Class B operations monthly. Excess storage is $0.015/GB-month; excess Class A operations are $4.50/million and Class B operations $0.36/million. Outgoing R2 bandwidth has no egress charge. Usage is rounded to billing units. [R2 rates][S14]

For the scenarios below:

```text
S = average billable storage in GB-month
B = monthly Class B operations reaching R2
A = monthly Class A operations

Storage cost = max(0, ceil(S) - 10) × $0.015
Read cost    = ceil(max(0, B - 10,000,000) / 1,000,000) × $0.36
Write cost   = ceil(max(0, A - 1,000,000) / 1,000,000) × $4.50
```

The model assumes one shared allowance across project usage, Standard storage, no other account consumption, and writes within the allowance. Reads include metadata requests and retries, not just visible tiles. Browser/CDN hits that avoid R2 reduce this count.

### 7.2 Map traffic sensitivity

Calculated at 20,000 visits/month; the read counts are scenarios, not predictions:

| R2 reads per visit | Monthly reads | R2 total: 50 GB | R2 total: 100 GB | 100 GB plus $5 Workers subscription |
|---:|---:|---:|---:|---:|
| 100 | 2 million | $0.60 | $1.35 | $6.35 |
| 500 | 10 million | $0.60 | $1.35 | $6.35 |
| 1,000 | 20 million | $4.20 | $4.95 | $9.95 |
| 2,000 | 40 million | $11.40 | $12.15 | $17.15 |
| 5,000 | 100 million | $33.00 | $33.75 | $38.75 |

The final column assumes API/database consumption remains within the paid plan's included usage. It excludes all items in section 7.4. This table is calculated from the cited rates and does not represent a fixed hosting quotation.

### 7.3 API and submission workload

Workers Free permits 100,000 dynamic requests/day and 10 ms CPU per invocation. Workers Paid starts at $5/month and includes 10 million requests and 30 million CPU milliseconds monthly; excess requests are $0.30/million and excess CPU $0.02/million milliseconds. [Workers pricing][S15]

As an illustration, 500,000 monthly API calls averaging 3 ms CPU consume 1.5 million CPU milliseconds and fit those paid allowances. This is a model input, not a measured benchmark. Direct R2 reads are separate from API invocations. Routing all spatial reads through a Worker would require adding those invocations and their CPU use to the model.

Contributor count does not establish media volume. An illustrative 1,000 new observations/month with two 2 MB photos each adds about 4 GB/month, or 48 GB after a year if retained. Thumbnails, originals, backups, and publication copies add further capacity. Database sizing must include revisions, indexes, and audit history.

### 7.4 Costs outside the base table

Budget separately for retained map releases, source archives, staging, independent backups, new media, domain renewal, identity/email services, paid basemaps, monitoring, batch computing, and any separately operated publishing server. Developer time, moderation, scientific review, and publishing support are operating costs even when a partner supplies infrastructure.

Initial recurring infrastructure planning allowance: **$10–$25/month** for moderate reads, bounded retention, and a small paid backend. This is an engineering budget with contingency, not a cap or complete project operating budget. The high-read scenario already exceeds it. Confirm the unknown services and pilot usage before presenting a total cost commitment.

Alert on forecast spend and unexpected operations. Distinguish notifications from enforceable spending controls: a Worker spending limit does not cap R2 storage or operation charges. Document which service would stop and what users would experience before enabling any automatic cutoff.

## 8. Alternatives and reasons for the initial choice

| Alternative | Where it is strong | Important tradeoff for this project |
|---|---|---|
| GitHub Pages + R2 | Free static frontend close to the public source repository | Still needs a submission backend and external large-data storage |
| GitHub Pages + Bunny Storage/CDN | Portable static delivery with bandwidth-based pricing and no CDN request fee | Adds delivered-byte charges and needs a backend elsewhere |
| S3 + CloudFront | Mature hosting and a path toward broader AWS services | More configuration; origin/storage/API charges still need modeling |
| Hostinger managed cloud | Managed website hosting, including surrounding WordPress content | Dataset capacity and renewal price matter; complex backend capabilities need verification |
| Hostinger VPS | Control over PostGIS, GDAL, IPT, or a custom tile server | Server administration, patching, recovery, and capacity planning become project work |

GitHub Pages is suitable for a small frontend, but its published site limit is 1 GB and its soft bandwidth allowance is 100 GB/month. It cannot host the proposed 50–100 GB dataset as a Pages site. [GitHub limits][S16]

Bunny's single-region Standard storage is $0.01/GB-month with a $1 storage minimum. Standard CDN delivery is $0.01/GB in North America/Europe, with different regional rates; Volume delivery starts at $0.005/GB using fewer locations. At 100 GB stored and 2 TB delivered, an illustrative total is $21/month on Standard NA/EU delivery or $11/month on Volume. These figures exclude a submission backend. [Bunny storage][S17] [Bunny CDN][S18]

CloudFront Pro currently costs $15/month, with a 50 TB delivery allowance, 10 million requests, and credits corresponding to 50 GB of S3 storage. It has no delivery overage charges under the plan, but extra origin storage and other AWS services can still cost more. Confirm required cache/header features for the GIS client before selecting a flat-rate tier. [CloudFront pricing][S19]

Hostinger Cloud Startup advertises 100 GB at $7.99/month equivalent for 48 months, renewing at $25.99; the initial commitment is $383.52. VPS KVM 4 advertises 200 GB and 16 TB bandwidth at $12.99/month equivalent for 24 months, renewing at $28.99; initial commitment is $311.76. A 100 GB dataset needs headroom beyond a nominal 100 GB plan. Offers and existing account entitlements vary. [Hostinger cloud/web pricing][S20] [Hostinger VPS][S21]

The proposed Cloudflare design is an initial economic and operational choice, not a claim of universally fastest delivery. If the pilot shows unfavorable cold-range performance or an extremely high request count relative to bytes delivered, compare Bunny and AWS using the same recorded workload.

## 9. Openness, portability, and exit plan

Publish the frontend, API domain logic, schema migrations, preprocessing scripts, dependency lockfiles, and deployment instructions under explicit open-source licenses. Retain source data licenses and attribution; do not relabel third-party data under the software license. Supply a small reusable demonstration dataset so a reviewer can reproduce the application without obtaining the full 100 GB.

Keep layer URLs in configuration and serve standard PMTiles, COG, CSV/GeoJSON, and Darwin Core products as appropriate. Avoid hard-coding provider credentials or account identifiers into scientific logic. Document basemap, font, glyph, and sprite dependencies and their terms and costs.

| Dependency | Expected migration work |
|---|---|
| Static frontend | Deploy the same build output to another static host |
| R2 data | Copy objects with S3-compatible tooling; change configured URLs and CORS |
| Worker API | Adapt request handling and bindings to another runtime |
| D1 | Export SQL, restore into a compatible database, and adapt the database access layer |
| CDN/DNS configuration | Recreate cache, range, header, and domain settings |
| Identity integration | Move credentials/configuration and verify account continuity |

D1 supports database export, which should be tested against a local SQLite restoration. Exportability does not eliminate runtime and SQL compatibility work. [D1 import/export][S22]

An exit exercise should demonstrate the frontend and a small archive on an alternative host, plus API/database operation outside Cloudflare. A fully open-source hosting platform is a different requirement from an open-source, portable application; if it becomes mandatory, reassess toward a maintained server deployment.

## 10. Deployment, maintenance, and recovery

Use separate development, staging, and production databases and buckets. Preview deployments must use synthetic or explicitly approved public data. Production publishing credentials belong only in restricted runtime or release environments.

Keep code deployment separate from large-data preparation. Run frontend checks and builds in CI. Start large spatial builds on a machine with measured disk, memory, and runtime capacity; do not assume a normal CI runner can accommodate the download and intermediate files. Record resource use during the pilot before selecting a paid batch service.

Deploy data to new versioned keys, run format and HTTP checks, then switch the manifest. Roll back frontend and data versions independently. Use backward-compatible schema migrations and test them against a restored database before production.

Assign named owners for infrastructure/billing, scientific data stewardship, publication, and incident response. The same person may initially hold several roles, but access recovery must not depend on a single unmanaged account.

Use D1 Time Travel (point-in-time recovery, 30 days on Workers Paid) for immediate operational rollback alongside independent encrypted SQL exports (`wrangler d1 export`) stored off-provider. Because `wrangler d1 export` blocks **all** requests (both reads and writes) to the database while generating the dump, schedule automated exports during low-traffic quiet hours. Proposed starting retention is seven daily, four weekly, and three monthly database backups, subject to the final privacy/deletion policy. Copy irreplaceable media and its manifest independently; keep reproducible source material or an independent copy for map releases. A second bucket in the same account is insufficient protection against complete account loss.

Proposed recovery objectives are at most 24 hours of lost accepted submissions and restoration of core service within one business day. These are targets requiring a tested off-provider backup process, not vendor guarantees. Perform a restore exercise before launch and quarterly thereafter; adjust targets if the operating team cannot support them.

Monitor map availability, API errors, database scans/size, R2 operations, client failures, backup age, publication lag, and monthly spend. On failure, the public map should remain usable when the submission API is unavailable, while forms clearly report failure and avoid implying that unsaved work was accepted.

## 11. Pilot validation and acceptance criteria

Use a dense occurrence region, representative boundaries, and the largest intended raster. Test a complete user journey: open map, change layers, pan/zoom, inspect a record, submit an observation with evidence, review it, and export it. Record actual usage rather than extrapolating from a single initial page load.

| Check | Proposed acceptance evidence |
|---|---|
| Range delivery | Correct 206 responses, byte offsets, response length, and CORS from deployed browser origins |
| Initial map | Target p75 useful-map display within 3 seconds on a defined 10 Mbps / 100 ms test connection |
| Exploration | Stable interaction on an agreed midrange mobile device without loading entire large archives |
| Large archives | Cold/warm results measured above and below the ordinary CDN cache-size threshold |
| API | Target p95 lightweight status/write response within 1 second, excluding media transfer and external services |
| Peak load | Initial exercise with 25 active map sessions and five simultaneous submitters; revise after geographic/peak inputs are known |
| Cost | Observed operations/bytes/CPU used to replace scenario assumptions and identify a high-usage case |
| Record integrity | Retry-safe submission, stable identifiers, complete revision history, and no double display after reconciliation |
| Publication | Authorized end-to-end dataset publication and later correction confirmed through GBIF |
| Recovery | Database and media restored independently and rollback demonstrated |
| Portability | Small deployment reproduced outside the primary provider |

Test failures should drive specific changes: smaller partitions for range/cache problems, fewer initial layers for slow first display, indexed/paginated database queries for high scan counts, and a separate batch/server service for genuinely heavy computation. No performance advantage over another provider is claimed before this exercise.

## 12. Delivery sequence and external review decisions

1. **Confirm scope and stewardship.** Resolve whether 50–100 GB includes source/history/backups, contributor/media volumes, target regions, record types, data licenses, and ownership. Start publisher discussions.
2. **Run a spatial hosting pilot.** Build a representative immutable release, test R2 range/CORS/cache behavior, benchmark the browser, and measure request/byte costs. Compare another provider if the result is weak.
3. **Build the static application and repeatable release process.** Establish repository documentation, local execution, sample data, CI, staging, manifest switching, and attribution.
4. **Implement submissions and review.** Add identity, database, private uploads, validation, idempotency, status tracking, exports, and recovery. Validate permissions and failure behavior.
5. **Connect publication.** Agree the publisher/import process, prove GBIF ingestion and record correction, and reconcile local versus indexed records. Until then, label records awaiting publication accurately.
6. **Launch and measure.** Confirm owners, recovery evidence, live monitoring, and an updated budget. Revisit traffic, media retention, and publication lag after the first month.

The reviewer is asked to resolve these decisions explicitly:

| Decision | Proposed default | Evidence or owner needed |
|---|---|---|
| Provider and architecture | Cloudflare stack on `basinmap.org` (Workers Static Assets + same-origin `/api/*`) and `data.basinmap.org` (R2) | Pilot measurements and portability demonstration |
| Query and analysis scope | Prepared display filters plus pre-aggregated hydrological basin summaries (counts in PMTiles; species lists in shards/GeoParquet) | Confirm arbitrary spatial queries before excluding a spatial backend |
| Public backend plan | Workers Paid at $5/month | Expected database size, CPU, and operational needs |
| Raster strategy | WebP raster `.pmtiles` for visual layers; browser COGs only where raw pixel inspection is required | Largest-raster and mobile tests |
| Archive partitioning | `< 500 MB` partitions on `data.basinmap.org` vs. Tile/Range Worker (test cache-in-front-of-Worker) | Cold-cache, request count, and regional pilot results |
| Publishing route | Automated DwC-A `.zip` export to `data.basinmap.org/exports/` + GBIF Data Validator + `gbif-uat.org` testing, alongside partner publisher endorsement | Named partner organization and confirmed update process |
| Data and media licensing | Single dataset-wide contribution license (`CC-BY 4.0` or `CC0 1.0`) covering records and media | Confirm single license choice before building contributor forms |
| Identity and contributor access | Replaceable standard integration (ORCID OAuth recommended) + Turnstile + external transactional email if needed | Access model, email flow, and cost |
| Privacy and location release | Private intake via Worker streaming upload + Cloudflare Images (`metadata: "none"`) public derivatives under `/media/` | Scientific steward and data policy |
| Recurring budget | $10–$25/month initial infrastructure allowance (with manifest-based release cleanup) | Complete retention/media/service assumptions |
| Recovery and maintenance | D1 Time Travel + quiet-hours off-provider exports; assigned owners | Restore exercise and operating commitment |
| Need for a VPS/PostGIS | Add when a demonstrated feature needs it | Workload beyond static products and simple transactional queries |

This document requests architectural review. No provider account, payment commitment, production deployment, publishing registration, or contact with an external organization has been made as part of preparing it.

## Sources

Links are primary provider, GBIF, or project documentation checked on 18 November 2026. Rates, limits, and product names can change; confirm applicable account terms when implementing.

[S01]: https://developers.cloudflare.com/workers/best-practices/workers-best-practices/
[S02]: https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/
[S03]: https://developers.cloudflare.com/r2/buckets/public-buckets/
[S04]: https://techdocs.gbif.org/en/data-use/api-downloads
[S05]: https://maplibre.org/maplibre-gl-js/docs/examples/add-a-cog-raster-source/
[S06]: https://developers.cloudflare.com/workers/platform/limits/
[S07]: https://docs.protomaps.com/pmtiles/cloud-storage
[S08]: https://developers.cloudflare.com/r2/buckets/cors/
[S09]: https://developers.cloudflare.com/cache/concepts/default-cache-behavior/
[S10]: https://techdocs.gbif.org/en/data-publishing/
[S11]: https://ipt.gbif.org/manual/en/ipt/latest/how-to-publish
[S12]: https://developers.cloudflare.com/d1/platform/limits/
[S13]: https://developers.cloudflare.com/d1/platform/pricing/
[S14]: https://developers.cloudflare.com/r2/pricing/
[S15]: https://developers.cloudflare.com/workers/platform/pricing/
[S16]: https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits
[S17]: https://bunny.net/pricing/storage/
[S18]: https://bunny.net/pricing/cdn/
[S19]: https://aws.amazon.com/cloudfront/pricing/
[S20]: https://www.hostinger.com/pricing
[S21]: https://www.hostinger.com/vps-hosting
[S22]: https://developers.cloudflare.com/d1/best-practices/import-export-data/

## Revision Log

- **Baseline (Commit 1):** Recorded original proposed architecture and GBIF publishing plan verbatim prior to domain and review updates.
- **Refinements (Commit 2):**
  - Locked in `basinmap.org` (Workers Static Assets + same-origin `/api/*` via `run_worker_first`) and `data.basinmap.org` (R2 Custom Domain), plus strict R2 prefix separation (`/releases/<version>/`, `/overlays/approved.geojson`, `/media/`, `/exports/`).
  - Replaced age-based R2 lifecycle cleanup guidance with a manifest-aware post-release script so paused builds cannot delete the live release and permanent `/media/` and `/exports/` paths remain untouched.
  - Added exact public R2 CORS policy (`AllowedOrigins: ["*"]`, exposing `ETag`, `Content-Length`, and `Content-Range`), Smart Tiered Cache, Cache Rule coverage for `.pmtiles`/`.json`/`.geojson`, and pilot comparison between `< 500 MB` partitions and a Tile Worker (including cache-in-front-of-Worker testing and default Worker invocation billing).
  - Specified WebP raster `.pmtiles` for visual rasters, HydroBASINS pre-aggregation (counts in vector tiles, species lists in shards/GeoParquet), static deterministic rebuilds for `/overlays/approved.geojson`, single dataset-level licensing (`CC-BY 4.0` or `CC0 1.0`) mapped to DwC `license` and Simple Multimedia `license` + `rightsHolder`, and direct DwC-A `.zip` export with GBIF Data Validator and `gbif-uat.org` testing alongside publisher endorsement.
  - Added ORCID OAuth, Turnstile, Worker-streamed uploads (`Content-Length` + magic-byte checks), Cloudflare Images (`metadata: "none"`) EXIF/HEIC derivative generation, outbound email provider budgeting, and `wrangler d1 export` full-database blocking scheduling guidance.
