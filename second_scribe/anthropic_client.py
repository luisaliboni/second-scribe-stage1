"""K12: a native client for Anthropic's Messages API, with the same ``complete`` contract as ``llm.HTTPClient``.

Tool configs only: a remote endpoint is reached only when the guard finds it in ``tool.remote_endpoints`` (K13,
``llm.guard_real_clients``). The study never builds this client (``SS_<P>_PROVIDER`` unset means ``"openai"``).

Request: ``POST {endpoint}/messages`` (endpoint e.g. ``https://api.anthropic.com/v1``) with the headers ``x-api-key``
(the value of the env var named by ``ModelConfig.api_key_env``, read at request time and never stored, logged or put
in a message), ``anthropic-version`` and ``content-type: application/json``. Body: ``{model, max_tokens, temperature,
system?, messages}``. The kit's system messages are hoisted into ``system``; the Messages API has no ``seed`` (it is
recorded by ``log_call`` as asked, and not sent) and no extra body fields, so a config or call that sets ``params``
is refused rather than logged as sent and silently dropped.

Reply: the text is the ``text`` blocks of ``content`` joined; a ``thinking`` or ``redacted_thinking`` block raises
``ThinkingDetected``. ``stop_reason`` ``max_tokens`` becomes the kit's ``finish_reason`` ``"length"`` (so
``log_call``'s truncation guard sees it), ``end_turn`` / ``stop_sequence`` become ``"stop"``, anything else is passed
through. Usage keeps Anthropic's fields and adds the kit's ``prompt_tokens`` / ``completion_tokens``.

Errors: as ``HTTPClient``. Transport errors and HTTP 429/5xx (Anthropic's 529 "overloaded" included) are retried
with backoff and end in ``TransportFailure``; any other 4xx and a malformed reply raise ``RuntimeError`` at once.
Redirects and proxies are refused (the kit's one opener). Every error string is scrubbed of the key.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .llm import (_OPENER, ModelConfig, ModelResponse, ThinkingDetected, TransportFailure, _scrub, _sha256_text,
                  safe_endpoint)

# Copied from Anthropic's API docs on 2026-10-18, https://platform.claude.com/docs/en/api/versioning
# (docs.claude.com/en/api/versioning redirects there): "you must send an anthropic-version request header. For
# example, anthropic-version: 2023-06-01"; the version history lists 2023-06-01 as the latest version.
ANTHROPIC_VERSION = "2023-06-01"

THINKING_BLOCKS = ("thinking", "redacted_thinking")
_STOP = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}


class ParamsNotSupported(ValueError):
    """The Messages body is fixed; extra body fields (``params``) cannot be sent. ``fatal``: ``log_call`` re-raises."""
    fatal = True


def messages_body(model: str, messages: list[dict], *, temperature: float, max_tokens: int) -> dict:
    """The Messages API body for the kit's chat ``messages``: every ``system`` message is hoisted into ``system``
    (joined by a blank line, in order); the other messages keep their order, role and content."""
    system = [m.get("content") or "" for m in messages if m.get("role") == "system"]
    rest = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("role") != "system"]
    body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "temperature": temperature, "messages": rest}
    if system:
        body["system"] = "\n\n".join(system)
    return body


class AnthropicClient:
    """Anthropic Messages API over stdlib urllib, with retries; see the module docstring."""

    def __init__(self, cfg: ModelConfig, attempts: int = 4, backoff=(2, 30), sleep=time.sleep):
        if cfg.params:
            raise ParamsNotSupported("the anthropic provider sends no extra body fields; leave SS_<P>_PARAMS unset")
        self.cfg = cfg
        self.attempts = attempts
        self.backoff = backoff
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"AnthropicClient({self.cfg!r})"

    def _request(self, body: dict) -> urllib.request.Request:
        headers = {"Content-Type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
        key_env = self.cfg.api_key_env
        key = os.environ.get(key_env) if key_env else None
        if key:
            headers["x-api-key"] = key            # header only, never logged
        url = self.cfg.endpoint.rstrip("/") + "/messages"
        return urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")

    def complete(self, messages: list[dict], *, temperature: float, seed: int | None,
                 max_tokens: int, params: dict | None = None) -> ModelResponse:
        if params:
            raise ParamsNotSupported("the anthropic provider cannot send per-call params (e.g. logprobs)")
        body = messages_body(self.cfg.model_id, messages, temperature=temperature, max_tokens=max_tokens)
        where = safe_endpoint(self.cfg.endpoint)

        def clean(text: str) -> str:
            return _scrub(text, self.cfg.api_key_env)
        last = "no attempt made"
        for k in range(self.attempts):
            if k:
                self._sleep(min(self.backoff[1], self.backoff[0] * 2 ** (k - 1)))
            t0 = time.monotonic()
            try:
                with _OPENER.open(self._request(body), timeout=self.cfg.timeout_s) as resp:
                    raw = resp.read()
            except urllib.error.HTTPError as exc:
                detail = clean(exc.read(4096).decode("utf-8", "replace"))[:300]
                exc.close()
                if exc.code == 429 or exc.code >= 500:
                    last = f"HTTP {exc.code}"
                    continue
                raise RuntimeError(f"model call to {where} failed: HTTP {exc.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                last = clean(f"{type(exc).__name__}: {exc}")
                continue
            latency_ms = int((time.monotonic() - t0) * 1000)
            try:
                data = json.loads(raw.decode("utf-8"))
                blocks = data["content"]
                if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
                    raise TypeError("content is not a list of blocks")
                text = "".join(b["text"] for b in blocks if b.get("type") == "text")
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"model call to {where} returned a malformed response "
                                   f"({type(exc).__name__})") from None
            seen = [b for b in blocks if b.get("type") in THINKING_BLOCKS]
            if seen:
                blob = json.dumps(seen, sort_keys=True, ensure_ascii=False)
                raise ThinkingDetected(f"reply from {where} carries thinking output: {seen[0]['type']} block "
                                       f"({len(blob)} chars, sha256 {_sha256_text(blob)[:16]}); thinking must be off")
            usage = dict(data.get("usage") or {})
            if isinstance(usage.get("input_tokens"), int):
                usage.setdefault("prompt_tokens", usage["input_tokens"])
            if isinstance(usage.get("output_tokens"), int):
                usage.setdefault("completion_tokens", usage["output_tokens"])
            stop = data.get("stop_reason")
            return ModelResponse(text=text, finish_reason=_STOP.get(stop, stop), usage=usage, latency_ms=latency_ms,
                                 raw_sha256=hashlib.sha256(raw).hexdigest(), logprobs=None)
        raise TransportFailure(f"model call to {where} failed after {self.attempts} attempts: {last}")
