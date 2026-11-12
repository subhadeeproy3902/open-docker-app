"""Integration test for the Connect features (no GUI): the database explorer backends (#25), local addresses through
the proxy (#26) and public links through Cloudflare Tunnel (#27), against a REAL engine.

Everything it creates is named berth-test-* / labelled io.berth.test=1, lives on its own network, and is removed at the
end (also after a failure). Databases you already run are only touched read-only, and only with --real.

    python3 tests/connect.py                 # all throwaway engines + proxy + tunnel
    python3 tests/connect.py --no-tunnel     # skip the public-internet test
    python3 tests/connect.py --real          # also read-only checks on your own database containers
    python3 tests/connect.py --only postgres,redis

DOCKER_HOST selects the engine, like the docker CLI. Needs internet to pull images (and for the tunnel)."""
import http.client
import io
import json
import os
import socket
import sqlite3
import ssl
import sys
import tarfile
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from berth import db, sharing  # noqa: E402
from berth.engine import Engine, EngineError  # noqa: E402

ARGS = sys.argv[1:]
ONLY = next((a.split("=", 1)[1] if "=" in a else ARGS[ARGS.index(a) + 1] for a in ARGS if a.startswith("--only")), "")
ONLY = set(filter(None, ONLY.split(",")))
NET, LABEL = "berth-test-net", {"io.berth.test": "1"}
eng = Engine(os.environ.get("DOCKER_HOST", "").replace("unix://", "") or None)
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  ({str(detail)[:150]})" if detail else ""), flush=True)
    return ok


def raises(fn, *a, contains=""):
    try:
        fn(*a)
    except (db.DbError, sharing.ShareError) as e:
        return contains.lower() in str(e).lower(), str(e)
    return False, "no error"


def want(kind):
    return not ONLY or kind in ONLY


# ── containers ──────────────────────────────────────────────────────────────────────────────────────────────────
def pull(ref):
    if not sharing.has_image(eng, ref):
        print(f"… pulling {ref}", flush=True)
        sharing.ensure_image(eng, ref)


