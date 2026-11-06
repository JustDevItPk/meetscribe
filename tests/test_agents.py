import json
import socket

import pytest

import agents
from mockagents import MockAgentServer


@pytest.fixture
def server():
    s = MockAgentServer()
    yield s
    s.close()


def cfg(service, url, model):
    return {"service": service, "base_url": url, "model": model}


def ask(c, key="sk-test", question="Summarize", **kw):
    got = []
    reply = agents.stream_reply(c, key, "SYSTEM PROMPT", [{"role": "user", "text": question}], got.append, **kw)
    return reply, got


def test_openai_compatible_streams_and_sends_history(server):
    c = cfg("custom", server.url + "/v1", "gpt-test")
    reply, chunks = ask(c)
    assert reply == "Hello from the mock." and chunks == ["Hello", " from", " the", " mock."]
    _method, path, headers, body = server.requests[-1]
    assert path == "/v1/chat/completions" and headers["Authorization"] == "Bearer sk-test"
    assert body["messages"][0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert body["messages"][1] == {"role": "user", "content": "Summarize"} and body["stream"] is True


def test_anthropic_streams(server):
    c = cfg("anthropic", server.url, "claude-test-1")
    reply, _ = ask(c)
    assert reply == "Hello from the mock."
    _m, path, headers, body = server.requests[-1]
    headers = {k.lower(): v for k, v in headers.items()}
    assert path == "/v1/messages" and headers["x-api-key"] == "sk-test" and headers["anthropic-version"]
    assert body["system"] == "SYSTEM PROMPT" and body["messages"] == [{"role": "user", "content": "Summarize"}]


def test_ollama_sizes_the_context_window_to_the_meeting(server):
    c = cfg("ollama", server.url, "llama-test:8b")
    long_system = "word " * 20000                          # ~28k tokens: far above Ollama's small default window
    got = []
    reply = agents.stream_reply(c, "", long_system, [{"role": "user", "text": "hi"}], got.append)
    assert reply == "Hello from the mock."
    body = [r for r in server.requests if r[1] == "/api/chat"][-1][3]
    assert body["options"]["num_ctx"] >= agents.estimate_tokens(long_system) + agents.REPLY_TOKENS - 10
    assert body["messages"][0]["role"] == "system"


def test_too_long_for_the_model_is_explained_before_sending(server):
    server.ollama_ctx = 4096
    c = cfg("ollama", server.url, "llama-test:8b")
    with pytest.raises(agents.AgentError, match="larger context window"):
        ask(c, key="", question="x" * 40000)
    assert not [r for r in server.requests if r[1] == "/api/chat"]


def test_list_models(server):
    assert agents.list_models(cfg("custom", server.url + "/v1", ""), "sk-test") == ["gpt-big", "gpt-test"]
    assert agents.list_models(cfg("anthropic", server.url, ""), "sk-test") == ["claude-test-1", "claude-test-2"]
    assert agents.list_models(cfg("ollama", server.url, ""), "") == ["llama-test:8b", "qwen-test:7b"]
    assert agents.list_models(cfg("claude-code", "", ""), "")[0] == "default"


@pytest.mark.parametrize("service,model,key,pattern", [
    ("custom", "gpt-test", "wrong", "didn't accept the API key"),
    ("anthropic", "claude-test-1", "wrong", "didn't accept the API key"),
    ("custom", "no-such-model", "sk-test", "no model called “no-such-model”"),
    ("custom", "rate-limited", "sk-test", "rate limit"),
    ("custom", "too-long", "sk-test", "too long for too-long"),
    ("ollama", "missing:1b", "", "no model called “missing:1b”"),
])
def test_errors_say_what_to_do(server, service, model, key, pattern):
    url = server.url + ("/v1" if service == "custom" else "")
    with pytest.raises(agents.AgentError, match=pattern) as e:
        ask(cfg(service, url, model), key=key)
    assert "sk-test" not in str(e.value) and "wrong" not in str(e.value)


def test_unreachable_local_server_is_explained():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]           # nothing listens here once the socket closes
    with pytest.raises(agents.AgentError, match="Make sure Ollama is running"):
        ask(cfg("ollama", f"http://127.0.0.1:{port}", "llama-test:8b"), key="")


def test_missing_settings_are_reported_before_any_request():
    assert agents.problem(cfg("openai", "", "")) == "Choose a model."
    assert agents.problem(cfg("openai", "", "gpt-test")).startswith("Enter your OpenAI API key")
    assert agents.problem(cfg("custom", "", "m")) == "Enter the server address."
    assert agents.problem(cfg("lmstudio", "", "m")) is None          # local servers need no key
    assert agents.problem(cfg("claude-code", "", "")) is None


def test_stop_part_way(server):
    server.delay = 0.05
    server.words = [f"w{i} " for i in range(40)]
    got = []
    reply = agents.stream_reply(cfg("custom", server.url + "/v1", "gpt-test"), "sk-test", "S",
                                [{"role": "user", "text": "q"}], got.append, should_stop=lambda: len(got) >= 3)
    assert 3 <= len(got) < 40 and reply == "".join(got)


def test_test_connection(server):
    assert "replied “Hello from the mock.”" in agents.test_connection(cfg("custom", server.url + "/v1", "gpt-test"),
                                                                       "sk-test")
    assert server.requests[-1][3]["max_tokens"] == 16


def test_config_round_trip_never_stores_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "CONFIG_DIR", tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps({"engine": 1, "agent": {"service": "bogus"}}))
    assert agents.load_config() == agents.DEFAULT                     # unknown service: back to Claude Code
    agents.save_config({"service": "openai", "model": "gpt-test", "key": "sk-secret"})
    data = json.loads((tmp_path / "settings.json").read_text())
    assert data["engine"] == 1 and data["agent"] == {"service": "openai", "base_url": "", "model": "gpt-test"}
    assert "sk-secret" not in (tmp_path / "settings.json").read_text()
    assert agents.base_url(agents.load_config()) == "https://api.openai.com/v1"


def test_names_and_privacy_notes():
    assert agents.display_name(agents.DEFAULT) == "Claude"
    assert agents.display_name(cfg("openai", "", "gpt-test")) == "gpt-test"
    assert "Runs on your own computer" in agents.usage_note(cfg("ollama", "", "m"))
    assert "Runs on your own computer" in agents.usage_note(cfg("custom", "http://localhost:8080/v1", "m"))
    assert "Uses your OpenAI account" in agents.usage_note(cfg("openai", "", "m"))
