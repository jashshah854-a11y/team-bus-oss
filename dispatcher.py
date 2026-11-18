#!/usr/bin/env python3
"""Team Bus dispatcher: the wake layer.

Store-and-forward alone means a member only learns about new mail when it
next checks. The dispatcher closes that gap: it watches the bus store on a
short adaptive poll and taps exactly the members an event concerns:

  message  -> the addressed member only
  fact     -> every member except the one who posted it

How a member is tapped is per member, set in dispatcher.json:

  every member  one JSON line appended to spool/<member>.jsonl. Any agent
                runtime can tail that file; this is the universal adapter.
  "command"     optional: a program run once per cycle with that member's
                due events as JSON lines on stdin. Exit 0 = delivered.
                Non-zero exit, a timeout or a crash = the wake is retried
                later with backoff (never fatal to the dispatcher).
  "notice_file" optional: true keeps spool/<member>-notice.json with an
                unread counter, for runtimes that poll a single small file.

Delivery is durable (team_wake_deliveries). Each cycle:
  1. discover: every queued, held or lease-expired message, and every fact
     newer than the start floor and inside fact_lookback_h, gets one row per
     recipient. Discovery does not rely on sequence order, so a row that
     commits late (a lower seq becoming visible after a higher one) is still
     found. A message that is re-queued after a failed attempt, or released
     from hold, is a new variant and wakes again.
  2. deliver: due rows are spooled (once) and handed to the member's command.
     Success marks the row delivered; failure schedules a retry with
     exponential backoff, and after max_wake_attempts marks it failed, where
     teambus_deadman.py reports it.
Each event carries a stable "event_id"; a consumer that sees it twice (a
retry after a partial failure) should ignore the repeat.

The store stays the record: a tap is only a hint. The member's own
`teambus.py inbox` / `claim` reads the full message.

Poll cadence is adaptive: poll_interval_s while cycles find work, then an
idle backoff ladder capped at max_idle_s, so a scale-to-zero database can
sleep. The heartbeat is time-based (heartbeat_interval_s), independent of
the idle backoff, and raises the watched member's presence TTL so a healthy
idle dispatcher never reads as offline. A local send touches the poke file
(state/poke) and cuts the sleep short. Remote senders cannot, so the worst
remote wake latency is max_idle_s plus the adapter's own time (PROTOCOL.md).

Config: dispatcher.json next to this file, or $TEAMBUS_DISPATCHER_CONFIG.
  teambus_config        member config the dispatcher connects with; it needs
                        role 'service' (or 'owner') to read every member's mail
  heartbeat_member      member whose heartbeat the dispatcher keeps fresh
                        (default: the config's member)
  poll_interval_s       base poll in seconds (default 2)
  max_idle_s            longest idle sleep (default 600)
  heartbeat_interval_s  seconds between heartbeats (default 300)
  fact_lookback_h       how far back fact discovery looks (default 24)
  max_wake_attempts     failed wakes before a delivery is marked failed (default 12)
  spool_dir, poke_path, state_path, log_path
  members               {"<name>": {"command": [...], "notice_file": true}}
  dry_adapters          true: log what would be dispatched; write nothing
                        (no spool, no command, no delivery rows, no state)
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import teambus  # noqa: E402

DEFAULTS = {
    "poll_interval_s": 2,
    "max_idle_s": 600,
    "heartbeat_interval_s": 300,
    "heartbeat_member": None,
    "fact_lookback_h": 24,
    "max_wake_attempts": 12,
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
# cycle after the base poll_interval_s; capped at max_idle_s.
BACKOFF_LADDER_S = (10, 30, 60, 120, 300, 600)

# Retry delay for a failed wake: 10 s doubling, capped at one hour.
RETRY_BASE_S, RETRY_CAP_S = 10, 3600

MEMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

DISCOVER_MESSAGES = """
INSERT INTO team_wake_deliveries (event_kind, ref, member, variant)
SELECT 'message', m.seq, m.to_member,
       CASE WHEN m.state = 'claimed' THEN 'expired' ELSE m.state END || ':' || m.attempts || ':' || m.releases
