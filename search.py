"""Search across every meeting: transcript lines, Ask Agent conversations, bookmark notes, titles and tags,
narrowed by people, tags and dates. Pure Python (no GTK), so it is tested on its own.

Meetings are small text files (a long meeting is ~100 KB), so the whole library is kept in memory and searched
directly; `Index.refresh()` re-reads only the meetings whose files changed since the last search.

Matching ignores case and accents, and each word must match the start of a word ("plan" finds "planning",
not "explain"). Every word of the query must appear in a meeting for it to match; the lines shown for it are
those holding all the words, or, when the words are spread over several lines, those holding the most.
"""
import datetime
import json
import pathlib
import re
import unicodedata

import meetings

SOURCES = ("transcript", "chat", "bookmark", "title")       # "title" covers titles and tags
SOURCE_WORDS = {"transcript": "transcript", "transcripts": "transcript", "said": "transcript",
                "chat": "chat", "chats": "chat", "agent": "chat", "ask": "chat",
                "bookmark": "bookmark", "bookmarks": "bookmark", "notes": "bookmark", "note": "bookmark",
                "title": "title", "titles": "title", "tag": "title", "tags": "title"}
GENERIC_SPEAKERS = re.compile(r"^(Them|Audio|Speaker \d+)$")    # recorder labels that aren't a person
MAX_HITS = 200          # lines kept per meeting; the page shows a few and offers the rest
TOKEN_RE = re.compile(r'(\w+):("[^"]*"?|\S+)|"([^"]*)"?|(\S+)')
OPERATORS = {"from": "people", "speaker": "people", "by": "people", "person": "people", "who": "people",
             "tag": "tags", "tagged": "tags",
             "after": "after", "since": "after", "before": "before", "until": "before", "on": "on", "date": "on",
             "in": "where"}


# --- text folding and highlighting ---------------------------------------------------------------
def fold(text):
    """Lower case without accents, so "Café" and "cafe" match."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def fold_map(text):
    """`fold(text)` plus, for each folded character, the index of the original character it came from
    (folding can change lengths: "ß" becomes "ss"), so matches can be highlighted in the original."""
    out, where = [], []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKD", ch.casefold()):
            if not unicodedata.combining(c):
                out.append(c)
                where.append(i)
    return "".join(out), where


def term_pattern(term):
    """A folded search term matching at the start of a word; spaces in a phrase match any whitespace."""
    words = [re.escape(w) for w in term.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words))


def spans(text, terms):
    """(start, end) character ranges of `text` matching any of `terms` (folded), merged and sorted."""
    if not terms or not text:
        return []
    folded, where = fold_map(text)
    found = []
    for t in terms:
        for m in term_pattern(t).finditer(folded):
            if m.end() > m.start():
                found.append((where[m.start()], where[m.end() - 1] + 1))
    found.sort()
    merged = []
    for a, b in found:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def snippet(text, marks, width=180):
    """A piece of `text` around its first match, at most about `width` characters, with "…" where it was
    cut; returns (piece, marks moved into the piece)."""
    text = " ".join(text.split()) if "\n" in text else text
    if len(text) <= width:
        return text, [m for m in marks if m[1] <= len(text)]
    first = marks[0][0] if marks else 0
    start = max(0, min(first - width // 3, len(text) - width))
    if start:
        space = text.find(" ", start, first) if first > start else -1
        start = space + 1 if space != -1 else start
    end = min(len(text), start + width)
    if end < len(text):
        space = text.rfind(" ", start + width // 2, end)
        end = space if space != -1 else end
    piece = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
    shift = start - (1 if start else 0)
    moved = [(a - shift, b - shift) for a, b in marks if a >= start and b <= end]
    return piece, moved


def plain(md):
    """Ask Agent answers without Markdown marks, so snippets read as text: no **bold**, `code`, # headings."""
    out = []
    for line in md.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", line)
        if line.strip().startswith("```"):
            continue
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<![\w*])\*(?![\s*])(.+?)(?<!\s)\*(?![\w*])", r"\1", text)
    return text.replace("`", "")


