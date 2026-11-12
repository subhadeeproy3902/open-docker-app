"""Database explorer backends: browse and query databases that run in containers.

How Berth reaches them (no drivers, no new ports):
  * SQL / Redis / MongoDB: the client that ships inside the image (psql, mariadb/mysql, redis-cli, mongosh),
    run with `docker exec`. Passwords travel as environment variables of that one exec, never in argv.
  * Search engines (Vespa, OpenSearch / Elasticsearch, Typesense, Meilisearch): HTTP, through the published port,
    else the container's IP (local Linux engines), else curl / wget inside the container.
  * SQLite: the file is copied out of the container or volume and opened read-only with Python's sqlite3.

Read-only is the default and is enforced as low as possible: Postgres runs every statement in a read-only transaction,
MySQL sets SESSION TRANSACTION READ ONLY and only lets read statements through, Redis / MongoDB / HTTP engines only
send read commands. Writing needs `allow_write=True`, which the UI only passes after the user switched it on.

Every public method blocks: call it through util.bg, never on the UI thread."""
import csv
import http.client
import io
import json
import os
import re
import shlex
import socket
import sqlite3
import ssl
import tarfile
import tempfile
import time
import urllib.parse

ROW_LIMIT = 200            # rows per page when browsing
QUERY_ROWS = 2000          # most rows shown for a query
OUT_LIMIT = 16 * 1024 * 1024
CELL_MAX = 4000            # characters of one value shown in the grid (the rest is summarised)
csv.field_size_limit(OUT_LIMIT * 2)         # psql may send one huge field; the default limit is 128 KB


def cap(v):
    """One cell, safe to show: long text cut with its real length noted, binary summarised."""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"‹binary · {_size(len(bytes(v)))}›"
    v = v if isinstance(v, str) else str(v)
    if len(v) > CELL_MAX + 64:                       # margin: a value the server already cut keeps its note
        return v[:CELL_MAX] + f"… [{len(v):,} characters]"
    return v


def _csv_rows(text):
    """Parse psql --csv output; if the output was cut mid-record, keep every complete record before it."""
    rows, reader = [], csv.reader(io.StringIO(text))
    while True:
        try:
            rows.append(next(reader))
        except StopIteration:
            return rows, False
        except csv.Error:
            return rows, True


def _shrinking(fn, node, offset, limit):
    """Fetch a page; if the reply is too large (huge documents), try again with fewer rows and say so."""
    last = None
    for n in [limit] + [x for x in (50, 10, 1) if x < limit]:
        try:
            r = fn(node, offset, n)
            r.page = n
            if n < limit:
                r.note = (r.note + " · " if r.note else "") + f"{n} per page — these documents are large"
            return r
        except DbError as e:
            if "too large" not in str(e).lower():
                raise
            last = e
    raise last
NULL = "\u2400"            # how psql marks NULL (an empty string stays an empty string)


class DbError(Exception):
    """An error to show the user. Messages are capped: a failing command must never dump megabytes into the UI."""

    def __init__(self, msg=""):
        msg = str(msg)
        if len(msg) > 1200:
            msg = msg[:1200] + f"… [{len(msg):,} characters]"
        super().__init__(msg)


class Result:
    """A grid of text. rows: list of lists (None = NULL). text: a document to show instead (schema files, JSON)."""

    def __init__(self, columns=(), rows=(), note="", truncated=False, text=None):
        self.columns, self.rows, self.note, self.truncated, self.text = list(columns), list(rows), note, truncated, text
        self.ms = 0

    def __repr__(self):
        return f"Result({len(self.columns)} cols, {len(self.rows)} rows{', text' if self.text else ''})"


class Node:
    """An item in the tree on the left: a schema, table, key, collection, index…"""

    def __init__(self, label, kind, ref=None, detail="", leaf=True, icon=None):
        self.label, self.kind, self.ref, self.detail, self.leaf, self.icon = label, kind, ref, detail, leaf, icon

    def __repr__(self):
        return f"Node({self.kind}:{self.label})"


# ── detection ───────────────────────────────────────────────────────────────────────────────────────────────────
def _env(ins):
    out = {}
    for kv in ((ins.get("Config") or {}).get("Env") or []):
        k, _, v = kv.partition("=")
        out[k] = v
    return out


def _image(c):
    """Image name without registry / tag, lower case: 'postgres', 'bitnami/redis', 'vespaengine/vespa'."""
    img = (c.get("Image") or (c.get("Config") or {}).get("Image") or "").lower()
    img = img.split("@")[0]
    if ":" in img.rsplit("/", 1)[-1]:
        img = img.rsplit(":", 1)[0]
    for reg in ("docker.io/library/", "docker.io/", "docker.elastic.co/elasticsearch/", "ghcr.io/", "quay.io/",
                "public.ecr.aws/", "mirror.gcr.io/"):
        if img.startswith(reg):
            img = img[len(reg):]
    return img


# (image fragment, kind) — checked in order, first match wins
IMAGES = [("postgis", "postgres"), ("timescale", "postgres"), ("pgvector", "postgres"), ("postgres", "postgres"),
          ("supabase/postgres", "postgres"), ("mariadb", "mysql"), ("mysql", "mysql"), ("percona", "mysql"),
          ("redis", "redis"), ("valkey", "redis"), ("keydb", "redis"), ("dragonfly", "redis"),
          ("mongo", "mongo"), ("vespa", "vespa"), ("opensearch", "opensearch"), ("elasticsearch", "opensearch"),
          ("typesense", "typesense"), ("meilisearch", "meilisearch")]
# images that contain one of the words above but are not databases
NOT_DB = ("exporter", "dashboards", "kibana", "adminer", "pgadmin", "redisinsight", "redis-commander",
          "mongo-express", "phpmyadmin", "pgbouncer", "-ui", "/ui", "operator", "backup", "-cli")
ENV_HINTS = [("POSTGRES_PASSWORD", "postgres"), ("POSTGRESQL_PASSWORD", "postgres"), ("MYSQL_ROOT_PASSWORD", "mysql"),
             ("MARIADB_ROOT_PASSWORD", "mysql"), ("MONGO_INITDB_ROOT_USERNAME", "mongo"), ("TYPESENSE_API_KEY", "typesense"),
             ("MEILI_MASTER_KEY", "meilisearch")]


def detect(c):
    """Database kind of a container summary (from /containers/json) or inspect, or None."""
    img = _image(c)
    if any(w in img for w in NOT_DB):
        return None
    for frag, kind in IMAGES:
        if frag in img:
            return kind
    env = c.get("Env") or {}
    for key, kind in ENV_HINTS:
        if key in env:
            return kind
    return None


LABELS = {"postgres": "PostgreSQL", "mysql": "MySQL / MariaDB", "redis": "Redis", "mongo": "MongoDB", "vespa": "Vespa",
          "opensearch": "OpenSearch / Elasticsearch", "typesense": "Typesense", "meilisearch": "Meilisearch",
          "sqlite": "SQLite"}


def open_backend(engine, ins):
    """The backend for an inspected container (raises DbError when it isn't a database we know)."""
    kind = detect({"Image": (ins.get("Config") or {}).get("Image"), "Env": _env(ins)})
    cls = BACKENDS.get(kind)
    if not cls:
        raise DbError("Berth doesn't recognise a database in this container")
    return cls(engine, ins)


# ── shared helpers ──────────────────────────────────────────────────────────────────────────────────────────────
READ_SQL = re.compile(r"^\s*(\(\s*)*(select|show|describe|desc|explain|with|values|table|use|pragma|help)\b", re.I)
DANGER_SQL = re.compile(r"\b(drop|truncate|alter|grant|revoke)\b|\b(delete|update)\b(?![\s\S]*\bwhere\b)", re.I)


def _split_sql(text):
    """Statements separated by ; (ignores ; inside quotes and comments)."""
    out, cur, q, i = [], [], None, 0
    while i < len(text):
        ch = text[i]
        if q:
            cur.append(ch)
            if ch == q:
                q = None
        elif ch in "'\"`":
            q = ch
            cur.append(ch)
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = len(text) if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i)
            i = len(text) if j < 0 else j + 2
            continue
        elif ch == ";":
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur).strip())
    return [s for s in out if s]


def _flatten(doc, prefix=""):
    """One level of a JSON document as columns; nested values stay as compact JSON."""
    out = {}
    for k, v in (doc or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, (dict, list)):
            out[key] = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        else:
            out[key] = v if v is None else str(v)
    return out


def docs_result(docs, note="", truncated=False):
    """Documents (list of dicts) as a grid: the union of their top-level fields, in first-seen order."""
    flat = [_flatten(d) for d in docs]
    cols = []
    for f in flat:
        for k in f:
            if k not in cols:
                cols.append(k)
    r = Result(cols, [[cap(f.get(c)) for c in cols] for f in flat], note, truncated)
    r.docs = docs                     # the original documents: their ids keep their type (ObjectId, number…)
    return r


