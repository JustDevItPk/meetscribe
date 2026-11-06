"""The speech models MeetScribe can use: what each needs, how well it would run on this computer, and
whether it's downloaded.

Pure Python (no GTK, no speech libraries) so both the window and the recorder can use it. Downloading
happens in the recorder's Python (`worker.py --download <id>`); deleting is plain file removal, here.

Speeds are "times faster than speech": 2.0 means a 10-minute stretch of talk takes 5 minutes to
transcribe. Live transcription needs comfortably more than 1. The reference figures were measured (Parakeet,
Whisper small and medium) or estimated from model size (the rest) on an 8-core / 16-thread laptop CPU
(AMD Ryzen 7 4750U), and are scaled to this computer's cores. They're estimates, not promises.
"""
import os
import pathlib
import shutil
from dataclasses import dataclass

DATA = pathlib.Path.home() / ".local/share/meetscribe"
MODELS_DIR = DATA / "models"
HF_HUB = pathlib.Path(os.environ.get("HF_HUB_CACHE") or
                      pathlib.Path(os.environ.get("HF_HOME", pathlib.Path.home() / ".cache/huggingface")) / "hub")
PARAKEET_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"

GB = 2**30
MB = 2**20
REF_THREADS = 4          # Whisper's reference speeds were measured with this many threads (Parakeet: 2)
REF_CORES = 8            # physical cores of the reference laptop
HEADROOM_GB = 2.5        # memory left for the system and the call app
LIVE_GOOD = 2.5          # at least this much faster than speech: keeps up live with room to spare
LIVE_OK = 1.3            # at least this: keeps up live, may fall behind during long monologues
ALL_LANGUAGES = "About 100 languages, including Urdu, Arabic, Hindi and Chinese"


@dataclass(frozen=True)
class Model:
    id: str              # model id as stored in meeting.json ("parakeet", "whisper-small", ...)
    engine: str          # "parakeet" or "whisper": the recorder's --engine
    model: str | None    # the recorder's --model (faster-whisper size name) for Whisper
    name: str
    languages: str
    multilingual: bool   # works for languages beyond English and the European ones
    download_mb: int     # on disk once downloaded
    ram_gb: float        # memory it needs while transcribing (about)
    speed: float         # times faster than speech at REF_THREADS threads on the reference laptop
    threads: int         # threads it uses while recording (Whisper: up to this many)
    accuracy: str
    rank: int            # higher is more accurate (for picking a suggestion)
    by: str
    license: str
    repo: str | None = None      # Hugging Face repository (Whisper)
    measured: bool = False       # speed measured rather than estimated


MODELS = [
    Model("parakeet", "parakeet", None, "Parakeet TDT 0.6B v3", "English and 24 European languages, detected "
          "automatically", False, 640, 1.2, 7.0, 2, "Excellent for English and European languages", 8,
          "NVIDIA", "CC-BY-4.0", measured=True),
    Model("whisper-tiny", "whisper", "tiny", "Whisper tiny", ALL_LANGUAGES, True, 75, 0.5, 25.0, 4,
          "Rough: fine for skimming, misses many words", 1, "OpenAI", "MIT", "Systran/faster-whisper-tiny"),
    Model("whisper-base", "whisper", "base", "Whisper base", ALL_LANGUAGES, True, 141, 0.6, 14.0, 4,
          "Basic: clear speech in a quiet room", 2, "OpenAI", "MIT", "Systran/faster-whisper-base"),
    Model("whisper-small", "whisper", "small", "Whisper small", ALL_LANGUAGES, True, 464, 1.0, 5.7, 4,
          "Good", 4, "OpenAI", "MIT", "Systran/faster-whisper-small", measured=True),
    Model("whisper-medium", "whisper", "medium", "Whisper medium", ALL_LANGUAGES, True, 1460, 2.5, 1.9, 4,
          "Very good", 6, "OpenAI", "MIT", "Systran/faster-whisper-medium", measured=True),
    Model("whisper-large-v3-turbo", "whisper", "large-v3-turbo", "Whisper large-v3 turbo", ALL_LANGUAGES, True,
          1547, 2.7, 1.6, 4, "Excellent, close to large-v3", 7, "OpenAI", "MIT",
          "mobiuslabsgmbh/faster-whisper-large-v3-turbo"),
    Model("whisper-large-v3", "whisper", "large-v3", "Whisper large-v3", ALL_LANGUAGES, True, 2948, 4.8, 0.9, 4,
          "The most accurate Whisper", 9, "OpenAI", "MIT", "Systran/faster-whisper-large-v3"),
    Model("whisper-distil-large-v3", "whisper", "distil-large-v3", "Distil-Whisper large-v3", "English only", False,
          1446, 2.5, 2.2, 4, "Excellent for English, faster than large-v3", 7, "Hugging Face (from OpenAI Whisper)",
          "MIT", "Systran/faster-distil-whisper-large-v3"),
]
BY_ID = {m.id: m for m in MODELS}
DEFAULT = "parakeet"


