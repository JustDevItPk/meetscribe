"""Meeting tags in the window: the tag pills under a meeting's title, the tag filter at the top of the meetings
list, and the "Manage tags" window that renames or deletes a tag in every meeting. Tags live in each meeting's
meeting.json (see meetings.tags)."""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

import meetings  # noqa: E402

CSS = """
.tag-pill { border-radius: 999px; padding: 0 4px 0 10px; min-height: 24px; font-size: 12px; font-weight: 600;
            background: alpha(currentColor, 0.08); }
.tag-pill.plain { padding: 0 10px; }
.tag-pill button { min-height: 20px; min-width: 20px; padding: 0; margin: 2px 0; }
.tag-pill.small { min-height: 18px; padding: 0 7px; font-size: 11px; }
.tag-pill.match { background: alpha(#f6d32d, 0.45); }
.tag-add { min-height: 24px; padding: 0 10px; border-radius: 999px; font-size: 12px; }
.tag-filter button.toggle { min-height: 24px; padding: 0 10px; border-radius: 999px; font-size: 12px; }
"""


def pill(text, small=False, match=False, on_remove=None):
    """A rounded tag label; with `on_remove`, it has a small × button."""
    box = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
    box.add_css_class("tag-pill")
    for flag, cls in ((small, "small"), (match, "match"), (on_remove is None, "plain")):
        if flag:
            box.add_css_class(cls)
    box.append(Gtk.Label(label=text, ellipsize=Pango.EllipsizeMode.END, max_width_chars=24, valign=Gtk.Align.CENTER))
    if on_remove is not None:
        x = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER)
        x.add_css_class("flat")
        x.add_css_class("circular")
        x.set_tooltip_text(f"Remove the tag “{text}” from this meeting")
        x.update_property([Gtk.AccessibleProperty.LABEL], [f"Remove tag {text}"])
        x.connect("clicked", lambda _b: on_remove(text))
        box.append(x)
    return box


def pill_row(tags, small=True, matches=()):
    """Tags in a row that wraps, for the meetings list and search results."""
    flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=4, row_spacing=4,
                       max_children_per_line=20, halign=Gtk.Align.START)
    keys = {m.casefold() for m in matches}
    for t in tags:
        child = Gtk.FlowBoxChild(focusable=False)
        child.set_child(pill(t, small=small, match=t.casefold() in keys))
        flow.append(child)
    return flow


