#!/usr/bin/env python3
"""Team Bus member CLI.

One addressed inbox for a team's agents, backed by Postgres tables
(`team_members`, `team_messages`, `team_facts`, ...).

  - Identity comes from the database login, enforced by the database
    (identity_mode 'enforced'): each member connects with its own login role,
    mapped in team_members.db_role. The config's "member" must match it. In
    'trusted' mode (v0.1 behaviour) the config's member name is believed.
  - Every write goes through a teambus_* function (api.sql) that applies
    identity, authority (only owners post directives), the recipient's
    hold/refuse policy, the inbox and rate fuses, and the message state machine.
  - Presence is heartbeat + TTL, computed at read time.
  - Delivery is claim-with-lease + explicit done. A claim returns a token;
    done/renew/fail must present it, so a worker whose lease expired cannot
    finish a newer attempt. Reading never deletes.
  - Idempotency keys are remembered for life, across compaction.
  - Message and fact text is untrusted data. Default output escapes it inside
    a tagged envelope; `--json` emits one JSON object per line with metadata
    and content in separate fields (use this from adapters).

Usage:
  teambus.py [--config PATH] [--json] whoami
  teambus.py [--config PATH] [--json] who
  teambus.py [--config PATH] send <member> <body...> [--key IDEMPOTENCY_KEY]
  teambus.py [--config PATH] [--json] inbox [--counts]
  teambus.py [--config PATH] [--json] claim [--from MEMBER] [--seq N]
  teambus.py [--config PATH] renew <seq> --token T [--minutes N]
  teambus.py [--config PATH] done <seq> [--token T]   (no token: finish an unclaimed queued message)
  teambus.py [--config PATH] fail <seq> --token T <reason...>
  teambus.py [--config PATH] release <seq>            (held or failed -> queued)
  teambus.py [--config PATH] heartbeat
  teambus.py [--config PATH] on|off <member>          (yourself, or anyone if you are an owner)
  teambus.py [--config PATH] fact [--directive] <key> <body...> [--key IDEM]
  teambus.py [--config PATH] [--json] facts [key]
  teambus.py [--config PATH] retire <key> <reason...>
  teambus.py [--config PATH] task add <owner> <title...>
  teambus.py [--config PATH] task done <id> [note...]
  teambus.py [--config PATH] [--json] tasks [owner | --all]
  teambus.py [--config PATH] compact --older-than 90d [--dry-run] [--export FILE.jsonl.gz]
  teambus.py [--config PATH] [--json] history <member|key> [--limit N]

Config (JSON): {"member": "alice", "db": {...}} where db.transport is one of
  "pg-direct"  Direct psycopg2: host, port, dbname, user, password_file.
  "pg-dsn"     Full libpq DSN string in key "dsn".

Config resolution: --config, then $TEAMBUS_CONFIG; if either is given and the
file is missing, that is an error (never a silent fallback to another
identity). Otherwise ./teambus.config.json, then ~/.teambus.json.
The DB password is never printed.
"""
import difflib
import json
import os
import sys
import time
import uuid

PGPASS = os.path.expanduser("~/.pgpass")

# Poke file for the dispatcher's adaptive backoff: after a send or fact post
# lands in the store, touching this local file tells a dispatcher on the same
# machine to run a cycle now. Remote senders cannot reach it; see PROTOCOL.md
# for the wake-latency contract.
POKE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "poke")

OUTPUT = {"json": False}

# Every connection resolves names in pg_catalog and public only: never in a
# "$user" schema that someone else could create to shadow the bus tables.
SEARCH_PATH = "pg_catalog, public, pg_temp"
SEARCH_PATH_OPT = "-c search_path=pg_catalog,public,pg_temp"


def poke_dispatcher():
    """Best-effort wake for the local dispatcher; never fails a send."""
    try:
        os.makedirs(os.path.dirname(POKE_PATH), exist_ok=True)
        with open(POKE_PATH, "a"):
            pass
        os.utime(POKE_PATH, None)
    except Exception:
        pass


