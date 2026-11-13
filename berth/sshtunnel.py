"""Remote engines over SSH: ssh://user@host[:port][/path/to/docker.sock].

One `ssh -N -L` process per remote engine forwards a free local port to the remote Docker socket; Berth then talks
to 127.0.0.1:<port> like any TCP engine. Uses the system's OpenSSH (Linux, macOS, Windows 10+) and your SSH keys or
agent — Berth never asks for or stores a password. The docker CLI follows on its own (DOCKER_HOST=ssh://… is
standard), so Compose and Exec work too."""
import atexit
import os
import shlex
import socket
import subprocess
import threading
import time
import urllib.parse

from .util import host

_lock = threading.Lock()
_tunnels = {}                 # address → (process, local port)


def parse(address):
    """(user@host, port or None, remote socket path)."""
    u = urllib.parse.urlsplit(address)
    if u.scheme != "ssh" or not u.hostname:
        raise ValueError(f"Not an ssh:// address: {address}")
    target = (f"{u.username}@" if u.username else "") + u.hostname
    return target, u.port, (u.path if u.path and u.path != "/" else "/var/run/docker.sock")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _explain(err, target):
    e = (err or "").strip()
    low = e.lower()
    if "permission denied" in low:
        return (f"Couldn't sign in to {target} over SSH ({e.splitlines()[-1]}). Berth uses your SSH keys: set one up "
                f"with  ssh-copy-id {target}  (or add the key to your SSH agent), then try again.")
    if "could not resolve" in low or "name or service not known" in low:
        return f"Can't find the computer {target.split('@')[-1]} — check the name."
    if "connection refused" in low or "timed out" in low or "no route" in low:
        return f"{target} doesn't answer on SSH ({e.splitlines()[-1] if e else 'no reply'})."
    if "docker.sock" in low or "open failed" in low:
        return f"Signed in to {target}, but its Docker socket isn't reachable: is Docker running there, and is your " \
               f"user allowed to use it (the docker group)?"
    return f"SSH to {target} failed: {e.splitlines()[-1] if e else 'it exited'}"


def ensure(address, timeout=20):
    """The local tcp:// address for this ssh:// engine, starting the tunnel if needed (blocks; call from a worker)."""
    with _lock:
        t = _tunnels.get(address)
        if t and t[0].poll() is None:
            return f"tcp://127.0.0.1:{t[1]}"
        target, port, sock = parse(address)
        local = _free_port()
        argv = ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=20",
                "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=accept-new",
                "-L", f"127.0.0.1:{local}:{sock}"]
        if port:
            argv += ["-p", str(port)]
        argv += shlex.split(os.environ.get("BERTH_SSH_ARGS", ""))     # extra options (tests, unusual setups)
        argv.append(target)
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            p = subprocess.Popen(host(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, **kw)
        except FileNotFoundError:
            raise ConnectionError("SSH isn't installed on this computer (Berth uses the system's OpenSSH client)")
        t0 = time.time()
        while time.time() - t0 < timeout:
            if p.poll() is not None:
                raise ConnectionError(_explain(p.stderr.read().decode(errors="replace"), target))
            try:
                socket.create_connection(("127.0.0.1", local), timeout=1).close()
                _tunnels[address] = (p, local)
                return f"tcp://127.0.0.1:{local}"
            except OSError:
                time.sleep(0.2)
        p.kill()
        raise ConnectionError(f"SSH to {target} took too long to connect")


def close(address=None):
    with _lock:
        for a in [address] if address else list(_tunnels):
            t = _tunnels.pop(a, None)
            if t and t[0].poll() is None:
                t[0].terminate()


atexit.register(close)
