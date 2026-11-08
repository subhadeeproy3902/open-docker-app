"""Containers: the grouped list (compose project → containers) and the detail page."""
import os
import re
import threading
import time

import gi
from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import charts
from . import compose
from .cli import run_command_text
from .edit import EditDialog
from .engine import demux
from .files import FileBrowser
from .logs import LogsExplorer
from .util import (Row, bg, cname, confirm, connect_row, disconnect_rows, esc, factory, fmt_bytes, fmt_pct,
                   fmt_rate, icon_button, label, labels, open_uri, short_status, spawn, state_of,
                   terminal_argv, themed, thread, detail_tag, push_detail, toolbar_row,
                   run_in_terminal, PageHeader, flat_page, table_card)

try:
    gi.require_version("Vte", "3.91")
    from gi.repository import Vte
except (ValueError, ImportError):
    Vte = None

STANDALONE = "\x00standalone"


def project(c):
    return labels(c).get("com.docker.compose.project", "")


def service(c):
    return labels(c).get("com.docker.compose.service", "") or cname(c)


def ports(c):
    seen, out = set(), []
    for p in c.get("Ports") or []:
        pub = p.get("PublicPort")
        if pub and pub not in seen:
            seen.add(pub)
            out.append((pub, p.get("PrivatePort")))
    return sorted(out)


# ── actions shared by the list and the detail page ──
class Actions:
    def __init__(self, win, store):
        self.win, self.store = win, store

    def verb(self, cid, verb, row=None):
        c = self.store.by_id(cid)
        name = cname(c) if c else cid[:12]
        if row is not None:
            row.extra["busy"] = True
            row.emit("changed")

        def finish(_r=None):
            if row is not None:
                row.extra.pop("busy", None)
                row.emit("changed")
            self.store.refresh("containers")

        def fail(e):
            self.win.toast(f"{name}: {e}")
            finish()
        bg(self.store.engine.action, cid, verb, done=finish, error=fail)

    def delete(self, cid, after=None):
        c = self.store.by_id(cid)
        name = cname(c) if c else cid[:12]
        extra = " Docker Compose creates it again the next time the stack starts." if c and project(c) else ""

        def go():
            bg(self.store.engine.remove_container, cid,
               done=lambda _r: (self.win.toast(f"Deleted {name}"), self.store.refresh("containers"), after and after()),
               error=lambda e: self.win.toast(f"{name}: {e}"))
        confirm(self.win, f"Delete {esc(name)}?",
                f"The container is stopped and removed. Its volumes (databases, uploads) are <b>kept</b>.{extra}",
                "Delete", go)

    def project_verb(self, proj, verb):
        cs = [c for c in self.store.containers if project(c) == proj]
        if verb == "start":
            targets = [c for c in cs if c["State"] != "running"]
        elif verb in ("stop", "restart"):
            targets = [c for c in cs if c["State"] in ("running", "paused", "restarting")]
        else:
            targets = cs

        def run():
            errs = []
            for c in targets:
                try:
                    if verb == "remove":
                        self.store.engine.remove_container(c["Id"])
                    else:
                        self.store.engine.action(c["Id"], verb)
                except Exception as e:  # noqa: BLE001
                    errs.append(f"{cname(c)}: {e}")
            return errs

        def done(errs):
            self.store.refresh("containers")
            self.win.toast(errs[0] if errs else f"{proj}: done")

        def go():
            bg(run, done=done)
        if verb == "start":
            go()
        else:
            what = {"stop": ("Stop", "They are stopped, not deleted."),
                    "restart": ("Restart", "Anything mid-request (a live call, a test) is interrupted."),
                    "remove": ("Delete", "Containers are removed; volumes are kept. Compose recreates them on the next start.")}[verb]
            confirm(self.win, f"{what[0]} every container in {esc(proj)}?", what[1], f"{what[0]} all", go,
                    destructive=verb == "remove")

    def commit(self, cid):
        c = self.store.by_id(cid)
        base = (cname(c) if c else cid[:12]).lower()

        def go(ref):
            last = ref.rsplit("/", 1)[-1]
            repo, tag = ref.rsplit(":", 1) if ":" in last else (ref, "latest")
            self.win.toast("Saving the container as an image…")
            bg(self.store.engine.commit, cid, repo, tag, "made with Berth",
               done=lambda _r: (self.win.toast(f"Saved as image {repo}:{tag}"), self.store.refresh("images")),
               error=lambda e: self.win.toast(str(e)))
        from .pages import ask_text
        ask_text(self.win, "Save container as image", "Name for the new image (its files as they are now).",
                 f"{base}-snapshot:latest", "Save", go)

    def export_fs(self, cid):
        c = self.store.by_id(cid)
        from .pages import save_dialog

        def go(path):
            self.win.toast("Exporting the container's files…")
            bg(self.store.engine.export_container, cid, path,
               done=lambda n: self.win.toast(f"Exported {fmt_bytes(n)} to {path}"), error=lambda e: self.win.toast(str(e)))
        save_dialog(self.win, "Export container filesystem", f"{cname(c) if c else cid[:12]}.tar", go)

    def copy_run(self, cid):
        def done(d):
            self.win.get_clipboard().set(run_command_text(d))
            self.win.toast("docker run command copied")
        bg(self.store.engine.inspect, cid, done=done, error=lambda e: self.win.toast(str(e)))

    def exec_terminal(self, cid):
        if not run_in_terminal(["docker", "exec", "-it", cid, "sh", "-c", "command -v bash >/dev/null && exec bash || exec sh"]):
            self.win.toast("No terminal emulator found (set $TERMINAL)")

    def open_port(self, port):
        open_uri(f"http://localhost:{port}")


