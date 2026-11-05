"""Global speaker list (names + optional emails) and the dialog that manages it."""
import json
import os
import pathlib
import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

import voices  # noqa: E402

CONFIG_DIR = pathlib.Path(os.environ.get("MEETSCRIBE_CONFIG_DIR", pathlib.Path.home() / ".config/meetscribe"))
FILE = CONFIG_DIR / "speakers.json"
RESERVED = {"you", "them", "audio"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load():
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return [s for s in data if isinstance(s, dict) and s.get("name")]
    except (OSError, ValueError):
        return []


def save(speakers):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(speakers, indent=1), encoding="utf-8")


def validate(name, email, speakers):
    """Return an error message, or None if the new speaker is fine."""
    if not name:
        return "Enter a name."
    if any(c in name for c in ":[]") or "  " in name:
        return "Names can't contain : [ ] or double spaces."
    if name.lower() in RESERVED:
        return f"“{name}” is used by MeetScribe itself. Pick another name."
    if any(s["name"].lower() == name.lower() for s in speakers):
        return f"“{name}” is already in the list."
    if email and not EMAIL_RE.match(email):
        return "That email address doesn't look right."
    return None


class SpeakersDialog(Adw.Dialog):
    """Add or remove the people who usually join your meetings."""

    def __init__(self, on_changed, on_review=None):
        super().__init__(title="Speakers", content_width=480, content_height=560)
        self.on_changed = on_changed
        self.on_review = on_review
        self.speakers = load()

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        view.set_content(page)
        self.set_child(view)

        add_group = Adw.PreferencesGroup(
            title="Add a speaker",
            description="People you add here can be picked for any line of any meeting. Once you pick someone, "
                        "MeetScribe learns their voice and names them automatically next time. Emails are "
                        "optional; the agent uses them when you ask it to write follow-up emails.")
        self.name_row = Adw.EntryRow(title="Name")
        self.email_row = Adw.EntryRow(title="Email (optional)")
        self.email_row.set_input_purpose(Gtk.InputPurpose.EMAIL)
        for row in (self.name_row, self.email_row):
            row.connect("entry-activated", lambda _r: self.on_add())
            row.connect("changed", lambda _r: self.error.set_visible(False))
            add_group.add(row)
        self.add_btn = Gtk.Button(child=Adw.ButtonContent(icon_name="list-add-symbolic", label="Add speaker"),
                                  halign=Gtk.Align.START, margin_top=10)
        self.add_btn.add_css_class("suggested-action")
        self.add_btn.add_css_class("pill")
        self.add_btn.connect("clicked", lambda _b: self.on_add())
        self.error = Gtk.Label(xalign=0, wrap=True, visible=False, margin_top=6)
        self.error.add_css_class("error")
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        footer.append(self.add_btn)
        footer.append(self.error)
        add_group.add(footer)
        page.add(add_group)

        self.list_group = Adw.PreferencesGroup(title="Your speakers")
        if on_review is not None:
            review = Gtk.Button(child=Adw.ButtonContent(icon_name="audio-volume-high-symbolic", label="Review voices…"),
                                valign=Gtk.Align.CENTER)
            review.add_css_class("flat")
            review.set_tooltip_text("Listen to the clips each voice was learned from and remove the wrong ones, "
                                    "so people are recognised more accurately")
            review.connect("clicked", lambda _b: self.on_review(None))
            self.list_group.set_header_suffix(review)
        page.add(self.list_group)
        self.rows = []
        self.refresh()

    def refresh(self):
        for row in self.rows:
            self.list_group.remove(row)
        self.rows = []
        if not self.speakers:
            row = Adw.ActionRow(title="No speakers yet",
                                subtitle="Add the people from your meetings above.")
            row.add_css_class("dim-label")
            self.rows.append(row)
        learned = voices.load()
        for s in self.speakers:
            heard = learned.get(s["name"])
            voice = (f"Voice learned from {round(heard['seconds'] / 60, 1)} min of speech" if heard else
                     "Voice not learned yet: pick them for a line in a transcript")
            row = Adw.ActionRow(title=s["name"], subtitle=f"{s.get('email') or 'No email'}\n{voice}", use_markup=False)
            row.add_prefix(Gtk.Image(icon_name="avatar-default-symbolic"))
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.set_tooltip_text(f"Remove {s['name']} from the list (transcripts keep the name)")
            remove.update_property([Gtk.AccessibleProperty.LABEL], [f"Remove {s['name']}"])
            remove.connect("clicked", lambda _b, name=s["name"]: self.on_remove(name))
            if heard and self.on_review is not None:
                clips = Gtk.Button(icon_name="audio-volume-high-symbolic", valign=Gtk.Align.CENTER)
                clips.add_css_class("flat")
                clips.set_tooltip_text(f"Review the clips {s['name']}'s voice was learned from")
                clips.update_property([Gtk.AccessibleProperty.LABEL], [f"Review {s['name']}'s voice"])
                clips.connect("clicked", lambda _b, name=s["name"]: self.on_review(name))
                row.add_suffix(clips)
            if heard:
                forget = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
                forget.add_css_class("flat")
                forget.set_tooltip_text(f"Forget {s['name']}'s voice (they won't be recognised automatically "
                                        "until you name them again)")
                forget.update_property([Gtk.AccessibleProperty.LABEL], [f"Forget {s['name']}'s voice"])
                forget.connect("clicked", lambda _b, name=s["name"]: self.on_forget(name))
                row.add_suffix(forget)
            row.add_suffix(remove)
            self.rows.append(row)
        for row in self.rows:
            self.list_group.add(row)
        self.list_group.set_description(f"{len(self.speakers)} saved" if self.speakers else "")

    def on_add(self):
        name = " ".join(self.name_row.get_text().split())
        email = self.email_row.get_text().strip()
        problem = validate(name, email, self.speakers)
        if problem:
            self.error.set_text(problem)
            self.error.set_visible(True)
            return
        self.speakers.append({"name": name, "email": email})
        self.speakers.sort(key=lambda s: s["name"].lower())
        save(self.speakers)
        self.name_row.set_text("")
        self.email_row.set_text("")
        self.name_row.grab_focus()
        self.refresh()
        self.on_changed()

    def on_forget(self, name):
        voices.forget(name)
        self.refresh()

    def on_remove(self, name):
        self.speakers = [s for s in self.speakers if s["name"] != name]
        save(self.speakers)
        voices.forget(name)
        self.refresh()
        self.on_changed()