def run(name, image, env=None, cmd=None, network=NET, ports=None, binds=None):
    pull(image)
    full = f"berth-test-{name}"
    for c in eng.containers_filtered({"name": [f"^/{full}$"]}):
        eng.remove_container(c["Id"], force=True)
    hc = {"NetworkMode": network, "Binds": binds or []}
    cfg = {"Image": image, "Env": [f"{k}={v}" for k, v in (env or {}).items()], "Labels": LABEL, "HostConfig": hc}
    if cmd:
        cfg["Cmd"] = cmd
    if ports:
        cfg["ExposedPorts"] = {f"{p}/tcp": {} for p in ports}
        hc["PortBindings"] = {f"{p}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(h)}] for p, h in ports.items()}
    r = eng.create_container(full, cfg)
    eng.action(r["Id"], "start")
    return r["Id"]


def wait(cid, probe, timeout=180, what=""):
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        try:
            if probe():
                return True
        except Exception as e:     # not ready yet
            last = e
        time.sleep(2)
    raise RuntimeError(f"{what or cid[:12]} not ready after {timeout}s: {last}")


def backend(cid):
    return db.open_backend(eng, eng.inspect(cid))


PROXY_VOLUME_EXISTED = None


def cleanup():
    if PROXY_VOLUME_EXISTED is False:          # the test created the proxy's CA volume: remove it again
        try:
            sharing.stop_proxy(eng, remove=True)
            eng.remove_volume(sharing.PROXY_DATA)
        except Exception:
            pass
    for c in eng.containers_filtered({"label": ["io.berth.test=1"]}):
        try:
            eng.remove_container(c["Id"], force=True)
        except Exception:
            pass
    for c in sharing.tunnels(eng.containers_filtered({"label": [f"{sharing.ROLE}=tunnel"]})):
        if (c.get("Labels") or {}).get(sharing.TARGET, "").startswith("berth-test-"):
            eng.remove_container(c["Id"], force=True)
    try:
        eng.remove_volume("berth-test-sqlite")
    except Exception:
        pass
    for n in eng.networks():
        if n["Name"] == NET:
            try:
                eng.remove_network(n["Id"])
            except Exception:
                pass


def ensure_net():
    if not any(n["Name"] == NET for n in eng.networks()):
        eng.req("POST", "/networks/create", body={"Name": NET, "Labels": LABEL})


# ── the database engines ────────────────────────────────────────────────────────────────────────────────────────
def common(b, kind, node_label, min_rows):
    desc = b.connect()
    check(f"{kind}: connects", desc, desc)
    nodes = b.tree()
    flat = []
    for n in nodes:
        flat += [n] if n.leaf else b.children(n)
    node = next((n for n in flat if n.label == node_label), None)
    check(f"{kind}: tree lists {node_label}", node, [n.label for n in flat][:8])
    if not node:
        return None
    r = b.browse(node, 0, 2)
    check(f"{kind}: browse pages (limit 2)", len(r.rows) == min(2, min_rows) and r.columns, f"{r} {r.note}")
    r2 = b.browse(node, 2, 2)
    check(f"{kind}: browse page 2 differs", min_rows <= 2 or (r2.rows and r2.rows != r.rows), r2)
    st = b.structure(node)
    check(f"{kind}: structure", st.rows or st.text, st.note or (st.text or "")[:80])
    return node




# ── editing (berth.db_edit): open a row in full, save, add, delete — through the same clients as browsing ──
def _ed(b, node):
    from berth import db_edit
    ed = db_edit.editor_for(b)
    r = b.browse(node, 0, 200)
    ed.annotate(node, r)
    return ed, r


def _vals(ed, node, ident):
    return {f.name: f for f in ed.fetch(node, ident)}


def edit_sql(kind, b, node, view=None, nokey=None):
    from berth.db_edit import DEFAULT
    ed, r = _ed(b, node)
    check(f"edit {kind}: rows are picked out by the primary key", r.edit_why is None and
          [r.columns[i] for i in r.key] == ["id"], (r.edit_why, r.key))
    pos = next(i for i, row in enumerate(r.rows) if row[r.columns.index("item")] not in (None, "new"))
    ident = ed.ident(node, r, pos)
    v = _vals(ed, node, ident)
    check(f"edit {kind}: a row opens in full with types", v["id"].pk and v["item"].type and "note" in v, list(v))
    ok, msg = raises(ed.save, node, ident, {"item": "x"}, False, contains="allow changes")
    check(f"edit {kind}: saving needs “Allow changes”", ok, msg)
    tricky = "it's \"quoted\" \\ back\nline two; drop table orders; --"
    ed.save(node, ident, {"note": tricky}, True)
    v = _vals(ed, node, ident)
    check(f"edit {kind}: quotes, backslashes, newlines and SQL in a value are stored as text", v["note"].value == tricky,
          repr(v["note"].value)[:80])
    ed.save(node, ident, {"note": None}, True)
    check(f"edit {kind}: NULL", _vals(ed, node, ident)["note"].value is None)
    ed.save(node, ident, {"note": "NULL"}, True)
    check(f"edit {kind}: the text “NULL” stays text", _vals(ed, node, ident)["note"].value == "NULL")
    big = "z" * 300_000
    ed.save(node, ident, {"note": big}, True)
    check(f"edit {kind}: a 300 KB value is saved (through a file)", _vals(ed, node, ident)["note"].value == big)
    ed.save(node, None, {"id": DEFAULT, "item": "new", "note": DEFAULT}, True)
    n = b.query("select count(*) from orders where item = 'new'").rows
    check(f"edit {kind}: add a row (untouched fields get their default)", n == [["1"]], n)
    ok, msg = raises(ed.save, node, ident, {"id": "not a number"}, True)
    check(f"edit {kind}: database errors come back to the user", ok and msg, msg)
    ed.delete(node, ident, True)
    ok, msg = raises(ed.delete, node, ident, True, contains="isn't there")
    check(f"edit {kind}: delete, then a stale delete says the row is gone", ok, msg)
    ok, msg = raises(ed.save, node, {"id": "1 or 1=1"}, {"note": "pwned"}, True)
    hit = b.query("select count(*) from orders where note = 'pwned'").rows
    check(f"edit {kind}: a key value can't widen the WHERE to other rows", int(hit[0][0]) <= 1, (msg, hit))
    if view:
        _ed2, rv = _ed(b, view)
        check(f"edit {kind}: views are read-only with a reason", rv.edit_why and "view" in rv.edit_why.lower(), rv.edit_why)
    if nokey:
        _ed3, rn = _ed(b, nokey)
        check(f"edit {kind}: tables without a primary key are read-only with a reason",
              rn.edit_why and "primary key" in rn.edit_why, rn.edit_why)
    if nokey:
        ed.empty(nokey, True)
        check(f"edit {kind}: empty a table", b.query("select count(*) from loose").rows == [["0"]])


def t_postgres():
    cid = run("pg", "postgres:16-alpine", {"POSTGRES_PASSWORD": "pw", "POSTGRES_USER": "app", "POSTGRES_DB": "shop"})
    b = backend(cid)
    wait(cid, lambda: b.connect(), what="postgres")
    b.query("create table orders(id serial primary key, item text not null, note text, qty int default 1);"
            "insert into orders(item, note, qty) select 'item ' || g, case when g % 2 = 0 then null else "
            "'line one' || chr(10) || 'line, \"two\"' end, g from generate_series(1, 5) g;", allow_write=True)
    node = common(b, "postgres", "orders", 5)
    r = b.browse(node, 0, 5)
    notes = [row[2] for row in r.rows]
    check("postgres: NULL vs text, newlines and quotes survive", None in notes and 'line one\nline, "two"' in notes,
          notes[:2])
    ok, msg = raises(b.query, "insert into orders(item) values ('x')", contains="read-only")
    check("postgres: read-only enforced by the server", ok, msg)
    ok, msg = raises(b.query, "drop table orders", contains="read-only")
    check("postgres: DDL refused in read-only mode", ok, msg)
    check("postgres: destructive statement flagged", b.is_destructive("delete from orders") and
          not b.is_destructive("delete from orders where id = 1"))
    check("postgres: databases listed", "shop" in b.databases(), b.databases())
    q = b.query("select count(*) as n from orders")
    check("postgres: query", q.rows == [["5"]], q.rows)
    b.query("create view v_orders as select * from orders; create table loose(a int); insert into loose values (1)",
            allow_write=True)
    flat = {n.label: n for sch in b.tree() for n in b.children(sch)}
    edit_sql("postgres", b, flat["orders"], view=flat["v_orders"], nokey=flat["loose"])
    b.sort, b.where = ("qty", True), "qty >= 2"
    r = b.browse(flat["orders"], 0, 50)
    qs = [int(x[r.columns.index("qty")]) for x in r.rows if x[r.columns.index("qty")] is not None]
    check("postgres: sort by a column (numeric, descending) and filter rows", qs == sorted(qs, reverse=True) and
          min(qs) >= 2, qs)
    b.where = "1=1; drop table orders"
    ok, msg = raises(b.browse, flat["orders"], 0, 5, contains="one condition")
    check("postgres: a filter can't carry a second statement", ok, msg)
    b.sort, b.where = None, ""
    b.query("create table files(id int primary key, data bytea); insert into files values (1, decode(repeat('00ff', 600000), "
            "'hex'))", allow_write=True)
    flat = {n.label: n for sch in b.tree() for n in b.children(sch)}
    ed, r = _ed(b, flat["files"])
    f = _vals(ed, flat["files"], ed.ident(flat["files"], r, 0))["data"]
    check("edit postgres: binary opens as size + preview, not editable", f.binary and f.binary[0] == 1_200_000 and
          f.binary[1].startswith("00ff") and f.readonly, (f.binary and f.binary[0], f.readonly))
    blob = ed.blob(flat["files"], {"id": "1"}, "data")
    check("edit postgres: binary saved to a file in full", blob == bytes.fromhex("00ff" * 600000), len(blob))


def t_mysql():
    cid = run("my", "mariadb:11", {"MARIADB_ROOT_PASSWORD": "pw", "MARIADB_DATABASE": "shop"})
    b = backend(cid)
    wait(cid, lambda: b.connect(), what="mariadb")
    b.query("create table orders(id int auto_increment primary key, item varchar(50), note longtext);"
            "insert into orders(item, note) values ('a', NULL), ('b', 'tab\\there'), ('c', 'x'), ('d', 'NULLish');",
            allow_write=True)
    node = common(b, "mysql", "orders", 4)
    r = b.browse(node, 0, 4)
    check("mysql: NULL and escaped tab decoded", r.rows[0][2] is None and r.rows[1][2] == "tab\there", r.rows[:2])
    ok, msg = raises(b.query, "insert into orders(item) values ('x')", contains="read-only")
    check("mysql: write refused in read-only mode", ok, msg)
    q = b.query("select count(*) n from orders")
    check("mysql: query", q.rows == [["4"]], q.rows)
    b.query("create view v_orders as select * from orders; create table loose(a int); insert into loose values (1)",
            allow_write=True)
    flat = {n.label: n for n in b.tree()}
    edit_sql("mysql", b, flat["orders"], view=flat["v_orders"], nokey=flat["loose"])
    from berth import db_edit
    ed, r = _ed(b, flat["orders"])
    ok, msg = raises(ed.save, flat["orders"], ed.ident(flat["orders"], r, 0), {"item": "y" * 120_000}, True,
                     contains="too long")
    check("edit mysql: a value too long for its column is refused, also through a file", ok, msg)


def t_redis():
    cid = run("redis", "redis:7-alpine", cmd=["redis-server", "--requirepass", "s3cret"])
    b = backend(cid)
    wait(cid, lambda: b.connect(), what="redis")
    b._cli("SET", "greeting", "hello\nworld")             # a value with a newline, set directly
    b.query("HSET user:1 name Ada lang Python\nRPUSH queue a b c\nSADD tags x y\nZADD board 10 alice 20 bob\n"
            "XADD events * kind click\nEXPIRE greeting 600", allow_write=True)
    common(b, "redis", "user:1", 2)
    keys = {n.label: n for n in b.tree()}
    check("redis: types and TTL in the tree", keys["greeting"].detail.startswith("string · expires"), keys["greeting"].detail)
    g = b.browse(keys["greeting"])
    check("redis: string value with newline", g.rows == [["hello\nworld"]], g.rows)
    z = b.browse(keys["board"])
    check("redis: sorted set with scores", z.rows[0][0] == "alice" and z.rows[0][1].startswith("10"), z.rows)
    lst = b.browse(keys["queue"], 1, 2)
    check("redis: list paging", [r[1] for r in lst.rows] == ["b", "c"], lst.rows)
    s = b.browse(keys["events"])
    check("redis: stream entries", s.rows and "click" in s.rows[0][1], s.rows)
    ok, msg = raises(b.query, "SET x 1", contains="read-only")
    check("redis: write refused in read-only mode", ok, msg)
    ok, msg = raises(b.query, "CONFIG SET maxmemory 1", contains="read-only")
    check("redis: CONFIG SET refused, CONFIG GET allowed", ok and b.query("CONFIG GET maxmemory").text, msg)
    check("redis: FLUSHALL flagged destructive", b.is_destructive("flushall"))
    b.pattern = "user:*"
    check("redis: key pattern", [n.label for n in b.tree()] == ["user:1"])
    b.pattern = "*"
    keys = {n.label: n for n in b.tree()}
    ed, r = _ed(b, keys["user:1"])
    ident = ed.ident(keys["user:1"], r, [x[0] for x in r.rows].index("name"))
    check("edit redis: hash fields are picked out by name", r.edit_why is None and ident == {"field": "name"}, ident)
    ed.save(keys["user:1"], ident, {"value": "Ada Lovelace"}, True)
    ed.save(keys["user:1"], {"field": "name"}, {"field": "full_name"}, True)
    h = b._val("HGETALL", "user:1")
    check("edit redis: hash value saved and field renamed", h.get("full_name") == "Ada Lovelace" and "name" not in h, h)
    ed.save(keys["user:1"], None, {"field": "age", "value": "36"}, True)
    ok, msg = raises(ed.save, keys["user:1"], None, {"field": "age", "value": "1"}, True, contains="already exists")
    check("edit redis: add a field; adding it twice is refused", ok, msg)
    ed.delete(keys["user:1"], {"field": "age"}, True)
    check("edit redis: delete a field", "age" not in b._val("HGETALL", "user:1"))
    ed, r = _ed(b, keys["queue"])
    ed.save(keys["queue"], {"index": "1"}, {"value": "B"}, True)
    ed.save(keys["queue"], None, {"value": "d"}, True)
    ed.delete(keys["queue"], {"index": "0"}, True)
    check("edit redis: list set / append / delete by position", b._val("LRANGE", "queue", "0", "-1") == ["B", "c", "d"],
          b._val("LRANGE", "queue", "0", "-1"))
    ed, r = _ed(b, keys["board"])
    ed.save(keys["board"], {"member": "alice"}, {"score": "99"}, True)
    ed.save(keys["board"], {"member": "bob"}, {"member": "robert"}, True)
    zs = b.query("ZRANGE board 0 -1 WITHSCORES").text
    check("edit redis: sorted set score and member edits", "robert" in zs and "99" in zs and "bob\n" not in zs, zs)
    ed, r = _ed(b, keys["tags"])
    ed.save(keys["tags"], {"member": "x"}, {"member": "z"}, True)
    check("edit redis: set member renamed", sorted(b._val("SMEMBERS", "tags")) == ["y", "z"], b._val("SMEMBERS", "tags"))
    ed, r = _ed(b, keys["events"])
    ed.save(keys["events"], None, {"fields": '{"kind": "view", "n": 2}'}, True)
    check("edit redis: stream entry added", b._val("XLEN", "events") == 2)
    ed, r = _ed(b, keys["greeting"])
    ttl0 = int(b._val("TTL", "greeting"))
    ed.save(keys["greeting"], {}, {"value": "hi\nthere"}, True)
    check("edit redis: string saved and its expiry kept", b._val("GET", "greeting") == "hi\nthere"
          and int(b._val("TTL", "greeting")) > 0 and ttl0 > 0)
    ed.save(keys["greeting"], {}, {"value": "q" * 300_000}, True)
    check("edit redis: 300 KB string saved (through a file)", int(b._val("STRLEN", "greeting")) == 300_000)
    ed.expire(keys["greeting"], None, True)
    check("edit redis: expiry removed", int(b._val("TTL", "greeting")) == -1)
    ed.new_key("fresh", "hash", "v1", True)
    check("edit redis: new key", b._val("HGET", "fresh", "field") == "v1")
    ed.drop(db.Node("fresh", "key", "fresh"), True)
    check("edit redis: key deleted", int(b._val("EXISTS", "fresh")) == 0)


def t_mongo():
    cid = run("mongo", "mongo:7", {"MONGO_INITDB_ROOT_USERNAME": "root", "MONGO_INITDB_ROOT_PASSWORD": "pw"})
    b = backend(cid)
    wait(cid, lambda: b.connect(), what="mongo")
    b.db = "shop"
    b.query('db.orders.insertMany([{item: "a", qty: 1, tags: ["x"]}, {item: "b", qty: 2, meta: {color: "red"}}, '
            '{item: "c", qty: 3}])', allow_write=True)
    common(b, "mongo", "orders", 3)
    b.filter = '{"qty": {"$gte": 2}}'
    node = next(n for n in b.tree() if n.label == "orders")
    r = b.browse(node)
    check("mongo: JSON filter", len(r.rows) == 2 and "meta" in r.columns, r.columns)
    b.filter = '{"$where": "true"}'
    ok, msg = raises(b.browse, node, contains="javascript")
    check("mongo: $where refused", ok, msg)
    b.filter = '{"item": db.orders.drop()}'
    ok, msg = raises(b.browse, node, contains="json")
    check("mongo: code in a filter is refused (filter is data)", ok, msg)
    b.filter = "{}"
    check("mongo: collection survived", len(b.browse(node).rows) == 3)
    ok, msg = raises(b.query, "db.orders.drop()", contains="read-only")
    check("mongo: shell needs Allow changes", ok, msg)
    check("mongo: drop() flagged destructive", b.is_destructive("db.orders.drop()"))
    ed, r = _ed(b, node)
    check("edit mongo: documents are picked out by _id", r.edit_why is None and r.key == [r.columns.index("_id")],
          r.edit_why)
    pos = [row[r.columns.index("item")] for row in r.rows].index("a")
    ident = ed.ident(node, r, pos)
    doc = json.loads(_vals(ed, node, ident)["document"].value)
    check("edit mongo: a document opens as JSON", doc.get("item") == "a" and "$oid" in json.dumps(doc["_id"]), doc)
    doc["qty"] = 42
    doc["note"] = "it's \"fine\"\n$where"
    ed.save(node, ident, {"document": json.dumps(doc)}, True)
    d2 = json.loads(_vals(ed, node, ident)["document"].value)
    check("edit mongo: replace the document", d2["qty"] == 42 and d2["note"] == doc["note"], d2)
    ok, msg = raises(ed.save, node, ident, {"document": "{not json"}, True, contains="json")
    check("edit mongo: invalid JSON is refused before sending", ok, msg)
    ed.save(node, None, {"document": '{"item": "z", "big": "' + "w" * 200_000 + '"}'}, True)
    check("edit mongo: insert (a 200 KB document goes through a file)", len(b.browse(node).rows) == 4)
    ed.delete(node, ident, True)
    ok, msg = raises(ed.delete, node, ident, True, contains="isn't there")
    check("edit mongo: delete, then a stale delete says it's gone", ok, msg)


def http_seed(b, method, path, body=None):
    return b.http.json(method, path, body)


def t_opensearch():
    cid = run("os", "opensearchproject/opensearch:2", {"discovery.type": "single-node", "DISABLE_SECURITY_PLUGIN": "true",
                                                       "OPENSEARCH_JAVA_OPTS": "-Xms384m -Xmx384m",
                                                       "DISABLE_INSTALL_DEMO_CONFIG": "true"})
    b = backend(cid)
    wait(cid, lambda: b.connect(), timeout=240, what="opensearch")
    for i in range(3):
        http_seed(b, "PUT", f"/products/_doc/{i}?refresh=true", {"name": f"thing {i}", "price": i * 10,
                                                                 "dims": {"w": i}})
    common(b, "opensearch", "products", 3)
    r = b.query('POST /products/_search\n{"query": {"range": {"price": {"gte": 10}}}}')
    check("opensearch: _search in the console → grid", len(r.rows) == 2 and "price" in r.columns, r)
    ok, msg = raises(b.query, "DELETE /products", contains="read-only")
    check("opensearch: DELETE refused in read-only mode", ok, msg)

    node = next(n for n in b.tree() if n.label == "products")
    ed, r = _ed(b, node)
    check("edit opensearch: documents are picked out by _id", r.edit_why is None, r.edit_why)
    ident = ed.ident(node, r, 0)
    doc = json.loads(_vals(ed, node, ident)["document"].value)
    doc["edited"] = "yes"
    ed.save(node, ident, {"document": json.dumps(doc)}, True)
    check("edit opensearch: save a document", json.loads(_vals(ed, node, ident)["document"].value).get("edited") == "yes")
    ed.save(node, None, {"document": json.dumps({"name": "new", "price": 5})}, True)
    ed.delete(node, ident, True)
    time.sleep(1)
    r2 = b.browse(node)
    check("edit opensearch: add one and delete one", len(r2.rows) == len(r.rows), (len(r.rows), len(r2.rows)))


def t_typesense():
    cid = run("ts", "typesense/typesense:27.1", cmd=["--data-dir", "/tmp", "--api-key=xyz", "--enable-cors"])
    b = backend(cid)
    wait(cid, lambda: b.connect(), what="typesense")
    http_seed(b, "POST", "/collections", {"name": "books", "fields": [{"name": "title", "type": "string"},
                                                                      {"name": "year", "type": "int32"}]})
    for i in range(3):
        http_seed(b, "POST", "/collections/books/documents", {"id": str(i), "title": f"Book {i}", "year": 2000 + i})
    common(b, "typesense", "books", 3)

    node = next(n for n in b.tree() if n.label == "books")
    ed, r = _ed(b, node)
    check("edit typesense: documents are picked out by id", r.edit_why is None, r.edit_why)
    ident = ed.ident(node, r, 0)
    doc = json.loads(_vals(ed, node, ident)["document"].value)
    doc["edited"] = "yes"
    ed.save(node, ident, {"document": json.dumps(doc)}, True)
    check("edit typesense: save a document", json.loads(_vals(ed, node, ident)["document"].value).get("edited") == "yes")
    ed.save(node, None, {"document": json.dumps({"id": "9", "title": "New", "year": 2024})}, True)
    ed.delete(node, ident, True)
    time.sleep(1)
    r2 = b.browse(node)
    check("edit typesense: add one and delete one", len(r2.rows) == len(r.rows), (len(r.rows), len(r2.rows)))


def t_meili():
    cid = run("meili", "getmeili/meilisearch:v1.11", {"MEILI_MASTER_KEY": "a-long-master-key-123456", "MEILI_ENV": "development"})
    b = backend(cid)
    wait(cid, lambda: b.connect(), what="meilisearch")
    t = http_seed(b, "POST", "/indexes/movies/documents", [{"id": i, "title": f"Movie {i}"} for i in range(3)])
    wait(cid, lambda: http_seed(b, "GET", f"/tasks/{t['taskUid']}")["status"] == "succeeded", 60, "meili task")
    common(b, "meilisearch", "movies", 3)

    node = next(n for n in b.tree() if n.label == "movies")
    ed, r = _ed(b, node)
    check("edit meilisearch: documents are picked out by id", r.edit_why is None, r.edit_why)
    ident = ed.ident(node, r, 0)
    doc = json.loads(_vals(ed, node, ident)["document"].value)
    doc["edited"] = "yes"
    ed.save(node, ident, {"document": json.dumps(doc)}, True)
    check("edit meilisearch: save a document", json.loads(_vals(ed, node, ident)["document"].value).get("edited") == "yes")
    ed.save(node, None, {"document": json.dumps({"id": 9, "title": "New"})}, True)
    ed.delete(node, ident, True)
    time.sleep(1)
    r2 = b.browse(node)
    check("edit meilisearch: add one and delete one", len(r2.rows) == len(r.rows), (len(r.rows), len(r2.rows)))


def t_sqlite():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "app.db")
    con = sqlite3.connect(path)
    con.execute("pragma journal_mode=wal")
    con.execute("create table notes(id integer primary key, body text)")
    con.executemany("insert into notes(body) values (?)", [(f"note {i}",) for i in range(5)])
    con.commit()
    # copy while the connection is open: the last rows are still only in the -wal file
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                tf.add(path + suffix, arcname="app.db" + suffix)
    walsize = os.path.getsize(path + "-wal") if os.path.exists(path + "-wal") else 0
    con.close()
    vol = "berth-test-sqlite"
    try:
        eng.remove_volume(vol)
    except Exception:
        pass
    eng.req("POST", "/volumes/create", body={"Name": vol, "Labels": LABEL})
    cid = run("files", "busybox:latest", cmd=["sleep", "600"], binds=[f"{vol}:/data"])
    eng.put_archive(cid, "/data", buf.getvalue())
    b = db.SQLite(eng, cid, "/data/app.db", "test")
    try:
        check("sqlite: WAL file present in the copy source", walsize > 0, f"{walsize} bytes")
        check("sqlite: opens a copy", b.connect())
        node = next(n for n in b.tree() if n.label == "notes")
        check("sqlite: rows that were only in the WAL are visible", node.detail.startswith("5 rows"), node.detail)
        check("sqlite: browse", len(b.browse(node, 0, 3).rows) == 3)
        ok, msg = raises(b.query, "delete from notes", contains="read-only")
        check("sqlite: the copy is read-only", ok, msg)
    finally:
        b.close()
    from berth.files import _helper, stop_volume_helper
    h = _helper(eng, [f"{vol}:/volume:ro"])
    try:
        b2 = db.SQLite(eng, h, "/volume/app.db", "vol")
        b2.connect()
        check("sqlite: from a volume (read-only helper)", any(n.label == "notes" for n in b2.tree()))
        b2.close()
    finally:
        stop_volume_helper(eng, h)
    check("sqlite: temp copy removed", not os.path.exists(b.tmp))


