"""Images, Volumes, Networks, Build cache, Resource usage and Disk usage pages."""
import json
import os
import shlex
import time

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import charts
from .cli import CommandDialog, run_capture
from .files import FileBrowser, clone_volume, export_volume, import_volume, start_volume_helper, stop_volume_helper
from .scan import ScanDialog
from .util import (release_on_leave, Row, ago, bg, cname, confirm, connect_row, disconnect_rows, esc, factory, fmt_bytes, fmt_pct,
                   fmt_rate, icon_button, label, labels, ordering, thread, detail_tag, push_detail,
                   toolbar_row, PageHeader, flat_page, table_card)


def save_dialog(win, title, name, then):
    d = Gtk.FileDialog(title=title, initial_name=name)

    def done(dlg, res):
        try:
            f = dlg.save_finish(res)
        except GLib.Error:
            return
        then(f.get_path())
    d.save(win, None, done)


def open_dialog(win, title, then, patterns=None):
    kw = {"title": title}
    if patterns:
        flt = Gtk.FileFilter()
        flt.set_name(", ".join(patterns))
        for p in patterns:
            flt.add_pattern(p)
        st = Gio.ListStore(item_type=Gtk.FileFilter)
        st.append(flt)
        kw["filters"] = st
    d = Gtk.FileDialog(**kw)

    def done(dlg, res):
        try:
            f = dlg.open_finish(res)
        except GLib.Error:
            return
        then(f.get_path())
    d.open(win, None, done)


def ask_text(win, heading, body, initial, ok, then):
    d = Adw.AlertDialog(heading=heading, body=body)
    e = Gtk.Entry(text=initial)
    e.add_css_class("monospace")
    d.set_extra_child(e)
    d.add_response("cancel", "Cancel")
    d.add_response("ok", ok)
    d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("ok")
    d.connect("response", lambda _d, r: r == "ok" and e.get_text().strip() and then(e.get_text().strip()))
    d.present(win)


def row_menu(holder, items):
    """A ⋮ menu for a table row. items: [(label, fn(row))]; holder.row is the row the cell shows right now."""
    menu = Gio.Menu()
    ag = Gio.SimpleActionGroup()
    for i, (lbl, fn) in enumerate(items):
        menu.append(lbl, f"row.a{i}")
        a = Gio.SimpleAction.new(f"a{i}", None)
        a.connect("activate", lambda *_a, f=fn: holder.row and f(holder.row))
        ag.add_action(a)
    mb = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="More actions",
                        valign=Gtk.Align.CENTER)
    mb.add_css_class("flat")
    mb.insert_action_group("row", ag)
    return mb


class TablePage(Gtk.Box):
    """Header (title + search + buttons) over a sortable, filterable ColumnView."""
    title = icon = ""
    wants_stats = False
    empty_text = ("Nothing here", "")

    def __init__(self, win, store, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store, self.settings = win, store, settings
        self.query = ""
        self.nav = Adw.NavigationView(vexpand=True)
        self.append(self.nav)
        tv = Adw.ToolbarView()
        self.wtitle = PageHeader(self.title)
        self.search = Gtk.SearchEntry(placeholder_text=f"Search {self.title.lower()}", width_chars=28)
        self.search.connect("search-changed", self._on_search)
        for b in self.header_buttons():
            self.wtitle.add(b)
        flat_page(tv, self.wtitle, toolbar_row(self.search, *self.toolbar_widgets()))
        self.model = Gio.ListStore(item_type=Row)
        self.filter = Gtk.CustomFilter.new(self._filter_fn, None)
        fm = Gtk.FilterListModel(model=self.model, filter=self.filter)
        self.view = Gtk.ColumnView(show_row_separators=True, single_click_activate=self.activatable())
        self.view.add_css_class("data-table")
        self.view.add_css_class("berth-table")
        self.sorted = Gtk.SortListModel(model=fm, sorter=self.view.get_sorter())
        self.view.set_model(Gtk.NoSelection(model=self.sorted))
        self.view.connect("activate", lambda _v, pos: self.on_activate(self.sorted.get_item(pos)))
        self.build_columns()
        self.stack = Gtk.Stack()
        self.stack.add_named(table_card(self.view), "list")
        self.stack.add_named(Adw.StatusPage(title=self.empty_text[0], description=self.empty_text[1],
                                            icon_name=self.icon), "empty")
        sp = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=32, height_request=32)
        self.stack.add_named(sp, "loading")
        self.stack.set_visible_child_name("loading")
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.note = label("", css=["dim-label", "caption", "page-note"], wrap=True)
        self.note.set_visible(False)
        top.append(self.note)
        top.append(self.stack)
        self.stack.set_vexpand(True)
        tv.set_content(top)
        self.nav.add(Adw.NavigationPage(title=self.title, child=tv, tag="list"))
        self.loaded = False

    # to override
    def header_buttons(self):
        return []

    def toolbar_widgets(self):
        return []

    def build_columns(self):
        pass

    def activatable(self):
        return False

    def on_activate(self, row):
        pass

    def match(self, row, q):
        return True

    def on_show(self):
        pass

    # helpers
    def column(self, title, text, css=(), width=-1, expand=False, sort=None, xalign=0.0, tooltip=None):
        def setup():
            lb = Gtk.Label(xalign=xalign, ellipsize=Pango.EllipsizeMode.END)
            if expand:
                lb.set_width_chars(22)
            for c in css:
                lb.add_css_class(c)
            return lb

        def bind(w, row, _li):
            disconnect_rows(w)

            def upd(r, w=w):
                w.set_text(text(r) or "")
                if tooltip:
                    w.set_tooltip_text(tooltip(r))
            upd(row)
            connect_row(w, row, "changed", upd)
        c = Gtk.ColumnViewColumn(title=title, factory=factory(setup, bind, lambda w, *_: disconnect_rows(w)),
                                 expand=expand, resizable=True)
        if width > 0:
            c.set_fixed_width(width)
        if sort:
            c.set_sorter(Gtk.CustomSorter.new(lambda a, b, _d: ordering(sort(a), sort(b)), None))
        self.view.append_column(c)
        return c

    def widget_column(self, title, setup, update, width=-1):
        def bind(w, row, _li):
            disconnect_rows(w)
            update(w, row)
            connect_row(w, row, "changed", lambda r, w=w: update(w, r))
        c = Gtk.ColumnViewColumn(title=title, factory=factory(setup, bind, lambda w, *_: disconnect_rows(w)), resizable=True)
        if width > 0:
            c.set_fixed_width(width)
        self.view.append_column(c)
        return c

    def set_rows(self, rows, sort_col=None):
        """Show these rows. Existing Row objects are updated in place (same key → same object), so a refresh every
        30 s doesn't create a fresh set of rows and cells each time — they used to pile up all day (memory leak)."""
        old = {self.model.get_item(i).key: self.model.get_item(i) for i in range(self.model.get_n_items())}
        merged = []
        for r in rows:
            o = old.get(r.key)
            if o is not None and o.kind == r.kind:
                o.data = r.data
                o.extra.update(r.extra)
                merged.append(o)
            else:
                merged.append(r)
        if [r.key for r in merged] == list(old) and all(a is b for a, b in zip(merged, old.values())):
            for o in merged:
                o.emit("changed")
            if self.view.get_sorter():
                self.view.get_sorter().changed(Gtk.SorterChange.DIFFERENT)
        else:
            self.model.splice(0, self.model.get_n_items(), merged)
        self.loaded = True
        self.stack.set_visible_child_name("list" if rows else "empty")
        if sort_col is not None and not self.view.get_sorter().get_primary_sort_column():
            self.view.sort_by_column(sort_col, Gtk.SortType.DESCENDING)

    def _filter_fn(self, row, _d):
        return not self.query or self.match(row, self.query)

    def _on_search(self, e):
        self.query = e.get_text().strip().lower()
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def focus_search(self):
        self.search.grab_focus()


