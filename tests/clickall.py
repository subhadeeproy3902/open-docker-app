"""Click every button / toggle / activatable row on every page and detail page, report errors and dead buttons.

Off-screen:  gtk4-broadwayd :9 &  then
  GDK_BACKEND=broadway BROADWAY_DISPLAY=:9 XDG_CONFIG_HOME=/tmp/x dbus-run-session -- python3 tests/clickall.py
Dialogs it opens are closed without confirming; only its own throwaway container `berth-clicktest` is acted on."""
import os
os.environ.setdefault("BERTH_NO_WELCOME", "1")      # the first-run welcome would cover the window
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

from berth import app as berth_app  # noqa: E402

ERRORS, DEAD, CLICKED = [], [], {}
_hook = sys.excepthook


def hook(t, v, tb):
    ERRORS.append("".join(traceback.format_exception(t, v, tb))[-1500:])
    _hook(t, v, tb)


sys.excepthook = hook
INTERNAL = (Gtk.WindowControls, Gtk.SearchEntry, Gtk.Entry, Gtk.DropDown, Gtk.SpinButton, Gtk.Expander, Gtk.PasswordEntry,
            Adw.ToggleGroup, Adw.ComboRow, Adw.SpinRow, Adw.EntryRow, Adw.ViewSwitcher, Adw.InlineViewSwitcher, Gtk.ColumnView,
            Gtk.Scrollbar, Gtk.Popover, Adw.Banner, Gtk.Calendar, Gtk.FlowBox, Gtk.ListView)
SKIP_TIPS = {"Close", "Minimize", "Maximize", "Quit", "Open in browser", "Check for known vulnerabilities (Trivy)"}


def walk(w):
    yield w
    c = w.get_first_child()
    while c is not None:
        yield from walk(c)
        c = c.get_next_sibling()


def inside(w, kinds, stop):
    p = w.get_parent()
    while p is not None and p is not stop:
        if isinstance(p, kinds):
            return True
        p = p.get_parent()
    return False


def has_handler(obj, *signals):
    for s in signals:
        sig, _, detail = s.partition("::")
        sid = GObject.signal_lookup(sig, type(obj))
        if sid and GObject.signal_has_handler_pending(obj, sid, GLib.quark_from_string(detail) if detail else 0, True):
            return True
    return False


def describe(w):
    t = w.get_tooltip_text() or ""
    lbl = w.get_label() if isinstance(w, Gtk.Button) else (w.get_title() if isinstance(w, Adw.PreferencesRow) else "")
    icon = w.get_icon_name() if isinstance(w, Gtk.Button) else ""
    return lbl or t or icon or type(w).__name__


def clickables(root, deny=()):
    out = []
    for w in walk(root):
        if not (w.get_mapped() and w.is_sensitive()):
            continue
        if isinstance(w, (Gtk.Button, Gtk.Switch)) or (isinstance(w, Gtk.ListBoxRow) and w.get_activatable()):
            if inside(w, INTERNAL, root) or isinstance(w, Gtk.LinkButton):
                continue
            d = describe(w)
            if d in SKIP_TIPS or any(x in d for x in deny):
                continue
            out.append(w)
    return out


def close_popups(win):
    dlg = win.get_visible_dialog()
    while dlg is not None:
        dlg.force_close()
        dlg = win.get_visible_dialog()
    for w in Gtk.Window.list_toplevels():
        if w is not win and w.get_visible():
            w.close()
    for w in walk(win):
        if isinstance(w, Gtk.Popover) and w.get_visible():
            w.popdown()


def click(w):
    if isinstance(w, Gtk.Switch):
        w.set_active(not w.get_active())
        w.set_active(not w.get_active())
        return has_handler(w, "notify::active", "state-set")
    if isinstance(w, Gtk.ListBoxRow):
        w.emit("activate")
        parent = w.get_parent()
        return has_handler(w, "activated", "activate") or (parent is not None and has_handler(parent, "row-activated"))
    ok = has_handler(w, "clicked", "toggled", "notify::active") or bool(w.get_action_name()) or isinstance(w, Gtk.MenuButton) \
        or inside(w, (Gtk.MenuButton,), None)
    if isinstance(w, Gtk.ToggleButton):
        w.set_active(not w.get_active())
        w.set_active(not w.get_active())
    else:
        w.emit("clicked")
    return ok


def audit(win, name, root, deny=()):
    btns = clickables(root, deny)
    n = 0
    print(f"  … {name}: {len(btns)} controls", flush=True)
    for w in btns:
        if not (w.get_mapped() and w.is_sensitive()):      # an earlier click navigated away / changed state
            continue
        before = len(ERRORS)
        try:
            alive = click(w)
        except Exception:  # noqa: BLE001
            ERRORS.append(traceback.format_exc()[-1500:])
            alive = True
        yield 0.35
        close_popups(win)
        if not alive:
            DEAD.append(f"{name}: {describe(w)} ({type(w).__name__})")
        if len(ERRORS) > before:
            ERRORS[-1] = f"[{name} → {describe(w)}]\n" + ERRORS[-1]
        n += 1
    CLICKED[name] = n


