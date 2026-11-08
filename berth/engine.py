"""Docker Engine API client over the local unix socket.

Every call here BLOCKS, so the UI only calls them from worker threads (see util.bg).
No third-party dependencies: http.client for normal requests, a raw HTTP/1.0
socket for streams (logs, events, pull), so nothing is chunk-encoded.
"""
import http.client
import json
import os
import select
import socket
import urllib.parse


def _candidates():
    """Where Docker-compatible engines usually listen, per platform: [(label, address)]."""
    home = os.path.expanduser("~")
    uid = os.getuid() if hasattr(os, "getuid") else 0
    rt = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    return [("Docker (system)", "/var/run/docker.sock"), ("Docker (rootless)", os.path.join(rt, "docker.sock")),
            ("Podman (your user)", os.path.join(rt, "podman", "podman.sock")), ("Podman (system)", "/run/podman/podman.sock"),
            ("Docker Desktop", os.path.join(home, ".docker", "run", "docker.sock")),
            ("Docker Desktop (Linux)", os.path.join(home, ".docker", "desktop", "docker.sock")),
            ("Colima", os.path.join(home, ".colima", "default", "docker.sock")),
            ("OrbStack", os.path.join(home, ".orbstack", "run", "docker.sock")),
            ("Rancher Desktop", os.path.join(home, ".rd", "docker.sock"))]


def socket_path():
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        return host[7:]
    if host.startswith("tcp://"):
        return host
    if os.name == "nt":                       # Docker Desktop on Windows: enable "Expose daemon on tcp://localhost:2375"
        return "tcp://localhost:2375"
    for _label, p in _candidates():
        if os.path.exists(p):
            return p
    return "/var/run/docker.sock"


def _open(address, timeout=None):
    """Connected socket to the engine: a unix socket path, unix://path, or tcp://host:port."""
    if address.startswith("tcp://"):
        hostport = address[6:].rstrip("/")
        host, _, port = hostport.rpartition(":")
        return socket.create_connection((host or "localhost", int(port or 2375)), timeout=timeout)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if timeout:
        s.settimeout(timeout)
    s.connect(address[7:] if address.startswith("unix://") else address)
    return s


def discover():
    """Engines Berth can talk to on this machine: [(label, address)]."""
    out = []
    for label, p in _candidates():
        if os.path.exists(p) and all(os.path.realpath(p) != os.path.realpath(a) for _l, a in out):
            out.append((label, p))
    if os.name == "nt":
        out.append(("Docker Desktop (tcp)", "tcp://localhost:2375"))
    try:
        import subprocess
        from .util import host
        r = subprocess.run(host(["docker", "context", "ls", "--format", "{{json .}}"]), capture_output=True, text=True, timeout=5)
        for ln in r.stdout.splitlines():
            try:
                c = json.loads(ln)
            except ValueError:
                continue
            ep = c.get("DockerEndpoint", "")
            addr = ep[7:] if ep.startswith("unix://") else ep if ep.startswith("tcp://") else None
            if addr and all(a != addr for _l, a in out):
                out.append((f"Context: {c.get('Name')}", addr))
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def diagnose(address):
    """Why an engine can or cannot be used, for onboarding and the 'engine is off' screen.
    Returns (state, detail): running (detail = version) · stopped · denied · missing · unreachable (detail = error)."""
    if address.startswith("tcp://"):
        try:
            return "running", (Engine(address).version() or {}).get("Version", "")
        except (EngineError, OSError) as e:
            return "unreachable", str(e)
    path = address[7:] if address.startswith("unix://") else address
    if not os.path.exists(path):
        from .util import host_which
        installed = any(host_which(b) for b in ("dockerd", "docker", "podman", "colima", "orbctl")) \
            or os.path.exists("/usr/lib/systemd/system/docker.service")
        return ("stopped" if installed else "missing"), ""
    try:
        _open(path, timeout=2).close()
    except PermissionError:
        return "denied", ""
    except OSError:
        return "stopped", ""                  # a socket file is left over but nothing listens
    try:
        return "running", (Engine(address).version() or {}).get("Version", "")
    except (EngineError, OSError) as e:
        return "unreachable", str(e)


