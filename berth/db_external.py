"""Databases outside Docker (docs/GAPS.md): a connection string — postgres://, mysql://, redis://, rediss://,
mongodb:// or mongodb+srv:// — opens the same explorer as a database container. Berth runs the database's own client
(psql, mariadb, redis-cli, mongosh) in a short-lived container made from the official image, pointed at that host;
the container is removed when the explorer closes. Nothing is installed on this computer.

"localhost" means this computer: on a local Linux engine the client shares the computer's network; elsewhere
(Docker Desktop, remote engines) it goes through host.docker.internal."""
import sys
import urllib.parse

from . import db

SCHEMES = {"postgres": "postgres", "postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql", "redis": "redis",
           "rediss": "redis", "valkey": "redis", "mongodb": "mongo", "mongodb+srv": "mongo"}
CLIENTS = {"postgres": "postgres:17-alpine", "mysql": "mariadb:11", "redis": "redis:7-alpine", "mongo": "mongo:8"}
PORTS = {"postgres": 5432, "mysql": 3306, "redis": 6379, "mongo": 27017}
LOCAL = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def parse(url):
    """{kind, host, port, user, password, db, tls, sslmode, query, scheme} from a connection string."""
    u = urllib.parse.urlsplit(url.strip())
    kind = SCHEMES.get(u.scheme.lower())
    if not kind:
        raise db.DbError("Start with postgres://, mysql://, redis://, rediss://, mongodb:// or mongodb+srv://")
    if not u.hostname:
        raise db.DbError("The connection string has no host")
    qs = dict(urllib.parse.parse_qsl(u.query))
    path = u.path.lstrip("/")
    return {"kind": kind, "scheme": u.scheme.lower(), "host": u.hostname, "port": u.port or PORTS[kind],
            "user": urllib.parse.unquote(u.username or ""), "password": urllib.parse.unquote(u.password or ""),
            "db": path, "tls": u.scheme.lower() in ("rediss", "mongodb+srv") or qs.get("ssl") in ("true", "1")
            or qs.get("tls") == "true" or qs.get("sslmode") in ("require", "verify-ca", "verify-full"),
            "sslmode": qs.get("sslmode", ""), "query": ("?" + u.query) if u.query else ""}


def redact(url):
    """The string without its password — what Berth remembers for "recent connections"."""
    u = urllib.parse.urlsplit(url.strip())
    if u.password:
        netloc = f"{u.username}@{u.hostname}" + (f":{u.port}" if u.port else "")
        return urllib.parse.urlunsplit((u.scheme, netloc, u.path, u.query, ""))
    return url.strip()


def _local_linux(engine):
    return sys.platform.startswith("linux") and engine.path.startswith(("/", "unix://"))


def open_external(engine, url):
    """Start the client container and return a backend for the outside database (raises DbError)."""
    from .sharing import ensure_image
    c = parse(url)
    host = c["host"]
    local = _local_linux(engine)
    hc = {"AutoRemove": True}
    if local:
        hc["NetworkMode"] = "host"                 # localhost is this computer
    else:
        hc["ExtraHosts"] = ["host.docker.internal:host-gateway"]
        if host in LOCAL:
            host = "host.docker.internal"
    image = CLIENTS[c["kind"]]
    ensure_image(engine, image)
    cid = engine.create_container(None, {"Image": image, "Entrypoint": ["sleep", "86400"], "Cmd": [],
                                         "Labels": {"io.github.subhadeeproy3902.berth.helper": "db-client"},
                                         "HostConfig": hc})["Id"]
    engine.action(cid, "start")
    ins = engine.inspect(cid)
    b = db.BACKENDS[c["kind"]](engine, ins)
    b._client_helper = cid
    b.name = f"{c['host']}:{c['port']}"
    b.remote = {"host": host, "port": c["port"], "tls": c["tls"], "sslmode": c["sslmode"], "user": ""}
    if c["kind"] == "postgres":
        b.user, b.password = c["user"] or "postgres", c["password"]
        b.db = c["db"] or b.user
    elif c["kind"] == "mysql":
        b.user, b.password, b.db = c["user"] or "root", c["password"], c["db"]
    elif c["kind"] == "redis":
        b.password = c["password"]
        b.remote["user"] = c["user"] if c["user"] and c["user"] != "default" else ""
        b.db = c["db"] if c["db"].isdigit() else "0"
    elif c["kind"] == "mongo":
        netloc = host + ("" if c["scheme"] == "mongodb+srv" else f":{c['port']}")
        b.remote["uri"] = f"{c['scheme']}://{netloc}"
        b.remote["query"] = c["query"]
        b.user, b.password = c["user"], c["password"]
        b.db = c["db"] or "test"
    try:
        b.connect_label = b.connect()
    except Exception:
        b.close()
        raise
    return b
