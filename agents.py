"""AI agents that can answer questions about a meeting: which ones exist, how they're configured, and how to
talk to them.

Pure Python (standard library only): the window runs on the system Python, which can't install packages.

Kinds of agent:
  claude-code  the Claude Code CLI with your Claude subscription (handled in chat.py; the default)
  openai       any OpenAI-compatible chat API: OpenAI, Google Gemini, OpenRouter, Groq, Mistral, Together,
               LM Studio, llama.cpp, vLLM or your own server
  anthropic    the Anthropic API with an API key
  ollama       Ollama on this computer (its own API, so the context window can be sized to the transcript)

The chosen agent lives in settings.json under "agent" ({"service", "base_url", "model"}); API keys never do,
see agentkeys.py. API agents get the whole conversation again on every question, with the transcript in the
system prompt, so switching agents part-way through a chat just works.
"""
import json
import os
import pathlib
import socket
import urllib.error
import urllib.parse
import urllib.request

CONFIG_DIR = pathlib.Path(os.environ.get("MEETSCRIBE_CONFIG_DIR", pathlib.Path.home() / ".config/meetscribe"))

SYSTEM = (
    "You are the meeting assistant inside MeetScribe, a desktop app. The user's meeting transcript is below. "
    "'Them' is the meeting audio (the other participants, possibly several people) and 'You' is the user's own "
    "microphone. The transcript comes from speech recognition, so expect misheard words and names; when something "
    "is unclear, say so instead of guessing. Base answers on the transcript; you may also help with writing tasks "
    "built on it (emails, notes, plans). Format replies in Markdown with short headings, bullet lists and bold "
    "for owners and deadlines. Do not use tables."
)

# id, label, kind, default server address, needs a key ("yes" / "no" / "optional"), runs on this computer,
# where to get a key or the app, one-line description
SERVICES = [
    ("claude-code", "Claude Code", "claude-code", "", "no", False, "https://docs.anthropic.com/en/docs/claude-code",
     "Uses your Claude subscription through the Claude Code app. No API key needed."),
    ("openai", "OpenAI (ChatGPT models)", "openai", "https://api.openai.com/v1", "yes", False,
     "https://platform.openai.com/api-keys", "GPT models with an OpenAI API key (billed by OpenAI per use)."),
    ("anthropic", "Anthropic API", "anthropic", "https://api.anthropic.com", "yes", False,
     "https://console.anthropic.com/settings/keys", "Claude models with an Anthropic API key (billed per use)."),
    ("gemini", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai", "yes", False,
     "https://aistudio.google.com/apikey", "Gemini models with a Google AI Studio API key."),
    ("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "yes", False,
     "https://openrouter.ai/keys", "Hundreds of models from many companies with one OpenRouter key."),
    ("groq", "Groq", "openai", "https://api.groq.com/openai/v1", "yes", False,
     "https://console.groq.com/keys", "Very fast open models (Llama, Qwen…) with a Groq API key."),
    ("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", "yes", False,
     "https://console.mistral.ai/api-keys", "Mistral models with a Mistral API key."),
    ("together", "Together AI", "openai", "https://api.together.xyz/v1", "yes", False,
     "https://api.together.xyz/settings/api-keys", "Open models (Llama, DeepSeek, Qwen…) with a Together key."),
    ("ollama", "Ollama (on this computer)", "ollama", "http://localhost:11434", "no", True,
     "https://ollama.com/download", "Free models that run on your own computer. Nothing leaves it."),
    ("lmstudio", "LM Studio (on this computer)", "openai", "http://localhost:1234/v1", "no", True,
     "https://lmstudio.ai", "Free models in LM Studio on your own computer. Start its local server first."),
    ("custom", "Other OpenAI-compatible server", "openai", "", "optional", None, "",
     "llama.cpp, vLLM, LiteLLM or any server with an OpenAI-style /chat/completions API."),
]
SERVICE = {s[0]: dict(zip(("id", "label", "kind", "base_url", "key", "local", "key_url", "about"), s, strict=True))
           for s in SERVICES}
DEFAULT = {"service": "claude-code", "base_url": "", "model": ""}
CLAUDE_CODE_MODELS = ["default", "sonnet", "opus", "haiku"]