def wait_for(cond, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        yield 0.25
    return False


def scenario(app):
    win = app.win
    st, eng = win.store, win.store.engine
    yield from wait_for(lambda: st.up and st.containers)
    for c in eng.containers():
        if "/berth-clicktest" in c.get("Names", []):
            eng.remove_container(c["Id"])
    cid = eng.create_container("berth-clicktest", {"Image": "busybox:latest", "Cmd": ["sh", "-c", "while true; do echo tick; sleep 1; done"],
                                                   "ExposedPorts": {"8000/tcp": {}},
                                                   "HostConfig": {"PortBindings": {"8000/tcp": [{"HostPort": "18765"}]}}})["Id"]
    eng.action(cid, "start")
    st.refresh("containers")
    yield from wait_for(lambda: st.by_id(cid))
    for fn, arg in ((eng.remove_volume, "berth-clicktest-vol"), (eng.remove_network, "berth-clicktest-net")):
        try:
            fn(arg)
        except Exception:  # noqa: BLE001 — not there
            pass
    eng.create_volume("berth-clicktest-vol")
    eng.create_network("berth-clicktest-net")

    # sidebar + header of the window itself
    yield from audit(win, "sidebar", win.get_content().get_child().get_sidebar().get_child())
    win.apply_theme("system")
    # every page: header + body (row cells of real containers are skipped by INTERNAL=ColumnView)
    for key in ("containers", "images", "volumes", "networks", "logs", "builds", "cache", "resources", "disk", "kubernetes", "settings"):
        win.go(key)
        yield 3
        page = win.pages[key]
        yield from audit(win, f"page:{key}", page, deny=("Stop engine",))
        close_popups(win)

    # container detail: all header buttons + every tab's own controls (acts only on berth-clicktest)
    cp = win.pages["containers"]
    win.go("containers")
    cp.open_detail(cid)
    yield 2
    det = cp.nav.get_visible_page()
    for tab in ("logs", "inspect", "mounts", "exec", "files", "stats"):
        det.tabs.set_visible_child_name(tab)
        yield 3
        yield from audit(win, f"container:{tab}", det.tabs.get_child_by_name(tab), deny=())
    hb_root = det.get_child()
    yield from audit(win, "container:header", hb_root, deny=("Delete",))
    yield 2
    cp.nav.pop_to_tag("list")

    # image / volume / network / build detail pages
    ip = win.pages["images"]
    win.go("images")
    yield from wait_for(lambda: ip.model.get_n_items() > 0)
    row = next((ip.model.get_item(i) for i in range(ip.model.get_n_items()) if ip.model.get_item(i).data["repo"] == "busybox"), None)
    if row:
        ip.on_activate(row)
        yield 3
        yield from audit(win, "image:detail", ip.nav.get_visible_page(), deny=("Push",))
        ip.nav.pop_to_tag("list")
    vp = win.pages["volumes"]
    win.go("volumes")
    st.refresh("volumes")
    yield from wait_for(lambda: any(vp.model.get_item(i).key == "berth-clicktest-vol" for i in range(vp.model.get_n_items())))
    vrow = next(vp.model.get_item(i) for i in range(vp.model.get_n_items()) if vp.model.get_item(i).key == "berth-clicktest-vol")
    vp.on_activate(vrow)
    vd = vp.nav.get_visible_page()
    yield from wait_for(lambda: vd.helper is not None, 60)
    yield 2
    yield from audit(win, "volume:detail", vd)
    vp.nav.pop_to_tag("list")
    np_ = win.pages["networks"]
    win.go("networks")
    st.refresh("networks")
    yield from wait_for(lambda: any(np_.model.get_item(i).data["name"] == "berth-clicktest-net" for i in range(np_.model.get_n_items())))
    nrow = next(np_.model.get_item(i) for i in range(np_.model.get_n_items()) if np_.model.get_item(i).data["name"] == "berth-clicktest-net")
    np_.on_activate(nrow)
    yield 2
    yield from audit(win, "network:detail", np_.nav.get_visible_page(), deny=("Connect",))
    np_.nav.pop_to_tag("list")
    bp = win.pages["builds"]
    win.go("builds")
    yield from wait_for(lambda: bp.model.get_n_items() > 0, 30)
    if bp.model.get_n_items():
        bp.on_activate(bp.model.get_item(0))
        yield 3
        yield from audit(win, "build:detail", bp.nav.get_visible_page(), deny=("Delete",))
        bp.nav.pop_to_tag("list")

    # cleanup
    yield 3
    for c in eng.containers():
        if any(n.startswith("/berth-clicktest") for n in c.get("Names", [])):
            eng.remove_container(c["Id"])
    yield 2
    for fn, arg in ((eng.remove_volume, "berth-clicktest-vol"), (eng.remove_network, "berth-clicktest-net")):
        try:
            fn(arg)
        except Exception as e:  # noqa: BLE001
            print("cleanup:", e)
    print("\n=== clicked per area:", CLICKED, flush=True)
    print(f"=== total clicked: {sum(CLICKED.values())}", flush=True)
    print(f"=== errors: {len(ERRORS)}", flush=True)
    for e in ERRORS:
        print("---\n" + e, flush=True)
    print(f"=== buttons with nothing connected: {len(DEAD)}", flush=True)
    for d in DEAD:
        print("  ", d, flush=True)
    app._really_quit()


def main():
    app = berth_app.App()
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    g = {}

    def step():
        try:
            d = next(g["g"])
        except StopIteration:
            return False
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            app.quit()
            return False
        GLib.timeout_add(int((d or 0.05) * 1000), step)
        return False
    app.connect("activate", lambda *_: GLib.idle_add(lambda: (g.__setitem__("g", scenario(app)), GLib.timeout_add(1500, step)) and False))
    app.run([])


if __name__ == "__main__":
    main()