def t_detect():
    cases = {"postgres:16-alpine": "postgres", "pgvector/pgvector:pg16": "postgres", "bitnami/redis:7": "redis",
             "valkey/valkey:8": "redis", "mongo:7": "mongo", "vespaengine/vespa:8": "vespa", "nginx": None,
             "docker.elastic.co/elasticsearch/elasticsearch:8.15.0": "opensearch", "dpage/pgadmin4": None,
             "oliver006/redis_exporter": None, "redis/redisinsight": None, "mariadb:11": "mysql"}
    bad = {k: db.detect({"Image": k}) for k, v in cases.items() if db.detect({"Image": k}) != v}
    check("detect: image names → engines", not bad, bad)


# ── your own databases (read-only) ──────────────────────────────────────────────────────────────────────────────
def t_real():
    for c in eng.containers():
        kind = db.detect(c)
        name = c["Names"][0].lstrip("/")
        if not kind or c["State"] != "running" or name.startswith("berth-test-"):
            continue
        try:
            b = backend(c["Id"])
            desc = b.connect()
            nodes = b.tree()
            flat = []
            for n in nodes:
                flat += [n] if n.leaf else b.children(n)
            r = b.browse(flat[0], 0, 5) if flat else None
            check(f"real {name} ({kind}): connect + tree + browse", True,
                  f"{desc} · {len(flat)} items · first: {flat[0].label if flat else '-'} · {r}")
            probe = {"postgres": "create table berth_should_not_exist(x int)", "mysql": "create table berth_x(x int)",
                     "redis": "SET berth_should_not_exist 1", "mongo": "db.x.drop()"}.get(kind)
            if probe:
                ok, msg = raises(b.query, probe, contains="read-only")
                check(f"real {name}: write refused (nothing changed)", ok, msg)
            b.close()
        except Exception as e:
            check(f"real {name} ({kind})", False, e)


