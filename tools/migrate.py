#!/usr/bin/env python3
"""Bring any Team Bus database to the current schema (v0.2).

Prints the plan by default. `--apply` runs everything in ONE transaction
against the configured bus, as the database owner: a failure changes nothing.

  0. preflight                      refuses if schema public is writable by
                                    everyone or a bus table has a foreign owner
  1. archive tables (tools/migrate_archive.py)
  2. migrations/002_hardening.sql   claim tokens, one fact head per key,
                                    lifetime dedupe, tasks, wake deliveries,
                                    member roles; existing installs start in
                                    identity_mode 'trusted'
  3. api.sql                        the write functions and row security
  4. grants.sql                     only if the teambus_agent role exists

  python tools/migrate.py [--config owner.json]            # print only
  python tools/migrate.py [--config owner.json] --apply

Safe to re-run. Back up first (pg_dump, or a provider branch/snapshot).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import migrate_archive  # noqa: E402


def preflight():
    """The guard block shared by schema.sql and api.sql."""
    s = (ROOT / "schema.sql").read_text(encoding="utf-8")
    return s[s.index("-- preflight:begin"):s.index("-- preflight:end")]


def steps():
    return [
        ("preflight", preflight()),
        ("archive tables", migrate_archive.ARCHIVE_SQL),
        ("migrations/002_hardening.sql", (ROOT / "migrations" / "002_hardening.sql").read_text(encoding="utf-8")),
        ("api.sql", (ROOT / "api.sql").read_text(encoding="utf-8")),
    ]


def apply(conn, grants=None):
    """Run every step in one transaction on `conn`. grants=None: run grants.sql
    only when the teambus_agent role exists. Returns the server notices."""
    prev = conn.autocommit
    seen = len(getattr(conn, "notices", []))
    conn.autocommit = False
    cur = conn.cursor()
    try:
        # Fail fast instead of stalling every client behind a long transaction.
        cur.execute("SET LOCAL lock_timeout = '10s'")
        for _, sql in steps():
            cur.execute(sql)
        if grants is None:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'teambus_agent'")
            grants = cur.fetchone() is not None
        if grants:
            cur.execute((ROOT / "grants.sql").read_text(encoding="utf-8"))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.autocommit = prev
    return list(getattr(conn, "notices", []))[seen:]


def warnings(conn):
    """Settings that would weaken the guarantees. Returns a list of text lines."""
    cur = conn.cursor()
    out = []
    cur.execute("SELECT m.name, r.rolname FROM team_members m JOIN pg_roles r ON r.rolname = m.db_role "
                "WHERE r.rolsuper OR r.rolbypassrls")
    for name, role in cur.fetchall():
        out.append("WARNING: member %s logs in as %s, which bypasses row security; its mail "
                   "privacy and limits are not enforced. Use a plain LOGIN role." % (name, role))
    cur.execute("SELECT name FROM team_members WHERE heartbeat_at IS NOT NULL AND kind = 'service' "
                "AND role = 'member'")
    for (name,) in cur.fetchall():
        out.append("NOTE: %s looks like a dispatcher or watchdog; give it role 'service' so it can "
                   "read all mail and beat for its watched member." % name)
    cur.close()
    return out


def main(argv):
    cfg_path = argv[argv.index("--config") + 1] if "--config" in argv else None
    if "--apply" not in argv:
        for name, sql in steps():
            print("-- ===== %s =====" % name)
            print(sql.strip())
        print("-- ===== grants.sql (only if role teambus_agent exists) =====")
        print("\n-- printed only. Re-run with --apply to change the configured database.")
        return 0
    import teambus
    cfg = teambus.load_config(cfg_path)
    conn = teambus.connect(cfg, write=True)
    try:
        for n in apply(conn):
            print(n.strip())
        for w in warnings(conn):
            print(w)
    finally:
        conn.close()
    print("migrated to v0.2. identity_mode is unchanged on existing installs ('trusted');")
    print("map members to login roles (team_members.db_role), then:")
    print("  UPDATE team_settings SET identity_mode = 'enforced';")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
