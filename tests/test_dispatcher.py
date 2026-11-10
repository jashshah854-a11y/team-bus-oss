"""Dispatcher routing without a database: a message wakes only its addressee,
a fact wakes everyone but its poster, and per-member adapters fire once per cycle."""
import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import dispatcher  # noqa: E402

NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)


class FakeDispatcher(dispatcher.Dispatcher):
    """Answers the dispatcher's five queries from in-memory rows."""

    def __init__(self, cfg, roster, messages, facts):
        self.roster, self.messages, self.facts = roster, messages, facts
        self.updates = []
        super().__init__(cfg)

    def query(self, sql, params=(), fetch=True):
        if sql.startswith("UPDATE team_members"):
            self.updates.append(params)
            return []
        if "FROM team_members" in sql:
            return [(m,) for m in self.roster]
        if "coalesce(max(id)" in sql:
            return [(0,)]
        if "FROM team_messages" in sql:
            return [m for m in self.messages if m[0] > params[0] and m[4] in ("queued", "held")]
        if "FROM team_facts" in sql:
            return [f for f in self.facts if f[0] > params[0]]
        raise AssertionError("unexpected query: " + sql)


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tbdisp-")
        conf = os.path.join(self.dir, "teambus.config.json")
        with open(conf, "w") as f:
            json.dump({"member": "relay", "db": {"transport": "pg-dsn", "dsn": "unused"}}, f)
        self.cfg = dict(dispatcher.DEFAULTS, teambus_config=conf, _path="test",
                        spool_dir=os.path.join(self.dir, "spool"),
                        poke_path=os.path.join(self.dir, "state", "poke"),
                        state_path=os.path.join(self.dir, "state.json"),
                        log_path=os.path.join(self.dir, "log.jsonl"))
        self.roster = ["alice", "bob", "carol", "relay"]
        self.messages = [
            (1, "alice", "bob", "message", "queued", "hi bob", NOW),
            (2, "carol", "alice", "message", "done", "already handled", NOW),
            (3, "alice", "bob", "message", "held", "held for bob", NOW),
        ]
        self.facts = [(10, "deploy-window", "fact", "Fridays", "carol", NOW)]

    def spool(self, member):
        path = os.path.join(self.cfg["spool_dir"], member + ".jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f]

    def make(self, **over):
        cfg = dict(self.cfg, **over)
        with mock.patch("builtins.print"):
            return FakeDispatcher(cfg, self.roster, self.messages, self.facts)

    def cycle(self, d):
        with mock.patch("builtins.print"):
            return d.cycle()

    def test_message_wakes_only_the_addressee(self):
        d = self.make()
        self.assertTrue(self.cycle(d))
        bob = [e for e in self.spool("bob") if e["event"] == "message"]
        self.assertEqual([e["ref"] for e in bob], [1, 3])          # queued and held, not done
        for other in ("alice", "carol", "relay"):
            self.assertEqual([e for e in self.spool(other) if e["event"] == "message"], [])

    def test_fact_wakes_everyone_but_the_poster(self):
        d = self.make()
        self.cycle(d)
        for member in ("alice", "bob", "relay"):
            self.assertEqual([e["ref"] for e in self.spool(member) if e["event"] == "fact"], [10])
        self.assertEqual([e for e in self.spool("carol") if e["event"] == "fact"], [])

    def test_nothing_is_tapped_twice(self):
        d = self.make()
        self.cycle(d)
        before = {m: len(self.spool(m)) for m in self.roster}
        self.assertFalse(self.cycle(d))
        self.assertEqual({m: len(self.spool(m)) for m in self.roster}, before)
        d2 = self.make()                                            # restart resumes from saved state
        self.assertFalse(self.cycle(d2))

    def test_command_runs_once_per_member_per_cycle_with_its_events(self):
        d = self.make(members={"bob": {"command": ["wake-bob"]}})
        with mock.patch.object(dispatcher.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.cycle(d)
        self.assertEqual(run.call_count, 1)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["wake-bob"])
        sent = [json.loads(line) for line in kwargs["input"].splitlines()]
        self.assertEqual([(e["event"], e["ref"]) for e in sent], [("message", 1), ("message", 3), ("fact", 10)])

    def test_failing_command_is_logged_not_fatal(self):
        d = self.make(members={"bob": {"command": ["wake-bob"]}})
        with mock.patch.object(dispatcher.subprocess, "run", side_effect=OSError("no such program")):
            self.assertTrue(self.cycle(d))
        self.assertEqual(len(self.spool("bob")), 3)

    def test_notice_file_counts_unread(self):
        d = self.make(members={"alice": {"notice_file": True}})
        self.cycle(d)
        with open(os.path.join(self.cfg["spool_dir"], "alice-notice.json")) as f:
            self.assertEqual(json.load(f)["unread_events"], 1)      # the fact only

    def test_dry_mode_writes_no_spool(self):
        d = self.make(dry_adapters=True, members={"bob": {"command": ["wake-bob"]}})
        with mock.patch.object(dispatcher.subprocess, "run") as run:
            self.cycle(d)
        run.assert_not_called()
        self.assertEqual(self.spool("bob"), [])

    def test_heartbeat_keeps_the_dispatcher_member_fresh(self):
        d = self.make()
        self.cycle(d)
        self.assertEqual(d.updates, [("relay",)])
        d2 = self.make(heartbeat_member="watcher")
        self.cycle(d2)
        self.assertEqual(d2.updates, [("watcher",)])

    def test_fresh_poke_cuts_sleep_short(self):
        d = self.make()
        pathlib.Path(self.cfg["poke_path"]).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(self.cfg["poke_path"]).touch()
        os.utime(self.cfg["poke_path"], (d.last_poke_mtime + 5, d.last_poke_mtime + 5))
        self.assertTrue(d.sleep_sliced(3))


if __name__ == "__main__":
    unittest.main()
