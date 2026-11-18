# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Security
- Identity is enforced by the database. Each member connects with its own login role, mapped in `team_members.db_role`; every write goes through `teambus_*` functions in the new `api.sql`, and `grants.sql` no longer gives the runtime role INSERT or UPDATE. A member can no longer impersonate another, rewrite message or fact history, bypass a recipient's hold or refuse policy, or change another member's policy.
- Only members with `role = 'owner'` can post directives. `role = 'service'` lets the dispatcher and dead-man switch read all mail.
- Row-level security limits mail and archived mail to sender and recipient (plus owners and services). `who` still shows unread counts.
- Default CLI output escapes message and fact text, so text cannot close or forge the envelope. New `--json` output keeps metadata and content in separate fields.
- An explicit `--config` or `$TEAMBUS_CONFIG` pointing at a missing file is an error instead of a silent fallback to another identity.
- The dispatcher refuses member names that would put a spool file outside `spool_dir`.
- Every `SECURITY DEFINER` function runs with `search_path = pg_catalog, pg_temp` and schema-qualified names, so overloads planted in a writable `public` schema (the PG13/14 default) cannot run as the owner. `tools/migrate.py` warns when `public` is writable by everyone or a mapped login bypasses row security.
- `schema.sql`, `api.sql` and `tools/migrate.py` refuse to run when schema `public` is writable by every role, or when a bus table already exists under another owner, so no login can plant tables, triggers or overloads that the owner's code runs. Client queries cast numeric parameters explicitly.
- Only an owner may replace or retire a current directive.
- Removed the `release-bootstrap` workflow, which force-pushed `main` from an archive that no longer exists.

### Fixed
- One current fact per key, enforced by a unique index; concurrent first posts to a new key are serialized with an advisory lock.
- Idempotency keys are remembered for life in `team_idempotency`, so an archived message or fact cannot be replayed as new work. Reusing a key for a different payload is refused.
- Claims return a token. `done`, the new `renew` and the new `fail` require it, so a worker whose lease expired cannot finish a newer attempt. `done` without a token only finishes an unclaimed queued message. `inbox` lists expired claims.
- New `release` moves held or failed messages back to the queue. Messages that fail `max_claim_attempts` times park as `failed`.
- An expired lease counts as a failed attempt, so a message that keeps crashing its worker parks as `failed`. Releasing a held or failed message wakes the recipient again. Held mail counts toward the inbox cap. Idempotency payload hashes use an unambiguous encoding.
- The dispatcher keeps delivering wakes when its heartbeat is refused. The dead-man switch refuses to run as a member that cannot read all mail. Migrations use a 10 s lock timeout.
- `team_tasks` now exists in the schema; `task` and `tasks` work on a fresh install.
- The dispatcher records wakes in `team_wake_deliveries` instead of a high-water cursor: rows that commit out of sequence order are found, failed wakes are retried with backoff and parked after `max_wake_attempts`, and wakes for finished mail are skipped. Dry runs write nothing.
- The dispatcher heartbeat is time-based (`heartbeat_interval_s`, default 300) instead of every N polls, and raises the presence TTL, so a healthy idle dispatcher no longer reads as stale. `max_idle_s` caps the idle backoff.
- The dead-man switch retries an alert that failed to send, and reports failed messages and failed or late wakes.
- `compact --export`: if the export cannot be moved into place after the archive commits, the full export is kept at `<file>.partial` and the error says so.

### Added
- `tools/migrate.py` and `migrations/002_hardening.sql`: upgrades a v0.1 install in one transaction. Existing duplicate fact heads are reconciled to the newest. Existing installs start in `identity_mode = 'trusted'` until members are mapped to logins.
- `whoami`, `renew`, `fail`, `release` commands. The adapter contract in `PROTOCOL.md`.
- Tests against real PostgreSQL with restricted member logins: identity, privacy, directives, hold, concurrent fact posts, replay after compaction, claim fencing, tasks, migration, dispatcher retry and out-of-order commits, dead-man alert retry.

### Earlier unreleased changes

### Added
- `dispatcher.py`: the wake layer the README describes. It polls the bus and taps only the addressed member for a message, and every member except the poster for a fact. Every member gets a spool file; per-member `command` and `notice_file` adapters are set in `dispatcher.json`. Adaptive idle backoff, instant wake on a local send (poke file), and per-dispatch latency logging. Ships with `install-dispatcher.sh` and a systemd unit template.
- `teambus_deadman.py`: an out-of-band stall alert (ntfy, optional healthchecks.io) when heartbeats go stale or mail sits undone.
- `teambus.py compact --older-than 90d [--dry-run] [--export FILE]`: moves done mail and superseded or retired facts into `team_messages_archive` and `team_facts_archive` in one transaction. Nothing is deleted, and inboxes and the board read the same afterwards.
- `teambus.py history <member|key>`: the full trace across hot and archive tables.
- `tools/migrate_archive.py` for existing installs. CI now runs the database tests against a PostgreSQL service.

### Changed
- `team_facts.supersedes` is a plain id, not a foreign key, so an archived predecessor can still be referenced. `grants.sql` gives `teambus_agent` read-only access to the archive tables.
- README quickstart, architecture diagram, and contributor docs (CONTRIBUTING, SECURITY, CODE_OF_CONDUCT).
- Issue templates for bug reports and feature requests.
- `pyproject.toml` so the CLI can be built as a Python package.

## [0.1.0-pre] - 2026-11-07

### Added
- First public release of the scrubbed code: member CLI (`teambus.py`), schema and grants, dispatcher wake layer, desktop-agent listener, dead-man switch, example lane, and the member protocol.
