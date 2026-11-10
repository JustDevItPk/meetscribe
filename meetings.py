"""Meeting folders: titles, the meeting list, bookmarks and the Markdown used for exports. Pure Python."""
import datetime
import json
import pathlib
import re

META = "meeting.json"
LINE_RE = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\] ([^:\[\]]+?): (.*)$")
VERSIONS_DIR = "versions"       # earlier transcripts: versions/<YYYYmmdd-HHMMSS>/ with version.json
VERSION_FILES = ("transcript.txt", "transcript.original.txt", "segments.json")
STAMP = "%Y%m%d-%H%M%S"
LEGACY_RE = re.compile(r"^transcript\.before-(\w+)-(\d{8}-\d{6})\.txt$")    # backups from older versions
MODEL_NAMES = {
    "parakeet": "Parakeet TDT 0.6B v3",
    "whisper": "Whisper",               # older meetings didn't record the Whisper size
    "whisper-tiny": "Whisper tiny",
    "whisper-base": "Whisper base",
    "whisper-small": "Whisper small",
    "whisper-medium": "Whisper medium",
    "whisper-large-v3": "Whisper large-v3",
    "whisper-large-v3-turbo": "Whisper large-v3 turbo",
    "whisper-distil-large-v3": "Distil-Whisper large-v3",
}


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


# --- tags ("tags" in meeting.json) --------------------------------------------------------------------
TAG_MAX = 40


def normalize_tag(text):
    """A tag as stored: single spaces, no commas (they separate tags when typing), at most TAG_MAX characters."""
    return " ".join(str(text).replace(",", " ").split())[:TAG_MAX].strip()


def _unique_tags(values):
    """Normalized tags without repeats; tags differing only in case count as one (the first spelling wins)."""
    out, seen = [], set()
    for v in values:
        t = normalize_tag(v)
        if t and t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
    return out


def tags(folder):
    value = load_meta(folder).get("tags")
    return _unique_tags(value) if isinstance(value, list) else []


def set_tags(folder, values):
    """Replace a meeting's tags; returns them as stored."""
    clean = _unique_tags(values)
    save_meta(folder, tags=clean)
    return clean


def add_tag(folder, tag):
    return set_tags(folder, tags(folder) + [tag])


def remove_tag(folder, tag):
    return set_tags(folder, [t for t in tags(folder) if t.casefold() != normalize_tag(tag).casefold()])


def count_tags(tag_lists):
    """{tag: number of meetings} over several meetings' tag lists, most used first. Spellings that differ
    only in case are counted together under the most common one."""
    counts, spellings = {}, {}
    for lst in tag_lists:
        for t in lst:
            key = t.casefold()
            counts[key] = counts.get(key, 0) + 1
            spellings.setdefault(key, {}).setdefault(t, 0)
            spellings[key][t] += 1
    named = {max(spellings[k].items(), key=lambda kv: kv[1])[0]: n for k, n in counts.items()}
    return dict(sorted(named.items(), key=lambda kv: (-kv[1], kv[0].casefold())))


def all_tags(root):
    return count_tags(tags(f) for f in list_meetings(root))


def rename_tag(root, old, new):
    """Rename a tag in every meeting (merging into `new` if a meeting has both). Returns meetings changed."""
    old_key, new = normalize_tag(old).casefold(), normalize_tag(new)
    if not old_key or not new:
        return 0
    changed = 0
    for f in list_meetings(root):
        current = tags(f)
        if any(t.casefold() == old_key for t in current):
            set_tags(f, [new if t.casefold() == old_key else t for t in current])
            changed += 1
    return changed


def delete_tag(root, tag):
    """Take a tag off every meeting. Returns the folders it was removed from (to put it back on undo)."""
    key = normalize_tag(tag).casefold()
    removed = []
    for f in list_meetings(root):
        current = tags(f)
        if any(t.casefold() == key for t in current):
            set_tags(f, [t for t in current if t.casefold() != key])
            removed.append(f)
    return removed


# --- transcript versions (one per speech model run) -------------------------------------------------
def model_id(engine, model=None):
    """'parakeet', 'whisper-medium', ... from the recorder's --engine / --model."""
    return f"whisper-{model}" if engine == "whisper" and model else engine


def model_name(mid):
    if not mid:
        return "an unknown model"
    if "+" in mid:      # switched models during the meeting
        return " + ".join(model_name(part) for part in mid.split("+"))
    return MODEL_NAMES.get(mid) or (f"Whisper {mid[8:]}" if mid.startswith("whisper-") else mid)


def _iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def _parse(value):
    try:
        return datetime.datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _legacy_chain(folder):
    """Work out who made the old transcript.before-*.txt backups.

    A backup is named after the engine that *replaced* it, so the first one came from the recording
    (meeting.json "engine") and each later one from the engine named on the backup before it.
    Returns ([(stamp, path, model, transcribed)], model of the current transcript, when it was made).
    """
    folder = pathlib.Path(folder)
    found = []
    for p in folder.glob("transcript.before-*.txt"):
        m = LEGACY_RE.match(p.name)
        if m:
            try:
                found.append((datetime.datetime.strptime(m.group(2), STAMP), m.group(2), p, m.group(1)))
            except ValueError:
                continue
    meta = load_meta(folder)
    made_by = meta.get("engine")
    start = _parse(meta.get("started"))
    made_at = start + datetime.timedelta(seconds=meta["duration"]) if start and meta.get("duration") else start
    chain = []
    for when, stamp, path, replaced_by in sorted(found):
        chain.append((stamp, path, made_by, made_at))
        made_by, made_at = replaced_by, when
    return chain, made_by, made_at