# ───────────────────────────── Images ─────────────────────────────
class ImagesPage(TablePage):
    title, icon = "Images", "drive-harddisk-symbolic"
    empty_text = ("No images", "Pull one from Docker Hub with the Pull button.")

    def __init__(self, win, store, settings):
        super().__init__(win, store, settings)
        store.connect("images", lambda *_: self._fill())
        store.connect("containers", lambda *_: self._fill() if store.images is not None else None)

    def header_buttons(self):
        pull = Gtk.Button(label="Pull", tooltip_text="Search Docker Hub and pull an image")
        pull.add_css_class("suggested-action")
        pull.connect("clicked", lambda *_: PullDialog(self.win, self.store).present(self.win))
        menu = Gio.Menu()
        menu.append("Remove dangling images", "img.prune::dangling")
        menu.append("Remove all unused images…", "img.prune::unused")
        mb = Gtk.MenuButton(icon_name="user-trash-full-symbolic", menu_model=menu, tooltip_text="Clean up")
        ag = Gio.SimpleActionGroup()
        a = Gio.SimpleAction.new("prune", GLib.VariantType.new("s"))
        a.connect("activate", lambda _a, p: self._prune(p.get_string()))
        ag.add_action(a)
        self.insert_action_group("img", ag)
        load = icon_button("document-open-symbolic", "Load an image from a .tar file", self._load_file, css=())
        return [mb, load, pull]

    def _load_file(self):
        def go(path):
            self.win.toast("Loading image…")
            bg(self.store.engine.load_image, path,
               done=lambda r: (self.win.toast((r or "Loaded").strip().splitlines()[-1][:120]), self.store.refresh("images")),
               error=lambda e: self.win.toast(str(e)))
        open_dialog(self.win, "Load image", go, ["*.tar", "*.tar.gz"])

    def activatable(self):
        return True

    def build_columns(self):
        self.c_name = self.column("Name", lambda r: r.data["repo"], ["heading-sm"], expand=True, sort=lambda r: r.data["repo"])
        self.column("Tag", lambda r: r.data["tag"], ["monospace"], width=110, sort=lambda r: r.data["tag"])
        self.col_id = self.column("Image ID", lambda r: r.data["id"][7:19], ["monospace", "dim-label"], width=112)
        self.c_created = self.column("Created", lambda r: ago(r.data["created"]), ["dim-label"], width=104,
                                     sort=lambda r: r.data["created"])
        self.c_size = self.column("Size", lambda r: fmt_bytes(r.data["size"]), ["numeric"], width=90,
                                  sort=lambda r: r.data["size"], xalign=1.0)

        def status_setup():
            return Gtk.Label(xalign=0.5, halign=Gtk.Align.START, valign=Gtk.Align.CENTER, css_classes=["status-pill"])

        def status_upd(w, r):
            used = r.data["used"]
            w.set_text("In use" if used else "Dangling" if r.data["repo"] == "<none>" else "Unused")
            w.set_css_classes(["status-pill", "ok" if used else "warn" if r.data["repo"] == "<none>" else "muted"])
            w.set_tooltip_text("\n".join(used) if used else None)
        self.widget_column("Status", status_setup, status_upd, width=100)

        def act_setup():
            box = Gtk.Box(spacing=2, halign=Gtk.Align.END)
            box.run = icon_button("media-playback-start-symbolic", "Run")
            box.pull = icon_button("folder-download-symbolic", "Pull again (update)")
            box.trash = icon_button("user-trash-symbolic", "Delete")
            from . import quick
            box.row = None
            box.more = row_menu(box, [
                ("Rename…", lambda r: quick.rename_image(self.win, self.store, r.data["ref"], r.data["id"])),
                ("Run…", lambda r: RunDialog(self.win, self.store, r.data).present(self.win)),
                ("Open details", lambda r: self.on_activate(r))])
            for w in (box.run, box.pull, box.more, box.trash):
                box.append(w)
            box.run.connect("clicked", lambda *_: box.row and RunDialog(self.win, self.store, box.row.data).present(self.win))
            box.pull.connect("clicked", lambda *_: box.row and PullDialog(self.win, self.store, box.row.data["ref"]).present(self.win))
            box.trash.connect("clicked", lambda *_: box.row and self._delete(box.row))
            return box

        def act_upd(box, r):
            box.row = r
            box.trash.set_sensitive(not r.data["used"])
            box.trash.set_tooltip_text("In use by " + ", ".join(r.data["used"]) if r.data["used"] else "Delete")
            box.pull.set_sensitive(r.data["repo"] != "<none>")
        self.widget_column("", act_setup, act_upd, width=150)

    def on_show(self):
        if self.store.images is None:
            self.store.refresh("images")
        else:
            self._fill()

    def match(self, row, q):
        return q in row.data["repo"].lower() or q in row.data["tag"].lower() or q in row.data["id"]

    def _fill(self):
        imgs = self.store.images
        if imgs is None:
            return
        used = {}
        for c in self.store.containers:
            used.setdefault(c.get("ImageID"), []).append(cname(c))
        rows = []
        for im in imgs:
            tags = [t for t in (im.get("RepoTags") or []) if t != "<none>:<none>"] or ["<none>:<none>"]
            for t in tags:
                repo, _, tag = t.rpartition(":")
                rows.append(Row("image", f"{im['Id']}|{t}", {
                    "repo": repo, "tag": tag, "id": im["Id"], "ref": t if repo != "<none>" else im["Id"],
                    "created": im.get("Created", 0), "size": im.get("Size", 0), "used": used.get(im["Id"], [])}))
        self.set_rows(rows, self.c_created)
        total = sum(im.get("Size", 0) for im in imgs)
        self.wtitle.set_subtitle(f"{len(imgs)} images · {fmt_bytes(total)} (shared layers counted once on disk)")

    def _delete(self, row):
        d = row.data
        name = d["ref"] if d["repo"] != "<none>" else d["id"][7:19]

        def go():
            target = d["ref"] if d["repo"] != "<none>" else d["id"]
            bg(self.store.engine.remove_image, target,
               done=lambda _r: (self.win.toast(f"Deleted {name}"), self.store.refresh("images")),
               error=lambda e: self.win.toast(str(e)))
        confirm(self.win, f"Delete {esc(name)}?", f"{fmt_bytes(d['size'])}. It can be pulled or rebuilt again.", "Delete", go)

    def _prune(self, kind):
        def go():
            self.win.toast("Cleaning up images…")
            bg(self.store.engine.prune, "images", {"dangling": ["false"]} if kind == "unused" else {"dangling": ["true"]},
               done=lambda r: (self.win.toast(f"Freed {fmt_bytes((r or {}).get('SpaceReclaimed', 0))}"),
                               self.store.refresh("images")),
               error=lambda e: self.win.toast(str(e)))
        if kind == "dangling":
            confirm(self.win, "Remove dangling images?", "Untagged leftovers that no tag points to. Named images stay.",
                    "Remove", go, destructive=False)
        else:
            confirm(self.win, "Remove every unused image?",
                    "Deletes all images that no container (running <b>or stopped</b>) uses. They will need to be pulled or rebuilt.",
                    "Remove all unused", go)

    def on_activate(self, row):
        if row is not None:
            push_detail(self.nav, ImageDetail(self, row.data))


