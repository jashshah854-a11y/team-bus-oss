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
  store      the database itself must answer

Alerts go to ntfy (https://ntfy.sh, free, no account): install the ntfy app
and subscribe to the random topic written to state/ntfy-topic.txt on first
run. One alert per problem, one "recovered" when it clears. If
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
NTFY = 'https://ntfy.sh/'


def log(msg):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write('%s %s\n' % (datetime.now().strftime('%Y-%m-%dT%H:%M:%S'), msg))


def problems(beats, stuck, now):
    """beats: [(member, heartbeat_at or None)]; stuck: [(seq, to, created_at)].
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
    return out


def diff(prev, cur):
    """prev, cur: {key: text}. Returns (new, cleared) as lists of texts."""
    new = [cur[k] for k in sorted(cur) if k not in prev]
    cleared = [prev[k] for k in sorted(prev) if k not in cur]
    return new, cleared


def gather(cfg):
    conn = teambus.connect(cfg)
    cur = conn.cursor()
    cur.execute("SELECT name, heartbeat_at FROM team_members WHERE kind <> 'human' ORDER BY name")
    beats = cur.fetchall()
    cur.execute("SELECT seq, to_member, created_at FROM team_messages WHERE state <> 'done' "
                "AND created_at < now() - (%s * interval '1 hour') ORDER BY seq LIMIT 20", (MAIL_STALE_H,))
    stuck = cur.fetchall()
    cur.execute('SELECT now()')
    now = cur.fetchone()[0]
    cur.close(); conn.close()
    return beats, stuck, now


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


def main(argv):
    cfg_path = argv[argv.index('--config') + 1] if '--config' in argv else None
    dry = '--dry-run' in argv
    cfg = teambus.load_config(cfg_path)
    state_file = STATE / 'deadman.json'
    prev = json.loads(state_file.read_text(encoding='utf-8')).get('open', {}) if state_file.exists() else {}
    try:
        cur = problems(*gather(cfg))
    except Exception as e:  # the store itself unreachable is a stall too
        cur = dict(prev)
        cur['store'] = 'bus store unreachable: %s' % repr(e)[:120]
    new, cleared = diff(prev, cur)
    where = cfg['member']
    msgs = []
    if new:
        msgs.append(('Team Bus STALLED (seen from %s)' % where, '\n'.join(new), 'high'))
    if cleared:
        msgs.append(('Team Bus recovered (seen from %s)' % where, '\n'.join(cleared), 'default'))
    for title, body, prio in msgs:
        log('%s: %s' % (title, body.replace('\n', '; ')))
        if not dry:
            try:
                post(NTFY + topic(), body, title, prio)
            except Exception as e:
                log('ntfy failed: %r' % e)
    hc = STATE / 'hc-url.txt'
    if hc.exists() and not dry:
        try:
            post(hc.read_text(encoding='utf-8').strip() + ('/fail' if cur else ''), '\n'.join(cur.values()) or 'ok')
        except Exception as e:
            log('healthchecks ping failed: %r' % e)
    if not dry:
        STATE.mkdir(exist_ok=True)
        state_file.write_text(json.dumps({'at': datetime.now().astimezone().isoformat(timespec='seconds'),
                                          'open': cur}, indent=1), encoding='utf-8')
    print('open: %s' % ('; '.join(cur.values()) or 'none'))
    return 1 if cur else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