# ---------------------------------------------------------------- output

def _esc(text, quote=False):
    s = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return s.replace('"', "&quot;") if quote else s


def emit(tag, attrs, body=None):
    """One record. Default: <tag k="v">escaped body</tag>, where body text can
    never close the envelope or forge attributes. --json: one JSON object with
    "type", the metadata fields, and "body" kept separate."""
    if OUTPUT["json"]:
        rec = {"type": tag}
        rec.update({k: v for k, v in attrs.items()})
        if body is not None:
            rec["body"] = body
        print(json.dumps(rec, default=str))
        return
    a = "".join(' %s="%s"' % (k, _esc(v, True)) for k, v in attrs.items() if v is not None)
    if body is None:
        print("<%s%s/>" % (tag, a))
    else:
        print("<%s%s>%s</%s>" % (tag, a, _esc(body), tag))


def note(text):
    """Human summary line; suppressed in --json mode."""
    if not OUTPUT["json"]:
        print(text)


# ---------------------------------------------------------------- config

def load_config(path):
    explicit = path or os.environ.get("TEAMBUS_CONFIG")
    if explicit:
        if not os.path.exists(explicit):
            src = "--config" if path else "$TEAMBUS_CONFIG"
            raise SystemExit("teambus: %s points at %s, which does not exist "
                             "(refusing to fall back to another identity)" % (src, explicit))
        candidates = [explicit]
    else:
        candidates = [os.path.join(os.getcwd(), "teambus.config.json"),
                      os.path.expanduser("~/.teambus.json")]
    for cand in candidates:
        if os.path.exists(cand):
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
        return psycopg2.connect(options=SEARCH_PATH_OPT, **kwargs)
    if transport == "pg-dsn":
        return psycopg2.connect(db["dsn"], options=SEARCH_PATH_OPT, connect_timeout=20,
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


def api(cfg, fn, *args, fetch="all"):
    """Call one teambus_* database function as this member. Database refusals
    (identity, authority, state) become a clean `teambus: ...` exit."""
    import psycopg2
    conn = connect(cfg)
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM %s(%s)" % (fn, ", ".join(["%s"] * (len(args) + 1))),
                    (cfg.get("member"),) + tuple(args))
        if fetch == "one":
            return cur.fetchone()
        if fetch == "value":
            row = cur.fetchone()
            return row[0] if row else None
        return cur.fetchall()
    except psycopg2.errors.UndefinedFunction:
        raise SystemExit("teambus: this database has no %s(); upgrade it with "
                         "`python tools/migrate.py --apply` (as the owner)" % fn)
    except psycopg2.Error as e:
        msg = (e.diag.message_primary or str(e)).strip()
        raise SystemExit("teambus: %s" % msg)
    finally:
        cur.close()
        conn.close()


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


def whoami(cfg):
    member, role, mode, admin = api(cfg, "teambus_whoami", fetch="one")
    return {"member": member, "role": role, "identity_mode": mode, "admin": admin}


# ---------------------------------------------------------------- commands

def cmd_whoami(cfg):
    w = whoami(cfg)
    emit("whoami", w)
    return 0


def cmd_who(cfg):
    conn = connect(cfg)
    cur = conn.cursor()
    cur.execute("SELECT EXTRACT(EPOCH FROM now())")
    now_ts = float(cur.fetchone()[0])
    cur.execute("SELECT m.name, m.kind, m.location, m.status, m.inbound_policy, "
                "m.heartbeat_at, m.ttl_s, m.role, u.unread "
                "FROM team_members m LEFT JOIN teambus_unread_counts() u ON u.member = m.name "
                "ORDER BY m.name")
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
        if OUTPUT["json"]:
            emit("member", {"name": r["name"], "kind": r["kind"], "location": r["location"],
                            "role": r["role"], "online": is_on, "heartbeat": seen,
                            "inbound_policy": r["inbound_policy"], "unread": r["unread"] or 0})
        else:
            print("%s · %s · %s · %s · heartbeat %s · %d unread" % (
                r["name"], r["kind"], r["location"],
                "online" if is_on else "offline", seen, r["unread"] or 0))
    return 0