class TagEditor(Gtk.Box):
    """The open meeting's tags as pills with ×, and an "Add tag" button suggesting tags used elsewhere."""

    def __init__(self, meetings_dir, on_changed, on_manage):
        super().__init__(spacing=6)
        self.meetings_dir = meetings_dir
        self.on_changed = on_changed        # called with the folder after its tags change
        self.on_manage = on_manage
        self.folder = None
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6, row_spacing=6,
                                max_children_per_line=30, hexpand=True, halign=Gtk.Align.START)
        self.append(self.flow)
        self.popover = Gtk.Popover()
        self.popover.connect("closed", lambda _p: self.entry.set_text(""))
        self.add_btn = Gtk.MenuButton(popover=self.popover, valign=Gtk.Align.CENTER,
                                      child=Adw.ButtonContent(icon_name="list-add-symbolic", label="Add tag"))
        self.add_btn.add_css_class("flat")
        self.add_btn.add_css_class("tag-add")
        self.add_btn.set_tooltip_text("Tag this meeting (for example a client, project or kind of meeting) "
                                      "so you can filter and search by it")
        self.build_popover()
        self.set_folder(None)

    def build_popover(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, width_request=260)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(6)
        title = Gtk.Label(label="Add a tag", xalign=0)
        title.add_css_class("heading")
        box.append(title)
        self.entry = Gtk.SearchEntry(placeholder_text="Type a tag, or pick one below")
        self.entry.connect("search-changed", lambda _e: self.fill_suggestions())
        self.entry.connect("activate", lambda _e: self.commit())
        self.entry.connect("stop-search", lambda _e: self.popover.popdown())
        box.append(self.entry)
        hint = Gtk.Label(label="Press Enter to add. Use a comma to add several at once.", xalign=0, wrap=True)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        box.append(hint)
        self.suggest = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.suggest.add_css_class("boxed-list")
        self.suggest.connect("row-activated", lambda _l, row: self.add(row.tag))
        box.append(Gtk.ScrolledWindow(child=self.suggest, propagate_natural_height=True, max_content_height=240,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER))
        manage = Gtk.Button(child=Adw.ButtonContent(icon_name="document-edit-symbolic", label="Manage tags…"))
        manage.add_css_class("flat")
        manage.connect("clicked", lambda _b: (self.popover.popdown(), self.on_manage()))
        box.append(manage)
        self.popover.set_child(box)
        self.popover.connect("show", lambda _p: (self.fill_suggestions(), GLib.idle_add(self.entry.grab_focus)))

    def set_folder(self, folder):
        self.folder = folder
        holder = self.add_btn.get_parent()
        if holder is not None:
            holder.set_child(None)      # keep the button (and its popover) while the pills are rebuilt
        while (child := self.flow.get_first_child()) is not None:
            self.flow.remove(child)
        self.set_visible(folder is not None)
        if folder is None:
            return
        for t in meetings.tags(folder):
            child = Gtk.FlowBoxChild(focusable=False)
            child.set_child(pill(t, on_remove=self.remove))
            self.flow.append(child)
        holder = Gtk.FlowBoxChild(focusable=False)
        holder.set_child(self.add_btn)
        self.flow.append(holder)

    def fill_suggestions(self):
        while (row := self.suggest.get_first_child()) is not None:
            self.suggest.remove(row)
        if self.folder is None:
            return
        typed = meetings.normalize_tag(self.entry.get_text())
        have = {t.casefold() for t in meetings.tags(self.folder)}
        known = meetings.all_tags(self.meetings_dir)
        words = typed.casefold().split()
        shown = 0
        if typed and typed.casefold() not in have and typed.casefold() not in {k.casefold() for k in known}:
            self.suggest.append(self.row(typed, f"New tag “{typed}”", "list-add-symbolic"))
            shown += 1
        for tag, n in known.items():
            if tag.casefold() in have or not all(w in tag.casefold() for w in words):
                continue
            self.suggest.append(self.row(tag, tag, None, f"{n} meeting{'s' if n != 1 else ''}"))
            shown += 1
        self.suggest.set_visible(shown > 0)

    @staticmethod
    def row(tag, title, icon, subtitle=None):
        row = Adw.ActionRow(title=title, activatable=True, use_markup=False)
        if subtitle:
            row.set_subtitle(subtitle)
        if icon:
            row.add_prefix(Gtk.Image(icon_name=icon))
        row.tag = tag
        return row

    def commit(self):
        """Enter: add what was typed (several, if separated by commas), or the only suggestion."""
        text = self.entry.get_text()
        parts = [meetings.normalize_tag(p) for p in text.split(",")]
        parts = [p for p in parts if p]
        if not parts:
            first = self.suggest.get_row_at_index(0)
            if first is not None:
                self.add(first.tag)
            return
        known = {k.casefold(): k for k in meetings.all_tags(self.meetings_dir)}
        for p in parts:
            self.add(known.get(p.casefold(), p), refresh=False)
        self.after_change()

    def add(self, tag, refresh=True):
        if self.folder is None:
            return
        meetings.add_tag(self.folder, tag)
        if refresh:
            self.after_change()

    def remove(self, tag):
        if self.folder is not None:
            meetings.remove_tag(self.folder, tag)
            self.after_change()

    def after_change(self):
        self.entry.set_text("")
        self.set_folder(self.folder)
        self.fill_suggestions()
        self.on_changed(self.folder)


