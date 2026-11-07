#!/usr/bin/env python3
"""Record meeting audio (system output + microphone), save it to disk, and transcribe it locally.

Engines:
  parakeet  NVIDIA Parakeet TDT 0.6B v3 via sherpa-onnx. Fast on CPU (about 7x real time on 2 threads),
            English + 24 European languages, automatic language detection. Transcribes live.
  whisper   faster-whisper (OpenAI Whisper). ~100 languages. On an 8-core laptop small runs ~5x and medium ~2x
            faster than speech on 4 threads, so it can transcribe live (--live, which the app always uses);
            without --live it only records during the call and transcribes after you stop.

With --control the recorder reads commands on stdin while recording. "ENGINE <parakeet|whisper> <model|-> <lang|->"
switches the speech model mid-meeting: the new one loads in the background while the current one keeps
transcribing, then takes over from the next speech chunk. Progress is reported as "SWITCH: ..." lines.

Speakers: each chunk of meeting audio gets a voiceprint (ERes2Net via sherpa-onnx). During the call chunks
are grouped online into "Speaker 1", "Speaker 2", ...; after the call all voiceprints are re-clustered and
compared with the voice profiles learned from earlier meetings (see voices.py) so known people get named.
"""
import argparse
import datetime
import difflib
import gc
import json
import os
import pathlib
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import meetings  # noqa: E402
import modelcatalog  # noqa: E402
import voices  # noqa: E402

RATE = 16000
FRAME = RATE // 10      # 100 ms of audio
LOUD = 0.006            # RMS (0..1) above which a frame counts as possible speech
END_SILENCE = 7         # quiet frames that close a chunk
MIN_CHUNK = 2.0         # seconds
MAX_CHUNK = 25.0        # seconds

DATA = pathlib.Path.home() / ".local/share/mintomeet"
PARAKEET_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
PARAKEET_URL = f"https://huggingface.co/csukuangfj/{PARAKEET_NAME}/resolve/main"
PARAKEET_FILES = ["tokens.txt", "decoder.int8.onnx", "joiner.int8.onnx", "encoder.int8.onnx"]
PARAKEET_DIR = DATA / "models" / PARAKEET_NAME
SPEAKER_FILE = "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
SPEAKER_URL = f"https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/main/{SPEAKER_FILE}"
SPEAKER_MODEL = DATA / "models" / "speaker-eres2net.onnx"
SAME_SPEAKER = 0.45     # cosine similarity above which two voiceprints are treated as one person
MIN_VOICE = 1.0         # seconds; shorter chunks get no voiceprint
MIN_CLUSTER = 2.5       # seconds; only chunks this long seed speakers in the final pass
MIN_SPEAKER_TALK = 8.0  # seconds of speech a group needs in total to count as its own speaker

_print_lock = threading.Lock()


def say(msg):
    with _print_lock:
        try:
            print(msg, flush=True)
        except (BrokenPipeError, ValueError):
            pass


