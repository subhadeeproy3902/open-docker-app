"""Databases: find the database containers and explore them (tables / keys / collections / indices, data,
structure, a query console). All engine work goes through berth.db in the background; this file is only the UI."""
import json
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from . import db
from .pages import TablePage, save_dialog
from .util import (Row, PageHeader, bg, cname, confirm, detail_tag, esc, flat_page, icon_button, label, push_detail,
                   state_of, table_card)


class _Obj(GObject.Object):
    def __init__(self, v):
        super().__init__()
        self.v = v


# ── result grid ─────────────────────────────────────────────────────────────────────────────────────────────────
class ResultGrid(Gtk.Box):
    """A result: a table (ColumnView with columns built per result) or a text document, with a status line and
    export actions. Double-click a row to see every value in full."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.win, self.result = win, None
        bar = Gtk.Box(spacing=8)
        self.status = label("", css=["caption", "dim-label"], hexpand=True)
        bar.append(self.status)
        menu = Gio.Menu()
        for lbl, act in (("Copy as CSV", "csv"), ("Copy as Markdown", "md"), ("Copy as JSON", "json"),
                         ("Save as CSV…", "save-csv"), ("Save as JSON…", "save-json")):
            menu.append(lbl, f"grid.{act}")
        self.export = Gtk.MenuButton(icon_name="document-save-symbolic", menu_model=menu, tooltip_text="Export",
                                     sensitive=False)
        self.export.add_css_class("flat")
        bar.append(self.export)
        self.append(bar)
        ag = Gio.SimpleActionGroup()
        for name, fn in (("csv", lambda: self._copy(db.to_csv)), ("md", lambda: self._copy(db.to_markdown)),
                         ("json", lambda: self._copy(db.to_json)), ("save-csv", lambda: self._save("csv")),
                         ("save-json", lambda: self._save("json"))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, f=fn: f())
            ag.add_action(a)
        self.insert_action_group("grid", ag)

        self.stack = Gtk.Stack(vexpand=True)
        self.model = Gio.ListStore(item_type=_Obj)
        self.view = Gtk.ColumnView(model=Gtk.SingleSelection(model=self.model), show_row_separators=True,
                                   show_column_separators=True, reorderable=False)
        self.view.add_css_class("data-table")
        self.view.add_css_class("berth-table")
        self.view.add_css_class("db-grid")
        self.view.connect("activate", lambda _v, pos: self._inspect(self.model.get_item(pos).v))
        self.stack.add_named(table_card(self.view), "grid")
        self.text = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 top_margin=12, bottom_margin=12, left_margin=14, right_margin=14)
        self.stack.add_named(table_card(Gtk.ScrolledWindow(child=self.text)), "text")
        self.msg = Adw.StatusPage(icon_name="view-grid-symbolic", title="", description="")
        self.msg.add_css_class("compact")
        self.stack.add_named(self.msg, "msg")
        spin = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=32, height_request=32)
        self.stack.add_named(spin, "loading")
        self.append(self.stack)
        self.message("Nothing to show yet", "", "view-grid-symbolic")

    def message(self, title, body, icon="dialog-information-symbolic"):
        self.result = None
        self.export.set_sensitive(False)
        self.msg.set_title(title)
        self.msg.set_description(body)
        self.msg.set_icon_name(icon)
        self.status.set_text("")
        self.stack.set_visible_child_name("msg")

    def error(self, text):
        t = str(text)
        if "read-only mode" in t:
            self.message("Blocked: read-only mode", esc(t), "changes-prevent-symbolic")
        else:
            self.message("That didn't work", esc(t), "dialog-warning-symbolic")

    def loading(self):
        self.stack.set_visible_child_name("loading")

    def show(self, r):
        self.result = r
        self.export.set_sensitive(bool(r.rows) or r.text is not None)
        parts = []
        if r.columns:
            parts.append(f"{len(r.rows):,} row{'s' if len(r.rows) != 1 else ''}" + (" (more available)" if r.truncated else ""))
        if r.ms:
            parts.append(f"{r.ms} ms")
        if r.note:
            parts.append(r.note)
        self.status.set_text("  ·  ".join(parts))
        if r.text is not None and not r.columns:
            self.text.get_buffer().set_text(r.text)
            self.stack.set_visible_child_name("text")
            return
        if not r.columns:
            self.message("Done", esc(r.note or "The statement ran; it returned no rows."), "emblem-ok-symbolic")
            self.status.set_text(f"{r.ms} ms" if r.ms else "")
            return
        for c in list(self.view.get_columns()):
            self.view.remove_column(c)
        for i, name in enumerate(r.columns):
            self.view.append_column(self._column(name, i))
        self.model.splice(0, self.model.get_n_items(), [_Obj(row) for row in r.rows])
        self.stack.set_visible_child_name("grid" if r.rows else "msg")
        if not r.rows:
            self.message("No rows", "The result is empty.", "view-grid-symbolic")
            self.result = r

    def _column(self, name, i):
        def setup(_f, li):
            lb = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=48, single_line_mode=True)
            li.set_child(lb)

        def bind(_f, li):
            v = li.get_item().v[i] if i < len(li.get_item().v) else None
            lb = li.get_child()
            if v is None:
                lb.set_text("NULL")
                lb.add_css_class("dim-label")
            else:
                s = str(v)
                lb.set_text(s.replace("\n", " ⏎ ")[:400])
                lb.remove_css_class("dim-label")
        f = Gtk.SignalListItemFactory()
        f.connect("setup", setup)
        f.connect("bind", bind)
        return Gtk.ColumnViewColumn(title=name, factory=f, resizable=True)

    def _inspect(self, values):
        r = self.result
        d = Adw.Dialog(title="Row", content_width=620, content_height=560)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup()
        for name, v in zip(r.columns, values):
            text = "NULL" if v is None else str(v)
            try:
                if text[:1] in "{[":
                    text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except ValueError:
                pass
            row = Adw.ActionRow(title=esc(name), subtitle=esc(text[:4000]), subtitle_selectable=True)
            row.add_css_class("property")
            cp = icon_button("edit-copy-symbolic", "Copy value",
                             lambda _b=None, t=("" if v is None else str(v)): (self.get_clipboard().set(t),
                                                                               self.win.toast("Copied")))
            row.add_suffix(cp)
            g.add(row)
        page.add(g)
        tv.set_content(page)
        d.set_child(tv)
        d.present(self.win)

    def _copy(self, fmt):
        if self.result:
            self.get_clipboard().set(fmt(self.result))
            self.win.toast("Copied")

    def _save(self, kind):
        if not self.result:
            return
        data = db.to_csv(self.result) if kind == "csv" else db.to_json(self.result)

        def write(path):
            try:
                with open(path, "w") as f:
                    f.write(data)
                self.win.toast(f"Saved {path}")
            except OSError as e:
                self.win.toast(f"Couldn't save: {e}")
        save_dialog(self.win, "Save result", f"result.{kind}", write)


# ── explorer (one database container) ───────────────────────────────────────────────────────────────────────────
class Explorer(Gtk.Box):
    def __init__(self, page, backend, title, on_close=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.page, self.win, self.b = page, page.win, backend
        self.on_close = on_close
        self.node, self.offset, self.gen, self.allow = None, 0, 0, False
        self._struct_for = None
        tv = Adw.ToolbarView(vexpand=True)
        self.header = PageHeader(title, "Connecting…")
        self.db_pick = Gtk.DropDown(visible=False, valign=Gtk.Align.CENTER, tooltip_text="Database")
        self.db_pick.connect("notify::selected", self._db_changed)
        self.header.add(self.db_pick)
        if backend.write_capable:
            wbox = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER, css_classes=["write-toggle"],
                           tooltip_text="Off: Berth only reads. On: statements that change data are allowed "
                                        "(until you leave this page).")
            wbox.append(label("Allow changes", css=["caption"]))
            self.write_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
            self.write_sw.connect("notify::active", self._toggle_write)
            wbox.append(self.write_sw)
            self.wbox = wbox
            self.header.add(wbox)
        self.header.add(icon_button("view-refresh-symbolic", "Reload", lambda *_: self.reload()))
        flat_page(tv, self.header)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=280, shrink_start_child=False,
                          resize_start_child=False, vexpand=True, css_classes=["db-paned"])
        # left: the tree
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["db-side"])
        if backend.kind == "redis":
            self.find = Gtk.SearchEntry(placeholder_text="Key pattern, e.g. user:*", text="*")
            self.find.connect("activate", lambda e: self._pattern(e.get_text()))
        else:
            self.find = Gtk.SearchEntry(placeholder_text="Filter")
            self.find.connect("search-changed", lambda *_: self._fill_tree())
        left.append(self.find)
        self.root = Gio.ListStore(item_type=_Obj)
        self.tree_model = Gtk.TreeListModel.new(self.root, False, False, self._children)
        self.sel = Gtk.SingleSelection(model=self.tree_model, autoselect=False, can_unselect=True)
        self.sel.connect("selection-changed", self._picked)
        f = Gtk.SignalListItemFactory()
        f.connect("setup", self._tree_setup)
        f.connect("bind", self._tree_bind)
        self.list = Gtk.ListView(model=self.sel, factory=f, vexpand=True, css_classes=["navigation-sidebar", "db-tree"])
        left.append(Gtk.ScrolledWindow(child=self.list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.tree_note = label("", css=["caption", "dim-label"], wrap=True)
        left.append(self.tree_note)
        paned.set_start_child(left)

        # right: data · structure · query
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["db-main"])
        self.views = Adw.ViewStack(vexpand=True)
        sw = Adw.InlineViewSwitcher(stack=self.views, halign=Gtk.Align.START)
        right.append(sw)
        right.append(self.views)

        data = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        top = Adw.WrapBox(child_spacing=8, line_spacing=6)
        self.node_lbl = label("Pick a table on the left", css=["heading"])
        top.append(self.node_lbl)
        if backend.kind == "mongo":
            self.mfilter = Gtk.Entry(placeholder_text='Filter, e.g. {"status": "active"}', width_chars=34)
            self.mfilter.add_css_class("monospace")
            self.mfilter.connect("activate", lambda *_: self._apply_filter())
            top.append(self.mfilter)
        self.prev = icon_button("go-previous-symbolic", "Previous page", lambda *_: self._page(-1))
        self.next = icon_button("go-next-symbolic", "Next page", lambda *_: self._page(1))
        self.range = label("", css=["caption", "dim-label", "numeric"], valign=Gtk.Align.CENTER)
        for w in (self.prev, self.range, self.next):
            top.append(w)
        data.append(top)
        self.data = ResultGrid(self.win)
        data.append(self.data)
        self.views.add_titled_with_icon(data, "data", "Data", "view-grid-symbolic")

        self.struct = ResultGrid(self.win)
        self.views.add_titled_with_icon(self.struct, "structure", "Structure", "view-list-bullet-symbolic")

        q = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=170, shrink_start_child=False)
        ed = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.editor = Gtk.TextView(monospace=True, top_margin=10, bottom_margin=10, left_margin=12, right_margin=12,
                                   wrap_mode=Gtk.WrapMode.WORD_CHAR, css_classes=["db-editor"])
        self.editor.get_buffer().set_text(backend.example())
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._editor_key)
        self.editor.add_controller(keys)
        ed.append(table_card(Gtk.ScrolledWindow(child=self.editor, min_content_height=90)))
        row = Gtk.Box(spacing=8)
        self.run = Gtk.Button(label="Run", tooltip_text="Run (Ctrl+Enter)")
        self.run.add_css_class("suggested-action")
        self.run.connect("clicked", lambda *_: self._run())
        row.append(self.run)
        self.hist = Gtk.MenuButton(icon_name="document-open-recent-symbolic", tooltip_text="Recent queries")
        self.hist.add_css_class("flat")
        row.append(self.hist)
        row.append(label({"sql": "SQL", "redis": "Redis commands, one per line", "yql": "YQL, a JSON query, or "
                          "GET/POST /path", "http": "METHOD /path, then an optional JSON body", "mongo":
                          "mongosh code (needs “Allow changes”)"}.get(backend.language, ""),
                         css=["caption", "dim-label"], valign=Gtk.Align.CENTER))
        ed.append(row)
        q.set_start_child(ed)
        self.out = ResultGrid(self.win)
        q.set_end_child(self.out)
        self.views.add_titled_with_icon(q, "query", "Query", "utilities-terminal-symbolic")
        self.views.connect("notify::visible-child-name", lambda *_: self._tab())
        paned.set_end_child(right)

        tv.set_content(paned)
        self.append(tv)
        self._fill_history()
        self.reload()

    # ── connection ──
    def reload(self):
        self.gen += 1
        g = self.gen
        self.header.set_subtitle("Connecting…")
        self.data.loading()

        def work():
            desc = self.b.connect()
            dbs = self.b.databases() if self.b.multi_db else []
            return desc, dbs, self.b.tree()

        def done(r):
            if g != self.gen:
                return
            desc, dbs, nodes = r
            self.desc = desc
            self.header.set_subtitle(f"{desc}" + (f" · database {self.b.db}" if self.b.db and self.b.kind != "redis"
                                                  else f" · db {self.b.db}" if self.b.kind == "redis" else ""))
            self._dbs = dbs
            if dbs:
                self._setting_db = True
                self.db_pick.set_model(Gtk.StringList.new(dbs))
                if str(self.b.db) in dbs:
                    self.db_pick.set_selected(dbs.index(str(self.b.db)))
                self._setting_db = False
            self.db_pick.set_visible(len(dbs) > 1)
            self.nodes = nodes
            self._fill_tree()
            n = sum(1 for _ in nodes)
            more = getattr(self.b, "_more", False)
            self.tree_note.set_text("Showing the first 1,000 keys — narrow the pattern to see others." if more else
                                    "" if n else "Empty: nothing to browse yet.")
            self.data.message("Pick something on the left", "Its rows appear here. The Query tab runs your own "
                              "statements.", "view-grid-symbolic")
            if self.node:
                self._open(self.node)

        def fail(e):
            if g != self.gen:
                return
            self.header.set_subtitle("Not connected")
            self.data.error(e)
            self.tree_note.set_text("")
        bg(work, done=done, error=fail)

    def _db_changed(self, *_):
        if getattr(self, "_setting_db", False):
            return
        i = self.db_pick.get_selected()
        dbs = getattr(self, "_dbs", [])
        if 0 <= i < len(dbs) and dbs[i] != str(self.b.db):
            self.b.db = dbs[i]
            self.node = None
            self.reload()

    def _toggle_write(self, sw, _p):
        self.allow = sw.get_active()
        if self.allow:
            self.wbox.add_css_class("on")
            self.win.toast("Changes allowed on this database until you leave this page")
        else:
            self.wbox.remove_css_class("on")

    # ── tree ──
    def _fill_tree(self):
        q = self.find.get_text().strip().lower() if self.b.kind != "redis" else ""
        items = []
        for n in getattr(self, "nodes", []):
            if not q:
                items.append(_Obj(n))
                continue
            kids = self.b.children(n) if not n.leaf else []
            if not n.leaf:
                items += [_Obj(k) for k in kids if q in k.label.lower() or q in n.label.lower()]
            elif q in n.label.lower():
                items.append(_Obj(n))
        self.root.splice(0, self.root.get_n_items(), items)
        if len(items) == 1 and not items[0].v.leaf:
            self.tree_model.get_row(0).set_expanded(True)
        elif not q:
            for i in range(self.tree_model.get_n_items()):
                r = self.tree_model.get_row(i)
                if r and not r.get_item().v.leaf and r.get_item().v.label in ("public", self.b.db):
                    r.set_expanded(True)
                    break

    def _children(self, item):
        n = item.v
        if n.leaf:
            return None
        st = Gio.ListStore(item_type=_Obj)
        for k in self.b.children(n):
            st.append(_Obj(k))
        return st

    def _tree_setup(self, _f, li):
        ex = Gtk.TreeExpander()
        box = Gtk.Box(spacing=8)
        box.append(Gtk.Image(icon_name="view-grid-symbolic"))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        col.append(label("", css=[]))
        col.append(label("", css=["caption", "dim-label"]))
        box.append(col)
        ex.set_child(box)
        li.set_child(ex)

    def _tree_bind(self, _f, li):
        row = li.get_item()
        ex = li.get_child()
        ex.set_list_row(row)
        n = row.get_item().v
        box = ex.get_child()
        box.get_first_child().set_from_icon_name(n.icon or ("folder-symbolic" if not n.leaf else "view-grid-symbolic"))
        col = box.get_last_child()
        col.get_first_child().set_text(n.label)
        col.get_first_child().set_tooltip_text(n.label)
        col.get_last_child().set_text(n.detail or "")
        col.get_last_child().set_visible(bool(n.detail))

    def _picked(self, *_):
        item = self.sel.get_selected_item()
        if item is None:
            return
        n = item.get_item().v
        if n.leaf:
            self.offset = 0
            self._open(n)
        else:
            item.set_expanded(not item.get_expanded())

    def _pattern(self, text):
        self.b.pattern = text.strip() or "*"
        self.node = None
        self.reload()

    # ── data / structure ──
    def _open(self, node):
        self.node = node
        self.node_lbl.set_text(node.label)
        self._load_data()
        self._struct_for = None
        if self.views.get_visible_child_name() == "structure":
            self._load_struct()
        if self.views.get_visible_child_name() == "query" and self.b.language == "sql":
            pass

    def _load_data(self):
        node, off = self.node, self.offset
        self.gen += 1
        g = self.gen
        self.data.loading()

        def done(r):
            if g != self.gen:
                return
            self.data.show(r)
            lim = db.ROW_LIMIT
            shown = len(r.rows)
            self.range.set_text(f"rows {off + 1:,}–{off + shown:,}" if shown else "")
            self.prev.set_sensitive(off > 0)
            self.next.set_sensitive(r.truncated or shown >= lim)

        def fail(e):
            if g == self.gen:
                self.data.error(e)
        bg(self.b.browse, node, off, db.ROW_LIMIT, done=done, error=fail)

    def _page(self, d):
        if self.node:
            self.offset = max(0, self.offset + d * db.ROW_LIMIT)
            self._load_data()

    def _apply_filter(self):
        self.b.filter = self.mfilter.get_text().strip() or "{}"
        self.offset = 0
        if self.node:
            self._load_data()

    def _tab(self):
        if self.views.get_visible_child_name() == "structure" and self.node and self._struct_for is not self.node:
            self._load_struct()

    def _load_struct(self):
        node = self.node
        self._struct_for = node
        self.struct.loading()
        bg(self.b.structure, node, done=lambda r: self.node is node and self.struct.show(r),
           error=lambda e: self.node is node and self.struct.error(e))

    # ── query ──
    def _editor_key(self, _c, keyval, _code, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.CONTROL_MASK:
            self._run()
            return True
        return False

    def _run(self):
        buf = self.editor.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if not text:
            return
        if self.allow and self.b.is_destructive(text):
            confirm(self.win, "Run a statement that can delete data?",
                    f"<tt>{esc(text[:300])}</tt>\n\nIt runs on <b>{esc(self.b.name)}</b> and can't be undone.",
                    "Run it", lambda: self._go(text))
            return
        self._go(text)

    def _go(self, text):
        self.run.set_sensitive(False)
        self.out.loading()
        allow = self.allow

        def done(r):
            self.run.set_sensitive(True)
            self.out.show(r)
            self._remember(text)

        def fail(e):
            self.run.set_sensitive(True)
            self.out.error(e)
        bg(self.b.query, text, allow, done=done, error=fail)

    def _hist_key(self):
        return f"{self.b.kind}:{self.b.name}"

    def _remember(self, text):
        s = self.page.settings
        h = dict(s["db_history"] or {})
        lst = [t for t in h.get(self._hist_key(), []) if t != text]
        h[self._hist_key()] = [text] + lst[:19]
        s["db_history"] = h
        self._fill_history()

    def _fill_history(self):
        items = (self.page.settings["db_history"] or {}).get(self._hist_key(), [])
        m = Gio.Menu()
        for i, t in enumerate(items):
            m.append(t.replace("\n", " ")[:70], f"hist.use({i})")
        ag = Gio.SimpleActionGroup()
        a = Gio.SimpleAction.new("use", GLib.VariantType.new("i"))
        a.connect("activate", lambda _a, p: self.editor.get_buffer().set_text(items[p.get_int32()]))
        ag.add_action(a)
        self.insert_action_group("hist", ag)
        self.hist.set_menu_model(m)
        self.hist.set_sensitive(bool(items))

    def close(self):
        self.gen += 1
        bg(self.b.close, error=lambda e: None)
        if self.on_close:
            self.on_close()


# ── the sidebar page ────────────────────────────────────────────────────────────────────────────────────────────
class DatabasesPage(TablePage):
    title = "Databases"
    icon = "drive-multidisk-symbolic"
    empty_text = ("No databases found",
                  "Berth recognises PostgreSQL, MySQL / MariaDB, Redis / Valkey, MongoDB, Vespa, OpenSearch / "
                  "Elasticsearch, Typesense and Meilisearch containers. SQLite files can be opened from any container "
                  "or volume with “Open SQLite file”.")

    def __init__(self, win, store, settings):
        super().__init__(win, store, settings)
        self.wtitle.set_subtitle("Browse and query the databases in your containers — read-only unless you allow "
                                 "changes")
        store.connect("containers", lambda *_: self.on_show())

    def header_buttons(self):
        b = Gtk.Button(label="Open SQLite file…", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: SqliteDialog(self).present(self.win))
        return [b]

    def activatable(self):
        return True

    def build_columns(self):
        self.column("Container", lambda r: cname(r.data), expand=True, sort=lambda r: cname(r.data))
        self.column("Engine", lambda r: db.LABELS.get(r.extra.get("kind"), ""), width=190,
                    sort=lambda r: r.extra.get("kind"))
        self.column("Image", lambda r: r.data.get("Image", ""), css=["monospace", "dim-label"], width=220)
        self.column("Status", lambda r: r.data.get("Status", ""), width=170)

    def match(self, row, q):
        return q in cname(row.data).lower() or q in (row.data.get("Image") or "").lower() \
            or q in row.extra.get("kind", "")

    def on_show(self):
        rows = []
        for c in self.store.containers:
            kind = db.detect(c)
            if kind:
                r = Row("db", c["Id"], c)
                r.extra["kind"] = kind
                rows.append(r)
        rows.sort(key=lambda r: (state_of(r.data) != "running", cname(r.data)))
        self.set_rows(rows)
        n = len(rows)
        self.wtitle.set_subtitle(f"{n} database container{'s' if n != 1 else ''} · read-only unless you allow changes"
                                 if n else "Browse and query the databases in your containers")

    def on_activate(self, row):
        if row:
            self.open(row.key)

    def open(self, cid):
        """Open the explorer for a container (also used by the Containers page menu)."""
        def work():
            ins = self.store.engine.inspect(cid)
            return ins, db.open_backend(self.store.engine, ins)

        def done(r):
            ins, backend = r
            self._push(backend, (ins.get("Name") or "").lstrip("/"))
        bg(work, done=done, error=lambda e: self.win.toast(str(e)))

    def open_backend(self, backend, title, on_close=None):
        self._push(backend, title, on_close)

    def _push(self, backend, title, on_close=None):
        ex = Explorer(self, backend, title, on_close)
        page = Adw.NavigationPage(title=title, child=ex, tag=detail_tag("db"))
        page.connect("hidden", lambda *_: ex.close())
        push_detail(self.nav, page)


class SqliteDialog(Adw.Dialog):
    """Pick an SQLite file inside a container or a volume. Berth copies it (with -wal / -shm) and opens the copy
    read-only, so the original is never touched."""

    def __init__(self, page):
        super().__init__(title="Open an SQLite file", content_width=520, content_height=420)
        self.page, self.store, self.win = page, page.store, page.win
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.ok = Gtk.Button(label="Open")
        self.ok.add_css_class("suggested-action")
        self.ok.connect("clicked", lambda *_: self._open())
        hb.pack_end(self.ok)
        tv.add_top_bar(hb)
        pp = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(description="Berth reads a copy of the file (including recent writes in its WAL) and "
                                             "never changes the original.")
        self.kind = Adw.ComboRow(title="Where is it?", model=Gtk.StringList.new(["In a container", "In a volume"]))
        self.kind.connect("notify::selected", lambda *_: self._sources())
        g.add(self.kind)
        self.src = Adw.ComboRow(title="Container")
        g.add(self.src)
        self.path = Adw.EntryRow(title="Path to the file, e.g. /data/app.db")
        g.add(self.path)
        pp.add(g)
        tv.set_content(pp)
        self.set_child(tv)
        self._sources()

    def _sources(self):
        if self.kind.get_selected() == 0:
            self.items = [(cname(c), c["Id"]) for c in sorted(self.store.containers, key=cname)]
            self.src.set_title("Container")
            self.path.set_title("Path to the file, e.g. /data/app.db")
        else:
            self.items = [(v["Name"], v["Name"]) for v in sorted(self.store.volumes or [], key=lambda v: v["Name"])]
            self.src.set_title("Volume")
            self.path.set_title("Path inside the volume, e.g. app.db")
        self.src.set_model(Gtk.StringList.new([n for n, _ in self.items] or ["(none)"]))

    def _open(self):
        if not self.items:
            return
        name, ref = self.items[self.src.get_selected()]
        path = self.path.get_text().strip()
        if not path:
            self.win.toast("Type the path of the file")
            return
        volume = self.kind.get_selected() == 1
        self.ok.set_sensitive(False)
        eng = self.store.engine

        def work():
            helper = None
            if volume:
                from .files import _helper
                helper = _helper(eng, [f"{ref}:/volume:ro"])
                full = "/volume/" + path.lstrip("/")
            else:
                full = path if path.startswith("/") else "/" + path
            b = db.SQLite(eng, helper or ref, full, f"{name}:{path}")
            try:
                b.connect()
            except Exception:
                if helper:
                    from .files import stop_volume_helper
                    stop_volume_helper(eng, helper)
                b.close()
                raise
            return b, helper

        def done(r):
            b, helper = r
            self.close()

            def cleanup():
                if helper:
                    from .files import stop_volume_helper
                    bg(stop_volume_helper, eng, helper, error=lambda e: None)
            self.page.open_backend(b, b.name, cleanup)

        def fail(e):
            self.ok.set_sensitive(True)
            self.win.toast(str(e))
        bg(work, done=done, error=fail)
