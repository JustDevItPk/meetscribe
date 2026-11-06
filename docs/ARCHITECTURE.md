# How MeetScribe works

A short map of the code for contributors. Every module starts with a docstring that explains it in more
detail.

## Two processes, two Pythons

```
┌──────────────────────────── system Python (PyGObject) ───────────────────────────┐
│ gui.py  window, transcript, speakers, chat, export, settings                     │
│   ├─ tray.py         tray icon over D-Bus (StatusNotifierItem + DBusMenu)        │
│   ├─ callwatch.py    notices when a call app starts using the microphone          │
│   ├─ speakers.py     speaker list + "Speakers" dialog                             │
│   ├─ voicereview.py  "Learned voices" dialog                                      │
│   ├─ storage.py      "Storage" dialog            datasize.py  disk-usage figures  │
│   ├─ modellibrary.py "Speech models" dialog + background model downloads         │
│   ├─ chat.py         Ask Agent conversations (Claude Code CLI or an API agent)    │
│   ├─ agents.py       AI agents: settings, OpenAI / Anthropic / Ollama streaming   │
│   ├─ agentkeys.py    API keys in the desktop keyring (or a 0600 file)              │
│   ├─ agentsetup.py   "AI agent" window: choose, connect, test                      │
│   └─ procstats.py    CPU / memory of the recorder, read from /proc                │
└───────────────┬──────────────────────────────────────────────────────────────────┘
       stdin:   │ ENGINE whisper medium en          ▲ stdout: one message per line
                ▼                                   │
┌──────────── MeetScribe's own Python 3.12 (~/.local/share/meetscribe/venv) ───────┐
│ worker.py  capture (parec) → speech chunks → Parakeet / Whisper → speaker voices │
│ export.py  Word / PDF export (python-docx, fpdf2)                                │
└──────────────────────────────────────────────────────────────────────────────────┘
        shared, pure Python: meetings.py (meeting folders), voices.py (voice profiles),
                             modelcatalog.py (speech models), version.py
```

The window needs GTK, which is easiest from the distribution's Python. The speech engines need numpy,
onnxruntime and CTranslate2 wheels, which the installer puts in a private Python 3.12 so they never clash with
system packages. Modules used by both sides (`meetings.py`, `voices.py`, `modelcatalog.py`) must stay **pure Python** with no
third-party imports.

`install.sh` copies the `.py` files into `~/.local/share/meetscribe/`; the app runs from there, not from the
clone. **A new module must be added to the `FILES` list in `install.sh`.**

## The recorder protocol

`gui.py` starts `worker.py --live --control --out <folder> --engine … [--model …] [--language …]` and reads its
standard output line by line:

| Line | Meaning |
|---|---|
| `OUT: <folder>` | where this meeting is saved |
| `STATE: downloading [pct=N]` / `loading` / `recording backlog=N` / `recording deferred` / `finishing done=N total=M` / `finishing speakers` / `cancelled` | progress |
| `LEVEL: them=<peak> you=<peak>` | loudest level since the last report (0–1), twice a second |
| `[HH:MM:SS] Label: text` | a transcript line (`You`, `Them`, `Speaker N` or a learned name) |
| `SWITCH: loading\|downloading\|done\|failed <model> [pct=N\|at=<secs>\|reason]` | live model switch |
| `NOTICE: …` / `ERROR: …` | messages shown to the user |
| `DONE: <folder>` | finished; the final transcript is on disk |

While recording, the window can send `ENGINE <parakeet|whisper> <model|-> <language|->` on the recorder's
standard input to switch models. The new model loads in the background and takes over from the next speech
chunk. Stopping is `SIGTERM`; the recorder then finishes transcribing, re-clusters speakers and writes the
files.

## Inside the recorder

1. **Capture.** One `Stream` thread per source: the default output's monitor ("Them") and the default
   microphone ("You"), read with `parec` as 16 kHz mono. Each is saved as Opus while recording (the mic in
   speech mode at 16 kbps). If the default device changes the stream reconnects, and the time without audio is
   filled with silence so both files and the transcript times stay in step.
2. **Chunking.** Speech is cut into chunks at pauses (RMS above `LOUD`, closed by `END_SILENCE` quiet frames,
   2–25 s long).
3. **Transcription.** Chunks go to Parakeet (sherpa-onnx) or Whisper (faster-whisper) on a worker thread.
   `live_threads()` picks the CPU threads: 2 for Parakeet, up to 4 for Whisper.
4. **Speakers.** Each meeting-audio chunk gets an ERes2Net voiceprint. During the call they're grouped online
   into `Speaker N`; at the end all voiceprints are clustered again (`SAME_SPEAKER`) and matched against learned
   voices (`voices.match`, `MATCH` and `MARGIN`) so known people are named.
5. **Files.** `transcript.txt`, `segments.json` (each chunk with times, text, voiceprint and model) and
   `meeting.json` are written; a previous transcript is first moved to `versions/` (`meetings.archive_current`).

