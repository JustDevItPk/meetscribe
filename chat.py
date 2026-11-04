"""Chat with Claude about a meeting transcript through the Claude Code CLI (uses your Claude subscription).

Each meeting folder keeps its own conversation: chat.json holds the Claude session id and the messages,
chat.md is a readable copy. Claude runs with all tools disabled, so it can only read the transcript
it is given and reply.
"""
import glob
import json
import pathlib
import shutil
import subprocess
import threading
import uuid

SYSTEM = (
    "You are the meeting assistant inside MeetScribe, a desktop app. The user's meeting transcript is below. "
    "'Them' is the meeting audio (the other participants, possibly several people) and 'You' is the user's own "
    "microphone. The transcript comes from speech recognition, so expect misheard words and names; when something "
    "is unclear, say so instead of guessing. Base answers on the transcript; you may also help with writing tasks "
    "built on it (emails, notes, plans). Format replies in Markdown with short headings, bullet lists and bold "
    "for owners and deadlines. Do not use tables."
)


def find_claude():
    found = shutil.which("claude")
    if found:
        return found
    pattern = str(pathlib.Path.home() / ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


def transcript_path(folder):
    folder = pathlib.Path(folder)
    return next((p for p in (folder / "transcript.txt", folder / "transcript.live.txt") if p.exists()), None)


class Chat:
    def __init__(self, folder):
        self.folder = pathlib.Path(folder)
        self.file = self.folder / "chat.json"
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.session_id = data.get("session_id")
        self.messages = data.get("messages", [])
        self.proc = None
        self.cancelled = False

    def save(self):
        self.file.write_text(json.dumps({"session_id": self.session_id, "messages": self.messages}, indent=1),
                             encoding="utf-8")
        md = [f"## {'You' if m['role'] == 'user' else 'Claude'}\n\n{m['text']}\n" for m in self.messages]
        (self.folder / "chat.md").write_text("\n".join(md), encoding="utf-8")

    def reset(self):
        self.session_id = None
        self.messages = []
        self.save()

    @property
    def busy(self):
        return self.proc is not None and self.proc.poll() is None

    def cancel(self):
        self.cancelled = True
        if self.busy:
            self.proc.terminate()

    def send(self, text, on_delta, on_done, participants=""):
        """Ask Claude. Calls on_delta(chunk) as the reply streams and on_done(full_text, error) at the end.

        Both callbacks run on a background thread.
        """
        claude = find_claude()
        transcript = transcript_path(self.folder)
        if not claude:
            on_done(None, "Could not find the 'claude' command. Install Claude Code and sign in first.")
            return
        if transcript is None:
            on_done(None, "This meeting has no transcript yet.")
            return
        context = self.folder / ".claude-context.md"
        extra = f"\n\n{participants}" if participants else ""
        context.write_text(f"{SYSTEM}{extra}\n\n<transcript>\n{transcript.read_text(encoding='utf-8')}\n</transcript>\n",
                           encoding="utf-8")
        new_session = self.session_id is None
        session = self.session_id or str(uuid.uuid4())
        cmd = [claude, "-p", "--tools", "", "--append-system-prompt-file", str(context),
               "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--resume" if not new_session else "--session-id", session]
        self.messages.append({"role": "user", "text": text})
        self.cancelled = False
        try:
            self.proc = subprocess.Popen(cmd, cwd=self.folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as e:
            self.messages.pop()
            on_done(None, f"Could not start Claude: {e}")
            return
        threading.Thread(target=self._run, args=(self.proc, text, session, on_delta, on_done), daemon=True).start()

    def _run(self, proc, text, session, on_delta, on_done):
        proc.stdin.write(text)
        proc.stdin.close()
        streamed, final, error = [], None, None
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "stream_event":
                inner = event.get("event", {})
                delta = inner.get("delta", {}) if inner.get("type") == "content_block_delta" else {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    streamed.append(delta["text"])
                    on_delta(delta["text"])
            elif event.get("type") == "result":
                if event.get("is_error"):
                    error = str(event.get("result") or event.get("subtype") or "Claude returned an error")
                else:
                    final = event.get("result")
        proc.wait()
        err_text = proc.stderr.read().strip()
        reply = final if final is not None else "".join(streamed)
        if self.cancelled:
            error = "Stopped."
        elif error is None and (proc.returncode != 0 or not reply):
            error = err_text[-400:] or f"Claude exited with code {proc.returncode}"
            if "No conversation found" in error:
                error += " - press “New chat” to start a fresh conversation."
        if reply and not error:
            self.session_id = session
            self.messages.append({"role": "assistant", "text": reply})
        elif self.messages and self.messages[-1]["role"] == "user":
            if reply:       # stopped part-way: keep what arrived so far
                self.session_id = session
                self.messages.append({"role": "assistant", "text": reply})
            else:
                self.messages.pop()
        self.save()
        on_done(reply, error)