def cmd_send(cfg, to, body, key):
    conn = connect(cfg)
    cur = conn.cursor()
    to = resolve_recipient(cur, to)          # friendly typo hints; the DB re-checks
    cur.close(); conn.close()
    key = key or ("tb-" + uuid.uuid4().hex)
    row = api(cfg, "teambus_send", to, body, key, fetch="one")
    if not row:
        raise SystemExit("teambus: key %s is registered but its message cannot be read" % key)
    seq, state, duplicate, live = row
    if duplicate:
        print("duplicate (idempotency key already used): original seq=%s state=%s" % (seq, state))
        return 0
    print("sent seq=%d to=%s state=%s key=%s" % (seq, to, state, key))
    if not live:
        print("note: %s has no live listener right now; this waits until %s checks the bus" % (to, to))
    poke_dispatcher()
    return 0


def cmd_switch(cfg, member, on):
    """on/off <member>: off maps to inbound_policy=refuse, on to accept."""
    pol = api(cfg, "teambus_set_policy", member, "accept" if on else "refuse", fetch="value")
    print("%s inbound_policy=%s" % (member, pol))
    return 0


def cmd_inbox(cfg, counts_only):
    me = whoami(cfg)["member"]
    conn = connect(cfg)
    cur = conn.cursor()
    # Expired claims are listed too: they are claimable again.
    cur.execute(
        "SELECT seq, from_member, kind, "
        "CASE WHEN state = 'claimed' THEN 'expired' ELSE state END, body, attempts "
        "FROM team_messages WHERE to_member = %s AND (state IN ('queued','held') "
        "OR (state = 'claimed' AND claim_lease_until < now())) ORDER BY seq", (me,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    if counts_only:
        if OUTPUT["json"]:
            emit("inbox-count", {"member": me, "unread": len(rows)})
        else:
            print("TEAM_UNREAD: %d" % len(rows))
        return 0
    if not rows:
        note("inbox empty for %s" % me)
        return 0
    for seq, frm, kind, state, body, attempts in rows:
        emit("team-message", {"from": frm, "kind": kind, "id": seq, "state": state,
                              "attempts": attempts}, body)
    note("%d message(s) waiting for %s" % (len(rows), me))
    return 0


def cmd_claim(cfg, frm=None, seq=None):
    row = api(cfg, "teambus_claim", frm, seq, fetch="one")
    if not row:
        note("inbox empty for %s (nothing claimable)" % cfg.get("member"))
        return 0
    seq, frm, kind, body, token, until, attempt = row
    emit("team-message", {"from": frm, "kind": kind, "id": seq, "state": "claimed",
                          "token": str(token), "lease_until": until.isoformat(),
                          "attempt": attempt}, body)
    note("claimed seq=%d; finish with: done %d --token %s (renew before %s)"
         % (seq, seq, token, until.isoformat(timespec="seconds")))
    return 0


def cmd_renew(cfg, seq, token, minutes):
    until = api(cfg, "teambus_renew", seq, token, minutes, fetch="value")
    print("renewed seq=%d until %s" % (seq, until.isoformat(timespec="seconds")))
    return 0


def cmd_done(cfg, seq, token=None):
    api(cfg, "teambus_done", seq, token, fetch="value")
    print("done seq=%d" % seq)
    return 0


def cmd_fail(cfg, seq, token, reason):
    st = api(cfg, "teambus_fail", seq, token, reason, fetch="value")
    print("seq=%d returned as %s" % (seq, st))
    return 0


def cmd_release(cfg, seq):
    st = api(cfg, "teambus_release", seq, fetch="value")
    print("released seq=%d -> %s" % (seq, st))
    return 0


def cmd_heartbeat(cfg):
    api(cfg, "teambus_heartbeat", None, None, fetch="value")
    print("heartbeat ok for %s" % cfg.get("member"))
    return 0


def cmd_fact(cfg, key, body, directive, idem):
    """Post to the facts board. Text is stored verbatim. Posting an existing key
    supersedes its current row atomically; the database guarantees one current
    row per key."""
    kind = "directive" if directive else "fact"
    idem = idem or ("tbf-" + uuid.uuid4().hex)
    new_id, prev, duplicate = api(cfg, "teambus_post_fact", key, kind, body, idem, fetch="one")
    if duplicate:
        print("duplicate (idempotency key already used): original fact id=%s" % new_id)
        return 0
    poke_dispatcher()
    print("fact posted id=%d key=%s kind=%s%s"
          % (new_id, key, kind, " (supersedes %d)" % prev if prev else ""))
    return 0


RETIRED_PREFIX = "RETIRED: "


def cmd_retire(cfg, key, reason):
    """Supersede a finished fact with a RETIRED tombstone. Readers hide it from
    the board; `facts <key>` still shows the full history."""
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


# ---------------------------------------------------------------- compaction
# Archive, never delete: every row that leaves a hot table lands in its
# archive twin inside the same statement, so a failure moves nothing.

MSG_COLS = ("seq, from_member, to_member, body, kind, state, idempotency_key, claimed_by, "
            "claim_lease_until, created_at, claimed_at, done_at, claim_token, attempts, last_error, releases")
FACT_COLS = "id, key, kind, body, posted_by, idempotency_key, supersedes, superseded_by, created_at"

MSG_WHERE = "state = 'done' AND COALESCE(done_at, created_at) < %(cutoff)s"
FACT_WHERE = """(
  (f.superseded_by IS NOT NULL AND f.created_at < %(cutoff)s AND
   COALESCE((SELECT s.created_at FROM team_facts s WHERE s.id = f.superseded_by),
            (SELECT a.created_at FROM team_facts_archive a WHERE a.id = f.superseded_by)) < %(cutoff)s)
  OR
  (f.superseded_by IS NULL AND f.body LIKE %(retired)s AND f.created_at < %(cutoff)s)
)"""


class ExportError(RuntimeError):
    """The archive move committed, but the export file could not be finished.
    The prepared export is kept at .path; nothing was lost."""

    def __init__(self, msg, path, result):
        super().__init__(msg)
        self.path, self.result = path, result


def parse_age(text):
    import datetime as _dt
    t = text.strip().lower()
    if len(t) < 2 or t[-1] not in "dh" or not t[:-1].isdigit():
        raise SystemExit("teambus: --older-than takes Nd or Nh, e.g. 90d")
    n = int(t[:-1])
    return _dt.timedelta(days=n) if t[-1] == "d" else _dt.timedelta(hours=n)


def compact(conn, older_than, dry_run=False, export_path=None):
    """Move old finished rows into the archive tables in one transaction.
    Returns {'messages': n, 'facts': n, 'dry_run': bool, 'cutoff': iso}.
    Idempotent: a second run with the same cutoff finds nothing to move.
    Delivered wake bookkeeping older than the cutoff is pruned (it is a hint
    log, not a record). The idempotency registry is never touched.
    If the export cannot be finalized after the commit, raises ExportError and
    keeps the prepared file at <export>.partial."""
    import datetime as _dt
    cutoff = _dt.datetime.now(_dt.timezone.utc) - older_than
    params = {"cutoff": cutoff, "retired": RETIRED_PREFIX + "%"}
    conn.autocommit = False
    cur = conn.cursor()
    committed = False
    partial = export_path + ".partial" if export_path else None
    try:
        cur.execute("SET LOCAL search_path = " + SEARCH_PATH)   # callers may pass their own conn
        cur.execute("SELECT to_regclass('team_messages_archive'), to_regclass('team_facts_archive')")
        if None in cur.fetchone():
            raise SystemExit("teambus: archive tables missing; run tools/migrate.py --apply first")
        cur.execute("LOCK TABLE team_facts IN SHARE ROW EXCLUSIVE MODE")
        if dry_run:
            cur.execute("SELECT count(*) FROM team_messages WHERE " + MSG_WHERE, params)
            nm = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM team_facts f WHERE " + FACT_WHERE, params)
            nf = cur.fetchone()[0]
            conn.rollback()
            return {"messages": nm, "facts": nf, "dry_run": True, "cutoff": cutoff.isoformat()}
        cur.execute(
            "WITH moved AS (DELETE FROM team_messages WHERE " + MSG_WHERE + " RETURNING " + MSG_COLS + ") "
            "INSERT INTO team_messages_archive (" + MSG_COLS + ") SELECT " + MSG_COLS + " FROM moved "
            "RETURNING " + MSG_COLS, params)
        mrows = cur.fetchall()
        cur.execute(
            "WITH pick AS (SELECT f.id FROM team_facts f WHERE " + FACT_WHERE + "), "
            "moved AS (DELETE FROM team_facts WHERE id IN (SELECT id FROM pick) RETURNING " + FACT_COLS + ") "
            "INSERT INTO team_facts_archive (" + FACT_COLS + ") SELECT " + FACT_COLS + " FROM moved "
            "RETURNING " + FACT_COLS, params)
        frows = cur.fetchall()
        cur.execute("SELECT to_regclass('team_wake_deliveries')")
        if cur.fetchone()[0]:
            cur.execute("DELETE FROM team_wake_deliveries WHERE status IN ('delivered','skipped') "
                        "AND COALESCE(delivered_at, created_at) < %(cutoff)s", params)
        tmp = None
        if export_path and (mrows or frows):
            tmp = _write_export(partial, mrows, frows, cutoff)
        conn.commit()
        committed = True
        result = {"messages": len(mrows), "facts": len(frows), "dry_run": False,
                  "cutoff": cutoff.isoformat()}
        if tmp:
            try:
                os.replace(tmp, export_path)
            except OSError as e:
                raise ExportError("archive committed (%d message(s), %d fact(s)) but the export could "
                                  "not be moved into place: %s; the full export is kept at %s"
                                  % (len(mrows), len(frows), e, tmp), tmp, result)
        return result
    except BaseException:
        if not committed:
            conn.rollback()
            if partial and os.path.exists(partial):
                os.remove(partial)
        raise
    finally:
        cur.close()
        conn.autocommit = True


def _write_export(path, mrows, frows, cutoff):
    import gzip
    mcols = [c.strip() for c in MSG_COLS.split(",")]
    fcols = [c.strip() for c in FACT_COLS.split(",")]
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"table": "_meta", "cutoff": cutoff.isoformat(),
                             "messages": len(mrows), "facts": len(frows)}) + "\n")
        for r in mrows:
            fh.write(json.dumps(dict({"table": "team_messages"}, **dict(zip(mcols, r))), default=str) + "\n")
        for r in frows:
            fh.write(json.dumps(dict({"table": "team_facts"}, **dict(zip(fcols, r))), default=str) + "\n")
    return path


