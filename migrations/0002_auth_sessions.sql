-- Stage 5: ORCID OAuth sessions, login audit, and explicit license acceptance.
-- Sessions are server-side so logout and role changes take effect immediately; the
-- browser only holds an HMAC-signed random token in an HttpOnly cookie, and the
-- database stores the token's SHA-256 hash (a leaked table cannot be replayed).

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id, expires_at);

ALTER TABLE users ADD COLUMN last_login_at TEXT;

-- When the contributor explicitly accepted the dataset-wide license (Section 5.1).
ALTER TABLE observations ADD COLUMN license_accepted_at TEXT;