# ── list cells ──
class NameCell(Gtk.TreeExpander):
    def __init__(self):
        super().__init__(indent_for_icon=True)
        box = Gtk.Box(spacing=10, width_request=180)
        self.dot = Gtk.Box(valign=Gtk.Align.CENTER, css_classes=["dot"])
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.title = label(css=["heading-sm"])
        self.sub = label(css=["dim-label", "caption"])
        col.append(self.title)
        col.append(self.sub)
        box.append(self.dot)
        box.append(col)
        self.set_child(box)

    def update(self, row, page):
        if row.kind == "project":
            cs = row.data["containers"]
            run = sum(1 for c in cs if c["State"] == "running")
            bad = sum(1 for c in cs if state_of(c) == "unhealthy")
            self.dot.set_css_classes(["dot", "group", "unhealthy" if bad else "running" if run else "exited"])
            self.title.set_text(row.key if row.key != STANDALONE else "Standalone")
            self.sub.set_text(f"{run} of {len(cs)} running" + (f" · {bad} unhealthy" if bad else "") + " · Compose")
        else:
            c = row.data
            st = state_of(c)
            self.dot.set_css_classes(["dot", st])
            self.dot.set_tooltip_text(st)
            self.title.set_text(service(c) if row.extra.get("in_group") else cname(c))
            self.sub.set_text(cname(c) if row.extra.get("in_group") else c.get("Id", "")[:12])


class TextCell(Gtk.Label):
    def __init__(self, css=(), xalign=0.0):
        super().__init__(xalign=xalign, ellipsize=Pango.EllipsizeMode.END)
        for c in css:
            self.add_css_class(c)


class StatCell(Gtk.DrawingArea):
    """A number that changes every couple of seconds. Drawn, not a Gtk.Label, so updating it only
    repaints this cell instead of re-measuring the whole table. Its size comes from the current font,
    so it grows with Settings → Accessibility → Text size."""
    TEMPLATE = "000.0 MB"

    def __init__(self):
        super().__init__(valign=Gtk.Align.CENTER, halign=Gtk.Align.END)
        self.text = "—"
        self.set_draw_func(self._draw)

    def _layout(self, text):
        layout = self.create_pango_layout(text)
        fd = layout.get_context().get_font_description() or Pango.FontDescription()
        fd.set_family("monospace")
        layout.set_font_description(fd)
        return layout

    def do_measure(self, orientation, for_size):
        w, h = self._layout(self.TEMPLATE).get_pixel_size()
        n = w + 4 if orientation == Gtk.Orientation.HORIZONTAL else h + 4
        return n, n, -1, -1

    def set_text(self, t):
        if t != self.text:
            self.text = t
            self.queue_draw()

    def _draw(self, _a, cr, w, h):
        from gi.repository import PangoCairo
        layout = self._layout(self.text)
        tw, th = layout.get_pixel_size()
        c = self.get_color()
        cr.set_source_rgba(c.red, c.green, c.blue, c.alpha * (0.55 if self.text == "—" else 1))
        cr.move_to(w - tw, (h - th) / 2)
        PangoCairo.show_layout(cr, layout)


class PortsCell(Gtk.Box):
    def __init__(self, actions):
        super().__init__(spacing=4, valign=Gtk.Align.CENTER)
        self.actions = actions

    def update(self, row):
        while (ch := self.get_first_child()):
            self.remove(ch)
        if row.kind != "container":
            return
        ps = ports(row.data)
        for pub, priv in ps[:2]:
            b = Gtk.Button(label=f"{pub}:{priv}", tooltip_text=f"Open http://localhost:{pub}")
            b.add_css_class("port")
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, p=pub: self.actions.open_port(p))
            self.append(b)
        if len(ps) > 2:
            more = Gtk.Label(label=f"+{len(ps) - 2}", tooltip_text=", ".join(f"{a}:{b}" for a, b in ps[2:]))
            more.add_css_class("dim-label")
            self.append(more)


