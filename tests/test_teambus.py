import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import teambus


class Cursor:
    def __init__(self, names):
        self.names = names
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchall(self):
        return [(name,) for name in self.names]


class ProtocolTests(unittest.TestCase):
    def test_recipient_resolution_accepts_registered_member(self):
        self.assertEqual(teambus.resolve_recipient(Cursor(["alice", "bob"]), "bob"), "bob")

    def test_recipient_resolution_rejects_unknown_member_with_hint(self):
        with self.assertRaisesRegex(SystemExit, "unknown recipient 'bo'.*bob"):
            teambus.resolve_recipient(Cursor(["alice", "bob"]), "bo")

    def test_presence_requires_a_recent_heartbeat(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.assertFalse(teambus.online({"heartbeat_at": None, "ttl_s": 60}, now.timestamp()))
        self.assertTrue(teambus.online({"heartbeat_at": now - dt.timedelta(seconds=30), "ttl_s": 60}, now.timestamp()))
        self.assertFalse(teambus.online({"heartbeat_at": now - dt.timedelta(seconds=61), "ttl_s": 60}, now.timestamp()))