class Backend:
    kind = ""
    language = "sql"            # what the query box speaks: sql · redis · http · yql · mongo
    multi_db = False            # can switch between databases
    write_capable = True
    browse_hint = ""

    def __init__(self, engine, ins):
        self.engine, self.ins = engine, ins
        self.cid = ins["Id"]
        self.name = (ins.get("Name") or "").lstrip("/") or self.cid[:12]
        self.env = _env(ins)
        self.db = None
        self.version = ""
        self.where = ""             # SQL engines: a row filter (a condition), set from the data tab
        self.sort = None            # (column, descending) from a click on a column header

    # to override
    def connect(self):
        """Check we can talk to it; returns a short description (version)."""
        raise NotImplementedError

    def databases(self):
        return []

    def tree(self):
        return []

    def children(self, node):
        return []

    def browse(self, node, offset=0, limit=ROW_LIMIT):
        return Result()

    def structure(self, node):
        return Result()

    def query(self, text, allow_write=False):
        raise NotImplementedError

    def example(self):
        return ""

    def is_destructive(self, text):
        return bool(DANGER_SQL.search(text or ""))

    def richest(self, dbs):
        """The database that actually holds data (used when the default one is empty), or None."""
        return None

    def can_rename(self, node):
        return False

    def rename(self, node, new, allow_write=False):
        raise DbError("Renaming isn't supported here")

    def _need_write(self, allow_write):
        if not allow_write:
            raise DbError("This changes the database. Switch on “Allow changes” at the top first.")

    # helpers
    def running(self):
        return ((self.ins.get("State") or {}).get("Running")) is True

    def exec(self, cmd, env=None, timeout=60, limit=OUT_LIMIT):
        if not self.running():
            raise DbError(f"{self.name} is not running. Start it to browse its data.")
        try:
            code, out, err = self.engine.exec_run(self.cid, cmd, limit=limit, timeout=timeout, env=env)
        except Exception as e:      # engine errors, connection issues
            raise DbError(str(e))
        return code, out, err, len(out) + len(err) >= limit

    def secret(self, *names):
        """The first of these env variables that is set, or the content of its *_FILE twin (Docker secrets)."""
        for n in names:
            if self.env.get(n):
                return self.env[n]
            path = self.env.get(n + "_FILE")
            if path:
                code, out, _e, _t = self.exec(["cat", path], timeout=10, limit=64 * 1024)
                if code == 0:
                    return out.strip()
        return ""

    def close(self):
        pass

    def which(self, *names):
        """First of `names` that exists in the container (None if none)."""
        code, out, _e, _t = self.exec(["sh", "-c", 'for b in "$@"; do command -v "$b" && exit 0; done; exit 1', "sh",
                                       *names], timeout=10)
        return os.path.basename(out.strip().splitlines()[0]) if code == 0 and out.strip() else None


def _timed(fn):
    def wrap(self, *a, **k):
        t = time.monotonic()
        r = fn(self, *a, **k)
        if isinstance(r, Result):
            r.ms = int((time.monotonic() - t) * 1000)
        return r
    return wrap


# ── PostgreSQL ──────────────────────────────────────────────────────────────────────────────────────────────────
def _condition(text):
    """A row filter typed by the user: one condition, never a second statement (it is checked, and Postgres runs
    it in a read-only transaction anyway)."""
    t = text.strip().rstrip(";").strip()
    if t.lower().startswith("where "):
        t = t[6:]
    if len(_split_sql(t)) > 1:
        raise DbError("A filter is one condition, e.g. status = 'paid' and total > 10 — not several statements")
    return f"({t})"


def _qi(name):
    return '"' + name.replace('"', '""') + '"'


def _ql(text):
    return "'" + text.replace("'", "''") + "'"


class Postgres(Backend):
    kind = "postgres"
    multi_db = True

    def __init__(self, engine, ins):
        super().__init__(engine, ins)
        e = self.env
        self.user = e.get("POSTGRES_USER") or e.get("POSTGRESQL_USERNAME") or e.get("POSTGRESQL_USER") or "postgres"
        self.password = None                   # read in connect(): it may live in a *_FILE secret
        self.db = e.get("POSTGRES_DB") or e.get("POSTGRESQL_DATABASE") or self.user

    def _psql(self, sql, readonly=True, db=None, file=None):
        env = {"PGPASSWORD": self.password or "", "PGCONNECT_TIMEOUT": "10",
               "PGOPTIONS": "-c statement_timeout=60000 -c standard_conforming_strings=on"
                            + (" -c default_transaction_read_only=on" if readonly else "")}
        cmd = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "--csv", "-P", f"null={NULL}", "-U", self.user,
               "-d", db or self.db] + (["-f", file] if file else ["-c", sql])
        code, out, err, trunc = self.exec(cmd, env=env, timeout=90)
        if code != 0 and not trunc:          # when we stop reading a huge reply, psql exits non-zero: not an error
            msg = err.strip() or out.strip() or "psql failed"
            if "read-only transaction" in msg:
                msg += "\n\nBerth is in read-only mode. Switch on “Allow changes” to modify data."
            raise DbError(msg.replace("psql: error: ", ""))
        return out, trunc

    def _rows(self, sql, readonly=True, db=None):
        out, trunc = self._psql(sql, readonly, db)
        rows, cut = _csv_rows(out)
        if (trunc or cut) and len(rows) > 1:      # the last record may be incomplete
            rows = rows[:-1]
        if not rows:
            return Result(note="Done.")
        cols, data = rows[0], [[None if v == NULL else cap(v) for v in r] for r in rows[1:]]
        note = "The reply was too large to show completely — showing the first rows." if trunc or cut else ""
        return Result(cols, data[:QUERY_ROWS], note=note, truncated=trunc or cut or len(data) > QUERY_ROWS)

    def connect(self):
        if self.password is None:
            names = ("POSTGRESQL_POSTGRES_PASSWORD",) if self.user == "postgres" else ()
            self.password = self.secret(*names, "POSTGRES_PASSWORD", "POSTGRESQL_PASSWORD")
        r = self._rows("select current_setting('server_version') as v, current_database() as d")
        self.version = r.rows[0][0] if r.rows else ""
        return f"PostgreSQL {self.version}"

    def databases(self):
        return [r[0] for r in self._rows("select datname from pg_database where not datistemplate "
                                         "and datallowconn order by 1").rows]

    def tree(self):
        r = self._rows("""select n.nspname, c.relname, c.relkind, c.reltuples::bigint,
                                 pg_size_pretty(pg_total_relation_size(c.oid))
                          from pg_class c join pg_namespace n on n.oid = c.relnamespace
                          where c.relkind in ('r','v','m','p','f')
                            and n.nspname not in ('pg_catalog','information_schema') and n.nspname not like 'pg_toast%'
                            and not c.relispartition
                          order by 1, 2""")
        schemas = {}
        for nsp, rel, kind, est, size in r.rows:
            label = {"v": "view", "m": "materialized view", "f": "foreign table"}.get(kind)
            # reltuples is -1 until the table is first analysed: then show only the size, not a wrong "0 rows"
            detail = label or (f"≈ {int(est):,} rows · {size}" if int(est) >= 0 else size)
            node = Node(rel, "table", (nsp, rel), detail,
                        icon="view-grid-symbolic" if kind in "rp" else "view-list-symbolic")
            node.relkind = kind
            schemas.setdefault(nsp, []).append(node)
        empty = self._rows("select nspname from pg_namespace where nspname not in ('pg_catalog','information_schema') "
                           "and nspname not like 'pg_toast%' and nspname not like 'pg_temp%'").rows
        for (nsp,) in empty:
            schemas.setdefault(nsp, [])
        self._schemas = schemas
        return [Node(s, "schema", s, f"{len(t)} tables", leaf=False, icon="folder-symbolic")
                for s, t in sorted(schemas.items(), key=lambda kv: (kv[0] != "public", kv[0]))]

    def children(self, node):
        return self._schemas.get(node.ref, [])

    @_timed
    def browse(self, node, offset=0, limit=ROW_LIMIT):
        """Long values are cut and binary summarised by Postgres itself, so a 59 MB blob column never travels."""
        s, t = node.ref
        cols = self._rows(f"select a.attname, format_type(a.atttypid, a.atttypmod), exists(select 1 from pg_index i "
                          f"where i.indrelid = a.attrelid and i.indisprimary and a.attnum = any(i.indkey))::text "
                          f"from pg_attribute a "
                          f"where a.attrelid = {_ql(_qi(s) + '.' + _qi(t))}::regclass and a.attnum > 0 "
                          f"and not a.attisdropped order by a.attnum").rows
        exprs = []
        # a stable order (the primary key, like a SQL client): pages don't shift and an edited row stays put
        # qualified with the table alias: a bare name would sort by the (text) display column, 1, 10, 11, 2…
        order = ", ".join("_t." + _qi(n) for n, _t, pk in cols if pk == "true")
        if self.sort and self.sort[0] in [c[0] for c in cols]:
            order = f"_t.{_qi(self.sort[0])} {'desc' if self.sort[1] else 'asc'} nulls last" + (", " + order if order else "")
        where = f" where {_condition(self.where)}" if self.where.strip() else ""
        for name, typ, _pk in cols:
            q = _qi(name)
            if typ == "bytea":
                exprs.append(f"case when {q} is null then null else '‹binary · ' || "
                             f"pg_size_pretty(octet_length({q})::bigint) || '›' end as {q}")
            else:
                exprs.append(f"case when length({q}::text) > {CELL_MAX} then left({q}::text, {CELL_MAX}) || '… [' || "
                             f"to_char(length({q}::text), 'FM999,999,999,999') || ' characters]' else {q}::text end as {q}")
        sel = ", ".join(exprs) or "*"
        return self._rows(f"select {sel} from {_qi(s)}.{_qi(t)} as _t{where}{' order by ' + order if order else ''} "
                          f"limit {int(limit)} offset {int(offset)}")

    @_timed
    def structure(self, node):
        s, t = node.ref
        cols = self._rows(f"""select a.attname as column, format_type(a.atttypid, a.atttypmod) as type,
                                 case when a.attnotnull then 'no' else 'yes' end as nullable,
                                 pg_get_expr(d.adbin, d.adrelid) as default,
                                 (select string_agg(con.conname || ' (' || case con.contype when 'p' then 'primary key'
                                         when 'f' then 'foreign key' when 'u' then 'unique' when 'c' then 'check'
                                         when 'x' then 'exclusion' else con.contype::text end || ')', ', ')
                                    from pg_constraint con where con.conrelid = a.attrelid
                                     and a.attnum = any(con.conkey)) as constraints
                          from pg_attribute a left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
                          where a.attrelid = {_ql(_qi(s) + '.' + _qi(t))}::regclass and a.attnum > 0
                            and not a.attisdropped order by a.attnum""")
        idx = self._rows(f"select indexname, indexdef from pg_indexes where schemaname = {_ql(s)} "
                         f"and tablename = {_ql(t)} order by 1")
        cols.note = "Indexes: " + ("; ".join(f"{n}: {d.split(' USING ', 1)[-1]}" for n, d in idx.rows) or "none")
        return cols

    @_timed
    def query(self, text, allow_write=False):
        if not text.strip():
            raise DbError("Type a query first")
        return self._rows(text, readonly=not allow_write)

    def example(self):
        return "select * from pg_stat_activity limit 20;"

    def richest(self, dbs):
        best, most = None, 0
        for d in dbs:
            try:
                n = int(self._rows("select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                                   "where c.relkind in ('r','p','v','m') and n.nspname not in "
                                   "('pg_catalog','information_schema') and n.nspname not like 'pg_toast%'",
                                   db=d).rows[0][0])
            except DbError:
                continue
            if n > most:
                best, most = d, n
        return best

    def can_rename(self, node):
        return node.kind in ("table", "schema", "database")

    def rename(self, node, new, allow_write=False):
        self._need_write(allow_write)
        if node.kind == "schema":
            self._rows(f"alter schema {_qi(node.ref)} rename to {_qi(new)}", readonly=False)
        elif node.kind == "database":         # can't rename the database you are connected to: connect elsewhere
            other = "postgres" if node.ref != "postgres" else "template1"
            try:
                self._rows(f"alter database {_qi(node.ref)} rename to {_qi(new)}", readonly=False, db=other)
            except DbError as e:
                if "being accessed by other users" in str(e):
                    raise DbError(f"“{node.ref}” is in use by running services. Stop the containers that use it, "
                                  "then rename it.")
                raise
            if self.db == node.ref:
                self.db = new
        else:
            s, t = node.ref
            what = {"v": "view", "m": "materialized view", "f": "foreign table"}.get(getattr(node, "relkind", "r"),
                                                                                       "table")
            self._rows(f"alter {what} {_qi(s)}.{_qi(t)} rename to {_qi(new)}", readonly=False)


