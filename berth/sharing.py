"""Ports & sharing: friendly local addresses (name.localhost) through one managed Caddy container, and public links
through Cloudflare Tunnel. Everything here blocks — call it through util.bg.

Local addresses (issue #26)
  * One opt-in container, `berth-proxy` (caddy:2-alpine), publishes ONLY on 127.0.0.1 (default ports 80 and 443),
    so nothing is reachable from the network. `*.localhost` resolves to the loopback address on every modern OS and
    browser, so no /etc/hosts edits are needed.
  * The proxy joins the network of each target. On user-defined networks it reaches the target by its container name
    (stable across restarts); on the default bridge, by IP (Berth re-applies the routes when a target restarts).
  * Its Caddyfile is written into the container and loaded with `caddy reload` from inside, so the admin API stays
    inside the container. HTTPS routes use Caddy's own local certificate authority (exportable from Berth).

Public links (issue #27)
  * One `cloudflare/cloudflared` container per shared port, running in the TARGET's network namespace
    (NetworkMode container:<id>), so `localhost:<port>` works for any network setup, without publishing ports.
  * Quick tunnels need no account and get a random https://….trycloudflare.com address (read from the logs).
    With a tunnel token (Cloudflare Zero Trust), the hostname is the one configured in the Cloudflare dashboard.
  * Shares stop when Berth quits unless they are marked "keep running"."""
import io
import re
import tarfile
import time

PROXY_NAME, PROXY_IMAGE, PROXY_DATA = "berth-proxy", "caddy:2-alpine", "berth-proxy-data"
TUNNEL_IMAGE = "cloudflare/cloudflared:latest"
ROLE, TARGET, PORT, KEEP, MODE = "io.berth.role", "io.berth.target", "io.berth.port", "io.berth.keep", "io.berth.mode"
EXPIRES = "io.berth.expires"          # unix time after which the link must be gone
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
ROOT_CA = "/data/caddy/pki/authorities/local/root.crt"


class ShareError(Exception):
    pass


def _labels(c):
    return c.get("Labels") or ((c.get("Config") or {}).get("Labels")) or {}


def _cname(c):
    return ((c.get("Names") or [c.get("Name") or "?"])[0]).lstrip("/")


def has_image(engine, ref):
    for im in engine.images():
        if ref in (im.get("RepoTags") or []):
            return True
    return False


def ensure_image(engine, ref, progress=None):
    if has_image(engine, ref):
        return
    if progress:
        progress(f"Downloading {ref}…")
    st = engine.pull(ref)
    last = b""
    try:
        while True:
            c = st.read(2.0)
            if c == b"":
                break
            if c:
                last = (last + c)[-4096:]
    finally:
        st.close()
    if not has_image(engine, ref):
        raise ShareError(f"Couldn't download {ref}: {last.decode(errors='replace')[-300:]}")


def container_ports(engine, cid):
    """Every TCP port the container listens on as far as Docker knows: published ones, EXPOSEd ones."""
    ins = engine.inspect(cid)
    ports = set()
    for k in ((ins.get("Config") or {}).get("ExposedPorts") or {}):
        p, _, proto = k.partition("/")
        if proto in ("", "tcp") and p.isdigit():
            ports.add(int(p))
    for k in ((ins.get("NetworkSettings") or {}).get("Ports") or {}):
        p, _, proto = k.partition("/")
        if proto in ("", "tcp") and p.isdigit():
            ports.add(int(p))
    return sorted(ports)


def published(containers):
    """Rows for the Ports overview: (container, container port, host ip, host port, reachable from the network?)."""
    out = []
    for c in containers:
        if _labels(c).get(ROLE):
            continue
        seen = set()
        for p in c.get("Ports") or []:
            if not p.get("PublicPort") or p.get("Type", "tcp") != "tcp":
                continue
            ip = p.get("IP") or "0.0.0.0"
            key = (p["PublicPort"], ip.replace("::", "0.0.0.0"))
            if key in seen:
                continue
            seen.add(key)
            out.append({"container": _cname(c), "id": c["Id"], "private": p.get("PrivatePort"), "ip": ip,
                        "public": p["PublicPort"], "lan": ip in ("0.0.0.0", "::", "")})
    out.sort(key=lambda r: r["public"])
    return out


