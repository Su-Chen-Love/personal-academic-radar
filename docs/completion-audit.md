# Completion audit

This audit maps the requested outcome to current, inspectable evidence. It is
updated before the project is declared complete.

| Requirement | Status | Evidence |
| --- | --- | --- |
| Independent public repository | Waiting for external authorization | Local repository is independent on `main` with no parent worktree or remote. GitHub account is authenticated, but `Su-Chen-Love/personal-academic-radar` does not yet exist and no public write has been authorized. |
| Reliable Crossref, OpenAlex, and CHI collection | Proven locally | Cursor pagination, retry/backoff, independent provider degradation, DOI/title deduplication, abstract reuse, `source_runs`, and `source_health`; seven configured sources reported healthy in a real run. |
| Codex Automation semantic judgment without another model key | Proven locally | Active `hci` task runs daily at 08:00 and uses export/judge/import. The direct path fails closed without a semantic provider. A real host-model run imported all 102 queued results atomically. |
| Today, Library, Sources, Profile, Feedback, and Status web pages | Proven locally | All six server-rendered pages return HTTP 200 against the private production state. Browser inspection showed real cards, metrics, forms, styling, and no console errors. |
| Interest, non-interest, reason, favorite, read, and read-later feedback | Proven by tests | Current feedback plus append-only events, reason validation, CSRF-protected web forms, filters, and balanced export examples are covered by automated integration tests. |
| Feedback calibration and confirmed profile versions | Proven by tests and queue data | Queues snapshot positive/negative examples and the confirmed profile ID. Profile edits remain drafts until explicit confirmation; drift blocks collection. |
| Migrations, recovery, tests, and real end-to-end verification | Proven locally | Schema v3 migrations, consistent SQLite backup, pre-restore preservation, integrity checks, wheel build, 28 automated tests, real collection, real 102/102 semantic import, and verified digest. |
| Accessible page, real results, and maintainable operation | Proven locally | `http://127.0.0.1:8765` serves 107 papers and 28 relevant results. A healthy per-user launchd service keeps the web app running; `verify`, backup, restore, service install/status/uninstall, and deployment documentation are present. |

The goal must remain active until the public GitHub repository is created and
verified. Public exposure of private profile and feedback data is deliberately
not treated as implicit authorization.