class ImageDetail(Adw.NavigationPage):
    def __init__(self, page, d):
        super().__init__(title=d["ref"] if d["repo"] != "<none>" else d["id"][7:19], tag=detail_tag("image"))
        self.page, self.d = page, d
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        tagb = Gtk.Button(label="Tag…", tooltip_text="Give this image another name:tag")
        tagb.connect("clicked", lambda *_: self._tag())
        renb = Gtk.Button(label="Rename…", tooltip_text="Change this image's name:tag (the old name is removed)")
        renb.connect("clicked", lambda *_: __import__("berth.quick", fromlist=["x"]).rename_image(
            page.win, page.store, d["ref"], d["id"]))
        self._renb = renb
        scan = Gtk.Button(label="Scan", tooltip_text="Check for known vulnerabilities (Trivy)")
        scan.connect("clicked", lambda *_: ScanDialog(page.win, page.store, d["ref"]).present(page.win))
        more = Gio.Menu()
        more.append("Save to a file…", "imgd.save")
        more.append("Push to registry", "imgd.push")
        mb = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=more, tooltip_text="More")
        ag = Gio.SimpleActionGroup()
        for n, fn in (("save", self._save), ("push", self._push)):
            a = Gio.SimpleAction.new(n, None)
            a.connect("activate", lambda *_a, f=fn: f())
            ag.add_action(a)
        self.insert_action_group("imgd", ag)
        hb.pack_end(mb)
        hb.pack_end(tagb)
        if d["repo"] != "<none>":
            hb.pack_end(self._renb)
        hb.pack_end(scan)
        run = Gtk.Button(label="Run")
        run.add_css_class("suggested-action")
        run.connect("clicked", lambda *_: RunDialog(page.win, page.store, d).present(page.win))
        hb.pack_end(run)
        tv.add_top_bar(hb)
        self.pp = Adw.PreferencesPage()
        tv.set_content(self.pp)
        self.set_child(tv)
        bg(self._load, done=self._fill, error=lambda e: page.win.toast(str(e)))

    def _save(self):
        name = (self.d["ref"].replace("/", "_").replace(":", "_") if self.d["repo"] != "<none>" else self.d["id"][7:19]) + ".tar"

        def go(path):
            self.page.win.toast("Saving image…")
            bg(self.page.store.engine.save_image, self.d["ref"], path,
               done=lambda n: self.page.win.toast(f"Saved {fmt_bytes(n)} to {path}"), error=lambda e: self.page.win.toast(str(e)))
        save_dialog(self.page.win, "Save image", name, go)

    def _push(self):
        if self.d["repo"] == "<none>":
            return self.page.win.toast("Tag the image first (Tag…), then push")
        CommandDialog(self.page.win, f"Push {self.d['ref']}", ["docker", "push", self.d["ref"]]).present(self.page.win)

    def _tag(self):
        d = Adw.AlertDialog(heading="Tag image", body="New name, e.g. myapp:1.2 or registry.example.com/team/myapp:latest")
        e = Gtk.Entry(text=self.d["ref"] if self.d["repo"] != "<none>" else "")
        e.add_css_class("monospace")
        d.set_extra_child(e)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Tag")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def go(ref):
            last = ref.rsplit("/", 1)[-1]
            repo, tag = (ref.rsplit(":", 1) if ":" in last else (ref, "latest"))
            bg(self.page.store.engine.tag, self.d["id"], repo, tag,
               done=lambda _r: (self.page.win.toast(f"Tagged {repo}:{tag}"), self.page.store.refresh("images")),
               error=lambda er: self.page.win.toast(str(er)))
        d.connect("response", lambda _d, r: r == "ok" and e.get_text().strip() and go(e.get_text().strip()))
        d.present(self.page.win)

    def _load(self):
        e = self.page.store.engine
        return e.image_inspect(self.d["id"]), e.image_history(self.d["id"])

    def _fill(self, res):
        ins, hist = res
        cfg = ins.get("Config") or {}
        g = Adw.PreferencesGroup(title="Overview")
        for t, v in (("Tags", ", ".join(ins.get("RepoTags") or []) or "none"), ("ID", ins.get("Id")),
                     ("Created", (ins.get("Created") or "")[:19].replace("T", " ")), ("Size", fmt_bytes(ins.get("Size"))),
                     ("Platform", f"{ins.get('Os')}/{ins.get('Architecture')}"),
                     ("Command", " ".join((cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or []))),
                     ("Exposed ports", ", ".join(cfg.get("ExposedPorts") or {}) or "none"),
                     ("Working dir", cfg.get("WorkingDir") or "/"),
                     ("Used by", ", ".join(self.d["used"]) or "no containers")):
            g.add(Adw.ActionRow(title=t, subtitle=esc(v), subtitle_selectable=True))
        self.pp.add(g)
        lg = Adw.PreferencesGroup(title="Layers", description="How the image was built, newest step first.")
        for h in hist[:200]:
            cmd = (h.get("CreatedBy") or "").replace("/bin/sh -c #(nop) ", "").replace("/bin/sh -c ", "RUN ").strip()
            r = Adw.ActionRow(title=esc(cmd[:160] or "—"), subtitle=f"{fmt_bytes(h.get('Size', 0))} · {ago(h.get('Created', 0))}",
                              title_lines=2, subtitle_selectable=False)
            r.add_css_class("monospace-title")
            lg.add(r)
        self.pp.add(lg)


class PullDialog(Adw.Dialog):
    def __init__(self, win, store, ref=None):
        super().__init__(title="Pull an image", content_width=560, content_height=560)
        self.win, self.store = win, store
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=16, margin_end=16, margin_bottom=16)
        row = Gtk.Box(spacing=6)
        self.entry = Gtk.SearchEntry(placeholder_text="Search Docker Hub, or type an image like nginx:alpine", hexpand=True)
        self.entry.connect("search-changed", self._search)
        self.entry.connect("activate", lambda *_: self._pull(self.entry.get_text().strip()))
        self.btn = Gtk.Button(label="Pull")
        self.btn.add_css_class("suggested-action")
        self.btn.connect("clicked", lambda *_: self._pull(self.entry.get_text().strip()))
        row.append(self.entry)
        row.append(self.btn)
        box.append(row)
        self.progress = Gtk.ProgressBar(show_text=True, visible=False)
        box.append(self.progress)
        self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.results.add_css_class("boxed-list")
        box.append(Gtk.ScrolledWindow(child=self.results, vexpand=True))
        tv.set_content(box)
        self.set_child(tv)
        self._gen = 0
        if ref:
            self.entry.set_text(ref)
            GLib.idle_add(lambda: self._pull(ref) or False)

    def _search(self, e):
        q = e.get_text().strip()
        self._gen += 1
        gen = self._gen
        if len(q) < 2 or ":" in q or "/" in q and "." in q.split("/")[0]:
            return

        def done(res):
            if gen != self._gen:
                return
            while (ch := self.results.get_first_child()):
                self.results.remove(ch)
            for r in res or []:
                name = r.get("name", "")
                row = Adw.ActionRow(title=esc(name), subtitle=esc((r.get("description") or "")[:140]), activatable=True)
                if r.get("is_official"):
                    row.add_suffix(Gtk.Label(label="Official", css_classes=["status-pill", "ok"], valign=Gtk.Align.CENTER))
                row.add_suffix(Gtk.Label(label=f"★ {r.get('star_count', 0)}", css_classes=["dim-label", "numeric"]))
                row.connect("activated", lambda _r, n=name: (self.entry.set_text(n), self._pull(n)))
                self.results.append(row)
        GLib.timeout_add(350, lambda: gen == self._gen and bg(self.store.engine.search, q, done=done,
                                                              error=lambda e: None) and False)

    def _pull(self, ref):
        if not ref:
            return
        self.btn.set_sensitive(False)
        self.progress.set_visible(True)
        self.progress.set_fraction(0)
        self.progress.set_text(f"Pulling {ref}…")
        layers = {}
        state = {"err": None}

        def work():
            st = self.store.engine.pull(ref)
            buf = b""
            while True:
                c = st.read(1.0)
                if c is None:
                    continue
                if not c:
                    break
                buf += c
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    try:
                        ev = json.loads(ln)
                    except ValueError:
                        continue
                    if ev.get("error"):
                        state["err"] = ev["error"]
                    det = ev.get("progressDetail") or {}
                    if ev.get("id") and det.get("total"):
                        layers[ev["id"]] = (det.get("current", 0), det["total"])
                    elif ev.get("id") and ev.get("status") in ("Pull complete", "Already exists"):
                        cur = layers.get(ev["id"], (1, 1))
                        layers[ev["id"]] = (cur[1], cur[1])
                GLib.idle_add(tick)
            st.close()

        def tick():
            if layers:
                cur = sum(a for a, _ in layers.values())
                tot = sum(b for _, b in layers.values()) or 1
                self.progress.set_fraction(min(1, cur / tot))
                self.progress.set_text(f"{fmt_bytes(cur)} of {fmt_bytes(tot)}")
            return False

        def done(_r):
            self.btn.set_sensitive(True)
            if state["err"]:
                self.progress.set_text(state["err"])
                return
            self.progress.set_fraction(1)
            self.progress.set_text("Done")
            self.win.toast(f"Pulled {ref}")
            self.store.refresh("images")

        def fail(e):
            self.btn.set_sensitive(True)
            self.progress.set_text(str(e))
        bg(work, done=done, error=fail)


