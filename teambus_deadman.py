#!/usr/bin/env python3
"""Team Bus dead-man's switch: alert out of band when the bus stalls.

A store-and-forward bus does not crash, it stalls silently: if the
dispatcher dies, mail still queues and nothing looks wrong until someone
asks why nobody answered. This check runs on a schedule (cron, a systemd
timer, or Task Scheduler every 15 minutes) and pages a phone when a stage
stops advancing.

Checks (each is a stage that must keep advancing):
  heartbeat  every non-human member that has ever beaten must have beaten in
             the last HEARTBEAT_STALE_MIN (the dispatcher keeps its own
             member's heartbeat fresh, so this also watches the wake layer)
  mail       no message may sit undone longer than MAIL_STALE_H
  failed     no message may sit in state 'failed' (gave up after retries)
  wakes      no wake delivery may be 'failed', or pending longer than WAKE_LATE_MIN
  store      the database itself must answer
The member this runs as needs role 'service' (or 'owner') to see all mail.

Alerts go to ntfy (https://ntfy.sh, free, no account): install the ntfy app
and subscribe to the random topic written to state/ntfy-topic.txt on first
run. One alert per problem, one "recovered" when it clears. An alert that
could not be sent is retried on the next run until it goes through. If
state/hc-url.txt holds a healthchecks.io ping URL, every run pings it
(/fail while a check fails), so a machine that is off still pages you.

  python teambus_deadman.py [--config teambus.config.json] [--dry-run]
Exit status: 0 healthy, 1 a check is failing.
"""
import json
import secrets
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import teambus  # noqa: E402

STATE = HERE / 'state'
LOG = HERE / 'logs' / 'deadman.log'
HEARTBEAT_STALE_MIN = 20
MAIL_STALE_H = 2
WAKE_LATE_MIN = 30
NTFY = 'https://ntfy.sh/'


def log(msg):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write('%s %s\n' % (datetime.now().strftime('%Y-%m-%dT%H:%M:%S'), msg))


def problems(beats, stuck, now, failed=(), wakes=(0, 0)):
    """beats: [(member, heartbeat_at or None)]; stuck: [(seq, to, created_at)];
    failed: [(seq, to, last_error)]; wakes: (failed_count, late_pending_count).
    Returns {key: text}."""
    out = {}
    for name, at in beats:
        if at is None:
            continue                     # never had a listener: not a stall
        mins = (now - at).total_seconds() / 60
        if mins > HEARTBEAT_STALE_MIN:
            out['heartbeat:' + name] = '%s heartbeat %d min old' % (name, mins)
    for seq, to, at in stuck:
        hours = (now - at).total_seconds() / 3600
        if hours > MAIL_STALE_H:
            out['mail:%s' % seq] = 'mail seq %s to %s undone for %.1f h' % (seq, to, hours)
    for seq, to, err in failed:
        out['failed:%s' % seq] = 'mail seq %s to %s failed: %s' % (seq, to, (err or '?')[:80])
    nfail, nlate = wakes
    if nfail:
        out['wakes:failed'] = '%d wake delivery(ies) failed after retries' % nfail
    if nlate:
        out['wakes:late'] = '%d wake delivery(ies) pending over %d min' % (nlate, WAKE_LATE_MIN)
    return out


def diff(prev, cur):
    # kept for callers of the v0.1 helper
    """prev, cur: {key: text}. Returns (new, cleared) as lists of texts."""
    new = [cur[k] for k in sorted(cur) if k not in prev]
    cleared = [prev[k] for k in sorted(prev) if k not in cur]
    return new, cleared


