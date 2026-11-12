"""Databases: find the database containers and explore them (tables / keys / collections / indices, data,
structure, a query console). All engine work goes through berth.db in the background; this file is only the UI."""
import json
import re
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from . import db, db_edit
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
    export actions. Like a SQL client: a row-number gutter, columns sized to their content, the last one filling
    the width, horizontal scrolling for wide tables, and a card that is only as tall as its rows.
    Clicking a row opens it in the panel on the right — the full values, editable when the explorer allows it."""

    def __init__(self, win, explorer=None, what="row", null_text="NULL"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.win, self.result, self.ex, self.what, self.null_text = win, None, explorer, what, null_text
        self.offset = 0
        self._quiet = False
        bar = Gtk.Box(spacing=8)
        self.status = label("", css=["caption", "dim-label"], hexpand=True, wrap=True)
        bar.append(self.status)
        self.text_save = Gtk.Button(label="Save", visible=False, tooltip_text="Save this document")
        self.text_save.add_css_class("suggested-action")
        self.text_save.connect("clicked", lambda *_: self.ex and self.ex.save_text(self._text_now()))
        bar.append(self.text_save)
        menu = Gio.Menu()
        for lbl, act in (("Copy as CSV", "csv"), ("Copy as Markdown", "md"), ("Copy as JSON", "json"),
                         ("Save as CSV…", "save-csv"), ("Save as JSON…", "save-json")):
            menu.append(lbl, f"grid.{act}")
        self.export = Gtk.MenuButton(icon_name="document-save-symbolic", menu_model=menu,
                                     tooltip_text="Copy or save these rows", sensitive=False)
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
        self.sel = Gtk.SingleSelection(model=self.model, autoselect=False, can_unselect=True)
        self.sel.connect("selection-changed", self._selected)
        self.view = Gtk.ColumnView(model=self.sel, show_row_separators=True, show_column_separators=True,
                                   reorderable=False, hexpand=True)
        for c in ("data-table", "berth-table", "db-grid"):
            self.view.add_css_class(c)
        self.view.connect("activate", lambda _v, pos: self.open(pos))
        self.view.get_sorter().connect("changed", self._sort_changed)
        sw = Gtk.ScrolledWindow(child=self.view, propagate_natural_height=True,
                                hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["table-card"], valign=Gtk.Align.START,
                       overflow=Gtk.Overflow.HIDDEN)
        card.append(sw)                                  # as tall as its rows; scrolls when there are more
        self.stack.add_named(card, "grid")
        self.text = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 top_margin=12, bottom_margin=12, left_margin=14, right_margin=14)
        self.text.get_buffer().connect("changed", lambda *_: self._text_changed())
        self.stack.add_named(table_card(Gtk.ScrolledWindow(child=self.text)), "text")
        self.msg = Adw.StatusPage(icon_name="view-grid-symbolic", title="", description="")
        self.msg.add_css_class("compact")
        self.stack.add_named(self.msg, "msg")
        spin = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=32, height_request=32)
        self.stack.add_named(spin, "loading")

        self.panel = RecordPanel(self)
        self.split = Adw.OverlaySplitView(sidebar_position=Gtk.PackType.END, show_sidebar=False, vexpand=True,
                                          min_sidebar_width=340, max_sidebar_width=620, sidebar_width_fraction=0.4,
                                          enable_show_gesture=False, css_classes=["db-split"])
        self.split.set_content(self.stack)
        self.split.set_sidebar(self.panel)
        self.split.connect("notify::show-sidebar", lambda *_: self.split.get_show_sidebar() or self._unselect())
        self.append(self.split)
        self.message("Nothing to show yet", "", "view-grid-symbolic")

    # ── states ──
    def message(self, title, body, icon="dialog-information-symbolic"):
        self.result = None
        self.export.set_sensitive(False)
        self.text_save.set_visible(False)
        self.msg.set_title(title)
        self.msg.set_description(body)
        self.msg.set_icon_name(icon)
        self.status.set_text("")
        self.stack.set_visible_child_name("msg")
        self.split.set_show_sidebar(False)

    def error(self, text):
        t = str(text)
        if "read-only mode" in t:
            self.message("Blocked: read-only mode", esc(t), "changes-prevent-symbolic")
        else:
            self.message("That didn't work", esc(t), "dialog-warning-symbolic")

    def loading(self):
        self.stack.set_visible_child_name("loading")

    def show(self, r, offset=0, keep=None):
        """keep: the row position to reopen in the panel (after a save), if it is still there."""
        self.result, self.offset = r, offset
        self.export.set_sensitive(bool(r.rows) or r.text is not None)
        parts = []
        if r.columns:
            parts.append(f"{len(r.rows):,} {self.what}{'s' if len(r.rows) != 1 else ''}"
                         + (" (more available)" if r.truncated else ""))
        if r.ms:
            parts.append(f"{r.ms} ms")
        if r.note:
            parts.append(r.note)
        self.status.set_text("  ·  ".join(parts))
        self.text_save.set_visible(False)
        if r.text is not None and not r.columns:
            self._text_orig = r.text
            self.text.get_buffer().set_text(r.text)
            self.sync_text_edit()
            self.stack.set_visible_child_name("text")
            self.split.set_show_sidebar(False)
            return
        if not r.columns:
            self.message("Done", esc(r.note or "The statement ran; it returned no rows."), "emblem-ok-symbolic")
            self.status.set_text(f"{r.ms} ms" if r.ms else "")
            return
        self._quiet = True
        for c in list(self.view.get_columns()):
            self.view.remove_column(c)
        self.view.append_column(self._gutter())
        widths = self._widths(r)
        sortable = bool(self.ex and self.ex.can_sort())
        cur = self.ex.b.sort if sortable else None
        for i, name in enumerate(r.columns):
            col = self._column(name, i)
            col.set_fixed_width(widths[i])
            col.set_expand(i == len(r.columns) - 1)      # the last column takes what is left: no empty strip
            if sortable:                                 # a click on the header sorts the whole table (server side)
                col.set_sorter(Gtk.CustomSorter.new(lambda *_a: 0, None))
            self.view.append_column(col)
            if cur and cur[0] == name:
                self.view.sort_by_column(col, Gtk.SortType.DESCENDING if cur[1] else Gtk.SortType.ASCENDING)
        self._quiet = False
        self._quiet = True
        self.model.splice(0, self.model.get_n_items(), [_Obj(row) for row in r.rows])
        self.sel.set_selected(Gtk.INVALID_LIST_POSITION)
        self._quiet = False
        if not r.rows:
            self.message("No rows", "The table is empty." + (" Add one with “Add row”." if self.ex and
                                                             self.ex.can_edit() else ""), "view-grid-symbolic")
            self.result = r
            return
        self.stack.set_visible_child_name("grid")
        if keep is not None and keep < len(r.rows):
            self.open(keep)
        elif self.split.get_show_sidebar() and not self.panel.is_new:
            self.split.set_show_sidebar(False)

    def _widths(self, r):
        """Width per column from its name and a sample of values (like a SQL client), within sensible bounds."""
        lay = self.view.create_pango_layout("")
        out = []
        for i, name in enumerate(r.columns):
            best = 0
            for t in [name + "    "] + [("NULL" if row[i] is None else str(row[i]).split("\n", 1)[0])[:48]
                                        for row in r.rows[:80] if i < len(row)]:
                lay.set_text(t, -1)
                best = max(best, lay.get_pixel_size()[0])
            out.append(max(84, min(best + 34, 420)))
        return out

    def _gutter(self):
        def setup(_f, li):
            li.set_child(Gtk.Label(xalign=1, css_classes=["db-rownum", "numeric", "dim-label"]))

        def bind(_f, li):
            li.get_child().set_text(str(self.offset + li.get_position() + 1))
        f = Gtk.SignalListItemFactory()
        f.connect("setup", setup)
        f.connect("bind", bind)
        col = Gtk.ColumnViewColumn(title="#", factory=f, resizable=False)
        n = len(str(self.offset + max(len(self.result.rows) if self.result else 1, 1)))
        col.set_fixed_width(26 + 9 * n)
        return col

    def _column(self, name, i):
        def setup(_f, li):
            li.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, single_line_mode=True))

        def bind(_f, li):
            v = li.get_item().v[i] if i < len(li.get_item().v) else None
            lb = li.get_child()
            if v is None:
                lb.set_text(self.null_text)
                lb.add_css_class("db-null")
            else:
                lb.set_text(str(v).replace("\n", " ⏎ ")[:400])
                lb.remove_css_class("db-null")
        f = Gtk.SignalListItemFactory()
        f.connect("setup", setup)
        f.connect("bind", bind)
        return Gtk.ColumnViewColumn(title=name, factory=f, resizable=True)

    def _sort_changed(self, sorter, _change):
        if self._quiet or not (self.ex and self.ex.can_sort()):
            return
        col = sorter.get_primary_sort_column()
        if col is None:
            self.ex.sort_by(None, False)
        else:
            self.ex.sort_by(col.get_title(), sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING)

    # ── the row panel ──
    def _selected(self, *_):
        if self._quiet:
            return
        pos = self.sel.get_selected()
        if pos != Gtk.INVALID_LIST_POSITION:
            self.open(pos)

    def _unselect(self):
        self._quiet = True
        self.sel.set_selected(Gtk.INVALID_LIST_POSITION)
        self._quiet = False

    def open(self, pos):
        if not self.result or not (0 <= pos < len(self.result.rows)):
            return
        if self.panel.dirty() and pos != self.panel.pos:
            back = self.panel.pos
            self._quiet = True
            self.sel.set_selected(back if back is not None else Gtk.INVALID_LIST_POSITION)
            self._quiet = False
            confirm(self.win, "Discard your changes?", "The row you were editing has changes that aren't saved.",
                    "Discard", lambda: (self.panel.reset(), self.open(pos)))
            return
        self._quiet = True
        self.sel.set_selected(pos)
        self._quiet = False
        self.split.set_show_sidebar(True)
        self.panel.show_row(pos)

    def open_new(self):
        if self.panel.dirty():
            confirm(self.win, "Discard your changes?", "The row you were editing has changes that aren't saved.",
                    "Discard", lambda: (self.panel.reset(), self.open_new()))
            return
        self._unselect()
        self.split.set_show_sidebar(True)
        self.panel.show_new()

    def step(self, d):
        if self.panel.pos is not None and self.result:
            p = self.panel.pos + d
            if 0 <= p < len(self.result.rows):
                self.open(p)

    def selected(self):
        p = self.sel.get_selected()
        return None if p == Gtk.INVALID_LIST_POSITION else p

    # ── an editable text document (Redis JSON) ──
    def sync_text_edit(self):
        ok = bool(self.ex and self.result is not None and getattr(self.result, "text_editable", False))
        self.text.set_editable(ok and self.ex.allow)
        self._text_changed()

    def _text_now(self):
        b = self.text.get_buffer()
        return b.get_text(b.get_start_iter(), b.get_end_iter(), False)

    def _text_changed(self):
        ok = bool(self.ex and self.result is not None and getattr(self.result, "text_editable", False)
                  and self.ex.allow)
        self.text_save.set_visible(ok and self._text_now() != getattr(self, "_text_orig", None))

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


def _default_hint(d):
    """A column default in plain words: sequences become "numbered automatically", casts are dropped."""
    d = d.strip()
    if d.startswith("nextval(") or "auto_increment" in d or d.startswith("generated"):
        return "Leave empty to number it automatically"
    if d.lower() in ("now()", "current_timestamp", "current_timestamp()", "clock_timestamp()"):
        return "Leave empty for the current time"
    d = re.sub(r"::[\w ]+(\[\])?(\))?$", lambda m: m.group(2) or "", d)
    return f"Leave empty for the default: {d}"


def _hex_preview(h, n=48):
    b = h[:n * 2]
    return " ".join(b[i:i + 2] for i in range(0, len(b), 2)) + (" …" if len(h) > n * 2 else "")


class RecordPanel(Gtk.Box):
    """One row in full on the right of the grid: every value untruncated, with its type; editable when the
    explorer allows changes and the row can be picked out by its key. Also used to add a new row."""

    def __init__(self, grid):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["db-record"])
        self.grid, self.win = grid, grid.win
        self.pos, self.ident, self.is_new, self.fields, self.widgets, self.gen = None, None, False, [], [], 0
        head = Gtk.Box(spacing=4, css_classes=["db-record-head"])
        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = label("", css=["heading"])
        self.sub = label("", css=["caption", "dim-label"], wrap=True)
        tbox.append(self.title)
        tbox.append(self.sub)
        head.append(tbox)
        self.up = icon_button("go-up-symbolic", "Previous row (Alt+↑)", lambda *_: grid.step(-1))
        self.down = icon_button("go-down-symbolic", "Next row (Alt+↓)", lambda *_: grid.step(1))
        self.cp = icon_button("edit-copy-symbolic", "Copy this row as JSON", lambda *_: self._copy_json())
        for w in (self.up, self.down, self.cp, icon_button("window-close-symbolic", "Close (Esc)",
                                                            lambda *_: self.close())):
            head.append(w)
        self.append(head)
        self.stack = Gtk.Stack(vexpand=True)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, css_classes=["db-record-body"])
        self.stack.add_named(Gtk.ScrolledWindow(child=self.body, hscrollbar_policy=Gtk.PolicyType.NEVER), "fields")
        self.stack.add_named(Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=28,
                                         height_request=28), "loading")
        self.err = Adw.StatusPage(icon_name="dialog-warning-symbolic", css_classes=["compact"])
        self.stack.add_named(self.err, "error")
        self.append(self.stack)
        foot = self.foot = Gtk.Box(spacing=8, css_classes=["db-record-foot"])
        self.delete = Gtk.Button(label="Delete", tooltip_text="Delete this row from the database")
        self.delete.add_css_class("destructive-action")
        self.delete.add_css_class("flat")
        self.delete.connect("clicked", lambda *_: self._delete())
        foot.append(self.delete)
        self.note = label("", css=["caption", "dim-label"], wrap=True, hexpand=True, xalign=1)
        foot.append(self.note)
        self.allow_btn = Gtk.Button(label="Allow changes", tooltip_text="Switch on “Allow changes” for this database "
                                    "(until you leave the page)")
        self.allow_btn.connect("clicked", lambda *_: self.ex() and self.ex().write_sw.set_active(True))
        foot.append(self.allow_btn)
        self.revert = Gtk.Button(label="Revert", tooltip_text="Undo your edits")
        self.revert.connect("clicked", lambda *_: self.reset())
        foot.append(self.revert)
        self.save = Gtk.Button(label="Save", tooltip_text="Write the changes to the database (Ctrl+S)")
        self.save.add_css_class("suggested-action")
        self.save.connect("clicked", lambda *_: self._save())
        foot.append(self.save)
        self.append(foot)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

    def ex(self):
        return self.grid.ex

    def _key(self, _c, kv, _code, state):
        ctrl, alt = state & Gdk.ModifierType.CONTROL_MASK, state & Gdk.ModifierType.ALT_MASK
        if kv == Gdk.KEY_Escape:
            self.close()
            return True
        if ctrl and kv in (Gdk.KEY_s, Gdk.KEY_S) and self.save.get_sensitive() and self.save.get_visible():
            self._save()
            return True
        if alt and kv in (Gdk.KEY_Up, Gdk.KEY_Down):
            self.grid.step(-1 if kv == Gdk.KEY_Up else 1)
            return True
        return False

    def close(self):
        if self.dirty():
            confirm(self.win, "Discard your changes?", "The row has changes that aren't saved.", "Discard",
                    lambda: (self.reset(), self.grid.split.set_show_sidebar(False)))
            return
        self.grid.split.set_show_sidebar(False)

    # ── what can be done ──
    def editable(self):
        ex, r = self.ex(), self.grid.result
        return bool(ex and r is not None and ex.can_edit() and getattr(r, "edit_why", "x") is None)

    def _what(self):
        ex = self.ex()
        return ex.ed.unit if ex else self.grid.what

    # ── showing ──
    def show_row(self, pos):
        self.gen += 1
        g = self.gen
        self.pos, self.is_new = pos, False
        r = self.grid.result
        n = len(r.rows)
        self.title.set_text(f"{self._what().capitalize()} {self.grid.offset + pos + 1:,}")
        self.up.set_sensitive(pos > 0)
        self.down.set_sensitive(pos < n - 1)
        self.up.set_visible(True)
        self.down.set_visible(True)
        ex = self.ex()
        if ex and getattr(r, "edit_why", "x") is None and ex.ed is not None:
            self.ident = ex.ed.ident(ex.node, r, pos)
            keys = ", ".join(self.ident) if self.ident else ""
            self.sub.set_text(f"{ex.node.label}" + (f" · picked out by {keys}" if keys else ""))
            self.stack.set_visible_child_name("loading")
            node, ident = ex.node, self.ident
            bg(ex.ed.fetch, node, ident, done=lambda f: g == self.gen and self._fill(f),
               error=lambda e: g == self.gen and self._error(e))
            return
        # not tied to a table row (query results, structure, tables without a key): the values as listed
        self.ident = None
        why = getattr(r, "edit_why", None)
        self.sub.set_text(why or ("Values as returned by the query" if self.grid.what == "row" and not ex else
                                  "Values as listed"))
        types = getattr(r, "types", {}) or {}
        from .db_edit import Field
        fields = [Field(c, types.get(c, ""), v, readonly="x", multiline=True) for c, v in zip(r.columns, r.rows[pos])]
        self._fill(fields, listed=True)

    def show_new(self):
        self.gen += 1
        g = self.gen
        ex = self.ex()
        self.pos, self.ident, self.is_new = None, None, True
        self.title.set_text(f"New {self._what()}")
        self.sub.set_text(f"In {ex.node.label}. Empty fields get their default.")
        self.up.set_visible(False)
        self.down.set_visible(False)
        self.stack.set_visible_child_name("loading")
        bg(ex.ed.blank, ex.node, done=lambda f: g == self.gen and self._fill(f),
           error=lambda e: g == self.gen and self._error(e))

    def _error(self, e):
        self.err.set_title("Couldn't open it")
        self.err.set_description(esc(str(e)))
        self.stack.set_visible_child_name("error")
        self.fields, self.widgets = [], []
        self.sync()

    def _fill(self, fields, listed=False):
        self.fields, self.widgets, self.listed = fields, [], listed
        while (c := self.body.get_first_child()) is not None:
            self.body.remove(c)
        for f in fields:
            self.body.append(self._block(f))
        self.stack.set_visible_child_name("fields")
        self.sync()
        first = next((w for f, w in zip(self.fields, self.widgets) if w["view"] and not f.readonly), None)
        if self.is_new and first:
            first["view"].grab_focus()

    def _block(self, f):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["db-field"])
        top = Gtk.Box(spacing=8)
        name = label(f.name, css=["db-field-name"], ellipsize=Pango.EllipsizeMode.MIDDLE)
        name.set_tooltip_text(f.name)
        top.append(name)
        if f.pk:
            top.append(label("key", css=["db-pill"]))
        if f.type:
            top.append(label(f.type, css=["caption", "dim-label", "monospace"], ellipsize=Pango.EllipsizeMode.END,
                             hexpand=True, xalign=0))
        else:
            top.append(Gtk.Box(hexpand=True))
        w = {"view": None, "null": None, "orig": f.value, "touched": False}
        if f.nullable and not f.binary and f.readonly != "x" and not f.readonly:
            nb = Gtk.CheckButton(label="NULL", tooltip_text="Store no value (NULL) in this column")
            nb.add_css_class("caption")
            nb.set_active(f.value is None and not self.is_new)
            nb.connect("toggled", lambda *_: self._null_toggled(w))
            top.append(nb)
            w["null"] = nb
        top.append(icon_button("edit-copy-symbolic", f"Copy {f.name}", lambda *_: self._copy_value(f, w)))
        box.append(top)
        if f.binary:
            size, head = f.binary
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["db-value", "binary"])
            card.append(label(f"Binary · {db._size(size)}", css=["heading"]))
            card.append(label(_hex_preview(head), css=["caption", "monospace", "dim-label"], wrap=True,
                              selectable=True))
            sv = Gtk.Button(label="Save to a file…", halign=Gtk.Align.START,
                            tooltip_text="Write the whole value to a file on this computer")
            sv.connect("clicked", lambda *_: self.ex() and self.ex().save_blob(self.ident, f.name))
            if self.ident is not None and hasattr(self.ex().ed, "blob"):
                card.append(sv)
            box.append(card)
        else:
            text = "" if f.value is None else f.value
            if f.json and text:
                try:
                    text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
                    w["orig"] = text
                except ValueError:
                    pass
            tv = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8, left_margin=10,
                              right_margin=10, accepts_tab=False, css_classes=["db-value"])
            if f.json or f.multiline and ("\n" in text or len(text) > 80):
                tv.set_monospace(True)
            tv.get_buffer().set_text(text)
            if f.value is None and not self.is_new:
                tv.add_css_class("db-null")
            tv.get_buffer().connect("changed", lambda *_: self._touched(w))
            big = f.json or len(text) > 240 or "\n" in text
            sw = Gtk.ScrolledWindow(child=tv, propagate_natural_height=True, max_content_height=420,
                                    hscrollbar_policy=Gtk.PolicyType.NEVER, css_classes=["db-value-frame"],
                                    vscrollbar_policy=Gtk.PolicyType.AUTOMATIC if big else Gtk.PolicyType.NEVER)
            box.append(sw)
            w["view"] = tv
            hint = ""
            if self.is_new and f.default:
                hint = _default_hint(f.default)
            elif self.is_new and not f.nullable and not f.default:
                hint = "Required"
            if hint:
                box.append(label(hint, css=["caption", "dim-label"], wrap=True))
        if f.readonly and f.readonly != "x":
            box.append(label(f.readonly, css=["caption", "dim-label"], wrap=True))
        self.widgets.append(w)
        return box

    def _null_toggled(self, w):
        w["touched"] = True
        on = w["null"].get_active()
        if w["view"]:
            w["view"].set_sensitive(not on)
            (w["view"].add_css_class if on else w["view"].remove_css_class)("db-null")
        self.sync()

    def _touched(self, w):
        w["touched"] = True
        if w["null"] and w["null"].get_active() and w["view"].get_buffer().get_char_count():
            w["null"].set_active(False)
        self.sync()

    @staticmethod
    def _text(w):
        b = w["view"].get_buffer()
        return b.get_text(b.get_start_iter(), b.get_end_iter(), False)

    def _value(self, f, w):
        if w["null"] and w["null"].get_active():
            return None
        return self._text(w) if w["view"] else f.value

    def values(self):
        """What to write: for a new row, every field the user touched (the rest get their default);
        for an existing row, only the fields that changed."""
        from .db_edit import DEFAULT
        out = {}
        for f, w in zip(self.fields, self.widgets):
            if f.readonly or f.binary:
                continue
            v = self._value(f, w)
            if self.is_new:
                if not w["touched"] or (v == "" and f.default):
                    out[f.name] = DEFAULT
                else:
                    out[f.name] = v
            elif w["touched"] and (v != w["orig"] or (v is None) != (f.value is None)):
                out[f.name] = v
        return out

    def dirty(self):
        if not self.fields or getattr(self, "listed", False):
            return False
        if self.is_new:
            return any(w["touched"] for w in self.widgets)
        return bool(self.values())

    def reset(self):
        if self.is_new:
            self.grid.split.set_show_sidebar(False)
            self.fields, self.widgets = [], []
            return
        for f, w in zip(self.fields, self.widgets):
            if w["view"]:
                w["view"].get_buffer().set_text("" if w["orig"] is None else w["orig"])
            if w["null"]:
                w["null"].set_active(f.value is None)
            w["touched"] = False
        self.sync()

    def sync(self):
        """Buttons and editability for the current state (also called when “Allow changes” flips)."""
        ex = self.ex()
        can = self.editable() and not getattr(self, "listed", False) and bool(self.fields)
        allow = bool(ex and ex.allow)
        for f, w in zip(self.fields, self.widgets):
            if w["view"]:
                w["view"].set_editable(can and allow and not f.readonly)
                w["view"].set_cursor_visible(can and allow and not f.readonly)
            if w["null"]:
                w["null"].set_sensitive(can and allow)
        dirty = self.dirty() if can else False
        self.save.set_visible(can and allow)
        self.save.set_sensitive(dirty or (self.is_new and can))
        self.save.set_label("Add" if self.is_new else "Save")
        self.revert.set_visible(can and allow and dirty and not self.is_new)
        self.delete.set_visible(can and allow and not self.is_new and ex.ed.can_delete)
        self.delete.set_label(f"Delete {self._what()}")
        self.allow_btn.set_visible(can and not allow and bool(ex and ex.b.write_capable))
        if not can:
            r = self.grid.result
            self.note.set_text("" if getattr(self, "listed", False) else
                               (getattr(r, "edit_why", "") or "") if r is not None else "")
        elif not allow:
            self.note.set_text("Read-only")
        else:
            self.note.set_text("Unsaved changes" if dirty and not self.is_new else "")
        self.foot.set_visible(bool(self.note.get_text()) or any(
            b.get_visible() for b in (self.save, self.revert, self.delete, self.allow_btn)))

    # ── actions ──
    def _save(self):
        ex = self.ex()
        values = self.values()
        if not self.is_new and not values:
            return
        self.save.set_sensitive(False)
        pos, new = self.pos, self.is_new
        node, ident = ex.node, self.ident

        def done(msg):
            self.win.toast(msg)
            for w in self.widgets:
                w["touched"] = False
            self.fields = [] if new else self.fields
            ex.reload_data(keep=None if new else pos)
            if new:
                self.grid.split.set_show_sidebar(False)

        def fail(e):
            self.sync()
            self.note.set_text("")
            show_error(self.win, "Not saved", str(e))
        bg(ex.ed.save, node, None if new else ident, values, ex.allow, done=done, error=fail)

    def _delete(self):
        ex = self.ex()
        node, ident, what = ex.node, self.ident, self._what()
        keys = ", ".join(f"{k} = {v}" for k, v in (ident or {}).items())

        def go():
            def done(msg):
                self.win.toast(msg)
                self.fields, self.widgets = [], []
                self.grid.split.set_show_sidebar(False)
                ex.reload_data()
            bg(ex.ed.delete, node, ident, ex.allow, done=done,
               error=lambda e: show_error(self.win, "Not deleted", str(e)))
        confirm(self.win, f"Delete this {what}?", f"<tt>{esc(keys[:300])}</tt> in <b>{esc(node.label)}</b>.\n\n"
                "This can't be undone.", "Delete", go)

    def _copy_value(self, f, w):
        v = self._value(f, w) if w["view"] else None
        self.get_clipboard().set("" if v is None else v)
        self.win.toast(f"Copied {f.name}")

    def _copy_json(self):
        data = {f.name: (None if f.binary else self._value(f, w)) for f, w in zip(self.fields, self.widgets)}
        self.get_clipboard().set(json.dumps(data, indent=2, ensure_ascii=False))
        self.win.toast("Copied as JSON")


def show_error(win, title, text):
    d = Adw.AlertDialog(heading=title, body=text[:1500])
    d.add_response("ok", "OK")
    d.present(win)


# ── explorer (one database container) ───────────────────────────────────────────────────────────────────────────
class Explorer(Gtk.Box):
    def __init__(self, page, backend, title, on_close=None, embedded=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.page, self.win, self.b = page, page.win, backend
        self.on_close = on_close
        self.node, self.offset, self.gen, self.allow = None, 0, 0, False
        self._picked_db = False
        self._struct_for = None
        self.ed = db_edit.editor_for(backend)
        self.embedded = embedded
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
        if backend.kind != "sqlite":
            menu = Gio.Menu()
            if backend.kind == "postgres":
                menu.append("Rename this database…", "dbx.renamedb")
            sec = Gio.Menu()
            sec.append("Rename the container…", "dbx.renamectr")
            sec.append("Change the container's ports…", "dbx.ports")
            menu.append_section(None, sec)
            mb = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="More",
                                valign=Gtk.Align.CENTER)
            mb.add_css_class("flat")
            self.header.add(mb)
            from . import quick
            ag = Gio.SimpleActionGroup()
            for n, fn in (("renamedb", self._rename_db),
                          ("renamectr", lambda: quick.rename_container(self.win, page.store, backend.cid)),
                          ("ports", lambda: quick.PortsDialog(self.win, page.store, backend.cid).present(self.win))):
                a = Gio.SimpleAction.new(n, None)
                a.connect("activate", lambda *_a, f=fn: f())
                ag.add_action(a)
            self.insert_action_group("dbx", ag)
        if embedded:                                   # inside a container's Database tab: no second header bar
            tv.add_top_bar(self.header)
        else:
            flat_page(tv, self.header)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=300, shrink_start_child=False,
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
        if backend.kind == "redis":
            nk = Gtk.Button(label="New key…", tooltip_text="Create a key (needs “Allow changes”)")
            nk.connect("clicked", lambda *_: self._new_key())
            left.append(nk)
            self.newkey = nk
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
        top = Gtk.Box(spacing=8, css_classes=["db-data-bar"])
        self.node_lbl = label("Pick a table on the left", css=["heading"])
        top.append(self.node_lbl)
        self.ren = Gtk.Button(label="Rename…", visible=False, valign=Gtk.Align.CENTER)
        self.ren.add_css_class("flat")
        self.ren.connect("clicked", lambda *_: self._rename_node())
        top.append(self.ren)
        if backend.kind in ("postgres", "mysql"):
            self.wfilter = Gtk.Entry(placeholder_text="Filter rows, e.g. status = 'paid' and total > 10",
                                     width_chars=36, primary_icon_name="system-search-symbolic",
                                     secondary_icon_tooltip_text="Clear the filter",
                                     tooltip_text="An SQL condition (what follows WHERE). Press Enter to apply; it "
                                                  "searches the whole table, not just this page.")
            self.wfilter.add_css_class("monospace")
            self.wfilter.connect("activate", lambda *_: self._apply_where())
            self.wfilter.connect("changed", lambda e: e.set_icon_from_icon_name(
                Gtk.EntryIconPosition.SECONDARY, "edit-clear-symbolic" if e.get_text() else None))
            self.wfilter.connect("icon-release", lambda e, pos: pos == Gtk.EntryIconPosition.SECONDARY and (
                e.set_text(""), self._apply_where()))
            top.append(self.wfilter)
        if backend.kind == "mongo":
            self.mfilter = Gtk.Entry(placeholder_text='Filter, e.g. {"status": "active"}', width_chars=34)
            self.mfilter.add_css_class("monospace")
            self.mfilter.connect("activate", lambda *_: self._apply_filter())
            top.append(self.mfilter)
        top.append(Gtk.Box(hexpand=True))
        self.add_btn = Gtk.Button(visible=False, valign=Gtk.Align.CENTER)
        self.add_btn.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Add row"))
        self.add_btn.connect("clicked", lambda *_: self.data.open_new())
        top.append(self.add_btn)
        self.node_menu = Gio.Menu()
        self.node_mb = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=self.node_menu, visible=False,
                                      tooltip_text="More for this table", valign=Gtk.Align.CENTER)
        self.node_mb.add_css_class("flat")
        top.append(self.node_mb)
        nag = Gio.SimpleActionGroup()
        for n, fn in (("copyname", lambda: (self.get_clipboard().set(self.node.label), self.win.toast("Copied"))),
                      ("empty", lambda: self._node_act("empty")), ("drop", lambda: self._node_act("drop")),
                      ("expire", self._expire), ("structure", lambda: self.views.set_visible_child_name("structure"))):
            a = Gio.SimpleAction.new(n, None)
            a.connect("activate", lambda *_a, f=fn: f())
            nag.add_action(a)
        self.insert_action_group("node", nag)
        self.node_ag = nag
        self.prev = icon_button("go-previous-symbolic", "Previous page", lambda *_: self._page(-1))
        self.next = icon_button("go-next-symbolic", "Next page", lambda *_: self._page(1))
        self.range = label("", css=["caption", "dim-label", "numeric"], valign=Gtk.Align.CENTER)
        for w in (self.prev, self.range, self.next):
            w.set_visible(False)
            top.append(w)
        data.append(top)
        self.data = ResultGrid(self.win, explorer=self, what=self.ed.unit)
        data.append(self.data)
        self.views.add_titled_with_icon(data, "data", "Data", "view-grid-symbolic")

        self.struct = ResultGrid(self.win, what="column", null_text="—")
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
            nodes = self.b.tree()
            moved = None
            # The default database is often empty while the data lives in another one (one database per service):
            # open the one that holds data, unless the user picked a database themselves.
            if dbs and len(dbs) > 1 and not self._picked_db and not self._has_leaves(nodes):
                best = self.b.richest(dbs)
                if best and str(best) != str(self.b.db):
                    moved = self.b.db
                    self.b.db = best
                    nodes = self.b.tree()
            return desc, dbs, nodes, moved

        def done(r):
            if g != self.gen:
                return
            desc, dbs, nodes, moved = r
            if moved is not None:
                self.win.toast(f"“{moved}” is empty — showing “{self.b.db}”, which has data. Switch databases at "
                               "the top right.")
            self.desc = desc
            if self.embedded:
                self.header.set_title(desc)
            where = (f"database {self.b.db}" if self.b.db and self.b.kind != "redis" else
                     f"db {self.b.db}" if self.b.kind == "redis" else "")
            self.header.set_subtitle(where if self.embedded else " · ".join(x for x in (desc, where) if x))
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
            else:
                self._open_first()

        def fail(e):
            if g != self.gen:
                return
            self.header.set_subtitle("Not connected")
            self.data.error(e)
            self.tree_note.set_text("")
        bg(work, done=done, error=fail)

    def _open_first(self):
        """Open the first table / key / collection straight away: one click less, and no empty page."""
        for i in range(self.tree_model.get_n_items()):
            row = self.tree_model.get_row(i)
            if row is not None and row.get_item().v.leaf:
                self.sel.set_selected(i)             # → _picked → _open
                return

    def _has_leaves(self, nodes):
        return any(n.leaf or self.b.children(n) for n in nodes)

    def _db_changed(self, *_):
        if getattr(self, "_setting_db", False):
            return
        i = self.db_pick.get_selected()
        dbs = getattr(self, "_dbs", [])
        if 0 <= i < len(dbs) and dbs[i] != str(self.b.db):
            self._picked_db = True
            self.b.db = dbs[i]
            self.node = None
            self.reload()

    def _sync_rename(self):
        n = self.node
        self.ren.set_visible(bool(n) and self.b.can_rename(n))
        self.ren.set_sensitive(self.allow)
        self.ren.set_tooltip_text("Rename" if self.allow else "Switch on “Allow changes” to rename")

    def _rename_node(self):
        from .quick import _ask
        n = self.node
        what = {"table": "table", "key": "key", "collection": "collection"}.get(n.kind, n.kind)

        def go(new):
            def done(_r):
                self.win.toast(f"Renamed to {new}")
                self.node = None
                self.reload()
            bg(self.b.rename, n, new, self.allow, done=done, error=lambda e: self.win.toast(f"Not renamed: {e}"))
        _ask(self.win, f"Rename {what}", "Applications that use the old name will stop finding it.", n.label,
             "Rename", go)

    def _rename_db(self):
        from .quick import _ask
        if not self.allow:
            self.win.toast("Switch on “Allow changes” first")
            return
        node = db.Node(str(self.b.db), "database", str(self.b.db))

        def go(new):
            bg(self.b.rename, node, new, True, done=lambda _r: (self.win.toast(f"Database renamed to {new}"),
                                                                 self.reload()),
               error=lambda e: self.win.toast(f"Not renamed: {e}"))
        _ask(self.win, "Rename database", "Postgres only allows this while nothing is connected to it: stop the "
             "services that use it first.", str(self.b.db), "Rename", go)

    def _toggle_write(self, sw, _p):
        self.allow = sw.get_active()
        self._sync_rename()
        self._sync_edit()
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
        if self.data.panel.dirty():
            confirm(self.win, "Discard your changes?", "The row you were editing has changes that aren't saved.",
                    "Discard", lambda: (self.data.panel.reset(), self._open(node)))
            return
        self.data.split.set_show_sidebar(False)
        if node is not self.node:                       # a new table starts unsorted and unfiltered
            self.b.sort, self.b.where = None, ""
            if hasattr(self, "wfilter"):
                self.wfilter.set_text("")
        self.node = node
        self.node_lbl.set_text(node.label)
        self.node_lbl.set_tooltip_text(node.label)
        for w in (self.prev, self.range, self.next):
            w.set_visible(True)
        self._sync_rename()
        self._load_data()
        self._struct_for = None
        if self.views.get_visible_child_name() == "structure":
            self._load_struct()
        if self.views.get_visible_child_name() == "query" and self.b.language == "sql":
            pass

    def _load_data(self, keep=None):
        node, off = self.node, self.offset
        self.gen += 1
        g = self.gen
        if keep is None:
            self.data.loading()

        def work():
            r = db._shrinking(self.b.browse, node, off, db.ROW_LIMIT)
            try:
                self.ed.annotate(node, r)
            except Exception as e:          # never let the edit check hide the data
                r.key, r.edit_why = [], f"Editing isn't available: {e}"
            return r

        def done(r):
            if g != self.gen:
                return
            self.data.what = self.ed.unit
            self.data.show(r, off, keep=keep)
            self._sync_edit()
            lim = getattr(r, "page", db.ROW_LIMIT)
            self.page_size = lim
            shown = len(r.rows)
            self.range.set_text(f"rows {off + 1:,}–{off + shown:,}" if shown else "")
            self.prev.set_sensitive(off > 0)
            self.next.set_sensitive(r.truncated or shown >= lim)

        def fail(e):
            if g == self.gen:
                self.data.error(e)
        bg(work, done=done, error=fail)

    # ── editing ──
    def can_edit(self):
        return bool(self.node) and self.b.write_capable and type(self.ed) is not db_edit.Editor

    def reload_data(self, keep=None):
        if self.node:
            self._struct_for = None
            self._load_data(keep=keep)

    def _sync_edit(self):
        r = self.data.result
        ok = self.can_edit() and r is not None and getattr(r, "edit_why", "x") is None
        unit = self.ed.unit
        self.add_btn.set_visible(ok and self.ed.can_add)
        self.add_btn.get_child().set_label(f"Add {unit}")
        self.add_btn.set_sensitive(self.allow)
        self.add_btn.set_tooltip_text(f"Add a {unit}" if self.allow else
                                      f"Switch on “Allow changes” at the top to add a {unit}")
        m = self.node_menu
        m.remove_all()
        if self.node:
            m.append("Copy the name", "node.copyname")
            if self.b.kind != "redis":
                m.append("Show its structure", "node.structure")
            sec = Gio.Menu()
            if self.can_edit() and self.b.kind == "redis":
                sec.append("Set expiry…", "node.expire")
            if self.can_edit() and self.ed.can_empty:
                sec.append("Empty it…", "node.empty")
            if self.can_edit() and self.ed.can_drop:
                sec.append("Delete it…", "node.drop")
            if sec.get_n_items():
                m.append_section(None, sec)
        self.node_mb.set_visible(bool(self.node))
        for n in ("empty", "drop", "expire"):
            self.node_ag.lookup_action(n).set_enabled(self.allow)
        if hasattr(self, "newkey"):
            self.newkey.set_sensitive(self.allow)
        self.data.panel.sync()
        self.data.sync_text_edit()

    def _node_act(self, what):
        node = self.node
        if what == "empty":
            title, body, ok = (f"Empty {node.label}?", f"Every {self.ed.unit} in <b>{esc(node.label)}</b> is removed; "
                               "the table itself stays. This can't be undone.", "Empty it")
        else:
            title, body, ok = (f"Delete {node.label}?", f"<b>{esc(node.label)}</b> and everything in it is deleted "
                               "for good. Applications that use it will break.", "Delete it")

        def done(msg):
            self.win.toast(msg)
            if what == "drop":
                self.node = None
                self.data.message("Deleted", "", "user-trash-symbolic")
                self.reload()
            else:
                self.reload_data()
        confirm(self.win, title, body, ok, lambda: bg(getattr(self.ed, what), node, self.allow, done=done,
                                                      error=lambda e: show_error(self.win, "That didn't work", str(e))))

    def _expire(self):
        from .quick import _ask
        node = self.node

        def go(text):
            t = text.strip().lower()
            secs = None if t in ("", "never", "0", "none") else t
            if secs is not None:
                try:
                    secs = int(float(secs))
                except ValueError:
                    self.win.toast("Type a number of seconds, or “never”")
                    return
            bg(self.ed.expire, node, secs, self.allow, done=lambda m: (self.win.toast(m), self.reload()),
               error=lambda e: show_error(self.win, "That didn't work", str(e)))
        _ask(self.win, "Set expiry", "Seconds until Redis deletes this key (e.g. 3600 = one hour), or “never”.",
             "never", "Set", go)

    def _new_key(self):
        d = Adw.AlertDialog(heading="New key", body="Created in database " + str(self.b.db) + ".")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        name = Gtk.Entry(placeholder_text="Name, e.g. user:42", activates_default=True)
        kinds = [("string", "Text (string)"), ("hash", "Fields (hash)"), ("list", "List"), ("set", "Set"),
                 ("zset", "Sorted set"), ("json", "JSON document (RedisJSON)")]
        kind = Gtk.DropDown(model=Gtk.StringList.new([k[1] for k in kinds]), tooltip_text="Kind of key")
        val = Gtk.Entry(placeholder_text="First value", activates_default=True)
        for w in (name, kind, val):
            box.append(w)
        d.set_extra_child(box)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Create")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")

        def resp(_d, r):
            if r != "ok":
                return
            k = kinds[kind.get_selected()][0]
            n = name.get_text().strip()

            def done(msg):
                self.win.toast(msg)
                self.node = db.Node(n, "key", n)
                self.reload()
            bg(self.ed.new_key, n, k, val.get_text(), self.allow, done=done,
               error=lambda e: show_error(self.win, "Not created", str(e)))
        d.connect("response", resp)
        d.present(self.win)

    def save_text(self, text):
        node = self.node
        bg(self.ed.save_text, node, text, self.allow, done=lambda m: (self.win.toast(m), self.reload_data()),
           error=lambda e: show_error(self.win, "Not saved", str(e)))

    def save_blob(self, ident, col):
        node = self.node

        def write(path):
            def done(data):
                with open(path, "wb") as f:
                    f.write(data)
                self.win.toast(f"Saved {db._size(len(data))} to {path}")
            bg(self.ed.blob, node, ident, col, done=done, error=lambda e: show_error(self.win, "Not saved", str(e)))
        save_dialog(self.win, "Save the value", f"{node.label}-{col}.bin", write)

    def _page(self, d):
        if self.node:
            self.offset = max(0, self.offset + d * getattr(self, "page_size", db.ROW_LIMIT))
            self._load_data()

    def can_sort(self):
        return self.b.kind in ("postgres", "mysql") and bool(self.node) and self.node.kind == "table"

    def sort_by(self, col, desc):
        self.b.sort = (col, desc) if col else None
        self.offset = 0
        self.reload_data()

    def _apply_where(self):
        self.b.where = self.wfilter.get_text().strip()
        self.offset = 0
        if self.node:
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
        # cards instead of a plain table (the table stays as the model behind them, for search and tests)
        self.cards = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, min_children_per_line=1,
                                 max_children_per_line=4, column_spacing=14, row_spacing=14, valign=Gtk.Align.START,
                                 css_classes=["db-cards"])
        self.cards.set_filter_func(lambda ch: self._card_match(ch))
        self.stack.add_named(Gtk.ScrolledWindow(child=self.cards, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                                vexpand=True), "cards")
        self._card_sig = None
        self.search.connect("search-changed", lambda *_: self.cards.invalidate_filter())
        store.connect("containers", lambda *_: self.get_mapped() and self.on_show())

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
        from . import quick
        from .pages import row_menu

        def act_setup():
            box = Gtk.Box(halign=Gtk.Align.END)
            box.row = None
            box.append(row_menu(box, [
                ("Open", lambda r: self.open(r.key)),
                ("Rename the container…", lambda r: quick.rename_container(self.win, self.store, r.key)),
                ("Change the container's ports…", lambda r: quick.PortsDialog(self.win, self.store, r.key).present(self.win))]))
            return box

        def act_upd(box, r):
            box.row = r
        self.widget_column("", act_setup, act_upd, width=48)

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
        if rows:
            self._fill_cards(rows)
            self.stack.set_visible_child_name("cards")
        n = len(rows)
        self.wtitle.set_subtitle(f"{n} database container{'s' if n != 1 else ''} · read-only unless you allow changes"
                                 if n else "Browse and query the databases in your containers")

    def on_size(self, n):
        from .util import per_size, set_columns
        set_columns(self.cards, per_size(n, 1, 2, 3, 4, 5, 6))

    BADGES = {"postgres": ("PG", "#336791"), "mysql": ("MY", "#00758f"), "redis": ("RD", "#d82c20"),
              "mongo": ("MG", "#13aa52"), "vespa": ("VS", "#ff6a13"), "opensearch": ("OS", "#005eb8"),
              "typesense": ("TS", "#d6336c"), "meilisearch": ("MS", "#ff5caa")}

    def _card_match(self, child):
        q = self.search.get_text().strip().lower()
        r = getattr(child.get_child(), "row", None)
        return not q or r is None or self.match(r, q)

    def _fill_cards(self, rows):
        sig = tuple((r.key, r.data.get("State"), r.data.get("Status"), str(r.data.get("Ports"))) for r in rows)
        if sig == self._card_sig:                         # nothing changed: keep the cards (no widget churn)
            return
        self._card_sig = sig
        while (ch := self.cards.get_first_child()):
            self.cards.remove(ch)
        for r in rows:
            self.cards.append(self._card(r))

    def _card(self, r):
        from . import quick
        from .pages import row_menu
        c, kind = r.data, r.extra.get("kind")
        running = c.get("State") == "running"
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["card", "db-card"] +
                       ([] if running else ["stopped"]))
        card.row = r
        top = Gtk.Box(spacing=12)
        text, color = self.BADGES.get(kind, ("DB", "#666"))
        badge = label(text, css=["db-badge"], ellipsize=False, xalign=0.5, valign=Gtk.Align.CENTER)
        prov = Gtk.CssProvider()
        prov.load_from_string(f"label {{ background: {color}; }}")
        badge.get_style_context().add_provider(prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        top.append(badge)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        col.append(label(cname(c), css=["heading"]))
        col.append(label(db.LABELS.get(kind, kind or "") + " · " + (c.get("Image") or ""), css=["caption", "dim-label"]))
        top.append(col)
        card.append(top)
        st = Gtk.Box(spacing=8)
        dot = Gtk.Box(css_classes=["dot", state_of(c)], valign=Gtk.Align.CENTER)
        st.append(dot)
        st.append(label(c.get("Status") or c.get("State") or "", css=["caption"]))
        card.append(st)
        # the port row is always there (empty state included), so every card has the same rows and height
        ports = [p for p in c.get("Ports") or [] if p.get("PublicPort")]
        pr = Gtk.Box(spacing=8, css_classes=["db-card-port"])
        if ports:
            p = ports[0]
            lan = (p.get("IP") or "0.0.0.0") in ("0.0.0.0", "::")
            pr.append(label(f"localhost:{p['PublicPort']}", css=["monospace", "caption"]))
            pr.append(label("whole network ⚠" if lan else "this computer only", css=["status-pill", "warn" if lan else "ok"],
                            ellipsize=False, tooltip_text="Other devices on your Wi-Fi can reach this port" if lan
                            else "Only this computer can reach this port"))
        else:
            pr.append(label("No published port", css=["caption", "dim-label"]))
        card.append(pr)
        card.append(Gtk.Box(vexpand=True))              # pushes the buttons to the bottom edge
        acts = Gtk.Box(spacing=8)
        if running:
            ob = Gtk.Button(label="Open", hexpand=True, tooltip_text="Browse its tables, keys or documents")
            ob.add_css_class("suggested-action")
            ob.connect("clicked", lambda *_: self.open(r.key))
        else:
            ob = Gtk.Button(label="Start", hexpand=True, tooltip_text="Start the container, then open it")
            ob.connect("clicked", lambda *_: (bg(self.store.engine.action, r.key, "start",
                                                 done=lambda _x: self.store.refresh("containers"),
                                                 error=lambda e: self.win.toast(str(e))),
                                              self.win.toast(f"Starting {cname(c)}…")))
        acts.append(ob)
        holder = Gtk.Box()
        holder.row = r
        acts.append(row_menu(holder, [
            ("Rename the container…", lambda rr: quick.rename_container(self.win, self.store, rr.key)),
            ("Change the container's ports…", lambda rr: quick.PortsDialog(self.win, self.store, rr.key).present(self.win))]))
        card.append(acts)
        return card

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
        super().__init__(title="Open an SQLite file", content_width=520)
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
