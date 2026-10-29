"""A fake model server for the app's tests. Test infrastructure only: it never calls a real model, Ollama, LM Studio or
any API, and it binds 127.0.0.1 on an OS-chosen port (port 0) and nothing else.

``MockModels(configs)`` answers the kit's prompts from ``demo_oracle.OracleClient`` (one per config, built from the
config's roles and labels exactly as ``cli.build_clients("oracle", cfg)`` builds it; tried in order until one answers).
It speaks the shapes the app has to discover and call:

  POST /v1/chat/completions   OpenAI-compatible chat (llama-server, MTPLX, LM Studio, Ollama's /v1)
  GET  /v1/models             OpenAI model list
  GET  /api/tags              Ollama list: details.family / families, digest, remote_host when cloud
  POST /api/show              Ollama show: details, model_info["general.architecture"], digest, remote_host when cloud
  POST /api/generate          Ollama load / unload with keep_alive (an empty prompt); a prompt is answered by the oracle
  GET  /api/v1/models         LM Studio v1 list (``architecture``)
  GET  /api/v0/models         LM Studio v0 list (``arch``)
  POST /v1/messages           Anthropic Messages (cloud stand-in)

Behaviour switches, changed at run time with ``set(**flags)`` (all off by default):

  thinking=True              chat replies carry ``reasoning_content``; /v1/messages replies carry a "thinking" block
  ignore_thinking_flag=True  a server that ignores the switch: a chat request that sends
                             ``chat_template_kwargs.enable_thinking`` false still gets ``reasoning_content``; a request
                             without the switch, or with it true, gets none
  slow_s=float               every reply waits this many seconds (stop() ends the wait at once)
  malformed=True             chat / messages / generate replies are HTTP 200 whose body is not JSON
  malformed_match=str        the same, only for a request whose message text contains this string (one item's
                             prompts, say: the kit retries that item, then gives it up); None turns it off
  cloud=True                 discovery (/api/tags, /api/show) reports ``remote_host`` for every model (its own from
                             ``models``, else ``DEFAULT_REMOTE_HOST``)
  canary_fail=True           the judge prompt is answered ``actual`` for every element
  status=int                 every request is answered with this HTTP error code (None turns it off)
  unload_ok=True             POST /api/generate with keep_alive 0 really unloads (``done_reason`` "unload"); off, the
                             server ignores the unload (``done_reason`` "load", the model stays in ``loaded``)

``MockModels(configs, ping=True)`` also answers the app's connection-test prompt (``app.models.PING_PROMPT``) with
``{"ok": true}``, so the Setup connection test passes through the real route (Task 44); off by default.

``requests`` holds one dict per request received: ``method``, ``path``, ``model`` (None when the body names none),
``headers`` (without Authorization and x-api-key, so no key ever sits in a test's memory or output), ``body`` (the
parsed JSON, or None). ``loaded`` is the set of models /api/generate has loaded. Stop it with ``stop()``, the context
manager, or ``addCleanup(srv.stop)``: stop joins the serving thread and every handler thread and closes the socket.
"""
from __future__ import annotations

import hashlib
import json
import re
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from second_scribe.cli import build_clients
from second_scribe.demo_oracle import _ITEM, _block
from second_scribe.pipeline import RunConfig

DEFAULT_REMOTE_HOST = "https://ollama.mock.invalid:443"
FLAGS = ("thinking", "ignore_thinking_flag", "slow_s", "malformed", "malformed_match", "cloud", "canary_fail", "status",
         "unload_ok")
_SECRET_HEADERS = {"authorization", "x-api-key"}
_MODIFIED = "2026-10-18T00:00:00Z"


