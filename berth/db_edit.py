"""Changing data in the database explorer: open one row / key item / document in full, edit it, add one, delete one,
plus table- and key-level actions (empty, delete, expiry, new key).

Every write needs "Allow changes" and goes through the same engine client the explorer reads with. Rows are picked
out by their primary key (or a document's id), never by position, and a save that matches nothing says so instead of
silently doing nothing. Values travel as data: quoted literals (SQL), hex literals (MySQL), separate argv items
(Redis), environment variables (MongoDB) or a JSON body (search engines) — and a file inside the container when they
are too large for a command line."""
import base64
import io
import json
import tarfile
import time
import urllib.parse
import uuid

from . import db
from .db import DbError, _bq, _qi, _ql

DEFAULT = object()          # "leave it to the column default" when adding a row
GONE = "That row isn't there any more: it was changed or deleted since the page loaded. Reload and try again."
BIG = 100_000               # bytes: above this a value goes through a file, not the command line
OPEN_MAX = 2_000_000        # characters of one value shown in the editor; larger ones open read-only


class Field:
    """One value in the record panel."""

    def __init__(self, name, type="", value=None, nullable=True, default="", pk=False, readonly="", binary=None,
                 json=False, multiline=False):
        self.name, self.type, self.value, self.nullable, self.default = name, type, value, nullable, default
        self.pk, self.readonly, self.binary, self.json, self.multiline = pk, readonly, binary, json, multiline
        if isinstance(value, str) and len(value) > OPEN_MAX:
            self.value = value[:OPEN_MAX]
            self.readonly = self.readonly or f"Too large to edit here ({len(value):,} characters); showing the start"


def upload(b, data, ext="txt"):
    """Put `data` into /tmp inside the container; returns its path (remove it with b.exec(["rm", "-f", path]))."""
    name = f"berth-{uuid.uuid4().hex[:12]}.{ext}"
    raw = data.encode() if isinstance(data, str) else data
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo(name)
        ti.size, ti.mode, ti.mtime = len(raw), 0o644, int(time.time())
        tf.addfile(ti, io.BytesIO(raw))
    try:
        b.engine.put_archive(b.cid, "/tmp", buf.getvalue())
    except Exception as e:
        raise DbError(f"Couldn't hand the value to the container: {e}")
    return "/tmp/" + name


class Editor:
    """What can be changed for one backend. Subclasses fill in the engine specifics."""
    unit = "row"                # what one line of the grid is called: row · field · item · member · document
    can_add = can_delete = True
    can_empty = can_drop = False
    document = False            # True: the panel edits one JSON document instead of separate fields
    inline = False              # True: a grid cell is one stored value, so it can be edited in place

    def __init__(self, b):
        self.b = b

    # which grid columns pick out a line; None = editable, else the reason it isn't
    def annotate(self, node, r):
        r.key, r.edit_why = [], "Editing isn't available here"

    def ident(self, node, r, pos):
        row = r.rows[pos]
        return {r.columns[i]: row[i] for i in r.key}

    def fetch(self, node, ident):
        raise DbError("Opening a row isn't supported here")

    def blank(self, node):
        return []

    def save(self, node, ident, values, allow):
        raise DbError("Saving isn't supported here")

    def delete(self, node, ident, allow):
        raise DbError("Deleting isn't supported here")

    def empty(self, node, allow):
        raise DbError("Not supported")

    def drop(self, node, allow):
        raise DbError("Not supported")

    def _ok(self, allow):
        self.b._need_write(allow)