# --- the query ------------------------------------------------------------------------------------
def parse_date(value, today=None):
    """A date or a (first, last) range from YYYY-MM-DD, YYYY-MM, YYYY, today or yesterday; None if not one."""
    today = today or datetime.date.today()
    v = value.strip().lower()
    if v == "today":
        return today, today
    if v == "yesterday":
        d = today - datetime.timedelta(days=1)
        return d, d
    for fmt, kind in (("%Y-%m-%d", "day"), ("%Y-%m", "month"), ("%Y", "year")):
        try:
            d = datetime.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
        if kind == "day":
            return d, d
        if kind == "month":
            nxt = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
            return d, nxt - datetime.timedelta(days=1)
        return d, d.replace(month=12, day=31)
    return None


class Query:
    """What to look for: words and phrases (`terms`, folded) plus filters. Filters typed into the search box
    (from:, tag:, after:, …) and those chosen with the page's buttons are combined."""

    def __init__(self, text="", people=(), tags=(), after=None, before=None, where=None, today=None):
        self.terms, self.people, self.tags = [], set(people), set(tags)
        self.after, self.before = after, before
        self.where = set(where) if where else set(SOURCES)
        self.typed = []         # (kind, value) of filters typed into the box, for showing them
        typed_where = set()
        for m in TOKEN_RE.finditer(text or ""):
            op, value, phrase, word = m.groups()
            kind = OPERATORS.get(op.lower()) if op else None
            if kind is not None:
                value = value.strip('"').strip()
                if not value:
                    continue
                if kind == "people":
                    self.people.add(value)
                elif kind == "tags":
                    self.tags.add(value)
                elif kind == "where" and value.lower() in SOURCE_WORDS:
                    typed_where.add(SOURCE_WORDS[value.lower()])
                elif kind in ("after", "before", "on") and (rng := parse_date(value, today)):
                    if kind in ("after", "on"):
                        self.after = max(self.after, rng[0]) if self.after else rng[0]
                    if kind in ("before", "on"):
                        self.before = min(self.before, rng[1]) if self.before else rng[1]
                else:
                    self.add_term(m.group(0))       # not a filter after all: search for it as typed
                    continue
                self.typed.append((kind, value))
            elif phrase is not None:
                self.add_term(phrase)
            elif op:
                self.add_term(m.group(0))           # e.g. "10:30" or "http://…"
            else:
                self.add_term(word)
        if typed_where:
            self.where &= typed_where

    def add_term(self, text):
        t = " ".join(fold(text).split())
        t = t.strip(".,;!?()[]{}'\"") if " " not in t else t
        if t and t not in self.terms:
            self.terms.append(t)

    @property
    def filtered(self):
        return bool(self.people or self.tags or self.after or self.before or self.where != set(SOURCES))

    @property
    def empty(self):
        return not self.terms and not self.filtered


def date_preset(name, today=None):
    """(first day, last day) for the Date filter's presets; (None, None) for any time."""
    today = today or datetime.date.today()
    day = datetime.timedelta(days=1)
    if name == "today":
        return today, today
    if name == "yesterday":
        return today - day, today - day
    if name == "week":
        return today - 6 * day, today
    if name == "month":
        return today - 29 * day, today
    if name == "this-month":
        return today.replace(day=1), today
    if name == "last-month":
        last = today.replace(day=1) - day
        return last.replace(day=1), last
    return None, None


def plain_terms(text):
    """Words and "quoted phrases" of `text` as folded terms, with no filters (for finding inside one meeting,
    where `from:` is just text)."""
    q = Query()
    for m in re.finditer(r'"([^"]*)"?|(\S+)', text or ""):
        q.add_term(m.group(1) if m.group(1) is not None else m.group(2))
    return q.terms


