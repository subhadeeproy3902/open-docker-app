"""Whole-table export / import and whole-database backup / restore, through the client tools that ship inside the
database's own image (psql, pg_dump, mysqldump, mongodump, redis-cli). No GTK here: the Backups page, the explorer
and `berth --run-backups` all use it.

Output streams straight to a file on this computer (engine.exec_to_file), so a 5 GB dump never sits in memory.
Input is copied into the container's /tmp first and removed afterwards."""
import csv
import io
import json
import os
import re
import tarfile
import tempfile
import time
import uuid

from .db import DbError, _bq, _qi, _ql

QUOTE_NOTNULL = getattr(csv, "QUOTE_NOTNULL", None)      # Python 3.12+; older: written by hand below


# ── helpers ──
def _put(b, local, ext="bin"):
    """Copy a local file into the container's /tmp (streamed, any size). Returns the path inside."""
    name = f"berth-{uuid.uuid4().hex[:12]}.{ext}"
    fd, tar_path = tempfile.mkstemp(suffix=".tar")
    os.close(fd)
    try:
        with tarfile.open(tar_path, "w") as tf:
            tf.add(local, arcname=name)
        b.engine.put_archive_file(b.cid, "/tmp", tar_path)
    except Exception as e:
        raise DbError(f"Couldn't copy the file into {b.name}: {e}")
    finally:
        os.remove(tar_path)
    return "/tmp/" + name


def _rm(b, path):
    try:
        b.exec(["rm", "-f", path], timeout=20)
    except DbError:
        pass


def _check(code, err, what):
    if code not in (0, None):
        msg = "\n".join(ln for ln in (err or "").strip().splitlines()[-8:])
        raise DbError(f"{what} failed: {msg or f'exit code {code}'}")


def _pg_env(b, readonly=True):
    return {"PGPASSWORD": b.password or "", "PGCONNECT_TIMEOUT": "10",
            "PGOPTIONS": "-c standard_conforming_strings=on" + (" -c default_transaction_read_only=on" if readonly
                                                                 else "")}


def _my_client(b, *names):
    c = b.which(*names)
    if not c:
        raise DbError(f"This image has no {names[0]}")
    return c


def _cell(v):
    """A JSON value as one text cell (SQL import / CSV export)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _csv_line(values):
    """CSV with NULL ≠ empty text: NULL is an empty unquoted field, text is always quoted (PostgreSQL's rule)."""
    return ",".join("" if v is None else '"' + str(v).replace('"', '""') + '"' for v in values) + "\n"


def stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def safe(name):
    return re.sub(r"[^\w.-]+", "_", str(name)).strip("_") or "db"


# ── export a whole table ──
def export_table(b, node, dest, fmt="csv", progress=None):
    """Every row of a table / collection to `dest` as CSV or JSON. Returns the number of rows."""
    nd = dest + ".ndjson.part"
    try:
        cols = _ndjson(b, node, nd, progress)
        n = _ndjson_to(nd, dest, fmt, cols)
    finally:
        if os.path.exists(nd):
            os.remove(nd)
    return n


def _ndjson(b, node, path, progress):
    """One JSON object per line; returns the column order (None: take it from the documents)."""
    eng = b.engine
    if b.kind == "postgres":
        s, t = node.ref
        cols = [r[0] for r in b._rows(f"select attname from pg_attribute where attrelid = {_ql(_qi(s) + '.' + _qi(t))}"
                                      "::regclass and attnum > 0 and not attisdropped order by attnum").rows]
        sql = f"select row_to_json(_t)::text from {_qi(s)}.{_qi(t)} as _t"
        code, err, _n = eng.exec_to_file(b.cid, ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", b.user,
                                                  "-d", b.db, "-c", sql], path, env=_pg_env(b), progress=progress)
        _check(code, err, "Export")
        return cols
    if b.kind == "mysql":
        info = b._run("select column_name, data_type from information_schema.columns where table_schema = database() "
                      "and table_name = '" + node.ref.replace("\\", "\\\\").replace("'", "''") + "' order by "
                      "ordinal_position", raw=True).rows
        from .db_edit import MYSQL_BINARY
        parts = ", ".join(f"'{n.replace(chr(39), chr(39) * 2)}', "
                          + (f"TO_BASE64({_bq(n)})" if (t or "").lower() in MYSQL_BINARY else _bq(n))
                          for n, t in info)
        client = b.client or _my_client(b, "mariadb", "mysql")
        code, err, _n = eng.exec_to_file(b.cid, [client, "--batch", "--raw", "--skip-column-names", "-u", b.user,
                                                  f"--database={b.db}", "-e",
                                                  f"SELECT JSON_OBJECT({parts}) FROM {_bq(node.ref)}"],
                                         path, env={"MYSQL_PWD": b.password or ""}, progress=progress)
        _check(code, err, "Export")
        return [n for n, _t in info]
    if b.kind == "mongo":
        from .db import MONGO_AUTH
        if b.password is None:
            b.password = b.secret("MONGO_INITDB_ROOT_PASSWORD", "MONGODB_ROOT_PASSWORD")
        env = {"BERTH_U": b.user, "BERTH_P": b.password} if b.user else {}
        js = (MONGO_AUTH + f"db.getCollection({json.dumps(node.ref)}).find().forEach(d => "
              "print(EJSON.stringify(d, {relaxed: true})))")
        code, err, _n = eng.exec_to_file(b.cid, ["mongosh", "mongodb://127.0.0.1:27017/" + b.db, "--quiet", "--norc",
                                                  "--eval", js], path, env=env or None, progress=progress)
        _check(code, err, "Export")
        return None
    if b.kind == "sqlite":
        cur = b.con.execute(f"select * from {_qi(node.ref)}")
        names = [d[0] for d in cur.description]
        with open(path, "w", encoding="utf-8") as f:
            for row in cur:
                f.write(json.dumps({k: (v.hex() if isinstance(v, bytes) else v) for k, v in zip(names, row)},
                                   ensure_ascii=False) + "\n")
        return names
    raise DbError("Exporting a whole table isn't available for this engine")


