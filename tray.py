"""Tray icon over D-Bus (StatusNotifierItem + DBusMenu), so no extra GTK 3 / AppIndicator packages are needed.

Ubuntu's built-in "AppIndicator and KStatusNotifierItem" extension shows it in the top bar.
"""
import os

from gi.repository import Gio, GLib

ITEM_XML = """<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewIcon"/><signal name="NewTitle"/><signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
</interface></node>"""

MENU_XML = """<node><interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg name="parentId" type="i" direction="in"/><arg name="recursionDepth" type="i" direction="in"/>
    <arg name="propertyNames" type="as" direction="in"/>
    <arg name="revision" type="u" direction="out"/><arg name="layout" type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg name="ids" type="ai" direction="in"/><arg name="propertyNames" type="as" direction="in"/>
    <arg name="properties" type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg name="id" type="i" direction="in"/><arg name="name" type="s" direction="in"/>
    <arg name="value" type="v" direction="out"/>
  </method>
  <method name="Event">
    <arg name="id" type="i" direction="in"/><arg name="eventId" type="s" direction="in"/>
    <arg name="data" type="v" direction="in"/><arg name="timestamp" type="u" direction="in"/>
  </method>
  <method name="EventGroup">
    <arg name="events" type="a(isvu)" direction="in"/><arg name="idErrors" type="ai" direction="out"/>
  </method>
  <method name="AboutToShow"><arg name="id" type="i" direction="in"/><arg name="needUpdate" type="b" direction="out"/></method>
  <method name="AboutToShowGroup">
    <arg name="ids" type="ai" direction="in"/>
    <arg name="updatesNeeded" type="ai" direction="out"/><arg name="idErrors" type="ai" direction="out"/>
  </method>
  <signal name="LayoutUpdated"><arg name="revision" type="u"/><arg name="parent" type="i"/></signal>
  <signal name="ItemsPropertiesUpdated">
    <arg name="updatedProps" type="a(ia{sv})"/><arg name="removedProps" type="a(ias)"/>
  </signal>
</interface></node>"""

ITEM_PATH, MENU_PATH = "/StatusNotifierItem", "/MenuBar"


class Tray:
    """A tray icon with a menu. `items()` returns [(label, enabled, callback) or None for a separator]."""

    def __init__(self, on_activate, items):
        self.on_activate, self.items = on_activate, items
        self.icon, self.title = "audio-input-microphone-symbolic", "MeetScribe"
        self.revision = 1
        self.callbacks = {}
        self.ok = False
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        item_info = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
        menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.bus.register_object(ITEM_PATH, item_info, self._item_call, self._item_prop, None)
        self.bus.register_object(MENU_PATH, menu_info, self._menu_call, self._menu_prop, None)
        Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE, self._register, None)

    def _register(self, *_):
        try:
            self.bus.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                               "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                               GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, 2000, None)
            self.ok = True
        except GLib.Error:
            self.ok = False     # no tray host (the GNOME extension is off); the app works without it

    # --- public ----------------------------------------------------------
    def update(self, recording):
        self.icon = "media-record-symbolic" if recording else "audio-input-microphone-symbolic"
        self.title = "MeetScribe - recording" if recording else "MeetScribe"
        for sig in ("NewIcon", "NewTitle", "NewToolTip"):
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", sig, None)
        self.refresh_menu()

    def refresh_menu(self):
        self.revision += 1
        self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self.revision, 0)))

    # --- D-Bus plumbing ------------------------------------------------------
    def _emit(self, path, iface, name, params):
        try:
            self.bus.emit_signal(None, path, iface, name, params)
        except GLib.Error:
            pass

    def _item_prop(self, _conn, _sender, _path, _iface, prop):
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"), "Id": GLib.Variant("s", "meetscribe"),
            "Title": GLib.Variant("s", self.title), "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", self.icon), "IconThemePath": GLib.Variant("s", ""),
            "ItemIsMenu": GLib.Variant("b", False), "Menu": GLib.Variant("o", MENU_PATH),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon, [], self.title, "")),
        }
        return values.get(prop)

    def _item_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self.on_activate)
        invocation.return_value(None)

    def _layout(self):
        self.callbacks = {}
        children = []
        for n, item in enumerate(self.items(), start=1):
            if item is None:
                props = {"type": GLib.Variant("s", "separator")}
            else:
                label, enabled, callback = item
                props = {"label": GLib.Variant("s", label), "enabled": GLib.Variant("b", enabled)}
                self.callbacks[n] = callback
            children.append(GLib.Variant("(ia{sv}av)", (n, props, [])))
        return (0, {"children-display": GLib.Variant("s", "submenu")}, children)

    def _menu_prop(self, _conn, _sender, _path, _iface, prop):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(prop)

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            layout = self._layout()[2]
            out = [(c.unpack()[0], {k: GLib.Variant("s", v) if isinstance(v, str) else GLib.Variant("b", v)
                                    for k, v in c.unpack()[1].items()})
                   for c in layout if not ids or c.unpack()[0] in ids]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            invocation.return_value(GLib.Variant("(v)", (GLib.Variant("s", ""),)))
        elif method == "Event":
            item_id, event = params.unpack()[0], params.unpack()[1]
            if event == "clicked" and item_id in self.callbacks:
                GLib.idle_add(self.callbacks[item_id])
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event, _data, _ts in params.unpack()[0]:
                if event == "clicked" and item_id in self.callbacks:
                    GLib.idle_add(self.callbacks[item_id])
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (True,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)
