CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS papers(
  identity TEXT PRIMARY KEY,
  doi TEXT,
  title TEXT NOT NULL,
  abstract TEXT,
  venue TEXT,
  published TEXT,
  url TEXT,
  authors_json TEXT,
  first_seen TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations(
  identity TEXT NOT NULL,
  source TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  PRIMARY KEY(identity, source),
  FOREIGN KEY(identity) REFERENCES papers(identity)
);
CREATE TABLE IF NOT EXISTS screenings(
  identity TEXT NOT NULL,
  profile_hash TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT,
  relevant INTEGER NOT NULL,
  score REAL NOT NULL,
  reasons TEXT,
  themes_json TEXT,
  confidence REAL,
  screened_at TEXT NOT NULL,
  PRIMARY KEY(identity, profile_hash, provider, model)
);
CREATE TABLE IF NOT EXISTS notifications(
  identity TEXT PRIMARY KEY,
  sent_at TEXT NOT NULL,
  digest_path TEXT
);
CREATE TABLE IF NOT EXISTS source_runs(
  run_id TEXT NOT NULL,
  source TEXT NOT NULL,
  status TEXT NOT NULL,
  count INTEGER NOT NULL,
  error TEXT,
  finished_at TEXT NOT NULL,
  PRIMARY KEY(run_id, source)
);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_seen ON papers(first_seen);