# ── MySQL / MariaDB ─────────────────────────────────────────────────────────────────────────────────────────────
def _bq(name):
    return "`" + name.replace("`", "``") + "`"


_UNESC = {"n": "\n", "t": "\t", "0": "\0", "\\": "\\", "r": "\r", "b": "\b", "Z": "\x1a"}


def _unescape_batch(v):
    if v == "NULL":
        return None
    if "\\" not in v:
        return v
    return re.sub(r"\\(.)", lambda m: _UNESC.get(m.group(1), m.group(1)), v)


class MySQL(Backend):
    kind = "mysql"
    multi_db = True

    def __init__(self, engine, ins):
        super().__init__(engine, ins)
        e = self.env
        self.user, self.password = "root", None     # resolved in connect()
        self.db = e.get("MYSQL_DATABASE") or e.get("MARIADB_DATABASE") or ""
        self.client = None

    def _credentials(self):
        e = self.env
        root = self.secret("MYSQL_ROOT_PASSWORD", "MARIADB_ROOT_PASSWORD")
        empty_ok = any(e.get(k, "").lower() in ("yes", "1", "true") for k in
                       ("MYSQL_ALLOW_EMPTY_PASSWORD", "MARIADB_ALLOW_EMPTY_ROOT_PASSWORD", "ALLOW_EMPTY_PASSWORD"))
        if root or empty_ok:
            self.user, self.password = "root", root
        else:       # random root password: use the application user
            self.user = e.get("MYSQL_USER") or e.get("MARIADB_USER") or "root"
            self.password = self.secret("MYSQL_PASSWORD", "MARIADB_PASSWORD")

    def _run(self, sql, db=None, raw=False, file=None):
        """raw: values exactly as stored (no cutting) — for opening one row to edit it.
        file: run the statements in this file inside the container (fed on stdin, so the first error stops it and
        is reported — `source` would carry on and exit 0)."""
        if self.client is None:
            self.client = self.which("mariadb", "mysql")
            if not self.client:
                raise DbError("This image has no mysql / mariadb client")
        cmd = [self.client, "--batch", "--column-names", "--connect-timeout=10", "-u", self.user]
        if db or self.db:
            cmd.append(f"--database={db or self.db}")
        env = {"MYSQL_PWD": self.password or ""}
        if file:
            cmd, env["BERTH_F"] = ["sh", "-c", 'exec "$0" "$@" < "$BERTH_F"'] + cmd, file
        else:
            cmd = cmd + ["-e", sql]
        code, out, err, trunc = self.exec(cmd, env=env, timeout=90)
        if code != 0 and not trunc:
            msg = err.strip() or "query failed"
            if msg.startswith("-----"):             # the client echoes the statement before the error: drop it
                msg = "\n".join(ln for ln in msg.splitlines() if ln.startswith("ERROR")) or msg[-600:]
            if "READ ONLY" in msg.upper():
                msg += "\n\nBerth is in read-only mode. Switch on “Allow changes” to modify data."
            raise DbError(msg)
        if trunc:
            out = out[:out.rfind("\n") + 1]
        lines = out.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if not lines:
            return Result(note="Done.")
        cols = lines[0].split("\t")
        conv = _unescape_batch if raw else (lambda v: cap(_unescape_batch(v)))
        rows = [[conv(v) for v in ln.split("\t")] for ln in lines[1:]]
        note = "The reply was too large to show completely — showing the first rows." if trunc else ""
        return Result(cols, rows[:QUERY_ROWS], note=note, truncated=trunc or len(rows) > QUERY_ROWS)

    def connect(self):
        if self.password is None:
            self._credentials()
        r = self._run("select version() as v")
        self.version = r.rows[0][0] if r.rows else ""
        if not self.db:
            dbs = self.databases()
            self.db = dbs[0] if dbs else ""
        return ("MariaDB " if "mariadb" in self.version.lower() else "MySQL ") + self.version.split("-")[0]

    def databases(self):
        hidden = {"information_schema", "mysql", "performance_schema", "sys"}
        return [r[0] for r in self._run("show databases", db="").rows if r[0] not in hidden]

    def tree(self):
        if not self.db:
            return []
        r = self._run(f"select table_name, table_type, table_rows, round((data_length + index_length) / 1024) "
                      f"from information_schema.tables where table_schema = '{self.db}' order by 1")
        return [Node(n, "table", n, "view" if "VIEW" in (t or "") else f"≈ {int(rows or 0):,} rows · {kb or 0} KB",
                     icon="view-grid-symbolic") for n, t, rows, kb in r.rows]

    @_timed
    def browse(self, node, offset=0, limit=ROW_LIMIT):
        cols = self._run(f"select column_name, data_type, column_key from information_schema.columns where "
                         f"table_schema = database() and table_name = '{node.ref.replace(chr(39), chr(39) * 2)}' "
                         f"order by ordinal_position").rows
        exprs = []
        order = ", ".join(f"{_bq(node.ref)}.{_bq(n)}" for n, _t, k in cols if k == "PRI")
        if self.sort and self.sort[0] in [c[0] for c in cols]:
            order = f"{_bq(node.ref)}.{_bq(self.sort[0])} {'DESC' if self.sort[1] else 'ASC'}" + (", " + order if order else "")
        where = f" where {_condition(self.where)}" if self.where.strip() else ""
        for name, typ, _k in cols:
            q = _bq(name)
            if (typ or "").lower() in ("blob", "tinyblob", "mediumblob", "longblob", "binary", "varbinary", "bit"):
                exprs.append(f"IF({q} IS NULL, NULL, CONCAT('‹binary · ', OCTET_LENGTH({q}), ' bytes›')) AS {q}")
            else:
                exprs.append(f"IF(CHAR_LENGTH({q}) > {CELL_MAX}, CONCAT(LEFT({q}, {CELL_MAX}), '… [', "
                             f"CHAR_LENGTH({q}), ' characters]'), {q}) AS {q}")
        sel = ", ".join(exprs) or "*"
        # a filter is the user's own SQL: run it in a read-only transaction like the query box
        return self._run(("SET SESSION TRANSACTION READ ONLY; " if where else "")
                         + f"select {sel} from {_bq(node.ref)}{where}{' order by ' + order if order else ''} "
                         f"limit {int(offset)}, {int(limit)}")

    @_timed
    def structure(self, node):
        r = self._run(f"show full columns from {_bq(node.ref)}")
        keep = [i for i, c in enumerate(r.columns) if c in ("Field", "Type", "Null", "Key", "Default", "Extra", "Comment")]
        r.columns = [r.columns[i] for i in keep]
        r.rows = [[row[i] for i in keep] for row in r.rows]
        idx = self._run(f"show index from {_bq(node.ref)}")
        names = {}
        for row in idx.rows:
            d = dict(zip(idx.columns, row))
            names.setdefault(d.get("Key_name"), []).append(d.get("Column_name"))
        r.note = "Indexes: " + ("; ".join(f"{k}({', '.join(v)})" for k, v in names.items()) or "none")
        return r

    @_timed
    def query(self, text, allow_write=False):
        stmts = _split_sql(text)
        if not stmts:
            raise DbError("Type a query first")
        if not allow_write:
            bad = [s for s in stmts if not READ_SQL.match(s)]
            if bad:
                raise DbError(f"“{bad[0][:60]}” changes data. Berth is in read-only mode: switch on “Allow changes” "
                              "to run it.")
            text = "SET SESSION TRANSACTION READ ONLY; " + text
        return self._run(text)

    def example(self):
        return "show processlist;"

    def richest(self, dbs):
        if not dbs:
            return None
        names = ",".join("'" + d.replace("'", "''") + "'" for d in dbs)
        r = self._run(f"select table_schema, count(*) n from information_schema.tables where table_schema in ({names}) "
                      "group by 1 order by 2 desc limit 1", db="")
        return r.rows[0][0] if r.rows else None

    def can_rename(self, node):
        return node.kind == "table"

    def rename(self, node, new, allow_write=False):
        self._need_write(allow_write)
        self._run(f"rename table {_bq(node.ref)} to {_bq(new)}")


