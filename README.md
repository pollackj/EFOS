# EFOS — Executive Function Optimization System

A standalone native Windows desktop application for dependency-driven executive
planning. EFOS combines a direct PostgreSQL data layer, a flat visual dependency
canvas, deterministic conjunctive Task/Project status evaluation, optional
temporal Task availability, a read-only Google Calendar OAuth synchronization
cache, and a two-pass DeepSeek RAG assistant.

Built with PyQt6 / Qt Graphics View. No browser, Streamlit, VS Code, or terminal
is required at runtime.

## What is included

- **Native desktop shell** — frameless PyQt6 window with a thin theme-aware outer
  outline, dark first-run default, complete light theme, and locally persisted
  appearance.
- **Suggested Focus** — a four-tier recommendation cascade over live database
  state: Ready Task, critical milestone, stalled Project, revisit candidate.
  Clicking it opens the shared metadata inspector without changing any data.
- **Flat dependency canvas** — only Tasks, Projects, and external Conditions.
  Condition → entity is a Precondition; entity → entity is a Prerequisite.
- **Manage + Flow dashboard** — metadata editing, recurrence, schedules,
  temporal availability, dependency attachment, deferral, shelving, and the
  canvas in one workspace.
- **Sortable Registry** — All/Tasks/Projects/Conditions filters, typed
  column sorting, `+N more`-style autocomplete search, and completed/deferred
  history.
- **Assistant launchpad** — permanent right-hand panel with four default
  queries, custom label/prompt presets, context envelope, two-pass diagnostics,
  and up to 256 FIFO conversation threads.
- **Four-view calendar** — Day, Week, Month, and Schedule layouts over local
  tasks plus the incremental Google Calendar cache.
- **Explicit dependency engine** — conjunctive three-valued preconditions,
  two-valued internal prerequisites, automatic derivation, and database-level
  bypass prevention. Temporal availability is a separate model and never
  changes Task Status or Project resolution.
- **Append-only audit ledger** — `public.events` rejects UPDATE and DELETE and
  survives entity deletion.
- **Background scheduler** — 60-second QTimer pulse runs idempotent recurrence
  resets; the database rejects unearned Ready transitions.

## Quick start

### 1. Configure `.env`

```dotenv
EFOS_DB_PASSWORD=...
DEEPSEEK_API_KEY=...
EFOS_GOOGLE_CLIENT_ID=...
EFOS_GOOGLE_CLIENT_SECRET=...
EFOS_GOOGLE_CALENDAR_ID=primary
```

Optional connection overrides are `EFOS_DB_HOST`, `EFOS_DB_PORT`,
`EFOS_DB_USER`, and `EFOS_DB_NAME`. A `.env` is discovered from
`EFOS_ENV_FILE`, the executable folder and its parents, the working directory,
and `%APPDATA%\EFOS`.

### 2. First launch

```powershell
python -m pip install -r requirements.txt
python main.py
```

If the target database or schema does not exist, the guided setup wizard asks
for:

- **Mode A** — one-time administrative credentials to create the database,
  execute the canonical schema, and grant routine privileges.
- **Mode B** — a separate low-privilege application role used for daily
  runtime queries. The app password is written to the Windows Credential
  Manager, not to a flat file.

Startup aborts with a detailed discrepancy report if the database exists but
its EFOS schema is partial, corrupted, or mismatched. No destructive recovery is
attempted.

An existing EFOS 2.0.0 database is upgraded automatically to schema 2.1.0 the
first time the 2.1.0 application starts. The migration is additive: it creates
`public.task_availability_windows`, its index and foreign key, and updates
`schema_meta`. Existing Tasks, Projects, Conditions, dependencies, recurrence
rules, notes, events, and calendar cache rows are preserved. If the runtime role
cannot execute DDL, EFOS tries the configured administrative credentials; if
neither role may migrate, startup stops with the exact missing-object report.
Starting from an empty database still executes the canonical schema verbatim.
To apply the migration without opening the UI, run `python main.py --self-test`;
it starts the service, performs the additive migration, validates the schema,
and writes `efos_selftest.json`.

### 3. Build the Windows executable

