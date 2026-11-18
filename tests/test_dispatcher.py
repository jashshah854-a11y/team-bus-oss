"""Dispatcher against real PostgreSQL, connecting as a restricted 'service' login.

Routing (a message wakes only its addressee, a fact everyone but its poster),
durable retry of failed wakes, discovery of rows that commit out of sequence
order, dry runs that write nothing, time-based heartbeats, and spool paths
that cannot escape spool_dir.
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import dispatcher  # noqa: E402
import pgtools  # noqa: E402
import teambus  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class PokeTests(unittest.TestCase):
    """No database needed."""

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

    def test_fresh_poke_cuts_sleep_short(self):
        d = dispatcher.Dispatcher(self.cfg)
        pathlib.Path(self.cfg["poke_path"]).touch()
        os.utime(self.cfg["poke_path"], (d.last_poke_mtime + 5, d.last_poke_mtime + 5))
        self.assertTrue(d.sleep_sliced(3))

    def test_member_names_cannot_escape_the_spool(self):
        d = dispatcher.Dispatcher(self.cfg)
        for bad in ("../outside", "..", "a/b", "a\\b", "", ".hidden", "x" * 80):
            with self.assertRaises(ValueError, msg=bad):
                d.spool_path(bad)
        self.assertTrue(d.spool_path("alice").endswith("alice.jsonl"))

    def test_ladder_is_capped_by_max_idle(self):
        d = dispatcher.Dispatcher(dict(self.cfg, max_idle_s=120))
        self.assertEqual(d.ladder, [2, 10, 30, 60, 120])

    def test_retry_delay_backs_off_and_caps(self):
        self.assertEqual([dispatcher.retry_delay(n) for n in (1, 2, 3)], [10, 20, 40])
        self.assertEqual(dispatcher.retry_delay(30), dispatcher.RETRY_CAP_S)


def tearDownModule():
    pgtools.stop()


class DispatcherDBTests(unittest.TestCase):
    members = {"alice": "member", "bob": "member", "carol": "member", "relay": "service"}

    @classmethod
    def setUpClass(cls):
        pgtools.start()

    def setUp(self):
        self.bus = pgtools.Bus(self.members)
        self.dir = tempfile.mkdtemp(prefix="tbdisp-")
        self.cfg = dict(dispatcher.DEFAULTS, teambus_config=self.bus.config("relay"), _path="test",
                        spool_dir=os.path.join(self.dir, "spool"),
                        poke_path=os.path.join(self.dir, "state", "poke"),
                        state_path=os.path.join(self.dir, "state.json"),
                        log_path=os.path.join(self.dir, "log.jsonl"))
        self.clock = Clock()

    def tearDown(self):
        self.bus.close()

    def send(self, frm, to, body):
        with mock.patch("builtins.print"):
            teambus.main(["--config", self.bus.config(frm), "send", to, body])

    def fact(self, frm, key, body):
        with mock.patch("builtins.print"):
            teambus.main(["--config", self.bus.config(frm), "fact", key, body])

    def make(self, **over):
        with mock.patch("builtins.print"):
            d = dispatcher.Dispatcher(dict(self.cfg, **over), clock=self.clock)
        # Facts posted by the tests are seconds old; start the floor at 0.
        d.state["fact_floor"] = 0
        return d

    def cycle(self, d):
        with mock.patch("builtins.print"):
            return d.cycle()

    def spool(self, member):
        path = os.path.join(self.cfg["spool_dir"], member + ".jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f]

    def deliveries(self):
        return self.bus.sql("SELECT event_kind, ref, member, status, attempts FROM team_wake_deliveries "
                            "ORDER BY event_kind, ref, member")

    def make_due(self):
        self.bus.sql("UPDATE team_wake_deliveries SET next_attempt_at = now() WHERE status = 'pending'")

    def test_message_wakes_only_the_addressee_and_fact_everyone_but_poster(self):
        self.send("alice", "bob", "hi bob")
        self.fact("carol", "deploy-window", "Fridays")
        d = self.make()
        self.assertTrue(self.cycle(d))
        self.assertEqual([e["event"] for e in self.spool("bob")], ["message", "fact"])
        self.assertEqual([e["event"] for e in self.spool("alice")], ["fact"])
        self.assertEqual([e["event"] for e in self.spool("relay")], ["fact"])
        self.assertEqual(self.spool("carol"), [])
        ev = self.spool("bob")[0]
        self.assertEqual((ev["from"], ev["preview"]), ("alice", "hi bob"))
        self.assertTrue(ev["event_id"].startswith("message:"))

    def test_nothing_is_tapped_twice_even_after_restart(self):
        self.send("alice", "bob", "once")
        d = self.make()
        self.cycle(d)
        self.assertFalse(self.cycle(d))
        self.assertFalse(self.cycle(self.make()))
        self.assertEqual(len(self.spool("bob")), 1)

    def test_failed_wake_is_retried_without_duplicating_the_spool(self):
        self.send("alice", "bob", "wake me")
        d = self.make(members={"bob": {"command": ["wake-bob"]}})
        with mock.patch.object(dispatcher.subprocess, "run", side_effect=OSError("adapter down")):
            self.cycle(d)
        self.assertEqual(self.deliveries(), [("message", 1, "bob", "pending", 1)])
        with mock.patch.object(dispatcher.subprocess, "run") as run:
            self.cycle(d)                                      # not due yet: backoff holds
            run.assert_not_called()
            self.make_due()
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.cycle(d)
            run.assert_called_once()
        self.assertEqual(self.deliveries(), [("message", 1, "bob", "delivered", 2)])
        self.assertEqual(len(self.spool("bob")), 1)

    def test_non_zero_exit_counts_as_failure_and_eventually_parks(self):
        self.send("alice", "bob", "x")
        d = self.make(members={"bob": {"command": ["wake-bob"]}}, max_wake_attempts=2)
        with mock.patch.object(dispatcher.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=3, stdout="", stderr="nope")
            self.cycle(d)
            self.make_due()
            self.cycle(d)
        self.assertEqual(self.deliveries(), [("message", 1, "bob", "failed", 2)])

    def test_a_late_committing_lower_seq_is_still_delivered(self):
        slow = self.bus.connect_as("alice")
        slow.autocommit = False
        cur = slow.cursor()
        cur.execute("SELECT * FROM teambus_send('alice', 'bob', 'slow commit', 'k-slow')")
        slow_seq = cur.fetchone()[0]
        self.send("carol", "bob", "fast commit")                   # higher seq, visible first
        d = self.make()
        self.cycle(d)
        self.assertEqual([e["preview"] for e in self.spool("bob")], ["fast commit"])
        slow.commit()
        slow.close()
        self.cycle(d)
        self.assertEqual([e["preview"] for e in self.spool("bob")], ["fast commit", "slow commit"])
        self.assertEqual(self.spool("bob")[1]["ref"], slow_seq)

    def test_dry_run_writes_nothing(self):
        self.send("alice", "bob", "x")
        d = self.make(dry_adapters=True, members={"bob": {"command": ["wake-bob"]}})
        with mock.patch.object(dispatcher.subprocess, "run") as run:
            self.assertTrue(self.cycle(d))
        run.assert_not_called()
        self.assertEqual(self.spool("bob"), [])
        self.assertEqual(self.deliveries(), [])
        self.assertFalse(os.path.exists(self.cfg["state_path"]))
        self.assertEqual(self.bus.sql("SELECT heartbeat_at FROM team_members WHERE name='relay'"), [(None,)])
        self.cycle(self.make())                                    # a real run still delivers it
        self.assertEqual(len(self.spool("bob")), 1)

    def test_released_hold_and_expired_claim_wake_again(self):
        conn = self.bus.connect_as("bob")
        conn.cursor().execute("SELECT teambus_set_policy('bob', 'bob', 'hold')")
        conn.close()
        self.send("alice", "bob", "held one")
        d = self.make()
        self.cycle(d)
        with mock.patch("builtins.print"):
            teambus.main(["--config", self.bus.config("bob"), "release", "1"])
        self.cycle(d)
        with mock.patch("builtins.print"):
            teambus.main(["--config", self.bus.config("bob"), "claim"])
        self.bus.sql("UPDATE team_messages SET claim_lease_until = now() - interval '1 minute'")
        self.cycle(d)
        self.assertEqual([e["variant"] for e in self.spool("bob")], ["held:0:0", "queued:0:1", "expired:1:1"])

    def test_wake_for_finished_mail_is_skipped(self):
        self.send("alice", "bob", "x")
        d = self.make(members={"bob": {"command": ["wake-bob"]}})
        with mock.patch.object(dispatcher.subprocess, "run", side_effect=OSError("down")):
            self.cycle(d)
        with mock.patch("builtins.print"):
            teambus.main(["--config", self.bus.config("bob"), "done", "1"])
        self.make_due()
        with mock.patch.object(dispatcher.subprocess, "run") as run:
            self.cycle(d)
            run.assert_not_called()
        self.assertEqual(self.deliveries()[0][3], "skipped")

    def test_heartbeat_is_time_based_and_raises_presence_ttl(self):
        d = self.make(heartbeat_interval_s=300)
        self.cycle(d)
        first = self.bus.sql("SELECT heartbeat_at, ttl_s FROM team_members WHERE name='relay'")[0]
        self.assertIsNotNone(first[0])
        self.assertEqual(first[1], 660)
        self.clock.t += 299
        for _ in range(20):                                        # many idle cycles: no beat
            self.cycle(d)
        self.assertEqual(self.bus.sql("SELECT heartbeat_at FROM team_members WHERE name='relay'")[0][0], first[0])
        self.clock.t += 2
        self.cycle(d)
        self.assertGreater(self.bus.sql("SELECT heartbeat_at FROM team_members WHERE name='relay'")[0][0], first[0])
        self.assertEqual(d.seconds_to_heartbeat(), 300)

    def test_service_may_beat_for_its_watched_member_but_a_member_may_not(self):
        d = self.make(heartbeat_member="carol")
        self.cycle(d)
        self.assertIsNotNone(self.bus.sql("SELECT heartbeat_at FROM team_members WHERE name='carol'")[0][0])
        with self.assertRaises(Exception):
            self.bus.connect_as("alice").cursor().execute("SELECT teambus_heartbeat('alice', 'carol', 0)")

    def test_releasing_a_failed_message_wakes_the_recipient_again(self):
        self.bus.sql("UPDATE team_settings SET max_claim_attempts = 1")
        self.send("alice", "bob", "retry me")
        d = self.make()
        self.cycle(d)
        bob = self.bus.config("bob")
        buf = __import__("io").StringIO()
        with mock.patch("sys.stdout", buf):
            teambus.main(["--config", bob, "--json", "claim"])
        token = json.loads(buf.getvalue().splitlines()[0])["token"]
        with mock.patch("builtins.print"):
            teambus.main(["--config", bob, "fail", "1", "--token", token, "boom"])
            teambus.main(["--config", bob, "release", "1"])
        self.cycle(d)
        self.assertEqual([e["variant"] for e in self.spool("bob")], ["queued:0:0", "queued:0:1"])

    def test_a_refused_heartbeat_does_not_stop_wakes(self):
        # A v0.1 dispatcher config with heartbeat_member, running as a plain member.
        self.bus.sql("UPDATE team_members SET role = 'member' WHERE name = 'relay'")
        self.bus.sql("UPDATE team_settings SET identity_mode = 'trusted'")   # it can still read mail
        self.send("alice", "bob", "still delivered")
        d = self.make(heartbeat_member="carol")
        self.cycle(d)
        self.assertEqual([e["preview"] for e in self.spool("bob")], ["still delivered"])
        with open(self.cfg["log_path"]) as f:
            self.assertIn("heartbeat-failed", f.read())

    def test_notice_file_counts_unread(self):
        self.fact("carol", "k", "v")
        d = self.make(members={"alice": {"notice_file": True}})
        self.cycle(d)
        with open(os.path.join(self.cfg["spool_dir"], "alice-notice.json")) as f:
            self.assertEqual(json.load(f)["unread_events"], 1)


if __name__ == "__main__":
    unittest.main()