# ── the local proxy ─────────────────────────────────────────────────────────────────────────────────────────────
def proxy(containers):
    return next((c for c in containers if _cname(c) == PROXY_NAME and _labels(c).get(ROLE) == "proxy"), None)


def url_for(route, ports):
    http_port, https_port = ports
    if route.get("https"):
        return f"https://{route['name']}.localhost" + ("" if https_port == 443 else f":{https_port}")
    return f"http://{route['name']}.localhost" + ("" if http_port == 80 else f":{http_port}")


LAN_FIRST = 8100


def lan_ports(routes):
    return sorted(int(r["lan_port"]) for r in routes if r.get("lan") and r.get("lan_port"))


def assign_lan_port(routes, route):
    """Give a route that should be reachable from phones its own port (8100, 8101, …)."""
    if route.get("lan") and not route.get("lan_port"):
        used = set(lan_ports(routes))
        p = LAN_FIRST
        while p in used:
            p += 1
        route["lan_port"] = p
    return route


def lan_url(route):
    from .fileshare import lan_ip
    return f"http://{lan_ip()}:{route['lan_port']}"


def start_proxy(engine, ports, progress=None, lan=()):
    """Create (or start) the proxy. ports = (http, https) on 127.0.0.1; lan = ports published to the whole network
    for addresses that phones should reach. Returns its id."""
    http_port, https_port = ports
    want = {80: http_port, 443: https_port, **{p: p for p in lan}}
    ensure_image(engine, PROXY_IMAGE, progress)
    existing = engine.containers_filtered({"name": [f"^/{PROXY_NAME}$"]})
    if existing:
        c = existing[0]
        bound = {p.get("PrivatePort"): p.get("PublicPort") for p in c.get("Ports") or [] if p.get("PublicPort")}
        if c["State"] != "running" and not bound and engine.inspect(c["Id"])["HostConfig"].get("PortBindings") and \
                {int(k.split("/")[0]): int(v[0]["HostPort"]) for k, v in
                 engine.inspect(c["Id"])["HostConfig"]["PortBindings"].items()} == want:
            engine.action(c["Id"], "start")
            return c["Id"]
        if bound == want:
            return c["Id"]
        engine.remove_container(c["Id"], force=True)           # ports changed: recreate (routes are re-applied)
    cfg = {"Image": PROXY_IMAGE, "Labels": {ROLE: "proxy"},
           "Cmd": ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"],
           "ExposedPorts": {f"{p}/tcp": {} for p in want},
           "HostConfig": {"PortBindings": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(http_port)}],
                                           "443/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(https_port)}],
                                           # only the addresses switched to "phones too" listen on the network
                                           **{f"{p}/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(p)}] for p in lan}},
                          "Binds": [f"{PROXY_DATA}:/data"], "RestartPolicy": {"Name": "unless-stopped"},
                          "ExtraHosts": ["host.docker.internal:host-gateway"]}}
    r = engine.create_container(PROXY_NAME, cfg)
    try:
        engine.action(r["Id"], "start")
    except Exception as e:
        engine.remove_container(r["Id"], force=True)
        msg = str(e)
        if "address already in use" in msg or "port is already allocated" in msg:
            raise ShareError(f"Port {http_port} or {https_port} is already used by another program. Pick other "
                             "ports in the proxy settings (for example 8080 and 8443).")
        raise ShareError(msg)
    return r["Id"]


def stop_proxy(engine, remove=False):
    for c in engine.containers_filtered({"name": [f"^/{PROXY_NAME}$"]}):
        if remove:
            engine.remove_container(c["Id"], force=True)
        elif c["State"] == "running":
            engine.action(c["Id"], "stop")


def _upstream(engine, proxy_id, target, port):
    """Join the proxy to a network of the target and return how Caddy should reach it (name or IP)."""
    ins = engine.inspect(target)
    if not (ins.get("State") or {}).get("Running"):
        return None
    nets = (ins.get("NetworkSettings") or {}).get("Networks") or {}
    if not nets:
        raise ShareError(f"{(ins.get('Name') or '').lstrip('/')} has no network the proxy can join")
    pnets = ((engine.inspect(proxy_id).get("NetworkSettings") or {}).get("Networks")) or {}
    user_nets = [n for n in nets if n not in ("bridge", "host", "none")]
    if "host" in nets:
        return f"host.docker.internal:{port}"
    name = user_nets[0] if user_nets else "bridge"
    if name not in pnets:
        engine.connect_network(name, proxy_id)
    if user_nets:
        return f"{(ins.get('Name') or '').lstrip('/')}:{port}"
    ip = nets["bridge"].get("IPAddress")
    return f"{ip}:{port}" if ip else None