# Rough context windows (tokens) by model name, to warn before sending a transcript that can't fit.
# Unknown models are simply sent; the server's own "too long" error is then explained instead.
CONTEXT = [("gpt-4.1", 1_000_000), ("gpt-5", 400_000), ("gpt-4o", 128_000), ("o3", 200_000), ("o4", 200_000),
           ("claude", 200_000), ("gemini", 1_000_000), ("llama-3.1", 128_000), ("llama3.1", 128_000),
           ("llama-3.3", 128_000), ("llama3.3", 128_000), ("llama3.2", 128_000), ("mistral-large", 128_000),
           ("mistral-small", 128_000), ("mixtral", 32_000), ("deepseek", 64_000), ("qwen2.5", 32_000)]
CHARS_PER_TOKEN = 3.5       # cautious average for English text; other scripts use more tokens per character
REPLY_TOKENS = 4096
CLOUD_TIMEOUT = 180         # seconds to wait for each piece of a reply
LOCAL_TIMEOUT = 900         # local models can take minutes just to read a long transcript


class AgentError(Exception):
    """A problem the user can act on; the message is shown as it is."""


# --- configuration -------------------------------------------------------------------------------------------
def _settings_file():
    return CONFIG_DIR / "settings.json"


def load_config():
    try:
        data = json.loads(_settings_file().read_text(encoding="utf-8")).get("agent") or {}
    except (OSError, ValueError, AttributeError):
        data = {}
    cfg = {**DEFAULT, **{k: v for k, v in data.items() if isinstance(v, str)}}
    if cfg["service"] not in SERVICE:
        cfg = dict(DEFAULT)
    return cfg


def save_config(cfg):
    """Store the chosen agent in settings.json (merged with the other settings). Never stores keys."""
    clean = {"service": cfg.get("service", "claude-code"), "base_url": (cfg.get("base_url") or "").strip(),
             "model": (cfg.get("model") or "").strip()}
    path = _settings_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data["agent"] = clean
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return clean


def service(cfg):
    return SERVICE.get(cfg.get("service"), SERVICE["claude-code"])


def kind(cfg):
    return service(cfg)["kind"]


def base_url(cfg):
    return ((cfg.get("base_url") or service(cfg)["base_url"]) or "").rstrip("/")


def account(cfg):
    """Name an API key is stored under: one key per service (and server address, for custom servers)."""
    return f"{cfg.get('service')}:{base_url(cfg)}"


def env_key(cfg):
    """An API key from the usual environment variable, for people who already set one."""
    names = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY",
             "openrouter": "OPENROUTER_API_KEY", "groq": "GROQ_API_KEY", "mistral": "MISTRAL_API_KEY",
             "together": "TOGETHER_API_KEY"}
    name = names.get(cfg.get("service"))
    return os.environ.get(name, "") if name else ""


def is_local(cfg):
    local = service(cfg)["local"]
    if local is None:       # custom server: local if its address is
        host = urllib.parse.urlparse(base_url(cfg)).hostname or ""
        return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")
    return local


def display_name(cfg):
    """Short name for the answering agent, used in the chat ("Claude is thinking…")."""
    if kind(cfg) == "claude-code":
        return "Claude"
    return cfg.get("model") or service(cfg)["label"]


def describe(cfg):
    """Longer name: "gpt-4o-mini via OpenAI (ChatGPT models)"."""
    if kind(cfg) == "claude-code":
        model = cfg.get("model")
        return "Claude via Claude Code" + (f" ({model})" if model and model != "default" else "")
    return f"{cfg.get('model') or 'no model chosen'} via {service(cfg)['label']}"


def usage_note(cfg):
    """Where the transcript goes when you ask a question, in one sentence."""
    if kind(cfg) == "claude-code":
        return "Uses your Claude subscription; only the transcript text is sent, never the audio."
    if is_local(cfg):
        return "Runs on your own computer; nothing leaves it."
    if service(cfg)["id"] == "custom":
        return "Sends the transcript text (never the audio) to the server address above."
    return f"Uses your {service(cfg)['label'].split(' (')[0]} account; only the transcript text is sent, never the audio."