# ── local addresses ─────────────────────────────────────────────────────────────────────────────────────────────
def fetch(port, host, https=False, path="/"):
    if https:
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        c = http.client.HTTPSConnection("127.0.0.1", port, timeout=10, context=ctx)
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        c.sock = ctx.wrap_socket(sock, server_hostname=host)
    else:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers={"Host": host})
    r = c.getresponse()
    return r.status, r.read().decode(errors="replace")


def t_proxy():
    ports = (18080, 18443)
    web = run("web", "nginx:alpine")                                      # user-defined network: by name
    web2 = run("web2", "nginx:alpine", network="bridge")                   # default bridge: by IP
    eng.exec_run(web2, ["sh", "-c", "echo second > /usr/share/nginx/html/index.html"])
    routes = [{"name": "berth-test-web", "container": "berth-test-web", "port": 80, "https": False},
              {"name": "berth-test-secure", "container": "berth-test-web", "port": 80, "https": True},
              {"name": "berth-test-web2", "container": "berth-test-web2", "port": 80, "https": False}]
    pid = sharing.start_proxy(eng, ports)
    ins = eng.inspect(pid)
    bound = {k: [b.get("HostIp") for b in v] for k, v in ins["NetworkSettings"]["Ports"].items() if v}
    check("proxy: published on 127.0.0.1 only", bound and all(ips == ["127.0.0.1"] for ips in bound.values()), bound)
    ups = sharing.apply_routes(eng, routes, pid)
    check("proxy: upstream by name on a user network", ups["berth-test-web"] == "berth-test-web:80", ups)
    check("proxy: upstream by IP on the default bridge", ups["berth-test-web2"].split(":")[0].count(".") == 3, ups)
    wait(pid, lambda: fetch(ports[0], "berth-test-web.localhost")[0] == 200, 30, "proxy")
    st, body = fetch(ports[0], "berth-test-web.localhost")
    check("proxy: http://berth-test-web.localhost serves the container", st == 200 and "nginx" in body, st)
    st, body = fetch(ports[0], "berth-test-web2.localhost")
    check("proxy: default-bridge container reachable", st == 200 and "second" in body, (st, body[:40]))
    for _ in range(15):     # the first HTTPS name waits for Caddy to create its local CA
        try:
            st, body = fetch(ports[1], "berth-test-secure.localhost", https=True)
            break
        except (ssl.SSLError, OSError) as e:
            st, body = e, ""
            time.sleep(1)
    check("proxy: https://berth-test-secure.localhost (local CA)", st == 200 and "nginx" in body, st)
    st, _b = fetch(ports[0], "nothing.localhost")
    check("proxy: unknown name → 404 page", st == 404, st)
    pem = sharing.root_certificate(eng, pid)
    check("proxy: root certificate exportable", pem.startswith(b"-----BEGIN CERTIFICATE"), pem[:30])
    try:
        socket.getaddrinfo("berth-test-web.localhost", ports[0])
        st, _b = http.client.HTTPConnection("berth-test-web.localhost", ports[0], timeout=5), None
        st.request("GET", "/")
        check("proxy: *.localhost resolves on this system (no hosts file edits)", st.getresponse().status == 200)
    except OSError as e:
        print(f"note: this system's resolver doesn't map *.localhost ({e}); browsers still do")
    # IP change on the default bridge → signature changes → re-apply keeps it working
    sig = sharing.signature(eng, routes)
    eng.action(web2, "stop")
    blocker = run("blocker", "busybox:latest", cmd=["sleep", "600"], network="bridge")   # takes web2's old IP
    eng.action(web2, "start")
    eng.exec_run(web2, ["sh", "-c", "echo second > /usr/share/nginx/html/index.html"])
    sig2 = sharing.signature(eng, routes)
    check("proxy: a restart with a new IP changes the signature", sig2 != sig)
    sharing.apply_routes(eng, routes, pid)
    st, body = fetch(ports[0], "berth-test-web2.localhost")
    check("proxy: re-applied after the IP changed", st == 200 and "second" in body, st)
    eng.action(web2, "stop")
    sharing.apply_routes(eng, routes, pid)
    st, body = fetch(ports[0], "berth-test-web2.localhost")
    check("proxy: stopped target → clear 502 message", st == 502 and "not running" in body, (st, body[:60]))
    eng.remove_container(blocker, force=True)
    ok, msg = raises(sharing.start_proxy, eng, (ports[0], ports[1]))
    check("proxy: start again is idempotent", not ok and msg == "no error", msg)
    # phones: one address also on the Wi-Fi, by its own port; the others stay on 127.0.0.1
    from berth.fileshare import lan_ip
    lan_route = {"name": "berth-test-phone", "container": "berth-test-web", "port": 80, "https": False, "lan": True,
                 "lan_port": 18100}
    pid = sharing.start_proxy(eng, ports, lan=sharing.lan_ports(routes + [lan_route]))
    sharing.apply_routes(eng, routes + [lan_route], pid)
    b = {k: [x.get("HostIp") for x in v] for k, v in eng.inspect(pid)["NetworkSettings"]["Ports"].items() if v}
    check("phones: only the phone port is on the network", b.get("18100/tcp") == ["0.0.0.0"] and
          b.get("80/tcp") == ["127.0.0.1"], b)
    ip = lan_ip()
    ok = False
    for _ in range(10):
        try:
            c = http.client.HTTPConnection(ip, 18100, timeout=5)
            c.request("GET", "/")
            r = c.getresponse()
            ok = r.status == 200 and "nginx" in r.read().decode(errors="replace")
            if ok:
                break
        except OSError:
            pass
        time.sleep(1)
    check(f"phones: http://{ip}:18100 serves the container (Wi-Fi address)", ok)
    pid = sharing.start_proxy(eng, ports, lan=sharing.lan_ports(routes))
    b = {k for k, v in eng.inspect(pid)["NetworkSettings"]["Ports"].items() if v}
    check("phones: switching it off closes the network port", "18100/tcp" not in b, b)
    sharing.stop_proxy(eng, remove=True)
    check("proxy: removed", not sharing.proxy(eng.containers_filtered({"name": ["berth-proxy"]})))
    check("names: validation", sharing.validate_name("Shop", []) and sharing.validate_name("a_b", [])
          and not sharing.validate_name("my-api", []))


