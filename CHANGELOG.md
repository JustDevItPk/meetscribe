# Changelog

All notable changes to MintoMeet are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Transcription runs in the background.** The window stays usable while meetings are transcribed: open and
  edit other meetings, chat, export or start a new recording. Stopping a recording frees the window at once and
  the rest is transcribed behind the scenes; *Transcribe again* does the same.
- **Pause and resume** any transcription (on the meeting's status card, or from the new *Transcribing* button in
  the header). It carries on from where it stopped, even after quitting MintoMeet or a crash. Quitting no longer
  waits for a transcription to finish: it pauses, and carries on the next time you open the app.
- **Several meetings at once.** Up to 3 on a 16-core computer (fewer on smaller ones, and only while there's
  enough free memory); the rest wait their turn. *Preferences → Meetings transcribed at once* changes it.
- **Search across all meetings** (Search button, Ctrl+Shift+F or Ctrl+K): finds words in transcripts, Ask Agent
  conversations, bookmark notes, titles and tags, ignoring case and accents. Filter by people (only the lines they
  said), tags, date (today, last 7 days, this month, a range…) and where to look; sort by best match or date.
  Filters can also be typed (`from:`, `tag:`, `after:`, `before:`, `on:`, `in:`), and `"quotes"` find a phrase.
  Results are grouped by meeting with the matching words highlighted; clicking one opens the meeting at that
  line, bookmark or chat answer, and *Back to results* returns to the search.
- **Find in this transcript** (Ctrl+F): every match highlighted, with next / previous (Enter / Shift+Enter).
- **Tags for meetings**: add them under a meeting's title (suggestions from your other meetings), see them in
  the meetings list, and filter the list by one or more tags. *☰ → Manage tags…* renames or merges a tag, or
  deletes it from every meeting (with Undo). Exports list the meeting's tags.
- **Choose your AI agent.** Ask Agent and `mintomeet summarize` can use Claude Code (still the default), OpenAI,
  the Anthropic API, Google Gemini, OpenRouter, Groq, Mistral, Together AI, Ollama or LM Studio on your own
  computer, or any OpenAI-compatible server. A new *AI agent* window (Preferences, or *Change…* in Ask Agent)
  lists each service's models, tests the connection and says where your transcript goes.
- If no agent is ready when you ask a question, MintoMeet opens the AI agent window and sends your question once
  it's set up.
- Answers are labelled with the model that gave them, also in `chat.md` and exports; you can switch agents in the
  middle of a chat and the new one sees the conversation so far.
- API keys are stored in the desktop keyring (or a file only you can read), never in `settings.json`; the usual
  environment variables (`OPENAI_API_KEY`, …) work too.
- Clear messages when a meeting is too long for the chosen model, a key is refused, a model doesn't exist, a rate
  limit is hit or a local server isn't running. Ollama gets a context window sized to the meeting.
- **Speech model library** (☰ → Speech models…, or *Get more speech models…* in the recording settings): your
  computer's processor, memory and free disk space; every supported model with its languages, accuracy, memory,
  processor and disk needs; a verdict on how well it would run here (runs well / may fall behind live / too slow
  for live use / not enough memory or disk) and suggestions for English and for other languages. Download (with
  progress, in the background, cancellable) and delete models from there.
- New models: Whisper tiny, base, large-v3 turbo, large-v3 and Distil-Whisper large-v3 (English).
- `worker.py --download <model id>` for scripted downloads.

### Changed
- **MeetScribe is now MintoMeet.** The command is `mintomeet`, meetings live in `~/MintoMeet`, settings in
  `~/.config/mintomeet` and the app in `~/.local/share/mintomeet`. Running `./install.sh` (or starting the app)
  moves everything from the old places, including downloaded models, saved API keys, the log-in entry and Ask
  Agent's Claude Code sessions; nothing needs downloading again.
- Recording settings, *Transcribe again* and the live model switcher list only the models on this computer; if
  the chosen model is deleted, new meetings fall back to Parakeet. *Transcribe again* suggests your most accurate
  model and estimates the time for this computer.
- Deleting a Whisper model also frees the large files newer Hugging Face downloads keep in a shared folder (only
  when no other model uses them).

## [0.1.0] - 2026-11-06

First public release.

### Recording and transcription
- Records the call (your computer's audio output) and your microphone as two tracks, with live transcription by
  Parakeet TDT 0.6B v3 or Whisper small / medium, all on your computer.
- Switch speech models in the middle of a meeting without losing anything.
- Live CPU and memory panel while transcribing.
- Follows device changes (e.g. plugging in a headset) and keeps both tracks and the transcript in step.
- Microphone saved in Opus speech mode at 16 kbps (about a third smaller than before).

### Speakers
- Your microphone is labelled *You*; other voices are told apart and, once learned, named automatically.
- Click a name to change it, *Replace all* for a detected speaker, or bulk assign several lines at once
  (Ctrl+click, Shift+click or a text selection). One Ctrl+Z undoes a change.
- *Learned voices*: review, play and remove the clips each voice was learned from; clips that sound like someone
  else are flagged. Each clip now counts once, and moves when you reassign its line.

### After the meeting
- Ask Agent: chat with Claude about the meeting through Claude Code, with a multi-line composer
  (Enter sends, Shift+Enter for a new line).
- Transcribe again with another model; earlier transcripts are kept as versions you can open and restore.
- Export to Word, PDF or Markdown, or as one mixed MP3 with background noise removed and voices evened out.
- Click a timestamp to hear that moment; bookmarks; edit the transcript in place.

### Housekeeping
- Optional data-size view for every meeting, model and file, plus a Storage overview with warnings.
- Tray icon, start at log-in, and a prompt when a call app starts using the microphone.
- Meeting and settings folders are readable by your user only.
- `install.sh` checks for libadwaita 1.6+, supports x86_64 and ARM64, and installs pinned engine versions;
  new `uninstall.sh`; `mintomeet --version` and an About window.

[Unreleased]: https://github.com/JustDevItPk/mintomeet/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/JustDevItPk/mintomeet/releases/tag/v0.1.0