# ── Redis / Valkey ──────────────────────────────────────────────────────────────────────────────────────────────
REDIS_READ = {"get", "mget", "strlen", "getrange", "exists", "type", "ttl", "pttl", "keys", "scan", "dbsize", "info",
              "hget", "hgetall", "hkeys", "hvals", "hlen", "hmget", "hscan", "hexists", "lrange", "llen", "lindex",
              "smembers", "scard", "sismember", "srandmember", "sscan", "zrange", "zrevrange", "zrangebyscore",
              "zcard", "zscore", "zscan", "zcount", "zrank", "xrange", "xrevrange", "xlen", "xinfo", "memory",
              "object", "ping", "time", "config", "client", "slowlog", "lastsave", "role", "json.get", "json.type",
              "ft._list", "ft.info", "ft.search", "ft.aggregate", "pfcount", "getbit", "bitcount", "geopos", "geodist",
              "georadius_ro", "geosearch", "randomkey", "echo", "command", "latency", "module", "acl"}
REDIS_SAFE_SUB = {"config": ("get",), "client": ("list", "info", "getname"), "memory": ("usage", "stats", "doctor"),
                  "object": ("encoding", "freq", "idletime", "refcount"), "acl": ("whoami", "list", "users", "cat"),
                  "module": ("list",), "latency": ("latest", "history", "doctor"), "slowlog": ("get", "len")}
REDIS_DANGER = {"flushall", "flushdb", "del", "unlink", "shutdown", "config", "debug", "swapdb", "rename", "migrate"}


class Redis(Backend):
    kind = "redis"
    language = "redis"
    multi_db = True
    browse_hint = "Keys matching"

    def __init__(self, engine, ins):
        super().__init__(engine, ins)
        e = self.env
        self.password = e.get("REDIS_PASSWORD") or e.get("VALKEY_PASSWORD") or e.get("REDIS_AUTH") or ""
        cmd = (ins.get("Config") or {}).get("Cmd") or []
        args = shlex.split(" ".join(cmd)) if len(cmd) == 1 else list(cmd)
        for i, a in enumerate(args):
            if a in ("--requirepass", "--masterauth") and i + 1 < len(args) and not self.password:
                self.password = args[i + 1]
        self.db = "0"
        self.pattern = "*"
        self.cli = None
        self.json = False

    def _cli(self, *args, raw=False):
        if self.cli is None:
            self.cli = self.which("redis-cli", "valkey-cli", "keydb-cli")
            if not self.cli:
                raise DbError("This image has no redis-cli")
            code, out, _e, _t = self.exec([self.cli, "--json", "PING"], env=self._auth(), timeout=10)
            self.json = code == 0 and "PONG" in out
        mode = ["--raw"] if raw else (["--json"] if self.json else ["--no-raw"])
        code, out, err, trunc = self.exec([self.cli, "-n", self.db, *mode, *args], env=self._auth(), timeout=60)
        if code != 0 or out.startswith("(error)") or (out.startswith("ERR") and raw):
            raise DbError((err or out).strip().replace("(error) ", "") or "redis-cli failed")
        return out, trunc

    def _auth(self):
        return {"REDISCLI_AUTH": self.password} if self.password else None

    def _val(self, *args):
        out, _t = self._cli(*args)
        if self.json:
            try:
                return json.loads(out)
            except ValueError:
                return out.strip()
        return _parse_noraw(out)

    def connect(self):
        info = self._cli("INFO", "server", raw=True)[0]
        m = re.search(r"(redis|valkey)_version:(\S+)", info)
        self.version = m.group(2) if m else ""
        return f"{'Valkey' if m and m.group(1) == 'valkey' else 'Redis'} {self.version}"

    def databases(self):
        out = self._cli("INFO", "keyspace", raw=True)[0]
        dbs = re.findall(r"^db(\d+):keys=(\d+)", out, re.M)
        names = [d for d, _n in dbs]
        if "0" not in names:
            names.insert(0, "0")
        self._counts = dict(dbs)
        return names

    def tree(self):
        # first 1000 keys matching the pattern (SCAN, so a big keyspace never blocks Redis), with type and TTL
        sh = ('"$0" -n "$1" --scan --pattern "$2" --count 1000 | head -n 1000')
        code, out, err, _t = self.exec(["sh", "-c", sh, self.cli or self.which("redis-cli", "valkey-cli"), self.db,
                                        self.pattern], env=self._auth(), timeout=60)
        if code != 0:
            raise DbError(err.strip() or "SCAN failed")
        keys = sorted(k for k in out.split("\n") if k)
        types = {}
        if keys:
            script = ("local r = {} for i, k in ipairs(KEYS) do r[#r + 1] = redis.call('TYPE', k)['ok'] "
                      "r[#r + 1] = tostring(redis.call('TTL', k)) end return r")
            try:
                out2, _t = self._cli("EVAL", script, str(len(keys)), *keys, raw=True)
                vals = out2.split("\n")
                types = {k: (vals[2 * i], vals[2 * i + 1]) for i, k in enumerate(keys) if 2 * i + 1 < len(vals)}
            except DbError:
                pass                # scripting disabled: types load when a key is opened
        nodes = []
        for k in keys:
            t, ttl = types.get(k, ("", "-1"))
            detail = t + ("" if ttl in ("-1", "") else f" · expires in {ttl} s")
            nodes.append(Node(k, "key", k, detail, icon="dialog-password-symbolic" if not t else _REDIS_ICON.get(t)))
        self._more = len(keys) >= 1000
        return nodes

    @_timed
    def browse(self, node, offset=0, limit=ROW_LIMIT):
        key = node.ref
        t = self._val("TYPE", key)
        t = t.get("ok", t) if isinstance(t, dict) else t
        a, b = int(offset), int(offset) + int(limit) - 1
        if t == "string":
            n = int(self._val("STRLEN", key) or 0)
            if n > 65536:                                   # don't pull a huge value across: show its start
                v = self._val("GETRANGE", key, "0", "65535")
                return Result(["value"], [[cap(v)]], note=f"string · {n:,} bytes (showing the start)")
            v = self._val("GET", key)
            return Result(["value"], [[v]], note=f"string · {len(v or ''):,} characters")
        if t == "hash":
            hl = int(self._val("HLEN", key) or 0)
            if hl > 2000:                                   # a huge hash: one HSCAN page instead of HGETALL
                out = self._val("HSCAN", key, "0", "COUNT", "1000")
                items = out[1] if isinstance(out, list) and len(out) == 2 else []
                if isinstance(items, dict):
                    items = [x for kv in items.items() for x in kv]
                pairs = list(zip(items[::2], items[1::2]))[:limit]
                return Result(["field", "value"], [[cap(a), cap(b)] for a, b in pairs],
                              note=f"hash · {hl:,} fields (showing a sample of {len(pairs)})", truncated=True)
            v = self._val("HGETALL", key)
            pairs = list(v.items()) if isinstance(v, dict) else list(zip(v[::2], v[1::2]))
            return Result(["field", "value"], [list(p) for p in pairs[offset:offset + limit]],
                          note=f"hash · {len(pairs):,} fields", truncated=len(pairs) > offset + limit)
        if t == "list":
            v = self._val("LRANGE", key, str(a), str(b))
            n = self._val("LLEN", key)
            return Result(["index", "value"], [[str(a + i), x] for i, x in enumerate(v or [])], note=f"list · {n} items",
                          truncated=int(n or 0) > b + 1)
        if t == "set":
            v = self._val("SRANDMEMBER", key, str(limit)) if offset == 0 else []
            n = self._val("SCARD", key)
            return Result(["member"], [[x] for x in v or []], note=f"set · {n} members (showing up to {limit})")
        if t == "zset":
            v = self._val("ZRANGE", key, str(a), str(b), "WITHSCORES")
            pairs = ([[m, str(s)] for m, s in v] if v and isinstance(v[0], list) else
                     [[m, s] for m, s in zip(v[::2], v[1::2])])
            n = self._val("ZCARD", key)
            return Result(["member", "score"], pairs, note=f"sorted set · {n} members", truncated=int(n or 0) > b + 1)
        if t == "stream":
            v = self._val("XRANGE", key, "-", "+", "COUNT", str(limit))
            rows = []
            for item in v or []:
                if isinstance(item, list) and len(item) == 2:
                    fields = item[1]
                    if isinstance(fields, list):
                        fields = dict(zip(fields[::2], fields[1::2]))
                    rows.append([item[0], json.dumps(fields, ensure_ascii=False)])
            return Result(["id", "fields"], rows, note=f"stream · {self._val('XLEN', key)} entries")
        if t in ("ReJSON-RL", "json"):
            v = self._val("JSON.GET", key)
            if isinstance(v, str) and len(v) > 1_000_000:
                return Result(text=v[:200_000] + f"\n… [{len(v):,} characters]", note="JSON document (start)")
            return Result(text=_pretty(v), note="JSON document")
        if t == "none":
            raise DbError("This key no longer exists")
        return Result(text=str(self._val("DUMP", key)), note=f"{t}: shown as DUMP")

    def structure(self, node):
        key = node.ref
        rows = [["type", str(self._val("TYPE", key))], ["ttl (s)", str(self._val("TTL", key))],
                ["encoding", str(self._val("OBJECT", "ENCODING", key))]]
        try:
            rows.append(["memory (bytes)", str(self._val("MEMORY", "USAGE", key))])
        except DbError:
            pass
        r = Result(["property", "value"], rows)
        return r

    @_timed
    def query(self, text, allow_write=False):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if not lines:
            raise DbError("Type a command, e.g. INFO memory")
        rows = []
        for ln in lines[:50]:
            try:
                args = shlex.split(ln)
            except ValueError as e:
                raise DbError(f"{ln}: {e}")
            cmd = args[0].lower()
            sub = args[1].lower() if len(args) > 1 else ""
            safe = cmd in REDIS_READ and (cmd not in REDIS_SAFE_SUB or sub in REDIS_SAFE_SUB[cmd])
            if not allow_write and not safe:
                raise DbError(f"{args[0].upper()} can change data. Berth is in read-only mode: switch on “Allow "
                              "changes” to run it.")
            out, _t = self._cli(*args, raw=True)
            rows.append([ln, out.rstrip("\n")])
        if len(rows) == 1:
            return Result(text=rows[0][1] or "(empty)", note=lines[0])
        return Result(["command", "reply"], rows)

    def is_destructive(self, text):
        return any((ln.split() or [""])[0].lower() in REDIS_DANGER for ln in text.splitlines())

    def example(self):
        return "INFO memory"

    def can_rename(self, node):
        return node.kind == "key"

    def rename(self, node, new, allow_write=False):
        self._need_write(allow_write)
        out, _t = self._cli("RENAMENX", node.ref, new, raw=True)
        if out.strip() == "0":
            raise DbError(f"A key called “{new}” already exists")


