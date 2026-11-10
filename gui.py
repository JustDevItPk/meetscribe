#!/usr/bin/env python3
"""MintoMeet: record meetings, transcribe them locally, tell speakers apart and chat with an agent about them."""
import datetime
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango  # noqa: E402

import callwatch  # noqa: E402
import agents  # noqa: E402
import agentsetup  # noqa: E402
import chat  # noqa: E402
import datasize  # noqa: E402
import jobs  # noqa: E402
import meetings  # noqa: E402
import migrate  # noqa: E402
import modelcatalog  # noqa: E402
import modellibrary  # noqa: E402
import procstats  # noqa: E402
import search  # noqa: E402
import searchpage  # noqa: E402
import speakers  # noqa: E402
import storage  # noqa: E402
import tagsui  # noqa: E402
import tray  # noqa: E402
import voicereview  # noqa: E402
import voices  # noqa: E402
from version import HOMEPAGE, __version__  # noqa: E402

APP_ID = "dev.mintomeet.app"
APP_DIR = pathlib.Path(__file__).resolve().parent
CONFIG_DIR = pathlib.Path(os.environ.get("MINTOMEET_CONFIG_DIR", pathlib.Path.home() / ".config/mintomeet"))
MEETINGS_DIR = pathlib.Path(os.environ.get("MINTOMEET_MEETINGS_DIR", pathlib.Path.home() / "MintoMeet"))
AUTOSTART = pathlib.Path.home() / ".config/autostart" / f"{APP_ID}.desktop"

OLD_ENGINE_SETTING = {0: "parakeet", 1: "whisper-small", 2: "whisper-medium"}   # settings saved before the model library
LANGUAGES = [
    ("Detect automatically", ""), ("English", "en"), ("Urdu", "ur"), ("Arabic", "ar"), ("Hindi", "hi"),
    ("Chinese", "zh"), ("Spanish", "es"), ("French", "fr"), ("German", "de"), ("Portuguese", "pt"),
    ("Russian", "ru"), ("Turkish", "tr"), ("Japanese", "ja"), ("Korean", "ko"),
]
SUGGESTIONS = [
    "Summarize the meeting",
    "List the action items with owners and deadlines",
    "What decisions were made?",
    "Write a follow-up email to the participants",
    "What questions are still open?",
]
EXPORT_FORMATS = [
    ("Word document", ".docx", "Opens in Word, LibreOffice or Google Docs."),
    ("PDF", ".pdf", "Good for sharing and printing."),
    ("Markdown", ".md", "Plain text with formatting, for notes apps such as Obsidian or Notion."),
]
LINE_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] ([^:\[\]]+?): (.*)$")       # transcript.txt
SCREEN_RE = re.compile(r"^(\d\d:\d\d:\d\d) {2}(.+?) {2}(.*)$")               # how lines look on screen
BASE_LABEL_RE = re.compile(r"^(Them|You|Audio|Speaker \d+)$")               # labels the recorder writes
SPEAKER_N_RE = re.compile(r"^Speaker \d+$")
SAVE_DELAY_MS = 800
SPEAKER_COLOURS = ["#3584e4", "#c061cb", "#e66100", "#2190a4", "#986a44", "#1c71d8", "#a51d2d", "#813d9c"]
NO_AUDIO_AFTER = 15     # seconds of recording before warning about silence
DEFAULT_PREFS = {"background": True, "detect_calls": True, "autostart": False, "speakers": True,
                 "parallel": 0}      # meetings transcribed at once; 0 = chosen for this computer

CSS = """
.timer { font-size: 30px; font-weight: 800; font-variant-numeric: tabular-nums; }
.status-card { padding: 16px 18px; }
.status-title { font-size: 17px; font-weight: 700; }
.meter-label { font-size: 12px; }
.user-bubble { background: var(--accent-bg-color); color: var(--accent-fg-color); border-radius: 16px;
               padding: 8px 14px; }
.assistant-bubble { background: var(--card-bg-color); border-radius: 16px; padding: 10px 14px;
                    box-shadow: 0 1px 2px rgba(0,0,0,0.12); }
.bubble-author { font-size: 11px; font-weight: 700; }
.meeting-title { font-size: 20px; font-weight: 800; }
.model-tag > button { min-height: 26px; padding: 0 12px; border-radius: 999px; font-size: 12px; font-weight: 600;
                      background: alpha(var(--accent-bg-color), 0.14); color: var(--accent-color); }
.model-badge { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px;
               background: alpha(var(--accent-bg-color), 0.14); color: var(--accent-color); }
.model-tag.earlier > button { background: alpha(var(--warning-bg-color), 0.2); color: var(--warning-color); }
.version-bar { padding: 8px 12px; }
.version-row { padding: 4px 6px; }
.composer { background: var(--view-bg-color); border-radius: 20px; padding: 8px 8px 8px 14px;
            border: 1px solid alpha(currentColor, 0.15); box-shadow: 0 1px 3px rgba(0,0,0,0.07); }
.composer:focus-within { border-color: alpha(var(--accent-bg-color), 0.7); }
.composer textview, .composer textview > text { background: transparent; }
.composer textview { font-size: 15px; }
.composer-placeholder { font-size: 15px; }
.composer .send { min-width: 34px; min-height: 34px; padding: 0; }
.size-note { font-size: 12px; }
.composer scrollbar slider { min-height: 12px; }    /* else the scrollbar keeps the box ~2 lines tall */
"""


def level_fraction(rms):
    """Map an RMS level (0..1) to a 0..1 meter position on a log scale."""
    return max(0.0, min(1.0, (math.log10(max(rms, 1e-5)) + 4.0) / 3.5))


def md_to_markup(md):
    """Small Markdown -> Pango markup converter for chat replies."""
    out, in_code = [], False
    for raw in md.splitlines():
        if raw.strip().startswith("```"):
            in_code = not in_code
            continue
        line = GLib.markup_escape_text(raw)
        if in_code:
            out.append(f"<tt>{line}</tt>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            text = m.group(2).replace("**", "")
            out.append(f"<b><big>{text}</big></b>" if len(m.group(1)) <= 2 else f"<b>{text}</b>")
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"(?<![*\w])\*(?![\s*])([^*]+?)(?<!\s)\*(?![*\w])", r"<i>\1</i>", line)
        line = re.sub(r"`([^`]+)`", r"<tt>\1</tt>", line)
        line = re.sub(r"^(\s*)[-*]\s+", r"\1  •  ", line)
        out.append(line)
    return "\n".join(out)


def set_markdown(label, md):
    markup = md_to_markup(md)
    try:
        Pango.parse_markup(markup, -1, "\0")
        label.set_markup(markup)
    except GLib.Error:
        label.set_text(md)


def secs(stamp):
    h, m, s = (int(x) for x in stamp.split(":"))
    return h * 3600 + m * 60 + s


def load_settings():
    try:
        return json.loads((CONFIG_DIR / "settings.json").read_text())
    except (OSError, ValueError):
        return {}


def save_settings(data):
    """Merge `data` into settings.json (recording settings and preferences share the file)."""
    try:
        merged = load_settings()
        merged.update(data)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (CONFIG_DIR / "settings.json").write_text(json.dumps(merged))
    except OSError:
        pass


def prefs():
    return {**DEFAULT_PREFS, **load_settings().get("prefs", {})}


def set_autostart(enabled):
    if enabled:
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text("[Desktop Entry]\nType=Application\nName=MintoMeet\n"
                             f"Exec={APP_DIR / 'mintomeet'} --background\nIcon=audio-input-microphone\n"
                             "X-GNOME-Autostart-enabled=true\nNoDisplay=false\n")
    elif AUTOSTART.exists():
        AUTOSTART.unlink()


