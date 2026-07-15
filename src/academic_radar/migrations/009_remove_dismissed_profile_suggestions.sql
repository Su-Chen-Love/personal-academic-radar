UPDATE profile_review_runs
SET profile_version_id = NULL
WHERE status = 'dismissed'
  AND profile_version_id IN (
    SELECT id FROM profile_versions
    WHERE source = 'feedback-ai' AND status = 'superseded'
  );

DELETE FROM profile_versions
WHERE source = 'feedback-ai'
  AND status = 'superseded'
  AND id NOT IN (
    SELECT profile_version_id
    FROM profile_review_runs
    WHERE profile_version_id IS NOT NULL
  );
