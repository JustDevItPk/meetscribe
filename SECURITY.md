# Security policy

MeetScribe handles sensitive material: recordings of conversations, transcripts and voiceprints of the people in
them. We take reports about anything that could expose that data seriously.

## Supported versions

Security fixes go into the latest release on the `main` branch. Please update (`git pull && ./install.sh`) before
reporting.

## Reporting a vulnerability

**Please don't open a public issue.** Instead, either:

- use GitHub's private reporting: the **Security** tab of this repository → **Report a vulnerability**, or
- email **team@saailverse.com** with "MeetScribe security" in the subject.

Include what you found, how to reproduce it, and what an attacker could do with it. Don't include real meeting
recordings or transcripts; a description or made-up example is enough.

We'll acknowledge your report within 5 working days, keep you updated, and credit you in the release notes unless
you'd rather stay anonymous.

## What's in scope

For example:

- meeting data (audio, transcripts, chats, voiceprints) becoming readable by other users or apps, or being sent
  anywhere other than described in the README's *Your data and privacy* section;
- the Ask Agent integration letting a transcript's content make Claude run commands or tools (it runs with all
  tools disabled; a bypass is a vulnerability);
- the installer downloading or running something it shouldn't (it verifies the `uv` checksum and installs pinned
  version ranges from `requirements.txt`).

Out of scope: vulnerabilities in the speech models, call apps, PipeWire/PulseAudio, GTK or Claude Code themselves.
Please report those to their projects.
