"""compact / history against a real, throwaway PostgreSQL.

Runs when either is set, skips otherwise:
  TEAMBUS_TEST_DSN  libpq DSN of a server where the user may CREATE DATABASE
                    (CI uses a postgres service container)
  TEAMBUS_PG_BIN    folder holding initdb and pg_ctl; a temp cluster is started
Never touches a configured bus: every test gets its own database.
"""
import datetime as dt
import gzip
import io
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import teambus  # noqa: E402
import migrate_archive  # noqa: E402

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None

# The pre-archive schema an existing install has (supersedes still a foreign
# key), so these tests also prove tools/migrate_archive.py upgrades it.
BASE_SQL = """
CREATE TABLE team_members (
  name text PRIMARY KEY, kind text NOT NULL, location text NOT NULL, machine text,
  card jsonb NOT NULL DEFAULT '{}', heartbeat_at timestamptz, ttl_s int NOT NULL DEFAULT 180,
  status text NOT NULL DEFAULT 'offline',
  inbound_policy text NOT NULL DEFAULT 'accept' CHECK (inbound_policy IN ('accept','hold','refuse')),
  key_thumbprint text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE team_messages (
  seq bigserial PRIMARY KEY, from_member text NOT NULL REFERENCES team_members(name),
  to_member text NOT NULL REFERENCES team_members(name), body text NOT NULL,
  kind text NOT NULL DEFAULT 'message',
  state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','claimed','done','held')),
  idempotency_key text NOT NULL UNIQUE, claimed_by text REFERENCES team_members(name),
  claim_lease_until timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
  claimed_at timestamptz, done_at timestamptz);
CREATE INDEX team_messages_inbox_idx ON team_messages (to_member, state, seq);
CREATE TABLE team_facts (
  id bigserial PRIMARY KEY, key text NOT NULL,
  kind text NOT NULL DEFAULT 'fact' CHECK (kind IN ('fact','directive')), body text NOT NULL,
  posted_by text NOT NULL REFERENCES team_members(name), idempotency_key text NOT NULL UNIQUE,
  supersedes bigint, superseded_by bigint, created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE team_facts ADD CONSTRAINT team_facts_supersedes_fkey FOREIGN KEY (supersedes) REFERENCES team_facts(id);
ALTER TABLE team_facts ADD CONSTRAINT team_facts_superseded_by_fkey FOREIGN KEY (superseded_by) REFERENCES team_facts(id);
CREATE INDEX team_facts_board_idx ON team_facts (key) WHERE superseded_by IS NULL;
"""

