"""Shared helpers for tests that need a real, throwaway PostgreSQL.

Set one of:
  TEAMBUS_TEST_DSN  libpq DSN of a server where the user may CREATE DATABASE
                    and CREATE ROLE (CI uses a postgres service container)
  TEAMBUS_PG_BIN    folder holding initdb and pg_ctl; a temp cluster is started
Never touches a configured bus: every test gets its own database and its own
login roles, dropped afterwards.
"""
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None

_server = {}


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def start():
    if psycopg2 is None:
        raise unittest.SkipTest("psycopg2 not installed")
    if "admin" in _server:
        return
    dsn = os.environ.get("TEAMBUS_TEST_DSN")
    if dsn:
        _server["admin"] = dsn
        return
    pg_bin = os.environ.get("TEAMBUS_PG_BIN")
    if not pg_bin:
        raise unittest.SkipTest("set TEAMBUS_TEST_DSN or TEAMBUS_PG_BIN to run the database tests")
    exe = lambda n: os.path.join(pg_bin, n + (".exe" if os.name == "nt" else ""))  # noqa: E731
    d = tempfile.mkdtemp(prefix="tbpg-")
    data, port = os.path.join(d, "data"), _free_port()
    subprocess.run([exe("initdb"), "-D", data, "-U", "postgres", "-A", "trust", "-E", "UTF8"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([exe("pg_ctl"), "-D", data, "-o", "-p %d -c listen_addresses=127.0.0.1" % port,
                    "-l", os.path.join(d, "log.txt"), "-w", "start"], check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _server.update(admin="host=127.0.0.1 port=%d user=postgres dbname=postgres" % port,
                   stop=[exe("pg_ctl"), "-D", data, "-m", "fast", "-w", "stop"], dir=d)


def stop():
    if "stop" in _server:
        subprocess.run(_server["stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(_server["dir"], ignore_errors=True)
        _server.clear()


def _replace(dsn, **kv):
    parts = [p for p in dsn.split() if p.split("=", 1)[0] not in kv]
    return " ".join(parts + ["%s=%s" % (k, v) for k, v in kv.items()])


def admin():
    conn = psycopg2.connect(_server["admin"])
    conn.autocommit = True
    return conn


class Bus:
    """A fresh v0.2 database with one login role per member.

    members: {name: role}; role is 'member', 'owner' or 'service'.
    legacy_sql: build this v0.1 schema instead and do not install v0.2.
    """

    def __init__(self, members, legacy_sql=None):
        self.tag = "t" + os.urandom(4).hex()
        self.name = self.tag
        self.roles = []
        self.tmp = tempfile.mkdtemp(prefix="tbcfg-")
        a = admin()
        cur = a.cursor()
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'teambus_agent'")
        if not cur.fetchone():
            try:
                cur.execute("CREATE ROLE teambus_agent NOLOGIN")
            except psycopg2.errors.DuplicateObject:
                pass
        cur.execute("CREATE DATABASE " + self.name)
        a.close()
        self.dsn = _replace(_server["admin"], dbname=self.name)
        self.conn = psycopg2.connect(self.dsn)
        self.conn.autocommit = True
        cur = self.conn.cursor()
        if legacy_sql:
            cur.execute(legacy_sql)
        else:
            cur.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))
            cur.execute((ROOT / "api.sql").read_text(encoding="utf-8"))
            cur.execute((ROOT / "grants.sql").read_text(encoding="utf-8"))
        self.dsns = {}
        for m, role in members.items():
            if legacy_sql:
                cur.execute("INSERT INTO team_members (name, kind, location) VALUES (%s,'agent','test')", (m,))
                continue
            self.add_login(m, role)

    def add_login(self, member, role="member", map_it=True):
        login = "%s_%s" % (self.tag, member.replace(".", "_").replace("-", "_"))
        pw = os.urandom(8).hex()
        cur = self.conn.cursor()
        cur.execute("CREATE ROLE %s LOGIN PASSWORD %%s IN ROLE teambus_agent" % login, (pw,))
        self.roles.append(login)
        if map_it:
            cur.execute("INSERT INTO team_members (name, kind, location, role, db_role) "
                        "VALUES (%s, 'agent', 'test', %s, %s)", (member, role, login))
        dsn = _replace(self.dsn, user=login, password=pw)
        self.dsns[member] = dsn
        return dsn

    def config(self, member, as_member=None, dsn=None):
        """Config file for `member`'s login; as_member overrides the claimed name."""
        path = os.path.join(self.tmp, "%s-as-%s.json" % (member, as_member or member))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"member": as_member or member,
                       "db": {"transport": "pg-dsn", "dsn": dsn or self.dsns.get(member, self.dsn)}}, f)
        return path

    def admin_config(self, member):
        path = os.path.join(self.tmp, "admin-as-%s.json" % member)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"member": member, "db": {"transport": "pg-dsn", "dsn": self.dsn}}, f)
        return path

    def connect_as(self, member):
        conn = psycopg2.connect(self.dsns[member])
        conn.autocommit = True
        return conn

    def sql(self, q, params=None):
        cur = self.conn.cursor()
        cur.execute(q, params)
        try:
            return cur.fetchall()
        except psycopg2.ProgrammingError:
            return None
        finally:
            cur.close()

    def close(self):
        self.conn.close()
        a = admin()
        cur = a.cursor()
        cur.execute("DROP DATABASE IF EXISTS " + self.name + " WITH (FORCE)")
        for r in self.roles:
            cur.execute("DROP ROLE IF EXISTS " + r)
        a.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
