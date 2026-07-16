-- Retained for migration-ledger compatibility.  Migration 011 immediately
-- removes this retired table, so new installations retain no annotation data.
CREATE TABLE IF NOT EXISTS fulltext_annotations(
  id INTEGER PRIMARY KEY,
  identity TEXT NOT NULL REFERENCES papers(identity) ON DELETE CASCADE,
  page_number INTEGER,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fulltext_annotations_identity
  ON fulltext_annotations(identity,page_number,created_at);
