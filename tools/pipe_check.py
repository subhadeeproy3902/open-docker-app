"""Berth ↔ Docker on Windows through the named pipe (\\\\.\\pipe\\docker_engine), end to end — run by CI on a
Windows runner that has Docker. Uses only berth.engine (no GTK needed).

    python tools/pipe_check.py"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from berth.engine import WIN_PIPE, Engine, socket_path  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  ({str(detail)[:200]})" if detail != "" else ""), flush=True)


check("default engine address on Windows is the named pipe", socket_path() == WIN_PIPE, socket_path())
e = Engine()
check("ping", e.req("GET", "/_ping", timeout=20) == "OK")
v = e.version() or {}
check("engine version", v.get("Version"), f"{v.get('Version')} {v.get('Os')}/{v.get('Arch')}")
check("alive()", e.alive())
check("list containers and images", isinstance(e.req("GET", "/containers/json", {"all": "1"}), list) and
      isinstance(e.images(), list))
st = e.stream("/events", {"since": "0", "until": str(int(time.time()))})
t0, n = time.time(), 0
while (c := st.read(3.0)) not in (b"", None) and time.time() - t0 < 30:
    n += len(c)
st.close()
check("a finite stream (events) ends cleanly", True, f"{n} bytes")
st = e.stream("/events", {"filters": {"type": ["plugin"]}})
t0 = time.time()
r = st.read(0.8)
st.close()
check("a quiet live stream times out instead of hanging", r is None and time.time() - t0 < 5, f"{time.time() - t0:.2f}s")

# a real container: pull, create, start, logs (multiplexed stream), remove
img = "mcr.microsoft.com/windows/nanoserver:ltsc2022" if (v.get("Os") or "") == "windows" else "busybox:latest"
name, tag = img.rsplit(":", 1)
pull = e.stream("/images/create", {"fromImage": name, "tag": tag}, method="POST")
t0 = time.time()
while pull.read(5.0) != b"" and time.time() - t0 < 600:
    pass
pull.close()
cmd = ["cmd", "/c", "echo hello from berth"] if "windows" in img else ["echo", "hello from berth"]
cid = e.create_container("berth-pipe-check", {"Image": img, "Cmd": cmd})["Id"]
try:
    e.action(cid, "start")
    time.sleep(8)
    logs = e.stream(f"/containers/{cid}/logs", {"stdout": "1", "stderr": "1"})
    buf = b""
    while (c := logs.read(3.0)) not in (b"", None):
        buf += c
    logs.close()
    check("run a container and read its logs through the pipe", b"hello from berth" in buf, buf[-80:])
finally:
    e.remove_container(cid, force=True)
check("the container is gone again", not [c for c in e.req("GET", "/containers/json", {"all": "1"})
                                         if c["Id"] == cid])
bad = results.count(False)
print(f"\n{len(results) - bad}/{len(results)} passed")
sys.exit(1 if bad else 0)
