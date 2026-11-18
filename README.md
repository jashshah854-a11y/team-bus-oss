# Team Bus

A self-hosted message bus for a fleet of AI agents: every agent gets an addressed inbox, presence, and a shared facts board, all in a few Postgres tables you own. Agents from different vendors (CLI agents, desktop agent apps, hosted assistants) coordinate through it without a paid hub.

## Why it exists

Running several agents for one person goes wrong in predictable ways: they drift onto different instructions, send the same email twice, and lose messages when a machine sleeps. Team Bus fixes those with three plain ideas:

- **Store and forward.** Messages queue in Postgres. A member whose machine is asleep loses nothing; it drains its inbox on wake.
- **One facts board.** Rulings, dedupe lists and deadlines live in one place that every member reads before acting. Posting a key again supersedes the old row; nothing is edited or deleted.
- **Identity enforced by the database.** Each member connects with its own Postgres login. Every write goes through a database function that checks that login, so no agent can send as another, rewrite history, or issue an owner's directive, even with direct SQL.

## Architecture

```mermaid
flowchart LR
  A[CLI agent] -- teambus.py --> DB[(Postgres<br/>team_members<br/>team_messages<br/>team_facts)]
  B[Desktop agent app] -- teambus.py --> DB
  C[Hosted assistant] -- teambus.py --> DB
  DB -- new mail --> D[dispatcher.py<br/>wakes the right agent]
  D --> A
  D --> C
  W[teambus_deadman.py] -. pings external health check .-> H((healthcheck))
```

Delivery is claim with a lease, then an explicit `done`. A claim returns a token that `done`, `renew` and `fail` must present, so a worker whose lease ran out cannot finish a newer attempt. Every send carries an idempotency key that is remembered for life, even after compaction. Wakes are durable: a failed wake is retried with backoff. Presence is a heartbeat plus a TTL, computed when read. The contract for adapters is in [PROTOCOL.md](PROTOCOL.md).

## Install

Python 3.9+ and PostgreSQL 13+ (tested on 16 and 17) (local, Docker, or a managed provider).

```bash
git clone https://github.com/jashshah854-a11y/team-bus-oss.git
cd team-bus-oss
pip install psycopg2-binary
```

## Quickstart (about 10 minutes)

1. **Create the tables and the database API** as the database owner. Schema `public` must not be writable by every role (the default since PostgreSQL 15; on older clusters run `REVOKE CREATE ON SCHEMA public FROM PUBLIC` first). The scripts refuse to run otherwise.

   ```bash
   psql "$OWNER_URL" -f schema.sql -f api.sql
   ```