class RunDialog(Adw.Dialog):
    """Docker Desktop's 'Run' with optional settings: name, ports, env, volumes, restart policy, command."""

    def __init__(self, win, store, image):
        super().__init__(title=f"Run {image['ref']}", content_width=520)
        self.win, self.store, self.image = win, store, image
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.run_btn = Gtk.Button(label="Run")
        self.run_btn.add_css_class("suggested-action")
        self.run_btn.connect("clicked", lambda *_: self._run())
        hb.pack_start(cancel)
        hb.pack_end(self.run_btn)
        tv.add_top_bar(hb)
        pp = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Optional settings")
        self.name = Adw.EntryRow(title="Container name")
        self.ports = Adw.EntryRow(title="Ports  host:container, comma separated")
        self.env = Adw.EntryRow(title="Environment  KEY=value, comma separated")
        self.vols = Adw.EntryRow(title="Volumes  name-or-path:/container/path, comma separated")
        self.cmd = Adw.EntryRow(title="Command (leave empty for the image default)")
        self.restart = Adw.ComboRow(title="Restart policy", model=Gtk.StringList.new(["no", "unless-stopped", "always", "on-failure"]))
        for w in (self.name, self.ports, self.env, self.vols, self.cmd, self.restart):
            g.add(w)
        pp.add(g)
        tv.set_content(pp)
        self.set_child(tv)
        bg(store.engine.image_inspect, image["id"], done=self._prefill, error=lambda e: None)

    def _prefill(self, ins):
        exposed = list(((ins or {}).get("Config") or {}).get("ExposedPorts") or {})
        if exposed and not self.ports.get_text():
            self.ports.set_text(", ".join(f"{p.split('/')[0]}:{p.split('/')[0]}" for p in exposed))

    def _run(self):
        split = lambda s: [x.strip() for x in s.split(",") if x.strip()]
        expose, bind = {}, {}
        try:
            for p in split(self.ports.get_text()):
                host, _, ctr = p.rpartition(":")
                ctr = ctr if "/" in ctr else ctr + "/tcp"
                expose[ctr] = {}
                bind.setdefault(ctr, []).append({"HostPort": host or ctr.split("/")[0]})
            cmd = shlex.split(self.cmd.get_text()) if self.cmd.get_text().strip() else None
        except ValueError as e:
            self.win.toast(f"Check the settings: {e}")
            return
        cfg = {"Image": self.image["ref"], "Env": split(self.env.get_text()), "ExposedPorts": expose,
               "HostConfig": {"PortBindings": bind, "Binds": split(self.vols.get_text()),
                              "RestartPolicy": {"Name": self.restart.get_selected_item().get_string()}}}
        if cmd:
            cfg["Cmd"] = cmd
        name = self.name.get_text().strip()
        self.run_btn.set_sensitive(False)

        def work():
            r = self.store.engine.create_container(name, cfg)
            self.store.engine.action(r["Id"], "start")
            return r["Id"]

        def done(cid):
            self.win.toast(f"Started {name or cid[:12]}")
            self.store.refresh("containers")
            self.close()
            self.win.show_container(cid)

        def fail(e):
            self.run_btn.set_sensitive(True)
            self.win.toast(str(e))
        bg(work, done=done, error=fail)


