"""Every database engine Berth supports, one at a time, on the demo engine (tcp://127.0.0.1:23750), seeded with
realistic data — for clicking through the explorer by hand or with tools/walkthrough.py.

    python3 tools/demo/engines.py up mysql        # start + seed one engine
    python3 tools/demo/engines.py down mysql
    python3 tools/demo/engines.py list

Only the demo engine is touched. Memory: each engine is started on its own and removed afterwards."""
import io
import json
import os
import subprocess
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from berth import db  # noqa: E402
from berth.engine import Engine  # noqa: E402

DEMO = "tcp://127.0.0.1:23750"
eng = Engine(DEMO)

HOTEL_FAQ = [
    ("What dining options are available at the hotel?", "The Lakeview Grand has three restaurants and a rooftop bar.", "dining"),
    ("What time is check-in and check-out?", "Check-in from 3 PM, check-out until 11 AM.", "stay"),
    ("Is Wi-Fi free at the hotel?", "Yes, high-speed Wi-Fi is complimentary in all rooms and public areas.", "amenities"),
    ("Do you have parking at the hotel?", "Yes, we offer complimentary valet parking for guests.", "amenities"),
    ("Do you have a swimming pool and gym?", "Yes, an outdoor rooftop pool and a 24-hour gym.", "amenities"),
    ("Are pets allowed at the hotel?", "Dogs under 15 kg are welcome for a small fee.", "policies"),
    ("Is breakfast included with my stay?", "Breakfast is included with most rates; check your booking.", "dining"),
    ("Do you offer airport transfers?", "Yes, we offer airport pickup and drop-off on request.", "transport"),
]
CATALOG = [
    ("Sea-View Flat in Worli", "Cozy Apartments", 7800.0, "A bright two-bedroom flat with views of the sea."),
    ("Budget Room in Juhu", "Cozy Apartments", 2100.0, "A simple room near the beach.\nFan, wifi, shared kitchen."),
    ("Luxury Suite at Marine Drive", "Luxury Suites", 15800.0, "Top floor, \"quoted\" view; butler on call."),
    ("Heritage Villa in Bandra", "Private Villas", 24000.0, "A restored 1920s villa with a garden."),
    ("Penthouse Suite in Lower Parel", "Luxury Suites", 19900.0, "Private terrace, jacuzzi, 3 bedrooms."),
]

ENGINES = {
    "mysql": {"image": "mariadb:11", "env": {"MARIADB_ROOT_PASSWORD": "demo", "MARIADB_DATABASE": "shop"}},
    "mongo": {"image": "mongo:7", "env": {"MONGO_INITDB_ROOT_USERNAME": "root", "MONGO_INITDB_ROOT_PASSWORD": "demo"}},
    "opensearch": {"image": "opensearchproject/opensearch:2",
                   "env": {"discovery.type": "single-node", "DISABLE_SECURITY_PLUGIN": "true",
                           "DISABLE_INSTALL_DEMO_CONFIG": "true", "OPENSEARCH_JAVA_OPTS": "-Xms256m -Xmx256m"}},
    "typesense": {"image": "typesense/typesense:27.1", "cmd": ["--data-dir", "/tmp", "--api-key=demo"]},
    "meilisearch": {"image": "getmeili/meilisearch:v1.11", "env": {"MEILI_MASTER_KEY": "demo-master-key-1234567890",
                                                                   "MEILI_ENV": "development"}},
    "vespa": {"image": "vespaengine/vespa:8", "env": {"VESPA_CONFIGSERVER_JVMARGS": "-Xms64m -Xmx256m",
                                                      "VESPA_CONFIGPROXY_JVMARGS": "-Xms32m -Xmx64m"}},
    "sqlite": {"image": "busybox:latest", "cmd": ["sleep", "86400"]},
    "redis": {"image": "redis:7-alpine"},
}


def name(kind):
    return f"demo-{kind}"