# ── public links ────────────────────────────────────────────────────────────────────────────────────────────────
def t_tunnel():
    web = run("pub", "nginx:alpine")
    eng.exec_run(web, ["sh", "-c", "echo berth-tunnel-ok > /usr/share/nginx/html/index.html"])
    tid, url = sharing.start_tunnel(eng, web, 80)
    check("tunnel: quick tunnel gives a trycloudflare.com URL", url and url.endswith(".trycloudflare.com"), url)
    ins = eng.inspect(tid)
    check("tunnel: runs in the target's network namespace", ins["HostConfig"]["NetworkMode"].startswith("container:"))
    host = url[8:]
    ok, body, last = False, "", None
    for _ in range(30):         # DNS for a fresh quick tunnel can take a few seconds
        try:
            c = http.client.HTTPSConnection(host, timeout=10)
            c.request("GET", "/", headers={"User-Agent": "berth-test"})
            r = c.getresponse()
            body = r.read().decode(errors="replace")
            if r.status == 200 and "berth-tunnel-ok" in body:
                ok = True
                break
            last = r.status
        except OSError as e:
            last = e
        time.sleep(3)
    check("tunnel: the public URL serves the container over the internet", ok, last if not ok else url)
    keep_id, _u = sharing.start_tunnel(eng, run("pub2", "nginx:alpine"), 80, keep=True)
    n = sharing.stop_unpinned(eng)
    alive = {c["Id"] for c in eng.containers_filtered({"label": [f"{sharing.ROLE}=tunnel"]})}
    check("tunnel: quitting stops unpinned links, keeps pinned ones", tid not in alive and keep_id in alive, n)
    sharing.stop_tunnel(eng, keep_id)
    check("tunnel: stop removes it", keep_id not in {c["Id"] for c in eng.containers_filtered(
        {"label": [f"{sharing.ROLE}=tunnel"]})})