# ── PostgreSQL ──────────────────────────────────────────────────────────────────────────────────────────────────
class PgEdit(Editor):
    can_empty = can_drop = inline = True

    def __init__(self, b):
        super().__init__(b)
        self._info = {}

    def _t(self, node):
        s, t = node.ref
        return f"{_qi(s)}.{_qi(t)}"

    def cols(self, node):
        key = (self.b.db,) + tuple(node.ref)
        if key not in self._info:
            reg = _ql(self._t(node))
            self._info[key] = self.b._rows(
                f"""select a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull::text,
                           coalesce(pg_get_expr(d.adbin, d.adrelid), ''),
                           exists(select 1 from pg_index i where i.indrelid = a.attrelid and i.indisprimary
                                  and a.attnum = any(i.indkey))::text,
                           a.attidentity::text, a.attgenerated::text
                    from pg_attribute a left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
                    where a.attrelid = {reg}::regclass and a.attnum > 0 and not a.attisdropped
                    order by a.attnum""").rows
        return self._info[key]

    def annotate(self, node, r):
        r.key, r.edit_why = [], None
        r.types = {}
        if node.kind != "table":
            r.edit_why = "Pick a table to edit its rows"
            return
        info = self.cols(node)
        r.types = {c[0]: c[1] for c in info}
        if getattr(node, "relkind", "r") not in ("r", "p"):
            r.edit_why = "Views can't be edited here — change the tables behind them"
            return
        pk = [c[0] for c in info if c[4] == "true"]
        if not pk:
            r.edit_why = "This table has no primary key, so Berth can't pick out one row safely (use the Query tab)"
            return
        r.key = [r.columns.index(c) for c in pk if c in r.columns]

    def _where(self, ident):
        return " and ".join(f"{_qi(k)} = {_ql(str(v))}" for k, v in ident.items())

    @staticmethod
    def _lit(v):
        return "NULL" if v is None else "DEFAULT" if v is DEFAULT else _ql(v)

    def _fields(self, node, values):
        out = []
        for (name, typ, notnull, default, pk, ident, gen), v in zip(self.cols(node), values):
            binary = None
            if typ == "bytea" and v is not None:
                size, head = v.split(":", 1)
                binary, v = (int(size), head), None
            ro = ("Computed by the database (generated column)" if gen else
                  "Always set by the database (identity column)" if ident == "a" else
                  "Binary — save it to a file to look inside" if typ == "bytea" else "")
            out.append(Field(name, typ, v, nullable=notnull != "true", default=default, pk=pk == "true",
                             readonly=ro, binary=binary, json=typ in ("json", "jsonb"),
                             multiline=typ in ("text", "json", "jsonb", "xml") or typ.endswith("[]")))
        return out

    def fetch(self, node, ident):
        exprs = [f"octet_length({_qi(c[0])})::text || ':' || encode(substring({_qi(c[0])} from 1 for 1024), 'hex')"
                 if c[1] == "bytea" else f"{_qi(c[0])}::text" for c in self.cols(node)]
        out, trunc = self.b._psql(f"select {', '.join(exprs)} from {self._t(node)} where {self._where(ident)} limit 1")
        if trunc:
            raise DbError("This row is over 16 MB — too large to open here. Read parts of it in the Query tab.")
        rows, _cut = db._csv_rows(out)
        if len(rows) < 2:
            raise DbError(GONE)
        return self._fields(node, [None if v == db.NULL else v for v in rows[1]])

    def blank(self, node):
        return [setattr(f, "value", None) or f for f in self._fields(node, [None] * len(self.cols(node)))]

    def _exec(self, stmt):
        sql = f"with w as ({stmt} returning 1) select count(*) from w"
        path = None
        if len(sql.encode()) > BIG:
            path = upload(self.b, sql, "sql")
        try:
            out, _t = self.b._psql(sql, readonly=False, file=path)
        finally:
            if path:
                self.b.exec(["rm", "-f", path], timeout=10)
        rows, _c = db._csv_rows(out)
        return int(rows[1][0]) if len(rows) > 1 else 0

    def save(self, node, ident, values, allow):
        self._ok(allow)
        if ident is None:
            cols = [k for k, v in values.items() if v is not DEFAULT]
            stmt = (f"insert into {self._t(node)} ({', '.join(map(_qi, cols))}) "
                    f"values ({', '.join(self._lit(values[c]) for c in cols)})" if cols else
                    f"insert into {self._t(node)} default values")
            self._exec(stmt)
            return "Row added"
        if not values:
            return "Nothing changed"
        sets = ", ".join(f"{_qi(k)} = {self._lit(v)}" for k, v in values.items())
        if self._exec(f"update {self._t(node)} set {sets} where {self._where(ident)}") == 0:
            raise DbError(GONE)
        return "Saved"

    def delete(self, node, ident, allow):
        self._ok(allow)
        if self._exec(f"delete from {self._t(node)} where {self._where(ident)}") == 0:
            raise DbError(GONE)
        return "Row deleted"

    def blob(self, node, ident, col):
        """The whole binary value (up to 12 MB: the reply travels as base64 through the client)."""
        q = _qi(col)
        out, _t = self.b._psql(f"select octet_length({q}) from {self._t(node)} where {self._where(ident)}")
        rows, _c = db._csv_rows(out)
        if len(rows) < 2:
            raise DbError(GONE)
        if rows[1][0] not in (db.NULL, "") and int(rows[1][0]) > 12_000_000:
            raise DbError(f"This value is {db._size(int(rows[1][0]))}; Berth saves values up to 12 MB. Use pg_dump "
                          "or COPY for larger ones.")
        out, trunc = self.b._psql(f"select encode({q}, 'base64') from {self._t(node)} where {self._where(ident)}")
        rows, _c = db._csv_rows(out)
        return base64.b64decode(rows[1][0]) if len(rows) > 1 and rows[1][0] != db.NULL else b""

    def empty(self, node, allow):
        self._ok(allow)
        self.b._psql(f"truncate {self._t(node)}", readonly=False)
        return "Table emptied"

    def drop(self, node, allow):
        self._ok(allow)
        kind = {"v": "view", "m": "materialized view", "f": "foreign table"}.get(getattr(node, "relkind", "r"), "table")
        self.b._psql(f"drop {kind} {self._t(node)}", readonly=False)
        self._info.clear()
        return f"{kind.capitalize()} deleted"


