CREATE TABLE IF NOT EXISTS profile_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_hash TEXT NOT NULL UNIQUE,
  content TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','active','superseded')),
  source TEXT NOT NULL DEFAULT 'manual',
  change_summary TEXT,
  created_at TEXT NOT NULL,
  confirmed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_profile
  ON profile_versions(status) WHERE status='active';

CREATE TABLE IF NOT EXISTS paper_feedback(
  identity TEXT PRIMARY KEY,
  interest TEXT CHECK(interest IN ('interested','not_interested') OR interest IS NULL),
  reason TEXT,
  favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0,1)),
  reading_status TEXT NOT NULL DEFAULT 'unread'
    CHECK(reading_status IN ('unread','read','read_later')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(identity) REFERENCES papers(identity) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_feedback_interest ON paper_feedback(interest);
CREATE INDEX IF NOT EXISTS idx_feedback_reading ON paper_feedback(reading_status);

CREATE TABLE IF NOT EXISTS pipeline_runs(
  run_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','succeeded','partial','failed','abandoned')),
  profile_version_id INTEGER,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  collected_count INTEGER NOT NULL DEFAULT 0,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  relevant_count INTEGER NOT NULL DEFAULT 0,
  error_summary TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(profile_version_id) REFERENCES profile_versions(id)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started ON pipeline_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS source_health(
  source TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('healthy','degraded','failed','unknown')),
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_success_at TEXT,
  last_failure_at TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_jobs(
  run_id TEXT PRIMARY KEY,
  profile_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('exported','imported','rejected','abandoned')),
  queue_path TEXT,
  results_path TEXT,
  exported_count INTEGER NOT NULL DEFAULT 0,
  imported_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  imported_at TEXT,
  FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id)
);

