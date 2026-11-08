#!/usr/bin/env python3
"""Team Bus member CLI.

One addressed inbox for a team's agents, backed by Postgres tables
(`team_members`, `team_messages`, and `team_facts`).

  - Identity comes from the local config file, NEVER from a command argument
    (agent-bus rule): a member cannot send as someone else.
  - Presence is heartbeat + TTL, computed at read time (Matrix pattern).
  - Delivery is claim-with-lease + explicit done + idempotency keys
    (Agent DMs semantics). Reading never deletes.
  - Unknown recipient is a hard error with close-match suggestions.
  - Poll by monotonic sequence number; no `LISTEN/NOTIFY` dependency.

Usage:
  teambus.py [--config PATH] who
  teambus.py [--config PATH] send <member> <body...> [--key IDEMPOTENCY_KEY]
  teambus.py [--config PATH] inbox [--counts]
  teambus.py [--config PATH] claim
  teambus.py [--config PATH] done <seq>
  teambus.py [--config PATH] heartbeat
  teambus.py [--config PATH] fact [--directive] <key> <body...> [--key IDEM]
  teambus.py [--config PATH] facts [key]
  teambus.py [--config PATH] claim --seq N              (lease one exact message)
  teambus.py [--config PATH] retire <key> <reason...>   (hide a finished fact)
  teambus.py [--config PATH] task add <owner> <title...>  (shared to-do; owner is a member name)
  teambus.py [--config PATH] task done <id> [note...]
  teambus.py [--config PATH] tasks [owner | --all]        (open to-dos; --all includes done)

Config (JSON): {"member": "alice", "db": {...}} where db.transport is one of
  "pg-direct"  Direct psycopg2: host, port, dbname, user, password_file.
  "pg-dsn"     Full libpq DSN string in key "dsn" (tests).

Config resolution order: --config, $TEAMBUS_CONFIG, ./teambus.config.json,
~/.teambus.json. The DB password is never printed.
"""
import difflib
import json
import os
import sys
import time
import uuid

LEASE_MINUTES = 15          # claim lease (Agent DMs semantics)
INBOX_CAP = 50              # flood fuse (0 = off)
SEND_RATE_PER_HOUR = 30     # loop fuse per sender (0 = off)

PGPASS = os.path.expanduser("~/.pgpass")

# Poke file for the dispatcher's adaptive backoff (2026-10-28): after a
# send or fact post lands in the store, touching this local file tells
# the dispatcher to run a cycle now instead of waiting out its idle
# backoff (up to 600s). Local filesystem only, no DB cost.
POKE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "state", "poke")


def poke_dispatcher():
    """Best-effort wake for the local dispatcher; never fails a send.
    A missed poke only falls back to the dispatcher's next poll."""
    try:
        os.makedirs(os.path.dirname(POKE_PATH), exist_ok=True)
        with open(POKE_PATH, "a"):
            pass
        os.utime(POKE_PATH, None)
    except Exception:
        pass


# ---------------------------------------------------------------- config

def load_config(path):
    candidates = [path, os.environ.get("TEAMBUS_CONFIG"),
                  os.path.join(os.getcwd(), "teambus.config.json"),
                  os.path.expanduser("~/.teambus.json")]
    for cand in candidates:
        if cand and os.path.exists(cand):
            with open(cand) as f:
                cfg = json.load(f)
            cfg["_path"] = cand
            return cfg
    raise SystemExit("teambus: no config file found (looked for --config, "
                     "$TEAMBUS_CONFIG, ./teambus.config.json, ~/.teambus.json)")


def _pgpass_password(user):
    with open(PGPASS) as f:
        for line in f:
            p = line.rstrip("\n").split(":")
            if len(p) >= 5 and p[3] == user:
                return ":".join(p[4:])
    raise RuntimeError("password for %s not found in ~/.pgpass" % user)


