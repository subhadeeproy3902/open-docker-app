"""Render every page off-screen and save PNGs (+ 2x2 contact sheets) — for design review and README screenshots.
  gtk4-broadwayd :9 &   GDK_BACKEND=broadway BROADWAY_DISPLAY=:9 python3 tools/shots.py OUTDIR [light|dark] [W H]"""
import os, sys, time
os.environ.setdefault("BERTH_NO_UPDATE_CHECK", "1")       # never ask GitHub for releases from a script
os.environ.setdefault("BERTH_NO_WELCOME", "1")      # the first-run welcome would cover the window
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gio, GLib, GdkPixbuf
from berth import app as A

OUT = sys.argv[1]; THEME = sys.argv[2] if len(sys.argv) > 2 else "dark"
os.makedirs(OUT, exist_ok=True)
PAGES = ["containers", "stacks", "images", "volumes", "networks", "backups", "databases", "sharing", "send", "logs", "builds", "cache", "resources", "disk", "kubernetes", "settings"]


def grab(win, name):
    """Generator: retry until a complete frame is available (yielding lets GTK finish drawing)."""
    for _ in range(80):
        try:
            win.screenshot(os.path.join(OUT, f"{THEME}-{name}.png")); return
        except RuntimeError:
            yield 0.1


def scenario(app):
    win = app.win
    win.apply_theme(THEME)
    if len(sys.argv) > 4:                          # broadway ignores the default size; force it for README shots
        win.set_size_request(int(sys.argv[3]), int(sys.argv[4]))
    t0 = time.time()
    while not (win.store.up and win.store.containers) and time.time() - t0 < 20:
        yield 0.5
    win.go("resources")
    yield 14                                     # let live stats collect a few samples first
    for p in PAGES:
        win.go(p)
        yield 5 if p in ("resources", "logs", "disk") else 2.5
        yield from grab(win, p)
    cs = sorted((c for c in win.store.containers if c.get("State") == "running"),
                key=lambda c: "api" not in c["Names"][0])            # the busiest demo container first
    if cs:
        cp = win.pages["containers"]; win.go("containers"); cp.open_detail(cs[0]["Id"])
        yield 2
        det = cp.nav.get_visible_page()
        for tab in ("logs", "inspect", "files", "stats"):
            det.tabs.set_visible_child_name(tab); yield 4 if tab == "stats" else 3
            yield from grab(win, f"container-{tab}")
    web = [c for c in win.store.containers if c.get("State") == "running" and "web" in c["Names"][0]]
    if web:                                      # the file editor with a real config file open
        from berth.files import FileBrowser
        cp.nav.pop_to_tag(cp.nav.get_navigation_stack().get_item(0).get_tag()) if cp.nav.get_navigation_stack().get_n_items() > 1 else None
        cp.open_detail(web[0]["Id"]); yield 2
        det = cp.nav.get_visible_page()
        det.tabs.set_visible_child_name("files"); yield 3
        fb, todo = None, [det]
        while todo and not fb:
            w = todo.pop()
            fb = w if isinstance(w, FileBrowser) else None
            ch = w.get_first_child()
            while ch:
                todo.append(ch); ch = ch.get_next_sibling()
        if fb:
            fb.load("/etc/nginx/conf.d"); yield 2
            fb._open(("/etc/nginx/conf.d/default.conf", False, 1200, "644")); yield 2
            yield from grab(win, "container-file-editor")
    # the database explorer with a row open in the side panel (demo shop-db-1 → customers)
    dbc = next((c for c in win.store.containers if c["Names"][0] == "/shop-db-1"), None)
    if dbc:
        dp = win.pages["databases"]; win.go("databases"); yield 1
        dp.open(dbc["Id"])
        for _ in range(80):
            ex = dp.nav.get_visible_page().get_child()
            if getattr(ex, "nodes", None) and getattr(ex, "data", None) and ex.data.result is not None:
                break
            yield 0.25
        node = next((k for n in ex.nodes for k in ([n] if n.leaf else ex.b.children(n)) if k.label == "customers"), None)
        if node:                                 # select it in the tree, as a click would
            for i in range(ex.tree_model.get_n_items()):
                if ex.tree_model.get_item(i).get_item().v is node:
                    ex.sel.set_selected(i)
                    break
            yield 3
            ex.data.open(2); yield 3
            yield from grab(win, "databases-row")
    app.quit()


def sheets():
    files = sorted(f for f in os.listdir(OUT) if f.startswith(THEME + "-") and f.endswith(".png"))
    for i in range(0, len(files), 4):
        group = files[i:i + 4]
        pbs = [GdkPixbuf.Pixbuf.new_from_file(os.path.join(OUT, f)) for f in group]
        w, h = pbs[0].get_width() // 2, pbs[0].get_height() // 2
        sheet = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, w * 2, h * 2)
        sheet.fill(0x808080ff)
        for k, pb in enumerate(pbs):
            pb.scale(sheet, (k % 2) * w, (k // 2) * h, w, h, (k % 2) * w, (k // 2) * h, 0.5, 0.5, GdkPixbuf.InterpType.BILINEAR)
        sheet.savev(os.path.join(OUT, f"sheet-{THEME}-{i // 4 + 1}.png"), "png", [], [])
        print("sheet", i // 4 + 1, ":", ", ".join(g[len(THEME) + 1:-4] for g in group), flush=True)


def main():
    app = A.App(shot=None)
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    g = {}

    def step():
        try:
            d = next(g["g"])
        except StopIteration:
            return False
        GLib.timeout_add(int(d * 1000), step); return False
    app.connect("activate", lambda *_: GLib.idle_add(lambda: (g.__setitem__("g", scenario(app)), GLib.timeout_add(1500, step)) and False))
    app.run([])
    sheets()


main()