def cmd_compact(cfg, age, dry_run, export):
    conn = connect(cfg, write=True)
    try:
        r = compact(conn, parse_age(age), dry_run, export)
    except ExportError as e:
        raise SystemExit("teambus: %s" % e)
    finally:
        conn.close()
    verb = "would archive" if r["dry_run"] else "archived"
    tail = ""
    if export and not r["dry_run"] and (r["messages"] or r["facts"]):
        tail = "; export %s" % export
    print("%s %d message(s) and %d fact(s) older than %s (cutoff %s)%s"
          % (verb, r["messages"], r["facts"], age, r["cutoff"], tail))
    return 0


def history(conn, target, limit=200):
    """Full trace for a member (mail to/from) or a fact key, hot + archive.
    Row security limits mail to what the caller may read."""
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM team_members WHERE name = %s", (target,))
    is_member = cur.fetchone() is not None
    cur.execute("SELECT to_regclass('team_messages_archive') IS NOT NULL")
    has_archive = cur.fetchone()[0]
    if is_member:
        q = ("SELECT seq, from_member, to_member, state, created_at, done_at, body, 'hot' FROM team_messages "
             "WHERE from_member = %(t)s OR to_member = %(t)s")
        if has_archive:
            q += (" UNION ALL SELECT seq, from_member, to_member, state, created_at, done_at, body, 'archive' "
                  "FROM team_messages_archive WHERE from_member = %(t)s OR to_member = %(t)s")
        cur.execute("SELECT * FROM (" + q + ") x ORDER BY seq DESC LIMIT %(n)s", {"t": target, "n": limit})
        out = [("message",) + tuple(r) for r in cur.fetchall()]
    else:
        q = ("SELECT id, key, kind, posted_by, created_at, superseded_by, body, 'hot' FROM team_facts "
             "WHERE key = %(t)s")
        if has_archive:
            q += (" UNION ALL SELECT id, key, kind, posted_by, created_at, superseded_by, body, 'archive' "
                  "FROM team_facts_archive WHERE key = %(t)s")
        cur.execute("SELECT * FROM (" + q + ") x ORDER BY id DESC LIMIT %(n)s", {"t": target, "n": limit})
        out = [("fact",) + tuple(r) for r in cur.fetchall()]
    cur.close()
    return out


