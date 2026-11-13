"""Send files page: share files and folders with a phone (same Wi-Fi: QR code) or anyone (public Cloudflare link),
and receive files from a phone. Backend: berth/fileshare.py."""
import os
import secrets
import time

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import fileshare as fs
from . import qr
from .util import PageHeader, app_icon, bg, flat_page, icon_button, label, open_uri
from .util import set_busy  # noqa: E402

EXPIRY = [("1 hour", 3600), ("15 minutes", 900), ("4 hours", 4 * 3600), ("1 day", 86400), ("Never", None)]
LIMITS = [("No limit", None), ("1 download", 1), ("5 downloads", 5), ("10 downloads", 10)]


def _qr_picture(url, size=176):
    tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(qr.png_bytes(qr.encode(url), scale=6, border=3)))
    pic = Gtk.Picture(paintable=tex, can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN, width_request=size,
                      height_request=size, css_classes=["send-qr"], halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
    pic.set_tooltip_text("Scan with your phone's camera")
    return pic


def _left(t):
    s = int(t - time.time())
    if s <= 0:
        return "ending"
    if s < 3600:
        return f"{max(1, s // 60)} min left"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60:02d} min left"
    return f"{s // 86400} d left"


class OptionsDialog(Adw.Dialog):
    """How to share the chosen files: PIN, expiry, download limit."""

    def __init__(self, page, paths=None, receive_dir=None):
        title = "Receive files from a phone" if receive_dir else "Share files"
        super().__init__(title=title, content_width=480)
        self.page, self.paths, self.receive_dir = page, paths or [], receive_dir
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        go = Gtk.Button(label="Start")
        go.add_css_class("suggested-action")
        go.connect("clicked", lambda *_: self._go())
        hb.pack_end(go)
        tv.add_top_bar(hb)
        pp = Adw.PreferencesPage()
        what = Adw.PreferencesGroup()
        if receive_dir:
            row = Adw.ActionRow(title="Files go to", subtitle=receive_dir)
            row.add_prefix(Gtk.Image(icon_name="folder-download-symbolic"))
        else:
            n = len(self.paths)
            row = Adw.ActionRow(title=os.path.basename(self.paths[0]) + (f" and {n - 1} more" if n > 1 else ""),
                                subtitle="Shared straight from this computer — nothing is uploaded first")
            row.add_prefix(Gtk.Image(icon_name="folder-symbolic" if os.path.isdir(self.paths[0]) else
                                     "text-x-generic-symbolic"))
        what.add(row)
        pp.add(what)
        g = Adw.PreferencesGroup(title="Protection")
        self.pin = Adw.SwitchRow(title="Ask for a PIN", subtitle="A 4-digit code you tell the other person")
        self.pin.set_active(bool(receive_dir))
        g.add(self.pin)
        self.exp = Adw.ComboRow(title="Stop sharing after", model=Gtk.StringList.new([e[0] for e in EXPIRY]))
        g.add(self.exp)
        if not receive_dir:
            self.lim = Adw.ComboRow(title="Downloads allowed", model=Gtk.StringList.new([x[0] for x in LIMITS]))
            g.add(self.lim)
        pp.add(g)
        tv.set_content(pp)
        self.set_child(tv)

    def _go(self):
        pin = f"{secrets.randbelow(10000):04d}" if self.pin.get_active() else None
        sec = EXPIRY[self.exp.get_selected()][1]
        lim = LIMITS[self.lim.get_selected()][1] if not self.receive_dir else None
        share = fs.Share(self.paths, receive_dir=self.receive_dir, pin=pin,
                         expires=time.time() + sec if sec else None, max_downloads=lim)
        self.close()
        self.page.start(share)