# --- the meetings -------------------------------------------------------------------------------------
class Doc:
    """One meeting's searchable text."""

    def __init__(self, folder):
        self.folder = pathlib.Path(folder)
        self.title = meetings.title(folder)
        self.started = meetings.started(folder)
        self.tags = meetings.tags(folder)
        self.lines = []         # (stamp, speaker, text)
        path = meetings.transcript_file(folder)
        if path is not None:
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                m = meetings.LINE_RE.match(raw)
                if m:
                    self.lines.append((f"{m.group(1)}:{m.group(2)}:{m.group(3)}", m.group(4), m.group(5)))
        self.speakers = {}
        for _s, who, _t in self.lines:
            self.speakers[who] = self.speakers.get(who, 0) + 1
        try:
            data = json.loads((self.folder / "chat.json").read_text(encoding="utf-8"))
            # "at" is the message's place in chat.json, which the window uses to scroll to it
            self.chat = [dict(m, text=plain(m["text"]) if m.get("role") != "user" else m["text"], at=i)
                         for i, m in enumerate(data.get("messages", [])) if isinstance(m, dict) and m.get("text")]
        except (OSError, ValueError, AttributeError):
            self.chat = []
        self.bookmarks = [b for b in meetings.load_bookmarks(folder) if isinstance(b, dict) and b.get("note")]
        self.folded_lines = [fold(t) for _s, _w, t in self.lines]
        self.folded_chat = [fold(m["text"]) for m in self.chat]
        self.folded_marks = [fold(b["note"]) for b in self.bookmarks]
        self.folded_people = {fold(w): w for w in self.speakers}

    @property
    def duration(self):
        return meetings.duration(self.folder)


