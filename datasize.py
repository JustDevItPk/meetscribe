"""How much disk space MeetScribe's data takes: each meeting (split into audio, transcripts, earlier
versions, chat), the speech models, learned voices and settings, and the app itself.

Pure Python (no GTK) so it can be tested on its own. Sizes are what the files take on disk.
"""
import os
import pathlib
import shutil
import time

DATA = pathlib.Path.home() / ".local/share/meetscribe"
CONFIG_DIR = pathlib.Path(os.environ.get("MEETSCRIBE_CONFIG_DIR", pathlib.Path.home() / ".config/meetscribe"))
HF_HUB = pathlib.Path(os.environ.get("HF_HUB_CACHE") or
                      pathlib.Path(os.environ.get("HF_HOME", pathlib.Path.home() / ".cache/huggingface")) / "hub")

# Warn when these are crossed (shown in the header and the storage overview).
WARN_MEETINGS = 5 * 2**30       # all meetings together
WARN_FREE_BYTES = 5 * 2**30     # free space left on the disk
WARN_FREE_FRACTION = 0.10

MEETING_PARTS = [("audio", "Audio recordings"), ("transcript", "Transcripts"),
                 ("versions", "Earlier transcripts"), ("chat", "Chat"), ("other", "Other files")]
STATIC_TTL = 600                # seconds to reuse model/app sizes (they rarely change)
_static = {"at": 0.0, "value": None}


def fmt(n):
    if n < 1024:
        return f"{n} B" if n else "0 B"
    for unit, scale in (("GB", 2**30), ("MB", 2**20), ("KB", 2**10)):
        if n >= scale:
            v = n / scale
            return f"{v:.1f} {unit}" if v < 10 and unit != "KB" else f"{v:.0f} {unit}"
    return f"{n} B"


def _file(path):
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


def tree(path):
    """Bytes in a file or folder (symlinks counted as links, not followed)."""
    path = pathlib.Path(path)
    if not path.is_dir():
        return _file(path) if path.exists() or path.is_symlink() else 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            total += _file(os.path.join(root, f))
    return total


def meeting_part(name):
    """Which part of a meeting a file at the top of its folder belongs to."""
    if name.endswith((".ogg", ".wav", ".mp3", ".m4a", ".flac")):
        return "audio"
    if name.startswith("transcript.before-") or name == "versions":
        return "versions"
    if name.startswith("transcript") or name == "segments.json":
        return "transcript"
    if name.startswith("chat."):
        return "chat"
    return "other"


def meeting(folder):
    """{part: bytes, ..., "total": bytes} for one meeting folder."""
    sizes = {key: 0 for key, _ in MEETING_PARTS}
    try:
        entries = list(os.scandir(folder))
    except OSError:
        entries = []
    for e in entries:
        sizes[meeting_part(e.name)] += tree(e.path) if e.is_dir(follow_symlinks=False) else _file(e.path)
    sizes["total"] = sum(sizes[k] for k, _ in MEETING_PARTS)
    return sizes


def meeting_line(sizes):
    """"24 MB on disk · audio 22 MB · transcripts 1.6 MB · …" (parts that hold something)."""
    parts = [f"{label.lower()} {fmt(sizes[key])}" for key, label in MEETING_PARTS if sizes[key] >= 1024]
    return f"{fmt(sizes['total'])} on disk" + (" · " + " · ".join(parts) if parts else "")


def hf_repo(path):
    """Size of a Hugging Face cache repo: the blobs its snapshots point to (shared blobs once)."""
    seen, total = set(), 0
    for root, _dirs, files in os.walk(pathlib.Path(path) / "snapshots"):
        for f in files:
            p = os.path.join(root, f)
            try:
                real = os.path.realpath(p)
                if real not in seen:
                    seen.add(real)
                    total += os.stat(real).st_size
            except OSError:
                pass
    for sub in ("blobs",):      # repos stored without the shared blob layout
        if not (pathlib.Path(path) / "snapshots").exists():
            total += tree(pathlib.Path(path) / sub)
    return total


