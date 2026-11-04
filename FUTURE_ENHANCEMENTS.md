# MeetScribe: future enhancements

These are the features that were proposed but not built yet. Each entry says what it would do, how it would fit the current code and roughly how big the job is.

Size: **S** is under a day, **M** is one to three days, **L** is more than three days.

## 3. Automatic summary after each meeting (S)

When a recording finishes, ask the agent to "Summarize the meeting" automatically. The summary would then be waiting in Ask Agent, and Export could include it straight away.

- **How:** call `send_chat(SUGGESTIONS[0])` from `Window.on_exit` once the transcript has loaded, behind an "Summarize automatically" option in Preferences that is on by default.
- **Watch out for:** each summary uses your Claude subscription. Skip meetings shorter than about 2 minutes or with no speech.

## 5. Search across all meetings (M)

Ask a question such as "what did we decide about the Atoms API?" and get answers from every saved meeting, not just the open one.

- **Simple first version:** a search box at the top of the sidebar does plain word search over every `transcript.txt` and highlights matching meetings and lines.
- **Smarter version:** an "Ask across meetings" chat sends the agent the matching excerpts, with meeting titles and dates.
- **Watch out for:** the agent can't take every transcript at once. Search first, then send only the best matches.

## 8. Send the follow-up email (S)

After the agent drafts a follow-up email, one button opens it in your email app with recipients, subject and body filled in.

- **How:** detect a reply that looks like an email (a subject line plus a body) and add an "Open in email app" button under it. That button opens a `mailto:` link built from the participants' saved emails, and `xdg-open` hands it to your default email app.
- **Watch out for:** `mailto:` links have length limits in some apps, so very long emails should be copied instead.

## 9. Custom vocabulary (M)

A list of names and terms that speech recognition often gets wrong, such as Atoms, UAD and Awan, with automatic correction.

- **How:** add a "Vocabulary" page in Speakers or Preferences with pairs like `atoms → Atoms` and `you a d → UAD`. Corrections apply to each line as it's transcribed (in `worker.py`) and when re-transcribing. Whisper can also take the terms as a hint (`initial_prompt`).
- **Watch out for:** Parakeet has no hint option, so only find-and-replace works there. Replacements must match whole words only.

## 10. Saved prompts (S)

Your own one-click requests next to the built-in suggestions, for example "Standup format: yesterday / today / blockers per person" or "Jira tickets from the action items".

- **How:** a "Manage prompts…" list in the chat tab, saved in `~/.config/meetscribe/prompts.json`. The suggestion chips come from the built-in prompts plus yours.

## 12. Global start/stop hotkey (M)

Start or stop recording, or add a bookmark, from anywhere without opening MeetScribe.

- **How:** GNOME on Wayland doesn't let apps grab keys themselves. MeetScribe would instead register a GNOME custom shortcut through `gsettings` that runs `meetscribe --toggle` or `meetscribe --bookmark`. Those commands reach the running app through its existing app actions. OpenWhispr does the same thing on GNOME.
- **Watch out for:** KDE and Hyprland need their own way of registering shortcuts.

## 15. Storage cleanup (S)

Keep transcripts forever but delete audio older than a set number of days. Each hour of a meeting uses about 22 MB of audio for both streams.

- **How:** a Preferences option "Delete meeting audio after: never / 30 / 90 days". It would run at startup, removing `audio-*.ogg` from old meetings. Clicking a line to hear it would then explain that the audio was removed.
- **Watch out for:** "Transcribe again" needs the audio. The option's description must say so.

## 17. Echo removal when using speakers (L)

Real echo cancellation for when you're not on headphones, instead of today's after-the-fact duplicate-line filter.

- **How:** route the microphone through PipeWire's built-in echo-cancel module (`libpipewire-module-echo-cancel`, WebRTC AEC) using the meeting audio as the reference. MeetScribe would load the module only while recording and record from its cleaned microphone.
- **Watch out for:** this changes your system audio routing while recording, so it needs careful testing with Zoom and Lark. They run their own echo cancellation and may react badly to a second layer. It should be an opt-in option.

## Known limits of the current version

- **One label per line:** each line belongs to the person who started speaking. If two people talk within one stretch of speech without a pause, the whole line goes to one of them. A fix would split lines using word timings and a speaker-change detector such as pyannote segmentation, which sherpa-onnx supports.
- **Speaker detection struggles with very short replies:** replies under about 2.5 seconds ("Okay", "Yeah") often stay as "Them".
- **Tuned on one meeting:** speaker detection was tuned on a single real meeting (Zoom/Lark audio, five speakers). Thresholds are `SAME_SPEAKER = 0.45` in `worker.py` and `MATCH = 0.50` in `voices.py`, and may need adjusting after more meetings.
- **Call detection keys off the microphone:** it reacts to any app that uses the microphone for more than about 6 seconds. That includes voice notes or browser dictation, not only calls.
