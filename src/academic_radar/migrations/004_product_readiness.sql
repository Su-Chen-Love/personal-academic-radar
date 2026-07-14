ALTER TABLE papers ADD COLUMN abstract_source TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE papers ADD COLUMN low_priority INTEGER NOT NULL DEFAULT 0 CHECK(low_priority IN (0,1));
ALTER TABLE papers ADD COLUMN low_priority_reason TEXT;

ALTER TABLE source_runs ADD COLUMN since TEXT;

CREATE TABLE IF NOT EXISTS fulltext_files(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  identity TEXT NOT NULL,
  stored_path TEXT NOT NULL UNIQUE,
  original_name TEXT NOT NULL,
  sha256 TEXT NOT NULL UNIQUE,
  size_bytes INTEGER NOT NULL,
  imported_at TEXT NOT NULL,
  FOREIGN KEY(identity) REFERENCES papers(identity) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_fulltext_identity ON fulltext_files(identity, imported_at DESC);
