"""Click through Berth like a user — every page's actions and every database engine — against the demo engine,
checking what is actually on screen and taking a screenshot after each step.

    sh tools/demo/up.sh && python3 tools/demo/engines.py up vespa
    GDK_BACKEND=broadway BROADWAY_DISPLAY=:9 python3 tools/walkthrough.py OUTDIR [db:<container> …] [pages]

db:<container>  walk the database explorer on that container (open, row panel, edit + save + verify, add form,
                inline edit, multi-select, structure, query, export)
pages           every page's actions: row menus, dialogs, stacks check, backups, run/build/pull, volumes, sharing,
                send files, Ctrl+K
Exit code 1 if a check failed. Only the demo engine is used (settings are written to a private config dir)."""
import os
import sys
import tempfile
import time

OUT = sys.argv[1]
WANT = sys.argv[2:] or ["pages"]
os.makedirs(OUT, exist_ok=True)
CFG = tempfile.mkdtemp(prefix="berth-walk-")
os.makedirs(os.path.join(CFG, "berth"))
with open(os.path.join(CFG, "berth", "settings.json"), "w") as f:
    f.write('{"onboarded": true, "saver": false, "engine": "tcp://127.0.0.1:23750"}')
os.environ.update(XDG_CONFIG_HOME=CFG, BERTH_NO_WELCOME="1", BERTH_NO_UPDATE_CHECK="1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402
from berth import app as A  # noqa: E402

app = A.App()
results = []
n_shot = [0]


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  ({str(detail)[:200]})" if detail != "" else ""), flush=True)


def render(widget, path):
    from gi.repository import Gdk, Graphene, Gsk
    w, h = widget.get_width(), widget.get_height()
    snap = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(widget).snapshot(snap, w, h)
    node = snap.to_node()
    if node is None or not w:
        raise RuntimeError("nothing rendered")
    r = Gsk.CairoRenderer()
    r.realize_for_display(Gdk.Display.get_default())
    rect = Graphene.Rect()
    rect.init(0, 0, w, h)
    r.render_texture(node, rect).save_to_png(path)
    r.unrealize()


def shot(name, widget=None):
    n_shot[0] += 1
    path = os.path.join(OUT, f"{n_shot[0]:03d}-{name}.png")
    for _ in range(40):
        try:
            (app.win.screenshot(path) if widget is None else render(widget, path))
            return
        except RuntimeError:
            yield 0.1


def wait(cond, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        yield 0.3
    return False


def walk(w):
    c = w.get_first_child()
    while c is not None:
        yield c
        yield from walk(c)
        c = c.get_next_sibling()


def shown_values(panel):
    """[(field, text on screen)] from the row panel — what the user actually sees under each name."""
    out = []
    for blk in walk_children(panel.body):
        name = None
        text = None
        for w in walk(blk):
            if isinstance(w, Gtk.Label) and "db-field-name" in w.get_css_classes() and name is None:
                name = w.get_text()
            elif isinstance(w, Gtk.TextView) and text is None:
                b = w.get_buffer()
                text = b.get_text(b.get_start_iter(), b.get_end_iter(), False)
            elif isinstance(w, Gtk.Label) and "db-value-label" in w.get_css_classes() and text is None:
                text = w.get_text()
            elif isinstance(w, Gtk.Label) and w.get_text().startswith("Binary ·") and text is None:
                text = w.get_text()
        if name is not None:
            out.append((name, text))
    return out


def walk_children(box):
    c = box.get_first_child()
    while c is not None:
        yield c
        c = c.get_next_sibling()


def close_dialogs():
    w = app.win
    while (d := w.get_visible_dialog()) is not None:
        d.force_close()


# ── databases ──
def db_walk(cname):
    cname, _, want_node = cname.partition("@")          # db:container@table opens that table first
    w = app.win
    st = w.store
    c = next((c for c in st.containers if c["Names"][0] == "/" + cname), None)
    if not c:
        check(f"{cname}: container present", False)
        return
    dp = w.pages["databases"]
    w.go("databases")
    yield 1
    if want_node.startswith("/"):                         # db:container@/path/to/file.db → an SQLite file
        from berth import db as _db
        dp.open_backend(_db.SQLite(st.engine, c["Id"], want_node, f"{cname}:{want_node}"), want_node)
        want_node = ""
    else:
        dp.open(c["Id"])
    yield from wait(lambda: dp.nav.get_visible_page().get_tag() != "list", 30)
    ex = dp.nav.get_visible_page().get_child()
    ok = yield from wait(lambda: getattr(ex, "nodes", None) is not None and ex.data.result is not None
                         and ex.node is not None, 120)
    yield 1
    check(f"{cname}: explorer opens with the first table/collection", ok, getattr(ex.node, "label", None))
    yield from shot(f"{cname}-open")
    if want_node:
        n = next((n for top in ex.nodes for n in ([top] if top.leaf else ex.b.children(top)) if n.label == want_node),
                 None)
        if n is not None:
            ex._open(n)
            yield from wait(lambda: ex.node is n and ex.data.result is not None and
                            ex.data.stack.get_visible_child_name() != "loading", 30)
            yield 0.5
    r = ex.data.result
    if ex.b.write_capable and r is not None and getattr(r, "edit_why", None):
        # the first table can't be edited (a view, no key): walk the first one that can
        leaves = [n for top in ex.nodes for n in ([top] if top.leaf else ex.b.children(top))]
        for n in leaves[:12]:
            if n is ex.node:
                continue
            ex._open(n)
            yield from wait(lambda: ex.data.result is not None and ex.node is n and
                            ex.data.stack.get_visible_child_name() != "loading", 30)
            yield 0.5
            r = ex.data.result
            if r is not None and r.rows and getattr(r, "edit_why", "x") is None and len(r.rows) > 1:
                break
        check(f"{cname}: an editable table/key to walk", r is not None and getattr(r, "edit_why", "x") is None,
              getattr(ex.node, "label", None))
        yield from shot(f"{cname}-editable-table")
    if not (r and r.rows):
        check(f"{cname}: rows shown", False, r)
        dp.nav.pop_to_tag("list")
        return
    check(f"{cname}: rows shown", True, f"{len(r.rows)} rows, columns {r.columns[:6]}")
    # click a row → panel with every value visible
    ex.data.open(min(2, len(r.rows) - 1))
    ok = yield from wait(lambda: ex.data.panel.stack.get_visible_child_name() in ("fields", "error")
                         and (ex.data.panel.fields or ex.data.panel.stack.get_visible_child_name() == "error"), 40)
    yield 0.8
    panel = ex.data.panel
    err = panel.stack.get_visible_child_name() == "error"
    check(f"{cname}: clicking a row opens it", ok and not err, panel.err.get_description() if err else "")
    vals = shown_values(panel) if not err else []
    empty = [n for n, t in vals if t is None]
    check(f"{cname}: every field in the panel shows its value", vals and not empty,
          f"{len(vals)} fields; without a value: {empty[:6]}")
    yield from shot(f"{cname}-row")
    # allow changes → edit a text field → save → read back
    if getattr(ex, "write_sw", None) is not None:
        ex.write_sw.set_active(True)
        yield 0.8
        yield from shot(f"{cname}-row-editable")
        can = panel.editable()
        check(f"{cname}: the row is editable with Allow changes on", can or getattr(r, "edit_why", None),
              getattr(r, "edit_why", None) or "")
        if can:
            target = None
            for f, wd in zip(panel.fields, panel.widgets):
                if wd.get("view") is not None and not f.readonly and not f.pk and not f.json and \
                        isinstance(f.value, str) and f.value and f.name not in ("id", "_id", "documentid"):
                    target = (f, wd)
                    break
            if target is None:
                check(f"{cname}: a text field to edit", False, [f.name for f in panel.fields])
            else:
                f, wd = target
                orig = f.value
                wd["view"].get_buffer().set_text(orig + " (edited)")
                yield 0.3
                check(f"{cname}: editing enables Save", panel.save.get_sensitive())
                pos = panel.pos
                panel._save()
                ok = yield from wait(lambda: panel.fields and any(
                    x.name == f.name and isinstance(x.value, str) and x.value.endswith("(edited)")
                    for x in panel.fields), 40)
                yield 1
                check(f"{cname}: Save writes {f.name} and the panel shows the stored value", ok)
                yield from shot(f"{cname}-saved")
                # put it back
                again = next((x for x in zip(panel.fields, panel.widgets) if x[0].name == f.name), None)
                if again and again[1].get("view"):
                    again[1]["view"].get_buffer().set_text(orig)
                    panel._save()
                    yield from wait(lambda: any(x.name == f.name and x.value == orig for x in panel.fields), 40)
                del pos
        # add form
        if ex.add_btn.get_visible():
            ex.data.open_new()
            ok = yield from wait(lambda: panel.is_new and panel.stack.get_visible_child_name() == "fields", 30)
            yield 0.5
            check(f"{cname}: the add form opens", ok and bool(panel.fields), [x.name for x in panel.fields][:6])
            yield from shot(f"{cname}-add")
            panel.reset()
            yield 0.3
        # inline cell edit (SQL)
        if ex.ed.inline:
            col = next((i for i, n in enumerate(r.columns) if n not in ("id",) and isinstance(r.rows[0][i], str)), 1)
            ex.data.edit_cell(0, col, ex.data.view)
            yield 0.8
            pops = [x for x in walk(ex.data.view) if isinstance(x, Gtk.Popover) and x.get_visible()]
            check(f"{cname}: double-click edit opens on the cell", bool(pops))
            yield from shot(f"{cname}-inline")
            for p in pops:
                p.popdown()
            yield 0.3
        # multi-select
        ex.data.split.set_show_sidebar(False)
        ex.data.sel.select_item(0, True)
        ex.data.sel.select_item(1, False)
        yield 0.6
        check(f"{cname}: selecting two rows offers Copy and Delete", ex.bulk_copy.get_visible() and
              (ex.bulk_del.get_visible() or not ex.ed.can_delete))
        yield from shot(f"{cname}-multi")
        ex.data.sel.unselect_all()
        ex.write_sw.set_active(False)
        yield 0.3
    # structure + query
    ex.views.set_visible_child_name("structure")
    yield from wait(lambda: ex.struct.stack.get_visible_child_name() != "loading" and
                    ex.struct.msg.get_title() != "Nothing to show yet", 30)
    yield 1
    check(f"{cname}: structure tab", ex.struct.stack.get_visible_child_name() in ("grid", "text"),
          ex.struct.msg.get_title())
    yield from shot(f"{cname}-structure")
    ex.views.set_visible_child_name("query")
    ex._run()
    yield from wait(lambda: ex.out.stack.get_visible_child_name() != "loading" and
                    ex.out.msg.get_title() != "Nothing to show yet", 40)
    yield 1
    check(f"{cname}: the example query runs", ex.out.stack.get_visible_child_name() in ("grid", "text") or
          "Blocked" in ex.out.msg.get_title() or "read-only" in (ex.out.msg.get_description() or ""),
          ex.out.msg.get_title() + " " + (ex.out.msg.get_description() or "")[:120])
    yield from shot(f"{cname}-query")
    ex.views.set_visible_child_name("data")
    # whole-table export (the code behind “Export all rows…”)
    from berth import dbtools
    if ex.b.kind in ("postgres", "mysql", "mongo", "sqlite"):
        out = os.path.join(tempfile.mkdtemp(), "t.csv")
        try:
            n = dbtools.export_table(ex.b, ex.node, out, "csv")
            check(f"{cname}: export all rows", n == len(r.rows) or n > 0, n)
        except Exception as e:  # noqa: BLE001
            check(f"{cname}: export all rows", False, e)
    dp.nav.pop_to_tag("list")
    yield 1


# ── pages and their actions ──
def pages_walk():
    w = app.win
    st = w.store
    from berth import compose, pages, quick, edit, sharing_page, send_page
    from berth.backups_page import ScheduleDialog
    from berth.palette import Palette
    cp = w.pages["containers"]
    w.go("containers")
    yield 2
    site = next(c for c in st.containers if c["Names"][0] == "/blog-site-1")
    dbc = next(c for c in st.containers if c["Names"][0] == "/shop-db-1")
    for name, mk in (("rename", lambda: quick.rename_container(w, st, site["Id"])),
                     ("ports", lambda: quick.PortsDialog(w, st, site["Id"]).present(w)),
                     ("edit", lambda: edit.EditDialog(w, st, site["Id"]).present(w)),
                     ("address", lambda: sharing_page.ExposeDialog(w.pages["sharing"]).present(w)),
                     ("share", lambda: sharing_page.ShareDialog(w.pages["sharing"]).present(w))):
        mk()
        yield 1.5
        d = w.get_visible_dialog()
        check(f"container action “{name}” opens its dialog", d is not None)
        if d is not None:
            yield from shot(f"container-{name}", d)
        close_dialogs()
        yield 0.3
    cp.sel_btn.set_active(True)
    cp._pick(next(r for r in cp.groups.values() if r.key == "shop"), True)
    yield 0.8
    check("Containers: ticking a stack selects its containers", len(cp.picked) == 5, len(cp.picked))
    yield from shot("containers-select")
    cp.sel_btn.set_active(False)
    for tab in ("logs", "inspect", "mounts", "exec", "files", "stats", "database"):
        cp.open_detail(dbc["Id"], tab)
        yield 3
        yield from shot(f"container-tab-{tab}")
        cp.nav.pop_to_tag("list")
        yield 0.5
    # stacks
    w.go("stacks")
    yield 1.5
    sp = w.pages["stacks"]
    yield from shot("stacks")
    sp._check("shop")
    yield 6
    d = w.get_visible_dialog()
    check("Stacks: Check opens and finishes", d is not None)
    if d is not None:
        yield from shot("stacks-check", d)
    close_dialogs()
    co = compose.ComposeOpen(w, st)
    co.present(w)
    demo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "demo")
    co.file = os.path.join(demo, "blog.compose.yaml")
    co.file_row.set_subtitle(co.file)
    co.name.set_text("blog")
    co.go.set_sensitive(True)
    co._check()
    ok = yield from wait(lambda: co._check_rows or "good" in (co.checks.get_description() or ""), 60)
    yield 1
    check("Run a Compose file: the check runs and shows its results", ok, co.checks.get_description())
    yield from shot("compose-open-checked", co)
    close_dialogs()
    compose.ComposeEditor(w, st, "shop").present(w)
    yield 2
    d = w.get_visible_dialog()
    check("Stacks: edit the compose file", d is not None)
    if d is not None:
        yield from shot("stacks-edit", d)
    close_dialogs()
    # images
    w.go("images")
    yield 2
    img = next(im for im in st.images if "busybox:latest" in (im.get("RepoTags") or []))
    rd = pages.RunDialog(w, st, {"ref": "busybox:latest", "repo": "busybox", "tag": "latest", "id": img["Id"]})
    rd.present(w)
    yield 1
    for x in walk(rd):
        if isinstance(x, Adw.ExpanderRow):
            x.set_expanded(True)
    yield 1
    yield from shot("images-run-all-options", rd)
    close_dialogs()
    pd = pages.PullDialog(w, st)
    pd.present(w)
    yield 0.5
    yield from shot("images-pull", pd)
    pd.entry.set_text("redis")
    ok = yield from wait(lambda: pd.results_sw.get_visible(), 20)
    yield 1
    check("Pull: typing searches Docker Hub", ok)
    yield from shot("images-pull-search", pd)
    close_dialogs()
    pages.BuildDialog(w, st).present(w)
    yield 1
    yield from shot("images-build", w.get_visible_dialog())
    close_dialogs()
    # volumes: open one and a file in it
    w.go("volumes")
    yield 2
    vp = w.pages["volumes"]
    vrow = next((vp.model.get_item(i) for i in range(vp.model.get_n_items())
                 if vp.model.get_item(i).data.get("name") == "shop_pgdata"), None)
    if vrow:
        vp.on_activate(vrow)
        yield 8
        yield from shot("volume-files")
        vp.nav.pop_to_tag("list")
        yield 1
    # backups: schedule, run, list, restore dialog
    w.go("backups")
    yield 1
    bp = w.pages["backups"]
    folder = tempfile.mkdtemp(prefix="berth-walk-backups-")
    from berth import backups as bk
    s = bk.new_schedule("db", "shop-db-1", "shop", 24, 3, folder)
    sd = ScheduleDialog(bp, sched=s)
    sd.present(w)
    yield 1
    yield from shot("backups-schedule", sd)
    close_dialogs()
    bp.save_schedule(s)
    ok = yield from wait(lambda: any(f.endswith(".dump") for f in os.listdir(folder)), 60)
    yield 1.5
    check("Backups: saving a schedule makes the first backup", ok, os.listdir(folder))
    yield from shot("backups-page")
    files = [os.path.join(folder, f) for f in os.listdir(folder)]
    if files:
        bp.restore(files[0], s)
        yield 1.5
        d = w.get_visible_dialog()
        check("Backups: Restore asks where and explains", d is not None)
        if d is not None:
            yield from shot("backups-restore", d)
        close_dialogs()
    w.settings["backups"] = []
    bk.set_timer(False)
    # sharing + send
    w.go("sharing")
    yield 2
    yield from shot("sharing")
    w.go("send")
    yield 1
    sendp = w.pages["send"]
    tmpf = os.path.join(tempfile.mkdtemp(), "menu.pdf")
    with open(tmpf, "wb") as f:
        f.write(b"%PDF-1.4 demo")
    send_page.OptionsDialog(sendp, [tmpf]).present(w)
    yield 1
    yield from shot("send-options", w.get_visible_dialog())
    close_dialogs()
    # Ctrl+K
    pal = Palette(w)
    pal.present(w)
    yield 1
    pal.entry.set_text("shop")
    yield 1
    check("Ctrl+K: typing finds things", pal.list.get_row_at_index(0) is not None)
    yield from shot("ctrl-k", pal)
    close_dialogs()
    # every remaining page as the user lands on it
    for key in ("networks", "logs", "resources", "disk", "builds", "cache", "kubernetes", "settings"):
        w.go(key)
        yield 3 if key in ("logs", "resources") else 1.5
        yield from shot(f"page-{key}")


def scenario():
    w = app.win
    w.set_size_request(1650, 950)
    yield from wait(lambda: w.store.up and w.store.containers, 60)
    yield 2
    for want in WANT:
        if want.startswith("db:"):
            yield from db_walk(want[3:])
        elif want == "pages":
            yield from pages_walk()
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f" — failed: {bad}" if bad else ""), flush=True)
    app.exit_code = 1 if bad else 0
    app.quit()


g = {}


def step():
    try:
        d = next(g["g"])
    except StopIteration:
        return False
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("walkthrough ran without crashing", False, e)
        app.quit()
        return False
    GLib.timeout_add(int((d or 0.05) * 1000), step)
    return False


app.connect("activate", lambda *_: GLib.idle_add(lambda: (g.__setitem__("g", scenario()),
                                                         GLib.timeout_add(800, step)) and False))
app.run([])
sys.exit(getattr(app, "exit_code", 1))
