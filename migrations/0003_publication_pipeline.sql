-- Stage 6: automated Darwin Core Archive publication, GBIF validation, crawl requests and
-- reconciliation (hosting plan Section 5.2).

-- SHA-256 of occurrence.txt + multimedia.txt: identical content => no new archive / crawl.
ALTER TABLE publication_exports ADD COLUMN content_sha256 TEXT;
-- Immutable copy kept for provenance (exports/archive/<export_id>.zip); never cleaned up.
ALTER TABLE publication_exports ADD COLUMN archive_snapshot_r2_key TEXT;
ALTER TABLE publication_exports ADD COLUMN excluded_license_count INTEGER NOT NULL DEFAULT 0;
-- GBIF Data Validator (POST /v1/validation, poll /v1/validation/{key}).
ALTER TABLE publication_exports ADD COLUMN validator_checked_at TEXT;
ALTER TABLE publication_exports ADD COLUMN validator_summary TEXT;
-- Registry crawl request after a changed export (POST /v1/dataset/{key}/crawl).
ALTER TABLE publication_exports ADD COLUMN crawl_status TEXT;

-- When GBIF reconciliation last saw the record in the registered dataset.
ALTER TABLE observations ADD COLUMN gbif_indexed_at TEXT;

CREATE INDEX IF NOT EXISTS idx_observations_publication ON observations (review_status, publication_status);
CREATE INDEX IF NOT EXISTS idx_publication_exports_created ON publication_exports (created_at);

-- One row per scheduled/manual pipeline run, for operators and the public status file.
CREATE TABLE IF NOT EXISTS publication_runs (
  run_id TEXT PRIMARY KEY,
  trigger TEXT NOT NULL,              -- 'cron' | 'manual'
  started_at TEXT NOT NULL,
  finished_at TEXT,
  export_id TEXT,                     -- NULL when content was unchanged
  summary_json TEXT NOT NULL DEFAULT '{}',
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_publication_runs_started ON publication_runs (started_at);