def problem(cfg, key=""):
    """Why this agent can't be used yet (a sentence), or None. `key` is the stored API key, if any."""
    s = service(cfg)
    if s["kind"] == "claude-code":
        return None     # chat.py checks for the claude command itself
    if not base_url(cfg):
        return "Enter the server address."
    if not cfg.get("model"):
        return "Choose a model."
    if s["key"] == "yes" and not (key or env_key(cfg)):
        return f"Enter your {s['label'].split(' (')[0]} API key."
    return None


# --- talking to the services -------------------------------------------------------------------------------
def _headers(cfg, key):
    k = kind(cfg)
    h = {"Content-Type": "application/json", "User-Agent": "MeetScribe"}
    if k == "anthropic":
        h.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif key:
        h["Authorization"] = f"Bearer {key}"
    if cfg.get("service") == "openrouter":
        h["X-Title"] = "MeetScribe"
    return h


def _request(cfg, key, path, body=None, timeout=None):
    url = base_url(cfg) + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(cfg, key), method="POST" if data else "GET")
    try:
        return urllib.request.urlopen(req, timeout=timeout or (LOCAL_TIMEOUT if is_local(cfg) else CLOUD_TIMEOUT))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")
        except OSError:
            detail = ""
        raise AgentError(http_problem(cfg, e.code, detail)) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise AgentError(network_problem(cfg, e)) from None


def _detail(text):
    """The human part of an error body, short."""
    try:
        data = json.loads(text)
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            text = err.get("message") or err.get("type") or json.dumps(err)
        elif isinstance(err, str):
            text = err
    except ValueError:
        pass
    text = " ".join(str(text).split())
    return text[:200] + ("…" if len(text) > 200 else "")


def http_problem(cfg, code, body=""):
    name = service(cfg)["label"].split(" (")[0]
    detail = _detail(body)
    low = detail.lower()
    model = cfg.get("model") or "that model"
    if any(w in low for w in ("context length", "context_length", "context window", "too long", "maximum context",
                              "too many tokens", "prompt is too long", "reduce the length")):
        return (f"This meeting's transcript is too long for {model}. Choose a model with a larger context window "
                "in AI agent settings.")
    if code == 401:
        return f"{name} didn't accept the API key. Check it in AI agent settings."
    if code == 403:
        return f"{name} refused the request: {detail or 'this key may not have access to ' + model}."
    if code == 404:
        if "model" in low or cfg.get("model", "") in detail:
            return f"{name} has no model called “{model}”. Choose one from the list in AI agent settings."
        return f"There's no chat API at {base_url(cfg)}. Check the server address in AI agent settings."
    if code == 429:
        return f"{name} says you've hit a rate limit or run out of credit. Wait a minute, or check your plan. ({detail})"
    if code >= 500:
        return f"{name} had a problem on its side (error {code}). Try again in a minute."
    return f"{name} returned error {code}" + (f": {detail}" if detail else ".")


def network_problem(cfg, error):
    reason = getattr(error, "reason", error)
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason):
        return f"{service(cfg)['label'].split(' (')[0]} took too long to answer. Try again, or pick a faster model."
    if is_local(cfg):
        return (f"Couldn't reach {base_url(cfg)}. Make sure {service(cfg)['label'].split(' (')[0]} is running "
                "(and its server is started), then try again.")
    return f"Couldn't reach {base_url(cfg)}. Check your internet connection and the server address."