def _connect_once(cfg):
    import psycopg2
    db = cfg["db"]
    transport = db.get("transport", "pg-direct")
    if transport == "pg-direct":
        with open(db["password_file"]) as f:
            pw = f.read().strip()
        kwargs = dict(host=db["host"], port=db.get("port", 5432),
                      dbname=db.get("dbname", "teambus"), user=db["user"],
                      password=pw, sslmode="require", connect_timeout=25,
                      keepalives=1, keepalives_idle=30,
                      keepalives_interval=10, keepalives_count=3)
        if db.get("sslnegotiation"):
            kwargs["sslnegotiation"] = db["sslnegotiation"]
        return psycopg2.connect(**kwargs)
    if transport == "pg-dsn":
        return psycopg2.connect(db["dsn"], connect_timeout=20,
                                keepalives=1, keepalives_idle=30)
    raise RuntimeError("unknown db.transport: %s" % transport)


def connect(cfg, write=False):
    """Fresh connection per operation, with one retry on an operational error."""
    import psycopg2
    last = None
    for attempt in range(2):
        try:
            conn = _connect_once(cfg)
            conn.autocommit = True
            return conn
        except psycopg2.OperationalError as e:
            last = e
            if attempt == 0:
                time.sleep(2)
    raise last


# ---------------------------------------------------------------- helpers

def member_names(cur):
    cur.execute("SELECT name FROM team_members ORDER BY name")
    return [r[0] for r in cur.fetchall()]


def resolve_recipient(cur, wanted):
    names = member_names(cur)
    if wanted in names:
        return wanted
    close = difflib.get_close_matches(wanted, names, n=3, cutoff=0.5)
    hint = (" Did you mean: %s?" % ", ".join(close)) if close else ""
    raise SystemExit("teambus: unknown recipient '%s'.%s Roster: %s"
                     % (wanted, hint, ", ".join(names)))


def online(row, now_ts):
    hb, ttl = row["heartbeat_at"], row["ttl_s"]
    if hb is None:
        return False
    return (now_ts - hb.timestamp()) <= ttl


# ---------------------------------------------------------------- commands

