#!/usr/bin/env python3
"""Archive tables for `teambus.py compact` (2026-11-10).

Prints the migration by default. `--apply` runs it against the configured bus.
Run --apply once per database, as the owner; review the printed SQL first.

What it changes:
  1. team_messages_archive / team_facts_archive: same columns as the hot
     tables plus archived_at. Primary key and two lookup indexes only.
  2. Drops team_facts_supersedes_fkey. A current fact keeps pointing at the
     version it replaced (supersedes); once that version moves to the archive
     the in-table foreign key can no longer hold. The link is kept as a plain
     id, and `teambus.py history <key>` follows it across both tables.
     team_facts_superseded_by_fkey stays: compaction never moves a row that a
     remaining row's superseded_by points at.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ARCHIVE_SQL = """
CREATE TABLE IF NOT EXISTS team_messages_archive (
  seq               bigint PRIMARY KEY,
  from_member       text NOT NULL,
  to_member         text NOT NULL,
  body              text NOT NULL,
  kind              text NOT NULL,
  state             text NOT NULL,
  idempotency_key   text NOT NULL,
  claimed_by        text,
  claim_lease_until timestamptz,
  created_at        timestamptz NOT NULL,
  claimed_at        timestamptz,
  done_at           timestamptz,
  archived_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS team_messages_archive_from_idx ON team_messages_archive (from_member, seq);
CREATE INDEX IF NOT EXISTS team_messages_archive_to_idx   ON team_messages_archive (to_member, seq);

CREATE TABLE IF NOT EXISTS team_facts_archive (
  id              bigint PRIMARY KEY,
  key             text NOT NULL,
  kind            text NOT NULL,
  body            text NOT NULL,
  posted_by       text NOT NULL,
  idempotency_key text NOT NULL,
  supersedes      bigint,
  superseded_by   bigint,
  created_at      timestamptz NOT NULL,
  archived_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS team_facts_archive_key_idx ON team_facts_archive (key, id);

ALTER TABLE team_facts DROP CONSTRAINT IF EXISTS team_facts_supersedes_fkey;
"""


def apply(conn):
    cur = conn.cursor()
    cur.execute(ARCHIVE_SQL)
    cur.close()


def main(argv):
    if "--apply" not in argv:
        print(ARCHIVE_SQL.strip())
        print("\n-- printed only. Re-run with --apply to change the configured database.")
        return 0
    import teambus
    cfg = teambus.load_config(None)
    conn = teambus.connect(cfg, write=True)
    apply(conn)
    conn.close()
    print("archive tables ready; team_facts_supersedes_fkey dropped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