# ───────────────────────────── Volumes ─────────────────────────────
class VolumesPage(TablePage):
    title, icon = "Volumes", "folder-symbolic"
    empty_text = ("No volumes", "Volumes keep data (databases, uploads) outside containers.")

    def __init__(self, win, store, settings):
        self.sizes = None
        super().__init__(win, store, settings)
        self.note.set_text("Volumes hold databases and uploads. Only unused volumes can be deleted, and you type the name to confirm.")
        self.note.set_visible(True)
        store.connect("volumes", lambda *_: self._fill())
        store.connect("containers", lambda *_: self._fill() if store.volumes is not None else None)

    def activatable(self):
        return True

    def on_activate(self, row):
        if row is not None:
            push_detail(self.nav, VolumeDetail(self, row.data))

    def header_buttons(self):
        new = icon_button("list-add-symbolic", "Create volume", self._create, css=())
        measure = Gtk.Button(label="Measure sizes", tooltip_text="Ask Docker for the size of every volume (takes a few seconds)")
        measure.connect("clicked", lambda *_: self._measure())
        prune = icon_button("user-trash-full-symbolic", "Delete all unused volumes…", self._prune, css=())
        return [prune, measure, new]

    def build_columns(self):
        self.column("Name", lambda r: r.data["name"], ["heading-sm"], expand=True, sort=lambda r: r.data["name"],
                    tooltip=lambda r: r.data["name"])
        self.column("Stack", lambda r: r.data["stack"], ["dim-label"], width=150, sort=lambda r: r.data["stack"])

        def used_setup():
            return Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)

        def used_upd(w, r):
            u = r.data["used"]
            w.set_text(u[0] + (f" +{len(u) - 1}" if len(u) > 1 else "") if u else "Unused")
            w.set_css_classes([] if u else ["dim-label"])
            w.set_tooltip_text("\n".join(u) if u else None)
        self.widget_column("In use by", used_setup, used_upd, width=230)
        self.c_size = self.column("Size", lambda r: fmt_bytes(r.data["size"]) if r.data["size"] is not None else "—",
                                  ["numeric"], width=90, xalign=1.0, sort=lambda r: r.data["size"] or -1)
        self.column("Created", lambda r: ago(r.data["created"]) if r.data["created"] else "", ["dim-label"], width=110,
                    sort=lambda r: r.data["created"])

        def act_setup():
            from . import quick
            box = Gtk.Box(spacing=2, halign=Gtk.Align.END)
            box.row = None
            box.more = row_menu(box, [
                ("Rename…", lambda r: quick.rename_volume(self.win, self.store, r.key)),
                ("Clone…", lambda r: quick.clone_volume_ask(self.win, self.store, r.key)),
                ("Open (browse files)", lambda r: self.on_activate(r))])
            box.trash = icon_button("user-trash-symbolic", "Delete")
            box.trash.connect("clicked", lambda *_: box.row and self._delete(box.row))
            box.append(box.more)
            box.append(box.trash)
            return box

        def act_upd(box, r):
            box.row = r
            box.trash.set_sensitive(not r.data["used"])
            box.trash.set_tooltip_text("In use — delete its container first" if r.data["used"] else "Delete")
        self.widget_column("", act_setup, act_upd, width=88)

    def on_show(self):
        if self.store.volumes is None:
            self.store.refresh("volumes")
        else:
            self._fill()

    def match(self, row, q):
        return q in row.data["name"].lower() or q in row.data["stack"].lower() or any(q in u.lower() for u in row.data["used"])

    def _fill(self):
        vols = self.store.volumes
        if vols is None:
            return
        used = {}
        for c in self.store.containers:
            for m in c.get("Mounts") or []:
                if m.get("Type") == "volume":
                    used.setdefault(m.get("Name"), []).append(cname(c))
        rows = []
        for v in vols:
            created = 0
            if v.get("CreatedAt"):
                try:
                    created = time.mktime(time.strptime(v["CreatedAt"][:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError:
                    pass
            rows.append(Row("volume", v["Name"], {"name": v["Name"], "stack": (v.get("Labels") or {}).get("com.docker.compose.project", ""),
                                                  "used": used.get(v["Name"], []), "created": created,
                                                  "size": (self.sizes or {}).get(v["Name"])}))
        self.set_rows(rows)
        self.wtitle.set_subtitle(f"{len(vols)} volumes · {sum(1 for r in rows if not r.data['used'])} unused")

    def _measure(self):
        self.win.toast("Measuring volume sizes…")

        def done(df):
            self.sizes = {v["Name"]: (v.get("UsageData") or {}).get("Size", -1) for v in (df.get("Volumes") or [])}
            self._fill()
        bg(self.store.engine.df, done=done, error=lambda e: self.win.toast(str(e)))

    def _create(self):
        d = Adw.AlertDialog(heading="Create a volume", body="Name it (letters, digits, - _ .).")
        e = Gtk.Entry(placeholder_text="my-data")
        d.set_extra_child(e)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Create")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.connect("response", lambda _d, r: r == "ok" and e.get_text().strip() and bg(
            self.store.engine.create_volume, e.get_text().strip(),
            done=lambda _r: self.store.refresh("volumes"), error=lambda er: self.win.toast(str(er))))
        d.present(self.win)

    def _delete(self, row):
        n = row.data["name"]
        confirm(self.win, "Delete this volume for good?",
                f"<tt>{esc(n)}</tt>\nWhatever it holds is gone permanently. Type the name to confirm.", "Delete volume",
                lambda: bg(self.store.engine.remove_volume, n, done=lambda _r: (self.win.toast("Volume deleted"),
                                                                              self.store.refresh("volumes")),
                           error=lambda e: self.win.toast(str(e))), typed=n)

    def _prune(self):
        n = sum(1 for i in range(self.model.get_n_items()) if not self.model.get_item(i).data["used"])
        confirm(self.win, f"Delete all {n} unused volumes?",
                "Every volume that no container (running or stopped) uses is deleted with its data. Type <b>delete</b> to confirm.",
                "Delete unused volumes",
                lambda: bg(self.store.engine.prune, "volumes", {"all": ["true"]},
                           done=lambda r: (self.win.toast(f"Freed {fmt_bytes((r or {}).get('SpaceReclaimed', 0))}"),
                                           self.store.refresh("volumes")),
                           error=lambda e: self.win.toast(str(e))), typed="delete")


class VolumeDetail(Adw.NavigationPage):
    """A volume's details and its files. Files are reached through a tiny throwaway busybox container that
    mounts the volume at /volume; it is stopped (and removed) when you leave this page."""

    def __init__(self, page, d):
        super().__init__(title=d["name"], tag=detail_tag("volume"))
        self.page, self.d, self.helper = page, d, None
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.wt = Adw.WindowTitle(title=d["name"], subtitle=("Used by " + ", ".join(d["used"])) if d["used"] else "Not used by any container")
        hb.set_title_widget(self.wt)
        for lbl, tip, fn in (("Rename…", "Give the volume a new name (containers using it are switched over)",
                              self._rename),
                             ("Clone…", "Copy everything into a new volume", self._clone),
                             ("Import…", "Unpack a .tar / .tar.gz into this volume", self._import),
                             ("Export…", "Save the volume's files as a .tar.gz", self._export)):
            b = Gtk.Button(label=lbl, tooltip_text=tip)
            b.connect("clicked", lambda *_, f=fn: f())
            hb.pack_end(b)
        tv.add_top_bar(hb)
        self.stack = Gtk.Stack()
        st = Adw.StatusPage(icon_name="folder-open-symbolic", title="Opening the volume…",
                            description="Starting a tiny helper container (busybox, about 4 MB the first time) to read it.")
        st.set_child(Adw.Spinner(width_request=32, height_request=32, halign=Gtk.Align.CENTER))
        self.stack.add_named(st, "wait")
        tv.set_content(self.stack)
        self.set_child(tv)
        self.connect("hidden", lambda *_: self._stop())
        bg(start_volume_helper, page.store.engine, d["name"], done=self._ready,
           error=lambda e: self._fail(e))

    def _ready(self, cid):
        self.helper = cid
        warn = ("Live data: these files belong to " + ", ".join(self.d["used"]) + ". Edits apply immediately — "
                "avoid editing database files while it runs.") if self.d["used"] else None
        fb = FileBrowser(self.page.win, self.page.store, cid, "/volume", note=warn)
        self.stack.add_named(fb, "files")
        self.stack.set_visible_child_name("files")

    def _fail(self, e):
        self.stack.add_named(Adw.StatusPage(icon_name="dialog-error-symbolic", title="Can't open this volume",
                                            description=esc(str(e))), "err")
        self.stack.set_visible_child_name("err")

    def _stop(self):
        if self.helper:
            bg(stop_volume_helper, self.page.store.engine, self.helper)
            self.helper = None

    def _export(self):
        win, eng, n = self.page.win, self.page.store.engine, self.d["name"]

        def go(path):
            win.toast("Exporting volume…")
            bg(export_volume, eng, n, path, done=lambda size: win.toast(f"Exported {fmt_bytes(size)} to {path}"),
               error=lambda e: win.toast(f"Export failed: {e}"))
        save_dialog(win, "Export volume", f"{n}-{time.strftime('%Y%m%d-%H%M')}.tar.gz", go)

    def _import(self):
        win, eng, n = self.page.win, self.page.store.engine, self.d["name"]

        def go(path):
            def really():
                win.toast("Importing…")
                bg(import_volume, eng, n, path, done=lambda _r: (win.toast("Imported"), self._reload_files()),
                   error=lambda e: win.toast(f"Import failed: {e}"))
            confirm(win, "Import into this volume?", f"Files from <tt>{esc(os.path.basename(path))}</tt> are unpacked into "
                    f"<tt>{esc(n)}</tt>; files with the same names are replaced.", "Import", really)
        open_dialog(win, "Import into volume", go, ["*.tar", "*.tar.gz", "*.tgz"])

    def _rename(self):
        from .files import stop_volume_helper
        from .quick import rename_volume
        if self.helper:                                   # release the volume from our own file browser first
            stop_volume_helper(self.page.store.engine, self.helper)
            self.helper = None
        self.page.nav.pop_to_tag("list")
        rename_volume(self.page.win, self.page.store, self.d["name"])

    def _clone(self):
        win, eng, n = self.page.win, self.page.store.engine, self.d["name"]

        def go(new):
            win.toast(f"Cloning into {new}…")
            bg(clone_volume, eng, n, new, done=lambda _r: (win.toast(f"Cloned to {new}"), self.page.store.refresh("volumes")),
               error=lambda e: win.toast(f"Clone failed: {e}"))
        ask_text(win, "Clone volume", "Name of the new volume", f"{n}-copy", "Clone", go)

    def _reload_files(self):
        fb = self.stack.get_child_by_name("files")
        if fb:
            fb.load(fb.cwd)


# ───────────────────────────── Networks ─────────────────────────────
BUILTIN = {"bridge", "host", "none"}


class NetworksPage(TablePage):
    title, icon = "Networks", "network-workgroup-symbolic"

    def __init__(self, win, store, settings):
        super().__init__(win, store, settings)
        store.connect("networks", lambda *_: self._fill())
        store.connect("containers", lambda *_: self._fill() if store.networks is not None else None)

    def activatable(self):
        return True

    def on_activate(self, row):
        if row is not None:
            push_detail(self.nav, NetworkDetail(self, row.data))

    def header_buttons(self):
        return [icon_button("user-trash-full-symbolic", "Delete unused networks", self._prune, css=()),
                icon_button("list-add-symbolic", "Create network", self._create, css=())]

    def _create(self):
        d = Adw.AlertDialog(heading="Create a network", body="A bridge network containers can join to reach each other by name.")
        e = Gtk.Entry(placeholder_text="my-network")
        d.set_extra_child(e)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Create")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.connect("response", lambda _d, r: r == "ok" and e.get_text().strip() and bg(
            self.store.engine.create_network, e.get_text().strip(),
            done=lambda _r: (self.win.toast("Network created"), self.store.refresh("networks")),
            error=lambda er: self.win.toast(str(er))))
        d.present(self.win)

    def build_columns(self):
        self.column("Name", lambda r: r.data["name"], ["heading-sm"], expand=True, sort=lambda r: r.data["name"])
        self.column("Driver", lambda r: r.data["driver"], ["dim-label"], width=100)
        self.column("Subnet", lambda r: r.data["subnet"], ["monospace", "dim-label"], width=150)
        self.column("Stack", lambda r: r.data["stack"], ["dim-label"], width=150, sort=lambda r: r.data["stack"])
        self.column("Containers", lambda r: str(len(r.data["att"])), ["numeric"], width=100, xalign=1.0,
                    sort=lambda r: len(r.data["att"]), tooltip=lambda r: "\n".join(r.data["att"]) or None)

        def act_setup():
            b = icon_button("user-trash-symbolic", "Delete")
            b.row = None
            b.connect("clicked", lambda *_: b.row and self._delete(b.row))
            return b

        def act_upd(b, r):
            b.row = r
            ok = r.data["name"] not in BUILTIN and not r.data["att"]
            b.set_sensitive(ok)
            b.set_tooltip_text("Delete" if ok else "Built-in" if r.data["name"] in BUILTIN else "Containers are attached")
        self.widget_column("", act_setup, act_upd, width=56)

    def on_show(self):
        if self.store.networks is None:
            self.store.refresh("networks")
        else:
            self._fill()

    def match(self, row, q):
        return q in row.data["name"].lower() or q in row.data["stack"].lower()

    def _fill(self):
        nets = self.store.networks
        if nets is None:
            return
        att = {}
        for c in self.store.containers:
            for n in ((c.get("NetworkSettings") or {}).get("Networks") or {}):
                att.setdefault(n, []).append(cname(c))
        rows = [Row("network", n["Id"], {"name": n["Name"], "id": n["Id"], "driver": n.get("Driver", ""),
                                         "subnet": ((n.get("IPAM") or {}).get("Config") or [{}])[0].get("Subnet", "") if (n.get("IPAM") or {}).get("Config") else "",
                                         "stack": (n.get("Labels") or {}).get("com.docker.compose.project", ""),
                                         "att": att.get(n["Name"], [])}) for n in nets]
        self.set_rows(rows)
        self.wtitle.set_subtitle(f"{len(nets)} networks")

    def _delete(self, row):
        confirm(self.win, f"Delete network {esc(row.data['name'])}?", "No container is attached. Compose recreates it when its stack starts.",
                "Delete", lambda: bg(self.store.engine.remove_network, row.data["id"],
                                     done=lambda _r: self.store.refresh("networks"), error=lambda e: self.win.toast(str(e))))

    def _prune(self):
        confirm(self.win, "Delete unused networks?", "Networks with no containers attached (built-in ones stay).", "Delete",
                lambda: bg(self.store.engine.prune, "networks", done=lambda _r: self.store.refresh("networks"),
                           error=lambda e: self.win.toast(str(e))), destructive=False)


