CREATE TABLE fulltext_files_new(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  identity TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  original_name TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  imported_at TEXT NOT NULL,
  FOREIGN KEY(identity) REFERENCES papers(identity) ON DELETE CASCADE,
  UNIQUE(identity, sha256)
);

INSERT INTO fulltext_files_new(
  id,identity,stored_path,original_name,sha256,size_bytes,imported_at
)
SELECT id,identity,stored_path,original_name,sha256,size_bytes,imported_at
FROM fulltext_files;

DROP TABLE fulltext_files;
ALTER TABLE fulltext_files_new RENAME TO fulltext_files;
CREATE INDEX idx_fulltext_identity ON fulltext_files(identity, imported_at DESC);
CREATE INDEX idx_fulltext_sha256 ON fulltext_files(sha256);