# ── MySQL / MariaDB ─────────────────────────────────────────────────────────────────────────────────────────────
MYSQL_BINARY = {"blob", "tinyblob", "mediumblob", "longblob", "binary", "varbinary", "bit", "geometry", "point",
                "linestring", "polygon", "multipoint", "multilinestring", "multipolygon", "geometrycollection"}


class MyEdit(Editor):
    can_empty = can_drop = inline = True

    def __init__(self, b):
        super().__init__(b)
        self._info = {}

    def cols(self, node):
        key = (self.b.db, node.ref)
        if key not in self._info:
            self._info[key] = self.b._run(
                "select column_name, column_type, is_nullable, column_default, column_key, extra, data_type "
                "from information_schema.columns where table_schema = database() and table_name = "
                "'" + node.ref.replace("\\", "\\\\").replace("'", "''") + "' order by ordinal_position", raw=True).rows
        return self._info[key]

    @staticmethod
    def _lit(v):
        if v is None:
            return "NULL"
        if v is DEFAULT:
            return "DEFAULT"
        return f"CONVERT(X'{v.encode().hex()}' USING utf8mb4)"     # hex: no quoting or escaping to get wrong

    def annotate(self, node, r):
        info = self.cols(node)
        r.types = {c[0]: c[1] for c in info}
        r.key, r.edit_why = [], None
        if node.detail == "view":
            r.edit_why = "Views can't be edited here — change the tables behind them"
            return
        pk = [c[0] for c in info if c[4] == "PRI"]
        if not pk:
            r.edit_why = "This table has no primary key, so Berth can't pick out one row safely (use the Query tab)"
            return
        r.key = [r.columns.index(c) for c in pk if c in r.columns]

    def _where(self, ident):
        return " AND ".join(f"{_bq(k)} = {self._lit(str(v))}" for k, v in ident.items())

    def _run(self, sql):
        if len(sql.encode()) <= BIG:
            return self.b._run(sql, raw=True)
        path = upload(self.b, sql, "sql")
        try:
            return self.b._run("", raw=True, file=path)
        finally:
            self.b.exec(["rm", "-f", path], timeout=10)

    def fetch(self, node, ident):
        info = self.cols(node)
        exprs = []
        for c in info:
            q = _bq(c[0])
            exprs.append(f"CONCAT(OCTET_LENGTH({q}), ':', HEX(LEFT({q}, 1024)))" if c[6] in MYSQL_BINARY else q)
            exprs.append(f"{q} IS NULL")
        r = self.b._run(f"SELECT {', '.join(exprs)} FROM {_bq(node.ref)} WHERE {self._where(ident)} LIMIT 1", raw=True)
        if not r.rows:
            raise DbError(GONE)
        vals = r.rows[0]
        # the client prints NULL for both a NULL and the text "NULL": the IS NULL flag tells them apart
        return self._fields(info, [None if vals[2 * i + 1] == "1" else ("NULL" if vals[2 * i] is None else vals[2 * i])
                                   for i in range(len(info))])

    def _fields(self, info, values):
        out = []
        for (name, ctype, nullable, default, key, extra, dtype), v in zip(info, values):
            binary = None
            if dtype in MYSQL_BINARY and v is not None:
                size, head = v.split(":", 1)
                binary, v = (int(size), head.lower()), None
            ro = ("Computed by the database (generated column)" if "GENERATED" in (extra or "").upper() else
                  "Binary — save it to a file to look inside" if dtype in MYSQL_BINARY else "")
            out.append(Field(name, ctype, v, nullable=nullable == "YES",
                             default=("" if default in (None, "NULL") else default)
                             + (" auto_increment" if "auto_increment" in (extra or "") else ""),
                             pk=key == "PRI", readonly=ro, binary=binary, json=dtype == "json",
                             multiline=dtype in ("text", "mediumtext", "longtext", "json")))
        return out

    def blank(self, node):
        info = self.cols(node)
        return self._fields(info, [None] * len(info))

    def save(self, node, ident, values, allow):
        self._ok(allow)
        t = _bq(node.ref)
        if ident is None:
            cols = [k for k, v in values.items() if v is not DEFAULT]
            self._run(f"INSERT INTO {t} ({', '.join(map(_bq, cols))}) VALUES "
                      f"({', '.join(self._lit(values[c]) for c in cols)})")
            return "Row added"
        if not values:
            return "Nothing changed"
        w = self._where(ident)
        sets = ", ".join(f"{_bq(k)} = {self._lit(v)}" for k, v in values.items())
        r = self._run(f"SELECT COUNT(*) INTO @berth_n FROM {t} WHERE {w}; UPDATE {t} SET {sets} WHERE {w} LIMIT 1; "
                      "SELECT @berth_n AS n")
        if not r.rows or r.rows[0][0] in ("0", None):
            raise DbError(GONE)
        return "Saved"

    def delete(self, node, ident, allow):
        self._ok(allow)
        r = self._run(f"DELETE FROM {_bq(node.ref)} WHERE {self._where(ident)} LIMIT 1; SELECT ROW_COUNT() AS n")
        if not r.rows or r.rows[0][0] in ("0", None):
            raise DbError(GONE)
        return "Row deleted"

    def blob(self, node, ident, col):
        w = self._where(ident)
        r = self.b._run(f"SELECT OCTET_LENGTH({_bq(col)}) FROM {_bq(node.ref)} WHERE {w}", raw=True)
        if not r.rows:
            raise DbError(GONE)
        if r.rows[0][0] and int(r.rows[0][0]) > 12_000_000:
            raise DbError(f"This value is {db._size(int(r.rows[0][0]))}; Berth saves values up to 12 MB. Use "
                          "mysqldump for larger ones.")
        r = self.b._run(f"SELECT TO_BASE64({_bq(col)}) FROM {_bq(node.ref)} WHERE {w}", raw=True)
        return base64.b64decode(r.rows[0][0] or "") if r.rows else b""

    def empty(self, node, allow):
        self._ok(allow)
        self.b._run(f"TRUNCATE TABLE {_bq(node.ref)}")
        return "Table emptied"

    def drop(self, node, allow):
        self._ok(allow)
        self.b._run(f"DROP {'VIEW' if node.detail == 'view' else 'TABLE'} {_bq(node.ref)}")
        self._info.clear()
        return "Deleted"