# ── renames, ports, richest database, link expiry ──────────────────────────────────────────────────────────────
def t_manage():
    from berth.edit import change_ports
    from berth.files import _helper, stop_volume_helper
    # container rename (live) + port change keeps the 127.0.0.1 binding
    cid = run("ports", "nginx:alpine", network="bridge", ports={80: 18091})
    eng.rename(cid, "berth-test-renamed")
    ins = eng.inspect(cid)
    check("container rename: live, still running", ins["Name"] == "/berth-test-renamed" and ins["State"]["Running"])
    new = change_ports(eng, cid, [("127.0.0.1:18092", "80")])
    b = eng.inspect(new)["HostConfig"]["PortBindings"]["80/tcp"][0]
    check("change ports: new host port, bound to 127.0.0.1 only", b == {"HostIp": "127.0.0.1", "HostPort": "18092"}, b)
    st, _b = fetch(18092, "localhost")
    check("change ports: reachable on the new port", st == 200, st)
    new2 = change_ports(eng, new, [("127.0.0.1:18093", "80")])     # edit again: the 127.0.0.1 binding survives
    b2 = eng.inspect(new2)["HostConfig"]["PortBindings"]["80/tcp"][0]
    check("change ports: a second edit keeps 127.0.0.1 (was lost before)", b2["HostIp"] == "127.0.0.1", b2)
    # image rename = tag + untag old
    eng.tag("nginx:alpine", "berth-test-img", "one")
    iid = eng.req("GET", "/images/berth-test-img:one/json")["Id"]
    eng.tag(iid, "berth-test-img", "two")
    eng.remove_image("berth-test-img:one")
    tags = eng.req("GET", f"/images/{iid}/json")["RepoTags"]
    check("image rename: new tag present, old gone, image kept", "berth-test-img:two" in tags and
          "berth-test-img:one" not in tags, tags)
    eng.remove_image("berth-test-img:two")
    # volume rename while a running container uses it
    vol = "berth-test-vol-a"
    for v in ("berth-test-vol-a", "berth-test-vol-b"):
        try:
            eng.remove_volume(v)
        except Exception:
            pass
    eng.req("POST", "/volumes/create", body={"Name": vol, "Labels": LABEL})
    user = run("voluser", "busybox:latest", cmd=["sh", "-c", "echo keep-me > /data/f; sleep 600"], binds=[f"{vol}:/data"])
    time.sleep(1)
    from berth import quick
    from berth.edit import repoint_volume
    from berth.files import clone_volume
    eng.action(user, "stop")                       # the same steps quick.rename_volume runs
    clone_volume(eng, vol, "berth-test-vol-b")
    nid = repoint_volume(eng, user, vol, "berth-test-vol-b")
    eng.action(nid, "start")
    eng.remove_volume(vol)
    mounts = [m.get("Name") for m in eng.inspect(nid)["Mounts"]]
    code, out, _e = eng.exec_run(nid, ["cat", "/data/f"])
    check("volume rename: container switched, data kept, running", mounts == ["berth-test-vol-b"] and out.strip() ==
          "keep-me" and eng.inspect(nid)["State"]["Running"], (mounts, out.strip()))
    check("volume rename: old volume gone", vol not in [v["Name"] for v in eng.volumes()])
    check("volume rename: helper containers are not counted as users", quick._users.__code__.co_names.count("HELPER_LABEL"))
    eng.remove_container(nid, force=True)
    eng.remove_volume("berth-test-vol-b")


