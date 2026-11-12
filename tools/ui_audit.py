"""UI audit (Rune-style "snapshot" check): open every page and the main dialogs off-screen, walk the widget tree and
report what a user would miss — icon-only buttons without a tooltip, text fields without a hint. Writes the element
tree of each screen as JSON next to a PNG, so changes can be compared.

    gtk4-broadwayd :9 &
    GDK_BACKEND=broadway BROADWAY_DISPLAY=:9 XDG_CONFIG_HOME=<cfg> python3 tools/ui_audit.py OUTDIR
Exit code 1 when something is missing."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BERTH_NO_WELCOME", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from berth import app as A  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "ui-audit"
os.makedirs(OUT, exist_ok=True)
problems, seen = [], set()


def walk(w):
    yield w
    c = w.get_first_child()
    while c:
        yield from walk(c)
        c = c.get_next_sibling()


def icon_only(b):
    if isinstance(b, Gtk.MenuButton):
        return not b.get_label() and (b.get_icon_name() or not b.get_child() or isinstance(b.get_child(), Gtk.Image))
    if isinstance(b, (Gtk.Button, Gtk.ToggleButton)):
        if b.get_label():
            return False
        ch = b.get_child()
        if isinstance(ch, Gtk.Image) or b.get_icon_name():
            return True
        if isinstance(ch, Adw.ButtonContent):
            return not ch.get_label()
    return False


def has_tip(w):
    p = w
    for _ in range(2):                         # a MenuButton's inner button carries no tip; its parent does
        if p is None:
            break
        if p.get_tooltip_text() or p.get_tooltip_markup():
            return True
        p = p.get_parent()
    return False


def audit(screen, root):
    tree = []
    for w in walk(root):
        if not w.get_mapped():
            continue
        name = type(w).__name__
        node = {"type": name, "css": list(w.get_css_classes())}
        if isinstance(w, Gtk.Label):
            node["text"] = w.get_text()[:60]
        if isinstance(w, (Gtk.Button, Gtk.MenuButton)):
            node["label"] = w.get_label() or ""
            node["tip"] = w.get_tooltip_text() or ""
            parent = w.get_parent()
            inner = isinstance(parent, (Gtk.MenuButton, Adw.SplitButton)) or \
                w.get_ancestor(Gtk.WindowControls) is not None
            if icon_only(w) and not has_tip(w) and not inner:
                icon = w.get_icon_name() if hasattr(w, "get_icon_name") else ""
                key = (screen, icon or "", name)
                if key not in seen:
                    seen.add(key)
                    problems.append(f"{screen}: icon-only {name} without tooltip (icon {icon or '?'})")
        if isinstance(w, (Gtk.Entry, Gtk.SearchEntry, Gtk.Text)) and not w.get_ancestor(Adw.EntryRow) \
                and not w.get_ancestor(Gtk.SpinButton) and not w.get_ancestor(Gtk.DropDown):
            if isinstance(w, Gtk.Text) and w.get_ancestor(Gtk.Entry) or isinstance(w, Gtk.Text) and \
                    w.get_ancestor(Gtk.SearchEntry) or isinstance(w, Gtk.Text) and w.get_ancestor(Gtk.PasswordEntry):
                pass
            elif not (w.get_placeholder_text() or w.get_text()):
                problems.append(f"{screen}: text field without a hint ({name})")
        tree.append(node)
    with open(os.path.join(OUT, f"{screen}.json"), "w") as f:
        json.dump(tree, f)


app = A.App(shot=None)
app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)


def shot(w, name):
    for _ in range(40):
        try:
            w.screenshot(os.path.join(OUT, name + ".png"))
            return
        except RuntimeError:
            yield 0.1


def scen():
    w = app.win
    w.set_size_request(1600, 1000)
    t = 0
    while not (w.store.up and w.store.containers) and t < 60:
        t += 1
        yield 0.5
    yield 2
    from berth.app import PAGES
    for key, _cls in PAGES:
        w.go(key)
        yield 2.5
        audit(f"page-{key}", w)
        yield from shot(w, f"page-{key}")
    # container detail, every tab
    cp = w.pages["containers"]
    run = [c for c in w.store.containers if c.get("State") == "running"]
    if run:
        w.go("containers")
        cp.open_detail(run[0]["Id"])
        yield 2
        det = cp.nav.get_visible_page()
        for tab in ("logs", "inspect", "mounts", "exec", "files", "stats"):
            det.tabs.set_visible_child_name(tab)
            yield 2
            audit(f"container-{tab}", w)
        cp.nav.pop_to_tag("list")
    # a database explorer (read-only: it only connects and lists)
    from berth import db
    dbc = next((c for c in w.store.containers if c.get("State") == "running" and db.detect(c) == "postgres"), None)
    if dbc:
        dp = w.pages["databases"]
        w.go("databases")
        yield 1
        dp.open(dbc["Id"])
        for _ in range(60):
            if dp.nav.get_visible_page().get_tag() != "list":
                break
            yield 0.25
        yield 4
        audit("database-explorer", w)
        yield from shot(w, "database-explorer")
        dp.nav.pop_to_tag("list")
    # dialogs
    from berth import quick, send_page, sharing_page
    dialogs = [("dialog-share", lambda: sharing_page.ShareDialog(w.pages["sharing"])),
               ("dialog-address", lambda: sharing_page.ExposeDialog(w.pages["sharing"])),
               ("dialog-send-options", lambda: send_page.OptionsDialog(w.pages["send"], [os.path.abspath(__file__)]))]
    if run:
        dialogs.append(("dialog-ports", lambda: quick.PortsDialog(w, w.store, run[0]["Id"])))
    for name, make in dialogs:
        d = make()
        d.present(w)
        yield 1.5
        audit(name, d)
        d.force_close()
        yield 0.5
    app.quit()


g = {}


def step():
    try:
        d = next(g["g"])
    except StopIteration:
        return False
    GLib.timeout_add(int(d * 1000), step)
    return False


app.connect("activate", lambda *_: GLib.idle_add(lambda: (g.__setitem__("g", scen()), GLib.timeout_add(800, step))
                                                and False))
app.run([])
print("\n".join(problems) or "No problems found")
print(f"\n{len(problems)} problem(s) · element trees + PNGs in {OUT}")
sys.exit(1 if problems else 0)