def fmt(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def pactl(*args):
    return subprocess.check_output(["pactl", *args], text=True, timeout=5).strip()


# --- models ---------------------------------------------------------------
def download_parakeet(report=lambda pct: None):
    if (PARAKEET_DIR / "tokens.txt").exists():
        return PARAKEET_DIR
    PARAKEET_DIR.parent.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp(dir=PARAKEET_DIR.parent))
    try:
        sizes = {}
        for name in PARAKEET_FILES:
            with urllib.request.urlopen(urllib.request.Request(f"{PARAKEET_URL}/{name}", method="HEAD"), timeout=60) as r:
                sizes[name] = int(r.headers.get("Content-Length", 0))
        total, done, last = sum(sizes.values()), 0, -1
        for name in PARAKEET_FILES:
            with urllib.request.urlopen(f"{PARAKEET_URL}/{name}", timeout=60) as r, open(tmp / name, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    pct = done * 100 // total if total else 0
                    if pct != last:
                        report(pct)
                        last = pct
            if sizes[name] and (tmp / name).stat().st_size != sizes[name]:
                raise OSError(f"download of {name} was incomplete")
        tmp.rename(PARAKEET_DIR)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return PARAKEET_DIR


def download_speaker_model(report=lambda pct: None):
    if SPEAKER_MODEL.exists():
        return SPEAKER_MODEL
    SPEAKER_MODEL.parent.mkdir(parents=True, exist_ok=True)
    part = SPEAKER_MODEL.with_suffix(".part")
    with urllib.request.urlopen(SPEAKER_URL, timeout=60) as r, open(part, "wb") as f:
        total, done, last = int(r.headers.get("Content-Length", 0)), 0, -1
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            pct = done * 100 // total if total else 0
            if pct != last:
                report(pct)
                last = pct
    part.rename(SPEAKER_MODEL)
    return SPEAKER_MODEL


class SpeakerID:
    """Voiceprints per chunk, live grouping during the call and a careful regrouping at the end."""

    def __init__(self):
        import sherpa_onnx
        self.ex = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(SPEAKER_MODEL), num_threads=1))
        self.profiles = voices.load()
        self.live = []          # [sum vector, label] per live group

    def embed(self, audio):
        if len(audio) < RATE * MIN_VOICE:
            return None
        s = self.ex.create_stream()
        s.accept_waveform(RATE, audio)
        s.input_finished()
        e = np.array(self.ex.compute(s))
        return e / (np.linalg.norm(e) or 1)

    def live_label(self, emb, secs):
        """Label for a chunk during the call (may be refined at the end)."""
        if emb is None:
            return "Them"
        best, best_sim = None, -1.0
        for group in self.live:
            sim = float(emb @ (group[0] / np.linalg.norm(group[0])))
            if sim > best_sim:
                best, best_sim = group, sim
        if best is not None and best_sim >= SAME_SPEAKER:
            best[0] = best[0] + emb * min(secs, 10)
            return best[1]
        if secs < MIN_CLUSTER:
            return "Them"       # too short to start a new speaker
        name = voices.match([emb.tolist()], self.profiles).get(0)
        label = name if name and name not in {g[1] for g in self.live} else f"Speaker {sum(1 for g in self.live if g[1].startswith('Speaker ')) + 1}"
        self.live.append([emb * min(secs, 10), label])
        return label

    def final_labels(self, segs):
        """Re-cluster every voiceprint from the call; returns a label per segment."""
        idx = [i for i, g in enumerate(segs) if g["emb"] is not None and g["end"] - g["start"] >= MIN_CLUSTER]
        groups = [[i] for i in idx]
        cent = [segs[i]["emb"] * min(segs[i]["end"] - segs[i]["start"], 10) for i in idx]
        while len(groups) > 1:              # average-linkage agglomerative clustering on cosine similarity
            m = np.array([c / np.linalg.norm(c) for c in cent])
            sim = m @ m.T
            np.fill_diagonal(sim, -1)
            a, b = np.unravel_index(np.argmax(sim), sim.shape)
            if sim[a, b] < SAME_SPEAKER:
                break
            groups[a] += groups[b]
            cent[a] = cent[a] + cent[b]
            del groups[b], cent[b]
        cent = [c / np.linalg.norm(c) for c in cent]
        cluster_of = {}
        for k, g in enumerate(groups):
            for i in g:
                cluster_of[i] = k
        for i, g in enumerate(segs):        # short chunks join the closest speaker if clearly similar
            if i not in cluster_of and g["emb"] is not None and cent:
                sims = [float(g["emb"] @ c) for c in cent]
                if max(sims) >= SAME_SPEAKER:
                    cluster_of[i] = int(np.argmax(sims))
        talk = [0.0] * len(groups)
        for i, k in cluster_of.items():
            talk[k] += segs[i]["end"] - segs[i]["start"]
        order = sorted(range(len(groups)), key=lambda k: min(segs[i]["start"] for i in groups[k]))
        names = voices.match([cent[k].tolist() for k in range(len(groups))], self.profiles)
        label_of, n = {}, 0
        for k in order:
            if talk[k] < MIN_SPEAKER_TALK and k not in names:
                label_of[k] = "Them"        # a few seconds is too little to call it a separate person
            elif k in names:
                label_of[k] = names[k]
            else:
                n += 1
                label_of[k] = f"Speaker {n}"
        return [label_of[cluster_of[i]] if i in cluster_of else "Them" for i in range(len(segs))]