class ActionsCell(Gtk.Box):
    def __init__(self, page):
        super().__init__(spacing=2, halign=Gtk.Align.END)
        self.page = page
        self.main = icon_button("media-playback-start-symbolic", "Start", self._main)
        self.menu = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER, tooltip_text="More actions")
        self.menu.add_css_class("flat")
        self.trash = icon_button("user-trash-symbolic", "Delete", self._trash)
        for w in (self.main, self.menu, self.trash):
            self.append(w)
        self.row = None

    def update(self, row):
        self.row = row
        busy = row.extra.get("busy")
        for w in (self.main, self.menu, self.trash):
            w.set_sensitive(not busy)
        m = Gio.Menu()
        if row.kind == "project":
            cs = row.data["containers"]
            run = any(c["State"] == "running" for c in cs)
            self.main.set_icon_name("media-playback-stop-symbolic" if run else "media-playback-start-symbolic")
            self.main.set_tooltip_text("Stop all" if run else "Start all")
            self.main.set_visible(row.key != STANDALONE)
            self.trash.set_visible(row.key != STANDALONE)
            self.trash.set_tooltip_text("Delete all containers")
            if row.key != STANDALONE:
                sec = Gio.Menu()
                for lbl, verb in (("Start all", "start"), ("Restart all", "restart"), ("Stop all", "stop")):
                    sec.append(lbl, f"ctr.project::{verb}|{row.key}")
                m.append_section(None, sec)
                cmp = Gio.Menu()
                for lbl, verb in (("Compose up (apply compose file)", "cup"), ("Pull newer images and recreate", "cpull"),
                                  ("Edit compose file…", "cedit"), ("Compose down…", "cdown")):
                    cmp.append(lbl, f"ctr.project::{verb}|{row.key}")
                m.append_section("Compose", cmp)
            self.menu.set_visible(row.key != STANDALONE)
        else:
            c = row.data
            run = c["State"] in ("running", "restarting")
            self.main.set_visible(True)
            self.trash.set_visible(True)
            self.menu.set_visible(True)
            self.trash.set_tooltip_text("Delete")
            self.main.set_icon_name("media-playback-stop-symbolic" if run or c["State"] == "paused" else "media-playback-start-symbolic")
            self.main.set_tooltip_text("Stop" if run or c["State"] == "paused" else "Start")
            cid = c["Id"]
            m.append("View details", f"ctr.open::{cid}")
            m.append("Edit…", f"ctr.edit::{cid}")
            if c["State"] == "running":
                m.append("Restart", f"ctr.verb::restart|{cid}")
                m.append("Pause", f"ctr.verb::pause|{cid}")
                m.append("Open in terminal", f"ctr.exec::{cid}")
            elif c["State"] == "paused":
                m.append("Resume", f"ctr.verb::unpause|{cid}")
            for pub, _ in ports(c)[:4]:
                m.append(f"Open localhost:{pub}", f"ctr.port::{pub}")
            extra = Gio.Menu()
            extra.append("Copy docker run command", f"ctr.runcmd::{cid}")
            extra.append("Save as image…", f"ctr.commit::{cid}")
            extra.append("Export filesystem…", f"ctr.export::{cid}")
            extra.append("Copy ID", f"ctr.copy::{cid}")
            m.append_section(None, extra)
        self.menu.set_menu_model(m)

    def _main(self):
        r = self.row
        if not r:
            return
        a = self.page.actions
        if r.kind == "project":
            run = any(c["State"] == "running" for c in r.data["containers"])
            a.project_verb(r.key, "stop" if run else "start")
        else:
            st = r.data["State"]
            a.verb(r.key, "stop" if st in ("running", "restarting", "paused") else "start", r)

    def _trash(self):
        r = self.row
        if not r:
            return
        if r.kind == "project":
            self.page.actions.project_verb(r.key, "remove")
        else:
            self.page.actions.delete(r.key)