_REDIS_ICON = {"string": "text-x-generic-symbolic", "hash": "view-list-symbolic", "list": "view-list-ordered-symbolic",
               "set": "view-grid-symbolic", "zset": "view-sort-ascending-symbolic", "stream": "network-transmit-receive-symbolic"}


def _unrepr(s):
    """redis-cli --no-raw quoting ("a\\nb", \\xNN) back to text."""
    if not (len(s) >= 2 and s[0] == '"' and s[-1] == '"'):
        return s
    body, out, i = s[1:-1], [], 0
    esc = {"n": "\n", "r": "\r", "t": "\t", "a": "\a", "b": "\b", '"': '"', "\\": "\\"}
    raw = bytearray()
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nx = body[i + 1]
            if nx == "x" and i + 3 < len(body):
                raw += bytes([int(body[i + 2:i + 4], 16)])
                i += 4
                continue
            raw += esc.get(nx, nx).encode()
            i += 2
            continue
        raw += ch.encode()
        i += 1
    out.append(raw.decode(errors="replace"))
    return "".join(out)


def _parse_noraw(out):
    """Flat replies of redis-cli --no-raw (strings, integers, nil, flat arrays)."""
    lines = [ln for ln in out.rstrip("\n").split("\n")]
    if len(lines) == 1:
        ln = lines[0]
        if ln == "(nil)":
            return None
        if ln.startswith("(integer) "):
            return ln[10:]
        if ln == "(empty array)" or ln == "(empty list or set)":
            return []
        return _unrepr(ln)
    vals = []
    for ln in lines:
        m = re.match(r"^\s*\d+\) (.*)$", ln)
        if m:
            v = m.group(1)
            vals.append(None if v == "(nil)" else v[10:] if v.startswith("(integer) ") else _unrepr(v))
    return vals


def _pretty(v):
    if isinstance(v, str):
        try:
            return json.dumps(json.loads(v), indent=2, ensure_ascii=False)
        except ValueError:
            return v
    return json.dumps(v, indent=2, ensure_ascii=False)


# ── MongoDB ─────────────────────────────────────────────────────────────────────────────────────────────────────
MONGO_AUTH = ("if (process.env.BERTH_U) { db.getSiblingDB(process.env.BERTH_AUTHDB || 'admin')"
              ".auth(process.env.BERTH_U, process.env.BERTH_P) } ")