def models():
    """[(label, path, bytes, used_by_meetscribe)] for every speech model on disk."""
    out = []
    mdir = DATA / "models"
    if mdir.is_dir():
        for e in sorted(mdir.iterdir()):
            if e.name.startswith("sherpa-onnx-nemo-parakeet"):
                label = "Parakeet"
            elif e.name.startswith("speaker-"):
                label = "Speaker recognition"
            else:
                label = e.name
            out.append((label, e, tree(e), True))
    import modelcatalog     # lazy: modelcatalog uses this module for sizes
    names = {"models--" + m.repo.replace("/", "--"): m.name for m in modelcatalog.MODELS if m.repo}
    if HF_HUB.is_dir():
        for e in sorted(HF_HUB.iterdir()):
            if not e.name.startswith("models--") or "whisper" not in e.name.lower():
                continue
            label = names.get(e.name)
            pretty = e.name.split("--")[-1]
            out.append((label or f"{pretty} (not used by MeetScribe)", e, hf_repo(e), label is not None))
    return out


def app_parts():
    """[(label, path, bytes)] for the app itself (everything in its folder except the models)."""
    names = {"venv": "Python packages (speech engines)", "python": "Python runtime", "uv": "Package installer"}
    out, code = [], 0
    if DATA.is_dir():
        for e in DATA.iterdir():
            if e.name == "models":
                continue
            if e.name in names:
                out.append((names[e.name], e, tree(e)))
            else:
                code += tree(e)
    out.append(("MeetScribe itself", DATA, code))
    return sorted(out, key=lambda x: -x[2])


def invalidate():
    """Forget the cached model/app sizes (after a model is downloaded or deleted)."""
    _static["value"] = None


def _static_parts(force=False):
    if force or _static["value"] is None or time.monotonic() - _static["at"] > STATIC_TTL:
        _static["value"] = {"models": models(), "app": app_parts()}
        _static["at"] = time.monotonic()
    return _static["value"]


def settings_parts():
    """[(label, [paths], bytes)] for learned voices, the speaker list and settings."""
    groups = [("Learned voices", ["voices.json", "voice_clips.json"]),
              ("Speaker list", ["speakers.json"]), ("Settings", ["settings.json"])]
    known = {n for _, names in groups for n in names}
    out = [(label, [CONFIG_DIR / n for n in names], sum(_file(CONFIG_DIR / n) for n in names)) for label, names in groups]
    if CONFIG_DIR.is_dir():
        rest = [e for e in CONFIG_DIR.iterdir() if e.name not in known]
        if rest:
            out.append(("Other", rest, sum(tree(e) for e in rest)))
    return out


def overview(meetings_dir, force=False):
    """Everything, with totals and warnings. Meetings and settings are measured fresh each time;
    models and the app are reused for a few minutes unless `force`."""
    root = pathlib.Path(meetings_dir)
    folders = sorted((e for e in root.iterdir() if e.is_dir()), key=lambda e: e.name, reverse=True) \
        if root.is_dir() else []
    per = {f: meeting(f) for f in folders}
    parts = {key: sum(s[key] for s in per.values()) for key, _ in MEETING_PARTS}
    loose = sum(_file(e) for e in root.iterdir() if e.is_file()) if root.is_dir() else 0
    meetings_total = sum(parts.values()) + loose
    static = _static_parts(force)
    settings = settings_parts()
    totals = {"meetings": meetings_total,
              "models": sum(s for _, _, s, _ in static["models"]),
              "settings": sum(s for _, _, s in settings),
              "app": sum(s for _, _, s in static["app"])}
    try:
        disk = shutil.disk_usage(root if root.is_dir() else pathlib.Path.home())
    except OSError:
        disk = None
    warnings = []
    if meetings_total >= WARN_MEETINGS:
        warnings.append(f"Meetings take {fmt(meetings_total)}. Consider moving old recordings off this computer "
                        "or deleting ones you no longer need.")
    if disk and (disk.free < WARN_FREE_BYTES or disk.free < disk.total * WARN_FREE_FRACTION):
        warnings.append(f"Only {fmt(disk.free)} free on this disk.")
    unused = [(label, s) for label, _, s, used in static["models"] if not used and s]
    return {"meetings": per, "meeting_parts": parts, "meetings_dir": root, "models": static["models"],
            "app": static["app"], "settings": settings, "totals": totals, "total": sum(totals.values()),
            "disk": disk, "warnings": warnings, "unused_models": unused}
