"""Learned voice profiles: one averaged voiceprint per named speaker, shared by every meeting.

voices.json holds just the profile the recorder matches against ({"emb", "seconds"} per name).
voice_clips.json holds the clips each profile was built from (meeting, time, words, voiceprint), so they
can be reviewed: removing a clip rebuilds that profile from the clips that are left. A profile learned
before clips were kept carries the part that couldn't be traced back to a clip as "legacy".

Pure Python on purpose: the window (system Python) and the recorder (its own venv) both use it.
"""
import datetime
import json
import math
import os
import pathlib

CONFIG_DIR = pathlib.Path(os.environ.get("MINTOMEET_CONFIG_DIR", pathlib.Path.home() / ".config/mintomeet"))
FILE = CONFIG_DIR / "voices.json"
MATCH = 0.50        # cosine similarity needed to recognise a known voice
MARGIN = 0.05       # ...and how much better than the second-best profile it must be
MAX_WEIGHT = 600.0  # seconds; caps a profile's history so it keeps adapting to new meetings
CLIPS_FILE = CONFIG_DIR / "voice_clips.json"
MAX_CLIPS = 300     # per person; the oldest go first
LEGACY_MIN = 20.0   # seconds of untraceable earlier learning worth keeping as its own item


def load():
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("emb")}
    except (OSError, ValueError):
        return {}


def save(profiles):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(profiles), encoding="utf-8")


