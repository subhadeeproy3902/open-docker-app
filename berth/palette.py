"""Ctrl+K: type a few letters to jump anywhere — a page, a container, a stack, an image, a volume, a network, a
database — or to start a common task. Arrow keys move, Enter opens, Esc closes."""
from gi.repository import Adw, Gdk, Gtk, Pango

from .util import cname, esc, labels


def score(query, text):
    """Higher is better; 0 = no match. Prefix > word start > anywhere > letters in order."""
    q, t = query.lower(), text.lower()
    if not q:
        return 1
    if t.startswith(q):
        return 100 - len(t) / 100
    i = t.find(q)
    if i > 0 and not t[i - 1].isalnum():
        return 80 - i / 100
    if i >= 0:
        return 60 - i / 100
    pos = 0
    for ch in q:                                   # subsequence: "shpdb" finds "shop-db-1"
        pos = t.find(ch, pos)
        if pos < 0:
            return 0
        pos += 1
    return 30 - len(t) / 100


class Palette(Adw.Dialog):
    def __init__(self, win):
        super().__init__(title="Go to", content_width=620)
        self.win = win
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        self.entry = Gtk.SearchEntry(placeholder_text="Search containers, stacks, images, pages, tasks…",
                                     hexpand=True)
        self.entry.connect("search-changed", lambda *_: self._fill())
        self.entry.connect("activate", lambda *_: self._open(self.list.get_selected_row()))
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.entry.add_controller(keys)
        box.append(self.entry)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE, css_classes=["boxed-list"])
        self.list.connect("row-activated", lambda _l, r: self._open(r))
        self.sw = Gtk.ScrolledWindow(child=self.list, propagate_natural_height=True, max_content_height=460,
                                     hscrollbar_policy=Gtk.PolicyType.NEVER)
        box.append(self.sw)
        self.hint = Gtk.Label(label="↑ ↓ to move · Enter to open · Esc to close", css_classes=["caption", "dim-label"])
        box.append(self.hint)
        self.set_child(box)
        self.items = self._items()
        self._fill()
        self.connect("map", lambda *_: self.entry.grab_focus())
        st = win.store
        self._sigs = []
        for kind in ("images", "volumes", "networks"):      # lists no page has loaded yet: fetch them now
            if getattr(st, kind) is None and st.up:
                self._sigs.append(st.connect(kind, lambda *_: self._reload()))
                st.refresh(kind)
        self.connect("closed", lambda *_: [st.disconnect(h) for h in self._sigs])

    def _reload(self):
        self.items = self._items()
        self._fill()

    def _items(self):
        """(title, subtitle, icon, open)"""
        w, st = self.win, self.win.store
        out = []
        from .app import PAGES
        for key, cls in PAGES:
            out.append((cls.title, "Page", cls.icon, lambda k=key: w.go(k)))
        tasks = [("Pull an image…", "pull", "folder-download-symbolic"),
                 ("Run a Compose file…", "compose", "media-playback-start-symbolic"),
                 ("Build an image…", "build", "applications-engineering-symbolic"),
                 ("Back up a database or volume now…", "backup", "document-save-symbolic"),
                 ("Schedule backups…", "schedule", "document-save-symbolic"),
                 ("Share files with a phone…", "send", "folder-publicshare-symbolic"),
                 ("Give a container a local address…", "address", "network-server-symbolic"),
                 ("Check images for updates", "updates", "software-update-available-symbolic")]
        for title, key, icon in tasks:
            out.append((title, "Task", icon, lambda k=key: self._task(k)))
        from . import db
        for c in st.containers or []:
            n = cname(c)
            out.append((n, f"Container · {c.get('Image', '')} · {c.get('State', '')}", "application-x-executable-symbolic",
                        lambda cid=c["Id"]: w.show_container(cid)))
            if db.detect(c) and c.get("State") == "running":
                out.append((n, "Database · open the explorer", "drive-multidisk-symbolic",
                            lambda cid=c["Id"]: (w.go("databases"), w.pages["databases"].open(cid))))
        for p in sorted({labels(c).get("com.docker.compose.project") for c in st.containers or []} - {None, ""}):
            out.append((p, "Stack", "view-dual-symbolic", lambda: w.go("stacks")))
        for im in st.images or []:
            for t in im.get("RepoTags") or []:
                if t != "<none>:<none>":
                    out.append((t, "Image", "drive-harddisk-symbolic", lambda: w.go("images")))
        for v in st.volumes or []:
            out.append((v["Name"], "Volume", "folder-symbolic", lambda: w.go("volumes")))
        for n in st.networks or []:
            out.append((n["Name"], "Network", "network-wired-symbolic", lambda: w.go("networks")))
        return out

    def _task(self, k):
        w, st = self.win, self.win.store
        if k == "pull":
            from .pages import PullDialog
            PullDialog(w, st).present(w)
        elif k == "compose":
            from .compose import ComposeOpen
            ComposeOpen(w, st).present(w)
        elif k == "build":
            from .pages import BuildDialog
            BuildDialog(w, st).present(w)
        elif k in ("backup", "schedule"):
            from .backups_page import ScheduleDialog
            w.go("backups")
            ScheduleDialog(w.pages["backups"], once=k == "backup").present(w)
        elif k == "send":
            w.go("send")
            w.pages["send"]._pick()
        elif k == "address":
            from .sharing_page import ExposeDialog
            w.go("sharing")
            ExposeDialog(w.pages["sharing"]).present(w)
        elif k == "updates":
            w.go("images")
            w.pages["images"].check_updates()

    def _fill(self):
        q = self.entry.get_text().strip()
        # empty box: pages and tasks first, in their own order; typing: best match first
        ranked = sorted(((score(q, t) or score(q, f"{t} {s}") * 0.5, -i) for i, (t, s, _ic, _f) in
                         enumerate(self.items)), reverse=True)
        ranked = [(sc, -j) for sc, j in ranked]
        while (r := self.list.get_first_child()) is not None:
            self.list.remove(r)
        shown = 0
        for sc, i in ranked:
            if sc <= 0 or shown >= 40:
                break
            t, s, icon, fn = self.items[i]
            row = Adw.ActionRow(title=esc(t), subtitle=esc(s), activatable=True)
            row.add_prefix(Gtk.Image(icon_name=icon))
            row.fn = fn
            self.list.append(row)
            shown += 1
        if not shown:
            empty = Gtk.ListBoxRow(activatable=False, selectable=False)
            empty.set_child(Gtk.Label(label=f"Nothing matches “{q}”", margin_top=12, margin_bottom=12,
                                      css_classes=["dim-label"], ellipsize=Pango.EllipsizeMode.END))
            self.list.append(empty)
        first = self.list.get_row_at_index(0)
        if first is not None and getattr(first, "fn", None):
            self.list.select_row(first)

    def _key(self, _c, kv, _code, _state):
        if kv in (Gdk.KEY_Down, Gdk.KEY_Up):
            cur = self.list.get_selected_row()
            i = (cur.get_index() if cur else -1) + (1 if kv == Gdk.KEY_Down else -1)
            r = self.list.get_row_at_index(max(0, i))
            if r is not None and getattr(r, "fn", None):
                self.list.select_row(r)
                r.grab_focus()
                self.entry.grab_focus()
            return True
        return False

    def _open(self, row):
        fn = getattr(row, "fn", None) if row is not None else None
        if fn:
            self.close()
            fn()