## A meeting folder

```
~/MeetScribe/2026-11-03_100000/
  meeting.json              title, start time, duration, model ("parakeet+whisper-medium" if switched)
  audio-meeting.ogg         what you heard (Opus 24 kbps)
  audio-mic.ogg             your microphone (Opus 16 kbps, speech mode)
  transcript.txt            [HH:MM:SS] Name: text   (edited in place by the window)
  transcript.original.txt   the machine transcript, kept once you edit
  segments.json             chunks with times, text, voiceprints, model
  speaker_map.json          speaker names you chose, by line
  versions/<stamp>/         earlier transcripts, each with version.json
  bookmarks.json, chat.json, chat.md
```

## Speech models

`modelcatalog.py` (pure Python, shared) lists every supported model: engine, languages, download size, memory
needed, reference speed, accuracy and license. It also reads this computer's specs from `/proc`, estimates each
model's live speed from its cores, gives a fit verdict, and knows where each model lives on disk: Parakeet in
`~/.local/share/meetscribe/models/`, Whisper in the Hugging Face cache (`~/.cache/huggingface/hub`, or
`$HF_HUB_CACHE`). Settings store the chosen model's id (`"model": "whisper-small"`).

`modellibrary.py` is the *Speech models* dialog plus a small download manager owned by the window, so a download
keeps going when the dialog is closed. Downloading runs `worker.py --download <id>` in MeetScribe's own Python,
which prints `STATE: downloading pct=N` and then `DONE: <id>` or `ERROR: …`; cancelling stops it and deletes the
half-finished files. Deleting (`modelcatalog.delete`) only removes that model's folder, plus shared Hugging Face
blobs nothing else links to.

To add a model, add a `Model(...)` entry to `modelcatalog.MODELS` (Whisper models must be ones faster-whisper
can load by name) and a display name to `meetings.MODEL_NAMES`.

## Ask Agent and AI agents

`agents.py` lists the services (`SERVICES`) and keeps the choice in `settings.json` under `"agent"`
(`service`, `base_url`, `model`, never a key). There are four kinds:

- **claude-code**: `chat.py` runs `claude -p --tools "" --append-system-prompt-file <context>` with
  `--session-id`/`--resume`, as before; the context file holds the system prompt, participants and transcript.
- **openai** (any OpenAI-compatible `/chat/completions`), **anthropic** (`/v1/messages`) and **ollama**
  (`/api/chat`): `agents.stream_reply()` sends the system prompt with the transcript plus the whole conversation
  every time and streams the answer (server-sent events, or NDJSON for Ollama) with `urllib` only. It estimates
  the size first and refuses with a clear message when the model's context window is known to be too small; for
  Ollama it asks the model's limit (`/api/show`) and sets `num_ctx` to fit the meeting, since Ollama otherwise
  reads only its small default window. HTTP and network errors become sentences saying what to do
  (`http_problem`, `network_problem`).

`chat.json` keeps `messages` (each answer with the `agent` that gave it), and for Claude Code its `session_id`
and `session_turns`, the number of messages that session has seen. When another agent answered in between, the
next Claude Code question starts a new session with the earlier conversation in its context. Old chat files
(no `agent`, no `session_turns`) resume as before.

The window checks `chat.agent_problem()` before sending; if the agent isn't ready (no `claude` command, no key,
no model) it opens the AI agent window and sends the question once that is saved.

## Learned voices

`~/.config/meetscribe/voices.json` holds one averaged voiceprint per person, which is all the recorder reads.
`voice_clips.json` keeps the clips each profile was built from (meeting, stream, time, words, voiceprint).
Naming a line adds its clip (and removes that moment from anyone else); removing clips in *Learned voices*
rebuilds the profile from what's left. `voices.migrate()` once rebuilt clip lists for profiles learned before
clips were kept.

## Testing

- `tests/` covers the pure modules (`meetings`, `voices`, `datasize`, `procstats`, `agents`, `chat`) and runs
  anywhere with pytest. `tests/mockagents.py` is a local server that answers like OpenAI, Anthropic and Ollama,
  and `tests/test_chat.py` puts a fake `claude` command on `PATH`, so no real keys or subscription are used. See [CONTRIBUTING.md](../CONTRIBUTING.md).
- The window can be driven without a real call: create `gui.Window` inside an `Adw.Application` with
  `MEETSCRIBE_CONFIG_DIR` and `MEETSCRIBE_MEETINGS_DIR` pointing at temporary folders, call its methods from
  `GLib.timeout_add`, and render screenshots with `Gtk.WidgetPaintable`. Under Xvfb, real clicks and keys can
  be sent with XTest.
- The recorder can be fed from a file instead of a microphone by replacing `Stream.command` with an
  `ffmpeg -re -i <file> -f s16le -ac 1 -ar 16000 -` command.