def signature(folder):
    """Changes whenever a file the search reads changes."""
    folder = pathlib.Path(folder)
    sig = []
    for name in (meetings.META, "transcript.txt", "transcript.live.txt", "chat.json", "bookmarks.json"):
        try:
            st = (folder / name).stat()
            sig.append((name, st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    return tuple(sig)


class Hit:
    """One matching line: a transcript line, an Ask Agent message or a bookmark note."""

    def __init__(self, source, index, who, when, text, marks, full):
        self.source, self.index, self.who, self.when = source, index, who, when
        self.text, self.marks, self.full = text, marks, full


class Result:
    """One matching meeting with its matching lines."""

    def __init__(self, doc, score, hits, title_marks, tag_hits, people_lines):
        self.doc, self.score, self.hits = doc, score, hits
        self.title_marks, self.tag_hits, self.people_lines = title_marks, tag_hits, people_lines
        self.total = len(hits)      # matching lines before MAX_HITS cut them


class Index:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.docs = {}      # folder -> (signature, Doc)

    def refresh(self, extra=()):
        """Re-read the meetings that changed; `extra` adds folders not listed yet (a meeting being recorded)."""
        folders = list(meetings.list_meetings(self.root))
        folders += [pathlib.Path(f) for f in extra if f is not None and pathlib.Path(f) not in folders]
        fresh = {}
        for f in folders:
            sig = signature(f)
            old = self.docs.get(f)
            if old is not None and old[0] == sig:
                fresh[f] = old
                continue
            try:
                fresh[f] = (sig, Doc(f))
            except OSError:
                continue
        self.docs = fresh
        return [d for _s, d in fresh.values()]

    def people(self):
        """{person: number of meetings they speak in}, without the recorder's generic labels."""
        count = {}
        for _s, d in self.docs.values():
            for who in d.speakers:
                if not GENERIC_SPEAKERS.match(who):
                    count[who] = count.get(who, 0) + 1
        return dict(sorted(count.items(), key=lambda kv: (-kv[1], kv[0].casefold())))

    def tags(self):
        return meetings.count_tags(d.tags for _s, d in self.docs.values())

    def search(self, query, sort="best"):
        """Matching meetings as a list of Result, best first (or "newest" / "oldest")."""
        results = []
        for _s, doc in self.docs.values():
            r = match(doc, query)
            if r is not None:
                results.append(r)
        if sort == "oldest":
            results.sort(key=lambda r: r.doc.started)
        elif sort == "newest" or query.empty or not query.terms:
            results.sort(key=lambda r: r.doc.started, reverse=True)
        else:
            results.sort(key=lambda r: (r.score, r.doc.started), reverse=True)
        return results


def _people_in(doc, wanted):
    """The speakers in `doc` matching each wanted person (case and accents ignored); None if one is missing."""
    found = set()
    for person in wanted:
        hit = doc.folded_people.get(fold(person))
        if hit is None:
            return None
        found.add(hit)
    return found


def match(doc, q):
    """A Result if `doc` matches query `q`, else None."""
    day = doc.started.date()
    if (q.after and day < q.after) or (q.before and day > q.before):
        return None
    if q.tags:
        have = {fold(t) for t in doc.tags}
        if not all(fold(t) in have for t in q.tags):
            return None
    people = _people_in(doc, q.people) if q.people else set()
    if people is None:
        return None
    people_lines = sum(doc.speakers[p] for p in people)
    if not q.terms:
        if not q.filtered:
            return Result(doc, 0, [], [], [], 0)
        if q.where != set(SOURCES) and not _has_source(doc, q.where, people):
            return None
        return Result(doc, 0, [], [], [], people_lines)

    patterns = [term_pattern(t) for t in q.terms]
    covered = set()
    title_marks, tag_hits = [], []
    if "title" in q.where:
        ft = fold(doc.title)
        got = {i for i, p in enumerate(patterns) if p.search(ft)}
        covered |= got
        if got:
            title_marks = spans(doc.title, [q.terms[i] for i in got])
        for tag in doc.tags:
            got = {i for i, p in enumerate(patterns) if p.search(fold(tag))}
            if got:
                covered |= got
                tag_hits.append(tag)
    candidates = []     # (number of terms, Hit)
    if "transcript" in q.where:
        for n, (stamp, who, text) in enumerate(doc.lines):
            if people and who not in people:
                continue
            got = {i for i, p in enumerate(patterns) if p.search(doc.folded_lines[n])}
            if got:
                candidates.append((got, Hit("transcript", n, who, stamp, text, None, False)))
    if "chat" in q.where:
        for n, m in enumerate(doc.chat):
            got = {i for i, p in enumerate(patterns) if p.search(doc.folded_chat[n])}
            if got:
                who = "You" if m.get("role") == "user" else (m.get("agent") or "Agent")
                candidates.append((got, Hit("chat", m["at"], who, None, m["text"], None, False)))
    if "bookmark" in q.where:
        for n, b in enumerate(doc.bookmarks):
            got = {i for i, p in enumerate(patterns) if p.search(doc.folded_marks[n])}
            if got:
                candidates.append((got, Hit("bookmark", n, "Bookmark", meetings.fmt_time(b.get("t", 0)),
                                            b["note"], None, False)))
    for got, _h in candidates:
        covered |= got
    if len(covered) < len(patterns):
        return None
    every = set(range(len(patterns)))
    full = [h for got, h in candidates if got == every]
    if full:
        hits = full
        for h in hits:
            h.full = True
    else:       # the words are spread over several lines: show the lines with the most of them
        best = max((len(got) for got, _h in candidates), default=0)
        hits = [h for got, h in candidates if len(got) == best]
    order = {"transcript": 0, "bookmark": 1, "chat": 2}
    hits.sort(key=lambda h: (order[h.source], h.index))
    total = len(hits)
    hits = hits[:MAX_HITS]
    for h in hits:
        h.marks = spans(h.text, q.terms)
    score = (12 if title_marks else 0) + 8 * len(tag_hits) + 3 * min(len(full), 30)
    score += 0.5 * min(len(candidates), 40) + (6 if full and len(patterns) > 1 else 0)
    r = Result(doc, score, hits, title_marks, tag_hits, people_lines)
    r.total = total
    return r


def _has_source(doc, where, people):
    """Whether `doc` has anything in the sources `where` (used when only filters are given)."""
    if "transcript" in where and any(not people or who in people for _s, who, _t in doc.lines):
        return True
    return ("chat" in where and bool(doc.chat)) or ("bookmark" in where and bool(doc.bookmarks)) or \
        ("title" in where and bool(doc.tags))
