"""Meeting folders: titles, the meeting list, bookmarks and the Markdown used for exports. Pure Python."""
import datetime
import json
import pathlib
import re

META = "meeting.json"
LINE_RE = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\] ([^:\[\]]+?): (.*)$")


def load_meta(folder):
    try:
        return json.loads((pathlib.Path(folder) / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_meta(folder, **updates):
    folder = pathlib.Path(folder)
    meta = load_meta(folder)
    meta.update(updates)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / META).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def started(folder):
    """When the meeting started, from meeting.json or the folder name (YYYY-MM-DD_HHMM[SS])."""
    meta = load_meta(folder)
    if meta.get("started"):
        try:
            return datetime.datetime.fromisoformat(meta["started"])
        except ValueError:
            pass
    name = pathlib.Path(folder).name
    for fmt in ("%Y-%m-%d_%H%M%S", "%Y-%m-%d_%H%M"):
        try:
            return datetime.datetime.strptime(name[:17] if fmt.endswith("%S") else name[:15], fmt)
        except ValueError:
            continue
    return datetime.datetime.fromtimestamp(pathlib.Path(folder).stat().st_mtime)


def title(folder):
    return load_meta(folder).get("title") or f"Meeting on {started(folder):%a %-d %b, %H:%M}"


def transcript_file(folder):
    folder = pathlib.Path(folder)
    return next((p for p in (folder / "transcript.txt", folder / "transcript.live.txt") if p.exists()), None)


def duration(folder):
    """Length in seconds: saved at the end of a recording, or estimated from the last transcript line."""
    meta = load_meta(folder)
    if meta.get("duration"):
        return meta["duration"]
    path = transcript_file(folder)
    last = 0
    if path:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = LINE_RE.match(line)
            if m:
                last = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return last


def describe(folder):
    mins = round(duration(folder) / 60)
    length = "under a minute" if mins < 1 else f"{mins} min"
    return f"{started(folder):%-d %b %Y, %H:%M} · {length}"


def list_meetings(root):
    """Meeting folders under root, newest first."""
    root = pathlib.Path(root)
    if not root.exists():
        return []
    found = [p for p in root.iterdir() if p.is_dir() and (transcript_file(p) or any(p.glob("audio-*.ogg")))]
    return sorted(found, key=lambda p: started(p), reverse=True)


def load_bookmarks(folder):
    try:
        return json.loads((pathlib.Path(folder) / "bookmarks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_bookmarks(folder, marks):
    (pathlib.Path(folder) / "bookmarks.json").write_text(json.dumps(marks, indent=1), encoding="utf-8")


def demote(md):
    """Push an agent reply's headings below the export's own section headings."""
    return re.sub(r"^(#{1,4})\s", lambda m: "#" * min(len(m.group(1)) + 2, 6) + " ", md, flags=re.M)


def fmt_time(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def summary_reply(messages):
    """The latest agent reply to a request that asked for a summary, if there is one."""
    for i in range(len(messages) - 1, 0, -1):
        if messages[i]["role"] == "assistant" and messages[i - 1]["role"] == "user" \
                and "summar" in messages[i - 1]["text"].lower():
            return messages[i]["text"]
    return None


def build_markdown(folder, messages, participants, summary=True, conversation=False, transcript=True,
                   bookmarks=True):
    folder = pathlib.Path(folder)
    parts = [f"# {title(folder)}", "", f"*{describe(folder)}*", ""]
    if participants:
        parts += ["## Participants", ""] + [f"- {p}" for p in participants] + [""]
    text = summary_reply(messages) if summary else None
    if text:
        parts += ["## Summary", "", demote(text), ""]
    marks = load_bookmarks(folder) if bookmarks else []
    if marks:
        parts += ["## Bookmarks", ""] + [f"- {fmt_time(b['t'])}" + (f" - {b['note']}" if b.get("note") else "")
                                         for b in marks] + [""]
    if conversation and messages:
        parts += ["## Ask Agent conversation", ""]
        for m in messages:
            who = "**You:**" if m["role"] == "user" else "**Agent:**"
            parts += [who, "", demote(m["text"]), ""]
    if transcript:
        path = transcript_file(folder)
        if path:
            parts += ["## Transcript", ""]
            for line in path.read_text(encoding="utf-8").splitlines():
                m = LINE_RE.match(line)
                parts.append(f"**{m.group(1)}:{m.group(2)}:{m.group(3)} {m.group(4)}:** {m.group(5)}" if m else line)
                parts.append("")
    return "\n".join(parts).rstrip() + "\n"
