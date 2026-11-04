#!/usr/bin/env python3
"""Record meeting audio (system output + microphone), save it to disk, and transcribe it locally.

Engines:
  parakeet  NVIDIA Parakeet TDT 0.6B v3 via sherpa-onnx. Fast on CPU (about 7x real time on 2 threads),
            English + 24 European languages, automatic language detection. Transcribes live.
  whisper   faster-whisper (OpenAI Whisper). ~100 languages but about real-time speed on a laptop CPU,
            so by default it only records during the call and transcribes after you stop.

Speakers: each chunk of meeting audio gets a voiceprint (ERes2Net via sherpa-onnx). During the call chunks
are grouped online into "Speaker 1", "Speaker 2", ...; after the call all voiceprints are re-clustered and
compared with the voice profiles learned from earlier meetings (see voices.py) so known people get named.
"""
import argparse
import datetime
import difflib
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
import voices  # noqa: E402

RATE = 16000
FRAME = RATE // 10      # 100 ms of audio
LOUD = 0.006            # RMS (0..1) above which a frame counts as possible speech
END_SILENCE = 7         # quiet frames that close a chunk
MIN_CHUNK = 2.0         # seconds
MAX_CHUNK = 25.0        # seconds

DATA = pathlib.Path.home() / ".local/share/meetscribe"
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
                "--latency-msec=100", "--client-name=meetscribe"]

    def emit(self, frames, start):
        with self.stats.lock:
            self.stats.total += 1
        self.out_q.put((start / 10, self.label, np.concatenate(frames)))

    def run(self):
        enc = None
        if self.save_to:
            enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "-",
                                    "-c:a", "libopus", "-b:a", "24k", "-flush_packets", "1", str(self.save_to)],
                                   stdin=subprocess.PIPE)      # flush so earlier moments can be played mid-call
        frames, active, quiet, pos, start, pre = [], False, 0, 0, 0, None
        while True:
            try:
                self.proc = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as e:
                say(f"NOTICE: Cannot capture {self.label}: {e}. Retrying...")
                self.proc = None
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
    ap.add_argument("--out", help="output folder (default ~/MeetScribe/<timestamp>)")
    ap.add_argument("--engine", choices=["parakeet", "whisper"], default="parakeet")
    ap.add_argument("--model", default="small", help="Whisper model: small, medium, large-v3-turbo")
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
    ap.add_argument("--retranscribe", metavar="FOLDER", help="transcribe a finished meeting again from its saved audio")
    ap.add_argument("--prepare", action="store_true", help="download the Parakeet and speaker models and exit")
    args = ap.parse_args()

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
    threads = args.threads or (half if (defer or offline) else 2)

    if args.retranscribe:
        out = pathlib.Path(args.retranscribe)
    elif args.out:
        out = pathlib.Path(args.out)
    else:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        name = pathlib.Path(args.file).stem + "_" if args.file else ""
        out = pathlib.Path.home() / "MeetScribe" / (name + stamp)
    out.mkdir(parents=True, exist_ok=True)

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

    segs = []
    with open(out / "transcript.live.txt", "w" if args.retranscribe else "a", encoding="utf-8") as live:
        while True:
            try:
                ts, stream, pcm = q.get(timeout=0.5)
            except queue.Empty:
                if not any(s.is_alive() for s in streams) and q.empty():
                    break
                continue
            audio = pcm.astype(np.float32) / 32768
            text = engine(audio)
            with stats.lock:
                stats.done += 1
            if text:
                secs = len(audio) / RATE
                emb = speakers.embed(audio) if speakers and stream != "You" else None
                label = "You" if stream == "You" else (speakers.live_label(emb, secs) if speakers else stream)
                segs.append({"start": ts, "end": ts + secs, "stream": stream, "label": label, "text": text,
                             "emb": emb})
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
    if args.retranscribe and final.exists():
        backup = out / f"transcript.before-{args.engine}-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt"
        final.rename(backup)
        say(f"NOTICE: Your previous transcript is kept as {backup.name}")
    final.write_text("".join(f"[{fmt(g['start'])}] {g['label']}: {g['text']}\n" for g in segs), encoding="utf-8")
    (out / "segments.json").write_text(json.dumps([
        {"start": round(g["start"], 1), "end": round(g["end"], 1), "stream": g["stream"], "label": g["label"],
         "text": g["text"], "emb": None if g["emb"] is None else [round(float(x), 4) for x in g["emb"]]}
        for g in segs]), encoding="utf-8")
    say(f"DONE: {final}")


if __name__ == "__main__":
    main()