# ── Redis / Valkey ──────────────────────────────────────────────────────────────────────────────────────────────
REDIS_SHAPES = {("value",): "string", ("field", "value"): "hash", ("index", "value"): "list", ("member",): "set",
                ("member", "score"): "zset", ("id", "fields"): "stream"}


class RedisEdit(Editor):
    can_drop = True

    def _kind(self, r):
        if r.text is not None and not r.columns:
            return "json"
        return REDIS_SHAPES.get(tuple(r.columns), "")

    def annotate(self, node, r):
        k = self._kind(r)
        r.key, r.edit_why, r.types = [], None, {}
        self.kind = k
        self.unit = {"hash": "field", "list": "item", "set": "member", "zset": "member", "stream": "entry"}.get(k, "value")
        self.can_add = self.can_delete = k in ("hash", "list", "set", "zset", "stream")
        if k == "string":
            if "showing the start" in (r.note or ""):
                r.edit_why = "This value is over 64 KB; open it to see the start (it can't be edited here)"
            return
        if k in ("hash", "list", "set", "zset", "stream"):
            r.key = [0]
            if k == "hash" and r.truncated and "sample" in (r.note or ""):
                r.note += " — open a field to edit it"
            return
        if k == "json":
            r.text_editable = True
            return
        r.edit_why = "This kind of key can't be edited here"

    def _cli(self, *args, big=None):
        """Run a command; `big` (the last argument) goes through a file + `redis-cli -x` when it is large."""
        if big is None or len(big.encode()) <= BIG:
            return self.b._cli(*args, *([] if big is None else [big]), raw=True)[0]
        path = upload(self.b, big)
        try:
            code, out, err, _t = self.b.exec(["sh", "-c", 'c="$0"; n="$1"; shift; "$c" -n "$n" --raw -x "$@" < "$BERTH_F"', self.b.cli,
                                              self.b.db, *args], env=dict(self.b._auth() or {}, BERTH_F=path))
        finally:
            self.b.exec(["rm", "-f", path], timeout=10)
        if code != 0 or out.startswith("ERR"):
            raise DbError((err or out).strip() or "redis-cli failed")
        return out

    def fetch(self, node, ident):
        key, k = node.ref, self.kind
        if k == "string":
            n = int(self.b._val("STRLEN", key) or 0)
            if n > OPEN_MAX:
                v = self._cli("GETRANGE", key, "0", str(OPEN_MAX - 1))
                return [Field("value", f"string · {n:,} bytes", v, readonly="Too large to edit here; showing the start",
                              multiline=True)]
            return [Field("value", f"string · {n:,} bytes", self.b._val("GET", key), multiline=True, nullable=False)]
        (col, v), = ident.items()
        if k == "hash":
            val = self.b._val("HGET", key, v)
            if val is None:
                raise DbError(GONE)
            return [Field("field", "hash field", v, nullable=False), Field("value", "", val, nullable=False,
                                                                             multiline=True)]
        if k == "list":
            val = self.b._val("LINDEX", key, v)
            if val is None:
                raise DbError(GONE)
            return [Field("index", "position", v, readonly="The position in the list"),
                    Field("value", "", val, nullable=False, multiline=True)]
        if k == "set":
            if str(self.b._val("SISMEMBER", key, v)) not in ("1", "True", "true"):
                raise DbError(GONE)
            return [Field("member", "set member", v, nullable=False, multiline=True)]
        if k == "zset":
            score = self.b._val("ZSCORE", key, v)
            if score is None:
                raise DbError(GONE)
            return [Field("member", "", v, nullable=False, multiline=True), Field("score", "number", str(score),
                                                                                  nullable=False)]
        if k == "stream":
            rows = self.b._val("XRANGE", key, v, v)
            if not rows:
                raise DbError(GONE)
            fields = rows[0][1]
            if isinstance(fields, list):
                fields = dict(zip(fields[::2], fields[1::2]))
            return [Field("id", "entry id", v, readonly="Stream entries can't be changed, only added or deleted"),
                    Field("fields", "JSON", json.dumps(fields, indent=2, ensure_ascii=False), json=True,
                          readonly="Stream entries can't be changed, only added or deleted")]
        raise DbError("This kind of key can't be edited here")

    def blank(self, node):
        k = self.kind
        return {"hash": [Field("field", "hash field", "", nullable=False), Field("value", "", "", nullable=False,
                                                                                  multiline=True)],
                "list": [Field("value", "added at the end", "", nullable=False, multiline=True)],
                "set": [Field("member", "set member", "", nullable=False, multiline=True)],
                "zset": [Field("member", "", "", nullable=False, multiline=True), Field("score", "number", "0",
                                                                                          nullable=False)],
                "stream": [Field("fields", "JSON object", "{\n  \"field\": \"value\"\n}", json=True,
                                 nullable=False)]}.get(k, [])

    def save(self, node, ident, values, allow):
        self._ok(allow)
        key, k = node.ref, self.kind
        if k == "string":
            if "value" in values:
                ttl = int(self.b._val("PTTL", key) or -1)
                self._cli("SET", key, big=values["value"] or "")
                if ttl > 0:
                    self._cli("PEXPIRE", key, str(ttl))
            return "Saved"
        old = next(iter(ident.values())) if ident else None
        if k == "hash":
            field = values.get("field", old)
            if not field:
                raise DbError("Give the field a name")
            if ident is None or field != old:
                if str(self.b._val("HEXISTS", key, field)) in ("1", "True", "true"):
                    raise DbError(f"The field “{field}” already exists")
            val = values["value"] if "value" in values else self.b._val("HGET", key, old)
            self._cli("HSET", key, field, big=val or "")
            if ident is not None and field != old:
                self._cli("HDEL", key, old)
            return "Field added" if ident is None else "Saved"
        if k == "list":
            if ident is None:
                self._cli("RPUSH", key, big=values.get("value") or "")
                return "Item added at the end"
            if "value" in values:
                self._cli("LSET", key, old, big=values["value"] or "")
            return "Saved"
        if k == "set":
            new = values.get("member", old)
            self._cli("SADD", key, big=new or "")
            if ident is not None and new != old:
                self._cli("SREM", key, old)
            return "Member added" if ident is None else "Saved"
        if k == "zset":
            new = values.get("member", old)
            score = values.get("score") or (self.b._val("ZSCORE", key, old) if old is not None else "0")
            try:
                float(score)
            except ValueError:
                raise DbError("The score must be a number")
            if ident is not None and new != old:
                self._cli("ZREM", key, old)
            self._cli("ZADD", key, str(score), new or "")
            return "Member added" if ident is None else "Saved"
        if k == "stream" and ident is None:
            try:
                obj = json.loads(values.get("fields") or "{}")
            except ValueError as e:
                raise DbError(f"The fields must be a JSON object: {e}")
            if not isinstance(obj, dict) or not obj:
                raise DbError("The fields must be a JSON object with at least one field")
            args = [x for kv in obj.items() for x in (str(kv[0]), kv[1] if isinstance(kv[1], str) else json.dumps(kv[1]))]
            self._cli("XADD", key, "*", *args)
            return "Entry added"
        raise DbError("This can't be changed here")

    def save_text(self, node, text, allow):
        self._ok(allow)
        try:
            json.loads(text)
        except ValueError as e:
            raise DbError(f"That isn't valid JSON: {e}")
        self._cli("JSON.SET", node.ref, "$", big=text)
        return "Saved"

    def delete(self, node, ident, allow):
        self._ok(allow)
        key, k = node.ref, self.kind
        (_c, v), = ident.items()
        if k == "hash":
            n = self._cli("HDEL", key, v)
        elif k == "list":
            mark = f"__berth_delete_{uuid.uuid4().hex}"
            self._cli("LSET", key, v, mark)
            n = self._cli("LREM", key, "1", mark)
        elif k == "set":
            n = self._cli("SREM", key, v)
        elif k == "zset":
            n = self._cli("ZREM", key, v)
        elif k == "stream":
            n = self._cli("XDEL", key, v)
        else:
            raise DbError("Not supported")
        if n.strip() == "0":
            raise DbError(GONE)
        return f"{self.unit.capitalize()} deleted"

    def drop(self, node, allow):
        self._ok(allow)
        self._cli("DEL", node.ref)
        return "Key deleted"

    def expire(self, node, seconds, allow):
        self._ok(allow)
        if seconds is None:
            self._cli("PERSIST", node.ref)
            return "The key no longer expires"
        self._cli("EXPIRE", node.ref, str(int(seconds)))
        return f"The key expires in {int(seconds):,} s"

    def new_key(self, name, kind, value, allow):
        """A new key with a first value. kind: string · hash · list · set · zset · json."""
        self._ok(allow)
        if not name:
            raise DbError("Give the key a name")
        if str(self.b._val("EXISTS", name)) not in ("0", "False", "false"):
            raise DbError(f"A key called “{name}” already exists")
        if kind == "string":
            self._cli("SET", name, big=value or "")
        elif kind == "hash":
            self._cli("HSET", name, "field", big=value or "")
        elif kind == "list":
            self._cli("RPUSH", name, big=value or "")
        elif kind == "set":
            self._cli("SADD", name, big=value or "")
        elif kind == "zset":
            self._cli("ZADD", name, "0", value or "")
        elif kind == "json":
            try:
                json.loads(value or "{}")
            except ValueError as e:
                raise DbError(f"That isn't valid JSON: {e}")
            self._cli("JSON.SET", name, "$", big=value or "{}")
        else:
            raise DbError("Unknown kind")
        return f"Key “{name}” created"