def cmd_history(cfg, target, limit):
    conn = connect(cfg)
    try:
        rows = history(conn, target, limit)
    finally:
        conn.close()
    if not rows:
        note("no history for '%s'" % target)
        return 0
    for r in rows:
        if r[0] == "message":
            _, seq, frm, to, state, at, done, body, where = r
            emit("message", {"seq": seq, "from": frm, "to": to, "state": state, "at": at, "in": where}, body)
        else:
            _, fid, key, kind, by, at, sup, body, where = r
            state = "current" if sup is None else "superseded by %d" % sup
            emit("fact", {"id": fid, "key": key, "kind": kind, "by": by, "at": at,
                          "state": state, "in": where}, body)
    note("%d row(s), newest first" % len(rows))
    return 0


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
            note("no fact with key '%s'" % key)
            return 0
        for fid, k, kind, body, by, at, sup in rows:
            state = "current" if sup is None else "superseded by %d" % sup
            emit("fact", {"id": fid, "key": k, "kind": kind, "by": by, "at": at, "state": state}, body)
        return 0
    cur.execute(
        "SELECT id, key, kind, body, posted_by, created_at FROM team_facts "
        "WHERE superseded_by IS NULL AND body NOT LIKE %s ORDER BY key",
        (RETIRED_PREFIX + "%",))
    rows = cur.fetchall()
    cur.close(); conn.close()
    if not rows:
        note("facts board is empty")
        return 0
    for fid, k, kind, body, by, at in rows:
        emit("fact", {"id": fid, "key": k, "kind": kind, "by": by, "at": at}, body)
    note("%d current fact(s) on the board" % len(rows))
    return 0