def _digest(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def default_models() -> dict[str, dict]:
    return {"qwen-mock:7b": {"family": "qwen2", "digest": _digest("qwen-mock:7b")},
            "gemma-mock:9b": {"family": "gemma2", "digest": _digest("gemma-mock:9b")}}


class Unanswerable(Exception):
    """No oracle could answer the prompt (or the request has no usable messages)."""


def _text_of(content) -> str:
    """Message content as text: a string, or a list of blocks (Anthropic) of which the text blocks count."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type", "text") == "text")
    raise Unanswerable("message content is neither text nor blocks")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):              # silent
        pass

    # ------------------------------------------------------------------ plumbing
    def _send(self, code: int, payload, *, raw: bool = False) -> None:
        data = (payload if raw else json.dumps(payload)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain" if raw else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        srv: MockModels = self.server            # type: ignore[assignment]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
                body = parsed if isinstance(parsed, dict) else None
            except ValueError:
                body = None
        path = self.path.split("?", 1)[0]
        model = body.get("model", body.get("name")) if body else None
        srv.requests.append({"method": method, "path": path, "model": model if isinstance(model, str) else None,
                             "headers": {k: v for k, v in self.headers.items() if k.lower() not in _SECRET_HEADERS},
                             "body": body})
        flags = dict(srv.flags)
        if flags["slow_s"]:
            srv._stopping.wait(float(flags["slow_s"]))
        if flags["status"]:
            return self._send(int(flags["status"]), {"error": {"message": "mock error", "type": "mock_error"}})
        route = srv.routes.get((method, path))
        if route is None:
            return self._send(404, {"error": {"message": "not found", "type": "mock_not_found"}})
        try:
            code, payload = route(srv, body or {}, flags)
        except Unanswerable as exc:
            return self._send(422, {"error": {"message": f"mock cannot answer: {exc}", "type": "mock_unanswerable"}})
        if payload is _MALFORMED:
            return self._send(200, "this is not json {", raw=True)
        self._send(code, payload)

    def do_GET(self):
        self._safe("GET")

    def do_POST(self):
        self._safe("POST")

    def _safe(self, method: str) -> None:
        try:
            self._handle(method)
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception as exc:  # noqa: BLE001 - a bug in the mock must be visible, not a hung client
            try:
                self._send(500, {"error": {"message": f"mock failure: {type(exc).__name__}", "type": "mock_bug"}})
            except OSError:
                pass


_MALFORMED = object()


class MockModels(ThreadingHTTPServer):
    daemon_threads = False        # handler threads are joined by server_close(), so a test leaves none behind
    block_on_close = True

    def __init__(self, configs: list, *, models: dict[str, dict] | None = None, ping: bool = False):
        super().__init__(("127.0.0.1", 0), _Handler, bind_and_activate=False)    # bound by start()
        self.oracles = []
        for path in configs:
            cfg = RunConfig.load(Path(path))
            self.oracles.append(build_clients("oracle", cfg)["recon"])
        self.models = {k: dict(v) for k, v in (default_models() if models is None else models).items()}
        self.ping = bool(ping)
        self.flags: dict = {"thinking": False, "ignore_thinking_flag": False, "slow_s": 0.0, "malformed": False,
                            "malformed_match": None, "cloud": False, "canary_fail": False, "status": None,
                            "unload_ok": False}
        self.requests: list[dict] = []
        self.loaded: set[str] = set()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------ lifecycle
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)          # no reverse-DNS lookup of the address, unlike HTTPServer
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port

    def start(self) -> str:
        with self._lock:
            if self._closed:
                raise RuntimeError("this MockModels was stopped; make a new one")
            if self._thread is None:
                try:
                    self.server_bind()
                    self.server_activate()
                except BaseException:
                    self.server_close()
                    raise
                self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05},
                                                name="mock-models", daemon=True)
                self._thread.start()
        return self.base_url

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stopping.set()                               # ends any slow_s wait
        if self._thread is not None:
            self.shutdown()
            self._thread.join(timeout=10)
        self.server_close()                                # joins the handler threads, closes the socket

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def set(self, **flags) -> None:
        bad = sorted(set(flags) - set(FLAGS))
        if bad:
            raise TypeError(f"unknown flag {bad[0]!r}; flags are {', '.join(FLAGS)}")
        self.flags.update(flags)

    # ------------------------------------------------------------------ answering
    def answer(self, messages, temperature=0.0, seed=None, max_tokens=8000, *, canary_fail=False) -> str:
        """The oracle's text for ``messages`` (first oracle that answers wins); Unanswerable if none does."""
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            raise Unanswerable("no messages")
        msgs = [{**m, "content": _text_of(m.get("content"))} for m in messages]
        user = next((m["content"] for m in msgs if m.get("role") == "user"), None)
        if user is None:
            raise Unanswerable("no user message")
        if self.ping:
            from second_scribe.app.models import PING_PROMPT
            if PING_PROMPT in user:
                return '{"ok": true}'
        if canary_fail and '{"labels"' in user:
            items = _ITEM.findall(_block(user, "ITEMS:\n"))
            return json.dumps({"labels": [{"n": int(n), "modality": "actual"} for n, *_ in items]},
                              ensure_ascii=False, sort_keys=True)
        for oracle in self.oracles:
            try:
                return oracle.complete(msgs, temperature=temperature, seed=seed, max_tokens=max_tokens).text
            except Exception:  # noqa: BLE001 - this oracle does not know the prompt; try the next one
                continue
        raise Unanswerable("no oracle knows this prompt")

    @staticmethod
    def _malformed(flags: dict, messages) -> bool:
        """``malformed``, or ``malformed_match`` found in the text of one of ``messages``."""
        if flags["malformed"]:
            return True
        needle = flags["malformed_match"]
        if not needle or not isinstance(messages, list):
            return False
        return any(needle in _text_of(m.get("content")) for m in messages if isinstance(m, dict))

    def _remote_host(self, name: str) -> str | None:
        if not self.flags["cloud"]:
            return None
        return self.models[name].get("remote_host") or DEFAULT_REMOTE_HOST

    # ------------------------------------------------------------------ endpoints
    def _chat(self, body: dict, flags: dict):
        text = self.answer(body.get("messages"), body.get("temperature", 0.0), body.get("seed"),
                           body.get("max_tokens", 8000), canary_fail=flags["canary_fail"])
        if self._malformed(flags, body.get("messages")):
            return 200, _MALFORMED
        message = {"role": "assistant", "content": text}
        kw = body.get("chat_template_kwargs")
        kw = kw if isinstance(kw, dict) else {}
        if flags["thinking"] or (flags["ignore_thinking_flag"] and kw.get("enable_thinking") is False):
            message["reasoning_content"] = "let me think about this first"
        prompt_tokens = sum(len(_text_of(m.get("content"))) for m in body["messages"]) // 4
        out_tokens = max(1, len(text) // 4)
        return 200, {"id": f"mock-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}", "object": "chat.completion",
                     "created": int(time.time()), "model": body.get("model") or next(iter(self.models), "mock"),
                     "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": out_tokens,
                               "total_tokens": prompt_tokens + out_tokens}}

    def _messages(self, body: dict, flags: dict):
        msgs = body.get("messages")
        if not isinstance(msgs, list):
            raise Unanswerable("no messages")
        system = body.get("system")
        chat = ([{"role": "system", "content": _text_of(system)}] if system else []) + msgs
        text = self.answer(chat, body.get("temperature", 0.0), None, body.get("max_tokens", 8000),
                           canary_fail=flags["canary_fail"])
        if self._malformed(flags, chat):
            return 200, _MALFORMED
        blocks = []
        if flags["thinking"]:
            blocks.append({"type": "thinking", "thinking": "let me think about this first", "signature": "mock"})
        blocks.append({"type": "text", "text": text})
        in_tokens = sum(len(_text_of(m.get("content"))) for m in chat) // 4
        return 200, {"id": f"msg_mock{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}", "type": "message",
                     "role": "assistant", "model": body.get("model") or "mock", "content": blocks,
                     "stop_reason": "end_turn", "stop_sequence": None,
                     "usage": {"input_tokens": in_tokens, "output_tokens": max(1, len(text) // 4)}}

    def _v1_models(self, body: dict, flags: dict):
        return 200, {"object": "list", "data": [{"id": n, "object": "model", "created": 0, "owned_by": "mock"}
                                                 for n in self.models]}

    def _details(self, name: str) -> dict:
        fam = self.models[name]["family"]
        return {"parent_model": "", "format": "gguf", "family": fam, "families": [fam],
                "parameter_size": "7B", "quantization_level": "Q4_K_M"}

    def _tags(self, body: dict, flags: dict):
        out = []
        for name, spec in self.models.items():
            entry = {"name": name, "model": name, "modified_at": _MODIFIED, "size": 4_000_000_000,
                     "digest": spec["digest"], "details": self._details(name)}
            if (host := self._remote_host(name)):
                entry["remote_host"], entry["remote_model"] = host, name
            out.append(entry)
        return 200, {"models": out}

    def _show(self, body: dict, flags: dict):
        name = body.get("model", body.get("name"))
        if name not in self.models:
            return 404, {"error": f"model '{name}' not found"}
        spec = self.models[name]
        out = {"license": "", "modelfile": "", "parameters": "", "template": "", "modified_at": _MODIFIED,
               "digest": spec["digest"], "details": self._details(name),
               "model_info": {"general.architecture": spec["family"], "general.parameter_count": 7_000_000_000,
                              "general.file_type": 15},
               "capabilities": ["completion"]}
        if (host := self._remote_host(name)):
            out["remote_host"], out["remote_model"] = host, name
        return 200, out

    def _generate(self, body: dict, flags: dict):
        name = body.get("model", body.get("name"))
        if name not in self.models:
            return 404, {"error": f"model '{name}' not found"}
        base = {"model": name, "created_at": _MODIFIED}
        prompt = body.get("prompt")
        if prompt:
            text = self.answer([{"role": "user", "content": prompt}], canary_fail=flags["canary_fail"])
            if self._malformed(flags, [{"role": "user", "content": prompt}]):
                return 200, _MALFORMED
            self.loaded.add(name)
            return 200, {**base, "response": text, "done": True, "done_reason": "stop"}
        if flags["malformed"]:
            return 200, _MALFORMED
        if _is_zero(body.get("keep_alive")) and flags["unload_ok"]:
            self.loaded.discard(name)
            return 200, {**base, "response": "", "done": True, "done_reason": "unload"}
        self.loaded.add(name)           # a load, or an unload the server ignores
        return 200, {**base, "response": "", "done": True, "done_reason": "load"}

    def _lmstudio_v1(self, body: dict, flags: dict):
        return 200, {"models": [{"type": "llm", "publisher": "mock", "key": n, "display_name": n,
                                 "architecture": s["family"], "quantization": {"name": "Q4_K_M", "bits_per_weight": 4},
                                 "size_bytes": 4_000_000_000, "params_string": "7B", "loaded_instances": [],
                                 "max_context_length": 32768, "format": "gguf"}
                                for n, s in self.models.items()]}

    def _lmstudio_v0(self, body: dict, flags: dict):
        return 200, {"object": "list", "data": [{"id": n, "object": "model", "type": "llm", "publisher": "mock",
                                                 "arch": s["family"], "compatibility_type": "gguf",
                                                 "quantization": "Q4_K_M", "state": "not-loaded",
                                                 "max_context_length": 32768} for n, s in self.models.items()]}

    routes = {
        ("POST", "/v1/chat/completions"): lambda s, b, f: s._chat(b, f),
        ("POST", "/v1/messages"): lambda s, b, f: s._messages(b, f),
        ("GET", "/v1/models"): lambda s, b, f: s._v1_models(b, f),
        ("GET", "/api/tags"): lambda s, b, f: s._tags(b, f),
        ("POST", "/api/show"): lambda s, b, f: s._show(b, f),
        ("POST", "/api/generate"): lambda s, b, f: s._generate(b, f),
        ("GET", "/api/v1/models"): lambda s, b, f: s._lmstudio_v1(b, f),
        ("GET", "/api/v0/models"): lambda s, b, f: s._lmstudio_v0(b, f),
    }


def _is_zero(keep_alive) -> bool:
    """Ollama's keep_alive 0: the number 0, or a duration string that is zero ("0", "0s", "0m")."""
    if isinstance(keep_alive, bool):
        return False
    if isinstance(keep_alive, (int, float)):
        return keep_alive == 0
    if isinstance(keep_alive, str):
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:ns|us|µs|ms|s|m|h)?\s*", keep_alive)
        return bool(m) and float(m.group(1)) == 0
    return False
