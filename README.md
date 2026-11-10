# MintoMeet

**Record and transcribe your meetings on your own computer, then ask an AI agent about them.**

MintoMeet is a Linux desktop app (GTK 4 / libadwaita) that records what you hear in a call and what you
say into your microphone, transcribes both live with a local speech model, tells the speakers apart, and
learns their voices so it can name them in later meetings. When the call ends you can ask questions about
it, get a summary or a follow-up email, and export it as Word, PDF, Markdown or one clean audio file.

It works with any call app (Zoom, Google Meet, Teams, Slack, Discord, Lark, a browser tab…) because it
records your computer's audio output instead of joining the call. Nothing is uploaded: the audio never leaves
your computer.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform: Linux](https://img.shields.io/badge/platform-Linux-informational)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-informational)

![MintoMeet showing a transcript with named speakers](docs/screenshots/transcript.png)

## Features

- **Live transcription, fully local.** Choose Parakeet (fast, English + 24 European languages) or one of six
  Whisper models (about 100 languages, including Urdu, Arabic, Hindi and Chinese). You can switch models in
  the middle of a meeting without losing anything.
- **A model library that knows your computer.** See each model's memory, processor and disk needs, how well it
  would run on *this* computer, and download or remove models with one click.
- **Who said what.** Your microphone is always labelled *You*. Other voices are told apart and named
  automatically once MintoMeet has learned them.
- **Fix speakers quickly.** Click a name to change it, *Replace all* for every line of a detected speaker,
  or Ctrl+click / Shift+click / select several lines and **bulk assign** them at once. One Ctrl+Z undoes it.
- **Review learned voices.** See every clip a person's voice was learned from, listen to them, and remove the
  ones that are someone else. Clips that sound like another person are flagged for you.
- **Ask Agent, with the AI you choose.** Chat about the meeting: summaries, decisions, action items with owners,
  follow-up emails. Use Claude through your Claude subscription, an API key for OpenAI, Anthropic, Google Gemini,
  OpenRouter, Groq, Mistral or Together, or a free model running on your own computer with Ollama or LM Studio.
- **Click a timestamp to hear that moment**, add bookmarks during the call (Ctrl+B), and edit the transcript
  in place (changes save automatically).
- **Search every meeting** (Ctrl+Shift+F): what was said, Ask Agent answers, bookmark notes, titles and tags,
  narrowed by people, tags and dates. Results open the meeting at the matching line. **Ctrl+F** finds words in
  the open transcript.
- **Tags** such as a client, project or kind of meeting, shown on the meetings list, which you can filter by
  tag. Rename, merge or delete tags in **☰ → Manage tags…**.
- **Transcribe again** with another model later. Earlier transcripts are kept as versions you can open and
  restore.
- **Export** to Word, PDF or Markdown (summary, chat, bookmarks, transcript), or **one mixed MP3** of the
  whole call with background noise removed and voices evened out.
- **Know what it costs.** A live CPU / memory panel while transcribing, and an optional disk-usage view for
  every meeting, model and file, with warnings before storage gets out of hand.
- **Stays out of the way.** Tray icon, optional start at log-in, and a prompt when a call app starts using
  your microphone.

| Ask Agent | Learned voices | Storage |
|---|---|---|
| ![Ask Agent chat with a meeting summary](docs/screenshots/ask-agent.png) | ![Reviewing the clips of a learned voice](docs/screenshots/learned-voices.png) | ![Storage overview](docs/screenshots/storage.png) |

*Screenshots use an invented demo meeting.*

## Requirements

- **Linux with PipeWire or PulseAudio** (the default on current Ubuntu, Fedora and Debian).
- **GTK 4 and libadwaita 1.6+ for Python**: Ubuntu 25.04+, Fedora 41+, Debian 13+, Arch.
- **x86_64 or ARM64**, about **1.5 GB of disk** for the app and the Parakeet model, plus whatever Whisper models
  you download (75 MB to 3 GB each).
- **4 GB of memory** or more. A CPU with 4+ cores is plenty for Parakeet; the larger Whisper models want 8 cores
  for live transcription. No graphics card is needed. The model library tells you what suits your computer.
- Optional, for Ask Agent and summaries: an AI agent (see [Choosing an AI agent](#choosing-an-ai-agent)):
  [Claude Code](https://docs.anthropic.com/en/docs/claude-code) signed in, an API key for a supported service, or
  [Ollama](https://ollama.com) / [LM Studio](https://lmstudio.ai) with a model downloaded.
- Optional on GNOME: the **AppIndicator and KStatusNotifierItem** extension, to show the tray icon
  (pre-installed on Ubuntu).

Install the system packages first:

```bash
# Ubuntu / Debian
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 pulseaudio-utils ffmpeg curl
# Fedora
sudo dnf install python3-gobject gtk4 libadwaita pulseaudio-utils ffmpeg curl
# Arch
sudo pacman -S python-gobject gtk4 libadwaita libpulse ffmpeg curl
```

## Install

```bash
git clone https://github.com/JustDevItPk/mintomeet.git
cd mintomeet
./install.sh
```

No sudo is needed. The installer puts a private Python 3.12 with the speech engines in
`~/.local/share/mintomeet`, downloads the Parakeet and speaker-recognition models (about 680 MB), adds the
`mintomeet` command to `~/.local/bin` and an entry to your app menu.

To update, `git pull` and run `./install.sh` again (quit MintoMeet from its tray menu first, and never in the
middle of a recording). To remove it, run `./uninstall.sh`. Your meetings and settings are kept unless you add
`--purge`.

> **Coming from MeetScribe?** That was this app's earlier name. Quit it, clone this repository and run
> `./install.sh`: your meetings, settings, learned voices, API keys and downloaded models move over automatically.

## Using it

1. **Before a call**, open MintoMeet and press **Start recording**. Wear headphones if you can: on speakers,
   your mic also hears the call.
2. **During the call** the transcript appears as people speak. Click a speaker name to say who it is; from
   then on that voice is named automatically, in this meeting and in future ones.
3. **After the call**, open **Ask Agent** and pick a suggestion such as *Summarize the meeting*, or type your
   own question (Enter sends, Shift+Enter starts a new line). The first time, MintoMeet helps you choose an
   AI agent if Claude Code isn't installed.
4. **Export** from the header bar, or click **Transcribe again** to try another model on the saved audio.
5. **Find it again later** with **Search** (Ctrl+Shift+F). Type words, or use the People, Tags and Date
   buttons; filters can also be typed: `from:Priya tag:client after:2026-11-01 in:chat`, and `"quotes"` find an
   exact phrase. Tag meetings with **Add tag** under the title, and filter the meetings list by tag.

Add the people you meet with under **Speakers…** (name and optional email; the agent uses emails for
follow-ups). Review and clean up their learned voices from **☰ → Learned voices…**.

### Which speech model?

Open **☰ → Speech models…** (or **Get more speech models…** in the recording settings). It shows your processor,
memory and free disk space, then every model with what it needs and how well it would run on this computer,
with **Download** and **Delete** buttons. Downloads run in the background, one at a time, and can be cancelled.
Recording settings, *Transcribe again* and the live model switcher list the models you have downloaded.

| Model | Languages | Memory | Download | Live, on a typical 8-core laptop |
|---|---|---|---|---|
| **Parakeet TDT 0.6B v3** (installed) | English + 24 European, auto-detected | ~1.2 GB | 640 MB | ~7× faster than speech on 2 cores |
| Whisper tiny | ~100 | ~0.5 GB | 75 MB | very fast, rough accuracy |
| Whisper base | ~100 | ~0.6 GB | 141 MB | very fast, basic accuracy |
| **Whisper small** | ~100 | ~1 GB | 464 MB | ~5× faster than speech on 4 cores |
| Whisper medium | ~100, more accurate | ~2.5 GB | 1.5 GB | ~2×; may lag in long monologues |
| Whisper large-v3 turbo | ~100, close to large-v3 | ~2.7 GB | 1.5 GB | ~1.5× (estimated) |
| Whisper large-v3 | ~100, most accurate | ~4.8 GB | 2.9 GB | too slow live; fine for *Transcribe again* |
| Distil-Whisper large-v3 | English only | ~2.5 GB | 1.4 GB | ~2× (estimated) |

Speeds for Parakeet, Whisper small and medium were measured; the others are estimated from model size. A model
that is too slow to keep up live still works: the transcript catches up after the meeting, and it's a good choice
for *Transcribe again*. For other languages, set **Meeting language** in the recording settings rather than relying
on detection.

### Choosing an AI agent

Open **Preferences → AI agent**, or **Change…** at the top of the Ask Agent tab. Pick a service, fill in what it
asks for, choose a model from the list, press **Test connection**, then **Save**.

| Service | What you need | Where your transcript goes |
|---|---|---|
| **Claude Code** (default) | [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and signed in | Anthropic, with your Claude subscription |
| **OpenAI**, **Anthropic API**, **Google Gemini**, **OpenRouter**, **Groq**, **Mistral**, **Together AI** | An API key from that service (billed by them per use) | That service |
| **Ollama**, **LM Studio** | The app running on this computer, with a model downloaded | Nowhere: it stays on your computer |
| **Other OpenAI-compatible server** | Its address (e.g. llama.cpp, vLLM, LiteLLM), a key if it needs one | That server |

- The whole transcript is sent with each question, so choose a model that can read long texts: a one-hour
  meeting is roughly 15,000 tokens. MintoMeet warns you when a meeting is too long for the chosen model, and
  tells Ollama to use a context window big enough for the meeting.
- Each answer is labelled with the model that gave it, and you can switch agents in the middle of a chat: the
  new one sees the conversation so far.
- API keys are kept in your desktop's keyring (GNOME Keyring or KWallet). Without a keyring they go in
  `~/.config/mintomeet/api-keys.json`, readable only by you. If you already set `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` or similar, that is used when no key is saved.

### Command line

```bash
mintomeet                         # open the window (--background: start in the tray)
mintomeet record                  # record from the terminal, Ctrl+C to stop
mintomeet record --engine whisper --model medium --language ur --live
mintomeet file interview.mp4      # transcribe an existing audio or video file
mintomeet summarize ~/MintoMeet/2026-11-03_100000
mintomeet --version
```

`mintomeet record --help` lists every option.

## Your data and privacy

- **Audio and transcripts stay on your computer**, in `~/MintoMeet/<date_time>/`: two audio files (the call
  and your mic), the transcript, earlier transcript versions, the chat and your bookmarks.
- **Speakers, learned voices and settings** are in `~/.config/mintomeet/`. A learned voice is a list of short
  numeric voiceprints plus the words of each clip, not audio.
- **Network use:** the installer and first use of a model download from GitHub and Hugging Face. **Ask Agent**
  and `mintomeet summarize` send the meeting's *transcript text* (never audio), the names and emails of known
  participants and your questions to the AI agent you chose: Anthropic for Claude Code (which runs with all tools
  disabled, so it can only read the transcript it's given), the service you picked for an API key, or nothing
  at all for an agent on your own computer. Nothing else is sent anywhere, and there is no telemetry.
- **API keys** are stored in your desktop's keyring, or in a file only you can read; never in `settings.json`.

**Recording consent.** Laws on recording conversations differ between countries and states, and many require
everyone's consent. Tell the people in your meetings that you're recording and transcribing, and follow your
organisation's policies.

## Troubleshooting

- **"No meeting audio yet"**: the call must play through your computer's default speakers or headphones.
  Check the output device in your system sound settings (and in the call app), then try again.
- **Your words also appear as "Them"**: you're on speakers and the mic is hearing the call. Use headphones.
- **No tray icon on GNOME**: install and enable the *AppIndicator and KStatusNotifierItem* extension.
- **Ask Agent can't find Claude**: install Claude Code, run `claude` once in a terminal to sign in, and make
  sure `claude` is on your `PATH`. Or choose another agent in **Preferences → AI agent**.
- **Ask Agent can't reach Ollama or LM Studio**: start the app (in LM Studio, also start its local server) and
  make sure the model you chose is downloaded. **Test connection** in the AI agent window shows what's wrong.
- **Something else**: errors from the window are in the system journal:
  `journalctl --user -t gui.py --since today`. Please include them when you
  [report a bug](../../issues/new/choose).

## Contributing

Bug reports, ideas and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for how to run it
from source and run the tests, [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how it fits together, and
[docs/ROADMAP.md](docs/ROADMAP.md) for planned features. Please follow the
[Code of Conduct](CODE_OF_CONDUCT.md), and report security issues as described in [SECURITY.md](SECURITY.md).

## License and credits

MintoMeet is released under the [MIT License](LICENSE). © 2026 Saailverse Technologies.

It builds on excellent open-source work, downloaded at install time under their own licenses:

- [Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) by NVIDIA (CC-BY-4.0), in the
  [sherpa-onnx int8 conversion](https://huggingface.co/csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8)
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) by k2-fsa (Apache-2.0)
- [Whisper](https://github.com/openai/whisper) by OpenAI (MIT), run with
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) by SYSTRAN (MIT)
- [ERes2Net speaker model](https://github.com/modelscope/3D-Speaker) from 3D-Speaker (Apache-2.0)
- [python-docx](https://github.com/python-openxml/python-docx) (MIT), [fpdf2](https://github.com/py-pdf/fpdf2)
  (LGPL-3.0), [uv](https://github.com/astral-sh/uv) (MIT / Apache-2.0)
- [GTK](https://gtk.org), [libadwaita](https://gitlab.gnome.org/GNOME/libadwaita) and
  [PyGObject](https://pygobject.gnome.org) (LGPL), [FFmpeg](https://ffmpeg.org) (LGPL / GPL)

MintoMeet is not affiliated with Anthropic, OpenAI, Google, NVIDIA, any other AI service or any call app.
