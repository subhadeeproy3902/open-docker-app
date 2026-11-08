"""Measure Berth's own footprint:  python3 tools/bench.py   (any display; broadway works for headless runs)
Prints start-up time, memory (RSS) and CPU with live stats on and paused."""
import os, sys, time
T0 = time.time()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gio, GLib
from berth import app as A

TCK = os.sysconf("SC_CLK_TCK")
cpu = lambda: sum(int(x) for x in open("/proc/self/stat").read().rsplit(")", 1)[1].split()[11:15]) / TCK
rss = lambda: int(next(l for l in open("/proc/self/status") if l.startswith("VmRSS")).split()[1]) / 1024
res = {}


def scenario(app):
    win = app.win
    while not (win.store.up and win.store.containers):
        yield 0.1
    res["ready_s"] = round(time.time() - T0, 2)
    res["containers"] = sum(1 for c in win.store.containers if c.get("State") == "running")
    win.go("containers")
    win.store.want_stats(True)
    yield 10
    c0 = cpu(); yield 30
    res["cpu_live_pct_of_one_core"] = round((cpu() - c0) / 30 * 100, 2)
    res["rss_live_mb"] = round(rss())
    win.store.want_stats(False)                 # what the resource saver does when the window loses focus
    yield 3
    c0 = cpu(); yield 30
    res["cpu_paused_pct_of_one_core"] = round((cpu() - c0) / 30 * 100, 2)
    res["rss_paused_mb"] = round(rss())
    print(res, flush=True)
    app.quit()


app = A.App(shot=None)
app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
g = {}


def step():
    try:
        d = next(g["g"])
    except StopIteration:
        return False
    GLib.timeout_add(int(d * 1000), step); return False


app.connect("activate", lambda *_: GLib.idle_add(lambda: (g.__setitem__("g", scenario(app)), GLib.timeout_add(100, step)) and False))
app.run([])