def _ndjson_to(nd, dest, fmt, cols):
    def rows():
        with open(nd, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
    if cols is None:                                     # documents: the union of their top-level fields
        seen = {}
        for d in rows():
            for k in d:
                seen.setdefault(k, None)
        cols = list(seen)
    n = 0
    with open(dest, "w", encoding="utf-8", newline="") as out:
        if fmt == "json":
            out.write("[")
            for d in rows():
                if cols:                                  # the table's column order (MySQL sorts JSON keys)
                    d = {c: d.get(c) for c in cols if c in d} | {k: v for k, v in d.items() if k not in cols}
                out.write(("\n" if n == 0 else ",\n") + json.dumps(d, ensure_ascii=False))
                n += 1
            out.write("\n]\n")
        else:
            out.write(_csv_line(cols))
            for d in rows():
                out.write(_csv_line([_cell(d.get(c)) for c in cols]))
                n += 1
    return n


# ── import into a table ──
def read_rows(path):
    """Rows from a CSV (with a header), a JSON array or JSON lines file: (columns, [dict, …])."""
    with open(path, "rb") as f:
        head = f.read(4096).lstrip()
    if head[:1] in (b"[", b"{"):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        try:
            data = json.loads(text)
            docs = data if isinstance(data, list) else [data]
        except ValueError:
            docs = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        docs = [d for d in docs if isinstance(d, dict)]
        cols = []
        for d in docs:
            for k in d:
                if k not in cols:
                    cols.append(k)
        return cols, docs
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(65536)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rd = csv.reader(f, dialect)
        cols = next(rd, [])
        docs = [dict(zip(cols, r)) for r in rd if any(r)]
    return cols, docs


def _pg_array(v):
    """A JSON list as a PostgreSQL array literal: [1, "a b", null] → {1,"a b",NULL}."""
    def el(x):
        if x is None:
            return "NULL"
        if isinstance(x, list):
            return _pg_array(x)
        if isinstance(x, bool):
            return "true" if x else "false"
        if isinstance(x, (int, float)):
            return str(x)
        t = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
        return '"' + t.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "{" + ",".join(el(x) for x in v) + "}"


def _pg_types(b, node):
    s, t = node.ref
    return dict(b._rows(f"select attname, format_type(atttypid, atttypmod) from pg_attribute where attrelid = "
                        f"{_ql(_qi(s) + '.' + _qi(t))}::regclass and attnum > 0 and not attisdropped").rows)


def table_columns(b, node):
    if b.kind == "postgres":
        s, t = node.ref
        return [r[0] for r in b._rows(f"select attname from pg_attribute where attrelid = {_ql(_qi(s) + '.' + _qi(t))}"
                                      "::regclass and attnum > 0 and not attisdropped and attgenerated = '' "
                                      "order by attnum").rows]
    if b.kind == "mysql":
        return [r[0] for r in b._run("select column_name from information_schema.columns where table_schema = "
                                     "database() and table_name = '" + node.ref.replace("\\", "\\\\").replace("'", "''")
                                     + "' and extra not like '%GENERATED%' order by ordinal_position", raw=True).rows]
    return None


def import_rows(b, node, path, replace=False, allow=False):
    """Add the file's rows to the table (or replace its contents). All or nothing. Returns (rows, ignored columns)."""
    b._need_write(allow)
    cols, docs = read_rows(path)
    with open(path, "rb") as f:
        is_csv = f.read(4096).lstrip()[:1] not in (b"[", b"{")      # CSV: an empty field means NULL
    if not docs:
        raise DbError("The file has no rows")
    if b.kind in ("postgres", "mysql"):
        have = table_columns(b, node)
        use = [c for c in cols if c in have]
        if not use:
            raise DbError(f"None of the file's columns ({', '.join(cols[:8])}) are in {node.label}")
        ignored = [c for c in cols if c not in have]
        fd, tmp = tempfile.mkstemp(suffix=".csv" if b.kind == "postgres" else ".sql")
        os.close(fd)
        try:
            if b.kind == "postgres":
                types = _pg_types(b, node)
                arrays = {c for c in use if types.get(c, "").endswith("[]")}

                def conv(c, x):
                    if c in arrays:                      # exported arrays come back as JSON lists
                        if isinstance(x, str) and x.startswith("["):
                            try:
                                x = json.loads(x)
                            except ValueError:
                                pass
                        if isinstance(x, list):
                            return _pg_array(x)
                    return _cell(x)
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(_csv_line(use))
                    for d in docs:
                        v = [conv(c, d.get(c)) for c in use]
                        f.write(_csv_line([None if is_csv and x == "" else x for x in v]))
                inside = _put(b, tmp, "csv")
                s, t = node.ref
                target = f"{_qi(s)}.{_qi(t)} ({', '.join(map(_qi, use))})"
                cmd = ["psql", "-X", "-q", "-1", "-v", "ON_ERROR_STOP=1", "-U", b.user, "-d", b.db]
                if replace:
                    cmd += ["-c", f"delete from {_qi(s)}.{_qi(t)}"]
                cmd += ["-c", f"\\copy {target} from '{inside}' with (format csv, header)"]
                code, out, err = b.engine.exec_run(b.cid, cmd, limit=65536, timeout=3600, env=_pg_env(b, False))
                _rm(b, inside)
                _check(code, err or out, "Import")
            else:
                from .db_edit import MyEdit
                lit = MyEdit._lit
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write("START TRANSACTION;\n")
                    if replace:
                        f.write(f"DELETE FROM {_bq(node.ref)};\n")
                    for i in range(0, len(docs), 500):
                        vals = ",\n".join("(" + ", ".join(lit(None if is_csv and d.get(c) == "" else _cell(d.get(c)))
                                                          for c in use) + ")" for d in docs[i:i + 500])
                        f.write(f"INSERT INTO {_bq(node.ref)} ({', '.join(map(_bq, use))}) VALUES\n{vals};\n")
                    f.write("COMMIT;\n")
                inside = _put(b, tmp, "sql")
                try:
                    b._run("", raw=True, file=inside)
                finally:
                    _rm(b, inside)
        finally:
            os.remove(tmp)
        return len(docs), ignored
    if b.kind == "mongo":
        fd, tmp = tempfile.mkstemp(suffix=".ndjson")
        os.close(fd)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for d in docs:
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")
            inside = _put(b, tmp, "ndjson")
            js = (f"const c = db.getCollection({json.dumps(node.ref)}); "
                  + ("c.deleteMany({}); " if replace else "")
                  + "const L = require('fs').readFileSync(process.env.BERTH_F, 'utf8').split('\\n').filter(x => x.trim())"
                  ".map(x => EJSON.parse(x)); let n = 0; for (let i = 0; i < L.length; i += 1000) "
                  "{ n += Object.keys(c.insertMany(L.slice(i, i + 1000)).insertedIds).length } print(EJSON.stringify(n))")
            try:
                b._js(js, timeout=3600, extra={"BERTH_F": inside})
            finally:
                _rm(b, inside)
        finally:
            os.remove(tmp)
        return len(docs), []
    raise DbError("Importing isn't available for this engine")


# ── back up / restore a whole database ──
BACKUP_EXT = {"postgres": "dump", "mysql": "sql", "mongo": "archive.gz", "redis": "rdb"}


def can_backup(b):
    return b.kind in BACKUP_EXT


def backup_name(b):
    db = "" if b.kind == "redis" else f"-{safe(b.db)}"
    return f"{safe(b.name)}{db}-{stamp()}.{BACKUP_EXT[b.kind]}"


def backup(b, dest, progress=None):
    """The whole current database to `dest`. Returns bytes written."""
    eng = b.engine
    b.connect()
    if b.kind == "postgres":
        code, err, n = eng.exec_to_file(b.cid, ["pg_dump", "-Fc", "-U", b.user, "-d", b.db], dest,
                                        env={"PGPASSWORD": b.password or "", "PGCONNECT_TIMEOUT": "10"},
                                        progress=progress)
        _check(code, err, "Backup")
        return n
    if b.kind == "mysql":
        dump = _my_client(b, "mariadb-dump", "mysqldump")
        code, err, n = eng.exec_to_file(b.cid, [dump, "--single-transaction", "--routines", "--triggers", "--events",
                                                 "--hex-blob", "-u", b.user, "--databases", b.db], dest,
                                        env={"MYSQL_PWD": b.password or ""}, progress=progress)
        _check(code, err, "Backup")
        return n
    if b.kind == "mongo":
        _my_client(b, "mongodump")
        cfg = _mongo_cfg(b)
        try:
            code, err, n = eng.exec_to_file(b.cid, ["mongodump", "--quiet", "--archive", "--gzip", f"--db={b.db}"]
                                            + cfg, dest, progress=progress)
        finally:
            _mongo_cfg_rm(b, cfg)
        _check(code, err, "Backup")
        return n
    if b.kind == "redis":
        tmp = f"/tmp/berth-{uuid.uuid4().hex[:12]}.rdb"
        code, out, err = eng.exec_run(b.cid, [b.cli or b.which("redis-cli", "valkey-cli"), "--rdb", tmp],
                                      limit=65536, timeout=3600, env=b._auth())
        try:
            _check(code, err or out, "Backup")
            code, err, n = eng.exec_to_file(b.cid, ["cat", tmp], dest, progress=progress)
            _check(code, err, "Backup")
        finally:
            _rm(b, tmp)
        return n
    raise DbError("Backups aren't available for this engine")


def _mongo_cfg(b):
    """mongodump / mongorestore read the password from a config file, never the command line."""
    if b.password is None:
        b.password = b.secret("MONGO_INITDB_ROOT_PASSWORD", "MONGODB_ROOT_PASSWORD")
    if not b.user:
        return []
    fd, tmp = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write("password: " + json.dumps(b.password or "") + "\n")
    try:
        inside = _put(b, tmp, "yaml")
    finally:
        os.remove(tmp)
    return [f"--username={b.user}", "--authenticationDatabase=admin", f"--config={inside}"]


def _mongo_cfg_rm(b, cfg):
    for a in cfg:
        if a.startswith("--config="):
            _rm(b, a.split("=", 1)[1])


def restore(b, src, allow=False):
    """Put a backup back into the current database. Objects in the backup replace the ones with the same names."""
    b._need_write(allow)
    eng = b.engine
    b.connect()
    if b.kind == "redis":
        return _restore_redis(b, src)
    inside = _put(b, src, BACKUP_EXT.get(b.kind, "bin"))
    try:
        if b.kind == "postgres":
            with open(src, "rb") as f:
                plain = f.read(5) != b"PGDMP"
            cmd = (["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", b.user, "-d", b.db, "-f", inside] if plain else
                   ["pg_restore", "--clean", "--if-exists", "--no-owner", "-U", b.user, "-d", b.db, inside])
            code, out, err = eng.exec_run(b.cid, cmd, limit=262144, timeout=7200, env=_pg_env(b, False))
            _check(code, err or out, "Restore")
        elif b.kind == "mysql":
            b._run("", raw=True, file=inside)
        elif b.kind == "mongo":
            _my_client(b, "mongorestore")
            cfg = _mongo_cfg(b)
            try:
                code, out, err = eng.exec_run(b.cid, ["mongorestore", "--quiet", f"--archive={inside}", "--gzip",
                                                      "--drop"] + cfg, limit=262144, timeout=7200)
            finally:
                _mongo_cfg_rm(b, cfg)
            _check(code, err or out, "Restore")
        else:
            raise DbError("Restoring isn't available for this engine")
    finally:
        _rm(b, inside)


def _restore_redis(b, src):
    """Redis reads its data file only at start: stop it, put the file in place, start it again."""
    conf = {}
    for key in ("dir", "dbfilename", "appendonly"):
        v = b._val("CONFIG", "GET", key)
        if isinstance(v, dict):
            conf[key] = v.get(key, "")
        elif isinstance(v, list) and len(v) >= 2:
            conf[key] = v[1]
    if str(conf.get("appendonly", "no")).lower() == "yes":
        raise DbError("This Redis keeps its data in an append-only file (AOF), which a .rdb backup doesn't replace. "
                      "Turn appendonly off, or restore it by hand.")
    folder, name = conf.get("dir") or "/data", conf.get("dbfilename") or "dump.rdb"
    eng = b.engine
    fd, tar_path = tempfile.mkstemp(suffix=".tar")
    os.close(fd)
    try:
        with tarfile.open(tar_path, "w") as tf:
            tf.add(src, arcname=name)
        eng.action(b.cid, "stop")
        try:
            eng.put_archive_file(b.cid, folder, tar_path)
        finally:
            eng.action(b.cid, "start")
    finally:
        os.remove(tar_path)
    for _ in range(30):                                  # wait until it answers again
        try:
            b.ins = eng.inspect(b.cid)
            b._cli("PING", raw=True)
            return
        except Exception:
            time.sleep(1)
