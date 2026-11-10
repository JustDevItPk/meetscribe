"""The Search page: one box that finds words in every meeting (transcripts, Ask Agent conversations, bookmark notes,
titles and tags), with People, Tags, Date, Where and Sort filters. Results are grouped by meeting; clicking one
opens that meeting at the matching line. The searching itself is search.py."""
import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

import search  # noqa: E402
import tagsui  # noqa: E402

SHOWN_HITS = 3          # lines shown per meeting before "Show all"
SHOWN_MEETINGS = 50     # meetings shown before "Show more meetings"
DATE_PRESETS = [("any", "Any time"), ("today", "Today"), ("yesterday", "Yesterday"), ("week", "Last 7 days"),
                ("month", "Last 30 days"), ("this-month", "This month"), ("last-month", "Last month"),
                ("custom", "Between dates…")]
WHERE = [(None, "Everywhere"), ("transcript", "What people said"), ("chat", "Ask Agent conversations"),
         ("bookmark", "Bookmark notes"), ("title", "Titles and tags")]
SORTS = [("best", "Best match"), ("newest", "Newest first"), ("oldest", "Oldest first")]
SOURCE_ICONS = {"transcript": "text-x-generic-symbolic", "chat": "user-available-symbolic",
                "bookmark": "user-bookmarks-symbolic"}
HIGHLIGHT = '<span background="#f6d32d" bgalpha="45%"><b>{}</b></span>'
TIPS = ("Find what was said, Ask Agent answers, bookmark notes, titles and tags in every meeting. "
        "Narrow it down with People, Tags and Date below.\n\n"
        "You can also type filters: <b>from:</b>Harris, <b>tag:</b>client, <b>after:</b>2026-11-01, "
        "<b>before:</b>2026-11, <b>on:</b>today, <b>in:</b>chat. Put \"quotes\" around an exact phrase.")

CSS = """
.search-entry { min-height: 40px; font-size: 15px; }
.filter-on > button { background: alpha(var(--accent-bg-color), 0.16); color: var(--accent-color); }
.result-card { padding: 0; }
.result-head { padding: 10px 14px 8px 14px; }
.result-title { font-weight: 700; font-size: 15px; }
.result-hit { padding: 6px 14px 6px 14px; }
.result-meta { font-size: 11px; font-weight: 600; }
.result-more { padding: 6px 14px; font-size: 12px; }
.search-flash { background: alpha(#f6d32d, 0.35); border-radius: 14px; }
"""


def markup(text, marks):
    """Pango markup for `text` with the `marks` ranges highlighted."""
    out, pos = [], 0
    for a, b in marks:
        out.append(GLib.markup_escape_text(text[pos:a]))
        out.append(HIGHLIGHT.format(GLib.markup_escape_text(text[a:b])))
        pos = b
    out.append(GLib.markup_escape_text(text[pos:]))
    return "".join(out)


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 'es' if word.endswith('ch') else 's'}"