class Mongo(Backend):
    kind = "mongo"
    language = "mongo"
    multi_db = True
    browse_hint = "Filter (JSON)"

    def __init__(self, engine, ins):
        super().__init__(engine, ins)
        e = self.env
        self.user = e.get("MONGO_INITDB_ROOT_USERNAME") or e.get("MONGODB_ROOT_USER") or ""
        if not self.user and (e.get("MONGODB_ROOT_PASSWORD") or e.get("MONGODB_ROOT_PASSWORD_FILE")):
            self.user = "root"
        self.password = None
        self.db = e.get("MONGO_INITDB_DATABASE") or "test"
        self.filter = "{}"

    def _js(self, js, timeout=60, extra=None):
        if self.password is None:
            self.password = self.secret("MONGO_INITDB_ROOT_PASSWORD", "MONGODB_ROOT_PASSWORD")
        env = dict(extra or {})
        if self.user:
            env.update(BERTH_U=self.user, BERTH_P=self.password)
        cmd = ["mongosh", "mongodb://127.0.0.1:27017/" + self.db, "--quiet", "--norc", "--eval", MONGO_AUTH + js]
        code, out, err, trunc = self.exec(cmd, env=env or None, timeout=timeout)
        if trunc:                            # we stopped reading; mongosh then exits non-zero — not a real error
            raise DbError("The reply is too large to show at once")
        if code != 0:
            if "executable file not found" in (err + out) or code == 127:
                raise DbError("This image has no mongosh (MongoDB 6 or newer images include it)")
            raise DbError((err or out).strip().splitlines()[-1] if (err or out).strip() else "mongosh failed")
        if trunc:
            raise DbError("The reply is too large to show; narrow the filter or lower the limit")
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise DbError(out.strip()[-500:] or "Unexpected reply from mongosh")

    def connect(self):
        self.version = self._js("print(EJSON.stringify(db.version()))")
        return f"MongoDB {self.version}"

    def databases(self):
        r = self._js("print(EJSON.stringify(db.adminCommand({listDatabases: 1, nameOnly: true}).databases"
                     ".map(d => d.name)))")
        names = [n for n in r if n not in ("admin", "config", "local")]
        return names or [self.db]

    def tree(self):
        r = self._js("print(EJSON.stringify(db.getCollectionInfos({}, {nameOnly: true}).map(c => [c.name, c.type || "
                     "'collection', (c.type === 'view') ? -1 : db.getCollection(c.name).estimatedDocumentCount()])))")
        return [Node(n, "collection", n, "view" if t == "view" else f"≈ {int(c):,} documents",
                     icon="view-list-symbolic") for n, t, c in sorted(r)]

    def _filter(self):
        """The filter is data, never code: it travels in an env variable and is parsed with EJSON (so {"$oid": …}
        and {"$date": …} work), and operators that run server-side JavaScript are refused."""
        f = (self.filter or "{}").strip() or "{}"
        try:
            parsed = json.loads(f)
        except ValueError as e:
            raise DbError(f"The filter must be JSON (Extended JSON for ids and dates, e.g. "
                          f'{{"_id": {{"$oid": "…"}}}}): {e}')
        if not isinstance(parsed, dict):
            raise DbError("The filter must be a JSON object, e.g. {\"status\": \"active\"}")
        if re.search(r'"\$(where|function|accumulator)"', f):
            raise DbError("Filters that run JavaScript ($where, $function) are not allowed")
        return f

    @_timed
    def browse(self, node, offset=0, limit=ROW_LIMIT):
        docs = self._js(f"print(EJSON.stringify(db.getCollection({json.dumps(node.ref)})"
                        f".find(EJSON.parse(process.env.BERTH_F)).skip({int(offset)}).limit({int(limit)}).toArray(), "
                        "{relaxed: true}))", extra={"BERTH_F": self._filter()})
        return docs_result(docs, truncated=len(docs) >= limit)

    def structure(self, node):
        idx = self._js(f"print(EJSON.stringify(db.getCollection({json.dumps(node.ref)}).getIndexes()))")
        sample = self._js(f"print(EJSON.stringify(db.getCollection({json.dumps(node.ref)}).aggregate([{{$sample: "
                          "{size: 50}}]).toArray(), {relaxed: true}))")
        fields = {}
        for d in sample:
            for k, v in d.items():
                fields.setdefault(k, set()).add(type(v).__name__.replace("str", "string").replace("dict", "object")
                                                .replace("list", "array").replace("NoneType", "null"))
        r = Result(["field", "types seen (sample of 50)"], [[k, ", ".join(sorted(v))] for k, v in fields.items()])
        r.note = "Indexes: " + "; ".join(f"{i.get('name')}: {json.dumps(i.get('key'))}" for i in idx)
        return r

    @_timed
    def query(self, text, allow_write=False):
        if not text.strip():
            raise DbError("Type mongosh code, e.g. db.users.find({}).limit(5)")
        if not allow_write:
            raise DbError("The shell can change data. In read-only mode, browse collections with a filter on the "
                          "left; switch on “Allow changes” to use the shell.")
        out = self._js(f"let __r = (() => {{ return {text.strip().rstrip(';')} }})(); "
                       "if (__r && typeof __r.toArray === 'function') __r = __r.toArray(); "
                       "print(EJSON.stringify(__r === undefined ? null : __r, {relaxed: true}))")
        if isinstance(out, list) and out and all(isinstance(d, dict) for d in out):
            return docs_result(out[:QUERY_ROWS], truncated=len(out) > QUERY_ROWS)
        return Result(text=_pretty(out))

    def is_destructive(self, text):
        return bool(re.search(r"\b(drop|dropDatabase|deleteMany|remove|renameCollection)\s*\(", text or ""))

    def example(self):
        return "db.getCollectionNames()"

    def richest(self, dbs):
        r = self._js("print(EJSON.stringify(db.adminCommand({listDatabases: 1}).databases"
                     ".map(d => [d.name, d.sizeOnDisk])))")
        sizes = [(int(sz or 0), n) for n, sz in r if n in dbs]
        return max(sizes)[1] if sizes else None

    def can_rename(self, node):
        return node.kind == "collection"

    def rename(self, node, new, allow_write=False):
        self._need_write(allow_write)
        self._js(f"print(EJSON.stringify(db.getCollection({json.dumps(node.ref)}).renameCollection("
                 f"{json.dumps(new)})))")


# ── HTTP engines ────────────────────────────────────────────────────────────────────────────────────────────────
class Http:
    """Reach an HTTP port of a container: published port → container IP → curl/wget inside the container."""

    def __init__(self, backend, port, headers=None, scheme=None):
        self.b, self.port, self.headers, self.scheme = backend, int(port), dict(headers or {}), scheme
        self.base = None

    def _targets(self):
        ins, eng = self.b.ins, self.b.engine
        host = "127.0.0.1"
        if eng.path.startswith("tcp://"):
            host = eng.path[6:].rsplit(":", 1)[0] or "127.0.0.1"
        out = []
        ports = ((ins.get("NetworkSettings") or {}).get("Ports") or {}).get(f"{self.port}/tcp") or []
        for p in ports:
            ip = p.get("HostIp") or ""
            if p.get("HostPort"):
                out.append((host if ip in ("", "0.0.0.0", "::", "127.0.0.1") else ip, int(p["HostPort"])))
                break
        if not eng.path.startswith("tcp://") and os.uname().sysname == "Linux" and "rootless" not in eng.path \
                and not eng.path.startswith(f"/run/user/"):
            for net in ((ins.get("NetworkSettings") or {}).get("Networks") or {}).values():
                if net.get("IPAddress"):
                    out.append((net["IPAddress"], self.port))
                    break
        return out

    def request(self, method, path, body=None, timeout=30):
        data = None if body is None else (body if isinstance(body, (bytes, str)) else json.dumps(body))
        if isinstance(data, str):
            data = data.encode()
        hdr = dict(self.headers)
        if data is not None:
            hdr.setdefault("Content-Type", "application/json")
        errors = []
        cands = [self.base] if self.base else [(h, p, s) for h, p in self._targets()
                                               for s in ([self.scheme] if self.scheme else ["http", "https"])]
        for h, p, scheme in cands:
            try:
                return self._direct(scheme, h, p, method, path, data, hdr, timeout)
            except (OSError, http.client.HTTPException, ssl.SSLError) as e:
                errors.append(f"{scheme}://{h}:{p}: {e}")
                if self.base:
                    self.base = None
        return self._inside(method, path, data, hdr, timeout, errors)

    def _direct(self, scheme, host, port, method, path, data, hdr, timeout):
        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE   # local, self-signed dev clusters
            c = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        else:
            c = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            c.request(method, path, body=data, headers=hdr)
            r = c.getresponse()
            raw = r.read(OUT_LIMIT)
            if r.status == 400 and b"HTTP request to an HTTPS server" in raw:
                raise http.client.HTTPException("needs https")
        finally:
            c.close()
        self.base = (host, port, scheme)
        return r.status, raw.decode(errors="replace")

    def _inside(self, method, path, data, hdr, timeout, errors):
        tool = self.b.which("curl", "wget")
        if not tool:
            raise DbError(f"Can't reach port {self.port} of {self.b.name}. " + " · ".join(errors[-2:]))
        url = f"{self.scheme or 'http'}://127.0.0.1:{self.port}{path}"
        if tool == "curl":
            cmd = ["curl", "-sS", "-k", "-X", method, "-w", "\n%{http_code}", url]
            for k, v in hdr.items():
                cmd += ["-H", f"{k}: {v}"]
            if data is not None:
                cmd += ["--data-binary", data.decode()]
        else:
            if method not in ("GET", "POST"):
                raise DbError("This image only has wget, which can't send " + method)
            cmd = ["wget", "-q", "-O", "-", "--no-check-certificate", url]
            for k, v in hdr.items():
                cmd += [f"--header={k}: {v}"]
            if data is not None:
                cmd += [f"--post-data={data.decode()}"]
        code, out, err, _t = self.b.exec(cmd, timeout=timeout)
        if tool == "curl":
            body, _, status = out.rpartition("\n")
            if code != 0 or not status.isdigit():
                raise DbError(err.strip() or f"curl failed ({code})")
            return int(status), body
        if code != 0:
            raise DbError(err.strip() or "wget failed")
        return 200, out

    def json(self, method, path, body=None, timeout=30):
        status, text = self.request(method, path, body, timeout)
        if len(text) >= OUT_LIMIT - 1:
            raise DbError("The reply is too large to show at once")
        try:
            data = json.loads(text) if text.strip() else None
        except ValueError:
            data = None
        if status >= 400:
            msg = data.get("error") if isinstance(data, dict) else None
            if isinstance(msg, dict):
                msg = msg.get("reason") or msg.get("message") or json.dumps(msg)
            if isinstance(data, dict) and not msg:
                msg = data.get("message") or (data.get("root", {}).get("errors") or [{}])[0].get("message")
            raise DbError(f"HTTP {status}: {msg or text[:300]}")
        return data if data is not None else text


