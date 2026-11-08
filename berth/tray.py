"""Optional tray icon (StatusNotifierItem + com.canonical.dbusmenu over D-Bus, no extra libraries).
Works with any SNI host: KDE, Quickshell/waybar trays, GNOME with the AppIndicator extension."""
import os

from gi.repository import Gio, GLib

SNI_XML = """<node><interface name="org.kde.StatusNotifierItem">
<property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
<property name="IconName" type="s" access="read"/><property name="IconThemePath" type="s" access="read"/>
<property name="ToolTip" type="(sa(iiay)ss)" access="read"/><property name="ItemIsMenu" type="b" access="read"/>
<property name="Menu" type="o" access="read"/>
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<signal name="NewToolTip"/><signal name="NewStatus"><arg type="s"/></signal><signal name="NewIcon"/><signal name="NewTitle"/>
</interface></node>"""

MENU_XML = """<node><interface name="com.canonical.dbusmenu">
<property name="Version" type="u" access="read"/><property name="TextDirection" type="s" access="read"/>
<property name="Status" type="s" access="read"/><property name="IconThemePath" type="as" access="read"/>
<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
<arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/></method>
<method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
<method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
<signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
<signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
</interface></node>"""

ITEM, MENU = "/StatusNotifierItem", "/MenuBar"


class Tray:
    """on_open() shows the window, on_engine() toggles the engine, on_quit() quits. Call update() when state changes."""

    def __init__(self, icon, on_open, on_engine, on_quit):
        self.icon, self.on_open, self.on_engine, self.on_quit = icon, on_open, on_engine, on_quit
        self.text, self.engine_up, self.rev = "Berth", False, 1
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.reg = [self.conn.register_object(ITEM, sni, self._sni_call, self._sni_prop, None),
                    self.conn.register_object(MENU, menu, self._menu_call, self._menu_prop, None)]
        self.own = Gio.bus_own_name_on_connection(self.conn, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        # (re-)register whenever a tray host (watcher) appears, e.g. after the bar restarts
        self.watch = Gio.bus_watch_name_on_connection(self.conn, "org.kde.StatusNotifierWatcher",
                                                      Gio.BusNameWatcherFlags.NONE, self._watcher_up, None)
        self.registered = False

    def _watcher_up(self, conn, _name, _owner):
        conn.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                  "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, 2000, None,
                  lambda c, r: self._registered(c, r))

    def _registered(self, conn, res):
        try:
            conn.call_finish(res)
            self.registered = True
        except GLib.Error as e:
            print("berth: tray host refused the icon:", e.message)

    def stop(self):
        for r in self.reg:
            self.conn.unregister_object(r)
        Gio.bus_unown_name(self.own)
        Gio.bus_unwatch_name(self.watch)

    # ── state ──
    def update(self, engine_up, running, total):
        self.engine_up = engine_up
        self.text = (f"{running} of {total} containers running" if engine_up else "Docker Engine is stopped")
        self.rev += 1
        for sig in ("NewToolTip", "NewTitle"):
            self.conn.emit_signal(None, ITEM, "org.kde.StatusNotifierItem", sig, None)
        self.conn.emit_signal(None, MENU, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self.rev, 0)))

    # ── StatusNotifierItem ──
    def _sni_prop(self, _c, _s, _p, _i, name):
        return {"Category": GLib.Variant("s", "ApplicationStatus"), "Id": GLib.Variant("s", "berth"),
                "Title": GLib.Variant("s", f"Berth — {self.text}"), "Status": GLib.Variant("s", "Active"),
                "IconName": GLib.Variant("s", self.icon), "IconThemePath": GLib.Variant("s", ""),
                "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon, [], "Berth", self.text)),
                "ItemIsMenu": GLib.Variant("b", False), "Menu": GLib.Variant("o", MENU)}.get(name)

    def _sni_call(self, _c, _s, _p, _i, method, _params, inv):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: self.on_open() or False)
        inv.return_value(None)

    # ── dbusmenu ──
    def _items(self):
        return [
            (1, {"label": "Open Berth"}),
            (2, {"type": "separator"}),
            (3, {"label": self.text, "enabled": False}),
            (4, {"label": "Stop Docker Engine" if self.engine_up else "Start Docker Engine"}),
            (5, {"type": "separator"}),
            (6, {"label": "Quit Berth"}),
        ]

    @staticmethod
    def _props(d):
        out = {}
        for k, v in d.items():
            out[k] = GLib.Variant("b", v) if isinstance(v, bool) else GLib.Variant("s", v)
        return out

    def _layout(self):
        kids = [GLib.Variant("(ia{sv}av)", (i, self._props(p), [])) for i, p in self._items()]
        return (0, {"children-display": GLib.Variant("s", "submenu")}, kids)

    def _menu_prop(self, _c, _s, _p, _i, name):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(name)

    def _menu_call(self, _c, _s, _p, _i, method, params, inv):
        if method == "GetLayout":
            inv.return_value(GLib.Variant("(u(ia{sv}av))", (self.rev, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            items = dict(self._items())
            inv.return_value(GLib.Variant("(a(ia{sv}))", ([(i, self._props(items[i])) for i in ids if i in items],)))
        elif method == "GetProperty":
            i, name = params.unpack()
            v = dict(self._items()).get(i, {}).get(name, "")
            inv.return_value(GLib.Variant("(v)", (GLib.Variant("b", v) if isinstance(v, bool) else GLib.Variant("s", str(v)),)))
        elif method == "Event":
            i, ev = params.unpack()[:2]
            if ev == "clicked":
                self._click(i)
            inv.return_value(None)
        elif method == "EventGroup":
            for i, ev, _d, _t in params.unpack()[0]:
                if ev == "clicked":
                    self._click(i)
            inv.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            inv.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            inv.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            inv.return_value(None)

    def _click(self, i):
        fn = {1: self.on_open, 4: self.on_engine, 6: self.on_quit}.get(i)
        if fn:
            GLib.idle_add(lambda: fn() or False)
