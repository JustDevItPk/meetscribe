"""Chat with an AI agent about a meeting transcript (see agents.py for the agents you can choose).

Each meeting folder keeps its own conversation: chat.json holds the messages (each answer remembers which agent
gave it) and, for Claude Code, its session id; chat.md is a readable copy.

Claude Code runs with all tools disabled, so it can only read the transcript it is given and reply. It keeps its
own session, which is resumed while every message in the chat went through it; after another agent has answered,
a new session starts with the earlier conversation included. API agents get the whole conversation every time.
"""
import glob
import json
import pathlib
import shutil
import subprocess
import threading
import uuid

import agentkeys
import agents
from agents import SYSTEM  # noqa: F401 - kept importable from here

CLAUDE = "Claude"           # how answers from Claude Code (and chats from before agents could be chosen) are labelled


def find_claude():
    found = shutil.which("claude")
    if found:
        return found
    pattern = str(pathlib.Path.home() / ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


def check_claude_code():
    """A sentence saying Claude Code is ready, or raises agents.AgentError. Doesn't use your subscription."""
    claude = find_claude()
    if not claude:
        raise agents.AgentError("The 'claude' command isn't installed. Install Claude Code, run `claude` once in a "
                                "terminal to sign in, then try again.")
    try:
        out = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        raise agents.AgentError(f"Claude Code didn't start: {e}") from None
    version = (out.stdout or out.stderr).strip().splitlines()[:1]
    return f"Claude Code is installed{f' ({version[0]})' if version else ''}. It answers with your Claude subscription."


def agent_problem(cfg):
    """Why the chosen agent can't answer yet (a sentence), or None."""
    if agents.kind(cfg) == "claude-code":
        return None if find_claude() else "Claude Code isn't installed on this computer."
    return agents.problem(cfg, agentkeys.get(agents.account(cfg)))


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
        self.messages = data.get("messages", [])
        self.session_id = data.get("session_id")
        # how many messages the Claude Code session has seen (older files: all of them)
        self.session_turns = data.get("session_turns", len(self.messages) if self.session_id else 0)
        self.proc = None
        self.thread = None
        self.cancelled = False

    def save(self):
        self.file.write_text(json.dumps({"session_id": self.session_id, "session_turns": self.session_turns,
                                         "messages": self.messages}, indent=1), encoding="utf-8")
        md = [f"## {'You' if m['role'] == 'user' else m.get('agent') or CLAUDE}\n\n{m['text']}\n" for m in self.messages]
        (self.folder / "chat.md").write_text("\n".join(md), encoding="utf-8")

    def reset(self):
        self.session_id = None
        self.session_turns = 0
        self.messages = []
        self.save()

    @property
    def busy(self):
        return (self.proc is not None and self.proc.poll() is None) or (self.thread is not None and self.thread.is_alive())

    def cancel(self):
        self.cancelled = True
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()

    def send(self, text, on_delta, on_done, participants="", agent=None):
        """Ask the chosen agent (agents.load_config() unless `agent` is given). Calls on_delta(chunk) as the reply
        streams and on_done(full_text, error) at the end. Both callbacks run on a background thread."""
        cfg = agent or agents.load_config()
        transcript = transcript_path(self.folder)
        if transcript is None:
            on_done(None, "This meeting has no transcript yet.")
            return
        extra = f"\n\n{participants}" if participants else ""
        if agents.kind(cfg) == "claude-code":
            self._send_claude(cfg, text, transcript, extra, on_delta, on_done)
        else:
            self._send_api(cfg, text, transcript, extra, on_delta, on_done)

    # --- Claude Code ------------------------------------------------------------------------------------------
    def _send_claude(self, cfg, text, transcript, extra, on_delta, on_done):
        claude = find_claude()
        if not claude:
            on_done(None, "Could not find the 'claude' command. Install Claude Code and sign in first, or choose "
                          "another AI agent.")
            return
        # The session can be resumed only if it has seen the whole chat (no other agent answered in between).
        resumable = self.session_id is not None and self.session_turns == len(self.messages)
        earlier = ""
        if not resumable and self.messages:
            earlier = "\n\n<earlier_conversation>\n" + "\n\n".join(
                f"{'User' if m['role'] == 'user' else m.get('agent') or CLAUDE}: {m['text']}" for m in self.messages
            ) + "\n</earlier_conversation>"
        context = self.folder / ".claude-context.md"
        context.write_text(f"{agents.SYSTEM}{extra}{earlier}\n\n<transcript>\n{transcript.read_text(encoding='utf-8')}"
                           "\n</transcript>\n", encoding="utf-8")
        session = self.session_id if resumable else str(uuid.uuid4())
        cmd = [claude, "-p", "--tools", "", "--append-system-prompt-file", str(context),
               "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--resume" if resumable else "--session-id", session]
        model = cfg.get("model")
        if model and model != "default":
            cmd += ["--model", model]
        self.messages.append({"role": "user", "text": text})
        self.cancelled = False
        try:
            self.proc = subprocess.Popen(cmd, cwd=self.folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as e:
            self.messages.pop()
            on_done(None, f"Could not start Claude: {e}")
            return
        threading.Thread(target=self._run_claude, args=(self.proc, text, session, on_delta, on_done), daemon=True).start()

    def _run_claude(self, proc, text, session, on_delta, on_done):
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
        if reply and (not error or self.messages[-1:] and self.messages[-1]["role"] == "user"):
            self.session_id = session          # stopped part-way: keep what arrived so far
            self.messages.append({"role": "assistant", "text": reply, "agent": CLAUDE})
            self.session_turns = len(self.messages)
        elif self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()
        self.save()
        on_done(reply, error)

    # --- API agents -------------------------------------------------------------------------------------------
    def _send_api(self, cfg, text, transcript, extra, on_delta, on_done):
        system = f"{agents.SYSTEM}{extra}\n\n<transcript>\n{transcript.read_text(encoding='utf-8')}\n</transcript>\n"
        self.messages.append({"role": "user", "text": text})
        history = list(self.messages)
        self.cancelled = False
        self.thread = threading.Thread(target=self._run_api, args=(cfg, system, history, on_delta, on_done),
                                       daemon=True)
        self.thread.start()

    def _run_api(self, cfg, system, history, on_delta, on_done):
        streamed, error = [], None

        def delta(chunk):
            streamed.append(chunk)
            on_delta(chunk)
        try:
            key = agentkeys.get(agents.account(cfg))
            reply = agents.stream_reply(cfg, key, system, history, delta, should_stop=lambda: self.cancelled)
        except agents.AgentError as e:
            reply, error = "".join(streamed), str(e)
        except Exception as e:  # noqa: BLE001 - a bad response shouldn't take the window down
            reply, error = "".join(streamed), f"Unexpected problem talking to {agents.describe(cfg)}: {e}"
        if self.cancelled:
            error = "Stopped."
        elif not reply and not error:
            error = f"{agents.display_name(cfg)} sent an empty reply."
        if reply:
            self.messages.append({"role": "assistant", "text": reply, "agent": agents.display_name(cfg)})
        elif self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()
        self.save()
        on_done(reply, error)
