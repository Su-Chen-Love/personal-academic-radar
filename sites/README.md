# Personal Academic Radar · Sites

Cloud companion for the local Python application. Papers, daily recommendations,
original abstracts and public source status are available anonymously. The
owner's research profile, feedback and modification API require ChatGPT sign-in.
Identity comes from Sites dispatch; the app does not implement OAuth routes.

## Build and check

```bash
npm ci
npm run db:generate  # only after changing db/schema.ts
node scripts/test-worker.mjs
npm run build
npm run validate
```

The Worker uses the logical `DB` D1 binding. Drizzle migrations contain schema
only and are included in the deployment artifact. Runtime secrets `SYNC_TOKEN`
and `OWNER_EMAIL` must be configured through Sites, never committed here.

## Updates

The existing local daily task collects configured public scholarly sources,
verifies publisher metadata, enriches traceable abstracts and runs Codex host
judgments. A local macOS service checks synchronization every five minutes, and
a completed judgment import also triggers sync. No cloud model API is required.
The computer must be online for new collection and judgment work; the Site
remains readable from the last complete snapshot when it is offline.

The private Python synchronizer sends its application bearer and, when needed,
the platform service-access header only to this Site. Service access does not
impersonate the owner. Public scholarly-source access and local database access
are independent from Site access. No SQLite/WAL, PDF, private paths, collector
credentials, queues, results, logs or backups enter this source repository.

Sync pulls owner feedback first, committing events and cursor together. It then
reads a consistent local snapshot, reuses records with matching checksums,
uploads changed records and verifies the full manifest before atomically
activating it. Interrupted uploads leave the previous complete snapshot active;
the next invocation resumes. The server retains the previous complete generation.
Verify a changed updater by running the local `academic-radar sync` command and
reading the authenticated `/api/view?view=library` or today's data path to compare
its generation with `cloud-sync-status.json`. Never create test feedback in the
production database. Full setup and recovery instructions are in the parent
repository's `docs/cloud-sync.md`.

Edit `worker/`, not generated `dist/`. Publish with the Sites hosting workflow,
using the exact source commit and archive. Keep `sites/` synchronized with the
GitHub project. Data and feedback updates use the sync API and do not require a
source deployment.

For loopback-only public UI preview, pass a private application-record snapshot
file to `node scripts/preview.mjs /absolute/path/to/records.json`. Do not commit
that file. The preview does not simulate owner authentication or hosted OAuth.