def _http_console(http, text, allow_write, read_ok):
    """Kibana-style console: first line `METHOD /path`, the rest an optional JSON body."""
    lines = text.strip().splitlines()
    if not lines:
        raise DbError("Type a request, e.g. GET /")
    first = lines[0].split(None, 1)
    if len(first) == 1:
        method, path = "GET", first[0]
    else:
        method, path = first[0].upper(), first[1].strip()
    if not path.startswith("/"):
        path = "/" + path
    body = "\n".join(lines[1:]).strip() or None
    if body:
        try:
            json.loads(body)
        except ValueError as e:
            raise DbError(f"The body isn't valid JSON: {e}")
    if not allow_write and not read_ok(method, path):
        raise DbError(f"{method} {path} can change data. Berth is in read-only mode: switch on “Allow changes” to "
                      "send it.")
    status, out = http.request(method, path, body.encode() if body else None)
    try:
        data = json.loads(out)
    except ValueError:
        return Result(text=out, note=f"HTTP {status}")
    for key in ("hits",):              # search results → a grid
        hits = data.get(key) if isinstance(data, dict) else None
        if isinstance(hits, dict) and isinstance(hits.get("hits"), list):
            docs = [dict(_id=h.get("_id"), **(h.get("_source") or {})) for h in hits["hits"]]
            r = docs_result(docs, note=f"HTTP {status} · {_total(hits)} hits")
            return r
        if isinstance(hits, list) and hits and isinstance(hits[0], dict):
            docs = [h.get("document", h) for h in hits]
            return docs_result(docs, note=f"HTTP {status} · {data.get('found', data.get('estimatedTotalHits', len(hits)))} hits")
    if isinstance(data, list) and data and all(isinstance(d, dict) for d in data):
        return docs_result(data, note=f"HTTP {status}")
    return Result(text=json.dumps(data, indent=2, ensure_ascii=False), note=f"HTTP {status}")


def _total(hits):
    t = hits.get("total")
    return t.get("value") if isinstance(t, dict) else t


class Vespa(Backend):
    kind = "vespa"
    language = "yql"
    multi_db = False
    write_capable = True             # documents are edited through the document API (db_edit.VespaEdit)

    def __init__(self, engine, ins):
        super().__init__(engine, ins)
        self.http = Http(self, 8080)
        self.cfg = Http(self, 19071)

    def connect(self):
        st = self.http.json("GET", "/state/v1/health")
        status = (st.get("status") or {}).get("code") if isinstance(st, dict) else None
        try:
            v = self.cfg.json("GET", "/state/v1/version")
            self.version = (v or {}).get("version", "") if isinstance(v, dict) else ""
        except DbError:
            self.version = ""
        return f"Vespa {self.version}".strip() + (f" · {status}" if status and status != "up" else "")

    def _schemas(self):
        base = "/application/v2/tenant/default/application/default/environment/prod/region/default/instance/default"
        try:
            files = self.cfg.json("GET", f"{base}/content/schemas/")
            out = {}
            for url in files or []:
                name = url.rstrip("/").rsplit("/", 1)[-1]
                if name.endswith(".sd"):
                    out[name[:-3]] = urllib.parse.urlparse(url).path
            if out:
                return out
        except DbError:
            pass
        # no config server reachable: learn the document types from the documents themselves
        r = self.http.json("GET", "/search/?" + urllib.parse.urlencode(
            {"yql": "select documentid from sources * where true", "hits": 400}))
        out = {}
        for h in ((r or {}).get("root") or {}).get("children") or []:
            did = (h.get("fields") or {}).get("documentid", "")
            parts = did.split(":")
            if len(parts) > 2:
                out[parts[2]] = None
        return out

    def tree(self):
        self._files = self._schemas()
        nodes = []
        for name in sorted(self._files):
            try:
                r = self.http.json("GET", "/search/?" + urllib.parse.urlencode(
                    {"yql": f"select * from sources {name} where true", "hits": 0, "timeout": "5s"}))
                n = (((r or {}).get("root") or {}).get("fields") or {}).get("totalCount")
            except DbError:
                n = None
            nodes.append(Node(name, "schema", name, f"{n:,} documents" if isinstance(n, int) else "schema",
                              icon="view-grid-symbolic"))
        return nodes

    def _yql(self, yql, hits=ROW_LIMIT, offset=0, body=None):
        req = body or {"yql": yql, "hits": hits, "offset": offset, "timeout": "20s"}
        r = self.http.json("POST", "/search/", req)
        root = (r or {}).get("root") or {}
        errs = root.get("errors")
        if errs:
            raise DbError("; ".join(e.get("message", "") for e in errs))
        docs = []
        for h in root.get("children") or []:
            f = dict(h.get("fields") or {})
            f.pop("sddocname", None)
            row = {"id": h.get("id", "")}
            if "relevance" in h:
                row["relevance"] = h["relevance"]
            row.update(f)
            docs.append(row)
        total = (root.get("fields") or {}).get("totalCount")
        return docs_result(docs, note=f"{total:,} matching documents" if isinstance(total, int) else "",
                           truncated=isinstance(total, int) and total > offset + len(docs))

    @_timed
    def browse(self, node, offset=0, limit=ROW_LIMIT):
        return self._yql(f"select * from sources {node.ref} where true", limit, offset)

    def structure(self, node):
        path = (self._files or {}).get(node.ref) if hasattr(self, "_files") else None
        if not path:
            return Result(text="The schema file is only available when the config server (port 19071) is reachable.")
        status, text = self.cfg.request("GET", path)
        return Result(text=text, note=f"{node.ref}.sd")

    @_timed
    def query(self, text, allow_write=False):
        t = text.strip()
        if not t:
            raise DbError("Type YQL, e.g. select * from sources * where true")
        if t.startswith("{"):
            try:
                body = json.loads(t)
            except ValueError as e:
                raise DbError(f"Not valid JSON: {e}")
            return self._yql(None, body=body)
        if re.match(r"^(GET|POST|PUT|DELETE)\s", t, re.I):
            return _http_console(self.http, t, allow_write, lambda m, p: m == "GET" or p.startswith("/search"))
        return self._yql(t, QUERY_ROWS if "limit" not in t.lower() else ROW_LIMIT)

    def is_destructive(self, text):
        return bool(re.match(r"^\s*(DELETE|PUT)\s", text or "", re.I))

    def example(self):
        return "select * from sources * where true limit 20"


class OpenSearch(Backend):
    kind = "opensearch"
    language = "http"
    write_capable = True

    def __init__(self, engine, ins):
        super().__init__(engine, ins)
        e = self.env
        hdr = {}
        pw = e.get("OPENSEARCH_INITIAL_ADMIN_PASSWORD") or e.get("ELASTIC_PASSWORD")
        user = "elastic" if e.get("ELASTIC_PASSWORD") else "admin"
        if pw or "opensearch" in _image(ins.get("Config") or {}) and e.get("DISABLE_SECURITY_PLUGIN", "") != "true":
            import base64
            hdr["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pw or 'admin'}".encode()).decode()
        self.http = Http(self, 9200, hdr)

    def connect(self):
        r = self.http.json("GET", "/")
        v = (r.get("version") or {}) if isinstance(r, dict) else {}
        self.version = v.get("number", "")
        dist = "OpenSearch" if v.get("distribution") == "opensearch" else "Elasticsearch"
        return f"{dist} {self.version}"

    def tree(self):
        r = self.http.json("GET", "/_cat/indices?format=json&bytes=b&s=index")
        nodes = []
        for i in r or []:
            name = i.get("index", "")
            if name.startswith("."):
                continue
            size = int(i.get("store.size") or 0)
            nodes.append(Node(name, "index", name, f"{int(i.get('docs.count') or 0):,} documents · {_size(size)}"
                              + ("" if i.get("health") in ("green", None) else f" · {i.get('health')}"),
                              icon="view-grid-symbolic"))
        return nodes

    @_timed
    def browse(self, node, offset=0, limit=ROW_LIMIT):
        r = self.http.json("POST", f"/{urllib.parse.quote(node.ref)}/_search",
                           {"from": offset, "size": limit, "query": {"match_all": {}}, "track_total_hits": True})
        hits = (r or {}).get("hits") or {}
        docs = [dict(_id=h.get("_id"), **(h.get("_source") or {})) for h in hits.get("hits") or []]
        total = _total(hits) or 0
        return docs_result(docs, note=f"{total:,} documents", truncated=total > offset + len(docs))

    def structure(self, node):
        r = self.http.json("GET", f"/{urllib.parse.quote(node.ref)}/_mapping")
        return Result(text=json.dumps(r, indent=2), note="mapping")

    @_timed
    def query(self, text, allow_write=False):
        def read_ok(m, p):
            return m in ("GET", "HEAD") or (m == "POST" and re.search(r"/_(search|count|msearch|validate|explain|"
                                                                     r"field_caps|mget|analyze)\b", p) is not None)
        return _http_console(self.http, text, allow_write, read_ok)

    def is_destructive(self, text):
        return bool(re.match(r"^\s*DELETE\s|^\s*POST\s+\S*_delete_by_query", text or "", re.I))

    def example(self):
        return 'GET /_cluster/health'