def current_model(folder):
    """(model id or None, when it was transcribed or None) for the meeting's current transcript."""
    meta = load_meta(folder)
    if meta.get("model"):
        return meta["model"], _parse(meta.get("transcribed"))
    _chain, model, when = _legacy_chain(folder)
    return model, _parse(meta.get("transcribed")) or when


def list_versions(folder):
    """Earlier transcripts of this meeting, newest first.

    Each is a dict: id, path (its transcript file), segments (path or None), model, transcribed, replaced.
    """
    folder = pathlib.Path(folder)
    out = []
    root = folder / VERSIONS_DIR
    if root.is_dir():
        for d in root.iterdir():
            if not (d / "transcript.txt").exists():
                continue
            try:
                info = json.loads((d / "version.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = {}
            out.append({"id": d.name, "path": d / "transcript.txt",
                        "segments": d / "segments.json" if (d / "segments.json").exists() else None,
                        "model": info.get("model"), "transcribed": _parse(info.get("transcribed")),
                        "replaced": _parse(info.get("replaced"))})
    for stamp, path, model, made_at in _legacy_chain(folder)[0]:
        out.append({"id": stamp, "path": path, "segments": None, "model": model, "transcribed": made_at,
                    "replaced": datetime.datetime.strptime(stamp, STAMP)})
    return sorted(out, key=lambda v: v["replaced"] or datetime.datetime.min, reverse=True)


def _version_dir(folder, stamp):
    d = pathlib.Path(folder) / VERSIONS_DIR / stamp
    n = 2
    while d.exists():
        d = d.with_name(f"{stamp}-{n}")
        n += 1
    d.mkdir(parents=True)
    return d


def _write_version(d, model, transcribed, replaced):
    (d / "version.json").write_text(json.dumps({"model": model, "transcribed": _iso(transcribed),
                                                "replaced": _iso(replaced)}, indent=1), encoding="utf-8")


def migrate_legacy(folder):
    """Move transcript.before-*.txt backups into versions/ (same ids), recording who made each one."""
    folder = pathlib.Path(folder)
    chain, model, made_at = _legacy_chain(folder)
    for stamp, path, by, at in chain:
        d = _version_dir(folder, stamp)
        path.rename(d / "transcript.txt")
        _write_version(d, by, at, datetime.datetime.strptime(stamp, STAMP))
    meta = load_meta(folder)
    if chain and not meta.get("model"):
        save_meta(folder, model=model, transcribed=meta.get("transcribed") or _iso(made_at))


def archive_current(folder):
    """Move the current transcript (with its segments and unedited copy) into versions/. Returns the id."""
    folder = pathlib.Path(folder)
    migrate_legacy(folder)
    if not (folder / "transcript.txt").exists():
        return None
    model, when = current_model(folder)
    now = datetime.datetime.now()
    d = _version_dir(folder, now.strftime(STAMP))
    for name in VERSION_FILES:
        if (folder / name).exists():
            (folder / name).rename(d / name)
    _write_version(d, model, when, now)
    return d.name


def restore_version(folder, version_id):
    """Make an earlier transcript the current one again; the current one becomes an earlier version."""
    folder = pathlib.Path(folder)
    migrate_legacy(folder)          # legacy backups keep their id when they move into versions/
    d = folder / VERSIONS_DIR / version_id
    if not (d / "transcript.txt").exists():
        raise FileNotFoundError(f"No earlier transcript called {version_id}")
    try:
        info = json.loads((d / "version.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    archive_current(folder)
    for name in VERSION_FILES:
        if (d / name).exists():
            (d / name).rename(folder / name)
    (d / "version.json").unlink(missing_ok=True)
    try:
        d.rmdir()
    except OSError:
        pass
    save_meta(folder, model=info.get("model"), transcribed=info.get("transcribed"))


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
    model = current_model(folder)[0]
    made_with = f" · Transcribed with {model_name(model)}" if model else ""
    tagged = f" · Tags: {', '.join(tags(folder))}" if tags(folder) else ""
    parts = [f"# {title(folder)}", "", f"*{describe(folder)}{made_with}{tagged}*", ""]
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
            who = "**You:**" if m["role"] == "user" else f"**{m.get('agent') or 'Agent'}:**"
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


# --- one mixed audio file ----------------------------------------------------
AUDIO_FILES = (("audio-meeting.ogg", "meeting"), ("audio-mic.ogg", "mic"))


def mixed_audio_command(folder, target):
    """ffmpeg command that mixes a meeting's recordings into one clear MP3 for listening or sharing: your
    mic's steady background noise is removed, quiet and loud voices on both sides are evened out, and the
    result is limited so it never clips. The recordings themselves are left untouched.
    Returns None when the meeting has no saved audio. Progress goes to stdout (-progress)."""
    folder = pathlib.Path(folder)
    inputs = [(folder / name, kind) for name, kind in AUDIO_FILES if (folder / name).exists()]
    if not inputs:
        return None
    cmd = ["ffmpeg", "-v", "error", "-y", "-nostats", "-progress", "pipe:1"]
    chains = []
    for i, (path, kind) in enumerate(inputs):
        cmd += ["-i", str(path)]
        clean = "highpass=f=80,afftdn=nf=-30:nr=12:tn=1," if kind == "mic" else ""
        chains.append(f"[{i}:a]{clean}dynaudnorm=f=250:g=15:m=8:p=0.9[a{i}]")
    if len(inputs) == 2:
        mix = ";".join(chains) + ";[a0][a1]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.89[out]"
    else:
        mix = chains[0].replace("[a0]", ",alimiter=limit=0.89[out]")
    return cmd + ["-filter_complex", mix, "-map", "[out]", "-ac", "1", "-ar", "16000",
                  "-c:a", "libmp3lame", "-b:a", "48k", str(target)]

