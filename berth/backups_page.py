"""Backups page: back up a database or a volume now, schedule it, see every backup file, restore one."""
import os
import time

from gi.repository import Adw, Gio, GLib, Gtk

from . import backups as bk, db, dbtools
from .util import (follow_content, PageHeader, WidePrefs, ago, bg, confirm, esc, flat_page, fmt_bytes, icon_button, label, open_uri)


def _targets(store):
    """[(kind, name, label)] — database containers Berth can back up, then volumes."""
    out = []
    for c in store.containers or []:
        k = db.detect(c)
        if k in dbtools.BACKUP_EXT:
            name = c["Names"][0].lstrip("/")
            out.append(("db", name, f"{name} · {_engine_label(k)}"))
    for v in store.volumes or []:
        out.append(("volume", v["Name"], f"{v['Name']} · volume"))
    return out


def _engine_label(kind):
    return {"postgres": "PostgreSQL", "mysql": "MySQL / MariaDB", "mongo": "MongoDB", "redis": "Redis"}.get(kind, kind)


class ScheduleDialog(Adw.Dialog):
    """New or changed schedule (also "back up once" when once=True)."""

    def __init__(self, page, sched=None, preset=None, once=False):
        super().__init__(title="Back up now" if once else ("Change schedule" if sched else "Schedule backups"),
                         content_width=520)
        self.page, self.sched, self.once = page, sched, once
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        hb.pack_start(cancel)
        self.ok = Gtk.Button(label="Back up" if once else "Save")
        self.ok.add_css_class("suggested-action")
        self.ok.connect("clicked", lambda *_: self._ok())
        hb.pack_end(self.ok)
        tv.add_top_bar(hb)
        pp = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(description="Databases are dumped with their own tool (pg_dump, mysqldump, mongodump, "
                                             "redis-cli); volumes are packed into a .tar.gz. Nothing is stopped.")
        self.targets = _targets(page.store)
        self.what = Adw.ComboRow(title="What", model=Gtk.StringList.new([t[2] for t in self.targets]))
        cur = (sched["kind"], sched["target"]) if sched else preset
        if cur:
            for i, t in enumerate(self.targets):
                if (t[0], t[1]) == tuple(cur[:2]):
                    self.what.set_selected(i)
        self.what.connect("notify::selected", lambda *_: self._sync())
        g.add(self.what)
        self.dbname = Adw.EntryRow(title="Database (empty: the container's default)")
        if sched and sched.get("db"):
            self.dbname.set_text(sched["db"])
        elif preset and len(preset) > 2 and preset[2]:
            self.dbname.set_text(preset[2])
        g.add(self.dbname)
        pp.add(g)
        g2 = Adw.PreferencesGroup(title="When and where")
        self.every = Adw.ComboRow(title="How often", model=Gtk.StringList.new([e[1] for e in bk.EVERY]))
        hours = sched["every"] if sched else 24
        self.every.set_selected(next((i for i, e in enumerate(bk.EVERY) if e[0] == hours), 2))
        self.keep = Adw.SpinRow(title="Keep the newest", subtitle="Older backups of this schedule are deleted",
                                adjustment=Gtk.Adjustment(value=sched["keep"] if sched else 7, lower=1, upper=365,
                                                          step_increment=1))
        if not once:
            g2.add(self.every)
            g2.add(self.keep)
        self.folder = sched["folder"] if sched else bk.default_folder()
        self.frow = Adw.ActionRow(title="Folder", subtitle=esc(self.folder), activatable=True)
        self.frow.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
        self.frow.connect("activated", lambda *_: self._pick_folder())
        self.frow.set_tooltip_text("Choose the folder the backups go to")
        g2.add(self.frow)
        pp.add(g2)
        if not once:
            note = ("Runs while Berth is open (also in the tray); a missed backup runs when Berth starts. "
                    + ("On this computer a background timer also runs it while Berth is closed."
                       if bk.timer_supported() else ""))
            g2.set_description(note)
        tv.set_content(pp)
        self.set_child(tv)
        follow_content(self, 520)
        self._sync()

    def _sync(self):
        i = self.what.get_selected()
        is_db = 0 <= i < len(self.targets) and self.targets[i][0] == "db"
        self.dbname.set_visible(is_db)
        self.ok.set_sensitive(0 <= i < len(self.targets))

    def _pick_folder(self):
        d = Gtk.FileDialog(title="Folder for backups", accept_label="Choose")
        try:
            d.set_initial_folder(Gio.File.new_for_path(self.folder))
        except GLib.Error:
            pass

        def done(dlg, res):
            try:
                f = dlg.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.folder = f.get_path()
                self.frow.set_subtitle(esc(self.folder))
        d.select_folder(self.page.win, None, done)

    def _ok(self):
        i = self.what.get_selected()
        if not (0 <= i < len(self.targets)):
            return
        kind, target, _l = self.targets[i]
        s = bk.new_schedule(kind, target, self.dbname.get_text().strip() if kind == "db" else "",
                            bk.EVERY[self.every.get_selected()][0], int(self.keep.get_value()), self.folder)
        if self.sched:
            s["id"] = self.sched["id"]
        self.close()
        if self.once:
            self.page.run_once(s)
        else:
            self.page.save_schedule(s)