def get(model_id):
    return BY_ID.get(model_id)


def by_engine(engine, model=None):
    """The catalog entry for the recorder's --engine / --model."""
    if engine == "parakeet":
        return BY_ID["parakeet"]
    return BY_ID.get(f"whisper-{model}")


# --- this computer ----------------------------------------------------------------
@dataclass(frozen=True)
class Specs:
    cpu: str
    cores: int           # physical cores
    threads: int         # logical CPUs
    ram_total: int       # bytes
    ram_available: int   # bytes
    disk_free: int       # bytes free where models are saved


def _meminfo():
    out = {}
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            out[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


def _cpuinfo():
    name, cores = "", set()
    phys = core = None
    try:
        for line in pathlib.Path("/proc/cpuinfo").read_text().splitlines():
            key, _, value = (s.strip() for s in line.partition(":"))
            if key == "model name" and not name:
                name = value
            elif key == "physical id":
                phys = value
            elif key == "core id":
                core = value
            elif not key and core is not None:
                cores.add((phys, core))
                phys = core = None
        if core is not None:
            cores.add((phys, core))
    except OSError:
        pass
    return " ".join(name.split()), len(cores)


def _disk_free(path):
    path = pathlib.Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def detect():
    """This computer's processor, memory and free disk space."""
    threads = os.cpu_count() or 1
    name, cores = _cpuinfo()
    mem = _meminfo()
    total = mem.get("MemTotal", 0)
    return Specs(cpu=name or "Unknown processor", cores=cores or max(1, threads // 2), threads=threads,
                 ram_total=total, ram_available=mem.get("MemAvailable", total),
                 disk_free=min(_disk_free(MODELS_DIR), _disk_free(HF_HUB)))


def live_threads(model, specs):
    """Threads the recorder gives this model while recording (same rule as worker.live_threads)."""
    if model.engine == "parakeet":
        return 2
    return min(model.threads, max(2, specs.threads // 2))


def _thread_scale(threads, cores):
    """How speed scales with threads, counting only real cores (hyper-threads add little)."""
    usable = max(1, min(threads, cores))
    if usable <= REF_THREADS:
        return (usable / REF_THREADS) ** 0.85
    return 1 + 0.25 * ((usable / REF_THREADS) - 1)      # measured: 8 threads only ~1.1-1.2x faster than 4


def speed(model, specs, live=True):
    """Estimated times-faster-than-speech on this computer, live (while recording) or afterwards."""
    threads = live_threads(model, specs) if live else max(2, specs.threads // 2)
    ref = REF_THREADS if model.engine == "whisper" else 2
    return model.speed * _thread_scale(threads, specs.cores) / _thread_scale(ref, REF_CORES)


def processing_factor(model, specs):
    """Rough time to transcribe again after a meeting, as a fraction of the meeting's length."""
    return 0.08 + 1 / max(0.1, speed(model, specs, live=False))


@dataclass(frozen=True)
class Fit:
    level: str           # "great", "ok", "slow" or "no"
    label: str           # short verdict for the list
    reason: str          # one sentence of explanation


def fit(model, specs, downloaded=False):
    """How well a model suits this computer."""
    need = model.ram_gb * GB
    if specs.ram_total and specs.ram_total < need + HEADROOM_GB * GB:
        return Fit("no", "Not enough memory",
                   f"It needs about {model.ram_gb:g} GB of memory on top of your other apps, and this computer has "
                   f"{specs.ram_total / GB:.0f} GB in total.")
    if not downloaded and specs.disk_free and specs.disk_free < model.download_mb * MB + GB:
        return Fit("no", "Not enough disk space",
                   f"It needs {model.download_mb / 1024:.1f} GB and only {specs.disk_free / GB:.1f} GB is free.")
    s = speed(model, specs)
    after = speed(model, specs, live=False)
    if s >= LIVE_GOOD:
        return Fit("great", "Runs well on this computer",
                   f"Keeps up live with room to spare (about {s:.0f}× faster than speech).")
    if s >= LIVE_OK:
        return Fit("ok", "Works, may fall a little behind live",
                   f"About {s:.1f}× faster than speech: during long stretches of talk it may lag and catch up in "
                   "pauses.")
    return Fit("slow", "Too slow for live use here",
               f"About {s:.1f}× faster than speech while recording, so the transcript would fall behind. Fine for "
               f"“Transcribe again” after a meeting (about {after:.1f}× faster than speech).")


def suggestions(specs):
    """{model id: (badge, explanation)} for the best choice on this computer for English and European languages,
    and for every other language."""
    out = {}
    for multilingual, badge in ((False, ("Best for English", "The most accurate model that runs well on this computer "
                                                             "for English and European languages")),
                                (True, ("Best for other languages", "The most accurate model that runs well on this "
                                                                     "computer for Urdu, Arabic, Hindi, Chinese and "
                                                                     "other languages"))):
        pool = [m for m in MODELS if m.multilingual == multilingual or (not multilingual and m.engine == "parakeet")]
        for level in ("great", "ok"):
            fitting = [m for m in pool if fit(m, specs, downloaded=True).level == level]
            if fitting:
                out.setdefault(max(fitting, key=lambda m: m.rank).id, badge)
                break
    return out


# --- on disk ----------------------------------------------------------------------
def _hf_dir(model):
    return HF_HUB / ("models--" + model.repo.replace("/", "--"))


def files_path(model):
    """Where the model's files live (whether or not they're there yet)."""
    return MODELS_DIR / PARAKEET_NAME if model.engine == "parakeet" else _hf_dir(model)


def is_downloaded(model):
    if model.engine == "parakeet":
        return (MODELS_DIR / PARAKEET_NAME / "tokens.txt").exists()
    snaps = _hf_dir(model) / "snapshots"
    try:
        return any((s / "model.bin").exists() and (s / "config.json").exists() for s in snaps.iterdir())
    except OSError:
        return False


def size_on_disk(model):
    import datasize     # lazy: datasize uses this module to label models
    path = files_path(model)
    if not path.exists():
        return 0
    return datasize.tree(path) if model.engine == "parakeet" else datasize.hf_repo(path)


def downloaded():
    """Downloaded models, in catalog order."""
    return [m for m in MODELS if is_downloaded(m)]


def _links_into(folder, shared):
    """Real paths of files under `shared` that symlinks in `folder` point to."""
    out = set()
    for root, _dirs, files in os.walk(folder):
        for f in files:
            p = os.path.join(root, f)
            if os.path.islink(p):
                real = os.path.realpath(p)
                if real.startswith(str(shared) + os.sep):
                    out.add(real)
    return out


def delete(model_id):
    """Remove a model's files (and any half-finished download) and return the bytes freed.

    Only that model's own folder is removed. Newer Hugging Face downloads keep big files in a shared
    hub/blobs folder that models link to; those are removed too, but only if no other model links to them."""
    model = BY_ID[model_id]
    path = files_path(model)
    root = MODELS_DIR if model.engine == "parakeet" else HF_HUB
    if path.parent.resolve() != root.resolve() or not path.name.startswith(
            (PARAKEET_NAME,) if model.engine == "parakeet" else ("models--",)):
        raise ValueError(f"Refusing to delete {path}: not a model folder")
    import datasize     # lazy: datasize uses this module to label models
    freed = datasize.tree(path) if path.exists() else 0
    shared = (HF_HUB / "blobs").resolve()
    candidates = _links_into(path, shared) if model.engine == "whisper" else set()
    shutil.rmtree(path, ignore_errors=True)
    if model.engine == "whisper":
        shutil.rmtree(HF_HUB / ".locks" / path.name, ignore_errors=True)
        if candidates:
            still_used = set()
            for other in HF_HUB.glob("models--*"):
                still_used |= _links_into(other, shared)
            for blob in candidates - still_used:
                try:
                    freed += os.path.getsize(blob)
                    os.unlink(blob)
                    os.rmdir(os.path.dirname(blob))     # the shared folder's subfolder, if now empty
                except OSError:
                    pass
    return freed
