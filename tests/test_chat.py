"""chat.Chat with a fake `claude` command (records how it's called) and the mock API server."""
import json
import os
import stat
import sys
import threading

import pytest

import agentkeys
import agents
import chat
from mockagents import MockAgentServer

FAKE_CLAUDE = """#!{python}
import json, sys
log = {log!r}
with open(log, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}) + "\\n")
for word in ["Claude", " says", " hi"]:
    print(json.dumps({{"type": "stream_event", "event": {{"type": "content_block_delta",
                                                       "delta": {{"type": "text_delta", "text": word}}}}}}))
print(json.dumps({{"type": "result", "result": "Claude says hi"}}))
"""


@pytest.fixture
def meeting(tmp_path):
    folder = tmp_path / "2026-11-03_100000"
    folder.mkdir()
    (folder / "transcript.txt").write_text("[00:00:01] You: Let's ship on Thursday.\n")
    return folder


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "claude-calls.jsonl"
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable, log=str(log)))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return lambda: [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture
def keys(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETSCRIBE_NO_KEYRING", "1")
    monkeypatch.setattr(agentkeys, "CONFIG_DIR", tmp_path / "config")
    return agentkeys


def send(c, question, agent=None, participants=""):
    done, out, chunks = threading.Event(), {}, []
    c.send(question, chunks.append, lambda full, err: (out.update(full=full, err=err), done.set()),
           participants=participants, agent=agent)
    assert done.wait(20)
    return out["full"], out["err"], chunks


def test_claude_code_command_is_unchanged(meeting, fake_claude):
    c = chat.Chat(meeting)
    full, err, chunks = send(c, "Summarize", agent=dict(agents.DEFAULT), participants="Known participants: Alex")
    assert (full, err, chunks) == ("Claude says hi", None, ["Claude", " says", " hi"])
    first = fake_claude()[0]
    ctx = str(meeting / ".claude-context.md")
    assert first["argv"][:-1] == ["-p", "--tools", "", "--append-system-prompt-file", ctx, "--output-format",
                                  "stream-json", "--verbose", "--include-partial-messages", "--session-id"]
    assert first["stdin"] == "Summarize"
    context = (meeting / ".claude-context.md").read_text()
    assert context == (f"{agents.SYSTEM}\n\nKnown participants: Alex\n\n<transcript>\n"
                       "[00:00:01] You: Let's ship on Thursday.\n\n</transcript>\n")

    send(c, "And the owners?", agent=dict(agents.DEFAULT))
    second = fake_claude()[1]
    assert second["argv"][-2:] == ["--resume", first["argv"][-1]]
    assert [m.get("agent") for m in c.messages] == [None, "Claude", None, "Claude"]
    assert "## Claude" in (meeting / "chat.md").read_text()


def test_old_chat_files_still_resume(meeting, fake_claude):
    (meeting / "chat.json").write_text(json.dumps({"session_id": "old-session", "messages": [
        {"role": "user", "text": "q"}, {"role": "assistant", "text": "a"}]}))
    send(chat.Chat(meeting), "next", agent=dict(agents.DEFAULT))
    assert fake_claude()[0]["argv"][-2:] == ["--resume", "old-session"]


def test_switching_agents_keeps_the_conversation(meeting, fake_claude, keys):
    server = MockAgentServer()
    try:
        api = {"service": "custom", "base_url": server.url + "/v1", "model": "gpt-test"}
        keys.put(agents.account(api), "sk-test")
        c = chat.Chat(meeting)
        send(c, "Summarize", agent=dict(agents.DEFAULT))                 # Claude Code first
        full, err, _ = send(c, "Who owns what?", agent=api)              # then an API agent
        assert (full, err) == ("Hello from the mock.", None)
        sent = server.requests[-1][3]["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
        assert sent[2]["content"] == "Claude says hi" and "Let's ship on Thursday" in sent[0]["content"]

        send(c, "Draft the email", agent=dict(agents.DEFAULT))           # back to Claude: a fresh session
        third = fake_claude()[-1]
        assert "--session-id" in third["argv"]
        context = (meeting / ".claude-context.md").read_text()
        assert "<earlier_conversation>" in context and "gpt-test: Hello from the mock." in context
        assert [m.get("agent") for m in c.messages if m["role"] == "assistant"] == ["Claude", "gpt-test", "Claude"]
    finally:
        server.close()


def test_api_errors_leave_the_chat_clean(meeting, keys):
    server = MockAgentServer()
    try:
        api = {"service": "custom", "base_url": server.url + "/v1", "model": "gpt-test"}
        keys.put(agents.account(api), "wrong-key")
        c = chat.Chat(meeting)
        full, err, _ = send(c, "Summarize", agent=api)
        assert not full and "didn't accept the API key" in err
        assert c.messages == []
    finally:
        server.close()


def test_key_file_is_private(keys, tmp_path):
    assert keys.put("openai:https://api.openai.com/v1", "sk-abc") == "file"
    path = tmp_path / "config" / "api-keys.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert keys.get("openai:https://api.openai.com/v1") == "sk-abc"
    keys.put("openai:https://api.openai.com/v1", "")
    assert keys.get("openai:https://api.openai.com/v1") == ""