class Window(Adw.ApplicationWindow):
    def __init__(self, app, hooks=None):
        super().__init__(application=app, title="MintoMeet", default_width=1360, default_height=840)
        self.hooks = hooks          # the App when running for real (tray, notifications); None in tests
        self.out_dir = None         # the meeting on screen
        self.chat = None
        # What the status card shows: the recorder (downloading, loading, recording) while a call is recorded,
        # else "job" when the meeting on screen is being transcribed, else idle, done, cancelled or error.
        self.state = "idle"
        self.rec_phase = "loading"  # the recorder's phase while it captures
        self.base = ("idle", None)  # the card's state for the meeting on screen when nothing is transcribing
        self.deferred = False
        # Every transcription runs in the background (see jobs.py): the recording, what's left of it after
        # you stop, and "Transcribe again". The window stays free, several meetings can be transcribed at once,
        # and each can be paused and resumed.
        self.jobs = jobs.Jobs(APP_DIR / "venv/bin/python", APP_DIR / "worker.py", on_change=self.on_job_change,
                              on_output=self.on_job_output, on_finished=self.on_job_finished,
                              post=lambda fn, *a: GLib.idle_add(fn, *a), limit=prefs()["parallel"],
                              extra_args=lambda: [] if prefs()["speakers"] else ["--no-speakers"])
        self.usage_job = None       # the job whose CPU and memory the status card shows
        self.row_info = {}          # meeting folder -> its sidebar row's info label (updated as jobs progress)
        self.record_start = 0.0
        self.peaks = {"them": 0.0, "you": 0.0}
        self.heard = {"them": False, "you": False}
        self.quitting = False
        self.quit_dialog = None
        self.reply_label = None
        self.reply_text = ""
        self.loading_transcript = False
        self.save_timer = 0
        self.speaker_map = {}       # "HH:MM:SS|text start" -> assigned name, for the current meeting
        self.live_names = {}        # "Speaker 2" -> "Priya" while recording, so new lines follow your choice
        self.speaker_popover = None
        self.downloads = None       # modellibrary.Downloads, made when the library is first opened
        self.show_sizes = bool(load_settings().get("show_sizes", False))    # data sizes shown everywhere
        self.agent_cfg = agents.load_config()   # which AI answers in Ask Agent
        self.bulk_marks = []          # TextMarks at the start of Ctrl-clicked transcript lines
        self.bulk_anchor = None       # last Ctrl-clicked line, for Shift+click ranges
        self.bulk_popover = None
        self.segments = []
        self.bookmarks = []
        self.bm_rows = []
        self.player = None
        self.usage = None           # procstats.Usage for the running recorder
        self.run_model = None       # model id the running recorder uses
        self.switching = None       # (model id, progress text) while a live model switch is loading
        self.viewing = None         # earlier transcript version shown read-only, or None for the current one
        self.export_dialog = None
        self.retx_dialog = None
        self.find_matches = []      # (start, end) buffer offsets of the transcript find bar's matches
        self.find_current = -1
        self.find_timer = 0
        self.chat_widgets = []      # one widget per Ask Agent message, to scroll to a search result
        self.came_from_search = False
        self.syncing_search = False

        provider = Gtk.CssProvider()
        provider.load_from_string(CSS + tagsui.CSS + searchpage.CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.split = Adw.OverlaySplitView(min_sidebar_width=250, max_sidebar_width=320, sidebar_width_fraction=0.2)
        self.toasts.set_child(self.split)
        self.split.set_sidebar(self.build_sidebar())
        view = Adw.ToolbarView()
        self.split.set_content(view)
        view.add_top_bar(self.build_header())
        self.banner = Adw.Banner(title="", revealed=False)
        view.add_top_bar(self.banner)

        # Two columns: controls on the left, transcript / chat on the right.
        columns = Gtk.Box(spacing=18)
        for side in ("top", "bottom", "start", "end"):
            getattr(columns, f"set_margin_{side}")(16)
        # the meeting view, or the Search page in its place
        self.main_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.main_stack.add_named(columns, "meeting")
        self.search_page = searchpage.SearchPage(
            MEETINGS_DIR, open_hit=self.open_search_hit, open_meeting=self.open_search_meeting,
            on_back=self.close_search,
            extra_folders=lambda: [self.rec.folder] if self.rec is not None else [])
        for side in ("top", "bottom", "start", "end"):
            getattr(self.search_page, f"set_margin_{side}")(16)
        self.main_stack.add_named(self.search_page, "search")
        view.set_content(self.main_stack)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_end=4)
        left.append(self.build_status_card())
        self.settings_group = self.build_settings()
        left.append(self.settings_group)
        left.append(self.build_start_button())
        left.append(self.build_extra_buttons())
        left.append(self.build_bookmarks())
        left_scroll = Gtk.ScrolledWindow(child=left, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         propagate_natural_height=True)
        left_scroll.set_size_request(380, -1)
        columns.append(left_scroll)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        right.append(self.build_title())
        right.append(self.build_results())
        columns.append(right)

        medium = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1200sp"))
        medium.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(medium)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
        narrow.add_setter(self.split, "collapsed", True)
        narrow.add_setter(columns, "orientation", Gtk.Orientation.VERTICAL)
        self.add_breakpoint(narrow)

        self.apply_saved_settings()
        self.connect("close-request", self.on_close_request)
        GLib.timeout_add_seconds(1, self.on_tick)
        self.set_state("idle")
        self.refresh_sidebar()
        self.jobs.restore(MEETINGS_DIR)     # transcriptions paused or interrupted last time carry on
        self.refresh_sidebar()
        self.update_jobs_button()

    @property
    def rec(self):
        """The job recording a call right now, or None."""
        return self.jobs.capturing()

    @property
    def vjob(self):
        """The transcription job of the meeting on screen (recording, waiting, running or paused), or None."""
        return self.jobs.get(self.out_dir)

    # --- building the UI -------------------------------------------
    @staticmethod
    def icon_label(icon, text):
        return Adw.ButtonContent(icon_name=icon, label=text)

    def build_header(self):
        header = Adw.HeaderBar()
        self.sidebar_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic")
        self.sidebar_btn.set_tooltip_text("Show or hide the list of meetings")
        self.sidebar_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Meetings list"])
        self.split.bind_property("show-sidebar", self.sidebar_btn, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        header.pack_start(self.sidebar_btn)
        self.speakers_btn = Gtk.Button(child=self.icon_label("system-users-symbolic", "Speakers…"))
        self.speakers_btn.set_tooltip_text("Add the people from your meetings (name and optional email), "
                                           "then click a speaker name on a transcript line to say who spoke")
        self.speakers_btn.connect("clicked", lambda _b: self.open_speakers_dialog())
        header.pack_start(self.speakers_btn)
        self.search_btn = Gtk.ToggleButton(child=self.icon_label("system-search-symbolic", "Search"))
        self.search_btn.set_tooltip_text("Search every meeting: what was said, Ask Agent answers, bookmarks, "
                                         "titles, tags, people and dates (Ctrl+Shift+F)")
        self.search_btn.connect("toggled", self.on_search_toggled)
        header.pack_start(self.search_btn)
        self.results_btn = Gtk.Button(child=self.icon_label("go-previous-symbolic", "Back to results"), visible=False)
        self.results_btn.add_css_class("flat")
        self.results_btn.set_tooltip_text("Go back to your search results")
        self.results_btn.connect("clicked", lambda _b: self.open_search(keep_place=True))
        header.pack_start(self.results_btn)
        for name, handler in (("search", lambda: self.open_search()), ("find", lambda: self.open_find())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            self.add_action(action)

        menu = Gio.Menu()
        menu.append("Speech models…", "win.models")
        menu.append("Learned voices…", "win.voices")
        menu.append("Manage tags…", "win.tags")
        menu.append("Storage…", "win.storage")
        menu.append("Preferences", "app.preferences")
        menu.append("About MintoMeet", "app.about")
        menu.append("Quit MintoMeet", "app.quit")
        self.menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        self.menu_btn.set_tooltip_text("Learned voices, preferences and quit")
        models_action = Gio.SimpleAction.new("models", None)
        models_action.connect("activate", lambda _a, _p: self.open_model_library())
        self.add_action(models_action)
        voices_action = Gio.SimpleAction.new("voices", None)
        voices_action.connect("activate", lambda _a, _p: self.open_voice_review())
        self.add_action(voices_action)
        tags_action = Gio.SimpleAction.new("tags", None)
        tags_action.connect("activate", lambda _a, _p: self.open_manage_tags())
        self.add_action(tags_action)
        storage_action = Gio.SimpleAction.new("storage", None)
        storage_action.connect("activate", lambda _a, _p: self.open_storage())
        self.add_action(storage_action)
        self.sizes_content = Adw.ButtonContent(icon_name="drive-harddisk-symbolic", label="")
        self.sizes_btn = Gtk.ToggleButton(child=self.sizes_content, active=self.show_sizes)
        self.sizes_btn.set_tooltip_text("Show how much disk space each meeting, model and file takes")
        self.sizes_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Show data sizes"])
        self.sizes_btn.connect("toggled", self.on_sizes_toggled)
        header.pack_end(self.sizes_btn)
        header.pack_end(self.menu_btn)
        self.jobs_content = Adw.ButtonContent(icon_name="view-list-bullet-symbolic", label="")
        self.jobs_btn = Gtk.MenuButton(child=self.jobs_content, visible=False)
        self.jobs_btn.set_tooltip_text("Meetings being transcribed: see progress, pause, resume or open them")
        self.jobs_popover = Gtk.Popover()
        self.jobs_btn.set_popover(self.jobs_popover)
        self.jobs_popover.connect("notify::visible", lambda p, _x: p.get_visible() and self.fill_jobs_popover())
        header.pack_end(self.jobs_btn)
        self.folder_btn = Gtk.Button(icon_name="folder-symbolic")
        self.folder_btn.set_tooltip_text("Open this meeting's folder (transcript, audio recordings, chat)")
        self.folder_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Open meeting folder"])
        self.folder_btn.connect("clicked", self.on_open_folder)
        header.pack_end(self.folder_btn)
        self.export_btn = Gtk.Button(child=self.icon_label("document-save-as-symbolic", "Export…"))
        self.export_btn.set_tooltip_text("Save this meeting as a Word document, PDF or Markdown file")
        self.export_btn.connect("clicked", lambda _b: self.open_export_dialog())
        header.pack_end(self.export_btn)
        return header

    def build_sidebar(self):
        view = Adw.ToolbarView()
        head = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        head.set_title_widget(Adw.WindowTitle(title="Meetings"))
        view.add_top_bar(head)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(8)
        self.new_btn = Gtk.Button(child=self.icon_label("list-add-symbolic", "New meeting"))
        self.new_btn.set_tooltip_text("Clear the window, ready to record a new meeting")
        self.new_btn.connect("clicked", lambda _b: self.new_meeting())
        box.append(self.new_btn)
        self.tag_filter = tagsui.TagFilter(on_changed=self.refresh_sidebar)
        box.append(self.tag_filter)
        self.meeting_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.meeting_list.add_css_class("navigation-sidebar")
        self.meeting_list.connect("row-activated", self.on_meeting_row)
        box.append(Gtk.ScrolledWindow(child=self.meeting_list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.meeting_empty = Gtk.Label(label="Your recorded meetings will appear here.", wrap=True, vexpand=True,
                                       valign=Gtk.Align.START)
        self.meeting_empty.add_css_class("dim-label")
        box.append(self.meeting_empty)
        self.storage_footer = Gtk.Button(visible=False)
        self.storage_footer.add_css_class("flat")
        self.storage_footer.set_tooltip_text("See what takes space and what you can clean up")
        self.storage_footer.connect("clicked", lambda _b: self.open_storage())
        box.append(self.storage_footer)
        view.set_content(box)
        return view

    def build_title(self):
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        col.append(self.build_title_row())
        self.version_popover = Gtk.Popover()
        self.model_tag = Gtk.MenuButton(popover=self.version_popover, always_show_arrow=True,
                                        halign=Gtk.Align.START, visible=False)
        self.model_tag.add_css_class("model-tag")
        self.model_tag.add_css_class("flat")
        self.model_tag.set_create_popup_func(self.fill_version_popover)
        tags = Gtk.Box(spacing=10)
        tags.append(self.model_tag)
        self.size_label = Gtk.Label(xalign=0, wrap=True, visible=False, hexpand=True)
        self.size_label.add_css_class("dim-label")
        self.size_label.add_css_class("size-note")
        tags.append(self.size_label)
        col.append(tags)
        self.tag_editor = tagsui.TagEditor(MEETINGS_DIR, on_changed=lambda _f: self.refresh_sidebar(),
                                           on_manage=self.open_manage_tags)
        col.append(self.tag_editor)
        return col

    def build_title_row(self):
        row = Gtk.Box(spacing=8)
        self.title_label = Gtk.EditableLabel(text="New meeting", hexpand=True)
        self.title_label.add_css_class("meeting-title")
        self.title_label.set_tooltip_text("Click to rename this meeting")
        self.title_label.connect("notify::editing", self.on_title_editing)
        row.append(self.title_label)
        self.title_hint = Gtk.Image(icon_name="document-edit-symbolic")
        self.title_hint.add_css_class("dim-label")
        self.title_hint.set_tooltip_text("Click the title to rename this meeting")
        row.append(self.title_hint)
        return row

    def build_status_card(self):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("card")
        card.add_css_class("status-card")
        row = Gtk.Box(spacing=14)
        self.status_icon = Gtk.Image(icon_name="audio-input-microphone-symbolic", pixel_size=32)
        self.spinner = Adw.Spinner(visible=False)
        self.spinner.set_size_request(32, 32)
        row.append(self.status_icon)
        row.append(self.spinner)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.status_title = Gtk.Label(xalign=0, wrap=True)
        self.status_title.add_css_class("status-title")
        self.status_detail = Gtk.Label(xalign=0, wrap=True)
        self.status_detail.add_css_class("dim-label")
        text.append(self.status_title)
        text.append(self.status_detail)
        row.append(text)
        self.timer_label = Gtk.Label(label="00:00:00", visible=False, valign=Gtk.Align.CENTER)
        self.timer_label.add_css_class("timer")
        self.timer_label.set_tooltip_text("Recording time")
        row.append(self.timer_label)
        card.append(row)

        self.progress = Gtk.ProgressBar(visible=False, show_text=True)
        card.append(self.progress)
        self.job_controls = Gtk.Box(spacing=8, halign=Gtk.Align.START, visible=False)
        self.pause_btn = Gtk.Button(child=self.icon_label("media-playback-pause-symbolic", "Pause"))
        self.pause_btn.add_css_class("pill")
        self.pause_btn.connect("clicked", lambda _b: self.toggle_pause(self.vjob))
        self.cancel_btn = Gtk.Button(child=self.icon_label("process-stop-symbolic", "Cancel"))
        self.cancel_btn.add_css_class("pill")
        self.cancel_btn.add_css_class("flat")
        self.cancel_btn.set_tooltip_text("Stop transcribing again and keep the transcript this meeting already has")
        self.cancel_btn.connect("clicked", lambda _b: self.confirm_cancel(self.vjob))
        self.job_controls.append(self.pause_btn)
        self.job_controls.append(self.cancel_btn)
        card.append(self.job_controls)
        self.meters = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        self.them_bar, self.them_note = self.add_meter("Meeting audio (other people)")
        self.you_bar, self.you_note = self.add_meter("Your microphone")
        card.append(self.meters)

        self.usage_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        self.usage_box.set_tooltip_text(
            "Counts the recorder and the audio tools it runs (parec, ffmpeg). It runs at low priority, so your "
            "meeting app always gets the CPU first.")
        heading = Gtk.Label(label="Computer resources used by transcription", xalign=0)
        heading.add_css_class("heading")
        self.usage_box.append(heading)
        self.cpu_bar, self.cpu_note = self.add_meter("Processor (CPU)", self.usage_box)
        self.mem_bar, self.mem_note = self.add_meter("Memory (RAM)", self.usage_box)
        for bar in (self.cpu_bar, self.mem_bar):
            for name in ("low", "high", "full"):    # plain fill: these colour "low" as bad, like a battery
                bar.remove_offset_value(name)
        self.sys_note = Gtk.Label(xalign=0, wrap=True)
        self.sys_note.add_css_class("meter-label")
        self.sys_note.add_css_class("dim-label")
        self.usage_box.append(self.sys_note)
        card.append(self.usage_box)
        return card

    def add_meter(self, title, parent=None):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        top = Gtk.Box(spacing=10)
        label = Gtk.Label(label=title, xalign=0, hexpand=True)
        label.add_css_class("meter-label")
        note = Gtk.Label(label="", xalign=1)
        note.add_css_class("meter-label")
        note.add_css_class("dim-label")
        top.append(label)
        top.append(note)
        bar = Gtk.LevelBar(min_value=0, max_value=1, hexpand=True)
        row.append(top)
        row.append(bar)
        (parent or self.meters).append(row)
        return bar, note

    def build_settings(self):
        group = Adw.PreferencesGroup()
        self.settings_expander = Adw.ExpanderRow(title="Recording settings", icon_name="emblem-system-symbolic")
        group.add(self.settings_expander)
        self.engine_rows, self.engine_checks, self.models = [], {}, []
        self.more_models_row = Adw.ActionRow(title="Get more speech models…", activatable=True,
                                             subtitle="See which models suit this computer, and download or remove them.")
        self.more_models_row.add_prefix(Gtk.Image(icon_name="folder-download-symbolic"))
        self.more_models_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.more_models_row.connect("activated", lambda _r: self.open_model_library())
        self.lang_row = Adw.ComboRow(title="Meeting language (Whisper only)",
                                     model=Gtk.StringList.new([n for n, _ in LANGUAGES]))
        self.lang_row.set_tooltip_text("Choosing the language is more reliable than automatic detection.")
        self.lang_row.set_factory(self.plain_factory())
        self.lang_row.set_list_factory(self.plain_factory())
        self.system_row = Adw.SwitchRow(title="Record meeting audio", active=True,
                                        subtitle="What other people say, captured from your computer's audio output.")
        self.mic_row = Adw.SwitchRow(title="Record my microphone", active=True,
                                     subtitle="What you say. Wear headphones so the meeting audio doesn't echo into your mic.")
        self.fill_model_rows()
        for row in (self.lang_row, self.system_row, self.mic_row):
            row.connect("notify::selected" if isinstance(row, Adw.ComboRow) else "notify::active",
                        lambda *_: self.update_settings_summary())
        self.on_engine_changed()
        return group

    @staticmethod
    def plain_factory():
        """List item factory whose labels never ellipsize."""
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, item: item.set_child(Gtk.Label(xalign=0)))
        factory.connect("bind", lambda _f, item: item.get_child().set_label(item.get_item().get_string()))
        return factory

    def fill_model_rows(self):
        """(Re)build Recording settings: one radio row per downloaded speech model, "Get more speech models…",
        then the language and audio-source rows. Keeps the current choice when that model is still there."""
        keep = self.get_engine() if self.engine_checks else None
        for row in (*self.engine_rows, self.more_models_row, self.lang_row, self.system_row, self.mic_row):
            if row.get_parent() is not None:
                self.settings_expander.remove(row)
        self.models = modelcatalog.downloaded()
        specs = modelcatalog.detect()
        self.engine_rows, self.engine_checks = [], {}
        self.engine_anchor = None
        # Models are radio rows rather than a dropdown so names and explanations are never cut off. They join a
        # hidden button's group so they look like radio buttons even when only one model is downloaded.
        anchor = self.engine_anchor = Gtk.CheckButton()
        for m in self.models:
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=anchor)
            check.connect("toggled", self.on_engine_changed)
            info = f"{m.languages}. {modelcatalog.fit(m, specs, True).label}."
            row = Adw.ActionRow(title=m.name, subtitle=info, activatable_widget=check)
            row.add_prefix(check)
            row.info, row.model = info, m
            self.engine_rows.append(row)
            self.engine_checks[m.id] = check
        for row in (*self.engine_rows, self.more_models_row, self.lang_row, self.system_row, self.mic_row):
            self.settings_expander.add_row(row)
        self.set_engine(keep if keep in self.engine_checks else self.fallback_model())
        self.update_size_summary()

    def fallback_model(self):
        """The model to use when the chosen one isn't available: Parakeet, else the first downloaded one."""
        ids = [m.id for m in self.models]
        return modelcatalog.DEFAULT if modelcatalog.DEFAULT in ids else (ids[0] if ids else None)

    def get_engine(self):
        """Id of the speech model chosen in Recording settings (None if no model is downloaded)."""
        return next((mid for mid, c in self.engine_checks.items() if c.get_active()), None)

    def set_engine(self, model_id):
        if model_id in self.engine_checks:
            self.engine_checks[model_id].set_active(True)

    def refresh_models(self):
        """After a model is downloaded or deleted."""
        datasize.invalidate()
        before = self.get_engine()
        self.fill_model_rows()
        if before and self.get_engine() != before:
            save_settings(self.current_settings())
            self.toast(f"{meetings.model_name(before)} was removed, so new meetings use "
                       f"{meetings.model_name(self.get_engine())}.")
        self.update_settings_summary()

    def update_settings_summary(self):
        """One-line summary shown on the collapsed settings row."""
        cfg = self.current_settings()
        m = modelcatalog.get(cfg["model"])
        parts = [m.name if m else "No speech model downloaded"]
        if m and m.engine == "whisper" and m.multilingual:
            parts.append(LANGUAGES[self.lang_row.get_selected()][0])
        sources = [w for w, on in (("meeting audio", cfg["system"]), ("your mic", cfg["mic"])) if on]
        parts.append(" + ".join(sources) if sources else "nothing selected to record")
        self.settings_expander.set_subtitle(" · ".join(parts))

    def build_start_button(self):
        self.start_btn = Gtk.Button(halign=Gtk.Align.CENTER)
        self.start_btn.add_css_class("pill")
        self.start_btn.set_size_request(260, 52)
        self.start_btn.connect("clicked", self.on_start_stop)
        return self.start_btn

    def build_extra_buttons(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        self.bookmark_btn = Gtk.Button(child=self.icon_label("starred-symbolic", "Bookmark this moment"))
        self.bookmark_btn.add_css_class("pill")
        self.bookmark_btn.set_tooltip_text("Mark this moment as important (Ctrl+B). The agent pays extra attention "
                                           "to bookmarked moments, and you can jump back to them.")
        self.bookmark_btn.connect("clicked", lambda _b: self.add_bookmark())
        self.retx_btn = Gtk.Button(child=self.icon_label("view-refresh-symbolic", "Transcribe again…"))
        self.retx_btn.set_tooltip_text("Run a fresh transcription of this meeting's saved audio, for example "
                                       "with the more accurate Whisper medium")
        self.retx_btn.connect("clicked", lambda _b: self.open_retranscribe_dialog())
        box.append(self.bookmark_btn)
        box.append(self.retx_btn)
        return box

    def build_bookmarks(self):
        self.bm_group = Adw.PreferencesGroup(title="Bookmarks",
                                             description="Click one to jump to that moment in the transcript.")
        return self.bm_group

    def build_results(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, vexpand=True)
        self.stack = Adw.ViewStack(vexpand=True)
        box.append(Adw.InlineViewSwitcher(stack=self.stack, halign=Gtk.Align.CENTER))
        box.append(self.stack)
        self.stack.add_titled_with_icon(self.build_transcript_page(), "transcript", "Transcript",
                                        "text-x-generic-symbolic")
        self.stack.add_titled_with_icon(self.build_chat_page(), "chat", "Ask Agent", "user-available-symbolic")
        return box

    def build_transcript_page(self):
        t_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.t_stack = Gtk.Stack(vexpand=True)
        self.t_empty = Adw.StatusPage(icon_name="audio-input-microphone-symbolic", title="No transcript yet",
                                      description="Press Start recording before your meeting begins. "
                                                  "The conversation will appear here as people speak.")
        self.t_empty.add_css_class("compact")
        self.t_stack.add_named(self.t_empty, "empty")
        self.t_buf = Gtk.TextBuffer()
        self.t_buf.create_tag("time", foreground="#8a8a8a", underline=Pango.Underline.SINGLE)
        self.t_buf.create_tag("them", foreground="#3584e4", weight=Pango.Weight.BOLD)
        for i, colour in enumerate(SPEAKER_COLOURS):
            self.t_buf.create_tag(f"spk{i}", foreground=colour, weight=Pango.Weight.BOLD)
        self.t_buf.create_tag("you", foreground="#26a269", weight=Pango.Weight.BOLD)
        self.t_buf.create_tag("speaker-link", underline=Pango.Underline.SINGLE)
        self.t_buf.create_tag("flash", background="#f6d32d")
        self.t_buf.create_tag("bulk", background="rgba(53,132,228,0.18)")
        self.t_buf.create_tag("find", background="rgba(246,211,45,0.45)")
        self.t_buf.create_tag("find-current", background="#ffa348", foreground="#000000")
        self.t_view = Gtk.TextView(buffer=self.t_buf, editable=False, cursor_visible=False,
                                   wrap_mode=Gtk.WrapMode.WORD_CHAR, pixels_below_lines=4)
        for side in ("top", "bottom", "left", "right"):
            getattr(self.t_view, f"set_{side}_margin")(10)
        self.t_buf.connect("changed", self.on_transcript_edited)
        self.t_buf.connect("notify::has-selection", self.update_bulk)
        self.t_buf.connect("mark-set", lambda _b, _it, mk: mk.get_name() in ("insert", "selection_bound") and
                           self.t_buf.get_has_selection() and self.update_bulk())
        esc = Gtk.EventControllerKey()
        esc.connect("key-pressed", lambda _c, key, _code, _mods: key == Gdk.KEY_Escape and bool(self.bulk_marks)
                    and (self.clear_bulk(selection=False) or True))
        self.t_view.add_controller(esc)
        # Decide on press (capture phase, so the text view's own selection drag never starts), act on
        # release (a popover opened while the button is still held is dismissed by that release). A drag
        # gesture rather than a click one, because a click is cancelled by a pixel of movement (touchpads)
        # and the press position is kept, so a live auto-scroll before the release doesn't matter.
        click = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        click.connect("drag-begin", self.on_transcript_pressed)
        click.connect("drag-end", self.on_transcript_released)
        self.t_view.add_controller(click)
        self.transcript_press = None
        hover = Gtk.EventControllerMotion()
        hover.connect("motion", self.on_transcript_hover)
        hover.connect("leave", lambda _c: self.t_view.set_cursor_from_name(None))
        self.t_view.add_controller(hover)
        self.t_stack.add_named(Gtk.ScrolledWindow(child=self.t_view, vexpand=True), "text")

        hint_row = Gtk.Box(spacing=8)
        self.edit_hint = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.edit_hint.add_css_class("dim-label")
        self.edit_hint.add_css_class("meter-label")
        self.save_status = Gtk.Label(xalign=1)
        self.save_status.add_css_class("meter-label")
        hint_row.append(self.edit_hint)
        hint_row.append(self.save_status)
        self.play_box = Gtk.Box(spacing=8, visible=False)
        self.play_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.play_label.add_css_class("meter-label")
        stop_play = Gtk.Button(child=self.icon_label("media-playback-stop-symbolic", "Stop"))
        stop_play.add_css_class("flat")
        stop_play.set_tooltip_text("Stop playback")
        stop_play.connect("clicked", lambda _b: self.stop_playback())
        self.play_box.append(Gtk.Image(icon_name="audio-speakers-symbolic"))
        self.play_box.append(self.play_label)
        self.play_box.append(stop_play)

        self.version_bar = Gtk.Revealer(reveal_child=False)
        bar = Gtk.Box(spacing=10)
        bar.add_css_class("card")
        bar.add_css_class("version-bar")
        bar.append(Gtk.Image(icon_name="document-open-recent-symbolic", valign=Gtk.Align.CENTER))
        self.version_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        bar.append(self.version_label)
        back = Gtk.Button(label="Back to current", valign=Gtk.Align.CENTER)
        back.set_tooltip_text("Show the transcript this meeting currently uses")
        back.connect("clicked", lambda _b: self.show_version(None))
        use = Gtk.Button(label="Use this version", valign=Gtk.Align.CENTER)
        use.add_css_class("suggested-action")
        use.set_tooltip_text("Make this the meeting's transcript again. The current one is kept as an earlier "
                             "version, so you can switch back at any time.")
        use.connect("clicked", lambda _b: self.restore_viewed_version())
        bar.append(back)
        bar.append(use)
        self.version_bar.set_child(bar)
        t_page.append(self.version_bar)
        self.bulk_bar = Gtk.Revealer(reveal_child=False)
        bulk = Gtk.Box(spacing=10)
        bulk.add_css_class("card")
        bulk.add_css_class("version-bar")
        bulk.append(Gtk.Image(icon_name="system-users-symbolic", valign=Gtk.Align.CENTER))
        self.bulk_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        bulk.append(self.bulk_label)
        clear = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        clear.set_tooltip_text("Unselect these chunks (Esc)")
        clear.connect("clicked", lambda _b: self.clear_bulk())
        self.bulk_popover = Gtk.Popover()
        assign = Gtk.MenuButton(label="Bulk assign speaker…", valign=Gtk.Align.CENTER, popover=self.bulk_popover)
        assign.add_css_class("suggested-action")
        assign.set_tooltip_text("Choose one speaker for every selected chunk")
        assign.set_create_popup_func(self.fill_bulk_popover)
        bulk.append(clear)
        bulk.append(assign)
        self.bulk_bar.set_child(bulk)
        t_page.append(self.bulk_bar)
        t_page.append(self.build_find_bar())
        t_page.append(hint_row)
        t_page.append(self.play_box)
        t_page.append(self.t_stack)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.copy_t_btn = Gtk.Button(child=self.icon_label("edit-copy-symbolic", "Copy transcript"))
        self.copy_t_btn.set_tooltip_text("Copy the whole transcript to the clipboard")
        self.copy_t_btn.connect("clicked", lambda _b: self.copy(self.t_text(), "Transcript copied"))
        self.ask_btn = Gtk.Button(child=self.icon_label("user-available-symbolic", "Ask Agent about it"))
        self.ask_btn.add_css_class("suggested-action")
        self.ask_btn.set_tooltip_text("Open the chat to summarize, find action items, draft emails and more")
        self.ask_btn.connect("clicked", lambda _b: self.stack.set_visible_child_name("chat"))
        actions.append(self.copy_t_btn)
        actions.append(self.ask_btn)
        t_page.append(actions)
        return t_page

    def build_find_bar(self):
        """Ctrl+F: find words in the open transcript, with every match highlighted and next / previous."""
        self.find_bar = Gtk.SearchBar(show_close_button=True)
        box = Gtk.Box(spacing=6)
        self.find_entry = Gtk.SearchEntry(placeholder_text="Find in this transcript", width_chars=32)
        self.find_entry.connect("search-changed", lambda _e: self.run_find())
        self.find_entry.connect("activate", lambda _e: self.step_find(1))
        self.find_entry.connect("next-match", lambda _e: self.step_find(1))
        self.find_entry.connect("previous-match", lambda _e: self.step_find(-1))
        self.find_entry.connect("stop-search", lambda _e: self.find_bar.set_search_mode(False))
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, key, _code, mods: key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
                     and bool(mods & Gdk.ModifierType.SHIFT_MASK) and (self.step_find(-1) or True))
        self.find_entry.add_controller(keys)
        box.append(self.find_entry)
        self.find_count = Gtk.Label(width_chars=12)
        self.find_count.add_css_class("dim-label")
        self.find_count.add_css_class("caption")
        box.append(self.find_count)
        for icon, step, tip in (("go-up-symbolic", -1, "Previous match (Shift+Enter)"),
                                ("go-down-symbolic", 1, "Next match (Enter)")):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, s=step: self.step_find(s))
            box.append(b)
        everywhere = Gtk.Button(label="Search all meetings")
        everywhere.add_css_class("flat")
        everywhere.set_tooltip_text("Look for these words in every meeting (Ctrl+Shift+F)")
        everywhere.connect("clicked", lambda _b: self.open_search(self.find_entry.get_text()))
        box.append(everywhere)
        self.find_bar.set_child(box)
        self.find_bar.connect_entry(self.find_entry)
        self.find_bar.connect("notify::search-mode-enabled",
                              lambda bar, _p: bar.get_search_mode() or self.clear_find())
        return self.find_bar

    def open_find(self, text=None, line=None):
        """Show the find bar (with `text`), going to the first match at or after transcript `line`."""
        if self.main_stack.get_visible_child_name() == "search":
            self.search_page.entry.grab_focus()
            return
        self.stack.set_visible_child_name("transcript")
        self.find_bar.set_search_mode(True)
        if text is not None and text != self.find_entry.get_text():
            self.find_entry.set_text(text)
        self.run_find(line=line)
        self.find_entry.grab_focus()
        self.find_entry.set_position(-1)

    def clear_find(self):
        self.t_buf.remove_tag_by_name("find", self.t_buf.get_start_iter(), self.t_buf.get_end_iter())
        self.t_buf.remove_tag_by_name("find-current", self.t_buf.get_start_iter(), self.t_buf.get_end_iter())
        self.find_matches, self.find_current = [], -1
        self.find_count.set_text("")

    def run_find(self, line=None, scroll=True):
        """Highlight every match of the find bar's words; select the one at `line`, near the last one, or
        the first after the cursor."""
        before = self.find_matches[self.find_current][0] if 0 <= self.find_current < len(self.find_matches) else None
        self.clear_find()
        terms = search.plain_terms(self.find_entry.get_text())
        if not terms:
            return
        buf = self.t_buf
        for n in range(buf.get_line_count()):
            ok, start = buf.get_iter_at_line(n)
            if not ok:
                continue
            end = start.copy()
            if not end.ends_line():
                end.forward_to_line_end()
            off = start.get_offset()
            for a, b in search.spans(buf.get_text(start, end, False), terms):
                self.find_matches.append((off + a, off + b))
                buf.apply_tag_by_name("find", buf.get_iter_at_offset(off + a), buf.get_iter_at_offset(off + b))
        if not self.find_matches:
            self.find_count.set_text("No matches")
            return
        if line is not None:
            at = buf.get_iter_at_line(line)[1].get_offset()
        elif before is not None:
            at = before
        else:
            at = buf.get_iter_at_mark(buf.get_insert()).get_offset()
        i = next((k for k, (a, _b) in enumerate(self.find_matches) if a >= at), 0)
        self.select_find(i, scroll)

    def select_find(self, i, scroll=True):
        buf = self.t_buf
        buf.remove_tag_by_name("find-current", buf.get_start_iter(), buf.get_end_iter())
        if not self.find_matches:
            return
        self.find_current = i % len(self.find_matches)
        a, b = self.find_matches[self.find_current]
        buf.apply_tag_by_name("find-current", buf.get_iter_at_offset(a), buf.get_iter_at_offset(b))
        self.find_count.set_text(f"{self.find_current + 1} of {len(self.find_matches)}")
        if scroll:
            self.scroll_transcript_to(buf.get_iter_at_offset(a))

    def scroll_transcript_to(self, it):
        """Scroll so `it` shows in the upper part of the transcript. Through a mark, because right after a
        meeting loads the lines aren't measured yet and scroll_to_iter would land in the wrong place."""
        mark = self.t_buf.get_mark("search-target") or self.t_buf.create_mark("search-target", it, True)
        self.t_buf.move_mark(mark, it)
        self.t_view.scroll_to_mark(mark, 0.1, True, 0.0, 0.35)

    def step_find(self, step):
        if self.find_matches:
            self.select_find(self.find_current + step)
        elif self.find_entry.get_text():
            self.run_find()

    def schedule_find(self):
        """Re-highlight after the transcript changed (new live lines, edits), without jumping around."""
        bar = getattr(self, "find_bar", None)
        if bar is None or not bar.get_search_mode() or self.find_timer:
            return

        def again():
            self.find_timer = 0
            if self.find_bar.get_search_mode():
                self.run_find(scroll=False)
            return False
        self.find_timer = GLib.timeout_add(400, again)

    def build_chat_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.c_stack = Gtk.Stack(vexpand=True)
        # which agent answers, and a way to change it
        bar = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.agent_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.agent_label.add_css_class("dim-label")
        self.agent_label.add_css_class("caption")
        change = Gtk.Button(label="Change…")
        change.add_css_class("flat")
        change.set_tooltip_text("Choose which AI answers: Claude, ChatGPT, Gemini, a model on this computer…")
        change.connect("clicked", lambda _b: self.open_agent_setup())
        bar.append(Gtk.Image(icon_name="user-available-symbolic"))
        bar.append(self.agent_label)
        bar.append(change)
        page.append(bar)

        locked = Adw.StatusPage(icon_name="user-available-symbolic")
        locked.add_css_class("compact")
        self.c_stack.add_named(locked, "locked")
        self.chat_locked = locked

        empty = Adw.StatusPage(icon_name="user-available-symbolic", title="Ask the agent anything about this meeting")
        empty.add_css_class("compact")
        self.chat_empty = empty
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=2, column_spacing=8,
                           row_spacing=8, halign=Gtk.Align.CENTER)
        self.suggestion_btns = []
        for text in SUGGESTIONS:
            b = Gtk.Button(label=text)
            b.add_css_class("pill")
            b.connect("clicked", lambda _b, t=text: self.send_chat(t))
            flow.append(b)
            self.suggestion_btns.append(b)
        start = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.agent_setup_btn = Gtk.Button(label="Set up an AI agent…", halign=Gtk.Align.CENTER)
        self.agent_setup_btn.add_css_class("suggested-action")
        self.agent_setup_btn.add_css_class("pill")
        self.agent_setup_btn.connect("clicked", lambda _b: self.open_agent_setup())
        start.append(self.agent_setup_btn)
        start.append(flow)
        empty.set_child(start)
        self.c_stack.add_named(empty, "empty")

        self.msg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.msg_box, f"set_margin_{side}")(6)
        self.c_scroll = Gtk.ScrolledWindow(child=self.msg_box, vexpand=True,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.c_stack.add_named(self.c_scroll, "messages")
        page.append(self.c_stack)

        page.append(self.build_composer())
        self.refresh_agent_texts()
        return page

    # --- AI agent ------------------------------------------------------------------------------------------
    def refresh_agent_texts(self):
        """Name the chosen agent wherever the chat talks about it."""
        cfg = self.agent_cfg
        name = agents.display_name(cfg)
        issue = chat.agent_problem(cfg)
        self.agent_label.set_text(f"Answers from {agents.describe(cfg)}")
        self.chat_locked.set_title(f"Chat with {name} about your meeting")
        self.chat_locked.set_description("Available once a recording has finished, or after you open a previous "
                                         f"meeting. {name} reads the transcript and answers like a normal chat.")
        if issue:
            self.chat_empty.set_description(f"{issue} Set up an AI agent to ask questions about this meeting: "
                                            "Claude, ChatGPT, Gemini, or a free model on this computer.")
        else:
            self.chat_empty.set_description(f"{name} has the full transcript. Pick a suggestion or type your own "
                                            f"question below. {agents.usage_note(cfg)}")
        self.agent_setup_btn.set_visible(bool(issue))

    def open_agent_setup(self, reason=None, then=None):
        """The "AI agent" window; `then` runs after a successful save (e.g. sending the question you typed)."""
        def saved(cfg):
            self.agent_cfg = cfg
            self.refresh_agent_texts()
            self.toast(f"Ask Agent now uses {agents.describe(cfg)}")
            if then is not None:
                then()
        agentsetup.AgentDialog(on_saved=saved, reason=reason).present(self)

    def build_composer(self):
        """Chat input like other AI chat apps: a rounded box that grows with what you type or paste (then
        scrolls), Enter sends, Shift+Enter starts a new line, send / stop button at the bottom right."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("composer")
        self.entry_buf = Gtk.TextBuffer()
        self.entry = Gtk.TextView(buffer=self.entry_buf, wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
                                  top_margin=4, bottom_margin=4, hexpand=True)
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], ["Message to the agent"])
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_composer_key)
        self.entry.add_controller(keys)
        scroller = Gtk.ScrolledWindow(child=self.entry, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True, max_content_height=220,
                                      min_content_height=28)     # one line; the default minimum is ~2 lines
        # The box grows after the text arrives, but the text view keeps the scroll offset it had while it
        # was smaller (a paste left the top lines out of sight above a blank gap): once the new size is in,
        # bring the scroll position back in range and the cursor into view.
        self.entry_scroller = scroller
        self.entry_buf.connect("changed", lambda _b: GLib.timeout_add(30, self.fit_composer_scroll))
        self.entry_placeholder = Gtk.Label(label="Ask anything about this meeting…", xalign=0,
                                           valign=Gtk.Align.START, margin_top=4, can_target=False)
        self.entry_placeholder.add_css_class("dim-label")
        self.entry_placeholder.add_css_class("composer-placeholder")
        overlay = Gtk.Overlay(child=scroller)
        overlay.add_overlay(self.entry_placeholder)
        card.append(overlay)
        # clicking the padding around the text still puts the cursor in it
        focus = Gtk.GestureClick()
        focus.connect("pressed", lambda *_a: self.entry.get_sensitive() and self.entry.grab_focus())
        card.add_controller(focus)

        row = Gtk.Box(spacing=8)
        self.new_chat_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.END)
        self.new_chat_btn.add_css_class("flat")
        self.new_chat_btn.add_css_class("circular")
        self.new_chat_btn.set_tooltip_text("New chat: clear this conversation and start fresh (transcript is kept)")
        self.new_chat_btn.update_property([Gtk.AccessibleProperty.LABEL], ["New chat"])
        self.new_chat_btn.connect("clicked", self.on_new_chat)
        row.append(self.new_chat_btn)
        row.append(Gtk.Box(hexpand=True))
        self.send_btn = Gtk.Button(icon_name="go-up-symbolic", valign=Gtk.Align.END)
        self.send_btn.add_css_class("circular")
        self.send_btn.add_css_class("send")
        self.send_btn.connect("clicked", lambda _b: self.on_send())
        row.append(self.send_btn)
        card.append(row)
        self.entry_buf.connect("changed", lambda _b: self.update_composer())
        return card

    def fit_composer_scroll(self):
        adj = self.entry_scroller.get_vadjustment()
        page = self.entry_scroller.get_height()
        if adj.get_page_size() != page:
            adj.set_page_size(page)
        adj.set_value(min(adj.get_value(), max(0.0, adj.get_upper() - page)))
        self.entry.scroll_mark_onscreen(self.entry_buf.get_insert())
        return False

    def composer_text(self):
        return self.entry_buf.get_text(self.entry_buf.get_start_iter(), self.entry_buf.get_end_iter(), False)

    def on_composer_key(self, _ctl, key, _code, mods):
        if key not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            return False
        if mods & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK):
            return False                    # Shift+Enter: new line
        if self.chat is not None and self.chat.busy:
            return True                     # don't send while a reply is coming; keep what's typed
        self.on_send()
        return True

    def update_composer(self):
        """Placeholder while empty; send needs some text (stop is always available while replying)."""
        has_text = bool(self.composer_text().strip())
        self.entry_placeholder.set_visible(self.entry_buf.get_char_count() == 0)
        if not (self.chat is not None and self.chat.busy):
            self.send_btn.set_sensitive(getattr(self, "chat_ready", False) and has_text)

    # --- settings --------------------------------------------------
    def apply_saved_settings(self):
        s = load_settings()
        langs = [c for _, c in LANGUAGES]
        chosen = s.get("model") or OLD_ENGINE_SETTING.get(s.get("engine"))
        self.set_engine(chosen if chosen in self.engine_checks else self.fallback_model())
        if s.get("language") in langs:
            self.lang_row.set_selected(langs.index(s["language"]))
        self.system_row.set_active(s.get("system", True))
        self.mic_row.set_active(s.get("mic", True))
        self.settings_expander.set_expanded("model" not in s and "engine" not in s)  # first launch: show the choices
        self.update_settings_summary()

    def current_settings(self):
        return {"model": self.get_engine(),
                "language": LANGUAGES[self.lang_row.get_selected()][1],
                "system": self.system_row.get_active(), "mic": self.mic_row.get_active()}

    def on_engine_changed(self, *_):
        if not hasattr(self, "mic_row") or self.get_engine() is None:
            return      # still building, or mid-switch between radio buttons
        m = modelcatalog.get(self.get_engine())
        self.lang_row.set_sensitive(m.engine == "whisper" and m.multilingual)
        self.update_settings_summary()

    # --- state machine ---------------------------------------------
    def set_state(self, state, detail=None):
        """Single place that decides what every widget shows. While a call is recorded the status card shows
        the recorder; otherwise, when the meeting on screen is being transcribed, it shows that transcription."""
        recorder = ("downloading", "loading", "recording")
        if state not in recorder and state != "job":
            self.base = (state, detail)     # what to show again once a recording or transcription is over
        rec, job = self.rec, self.vjob
        if rec is not None:
            if state not in recorder:
                state, detail = self.rec_phase, None
        elif job is not None and state != "error":
            state, detail = "job", None
        elif state in recorder or state == "job":
            state, detail = self.base
        self.state = state
        titles = {
            "idle": ("Ready to record",
                     "Press Start recording before your Zoom, Lark or other call begins. MintoMeet records your "
                     "computer's audio output and, if you want, your microphone."),
            "downloading": ("Downloading the speech model…",
                            "One-time download. Recording has already started, so nothing is lost."),
            "loading": ("Loading the speech model…", "Recording has already started, so nothing is lost."),
            "recording": ("Recording", "The live transcript appears on the right. You can open other meetings "
                                       "or leave this window in the background during your meeting."),
            "done": ("Transcript saved", "Open “Ask Agent” to summarize it, list action items or ask questions."),
            "cancelled": ("Stopped", "Recording stopped before transcription could start."),
            "error": ("Something went wrong", ""),
        }
        if state == "job":
            title, text = self.job_texts(job)
        else:
            title, text = titles[state]
        self.status_title.set_text(title)
        self.status_detail.set_text(detail or text)

        recording = state in ("downloading", "loading", "recording")
        working = state in ("downloading", "loading") or (state == "job" and job.state in ("running", "pausing"))
        self.spinner.set_visible(working)
        self.status_icon.set_visible(not working)
        icon = {"recording": "media-record-symbolic", "done": "emblem-ok-symbolic", "error": "dialog-warning-symbolic",
                "cancelled": "dialog-information-symbolic"}.get(state, "audio-input-microphone-symbolic")
        if state == "job":
            icon = "dialog-warning-symbolic" if job.error else \
                "media-playback-pause-symbolic" if job.state == "paused" else "document-open-recent-symbolic"
        self.status_icon.set_from_icon_name(icon)
        self.timer_label.set_visible(recording)
        self.meters.set_visible(recording)
        if state == "job":
            self.progress.set_visible(bool(job.total))
            if job.total:
                self.progress.set_fraction(job.fraction)
                self.progress.set_text(f"Transcribed {job.done} of {job.total} speech segments")
        else:
            self.progress.set_visible(state == "downloading")
        self.job_controls.set_visible(state == "job")
        if state == "job":
            paused = job.state == "paused"
            self.pause_btn.set_child(self.icon_label("media-playback-start-symbolic" if paused else
                                                     "media-playback-pause-symbolic", "Resume" if paused else "Pause"))
            self.pause_btn.set_tooltip_text(
                "Carry on from where it stopped" if paused else
                "Stop for now; it carries on from where it stopped when you resume, also after you quit MintoMeet")
            self.pause_btn.set_sensitive(job.state in ("queued", "running", "paused"))
            self.cancel_btn.set_visible(job.kind == "again")
        self.watch_usage(rec if rec is not None else job if job is not None and job.proc is not None else None)
        self.settings_group.set_sensitive(rec is None)
        self.new_btn.set_sensitive(rec is None)
        self.bookmark_btn.set_visible(rec is not None)

        self.start_btn.remove_css_class("suggested-action")
        self.start_btn.remove_css_class("destructive-action")
        if rec is not None:
            self.start_btn.set_child(self.icon_label("media-playback-stop-symbolic", "Stop recording"))
            self.start_btn.add_css_class("destructive-action")
            self.start_btn.set_tooltip_text("Stop recording; what's left is transcribed in the background")
        else:
            self.start_btn.set_child(self.icon_label("media-record-symbolic", "Start recording"))
            self.start_btn.add_css_class("suggested-action")
            self.start_btn.set_tooltip_text("Start recording the meeting audio and transcribing it")
        self.refresh_buttons()
        self.update_tag()
        if self.hooks:
            self.hooks.state_changed()

    def job_texts(self, job):
        """Title and explanation for the status card while the meeting on screen is being transcribed."""
        name = meetings.model_name(job.model_id)
        again = job.kind == "again"
        if job.error:
            return "Transcription stopped", f"{job.error} Press Resume to try again."
        if job.state == "pausing":
            return "Pausing…", "Saving what's done so far."
        if job.state == "running":
            return ("Transcribing again…" if again else "Finishing the transcription…",
                    job.phase or f"Using {name}. Keep using MintoMeet as usual: open other meetings, chat or "
                                  "record a new one.")
        if job.state == "queued":
            n = len(self.jobs.running())
            return "Waiting to transcribe", (f"{n} other meeting{'s are' if n != 1 else ' is'} being transcribed. "
                                             f"This one starts with {name} as soon as there's room.")
        if job.state == "paused":
            done = f"{job.fraction:.0%} is done. " if job.total else ""
            return "Transcription paused", (f"{done}Resume carries on with {name} from where it stopped, also after "
                                            "you quit MintoMeet.")
        return "Transcribing…", ""

    def refresh_buttons(self):
        has_text = bool(self.t_text().strip())
        job = self.vjob
        locked = job is not None                            # being transcribed: the transcript will change
        live_here = job is not None and job.kind == "recording"    # no full transcript yet
        chatting = self.chat is not None and self.chat.busy
        have_meeting = self.out_dir is not None and self.out_dir.exists()
        self.copy_t_btn.set_sensitive(has_text)
        self.ask_btn.set_sensitive(has_text and not live_here)
        editable = has_text and not locked and self.out_dir is not None and self.viewing is None
        self.t_view.set_editable(editable)
        self.t_view.set_cursor_visible(editable)
        self.edit_hint.set_visible(has_text)
        self.save_status.set_visible(editable)
        self.edit_hint.set_text(
            "Earlier transcript (read-only). Click a time to hear that moment." if self.viewing is not None else
            "Click a time to hear that moment. Click a speaker name to choose who said it; Ctrl+click several "
            "names (or select across lines) to bulk assign a speaker. Click anywhere else to fix mistakes; "
            "changes save automatically (Ctrl+Z undoes)." if editable else
            "This meeting is being transcribed again; you can edit the text once that has finished. Click a time "
            "to hear that moment." if locked and not live_here else
            "Click a time to hear that moment, or a speaker name to choose who is speaking; Ctrl+click several "
            "names to bulk assign a speaker. Text editing opens once the transcription has finished.")
        self.folder_btn.set_sensitive(have_meeting)
        self.export_btn.set_sensitive(have_meeting and has_text and not live_here)
        has_audio = have_meeting and any((self.out_dir / n).exists() for n in ("audio-meeting.ogg", "audio-mic.ogg"))
        self.retx_btn.set_visible(has_audio and not locked)
        self.title_label.set_sensitive(self.out_dir is not None)
        self.title_hint.set_visible(self.out_dir is not None)
        self.t_stack.set_visible_child_name("text" if has_text else "empty")
        self.t_empty.set_description(
            "Recording. Whisper transcribes after you stop, so there is no live transcript."
            if job is not None and job.state == "capturing" and self.deferred else
            "Transcribing. Lines appear here as they're done." if live_here else
            "Press Start recording before your meeting begins. The conversation will appear here as people speak.")

        chat_ready = has_text and not live_here and self.chat is not None
        if not chat_ready:
            self.c_stack.set_visible_child_name("locked")
        elif self.chat.messages or chatting:
            self.c_stack.set_visible_child_name("messages")
        else:
            self.c_stack.set_visible_child_name("empty")
        self.entry.set_sensitive(chat_ready)      # you can type the next question while the agent replies
        self.new_chat_btn.set_sensitive(chat_ready and not chatting and bool(self.chat.messages))
        for b in self.suggestion_btns:
            b.set_sensitive(chat_ready and not chatting)
        self.chat_ready = chat_ready
        self.send_btn.add_css_class("suggested-action")
        if chatting:
            self.send_btn.set_icon_name("media-playback-stop-symbolic")
            self.send_btn.set_tooltip_text("Stop the reply")
            self.send_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Stop reply"])
            self.send_btn.set_sensitive(True)
        else:
            self.send_btn.set_icon_name("go-up-symbolic")
            self.send_btn.set_tooltip_text("Send (Enter). Shift+Enter for a new line.")
            self.send_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Send message"])
        self.update_composer()

    # --- recording -------------------------------------------------
    def on_start_stop(self, _btn):
        if self.rec is None:
            self.start()
        else:
            self.stop()

    def worker_cmd(self):
        python = APP_DIR / "venv/bin/python"
        if not python.exists():
            self.set_state("error", "The speech engine isn't installed. Run install.sh in the mintomeet folder.")
            return None
        cmd = [str(python), str(APP_DIR / "worker.py")]
        if not prefs()["speakers"]:
            cmd.append("--no-speakers")
        return cmd

    def start(self):
        if self.rec is not None:
            return
        self.flush_transcript_save()
        self.stop_playback()
        self.close_search()
        cfg = self.current_settings()
        self.settings_expander.set_expanded(False)
        if not cfg["system"] and not cfg["mic"]:
            self.toast("Turn on “Record meeting audio”, “Record my microphone”, or both.")
            return
        chosen = modelcatalog.get(cfg["model"])
        if chosen is None:
            self.toast("Download a speech model first: Recording settings → Get more speech models…")
            self.open_model_library()
            return
        cmd = self.worker_cmd()
        if cmd is None:
            return
        save_settings(cfg)
        engine, model = chosen.engine, chosen.model
        self.run_model = chosen.id
        self.switching = None
        self.leave_version()
        folder = MEETINGS_DIR / time.strftime("%Y-%m-%d_%H%M%S")
        meetings.save_meta(folder, started=datetime.datetime.now().isoformat(timespec="seconds"), engine=engine)
        cmd += ["--out", str(folder), "--engine", engine, "--live", "--control"]
        if engine == "whisper":
            cmd += ["--model", model, "--language", cfg["language"]]
        if not cfg["system"]:
            cmd.append("--no-system")
        if not cfg["mic"]:
            cmd.append("--no-mic")
        if os.environ.get("MINTOMEET_SYSTEM_SOURCE"):
            cmd += ["--system-source", os.environ["MINTOMEET_SYSTEM_SOURCE"]]
        self.peaks = {"them": 0.0, "you": 0.0}
        self.heard = {"them": False, "you": False}
        self.deferred = False
        self.rec_phase = "loading"
        self.record_start = time.monotonic()
        self.timer_label.set_text("00:00:00")
        self.out_dir = folder       # show the new meeting (before the job exists, so its first lines land here)
        job = self.jobs.record(folder, cmd, chosen.id, cfg["language"] if engine == "whisper" else None,
                               prefs()["speakers"])
        if job is None:
            self.set_state("error", "Could not start the recorder.")
            return
        self.chat = None
        self.speaker_map = {}
        self.live_names = job.live_names
        self.segments = []
        self.bookmarks = []
        self.render_bookmarks()
        self.clear_chat_view()
        self.loading_transcript = True
        self.clear_bulk()
        self.t_buf.set_text("")
        self.loading_transcript = False
        self.banner.set_revealed(False)
        self.stack.set_visible_child_name("transcript")
        self.set_state("loading")
        self.update_title()
        self.refresh_sidebar()
        self.update_jobs_button()

    def watch_usage(self, job):
        """Measure this job's recorder for the CPU / memory meters (None hides them)."""
        proc = job.proc if job is not None else None
        self.usage_box.set_visible(proc is not None)
        if proc is None:
            self.usage_job = None
            return
        if self.usage_job is job and self.usage is not None and self.usage.pid == proc.pid:
            return
        self.usage_job = job
        self.usage = procstats.Usage(proc.pid)
        for bar, note in ((self.cpu_bar, self.cpu_note), (self.mem_bar, self.mem_note)):
            bar.set_value(0)
            note.set_text("measuring…")
        self.sys_note.set_text("")

    def stop(self):
        """Stop recording. What's left is transcribed in the background; the window is free at once."""
        job = self.rec
        if job is None:
            return
        self.banner.set_revealed(False)
        try:
            meetings.save_meta(job.folder, duration=int(time.monotonic() - self.record_start))
        except OSError:
            pass
        self.switching = None
        self.jobs.stop_capture(job)

    # --- what the background transcriptions report -----------------------------------------------------
    def on_job_output(self, job, line):
        """Every line a recorder prints. The recording's levels, phases and model switches drive the status card;
        transcript lines appear when that meeting is on screen."""
        capturing = job.state == "capturing"
        here = job.folder == self.out_dir
        if line.startswith("LEVEL: "):
            if capturing:
                for part in line[7:].split():
                    key, _, val = part.partition("=")
                    if key in self.peaks:
                        self.update_meter(key, float(val))
        elif line.startswith("STATE: ") and capturing:
            words = line[7:].split()
            word, opts = words[0], dict(w.split("=", 1) for w in words[1:] if "=" in w)
            if word == "downloading":
                self.rec_phase = "downloading"
                self.set_state("downloading")
                if "pct" in opts:
                    self.progress.set_fraction(int(opts["pct"]) / 100)
                    self.progress.set_text(f"Downloading a speech model: {opts['pct']}%")
                else:
                    self.progress.pulse()
                    self.progress.set_text("Downloading a speech model…")
            elif word == "loading" and self.rec_phase == "downloading":
                self.rec_phase = "loading"
                self.set_state("loading")
            elif word == "recording" and "deferred" in words:
                self.deferred = True
                self.rec_phase = "recording"
                self.set_state("recording", "Recording. Whisper will transcribe after you stop, so your computer "
                                            "stays fast during the call.")
            elif word == "recording":
                backlog = int(opts.get("backlog", 0))
                was = self.rec_phase
                self.rec_phase = "recording"
                if backlog > 3:
                    lighter = (" A lighter model catches up faster: click the model tag to switch."
                               if (self.run_model or "").startswith("whisper") else "")
                    self.set_state("recording", f"Transcription is {backlog} segments behind. It will catch up; "
                                                f"nothing is lost.{lighter}")
                elif was != "recording" or self.status_detail.get_text().startswith("Transcription is"):
                    self.set_state("recording")
        elif line.startswith("SWITCH: "):
            if capturing:
                self.on_switch(line[8:].split(" ", 2))
        elif line.startswith("NOTICE: "):
            if (here or capturing) and not line.startswith("NOTICE: Your previous transcript"):
                self.toast(line[8:])
        elif LINE_RE.match(line) and here and job.kind == "recording":
            self.append_transcript_line(line, live=True)
            self.refresh_buttons()
        return False

    def on_job_change(self, job):
        """A transcription started, progressed, paused or finished: update its row, the header and the card."""
        label = self.row_info.get(job.folder)
        if label is not None and job.active:
            label.set_text(job.status() + (f" · {datasize.fmt(datasize.meeting(job.folder)['total'])}"
                                           if self.show_sizes and job.state != "capturing" else ""))
        if job.state == "capturing":
            pass        # the recorder's own lines drive the status card
        elif job.folder == self.out_dir or (self.rec is None and self.state in ("downloading", "loading", "recording")):
            self.set_state(self.state)
        self.update_jobs_button()
        if self.jobs_popover.get_visible():
            self.fill_jobs_popover()

    def on_job_finished(self, job):
        here = job.folder == self.out_dir
        title = meetings.title(job.folder)
        if job.state == "done":
            self.learn_from_map(job.folder)
            if here:
                self.show_transcript_file(job.folder)
                self.chat = chat.Chat(job.folder)
                self.render_chat()
                if job.kind == "again":
                    self.set_state("done", "Transcribed again. Your previous transcript is kept: open it from the "
                                           "“Transcribed with” tag above the transcript.")
                elif self.t_text().strip():
                    self.set_state("done", f"Saved in {self.short(job.folder)}. Open “Ask Agent” to summarize "
                                           "it, list action items or ask questions.")
                else:
                    self.set_state("done", "No speech was detected. Next time, check the level meters to confirm "
                                           "the meeting audio is coming through.")
            else:
                self.toast(f"“{title}” is transcribed.", "Open", lambda f=job.folder: self.load_folder(f))
            if self.hooks and not self.is_visible():
                self.hooks.notify(f"“{title}” is transcribed", "Open MintoMeet to read it or ask about it.")
        elif job.state == "cancelled":
            if here:
                self.load_folder(job.folder) if chat.transcript_path(job.folder) else self.new_meeting()
                self.set_state("done", "Stopped transcribing again. The meeting keeps the transcript it had.")
        elif job.error and not here:
            self.toast(f"Transcribing “{title}” stopped: {job.error}", "Open", lambda f=job.folder: self.load_folder(f))
        elif here:
            self.set_state(self.state)
        self.refresh_sidebar()
        self.update_jobs_button()
        return False

    def update_meter(self, key, rms):
        bar, note = (self.them_bar, self.them_note) if key == "them" else (self.you_bar, self.you_note)
        bar.set_value(level_fraction(rms))
        self.peaks[key] = max(self.peaks[key], rms)
        if rms > 0.003:
            self.heard[key] = True
            note.set_text("hearing audio")
        elif not self.heard[key]:
            note.set_text("silent")

    # --- your controls for transcriptions --------------------------------------------------------------
    def toggle_pause(self, job):
        if job is None:
            return
        if job.state in ("paused",) or job.error:
            self.jobs.resume(job)
        elif self.jobs.can_pause(job):
            self.jobs.pause(job)

    def confirm_cancel(self, job):
        if job is None or job.kind != "again":
            return
        dialog = Adw.AlertDialog(heading=f"Stop transcribing “{meetings.title(job.folder)}” again?",
                                 body="What it has done so far is thrown away. The meeting keeps the transcript it "
                                      "had before.")
        dialog.add_response("keep", "Keep going")
        dialog.add_response("stop", "Stop")
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("keep")
        dialog.connect("response", lambda _d, r: r == "stop" and self.jobs.cancel(job))
        self.cancel_dialog = dialog
        dialog.present(self)

    def update_jobs_button(self):
        """The header button: how many meetings are being transcribed (or wait, or are paused)."""
        active = [j for j in self.jobs.all() if j.state != "capturing"]
        self.jobs_btn.set_visible(bool(active))
        if not active:
            self.jobs_popover.popdown()
            return
        running = sum(1 for j in active if j.state in ("running", "pausing"))
        self.jobs_content.set_label(f"Transcribing {running}" if running else
                                    f"{len(active)} paused" if all(j.state == "paused" for j in active) else
                                    f"{len(active)} waiting")
        self.jobs_content.set_icon_name("media-playback-pause-symbolic" if not running else
                                        "document-open-recent-symbolic")
        self.jobs_btn.update_property([Gtk.AccessibleProperty.LABEL],
                                      [f"Transcriptions: {running} running, {len(active) - running} waiting or paused"])

    def fill_jobs_popover(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, width_request=380)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(10)
        head = Gtk.Label(label="Transcriptions", xalign=0)
        head.add_css_class("heading")
        box.append(head)
        limit = self.jobs.limit()
        note = Gtk.Label(xalign=0, wrap=True, label=f"Up to {limit} meeting{'s' if limit != 1 else ''} at once on "
                                                    "this computer; the others wait their turn. Change it in "
                                                    "Preferences.")
        note.add_css_class("dim-label")
        note.add_css_class("caption")
        box.append(note)
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        for job in self.jobs.all():
            if job.state == "capturing":
                continue
            row = Gtk.ListBoxRow(activatable=True)
            row.folder = job.folder
            line = Gtk.Box(spacing=8, margin_top=8, margin_bottom=8, margin_start=10, margin_end=6)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
            name = Gtk.Label(label=meetings.title(job.folder), xalign=0, ellipsize=Pango.EllipsizeMode.END)
            name.add_css_class("heading")
            what = "Again with" if job.kind == "again" else "With"
            info = Gtk.Label(label=f"{job.status()} · {what} {meetings.model_name(job.model_id)}", xalign=0,
                             wrap=True)
            info.add_css_class("dim-label")
            info.add_css_class("caption")
            texts.append(name)
            texts.append(info)
            if job.total:
                texts.append(Gtk.ProgressBar(fraction=job.fraction))
            line.append(texts)
            paused = job.state == "paused" or bool(job.error)
            btn = Gtk.Button(icon_name="media-playback-start-symbolic" if paused else "media-playback-pause-symbolic",
                             valign=Gtk.Align.CENTER, sensitive=job.state in ("queued", "running", "paused"))
            btn.add_css_class("flat")
            btn.set_tooltip_text("Resume" if paused else "Pause")
            btn.update_property([Gtk.AccessibleProperty.LABEL],
                                [f"{'Resume' if paused else 'Pause'} {meetings.title(job.folder)}"])
            btn.connect("clicked", lambda _b, j=job: self.toggle_pause(j))
            line.append(btn)
            if job.kind == "again":
                stop = Gtk.Button(icon_name="process-stop-symbolic", valign=Gtk.Align.CENTER)
                stop.add_css_class("flat")
                stop.set_tooltip_text("Stop transcribing again")
                stop.update_property([Gtk.AccessibleProperty.LABEL], [f"Stop {meetings.title(job.folder)}"])
                stop.connect("clicked", lambda _b, j=job: (self.jobs_popover.popdown(), self.confirm_cancel(j)))
                line.append(stop)
            row.set_child(line)
            listbox.append(row)
        listbox.connect("row-activated", lambda _l, r: (self.jobs_popover.popdown(), self.load_folder(r.folder)))
        box.append(listbox)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        if any(self.jobs.can_pause(j) for j in self.jobs.all()):
            b = Gtk.Button(label="Pause all")
            b.connect("clicked", lambda _b: [self.jobs.pause(j) for j in self.jobs.all() if self.jobs.can_pause(j)])
            actions.append(b)
        if any(j.state == "paused" for j in self.jobs.all()):
            b = Gtk.Button(label="Resume all")
            b.connect("clicked", lambda _b: [self.jobs.resume(j) for j in self.jobs.all() if j.state == "paused"])
            actions.append(b)
        box.append(actions)
        self.jobs_popover.set_child(box)

    def update_usage(self):
        """Show how much CPU and memory the recorder (and the tools it started) is using right now."""
        job = self.usage_job
        if self.usage is None or job is None or job.proc is None or self.usage.pid != job.proc.pid:
            return
        s = self.usage.sample()
        if s is None:
            return
        cpus, size = s["cpus"], procstats.size
        self.cpu_bar.set_value(min(1.0, s["cores"] / cpus))
        self.cpu_note.set_text(f"{s['cores']:.1f} of {cpus} cores ({s['cores'] / cpus:.0%}) · "
                               f"avg {s['avg_cores']:.1f}")
        total = s["mem_total"] or 1
        self.mem_bar.set_value(min(1.0, s["memory"] / total))
        self.mem_note.set_text(f"{size(s['memory'])} ({s['memory'] / total:.0%} of {size(total)}) · "
                               f"peak {size(s['peak'])}")
        low_mem = s["mem_avail"] < total * 0.1
        busy = s["system_cpu"] > 0.9
        text = (f"Whole computer: CPU {s['system_cpu']:.0%} busy · {size(s['mem_avail'])} memory free · "
                f"{s['processes']} MintoMeet process{'es' if s['processes'] != 1 else ''}")
        if low_mem or busy:
            text += ("\nYour computer is running low on memory." if low_mem else
                     "\nYour computer is very busy, so transcription may fall behind (nothing is lost).")
        self.sys_note.set_text(text)
        for cls in ("dim-label", "warning"):
            self.sys_note.remove_css_class(cls)
        self.sys_note.add_css_class("warning" if low_mem or busy else "dim-label")

    def on_tick(self):
        if self.usage_job is not None:
            self.update_usage()
            if self.show_sizes and int(time.monotonic()) % 5 == 0:
                self.update_meeting_size()      # the recording grows
                self.update_size_summary()
        if self.rec is not None:
            elapsed = int(time.monotonic() - self.record_start)
            self.timer_label.set_text(meetings.fmt_time(elapsed))
            if elapsed >= NO_AUDIO_AFTER:
                cfg = self.current_settings()
                if cfg["system"] and not self.heard["them"]:
                    self.banner.set_title("No meeting audio yet. Make sure the meeting plays through your "
                                          "computer's default speakers or headphones.")
                    self.banner.set_revealed(True)
                elif cfg["mic"] and not self.heard["you"]:
                    self.banner.set_title("Your microphone seems silent. Check that it isn't muted.")
                    self.banner.set_revealed(True)
                else:
                    self.banner.set_revealed(False)
        return True

    # --- meetings list, title, new meeting ---------------------------------
    def refresh_sidebar(self):
        while (child := self.meeting_list.get_first_child()) is not None:
            self.meeting_list.remove(child)
        folders = meetings.list_meetings(MEETINGS_DIR)
        rec = self.rec
        live = rec.folder if rec is not None else None
        if live is not None and live not in folders:
            folders.insert(0, live)
        for job in self.jobs.all():        # a recording paused before any speech has no transcript yet
            if job.folder not in folders and job.folder.is_dir():
                folders.append(job.folder)
        self.row_info = {}
        self.tag_filter.set_tags(meetings.count_tags(meetings.tags(f) for f in folders))
        every = len(folders)
        folders = [f for f in folders if f == live or self.tag_filter.accepts(f)]
        for folder in folders:
            row = Gtk.ListBoxRow()
            row.folder = folder
            box = Gtk.Box(spacing=6, margin_top=4, margin_bottom=4)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            name = Gtk.Label(label=meetings.title(folder), xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
            name.add_css_class("heading")
            job = self.jobs.get(folder)
            text = job.status() if job is not None and job.state != "queued" or folder == live else \
                meetings.describe(folder) + (" · waiting to transcribe" if job is not None else "")
            if self.show_sizes and folder != live:
                text += f" · {datasize.fmt(datasize.meeting(folder)['total'])}"
            info = Gtk.Label(label=text, xalign=0, wrap=True)
            info.add_css_class("dim-label")
            info.add_css_class("caption")
            texts.append(name)
            texts.append(info)
            if job is not None:
                self.row_info[folder] = info
            tags = meetings.tags(folder)
            if tags:
                texts.append(tagsui.pill_row(tags, matches=[t for t in tags if t.casefold() in self.tag_filter.selected]))
            box.append(texts)
            trash = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            trash.add_css_class("flat")
            trash.set_tooltip_text("Move this meeting to the Trash")
            trash.update_property([Gtk.AccessibleProperty.LABEL], [f"Delete {meetings.title(folder)}"])
            trash.connect("clicked", lambda _b, f=folder: self.confirm_delete(f))
            trash.set_sensitive(job is None)
            if job is not None:
                trash.set_tooltip_text("It's being transcribed. Stop or finish that first.")
            box.append(trash)
            row.set_child(box)
            self.meeting_list.append(row)
            if self.out_dir is not None and folder == self.out_dir:
                self.meeting_list.select_row(row)
        self.meeting_empty.set_label("No meetings have all the chosen tags." if every and not folders else
                                     "Your recorded meetings will appear here.")
        self.meeting_empty.set_visible(not folders)
        self.update_size_summary()

    def on_meeting_row(self, _list, row):
        if row.folder != self.out_dir:
            self.load_folder(row.folder)
        self.close_search()
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def confirm_delete(self, folder):
        dialog = Adw.AlertDialog(heading=f"Move “{meetings.title(folder)}” to the Trash?",
                                 body="Its transcript, audio, chat and bookmarks go to the Trash, "
                                      "so you can still restore them from there.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Move to Trash")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "delete" and self.delete_meeting(folder))
        self.delete_dialog = dialog
        dialog.present(self)

    def delete_meeting(self, folder):
        try:
            Gio.File.new_for_path(str(folder)).trash(None)
        except GLib.Error as e:
            self.toast(f"Couldn't move it to the Trash: {e.message}")
            return
        if self.out_dir is not None and folder == self.out_dir:
            self.new_meeting()
        self.refresh_sidebar()
        self.toast("Moved to the Trash")

    def new_meeting(self):
        if self.rec is not None:
            return
        self.flush_transcript_save()
        self.stop_playback()
        self.close_search()
        self.leave_version()
        self.out_dir = None
        self.chat = None
        self.speaker_map = {}
        self.segments = []
        self.bookmarks = []
        self.render_bookmarks()
        self.clear_chat_view()
        self.loading_transcript = True
        self.clear_bulk()
        self.t_buf.set_text("")
        self.loading_transcript = False
        self.stack.set_visible_child_name("transcript")
        self.set_state("idle")
        self.update_title()
        self.refresh_sidebar()

    def update_title(self):
        self.title_label.set_text(meetings.title(self.out_dir) if self.out_dir is not None else "New meeting")
        if self.tag_editor.folder != self.out_dir:
            self.tag_editor.set_folder(self.out_dir)

    def open_manage_tags(self):
        def changed():
            self.tag_editor.set_folder(self.out_dir)
            self.refresh_sidebar()
        tagsui.ManageTagsDialog(MEETINGS_DIR, on_changed=changed).present(self)

    def on_title_editing(self, label, _param):
        if label.get_editing() or self.out_dir is None:
            return
        text = " ".join(label.get_text().split())
        if text and text != meetings.title(self.out_dir):
            meetings.save_meta(self.out_dir, title=text)
            self.refresh_sidebar()
        self.update_title()

    # --- transcript ------------------------------------------------
    def append_transcript_line(self, line, live=False):
        m = LINE_RE.match(line)
        if m and live and m.group(2) in self.live_names:        # you already named this voice during the call
            name = self.live_names[m.group(2)]
            self.speaker_map[self.line_key(m.group(1), m.group(3))] = name
            self.save_speaker_map()
            line = f"[{m.group(1)}] {name}: {m.group(3)}"
            m = LINE_RE.match(line)
        self.loading_transcript = True
        end = self.t_buf.get_end_iter
        if m:
            stamp, who, text = m.groups()
            self.t_buf.insert_with_tags_by_name(end(), stamp, "time")
            self.t_buf.insert(end(), "  ")
            self.t_buf.insert_with_tags_by_name(end(), who, self.speaker_tag(who), "speaker-link")
            self.t_buf.insert(end(), "  ")
            self.t_buf.insert(end(), text + "\n")
        else:
            self.t_buf.insert(end(), line + "\n")
        self.loading_transcript = False
        if live:
            mark = self.t_buf.get_mark("live-end") or self.t_buf.create_mark("live-end", end(), False)
            self.t_buf.move_mark(mark, end())
            self.t_view.scroll_to_mark(mark, 0.0, False, 0.0, 0.0)

    def show_job_lines(self, job):
        """A recording still being transcribed: the lines done so far, with the speaker names you gave."""
        self.flush_transcript_save()
        self.leave_version()
        self.loading_transcript = True
        self.clear_bulk()
        self.t_buf.set_text("")
        self.loading_transcript = False
        self.speaker_map = self.load_speaker_map(job.folder)
        self.segments = self.load_segments(job.folder)
        for line in job.lines or meetings.partial_lines(job.folder):
            m = LINE_RE.match(line)
            name = self.speaker_map.get(self.line_key(m.group(1), m.group(3))) if m else None
            if name and name != m.group(2) and BASE_LABEL_RE.match(m.group(2)):
                line = f"[{m.group(1)}] {name}: {m.group(3)}"
            self.append_transcript_line(line)
        self.t_buf.set_enable_undo(False)
        self.t_buf.set_enable_undo(True)
        self.save_status.set_text("")

    def show_transcript_file(self, folder):
        self.flush_transcript_save()
        self.leave_version()
        path = chat.transcript_path(folder)
        self.loading_transcript = True
        self.clear_bulk()
        self.t_buf.set_text("")
        self.loading_transcript = False
        self.speaker_map = self.load_speaker_map(folder)
        self.segments = self.load_segments(folder)
        changed = False
        if path:
            for line in path.read_text(encoding="utf-8").splitlines():
                m = LINE_RE.match(line)
                name = self.speaker_map.get(self.line_key(m.group(1), m.group(3))) if m else None
                # only fill in lines still carrying the recorder's label, never undo a name you typed later
                if name and name != m.group(2) and BASE_LABEL_RE.match(m.group(2)):
                    line = f"[{m.group(1)}] {name}: {m.group(3)}"
                    changed = True
                if line.strip():
                    self.append_transcript_line(line)
        self.t_buf.set_enable_undo(False)   # forget the load itself, so Ctrl+Z only undoes your edits
        self.t_buf.set_enable_undo(True)
        self.save_status.set_text("")
        if changed:     # speakers picked during the recording: write them into transcript.txt
            self.save_transcript()

    # --- transcript versions: which model made it, and transcripts from earlier runs ----------------
    @staticmethod
    def when_text(dt):
        return f"{dt:%-d %b, %H:%M}" if dt else "time unknown"

    def leave_version(self):
        self.viewing = None
        self.version_bar.set_reveal_child(False)

    def update_tag(self):
        """The pill under the title: which speech model made (or is making) the transcript on screen."""
        self.update_meeting_size()
        tag = self.model_tag
        tag.remove_css_class("earlier")
        job = self.vjob
        if job is not None:
            capturing = job.state == "capturing"
            name = meetings.model_name(self.run_model if capturing else job.model_id)
            if capturing and self.deferred:
                text = f"Recording · {name} transcribes after you stop"
            elif job.state == "paused":
                text = f"Paused · {name}"
            elif job.state == "queued":
                text = f"Waiting · {name}"
            elif job.kind == "again":
                text = f"Transcribing again with {name}"
            else:
                text = f"Transcribing with {name}"
            live = capturing and self.rec_phase == "recording" and not self.deferred
            if live and self.switching:
                mid, progress = self.switching
                text += f" · switching to {meetings.model_name(mid)}{progress}"
            tag.set_label(text)
            tag.set_tooltip_text("Click to switch the speech model without stopping the recording" if live else
                                 "The speech model making this transcript")
            tag.set_sensitive(live)
            tag.set_visible(True)
            return
        if self.out_dir is None or chat.transcript_path(self.out_dir) is None:
            tag.set_visible(False)
            return
        if self.viewing is not None:
            tag.set_label(f"Earlier transcript · {meetings.model_name(self.viewing['model'])}")
            tag.add_css_class("earlier")
        else:
            n = len(meetings.list_versions(self.out_dir))
            more = f" · {n} earlier version{'s' if n > 1 else ''}" if n else ""
            tag.set_label(f"Transcribed with {meetings.model_name(meetings.current_model(self.out_dir)[0])}{more}")
        tag.set_tooltip_text("Which speech model made this transcript. Click to open transcripts made with "
                             "other models.")
        tag.set_sensitive(True)
        tag.set_visible(True)

    def fill_version_popover(self, *_):
        if self.rec is not None and self.vjob is self.rec:
            self.fill_switch_popover()
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, width_request=320)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(6)
        title = Gtk.Label(label="Transcripts of this meeting", xalign=0, margin_bottom=4)
        title.add_css_class("heading")
        box.append(title)
        if self.out_dir is None:
            self.version_popover.set_child(box)
            return
        model, when = meetings.current_model(self.out_dir)
        versions = meetings.list_versions(self.out_dir)
        entries = [(None, model, f"Current transcript · made {self.when_text(when)}")]
        entries += [(v, v["model"], f"Made {self.when_text(v['transcribed'])} · replaced {self.when_text(v['replaced'])}")
                    for v in versions]
        if self.show_sizes:
            current = sum(datasize.tree(self.out_dir / n) for n in meetings.VERSION_FILES)

            def version_size(v):    # a versions/<stamp>/ folder, or an older single backup file
                folder = v["path"].parent
                return datasize.tree(v["path"]) if folder == self.out_dir else datasize.tree(folder)
            entries = [(v, mid, f"{sub} · {datasize.fmt(current if v is None else version_size(v))}")
                       for v, mid, sub in entries]
        for v, mid, sub in entries:
            shown = (v is None and self.viewing is None) or \
                    (v is not None and self.viewing is not None and v["id"] == self.viewing["id"])
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            label = meetings.model_name(mid)
            name = Gtk.Label(label=label[:1].upper() + label[1:], xalign=0)
            name.add_css_class("heading" if shown else "body")
            info = Gtk.Label(label=sub, xalign=0)
            info.add_css_class("dim-label")
            info.add_css_class("caption")
            texts.append(name)
            texts.append(info)
            row = Gtk.Box(spacing=8)
            row.add_css_class("version-row")
            row.append(texts)
            texts.set_hexpand(True)
            if shown:
                row.append(Gtk.Image(icon_name="object-select-symbolic"))
            b = Gtk.Button(child=row)
            b.add_css_class("flat")
            b.set_tooltip_text("Showing now" if shown else "Show this transcript" +
                               ("" if v is None else " (read-only)"))
            b.connect("clicked", lambda _b, ver=v: (self.version_popover.popdown(), self.show_version(ver)))
            box.append(b)
        if not versions:
            note = Gtk.Label(label="No earlier transcripts yet. Use Transcribe again to make one with another model "
                                   "and compare.", xalign=0, wrap=True, max_width_chars=40, margin_top=4)
            note.add_css_class("dim-label")
            note.add_css_class("caption")
            box.append(note)
        if self.retx_btn.get_visible():
            box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
            again = Gtk.Button(child=self.icon_label("view-refresh-symbolic", "Transcribe again with another model…"))
            again.add_css_class("flat")
            again.connect("clicked", lambda _b: (self.version_popover.popdown(), self.open_retranscribe_dialog()))
            box.append(again)
        self.version_popover.set_child(box)

    def show_version(self, version):
        """Show an earlier transcript read-only (None: back to the current one)."""
        if self.out_dir is None or self.vjob is not None:
            return
        if version is None:
            if self.viewing is not None:
                self.show_transcript_file(self.out_dir)
        else:
            try:
                lines = version["path"].read_text(encoding="utf-8").splitlines()
            except OSError as e:
                self.toast(f"Couldn't open that transcript: {e}")
                return
            self.flush_transcript_save()
            self.stop_playback()
            self.viewing = version
            self.segments = self.load_segments(version["path"].parent) if version["segments"] else []
            self.loading_transcript = True
            self.clear_bulk()
            self.t_buf.set_text("")
            self.loading_transcript = False
            for line in lines:
                if line.strip():
                    self.append_transcript_line(line)
            self.t_buf.set_enable_undo(False)
            self.t_buf.set_enable_undo(True)
            self.version_label.set_text(
                f"You're viewing an earlier transcript made with {meetings.model_name(version['model'])} "
                f"({self.when_text(version['transcribed'])}). It's read-only; Ask Agent and Export use the "
                "current transcript.")
            self.version_bar.set_reveal_child(True)
            self.stack.set_visible_child_name("transcript")
        self.render_bookmarks()
        self.refresh_buttons()
        self.update_tag()

    # --- switching the speech model during a live recording ----------------------------------------
    def fill_switch_popover(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, width_request=340)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(6)
        title = Gtk.Label(label="Speech model for the rest of the meeting", xalign=0)
        title.add_css_class("heading")
        box.append(title)
        note = Gtk.Label(label="Recording never stops. The current model keeps transcribing until the new one is "
                               "ready, then it takes over. Earlier lines stay as they are.",
                         xalign=0, wrap=True, max_width_chars=44, margin_bottom=4)
        note.add_css_class("dim-label")
        note.add_css_class("caption")
        box.append(note)
        lang = Gtk.DropDown(model=Gtk.StringList.new([n for n, _ in LANGUAGES]))
        lang.set_selected(self.lang_row.get_selected())
        for m in self.models:
            mid, info = m.id, m.languages
            current = mid == self.run_model
            pending = self.switching is not None and self.switching[0] == mid
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
            head = Gtk.Label(label=meetings.model_name(mid) + (" (in use)" if current else
                                                                " (loading…)" if pending else ""), xalign=0)
            head.add_css_class("heading" if current else "body")
            sub = Gtk.Label(label=info + ".", xalign=0, wrap=True, max_width_chars=44)
            sub.add_css_class("dim-label")
            sub.add_css_class("caption")
            texts.append(head)
            texts.append(sub)
            row = Gtk.Box(spacing=8)
            row.add_css_class("version-row")
            row.append(texts)
            if current:
                row.append(Gtk.Image(icon_name="object-select-symbolic"))
            b = Gtk.Button(child=row, sensitive=not current and self.switching is None)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, k=mid: (self.version_popover.popdown(),
                                                    self.request_switch(k, LANGUAGES[lang.get_selected()][1])))
            box.append(b)
        lang_row = Gtk.Box(spacing=8, margin_top=6)
        lang_label = Gtk.Label(label="Language (Whisper only)", xalign=0, hexpand=True)
        lang_label.add_css_class("caption")
        lang_row.append(lang_label)
        lang_row.append(lang)
        box.append(lang_row)
        self.version_popover.set_child(box)

    def request_switch(self, mid, language):
        """Ask the running recorder to change speech model; capture carries on uninterrupted."""
        rec = self.rec
        if rec is None or rec.proc is None or self.switching is not None:
            return
        m = modelcatalog.get(mid)
        if m is None or m not in self.models or mid == self.run_model:
            return      # only downloaded models: the recorder mustn't start a big download mid-meeting
        engine, model = m.engine, m.model
        try:
            rec.proc.stdin.write(f"ENGINE {engine} {model or '-'} {language or '-'}\n")
            rec.proc.stdin.flush()
        except (OSError, ValueError) as e:
            self.toast(f"Couldn't switch the model: {e}")
            return
        self.switching = (mid, "…")
        self.switch_choice = (mid, language)
        self.update_tag()

    def on_switch(self, words):
        """SWITCH: loading|downloading|done|failed <model id> [details] from the recorder."""
        if len(words) < 2:
            return
        kind, mid, rest = words[0], words[1], (words[2] if len(words) > 2 else "")
        name = meetings.model_name(mid)
        if kind == "loading":
            self.switching = (mid, "…")
        elif kind == "downloading":
            pct = rest.partition("pct=")[2]
            self.switching = (mid, f" (downloading{' ' + pct + '%' if pct else ''})")
        elif kind == "done":
            old = meetings.model_name(self.run_model)
            self.run_model, self.switching = mid, None
            choice = getattr(self, "switch_choice", None)
            if choice and choice[0] == mid:
                self.set_engine(choice[0])      # the settings show what is in use, and the next meeting starts with it
                langs = [c for _, c in LANGUAGES]
                if choice[1] in langs:
                    self.lang_row.set_selected(langs.index(choice[1]))
                save_settings(self.current_settings())
            at = rest.partition("at=")[2]
            when = f" from {meetings.fmt_time(float(at))}" if at else ""
            self.toast(f"Now transcribing with {name}{when}. Earlier lines keep their {old} text.")
        elif kind == "failed":
            self.switching = None
            self.toast(f"Couldn't switch to {name}: {rest or 'unknown error'}. Still using "
                       f"{meetings.model_name(self.run_model)}.")
        self.update_tag()

    def restore_viewed_version(self):
        v = self.viewing
        if v is None or self.out_dir is None or self.vjob is not None:
            return
        old = meetings.model_name(meetings.current_model(self.out_dir)[0])
        try:
            meetings.restore_version(self.out_dir, v["id"])
        except OSError as e:
            self.toast(f"Couldn't switch transcripts: {e}")
            return
        self.show_transcript_file(self.out_dir)
        self.render_bookmarks()
        self.refresh_buttons()
        self.update_tag()
        self.toast(f"Now using the {meetings.model_name(v['model'])} transcript. The {old} one is kept as an "
                   "earlier version.")

    def on_transcript_edited(self, _buf):
        self.schedule_find()        # offsets moved: highlight the find bar's matches again
        if self.loading_transcript or not self.t_view.get_editable():
            return
        self.save_status.set_text("Saving…")
        if self.save_timer:
            GLib.source_remove(self.save_timer)
        self.save_timer = GLib.timeout_add(SAVE_DELAY_MS, self.save_transcript)

    def flush_transcript_save(self):
        """Write a pending edit now (before switching meetings, chatting or closing)."""
        if self.save_timer:
            GLib.source_remove(self.save_timer)
            self.save_transcript()

    def save_transcript(self):
        self.save_timer = 0
        if self.out_dir is None or self.viewing is not None:
            return False
        lines = []
        for line in self.t_text().splitlines():
            m = SCREEN_RE.match(line)
            if m:
                lines.append(f"[{m.group(1)}] {m.group(2).strip()}: {m.group(3)}")
            elif line.strip():
                lines.append(line)
        target = self.out_dir / "transcript.txt"
        original = self.out_dir / "transcript.original.txt"
        try:
            if target.exists() and not original.exists():
                original.write_bytes(target.read_bytes())       # keep the machine transcript once
            tmp = target.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(target)
        except OSError as e:
            self.save_status.set_text("Not saved")
            self.toast(f"Couldn't save the transcript: {e}")
            return False
        self.retag_transcript()
        self.save_status.set_text("All changes saved")
        return False

    def retag_transcript(self):
        """Re-apply the time / speaker colours after edits."""
        buf = self.t_buf
        buf.remove_all_tags(buf.get_start_iter(), buf.get_end_iter())
        for n in range(buf.get_line_count()):
            info = self.line_info(n)
            if info is None:
                continue
            start, _end, m = info
            offset = start.get_offset()
            who_start = offset + len(m.group(1)) + 2
            buf.apply_tag_by_name("time", buf.get_iter_at_offset(offset), buf.get_iter_at_offset(offset + 8))
            for tag in (self.speaker_tag(m.group(2)), "speaker-link"):
                buf.apply_tag_by_name(tag, buf.get_iter_at_offset(who_start),
                                      buf.get_iter_at_offset(who_start + len(m.group(2))))
        self.update_bulk()

    @staticmethod
    def speaker_tag(name):
        """Each person keeps the same colour; you are green, unknown voices plain blue."""
        if name == "You":
            return "you"
        if name == "Them":
            return "them"
        return f"spk{sum(map(ord, name)) % len(SPEAKER_COLOURS)}"

    @staticmethod
    def short(path):
        home = str(pathlib.Path.home())
        return "~" + str(path)[len(home):] if str(path).startswith(home + "/") else str(path)

    def t_text(self):
        return self.t_buf.get_text(self.t_buf.get_start_iter(), self.t_buf.get_end_iter(), False)

    def copy(self, text, message):
        if not text.strip():
            return
        self.get_clipboard().set(text)
        self.toast(message)

    def toast(self, message, button=None, action=None):
        t = Adw.Toast(title=message, timeout=5, use_markup=False)     # names like "F&B" are not markup
        if button:
            t.set_button_label(button)
            t.connect("button-clicked", lambda _t: action())
        self.toasts.add_toast(t)

    # --- playback ------------------------------------------------------
    @staticmethod
    def load_segments(folder):
        try:
            return json.loads((pathlib.Path(folder) / "segments.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def line_audio(self, n):
        """(audio file, start seconds, duration) for transcript line n."""
        info = self.line_info(n)
        if info is None or self.out_dir is None:
            return None
        _s, _e, m = info
        start, mine = secs(m.group(1)), m.group(2) == "You"
        path = self.out_dir / ("audio-mic.ogg" if mine else "audio-meeting.ogg")
        seg = next((g for g in self.segments if int(g["start"]) == start and (g["stream"] == "You") == mine), None)
        if seg:
            return path, seg["start"], seg["end"] - seg["start"] + 0.4
        later = [secs(i[2].group(1)) for k in range(n + 1, n + 8) if (i := self.line_info(k)) and
                 (i[2].group(2) == "You") == mine]
        return path, start, min(max((later[0] - start) if later else 15, 3), 30)

    def play_line(self, n):
        audio = self.line_audio(n)
        if audio:
            self.play(*audio, label=self.line_info(n)[2].group(1))

    def play(self, path, start, duration, label):
        self.stop_playback()
        if not shutil.which("ffplay"):
            self.toast("Playing audio needs ffplay: sudo apt install ffmpeg")
            return
        if not path.exists():
            self.toast("There's no saved audio for that part of the meeting.")
            return
        self.player = subprocess.Popen(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                                        "-ss", f"{max(0.0, start - 0.3):.1f}", "-t", f"{duration:.1f}", str(path)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
        self.playing_label = label
        self.play_label.set_text(f"Playing from {label} ({'your microphone' if 'mic' in path.name else 'meeting audio'})")
        self.play_box.set_visible(True)
        GLib.timeout_add(300, self.check_player)

    def check_player(self):
        if self.player is None:
            return False
        if self.player.poll() is not None:
            self.player = None
            self.play_box.set_visible(False)
            return False
        return True

    def stop_playback(self):
        if self.player is not None:
            if self.player.poll() is None:
                self.player.terminate()
            self.player = None
        self.play_box.set_visible(False)

    # --- bookmarks -------------------------------------------------------
    def add_bookmark(self):
        rec = self.rec
        if rec is None:
            return
        t = round(time.monotonic() - self.record_start, 1)
        here = rec.folder == self.out_dir
        marks = self.bookmarks if here else meetings.load_bookmarks(rec.folder)
        marks.append({"t": t, "note": ""})
        try:
            rec.folder.mkdir(parents=True, exist_ok=True)
            meetings.save_bookmarks(rec.folder, marks)
        except OSError as e:
            self.toast(f"Couldn't save the bookmark: {e}")
        if here:
            self.render_bookmarks()
        self.toast(f"Bookmarked {meetings.fmt_time(t)}")
        if self.hooks:
            self.hooks.state_changed()

    def nearest_line(self, t):
        best = None
        for n in range(self.t_buf.get_line_count()):
            info = self.line_info(n)
            if info and secs(info[2].group(1)) <= t + 1:
                best = n
        return best

    def render_bookmarks(self):
        for row in self.bm_rows:
            self.bm_group.remove(row)
        self.bm_rows = []
        for i, b in enumerate(self.bookmarks):
            n = self.nearest_line(b["t"])
            said = self.line_info(n)[2].group(3) if n is not None else "Nothing transcribed at this point yet."
            row = Adw.ActionRow(title=f"★  {meetings.fmt_time(b['t'])}", subtitle=said, activatable=True)
            row.connect("activated", lambda _r, t=b["t"]: self.jump_to(t))
            play = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER)
            play.add_css_class("flat")
            play.set_tooltip_text("Hear this moment")
            play.connect("clicked", lambda _b, t=b["t"]: self.play(self.out_dir / "audio-meeting.ogg", t - 5, 20,
                                                                    meetings.fmt_time(t)))
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.set_tooltip_text("Remove this bookmark")
            remove.connect("clicked", lambda _b, k=i: self.delete_bookmark(k))
            row.add_suffix(play)
            row.add_suffix(remove)
            self.bm_group.add(row)
            self.bm_rows.append(row)
        self.bm_group.set_visible(bool(self.bookmarks))

    def delete_bookmark(self, index):
        if 0 <= index < len(self.bookmarks):
            del self.bookmarks[index]
            if self.out_dir is not None:
                meetings.save_bookmarks(self.out_dir, self.bookmarks)
            self.render_bookmarks()

    def jump_to(self, t):
        n = self.nearest_line(t)
        if n is None:
            return
        self.stack.set_visible_child_name("transcript")
        start = self.t_buf.get_iter_at_line(n)[1]
        end = start.copy()
        end.forward_to_line_end()
        self.t_buf.apply_tag_by_name("flash", start, end)
        self.t_view.scroll_to_iter(start, 0.1, True, 0.0, 0.3)
        GLib.timeout_add(2500, lambda: self.t_buf.remove_tag_by_name(
            "flash", self.t_buf.get_start_iter(), self.t_buf.get_end_iter()) or False)

    # --- chat --------------------------------------------------------
    def clear_chat_view(self):
        while (child := self.msg_box.get_first_child()) is not None:
            self.msg_box.remove(child)
        self.reply_label = None

    def render_chat(self):
        self.clear_chat_view()
        self.chat_widgets = []
        for m in self.chat.messages if self.chat else []:
            if m["role"] == "user":
                self.add_user_bubble(m["text"])
            else:
                set_markdown(self.add_assistant_bubble(m["text"], m.get("agent") or chat.CLAUDE), m["text"])
            self.chat_widgets.append(self.msg_box.get_last_child())
        self.refresh_buttons()

    def scroll_to_message(self, index):
        """Show Ask Agent message `index` (as in chat.json), briefly highlighted."""
        self.stack.set_visible_child_name("chat")
        if not 0 <= index < len(self.chat_widgets):
            return
        widget = self.chat_widgets[index]

        def go():
            ok, point = widget.compute_point(self.msg_box, Graphene.Point())
            if ok:
                self.c_scroll.get_vadjustment().set_value(max(0, point.y - 12))
            widget.add_css_class("search-flash")
            GLib.timeout_add(2500, lambda: widget.remove_css_class("search-flash") or False)
            return False
        GLib.timeout_add(150, go)       # after the chat has been laid out (and scroll_chat has run)

    def add_user_bubble(self, text):
        label = Gtk.Label(label=text, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0, selectable=True,
                          max_width_chars=60)
        label.add_css_class("user-bubble")
        row = Gtk.Box(halign=Gtk.Align.END, margin_start=80)
        row.append(label)
        self.msg_box.append(row)
        self.scroll_chat()

    def add_assistant_bubble(self, text_for_copy, author_name=None):
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, halign=Gtk.Align.START, margin_end=40)
        author = Gtk.Label(label=author_name or agents.display_name(self.agent_cfg), xalign=0)
        author.add_css_class("bubble-author")
        author.add_css_class("dim-label")
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bubble.add_css_class("assistant-bubble")
        label = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0, selectable=True,
                          max_width_chars=80)
        bubble.append(label)
        copy_btn = Gtk.Button(child=self.icon_label("edit-copy-symbolic", "Copy"), halign=Gtk.Align.START)
        copy_btn.add_css_class("flat")
        copy_btn.set_tooltip_text("Copy this reply")
        copy_btn.connect("clicked", lambda _b: self.copy(label.reply_text, "Reply copied"))
        label.reply_text = text_for_copy
        label.copy_btn = copy_btn
        for w in (author, bubble, copy_btn):
            col.append(w)
        self.msg_box.append(col)
        self.scroll_chat()
        return label

    def scroll_chat(self):
        def go():
            adj = self.c_scroll.get_vadjustment()
            adj.set_value(adj.get_upper())
            return False
        GLib.timeout_add(50, go)

    def on_send(self):
        if self.chat is not None and self.chat.busy:
            self.chat.cancel()
            return
        text = self.composer_text().strip()
        if text:
            self.send_chat(text)

    def send_chat(self, text):
        if self.chat is None or self.chat.busy or not text.strip():
            return
        issue = chat.agent_problem(self.agent_cfg)
        if issue:                          # first use, or the chosen agent isn't ready: set it up, then send
            self.open_agent_setup(reason=issue, then=lambda: self.send_chat(text))
            return
        self.flush_transcript_save()      # the agent should see your corrections
        self.entry_buf.set_text("")
        self.c_stack.set_visible_child_name("messages")
        self.add_user_bubble(text)
        self.reply_text = ""
        self.reply_label = self.add_assistant_bubble("")
        self.reply_label.set_text(f"{agents.display_name(self.agent_cfg)} is thinking…")
        self.reply_label.add_css_class("dim-label")
        self.reply_label.copy_btn.set_visible(False)
        self.chat.send(text, lambda chunk: GLib.idle_add(self.on_chat_delta, chunk),
                       lambda full, err: GLib.idle_add(self.on_chat_done, full, err),
                       participants=self.agent_context(), agent=self.agent_cfg)
        self.refresh_buttons()

    def on_chat_delta(self, chunk):
        if self.reply_label is None:
            return False
        self.reply_text += chunk
        self.reply_label.remove_css_class("dim-label")
        set_markdown(self.reply_label, self.reply_text)
        self.scroll_chat()
        return False

    def on_chat_done(self, full, error):
        label = self.reply_label
        self.reply_label = None
        if label is not None:
            label.remove_css_class("dim-label")
            text = full or self.reply_text
            if text:
                set_markdown(label, text)
                label.reply_text = text
                label.copy_btn.set_visible(True)
            if error:
                note = Gtk.Label(label=error, xalign=0, wrap=True)
                note.add_css_class("error" if error != "Stopped." else "dim-label")
                label.get_parent().append(note)
                if not text:
                    label.set_text("No reply.")
        if error and error != "Stopped.":
            self.toast(f"{agents.display_name(self.agent_cfg)} couldn't answer. See the message in the chat.",
                       "AI agent…", self.open_agent_setup)
        self.refresh_buttons()
        self.entry.grab_focus()
        return False

    def on_new_chat(self, _btn):
        if self.chat is None or self.chat.busy:
            return
        self.chat.reset()
        self.render_chat()
        self.toast("Started a new chat")

    def agent_context(self):
        """Extra notes for the agent: who took part (with emails) and the moments you bookmarked."""
        parts = []
        note = self.participants_note()
        if note:
            parts.append(note)
        if self.bookmarks:
            parts.append("The user bookmarked these moments as important; pay extra attention to what was said "
                         "around them:\n" + "\n".join(f"- {meetings.fmt_time(b['t'])}" for b in self.bookmarks))
        return "\n\n".join(parts)

    # --- speakers ----------------------------------------------------
    def open_speakers_dialog(self):
        if self.speaker_popover is not None:
            self.speaker_popover.popdown()
        self.speakers_dialog = speakers.SpeakersDialog(on_changed=lambda: None, on_review=self.open_voice_review)
        self.speakers_dialog.present(self)

    # --- data sizes ----------------------------------------------------
    def on_sizes_toggled(self, btn):
        self.show_sizes = btn.get_active()
        save_settings({"show_sizes": self.show_sizes})
        self.refresh_sidebar()          # meeting rows, header total and footer
        self.update_meeting_size()

    def update_meeting_size(self):
        folder = self.out_dir
        show = self.show_sizes and folder is not None and folder.exists()
        self.size_label.set_visible(show)
        if show:
            self.size_label.set_text(datasize.meeting_line(datasize.meeting(folder)))

    def update_size_summary(self):
        """Header button label, sidebar footer and model sizes in the settings."""
        if not hasattr(self, "sizes_btn"):
            return
        self.sizes_btn.remove_css_class("warning")
        if not self.show_sizes:
            self.sizes_content.set_label("")
            self.storage_footer.set_visible(False)
            for row in self.engine_rows:
                row.set_subtitle(row.info)
            return
        o = datasize.overview(MEETINGS_DIR)
        self.sizes_content.set_label(datasize.fmt(o["total"]))
        tip = (f"MintoMeet data: {datasize.fmt(o['total'])} (meetings {datasize.fmt(o['totals']['meetings'])}, "
               f"models {datasize.fmt(o['totals']['models'])}). Click to hide sizes.")
        if o["warnings"]:
            self.sizes_btn.add_css_class("warning")
            tip = " ".join(o["warnings"]) + " " + tip
        self.sizes_btn.set_tooltip_text(tip)
        self.storage_footer.set_child(self.icon_label(
            "dialog-warning-symbolic" if o["warnings"] else "drive-harddisk-symbolic",
            f"All meetings: {datasize.fmt(o['totals']['meetings'])} · Storage…"))
        self.storage_footer.set_visible(True)
        for row in self.engine_rows:
            row.set_subtitle(row.info + f"\nOn disk: {datasize.fmt(modelcatalog.size_on_disk(row.model))}")

    def open_storage(self):
        storage.StorageDialog(MEETINGS_DIR, open_meeting=self.open_from_storage).present(self)

    def open_from_storage(self, folder):
        self.load_folder(folder)

    # --- speech model library --------------------------------------------------------
    def open_model_library(self):
        if self.downloads is None:
            self.downloads = modellibrary.Downloads(APP_DIR / "venv/bin/python", APP_DIR / "worker.py",
                                                    self.on_model_downloaded)
        modellibrary.ModelLibraryDialog(self.downloads, in_use=self.model_in_use, can_delete=self.model_cannot_go,
                                        on_changed=self.refresh_models).present(self)

    def model_in_use(self, model_id):
        """Why a model can't be deleted right now (shown as "In use"), or ""."""
        busy = self.jobs.models_in_use()
        if self.rec is not None:
            busy |= set((self.run_model or "").split("+")) | {self.switching[0] if self.switching else None}
        if model_id in busy:
            return "It's transcribing right now. You can delete it when that has finished."
        return ""

    def model_cannot_go(self, model_id):
        if self.model_in_use(model_id):
            return self.model_in_use(model_id)
        if [m.id for m in self.models] == [model_id]:
            return "MintoMeet needs at least one speech model. Download another one first."
        return None

    def on_model_downloaded(self, model_id, ok, error):
        name = meetings.model_name(model_id)
        if ok:
            self.refresh_models()
            self.toast(f"{name} is ready. Choose it in Recording settings.")
        else:
            self.toast(error or f"Couldn't download {name}.")

    def open_voice_review(self, name=None):
        """Review the clips each learned voice was built from (and remove the wrong ones)."""
        dialog = voicereview.VoiceReviewDialog(
            MEETINGS_DIR, play=lambda path, start, dur, label: self.play(path, start, dur, label),
            stop=self.stop_playback, is_playing=lambda: self.player is not None and self.player.poll() is None,
            focus=name, show_sizes=self.show_sizes)
        dialog.connect("closed", lambda _d: getattr(self, "speakers_dialog", None) and self.speakers_dialog.refresh())
        dialog.present(self)

    @staticmethod
    def line_key(stamp, text):
        return f"{stamp}|{text.strip()[:40]}"

    @staticmethod
    def load_speaker_map(folder):
        try:
            return json.loads((pathlib.Path(folder) / "speaker_map.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_speaker_map(self):
        if self.out_dir is None:
            return
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            (self.out_dir / "speaker_map.json").write_text(json.dumps(self.speaker_map, indent=1), encoding="utf-8")
        except OSError as e:
            self.toast(f"Couldn't save the speaker choice: {e}")

    def line_info(self, n):
        """(start iter, end iter, regex match) for transcript line n, or None if it isn't a speaker line."""
        if n is None:
            return None
        ok, start = self.t_buf.get_iter_at_line(n)
        if not ok:
            return None
        end = start.copy()
        if not end.ends_line():
            end.forward_to_line_end()
        m = SCREEN_RE.match(self.t_buf.get_text(start, end, False))
        return (start, end, m) if m else None

    def transcript_hit(self, x, y):
        """What a point on the transcript (widget coords) is over: ("time"|"speaker", line, match), or None."""
        bx, by = self.t_view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        ok, it = self.t_view.get_iter_at_location(bx, by)
        if not ok:
            return None
        line = it.get_line()
        info = self.line_info(line)
        if info is None:
            return None
        start, _end, m = info
        col = it.get_offset() - start.get_offset()
        if col < len(m.group(1)):
            return "time", line, m
        who_start = len(m.group(1)) + 2
        if who_start - 1 <= col <= who_start + len(m.group(2)):     # the name plus the space either side
            return "speaker", line, m
        return None

    def on_transcript_hover(self, _ctl, x, y):
        self.t_view.set_cursor_from_name("pointer" if self.transcript_hit(x, y) else None)

    def on_transcript_pressed(self, gesture, x, y):
        event = gesture.get_current_event()
        if event is not None and event.get_surface() != self.get_surface():
            gesture.set_state(Gtk.EventSequenceState.DENIED)    # a press inside the popover, not on the text
            return
        hit = self.transcript_hit(x, y)
        mods = gesture.get_current_event_state()
        if hit and mods & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
            # Ctrl+click toggles a chunk for bulk assign, Shift+click adds the range from the last one
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.transcript_press = None
            self.toggle_bulk(hit[1], extend=bool(mods & Gdk.ModifierType.SHIFT_MASK))
            return
        self.transcript_press = (hit, x, y) if hit else None
        # claim speaker names / timestamps so pressing them never starts a text selection;
        # leave ordinary text to the text view for normal selecting and editing
        gesture.set_state(Gtk.EventSequenceState.CLAIMED if hit else Gtk.EventSequenceState.DENIED)

    def on_transcript_released(self, _gesture, _dx, _dy):
        press, self.transcript_press = self.transcript_press, None
        if press is not None:
            (kind, line, m), x, y = press
            self.on_transcript_clicked(kind, line, m, x, y)

    def on_transcript_clicked(self, kind, line, m, x, y):
        if kind == "time":
            if self.player is not None and getattr(self, "playing_label", None) == m.group(1):
                self.stop_playback()
            else:
                self.play_line(line)
            return
        if self.viewing is not None:
            self.toast("This earlier transcript is read-only. Press “Use this version” to change its speakers.")
            return
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        selected = self.bulk_lines()
        if line in selected and len(selected) > 1:
            lines = selected        # a name inside the selection: assign the whole selection
        else:
            lines = [line]
        if self.speaker_popover is None:
            self.speaker_popover = Gtk.Popover(has_arrow=True)
            self.speaker_popover.set_parent(self.t_view)
        search = self.fill_speaker_menu(self.speaker_popover, lines)
        self.speaker_popover.set_pointing_to(rect)
        self.speaker_popover.popup()
        if search is not None:
            search.grab_focus()

    # --- bulk assign speaker ----------------------------------------------
    def bulk_lines(self):
        """Transcript lines chosen for bulk assign: Ctrl/Shift-clicked chunks plus every speaker line a
        text selection spans (when it spans more than one)."""
        lines = {self.t_buf.get_iter_at_mark(mk).get_line() for mk in self.bulk_marks}
        bounds = self.t_buf.get_selection_bounds()
        if bounds:
            a, b = bounds
            last = b.get_line() - (1 if b.starts_line() and b.get_line() > a.get_line() else 0)
            spanned = [n for n in range(a.get_line(), last + 1) if self.line_info(n) is not None]
            if len(spanned) > 1:
                lines.update(spanned)
        return sorted(n for n in lines if self.line_info(n) is not None)

    def toggle_bulk(self, line, extend=False):
        if self.viewing is not None:
            self.toast("This earlier transcript is read-only. Press “Use this version” to change its speakers.")
            return
        marked = {self.t_buf.get_iter_at_mark(mk).get_line(): mk for mk in self.bulk_marks}
        if extend and self.bulk_anchor is not None:
            lo, hi = sorted((self.bulk_anchor, line))
            for n in range(lo, hi + 1):
                if n not in marked and self.line_info(n) is not None:
                    self.bulk_marks.append(self.t_buf.create_mark(None, self.t_buf.get_iter_at_line(n)[1], True))
        elif line in marked:
            self.t_buf.delete_mark(marked[line])
            self.bulk_marks.remove(marked[line])
        else:
            self.bulk_marks.append(self.t_buf.create_mark(None, self.t_buf.get_iter_at_line(line)[1], True))
        self.bulk_anchor = line
        self.update_bulk()

    def clear_bulk(self, selection=True):
        for mk in self.bulk_marks:
            if not mk.get_deleted():
                self.t_buf.delete_mark(mk)
        self.bulk_marks = []
        self.bulk_anchor = None
        if selection and self.t_buf.get_has_selection():
            self.t_buf.place_cursor(self.t_buf.get_iter_at_mark(self.t_buf.get_insert()))
        self.update_bulk()

    def update_bulk(self, *_args):
        """Highlight the Ctrl-clicked chunks and show the bulk assign bar while two or more are chosen
        (or one Ctrl-clicked chunk, so it's clear what Ctrl+click did)."""
        if not hasattr(self, "bulk_bar"):
            return      # still building the window
        buf = self.t_buf
        buf.remove_tag_by_name("bulk", buf.get_start_iter(), buf.get_end_iter())
        for mk in self.bulk_marks:
            info = self.line_info(buf.get_iter_at_mark(mk).get_line())
            if info is not None:
                buf.apply_tag_by_name("bulk", info[0], info[1])
        n = len(self.bulk_lines())
        show = n > 1 or (n == 1 and bool(self.bulk_marks))
        self.bulk_bar.set_reveal_child(show)
        if show:
            self.bulk_label.set_text(f"{n} chunk{'s' if n != 1 else ''} selected. "
                                     "Ctrl+click names to add or remove, Shift+click for a range.")
        if self.bulk_popover is not None and self.bulk_popover.get_visible() and not show:
            self.bulk_popover.popdown()

    def fill_bulk_popover(self, _button):
        lines = self.bulk_lines()
        if lines:
            search = self.fill_speaker_menu(self.bulk_popover, lines)
            if search is not None:
                GLib.idle_add(lambda: search.grab_focus() and False)

    def fill_speaker_menu(self, popover, lines, replace=None):
        """Put the "who is speaking?" list into `popover` for transcript `lines` (one line, or several
        for bulk assign). With `replace` (a speaker name) it is instead the "Replace all" list: picking a
        name relabels every line of that speaker. Returns its search entry, if any, for focusing."""
        names = {self.line_info(n)[2].group(2) for n in lines}
        current = replace or (next(iter(names)) if len(names) == 1 else None)
        bulk = len(lines) > 1 and not replace
        same = len(self.speaker_lines(current)) if current else 0
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(6)
        if replace:
            heading = f"Replace all “{replace}”"
        else:
            heading = f"Bulk assign speaker · {len(lines)} chunks" if bulk else "Who is speaking?"
        title = Gtk.Label(label=heading, xalign=0)
        title.add_css_class("heading")
        box.append(title)
        if replace:
            note = Gtk.Label(label=f"Choose the right speaker. All {same} line{'s' if same != 1 else ''} "
                                   f"labelled {replace}\nin this meeting will change.", xalign=0)
            note.add_css_class("dim-label")
            box.append(note)
        group = not bulk and not replace and bool(SPEAKER_N_RE.match(current))
        follow = Gtk.CheckButton(
            label=f"All lines from {current}" if group else "Also the following “Them” lines\n(until someone else speaks)",
            active=group)

        def pick(name):
            popover.popdown()
            if replace:
                self.replace_speaker(replace, name)
            else:
                self.assign_speaker(lines, name, follow.get_active() and not bulk)
        people = speakers.load()
        if not people:
            note = Gtk.Label(label="No speakers yet.", xalign=0)
            note.add_css_class("dim-label")
            box.append(note)
        learned = voices.load()
        # The people list scrolls: a popover taller than the room around the click can't be placed,
        # and GTK then closes it the moment it opens (this is what broke once there were ~15 speakers).
        people_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        buttons = []
        for person in people:
            label = person["name"] + ("  ✓" if person["name"] == current else "")
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            b.get_child().set_ellipsize(Pango.EllipsizeMode.END)
            tip = [person.get("email") or "", "Voice learned" if person["name"] in learned else ""]
            if any(tip):
                b.set_tooltip_text(" · ".join(t for t in tip if t))
            b.connect("clicked", lambda _b, name=person["name"]: pick(name))
            b.set_sensitive(not (replace and person["name"] == replace))
            people_box.append(b)
            buttons.append((f"{person['name']} {person.get('email') or ''}".lower(), b))
        search = None
        if len(people) > 6:
            search = Gtk.SearchEntry(placeholder_text="Find a speaker")

            def filter_people(entry):
                words = entry.get_text().lower().split()
                for haystack, b in buttons:
                    b.set_visible(all(w in haystack for w in words))

            def pick_first(_entry):
                first = next((b for _h, b in buttons if b.get_visible()), None)
                if first is not None:
                    first.emit("clicked")
            search.connect("search-changed", filter_people)
            search.connect("activate", pick_first)
            search.connect("stop-search", lambda _e: popover.popdown())     # Escape
            box.append(search)
        if people:
            box.append(Gtk.ScrolledWindow(child=people_box, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                          propagate_natural_height=True, max_content_height=300,
                                          min_content_width=240))
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        for name, caption in (("You", "You (your microphone)"), ("Them", "Them (unknown)")):
            b = Gtk.Button(label=caption + ("  ✓" if name == current else ""))
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, n=name: pick(n))
            b.set_sensitive(not (replace and name == replace))
            box.append(b)
        if not bulk and not replace and (people or group):
            box.append(follow)
        if not bulk and not replace and same > 1:
            def to_replace(_b):
                found = self.fill_speaker_menu(popover, lines, replace=current)
                if found is not None:
                    found.grab_focus()
            swap = Gtk.Button(child=self.icon_label("edit-find-replace-symbolic",
                                                    f"Replace all “{current}” ({same} lines)…"))
            swap.add_css_class("flat")
            swap.set_tooltip_text(f"Give every line labelled {current} in this meeting to someone else")
            swap.connect("clicked", to_replace)
            box.append(swap)
        manage = Gtk.Button(child=self.icon_label("system-users-symbolic", "Add or remove speakers…"))
        manage.add_css_class("flat")
        manage.connect("clicked", lambda _b: (popover.popdown(), self.open_speakers_dialog()))
        box.append(manage)
        popover.set_child(box)
        return search

    def assign_speaker(self, lines, name, follow=False, message=None):
        """Give transcript `lines` (a line number or a list, for bulk assign) to `name`; with `follow` and
        one line, also every line of that detected speaker (for "Speaker N") or the following unnamed
        "Them" lines. `message` replaces the usual "N lines assigned to …" toast text."""
        for pop in (self.speaker_popover, self.bulk_popover):
            if pop is not None:
                pop.popdown()
        targets = [lines] if isinstance(lines, int) else list(lines)
        bulk = len(targets) > 1
        targets = [n for n in targets if self.line_info(n) is not None]
        if not targets:
            return
        if follow and len(targets) == 1:
            line = targets[0]
            current = self.line_info(line)[2].group(2)
            if SPEAKER_N_RE.match(current):
                targets = [n for n in range(self.t_buf.get_line_count())
                           if (i := self.line_info(n)) is not None and i[2].group(2) == current]
                if self.vjob is not None and self.vjob.kind == "recording":
                    self.live_names[current] = name     # later lines from this voice follow your choice
            else:
                n = line + 1
                while (i := self.line_info(n)) is not None and i[2].group(2) == "Them":
                    targets.append(n)
                    n += 1
        samples = []
        # Ctrl+Z returns the cursor to where it was when this started, and scrolls there: put it on the
        # first changed line, or undo would jump to wherever it was left (often the end of the transcript).
        self.t_buf.place_cursor(self.t_buf.get_iter_at_line(min(targets))[1])
        self.t_buf.begin_user_action()      # one Ctrl+Z undoes the whole assignment
        for n in targets:
            i = self.line_info(n)
            if i is None:
                continue
            start, _end, m = i
            who_start = start.get_offset() + len(m.group(1)) + 2
            a = self.t_buf.get_iter_at_offset(who_start)
            b = self.t_buf.get_iter_at_offset(who_start + len(m.group(2)))
            self.t_buf.delete(a, b)
            self.t_buf.insert_with_tags_by_name(self.t_buf.get_iter_at_offset(who_start), name,
                                                self.speaker_tag(name), "speaker-link")
            self.speaker_map[self.line_key(m.group(1), m.group(3))] = name
            samples += self.voice_samples(m.group(1), m.group(3))
        self.t_buf.end_user_action()
        if bulk:
            self.clear_bulk()
        self.save_speaker_map()
        if self.t_view.get_editable():
            self.flush_transcript_save()     # write it now rather than after the usual pause
        who = "you" if name == "You" else name
        count = f"{len(targets)} line{'s' if len(targets) > 1 else ''}"
        msg = message or (f"Bulk assigned {count} to {who}" if bulk else f"{count} assigned to {who}")
        voices.migrate(MEETINGS_DIR)        # no-op once the clip list exists
        if name not in ("You", "Them") and samples:
            voices.learn(name, samples)     # also takes these moments out of anyone they were learned for
            msg += f". Voice learned, so {name} will be recognised in future meetings"
        elif samples:
            voices.unlearn(samples)
        self.toast(msg)

    def speaker_lines(self, name):
        """Every transcript line currently labelled `name`."""
        return [n for n in range(self.t_buf.get_line_count())
                if (i := self.line_info(n)) is not None and i[2].group(2) == name]

    def replace_speaker(self, old, new):
        """Replace all: every line labelled `old` in this transcript becomes `new` (one Ctrl+Z undoes it)."""
        targets = self.speaker_lines(old)
        if not targets or old == new:
            return
        if self.vjob is not None and self.vjob.kind == "recording":
            # while recording, lines that still arrive under the old name (or under a voice you had
            # pointed at it) follow the change too; "You"/"Them" are whole streams, so they don't
            for label, named in list(self.live_names.items()):
                if named == old:
                    self.live_names[label] = new
            if old not in ("You", "Them"):
                self.live_names[old] = new
        who = "you" if new == "You" else new
        count = f"{len(targets)} line{'s' if len(targets) > 1 else ''}"
        self.assign_speaker(targets, new, message=f"Replaced “{old}” with {who} on {count}")

    def voice_samples(self, stamp, text, segments=None, folder=None):
        """The recorded segment behind a transcript line as a voice clip (see voices.clip), if any. Defaults to
        the meeting on screen."""
        segments = self.segments if segments is None else segments
        folder = self.out_dir if folder is None else folder
        t = secs(stamp)
        for g in segments:
            if int(g["start"]) == t and g.get("emb") and g["text"].strip()[:40] == text.strip()[:40]:
                if folder is None:
                    return [(g["emb"], g["end"] - g["start"])]
                return [voices.clip(folder.name, g.get("stream", "Them"), g["start"], g["end"], g["text"], g["emb"])]
        return []

    def learn_from_map(self, folder):
        """After a meeting is transcribed, learn the voices of people you named in it (clips already learned
        while naming them are recognised as the same and not counted twice)."""
        voices.migrate(MEETINGS_DIR)
        segments = self.load_segments(folder)
        by_name = {}
        for key, name in self.load_speaker_map(folder).items():
            if name in ("You", "Them") or "|" not in key:
                continue
            stamp, text = key.split("|", 1)
            by_name.setdefault(name, []).extend(self.voice_samples(stamp, text, segments, folder))
        for name, samples in by_name.items():
            if samples:
                voices.learn(name, samples)

    def participants_note(self):
        """Names (and emails) of saved speakers who appear in this transcript, for the agent."""
        names = {m.group(2) for m in map(SCREEN_RE.match, self.t_text().splitlines()) if m}
        found = [p for p in speakers.load() if p["name"] in names]
        if not found:
            return ""
        return "Known participants in this meeting:\n" + "\n".join(
            f"- {p['name']}" + (f" <{p['email']}>" if p.get("email") else "") for p in found)

    # --- export --------------------------------------------------------
    def open_export_dialog(self):
        if self.out_dir is None:
            return
        self.flush_transcript_save()
        messages = self.chat.messages if self.chat else []
        summary = meetings.summary_reply(messages)
        dialog = Adw.Dialog(title="Export meeting", content_width=460)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        view.set_content(page)
        dialog.set_child(view)

        fmt_group = Adw.PreferencesGroup(title="Format")
        checks = []
        for name, ext, info in EXPORT_FORMATS:
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if checks:
                check.set_group(checks[0])
            row = Adw.ActionRow(title=f"{name} ({ext})", subtitle=info, activatable_widget=check)
            row.add_prefix(check)
            fmt_group.add(row)
            checks.append(check)
        checks[0].set_active(True)
        inc = Adw.PreferencesGroup(title="Include")
        rows = {
            "summary": Adw.SwitchRow(title="Summary", active=bool(summary), sensitive=bool(summary),
                                     subtitle="The agent's latest summary of this meeting." if summary else
                                     "Ask the agent to “Summarize the meeting” first."),
            "conversation": Adw.SwitchRow(title="Ask Agent conversation", active=False, sensitive=bool(messages),
                                          subtitle="Every question and answer from the chat."),
            "bookmarks": Adw.SwitchRow(title="Bookmarks", active=bool(self.bookmarks), sensitive=bool(self.bookmarks),
                                       subtitle="The moments you marked as important."),
            "transcript": Adw.SwitchRow(title="Transcript", active=True,
                                        subtitle="Everything that was said, with times and speakers."),
        }
        for r in rows.values():
            inc.add(r)
        go = Gtk.Button(label="Export…", halign=Gtk.Align.CENTER, margin_top=12)
        go.add_css_class("suggested-action")
        go.add_css_class("pill")
        go.connect("clicked", lambda _b: self.choose_export_file(
            EXPORT_FORMATS[next(i for i, c in enumerate(checks) if c.get_active())][1],
            {k: r.get_active() for k, r in rows.items()}))
        inc.add(go)
        page.add(fmt_group)
        page.add(inc)
        page.add(self.build_audio_export_group())
        self.export_dialog = dialog
        self.export_checks = checks
        self.export_rows = rows
        dialog.present(self)

    def build_audio_export_group(self):
        group = Adw.PreferencesGroup(title="Audio")
        have = [kind for name, kind in meetings.AUDIO_FILES if (self.out_dir / name).exists()]
        recording = self.vjob is not None and self.vjob.kind == "recording"
        row = Adw.ActionRow(title="One mixed audio file (.mp3)")
        row.set_subtitle_lines(0)
        if recording:
            row.set_subtitle("Available when the recording has finished.")
        elif not have:
            row.set_subtitle("This meeting has no saved audio.")
        else:
            row.set_subtitle("Everyone on the call and your mic in one file, with background noise removed and "
                             "voices evened out. Your original recordings stay as they are.")
        go = Gtk.Button(label="Export audio…", valign=Gtk.Align.CENTER, sensitive=bool(have) and not recording)
        go.connect("clicked", lambda _b: self.choose_audio_file())
        row.add_suffix(go)
        group.add(row)
        return group

    def choose_audio_file(self):
        if getattr(self, "audio_export", None) is not None:
            self.toast("Already exporting audio. It'll be ready shortly.")
            return
        folder = self.out_dir
        name = re.sub(r"[^\w\- ,.]+", "", meetings.title(folder)).strip() or "meeting"
        dialog = Gtk.FileDialog(title="Export meeting audio", initial_name=f"{name}.mp3")
        music = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_MUSIC) or \
            GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS) or str(pathlib.Path.home())
        dialog.set_initial_folder(Gio.File.new_for_path(music))

        def done(d, result):
            try:
                target = pathlib.Path(d.save_finish(result).get_path())
            except GLib.Error:
                return
            if target.suffix.lower() != ".mp3":
                target = target.with_name(target.name + ".mp3")
            if self.export_dialog is not None:
                self.export_dialog.close()
            self.export_audio(folder, target)
        dialog.save(self, None, done)

    def export_audio(self, folder, target):
        """Mix the meeting's recordings into one MP3 in the background, with progress in a toast."""
        cmd = meetings.mixed_audio_command(folder, target)
        if cmd is None:
            self.toast("This meeting has no saved audio.")
            return
        total = max(1, meetings.duration(folder) or 1)
        toast = Adw.Toast(title="Exporting audio…", timeout=0, use_markup=False)
        self.toasts.add_toast(toast)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True)
        except OSError as e:
            toast.dismiss()
            self.toast(f"Couldn't export the audio: {e}")
            return
        self.audio_export = proc

        def progress():
            for line in proc.stdout:
                if line.startswith("out_time_us="):
                    try:
                        pct = min(99, int(int(line.split("=", 1)[1]) / 1e6 / total * 100))
                    except ValueError:
                        continue
                    GLib.idle_add(toast.set_title, f"Exporting audio… {pct}%")
            err = proc.stderr.read().strip()
            code = proc.wait()
            GLib.idle_add(finished, code, err)

        def finished(code, err):
            self.audio_export = None
            toast.dismiss()
            if code == 0 and target.exists():
                self.toast(f"Audio saved: {target.name} ({datasize.fmt(target.stat().st_size)})", "Open",
                           lambda: Gio.AppInfo.launch_default_for_uri(target.as_uri(), None))
            else:
                self.toast(f"Couldn't export the audio. {err.splitlines()[-1] if err else ''}".strip())
            return False
        threading.Thread(target=progress, daemon=True).start()

    def choose_export_file(self, ext, options):
        name = re.sub(r"[^\w\- ,.]+", "", meetings.title(self.out_dir)).strip() or "meeting"
        dialog = Gtk.FileDialog(title="Export meeting", initial_name=f"{name}{ext}")
        docs = pathlib.Path(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS) or pathlib.Path.home())
        dialog.set_initial_folder(Gio.File.new_for_path(str(docs)))

        def done(d, result):
            try:
                target = pathlib.Path(d.save_finish(result).get_path())
            except GLib.Error:
                return
            if target.suffix.lower() != ext:
                target = target.with_name(target.name + ext)
            self.export_to(target, options)
        dialog.save(self, None, done)

    def export_to(self, target, options, on_done=None):
        """Write the export; .md directly, .docx/.pdf through export.py in MintoMeet's own Python."""
        messages = self.chat.messages if self.chat else []
        people = []
        names = {m.group(2) for m in map(SCREEN_RE.match, self.t_text().splitlines()) if m}
        for p in speakers.load():
            if p["name"] in names:
                people.append(p["name"] + (f" <{p['email']}>" if p.get("email") else ""))
        md = meetings.build_markdown(self.out_dir, messages, people, **options)
        if self.export_dialog is not None:
            self.export_dialog.close()
        target = pathlib.Path(target)

        def finished(error):
            if error:
                self.toast(f"Export failed: {error}")
            else:
                self.toast(f"Exported to {target.name}", "Open", lambda: subprocess.Popen(["xdg-open", str(target)]))
            if on_done:
                on_done(error)
            return False

        if target.suffix.lower() == ".md":
            try:
                target.write_text(md, encoding="utf-8")
                finished(None)
            except OSError as e:
                finished(str(e))
            return

        def work():
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
                f.write(md)
            r = subprocess.run([str(APP_DIR / "venv/bin/python"), str(APP_DIR / "export.py"), f.name, str(target)],
                               capture_output=True, text=True)
            os.unlink(f.name)
            GLib.idle_add(finished, None if r.returncode == 0 else (r.stderr.strip().splitlines() or ["unknown"])[-1])
        threading.Thread(target=work, daemon=True).start()

    # --- transcribe again -----------------------------------------------------
    def open_retranscribe_dialog(self):
        if self.out_dir is None or self.vjob is not None:
            return
        minutes = max(1, round(meetings.duration(self.out_dir) / 60))
        dialog = Adw.Dialog(title="Transcribe again", content_width=480)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        view.set_content(page)
        dialog.set_child(view)
        group = Adw.PreferencesGroup(
            title="Engine",
            description=f"The current transcript was made with "
                        f"{meetings.model_name(meetings.current_model(self.out_dir)[0])}. This makes a fresh one from "
                        "the meeting's saved audio. The current transcript is kept, and you can open it again from "
                        "the “Transcribed with” tag. Speakers are detected again, and voices you have named before "
                        "are recognised automatically.")
        checks = {}
        specs = modelcatalog.detect()
        anchor = Gtk.CheckButton()      # hidden: keeps a lone choice looking like a radio button
        for m in self.models:
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=anchor)
            est = max(1, round(minutes * modelcatalog.processing_factor(m, specs)))
            row = Adw.ActionRow(title=m.name, subtitle=f"{m.languages}. About {est} minute{'s' if est > 1 else ''} "
                                                       f"for this {minutes}-minute meeting.", activatable_widget=check)
            row.add_prefix(check)
            group.add(row)
            checks[m.id] = check
        if checks:      # suggest the most accurate model you have
            checks[max(self.models, key=lambda m: m.rank).id].set_active(True)
        more = Adw.ActionRow(title="Get more speech models…", activatable=True,
                             subtitle="Larger models are slower but more accurate, which suits transcribing again.")
        more.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        more.connect("activated", lambda _r: (dialog.close(), self.open_model_library()))
        group.add(more)
        lang = Adw.ComboRow(title="Meeting language (Whisper only)", model=Gtk.StringList.new([n for n, _ in LANGUAGES]))
        lang.set_factory(self.plain_factory())
        lang.set_list_factory(self.plain_factory())
        lang.set_selected(self.lang_row.get_selected())
        group.add(lang)
        go = Gtk.Button(label="Start", halign=Gtk.Align.CENTER, margin_top=12)
        go.add_css_class("suggested-action")
        go.add_css_class("pill")
        go.set_sensitive(bool(checks))
        dialog.radio_anchor = anchor
        go.connect("clicked", lambda _b: (dialog.close(), self.start_retranscribe(
            next(mid for mid, c in checks.items() if c.get_active()), LANGUAGES[lang.get_selected()][1])))
        group.add(go)
        page.add(group)
        self.retx_dialog = dialog
        dialog.present(self)

    def start_retranscribe(self, model_id, language):
        """Transcribe the meeting on screen again, in the background (it waits if others are running)."""
        folder = self.out_dir
        chosen = modelcatalog.get(model_id)
        if folder is None or self.vjob is not None or chosen is None:
            return
        if not (APP_DIR / "venv/bin/python").exists():
            self.set_state("error", "The speech engine isn't installed. Run install.sh in the mintomeet folder.")
            return
        self.flush_transcript_save()
        self.stop_playback()
        self.leave_version()
        job = self.jobs.add(folder, chosen.id, language if chosen.engine == "whisper" else None, prefs()["speakers"])
        if job.state == "queued":
            self.toast(f"Waiting: {len(self.jobs.running())} other meetings are being transcribed. This one starts "
                       "when there's room.")
        self.set_state("job")
        self.refresh_sidebar()

    # --- preferences -------------------------------------------------------
    def show_about(self):
        about = Adw.AboutDialog(
            application_name="MintoMeet", application_icon="audio-input-microphone", version=__version__,
            developer_name="Saailverse Technologies", copyright="© 2026 Saailverse Technologies", license_type=Gtk.License.MIT_X11,
            comments="Record and transcribe your meetings on your own computer, then ask an AI agent about them.")
        if HOMEPAGE:
            about.set_website(HOMEPAGE)
            about.set_issue_url(HOMEPAGE.rstrip("/") + "/issues")
        about.add_credit_section("Speech recognition", [
            "Parakeet TDT by NVIDIA (CC-BY-4.0)", "Whisper by OpenAI, via faster-whisper (MIT)",
            "sherpa-onnx by k2-fsa (Apache-2.0)", "ERes2Net speaker model by 3D-Speaker (Apache-2.0)"])
        about.present(self)

    def open_preferences(self):
        p = prefs()
        dialog = Adw.PreferencesDialog(title="Preferences")
        page = Adw.PreferencesPage(title="General", icon_name="emblem-system-symbolic")
        bg = Adw.PreferencesGroup(title="Running in the background")
        rows = {
            "background": Adw.SwitchRow(
                title="Keep running when the window is closed", active=p["background"],
                subtitle="MintoMeet stays in the top bar (tray icon), keeps any recording going and can notice "
                         "when a call starts."),
            "autostart": Adw.SwitchRow(title="Start when you log in", active=p["autostart"],
                                       subtitle="Opens quietly in the tray, ready for your first call."),
            "detect_calls": Adw.SwitchRow(
                title="Ask to record when a call starts", active=p["detect_calls"],
                subtitle="When Zoom, Lark, Teams or a browser starts using your microphone, MintoMeet offers to "
                         "record, and offers to stop when the call ends."),
            "speakers": Adw.SwitchRow(
                title="Tell speakers apart", active=p["speakers"],
                subtitle="Labels other people as Speaker 1, Speaker 2… and recognises voices you have named "
                         "before. Uses a little extra CPU."),
        }
        bg.add(rows["background"])
        bg.add(rows["autostart"])
        meet = Adw.PreferencesGroup(title="Meetings")
        meet.add(rows["detect_calls"])
        meet.add(rows["speakers"])
        auto = jobs.auto_limit()
        choices = [(f"Automatic ({auto} on this computer)", 0)] + [(str(n), n) for n in range(1, 5)]
        parallel = Adw.ComboRow(title="Meetings transcribed at once",
                                subtitle="More at once share the processor, so each one takes longer. The rest "
                                         "wait their turn, and one only starts when there's enough free memory.",
                                model=Gtk.StringList.new([label for label, _ in choices]))
        parallel.set_subtitle_lines(0)
        parallel.set_selected(next((i for i, (_l, n) in enumerate(choices) if n == p["parallel"]), 0))

        def parallel_changed(row, _param):
            new = prefs()
            new["parallel"] = choices[row.get_selected()][1]
            save_settings({"prefs": new})
            self.jobs.limit_setting = new["parallel"]
            self.jobs.schedule()
        parallel.connect("notify::selected", parallel_changed)
        meet.add(parallel)
        ai = Adw.PreferencesGroup(title="AI agent", description="Who answers in Ask Agent and in “mintomeet summarize”.")
        agent_row = Adw.ActionRow(title=agents.describe(self.agent_cfg), subtitle=agents.usage_note(self.agent_cfg),
                                  use_markup=False)
        agent_row.set_subtitle_lines(2)
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        change.connect("clicked", lambda _b: (dialog.close(), self.open_agent_setup()))
        agent_row.add_suffix(change)
        ai.add(agent_row)
        page.add(bg)
        page.add(meet)
        page.add(ai)
        dialog.add(page)

        def changed(row, _param, key):
            new = prefs()
            new[key] = row.get_active()
            save_settings({"prefs": new})
            if key == "autostart":
                set_autostart(new["autostart"])
            if self.hooks:
                self.hooks.apply_prefs()
        for key, row in rows.items():
            row.connect("notify::active", changed, key)
        self.prefs_dialog = dialog
        self.prefs_rows = rows
        dialog.present(self)

    # --- folders ---------------------------------------------------
    def on_open_folder(self, _btn):
        if self.out_dir and self.out_dir.exists():
            subprocess.Popen(["xdg-open", str(self.out_dir)])

    def load_folder(self, folder):
        """Show an earlier meeting's transcript, chat and bookmarks."""
        folder = pathlib.Path(folder)
        job = self.jobs.get(folder)
        if chat.transcript_path(folder) is None and job is None:
            self.toast("That folder has no transcript.")
            return False
        if self.chat is not None and self.chat.busy:
            self.chat.cancel()
        self.flush_transcript_save()
        self.stop_playback()
        self.out_dir = folder
        if job is not None and job.kind == "recording":
            self.show_job_lines(job)        # what's transcribed so far; new lines keep arriving
        else:
            self.show_transcript_file(folder)
        self.live_names = job.live_names if job is not None else {}
        self.chat = chat.Chat(folder)
        self.render_chat()
        self.bookmarks = meetings.load_bookmarks(folder)
        self.render_bookmarks()
        self.stack.set_visible_child_name("transcript")
        self.set_state("done", f"Opened “{meetings.title(folder)}”. Use “Ask Agent” to summarize it or ask "
                               "questions.")
        self.update_title()
        self.refresh_sidebar()
        return True

    # --- search across meetings ------------------------------------
    def set_search_button(self, on):
        if self.search_btn.get_active() != on:
            self.syncing_search = True
            self.search_btn.set_active(on)
            self.syncing_search = False

    def on_search_toggled(self, btn):
        if not self.syncing_search:
            self.open_search() if btn.get_active() else self.close_search()

    def open_search(self, text=None, keep_place=False):
        self.flush_transcript_save()        # so the search sees your latest edits
        self.main_stack.set_visible_child_name("search")
        self.set_search_button(True)
        self.results_btn.set_visible(False)
        self.search_page.present_search(text, keep_place=keep_place)

    def close_search(self):
        self.main_stack.set_visible_child_name("meeting")
        self.set_search_button(False)
        self.came_from_search = False
        self.results_btn.set_visible(False)

    def open_search_meeting(self, doc, query):
        self.go_to_result(doc, query)

    def open_search_hit(self, doc, hit, query):
        self.go_to_result(doc, query, hit)

    def go_to_result(self, doc, query, hit=None):
        """Open a search result's meeting and show the match: the transcript line (with the find bar on the
        searched words), the bookmark, or the Ask Agent message."""
        folder = doc.folder
        if folder != self.out_dir or self.viewing is not None:
            if not self.load_folder(folder):
                return
        self.main_stack.set_visible_child_name("meeting")
        self.set_search_button(False)
        self.came_from_search = True
        self.results_btn.set_visible(True)
        words = " ".join(f'"{t}"' if " " in t else t for t in query.terms)
        if hit is None or hit.source == "transcript":
            line = self.find_line(hit.when, hit.text) if hit is not None else None
            if words:
                self.open_find(words, line=line if line is not None else 0)
            else:
                self.find_bar.set_search_mode(False)
                self.stack.set_visible_child_name("transcript")
            if line is not None:
                self.flash_line(line)
        elif hit.source == "bookmark":
            self.find_bar.set_search_mode(False)
            self.jump_to(doc.bookmarks[hit.index].get("t", 0))
        else:
            self.find_bar.set_search_mode(False)
            self.scroll_to_message(hit.index)

    def find_line(self, stamp, text):
        """The transcript line with this time and text (or, failing that, the first at this time)."""
        first = None
        for n in range(self.t_buf.get_line_count()):
            info = self.line_info(n)
            if info is None or info[2].group(1) != stamp:
                continue
            if info[2].group(3).strip() == text.strip():
                return n
            first = n if first is None else first
        return first

    def flash_line(self, n):
        start = self.t_buf.get_iter_at_line(n)[1]
        end = start.copy()
        if not end.ends_line():
            end.forward_to_line_end()
        self.t_buf.apply_tag_by_name("flash", start, end)
        self.scroll_transcript_to(start)
        GLib.timeout_add(2500, lambda: self.t_buf.remove_tag_by_name(
            "flash", self.t_buf.get_start_iter(), self.t_buf.get_end_iter()) or False)

    # --- closing ---------------------------------------------------
    def on_close_request(self, _win):
        self.flush_transcript_save()
        busy = [j for j in self.jobs.all() if j.proc is not None]
        if self.hooks and self.hooks.runs_in_background():
            self.set_visible(False)         # keep running in the tray
            if self.rec is not None:
                self.hooks.notify("MintoMeet is still recording",
                                  "Use the MintoMeet icon in the top bar to stop or reopen the window.")
            elif busy:
                self.hooks.notify("MintoMeet keeps transcribing",
                                  "You'll get a notification when it's done. Reopen it from the top bar icon.")
            return True
        if self.chat is not None and self.chat.busy:
            self.chat.cancel()
        self.stop_playback()
        if not busy or self.quitting:
            return False
        if self.rec is not None:
            heading = "Recording in progress"
            body = ("Closing stops the recording. The audio is saved, and whatever isn't transcribed yet is "
                    "transcribed the next time you open MintoMeet.")
            keep, stop = "Keep recording", "Stop and close"
        else:
            heading = f"Transcribing {len(busy)} meeting{'s' if len(busy) != 1 else ''}"
            body = ("Closing pauses the transcription. It carries on from where it stopped the next time you open "
                    "MintoMeet.")
            keep, stop = "Keep open", "Pause and close"
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("keep", keep)
        dialog.add_response("stop", stop)
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("keep")
        dialog.set_close_response("keep")
        dialog.connect("response", self.on_quit_response)
        self.quit_dialog = dialog
        dialog.present(self)
        return True

    def on_quit_response(self, _dialog, response):
        if response == "stop":
            self.quit_safely(lambda: self.hooks.quit_now() if self.hooks else self.destroy())

    def quit_safely(self, then):
        """Stop a recording (keeping its audio) and pause every transcription with its progress saved, then
        `then`. Everything carries on from where it stopped the next time the app starts."""
        self.quitting = True
        rec = self.rec
        if rec is not None:
            try:
                meetings.save_meta(rec.folder, duration=int(time.monotonic() - self.record_start))
            except OSError:
                pass
        self.jobs.suspend_all(then)


