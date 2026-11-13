"""Remote engines over SSH, end to end, against the demo engine only (tools/demo/up.sh):
an SSH server runs inside the demo engine with its Docker socket; Berth connects with ssh://root@<demo ip>:2222
using a throwaway key. Nothing touches the engine Berth normally uses.

    sh tools/demo/up.sh && python3 tests/ssh.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from berth.engine import Engine, EngineError  # noqa: E402

DEMO = "tcp://127.0.0.1:23750"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  ({str(detail)[:160]})" if detail != "" else ""), flush=True)


def demo(*args, **kw):
    return subprocess.run(["docker", *args], env={**os.environ, "DOCKER_HOST": DEMO}, capture_output=True, text=True,
                          **kw)


def main():
    if not shutil.which("ssh") or not shutil.which("ssh-keygen"):
        print("SKIP: OpenSSH client not installed")
        return 0
    ip = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                         "berth-demo-engine"], capture_output=True, text=True).stdout.strip()
    if not ip:
        sys.exit("The demo engine isn't running: sh tools/demo/up.sh")
    tmp = tempfile.mkdtemp(prefix="berth-test-ssh-")
    key = os.path.join(tmp, "key")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key], check=True)
    demo("rm", "-f", "berth-test-sshd")
    setup = ('apk add --no-cache openssh >/dev/null && ssh-keygen -A >/dev/null && mkdir -p /root/.ssh && '
             'echo "$KEY" > /root/.ssh/authorized_keys && chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys'
             ' && echo "root:$(head -c 12 /dev/urandom | base64)" | chpasswd && '
             'sed -i "s/^AllowTcpForwarding no/AllowTcpForwarding yes/" /etc/ssh/sshd_config && '
             'echo "PasswordAuthentication no" >> /etc/ssh/sshd_config && exec /usr/sbin/sshd -D -e')
    r = demo("run", "-d", "--name", "berth-test-sshd", "-p", "2222:22", "-v", "/var/run/docker.sock:/var/run/docker.sock",
             "-e", "KEY=" + open(key + ".pub").read().strip(), "alpine:3.20", "sh", "-c", setup)
    check("ssh server starts inside the demo engine", r.returncode == 0, r.stderr)
    os.environ["BERTH_SSH_ARGS"] = f"-i {key} -o UserKnownHostsFile=/dev/null -o IdentitiesOnly=yes"
    addr = f"ssh://root@{ip}:2222"
    e = Engine(addr)
    ok, t0 = False, time.time()
    while time.time() - t0 < 90:
        try:
            ok = e.req("GET", "/_ping") == "OK"
            break
        except EngineError:
            time.sleep(2)
    check("ssh:// engine answers", ok)
    names = sorted(c["Names"][0] for c in e.req("GET", "/containers/json", {"all": "1"}) or [])
    check("lists the remote engine's containers", "/shop-db-1" in names and "/berth-test-sshd" in names, names[:6])
    t = time.time()
    for _ in range(20):
        e.req("GET", "/_ping")
    check("one tunnel serves many requests quickly", time.time() - t < 3, f"{time.time() - t:.2f}s")
    st = e.stream("/events", {"since": "0", "until": str(int(time.time()))})
    n = 0
    while (c := st.read(3.0)) not in (b"", None):
        n += len(c)
    check("streams (events, logs) work through the tunnel", n > 0, n)
    info = e.req("GET", "/info") or {}
    check("engine info through the tunnel", bool(info.get("ServerVersion")), info.get("ServerVersion"))
    try:
        Engine(f"ssh://nobody@{ip}:2222").req("GET", "/_ping")
        check("a wrong user is explained", False)
    except EngineError as x:
        check("a wrong user is explained", "ssh-copy-id" in str(x), x)
    try:
        Engine("ssh://root@no-such-host.invalid").req("GET", "/_ping")
        check("an unknown host is explained", False)
    except EngineError as x:
        check("an unknown host is explained", "find the computer" in str(x), x)
    from berth.sshtunnel import close
    close()
    demo("rm", "-f", "berth-test-sshd")
    shutil.rmtree(tmp, ignore_errors=True)
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f" — failed: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