class SendPage(Gtk.Box):
    title = "Send files"
    icon = "folder-publicshare-symbolic"

    def __init__(self, win, store, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store, self.settings = win, store, settings
        self.server = fs.Server(port=int(settings["send_port"] or fs.DEFAULT_PORT))
        self.server.on_change = lambda: GLib.idle_add(lambda: self.refresh() and False)
        self.cards = {}
        tv = Adw.ToolbarView(vexpand=True)
        self.header = PageHeader("Send files", "Share with your phone or anyone — straight from this computer")
        recv = Gtk.Button(label="Receive from phone…", valign=Gtk.Align.CENTER)
        recv.connect("clicked", lambda *_: self._pick_receive())
        folder = Gtk.Button(label="Share a folder…", valign=Gtk.Align.CENTER)
        folder.connect("clicked", lambda *_: self._pick(folder=True))
        files = Gtk.Button(label="Share files…", valign=Gtk.Align.CENTER)
        files.add_css_class("suggested-action")
        files.connect("clicked", lambda *_: self._pick())
        for b in (recv, folder, files):
            self.header.add(b)
        flat_page(tv, self.header)
        self.banner = Adw.Banner(title="Phones on your Wi-Fi can't reach Berth yet: the firewall blocks the port.",
                                 button_label="Allow (asks for your password)", revealed=False)
        self.banner.connect("button-clicked", lambda *_: self._open_firewall())
        tv.add_top_bar(self.banner)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, css_classes=["page-body"])
        self.list = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, max_children_per_line=2,
                                min_children_per_line=1, column_spacing=16, row_spacing=16, valign=Gtk.Align.START)
        body.append(self.list)
        self.empty = self._empty()
        body.append(self.empty)
        tv.set_content(Gtk.ScrolledWindow(child=body, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True))
        self.append(tv)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._dropped)
        self.add_controller(drop)
        GLib.timeout_add_seconds(15, self._tick)

    # ── empty state ──
    def _empty(self):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["send-drop"],
                       halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
        card.append(Gtk.Image(icon_name=app_icon(), pixel_size=96, css_classes=["send-logo"]))
        card.append(label("Drop files here to share them", css=["send-drop-title"], xalign=0.5))
        card.append(label("Or use the buttons at the top. The other person opens a link or scans a QR code — no app, "
                          "no account, nothing uploaded first.", css=["dim-label"], wrap=True, xalign=0.5,
                          justify=Gtk.Justification.CENTER))
        tips = Gtk.Box(spacing=12, homogeneous=True, margin_top=10)
        for icon, title, text in (("network-wireless-symbolic", "Same Wi-Fi", "Scan the QR code. Fast, stays on "
                                   "your network."),
                                  ("network-workgroup-symbolic", "Anywhere", "Tap “Public link” for a Cloudflare "
                                   "link that works on mobile data."),
                                  ("changes-prevent-symbolic", "Under control", "PIN, expiry and a download limit; "
                                   "stop any time.")):
            t = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["send-tip"])
            t.append(Gtk.Image(icon_name=icon, pixel_size=22, halign=Gtk.Align.START, css_classes=["accent"]))
            t.append(label(title, css=["heading"]))
            t.append(label(text, css=["dim-label", "caption"], wrap=True))
            tips.append(t)
        card.append(tips)
        return card

    # ── choosing ──
    def _pick(self, folder=False):
        d = Gtk.FileDialog(title="Choose a folder to share" if folder else "Choose files to share")

        def done(dlg, res):
            try:
                if folder:
                    paths = [dlg.select_folder_finish(res).get_path()]
                else:
                    paths = [f.get_path() for f in dlg.open_multiple_finish(res)]
            except GLib.Error:
                return
            paths = [p for p in paths if p]
            if paths:
                OptionsDialog(self, paths).present(self.win)
        (d.select_folder if folder else d.open_multiple)(self.win, None, done)

    def _pick_receive(self):
        d = Gtk.FileDialog(title="Where should received files go?")
        dl = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if dl:
            d.set_initial_folder(Gio.File.new_for_path(dl))

        def done(dlg, res):
            try:
                p = dlg.select_folder_finish(res).get_path()
            except GLib.Error:
                return
            OptionsDialog(self, receive_dir=p).present(self.win)
        d.select_folder(self.win, None, done)

    def _dropped(self, _t, value, _x, _y):
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if paths:
            OptionsDialog(self, paths).present(self.win)
        return True

    # ── shares ──
    def start(self, share):
        try:
            self.server.add(share)
        except OSError as e:
            self.win.toast(f"Couldn't start sharing: {e}")
            return
        self.refresh()
        url = self.server.lan_url(share)
        self.get_clipboard().set(url)
        self.win.toast("Sharing — link copied. Scan the QR code with your phone.")
        self._check_firewall()

    def stop(self, sid):
        sh = self.server.shares.get(sid)
        if sh and sh.tunnel_id:
            from . import sharing
            bg(sharing.stop_tunnel, self.store.engine, sh.tunnel_id, error=lambda e: None)
        self.server.remove(sid)
        self.refresh()

    def _public(self, sid, btn):
        sh = self.server.shares.get(sid)
        if not sh or not self.store.up:
            self.win.toast("Public links need a running Docker engine (they use Cloudflare's cloudflared container)")
            return
        set_busy(btn, True)
        btn.set_label("Starting…")

        def done(r):
            sh.tunnel_id, base = r
            sh.public_url = f"{base}/s/{sh.id}/"
            self.get_clipboard().set(sh.public_url)
            self.win.toast("Public link ready — copied")
            self.refresh()

        def fail(e):
            set_busy(btn, False)
            btn.set_label("Public link")
            self.win.toast(f"Public link failed: {e}")
        bg(fs.start_public, self.store.engine, self.server.port, done=done, error=fail)

    def _check_firewall(self):
        bg(fs.firewall_state, self.server.port,
           done=lambda st: self.banner.set_revealed(st[0] is not None and st[1] is False), error=lambda e: None)

    def _open_firewall(self):
        def done(r):
            ok, msg = r
            self.banner.set_revealed(not ok)
            self.win.toast(f"Port {self.server.port} is open until the next restart — phones can connect now" if ok
                           else f"The firewall wasn't changed: {msg[-160:]}")
        bg(fs.open_firewall, self.server.port, done=done, error=lambda e: self.win.toast(str(e)))

    def _tick(self):
        if self.server.shares:
            gone = self.server.sweep()
            if gone:
                self.win.toast("Sharing ended: " + ", ".join(gone))
            self.refresh()
        return True

    # ── cards ──
    def refresh(self):
        while (ch := self.list.get_first_child()):
            self.list.remove(ch)
        shares = list(self.server.shares.values())
        self.empty.set_visible(not shares)
        self.list.set_visible(bool(shares))
        for sh in shares:
            self.list.append(self._card(sh))
        n = len(shares)
        self.header.set_subtitle(f"{n} active share{'s' if n != 1 else ''} · port {self.server.port}" if n else
                                 "Share with your phone or anyone — straight from this computer")

    def _card(self, sh):
        url = sh.public_url or self.server.lan_url(sh)
        card = Gtk.Box(spacing=18, css_classes=["card", "send-card"])
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.append(_qr_picture(url))
        left.append(label("Public link" if sh.public_url else "Same Wi-Fi", css=["caption", "dim-label"], xalign=0.5))
        card.append(left)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        col.append(label(sh.title, css=["title-4"]))
        meta = []
        if sh.paths:
            meta.append(fs.human(sh.size))
            meta.append(f"{sh.downloads} download{'s' if sh.downloads != 1 else ''}" +
                        (f" of {sh.max_downloads}" if sh.max_downloads else ""))
        else:
            meta.append(f"{len(sh.uploads)} file{'s' if len(sh.uploads) != 1 else ''} received")
        if sh.expires:
            meta.append(_left(sh.expires))
        col.append(label(" · ".join(meta), css=["dim-label"]))
        pin = Gtk.Box(spacing=8)                          # always present: same rows on every card
        pin.append(label("PIN", css=["caption", "dim-label"], valign=Gtk.Align.CENTER))
        pin.append(label(sh.pin, css=["send-pin"], selectable=True, ellipsize=False,
                         tooltip_text="Tell this code to the person you share with") if sh.pin else
                   label("none — anyone with the link can open it", css=["caption", "dim-label"]))
        col.append(pin)
        link = Gtk.Box(spacing=6, css_classes=["send-link"])
        link.append(label(url, css=["monospace"], hexpand=True, selectable=True, tooltip_text=url))
        link.append(icon_button("edit-copy-symbolic", "Copy link",
                                lambda *_: (self.get_clipboard().set(url), self.win.toast("Link copied"))))
        link.append(icon_button("adw-external-link-symbolic", "Open", lambda *_: open_uri(url)))
        col.append(link)
        if sh.uploads:
            rec = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for name, size, _t in sh.uploads[-4:]:
                rec.append(label(f"⬇ {name} · {fs.human(size)}", css=["caption"]))
            col.append(rec)
        col.append(Gtk.Box(vexpand=True))                 # buttons sit on the bottom edge of every card
        acts = Gtk.Box(spacing=8, homogeneous=True)       # equal buttons across the card's full width
        if not sh.public_url:
            pub = Gtk.Button(label="Public link", tooltip_text="Phone not on the same Wi-Fi, or the Wi-Fi blocks it? "
                                                               "Get a link that works anywhere (Cloudflare)")
            pub.connect("clicked", lambda b, s=sh.id: self._public(s, b))
            acts.append(pub)
        if sh.receive_dir:
            ob = Gtk.Button(label="Open folder")
            ob.connect("clicked", lambda *_: open_uri("file://" + sh.receive_dir))
            acts.append(ob)
        stop = Gtk.Button(label="Stop sharing")
        stop.add_css_class("destructive-action")
        stop.connect("clicked", lambda *_, s=sh.id: self.stop(s))
        acts.append(stop)
        col.append(acts)
        card.append(col)
        return card

    def on_show(self):
        self.refresh()

    def on_size(self, n):
        from .util import per_size, set_columns
        set_columns(self.list, per_size(n, 1, 1, 2, 2, 3, 4))
        self.empty.set_size_request(per_size(n, -1, 600, 760, 880, 1040, 1240), -1)

    def shutdown(self):
        for sid in list(self.server.shares):
            self.server.remove(sid)