class App(Adw.Application):
    """Owns the window, the tray icon, call detection and notifications."""

    def __init__(self, background=False, app_id=APP_ID):
        super().__init__(application_id=app_id)
        self.win = None
        self.tray = None
        self.watch = None
        self.held = False
        self.start_hidden = background
        self.call_app = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        for name, handler in (("show", self.show_window), ("start-recording", self.start_recording),
                              ("stop-recording", self.stop_recording), ("bookmark", lambda: self.win.add_bookmark()),
                              ("preferences", lambda: (self.show_window(), self.win.open_preferences())),
                              ("about", lambda: (self.show_window(), self.win.show_about())),
                              ("quit", self.quit_all)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            self.add_action(action)
        self.set_accels_for_action("app.bookmark", ["<Control>b"])
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("win.search", ["<Control><Shift>f", "<Control>k"])
        self.set_accels_for_action("win.find", ["<Control>f"])

    def do_activate(self):
        if self.win is None:
            self.win = Window(self, hooks=self)
            self.apply_prefs()
            if self.start_hidden and self.runs_in_background():
                return
        self.show_window()

    # --- hooks used by the window ------------------------------------------------
    def runs_in_background(self):
        return prefs()["background"] and self.tray is not None and self.tray.ok

    def apply_prefs(self):
        p = prefs()
        if p["background"] and self.tray is None:
            self.tray = tray.Tray(self.show_window, self.tray_items)
        if p["background"] and not self.held:
            self.hold()
            self.held = True
        elif not p["background"] and self.held:
            self.release()
            self.held = False
        if p["detect_calls"] and self.watch is None:
            self.watch = callwatch.CallWatch(self.on_call_start, self.on_call_end)
        if self.watch is not None:
            self.watch.enabled = p["detect_calls"]

    def state_changed(self):
        if self.tray is not None:
            self.tray.update(self.recording())

    def recording(self):
        return self.win is not None and self.win.rec is not None

    def notify(self, title, body, buttons=()):
        n = Gio.Notification.new(title)
        n.set_body(body)
        n.set_default_action("app.show")
        for label, action in buttons:
            n.add_button(label, action)
        self.send_notification("mintomeet", n)

    def quit_now(self):
        if self.held:
            self.release()
            self.held = False
        self.quit()

    # --- tray menu and actions --------------------------------------------------
    def tray_items(self):
        w = self.win
        rec = self.recording()
        can_toggle = w is not None
        return [("Open MintoMeet", True, self.show_window), None,
                ("Stop recording" if rec else "Start recording", can_toggle,
                 self.stop_recording if rec else self.start_recording),
                ("Bookmark this moment", rec and w.rec_phase == "recording", lambda: w.add_bookmark()), None,
                ("Quit MintoMeet", True, self.quit_all)]

    def show_window(self):
        if self.win is not None:
            self.win.set_visible(True)
            self.win.present()

    def start_recording(self):
        if self.win is not None and self.win.rec is None:
            self.show_window()
            self.win.start()

    def stop_recording(self):
        if self.recording():
            self.win.stop()

    def quit_all(self):
        w = self.win
        if w is not None and any(j.proc is not None for j in w.jobs.all()):
            if w.rec is not None:
                self.notify("Saving your recording", "MintoMeet closes once the audio is saved. Whatever isn't "
                                                     "transcribed yet carries on the next time you open it.")
            w.quit_safely(self.quit_now)
            return
        self.quit_now()

    # --- call detection --------------------------------------------------------
    def on_call_start(self, app_name):
        self.call_app = app_name
        if self.win is None or self.win.rec is not None:
            return
        self.notify(f"{app_name} is using your microphone", "Looks like a call. Record and transcribe it?",
                    [("Start recording", "app.start-recording")])
        if self.win.is_visible():
            self.win.toast(f"{app_name} started using your microphone.", "Record", self.start_recording)

    def on_call_end(self, app_name):
        if self.recording():
            self.notify(f"Your {app_name} call seems to have ended",
                        "Stop recording and save the transcript?", [("Stop recording", "app.stop-recording")])
            if self.win.is_visible():
                self.win.toast(f"{app_name} released your microphone.", "Stop recording", self.stop_recording)


def make_private(*folders):
    """Recordings, transcripts and voiceprints are personal: keep their folders readable by you only."""
    for folder in folders:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o700)
        except OSError:
            pass


def main():
    os.chdir(pathlib.Path.home())
    if not (os.environ.get("MINTOMEET_CONFIG_DIR") or os.environ.get("MINTOMEET_MEETINGS_DIR")):
        migrate.run()       # data left by MeetScribe, this app's former name
    make_private(MEETINGS_DIR, CONFIG_DIR)
    background = "--background" in sys.argv
    App(background=background).run([sys.argv[0]])


if __name__ == "__main__":
    main()
