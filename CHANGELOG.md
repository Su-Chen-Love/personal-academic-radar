# Changelog

All notable changes to Personal Academic Radar are documented here.

## 0.8.0 — 2026-07-14

### Added

- Traceable, retryable abstract enrichment across same-DOI local records,
  Crossref, OpenAlex, Semantic Scholar, Europe PMC, PubMed, and verified
  publisher structured metadata, plus strict JSON/CSV evidence import.
- Publication-type evidence and allowlist governance with recoverable cleanup
  previews, verified online backups, audit reports, quarantine, and low-score
  exclusion from all normal product views.
- Live accessible source search, candidate preview, duplicate protection, safe
  source removal, background task progress, and direct retry actions.
- Compact Today/Library cards, expandable abstracts and details, persistent
  favorites, combined sorting/filtering, and one page-level PDF import dialog.
- One-command `academic-radar setup` with legacy-state detection, migration,
  verification, and reversible macOS background-service installation.

### Changed

- “Today” now means newly selected papers from the latest successful Codex
  import, not every record whose screening timestamp happens to be today.
- Feedback management is interactive while append-only history remains an
  internal recovery detail; source health is no longer a user-facing product
  concept.
- The supported product boundary is explicitly local-only, single-user, and
  private SQLite. Cloud synchronization and public/multi-user deployment are
  paused.

### Removed

- All legacy direct model-provider configuration, routes, code,
  tests, UI, and documentation. Legacy `[llm]` sections are backed up and
  removed during initialization.
- Manual ISSN/OpenAlex/query source entry and the page-refreshing
  `/sources/match` workflow.
- Repeated PDF upload forms on every card and user-facing feedback history.

## 0.7.0 — 2026-07-14

### Added

- Idempotent repair for partially upgraded legacy databases and automatic
  online backups before initialization upgrades.
- Abstract-source and coverage diagnostics, DOI-based OpenAlex repair command,
  low-priority filtering, and confidence caps when abstracts are missing.
- Journal-name candidate discovery through Crossref/OpenAlex with real metadata
  preview and cautious conference matching.
- Full-text PDF import with size/type validation, normalized filenames, SHA-256
  reuse, and correct many-paper bindings.
- Friendly server-rendered error pages and actionable Run Status checks for
  schema, integrity, profiles, source health, semantic coverage, abstracts,
  background service mode, and optional provider state.
- macOS service install/status/restart/log/uninstall commands, log archival, and
  detection when the per-user service is running a different state config.
- AI-assisted external-user installation prompt, Linux/Windows background
  service guidance, and an executable cloud/authentication/sync roadmap.

### Fixed

- Legacy states with papers but no profile ledger now repair safely on init/web
  startup instead of causing template or semantic-state failures.
- SQLite connections are closed during exceptional semantic import/export paths.
- Reinstalling a launchd service now waits for asynchronous bootout and retries
  bootstrap, avoiding transient I/O failures.
- Old service logs are archived before a new service starts so resolved
  tracebacks do not look like current failures.

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