# ── documents: MongoDB and the search engines ──────────────────────────────────────────────────────────────────
class DocEdit(Editor):
    unit = "document"
    document = True
    id_field = "_id"

    def annotate(self, node, r):
        r.types = {}
        docs = getattr(r, "docs", None)
        if docs is None or self.id_field not in r.columns:
            r.key, r.edit_why = [], f"These documents have no “{self.id_field}”, so one can't be picked out to edit"
            return
        r.key, r.edit_why = [r.columns.index(self.id_field)], None

    def ident(self, node, r, pos):
        return {self.id_field: r.docs[pos].get(self.id_field)}

    def _doc(self, values):
        text = values.get("document")
        if text is None:
            raise DbError("Nothing changed")
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise DbError(f"That isn't valid JSON: {e}")
        if not isinstance(doc, dict):
            raise DbError("A document must be a JSON object: { … }")
        return doc

    @staticmethod
    def _field(doc, readonly=""):
        return [Field("document", "JSON", json.dumps(doc, indent=2, ensure_ascii=False), json=True, nullable=False,
                      readonly=readonly, multiline=True)]

    def blank(self, node):
        return self._field({})


class MongoEdit(DocEdit):
    can_drop = can_empty = True

    def _run(self, js, doc=None, ident=None):
        extra = {}
        if ident is not None:
            extra["BERTH_ID"] = json.dumps(ident["_id"])
        path = None
        if doc is not None:
            text = json.dumps(doc, ensure_ascii=False)
            if len(text.encode()) > BIG:
                path = upload(self.b, text, "json")
                extra["BERTH_DF"] = path
            else:
                extra["BERTH_D"] = text
        pre = ("const __D = process.env.BERTH_DF ? require('fs').readFileSync(process.env.BERTH_DF, 'utf8') : "
               "process.env.BERTH_D; ")
        try:
            return self.b._js(pre + js, extra=extra)
        finally:
            if path:
                self.b.exec(["rm", "-f", path], timeout=10)

    def _c(self, node):
        return f"db.getCollection({json.dumps(node.ref)})"

    def fetch(self, node, ident):
        doc = self._run(f"print(EJSON.stringify({self._c(node)}.findOne({{_id: EJSON.parse(process.env.BERTH_ID)}}), "
                        "{relaxed: true}))", ident=ident)
        if not doc:
            raise DbError(GONE)
        return self._field(doc)

    def blank(self, node):
        return [Field("document", "JSON — leave out _id to get a new ObjectId", "{\n  \n}", json=True, nullable=False,
                      multiline=True)]

    def save(self, node, ident, values, allow):
        self._ok(allow)
        doc = self._doc(values)
        if ident is None:
            self._run(f"print(EJSON.stringify({self._c(node)}.insertOne(EJSON.parse(__D)).insertedId))", doc)
            return "Document added"
        n = self._run(f"print(EJSON.stringify({self._c(node)}.replaceOne({{_id: EJSON.parse(process.env.BERTH_ID)}}, "
                      "EJSON.parse(__D)).matchedCount))", doc, ident)
        if not n:
            raise DbError(GONE)
        return "Saved"

    def delete(self, node, ident, allow):
        self._ok(allow)
        n = self._run(f"print(EJSON.stringify({self._c(node)}.deleteOne({{_id: EJSON.parse(process.env.BERTH_ID)}})"
                      ".deletedCount))", ident=ident)
        if not n:
            raise DbError(GONE)
        return "Document deleted"

    def empty(self, node, allow):
        self._ok(allow)
        n = self._run(f"print(EJSON.stringify({self._c(node)}.deleteMany({{}}).deletedCount))")
        return f"Removed {int(n or 0):,} documents"

    def drop(self, node, allow):
        self._ok(allow)
        self._run(f"print(EJSON.stringify({self._c(node)}.drop()))")
        return "Collection deleted"


