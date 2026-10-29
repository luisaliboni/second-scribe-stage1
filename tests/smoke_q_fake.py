"""Test helper for smoke-q: a local fake of a llama-server / MTPLX style OpenAI-compatible endpoint (POST
/v1/chat/completions on 127.0.0.1, no network beyond loopback). It answers only the three Q-phase prompts, with
valid replies built from the prompt text, and mimics the thinking switch: a request without
``chat_template_kwargs.enable_thinking == false`` gets a ``reasoning_content`` field (as a server that ignored the
switch would), and ``always_think`` does that for every request."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from second_scribe.demo_oracle import _transcript_of
from second_scribe.schemas import NODE_TYPES

_NUM = re.compile(r"^(\d+)\. (.*)$", re.M)
PHASES = ("Planning", "Permits", "Inspections", "Operating")


def _block(text: str, start: str) -> str:
    i = text.index(start) + len(start)
    stop = text.find("\nREMINDER:", i)
    return text[i:stop if stop >= 0 else len(text)].strip("\n")


def answer(user: str) -> dict:
    """The JSON a well-behaved model would give for this Q-phase prompt."""
    if '"map_talk"' in user:
        turns = {int(n) for n, _ in _NUM.findall(_block(user, "TURNS:\n"))}
        return {"answers": [{"n": n, "map_talk": "no"} for n in sorted(turns)]}
    transcript = _transcript_of(user)
    if transcript is None:                       # prior_only: no transcript, no quotes
        nodes = [{"id": f"n{k}", "label": f"Step {k}", "type": NODE_TYPES[k % 3], "phase": PHASES[k % 4],
                  "status": "experienced", "quote": None} for k in range(1, 6)]
        edges = [{"source": f"n{k}", "target": f"n{k + 1}", "status": "experienced", "quote": None}
                 for k in range(1, 5)]
        return {"nodes": nodes, "edges": edges, "emotion_word": None}
    lines = [x for x in transcript.splitlines() if len(x.split()) >= 8][:3] or [transcript[:200]]
    quotes = [" ".join(x.split()[-6:]) for x in lines]
    nodes = [{"id": f"n{k}", "label": f"Step {k}", "type": NODE_TYPES[k % 3], "phase": PHASES[k % 4],
              "status": "experienced", "quote": q} for k, q in enumerate(quotes, 1)]
    edges = [{"source": f"n{k}", "target": f"n{k + 1}", "status": "experienced", "quote": quotes[k - 1]}
             for k in range(1, len(quotes))]
    return {"nodes": nodes, "edges": edges, "emotion_word": None}


class FakeQwen:
    """``with FakeQwen() as srv:`` then ``srv.base_url`` (ends in /v1), ``srv.requests`` (parsed bodies)."""

    def __init__(self, *, always_think: bool = False, model: str = "fake-qwen38-flashnext", delay_s: float = 0.0):
        self.always_think, self.model, self.delay_s = always_think, model, delay_s
        self.requests: list[dict] = []
        self.paths: list[str] = []
        self.auth: list[str | None] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):            # silent
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                fake.paths.append(self.path)
                fake.auth.append(self.headers.get("Authorization"))
                fake.requests.append(body)
                if fake.delay_s:
                    time.sleep(fake.delay_s)
                user = next(m["content"] for m in body["messages"] if m["role"] == "user")
                text = json.dumps(answer(user), sort_keys=True)
                message = {"role": "assistant", "content": text}
                kw = body.get("chat_template_kwargs") or {}
                if fake.always_think or kw.get("enable_thinking") is not False:
                    message["reasoning_content"] = "let me think about this first"
                prompt_tokens = sum(len(m["content"]) for m in body["messages"]) // 4
                out = json.dumps({"id": hashlib.sha256(text.encode()).hexdigest()[:12], "model": fake.model,
                                  "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(text) // 4,
                                            "total_tokens": prompt_tokens + len(text) // 4}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self):
                fake.paths.append(self.path)
                self.send_response(404)
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
