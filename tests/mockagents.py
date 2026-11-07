"""A tiny local server that answers like OpenAI, Anthropic and Ollama, for testing agents.py without real keys."""
import http.server
import json
import threading
import time


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockAgent"

    def log_message(self, *_args):
        pass

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, lines, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Connection", "close")
        self.end_headers()
        for line in lines:
            self.wfile.write(line.encode())
            self.wfile.flush()
            time.sleep(self.server.delay)
        self.close_connection = True

    def _auth_ok(self, header, prefix=""):
        return self.headers.get(header) == prefix + self.server.key

    def do_GET(self):
        self.server.requests.append(("GET", self.path, dict(self.headers), None))
        if self.path == "/v1/models" and self.headers.get("x-api-key") is not None:      # Anthropic
            if not self._auth_ok("x-api-key"):
                return self._json(401, {"type": "error", "error": {"type": "authentication_error",
                                                                    "message": "invalid x-api-key"}})
            return self._json(200, {"data": [{"id": "claude-test-1"}, {"id": "claude-test-2"}]})
        if self.path == "/v1/models":                                                       # OpenAI-style
            if self.server.key and not self._auth_ok("Authorization", "Bearer "):
                return self._json(401, {"error": {"message": "Incorrect API key provided"}})
            return self._json(200, {"data": [{"id": "gpt-test"}, {"id": "gpt-big"}]})
        if self.path == "/api/tags":
            return self._json(200, {"models": [{"name": "llama-test:8b"}, {"name": "qwen-test:7b"}]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.server.requests.append(("POST", self.path, dict(self.headers), body))
        model = body.get("model", "")
        if model == "rate-limited":
            return self._json(429, {"error": {"message": "Rate limit reached for requests"}})
        if model == "too-long":
            return self._json(400, {"error": {"message": "This model's maximum context length is 8192 tokens"}})
        if self.path == "/v1/chat/completions":
            if self.server.key and not self._auth_ok("Authorization", "Bearer "):
                return self._json(401, {"error": {"message": "Incorrect API key provided"}})
            if model not in ("gpt-test", "gpt-big"):
                return self._json(404, {"error": {"message": f"The model `{model}` does not exist"}})
            chunks = [{"choices": [{"delta": {"role": "assistant"}}]}] + \
                     [{"choices": [{"delta": {"content": w}}]} for w in self.server.words]
            return self._stream([f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"],
                                "text/event-stream")
        if self.path == "/v1/messages":
            if not self._auth_ok("x-api-key"):
                return self._json(401, {"type": "error", "error": {"message": "invalid x-api-key"}})
            events = [("message_start", {"type": "message_start"})] + \
                     [("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": w}}) for w in self.server.words] + \
                     [("message_stop", {"type": "message_stop"})]
            return self._stream([f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events], "text/event-stream")
        if self.path == "/api/show":
            return self._json(200, {"model_info": {"llama.context_length": self.server.ollama_ctx}})
        if self.path == "/api/chat":
            if model not in ("llama-test:8b", "qwen-test:7b"):
                return self._json(404, {"error": f"model '{model}' not found"})
            lines = [json.dumps({"message": {"role": "assistant", "content": w}, "done": False}) + "\n"
                     for w in self.server.words] + [json.dumps({"done": True}) + "\n"]
            return self._stream(lines, "application/x-ndjson")
        self._json(404, {"error": "no such path"})


class MockAgentServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, key="sk-test", words=("Hello", " from", " the", " mock."), delay=0.0, ollama_ctx=131072):
        super().__init__(("127.0.0.1", 0), Handler)
        self.key, self.words, self.delay, self.ollama_ctx = key, list(words), delay, ollama_ctx
        self.requests = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def close(self):
        self.shutdown()
        self.server_close()