class Parakeet:
    def __init__(self, threads):
        import sherpa_onnx
        d = PARAKEET_DIR
        self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(d / "encoder.int8.onnx"), decoder=str(d / "decoder.int8.onnx"),
            joiner=str(d / "joiner.int8.onnx"), tokens=str(d / "tokens.txt"),
            model_type="nemo_transducer", num_threads=threads)

    def __call__(self, audio):
        s = self.rec.create_stream()
        s.accept_waveform(RATE, audio)
        self.rec.decode_stream(s)
        return s.result.text.strip()


class Whisper:
    def __init__(self, path, threads, language):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=threads)
        self.language = language

    def __call__(self, audio):
        segments, _ = self.model.transcribe(audio, language=self.language, vad_filter=True, beam_size=5,
                                            condition_on_previous_text=False)
        parts = [s.text.strip() for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)]
        return " ".join(p for p in parts if p).strip()


def live_threads(engine):
    """Threads while recording: Parakeet needs few; Whisper gets 4 (more barely helps and competes with the call)."""
    return 2 if engine == "parakeet" else min(4, max(2, (os.cpu_count() or 4) // 2))


def make_engine(engine, model, language, threads, report):
    if engine == "parakeet":
        download_parakeet(report)
        return Parakeet(threads)
    return Whisper(whisper_path(model, report), threads, language)


class Switcher:
    """Loads another speech engine in the background; the main loop picks it up between chunks."""

    def __init__(self, threads=None):
        self.lock = threading.Lock()
        self.threads = threads
        self.ready = None       # (engine, model id) waiting to take over
        self.generation = 0     # a newer request makes older ones void

    def request(self, engine, model, language):
        with self.lock:
            self.generation += 1
            gen = self.generation
        threading.Thread(target=self._load, args=(gen, engine, model, language), daemon=True).start()

    def _load(self, gen, engine, model, language):
        mid = meetings.model_id(engine, model)
        say(f"SWITCH: loading {mid}")
        try:
            new = make_engine(engine, model, language, self.threads or live_threads(engine),
                              lambda pct: say(f"SWITCH: downloading {mid}" + (f" pct={pct}" if pct is not None else "")))
        except Exception as e:  # noqa: BLE001 - keep the current engine and tell the UI
            say(f"SWITCH: failed {mid} {e}")
            return
        with self.lock:
            if gen == self.generation:
                self.ready = (new, mid)

    def take(self):
        with self.lock:
            ready, self.ready = self.ready, None
        return ready


def read_commands(switcher):
    """--control: commands from the app on stdin, one per line."""
    for line in sys.stdin:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "ENGINE" and parts[1] in ("parakeet", "whisper"):
            model = parts[2] if len(parts) > 2 and parts[2] != "-" else "small"
            language = parts[3] if len(parts) > 3 and parts[3] not in ("-", "auto") else None
            switcher.request(parts[1], model, language)


def download_for_library(model_id):
    """--download: fetch one model for the "Speech models" library, reporting progress on stdout as
    "STATE: downloading pct=N", then "DONE: <id>" or "ERROR: <message>". Cancelling is the app stopping this
    process and deleting the model's folder (modelcatalog.delete)."""
    model = modelcatalog.get(model_id)
    if model is None:
        say(f"ERROR: Unknown speech model “{model_id}”.")
        sys.exit(2)
    say("STATE: downloading pct=0")
    last = [0]

    def report(pct):
        if pct is not None and pct != last[0]:
            last[0] = pct
            say(f"STATE: downloading pct={min(pct, 99)}")
    try:
        download_speaker_model()        # needed by every model; normally there since install
        if model.engine == "parakeet":
            download_parakeet(report)
        else:
            from huggingface_hub import snapshot_download
            from huggingface_hub.utils import tqdm as hf_tqdm
            bars = []

            class Progress(hf_tqdm):
                """Collects the byte counts snapshot_download reports; nothing is drawn."""
                def __init__(self, *a, **k):
                    k["file"] = open(os.devnull, "w")   # noqa: SIM115 - lives as long as the bar
                    k["disable"] = False                # tqdm turns itself off without a terminal
                    super().__init__(*a, **k)
                    bars.append(self)

            result = {}

            def fetch():
                try:
                    # the same files faster-whisper fetches, so it finds the model afterwards
                    result["path"] = snapshot_download(model.repo, tqdm_class=Progress, allow_patterns=[
                        "config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"])
                except Exception as e:  # noqa: BLE001 - reported below
                    result["error"] = e
            t = threading.Thread(target=fetch, daemon=True)
            t.start()
            while t.is_alive():
                t.join(0.5)
                fractions = [b.n / b.total for b in list(bars) if getattr(b, "unit", "") == "B" and b.total]
                if fractions:
                    report(int(max(fractions) * 100))
            if "error" in result:
                raise result["error"]
    except Exception as e:  # noqa: BLE001 - the app shows it
        say(f"ERROR: Couldn't download {model.name}: {e}")
        sys.exit(1)
    say(f"DONE: {model.id}")


def whisper_path(name, report):
    from faster_whisper.utils import download_model
    try:
        return download_model(name, local_files_only=True)
    except Exception:  # noqa: BLE001 - not cached yet
        report(None)
        return download_model(name)


# --- audio capture --------------------------------------------------------
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.total = 0
        self.done = 0


class Stream(threading.Thread):
    """Reads 16 kHz mono PCM, saves it, and cuts it into speech chunks at pauses.

    `source` is either a callable returning a PulseAudio/PipeWire device name (live capture; the stream
    reconnects if the device disappears or the default device changes) or a fixed command (file mode).
    """

    # How each recording is saved. Your mic is speech only, so it uses Opus's speech mode at 16 kbps: about a
    # third smaller than 24 kbps, and transcribed just as well when tested on real recordings. Meeting audio
    # can carry music or screen-share sound, so it keeps the general-purpose setting.
    ENCODING = {"You": ["-b:a", "16k", "-application", "voip"], "Them": ["-b:a", "24k"]}
    T0 = None   # time.monotonic() when recording began, shared by the streams to keep their timelines in step
    GAP = 2     # frames (0.2 s) behind the clock before silence is filled in

    def __init__(self, label, source, out_q, stats, save_to=None):
        super().__init__(daemon=True)
        self.label, self.source, self.out_q, self.stats, self.save_to = label, source, out_q, stats, save_to
        self.proc = None
        self.device = None
        self.stopped = False
        self.peak = 0.0     # loudest frame since the last level report

    def stop(self):
        self.stopped = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def check_device(self):
        """Follow the default device: restart capture if it changed (e.g. headset plugged in)."""
        if not callable(self.source) or self.stopped:
            return
        try:
            wanted = self.source()
        except (OSError, subprocess.SubprocessError):
            return
        if wanted != self.device and self.proc and self.proc.poll() is None:
            say(f"NOTICE: Audio device changed - now recording {'meeting audio' if self.label == 'Them' else 'microphone'} from the new device.")
            self.proc.terminate()

    def command(self):
        if not callable(self.source):
            return self.source
        self.device = self.source()
        return ["parec", "--device", self.device, "--format=s16le", f"--rate={RATE}", "--channels=1",
                "--latency-msec=100", "--client-name=mintomeet"]

    def emit(self, frames, start):
        with self.stats.lock:
            self.stats.total += 1
        self.out_q.put((start / 10, self.label, np.concatenate(frames)))

    def run(self):
        enc = None
        if self.save_to:
            enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "-",
                                    "-c:a", "libopus", *self.ENCODING.get(self.label, ["-b:a", "24k"]),
                                    "-flush_packets", "1", str(self.save_to)],
                                   stdin=subprocess.PIPE)      # flush so earlier moments can be played mid-call
        frames, active, quiet, pos, start, pre = [], False, 0, 0, 0, None
        while True:
            try:
                self.proc = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as e:
                say(f"NOTICE: Cannot capture {self.label}: {e}. Retrying...")
                self.proc = None
            if self.proc and callable(self.source) and self.T0 is not None:
                # Time that passed without audio (starting up, or reconnecting after a device change) becomes
                # silence, so this recording, its transcript times and the other recording stay in step.
                behind = int((time.monotonic() - self.T0) * 10) - pos
                if behind >= self.GAP:
                    if active and frames:
                        self.emit(frames, start)
                    frames, active, quiet, pre = [], False, 0, None
                    if enc:
                        try:
                            enc.stdin.write(bytes(FRAME * 2 * behind))
                        except (BrokenPipeError, ValueError):
                            enc = None
                    pos += behind
            while self.proc:
                raw = self.proc.stdout.read(FRAME * 2)
                if len(raw) < FRAME * 2:
                    break
                if enc:
                    try:
                        enc.stdin.write(raw)
                    except (BrokenPipeError, ValueError):
                        enc = None
                pcm = np.frombuffer(raw, np.int16)
                rms = float(np.sqrt(np.mean((pcm.astype(np.float32) / 32768) ** 2)))
                self.peak = max(self.peak, rms)
                loud = rms > LOUD
                if not active:
                    if loud:
                        active, quiet = True, 0
                        frames = [pre, pcm] if pre is not None else [pcm]
                        start = pos - 1 if pre is not None else pos
                    else:
                        pre = pcm
                else:
                    frames.append(pcm)
                    quiet = 0 if loud else quiet + 1
                    secs = len(frames) / 10
                    if (quiet >= END_SILENCE and secs >= MIN_CHUNK) or secs >= MAX_CHUNK or quiet >= 15:
                        self.emit(frames, start)
                        frames, active, pre = [], False, None
                pos += 1
            if self.proc:
                self.proc.wait()
            if self.stopped or not callable(self.source):
                break
            time.sleep(1)       # device vanished or changed: reconnect to the current default
        if active and frames:
            self.emit(frames, start)
        if enc:
            enc.stdin.close()
            enc.wait()


