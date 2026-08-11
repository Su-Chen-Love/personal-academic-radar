ALTER TABLE screenings ADD COLUMN reasoning_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE screenings ADD COLUMN score_dimensions_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE screenings ADD COLUMN rubric_version TEXT NOT NULL DEFAULT 'legacy';