def caddyfile(routes, upstreams):
    # disable_redirects: the HTTPS port may not be 443 on the host, so an automatic redirect would go astray
    out = ["{", "\tlocal_certs", "\tskip_install_trust", "\tadmin localhost:2019", "\tauto_https disable_redirects",
           "}", ""]
    for r in routes:
        up = upstreams.get(r["name"])
        host = f"{r['name']}.localhost"
        site = f"https://{host}" if r.get("https") else f"http://{host}"
        out.append(f"{site} {{")
        if r.get("https"):
            out.append("\ttls internal")
        if up:
            out.append(f"\treverse_proxy {up}")
        else:
            out.append(f'\trespond "{r["container"]} is not running — start it in Berth" 502')
        out.append("}")
        out.append("")
        if r.get("lan") and r.get("lan_port"):        # the same app for phones on this Wi-Fi, by port
            out.append(f"http://:{r['lan_port']} {{")
            out.append(f"\treverse_proxy {up}" if up else
                       f'\trespond "{r["container"]} is not running — start it in Berth" 502')
            out.append("}")
            out.append("")
    out.append(":80 {")
    out.append('\trespond "No Berth address with this name. Add one in Berth → Ports & sharing." 404')
    out.append("}")
    return "\n".join(out) + "\n"


def _tar(name, data):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo(name)
        ti.size, ti.mode, ti.mtime = len(data), 0o644, int(time.time())
        tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def apply_routes(engine, routes, proxy_id):
    """Write the Caddyfile for `routes` into the proxy and reload it. Returns {name: upstream or None}."""
    ups = {}
    for r in routes:
        try:
            ups[r["name"]] = _upstream(engine, proxy_id, r["container"], int(r["port"]))
        except Exception:      # target removed or not running: the route answers 502 until it is back
            ups[r["name"]] = None
    text = caddyfile(routes, ups)
    engine.put_archive(proxy_id, "/etc/caddy", _tar("Caddyfile", text.encode()))
    for _ in range(20):        # the proxy may still be starting
        code, out, err = engine.exec_run(proxy_id, ["caddy", "reload", "--config", "/etc/caddy/Caddyfile",
                                                    "--adapter", "caddyfile", "--force"], timeout=20)
        if code == 0:
            return ups
        if "connection refused" not in (err + out) and "dial tcp" not in (err + out):
            break
        time.sleep(0.5)
    raise ShareError("The proxy rejected the configuration: " + (err or out).strip()[-400:])


def signature(engine, routes):
    """What the routes currently resolve to — when it changes (a target restarted with a new IP), re-apply."""
    sig = []
    for r in routes:
        try:
            ins = engine.inspect(r["container"])
            nets = (ins.get("NetworkSettings") or {}).get("Networks") or {}
            sig.append((r["name"], (ins.get("State") or {}).get("Running"),
                        tuple(sorted((k, v.get("IPAddress")) for k, v in nets.items()))))
        except Exception:
            sig.append((r["name"], None, ()))
    return tuple(sig)


def root_certificate(engine, proxy_id):
    """Caddy's local CA certificate (PEM), to trust HTTPS on *.localhost."""
    data = engine.get_archive(proxy_id, ROOT_CA, limit=1024 * 1024)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        m = next((m for m in tf.getmembers() if m.isfile()), None)
        if not m:
            raise ShareError("The certificate is created the first time an HTTPS address is used")
        return tf.extractfile(m).read()


def validate_name(name, routes, editing=None):
    if not NAME_RE.match(name or ""):
        return "Use lower-case letters, digits and dashes (for example shop or my-api)"
    if any(r["name"] == name and r is not editing for r in routes):
        return f"{name}.localhost is already in use"
    return None


# ── public links (Cloudflare Tunnel) ────────────────────────────────────────────────────────────────────────────
def tunnels(containers):
    return [c for c in containers if _labels(c).get(ROLE) == "tunnel"]


def tunnel_name(target_name, port):
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", target_name)[:40]
    return f"berth-share-{safe}-{port}"