def t_dbrename():
    # Postgres: empty default database, data in another one → richest; renames need allow_write
    cid = run("pg2", "postgres:16-alpine", {"POSTGRES_PASSWORD": "pw", "POSTGRES_DB": "empty"})
    b = backend(cid)
    wait(cid, lambda: b.connect(), what="postgres")
    b.query("create database shopdata", allow_write=True)
    b.db = "shopdata"
    b.query("create table items(id int); insert into items values (1)", allow_write=True)
    b.db = "empty"
    check("richest: picks the database that has tables", b.richest(b.databases()) == "shopdata", b.databases())
    b.db = "shopdata"
    node = next(n for s in b.tree() for n in b.children(s) if n.label == "items")
    ok, msg = raises(b.rename, node, "goods", contains="allow changes")
    check("rename refused without Allow changes", ok, msg)
    b.rename(node, "goods", True)
    check("postgres: rename table", any(n.label == "goods" for s in b.tree() for n in b.children(s)))
    b.rename(db.Node("shopdata", "database", "shopdata"), "shop2", True)
    check("postgres: rename database (connected elsewhere)", "shop2" in b.databases() and b.db == "shop2", b.databases())
    # MySQL table, Redis key, Mongo collection
    m = backend(run("my2", "mariadb:11", {"MARIADB_ROOT_PASSWORD": "pw", "MARIADB_DATABASE": "shop"}))
    wait(m.cid, lambda: m.connect(), what="mariadb")
    m.query("create table t1(x int)", allow_write=True)
    m.rename(db.Node("t1", "table", "t1"), "t2", True)
    check("mysql: rename table", [n.label for n in m.tree()] == ["t2"], [n.label for n in m.tree()])
    r = backend(run("redis2", "redis:7-alpine"))
    wait(r.cid, lambda: r.connect(), what="redis")
    r.query("SET a 1\nSET b 2", allow_write=True)
    r.rename(db.Node("a", "key", "a"), "c", True)
    ok, msg = raises(r.rename, db.Node("c", "key", "c"), "b", True, contains="already exists")
    check("redis: rename key, never overwrites another key", {n.label for n in r.tree()} == {"b", "c"} and ok, msg)
    g = backend(run("mongo2", "mongo:7"))
    wait(g.cid, lambda: g.connect(), what="mongo")
    g.db = "shop"
    g.query('db.old.insertOne({a: 1})', allow_write=True)
    g.rename(db.Node("old", "collection", "old"), "new", True)
    check("mongo: rename collection", [n.label for n in g.tree()] == ["new"])
    check("mongo: richest by size", g.richest(g.databases()) == "shop", g.databases())