class SearchPage(Gtk.Box):
    def __init__(self, meetings_dir, open_hit, open_meeting, on_back, extra_folders=lambda: ()):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.index = search.Index(meetings_dir)
        self.open_hit, self.open_meeting, self.on_back = open_hit, open_meeting, on_back
        self.extra_folders = extra_folders
        self.people, self.tags = set(), set()
        self.date, self.custom = "any", [None, None]
        self.where, self.sort = None, "best"
        self.expanded = set()           # meetings showing all their matches
        self.shown_meetings = SHOWN_MEETINGS
        self.query = search.Query()
        self.results = []

        top = Gtk.Box(spacing=8)
        back = Gtk.Button(icon_name="go-previous-symbolic", valign=Gtk.Align.CENTER)
        back.set_tooltip_text("Back to the meeting (Escape)")
        back.update_property([Gtk.AccessibleProperty.LABEL], ["Back to the meeting"])
        back.connect("clicked", lambda _b: self.on_back())
        top.append(back)
        self.entry = Gtk.SearchEntry(hexpand=True, placeholder_text="Search all meetings: words, people, tags…")
        self.entry.add_css_class("search-entry")
        self.entry.set_tooltip_markup(TIPS)
        self.entry.connect("search-changed", lambda _e: self.run(reset=True))
        self.entry.connect("activate", lambda _e: self.open_first())
        self.entry.connect("stop-search", lambda _e: self.on_back() if not self.entry.get_text() else None)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_entry_key)
        self.entry.add_controller(keys)
        top.append(self.entry)
        self.append(top)

        filters = Gtk.Box(spacing=6)
        self.people_btn = self.filter_button("avatar-default-symbolic", "People", self.fill_people)
        self.tags_btn = self.filter_button("user-bookmarks-symbolic", "Tags", self.fill_tags)
        self.date_btn = self.filter_button("x-office-calendar-symbolic", "Any time", self.fill_date)
        self.where_btn = self.filter_button("edit-find-symbolic", "Everywhere", self.fill_where)
        self.sort_btn = self.filter_button("view-sort-descending-symbolic", "Best match", self.fill_sort)
        for b in (self.people_btn, self.tags_btn, self.date_btn, self.where_btn):
            filters.append(b)
        self.clear_btn = Gtk.Button(label="Clear filters", visible=False)
        self.clear_btn.add_css_class("flat")
        self.clear_btn.connect("clicked", lambda _b: self.clear_filters())
        filters.append(self.clear_btn)
        filters.append(Gtk.Box(hexpand=True))
        filters.append(self.sort_btn)
        self.append(Gtk.ScrolledWindow(child=filters, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                       propagate_natural_height=True))

        self.summary = Gtk.Label(xalign=0, wrap=True)
        self.summary.add_css_class("dim-label")
        self.append(self.summary)

        self.stack = Gtk.Stack(vexpand=True)
        start = Adw.StatusPage(icon_name="system-search-symbolic", title="Search all your meetings")
        start.add_css_class("compact")
        tips = Gtk.Label(label=TIPS, use_markup=True, wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=70)
        tips.add_css_class("dim-label")
        start.set_child(tips)
        self.stack.add_named(start, "start")
        none = Adw.StatusPage(icon_name="edit-find-symbolic", title="No matches")
        none.add_css_class("compact")
        self.none_page = none
        clear = Gtk.Button(label="Clear filters", halign=Gtk.Align.CENTER)
        clear.add_css_class("pill")
        clear.connect("clicked", lambda _b: self.clear_filters())
        self.none_clear = clear
        none.set_child(clear)
        self.stack.add_named(none, "none")
        self.results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_bottom=12)
        self.scroll = Gtk.ScrolledWindow(child=self.results_box, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_named(self.scroll, "results")
        self.append(self.stack)

    # --- filter buttons --------------------------------------------------------------------------------
    def filter_button(self, icon, label, fill):
        popover = Gtk.Popover()
        btn = Gtk.MenuButton(popover=popover, always_show_arrow=True,
                             child=Adw.ButtonContent(icon_name=icon, label=label))
        btn.set_create_popup_func(lambda _b: fill(popover))
        return btn

    @staticmethod
    def popover_box(popover, title):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, width_request=260)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(6)
        head = Gtk.Label(label=title, xalign=0)
        head.add_css_class("heading")
        box.append(head)
        popover.set_child(box)
        return box

    def check_list(self, box, items, chosen, on_toggle, searchable=False):
        """CheckButtons for (key, label) items, in a scrolling list with an optional filter box."""
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        checks = []
        for key, label in items:
            c = Gtk.CheckButton(label=label, active=key in chosen)
            c.connect("toggled", lambda b, k=key: on_toggle(k, b.get_active()))
            inner.append(c)
            checks.append((label.casefold(), c))
        if searchable and len(items) > 8:
            find = Gtk.SearchEntry(placeholder_text="Find")
            find.connect("search-changed", lambda e: [c.set_visible(e.get_text().casefold() in text)
                                                      for text, c in checks])
            box.append(find)
            GLib.idle_add(lambda: find.grab_focus() and False)
        box.append(Gtk.ScrolledWindow(child=inner, propagate_natural_height=True, max_content_height=320,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER))

    def fill_people(self, popover):
        box = self.popover_box(popover, "Meetings with")
        people = self.index.people()
        if not people:
            box.append(self.note("No named speakers yet. Click a speaker name on a transcript line to say who "
                                 "spoke."))
            return
        items = [(p, f"{'You (your microphone)' if p == 'You' else p}  ·  {n}") for p, n in people.items()]
        self.check_list(box, items, self.people, lambda k, on: self.toggle(self.people, k, on), searchable=True)
        box.append(self.note("With words, only lines these people said are shown. Choosing several finds "
                             "meetings with all of them."))

    def fill_tags(self, popover):
        box = self.popover_box(popover, "Meetings tagged")
        tags = self.index.tags()
        if not tags:
            box.append(self.note("No tags yet. Open a meeting and press “Add tag” under its title."))
            return
        chosen = {t.casefold() for t in self.tags}
        items = [(t, f"{t}  ·  {n}") for t, n in tags.items()]
        self.check_list(box, items, {t for t in tags if t.casefold() in chosen},
                        lambda k, on: self.toggle(self.tags, k, on), searchable=True)
        box.append(self.note("Choosing several finds meetings with all of them."))

    def radio_list(self, box, items, current, on_pick):
        group = None
        for key, label in items:
            r = Gtk.CheckButton(label=label, active=key == current)
            if group is None:
                group = r
            else:
                r.set_group(group)
            r.connect("toggled", lambda b, k=key: b.get_active() and on_pick(k))
            box.append(r)

    def fill_date(self, popover):
        box = self.popover_box(popover, "When")
        custom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=self.date == "custom")

        def pick(key):
            custom.set_visible(key == "custom")
            if key != "custom":
                self.date = key
                self.run(reset=True)
                popover.popdown()
            elif all(self.custom):
                self.date = "custom"
                self.run(reset=True)
        self.radio_list(box, DATE_PRESETS, self.date, pick)
        ends = Gtk.Box(spacing=6, homogeneous=True)
        today = datetime.date.today()
        cal = Gtk.Calendar()
        buttons = []

        def label(i):
            d = self.custom[i]
            return f"{'From' if i == 0 else 'To'}: {d:%-d %b %Y}" if d else ("From…" if i == 0 else "To…")

        def choose_end(i):
            for j, b in enumerate(buttons):
                b.set_active(j == i)
            d = self.custom[i] or today
            day = GLib.DateTime.new_local(d.year, d.month, d.day, 0, 0, 0)
            (cal.set_date if hasattr(cal, "set_date") else cal.select_day)(day)    # select_day: GTK < 4.20
        for i in range(2):
            b = Gtk.ToggleButton(label=label(i), active=i == 0)
            b.connect("clicked", lambda _b, i=i: choose_end(i))
            buttons.append(b)
            ends.append(b)

        def day_picked(c):
            dt = c.get_date()
            d = datetime.date(dt.get_year(), dt.get_month(), dt.get_day_of_month())
            i = 0 if buttons[0].get_active() else 1
            self.custom[i] = d
            if self.custom[0] and self.custom[1] and self.custom[0] > self.custom[1]:
                self.custom[1 - i] = d
            for j, b in enumerate(buttons):
                b.set_label(label(j))
            if i == 0:
                choose_end(1)
            if all(self.custom):
                self.date = "custom"
                self.run(reset=True)
        cal.connect("day-selected", day_picked)
        custom.append(ends)
        custom.append(cal)
        box.append(custom)

    def fill_where(self, popover):
        box = self.popover_box(popover, "Look in")
        self.radio_list(box, WHERE, self.where, lambda k: (setattr(self, "where", k), self.run(reset=True),
                                                          popover.popdown()))

    def fill_sort(self, popover):
        box = self.popover_box(popover, "Sort meetings")
        self.radio_list(box, SORTS, self.sort, lambda k: (setattr(self, "sort", k), self.run(reset=True),
                                                         popover.popdown()))

    @staticmethod
    def note(text):
        n = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=36)
        n.add_css_class("dim-label")
        n.add_css_class("caption")
        return n

    def toggle(self, chosen, key, on):
        (chosen.add if on else chosen.discard)(key)
        self.run(reset=True)

    def date_range(self):
        if self.date == "custom":
            return tuple(self.custom) if all(self.custom) else (None, None)
        return search.date_preset(self.date)

    def clear_filters(self):
        self.people.clear()
        self.tags.clear()
        self.date, self.custom, self.where = "any", [None, None], None
        self.run(reset=True)

    def update_filter_labels(self):
        def show(btn, label, on):
            btn.get_child().set_label(label)
            (btn.add_css_class if on else btn.remove_css_class)("filter-on")

        def names(chosen, word):
            items = sorted(chosen, key=str.casefold)
            if not items:
                return word
            return f"{word}: {items[0]}" + (f" +{len(items) - 1}" if len(items) > 1 else "")
        show(self.people_btn, names(self.people, "People"), bool(self.people))
        show(self.tags_btn, names(self.tags, "Tags"), bool(self.tags))
        if self.date == "custom" and all(self.custom):
            a, b = self.custom
            text = f"{a:%-d %b}" if a == b else f"{a:%-d %b} – {b:%-d %b %Y}"
        else:
            text = dict(DATE_PRESETS)[self.date if self.date != "custom" else "any"]
        show(self.date_btn, text, self.date != "any" and text != "Any time")
        show(self.where_btn, dict(WHERE)[self.where], self.where is not None)
        show(self.sort_btn, dict(SORTS)[self.sort], False)
        self.clear_btn.set_visible(bool(self.people or self.tags or self.where or self.date != "any"))

    # --- searching ---------------------------------------------------------------------------------------
    def present_search(self, text=None, keep_place=False):
        """Show the page with fresh data (meetings may have changed since last time). With `keep_place`
        (coming back from a result) the same results are shown again where you left them."""
        self.index.refresh(self.extra_folders())
        if keep_place:
            pos = self.scroll.get_vadjustment().get_value()
            self.run()
            GLib.idle_add(lambda: self.scroll.get_vadjustment().set_value(pos) or False)
            return
        if text is not None:
            self.entry.set_text(text)
        self.run(reset=True)
        self.entry.grab_focus()
        self.entry.select_region(0, -1)

    def run(self, reset=False):
        if reset:
            self.expanded.clear()
            self.shown_meetings = SHOWN_MEETINGS
        after, before = self.date_range()
        self.query = search.Query(self.entry.get_text(), people=self.people, tags=self.tags, after=after,
                                  before=before, where={self.where} if self.where else None)
        self.update_filter_labels()
        if self.query.empty:
            self.results = []
            self.summary.set_text(f"{plural(len(self.index.docs), 'meeting')} to search.")
            self.stack.set_visible_child_name("start")
            return
        self.results = self.index.search(self.query, self.sort)
        self.render(top=reset)

    def describe_query(self):
        """What the typed filters mean, so `from:harris` visibly became a filter."""
        parts = []
        for kind, value in self.query.typed:
            word = {"people": "from", "tags": "tagged", "after": "after", "before": "before", "on": "on",
                    "where": "in"}[kind]
            parts.append(f"{word} {value}")
        return f" Filters typed: {', '.join(parts)}." if parts else ""

    def render(self, top=False):
        """Show self.results; `top` scrolls back to the first one (a new search), otherwise the place is kept."""
        while (child := self.results_box.get_first_child()) is not None:
            self.results_box.remove(child)
        if not self.results:
            self.summary.set_text("No matches." + self.describe_query())
            self.none_page.set_description(
                "Try fewer or different words, or clear the filters." if self.query.filtered else
                "Try fewer or different words. Each word must appear somewhere in a meeting.")
            self.none_clear.set_visible(self.query.filtered)
            self.stack.set_visible_child_name("none")
            return
        lines = sum(r.total for r in self.results)
        text = plural(len(self.results), "meeting")
        if self.query.terms:
            text = f"{plural(lines, 'match')} in {text}" if lines else f"{text} match"
        self.summary.set_text(text + "." + self.describe_query())
        for r in self.results[:self.shown_meetings]:
            self.results_box.append(self.card(r))
        if len(self.results) > self.shown_meetings:
            more = Gtk.Button(label=f"Show more meetings ({len(self.results) - self.shown_meetings} more)",
                              halign=Gtk.Align.CENTER)
            more.add_css_class("pill")
            more.connect("clicked", lambda _b: (setattr(self, "shown_meetings", self.shown_meetings + SHOWN_MEETINGS),
                                                self.render()))
            self.results_box.append(more)
        self.stack.set_visible_child_name("results")
        if top:
            self.scroll.get_vadjustment().set_value(0)

    def card(self, r):
        doc = r.doc
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        box.add_css_class("result-card")
        box.connect("row-activated", lambda _l, row: row.action())

        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        head.add_css_class("result-head")
        title = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, use_markup=True)
        title.set_markup(markup(doc.title, r.title_marks))
        title.add_css_class("result-title")
        head.append(title)
        bits = [f"{doc.started:%a %-d %b %Y, %H:%M}"]
        mins = round(doc.duration / 60)
        bits.append("under a minute" if mins < 1 else f"{mins} min")
        if self.query.terms and r.total:
            bits.append(plural(r.total, "match"))
        if self.query.people:
            people = sorted({doc.folded_people[search.fold(p)] for p in self.query.people}, key=str.casefold)
            bits.append(f"{' and '.join(people)}: {r.people_lines} line{'s' if r.people_lines != 1 else ''}")
        meta = Gtk.Label(label=" · ".join(bits), xalign=0, wrap=True)
        meta.add_css_class("dim-label")
        meta.add_css_class("caption")
        head.append(meta)
        if doc.tags:
            head.append(tagsui.pill_row(doc.tags, matches=r.tag_hits + [t for t in doc.tags if t.casefold() in
                                                                         {x.casefold() for x in self.query.tags}]))
        box.append(self.row(head, lambda: self.open_meeting(doc, self.query),
                            "Open this meeting"))

        limit = search.MAX_HITS if doc.folder in self.expanded else SHOWN_HITS
        for h in r.hits[:limit]:
            box.append(self.hit_row(doc, h))
        hidden = len(r.hits) - min(limit, len(r.hits))
        if hidden > 0:
            more = Gtk.Label(label=f"Show all {plural(r.total, 'match')} in this meeting", xalign=0)
            more.add_css_class("result-more")
            more.add_css_class("accent")
            box.append(self.row(more, lambda: (self.expanded.add(doc.folder), self.render())))
        elif doc.folder in self.expanded and r.total > search.MAX_HITS:
            more = Gtk.Label(label=f"Showing the first {search.MAX_HITS}. Open the meeting and press Ctrl+F to go through "
                                   "the rest.", xalign=0, wrap=True)
            more.add_css_class("result-more")
            more.add_css_class("dim-label")
            box.append(self.row(more, lambda: self.open_meeting(doc, self.query)))
        return box

    @staticmethod
    def row(child, action, tip=None):
        row = Gtk.ListBoxRow(activatable=True, child=child)
        row.action = action
        if tip:
            row.set_tooltip_text(tip)
        return row

    def hit_row(self, doc, h):
        line = Gtk.Box(spacing=10)
        line.add_css_class("result-hit")
        icon = Gtk.Image(icon_name=SOURCE_ICONS[h.source], valign=Gtk.Align.START, margin_top=2)
        icon.add_css_class("dim-label")
        line.append(icon)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        if h.source == "transcript":
            who = f"{h.when} · {'You' if h.who == 'You' else h.who}"
        elif h.source == "chat":
            who = f"Ask Agent · {'Your question' if h.who == 'You' else h.who}"
        else:
            who = f"Bookmark · {h.when}"
        meta = Gtk.Label(label=who, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        meta.add_css_class("result-meta")
        meta.add_css_class("dim-label")
        col.append(meta)
        piece, marks = search.snippet(h.text, h.marks)
        text = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, use_markup=True, lines=3,
                         ellipsize=Pango.EllipsizeMode.END)
        text.set_markup(markup(piece, marks))
        col.append(text)
        line.append(col)
        tip = {"transcript": "Open the meeting at this line", "chat": "Open this Ask Agent conversation",
               "bookmark": "Open the meeting at this bookmark"}[h.source]
        return self.row(line, lambda: self.open_hit(doc, h, self.query), tip)

    def open_first(self):
        if not self.results:
            return
        r = self.results[0]
        if r.hits:
            self.open_hit(r.doc, r.hits[0], self.query)
        else:
            self.open_meeting(r.doc, self.query)

    def on_entry_key(self, _ctl, key, _code, _mods):
        if key == Gdk.KEY_Down and self.stack.get_visible_child_name() == "results":
            first = self.results_box.get_first_child()
            if first is not None:
                first.child_focus(Gtk.DirectionType.TAB_FORWARD)
                return True
        return False
