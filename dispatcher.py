#!/usr/bin/env python3
"""Team Bus dispatcher: the wake layer.

Store-and-forward alone means a member only learns about new mail when it
next checks. The dispatcher closes that gap: it watches the bus store on a
short adaptive poll and, the moment a message or a facts-board post lands,
taps exactly the members it concerns:

  message  -> the addressed member only
  fact     -> every member except the one who posted it

(Many managed Postgres poolers do not pass LISTEN/NOTIFY through, so a
tight poll is the portable mechanism.)

How a member is tapped is per member, set in dispatcher.json:

  every member  one JSON line appended to spool/<member>.jsonl. Any agent
                runtime can tail that file; this is the universal adapter.
  "command"     optional: a program run once per cycle with that member's
                new events as JSON lines on stdin, e.g. a script that
                notifies a desktop app, posts to a local agent's API, or
                starts a CLI agent session. Non-zero exit is logged, never
                fatal.
  "notice_file" optional: true keeps spool/<member>-notice.json with an
                unread counter, for runtimes that poll a single small file.

The store stays the record: a tap is only a hint, so a missed tap loses
nothing; the member's own `teambus.py inbox` reads the full message.
Every dispatch is logged with posted_at, detected_at and dispatched_at to
dispatcher-log.jsonl, so wake latency is measured, not assumed.

Poll cadence is adaptive: poll_interval_s while cycles find work, then
an idle backoff ladder (BACKOFF_LADDER_S) so a scale-to-zero database can
sleep. A send through teambus.py on the same machine touches the poke file
(state/poke), which cuts the sleep short and resets the ladder, so local
sends never wait for the backoff.

Config: dispatcher.json next to this file, or $TEAMBUS_DISPATCHER_CONFIG.
  teambus_config   member config the dispatcher connects with (default teambus.config.json)
  heartbeat_member member whose heartbeat the dispatcher keeps fresh, so
                   `who` shows the wake layer is alive (default: the config's member)
  poll_interval_s  base poll in seconds (default 2)
  heartbeat_polls  heartbeat every N cycles (default 15)
  spool_dir, poke_path, state_path, log_path
  members          {"<name>": {"command": [...], "notice_file": true}}
  dry_adapters     true: log what would be dispatched, write nothing
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import teambus  # noqa: E402

DEFAULTS = {
    "poll_interval_s": 2,
    "heartbeat_polls": 15,
    "heartbeat_member": None,
    "teambus_config": os.path.join(HERE, "teambus.config.json"),
    "spool_dir": os.path.join(HERE, "spool"),
    "poke_path": os.path.join(HERE, "state", "poke"),
    "state_path": os.path.join(HERE, "dispatcher-state.json"),
    "log_path": os.path.join(HERE, "dispatcher-log.jsonl"),
    "members": {},
    "command_timeout_s": 60,
    "dry_adapters": False,
}

# Idle backoff ladder in seconds, climbed one rung per consecutive empty
# cycle after the base poll_interval_s; capped at the last rung.
BACKOFF_LADDER_S = (10, 30, 60, 120, 300, 600)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_settings():
    path = os.environ.get("TEAMBUS_DISPATCHER_CONFIG", os.path.join(HERE, "dispatcher.json"))
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    cfg["_path"] = path
    return cfg


class Dispatcher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tcfg = teambus.load_config(cfg["teambus_config"])
        self.heartbeat_member = cfg.get("heartbeat_member") or self.tcfg["member"]
        self.conn = None
        self.state = {"last_msg_seq": None, "last_fact_id": None}
        if os.path.exists(cfg["state_path"]):
            with open(cfg["state_path"], encoding="utf-8") as f:
                self.state.update(json.load(f))
        os.makedirs(cfg["spool_dir"], exist_ok=True)
        self.poll_count = 0
        self.last_error = None
        base = cfg["poll_interval_s"]
        self.ladder = [base] + [s for s in BACKOFF_LADDER_S if s > base]
        self.backoff_idx = 0
        self.interval = base
        poke_dir = os.path.dirname(cfg["poke_path"])
        if poke_dir:
            os.makedirs(poke_dir, exist_ok=True)
        # Only touches after startup count, so a stale poke file never fires.
        self.last_poke_mtime = self._poke_mtime()

    def _poke_mtime(self):
        try:
            return os.path.getmtime(self.cfg["poke_path"])
        except OSError:
            return 0.0

    def poked(self):
        """True once per fresh poke-file touch since the last check."""
        mtime = self._poke_mtime()
        if mtime > self.last_poke_mtime:
            self.last_poke_mtime = mtime
            return True
        return False

    def sleep_sliced(self, seconds):
        """Sleep up to `seconds` in <=1s slices; True early on a fresh poke."""
        deadline = time.time() + seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            time.sleep(min(1.0, remaining))
            if self.poked():
                return True

    # ------------------------------------------------------------ logging
    def log(self, record):
        record = dict(record)
        record.setdefault("ts", now_iso())
        line = json.dumps(record)
        with open(self.cfg["log_path"], "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def save_state(self):
        tmp = self.cfg["state_path"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.cfg["state_path"])

    # ------------------------------------------------------------ store
    def query(self, sql, params=(), fetch=True):
        for attempt in range(2):
            try:
                if self.conn is None:
                    self.conn = teambus.connect(self.tcfg)
                cur = self.conn.cursor()
                cur.execute(sql, params)
                rows = cur.fetchall() if fetch else []
                cur.close()
                return rows
            except Exception as e:
                self.conn = None
                if attempt == 1:
                    raise
                self.log({"event": "db-retry", "error": str(e)[:160]})
                time.sleep(1)

    # ------------------------------------------------------------ adapters
    def member_cfg(self, member):
        return self.cfg.get("members", {}).get(member, {})

    def dispatch(self, member, event):
        """Spool one event for one member (and bump its notice file); log latency."""
        t0 = time.time()
        event = dict(event)
        event["dispatched_at"] = now_iso()
        if not self.cfg["dry_adapters"]:
            with open(os.path.join(self.cfg["spool_dir"], member + ".jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
            if self.member_cfg(member).get("notice_file"):
                path = os.path.join(self.cfg["spool_dir"], member + "-notice.json")
                notice = {"unread_events": 0}
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        notice = json.load(f)
                notice["unread_events"] = notice.get("unread_events", 0) + 1
                notice["last_event_at"] = event["dispatched_at"]
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(notice, f)
        self.log({"event": "dispatch", "member": member, "ref_kind": event["event"],
                  "ref": event.get("ref"), "from": event["from"],
                  "posted_at": event.get("posted_at"), "detected_at": event.get("detected_at"),
                  "dispatch_ms": round((time.time() - t0) * 1000, 1),
                  "dry": self.cfg["dry_adapters"]})
        return event

    def run_command(self, member, events):
        """One command run per member per cycle, however many events it carries."""
        cmd = self.member_cfg(member).get("command")
        if not cmd or not events:
            return
        if self.cfg["dry_adapters"]:
            self.log({"event": "command-skipped", "member": member, "count": len(events)})
            return
        payload = "".join(json.dumps(e) + "\n" for e in events)
        try:
            out = subprocess.run(cmd, input=payload, capture_output=True, text=True,
                                 timeout=self.cfg["command_timeout_s"])
            self.log({"event": "command", "member": member, "count": len(events), "rc": out.returncode,
                      "detail": (out.stderr or out.stdout or "")[:160]})
        except Exception as e:
            self.log({"event": "command", "member": member, "count": len(events), "rc": None,
                      "detail": str(e)[:160]})

    # ------------------------------------------------------------ cycle
    def cycle(self):
        self.poll_count += 1
        cur_time = now_iso()
        if self.poll_count % self.cfg["heartbeat_polls"] == 1:
            self.query("UPDATE team_members SET heartbeat_at = now(), status = 'online', "
                       "updated_at = now() WHERE name = %s", (self.heartbeat_member,), fetch=False)
        roster = [r[0] for r in self.query("SELECT name FROM team_members ORDER BY name")]
        # First run: facts start from the board as of two minutes ago (older
        # history is not replayed); messages start at 0, but only queued or
        # held rows dispatch, so already-handled mail is never re-tapped.
        if self.state["last_fact_id"] is None:
            rows = self.query("SELECT coalesce(max(id), 0) FROM team_facts "
                              "WHERE created_at < now() - interval '2 minutes'")
            self.state["last_fact_id"] = rows[0][0]
        if self.state["last_msg_seq"] is None:
            self.state["last_msg_seq"] = 0
        batches = {}
        msg_rows = self.query(
            "SELECT seq, from_member, to_member, kind, state, body, created_at FROM team_messages "
            "WHERE seq > %s AND state IN ('queued', 'held') ORDER BY seq LIMIT 100",
            (self.state["last_msg_seq"],))
        for seq, frm, to, kind, state, body, created in msg_rows:
            event = {"event": "message", "ref": seq, "from": frm, "to": to, "kind": kind,
                     "state": state, "preview": body[:200],
                     "posted_at": created.isoformat() if created else None, "detected_at": cur_time}
            batches.setdefault(to, []).append(self.dispatch(to, event))
            self.state["last_msg_seq"] = max(self.state["last_msg_seq"], seq)
        fact_rows = self.query(
            "SELECT id, key, kind, body, posted_by, created_at FROM team_facts "
            "WHERE id > %s ORDER BY id LIMIT 100", (self.state["last_fact_id"],))
        for fid, key, kind, body, by, created in fact_rows:
            event = {"event": "fact", "ref": fid, "from": by, "key": key, "kind": kind,
                     "preview": body[:200], "posted_at": created.isoformat() if created else None,
                     "detected_at": cur_time}
            for member in roster:
                if member != by:
                    batches.setdefault(member, []).append(self.dispatch(member, event))
            self.state["last_fact_id"] = max(self.state["last_fact_id"], fid)
        for member, events in batches.items():
            self.run_command(member, events)
        self.save_state()
        return bool(msg_rows) or bool(fact_rows)

    def run(self):
        self.log({"event": "dispatcher-start", "config": self.cfg["_path"],
                  "poll_interval_s": self.cfg["poll_interval_s"], "backoff_ladder_s": self.ladder,
                  "poke_path": self.cfg["poke_path"], "dry_adapters": self.cfg["dry_adapters"]})
        while True:
            try:
                activity = self.cycle()
                if self.last_error is not None:
                    self.log({"event": "dispatcher-recovered"})
                    self.last_error = None
                old = self.interval
                if activity:
                    self.backoff_idx = 0
                elif self.backoff_idx < len(self.ladder) - 1:
                    self.backoff_idx += 1
                self.interval = self.ladder[self.backoff_idx]
                if self.interval != old:
                    self.log({"event": "poll-interval", "from_s": old, "to_s": self.interval,
                              "reason": "activity" if activity else "idle"})
            except Exception as e:
                msg = str(e).strip().splitlines()[0][:200] if str(e) else "?"
                if msg != self.last_error:
                    self.log({"event": "dispatcher-error", "error": msg})
                    self.last_error = msg
                self.conn = None
                time.sleep(5)
            if self.sleep_sliced(self.interval):
                self.log({"event": "poke", "from_s": self.interval, "to_s": self.ladder[0]})
                self.backoff_idx = 0
                self.interval = self.ladder[0]


if __name__ == "__main__":
    Dispatcher(load_settings()).run()