def _q(s):
    return urllib.parse.quote(str(s), safe="")


class OpenSearchEdit(DocEdit):
    can_drop = True

    def fetch(self, node, ident):
        r = self.b.http.json("GET", f"/{_q(node.ref)}/_doc/{_q(ident['_id'])}")
        if not isinstance(r, dict) or not r.get("found"):
            raise DbError(GONE)
        return self._field(r.get("_source") or {})

    def save(self, node, ident, values, allow):
        self._ok(allow)
        doc = self._doc(values)
        if ident is None:
            self.b.http.json("POST", f"/{_q(node.ref)}/_doc?refresh=true", doc)
            return "Document added"
        self.b.http.json("PUT", f"/{_q(node.ref)}/_doc/{_q(ident['_id'])}?refresh=true", doc)
        return "Saved"

    def delete(self, node, ident, allow):
        self._ok(allow)
        self.b.http.json("DELETE", f"/{_q(node.ref)}/_doc/{_q(ident['_id'])}?refresh=true")
        return "Document deleted"

    def drop(self, node, allow):
        self._ok(allow)
        self.b.http.json("DELETE", f"/{_q(node.ref)}")
        return "Index deleted"


class TypesenseEdit(DocEdit):
    id_field = "id"
    can_drop = True

    def _p(self, node, i=None):
        return f"/collections/{_q(node.ref)}/documents" + ("" if i is None else f"/{_q(i)}")

    def fetch(self, node, ident):
        return self._field(self.b.http.json("GET", self._p(node, ident["id"])))

    def save(self, node, ident, values, allow):
        self._ok(allow)
        doc = self._doc(values)
        if ident is None:
            self.b.http.json("POST", self._p(node), doc)
            return "Document added"
        doc.setdefault("id", ident["id"])
        self.b.http.json("POST", self._p(node) + "?action=upsert", doc)
        return "Saved"

    def delete(self, node, ident, allow):
        self._ok(allow)
        self.b.http.json("DELETE", self._p(node, ident["id"]))
        return "Document deleted"

    def drop(self, node, allow):
        self._ok(allow)
        self.b.http.json("DELETE", f"/collections/{_q(node.ref)}")
        return "Collection deleted"