class BackupsPage(Gtk.Box):
    title = "Backups"
    icon = "document-save-symbolic"

    def __init__(self, win, store, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store, self.settings = win, store, settings
        self.running = set()
        tv = Adw.ToolbarView(vexpand=True)
        self.header = PageHeader("Backups", "Databases and volumes — right now, or on a schedule")
        now = Gtk.Button(label="Back up now…", valign=Gtk.Align.CENTER,
                         tooltip_text="Make one backup of a database or a volume")
        now.connect("clicked", lambda *_: ScheduleDialog(self, once=True).present(self.win))
        rest = Gtk.Button(label="Restore…", valign=Gtk.Align.CENTER, tooltip_text="Put a backup file back")
        rest.connect("clicked", lambda *_: self._restore_file())
        new = Gtk.Button(label="Schedule backups…", valign=Gtk.Align.CENTER,
                         tooltip_text="Back up a database or a volume regularly")
        new.add_css_class("suggested-action")
        new.connect("clicked", lambda *_: ScheduleDialog(self).present(self.win))
        for b in (rest, now, new):
            self.header.add(b)
        flat_page(tv, self.header)
        self.pp = WidePrefs(two_at=("lg", "xl", "2xl", "3xl"))
        self.g_sched = Adw.PreferencesGroup(title="Schedules")
        self.g_files = Adw.PreferencesGroup(title="Backup files", description="Newest first, from every schedule's "
                                            "folder and the default folder.")
        self.pp.add(self.g_sched)
        self.pp.add(self.g_files)
        self._rows = []
        tv.set_content(self.pp)
        self.append(tv)

    # ── data ──
    def schedules(self):
        return list(self.settings["backups"] or [])

    def save_schedule(self, s):
        lst = [x for x in self.schedules() if x["id"] != s["id"]] + [s]
        self.settings["backups"] = lst
        self.settings.save()
        bg(bk.set_timer, True, done=lambda _r: None, error=lambda e: None)
        self.win.toast(f"Backups of {s['target']} scheduled")
        self.refresh()
        self.run_now(s["id"]) if bk.is_due(s, bk.load_state()) else None

    def delete_schedule(self, s):
        def go():
            lst = [x for x in self.schedules() if x["id"] != s["id"]]
            self.settings["backups"] = lst
            self.settings.save()
            if not lst:
                bg(bk.set_timer, False, done=lambda _r: None, error=lambda e: None)
            self.refresh()
        confirm(self.win, "Stop these backups?", f"The schedule for <b>{esc(s['target'])}</b> is removed. Backup "
                "files already made stay in their folder.", "Remove schedule", go)

    def run_now(self, sid):
        if sid in self.running:
            return
        self.running.add(sid)
        self.refresh()

        def done(res):
            self.running.discard(sid)
            for s, ok, msg in res:
                self.win.toast(f"Backed up {s['target']}" if ok else f"Backup of {s['target']} failed: {msg[:120]}")
                if not ok:
                    self.win.notify("Backup failed", f"{s['target']}: {msg[:200]}")
            if not res:
                self.win.toast("Another backup is running — try again in a moment")
            self.refresh()

        def fail(e):
            self.running.discard(sid)
            self.win.toast(f"Backup failed: {e}")
            self.refresh()
        bg(bk.run_due, self.store.engine, self.schedules(), sid, True, done=done, error=fail)

    def run_once(self, s):
        """A one-off backup (not saved as a schedule)."""
        self.win.toast(f"Backing up {s['target']}…")

        def done(r):
            path, n = r
            self.win.toast(f"Saved {os.path.basename(path)} · {fmt_bytes(n)}")
            self.refresh()
        bg(bk.run_one, self.store.engine, s, done=done,
           error=lambda e: self.win.toast(f"Backup failed: {e}"))

    # ── showing ──
    def on_show(self):
        self.refresh()

    def on_size(self, n):
        self.pp.on_size(n)

    def refresh(self):
        for g, r in self._rows:
            g.remove(r)
        self._rows = []
        state = bk.load_state()
        scheds = self.schedules()
        if not scheds:
            r = Adw.ActionRow(title="No schedules yet", subtitle="“Schedule backups…” backs up a database or a volume "
                                                                 "every hour, day or week, and keeps the newest few.")
            self._add(self.g_sched, r)
        for s in scheds:
            st = state.get(s["id"]) or {}
            every = next((e[1] for e in bk.EVERY if e[0] == s["every"]), f"Every {s['every']} h")
            what = s["target"] + (f" · {s['db']}" if s.get("db") else "")
            if s["id"] in self.running:
                last = "Backing up now…"
            elif st.get("last"):
                last = ("✓ " if st.get("ok") else "✗ ") + f"{ago(st['last'])} — {st.get('message', '')}"
            else:
                last = "Not run yet"
            nxt = bk.next_run(s, state) - time.time()
            when = "due now" if nxt < 60 else f"next in {_span(nxt)}"
            r = Adw.ActionRow(title=esc(what), subtitle=esc(f"{every} · keep {s['keep']} · {when}\n{last}"),
                              activatable=True, subtitle_lines=3)
            r.add_prefix(Gtk.Image(icon_name="drive-multidisk-symbolic" if s["kind"] == "db" else
                                   "folder-symbolic"))
            if st.get("last") and not st.get("ok"):
                r.add_css_class("error")
            r.connect("activated", lambda _r, s=s: ScheduleDialog(self, sched=s).present(self.win))
            r.set_tooltip_text("Change this schedule")
            run = icon_button("media-playback-start-symbolic", "Back up now", lambda *_a, s=s: self.run_now(s["id"]))
            run.set_sensitive(s["id"] not in self.running)
            r.add_suffix(run)
            r.add_suffix(icon_button("folder-open-symbolic", "Open the folder",
                                     lambda *_a, s=s: _open_folder(s["folder"])))
            r.add_suffix(icon_button("user-trash-symbolic", "Remove this schedule",
                                     lambda *_a, s=s: self.delete_schedule(s)))
            self._add(self.g_sched, r)
        files = []
        seen = set()
        for s in scheds:
            for p, size, t in bk.files_of(s):
                if p not in seen:
                    seen.add(p)
                    files.append((p, size, t, s))
        d = bk.default_folder()
        try:
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if p not in seen and os.path.isfile(p) and not name.endswith(".part") and \
                        name.split(".")[-1] in ("dump", "sql", "gz", "rdb"):
                    st_ = os.stat(p)
                    files.append((p, st_.st_size, st_.st_mtime, None))
        except OSError:
            pass
        files.sort(key=lambda f: -f[2])
        if not files:
            self._add(self.g_files, Adw.ActionRow(title="No backup files yet",
                                                  subtitle=esc(f"They are saved to {d} unless you choose a folder.")))
        for p, size, t, s in files[:200]:
            r = Adw.ActionRow(title=esc(os.path.basename(p)), subtitle=esc(f"{fmt_bytes(size)} · {ago(t)} · "
                                                                          f"{os.path.dirname(p)}"))
            r.set_title_lines(1)
            r.add_suffix(icon_button("edit-undo-symbolic", "Restore this backup…",
                                     lambda *_a, p=p, s=s: self.restore(p, s)))
            r.add_suffix(icon_button("folder-open-symbolic", "Show in its folder",
                                     lambda *_a, p=p: _open_folder(os.path.dirname(p))))
            r.add_suffix(icon_button("user-trash-symbolic", "Delete this backup file",
                                     lambda *_a, p=p: self._delete_file(p)))
            self._add(self.g_files, r)

    def _add(self, g, r):
        g.add(r)
        self._rows.append((g, r))

    def _delete_file(self, p):
        def go():
            try:
                os.remove(p)
            except OSError as e:
                self.win.toast(f"Couldn't delete: {e}")
            self.refresh()
        confirm(self.win, "Delete this backup?", f"<b>{esc(os.path.basename(p))}</b> is deleted from the disk.",
                "Delete", go)

    # ── restore ──
    def _restore_file(self):
        d = Gtk.FileDialog(title="Choose a backup to restore", accept_label="Choose")
        try:
            d.set_initial_folder(Gio.File.new_for_path(bk.default_folder()))
        except GLib.Error:
            pass

        def done(dlg, res):
            try:
                f = dlg.open_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.restore(f.get_path(), None)
        d.open(self.win, None, done)

    def restore(self, path, sched):
        RestoreDialog(self, path, sched).present(self.win)


def _span(sec):
    sec = int(sec)
    if sec < 3600:
        return f"{max(1, sec // 60)} min"
    if sec < 86400 * 2:
        return f"{sec // 3600} h"
    return f"{sec // 86400} days"


def _open_folder(path):
    os.makedirs(path, exist_ok=True)
    open_uri(Gio.File.new_for_path(path).get_uri())


def _kind_of_file(path):
    n = path.lower()
    if n.endswith(".tar.gz") or n.endswith(".tgz") or n.endswith(".tar"):
        return "volume"
    if n.endswith(".dump"):
        return "postgres"
    if n.endswith(".archive.gz"):
        return "mongo"
    if n.endswith(".rdb"):
        return "redis"
    if n.endswith(".sql"):
        return "sql"
    return None


class RestoreDialog(Adw.Dialog):
    """Pick where a backup file goes, say plainly what will happen, then restore."""

    def __init__(self, page, path, sched):
        super().__init__(title="Restore a backup", content_width=520)
        self.page, self.path = page, path
        kind = _kind_of_file(path)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        hb.pack_start(cancel)
        self.ok = Gtk.Button(label="Restore")
        self.ok.add_css_class("destructive-action")
        self.ok.connect("clicked", lambda *_: self._go())
        hb.pack_end(self.ok)
        tv.add_top_bar(hb)
        pp = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title=esc(os.path.basename(path)))
        want = {"volume": ("volume",), "postgres": ("postgres",), "mongo": ("mongo",), "redis": ("redis",),
                "sql": ("postgres", "mysql")}.get(kind, ())
        self.targets = []
        for c in page.store.containers or []:
            k = db.detect(c)
            if k in want and c.get("State") == "running":
                self.targets.append(("db", c["Names"][0].lstrip("/"), k))
        if "volume" in want:
            self.targets += [("volume", v["Name"], "volume") for v in page.store.volumes or []]
        labels = [f"{n} · {_engine_label(k)}" for _t, n, k in self.targets] or ["Nothing running matches this file"]
        self.into = Adw.ComboRow(title="Into", model=Gtk.StringList.new(labels))
        if sched:
            for i, t in enumerate(self.targets):
                if t[1] == sched["target"]:
                    self.into.set_selected(i)
        g.add(self.into)
        self.dbname = Adw.EntryRow(title="Database (empty: the container's default)")
        if sched and sched.get("db"):
            self.dbname.set_text(sched["db"])
        g.add(self.dbname)
        self.into.connect("notify::selected", lambda *_: self._sync())
        pp.add(g)
        self.note = Adw.PreferencesGroup()
        self.what = label("", wrap=True, css=["dim-label"])
        self.note.add(self.what)
        pp.add(self.note)
        tv.set_content(pp)
        self.set_child(tv)
        follow_content(self, 520)
        self._sync()

    def _sync(self):
        i = self.into.get_selected()
        ok = 0 <= i < len(self.targets)
        self.ok.set_sensitive(ok)
        if not ok:
            self.what.set_text("Start the database container this backup belongs to, then try again.")
            return
        kind, name, eng = self.targets[i]
        self.dbname.set_visible(kind == "db" and eng != "redis")
        self.what.set_text({
            "volume": "The files in the backup are written into the volume; files with the same names are replaced. "
                      "Stop the containers that use it first if they write to it.",
            "redis": "Redis is stopped, its data file is replaced with the backup, and it starts again. Keys written "
                     "since the backup are lost.",
            "mongo": "Collections in the backup replace the ones with the same names.",
        }.get(eng if kind == "db" else "volume", "Tables in the backup replace the ones with the same names; other "
                                                 "tables are left alone."))

    def _go(self):
        i = self.into.get_selected()
        kind, name, eng = self.targets[i]
        dbn = self.dbname.get_text().strip()
        page, path = self.page, self.path
        self.close()

        def work():
            e = page.store.engine
            if kind == "volume":
                from .files import import_volume
                import_volume(e, name, path)
                return
            b = db.open_backend(e, e.inspect(name))
            b.connect()
            if dbn:
                b.db = dbn
            dbtools.restore(b, path, allow=True)

        def go():
            page.win.toast(f"Restoring into {name}…")
            bg(work, done=lambda _r: (page.win.toast(f"Restored {os.path.basename(path)} into {name}"),
                                      page.refresh()),
               error=lambda e: page.win.notify("Restore failed", str(e)[:300], toast=True))
        confirm(page.win, f"Restore into {name}?", esc(self.what.get_text()) + "\n\nThis can't be undone.",
                "Restore", go)