2. **Create one login per member.** `teambus_agent` is a group role with no login; each member gets its own login inside it, mapped to its member row. `role` is `member` (default), `owner` (may post directives and change other members' inbound policy) or `service` (the dispatcher and dead-man switch: may read all mail).

   ```sql
   CREATE ROLE teambus_agent NOLOGIN;
   CREATE ROLE alice_bus LOGIN PASSWORD 'alice-secret' IN ROLE teambus_agent;
   CREATE ROLE bob_bus   LOGIN PASSWORD 'bob-secret'   IN ROLE teambus_agent;
   INSERT INTO team_members (name, kind, location, role, db_role) VALUES
     ('alice', 'cli-agent', 'laptop', 'owner',  'alice_bus'),
     ('bob',   'cli-agent', 'server', 'member', 'bob_bus');
   ```

   Then: `psql "$OWNER_URL" -f grants.sql`

3. **Give each member a config file with its own login.** Never hand a member the owner URL.

   ```bash
   echo '{"member": "alice", "db": {"transport": "pg-dsn", "dsn": "postgresql://alice_bus:alice-secret@HOST/DB"}}' > alice.json
   echo '{"member": "bob",   "db": {"transport": "pg-dsn", "dsn": "postgresql://bob_bus:bob-secret@HOST/DB"}}'   > bob.json
   ```

   Keep these files private. For shared machines, use `pg-direct` with a `password_file` (options at the top of `teambus.py`). If the config's `member` does not match the login, every command is refused.

4. **Talk:**

   ```bash
   python teambus.py --config alice.json send bob "hello from alice"
   python teambus.py --config bob.json inbox
   python teambus.py --config bob.json claim               # prints the message and a claim token
   python teambus.py --config bob.json done 1 --token <token>
   python teambus.py --config alice.json fact deploy-window "Fridays 14:00-16:00 UTC"
   python teambus.py --config bob.json facts
   python teambus.py --config bob.json --json inbox        # JSON lines, for adapters
   ```

   Long work: `renew <seq> --token T --minutes 60` before the lease runs out. Giving up: `fail <seq> --token T <reason>` re-queues it; after `max_claim_attempts` (default 5) it parks as `failed` and the dead-man switch reports it. `release <seq>` moves a held or failed message back to the queue.

5. **Wake the addressed agent (dispatcher).** Without it, members learn about mail when they next run `inbox`. The dispatcher polls the bus every couple of seconds and taps only the member a message is addressed to (a facts-board post taps everyone but its poster):

   ```bash
   # a service member for the dispatcher: it may read all mail, and `who` shows it is alive
   psql "$OWNER_URL" -c "CREATE ROLE dispatcher_bus LOGIN PASSWORD 'd-secret' IN ROLE teambus_agent"
   psql "$OWNER_URL" -c "INSERT INTO team_members (name, kind, location, role, db_role) VALUES ('dispatcher','service','server','service','dispatcher_bus')"
   echo '{"member": "dispatcher", "db": {"transport": "pg-dsn", "dsn": "postgresql://dispatcher_bus:d-secret@HOST/DB"}}' > teambus.config.json
   cp dispatcher.example.json dispatcher.json   # edit: how each member is woken
   python dispatcher.py                         # foreground; Ctrl+C to stop
   ```

   Every member gets one JSON line per event in `spool/<member>.jsonl`, which any agent runtime can tail. Per member, `dispatcher.json` can add a `command` (run once per cycle with that member's due events as JSON lines on stdin; exit 0 means delivered) or a `notice_file` (an unread counter in `spool/<member>-notice.json`). Wakes are recorded in `team_wake_deliveries`: a failed command is retried with backoff (10 s doubling to 1 h, up to `max_wake_attempts`), a message that commits out of sequence order is still found, and `dry_adapters` writes nothing. Each event has a stable `event_id`; ignore repeats. A tap is only a hint: the message stays in the bus until the member claims it. The heartbeat runs every `heartbeat_interval_s` (default 300) whatever the idle backoff, and a remote send is noticed within `max_idle_s` (default 600). Each dispatch is logged with posted, detected and dispatched times in `dispatcher-log.jsonl`.

   To run it as a service on Linux (systemd), from the checkout: `./install-dispatcher.sh`. Re-run it after an upgrade.

6. **Optional: dead-man's switch.** `python teambus_deadman.py --config service.json` checks that heartbeats are fresh, no mail sits undone for hours, nothing is parked as `failed`, and no wake has failed or waited too long, and pages your phone through [ntfy](https://ntfy.sh). An alert that cannot be sent is retried on the next run. Run it from cron every 15 minutes with a `service` member's config.

## Upgrading from v0.1

```bash
pg_dump "$OWNER_URL" > before-v0.2.sql          # or a provider branch/snapshot
python tools/migrate.py --config owner.json      # prints every statement
python tools/migrate.py --config owner.json --apply
```

It runs in one transaction: archive tables, claim tokens, one current fact per key (existing duplicate heads are reconciled to the newest and reported), the lifetime idempotency registry backfilled from hot and archived rows, the task table, wake deliveries, the database API, and (if `teambus_agent` exists) the new grants, which remove direct INSERT/UPDATE. Existing installs start in `identity_mode = 'trusted'`, which believes the config's member name exactly as v0.1 did. Then:

1. Upgrade every client to this `teambus.py`. v0.1 clients write tables directly and are refused once the new grants apply, so upgrade clients before or together with the migration.
2. Create one login per member and set `team_members.db_role`; give owners `role = 'owner'` and the dispatcher and dead-man switch `role = 'service'`.
3. Switch on enforcement: `UPDATE team_settings SET identity_mode = 'enforced';`

Claims taken by v0.1 clients get a token they cannot present; those messages are re-claimed after the lease expires.

## Compaction: keep everything, keep it fast

Nothing on the bus is ever deleted. Over time, finished mail and replaced facts pile up, so `compact` moves them into archive tables in one all-or-nothing transaction and `history` reads both:

```bash
python tools/migrate.py --apply              # existing installs, once, as the database owner
python teambus.py --config owner.json compact --older-than 90d --dry-run
python teambus.py --config owner.json compact --older-than 90d --export archive-2026-11.jsonl.gz
python teambus.py --config alice.json history deploy-window   # a fact key, hot + archived versions
python teambus.py --config alice.json history bob --limit 50  # a member's mail, hot + archived
```

What moves: messages in state `done` finished before the cutoff, and facts that were superseded (when both the old row and its replacement are older than the cutoff) or retired. Queued, claimed and held mail, and every current fact, stay put, so inboxes and the board read exactly the same afterwards. A second run moves nothing. `--export` also writes the moved rows to a gzip JSONL file.

`compact` needs DELETE on the hot tables, which `grants.sql` deliberately withholds from `teambus_agent`, so run it with an owner config. Agents get read-only access to the archive tables for `history`, limited by row security to their own mail. Duplicate protection survives compaction: idempotency keys live in `team_idempotency`, which is never compacted. If an export cannot be moved into place after the archive commits, the command says so and keeps the full export at `<file>.partial`. Compaction also prunes delivered wake bookkeeping older than the cutoff.

## Files

| File | What it is |
|---|---|
| `teambus.py` | Member CLI: whoami, who, send, inbox, claim, renew, done, fail, release, heartbeat, on/off, fact, facts, retire, task, compact, history |
| `dispatcher.py`, `dispatcher.example.json` | Wake layer: taps the addressed member when mail or a fact lands |
| `install-dispatcher.sh`, `teambus-dispatcher.service` | Installs the dispatcher as a systemd service |
| `teambus_deadman.py` | Dead-man's switch: pages your phone when the bus stalls |
| `tools/migrate.py`, `migrations/` | Upgrades an existing install in one transaction |
| `schema.sql` | Tables, constraints and indexes |
| `api.sql` | Write functions and row security: identity, authority and the message state machine |
| `grants.sql` | Least-privilege runtime grants: read the tables, write only through `api.sql` |
| `config.example.json` | Safe config shape with placeholders |
| `PROTOCOL.md` | Operating rules and the adapter contract |

## Status

Early and running in production for one person's fleet of agents since October 2026. Expect rough edges; issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
