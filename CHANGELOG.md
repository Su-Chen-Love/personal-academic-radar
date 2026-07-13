# Changelog

All notable changes to Personal Academic Radar are documented here.

## 0.6.0 — 2026-07-13

Initial public release candidate.

### Added

- Cursor-paginated Crossref, OpenAlex, and CHI collection with retries,
  provider-level degradation, DOI/title deduplication, abstract enrichment, and
  persistent source health.
- SQLite schema migrations, online backups, integrity verification, guarded
  restore, and legacy-state migration.
- Codex Automation export/judge/import flow using the host model with no extra
  model API key.
- Atomic semantic imports with complete-queue, profile-version, feedback
  snapshot, run metadata, and stale-job validation.
- Confirmed research-profile versions plus explicit draft and activation flow.
- Interested/not-interested reasons, favorites, unread/read/read-later state,
  append-only feedback history, and balanced calibration examples.
- Six-page local web application: Today, Library, Sources, Research Profile,
  Feedback, and Run Status.
- Safe source preview-before-confirmation flow and configuration backups.
- Loopback-only default binding, CSRF protection, restrictive browser headers,
  and reversible macOS launchd service management.
- Installation verification, packaging checks, unit/integration tests, and
  end-to-end operational documentation.

### Removed

- Silent heuristic fallback when no semantic model provider is available.
- API-key-dependent GitHub Actions monitoring and the obsolete direct-run
  macOS script from the supported daily workflow.