class MeiliEdit(DocEdit):
    can_drop = can_empty = True

    def annotate(self, node, r):
        try:
            self.id_field = (self.b.http.json("GET", f"/indexes/{_q(node.ref)}") or {}).get("primaryKey") or "id"
        except DbError:
            self.id_field = "id"
        super().annotate(node, r)

    def fetch(self, node, ident):
        return self._field(self.b.http.json("GET", f"/indexes/{_q(node.ref)}/documents/{_q(ident[self.id_field])}"))

    def _wait(self, task):
        """Meilisearch applies writes as tasks: wait (briefly) so the reload shows the change."""
        uid = (task or {}).get("taskUid") if isinstance(task, dict) else None
        for _ in range(40):
            if uid is None:
                return
            t = self.b.http.json("GET", f"/tasks/{uid}") or {}
            if t.get("status") == "failed":
                raise DbError(((t.get("error") or {}).get("message")) or "Meilisearch rejected the change")
            if t.get("status") == "succeeded":
                return
            time.sleep(0.25)

    def save(self, node, ident, values, allow):
        self._ok(allow)
        doc = self._doc(values)
        if ident is not None:
            doc.setdefault(self.id_field, ident[self.id_field])
        elif self.id_field not in doc:
            raise DbError(f"Add the “{self.id_field}” field: it's this index's primary key")
        self._wait(self.b.http.json("PUT" if ident is None else "POST", f"/indexes/{_q(node.ref)}/documents", [doc]))
        return "Document added" if ident is None else "Saved"

    def delete(self, node, ident, allow):
        self._ok(allow)
        self._wait(self.b.http.json("DELETE", f"/indexes/{_q(node.ref)}/documents/{_q(ident[self.id_field])}"))
        return "Document deleted"

    def empty(self, node, allow):
        self._ok(allow)
        self._wait(self.b.http.json("DELETE", f"/indexes/{_q(node.ref)}/documents"))
        return "Index emptied"

    def drop(self, node, allow):
        self._ok(allow)
        self._wait(self.b.http.json("DELETE", f"/indexes/{_q(node.ref)}"))
        return "Index deleted"