def monitor(streams, stopping):
    """Twice a second print audio levels for the UI; every 2 s check for device changes."""
    names = {"Them": "them", "You": "you"}
    tick = 0
    while not stopping.wait(0.5):
        parts = []
        for s in streams:
            if s.label in names:
                parts.append(f"{names[s.label]}={s.peak:.4f}")
                s.peak = 0.0
        if parts:
            say("LEVEL: " + " ".join(parts))
        tick += 1
        if tick % 4 == 0:
            for s in streams:
                s.check_device()


def clean(segs):
    """Sort by time and drop mic lines that are just the meeting audio leaking into the microphone."""
    them = [(g["start"], g["text"].lower()) for g in segs if g["stream"] != "You"]
    kept = []
    for g in sorted(segs, key=lambda g: g["start"]):
        if g["stream"] == "You" and any(
            abs(g["start"] - tt) <= 12 and difflib.SequenceMatcher(None, g["text"].lower(), xx).ratio() > 0.6
            for tt, xx in them
        ):
            continue
        kept.append(g)
    return kept


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="output folder (default ~/MintoMeet/<timestamp>)")
    ap.add_argument("--engine", choices=["parakeet", "whisper"], default="parakeet")
    ap.add_argument("--model", default="small",
                    help="Whisper model: " + ", ".join(m.model for m in modelcatalog.MODELS if m.engine == "whisper"))
    ap.add_argument("--language", default=None, help="Whisper only, e.g. en, ur, ar (default: auto-detect)")
    ap.add_argument("--threads", type=int, help="CPU threads for transcription (default: 2 live, half the CPU after)")
    ap.add_argument("--live", action="store_true", help="with Whisper, transcribe during the call (heavy on CPU)")
    ap.add_argument("--file", help="transcribe an audio/video file instead of recording")
    ap.add_argument("--no-mic", action="store_true")
    ap.add_argument("--no-system", action="store_true")
    ap.add_argument("--no-save-audio", action="store_true", help="don't keep the recorded audio files")
    ap.add_argument("--system-source", help="fixed system-audio source (default: follow the default output's monitor)")
    ap.add_argument("--mic-source", help="fixed microphone source (default: follow the default input)")
    ap.add_argument("--no-speakers", action="store_true", help="don't tell speakers apart")
    ap.add_argument("--control", action="store_true",
                    help="accept commands on stdin while recording, e.g. ENGINE whisper medium en")
    ap.add_argument("--retranscribe", metavar="FOLDER", help="transcribe a finished meeting again from its saved audio")
    ap.add_argument("--prepare", action="store_true", help="download the Parakeet and speaker models and exit")
    ap.add_argument("--download", metavar="MODEL_ID", help="download one model from the catalog (modelcatalog.py) and exit")
    args = ap.parse_args()

    if args.download:
        download_for_library(args.download)
        return

    if args.prepare:
        download_parakeet(lambda pct: print(f"\rDownloading Parakeet model: {pct}%", end="", flush=True))
        download_speaker_model(lambda pct: print(f"\rDownloading speaker model: {pct}%  ", end="", flush=True))
        print(f"\nModels ready in {PARAKEET_DIR.parent}")
        return
    offline = bool(args.file or args.retranscribe)

    try:
        os.nice(10)     # meeting apps always get the CPU first
    except OSError:
        pass
    language = None if args.language in (None, "", "auto") else args.language
    defer = args.engine == "whisper" and not args.live and not offline
    half = max(2, (os.cpu_count() or 4) // 2)
    threads = args.threads or (half if (defer or offline) else live_threads(args.engine))

    if args.retranscribe:
        out = pathlib.Path(args.retranscribe)
    elif args.out:
        out = pathlib.Path(args.out)
    else:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        name = pathlib.Path(args.file).stem + "_" if args.file else ""
        out = pathlib.Path.home() / "MintoMeet" / (name + stamp)
    out.mkdir(parents=True, exist_ok=True)
    try:
        out.chmod(0o700)            # recordings are personal: readable by you only
    except OSError:
        pass

    q, stats = queue.Queue(), Stats()
    streams = []
    save = not args.no_save_audio and not offline

    def decode(path):
        return ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"]
    if args.file:
        streams.append(Stream("Audio", decode(args.file), q, stats))
    elif args.retranscribe:
        for label, name in (("Them", "audio-meeting.ogg"), ("You", "audio-mic.ogg")):
            if (out / name).exists():
                streams.append(Stream(label, decode(out / name), q, stats))
        if not streams:
            say(f"ERROR: No saved audio in {out} to transcribe again.")
            sys.exit(1)
    else:
        try:
            pactl("info")
        except (OSError, subprocess.SubprocessError) as e:
            say(f"ERROR: Cannot reach the sound server (PipeWire/PulseAudio): {e}")
            sys.exit(1)
        if not args.no_system:
            src = (lambda: args.system_source) if args.system_source else (lambda: pactl("get-default-sink") + ".monitor")
            streams.append(Stream("Them", src, q, stats, out / "audio-meeting.ogg" if save else None))
        if not args.no_mic:
            src = (lambda: args.mic_source) if args.mic_source else (lambda: pactl("get-default-source"))
            streams.append(Stream("You", src, q, stats, out / "audio-mic.ogg" if save else None))
        if not streams:
            say("ERROR: Nothing to record - turn on meeting audio, the microphone, or both")
            sys.exit(1)

    stopping = threading.Event()
    downloading = threading.Event()

    def on_signal(*_):
        stopping.set()
        for s in streams:
            s.stop()
        if downloading.is_set():    # can't transcribe yet; keep the audio and leave
            for s in streams:
                s.join(10)
            say(f"NOTICE: Stopped before the speech model finished downloading. The audio is saved in {out}")
            say("STATE: cancelled")
            os._exit(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    Stream.T0 = time.monotonic()
    for s in streams:   # start capturing now so nothing is missed while the model loads
        s.start()
    say(f"OUT: {out}")
    if not offline:
        threading.Thread(target=monitor, args=(streams, stopping), daemon=True).start()

    def report(pct):
        downloading.set()
        say("STATE: downloading" + (f" pct={pct}" if pct is not None else ""))

    try:
        if args.engine == "parakeet":
            download_parakeet(report)
        else:
            model_path = whisper_path(args.model, report)
        if not args.no_speakers:
            download_speaker_model(report)
        downloading.clear()
        if defer:
            say("STATE: recording deferred")
            while not stopping.is_set() and any(s.is_alive() for s in streams):
                stopping.wait(0.5)
        say("STATE: loading")
        engine = Parakeet(threads) if args.engine == "parakeet" else Whisper(model_path, threads, language)
        speakers = None if args.no_speakers else SpeakerID()
    except Exception as e:  # noqa: BLE001 - report any failure to the UI
        downloading.clear()
        stopping.set()
        for s in streams:
            s.stop()
            s.join(10)
        saved = f" The audio is saved in {out}." if save else ""
        say(f"ERROR: Could not prepare the speech engine: {e}.{saved}")
        sys.exit(1)
    say("STATE: recording backlog=0" if not (stopping.is_set() or offline)
        else f"STATE: finishing done=0 total={stats.total}")
    model_now = meetings.model_id(args.engine, args.model)
    switcher = None
    if args.control and not offline:
        switcher = Switcher(args.threads)
        threading.Thread(target=read_commands, args=(switcher,), daemon=True).start()

    segs = []
    with open(out / "transcript.live.txt", "w" if args.retranscribe else "a", encoding="utf-8") as live:
        while True:
            try:
                ts, stream, pcm = q.get(timeout=0.5)
            except queue.Empty:
                if not any(s.is_alive() for s in streams) and q.empty():
                    break
                continue
            ready = switcher.take() if switcher else None
            if ready:       # a newly loaded model takes over from this chunk on; the old one is freed
                engine, model_now = ready
                gc.collect()
                say(f"SWITCH: done {model_now} at={ts:.1f}")
            audio = pcm.astype(np.float32) / 32768
            text = engine(audio)
            with stats.lock:
                stats.done += 1
            if text:
                secs = len(audio) / RATE
                emb = speakers.embed(audio) if speakers and stream != "You" else None
                label = "You" if stream == "You" else (speakers.live_label(emb, secs) if speakers else stream)
                segs.append({"start": ts, "end": ts + secs, "stream": stream, "label": label, "text": text,
                             "emb": emb, "model": model_now})
                line = f"[{fmt(ts)}] {label}: {text}"
                live.write(line + "\n")
                live.flush()
                say(line)
            if stopping.is_set() or offline:
                say(f"STATE: finishing done={stats.done} total={stats.total}")
            else:
                say(f"STATE: recording backlog={q.qsize()}")

    segs = clean(segs)
    others = [g for g in segs if g["stream"] != "You"]
    if speakers and others:
        say("STATE: finishing speakers")
        for g, label in zip(others, speakers.final_labels(others)):
            g["label"] = label
    final = out / "transcript.txt"
    if final.exists():      # transcribing again: keep the previous transcript as an earlier version
        old = meetings.model_name(meetings.current_model(out)[0])
        meetings.archive_current(out)
        say(f"NOTICE: Your previous transcript ({old}) is kept. Open it from the “Transcribed with” tag.")
    final.write_text("".join(f"[{fmt(g['start'])}] {g['label']}: {g['text']}\n" for g in segs), encoding="utf-8")
    (out / "segments.json").write_text(json.dumps([
        {"start": round(g["start"], 1), "end": round(g["end"], 1), "stream": g["stream"], "label": g["label"],
         "text": g["text"], "emb": None if g["emb"] is None else [round(float(x), 4) for x in g["emb"]],
         "model": g["model"]}
        for g in segs]), encoding="utf-8")
    used = list(dict.fromkeys(g["model"] for g in sorted(segs, key=lambda g: g["start"]))) or [model_now]
    meetings.save_meta(out, model="+".join(used),     # "parakeet+whisper-medium" if you switched mid-meeting
                       transcribed=datetime.datetime.now().isoformat(timespec="seconds"))
    say(f"DONE: {final}")


if __name__ == "__main__":
    main()