class TagFilter(Gtk.Box):
    """The "Filter by tag" pills at the top of the meetings list. Choosing several shows meetings with all."""

    def __init__(self, on_changed):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("tag-filter")
        self.on_changed = on_changed
        self.selected = set()       # casefolded tags
        head = Gtk.Box(spacing=6)
        label = Gtk.Label(label="Filter by tag", xalign=0, hexpand=True)
        label.add_css_class("caption-heading")
        label.add_css_class("dim-label")
        head.append(label)
        self.clear_btn = Gtk.Button(label="Clear", visible=False)
        self.clear_btn.add_css_class("flat")
        self.clear_btn.add_css_class("caption")
        self.clear_btn.set_tooltip_text("Show all meetings again")
        self.clear_btn.connect("clicked", lambda _b: self.clear())
        head.append(self.clear_btn)
        self.append(head)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=4, row_spacing=4,
                                max_children_per_line=20, halign=Gtk.Align.START)
        self.append(self.flow)
        self.updating = False
        self.set_visible(False)

    def set_tags(self, counts):
        """Show these tags ({tag: meetings}); a chosen tag that no meeting has any more is dropped."""
        self.updating = True
        while (child := self.flow.get_first_child()) is not None:
            self.flow.remove(child)
        keys = {t.casefold() for t in counts}
        dropped = self.selected - keys
        self.selected &= keys
        for tag, n in counts.items():
            b = Gtk.ToggleButton(label=f"{tag}  {n}", active=tag.casefold() in self.selected)
            b.set_tooltip_text(f"Show only meetings tagged “{tag}”")
            b.connect("toggled", self.on_toggled, tag.casefold())
            child = Gtk.FlowBoxChild(focusable=False)
            child.set_child(b)
            self.flow.append(child)
        self.set_visible(bool(counts))
        self.clear_btn.set_visible(bool(self.selected))
        self.updating = False
        if dropped:
            GLib.idle_add(lambda: self.on_changed() and False)

    def on_toggled(self, btn, key):
        if self.updating:
            return
        (self.selected.add if btn.get_active() else self.selected.discard)(key)
        self.clear_btn.set_visible(bool(self.selected))
        self.on_changed()

    def clear(self):
        self.selected.clear()
        self.on_changed()

    def accepts(self, folder):
        if not self.selected:
            return True
        have = {t.casefold() for t in meetings.tags(folder)}
        return self.selected <= have


class ManageTagsDialog(Adw.Dialog):
    """Rename (or merge) and delete tags across every meeting."""

    def __init__(self, meetings_dir, on_changed):
        super().__init__(title="Manage tags", content_width=480, content_height=560)
        self.meetings_dir = meetings_dir
        self.on_changed = on_changed
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.toasts = Adw.ToastOverlay()
        view.set_content(self.toasts)
        self.set_child(view)
        self.fill()

    def fill(self):
        page = Adw.PreferencesPage()
        counts = meetings.all_tags(self.meetings_dir)
        group = Adw.PreferencesGroup(
            title="Tags",
            description="Edit a name and press ✓ to rename it in every meeting. Renaming to a tag that already "
                        "exists merges the two." if counts else
            "No tags yet. Open a meeting and press “Add tag” under its title.")
        for tag, n in counts.items():
            row = Adw.EntryRow(title=f"{n} meeting{'s' if n != 1 else ''}", text=tag, show_apply_button=True)
            row.connect("apply", self.on_rename, tag)
            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            delete.add_css_class("flat")
            delete.set_tooltip_text(f"Remove “{tag}” from every meeting")
            delete.update_property([Gtk.AccessibleProperty.LABEL], [f"Delete tag {tag}"])
            delete.connect("clicked", lambda _b, t=tag, c=n: self.confirm_delete(t, c))
            row.add_suffix(delete)
            group.add(row)
        page.add(group)
        self.toasts.set_child(page)

    def on_rename(self, row, old):
        new = meetings.normalize_tag(row.get_text())
        if not new or new == old:
            row.set_text(old)
            return
        merged = any(t.casefold() == new.casefold() and t.casefold() != old.casefold()
                     for t in meetings.all_tags(self.meetings_dir))
        n = meetings.rename_tag(self.meetings_dir, old, new)
        self.fill()
        self.on_changed()
        self.toasts.add_toast(Adw.Toast(title=f"“{old}” {'merged into' if merged else 'renamed to'} “{new}” "
                                              f"in {n} meeting{'s' if n != 1 else ''}", use_markup=False))

    def confirm_delete(self, tag, n):
        dialog = Adw.AlertDialog(heading=f"Delete the tag “{tag}”?",
                                 body=f"It is taken off {n} meeting{'s' if n != 1 else ''}. The meetings "
                                      "themselves aren't changed.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete tag")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "delete" and self.delete(tag))
        dialog.present(self)

    def delete(self, tag):
        folders = meetings.delete_tag(self.meetings_dir, tag)
        self.fill()
        self.on_changed()
        toast = Adw.Toast(title=f"Deleted “{tag}”", button_label="Undo", use_markup=False)

        def undo(_t):
            for f in folders:
                meetings.add_tag(f, tag)
            self.fill()
            self.on_changed()
        toast.connect("button-clicked", undo)
        self.toasts.add_toast(toast)