_server = {}


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def setUpModule():
    if psycopg2 is None:
        raise unittest.SkipTest("psycopg2 not installed")
    dsn = os.environ.get("TEAMBUS_TEST_DSN")
    if dsn:
        _server["admin"] = dsn
        return
    pg_bin = os.environ.get("TEAMBUS_PG_BIN")
    if not pg_bin:
        raise unittest.SkipTest("set TEAMBUS_TEST_DSN or TEAMBUS_PG_BIN to run the database tests")
    exe = lambda n: os.path.join(pg_bin, n + (".exe" if os.name == "nt" else ""))  # noqa: E731
    d = tempfile.mkdtemp(prefix="tbpg-")
    data, port = os.path.join(d, "data"), _free_port()
    subprocess.run([exe("initdb"), "-D", data, "-U", "postgres", "-A", "trust", "-E", "UTF8"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # The server inherits our handles; a pipe here would never close.
    subprocess.run([exe("pg_ctl"), "-D", data, "-o", "-p %d -c listen_addresses=127.0.0.1" % port,
                    "-l", os.path.join(d, "log.txt"), "-w", "start"], check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _server.update(admin="host=127.0.0.1 port=%d user=postgres dbname=postgres" % port,
                   stop=[exe("pg_ctl"), "-D", data, "-m", "fast", "-w", "stop"], dir=d)


def tearDownModule():
    if "stop" in _server:
        subprocess.run(_server["stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(_server["dir"], ignore_errors=True)


NOW = dt.datetime.now(dt.timezone.utc)
OLD = NOW - dt.timedelta(days=200)
MID = NOW - dt.timedelta(days=120)
NEW = NOW - dt.timedelta(days=5)
AGE = dt.timedelta(days=90)


def _with_db(dsn, name):
    parts = [p for p in dsn.split() if not p.startswith("dbname=")]
    return " ".join(parts + ["dbname=" + name])


class CompactTests(unittest.TestCase):
    schema = "legacy"  # pre-archive install, upgraded by migrate_archive

    def setUp(self):
        self.name = "t" + os.urandom(4).hex()
        self.admin = psycopg2.connect(_server["admin"])
        self.admin.autocommit = True
        self.admin.cursor().execute("CREATE DATABASE " + self.name)
        self.dsn = _with_db(_server["admin"], self.name)
        self.conn = psycopg2.connect(self.dsn)
        self.conn.autocommit = True
        cur = self.conn.cursor()
        if self.schema == "legacy":
            cur.execute(BASE_SQL)
        else:
            cur.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))
        for m in ("alice", "bob", "carol"):
            cur.execute("INSERT INTO team_members (name, kind, location) VALUES (%s,'agent','test')", (m,))
        msgs = [  # (from, to, state, created, done)
            ("alice", "bob", "done", OLD, OLD),        # moves
            ("bob", "alice", "done", MID, MID),        # moves
            ("alice", "carol", "done", NEW, NEW),      # too new: stays
            ("alice", "bob", "queued", OLD, None),     # old but unread: stays
            ("carol", "bob", "claimed", OLD, None),    # in flight: stays
            ("alice", "bob", "held", OLD, None),       # held: stays
            ("bob", "carol", "done", OLD, None),       # done without done_at, old: moves
        ]
        for i, (f, t, st, c, dn) in enumerate(msgs):
            cur.execute("INSERT INTO team_messages (from_member,to_member,body,state,idempotency_key,"
                        "created_at,done_at,claimed_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (f, t, "msg %d" % i, st, "k%d" % i, c, dn, t if st == "claimed" else None))

        def fact(key, body, at):
            cur.execute("SELECT id FROM team_facts WHERE key=%s AND superseded_by IS NULL", (key,))
            prev = cur.fetchone()
            cur.execute("INSERT INTO team_facts (key,body,posted_by,idempotency_key,supersedes,created_at) "
                        "VALUES (%s,%s,'alice',%s,%s,%s) RETURNING id",
                        (key, body, os.urandom(6).hex(), prev[0] if prev else None, at))
            nid = cur.fetchone()[0]
            if prev:
                cur.execute("UPDATE team_facts SET superseded_by=%s WHERE id=%s", (nid, prev[0]))
        fact("alpha", "a1", OLD); fact("alpha", "a2", MID); fact("alpha", "a3 current", NEW)
        fact("beta", "b1", OLD); fact("beta", "b2", OLD)
        fact("gamma", "g1", OLD); fact("gamma", teambus.RETIRED_PREFIX + "done", MID)
        fact("delta", "d1", NEW); fact("delta", teambus.RETIRED_PREFIX + "fresh", NEW)
        migrate_archive.apply(self.conn)
        self.tmp = tempfile.mkdtemp(prefix="tbcfg-")

    def tearDown(self):
        self.conn.close()
        self.admin.cursor().execute("DROP DATABASE " + self.name + " WITH (FORCE)")
        self.admin.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self, table, cols):
        cur = self.conn.cursor()
        cur.execute("SELECT %s FROM %s ORDER BY 1" % (cols, table))
        r = cur.fetchall()
        cur.close()
        return r

    def everything(self):
        m = self.rows("team_messages", teambus.MSG_COLS) + self.rows("team_messages_archive", teambus.MSG_COLS)
        f = self.rows("team_facts", teambus.FACT_COLS) + self.rows("team_facts_archive", teambus.FACT_COLS)
        return sorted(m), sorted(f)

    def hot(self):
        return self.rows("team_messages", "seq"), self.rows("team_facts", "id")

    def run_cli(self, member, *argv):
        path = os.path.join(self.tmp, member + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"member": member, "db": {"transport": "pg-dsn", "dsn": self.dsn}}, f)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = teambus.main(["--config", path] + list(argv))
        return rc, buf.getvalue()

    def test_no_row_is_lost_and_only_eligible_rows_move(self):
        before = self.everything()
        r = teambus.compact(self.conn, AGE)
        self.assertEqual((r["messages"], r["facts"]), (3, 4))
        self.assertEqual(self.everything(), before)
        self.assertEqual(sorted(x[0] for x in self.rows("team_messages", "state")),
                         ["claimed", "done", "held", "queued"])
        self.assertEqual(sorted(x[0] for x in self.rows("team_facts", "body")),
                         ["RETIRED: fresh", "a2", "a3 current", "b2", "d1"])

    def test_inbox_and_board_read_the_same_after_compaction(self):
        def views():
            return (self.run_cli("bob", "inbox")[1], self.run_cli("alice", "inbox")[1],
                    self.run_cli("alice", "facts")[1])
        before = views()
        teambus.compact(self.conn, AGE)
        after = views()
        self.assertEqual(after, before)
        self.assertIn("a3 current", after[2])
        self.assertNotIn("RETIRED", after[2])

    def test_second_run_is_a_no_op(self):
        teambus.compact(self.conn, AGE)
        snap = self.everything(), self.hot()
        r = teambus.compact(self.conn, AGE)
        self.assertEqual((r["messages"], r["facts"]), (0, 0))
        self.assertEqual((self.everything(), self.hot()), snap)

    def test_dry_run_counts_match_and_move_nothing(self):
        before = self.hot()
        d = teambus.compact(self.conn, AGE, dry_run=True)
        self.assertEqual(self.hot(), before)
        self.assertEqual(self.rows("team_messages_archive", "seq"), [])
        r = teambus.compact(self.conn, AGE)
        self.assertEqual((d["messages"], d["facts"]), (r["messages"], r["facts"]))

    def test_failure_moves_nothing(self):
        cur = self.conn.cursor()
        cur.execute("SELECT " + teambus.MSG_COLS + " FROM team_messages WHERE body='msg 0'")
        cur.execute("INSERT INTO team_messages_archive (" + teambus.MSG_COLS + ") VALUES (" +
                    ",".join(["%s"] * 12) + ")", cur.fetchone())  # PK clash mid-move
        before = self.hot()
        with self.assertRaises(psycopg2.IntegrityError):
            teambus.compact(self.conn, AGE)
        self.assertEqual(self.hot(), before)
        self.assertEqual(self.rows("team_facts_archive", "id"), [])

    def test_posting_after_compaction_still_supersedes_cleanly(self):
        teambus.compact(self.conn, AGE)
        rc, out = self.run_cli("alice", "fact", "beta", "b3")        # head b2 points at archived b1
        self.assertEqual(rc, 0)
        self.assertIn("supersedes", out)
        rc, out = self.run_cli("alice", "fact", "gamma", "g-again")  # retired chain fully archived
        self.assertEqual(rc, 0)
        self.assertNotIn("supersedes", out)
        board = self.run_cli("alice", "facts")[1]
        self.assertIn("b3", board)
        self.assertIn("g-again", board)

    def test_history_reads_across_hot_and_archive(self):
        teambus.compact(self.conn, AGE)
        out = self.run_cli("alice", "history", "alpha")[1]
        self.assertEqual(out.count("<fact "), 3)
        self.assertIn('in="archive"', out)
        self.assertIn('in="hot"', out)
        self.assertIn("RETIRED", self.run_cli("alice", "history", "gamma")[1])
        out = self.run_cli("alice", "history", "bob")[1]
        self.assertEqual(out.count("<message "), 6)
        self.assertIn('in="archive"', out)
        self.assertEqual(self.run_cli("alice", "history", "bob", "--limit", "2")[1].count("<message "), 2)

    def test_export_holds_every_moved_row(self):
        path = os.path.join(self.tmp, "snap.jsonl.gz")
        r = teambus.compact(self.conn, AGE, export_path=path)
        with gzip.open(path, "rt", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        self.assertEqual(lines[0]["table"], "_meta")
        self.assertEqual(sum(1 for x in lines if x["table"] == "team_messages"), r["messages"])
        self.assertEqual(sum(1 for x in lines if x["table"] == "team_facts"), r["facts"])
        self.assertFalse(os.path.exists(path + ".partial"))

    def test_cli_compact_prints_counts(self):
        self.assertTrue(self.run_cli("alice", "compact", "--older-than", "90d", "--dry-run")[1]
                        .startswith("would archive 3 message(s) and 4 fact(s)"))
        self.assertTrue(self.run_cli("alice", "compact", "--older-than", "90d")[1]
                        .startswith("archived 3 message(s) and 4 fact(s)"))
        self.assertTrue(self.run_cli("alice", "compact", "--older-than", "90d")[1]
                        .startswith("archived 0 message(s) and 0 fact(s)"))

    def test_missing_archive_tables_refuse(self):
        self.conn.cursor().execute("DROP TABLE team_messages_archive")
        with self.assertRaisesRegex(SystemExit, "migrate_archive"):
            teambus.compact(self.conn, AGE)


class ShippedSchemaTests(CompactTests):
    """The same guarantees on a fresh install built from schema.sql."""
    schema = "shipped"


class ParseAgeTests(unittest.TestCase):
    def test_parse_age(self):
        self.assertEqual(teambus.parse_age("90d"), dt.timedelta(days=90))
        self.assertEqual(teambus.parse_age("12h"), dt.timedelta(hours=12))
        for bad in ("90", "d", "-3d", "2w"):
            with self.assertRaises(SystemExit):
                teambus.parse_age(bad)


if __name__ == "__main__":
    unittest.main()
