CREATE TABLE IF NOT EXISTS feedback_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  identity TEXT NOT NULL,
  interest TEXT CHECK(interest IN ('interested','not_interested') OR interest IS NULL),
  reason TEXT,
  favorite INTEGER NOT NULL CHECK(favorite IN (0,1)),
  reading_status TEXT NOT NULL CHECK(reading_status IN ('unread','read','read_later')),
  created_at TEXT NOT NULL,
  FOREIGN KEY(identity) REFERENCES papers(identity) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_feedback_events_identity ON feedback_events(identity,created_at DESC);

ALTER TABLE agent_jobs ADD COLUMN profile_version_id INTEGER;
ALTER TABLE agent_jobs ADD COLUMN feedback_snapshot_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE screenings ADD COLUMN profile_version_id INTEGER;
ALTER TABLE screenings ADD COLUMN feedback_snapshot_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE screenings ADD COLUMN run_id TEXT;

