# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