def list_models(cfg, key=""):
    """Model names the service offers (sorted), or raises AgentError."""
    k = kind(cfg)
    if k == "claude-code":
        return list(CLAUDE_CODE_MODELS)
    key = key or env_key(cfg)
    with _request(cfg, key, "/api/tags" if k == "ollama" else "/v1/models" if k == "anthropic" else "/models",
                  timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    if k == "ollama":
        names = [m.get("name") or m.get("model") for m in data.get("models", [])]
    else:
        names = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
    return sorted({n for n in names if n})


def context_limit(cfg, key=""):
    """Context window in tokens if known (Ollama is asked; others come from CONTEXT), else None."""
    model = (cfg.get("model") or "").lower()
    if kind(cfg) == "ollama":
        try:
            with _request(cfg, key, "/api/show", {"model": cfg.get("model")}, timeout=20) as resp:
                info = json.loads(resp.read().decode("utf-8", "replace") or "{}").get("model_info", {})
            sizes = [v for k, v in info.items() if k.endswith(".context_length") and isinstance(v, int)]
            if sizes:
                return max(sizes)
        except AgentError:
            pass
    for name, size in CONTEXT:
        if name in model:
            return size
    return None


def estimate_tokens(*texts):
    return int(sum(len(t) for t in texts) / CHARS_PER_TOKEN) + 1


def _sse(resp, should_stop):
    """Yield the JSON payloads of a server-sent-events stream."""
    for raw in resp:
        if should_stop():
            return
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except ValueError:
            continue


def stream_reply(cfg, key, system, messages, on_delta, should_stop=lambda: False, max_tokens=None):
    """Ask an API agent. `messages` is the chat so far ([{"role": "user"|"assistant", "text": …}], ending with
    the new question). Calls on_delta(chunk) as text arrives and returns the whole reply. Raises AgentError."""
    k = kind(cfg)
    if k == "claude-code":
        raise AgentError("Claude Code is used through chat.Chat, not stream_reply().")
    key = key or env_key(cfg)
    issue = problem(cfg, key)
    if issue:
        raise AgentError(issue)
    history = [{"role": m["role"], "content": m["text"]} for m in messages if m.get("text")]
    tokens = estimate_tokens(system, *(m["content"] for m in history))
    limit = context_limit(cfg, key)
    if limit and tokens + (max_tokens or 1024) > limit:
        raise AgentError(f"This meeting is about {tokens:,} tokens, more than {cfg['model']} can read at once "
                         f"({limit:,}). Choose a model with a larger context window in AI agent settings.")
    parts = []

    def add(text):
        if text:
            parts.append(text)
            on_delta(text)

    if k == "anthropic":
        body = {"model": cfg["model"], "max_tokens": max_tokens or REPLY_TOKENS, "system": system,
                "messages": history, "stream": True}
        with _request(cfg, key, "/v1/messages", body) as resp:
            for event in _sse(resp, should_stop):
                if event.get("type") == "content_block_delta":
                    add(event.get("delta", {}).get("text", ""))
                elif event.get("type") == "error":
                    raise AgentError(http_problem(cfg, 500 if "overloaded" in json.dumps(event) else 400,
                                                  json.dumps(event)))
    elif k == "ollama":
        # Ollama reads only its default few thousand tokens unless told otherwise, silently dropping the start of
        # a long transcript: size the window to the meeting.
        need = tokens + (max_tokens or REPLY_TOKENS)
        options = {"num_ctx": min(limit, need) if limit else max(need, 8192)}
        if max_tokens:
            options["num_predict"] = max_tokens
        body = {"model": cfg["model"], "messages": [{"role": "system", "content": system}, *history],
                "stream": True, "options": options}
        with _request(cfg, key, "/api/chat", body) as resp:
            for raw in resp:
                if should_stop():
                    break
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if event.get("error"):
                    raise AgentError(http_problem(cfg, 404 if "not found" in event["error"] else 500,
                                                  json.dumps(event)))
                add(event.get("message", {}).get("content", ""))
                if event.get("done"):
                    break
    else:
        body = {"model": cfg["model"], "messages": [{"role": "system", "content": system}, *history], "stream": True}
        if max_tokens:
            body["max_tokens"] = max_tokens
        with _request(cfg, key, "/chat/completions", body) as resp:
            for event in _sse(resp, should_stop):
                if event.get("error"):
                    err = event["error"]
                    code = err.get("code") if isinstance(err, dict) else None
                    raise AgentError(http_problem(cfg, code if isinstance(code, int) else 400, json.dumps(event)))
                for choice in event.get("choices") or []:
                    add((choice.get("delta") or {}).get("content") or "")
    return "".join(parts)


def test_connection(cfg, key=""):
    """Try the agent with a tiny question. Returns a sentence saying it works, or raises AgentError."""
    if kind(cfg) == "claude-code":
        raise AgentError("Claude Code is checked through chat.check_claude_code().")
    reply = stream_reply(cfg, key, "You are a connection test.", [{"role": "user", "text": "Reply with just: OK"}],
                         lambda _t: None, max_tokens=16)
    said = " ".join(reply.split())[:40]
    return f"Connected. {cfg['model']} replied “{said}”." if said else f"Connected to {cfg['model']}."