def cmd_task_add(cfg, owner, title):
    tid = api(cfg, "teambus_task_add", owner, title, fetch="value")
    print("task #%d added for %s" % (tid, owner))
    return 0


def cmd_task_done(cfg, tid, note_text):
    title = api(cfg, "teambus_task_done", tid, note_text, fetch="value")
    print(("task #%d done: %s" % (tid, title)) if title
          else ("no open task #%d that you may close" % tid))
    return 0


def cmd_tasks(cfg, owner, show_all):
    conn = connect(cfg)
    cur = conn.cursor()
    cur.execute("SELECT id, owner, title, created_by, status, created_at, done_by, note FROM team_tasks "
                "WHERE (%s::text IS NULL OR owner = %s) AND (%s OR status = 'open') "
                "ORDER BY status, id", (owner, owner, show_all))
    rows = cur.fetchall()
    cur.close(); conn.close()
    for tid, own, title, by, status, at, done_by, note_text in rows:
        emit("task", {"id": tid, "owner": own, "by": by, "status": status, "at": at,
                      "done_by": done_by, "note": note_text}, title)
    note("%d task(s)" % len(rows))
    return 0


# ---------------------------------------------------------------- main

def _take(rest, flag, conv=str):
    """Remove `flag VALUE` from rest and return VALUE (or None)."""
    if flag not in rest:
        return None
    i = rest.index(flag)
    if i + 1 >= len(rest):
        raise SystemExit("teambus: %s needs a value" % flag)
    val = rest[i + 1]
    del rest[i:i + 2]
    return conv(val)