class NetworkDetail(Adw.NavigationPage):
    def __init__(self, page, d):
        super().__init__(title=d["name"], tag=detail_tag("network"))
        self.page, self.d = page, d
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.pp = Adw.PreferencesPage()
        tv.set_content(self.pp)
        self.set_child(tv)
        self.groups = []
        self._h = page.store.connect("containers", lambda *_: self.reload())
        release_on_leave(self, lambda: page.store.disconnect(self._h))
        self.reload()

    def reload(self):
        bg(self.page.store.engine.req, "GET", f"/networks/{self.d['id']}", done=self._fill,
           error=lambda e: self.page.win.toast(str(e)))

    def _fill(self, n):
        for g in self.groups:
            self.pp.remove(g)
        self.groups = []
        ipam = ((n.get("IPAM") or {}).get("Config") or [{}])
        g = Adw.PreferencesGroup(title="Network")
        for t, v in (("Driver", n.get("Driver")), ("Scope", n.get("Scope")), ("Subnet", ipam[0].get("Subnet") if ipam else ""),
                     ("Gateway", ipam[0].get("Gateway") if ipam else ""), ("Internal", "yes" if n.get("Internal") else "no"),
                     ("Created", (n.get("Created") or "")[:19].replace("T", " ")), ("ID", n.get("Id"))):
            g.add(Adw.ActionRow(title=t, subtitle=esc(v or "—"), subtitle_selectable=True))
        self.pp.add(g)
        self.groups.append(g)
        att = n.get("Containers") or {}
        g2 = Adw.PreferencesGroup(title="Connected containers",
                                  description="Containers on the same network reach each other by name.")
        builtin = n.get("Name") in BUILTIN
        for cid, info in sorted(att.items(), key=lambda kv: kv[1].get("Name", "")):
            r = Adw.ActionRow(title=esc(info.get("Name")), subtitle=esc(info.get("IPv4Address") or ""))
            if not builtin:
                b = icon_button("list-remove-symbolic", "Disconnect", lambda c=cid, nm=info.get("Name"): self._disconnect(c, nm))
                r.add_suffix(b)
            g2.add(r)
        if not att:
            g2.add(Adw.ActionRow(title="No containers connected"))
        if not builtin and n.get("Driver") != "host":
            others = [c for c in self.page.store.containers if c["Id"] not in att]
            if others:
                names = [cname(c) for c in sorted(others, key=cname)]
                ids = [c["Id"] for c in sorted(others, key=cname)]
                row = Adw.ActionRow(title="Connect a container")
                dd = Gtk.DropDown.new_from_strings(names)
                dd.set_enable_search(True)
                dd.set_valign(Gtk.Align.CENTER)
                go = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER)
                go.add_css_class("suggested-action")
                go.connect("clicked", lambda *_: self._connect(ids[dd.get_selected()], names[dd.get_selected()]))
                row.add_suffix(dd)
                row.add_suffix(go)
                g2.add(row)
        self.pp.add(g2)
        self.groups.append(g2)

    def _connect(self, cid, name):
        bg(self.page.store.engine.connect_network, self.d["id"], cid,
           done=lambda _r: (self.page.win.toast(f"Connected {name}"), self.page.store.refresh("containers")),
           error=lambda e: self.page.win.toast(str(e)))

    def _disconnect(self, cid, name):
        confirm(self.page.win, f"Disconnect {esc(name)}?", f"It can no longer reach containers on {esc(self.d['name'])}.", "Disconnect",
                lambda: bg(self.page.store.engine.disconnect_network, self.d["id"], cid,
                           done=lambda _r: (self.page.win.toast(f"Disconnected {name}"), self.page.store.refresh("containers")),
                           error=lambda e: self.page.win.toast(str(e))))


# ───────────────────────────── Builds (buildx history) ─────────────────────────────
class BuildHistoryPage(TablePage):
    title, icon = "Builds", "system-run-symbolic"
    empty_text = ("No builds recorded", "Images you build with docker build / compose build appear here.")

    def header_buttons(self):
        return [icon_button("view-refresh-symbolic", "Reload", self.on_show, css=())]

    def activatable(self):
        return True

    def build_columns(self):
        self.column("Name", lambda r: r.data.get("name") or r.data.get("ref", "")[-12:], ["heading-sm"], expand=True,
                    sort=lambda r: r.data.get("name") or "")

        def st_setup():
            return Gtk.Label(xalign=0.5, halign=Gtk.Align.START, valign=Gtk.Align.CENTER, css_classes=["status-pill"])

        def st_upd(w, r):
            stt = r.data.get("status", "")
            w.set_text(stt)
            w.set_css_classes(["status-pill", "ok" if stt == "Completed" else "warn" if stt in ("Running", "Canceled") else "bad"])
        self.widget_column("Status", st_setup, st_upd, width=110)
        self.column("Steps", lambda r: f"{r.data.get('completed_steps', 0)}/{r.data.get('total_steps', 0)}"
                    + (f" · {r.data.get('cached_steps')} cached" if r.data.get("cached_steps") else ""), ["dim-label"], width=150)
        self.column("Duration", lambda r: _dur(r.data), ["numeric"], width=90, xalign=1.0, sort=lambda r: _secs(r.data))
        self.c_started = self.column("Started", lambda r: _iso_ago(r.data.get("created_at")), ["dim-label"], width=120,
                                     sort=lambda r: r.data.get("created_at") or "")

    def match(self, row, q):
        return q in (row.data.get("name") or "").lower()

    def on_show(self):
        self.stack.set_visible_child_name("loading")

        def work():
            out = run_capture(["docker", "buildx", "history", "ls", "--format", "{{json .}}"], 60)
            return [json.loads(ln) for ln in out.splitlines() if ln.strip().startswith("{")]

        def done(rows):
            self.set_rows([Row("build", b.get("ref", str(i)), b) for i, b in enumerate(rows)], self.c_started)
            self.wtitle.set_subtitle(f"{len(rows)} builds · {sum(1 for b in rows if b.get('status') != 'Completed')} not completed")
        bg(work, done=done, error=lambda e: (self.set_rows([]), self.win.toast(f"Build history: {e}")))

    def on_activate(self, row):
        if row is not None:
            push_detail(self.nav, BuildDetail(self, row.data))


def _short_ref(ref):
    """`buildx history ls` reports refs as builder/node/id, but inspect / logs / rm only accept the id."""
    return ref.rsplit("/", 1)[-1]


def _build_error(e):
    msg = str(e)
    if "no record found" in msg:
        return ("This build's record is no longer kept by BuildKit — records are dropped when the build cache is "
                "cleared or the history limit is reached. Reload the list to see the builds that are still recorded.")
    return msg


def _secs(b):
    try:
        import calendar
        a = calendar.timegm(time.strptime(b["created_at"][:19], "%Y-%m-%dT%H:%M:%S"))
        z = calendar.timegm(time.strptime(b["completed_at"][:19], "%Y-%m-%dT%H:%M:%S"))
        return z - a
    except (KeyError, ValueError, TypeError):
        return -1


def _dur(b):
    s = _secs(b)
    return "—" if s < 0 else f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


