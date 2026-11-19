-- basinmap.org D1 Schema & Initial Seed Data
-- Follows Sections 5.1 and 6 of hosting-and-gbif-publishing-plan.md.

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  orcid_id TEXT UNIQUE,
  display_name TEXT NOT NULL,
  email TEXT,
  role TEXT NOT NULL DEFAULT 'contributor' CHECK (role IN ('contributor', 'reviewer', 'admin')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
  occurrence_id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE NOT NULL,
  submitter_user_id TEXT NOT NULL REFERENCES users(id),
  revision_number INTEGER NOT NULL DEFAULT 1,
  review_status TEXT NOT NULL DEFAULT 'submitted'
    CHECK (review_status IN ('draft', 'submitted', 'needs_changes', 'approved')),
  publication_status TEXT NOT NULL DEFAULT 'not_exported'
    CHECK (publication_status IN ('not_exported', 'exported', 'published_by_provider', 'indexed_by_gbif', 'publication_error')),

  -- Verbatim contributor values
  verbatim_scientific_name TEXT NOT NULL,
  verbatim_event_date TEXT NOT NULL,
  verbatim_latitude REAL NOT NULL,
  verbatim_longitude REAL NOT NULL,
  verbatim_coordinate_uncertainty_m INTEGER,
  verbatim_locality TEXT,
  verbatim_notes TEXT,

  -- Interpreted & generalized public Darwin Core fields
  scientific_name TEXT NOT NULL,
  taxon_key INTEGER,
  kingdom TEXT DEFAULT 'Animalia',
  family TEXT,
  iucn_category TEXT,
  is_sensitive_species INTEGER NOT NULL DEFAULT 0 CHECK (is_sensitive_species IN (0, 1)),
  public_latitude REAL NOT NULL,
  public_longitude REAL NOT NULL,
  public_coordinate_uncertainty_m INTEGER NOT NULL DEFAULT 30,
  information_withheld TEXT,
  geodetic_datum TEXT NOT NULL DEFAULT 'EPSG:4326',
  event_date TEXT NOT NULL,
  year INTEGER NOT NULL,
  basis_of_record TEXT NOT NULL DEFAULT 'HUMAN_OBSERVATION',
  hybas_id TEXT,
  locality TEXT,
  recorded_by TEXT NOT NULL,
  recorded_by_id TEXT,
  identified_by TEXT,
  identified_by_id TEXT,

  -- Dataset-level GBIF-compatible license (Section 5.1)
  license TEXT NOT NULL CHECK (license IN ('CC-BY 4.0', 'CC0 1.0')),
  rights_holder TEXT NOT NULL,

  -- Review and publication audit metadata
  reviewer_user_id TEXT REFERENCES users(id),
  review_notes TEXT,
  reviewed_at TEXT,
  exported_dwca_version TEXT,
  gbif_occurrence_key INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observation_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  occurrence_id TEXT NOT NULL REFERENCES observations(occurrence_id),
  revision_number INTEGER NOT NULL,
  actor_user_id TEXT NOT NULL REFERENCES users(id),
  change_type TEXT NOT NULL
    CHECK (change_type IN ('created', 'updated', 'needs_changes', 'approved', 'exported', 'indexed_by_gbif')),
  snapshot_json TEXT NOT NULL,
  notes TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_intake (
  media_id TEXT PRIMARY KEY,
  occurrence_id TEXT NOT NULL REFERENCES observations(occurrence_id),
  uploader_user_id TEXT NOT NULL REFERENCES users(id),
  private_r2_key TEXT NOT NULL UNIQUE,
  public_r2_key TEXT UNIQUE,
  detected_mime_type TEXT NOT NULL
    CHECK (detected_mime_type IN ('image/jpeg', 'image/png', 'image/webp', 'image/heic')),
  byte_length INTEGER NOT NULL CHECK (byte_length > 0 AND byte_length <= 15728640),
  sha256 TEXT NOT NULL,
  exif_gps_latitude REAL,
  exif_gps_longitude REAL,
  exif_datetime TEXT,
  exif_orientation INTEGER DEFAULT 1,
  license TEXT NOT NULL CHECK (license IN ('CC-BY 4.0', 'CC0 1.0')),
  rights_holder TEXT NOT NULL,
  derivative_status TEXT NOT NULL DEFAULT 'private_only'
    CHECK (derivative_status IN ('private_only', 'public_sanitized', 'revoked')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_exports (
  export_id TEXT PRIMARY KEY,
  archive_r2_key TEXT NOT NULL,
  dataset_license TEXT NOT NULL,
  record_count INTEGER NOT NULL,
  media_count INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  validator_job_id TEXT,
  validator_status TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_observations_review_pub
  ON observations (review_status, publication_status);

CREATE INDEX IF NOT EXISTS idx_observations_submitter_created
  ON observations (submitter_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_observations_hybas_review
  ON observations (hybas_id, review_status);

CREATE INDEX IF NOT EXISTS idx_revisions_occ_rev
  ON observation_revisions (occurrence_id, revision_number);

CREATE INDEX IF NOT EXISTS idx_media_occurrence
  ON media_intake (occurrence_id);

-- Seed pilot/development contributor and reviewer accounts with ORCID iDs
INSERT OR IGNORE INTO users (id, orcid_id, display_name, email, role, created_at)
VALUES
  (
    'usr-contrib-1',
    'https://orcid.org/0000-0002-1825-0097',
    'Pilot Field Contributor',
    'contributor@basinmap.org',
    'contributor',
    '2026-11-18T00:00:00Z'
  ),
  (
    'usr-review-1',
    'https://orcid.org/0000-0001-5109-3700',
    'Pilot Taxonomic Reviewer',
    'reviewer@basinmap.org',
    'reviewer',
    '2026-11-18T00:00:00Z'
  );
