"""Storage overview: how much space each part of MintoMeet's data takes, the largest meetings, and
warnings when it's getting large, so you know when to clean up."""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402

import datasize  # noqa: E402
import meetings  # noqa: E402

LARGEST = 5


class StorageDialog(Adw.Dialog):
    def __init__(self, meetings_dir, open_meeting):
        super().__init__(title="Storage", content_width=600, content_height=720)
        self.meetings_dir = meetings_dir
        self.open_meeting = open_meeting
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Measure again")
        refresh.connect("clicked", lambda _b: self.fill(force=True))
        header.pack_start(refresh)
        view.add_top_bar(header)
        self.view = view
        self.set_child(view)
        self.fill()

    @staticmethod
    def row(title, size, subtitle=None, icon=None):
        row = Adw.ActionRow(title=title, use_markup=False)
        if subtitle:
            row.set_subtitle(subtitle)
        if icon:
            row.add_prefix(Gtk.Image(icon_name=icon))
        value = Gtk.Label(label=datasize.fmt(size), valign=Gtk.Align.CENTER)
        value.add_css_class("numeric")
        row.add_suffix(value)
        return row

    @staticmethod
    def open_button(path, tip):
        b = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text=tip)
        b.add_css_class("flat")
        b.connect("clicked", lambda _b: Gio.AppInfo.launch_default_for_uri(path.as_uri(), None))
        return b

    def group(self, title, total, description=None, folder=None, tip=None):
        g = Adw.PreferencesGroup(title=f"{title} · {datasize.fmt(total)}", description=description or "")
        if folder is not None and folder.exists():
            g.set_header_suffix(self.open_button(folder, tip or "Open the folder"))
        return g

    def fill(self, force=False):
        o = datasize.overview(self.meetings_dir, force=force)
        t = o["totals"]
        page = Adw.PreferencesPage()

        top = Adw.PreferencesGroup(title=f"MintoMeet uses {datasize.fmt(o['total'])} on this computer")
        if o["disk"]:
            d = o["disk"]
            used = d.total - d.free
            top.set_description(f"{datasize.fmt(d.free)} free of {datasize.fmt(d.total)} on the disk your meetings "
                                "are saved on.")
            bar = Gtk.LevelBar(min_value=0, max_value=1, value=used / d.total, margin_top=4)
            bar.remove_offset_value("low")
            bar.remove_offset_value("high")
            bar.remove_offset_value("full")
            bar.update_property([Gtk.AccessibleProperty.LABEL], ["Disk space used"])
            top.add(bar)
        for text in o["warnings"]:
            row = Adw.ActionRow(title=text, use_markup=False)
            row.set_title_lines(3)
            icon = Gtk.Image(icon_name="dialog-warning-symbolic")
            icon.add_css_class("warning")
            row.add_prefix(icon)
            top.add(row)
        page.add(top)

        mg = self.group("Meetings", t["meetings"], f"{len(o['meetings'])} meetings. Delete ones you no longer "
                        "need from the list on the left (they go to the Trash).", o["meetings_dir"],
                        "Open the meetings folder")
        icons = {"audio": "audio-x-generic-symbolic", "transcript": "text-x-generic-symbolic",
                 "versions": "document-open-recent-symbolic", "chat": "user-available-symbolic",
                 "other": "folder-documents-symbolic"}
        for key, label in datasize.MEETING_PARTS:
            if o["meeting_parts"][key]:
                mg.add(self.row(label, o["meeting_parts"][key], icon=icons[key]))
        page.add(mg)

        if o["meetings"]:
            big = Adw.PreferencesGroup(title="Largest meetings")
            for folder, sizes in sorted(o["meetings"].items(), key=lambda kv: -kv[1]["total"])[:LARGEST]:
                row = self.row(meetings.title(folder), sizes["total"],
                               f"{meetings.started(folder):%a %-d %b %Y} · " +
                               datasize.meeting_line(sizes).split(" · ", 1)[-1])
                row.set_activatable(True)
                row.set_subtitle_lines(2)
                row.connect("activated", lambda _r, f=folder: (self.close(), self.open_meeting(f)))
                big.add(row)
            page.add(big)

        unused = sum(s for _, s in o["unused_models"])
        models = self.group("Speech models", t["models"],
                            "Downloaded once and reused by every meeting." +
                            (f" {datasize.fmt(unused)} is in models MintoMeet doesn't use, which you can delete."
                             if unused else ""), datasize.DATA / "models", "Open the models folder")
        for label, path, size, used in o["models"]:
            row = self.row(label, size, None if used else f"Safe to delete: {path}")
            if not used:
                row.set_subtitle_lines(2)
                row.add_suffix(self.open_button(path.parent, "Show it in its folder"))
            models.add(row)
        page.add(models)

        cfg = self.group("Voices and settings", t["settings"], folder=datasize.CONFIG_DIR,
                         tip="Open the settings folder")
        for label, _paths, size in o["settings"]:
            cfg.add(self.row(label, size))
        page.add(cfg)

        app = self.group("The app", t["app"], "Program files. They only change when MintoMeet is reinstalled.",
                         datasize.DATA, "Open the app folder")
        for label, _path, size in o["app"]:
            app.add(self.row(label, size))
        page.add(app)
        self.view.set_content(page)