class VespaEdit(DocEdit):
    id_field = "id"

    @staticmethod
    def _path(docid):
        """id:namespace:doctype::local  →  /document/v1/namespace/doctype/docid/local"""
        parts = str(docid).split(":", 4)
        if len(parts) < 5 or parts[0] != "id":
            raise DbError(f"Unexpected document id: {docid}")
        _id, ns, dtype, _group, local = parts
        return f"/document/v1/{_q(ns)}/{_q(dtype)}/docid/{_q(local)}"

    def fetch(self, node, ident):
        r = self.b.http.json("GET", self._path(ident["id"]))
        return self._field((r or {}).get("fields") or {})

    def blank(self, node):
        return [Field("document", f"JSON — include \"id\": the new document's id in {node.ref}",
                      "{\n  \"id\": \"\",\n  \n}", json=True, nullable=False, multiline=True)]

    def save(self, node, ident, values, allow):
        self._ok(allow)
        doc = self._doc(values)
        if ident is None:
            local = doc.pop("id", None)
            if not local:
                raise DbError("Add an \"id\" for the new document")
            ns = self._namespace(node)
            self.b.http.json("POST", f"/document/v1/{_q(ns)}/{_q(node.ref)}/docid/{_q(local)}", {"fields": doc})
            return "Document added"
        self.b.http.json("PUT", self._path(ident["id"]), {"fields": {k: {"assign": v} for k, v in doc.items()}})
        return "Saved"

    def _namespace(self, node):
        r = self.b.http.json("POST", "/search/", {"yql": f"select documentid from sources {node.ref} where true",
                                                  "hits": 1})
        for h in ((r or {}).get("root") or {}).get("children") or []:
            did = (h.get("fields") or {}).get("documentid") or h.get("id", "")
            if str(did).startswith("id:"):
                return str(did).split(":")[1]
        return node.ref

    def delete(self, node, ident, allow):
        self._ok(allow)
        self.b.http.json("DELETE", self._path(ident["id"]))
        return "Document deleted"


EDITORS = {"postgres": PgEdit, "mysql": MyEdit, "redis": RedisEdit, "mongo": MongoEdit, "opensearch": OpenSearchEdit,
           "typesense": TypesenseEdit, "meilisearch": MeiliEdit, "vespa": VespaEdit}


def editor_for(b):
    cls = EDITORS.get(b.kind)
    return cls(b) if cls and b.write_capable else Editor(b)
