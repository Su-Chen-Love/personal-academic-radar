ALTER TABLE papers ADD COLUMN published_precision TEXT NOT NULL DEFAULT 'unknown'
    CHECK (published_precision IN ('day', 'month', 'year', 'unknown'));