class ContainersPage(Gtk.Box):
    title = "Containers"
    icon = "view-grid-symbolic"
    wants_stats = True

    def __init__(self, win, store, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store, self.settings = win, store, settings
        self.actions = Actions(win, store)
        self.groups, self.items, self._sig = {}, {}, None
        self.expanded = dict(settings["expanded"] or {})
        self.query, self.status_filter = "", "all"

        self.nav = Adw.NavigationView()
        self.append(self.nav)
        self.nav.set_vexpand(True)

        # header
        tv = Adw.ToolbarView()
        self.wtitle = PageHeader("Containers")
        self.search = Gtk.SearchEntry(placeholder_text="Search containers", width_chars=28)
        self.search.connect("search-changed", self._on_search)
        run = Adw.ToggleGroup(tooltip_text="Show containers by state")
        self.filter_toggles = {}
        for key, lbl in (("all", "All"), ("running", "Running"), ("unhealthy", "Unhealthy"), ("stopped", "Stopped")):
            t = Adw.Toggle(name=key, label=lbl)
            self.filter_toggles[key] = t
            run.add(t)
        run.set_active_name("all")
        self.run_filter = run
        run.connect("notify::active-name", lambda g, *_: self._on_filter(g.get_active_name()))
        exp = icon_button("view-list-symbolic", "Expand / collapse all stacks", self._toggle_all, css=("flat",))
        cmp = Gtk.Button(label="Run Compose file…", tooltip_text="Start a stack from a compose.yaml")
        cmp.add_css_class("suggested-action")
        cmp.connect("clicked", lambda *_: compose.ComposeOpen(self.win, self.store).present(self.win))
        self.wtitle.add(cmp)
        flat_page(tv, self.wtitle, toolbar_row(self.search, run, Gtk.Box(hexpand=True), exp))

        # list
        self.root = Gio.ListStore(item_type=Row)
        self.tree = Gtk.TreeListModel.new(self.root, False, False, lambda it: it.children if it.kind == "project" else None)
        sel = Gtk.NoSelection(model=self.tree)
        self.view = Gtk.ColumnView(model=sel, show_row_separators=True, single_click_activate=True)
        self.view.add_css_class("data-table")
        self.view.add_css_class("berth-table")
        self.view.connect("activate", self._on_activate)
        self._columns()
        sw = table_card(self.view)
        self.stack = Gtk.Stack()
        self.stack.add_named(sw, "list")
        self.empty = Adw.StatusPage(icon_name="view-grid-symbolic")
        acts = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self.empty_run = Gtk.Button(label="Run a sample container")
        self.empty_run.add_css_class("pill")
        self.empty_run.add_css_class("suggested-action")
        self.empty_run.connect("clicked", lambda *_: self._run_sample())
        self.empty_pull = Gtk.Button(label="Pull an image")
        self.empty_pull.add_css_class("pill")
        self.empty_pull.connect("clicked", lambda *_: self._pull_image())
        acts.append(self.empty_run)
        acts.append(self.empty_pull)
        self.empty.set_child(acts)
        self.stack.add_named(self.empty, "empty")
        tv.set_content(self.stack)
        self.nav.add(Adw.NavigationPage(title="Containers", child=tv, tag="list"))

        # menu actions (target "verb|id" strings)
        ag = Gio.SimpleActionGroup()
        for name, fn in (("verb", self._a_verb), ("project", self._a_project), ("open", self._a_open),
                         ("exec", lambda v: self.actions.exec_terminal(v)), ("port", lambda v: self.actions.open_port(v)),
                         ("copy", self._a_copy),
                         ("edit", lambda v: EditDialog(self.win, self.store, v).present(self.win)),
                         ("runcmd", lambda v: self.actions.copy_run(v)), ("commit", lambda v: self.actions.commit(v)),
                         ("export", lambda v: self.actions.export_fs(v))):
            a = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            a.connect("activate", lambda _a, p, f=fn: f(p.get_string()))
            ag.add_action(a)
        self.insert_action_group("ctr", ag)

        store.connect("containers", lambda *_: self._sync())
        store.connect("stats", lambda *_: self._stats())
        self._sync()

    # ── columns ──
    def _columns(self):
        def col(title, setup, bind, expand=False, width=-1):
            def b(w, tree_row, _li):
                disconnect_rows(w)
                row = tree_row.get_item()
                bind(w, row, tree_row)
                connect_row(w, row, "changed", lambda r, w=w, tr=tree_row: bind(w, r, tr))
            c = Gtk.ColumnViewColumn(title=title, factory=factory(setup, b, lambda w, *_: disconnect_rows(w)),
                                     expand=expand, resizable=True)
            if width > 0:
                c.set_fixed_width(width)
            self.view.append_column(c)
            return c

        def name_bind(w, row, tr):
            w.set_list_row(tr)
            w.update(row, self)
        col("Name", NameCell, name_bind, expand=True)
        self.col_image = col("Image", lambda: TextCell(["monospace", "dim-label"]),
            lambda w, r, _t: (w.set_text(r.data.get("Image", "") if r.kind == "container" else ""),
                              w.set_tooltip_text(r.data.get("Image") if r.kind == "container" else None)), width=160)
        col("Status", lambda: TextCell(["dim-label"]),
            lambda w, r, _t: w.set_text(short_status(r.data.get("Status", "")) if r.kind == "container" else ""), width=132)
        self.col_ports = col("Ports", lambda: PortsCell(self.actions), lambda w, r, _t: w.update(r), width=118)

        def stat_col(title, idx):
            def setup():
                return StatCell()

            def bind(w, r, _t):
                self._stat_text(w, r, idx)
            def b(w, tree_row, _li):
                disconnect_rows(w)
                row = tree_row.get_item()
                bind(w, row, tree_row)
                connect_row(w, row, "stats", lambda r, w=w: self._stat_text(w, r, idx))
                connect_row(w, row, "changed", lambda r, w=w: self._stat_text(w, r, idx))
            c = Gtk.ColumnViewColumn(title=title, factory=factory(setup, b, lambda w, *_: disconnect_rows(w)), resizable=True)
            # no fixed width: the column follows the cells, which follow the font size
            self.view.append_column(c)
        stat_col("CPU", 1)
        stat_col("Memory", 2)
        col("", lambda: ActionsCell(self), lambda w, r, _t: w.update(r), width=124)

    def _stat_text(self, w, row, idx):
        s = self.store.sampler
        if row.kind == "container":
            p = s.latest(row.key)
            v = p[idx] if p else None
        else:
            vals = [s.latest(c["Id"]) for c in row.data["containers"]]
            vals = [p[idx] for p in vals if p]
            v = sum(vals) if vals else None
            if v is not None and idx == 1:
                pass
        w.set_text("—" if v is None else fmt_pct(v) if idx == 1 else fmt_bytes(v))

    # ── model sync (in place; no flicker, keeps expansion) ──
    def _match(self, c):
        f = self.status_filter
        if f != "all":
            st = state_of(c)
            if (f == "running" and c["State"] != "running") or (f == "unhealthy" and st != "unhealthy") or \
               (f == "stopped" and c["State"] == "running"):
                return False
        q = self.query
        return not q or any(q in (x or "").lower() for x in (cname(c), c.get("Image"), project(c), service(c)))

    def _sync(self):
        cs = [c for c in self.store.containers if self._match(c)]
        grouped = {}
        top = []
        for c in sorted(cs, key=lambda c: service(c).lower()):
            p = project(c)
            (grouped.setdefault(p, []) if p else top).append(c)
        order = sorted(grouped.items(), key=lambda kv: (-sum(c["State"] == "running" for c in kv[1]), kv[0].lower()))
        entries = [("project", p, lst) for p, lst in order] + [("container", c["Id"], c) for c in
                                                               sorted(top, key=lambda c: cname(c).lower())]
        sig = tuple((k, key, tuple(c["Id"] for c in v) if k == "project" else None) for k, key, v in entries)
        running = sum(1 for c in self.store.containers if c["State"] == "running")
        allc = self.store.containers
        unhealthy = sum(1 for c in allc if state_of(c) == "unhealthy")
        stopped = sum(1 for c in allc if c["State"] != "running")
        self.wtitle.set_subtitle(f"{running} running" + (f" · {unhealthy} unhealthy" if unhealthy else "")
                                 + f" · {len(allc)} total")
        for key, n in (("all", len(allc)), ("running", running), ("unhealthy", unhealthy), ("stopped", stopped)):
            self.filter_toggles[key].set_label(f"{key.capitalize()} {n}" if key != "all" else f"All {n}")
        self.stack.set_visible_child_name("list" if entries else "empty")
        if not entries:
            filtered = bool(self.store.containers)
            self.empty.set_icon_name("edit-find-symbolic" if filtered else "view-grid-symbolic")
            self.empty.set_title("Nothing matches" if filtered else "No containers yet")
            self.empty.set_description("Clear the search or pick another filter above." if filtered else
                                       "A container is a running copy of an image. Start with a small sample web "
                                       "page, pull an image to run, or open a Compose file.")
            self.empty_run.set_visible(not filtered)
            self.empty_pull.set_visible(not filtered)

        def row_for_container(c, in_group):
            r = self.items.get(c["Id"])
            if r is None:
                r = self.items[c["Id"]] = Row("container", c["Id"])
            r.data = c
            r.extra["in_group"] = in_group
            return r

        if sig == self._sig:                       # same shape: just refresh data
            for k, key, v in entries:
                if k == "project":
                    g = self.groups[key]
                    g.data = {"containers": v}
                    g.emit("changed")
                    for c in v:
                        row_for_container(c, True).emit("changed")
                else:
                    row_for_container(v, False).emit("changed")
            return

        self._remember_expansion()
        new_root = []
        live = set()
        for k, key, v in entries:
            if k == "project":
                g = self.groups.get(key)
                if g is None:
                    g = self.groups[key] = Row("project", key)
                    g.children = Gio.ListStore(item_type=Row)
                g.data = {"containers": v}
                kids = [row_for_container(c, True) for c in v]
                g.children.splice(0, g.children.get_n_items(), kids)
                new_root.append(g)
                live.update(c["Id"] for c in v)
            else:
                new_root.append(row_for_container(v, False))
                live.add(key)
        for cid in set(self.items) - live:
            del self.items[cid]
        self.root.splice(0, self.root.get_n_items(), new_root)
        self._sig = sig
        for g in new_root:
            if g.kind == "project":
                g.emit("changed")
        self._restore_expansion()

    def _remember_expansion(self):
        for i in range(self.tree.get_n_items()):
            tr = self.tree.get_row(i)
            it = tr.get_item()
            if it.kind == "project":
                self.expanded[it.key] = tr.get_expanded()

    def _restore_expansion(self):
        i = 0
        while i < self.tree.get_n_items():
            tr = self.tree.get_row(i)
            it = tr.get_item()
            if it.kind == "project":
                default = any(c["State"] == "running" for c in it.data["containers"])
                if any(state_of(c) == "unhealthy" for c in it.data["containers"]):
                    self.expanded[it.key] = True          # never hide a problem inside a collapsed stack
                want = self.expanded.get(it.key, default) or bool(self.query)
                if tr.get_expanded() != want:
                    tr.set_expanded(want)
            i += 1

    def save_state(self):
        self._remember_expansion()
        self.settings["expanded"] = self.expanded

    def _toggle_all(self):
        self._remember_expansion()
        target = not any(self.expanded.values())
        for k in self.groups:
            self.expanded[k] = target
        self._restore_expansion()

    def _stats(self):
        if not self.get_mapped():
            return
        for r in list(self.items.values()) + list(self.groups.values()):
            r.emit("stats")

    # ── handlers ──
    def _on_search(self, e):
        self.query = e.get_text().strip().lower()
        self._sync()

    def _on_filter(self, name):
        self.status_filter = name or "all"
        self._sync()

    def _run_sample(self):
        from .onboarding import SAMPLE_NAME, SAMPLE_PORT, run_sample
        self.empty_run.set_sensitive(False)
        self.win.toast("Downloading and starting the sample…")

        def done(cid):
            self.empty_run.set_sensitive(True)
            self.store.refresh("containers")
            t = Adw.Toast(title=f"{SAMPLE_NAME} is running on localhost:{SAMPLE_PORT}", button_label="Open", timeout=8)
            t.connect("button-clicked", lambda *_: __import__("webbrowser").open(f"http://localhost:{SAMPLE_PORT}"))
            self.win.toasts.add_toast(t)

        def fail(e):
            self.empty_run.set_sensitive(True)
            self.win.toast(f"Couldn't run the sample: {e}")
        run_sample(self.store, lambda t: False, done, fail)

    def _pull_image(self):
        from .pages import PullDialog
        self.win.go("images")
        PullDialog(self.win, self.store).present(self.win)

    def show_unhealthy(self):
        """Jump straight to the unhealthy containers (used by the status bar)."""
        self.run_filter.set_active_name("unhealthy")

    def _on_activate(self, _view, pos):
        tr = self.tree.get_row(pos)
        it = tr.get_item() if tr else None
        if not it:
            return
        if it.kind == "project":
            tr.set_expanded(not tr.get_expanded())
        else:
            self.open_detail(it.key)

    def open_detail(self, cid, tab=None):
        page = ContainerDetail(self, cid)
        push_detail(self.nav, page)
        if tab:
            page.tabs.set_visible_child_name(tab)

    def _a_verb(self, v):
        verb, cid = v.split("|", 1)
        self.actions.verb(cid, verb, self.items.get(cid))

    def _a_project(self, v):
        verb, proj = v.split("|", 1)
        if verb == "cup":
            compose.up(self.win, self.store, proj)
        elif verb == "cpull":
            compose.pull_up(self.win, self.store, proj)
        elif verb == "cedit":
            compose.ComposeEditor(self.win, self.store, proj).present(self.win)
        elif verb == "cdown":
            compose.down(self.win, self.store, proj)
        else:
            self.actions.project_verb(proj, verb)

    def _a_open(self, cid):
        self.open_detail(cid)

    def _a_copy(self, cid):
        self.get_clipboard().set(cid)
        self.win.toast("Container ID copied")

    def focus_search(self):
        self.search.grab_focus()


# ── detail page ──
class ContainerDetail(Adw.NavigationPage):
    def __init__(self, page, cid):
        c = page.store.by_id(cid) or {"Id": cid, "Names": [cid[:12]], "State": "unknown"}
        super().__init__(title=cname(c), tag=detail_tag("detail"))
        self.page, self.store, self.cid, self.actions = page, page.store, cid, page.actions
        self.built = set()
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.tabs = Adw.ViewStack()
        self.wtitle = Adw.WindowTitle(title=cname(c))
        hb.set_title_widget(self.wtitle)
        self.btn_main = icon_button("media-playback-stop-symbolic", "Stop", self._main)
        self.btn_restart = icon_button("view-refresh-symbolic", "Restart", lambda: self.actions.verb(cid, "restart"))
        self.btn_pause = icon_button("media-playback-pause-symbolic", "Pause", self._pause)
        self.btn_port = icon_button("web-browser-symbolic", "Open in browser", self._port)
        self.btn_delete = icon_button("user-trash-symbolic", "Delete", lambda: self.actions.delete(cid, after=self._gone))
        self.btn_edit = Gtk.Button(label="Edit", valign=Gtk.Align.CENTER,
                                   tooltip_text="Change name, environment, ports, volumes, limits…")
        self.btn_edit.connect("clicked", lambda *_: EditDialog(page.win, self.store, self.cid).present(page.win))
        more = Gio.Menu()
        more.append("Copy docker run command", "det.runcmd")
        more.append("Save as image…", "det.commit")
        more.append("Export filesystem…", "det.export")
        self.btn_more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=more, valign=Gtk.Align.CENTER,
                                       tooltip_text="More")
        self.btn_more.add_css_class("flat")
        dag = Gio.SimpleActionGroup()
        for n, fn in (("runcmd", self.actions.copy_run), ("commit", self.actions.commit), ("export", self.actions.export_fs)):
            a = Gio.SimpleAction.new(n, None)
            a.connect("activate", lambda *_a, f=fn: f(self.cid))
            dag.add_action(a)
        self.insert_action_group("det", dag)
        for b in (self.btn_more, self.btn_delete, self.btn_port, self.btn_pause, self.btn_restart, self.btn_main, self.btn_edit):
            hb.pack_end(b)
        tv.set_top_bar_style(Adw.ToolbarStyle.FLAT)
        tv.add_top_bar(hb)
        self.sub = label(css=["dim-label", "caption"], hexpand=True)
        tabbar = Gtk.Box(spacing=12, css_classes=["explorer-bar"])
        tabbar.append(Adw.InlineViewSwitcher(stack=self.tabs, display_mode=Adw.InlineViewSwitcherDisplayMode.BOTH))
        tabbar.append(self.sub)
        self.sub.set_xalign(1.0)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(tabbar)
        content.append(self.tabs)
        self.tabs.set_vexpand(True)
        tv.set_content(content)
        self.set_child(tv)

        self.logs = LogsExplorer(page.win, self.store, lambda: [(self.cid, cname(self.c or c), False)])
        self.tabs.add_titled_with_icon(self.logs, "logs", "Logs", "text-x-generic-symbolic")
        for name, title, icon in (("inspect", "Inspect", "dialog-information-symbolic"),
                                  ("mounts", "Mounts", "folder-symbolic"),
                                  ("exec", "Exec", "utilities-terminal-symbolic"),
                                  ("files", "Files", "folder-open-symbolic"),
                                  ("stats", "Stats", "utilities-system-monitor-symbolic")):
            if icon == "utilities-system-monitor-symbolic":
                icon = themed(icon, "power-profile-performance-symbolic")
            self.tabs.add_titled_with_icon(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), name, title, icon)
        self.tabs.connect("notify::visible-child-name", lambda *_: self._tab())
        self.connect("shown", lambda *_: self._tab())
        self._h1 = self.store.connect("containers", lambda *_: self._refresh())
        self._h2 = self.store.connect("stats", lambda *_: self._stats())
        self.connect("destroy", lambda *_: self._disconnect())
        self._refresh()

    def _disconnect(self):
        for h in (self._h1, self._h2):
            try:
                self.store.disconnect(h)
            except TypeError:
                pass

    def _gone(self):
        self.page.nav.pop_to_tag("list")

    @property
    def c(self):
        return self.store.by_id(self.cid)

    def _refresh(self):
        c = self.c
        if not c:
            return
        st = c["State"]
        self.set_title(cname(c))
        self.wtitle.set_title(cname(c))
        self.sub.set_text(f"{c.get('Image', '')} · {short_status(c.get('Status', ''))}"
                          + (f" · {project(c)}" if project(c) else ""))
        stopped = st not in ("running", "paused", "restarting")
        self.btn_main.set_icon_name("media-playback-start-symbolic" if stopped else "media-playback-stop-symbolic")
        self.btn_main.set_tooltip_text("Start" if stopped else "Stop")
        self.btn_restart.set_sensitive(st == "running")
        self.btn_pause.set_sensitive(st in ("running", "paused"))
        self.btn_pause.set_icon_name("media-playback-start-symbolic" if st == "paused" else "media-playback-pause-symbolic")
        self.btn_pause.set_tooltip_text("Resume" if st == "paused" else "Pause")
        self.btn_port.set_visible(bool(ports(c)))

    def _main(self):
        c = self.c
        if c:
            self.actions.verb(self.cid, "start" if c["State"] not in ("running", "paused", "restarting") else "stop")

    def _pause(self):
        c = self.c
        if c:
            self.actions.verb(self.cid, "unpause" if c["State"] == "paused" else "pause")

    def _port(self):
        c = self.c
        p = ports(c) if c else []
        if p:
            self.actions.open_port(p[0][0])

    # ── tabs (built the first time they are shown) ──
    def _tab(self):
        name = self.tabs.get_visible_child_name()
        if name not in self.built and name != "logs":
            self.built.add(name)
            builder = {"inspect": self._build_inspect, "mounts": self._build_mounts, "exec": self._build_exec,
                       "files": self._build_files, "stats": self._build_stats}[name]
            child = self.tabs.get_child_by_name(name)
            new = builder()
            new.set_vexpand(True)
            new.set_hexpand(True)
            child.append(new)
        self.page.win.update_saver()

    def stats_visible(self):
        return self.get_mapped() and self.tabs.get_visible_child_name() == "stats"

    # inspect
    def _build_inspect(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.insp = Adw.PreferencesPage()
        box.append(self.insp)
        self.insp.set_vexpand(True)
        self.reveal = False
        self._load_inspect()
        return box

    def _load_inspect(self):
        bg(self.store.engine.inspect, self.cid, done=self._fill_inspect,
           error=lambda e: self.page.win.toast(str(e)))

    def _fill_inspect(self, d):
        for g in list(getattr(self, "_insp_groups", [])):
            self.insp.remove(g)
        self._insp_groups = []
        cfg, hc, st = d.get("Config") or {}, d.get("HostConfig") or {}, d.get("State") or {}

        def group(title, rows, suffix=None):
            g = Adw.PreferencesGroup(title=title)
            if suffix:
                g.set_header_suffix(suffix)
            for t, v in rows:
                r = Adw.ActionRow(title=t, subtitle=esc(v) if v not in (None, "") else "—", subtitle_selectable=True)
                r.add_css_class("property")
                g.add(r)
            self.insp.add(g)
            self._insp_groups.append(g)
            return g
        started = st.get("StartedAt", "")
        group("Overview", [
            ("State", f"{st.get('Status')}" + (f" · health {st['Health']['Status']}" if st.get("Health") else "")
             + (f" · exit {st.get('ExitCode')}" if st.get("ExitCode") else "") + (" · out of memory" if st.get("OOMKilled") else "")),
            ("Started", started[:19].replace("T", " ") if started and not started.startswith("0001") else ""),
            ("Image", cfg.get("Image")), ("Command", " ".join((cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or []))),
            ("Restart policy", (hc.get("RestartPolicy") or {}).get("Name") or "no"),
            ("Restarts", str(d.get("RestartCount", 0))),
            ("Memory limit", fmt_bytes(hc["Memory"]) if hc.get("Memory") else "none"),
            ("Compose folder", (cfg.get("Labels") or {}).get("com.docker.compose.project.working_dir")),
            ("ID", d.get("Id")),
        ])
        prt = [(k, ", ".join(f"{b.get('HostIp') or '0.0.0.0'}:{b.get('HostPort')}" for b in (v or [])) or "not published")
               for k, v in ((d.get("NetworkSettings") or {}).get("Ports") or {}).items()]
        if prt:
            group("Ports", prt)
        nets = [(n, f"{v.get('IPAddress') or ''}  {', '.join(v.get('Aliases') or [])}".strip())
                for n, v in ((d.get("NetworkSettings") or {}).get("Networks") or {}).items()]
        if nets:
            group("Networks", nets)
        sw = Gtk.Switch(active=self.reveal, valign=Gtk.Align.CENTER, tooltip_text="Show values")
        sw.connect("notify::active", lambda s, *_: (setattr(self, "reveal", s.get_active()), self._fill_inspect(d)))
        env = []
        for e in sorted(cfg.get("Env") or []):
            k, _, v = e.partition("=")
            env.append((k, v if self.reveal else "••••••"))
        group("Environment", env, suffix=sw)
        lab = sorted((cfg.get("Labels") or {}).items())
        if lab:
            group("Labels", lab)
        raw = Adw.PreferencesGroup(title="Raw JSON")
        tvw = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.CHAR,
                           top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        tvw.add_css_class("card")
        import json
        tvw.get_buffer().set_text(json.dumps(d, indent=2))
        exp = Gtk.Expander(label="Show the full inspect output", child=tvw)
        raw.add(exp)
        self.insp.add(raw)
        self._insp_groups.append(raw)

    # mounts
    def _build_mounts(self):
        pp = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Mounts", description="Folders and volumes this container reads and writes.")
        pp.add(g)

        def fill(d):
            ms = d.get("Mounts") or []
            if not ms:
                g.add(Adw.ActionRow(title="No mounts"))
            for m in ms:
                src = m.get("Name") or m.get("Source")
                r = Adw.ActionRow(title=esc(m.get("Destination")), subtitle=esc(f"{m.get('Type')} · {src}" + ("" if m.get("RW") else " · read-only")),
                                  subtitle_selectable=True)
                if m.get("Type") == "bind" and m.get("Source"):
                    b = icon_button("folder-open-symbolic", "Open folder", lambda s=m["Source"]: open_uri("file://" + s))
                    r.add_suffix(b)
                g.add(r)
        bg(self.store.engine.inspect, self.cid, done=fill)
        return pp

    # exec
    def _build_exec(self):
        c = self.c or {}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        if c.get("State") != "running":
            box.append(Adw.StatusPage(icon_name="utilities-terminal-symbolic", title="Container is not running",
                                      description="Start it to open a shell.", vexpand=True))
            return box
        if Vte is not None:
            term = Vte.Terminal(vexpand=True, hexpand=True, margin_start=10, margin_end=4, margin_top=6)
            term.set_scrollback_lines(5000)
            term.set_cursor_blink_mode(Vte.CursorBlinkMode.OFF)
            term.set_clear_background(False)            # use the window background → follows light/dark
            term.add_css_class("exec-term")

            def colours(*_):
                fg = term.get_color()
                term.set_color_foreground(fg)
                term.set_color_cursor(fg)
            colours()
            sm = Adw.StyleManager.get_default()
            hid = sm.connect("notify::dark", lambda *_: GLib.idle_add(lambda: colours() or False))
            term.connect("destroy", lambda *_: sm.disconnect(hid))

            ended = Adw.Banner(title="Shell session ended", button_label="Reconnect", revealed=False)

            def spawn_shell(*_):
                ended.set_revealed(False)
                term.reset(True, True)
                term.spawn_async(Vte.PtyFlags.DEFAULT, None,
                                 ["docker", "exec", "-it", self.cid, "sh", "-c",
                                  "command -v bash >/dev/null && exec bash || exec sh"],
                                 None, GLib.SpawnFlags.SEARCH_PATH, None, None, -1, None, None, None)
                term.grab_focus()
            ended.connect("button-clicked", spawn_shell)
            term.connect("child-exited", lambda *_: ended.set_revealed(True))
            spawn_shell()
            if os.environ.get("BERTH_EXEC_INPUT"):      # dev/test only: type a command into the shell
                GLib.timeout_add(1500, lambda: term.feed_child(os.environ["BERTH_EXEC_INPUT"].encode() + b"\n") or False)
            box.append(ended)
            box.append(term)
            return box
        # no VTE: a simple command runner + "open in terminal"
        bar = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=10, margin_bottom=8)
        entry = Gtk.Entry(placeholder_text="Command to run, e.g.  ls -la /app", hexpand=True)
        entry.add_css_class("monospace")
        run = Gtk.Button(label="Run")
        run.add_css_class("suggested-action")
        termb = Gtk.Button(label="Open in terminal")
        termb.connect("clicked", lambda *_: self.actions.exec_terminal(self.cid))
        for w in (entry, run, termb):
            bar.append(w)
        out = Gtk.TextView(editable=False, monospace=True, left_margin=12, right_margin=12, top_margin=6)
        out.add_css_class("logview")
        buf = out.get_buffer()
        buf.set_text("Runs one command inside the container (no interactive programs).\n"
                     "For a full interactive shell here, install the vte4 package.\n\n")

        def go(*_):
            cmd = entry.get_text().strip()
            if not cmd:
                return
            run.set_sensitive(False)
            buf.insert(buf.get_end_iter(), f"$ {cmd}\n")

            def done(r):
                code, so, se = r
                buf.insert(buf.get_end_iter(), so + se + (f"[exit {code}]\n\n" if code else "\n"))
                run.set_sensitive(True)
                adj = out.get_parent().get_vadjustment() if out.get_parent() else None
                if adj:
                    GLib.idle_add(lambda: adj.set_value(adj.get_upper()) or False)

            def fail(e):
                buf.insert(buf.get_end_iter(), f"error: {e}\n\n")
                run.set_sensitive(True)
            bg(self.store.engine.exec_run, self.cid, ["sh", "-c", cmd], done=done, error=fail)
        entry.connect("activate", go)
        run.connect("clicked", go)
        box.append(bar)
        box.append(Gtk.ScrolledWindow(child=out, vexpand=True))
        return box

    # files
    def _build_files(self):
        c = self.c or {}
        if c.get("State") != "running":
            return Adw.StatusPage(icon_name="folder-open-symbolic", title="Container is not running",
                                  description="Files can be browsed and edited while it runs.")
        return FileBrowser(self.page.win, self.store, self.cid, "/",
                           note="Edits apply inside the running container. Files outside volumes are lost when it is recreated.")

    # stats
    def _build_stats(self):
        pal = charts.palette()
        grid = Gtk.Grid(column_spacing=12, row_spacing=12, margin_start=14, margin_end=14, margin_top=12, margin_bottom=14,
                        column_homogeneous=True)
        self.ch_cpu = charts.LineChart(fmt_pct, 160, fill=True)
        self.ch_mem = charts.LineChart(fmt_bytes, 160, binary=True, fill=True)
        self.ch_net = charts.LineChart(fmt_rate, 140, binary=True)
        self.ch_io = charts.LineChart(fmt_rate, 140, binary=True)
        self.tiles = {}
        for i, (title, ch, key, extra) in enumerate((("CPU · % of one thread", self.ch_cpu, "cpu", None),
                                                    ("Memory", self.ch_mem, "mem", None),
                                                    ("Network", self.ch_net, "net", [("in", pal[0]), ("out", pal[1])]),
                                                    ("Disk", self.ch_io, "io", [("read", pal[2]), ("write", pal[4])]))):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card", "chart-card"])
            head = Gtk.Box(spacing=8)
            head.append(label(title, css=["heading"]))
            val = label("", css=["numeric", "dim-label"], xalign=1.0, hexpand=True)
            head.append(val)
            self.tiles[key] = val
            card.append(head)
            card.append(ch)
            if extra:
                card.append(charts.legend(ch, extra))
            grid.attach(card, i % 2, i // 2, 1, 1)
        sw = Gtk.ScrolledWindow(child=grid, vexpand=True)
        GLib.idle_add(lambda: self._stats(force=True) or False)
        return sw

    def _stats(self, force=False):
        if not hasattr(self, "ch_cpu") or not (force or self.stats_visible()):
            return
        pal = charts.palette()
        pts = self.store.sampler.points(self.cid)
        col = lambda i: [(p[0], p[i]) for p in pts]
        self.ch_cpu.set_data([("CPU", pal[0], col(1))])
        self.ch_mem.set_data([("Memory", pal[3], col(2))])
        self.ch_net.set_data([("in", pal[0], col(3)), ("out", pal[1], col(4))])
        self.ch_io.set_data([("read", pal[2], col(5)), ("write", pal[4], col(6))])
        if pts:
            p = pts[-1]
            self.tiles["cpu"].set_text(fmt_pct(p[1]))
            self.tiles["mem"].set_text(fmt_bytes(p[2]))
            self.tiles["net"].set_text(f"↓ {fmt_rate(p[3])}  ↑ {fmt_rate(p[4])}")
            self.tiles["io"].set_text(f"r {fmt_rate(p[5])}  w {fmt_rate(p[6])}")