class Typesense(Backend):
    kind = "typesense"
    language = "http"

    def __init__(self, engine, ins):
        super().__init__(engine, ins)
        key = self.env.get("TYPESENSE_API_KEY", "")
        cmd = (ins.get("Config") or {}).get("Cmd") or []
        for i, a in enumerate(cmd):
            if a.startswith("--api-key="):
                key = a.split("=", 1)[1]
            elif a == "--api-key" and i + 1 < len(cmd):
                key = cmd[i + 1]
        self.http = Http(self, 8108, {"X-TYPESENSE-API-KEY": key})

    def connect(self):
        self.http.json("GET", "/health")
        try:
            d = self.http.json("GET", "/debug")
            self.version = (d or {}).get("version", "") if isinstance(d, dict) else ""
        except DbError:
            pass
        return f"Typesense {self.version}".strip()

    def tree(self):
        self._schemas = {c["name"]: c for c in self.http.json("GET", "/collections") or []}
        return [Node(n, "collection", n, f"{int(c.get('num_documents') or 0):,} documents", icon="view-grid-symbolic")
                for n, c in sorted(self._schemas.items())]

    @_timed
    def browse(self, node, offset=0, limit=ROW_LIMIT):
        sch = getattr(self, "_schemas", {}).get(node.ref) or self.http.json("GET", f"/collections/{node.ref}")
        strings = [f["name"] for f in sch.get("fields") or [] if f.get("type") in ("string", "string[]")
                   and f.get("index", True) and "*" not in f["name"]]
        per = min(int(limit), 250)
        q = {"q": "*", "per_page": per, "page": int(offset) // per + 1}
        if strings:
            q["query_by"] = strings[0]
        r = self.http.json("GET", f"/collections/{urllib.parse.quote(node.ref)}/documents/search?"
                           + urllib.parse.urlencode(q))
        docs = [h.get("document", {}) for h in (r or {}).get("hits") or []]
        found = (r or {}).get("found", 0)
        return docs_result(docs, note=f"{found:,} documents", truncated=found > offset + len(docs))

    def structure(self, node):
        return Result(text=json.dumps(self.http.json("GET", f"/collections/{urllib.parse.quote(node.ref)}"), indent=2))

    @_timed
    def query(self, text, allow_write=False):
        return _http_console(self.http, text, allow_write,
                             lambda m, p: m == "GET" or (m == "POST" and "/multi_search" in p))

    def is_destructive(self, text):
        return bool(re.match(r"^\s*DELETE\s", text or "", re.I))

    def example(self):
        return "GET /collections"


class Meilisearch(Backend):
    kind = "meilisearch"
    language = "http"

    def __init__(self, engine, ins):
        super().__init__(engine, ins)
        key = self.env.get("MEILI_MASTER_KEY", "")
        self.http = Http(self, 7700, {"Authorization": f"Bearer {key}"} if key else {})

    def connect(self):
        v = self.http.json("GET", "/version")
        self.version = (v or {}).get("pkgVersion", "") if isinstance(v, dict) else ""
        return f"Meilisearch {self.version}".strip()

    def tree(self):
        r = self.http.json("GET", "/indexes?limit=1000")
        stats = self.http.json("GET", "/stats") or {}
        idx = (stats.get("indexes") or {}) if isinstance(stats, dict) else {}
        return [Node(i["uid"], "index", i["uid"],
                     f"{int((idx.get(i['uid']) or {}).get('numberOfDocuments') or 0):,} documents",
                     icon="view-grid-symbolic") for i in (r or {}).get("results") or []]

    @_timed
    def browse(self, node, offset=0, limit=ROW_LIMIT):
        r = self.http.json("GET", f"/indexes/{urllib.parse.quote(node.ref)}/documents?offset={int(offset)}"
                           f"&limit={int(limit)}")
        docs, total = (r or {}).get("results") or [], (r or {}).get("total", 0)
        return docs_result(docs, note=f"{total:,} documents", truncated=total > offset + len(docs))

    def structure(self, node):
        return Result(text=json.dumps(self.http.json("GET", f"/indexes/{urllib.parse.quote(node.ref)}/settings"),
                                      indent=2), note="settings")

    @_timed
    def query(self, text, allow_write=False):
        return _http_console(self.http, text, allow_write,
                             lambda m, p: m == "GET" or (m == "POST" and (p.endswith("/search")
                                                                         or p.startswith("/multi-search"))))

    def is_destructive(self, text):
        return bool(re.match(r"^\s*DELETE\s", text or "", re.I))

    def example(self):
        return "GET /indexes"


def _size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ── SQLite (a copy of a file from a container or volume) ────────────────────────────────────────────────────────
SQLITE_MAX = 512 * 1024 * 1024


class SQLite(Backend):
    """Opens a read-only copy: the file is fetched with the engine's archive API (plus -wal / -shm, so recent writes
    are included), then queried locally with Python's sqlite3. Changes are never written back."""
    kind = "sqlite"
    write_capable = False
    multi_db = False

    def __init__(self, engine, source_cid, path, title):
        self.engine, self.cid, self.path = engine, source_cid, path
        self.name, self.ins, self.env, self.db, self.version = title, {"Id": source_cid}, {}, None, ""
        self.tmp = tempfile.mkdtemp(prefix="berth-sqlite-")
        self.local = None
        self.con = None

    def connect(self):
        if self.con is not None:
            return f"SQLite {self.version} · read-only copy of {self.path}"
        base = os.path.basename(self.path)
        for suffix in ("", "-wal", "-shm"):
            try:
                data = self.engine.get_archive(self.cid, self.path + suffix, limit=SQLITE_MAX)
            except Exception as e:
                if suffix:
                    continue
                raise DbError(f"Can't read {self.path}: {e}")
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                m = next((m for m in tf.getmembers() if m.isfile()), None)
                if m is None:
                    if suffix:
                        continue
                    raise DbError(f"{self.path} is not a file")
                with open(os.path.join(self.tmp, base + suffix), "wb") as f:
                    f.write(tf.extractfile(m).read())
        self.local = os.path.join(self.tmp, base)
        with open(self.local, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                raise DbError(f"{self.path} is not an SQLite database")
        if os.path.exists(self.local + "-wal"):        # fold recent writes into OUR copy (never the original)
            c = sqlite3.connect(self.local)
            c.execute("pragma wal_checkpoint(truncate)")
            c.execute("pragma journal_mode=delete")
            c.close()
        # immutable copy: open read-only so even "Allow changes" can't write to it
        self.con = sqlite3.connect(f"file:{self.local}?mode=ro", uri=True, check_same_thread=False)
        self.version = sqlite3.sqlite_version
        return f"SQLite {self.version} · read-only copy of {self.path}"

    def tree(self):
        rows = self.con.execute("select name, type from sqlite_master where type in ('table','view') "
                                "and name not like 'sqlite_%' order by name").fetchall()
        out = []
        for n, t in rows:
            detail = "view"
            if t == "table":
                try:
                    detail = f"{self.con.execute(f'select count(*) from {_qi(n)}').fetchone()[0]:,} rows"
                except sqlite3.Error:
                    detail = "table"
            out.append(Node(n, "table", n, detail, icon="view-grid-symbolic"))
        return out

    def _run(self, sql, params=()):
        try:
            cur = self.con.execute(sql, params)
        except sqlite3.Error as e:
            msg = str(e)
            if "readonly" in msg:
                msg += "\n\nThis is a read-only copy of the file; changes can't be saved back."
            raise DbError(msg)
        if cur.description is None:
            return Result(note="Done.")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(QUERY_ROWS + 1)
        return Result(cols, [[cap(v) for v in r]
                             for r in rows[:QUERY_ROWS]], truncated=len(rows) > QUERY_ROWS)

    @_timed
    def browse(self, node, offset=0, limit=ROW_LIMIT):
        return self._run(f"select * from {_qi(node.ref)} limit ? offset ?", (int(limit), int(offset)))

    @_timed
    def structure(self, node):
        r = self._run(f"select name as column, type, case when \"notnull\" then 'no' else 'yes' end as nullable, "
                      f"dflt_value as \"default\", pk from pragma_table_info({_ql(node.ref)})")
        idx = self.con.execute(f"select name from pragma_index_list({_ql(node.ref)})").fetchall()
        r.note = "Indexes: " + (", ".join(i[0] for i in idx) or "none")
        return r

    @_timed
    def query(self, text, allow_write=False):
        stmts = _split_sql(text)
        if not stmts:
            raise DbError("Type a query first")
        r = Result(note="Done.")
        for s in stmts:
            r = self._run(s)
        return r

    def example(self):
        return "select name, sql from sqlite_master;"

    def close(self):
        try:
            if self.con:
                self.con.close()
            for f in os.listdir(self.tmp):
                os.remove(os.path.join(self.tmp, f))
            os.rmdir(self.tmp)
        except OSError:
            pass


BACKENDS = {"postgres": Postgres, "mysql": MySQL, "redis": Redis, "mongo": Mongo, "vespa": Vespa,
            "opensearch": OpenSearch, "typesense": Typesense, "meilisearch": Meilisearch}


# ── export ──────────────────────────────────────────────────────────────────────────────────────────────────────
def to_csv(result):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(result.columns)
    for r in result.rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


def to_json(result):
    if result.text is not None and not result.columns:
        return result.text
    return json.dumps([dict(zip(result.columns, r)) for r in result.rows], indent=2, ensure_ascii=False)


def to_markdown(result):
    def cell(v):
        return ("NULL" if v is None else str(v)).replace("|", "\\|").replace("\n", " ")
    head = "| " + " | ".join(result.columns) + " |\n|" + "---|" * len(result.columns) + "\n"
    return head + "".join("| " + " | ".join(cell(v) for v in r) + " |\n" for r in result.rows)