def cmd_who(cfg):
    conn = connect(cfg)
    cur = conn.cursor()
    cur.execute("SELECT EXTRACT(EPOCH FROM now())")
    now_ts = float(cur.fetchone()[0])
    cur.execute(
        "SELECT m.name, m.kind, m.location, m.status, m.inbound_policy, "
        "m.heartbeat_at, m.ttl_s, "
        "(SELECT count(*) FROM team_messages x WHERE x.to_member = m.name "
        " AND x.state IN ('queued','held')) AS unread "
        "FROM team_members m ORDER BY m.name")
    cols = [d.name for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close(); conn.close()
    for r in rows:
        is_on = online(r, now_ts)
        if r["heartbeat_at"] is None:
            seen = "never"
        else:
            ago = max(0, int(now_ts - r["heartbeat_at"].timestamp()))
            seen = "%ds ago" % ago if ago < 3600 else "%dm ago" % (ago // 60)
        print("%s · %s · %s · %s · heartbeat %s · %d unread" % (
            r["name"], r["kind"], r["location"],
            "online" if is_on else "offline", seen, r["unread"]))
    return 0


def cmd_send(cfg, to, body, key):
    me = cfg["member"]
    conn = connect(cfg)
    cur = conn.cursor()
    to = resolve_recipient(cur, to)
    if to == me:
        raise SystemExit("teambus: cannot send to yourself")
    cur.execute("SELECT inbound_policy FROM team_members WHERE name = %s", (to,))
    policy = cur.fetchone()[0]
    if policy == "refuse":
        raise SystemExit("teambus: %s refuses inbound messages (inbound_policy=refuse)" % to)
    state = "held" if policy == "hold" else "queued"
    cur.execute(
        "SELECT count(*) FROM team_messages WHERE to_member = %s AND state IN ('queued','claimed')",
        (to,))
    if INBOX_CAP and cur.fetchone()[0] >= INBOX_CAP:
        raise SystemExit("teambus: %s inbox is at the %d-message unread cap; send refused"
                         % (to, INBOX_CAP))
    cur.execute(
        "SELECT count(*) FROM team_messages WHERE from_member = %s AND created_at > now() - interval '1 hour'",
        (me,))
    if SEND_RATE_PER_HOUR and cur.fetchone()[0] >= SEND_RATE_PER_HOUR:
        raise SystemExit("teambus: sender rate limit (%d/hour) reached; send refused"
                         % SEND_RATE_PER_HOUR)
    key = key or ("tb-" + uuid.uuid4().hex)
    cur.execute(
        "INSERT INTO team_messages (from_member, to_member, body, state, idempotency_key) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING seq",
        (me, to, body, state, key))
    row = cur.fetchone()
    if row:
        print("sent seq=%d to=%s state=%s key=%s" % (row[0], to, state, key))
        cur.execute(
            "SELECT heartbeat_at > now() - (ttl_s * interval '1 second') "
            "FROM team_members WHERE name = %s", (to,))
        live = cur.fetchone()[0]
        if not live:
            print("note: %s has no live listener right now; this waits until %s "
                  "checks the bus" % (to, to))
        poke_dispatcher()
    else:
        cur.execute("SELECT seq, to_member, state FROM team_messages WHERE idempotency_key = %s",
                    (key,))
        orig = cur.fetchone()
        print("duplicate (idempotency key already used): original seq=%d to=%s state=%s"
              % (orig[0], orig[1], orig[2]))
    cur.close(); conn.close()
    return 0


def cmd_switch(cfg, member, on):
    """on/off <member>: off maps to inbound_policy=refuse, on to accept."""
    conn = connect(cfg)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("UPDATE team_members SET inbound_policy = %s, updated_at = now() "
                "WHERE name = %s RETURNING inbound_policy", ("accept" if on else "refuse", member))
    row = cur.fetchone()
    if not row:
        raise SystemExit("teambus: no such member %s" % member)
    print("%s inbound_policy=%s" % (member, row[0]))
    cur.close(); conn.close()
    return 0


def cmd_inbox(cfg, counts_only):
    me = cfg["member"]
    conn = connect(cfg)
    cur = conn.cursor()
    cur.execute(
        "SELECT seq, from_member, kind, state, body FROM team_messages "
        "WHERE to_member = %s AND state IN ('queued','held') ORDER BY seq", (me,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    if counts_only:
        print("TEAM_UNREAD: %d" % len(rows))
        return 0
    if not rows:
        print("inbox empty for %s" % me)
        return 0
    for seq, frm, kind, state, body in rows:
        print('<team-message from="%s" kind="%s" id="%d" state="%s">%s</team-message>'
              % (frm, kind, seq, state, body))
    print("%d message(s) waiting for %s" % (len(rows), me))
    return 0


def cmd_claim(cfg, frm=None, seq=None):
    me = cfg["member"]
    conn = connect(cfg)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        # Reclaim expired leases first (crashed holders lose nothing).
        cur.execute(
            "UPDATE team_messages SET state='queued', claimed_by=NULL, "
            "claim_lease_until=NULL, claimed_at=NULL WHERE to_member=%s "
            "AND state='claimed' AND claim_lease_until < now()", (me,))
        cur.execute(
            "SELECT seq, from_member, kind, body FROM team_messages "
            "WHERE to_member=%s AND state='queued' "
            "AND (%s::text IS NULL OR from_member=%s) "
            "AND (%s::bigint IS NULL OR seq=%s) ORDER BY seq LIMIT 1 "
            "FOR UPDATE SKIP LOCKED", (me, frm, frm, seq, seq))
        row = cur.fetchone()
        if not row:
            conn.commit()
            print("inbox empty for %s (nothing claimable)" % me)
            return 0
        seq, frm, kind, body = row
        cur.execute(
            "UPDATE team_messages SET state='claimed', claimed_by=%s, "
            "claimed_at=now(), claim_lease_until=now() + (%s * interval '1 minute') "
            "WHERE seq=%s", (me, LEASE_MINUTES, seq))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()
    print('<team-message from="%s" kind="%s" id="%d" state="claimed">%s</team-message>'
          % (frm, kind, seq, body))
    return 0


def cmd_done(cfg, seq):
    me = cfg["member"]
    conn = connect(cfg)
    cur = conn.cursor()
    cur.execute(
        "UPDATE team_messages SET state='done', done_at=now() WHERE seq=%s "
        "AND to_member=%s AND state IN ('queued','claimed') "
        "AND (claimed_by IS NULL OR claimed_by=%s)", (seq, me, me))
    n = cur.rowcount
    cur.close(); conn.close()
    if n == 0:
        raise SystemExit("teambus: seq %s is not an open message for %s "
                         "(already done, claimed by someone else, or not yours)"
                         % (seq, me))
    print("done seq=%d" % seq)
    return 0


def cmd_heartbeat(cfg):
    me = cfg["member"]
    conn = connect(cfg)
    cur = conn.cursor()
    cur.execute(
        "UPDATE team_members SET heartbeat_at=now(), status='online', "
        "updated_at=now() WHERE name=%s", (me,))
    n = cur.rowcount
    cur.close(); conn.close()
    if n == 0:
        raise SystemExit("teambus: '%s' is not on the roster" % me)
    print("heartbeat ok for %s" % me)
    return 0


# ---------------------------------------------------------------- main

def cmd_fact(cfg, key, body, directive, idem):
    """Post to the facts board. Exact text is stored verbatim. Posting an
    existing key supersedes its current row in the same transaction, so
    the board is never ambiguous about which version is live."""
    me = cfg["member"]
    kind = "directive" if directive else "fact"
    idem = idem or ("tbf-" + uuid.uuid4().hex)
    conn = connect(cfg)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id FROM team_facts WHERE idempotency_key = %s", (idem,))
        dup = cur.fetchone()
        if dup:
            conn.commit()
            print("duplicate (idempotency key already used): original fact id=%d"
                  % dup[0])
            return 0
        cur.execute(
            "SELECT id FROM team_facts WHERE key = %s AND superseded_by IS NULL "
            "FOR UPDATE", (key,))
        prev = cur.fetchone()
        cur.execute(
            "INSERT INTO team_facts (key, kind, body, posted_by, idempotency_key, "
            "supersedes) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (key, kind, body, me, idem, prev[0] if prev else None))
        new_id = cur.fetchone()[0]
        if prev:
            cur.execute(
                "UPDATE team_facts SET superseded_by = %s WHERE id = %s",
                (new_id, prev[0]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()
    poke_dispatcher()
    print("fact posted id=%d key=%s kind=%s%s"
          % (new_id, key, kind, " (supersedes %d)" % prev[0] if prev else ""))
    return 0


RETIRED_PREFIX = "RETIRED: "


def cmd_retire(cfg, key, reason):
    """Supersede a finished fact with a RETIRED tombstone. Readers hide it from
    the board; `facts <key>` still shows the full history. No schema change,
    and readers that do not know the convention just see the tombstone text."""
    conn = connect(cfg)
    cur = conn.cursor()
    cur.execute("SELECT body FROM team_facts WHERE key = %s AND superseded_by IS NULL", (key,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        raise SystemExit("teambus: no current fact with key '%s'" % key)
    if row[0].startswith(RETIRED_PREFIX):
        print("already retired: %s" % key)
        return 0
    return cmd_fact(cfg, key, RETIRED_PREFIX + reason, False, None)


def cmd_facts(cfg, key):
    conn = connect(cfg)
    cur = conn.cursor()
    if key:
        cur.execute(
            "SELECT id, key, kind, body, posted_by, created_at, superseded_by "
            "FROM team_facts WHERE key = %s ORDER BY id DESC", (key,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        if not rows:
            print("no fact with key '%s'" % key)
            return 0
        for fid, k, kind, body, by, at, sup in rows:
            state = "current" if sup is None else "superseded by %d" % sup
            print('<fact id="%d" key="%s" kind="%s" by="%s" at="%s" state="%s">%s</fact>'
                  % (fid, k, kind, by, at, state, body))
        return 0
    cur.execute(
        "SELECT id, key, kind, body, posted_by, created_at FROM team_facts "
        "WHERE superseded_by IS NULL AND body NOT LIKE %s ORDER BY key",
        (RETIRED_PREFIX + "%",))
    rows = cur.fetchall()
    cur.close(); conn.close()
    if not rows:
        print("facts board is empty")
        return 0
    for fid, k, kind, body, by, at in rows:
        print('<fact id="%d" key="%s" kind="%s" by="%s" at="%s">%s</fact>'
              % (fid, k, kind, by, at, body))
    print("%d current fact(s) on the board" % len(rows))
    return 0


def cmd_task_add(cfg, owner, title):
    conn = connect(cfg)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM team_members WHERE name = %s", (owner,))
    if not cur.fetchone():
        raise SystemExit("teambus: '%s' is not on the roster" % owner)
    cur.execute("INSERT INTO team_tasks (title, owner, created_by) VALUES (%s, %s, %s) RETURNING id",
                (title, owner, cfg["member"]))
    tid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    print("task #%d added for %s" % (tid, owner))
    return 0


def cmd_task_done(cfg, tid, note):
    conn = connect(cfg)
    cur = conn.cursor()
    cur.execute("UPDATE team_tasks SET status = 'done', done_at = now(), done_by = %s, "
                "note = COALESCE(%s, note) WHERE id = %s AND status = 'open' RETURNING title",
                (cfg["member"], note, tid))
    row = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    print(("task #%d done: %s" % (tid, row[0])) if row else ("no open task #%d" % tid))
    return 0


def cmd_tasks(cfg, owner, show_all):
    conn = connect(cfg)
    cur = conn.cursor()
    cur.execute("SELECT id, owner, title, created_by, status, created_at, done_by, note FROM team_tasks "
                "WHERE (%s::text IS NULL OR owner = %s) AND (%s OR status = 'open') "
                "ORDER BY status, id", (owner, owner, show_all))
    rows = cur.fetchall()
    cur.close(); conn.close()
    for tid, own, title, by, status, at, done_by, note in rows:
        print('<task id="%d" owner="%s" by="%s" status="%s" at="%s"%s>%s%s</task>'
              % (tid, own, by, status, at, ' done_by="%s"' % done_by if done_by else '',
                 title, (' [note: %s]' % note) if note else ''))
    print("%d task(s)" % len(rows))
    return 0


def main(argv):
    args = list(argv)
    cfg_path = None
    if "--config" in args:
        i = args.index("--config")
        cfg_path = args[i + 1]
        del args[i:i + 2]
    if not args:
        print(__doc__)
        return 2
    cfg = load_config(cfg_path)
    cmd, rest = args[0], args[1:]
    if cmd == "who":
        return cmd_who(cfg)
    if cmd == "heartbeat":
        return cmd_heartbeat(cfg)
    if cmd == "inbox":
        return cmd_inbox(cfg, counts_only="--counts" in rest)
    if cmd == "claim":
        frm = rest[rest.index("--from") + 1] if "--from" in rest else None
        seq = int(rest[rest.index("--seq") + 1]) if "--seq" in rest else None
        return cmd_claim(cfg, frm, seq)
    if cmd == "done":
        if len(rest) != 1:
            raise SystemExit("usage: teambus.py done <seq>")
        return cmd_done(cfg, int(rest[0]))
    if cmd == "send":
        key = None
        if "--key" in rest:
            i = rest.index("--key")
            key = rest[i + 1]
            del rest[i:i + 2]
        if len(rest) < 2:
            raise SystemExit("usage: teambus.py send <member> <body...> [--key K]")
        return cmd_send(cfg, rest[0], " ".join(rest[1:]), key)
    if cmd == "fact":
        directive = "--directive" in rest
        if directive:
            rest.remove("--directive")
        idem = None
        if "--key" in rest:
            i = rest.index("--key")
            idem = rest[i + 1]
            del rest[i:i + 2]
        if len(rest) < 2:
            raise SystemExit("usage: teambus.py fact [--directive] <key> <body...> [--key IDEM]")
        return cmd_fact(cfg, rest[0], " ".join(rest[1:]), directive, idem)
    if cmd == "facts":
        return cmd_facts(cfg, rest[0] if rest else None)
    if cmd in ("on", "off"):
        if len(rest) != 1:
            raise SystemExit("usage: teambus.py on|off <member>")
        return cmd_switch(cfg, rest[0], cmd == "on")
    if cmd == "task":
        if len(rest) >= 3 and rest[0] == "add":
            return cmd_task_add(cfg, rest[1], " ".join(rest[2:]))
        if len(rest) >= 2 and rest[0] == "done":
            return cmd_task_done(cfg, int(rest[1]), " ".join(rest[2:]) or None)
        raise SystemExit("usage: teambus.py task add <owner> <title...> | task done <id> [note...]")
    if cmd == "tasks":
        owner = next((a for a in rest if not a.startswith("--")), None)
        return cmd_tasks(cfg, owner, "--all" in rest)
    if cmd == "retire":
        if len(rest) < 2:
            raise SystemExit("usage: teambus.py retire <key> <reason...>")
        return cmd_retire(cfg, rest[0], " ".join(rest[1:]))
    print("unknown command: %s" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