FROM team_messages m
WHERE m.state IN ('queued', 'held') OR (m.state = 'claimed' AND m.claim_lease_until < now())
ON CONFLICT DO NOTHING
"""

DISCOVER_FACTS = """
INSERT INTO team_wake_deliveries (event_kind, ref, member, variant)
SELECT 'fact', f.id, r.name, ''
FROM team_facts f JOIN team_members r ON r.name <> f.posted_by
WHERE f.id > %(floor)s AND f.created_at > now() - %(lookback)s::float8 * interval '1 hour'
ON CONFLICT DO NOTHING
"""

# Dry run: what discovery would add, without writing.
PREVIEW = """
SELECT 'message', m.seq, m.to_member,
       CASE WHEN m.state = 'claimed' THEN 'expired' ELSE m.state END || ':' || m.attempts || ':' || m.releases
FROM team_messages m
WHERE (m.state IN ('queued', 'held') OR (m.state = 'claimed' AND m.claim_lease_until < now()))
  AND NOT EXISTS (SELECT 1 FROM team_wake_deliveries d WHERE d.event_kind = 'message' AND d.ref = m.seq
                  AND d.member = m.to_member
                  AND d.variant = CASE WHEN m.state = 'claimed' THEN 'expired' ELSE m.state END || ':' || m.attempts || ':' || m.releases)
UNION ALL
SELECT 'fact', f.id, r.name, ''
FROM team_facts f JOIN team_members r ON r.name <> f.posted_by
WHERE f.id > %(floor)s AND f.created_at > now() - %(lookback)s::float8 * interval '1 hour'
  AND NOT EXISTS (SELECT 1 FROM team_wake_deliveries d WHERE d.event_kind = 'fact' AND d.ref = f.id
                  AND d.member = r.name)
"""

DUE = """
SELECT d.event_kind, d.ref, d.member, d.variant, d.attempts, d.spooled,
       m.from_member, m.to_member, m.kind, m.state, m.body, m.created_at, m.claim_lease_until,
       f.posted_by, f.key, f.kind, f.body, f.created_at