def docker_host_env(address):
    return address if address.startswith(("tcp://", "unix://")) else "unix://" + address


class EngineError(Exception):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


class _Conn(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("docker", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = _open(self._path, self.timeout)


def _q(query):
    if not query:
        return ""
    clean = {}
    for k, v in query.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "1" if v else "0"
        elif isinstance(v, (dict, list)):
            v = json.dumps(v)
        clean[k] = v
    return "?" + urllib.parse.urlencode(clean)


class Stream:
    """A long-lived response (logs/events/pull). read() returns bytes, None on
    timeout, b"" when the engine closed it. close() from any thread stops it."""

    def __init__(self, path, method="GET", body=None, sock_path=None):
        self.sock = _open(sock_path or socket_path())
        payload = json.dumps(body).encode() if body is not None else b""
        head = f"{method} {path} HTTP/1.0\r\nHost: docker\r\n"
        if body is not None:
            head += "Content-Type: application/json\r\n"
        head += f"Content-Length: {len(payload)}\r\n\r\n"
        self.sock.sendall(head.encode() + payload)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        header, _, self.pending = buf.partition(b"\r\n\r\n")
        try:
            self.status = int(header.split(b" ", 2)[1])
        except (IndexError, ValueError):
            self.status = 500
        if self.status >= 400:
            rest = self.pending + self._drain()
            self.close()
            raise EngineError(_message(rest) or f"HTTP {self.status}", self.status)
        self.closed = False

    def _drain(self):
        out = b""
        self.sock.settimeout(2)
        try:
            while True:
                c = self.sock.recv(65536)
                if not c:
                    break
                out += c
        except OSError:
            pass
        return out

    def read(self, timeout=1.0):
        if self.pending:
            p, self.pending = self.pending, b""
            return p
        if self.closed:
            return b""
        try:
            r, _, _ = select.select([self.sock], [], [], timeout)
            if not r:
                return None
            return self.sock.recv(65536)
        except (OSError, ValueError):
            return b""

    def close(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _message(raw):
    try:
        d = json.loads(raw)
        return d.get("message") if isinstance(d, dict) else str(d)
    except (ValueError, TypeError):
        return raw.decode(errors="replace").strip()[:300] if raw else ""


def demux(buf):
    """Split Docker's multiplexed stdout/stderr frames. Returns ([(stream, bytes)], rest)."""
    out = []
    while len(buf) >= 8:
        size = int.from_bytes(buf[4:8], "big")
        if len(buf) < 8 + size:
            break
        out.append((2 if buf[0] == 2 else 1, buf[8:8 + size]))
        buf = buf[8 + size:]
    return out, buf


class Engine:
    def __init__(self, address=None):
        self.path = address or socket_path()

    # ── plumbing ──
    def req(self, method, path, query=None, body=None, timeout=60, raw=None, ctype=None):
        c = _Conn(self.path, timeout)
        try:
            if raw is not None:
                hdr, payload = {"Content-Type": ctype or "application/octet-stream"}, raw
            else:
                hdr = {"Content-Type": "application/json"} if body is not None else {}
                payload = None if body is None else json.dumps(body)
            c.request(method, path + _q(query), body=payload, headers=hdr)
            r = c.getresponse()
            raw = r.read()
        except FileNotFoundError:
            raise EngineError("Docker Engine is not running", 0)
        except socket.gaierror as e:
            raise EngineError(f"Engine address: {e}", 0)
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            raise EngineError(f"Docker Engine: {e}", 0)
        finally:
            c.close()
        if r.status >= 400:
            raise EngineError(_message(raw) or f"HTTP {r.status}", r.status)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace")

    def stream(self, path, query=None, method="GET", body=None):
        return Stream(path + _q(query), method, body, self.path)

    def alive(self):
        if not self.path.startswith("tcp://") and not os.path.exists(self.path.replace("unix://", "")):
            return False
        try:
            return self.req("GET", "/_ping", timeout=2) == "OK"
        except EngineError:
            return False

    # ── system ──
    def info(self):
        return self.req("GET", "/info", timeout=10)

    def version(self):
        return self.req("GET", "/version", timeout=10)

    def df(self):
        return self.req("GET", "/system/df", timeout=300)

    def events(self, filters):
        return self.stream("/events", {"filters": filters})

    # ── containers ──
    def containers(self):
        return self.req("GET", "/containers/json", {"all": True}, timeout=20)

    def inspect(self, cid):
        return self.req("GET", f"/containers/{cid}/json")

    def top(self, cid):
        return self.req("GET", f"/containers/{cid}/top", {"ps_args": "-eo pid,user,pcpu,rss,etime,args"})

    def action(self, cid, verb):
        return self.req("POST", f"/containers/{cid}/{verb}", timeout=180)

    def remove_container(self, cid, force=True):
        return self.req("DELETE", f"/containers/{cid}", {"force": force, "v": False}, timeout=120)

    def logs(self, cid, tail="500", follow=True, since=None, until=None):
        return self.stream(f"/containers/{cid}/logs",
                           {"follow": follow, "stdout": True, "stderr": True, "timestamps": True, "tail": tail,
                            "since": since, "until": until})

    def create_container(self, name, config):
        return self.req("POST", "/containers/create", {"name": name or None}, body=config)

    def stats_once(self, cid):
        return self.req("GET", f"/containers/{cid}/stats", {"stream": False, "one-shot": True}, timeout=10)

    def exec_run(self, cid, cmd, limit=512 * 1024, timeout=20):
        """Run a non-interactive command in a running container; returns (exit_code, stdout, stderr)."""
        ex = self.req("POST", f"/containers/{cid}/exec",
                      body={"AttachStdout": True, "AttachStderr": True, "Tty": False, "Cmd": cmd})
        st = self.stream(f"/exec/{ex['Id']}/start", method="POST", body={"Detach": False, "Tty": False})
        buf, out, err, waited = b"", b"", b"", 0.0
        try:
            while len(out) + len(err) < limit:
                c = st.read(0.5)
                if c is None:
                    waited += 0.5
                    if waited > timeout:
                        break
                    continue
                if not c:
                    break
                frames, buf = demux(buf + c)
                for s, data in frames:
                    if s == 2:
                        err += data
                    else:
                        out += data
        finally:
            st.close()
        code = (self.req("GET", f"/exec/{ex['Id']}/json") or {}).get("ExitCode")
        return code, out.decode(errors="replace"), err.decode(errors="replace")

    # ── images ──
    def images(self):
        return self.req("GET", "/images/json", timeout=60)

    def image_inspect(self, iid):
        return self.req("GET", f"/images/{iid}/json")

    def image_history(self, iid):
        return self.req("GET", f"/images/{iid}/history")

    def remove_image(self, iid, force=False):
        return self.req("DELETE", f"/images/{iid}", {"force": force}, timeout=120)

    def search(self, term, limit=25):
        return self.req("GET", "/images/search", {"term": term, "limit": limit}, timeout=30)

    def pull(self, ref):
        name, tag = ref, "latest"
        last = ref.rsplit("/", 1)[-1]
        if "@" in ref:
            name, tag = ref, None
        elif ":" in last:
            name, tag = ref.rsplit(":", 1)
        return self.stream("/images/create", {"fromImage": name, "tag": tag}, method="POST")

    # ── volumes / networks / prune ──
    def volumes(self):
        return (self.req("GET", "/volumes") or {}).get("Volumes") or []

    def create_volume(self, name):
        return self.req("POST", "/volumes/create", body={"Name": name})

    def remove_volume(self, name):
        return self.req("DELETE", f"/volumes/{urllib.parse.quote(name, safe='')}")

    def networks(self):
        return self.req("GET", "/networks") or []

    def remove_network(self, nid):
        return self.req("DELETE", f"/networks/{nid}")

    def prune(self, kind, filters=None):
        path = {"images": "/images/prune", "containers": "/containers/prune", "volumes": "/volumes/prune",
                "networks": "/networks/prune", "builder": "/build/prune"}[kind]
        q = {"filters": filters} if filters else {}
        if kind == "builder":
            q["all"] = True
        return self.req("POST", path, q, timeout=900) or {}

    # ── editing ──
    def update(self, cid, body):
        """Change restart policy / memory / CPU limits of a container without recreating it."""
        return self.req("POST", f"/containers/{cid}/update", body=body)

    def rename(self, cid, name):
        return self.req("POST", f"/containers/{cid}/rename", {"name": name})

    def tag(self, iid, repo, tag):
        return self.req("POST", f"/images/{iid}/tag", {"repo": repo, "tag": tag})

    def create_network(self, name, driver="bridge"):
        return self.req("POST", "/networks/create", body={"Name": name, "Driver": driver})

    def connect_network(self, nid, cid):
        return self.req("POST", f"/networks/{nid}/connect", body={"Container": cid})

    def disconnect_network(self, nid, cid):
        return self.req("POST", f"/networks/{nid}/disconnect", body={"Container": cid, "Force": True})

    def containers_filtered(self, filters):
        return self.req("GET", "/containers/json", {"all": True, "filters": filters}, timeout=20)

    def get_archive(self, cid, path, limit=512 * 1024 * 1024):
        """A tar of `path` inside the container (works on stopped containers too)."""
        st = self.stream(f"/containers/{cid}/archive", {"path": path})
        out = bytearray()
        try:
            while len(out) < limit:
                c = st.read(5.0)
                if c is None:
                    continue
                if not c:
                    break
                out += c
        finally:
            st.close()
        return bytes(out)

    def put_archive(self, cid, path, tar_bytes):
        """Extract a tar into `path` inside the container."""
        return self.req("PUT", f"/containers/{cid}/archive", {"path": path}, raw=tar_bytes,
                        ctype="application/x-tar", timeout=600)

    # ── import / export / commit ──
    def stream_to_file(self, path, query, dest, method="GET"):
        """Write a (possibly large) response body straight to a file."""
        st = self.stream(path, query, method=method)
        n = 0
        try:
            with open(dest, "wb") as f:
                while True:
                    c = st.read(10.0)
                    if c is None:
                        continue
                    if not c:
                        break
                    f.write(c)
                    n += len(c)
        finally:
            st.close()
        return n

    def save_image(self, ref, dest):
        return self.stream_to_file(f"/images/{ref}/get", None, dest)

    def export_container(self, cid, dest):
        return self.stream_to_file(f"/containers/{cid}/export", None, dest)

    def load_image(self, src):
        """POST a docker-save tar file to /images/load (streams the file from disk)."""
        size = os.path.getsize(src)
        c = _Conn(self.path, 3600)
        try:
            with open(src, "rb") as f:
                c.request("POST", "/images/load?quiet=1", body=f,
                          headers={"Content-Type": "application/x-tar", "Content-Length": str(size)})
                r = c.getresponse()
                raw = r.read()
        finally:
            c.close()
        if r.status >= 400:
            raise EngineError(_message(raw) or f"HTTP {r.status}", r.status)
        return raw.decode(errors="replace")

    def commit(self, cid, repo, tag, comment=""):
        return self.req("POST", "/commit", {"container": cid, "repo": repo, "tag": tag, "comment": comment}, timeout=600)

    def wait(self, cid, timeout=3600):
        return self.req("POST", f"/containers/{cid}/wait", timeout=timeout)

    def logs_text(self, cid, tail="all"):
        """All logs of a (finished) container as text, stdout and stderr separated."""
        st = self.stream(f"/containers/{cid}/logs", {"follow": False, "stdout": True, "stderr": True, "tail": tail})
        buf, out, err = b"", b"", b""
        try:
            while True:
                c = st.read(5.0)
                if c is None:
                    continue
                if not c:
                    break
                frames, buf = demux(buf + c)
                for s, d in frames:
                    if s == 2:
                        err += d
                    else:
                        out += d
        finally:
            st.close()
        return out.decode(errors="replace"), err.decode(errors="replace")

    def put_archive_file(self, cid, path, src):
        """Like put_archive, but streams a tar file from disk (for big imports)."""
        size = os.path.getsize(src)
        c = _Conn(self.path, 3600)
        try:
            with open(src, "rb") as f:
                c.request("PUT", f"/containers/{cid}/archive" + _q({"path": path}), body=f,
                          headers={"Content-Type": "application/x-tar", "Content-Length": str(size)})
                r = c.getresponse()
                raw = r.read()
        finally:
            c.close()
        if r.status >= 400:
            raise EngineError(_message(raw) or f"HTTP {r.status}", r.status)