class BuildDetail(Adw.NavigationPage):
    def __init__(self, page, b):
        super().__init__(title=b.get("name") or "Build", tag=detail_tag("build"))
        self.page, self.b = page, b
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        rm = icon_button("user-trash-symbolic", "Delete this build record", self._delete)
        hb.pack_end(rm)
        tv.add_top_bar(hb)
        self.tabs = Adw.ViewStack()
        sw = Adw.InlineViewSwitcher(stack=self.tabs, margin_start=14, margin_top=8, margin_bottom=6, halign=Gtk.Align.START)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(sw)
        box.append(self.tabs)
        self.tabs.set_vexpand(True)
        self.views = {}
        for name, title in (("logs", "Build log"), ("inspect", "Details")):
            t = Gtk.TextView(editable=False, monospace=True, left_margin=12, right_margin=12, top_margin=6, bottom_margin=10)
            t.add_css_class("logview")
            t.get_buffer().set_text("Loading…")
            self.views[name] = t
            self.tabs.add_titled(Gtk.ScrolledWindow(child=t, vexpand=True), name, title)
        tv.set_content(box)
        self.set_child(tv)
        ref = _short_ref(b.get("ref", ""))
        for name, argv in (("logs", ["docker", "buildx", "history", "logs", ref]),
                           ("inspect", ["docker", "buildx", "history", "inspect", ref])):
            bg(run_capture, argv, 120, True, done=lambda out, n=name: self.views[n].get_buffer().set_text(out or "(empty)"),
               error=lambda e, n=name: self.views[n].get_buffer().set_text(_build_error(e)))

    def _delete(self):
        confirm(self.page.win, "Delete this build record?", "Only the history entry is removed, not the image.", "Delete",
                lambda: bg(run_capture, ["docker", "buildx", "history", "rm", _short_ref(self.b.get("ref", ""))], 60,
                           done=lambda _o: (self.page.win.toast("Build record deleted"), self.page.nav.pop_to_tag("list"),
                                            self.page.on_show()),
                           error=lambda e: self.page.win.toast(str(e))))


# ───────────────────────────── Build cache ─────────────────────────────
class BuildsPage(TablePage):
    title, icon = "Build cache", "applications-engineering-symbolic"
    empty_text = ("Build cache is empty", "It fills up as images are built; clearing it only costs rebuild time.")

    def header_buttons(self):
        clear = Gtk.Button(label="Clear build cache")
        clear.add_css_class("destructive-action")
        clear.connect("clicked", lambda *_: self._clear())
        return [icon_button("view-refresh-symbolic", "Reload", self.on_show, css=()), clear]

    def build_columns(self):
        self.column("Description", lambda r: r.data.get("Description") or r.data.get("ID", "")[:12], ["monospace"], expand=True,
                    tooltip=lambda r: r.data.get("Description"))
        self.column("Type", lambda r: r.data.get("Type", ""), ["dim-label"], width=110, sort=lambda r: r.data.get("Type", ""))
        self.c_size = self.column("Size", lambda r: fmt_bytes(r.data.get("Size", 0)), ["numeric"], width=90, xalign=1.0,
                                  sort=lambda r: r.data.get("Size", 0))
        self.column("In use", lambda r: "yes" if r.data.get("InUse") else "", ["dim-label"], width=70)
        self.column("Shared", lambda r: "yes" if r.data.get("Shared") else "", ["dim-label"], width=70)
        self.column("Last used", lambda r: _iso_ago(r.data.get("LastUsedAt")), ["dim-label"], width=120,
                    sort=lambda r: r.data.get("LastUsedAt") or "")

    def match(self, row, q):
        return q in (row.data.get("Description") or "").lower()

    def on_show(self):
        self.stack.set_visible_child_name("loading")

        def done(df):
            bc = df.get("BuildCache") or []
            self.set_rows([Row("cache", b.get("ID", str(i)), b) for i, b in enumerate(bc)], self.c_size)
            self.wtitle.set_subtitle(f"{len(bc)} entries · {fmt_bytes(sum(b.get('Size', 0) for b in bc))}")
        bg(self.store.engine.df, done=done, error=lambda e: self.win.toast(str(e)))

    def _clear(self):
        confirm(self.win, "Clear the build cache?", "The next build of each image takes longer; nothing else changes.", "Clear",
                lambda: bg(self.store.engine.prune, "builder",
                           done=lambda r: (self.win.toast(f"Freed {fmt_bytes((r or {}).get('SpaceReclaimed', 0))}"), self.on_show()),
                           error=lambda e: self.win.toast(str(e))), destructive=False)


def _iso_ago(s):
    if not s:
        return ""
    try:
        import calendar
        return ago(calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return ""


# ───────────────────────────── Resource usage ─────────────────────────────
class ResourcesPage(Gtk.Box):
    title, icon = "Resource usage", "utilities-system-monitor-symbolic"
    wants_stats = True

    def on_size(self, n):
        from .util import set_columns
        set_columns(self.tiles_box, 2 if n in ("sm", "md") else 4)
        set_columns(self.charts_box, 1 if n == "sm" else 2)

    def __init__(self, win, store, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store, self.settings = win, store, settings
        tv = Adw.ToolbarView(vexpand=True)
        self.header = PageHeader("Resource usage", "Live CPU, memory, network and disk — read only while this page is open")
        self.range = Adw.ToggleGroup(can_shrink=False)
        for name, lbl in (("60", "1 min"), ("300", "5 min"), ("600", "10 min"), ("1800", "30 min")):
            self.range.add(Adw.Toggle(name=name, label=lbl))
        self.range.set_active_name(str(settings["range"] or 600))
        self.range.connect("notify::active-name", lambda *_: (self.settings.__setitem__("range", int(self.range.get_active_name())), self.update(force=True)))
        flat_page(tv, self.header, toolbar_row(label("Show the last", css=["dim-label"], valign=Gtk.Align.CENTER), self.range))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, css_classes=["page-body"])
        # 4 tiles side by side; the window's breakpoint switches this to 2 × 2 on narrow windows (never 3 + 1)
        tiles = Gtk.FlowBox(homogeneous=True, min_children_per_line=4, max_children_per_line=4, column_spacing=12,
                            row_spacing=12, selection_mode=Gtk.SelectionMode.NONE)
        self.tiles_box = tiles
        self.tiles = {}
        for key, title in (("cpu", "CPU · containers"), ("mem", "Memory · containers"), ("net", "Network"), ("io", "Disk I/O")):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["card", "tile"])
            card.append(label(title.upper(), css=["caption", "dim-label", "tile-k"]))
            v = label("—", css=["tile-v", "numeric"])
            s = label(" ", css=["caption", "dim-label"])
            card.append(v)
            card.append(s)
            if key == "mem":
                lvl = Gtk.LevelBar(min_value=0, max_value=1, margin_top=6)
                card.append(lvl)
                self.memlvl = lvl
            self.tiles[key] = (v, s)
            tiles.append(card)
        body.append(tiles)
        row = Gtk.FlowBox(homogeneous=True, min_children_per_line=1, max_children_per_line=2, column_spacing=12,
                          row_spacing=12, selection_mode=Gtk.SelectionMode.NONE)
        self.charts_box = row
        self.ch_cpu = charts.LineChart(lambda v: fmt_pct(v), 200)
        self.ch_mem = charts.LineChart(fmt_bytes, 200, binary=True)
        self.legends = {}
        for title, ch, key in (("CPU by stack · % of all threads", self.ch_cpu, "cpu"), ("Memory by stack", self.ch_mem, "mem")):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card", "chart-card"])
            card.append(label(title, css=["heading"]))
            card.append(ch)
            holder = Gtk.Box()
            card.append(holder)
            self.legends[key] = (holder, None)
            row.append(card)
        body.append(row)
        # heaviest containers table
        tcard = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card", "chart-card"])
        tcard.append(label("Containers by memory", css=["heading"]))
        self.top_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True)
        self.top_list.connect("row-activated", lambda _l, row: getattr(row.get_child(), "cid", None)
                              and self.win.show_container(row.get_child().cid))
        self.top_list.add_css_class("top-list")
        tcard.append(self.top_list)
        body.append(tcard)
        self.top_rows = []
        for _ in range(12):
            r = Gtk.Box(spacing=12, margin_top=3, margin_bottom=3, margin_start=4, margin_end=4)
            r.name = label("", hexpand=True)
            r.cpu = label("", css=["numeric"], xalign=1.0, width_chars=7)
            r.mem = label("", css=["numeric"], xalign=1.0, width_chars=9)
            r.net = label("", css=["numeric", "dim-label"], xalign=1.0, width_chars=18)
            r.spark = charts.Sparkline(90, 20)
            for w in (r.name, r.cpu, r.mem, r.net, r.spark):
                r.append(w)
            self.top_rows.append(r)
            self.top_list.append(r)
        tv.set_content(Gtk.ScrolledWindow(child=body, vexpand=True))
        self.append(tv)
        store.connect("stats", lambda *_: self.update())

    def on_show(self):
        self.update(force=True)

    def _by_project(self, allpts, idx, scale=1.0):
        proj = {}
        names = {c["Id"]: (labels(c).get("com.docker.compose.project") or cname(c)) for c in self.store.containers}
        for cid, pts in allpts.items():
            m = proj.setdefault(names.get(cid, cid[:12]), {})
            for p in pts:
                t = round(p[0])
                m[t] = m.get(t, 0) + p[idx] * scale
        out = []
        for name, m in proj.items():
            pts = sorted(m.items())
            out.append((pts[-1][1] if pts else 0, name, pts))
        out.sort(key=lambda x: -x[0])
        pal = charts.palette()
        return [(n, pal[i % len(pal)], pts) for i, (_, n, pts) in enumerate(out)]

    def update(self, force=False):
        if not (force or self.get_mapped()):
            return
        s = self.store.sampler
        allpts = s.all_points()
        win = int(self.range.get_active_name() or 600)
        cpu = self._by_project(allpts, 1, 1 / s.ncpu)
        mem = self._by_project(allpts, 2)
        self.ch_cpu.set_data(cpu, win)
        self.ch_mem.set_data(mem, win)
        for key, ch, ser in (("cpu", self.ch_cpu, cpu), ("mem", self.ch_mem, mem)):
            holder, sig = self.legends[key]
            new = tuple((n, c) for n, c, _ in ser)
            if new != sig:
                if holder.get_first_child():
                    holder.remove(holder.get_first_child())
                holder.append(charts.legend(ch, new))
                self.legends[key] = (holder, new)
        h = s.host_points()
        if h:
            t, hcpu, used, total, dcpu, dmem = h[-1]
            self.tiles["cpu"][0].set_text(fmt_pct(dcpu))
            self.tiles["cpu"][1].set_text(f"system {fmt_pct(hcpu)} · {s.ncpu} threads")
            self.tiles["mem"][0].set_text(fmt_bytes(dmem))
            self.tiles["mem"][1].set_text(f"system {fmt_bytes(used)} / {fmt_bytes(total, 0)}")
            self.memlvl.set_value(used / total if total and used is not None else 0)
        rx = tx = rd = wr = 0
        lat = []
        for cid, pts in allpts.items():
            if pts:
                p = pts[-1]
                rx, tx, rd, wr = rx + p[3], tx + p[4], rd + p[5], wr + p[6]
                lat.append((p, cid, pts))
        self.tiles["net"][0].set_text(fmt_rate(rx + tx))
        self.tiles["net"][1].set_text(f"↓ {fmt_rate(rx)}   ↑ {fmt_rate(tx)}")
        self.tiles["io"][0].set_text(fmt_rate(rd + wr))
        self.tiles["io"][1].set_text(f"read {fmt_rate(rd)}   write {fmt_rate(wr)}")
        lat.sort(key=lambda x: -x[0][2])
        names = {c["Id"]: cname(c) for c in self.store.containers}
        for i, r in enumerate(self.top_rows):
            if i < len(lat):
                p, cid, pts = lat[i]
                r.set_visible(True)
                r.cid = cid
                r.set_tooltip_text("Click to open")
                r.name.set_text(names.get(cid, cid[:12]))
                r.cpu.set_text(fmt_pct(p[1]))
                r.mem.set_text(fmt_bytes(p[2]))
                r.net.set_text(f"↓{fmt_rate(p[3])} ↑{fmt_rate(p[4])}")
                r.spark.set_values([q[2] for q in pts])
            else:
                r.set_visible(False)