def start_tunnel(engine, target_id, port, token=None, keep=False, progress=None, expires=None):
    """Start a tunnel to target:port. Returns (container id, public url or None for token tunnels).
    expires: unix time when the link is removed (Berth checks while running; with keep=True on Linux a systemd user
    timer removes it even when Berth is closed)."""
    ins = engine.inspect(target_id)
    if not (ins.get("State") or {}).get("Running"):
        raise ShareError("Start the container first")
    tname = (ins.get("Name") or "").lstrip("/")
    ensure_image(engine, TUNNEL_IMAGE, progress)
    name = tunnel_name(tname, port)
    for c in engine.containers_filtered({"name": [f"^/{name}$"]}):
        engine.remove_container(c["Id"], force=True)
    cmd = ["tunnel", "--no-autoupdate"]
    env = []
    if token:
        cmd += ["run"]
        env = [f"TUNNEL_TOKEN={token}"]
    else:
        cmd += ["--url", f"http://localhost:{int(port)}"]
    cfg = {"Image": TUNNEL_IMAGE, "Cmd": cmd, "Env": env,
           "Labels": {ROLE: "tunnel", TARGET: tname, PORT: str(port), KEEP: "1" if keep else "0",
                      MODE: "token" if token else "quick", **({EXPIRES: str(int(expires))} if expires else {})},
           "HostConfig": {"NetworkMode": f"container:{ins['Id']}", "RestartPolicy": {"Name": "no"}}}
    r = engine.create_container(name, cfg)
    engine.action(r["Id"], "start")
    if expires and keep:
        schedule_removal(engine, name, expires)
    if token:
        return r["Id"], None
    if progress:
        progress("Waiting for Cloudflare to assign an address…")
    return r["Id"], wait_url(engine, r["Id"])


def wait_url(engine, cid, timeout=45):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        url = tunnel_url(engine, cid)
        if url:
            return url
        st = (engine.inspect(cid).get("State") or {})
        if not st.get("Running"):
            raise ShareError("The tunnel stopped: " + "".join(engine.logs_text(cid, tail="20")).strip()[-400:])
        time.sleep(1)
    raise ShareError("Cloudflare didn't answer in time. Check the internet connection and try again.")


def tunnel_url(engine, cid):
    text = "".join(engine.logs_text(cid, tail="200"))         # cloudflared logs on stderr
    m = URL_RE.findall(text)
    return m[-1] if m else None


def stop_tunnel(engine, cid):
    engine.remove_container(cid, force=True)


def stop_unpinned(engine, timeout=5):
    """On quit: remove every tunnel that wasn't marked 'keep running'. Returns how many were stopped."""
    n = 0
    for c in engine.containers_filtered({"label": [f"{ROLE}=tunnel"]}):
        if _labels(c).get(KEEP) != "1":
            try:
                engine.remove_container(c["Id"], force=True)
                n += 1
            except Exception:
                pass
    return n


def expires_at(c):
    try:
        return int(_labels(c).get(EXPIRES) or 0) or None
    except ValueError:
        return None


def remove_expired(engine, now=None):
    """Remove every tunnel whose expiry time has passed. Returns the names removed."""
    now = now or time.time()
    gone = []
    for c in engine.containers_filtered({"label": [f"{ROLE}=tunnel"]}):
        t = expires_at(c)
        if t and t <= now:
            try:
                engine.remove_container(c["Id"], force=True)
                gone.append(_cname(c))
            except Exception:
                pass
    return gone


def schedule_removal(engine, name, expires):
    """Backstop for links that outlive Berth: a transient systemd user timer runs `docker rm -f` at the expiry time.
    Linux with systemd only; elsewhere Berth removes expired links the next time it runs."""
    import shutil
    import subprocess
    from .engine import docker_host_env
    from .util import host, host_which
    if not (host_which("systemd-run") and host_which("docker")):
        return False
    delay = max(1, int(expires - time.time()))
    unit = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"berth-expire-{name}")
    argv = ["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}", f"--on-active={delay}s",
            f"--setenv=DOCKER_HOST={docker_host_env(engine.path)}", host_which("docker") or "docker",
            "rm", "-f", name]
    try:
        subprocess.run(["systemctl", "--user", "stop", f"{unit}.timer"] if shutil.which("systemctl") else ["true"],
                       capture_output=True, timeout=5)
        return subprocess.run(host(argv), capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