def gather(cfg):
    conn = teambus.connect(cfg)
    cur = conn.cursor()
    # Under row security a non-service member sees only its own mail and would
    # report a healthy bus it cannot actually see. Refuse to be blind.
    cur.execute("SELECT to_regprocedure('teambus_can_read_all()') IS NOT NULL")
    if cur.fetchone()[0]:
        cur.execute("SELECT teambus_can_read_all()")
        if not cur.fetchone()[0]:
            cur.close(); conn.close()
            raise RuntimeError("deadman member %s cannot read all mail; give it role 'service'"
                               % cfg.get('member'))
    cur.execute("SELECT name, heartbeat_at FROM team_members WHERE kind <> 'human' ORDER BY name")
    beats = cur.fetchall()
    cur.execute("SELECT seq, to_member, created_at FROM team_messages WHERE state NOT IN ('done', 'failed') "
                "AND created_at < now() - (%s::float8 * interval '1 hour') ORDER BY seq LIMIT 20", (MAIL_STALE_H,))
    stuck = cur.fetchall()
    failed, wakes = [], (0, 0)
    cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'team_messages' "
                "AND column_name = 'last_error')")
    if cur.fetchone()[0]:
        cur.execute("SELECT seq, to_member, last_error FROM team_messages WHERE state = 'failed' "
                    "ORDER BY seq LIMIT 20")
        failed = cur.fetchall()
    cur.execute("SELECT to_regclass('team_wake_deliveries')")
    if cur.fetchone()[0]:
        cur.execute("SELECT count(*) FILTER (WHERE status = 'failed'), "
                    "count(*) FILTER (WHERE status = 'pending' AND created_at < now() - (%s::float8 * interval '1 minute')) "
                    "FROM team_wake_deliveries", (WAKE_LATE_MIN,))
        wakes = tuple(cur.fetchone())
    cur.execute('SELECT now()')
    now = cur.fetchone()[0]
    cur.close(); conn.close()
    return beats, stuck, now, failed, wakes


def topic():
    f = STATE / 'ntfy-topic.txt'
    if not f.exists():
        STATE.mkdir(exist_ok=True)
        f.write_text('teambus-' + secrets.token_hex(8), encoding='utf-8')
    return f.read_text(encoding='utf-8').strip()


def post(url, body, title=None, priority=None):
    headers = {'user-agent': 'teambus-deadman'}
    if title:
        headers['Title'] = title
    if priority:
        headers['Priority'] = priority
    req = urllib.request.Request(url, data=body.encode('utf-8'), headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def notify(title, body, prio):
    """Send one alert. True when ntfy accepted it."""
    try:
        post(NTFY + topic(), body, title, prio)
        return True
    except Exception as e:
        log('ntfy failed: %r' % e)
        return False


def main(argv):
    cfg_path = argv[argv.index('--config') + 1] if '--config' in argv else None
    dry = '--dry-run' in argv
    cfg = teambus.load_config(cfg_path)
    state_file = STATE / 'deadman.json'
    saved = json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else {}
    prev_open = saved.get('open', {})
    # 'notified': problems whose alert actually went out. v0.1 state files
    # only had 'open', which recorded attempts; treat those as sent.
    notified = dict(saved.get('notified', prev_open))
    try:
        cur = problems(*gather(cfg))
    except Exception as e:  # the store itself unreachable is a stall too
        cur = dict(prev_open)
        cur['store'] = 'bus store unreachable: %s' % repr(e)[:120]
    to_alert = {k: cur[k] for k in sorted(cur) if k not in notified}
    to_clear = {k: notified[k] for k in sorted(notified) if k not in cur}
    where = cfg['member']
    if to_alert:
        title, body = 'Team Bus STALLED (seen from %s)' % where, '\n'.join(to_alert.values())
        log('%s: %s' % (title, body.replace('\n', '; ')))
        if not dry and notify(title, body, 'high'):
            notified.update(to_alert)
    if to_clear:
        title, body = 'Team Bus recovered (seen from %s)' % where, '\n'.join(to_clear.values())
        log('%s: %s' % (title, body.replace('\n', '; ')))
        if not dry and notify(title, body, 'default'):
            for k in to_clear:
                notified.pop(k, None)
    hc = STATE / 'hc-url.txt'
    if hc.exists() and not dry:
        try:
            post(hc.read_text(encoding='utf-8').strip() + ('/fail' if cur else ''), '\n'.join(cur.values()) or 'ok')
        except Exception as e:
            log('healthchecks ping failed: %r' % e)
    if not dry:
        STATE.mkdir(exist_ok=True)
        state_file.write_text(json.dumps({'at': datetime.now().astimezone().isoformat(timespec='seconds'),
                                          'open': cur, 'notified': notified}, indent=1), encoding='utf-8')
    print('open: %s' % ('; '.join(cur.values()) or 'none'))
    return 1 if cur else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
