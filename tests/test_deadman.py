"""Dead-man switch alert bookkeeping, without a database or network.

An alert that fails to send must be retried on the next run; an alert that
went out must not repeat; a cleared problem sends one 'recovered'.
"""
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import teambus_deadman as dm  # noqa: E402

NOW = dt.datetime(2026, 11, 17, 12, 0, tzinfo=dt.timezone.utc)


class DeadmanTests(unittest.TestCase):
    def setUp(self):
        d = pathlib.Path(tempfile.mkdtemp(prefix="tbdm-"))
        self.cfg = d / "cfg.json"
        self.cfg.write_text(json.dumps({"member": "watch", "db": {"transport": "pg-dsn", "dsn": "x"}}))
        self.patches = [mock.patch.object(dm, "STATE", d / "state"),
                        mock.patch.object(dm, "LOG", d / "logs" / "deadman.log")]
        for p in self.patches:
            p.start()
        self.sent = []
        self.fail_next = False

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def fake_post(self, url, body, title=None, priority=None):
        if self.fail_next:
            raise OSError("network down")
        self.sent.append(title.split(" (")[0])
        return 200

    def run_once(self, beats):
        with mock.patch.object(dm, "gather", return_value=(beats, [], NOW, [], (0, 0))), \
             mock.patch.object(dm, "post", side_effect=self.fake_post), \
             mock.patch("builtins.print"):
            return dm.main(["--config", str(self.cfg)])

    def test_failed_alert_is_retried_then_not_repeated_then_recovers(self):
        stale = [("relay", NOW - dt.timedelta(minutes=45))]
        self.fail_next = True
        self.assertEqual(self.run_once(stale), 1)
        self.assertEqual(self.sent, [])
        self.fail_next = False
        self.run_once(stale)
        self.assertEqual(self.sent, ["Team Bus STALLED"])
        self.run_once(stale)
        self.assertEqual(self.sent, ["Team Bus STALLED"])          # no repeat
        self.assertEqual(self.run_once([("relay", NOW)]), 0)
        self.assertEqual(self.sent, ["Team Bus STALLED", "Team Bus recovered"])
        self.run_once([("relay", NOW)])
        self.assertEqual(len(self.sent), 2)

    def test_failed_mail_and_failed_wakes_are_problems(self):
        out = dm.problems([], [], NOW, failed=[(7, "bob", "boom")], wakes=(2, 1))
        self.assertEqual(sorted(out), ["failed:7", "wakes:failed", "wakes:late"])

    def test_healthy_idle_dispatcher_heartbeat_is_not_stale(self):
        # Default dispatcher heartbeat is 300 s; the alarm is 20 min.
        self.assertEqual(dm.problems([("relay", NOW - dt.timedelta(seconds=310))], [], NOW), {})


if __name__ == "__main__":
    unittest.main()