def normalize(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / ((math.sqrt(sum(x * x for x in a)) or 1) * (math.sqrt(sum(y * y for y in b)) or 1))


# --- clips -------------------------------------------------------------------
def load_clips():
    """{name: {"clips": [clip, ...], "legacy": {"emb", "seconds"} | None}}"""
    try:
        data = json.loads(CLIPS_FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def save_clips(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CLIPS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(CLIPS_FILE)


def clip(meeting, stream, start, end, text, emb):
    """A learned moment of speech: `meeting` is the meeting folder's name."""
    return {"id": f"{meeting}|{stream}|{start:.2f}", "meeting": meeting, "stream": stream,
            "start": round(start, 2), "end": round(end, 2), "text": text.strip(),
            "emb": [round(x, 5) for x in emb], "added": datetime.datetime.now().isoformat(timespec="seconds")}


def seconds(c):
    return max(0.1, c["end"] - c["start"])


def same_moment(a, b):
    """The same stretch of speech (also after "Transcribe again" shifted its timing a little)."""
    if a["meeting"] != b["meeting"] or a["stream"] != b["stream"]:
        return False
    overlap = min(a["end"], b["end"]) - max(a["start"], b["start"])
    return overlap > 0.5 * min(seconds(a), seconds(b))


def _entry(data, name, profiles):
    if name not in data:
        old = profiles.get(name)
        data[name] = {"clips": [], "legacy": {"emb": old["emb"], "seconds": old.get("seconds", 1.0)} if old else None}
    return data[name]


def _rebuild(data, profiles, name):
    """Recompute `name`'s profile from its clips (and legacy part); drop it when nothing is left."""
    entry = data.get(name) or {"clips": [], "legacy": None}
    items = [(c["emb"], seconds(c)) for c in entry["clips"]]
    if entry.get("legacy"):
        items.append((entry["legacy"]["emb"], min(entry["legacy"]["seconds"], MAX_WEIGHT)))
    if not items:
        profiles.pop(name, None)
        data.pop(name, None)
        return
    acc = [0.0] * len(items[0][0])
    for emb, w in items:
        if len(emb) == len(acc):
            acc = [a + x * w for a, x in zip(acc, emb)]
    total = sum(seconds(c) for c in entry["clips"]) + (entry["legacy"]["seconds"] if entry.get("legacy") else 0)
    profiles[name] = {"emb": normalize(acc), "seconds": round(total, 1)}


def _take_out(data, new_clips, keep=None):
    """Remove every stored clip that is the same moment as one of `new_clips` (a moment of speech
    belongs to one person). Returns the names whose clips changed."""
    touched = set()
    for name, entry in data.items():
        if name == keep:
            continue
        kept = [c for c in entry["clips"] if not any(same_moment(c, n) for n in new_clips)]
        if len(kept) != len(entry["clips"]):
            entry["clips"] = kept
            touched.add(name)
    return touched


def learn(name, samples):
    """Add clips (see clip()) to `name`'s voice and rebuild it. The same moment is moved away from anyone
    else it was learned for, and learning a moment twice changes nothing. Plain (embedding, seconds)
    pairs without a source are folded into the legacy part. Returns seconds learned."""
    clips = [c for c in samples if isinstance(c, dict) and c.get("emb")]
    loose = [(e, s) for e, s in (x for x in samples if not isinstance(x, dict)) if e]
    if not clips and not loose:
        return 0.0
    data, profiles = load_clips(), load()
    entry = _entry(data, name, profiles)
    touched = _take_out(data, clips, keep=name) | {name}
    for c in clips:
        entry["clips"] = [old for old in entry["clips"] if not same_moment(old, c)] + [c]
    entry["clips"] = sorted(entry["clips"], key=lambda c: c.get("added", ""))[-MAX_CLIPS:]
    if loose:
        leg = entry.get("legacy") or {"emb": [0.0] * len(loose[0][0]), "seconds": 0.0}
        acc = [x * leg["seconds"] for x in leg["emb"]]
        for emb, secs in loose:
            acc = [a + x * secs for a, x in zip(acc, emb)]
        entry["legacy"] = {"emb": normalize(acc), "seconds": round(leg["seconds"] + sum(s for _, s in loose), 1)}
    for n in touched:
        _rebuild(data, profiles, n)
    save_clips(data)
    save(profiles)
    return sum(seconds(c) for c in clips) + sum(s for _, s in loose)


def unlearn(samples):
    """These moments are you or an unknown voice: take them out of every learned voice."""
    clips = [c for c in samples if isinstance(c, dict)]
    if not clips:
        return
    data, profiles = load_clips(), load()
    touched = _take_out(data, clips)
    for n in touched:
        _rebuild(data, profiles, n)
    if touched:
        save_clips(data)
        save(profiles)


def remove_clips(name, ids, legacy=False):
    """Drop clips (and with `legacy`, the untraceable earlier learning) from `name`'s voice.
    Returns what was removed, for restore_clips()."""
    data, profiles = load_clips(), load()
    entry = data.get(name)
    if entry is None:
        return {"clips": [], "legacy": None}
    removed = {"clips": [c for c in entry["clips"] if c["id"] in ids],
               "legacy": entry.get("legacy") if legacy else None}
    entry["clips"] = [c for c in entry["clips"] if c["id"] not in ids]
    if legacy:
        entry["legacy"] = None
    _rebuild(data, profiles, name)
    save_clips(data)
    save(profiles)
    return removed


def restore_clips(name, removed):
    """Undo remove_clips()."""
    data, profiles = load_clips(), load()
    entry = data.setdefault(name, {"clips": [], "legacy": None})
    have = {c["id"] for c in entry["clips"]}
    entry["clips"] = sorted(entry["clips"] + [c for c in removed["clips"] if c["id"] not in have],
                            key=lambda c: c.get("added", ""))
    if removed.get("legacy"):
        entry["legacy"] = removed["legacy"]
    _rebuild(data, profiles, name)
    save_clips(data)
    save(profiles)


def review(name):
    """Clips of `name` with how well each fits: "fit" is its similarity to the voice built from all the
    *other* clips; "other"/"other_fit" the closest other learned voice. "suspect" when it fits poorly or
    sounds more like someone else."""
    data, profiles = load_clips(), load()
    entry = data.get(name) or {"clips": [], "legacy": None}
    items = [(c["emb"], seconds(c)) for c in entry["clips"]]
    if entry.get("legacy"):
        items.append((entry["legacy"]["emb"], min(entry["legacy"]["seconds"], MAX_WEIGHT)))
    if not items:
        return []
    dim = len(items[0][0])
    total = [0.0] * dim
    for emb, w in items:
        if len(emb) == dim:
            total = [a + x * w for a, x in zip(total, emb)]
    others = {n: p["emb"] for n, p in profiles.items() if n != name and len(p["emb"]) == dim}
    out = []
    for c in entry["clips"]:
        w = seconds(c)
        rest = [t - x * w for t, x in zip(total, c["emb"])]
        fit = cosine(c["emb"], rest) if any(rest) and len(items) > 1 else 1.0
        best = max(((cosine(c["emb"], e), n) for n, e in others.items()), default=(0.0, None))
        out.append(dict(c, fit=fit, other=best[1], other_fit=best[0],
                        suspect=fit < MATCH or (best[1] is not None and best[0] > fit)))
    return out


def migrate(meetings_root):
    """Once: rebuild the clip list of voices learned before clips were kept, from each meeting's speaker
    choices (speaker_map.json) and voiceprints (segments.json). What can't be traced stays as legacy.
    Profiles themselves are left as they are until something changes."""
    if CLIPS_FILE.exists():
        return False
    profiles = load()
    data = {}
    root = pathlib.Path(meetings_root)
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        try:
            names = json.loads((folder / "speaker_map.json").read_text(encoding="utf-8"))
            segments = json.loads((folder / "segments.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stamp_dt = datetime.datetime.fromtimestamp((folder / "speaker_map.json").stat().st_mtime)
        for key, name in names.items():
            if name not in profiles or "|" not in key:
                continue
            stamp, text = key.split("|", 1)
            try:
                h, m, sec = map(int, stamp.split(":"))
            except ValueError:
                continue
            t = h * 3600 + m * 60 + sec
            seg = next((g for g in segments if int(g["start"]) == t and g.get("emb")
                        and g["text"].strip()[:40] == text.strip()[:40]), None)
            if seg is None or len(seg["emb"]) != len(profiles[name]["emb"]):
                continue
            c = clip(folder.name, seg.get("stream", "Them"), seg["start"], seg["end"], seg["text"], seg["emb"])
            c["added"] = stamp_dt.isoformat(timespec="seconds")
            _take_out(data, [c])
            entry = data.setdefault(name, {"clips": [], "legacy": None})
            entry["clips"] = [o for o in entry["clips"] if not same_moment(o, c)] + [c]
    for name, p in profiles.items():
        entry = data.setdefault(name, {"clips": [], "legacy": None})
        residual = p.get("seconds", 0.0) - sum(seconds(c) for c in entry["clips"])
        if residual >= LEGACY_MIN or not entry["clips"]:
            entry["legacy"] = {"emb": p["emb"], "seconds": round(max(residual, 1.0), 1)}
        # same voiceprint, but the total now counts each clip once (it used to double-count)
        p["seconds"] = round(sum(seconds(c) for c in entry["clips"]) +
                             (entry["legacy"]["seconds"] if entry["legacy"] else 0), 1)
    save_clips(data)
    save(profiles)
    return True


def forget(name):
    profiles = load()
    if profiles.pop(name, None) is not None:
        save(profiles)
    data = load_clips()
    if data.pop(name, None) is not None:
        save_clips(data)


def match(centroids, profiles=None):
    """Map cluster index -> profile name for clusters that clearly sound like a known person.

    Each profile is used at most once (the most similar cluster wins).
    """
    profiles = load() if profiles is None else profiles
    pairs = []
    for i, c in enumerate(centroids):
        scores = sorted(((cosine(c, p["emb"]), name) for name, p in profiles.items()), reverse=True)
        if scores and scores[0][0] >= MATCH and (len(scores) == 1 or scores[0][0] - scores[1][0] >= MARGIN):
            pairs.append((scores[0][0], i, scores[0][1]))
    result, used = {}, set()
    for _score, i, name in sorted(pairs, reverse=True):
        if name not in used and i not in result:
            result[i] = name
            used.add(name)
    return result