def t_expiry():
    web = run("exp", "nginx:alpine")
    ins = eng.inspect(web)
    sharing.ensure_image(eng, sharing.TUNNEL_IMAGE)
    mk = lambda name, exp: eng.create_container(name, {"Image": sharing.TUNNEL_IMAGE, "Cmd": ["--version"],
        "Labels": {**LABEL, sharing.ROLE: "tunnel", sharing.TARGET: "berth-test-exp", sharing.PORT: "80",
                   **({sharing.EXPIRES: str(exp)} if exp else {})},
        "HostConfig": {"NetworkMode": f"container:{ins['Id']}"}})["Id"]
    past, future, never = mk("berth-test-t1", int(time.time()) - 5), mk("berth-test-t2", int(time.time()) + 3600), \
        mk("berth-test-t3", None)
    gone = sharing.remove_expired(eng)
    left = {c["Id"] for c in eng.containers_filtered({"label": [f"{sharing.ROLE}=tunnel"]})}
    check("expiry: expired link removed, others kept", past not in left and future in left and never in left, gone)
    check("expiry: label parsed", sharing.expires_at(eng.inspect(future)) is not None)

def t_huge():
    """Huge cells, binary, huge keys and oversized pages must never break the explorer (a 59 MB blob table did)."""
    cid = run("pgbig", "postgres:16-alpine", {"POSTGRES_PASSWORD": "pw"})
    b = backend(cid)
    wait(cid, lambda: b.connect(), what="postgres")
    b.query("create table big(id int, b bytea, t text, j jsonb); insert into big select g, decode(repeat('ab', 2500000), "
            "'hex'), repeat('x', 1000000), jsonb_build_object('k', repeat('y', 300000)) from generate_series(1, 5) g",
            allow_write=True)
    node = next(n for sch in b.tree() for n in b.children(sch) if n.label == "big")
    r = db._shrinking(b.browse, node, 0, db.ROW_LIMIT)
    check("huge: postgres page with 2.5 MB binary + 1 M-char text cells", len(r.rows) == 5 and
          r.rows[0][1].startswith("‹binary") and "1,000,000 characters" in r.rows[0][2] and
          len(r.rows[0][3]) < db.CELL_MAX + 40, [str(v)[:40] for v in r.rows[0]])
    q = b.query("select * from big")
    check("huge: raw query over 16 MB of output doesn't crash", q.rows and q.truncated and
          all(len(str(v or "")) <= db.CELL_MAX + 40 for row in q.rows for v in row), (len(q.rows), q.note))
    rd = backend(run("redisbig", "redis:7-alpine"))
    wait(rd.cid, lambda: rd.connect(), what="redis")
    rd.exec(["sh", "-c", "head -c 1000000 /dev/zero | tr '\\\\0' x | redis-cli -x SET big >/dev/null; "
                         "for i in $(seq 1 5000); do echo HSET wide f$i v$i; done | redis-cli >/dev/null"])
    keys = {n.label: n for n in rd.tree()}
    a = rd.browse(keys["big"])
    w = rd.browse(keys["wide"])
    check("huge: 1 MB redis string shows its start", "1,000,000 bytes" in a.note and len(a.rows[0][0]) < db.CELL_MAX + 40,
          a.note)
    check("huge: 5,000-field hash shows a sample, not everything", w.truncated and 0 < len(w.rows) <= db.ROW_LIMIT, w.note)
    m = backend(run("mongobig", "mongo:7"))
    wait(m.cid, lambda: m.connect(), what="mongo")
    m.db = "shop"
    m.query("db.docs.insertMany(Array.from({length: 10}, (_, i) => ({i: i, blob: 'z'.repeat(3000000)})))",
            allow_write=True)
    node = next(n for n in m.tree() if n.label == "docs")
    r = db._shrinking(m.browse, node, 0, db.ROW_LIMIT)
    check("huge: 30 MB of mongo documents → smaller pages instead of an error", r.rows and getattr(r, "page", 200) < 200,
          (len(r.rows), r.note))
    check("huge: cell helper summarises binary and long text",
          db.cap(b"\x00" * 10) == "‹binary · 10 B›" and db.cap("a" * 5000).endswith("[5,000 characters]"))


TESTS = [("detect", t_detect), ("postgres", t_postgres), ("mysql", t_mysql), ("redis", t_redis), ("mongo", t_mongo),
         ("opensearch", t_opensearch), ("typesense", t_typesense), ("meilisearch", t_meili), ("sqlite", t_sqlite),
         ("proxy", t_proxy), ("tunnel", t_tunnel), ("manage", t_manage), ("dbrename", t_dbrename),
         ("expiry", t_expiry), ("huge", t_huge)]

if __name__ == "__main__":
    try:
        eng.req("GET", "/_ping", timeout=5)
    except EngineError as e:
        sys.exit(f"No engine: {e}")
    try:
        PROXY_VOLUME_EXISTED = any(v["Name"] == sharing.PROXY_DATA for v in eng.volumes())
        ensure_net()
        for name, fn in TESTS:
            if not want(name) or (name == "tunnel" and "--no-tunnel" in ARGS):
                continue
            t0 = time.time()
            try:
                fn()
            except Exception as e:
                traceback.print_exc()
                check(f"{name}: ran without crashing", False, e)
            print(f"   ({name}: {time.time() - t0:.0f}s)", flush=True)
        if "--real" in ARGS:
            t_real()
    finally:
        cleanup()
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f" — failed: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)
