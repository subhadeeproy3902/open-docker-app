"""Check a Compose file before running it — in plain words. No GTK here.

1. `docker compose config` must accept it (syntax, schema, interpolation); its warnings name unset variables.
2. Then Berth's own checks against this computer: published ports already used (by another container or another
   program), container names already taken, build folders and env files that don't exist, external volumes and
   networks that aren't there, and images that will be pulled."""
import json
import os
import re
import socket
import subprocess

from .util import host


def _ports_used_by_others(containers, project):
    used = {}
    for c in containers or []:
        if (c.get("Labels") or {}).get("com.docker.compose.project") == project:
            continue
        for p in c.get("Ports") or []:
            if p.get("PublicPort"):
                used[(int(p["PublicPort"]), (p.get("Type") or "tcp"))] = (c.get("Names") or ["?"])[0].lstrip("/")
    return used


def _port_free(port, proto="tcp"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def config(files, project=None, wd=None, env_file=None):
    """(parsed config dict or None, stderr text). Runs `docker compose config --format json`."""
    argv = ["docker", "compose"]
    if project:
        argv += ["-p", project]
    if wd:
        argv += ["--project-directory", wd]
    for f in files:
        argv += ["-f", f]
    if env_file:
        argv += ["--env-file", env_file]
    argv += ["config", "--format", "json"]
    try:
        r = subprocess.run(host(argv), capture_output=True, text=True, timeout=60,
                           cwd=wd or os.path.dirname(files[0]) or None)
    except FileNotFoundError:
        return None, "The docker command isn't installed, so Compose files can't be checked or run."
    except subprocess.TimeoutExpired:
        return None, "docker compose config took too long"
    if r.returncode != 0:
        return None, r.stderr.strip() or r.stdout.strip() or "docker compose config failed"
    try:
        return json.loads(r.stdout), r.stderr
    except ValueError:
        return None, "docker compose config gave an unexpected answer"


def check(files, containers=None, volumes=None, networks=None, images=None, project=None, wd=None):
    """[(level, message)] with level in error · warning · info; empty means it looks good."""
    out = []
    missing = [f for f in files if not os.path.exists(f)]
    if missing:
        return [("error", f"The file {missing[0]} doesn't exist")]
    cfg, err = config(files, project, wd)
    seen_vars = set()
    for m in re.finditer(r'The \\?"?([A-Za-z_][A-Za-z0-9_]*)\\?"? variable is not set', err or ""):
        if m.group(1) in seen_vars:
            continue
        seen_vars.add(m.group(1))
        out.append(("warning", f"{m.group(1)} isn't set — it becomes an empty value. Put it in a .env file next to "
                               "the compose file, or in your environment."))
    if cfg is None:
        lines = [ln.strip() for ln in (err or "").splitlines() if ln.strip() and "variable is not set" not in ln]
        out.insert(0, ("error", _plain(lines[-1]) if lines else "Compose couldn't read the file"))
        return out
    project = project or cfg.get("name") or ""
    base = wd or os.path.dirname(os.path.abspath(files[0]))
    used = _ports_used_by_others(containers, project)
    names_taken = {(c.get("Names") or ["?"])[0].lstrip("/"): (c.get("Labels") or {}).get("com.docker.compose.project")
                   for c in containers or []}
    have_images = {t for im in images or [] for t in (im.get("RepoTags") or [])}
    seen_ports = {}
    for name, svc in (cfg.get("services") or {}).items():
        for p in svc.get("ports") or []:
            pub, proto = p.get("published"), p.get("protocol") or "tcp"
            if not pub:
                continue
            for port in _expand(pub):
                if (port, proto) in seen_ports:
                    out.append(("error", f"Port {port} is published twice: by {seen_ports[(port, proto)]} and {name}"))
                    continue
                seen_ports[(port, proto)] = name
                if (port, proto) in used:
                    out.append(("error", f"Port {port} ({name}) is already used by the container {used[(port, proto)]}"))
                elif not _port_free(port, proto) and not any(
                        (c.get("Labels") or {}).get("com.docker.compose.project") == project for c in containers or []):
                    out.append(("error", f"Port {port} ({name}) is already used by another program on this computer"))
                elif p.get("host_ip") in (None, "", "0.0.0.0", "::") and _looks_like_db(svc):
                    out.append(("warning", f"{name} publishes port {port} to your whole network. For a database, "
                                           f"\"127.0.0.1:{port}:{p.get('target')}\" keeps it on this computer."))
        cn = svc.get("container_name")
        if cn and cn in names_taken and names_taken[cn] != project:
            out.append(("error", f"The container name {cn} ({name}) is already taken by another container"))
        b = svc.get("build")
        if b:
            ctx = b.get("context") if isinstance(b, dict) else b
            if ctx and not re.match(r"^[a-z]+://", ctx) and not os.path.isdir(os.path.join(base, ctx)):
                rel = os.path.relpath(ctx, base) if os.path.isabs(ctx) else ctx
                out.append(("error", f"{name} builds from the folder {'./' + rel if not rel.startswith('.') else rel}, "
                                     "which doesn't exist"))
        elif svc.get("image") and _norm(svc["image"]) not in have_images:
            out.append(("info", f"{name}: {svc['image']} will be downloaded first"))
        for ef in svc.get("env_file") or []:
            path = ef.get("path") if isinstance(ef, dict) else ef
            required = ef.get("required", True) if isinstance(ef, dict) else True
            if path and required and not os.path.exists(os.path.join(base, path)):
                out.append(("error", f"{name} needs the env file {path}, which doesn't exist"))
    vol_names = {v.get("Name") for v in volumes or []}
    for key, v in (cfg.get("volumes") or {}).items():
        if (v or {}).get("external") and (v.get("name") or key) not in vol_names:
            out.append(("error", f"The external volume {v.get('name') or key} doesn't exist — create it first"))
    net_names = {n.get("Name") for n in networks or []}
    for key, n in (cfg.get("networks") or {}).items():
        if (n or {}).get("external") and (n.get("name") or key) not in net_names:
            out.append(("error", f"The external network {n.get('name') or key} doesn't exist — create it first"))
    return out


def _expand(pub):
    s = str(pub)
    if "-" in s:
        a, _, b = s.partition("-")
        if a.isdigit() and b.isdigit() and int(b) - int(a) < 200:
            return list(range(int(a), int(b) + 1))
    return [int(s)] if s.isdigit() else []


def _norm(image):
    return image if ":" in image.rsplit("/", 1)[-1] else image + ":latest"


def _looks_like_db(svc):
    img = (svc.get("image") or "").lower()
    return any(w in img for w in ("postgres", "mysql", "mariadb", "mongo", "redis", "valkey", "elasticsearch",
                                  "opensearch", "clickhouse", "cassandra", "influxdb", "couchdb", "neo4j"))


def _plain(msg):
    """Compose's own errors, said plainly where we recognise them."""
    m = re.search(r"env file (\S+) not found", msg)
    if m:
        return f"The env file {os.path.basename(m.group(1))} doesn't exist (next to the compose file: {m.group(1)})"
    m = re.search(r"yaml.*?(L\d+)", msg, re.I)
    if m or "yaml" in msg.lower():
        return f"The file isn't valid YAML — {msg.split(':', 1)[-1].strip()}"
    m = re.search(r"service \"?([\w.-]+)\"? refers to undefined (volume|network) ([\w.-]+)", msg)
    if m:
        return f"{m.group(1)} uses the {m.group(2)} {m.group(3)}, which the file doesn't define"
    return msg.replace("validating ", "").strip()