```powershell
python -m pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

Output: `release/EFOS.exe`, a portable one-file windowed executable, plus
`EFOS.lnk`. Add `-DesktopShortcut` to place a shortcut on the Desktop. The build
first packages into `build/release-staging`, runs the staged binary's
`--self-test` against the configured database, and only then replaces
`release/EFOS.exe`; the previous verified executable is preserved at
`.tmp/EFOS.previous.exe`.

Verify a packaged build without opening a window:

```powershell
.\release\EFOS.exe --self-test
Get-Content .\efos_selftest.json
```

## Architecture

```
main.py
└─ efos_app/
   ├─ config.py           .env/runtime profile, data directory, UI state
   ├─ credentials.py      Windows Credential Manager wrapper
   ├─ db.py               bootstrap, additive schema migration, verification, query helpers
   ├─ repository.py       transactional CRUD, append-only events, cache
   ├─ models.py           domain records, enums, graph snapshots
   ├─ logic.py            dependency evaluation, cycles, recommendations
   ├─ service.py          mutations and derived reconciliation
   ├─ scheduler.py        60-second recurrence pulse
   ├─ calendar_service.py OAuth 2.0, incremental sync, local cache
   ├─ rag_service.py      two-pass DeepSeek pipeline + priority analyzer
   ├─ workers.py          QThread workers for network/probe calls
   ├─ sql/schema.sql      canonical schema executed on first launch
   └─ ui/
      ├─ theme.py         dark/light palettes, QSS, native chrome
      ├─ graph.py         flat dependency canvas
      ├─ flow_page.py     canvas workspace
      ├─ inspector.py     shared metadata inspector and gesture guide
      ├─ dashboard.py     Overview + Manage/Flow dashboard
      ├─ registry_page.py sortable registry + autocomplete
      ├─ chat_page.py     assistant stream + permanent launchpad
      ├─ calendar_page.py Day/Week/Month/Schedule views
      └─ main_window.py   shell, sidebar, Suggested Focus, status bar
