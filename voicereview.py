"""Review learned voices: every clip each person's voice was learned from, how well it fits, and ways to
play and remove the ones that are wrong, so recognition gets more accurate over time."""
import pathlib

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

import meetings  # noqa: E402
import voices  # noqa: E402

SHORT = 1.5     # seconds; shorter clips give unreliable voiceprints


def pct(x):
    return f"{max(0.0, x) * 100:.0f}%"


class VoiceReviewDialog(Adw.Dialog):
    """List of learned voices; each opens a page of its clips."""

    def __init__(self, meetings_dir, play, stop, is_playing, focus=None):
        super().__init__(title="Learned voices", content_width=680, content_height=720)
        self.meetings_dir = pathlib.Path(meetings_dir)
        self.play, self.stop, self.is_playing = play, stop, is_playing
        self.playing = None             # (clip id, button) being played
        self.meeting_info = {}
        voices.migrate(self.meetings_dir)
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_child(self.toasts)
        self.list_page = Adw.NavigationPage(title="Learned voices", tag="list")
        self.nav.add(self.list_page)
        self.fill_list()
        self.connect("closed", lambda _d: self.stop_clip())
        if focus and focus in voices.load_clips():
            self.open_person(focus)

    # --- helpers ---------------------------------------------------------------
    def toast(self, message, undo=None):
        t = Adw.Toast(title=message, timeout=6, use_markup=False)
        if undo:
            t.set_button_label("Undo")
            t.connect("button-clicked", lambda _t: undo())
        self.toasts.add_toast(t)

    def meeting(self, name):
        """(folder, "Mon 3 Nov · Title") for a meeting folder name, cached."""
        if name not in self.meeting_info:
            folder = self.meetings_dir / name
            if folder.is_dir():
                when = meetings.started(folder)
                self.meeting_info[name] = (folder, f"{when:%a %-d %b} · {meetings.title(folder)}")
            else:
                self.meeting_info[name] = (None, "A meeting that has been deleted")
        return self.meeting_info[name]

    @staticmethod
    def page_shell(page, content, header_extra=None):
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        if header_extra is not None:
            header.pack_end(header_extra)
        view.add_top_bar(header)
        view.set_content(content)
        page.set_child(view)

    # --- list of people ---------------------------------------------------------
    def fill_list(self):
        prefs = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Voices MeetScribe recognises",
            description="Each voice is learned from the lines you named that person on. Open someone to listen "
                        "to those clips and remove any that are someone else, noise or too short to tell. "
                        "Recognition uses what's left, from the next recording on.")
        prefs.add(group)
        data = voices.load_clips()
        if not data:
            row = Adw.ActionRow(title="No voices learned yet",
                                subtitle="Click a speaker name on a transcript line and pick who it is.")
            row.add_css_class("dim-label")
            group.add(row)
        self.person_rows = {}
        for name in sorted(data, key=str.lower):
            entry = data[name]
            secs = sum(voices.seconds(c) for c in entry["clips"]) + (entry["legacy"]["seconds"] if entry.get("legacy") else 0)
            row = Adw.ActionRow(title=name, activatable=True, use_markup=False)
            row.set_subtitle(f"{len(entry['clips'])} clips · {secs / 60:.1f} min of speech")
            row.add_prefix(Gtk.Image(icon_name="avatar-default-symbolic"))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, n=name: self.open_person(n))
            group.add(row)
            self.person_rows[name] = row
        self.page_shell(self.list_page, prefs)
        # how many clips to check: a little work per person, so done after the window is up
        pending = list(self.person_rows)

        def next_count():
            if not pending:
                return False
            name = pending.pop(0)
            row = self.person_rows.get(name)
            if row is not None and row.get_root() is not None:
                review = voices.review(name)
                bad = sum(c["suspect"] for c in review)
                if bad:
                    row.set_subtitle(f"{row.get_subtitle()} · {bad} to check")
                    icon = Gtk.Image(icon_name="dialog-warning-symbolic", tooltip_text=f"{bad} clips may not be {name}")
                    icon.add_css_class("warning")
                    row.add_suffix(icon)
            return True
        GLib.idle_add(next_count)

    # --- one person --------------------------------------------------------------
    def open_person(self, name):
        self.stop_clip()
        page = Adw.NavigationPage(title=name, tag=f"person:{name}")
        page.connect("hidden", lambda _p: self.stop_clip())
        self.fill_person(page, name)
        self.nav.push(page)

    def fill_person(self, page, name):
        entry = voices.load_clips().get(name) or {"clips": [], "legacy": None}
        review = voices.review(name)
        suspects = sorted((c for c in review if c["suspect"]), key=lambda c: c["fit"] - c["other_fit"])
        good = sorted((c for c in review if not c["suspect"]),
                      key=lambda c: (c["meeting"], c["start"]), reverse=True)
        prefs = Adw.PreferencesPage()
        secs = sum(voices.seconds(c) for c in entry["clips"]) + (entry["legacy"]["seconds"] if entry.get("legacy") else 0)

        summary = Adw.PreferencesGroup(
            title=f"{len(entry['clips'])} clips · {secs / 60:.1f} min of speech",
            description="“Fit” is how much a clip sounds like the rest of this voice; a new recording is named "
                        f"{name} when it fits at least {pct(voices.MATCH)}. Clips are flagged when they fit poorly "
                        "or sound more like someone else. Removing the wrong ones makes recognition more accurate.")
        buttons = Gtk.Box(spacing=8, margin_top=8)
        if suspects:
            drop = Gtk.Button(label=f"Remove {len(suspects)} flagged clip{'s' if len(suspects) > 1 else ''}")
            drop.add_css_class("destructive-action")
            drop.set_tooltip_text("Remove every clip listed under “Clips to check”")
            drop.connect("clicked", lambda _b: self.remove(page, name, {c["id"] for c in suspects}))
            buttons.append(drop)
        forget = Gtk.Button(label="Forget this voice")
        forget.set_tooltip_text(f"Remove all of {name}'s clips. They won't be recognised until you name them again.")
        forget.connect("clicked", lambda _b: self.confirm_forget(page, name))
        buttons.append(forget)
        summary.add(buttons)
        prefs.add(summary)

        if suspects:
            check = Adw.PreferencesGroup(title="Clips to check",
                                         description="Listen and remove the ones that aren't " + name + ".")
            for c in suspects:
                check.add(self.clip_row(page, name, c))
            prefs.add(check)
        rest = Adw.PreferencesGroup(title="Clips" if not suspects else "Other clips")
        if entry.get("legacy"):
            row = Adw.ActionRow(title="Earlier learning", use_markup=False,
                                subtitle=f"{entry['legacy']['seconds'] / 60:.1f} min learned before clips were kept, "
                                         "so it can't be played back. Remove it if this voice is often mistaken "
                                         "for someone else.")
            row.set_subtitle_lines(3)
            row.add_prefix(Gtk.Image(icon_name="document-open-recent-symbolic"))
            row.add_suffix(self.remove_button(lambda: self.remove(page, name, set(), legacy=True),
                                              "Remove the earlier learning"))
            rest.add(row)
        for c in good:
            rest.add(self.clip_row(page, name, c))
        if entry.get("legacy") or good:
            prefs.add(rest)
        self.page_shell(page, prefs)

    def clip_row(self, page, name, c):
        folder, where = self.meeting(c["meeting"])
        dur = voices.seconds(c)
        notes = [f"fit {pct(c['fit'])}"]
        if c["other"] and c["other_fit"] > c["fit"]:
            notes.append(f"sounds more like {c['other']} ({pct(c['other_fit'])})")
        elif c["fit"] < voices.MATCH:
            notes.append("weak match")
        if dur < SHORT:
            notes.append("very short")
        row = Adw.ActionRow(title=f"“{c['text']}”" if c["text"] else "(no words)", use_markup=False)
        row.set_title_lines(2)
        row.set_subtitle(f"{where} · at {meetings.fmt_time(c['start'])} · {dur:.1f} s\n" + " · ".join(notes))
        row.set_subtitle_lines(3)
        play = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER)
        play.add_css_class("flat")
        play.add_css_class("circular")
        audio = folder / ("audio-mic.ogg" if c["stream"] == "You" else "audio-meeting.ogg") if folder else None
        if audio is not None and audio.exists():
            play.set_tooltip_text("Listen to this clip")
            play.connect("clicked", lambda b: self.toggle_clip(c, audio, b))
        else:
            play.set_sensitive(False)
            play.set_tooltip_text("This meeting's audio is no longer saved")
        row.add_prefix(play)
        if c["suspect"]:
            icon = Gtk.Image(icon_name="dialog-warning-symbolic", tooltip_text=" · ".join(notes[1:]) or "Weak match")
            icon.add_css_class("warning")
            row.add_suffix(icon)
        row.add_suffix(self.remove_button(lambda: self.remove(page, name, {c["id"]}), f"Remove this clip from {name}'s voice"))
        return row

    @staticmethod
    def remove_button(action, tip):
        b = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text=tip)
        b.add_css_class("flat")
        b.update_property([Gtk.AccessibleProperty.LABEL], [tip])
        b.connect("clicked", lambda _b: action())
        return b

    # --- actions ------------------------------------------------------------------
    def remove(self, page, name, ids, legacy=False):
        self.stop_clip()
        removed = voices.remove_clips(name, ids, legacy=legacy)
        count = len(removed["clips"]) + (1 if removed.get("legacy") else 0)
        if not count:
            return
        self.after_change(page, name)
        what = "the earlier learning" if legacy and not ids else f"{len(removed['clips'])} clip{'s' if len(removed['clips']) != 1 else ''}"

        def undo():
            voices.restore_clips(name, removed)
            self.after_change(page, name, reopen=True)
        self.toast(f"Removed {what} from {name}'s voice", undo)

    def confirm_forget(self, page, name):
        dialog = Adw.AlertDialog(heading=f"Forget {name}'s voice?",
                                 body="All of their clips are removed and they won't be recognised automatically "
                                      "until you name them on a transcript line again. You can undo this right after.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("forget", "Forget voice")
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, r: r == "forget" and self.forget(page, name))
        dialog.present(self)

    def forget(self, page, name):
        self.stop_clip()
        entry = voices.load_clips().get(name) or {"clips": []}
        removed = voices.remove_clips(name, {c["id"] for c in entry["clips"]}, legacy=True)

        def undo():
            voices.restore_clips(name, removed)
            self.after_change(page, name, reopen=True)
        self.after_change(page, name)
        self.toast(f"Forgot {name}'s voice", undo)

    def after_change(self, page, name, reopen=False):
        """Redraw the list and the person's page (or leave it when the voice is gone)."""
        self.fill_list()
        exists = name in voices.load_clips()
        visible = self.nav.get_visible_page()
        if exists and visible is page:
            self.fill_person(page, name)
        elif not exists and visible is page:
            self.nav.pop()
        elif exists and reopen and visible is self.list_page:
            self.open_person(name)

    # --- playback ----------------------------------------------------------------
    def toggle_clip(self, c, audio, button):
        if self.playing and self.playing[0] == c["id"]:
            self.stop_clip()
            return
        self.stop_clip()
        self.play(audio, c["start"], voices.seconds(c) + 0.4, meetings.fmt_time(c["start"]))
        self.playing = (c["id"], button)
        button.set_icon_name("media-playback-stop-symbolic")
        GLib.timeout_add(300, self.watch_player)

    def watch_player(self):
        if self.playing is None:
            return False
        if not self.is_playing():
            self.playing[1].set_icon_name("media-playback-start-symbolic")
            self.playing = None
            return False
        return True

    def stop_clip(self):
        if self.playing is not None:
            self.playing[1].set_icon_name("media-playback-start-symbolic")
            self.playing = None
            self.stop()