# ───────────────────────────── Disk usage ─────────────────────────────
class DiskPage(Gtk.Box):
    title, icon = "Disk usage", "drive-harddisk-system-symbolic"
    wants_stats = False

    def __init__(self, win, store, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store = win, store
        tv = Adw.ToolbarView(vexpand=True)
        header = PageHeader("Disk usage", "Where Docker keeps its data, and what you can safely reclaim")
        header.add(Gtk.Button(label="Measure again", tooltip_text="Ask Docker for fresh sizes")).connect("clicked", lambda *_: self.on_show())
        flat_page(tv, header)
        self.pp = Adw.PreferencesPage()
        self.fs_group = Adw.PreferencesGroup(title="Where Docker keeps its data")
        self.pp.add(self.fs_group)
        self.cat_group = Adw.PreferencesGroup(title="Inside Docker", description="Sizes as Docker reports them. Cleaning frees space on the drive above.")
        self.spinner = Adw.Spinner(width_request=20, height_request=20)
        self.cat_group.set_header_suffix(self.spinner)
        self.pp.add(self.cat_group)
        self._rows = []
        tv.set_content(self.pp)
        self.append(tv)

    def _clear(self, group):
        for r in [r for r in self._rows if r.get_parent() is not None and r._group is group]:
            group.remove(r)
            self._rows.remove(r)

    def _add(self, group, row):
        row._group = group
        group.add(row)
        self._rows.append(row)

    def on_show(self):
        self._clear(self.fs_group)
        root = self.store.info.get("DockerRootDir") or "/var/lib/docker"
        try:
            st = os.statvfs(root)
            total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
            r = Adw.ActionRow(title=esc(root), subtitle=f"{fmt_bytes(total - free)} used · {fmt_bytes(free)} free of {fmt_bytes(total)}")
            lvl = Gtk.LevelBar(min_value=0, max_value=1, value=(total - free) / total if total else 0, valign=Gtk.Align.CENTER,
                               width_request=220)
            r.add_suffix(lvl)
            self._add(self.fs_group, r)
        except OSError:
            self._add(self.fs_group, Adw.ActionRow(title=esc(root), subtitle="Not readable without root — sizes below still work"))
        self.spinner.set_visible(True)
        bg(self.store.engine.df, done=self._fill, error=lambda e: (self.spinner.set_visible(False), self.win.toast(str(e))))

    def _fill(self, d):
        self.spinner.set_visible(False)
        self._clear(self.cat_group)
        imgs, cs, vols, bc = d.get("Images") or [], d.get("Containers") or [], d.get("Volumes") or [], d.get("BuildCache") or []
        vsize = lambda v: max(0, (v.get("UsageData") or {}).get("Size", 0))
        rows = [
            ("Images", f"{len(imgs)} images", d.get("LayersSize") or sum(i.get("Size", 0) for i in imgs),
             sum(i.get("Size", 0) for i in imgs if not i.get("Containers")), "Remove dangling", lambda: self._prune("images", {"dangling": ["true"]})),
            ("Containers", f"{len(cs)} containers (their writable layers)", sum(c.get("SizeRw", 0) or 0 for c in cs),
             sum(c.get("SizeRw", 0) or 0 for c in cs if c.get("State") != "running"), None, None),
            ("Volumes", f"{len(vols)} volumes", sum(vsize(v) for v in vols),
             sum(vsize(v) for v in vols if not (v.get("UsageData") or {}).get("RefCount")), "Review", lambda: self.win.go("volumes")),
            ("Build cache", f"{len(bc)} entries", sum(b.get("Size", 0) for b in bc),
             sum(b.get("Size", 0) for b in bc if not b.get("InUse")), "Clear", lambda: self._prune("builder", None)),
        ]
        for title, sub, size, recl, btn, fn in rows:
            r = Adw.ActionRow(title=f"{title}  ·  {fmt_bytes(size)}", subtitle=f"{sub} · {fmt_bytes(recl)} not in use")
            if btn:
                b = Gtk.Button(label=btn, valign=Gtk.Align.CENTER)
                b.connect("clicked", lambda *_, f=fn: f())
                r.add_suffix(b)
            self._add(self.cat_group, r)

    def _prune(self, kind, filters):
        what = {"images": ("Remove dangling images?", "Untagged leftovers. Named images stay."),
                "builder": ("Clear the build cache?", "Rebuilds take longer; nothing else changes.")}[kind]
        confirm(self.win, what[0], what[1], "Remove",
                lambda: bg(self.store.engine.prune, kind, filters,
                           done=lambda r: (self.win.toast(f"Freed {fmt_bytes((r or {}).get('SpaceReclaimed', 0))}"), self.on_show()),
                           error=lambda e: self.win.toast(str(e))), destructive=False)