def main(argv):
    args = list(argv)
    cfg_path = _take(args, "--config")
    OUTPUT["json"] = "--json" in args
    if OUTPUT["json"]:
        args.remove("--json")
    if not args:
        print(__doc__)
        return 2
    cfg = load_config(cfg_path)
    cmd, rest = args[0], args[1:]
    if cmd == "whoami":
        return cmd_whoami(cfg)
    if cmd == "who":
        return cmd_who(cfg)
    if cmd == "heartbeat":
        return cmd_heartbeat(cfg)
    if cmd == "inbox":
        return cmd_inbox(cfg, counts_only="--counts" in rest)
    if cmd == "claim":
        return cmd_claim(cfg, _take(rest, "--from"), _take(rest, "--seq", int))
    if cmd == "renew":
        token, minutes = _take(rest, "--token"), _take(rest, "--minutes", int)
        if len(rest) != 1 or not token:
            raise SystemExit("usage: teambus.py renew <seq> --token T [--minutes N]")
        return cmd_renew(cfg, int(rest[0]), token, minutes)
    if cmd == "done":
        token = _take(rest, "--token")
        if len(rest) != 1:
            raise SystemExit("usage: teambus.py done <seq> [--token T]")
        return cmd_done(cfg, int(rest[0]), token)
    if cmd == "fail":
        token = _take(rest, "--token")
        if len(rest) < 2 or not token:
            raise SystemExit("usage: teambus.py fail <seq> --token T <reason...>")
        return cmd_fail(cfg, int(rest[0]), token, " ".join(rest[1:]))
    if cmd == "release":
        if len(rest) != 1:
            raise SystemExit("usage: teambus.py release <seq>")
        return cmd_release(cfg, int(rest[0]))
    if cmd == "send":
        key = _take(rest, "--key")
        if len(rest) < 2:
            raise SystemExit("usage: teambus.py send <member> <body...> [--key K]")
        return cmd_send(cfg, rest[0], " ".join(rest[1:]), key)
    if cmd == "fact":
        directive = "--directive" in rest
        if directive:
            rest.remove("--directive")
        idem = _take(rest, "--key")
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
    if cmd == "compact":
        age = _take(rest, "--older-than")
        if not age:
            raise SystemExit("usage: teambus.py compact --older-than 90d [--dry-run] [--export FILE.jsonl.gz]")
        return cmd_compact(cfg, age, "--dry-run" in rest, _take(rest, "--export"))
    if cmd == "history":
        limit = _take(rest, "--limit", int) or 200
        if len(rest) != 1:
            raise SystemExit("usage: teambus.py history <member|key> [--limit N]")
        return cmd_history(cfg, rest[0], limit)
    if cmd == "retire":
        if len(rest) < 2:
            raise SystemExit("usage: teambus.py retire <key> <reason...>")
        return cmd_retire(cfg, rest[0], " ".join(rest[1:]))
    print("unknown command: %s" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