FROM team_wake_deliveries d
LEFT JOIN team_messages m ON d.event_kind = 'message' AND m.seq = d.ref
LEFT JOIN team_facts f ON d.event_kind = 'fact' AND f.id = d.ref
WHERE d.status = 'pending' AND d.next_attempt_at <= now()
ORDER BY d.created_at, d.ref
LIMIT 500
"""


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


def retry_delay(attempts):
    return min(RETRY_CAP_S, RETRY_BASE_S * (2 ** max(0, attempts - 1)))


def still_wanted(variant, state, lease_until, now):
    """Is a message wake still worth sending? Its state may have moved on."""
    kind = variant.split(":", 1)[0]
    if kind == "expired":
        return state == "claimed" and lease_until is not None and lease_until < now
    return state == kind


class Dispatcher:
    def __init__(self, cfg, clock=time.monotonic):
        self.cfg = cfg
        self.clock = clock
        self.tcfg = teambus.load_config(cfg["teambus_config"])
        self.heartbeat_member = cfg.get("heartbeat_member") or self.tcfg["member"]
        self.conn = None
        self.state = {"fact_floor": None}
        if os.path.exists(cfg["state_path"]):
            with open(cfg["state_path"], encoding="utf-8") as f:
                self.state.update(json.load(f))
        # v0.1 state files carried a high-water cursor; it becomes the fact floor.
        if self.state.get("fact_floor") is None and self.state.get("last_fact_id") is not None:
            self.state["fact_floor"] = self.state["last_fact_id"]
        self.spool_root = os.path.realpath(cfg["spool_dir"])
        if not cfg["dry_adapters"]:
            os.makedirs(self.spool_root, exist_ok=True)
        self.poll_count = 0
        self.last_error = None
        self.last_heartbeat = None
        base = cfg["poll_interval_s"]
        cap = max(base, cfg.get("max_idle_s") or base)
        self.ladder = [base] + [s for s in BACKOFF_LADDER_S if base < s < cap] + ([cap] if cap > base else [])
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

    def seconds_to_heartbeat(self):
        if self.cfg["dry_adapters"]:
            return self.cfg["heartbeat_interval_s"]      # dry runs never beat
        if self.last_heartbeat is None:
            return 0
        return max(0, self.cfg["heartbeat_interval_s"] - (self.clock() - self.last_heartbeat))

    # ------------------------------------------------------------ logging
    def log(self, record):
        record = dict(record)
        record.setdefault("ts", now_iso())
        line = json.dumps(record, default=str)
        with open(self.cfg["log_path"], "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def save_state(self):
        if self.cfg["dry_adapters"]:
            return
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
                import psycopg2
                if isinstance(e, psycopg2.Error) and not isinstance(
                        e, (psycopg2.OperationalError, psycopg2.InterfaceError)):
                    raise                                  # a real SQL error: do not retry blindly
                self.conn = None
                if attempt == 1:
                    raise
                self.log({"event": "db-retry", "error": str(e)[:160]})
                time.sleep(1)

    # ------------------------------------------------------------ adapters
    def member_cfg(self, member):
        return self.cfg.get("members", {}).get(member, {})

    def spool_path(self, member, suffix=".jsonl"):
        """Spool file for a member, refusing names that could escape spool_dir."""
        if not MEMBER_RE.match(member or ""):
            raise ValueError("unsafe member name for a spool file: %r" % member)
        path = os.path.realpath(os.path.join(self.spool_root, member + suffix))
        if os.path.dirname(path) != self.spool_root:
            raise ValueError("spool path escapes spool_dir: %r" % member)
        return path

    def spool(self, member, events):
        with open(self.spool_path(member), "a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, default=str) + "\n")
        if self.member_cfg(member).get("notice_file"):
            path = self.spool_path(member, "-notice.json")
            notice = {"unread_events": 0}
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    notice = json.load(f)
            notice["unread_events"] = notice.get("unread_events", 0) + len(events)
            notice["last_event_at"] = events[-1]["dispatched_at"]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(notice, f)
            os.replace(tmp, path)

    def run_command(self, member, events):
        """One command run per member per cycle. Returns (ok, detail)."""
        cmd = self.member_cfg(member).get("command")
        if not cmd or not events:
            return True, None
        payload = "".join(json.dumps(e, default=str) + "\n" for e in events)
        try:
            out = subprocess.run(cmd, input=payload, capture_output=True, text=True,
                                 timeout=self.cfg["command_timeout_s"])
            detail = (out.stderr or out.stdout or "")[:160]
            self.log({"event": "command", "member": member, "count": len(events),
                      "rc": out.returncode, "detail": detail})
            return out.returncode == 0, ("rc=%s %s" % (out.returncode, detail)).strip()
        except Exception as e:
            self.log({"event": "command", "member": member, "count": len(events), "rc": None,
                      "detail": str(e)[:160]})
            return False, str(e)[:500]

    # ------------------------------------------------------------ cycle
    def heartbeat(self):
        min_ttl = int(self.cfg["heartbeat_interval_s"] * 2 + 60)
        self.query("SELECT teambus_heartbeat(%s, %s, %s)",
                   (self.tcfg.get("member"), self.heartbeat_member, min_ttl))
        self.last_heartbeat = self.clock()

    def _event(self, row, detected):
        (kind, ref, member, variant, attempts, spooled,
         m_from, m_to, m_kind, m_state, m_body, m_created, m_lease,
         f_by, f_key, f_kind, f_body, f_created) = row
        base = {"event_id": "%s:%s:%s" % (kind, ref, variant) if variant else "%s:%s" % (kind, ref),
                "event": kind, "ref": ref, "attempt": attempts + 1, "detected_at": detected}
        if kind == "message":
            base.update({"from": m_from, "to": m_to, "kind": m_kind, "state": m_state,
                         "variant": variant, "preview": (m_body or "")[:200],
                         "posted_at": m_created.isoformat() if m_created else None})
        else:
            base.update({"from": f_by, "key": f_key, "kind": f_kind, "preview": (f_body or "")[:200],
                         "posted_at": f_created.isoformat() if f_created else None})
        return base

    def mark(self, rows, status, error=None):
        for kind, ref, member, variant, attempts in rows:
            if status == "delivered" or status == "skipped":
                self.query("UPDATE team_wake_deliveries SET status=%s, attempts=attempts+1, "
                           "delivered_at=now(), last_error=NULL WHERE event_kind=%s AND ref=%s "
                           "AND member=%s AND variant=%s",
                           (status, kind, ref, member, variant), fetch=False)
            elif status == "spooled":
                self.query("UPDATE team_wake_deliveries SET spooled=true WHERE event_kind=%s "
                           "AND ref=%s AND member=%s AND variant=%s",
                           (kind, ref, member, variant), fetch=False)
            else:
                n = attempts + 1
                final = n >= self.cfg["max_wake_attempts"]
                self.query("UPDATE team_wake_deliveries SET status=%s, attempts=%s, last_error=%s, "
                           "next_attempt_at=now() + %s::float8 * interval '1 second' WHERE event_kind=%s "
                           "AND ref=%s AND member=%s AND variant=%s",
                           ("failed" if final else "pending", n, (error or "")[:500],
                            retry_delay(n), kind, ref, member, variant), fetch=False)
                self.log({"event": "wake-failed" if final else "wake-retry", "member": member,
                          "ref_kind": kind, "ref": ref, "attempt": n,
                          "retry_in_s": None if final else retry_delay(n), "error": (error or "")[:160]})

    def cycle(self):
        self.poll_count += 1
        dry = self.cfg["dry_adapters"]
        if not dry and self.seconds_to_heartbeat() <= 0:
            try:
                self.heartbeat()
            except Exception as e:
                # A refused heartbeat (e.g. heartbeat_member set but this member
                # is not 'service') must never stop wakes from going out.
                self.last_heartbeat = self.clock()
                self.log({"event": "heartbeat-failed", "member": self.heartbeat_member,
                          "error": str(e).strip().splitlines()[0][:200] if str(e) else "?"})
                if self.conn is not None and getattr(self.conn, "closed", 0):
                    self.conn = None
        if self.state.get("fact_floor") is None:
            # First run: older facts are not replayed.
            rows = self.query("SELECT coalesce(max(id), 0) FROM team_facts "
                              "WHERE created_at < now() - interval '2 minutes'")
            self.state["fact_floor"] = rows[0][0]
            self.save_state()
        params = {"floor": self.state["fact_floor"], "lookback": self.cfg["fact_lookback_h"]}
        detected = now_iso()

        if dry:
            rows = self.query(PREVIEW, params)
            for kind, ref, member, variant in rows:
                self.log({"event": "dispatch", "member": member, "ref_kind": kind, "ref": ref,
                          "variant": variant, "dry": True})
                if self.member_cfg(member).get("command"):
                    self.log({"event": "command-skipped", "member": member, "ref": ref})
            return bool(rows)

        self.query(DISCOVER_MESSAGES, fetch=False)
        self.query(DISCOVER_FACTS, params, fetch=False)
        due = self.query(DUE)
        db_now = self.query("SELECT now()")[0][0]
        batches = {}
        for row in due:
            kind, ref, member, variant, attempts, spooled = row[:6]
            key = (kind, ref, member, variant, attempts)
            if kind == "message" and not still_wanted(variant, row[9], row[12], db_now):
                self.mark([key], "skipped")
                continue
            if kind == "fact" and row[13] is None:     # fact archived before its wake
                self.mark([key], "skipped")
                continue
            ev = self._event(row, detected)
            ev["dispatched_at"] = now_iso()
            batches.setdefault(member, []).append((key, ev, spooled))

        for member, items in batches.items():
            keys = [k for k, _, _ in items]
            try:
                fresh = [(k, ev) for k, ev, sp in items if not sp]
                if fresh:
                    self.spool(member, [ev for _, ev in fresh])
                    self.mark([k for k, _ in fresh], "spooled")
            except Exception as e:
                self.mark(keys, "failed", "spool: %s" % e)
                continue
            ok, detail = self.run_command(member, [ev for _, ev, _ in items])
            if ok:
                self.mark(keys, "delivered")
            else:
                self.mark(keys, "failed", detail)
            for k, ev, _ in items:
                self.log({"event": "dispatch", "member": member, "ref_kind": k[0], "ref": k[1],
                          "event_id": ev["event_id"], "from": ev.get("from"), "ok": ok,
                          "posted_at": ev.get("posted_at"), "detected_at": detected,
                          "dispatched_at": ev["dispatched_at"]})
        return bool(due)

    def run(self):
        self.log({"event": "dispatcher-start", "config": self.cfg["_path"],
                  "poll_interval_s": self.cfg["poll_interval_s"], "backoff_ladder_s": self.ladder,
                  "heartbeat_interval_s": self.cfg["heartbeat_interval_s"],
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
            # Never sleep past the next heartbeat.
            nap = min(self.interval, max(1, self.seconds_to_heartbeat()))
            if self.sleep_sliced(nap):
                self.log({"event": "poke", "from_s": self.interval, "to_s": self.ladder[0]})
                self.backoff_idx = 0
                self.interval = self.ladder[0]


if __name__ == "__main__":
    Dispatcher(load_settings()).run()