```

### Canonical schema

`efos_app/sql/schema.sql` is the authoritative initialization script for schema
version 2.1.0. Core tables:

- `tasks` — Status: `ready`, `blocked`, `completed`, `deferred`
- `projects` — Status: `active`, `stalled`, `shelved`, `resolved`, plus
  `resolution_conditions_unknown`
- `conditions` — nullable three-valued external facts
- `task_preconditions`, `project_preconditions`
- `task_prerequisites`, `project_prerequisites`
- `task_recurrence_rules`, `task_availability_windows`, `notes` + one-to-one
  junction tables
- `events` — append-only audit ledger without foreign keys
- `assistant_threads`, `assistant_messages`
- `custom_queries`
- `calendar_accounts`, `calendar_events` (OAuth metadata + local snapshot)

Native `CHECK` constraints (`valid_task_transition`, `valid_project_transition`)
and triggers enforce legal state changes, reject unearned Ready or resolution
transitions, maintain `previous_state`, synchronize the project requirements
flag, and block UPDATE/DELETE on `events`. The availability table has its own
range constraints and `ON DELETE CASCADE` foreign key; it is not referenced by
any dependency trigger.

### Authoritative dependency model

- **Precondition** — external 3VL fact. Only `TRUE` clears the barrier; `FALSE`
  and `UNKNOWN/NULL` block.
- **Prerequisite** — internal 2VL state. A source Task counts only when
  `completed`; a source Project only when `resolved`.
- Tasks are `ready` iff every attached precondition is TRUE and every
  prerequisite source satisfies its state. Projects resolve the same way.
- Zero dependencies means the execution criteria are satisfied by default.
- Status and resolution are derived. There is no manual override.
- New projects begin with `resolution_conditions_unknown = TRUE`; automated
  resolution is blocked until a dependency is attached and the flag drops.
- Removing a dependency deletes only the mapping row, leaves the source record
  untouched, re-evaluates every downstream target, and appends an audit event
  in the same transaction.
- Temporal availability is not a dependency. A Task can be `ready` while its
  availability window is closed, and a `blocked` Task can be inside its
  availability window.

### Temporal availability

Availability describes when a Task can actually be performed in the real world
(for example, a telephone call to a business during its operating hours). It is
separate from Task Status, scheduling, and recurrence:

- Every new Task defaults to **24/7 availability**. No configuration is
  required to create or complete a Task.
- Custom availability is optional and is edited through **Edit availability…**
  in the shared Task inspector. The dialog follows the recurrence editor's
  weekday and window conventions.
- The dialog supports selected weekdays, multiple windows within a day, and
  local wall-clock times such as Mon–Fri 09:00–17:00, selected weekdays
  08:00–12:00, or every day 07:00–22:00.
- A window whose end is at or before its start crosses midnight. It is
  normalized on save into two non-crossing rows (for example Fri 22:00–24:00
  and Sat 00:00–02:00) so evaluation and SQL constraints stay simple.
- `public.task_availability_windows` stores one row per weekday/window:
  `weekday` 0 = Monday … 6 = Sunday, `start_minute` inclusive and `end_minute`
  exclusive, in minutes after local midnight. **No rows means 24/7.**
- Availability never changes Task Status, never affects dependency evaluation,
  never prevents completion, and never prevents Project resolution. It also
  does not reuse `scheduled_start`, `scheduled_end`, or any recurrence record.

The UI presents these as separate concepts: **Status** (formal Task state),
**Availability** (when it can be performed), **Schedule** (a dated block on the
calendar), and **Recurrence** (a repeat rule). Selecting 24/7 in the dialog
clears custom windows and restores the default.

### Notes

`notes` is a central table mapped one-to-one through `task_notes`,
`project_notes`, and `condition_notes`. Note edits save on focus blur. An empty
edit never overwrites or deletes historical content; only the trash icon
removes the note. Context changes synchronously commit a pending note before
navigation. Entity deletion removes the entity row, mapping row, and note in one
transaction.

### Assistant pipeline

Exactly two DeepSeek calls per ordinary message:

1. **Intent routing** → `QUERY` or `CHAT`.
2. **SQL compiler or grounded text**. SQL runs inside a `READ ONLY` transaction
   with a statement timeout, 200-row cap, and a strict local guard.

The top-3 priorities preset is intercepted before any model call and answered by
a deterministic, read-only analyzer that only selects authoritatively Ready
Tasks.

Conversation threads are stored in PostgreSQL. The repository keeps at most 256
threads with a rolling FIFO deletion at the boundary. Custom queries store a
short display label separately from the full backend prompt and support create,
edit, delete, reorder, and favorite pins.

### Google Calendar

- OAuth 2.0 authorization code flow with PKCE and a loopback redirect.
- Scope: `calendar.readonly`. Access and refresh tokens live only in the Windows
  Credential Manager.
- One-way Google → EFOS synchronization, incremental via `syncToken`; a full
  import is used only on first sync or when Google expires the token.
- The local snapshot is preserved when synchronization fails and the header
  displays `Sync Unavailable — Data Stale as of [timestamp]`.
- Incoming datetimes are converted to the local Windows system timezone before
  storage because schedules use `timestamp without time zone`.
- Collision detection runs entirely offline against the persisted cache. Local
  scheduling conflicts present a warning with an explicit **Force Save** button.
  The legacy API-key audit panel is removed.

### Application scheduler

A 60-second QTimer in the application layer queries recurring tasks and computes
the latest window occurrence. `last_reset_window` makes resets idempotent. The
database trigger rejects a reset whose preconditions or prerequisites are
unresolved; the repository logs `recurrence.blocked` and keeps the task
completed.

## Using EFOS

- **Overview** — execution posture, Ready Tasks, schedule, Projects, and
  dependency health.
- **Dashboard → Manage** — create, rename, inspect, schedule, configure
  availability, attach dependencies, defer/reopen, shelve/unshelve, toggle
  Conditions, and write notes.
- **Dashboard → Flow** — drag from a node's right-hand port. Wheel zooms;
  middle-drag pans; double-click an edge or press Delete to remove a dependency.
  The inspector includes the current canvas gesture guide.
- **Registry** — filter by object type, type to see autocomplete suggestions,
  click column headers to sort, and reopen completed/deferred tasks. The Status
  column always shows the Task's formal state; availability, schedule, and
  recurrence appear in the row tooltip. Double-click or use the context menu to
  open the shared inspector.
- **Assistant** — use a default or custom launchpad query. The right panel shows
  the context envelope and the two-pass routing trace.
- **Calendar** — switch among Day, Week, Month, and Schedule. Double-click a
  local task to open its metadata inspector; external events are read-only.
  Calendar blocks are scheduling data, not temporal availability.

Keyboard shortcuts: `Ctrl+1…5` pages, `Ctrl+N` new task, `Ctrl+R` recompute,
`Ctrl+K` assistant. `Enter` sends; `Shift+Enter` inserts a newline.

## Tests

```powershell
$env:QT_QPA_PLATFORM="offscreen"
$env:QT_QPA_FONTDIR="C:\Windows\Fonts"
python -m pytest -q -p no:cacheprovider
```

The suite creates and drops disposable PostgreSQL databases (`efos_test_*`,
`efos_bad_*`, and `efos_migrate_*`) and covers:

- first-launch schema bootstrap, partial-schema abort, and the additive
  2.0.0 → 2.1.0 migration with data preservation
- all supported Task and Project states
- conjunctive preconditions, FALSE/NULL handling, prerequisite completion gates,
  and project resolution
- database rejection of forced Ready/resolution transitions
- unknown-requirement blocking
- dependency removal cascade, transactional events, append-only enforcement
- temporal availability: 24/7 default, custom weekday windows, multiple daily
  windows, cross-midnight normalization, persistence/edit/clear, and proof that
  availability never changes Status, completion, or Project resolution
- note blur semantics and atomic cascade deletion
- idempotent recurrence resets and dependency-blocked resets
- 256-thread FIFO (bounded in tests), custom query metadata separation
- calendar cache collisions, stale-state warning, timezone mapping
- typed registry sorting, autocomplete selection, Suggested Focus navigation,
  notes context interception, assistant priority routing, four calendar views,
  the availability dialog and Status/Availability separation, and theme
  persistence

## Security

- PostgreSQL passwords, DeepSeek keys, and Google OAuth tokens are held in memory;
  OAuth tokens and the optional app-role password are stored in the Windows
  Credential Manager.
- The packaged build does not embed `.env`.
- Model-generated SQL is validated and executed read-only with a row cap.
- `public.events` is append-only at the database layer.
- Google synchronization is read-only and one-way.

## Repository layout

```
main.py                 entry point
efos_app/               application package
assets/                 Windows version metadata
scripts/                demo seed + build icon helper
tests/                  logic, integration, and headless UI tests
build.ps1               test + staged verification + PyInstaller packaging
release/                generated EFOS.exe and README.txt
requirements.txt
```