def docker(*a, check=True):
    r = subprocess.run(["docker", *a], env={**os.environ, "DOCKER_HOST": DEMO}, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def backend(kind):
    return db.open_backend(eng, eng.inspect(name(kind)))


def wait(fn, what, timeout=300):
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        try:
            if fn():
                return
        except Exception as e:   # noqa: BLE001
            last = e
        time.sleep(3)
    raise RuntimeError(f"{what} didn't come up: {last}")


def up(kind):
    spec = ENGINES[kind]
    docker("rm", "-f", name(kind), check=False)
    args = ["run", "-d", "--name", name(kind), "--label", "com.docker.compose.project=data"]
    for k, v in (spec.get("env") or {}).items():
        args += ["-e", f"{k}={v}"]
    docker(*args, spec["image"], *(spec.get("cmd") or []))
    globals()["seed_" + kind]()
    print(f"{kind} ready: {name(kind)}")


def down(kind):
    docker("rm", "-f", name(kind), check=False)


def seed_mysql():
    b = backend("mysql")
    wait(lambda: b.connect(), "mariadb")
    b.db = "shop"
    rows = ", ".join(f"('{n}', '{c}', {p}, {i * 3}, JSON_OBJECT('rating', {4 + i % 2}.{i}, 'tags', JSON_ARRAY('wifi','sea')), "
                     f"'{d.replace(chr(39), chr(39) * 2)}')" for i, (n, c, p, d) in enumerate(CATALOG))
    b.query("create table listings(id int auto_increment primary key, name varchar(120) not null, "
            "category varchar(60), price decimal(10,2), stock int default 0, meta json, description text, "
            "photo blob, created_at timestamp default current_timestamp); "
            f"insert into listings(name, category, price, stock, meta, description) values {rows}; "
            "update listings set photo = unhex(repeat('89504E47', 300)) where id = 1; "
            "create table audit(at timestamp default current_timestamp, actor varchar(40), action varchar(40)); "
            "insert into audit(actor, action) values ('ada', 'login'), ('bob', 'export'); "
            "create view cheap as select name, price from listings where price < 5000", allow_write=True)


def seed_mongo():
    b = backend("mongo")
    wait(lambda: b.connect(), "mongo")
    b.db = "shop"
    docs = [{"name": n, "category": c, "price": {"amount": p, "currency": "INR"}, "description": d,
             "amenities": ["wifi", "ac"], "rating": 4.5 - i / 10, "available": i % 2 == 0}
            for i, (n, c, p, d) in enumerate(CATALOG)]
    b.query(f"db.listings.insertMany({json.dumps(docs)})", allow_write=True)
    b.query('db.faq.insertMany([' + ", ".join(json.dumps({"_id": f"faq-{i}", "question": q, "answer": a,
                                                          "category": c}) for i, (q, a, c) in enumerate(HOTEL_FAQ))
            + "])", allow_write=True)


def seed_opensearch():
    b = backend("opensearch")
    wait(lambda: b.connect(), "opensearch", 400)
    for i, (n, c, p, d) in enumerate(CATALOG):
        b.http.json("PUT", f"/listings/_doc/listing-{i + 1}?refresh=true",
                    {"name": n, "category": c, "price": p, "description": d, "location": {"city": "Mumbai"}})


def seed_typesense():
    b = backend("typesense")
    wait(lambda: b.connect(), "typesense")
    b.http.json("POST", "/collections", {"name": "hotels", "fields": [
        {"name": "name", "type": "string"}, {"name": "city", "type": "string"}, {"name": "rating", "type": "float"},
        {"name": "amenities", "type": "string[]"}]})
    for i, (n, c, p, d) in enumerate(CATALOG):
        b.http.json("POST", "/collections/hotels/documents", {"id": str(i + 1), "name": n, "city": "Mumbai",
                                                               "rating": 4.0 + i / 10, "amenities": ["wifi", c]})


def seed_meilisearch():
    b = backend("meilisearch")
    wait(lambda: b.connect(), "meilisearch")
    t = b.http.json("POST", "/indexes/faq/documents", [{"id": i + 1, "question": q, "answer": a, "category": c}
                                                       for i, (q, a, c) in enumerate(HOTEL_FAQ)])
    wait(lambda: b.http.json("GET", f"/tasks/{t['taskUid']}")["status"] == "succeeded", "meili task")


VESPA_SERVICES = """<?xml version="1.0" encoding="utf-8" ?>
<services version="1.0">
  <container id="default" version="1.0"><document-api/><search/></container>
  <content id="content" version="1.0">
    <min-redundancy>1</min-redundancy>
    <documents><document type="faq" mode="index"/></documents>
  </content>
</services>
"""
VESPA_SCHEMA = """schema faq {
    document faq {
        field id type string { indexing: summary | attribute }
        field tenant_id type string { indexing: summary | attribute }
        field question type string { indexing: summary | index }
        field answer type string { indexing: summary | index }
        field category type string { indexing: summary | attribute }
        field extra_fields type string { indexing: summary }
        field price_amount type double { indexing: summary | attribute }
    }
}
"""


def seed_vespa():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for path, text in (("app/services.xml", VESPA_SERVICES), ("app/schemas/faq.sd", VESPA_SCHEMA)):
            data = text.encode()
            ti = tarfile.TarInfo(path)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    eng.put_archive(name("vespa"), "/tmp", buf.getvalue())
    wait(lambda: eng.exec_run(name("vespa"), ["curl", "-sf", "http://localhost:19071/state/v1/health"],
                              timeout=10)[0] == 0, "vespa config server", 400)
    code, out, err = eng.exec_run(name("vespa"), ["sh", "-c", "cd /tmp/app && /opt/vespa/bin/vespa-deploy prepare . "
                                                  "&& /opt/vespa/bin/vespa-deploy activate"], timeout=300)
    if code != 0:
        raise RuntimeError(f"vespa deploy: {err or out}")
    wait(lambda: eng.exec_run(name("vespa"), ["curl", "-sf", "http://localhost:8080/state/v1/health"],
                              timeout=10)[0] == 0, "vespa container", 400)
    for i, (q, a, c) in enumerate(HOTEL_FAQ):
        doc = {"fields": {"id": f"catalog-{i + 1:03d}", "tenant_id": "d356152a-3b39-4ce5-b59a-3d6e0a6b6fba",
                          "question": q, "answer": a, "category": c, "price_amount": 1000.0 + i,
                          "extra_fields": json.dumps({"gender": None, "image_url": f"https://example.com/{i}.webp"})}}
        wait(lambda doc=doc, i=i: eng.exec_run(name("vespa"), ["curl", "-sf", "-X", "POST", "-H",
                                                                "Content-Type: application/json", "--data",
                                                                json.dumps(doc),
                                                                f"http://localhost:8080/document/v1/faq/faq/docid/{i + 1}"],
                                               timeout=30)[0] == 0, "vespa feed", 120)


def seed_redis():
    b = backend("redis")
    wait(lambda: b.connect(), "redis")
    b.query("\n".join([
        "HSET listing:1 name \"Sea-View Flat in Worli\" category \"Cozy Apartments\" price 7800 currency INR",
        "HSET listing:2 name \"Budget Room in Juhu\" category \"Cozy Apartments\" price 2100 currency INR",
        "SET greeting \"Welcome to The Lakeview Grand\"",
        "SET session:abc123 \"{\\\"user\\\": \\\"ada\\\", \\\"cart\\\": [1, 2]}\"",
        "EXPIRE session:abc123 86400",
        "RPUSH queue:emails welcome@x.test reset@x.test receipt@x.test",
        "SADD tags:wifi listing:1 listing:2",
        "ZADD leaderboard 120 ada 95 bob 77 cy",
        "XADD events * kind booking listing 1",
        "XADD events * kind cancel listing 2"]), allow_write=True)


def seed_sqlite():
    import sqlite3
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("create table notes(id integer primary key, title text not null, body text, done integer default 0)")
    c.executemany("insert into notes(title, body, done) values (?, ?, ?)",
                  [(n, d, i % 2) for i, (n, _c, _p, d) in enumerate(CATALOG)])
    c.commit()
    c.close()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.add(path, arcname="app.db")
    os.remove(path)
    docker("exec", name("sqlite"), "mkdir", "-p", "/data")
    eng.put_archive(name("sqlite"), "/data", buf.getvalue())


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "list":
        print(" ".join(ENGINES))
    elif sys.argv[1] == "up":
        for k in sys.argv[2:] or ENGINES:
            up(k)
    elif sys.argv[1] == "down":
        for k in sys.argv[2:] or ENGINES:
            down(k)
