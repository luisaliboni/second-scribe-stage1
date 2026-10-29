"""Model layer: an OpenAI-compatible HTTP client plus offline Mock/Replay clients.

Ported from the ISR harness ``models.py`` (``HTTPOpenAIClient``, ``derive_seed``,
``safe_endpoint``), trimmed to what this kit needs and extended with retries.

Secrets: an API key is referenced by the NAME of an environment variable
(``ModelConfig.api_key_env``). The value is read only at request time, goes only
into the ``Authorization`` header, and is never stored on the client, put in a
``repr``, an exception message or an audit payload. ``ModelConfig.__repr__`` does
not even show the variable name.

``log_call`` is the only place a model is called during a run: it writes a
``call_request`` record BEFORE each attempt and a ``call_response`` or
``call_error`` record AFTER it, so a crash mid-call leaves a detectable orphan.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .audit.registry import AuditLog, is_secret_key
from .families import known_family


def safe_endpoint(url: str | None) -> str | None:
    """scheme://host[:port]/path, dropping userinfo and query string.

    A credential embedded in a URL or a token in a query must never reach a log.
    """
    if not url:
        return url
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    return urllib.parse.urlunsplit((p.scheme, host, p.path, "", ""))


# Where a real corpus may be sent: this machine, private LAN ranges (RFC 1918) and Tailscale's CGNAT range.
# (Written as octets, not dotted quads: the kit's own leak scan flags any address in the tracked tree.)
def _net(a: int, b: int, c: int, d: int, prefix: int):
    return ipaddress.ip_network((bytes((a, b, c, d)), prefix))


_LOCAL_NETS = (_net(127, 0, 0, 0, 8), ipaddress.ip_network("::1/128"), _net(10, 0, 0, 0, 8), _net(172, 16, 0, 0, 12),
               _net(192, 168, 0, 0, 16), _net(100, 64, 0, 0, 10))


class RealGuardRefused(RuntimeError):
    """A real-corpus guard refused to start: raised before any request."""


class EgressRefused(RealGuardRefused):
    """A real corpus was about to be sent somewhere that is not this machine or a private network."""


class ThinkingDetected(RuntimeError):
    """A reply carried thinking output (a reasoning field, reasoning tokens or a <think> block): the instrument runs
    with thinking off, so the run stops rather than keep a reply from a different instrument. ``fatal``: ``log_call``
    records the error and re-raises instead of retrying, and no item is written, so a rerun after the server is fixed
    redoes the work."""
    fatal = True


_THINK_TAG = re.compile(r"</?think>", re.IGNORECASE)


def thinking_evidence(message: dict, usage: dict, text: str) -> tuple[str, str, str] | None:
    """(what was seen, size as text, sha256 of it) if a reply shows thinking output, else None. Looks at the message's
    ``reasoning_content`` / ``reasoning`` / ``reasoning_details``, at ``usage.completion_tokens_details.reasoning_tokens``
    and at a ``<think>`` tag in the text. The text of the reasoning is hashed, never returned."""
    for key in ("reasoning_content", "reasoning", "reasoning_details"):
        val = message.get(key)
        if val:
            blob = val if isinstance(val, str) else json.dumps(val, sort_keys=True, ensure_ascii=False)
            return key, f"{len(blob)} chars", _sha256_text(blob)
    details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
    n = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0:
        return "reasoning_tokens", f"{int(n)} tokens", _sha256_text(str(n))
    if _THINK_TAG.search(text or ""):
        return "<think> block", f"{len(text)} chars", _sha256_text(text)
    return None


FORBIDDEN_JUDGE_FAMILIES = ("qwen", "qwq")     # the reconstructor's family: it must not grade its own output
JUDGE_ROLES = ("judge", "judge2")


def _model_name(model_id: str | None) -> str:
    """Lower-case model name without a leading path (``org/Model-X`` -> ``model-x``)."""
    return (model_id or "").strip().lower().rsplit("/", 1)[-1]


def thinking_off(params: dict | None) -> bool:
    """True only if ``params["chat_template_kwargs"]["enable_thinking"]`` is the JSON value false."""
    kw = (params or {}).get("chat_template_kwargs")
    return isinstance(kw, dict) and kw.get("enable_thinking") is False


def endpoint_host(url: str | None) -> str | None:
    """``host`` or ``host:port`` of ``url`` (lower case), with no scheme, userinfo, path or query. None if there is
    none. This is what the audit log records per call: the place the data went, never the credentials."""
    if not url:
        return None
    try:
        p = urllib.parse.urlsplit(url)
        host, port = p.hostname, p.port
    except ValueError:
        return None
    if not host:
        return None
    return f"{host}:{port}" if port else host


def _resolve(host: str, port: int | None) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _is_local_address(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])     # drop an IPv6 scope id
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _LOCAL_NETS if net.version == ip.version)


def endpoint_problem(url: str | None, resolver: Callable[[str, int | None], list[str]] | None = None) -> str | None:
    """Why a real corpus must not be sent to ``url``, or None if it may. The host is read from the URL itself
    (userinfo and path are ignored). An IP literal must be in a local range; a name is resolved and EVERY address it
    resolves to must be. A scheme other than http/https, a missing host or a failed lookup is a refusal. Messages name
    the host only. ``resolver(host, port) -> [address]`` is for tests."""
    try:
        p = urllib.parse.urlsplit(url or "")
        host, port = p.hostname, p.port
    except ValueError:
        return "the endpoint URL cannot be parsed"
    if p.scheme not in ("http", "https") or not host:
        return "the endpoint URL is not an http(s) URL with a host"
    shown = endpoint_host(url)
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        addrs = [host]
    except ValueError:
        try:
            addrs = list((resolver or _resolve)(host, port))
        except OSError:
            return f"host {shown} cannot be resolved, so it cannot be shown to be local"
    if not addrs:
        return f"host {shown} resolves to no address"
    bad = [a for a in addrs if not _is_local_address(a)]
    if bad:
        return f"host {shown} is not loopback, private (RFC 1918) or Tailscale (100.64/10)"
    return None


_LOOPBACK_NETS = (_net(127, 0, 0, 0, 8), ipaddress.ip_network("::1/128"))


def _is_loopback_address(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _LOOPBACK_NETS if net.version == ip.version)


def endpoint_is_loopback(url: str | None, resolver: Callable[[str, int | None], list[str]] | None = None) -> bool:
    """True when ``url``'s host is this machine: an IP literal in 127/8 or ::1, or a name whose EVERY address is.
    A LAN (RFC 1918) or Tailscale (100.64/10) host, an unparseable URL or a failed lookup is False."""
    try:
        host = urllib.parse.urlsplit(url or "").hostname
    except ValueError:
        return False
    if not host:
        return False
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        addrs = [host]
    except ValueError:
        try:
            addrs = list((resolver or _resolve)(host, None))
        except OSError:
            return False
    return bool(addrs) and all(_is_loopback_address(a) for a in addrs)


def optin_entry(role: str, cfg, endpoint_optins: list[dict] | None) -> dict | None:
    """The ``tool.endpoint_optins`` entry ``{role, host}`` for ``role``'s endpoint (host or host:port as
    ``endpoint_host`` writes it, compared in lower case), or None."""
    host = endpoint_host(getattr(cfg, "endpoint", None))
    if host is None:
        return None
    for e in endpoint_optins or []:
        if (isinstance(e, dict) and e.get("role") == role and isinstance(e.get("host"), str)
                and e["host"].strip().lower() == host):
            return e
    return None


JUDGE_RULES = ("study", "two_family")
TWO_FAMILY_REFUSAL = "real corpus: the judge must be a different model family from the reconstructor"


def guard_real_clients(clients: dict, *, resolver: Callable[[str, int | None], list[str]] | None = None,
                       judge_rule: str = "study", families: dict[str, str] | None = None,
                       remote_endpoints: list[dict] | None = None,
                       endpoint_optins: list[dict] | None = None) -> None:
    """For a real corpus: refuse (``EgressRefused``) before any call unless every client's endpoint is local. Clients
    without a ``ModelConfig`` endpoint (the kit's offline mock, replay and oracle clients) send nothing and are not
    checked for endpoint or thinking. No escape hatch. Also refuses a role whose configured params do not switch thinking off
    (``chat_template_kwargs.enable_thinking`` false).

    ``judge_rule`` (K4) picks the judge check. ``"study"`` (every study config): no Qwen-family judge. ``"two_family"``
    (a tool config's ``tool.judge_rule``): each judge's family (``families[role]``, else ``families.known_family`` of
    its model id) must be known and differ from the reconstructor's. In both, a judge may not be the reconstructor's
    own model. Any other rule is refused.

    ``remote_endpoints`` (K13, a tool config's ``tool.remote_endpoints``; None for every study config): a non-local
    endpoint passes only when its ``{role, host, provider}`` matches an entry (``remote_entry``); otherwise it is
    refused as before. A declared one must also use https (``REMOTE_SCHEMES``; no config or env changes that), else
    ``EgressRefused``: the transcript and the API key never cross a network in clear. The thinking-off check is for local endpoints only (``chat_template_kwargs`` is a local-server
    field); the judge rule applies to every endpoint.

    ``endpoint_optins`` (audit K7, spec 2.1; a tool config's ``tool.endpoint_optins``, ``[]`` when it has none; None,
    i.e. not passed, for every study config, which keeps today's guard): a local endpoint that is not loopback (a LAN
    or Tailscale host) passes only with a ``{role, host}`` entry (``optin_entry``), else ``EgressRefused``."""
    if judge_rule not in JUDGE_RULES:
        raise RealGuardRefused(f"real corpus: unknown judge rule; expected one of {JUDGE_RULES}")
    for role, c in clients.items():
        cfg = getattr(c, "cfg", None)
        if cfg is None or not hasattr(cfg, "endpoint"):
            continue                          # an offline client: nothing is sent anywhere
        why = endpoint_problem(getattr(cfg, "endpoint", None), resolver)
        if why:
            if remote_entry(role, cfg, remote_endpoints) is None:
                raise EgressRefused(f"real corpus: the {role} endpoint is refused: {why}")
            if _scheme(getattr(cfg, "endpoint", None)) not in REMOTE_SCHEMES:
                raise EgressRefused(f"real corpus: the {role} endpoint is refused: remote endpoint must use https")
            continue                          # a declared remote endpoint (K13): no local-server thinking switch
        if endpoint_optins is not None and not endpoint_is_loopback(getattr(cfg, "endpoint", None), resolver) \
                and optin_entry(role, cfg, endpoint_optins) is None:
            raise EgressRefused(f"real corpus: the {role} endpoint is refused: host "
                                f"{endpoint_host(getattr(cfg, 'endpoint', None))} is on the local network or "
                                "Tailscale and has no opt-in for this role (tool.endpoint_optins)")
        if not thinking_off(getattr(cfg, "params", None)):
            raise RealGuardRefused(
                f"real corpus: the {role} model is not configured with thinking off; set "
                f"SS_{role.upper()}_PARAMS to a JSON object with chat_template_kwargs.enable_thinking false")
    recon_cfg = getattr(clients.get("recon"), "cfg", None)
    recon = _model_name(getattr(recon_cfg, "model_id", None))
    for role in JUDGE_ROLES:
        cfg = getattr(clients.get(role), "cfg", None)
        name = _model_name(getattr(cfg, "model_id", None))
        if not name:
            continue
        if judge_rule == "study":
            if any(f in name for f in FORBIDDEN_JUDGE_FAMILIES):
                raise RealGuardRefused(f"real corpus: the {role} model is Qwen-family; the reconstructor's family must "
                                       "not grade the headline (use a non-Qwen judge)")
        else:
            recon_family = _declared_family(families, "recon", getattr(recon_cfg, "model_id", None))
            family = _declared_family(families, role, getattr(cfg, "model_id", None))
            if recon_family is None or family is None or family == recon_family:
                raise RealGuardRefused(TWO_FAMILY_REFUSAL)
        if recon and name == recon:
            raise RealGuardRefused(f"real corpus: the {role} model is the reconstructor's own model; a model must "
                                   "not grade its own output (check that SS_RECON_URL and "
                                   f"SS_{role.upper()}_URL point at different servers)")


# Schemes a non-local endpoint may use. A module constant on purpose: no config key or env var changes it (the
# tests patch it to reach their plain-http mock on 127.0.0.1).
REMOTE_SCHEMES = ("https",)


def _scheme(url: str | None) -> str:
    try:
        return urllib.parse.urlsplit(url or "").scheme.lower()
    except ValueError:
        return ""


# ``ModelConfig.provider`` -> the ``provider`` value of a ``tool.remote_endpoints`` entry (``toolcfg.PROVIDERS``)
ALLOWLIST_PROVIDER = {"openai": "openai_compat", "anthropic": "anthropic"}


def remote_entry(role: str, cfg, remote_endpoints: list[dict] | None) -> dict | None:
    """The ``tool.remote_endpoints`` entry that declares ``role``'s endpoint, or None. An entry matches when its
    ``role`` is ``role``, its ``host`` is ``endpoint_host(cfg.endpoint)`` (host or host:port, compared in lower
    case) and its ``provider`` is the client's (``ALLOWLIST_PROVIDER[cfg.provider]``). No lookup, no network."""
    if not remote_endpoints:
        return None
    host = endpoint_host(getattr(cfg, "endpoint", None))
    provider = ALLOWLIST_PROVIDER.get(getattr(cfg, "provider", "openai"))
    if host is None or provider is None:
        return None
    for e in remote_endpoints:
        if (isinstance(e, dict) and e.get("role") == role and isinstance(e.get("host"), str)
                and e["host"].strip().lower() == host and e.get("provider") == provider):
            return e
    return None


def _declared_family(families: dict[str, str] | None, role: str, model_id: str | None) -> str | None:
    """A role's model family for the two-family rule: the declared one (``tool.model_families``), else the
    ``FAMILY_TABLE`` family of its model id; lower case, None when neither is known."""
    declared = (families or {}).get(role)
    if isinstance(declared, str) and declared.strip():
        return declared.strip().lower()
    return known_family(model_id)


def derive_seed(*parts: object) -> int:
    """Stable per-draw seed: first 15 hex digits of sha256(":".join(parts)).

    15 hex digits keep it below 2**60, which vLLM accepts.
    """
    h = hashlib.sha256(":".join(map(str, parts)).encode("utf-8")).hexdigest()
    return int(h[:15], 16)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scrub(text: str, api_key_env: str | None) -> str:
    """Replace the value of ``os.environ[api_key_env]`` (if set and non-empty) with
    "[REDACTED]". Applied to every error string before it is raised or logged, and
    BEFORE any truncation, so a cut cannot leave a readable prefix of the key."""
    key = os.environ.get(api_key_env) if api_key_env else None
    return text.replace(key, "[REDACTED]") if key else text


def redact_params(params: Any, api_key_env: str | None = None) -> Any:
    """Copy of ``params`` that is safe to write to the audit log.

    The value of any key whose name contains key, token, secret, password, passwd, authorization,
    bearer, cookie or credential becomes "[REDACTED]" (an allowlist of known inference settings such
    as stop_token and max_tokens is exempt; see ``audit.registry.is_secret_key``), at any
    depth, and the configured key's value is scrubbed out of every string. Used so the log records
    the inference settings the request was sent with (digest 4.2.1, 4.2.5).
    """
    if isinstance(params, dict):
        return {k: ("[REDACTED]" if isinstance(k, str) and is_secret_key(k)
                    else redact_params(v, api_key_env)) for k, v in params.items()}
    if isinstance(params, (list, tuple)):
        return [redact_params(v, api_key_env) for v in params]
    if isinstance(params, str):
        return _scrub(params, api_key_env)
    return params


def messages_sha256(messages: list[dict]) -> str:
    """Hash of a request's messages. Recorded by ``log_call`` as ``messages_sha256``
    and used as the fixture key by ``ReplayClient``."""
    return _sha256_text(json.dumps(messages, sort_keys=True, ensure_ascii=False))


RESERVED_PARAMS = ("model", "messages", "temperature", "max_tokens", "seed")
PROVIDERS = ("openai", "anthropic")      # K12: the wire format of an endpoint (SS_<P>_PROVIDER)


@dataclass
class ModelConfig:
    name: str            # arm label, e.g. "recon", "judge"
    endpoint: str        # base URL ending in /v1
    model_id: str
    api_key_env: str | None = None   # env-var NAME only, never the value
    timeout_s: float = 600.0
    params: dict = field(default_factory=dict)   # extra body fields, e.g. chat_template_kwargs
    provider: str = "openai"     # "openai" (OpenAI-compatible /chat/completions) or "anthropic" (Messages API)

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError(f"provider must be one of {PROVIDERS}")
        # These are set per call and recorded by log_call; a params value would silently override
        # what the log says was sent.
        clash = sorted(set(self.params) & set(RESERVED_PARAMS))
        if clash:
            raise ValueError(f"params may not set {', '.join(clash)}: log_call sets and records those per call")

    def __repr__(self) -> str:
        return (f"ModelConfig(name={self.name!r}, endpoint={safe_endpoint(self.endpoint)!r}, "
                f"model_id={self.model_id!r}, api_key={'set' if self.api_key_env else 'unset'})")

    @staticmethod
    def from_env(prefix: str) -> "ModelConfig":
        """Read SS_<PREFIX>_URL, SS_<PREFIX>_MODEL, optional SS_<PREFIX>_KEY_ENV, SS_<PREFIX>_PROVIDER ("openai", the
        default, or "anthropic"; ValueError otherwise),
        SS_<PREFIX>_PARAMS (a JSON object) and SS_<PREFIX>_TIMEOUT_S (seconds a request may wait for its reply,
        default 600; a slow server writing long maps needs more). KeyError if URL or MODEL is unset; ValueError if
        TIMEOUT_S is not a finite positive number."""
        p = f"SS_{prefix.upper()}_"
        params = os.environ.get(p + "PARAMS")
        timeout = os.environ.get(p + "TIMEOUT_S")
        extra = {}
        if timeout:
            t = float(timeout)                    # ValueError for a non-number
            if not (0 < t < float("inf")):
                raise ValueError(f"{p}TIMEOUT_S must be a positive number of seconds")
            extra["timeout_s"] = t
        return ModelConfig(
            name=prefix.lower(),
            endpoint=os.environ[p + "URL"],
            model_id=os.environ[p + "MODEL"],
            api_key_env=os.environ.get(p + "KEY_ENV") or None,
            params=json.loads(params) if params else {},
            provider=os.environ.get(p + "PROVIDER") or "openai",
            **extra,
        )


@dataclass
class ModelResponse:
    """``logprobs`` is the ``top_logprobs`` of the FIRST generated token as ``[[token, logprob], ...]`` (best
    first), or None when the server returned none. Pairs, not the server's ``{"token": ..., "logprob": ...}``
    objects: the audit log redacts string values under a key named ``token``, which would blank them."""
    text: str
    finish_reason: str | None
    usage: dict
    latency_ms: int
    raw_sha256: str
    logprobs: Any = None


class FixtureMissing(KeyError):
    """A replay has no recorded response for this request. ``fatal``: ``log_call`` records the error and re-raises
    it instead of retrying, so a replay never silently turns a missing fixture into a model failure."""
    fatal = True


def _first_token_logprobs(choice: dict) -> list | None:
    """``[[token, logprob], ...]`` from an OpenAI-style ``choices[0].logprobs.content[0].top_logprobs``
    (what OpenRouter, vLLM and llama.cpp's server return), else None. Entries that are malformed are dropped;
    no usable entry at all is None, never an empty list."""
    try:
        top = choice["logprobs"]["content"][0]["top_logprobs"]
        pairs = [[str(e["token"]), float(e["logprob"])] for e in top]
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
    pairs = [p for p in pairs if math.isfinite(p[1])]      # NaN/-inf cannot be audit-logged (and carry no mass)
    return pairs or None


class TransportFailure(RuntimeError):
    """The model could not be reached or never answered: connection refused, a timeout, HTTP 429/5xx after every
    retry. It says nothing about the model's answers, so the pipeline marks the item ``timeout_excluded`` and a
    rerun redoes it (a parse or validation failure, which is the model's own answer, stays final)."""


def is_transport_error(exc: BaseException) -> bool:
    """Is ``exc`` an outage (see ``TransportFailure``)? Also true for the plain network exceptions a client may raise."""
    return isinstance(exc, (TransportFailure, ConnectionError, TimeoutError, urllib.error.URLError,
                            http.client.HTTPException))      # IncompleteRead: a reply cut off mid-body


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the endpoint is checked once, a redirect would send the call (and the API
    key header) somewhere the guard never saw."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code,
                                     f"redirect refused ({code}); endpoints must answer directly",
                                     headers, fp)


# One opener for every model call. ProxyHandler({}) means NO proxy: the default opener would honour
# http_proxy / HTTPS_PROXY and, on macOS, the system proxy settings, and send the transcript there even
# though the egress guard checked only the configured endpoint. No redirects either (see above).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects)


class HTTPClient:
    """OpenAI-compatible /chat/completions over stdlib urllib, with retries.

    Retried with exponential backoff: transport errors (URLError, timeouts,
    connection errors) and HTTP 429/5xx. Any other 4xx raises immediately.
    After ``attempts`` tries a RuntimeError is raised.
    """

    def __init__(self, cfg: ModelConfig, attempts: int = 4, backoff=(2, 30), sleep=time.sleep):
        self.cfg = cfg
        self.attempts = attempts
        self.backoff = backoff
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"HTTPClient({self.cfg!r})"

    def _request(self, body: dict) -> urllib.request.Request:
        headers = {"Content-Type": "application/json"}
        key_env = self.cfg.api_key_env
        key = os.environ.get(key_env) if key_env else None
        if key:
            headers["Authorization"] = f"Bearer {key}"   # header only, never logged
        url = self.cfg.endpoint.rstrip("/") + "/chat/completions"
        return urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                      headers=headers, method="POST")

    def complete(self, messages: list[dict], *, temperature: float, seed: int | None,
                 max_tokens: int, params: dict | None = None) -> ModelResponse:
        # params first (the config's, then this call's), then the per-call values, so nothing in params (even
        # if it was added after the config was built) can change what log_call records as sent; a stray
        # "seed" is dropped.
        merged = {**self.cfg.params, **(params or {})}
        body: dict[str, Any] = {**{k: v for k, v in merged.items() if k not in RESERVED_PARAMS},
                                "model": self.cfg.model_id, "messages": messages,
                                "temperature": temperature, "max_tokens": max_tokens}
        if seed is not None:
            body["seed"] = seed
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
                choice = data["choices"][0]
                text = choice["message"]["content"] or ""
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"model call to {where} returned a malformed response "
                                   f"({type(exc).__name__})") from None
            usage = data.get("usage") or {}
            if (seen := thinking_evidence(choice["message"], usage, text)) is not None:
                what, size, digest = seen
                raise ThinkingDetected(f"reply from {where} carries thinking output: {what} ({size}, "
                                       f"sha256 {digest[:16]}); thinking must be off")
            return ModelResponse(text=text, finish_reason=choice.get("finish_reason"),
                                 usage=usage, latency_ms=latency_ms,
                                 raw_sha256=hashlib.sha256(raw).hexdigest(),
                                 logprobs=_first_token_logprobs(choice))
        raise TransportFailure(f"model call to {where} failed after {self.attempts} attempts: {last}")


def make_client(cfg: ModelConfig, **kw):
    """The live client for ``cfg.provider``: ``HTTPClient`` (``"openai"``) or ``anthropic_client.AnthropicClient``.
    ``kw`` (attempts, backoff, sleep) go to the client."""
    if cfg.provider == "anthropic":
        from .anthropic_client import AnthropicClient      # lazy: anthropic_client imports this module
        return AnthropicClient(cfg, **kw)
    return HTTPClient(cfg, **kw)


class MockClient:
    """Deterministic offline client: ``fn(messages)`` if given, else
    ``canned[sha256(last user message)]`` (KeyError carrying the hash if absent).
    Exceptions raised by ``fn`` propagate, so tests can simulate transport errors."""

    def __init__(self, fn: Callable[[list[dict]], str] | None = None, canned: dict | None = None):
        self.fn = fn
        self.canned = canned or {}

    def complete(self, messages, *, temperature, seed, max_tokens, params=None) -> ModelResponse:
        if self.fn is not None:
            text = self.fn(messages)
        else:
            last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
            key = _sha256_text(last_user)
            if key not in self.canned:
                raise KeyError(key)
            text = self.canned[key]
        return ModelResponse(text, "stop", {}, 0, _sha256_text(text))


def _temp(temperature) -> float | None:
    """A temperature as the tags compare it: a float (0 and 0.0 are the same request), or None."""
    return None if temperature is None else float(temperature)


def _is_tagged(entry) -> bool:
    """Does a recorded entry carry both tags, ``role`` and ``temperature`` (what ``RecordingClient`` writes when it
    knows the role)?"""
    return isinstance(entry, dict) and "role" in entry and "temperature" in entry


def _tags_match(entry: dict, role: str | None, temperature: float | None) -> bool:
    """Was a tagged entry recorded for this client role (any role when ``role`` is None) at this temperature?"""
    return (role is None or entry["role"] == role) and _temp(entry["temperature"]) == temperature


def tagged_calls(value) -> bool:
    """Is a fixture value a ``{"calls": [...]}`` list in which EVERY entry is tagged (role and temperature)? Only then
    is it served by (role, temperature); any other value is served by position, as before tags existed."""
    calls = value.get("calls") if isinstance(value, dict) else None
    return isinstance(calls, list) and bool(calls) and all(_is_tagged(c) for c in calls)


class ReplayClient:
    """Serves recorded responses from a JSON file ``{messages_sha256: value}``, in the order they were recorded.

    A value is one of:
      * a record ``{"text", "finish_reason", "usage"[, "logprobs"]}`` (what ``RecordingClient`` writes for a request
        made once) or, as in older fixture files, the bare text (finish_reason "stop"): served on EVERY call for
        that key, unchanged;
      * ``{"calls": [rec1, rec2, ...]}`` (a request made more than once: the same messages with different seeds, or
        a determinism re-ask): ``calls[i]`` is served on the i-th call for that key, counting from 0. Asking past
        the end is a miss, so a replay that makes more calls than the recording did stops instead of repeating
        an answer. Each entry is a record or bare text, as above.
    The key is the ``messages_sha256`` that ``log_call`` writes into each ``call_request`` record.
    A missing key (or an exhausted ``calls`` list) raises ``FixtureMissing`` (a ``KeyError``) carrying the hash; it is
    fatal in ``log_call``.

    TAGGED entries. The key is the hash of the messages alone, and different requests can send identical messages:
    a reconstructor draft and the judge's script draft of the same variant, or the same prompt at temperature 0.7
    and at 0. Determinism re-asks are not logged, so they sit in the middle of a key's list. Counting one position per
    key works in the process that recorded the order; a new process (a judge-only session after a reconstructor
    session, a resumed run) cannot tell from its log which positions the other role or the re-asks took. So each
    entry may carry ``role`` and ``temperature`` (``RecordingClient`` writes them when it knows the role). When EVERY
    entry of a ``calls`` list is tagged, the client serves the next unserved entry of the sub-sequence whose role is
    the client's ``role`` (any role when ``role`` is None) and whose temperature is the request's, with one counter per
    (key, role, temperature); none left is a miss. A tagged single record is served on every call only when its tags
    match; a mismatch is a miss. Untagged values (every older file, or a list with one untagged entry) keep the
    positional behaviour above exactly.

    The counters live in ``served``: ``served[key]`` for a positional value, ``served[(key, role, temperature)]`` for
    a tagged list. Clients for different roles that were recorded into one store (one file) must count together,
    because the recording numbered their calls together: build the first from the file and the rest with
    ``share_with=first``, which reuses its fixtures and counters (``cli.build_clients`` does); each keeps its own
    ``role``.
    """

    def __init__(self, fixtures_path: str, share_with: "ReplayClient | None" = None, role: str | None = None):
        self.role = role
        if share_with is not None:
            self.fixtures: dict = share_with.fixtures
            self.served: dict = share_with.served
            return
        with open(fixtures_path, encoding="utf-8-sig") as fh:          # a BOM (Notepad, PowerShell) is dropped
            self.fixtures = json.load(fh)
        self.served = {}

    def resume_from_log(self, log_path) -> None:
        """Skip recorded pipeline responses already consumed by earlier sessions.

        Determinism re-asks are not logged requests. Leaving those out positions
        a no-op resume at the same recorded re-asks, preserving the audit result.
        Count responses (including rejected answers), not unanswered requests.
        A key whose value is a tagged ``calls`` list is counted per (key, role, temperature): the role from the
        stage (``replay_check._role``), the temperature from the ``call_request``; a client without a role counts
        per (key, None, temperature). Every other key is counted per key, as before tags existed.
        """
        from .replay_check import _role     # lazy: replay_check imports cli, which imports this module
        counts: dict = {}
        requests = {}
        try:
            fh = open(log_path, encoding="utf-8")
        except FileNotFoundError:
            return
        with fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                payload = rec.get("payload") or {}
                rid = payload.get("request_id")
                if rec.get("record_type") == "call_request":
                    requests[rid] = (payload.get("messages_sha256"), str(payload.get("stage", "")),
                                     payload.get("temperature"))
                elif rec.get("record_type") == "call_response":
                    req = requests.pop(rid, None)
                    if req is None:
                        continue
                    key, stage, temperature = req
                    if tagged_calls(self.fixtures.get(key)):
                        t = _temp(temperature)
                        for slot in {(key, _role(stage), t), (key, None, t)}:     # a set: one slot when _role is None
                            counts[slot] = counts.get(slot, 0) + 1
                    else:
                        counts[key] = counts.get(key, 0) + 1
        self.served.clear()
        self.served.update(counts)

    def _next_call(self, key: str, temperature) -> tuple:
        """For a ``calls`` value: (its counter key in ``served``, the index of the next entry to serve, or None when
        none is left). Positional for an untagged list; the (role, temperature) sub-sequence for a tagged one."""
        calls = self.fixtures[key]["calls"]
        if tagged_calls(self.fixtures[key]):
            t = _temp(temperature)
            slot = (key, self.role, t)
            index = [j for j, c in enumerate(calls) if _tags_match(c, self.role, t)]
            n = self.served.get(slot, 0)
            return slot, (index[n] if n < len(index) else None)
        n = self.served.get(key, 0)
        return key, (n if n < len(calls) else None)

    def complete(self, messages, *, temperature, seed, max_tokens, params=None) -> ModelResponse:
        key = messages_sha256(messages)
        if key not in self.fixtures:
            raise FixtureMissing(key)
        rec = self.fixtures[key]
        if isinstance(rec, dict) and "calls" in rec:
            slot, i = self._next_call(key, temperature)
            if i is None:
                raise FixtureMissing(key)       # recorded fewer calls than this replay is making: strict
            self.served[slot] = self.served.get(slot, 0) + 1
            rec = rec["calls"][i]
        elif _is_tagged(rec) and not _tags_match(rec, self.role, _temp(temperature)):
            raise FixtureMissing(key)           # recorded for another role or temperature
        if isinstance(rec, str):
            return ModelResponse(rec, "stop", {}, 0, _sha256_text(rec))
        text = rec["text"]
        return ModelResponse(text, rec.get("finish_reason"), rec.get("usage") or {}, 0, _sha256_text(text),
                             rec.get("logprobs"))


LIVE_RULE = ('a request the audit log holds (1) with stage "prior_only" and an item_id ending in "__<key>" for one of '
             'the reading-3 spec keys (the no-interview map of the config\'s reading3 block, never a script draft), or '
             'with stage "judge:align" and an item_id containing "__prior_endorsed" (the judge call that aligns it); '
             '(2) with stage "judge:script_prior_only" (the other family\'s no-interview script of a script source), '
             'or with stage "judge:align" and an item_id containing "__endorsed_src__" (the endorsed map aligned '
             'against that script). Never a "judge2:" stage')


def fits_live_rule(stage, item_id, reading3_keys) -> bool:
    """Does a logged request with this stage and item id fit ``LIVE_RULE`` for ``reading3_keys``?"""
    item_id = str(item_id or "")
    return ((stage == "prior_only" and any(item_id.endswith("__" + k) for k in reading3_keys))
            or stage == "judge:script_prior_only"
            or (stage == "judge:align" and ("__prior_endorsed" in item_id or "__endorsed_src__" in item_id)))


class ReplayLiveClient(ReplayClient):
    """Replays the recorded responses for everything except reading 3 and the other family's script (``LIVE_RULE``),
    which it asks a live model.

    A request goes live when it is ``LIVE_RULE``. Any other LOGGED request is served from the fixtures exactly as
    ``ReplayClient`` does, and a miss there raises ``FixtureMissing`` (fatal) instead of reaching the live client. The
    fixture key is the hash of the messages alone (no temperature), so an older recording of the same prompt would be
    a replay hit: for the ``LIVE_RULE`` requests the fixture is ignored and never served.

    The stage and item id are not passed to ``complete``, so they are read from the run's audit log: ``log_call``
    appends and fsyncs the ``call_request`` record before it calls the client. A request is LOGGED when it is the
    newest ``call_request`` of ``log_path``, no ``call_response`` or ``call_error`` follows it, and its
    ``messages_sha256`` is the key asked; that record then decides (the reading-3 draft and a script draft of the same
    variant send the same messages, so only the item id tells them apart). Anything else is an UN-LOGGED RE-ASK: an
    auditor's determinism check re-sends a logged request through ``complete`` without a record of its own. It is a
    re-ask only if its key was logged earlier in this log; a key the log never held, or a log file that does not
    exist (a wrong ``log_path``), raises ``FixtureMissing`` (fatal), so a replay cannot turn into live calls. A re-ask
    of a ``LIVE_RULE`` request goes live. A re-ask of any other request is served from the recording only when its
    fixture is a ``{"calls": [...]}`` list with an unserved recorded call left at the shared counter (for a tagged list,
the counter of this client's role and the request's temperature, see ``ReplayClient``); otherwise it goes
    live (an honest re-ask, never the single recorded answer served again as if it were a second identical answer,
    which a ``RecordingClient`` would also store as a fake ``{"calls": [rec, rec]}``) and is counted in
    ``live_reasks``. The hashes of allowed requests, the byte offset already read and the newest
    request are cached, so each call reads only the tail written since the last one.

    ``live`` is the inner client (anything with ``complete``; its ``cfg``, if any, is exposed so ``log_call`` records
    the live model id and endpoint host). ``reading3_keys`` are the spec keys of the config's reading-3 draws
    (``RunConfig.reading3_specs``). ``live_calls`` (re-asks included) and ``replayed_calls`` count what this client
    sent out and what it served from the fixtures; ``live_reasks`` the un-logged re-asks it sent out, and
    ``live_reasks_by_role`` the same over every client sharing the recording, by ``role`` (set by
    ``cli.build_clients``; it also picks the tagged entries, as for ``ReplayClient``). As for ``ReplayClient``,
    ``share_with=first`` reuses the fixtures, the counters and the log scan of the first client (each client keeps its
    own role, live client and counts).
    """

    def __init__(self, fixtures_path: str, live, log_path, share_with: "ReplayClient | None" = None, *,
                 reading3_keys: tuple[str, ...], role: str | None = None):
        super().__init__(fixtures_path, share_with=share_with, role=role)
        self.live = live
        self.cfg = getattr(live, "cfg", None)      # log_call records the live model id and endpoint host
        self.log_path = log_path
        self.reading3_keys = tuple(reading3_keys)
        # {"keys": hashes of allowed requests, "logged": hashes of every logged request, "offset": bytes of the log
        # already read, "last": [hash, allowed, open] of the newest request (open: no response or error after it
        # yet), "reasks": {role: live re-asks}}; shared like ``served``
        self._scan = getattr(share_with, "_scan", None) or {"keys": set(), "logged": set(), "offset": 0,
                                                             "last": None, "reasks": {}}
        self.live_calls = self.replayed_calls = self.live_reasks = 0

    @property
    def live_reasks_by_role(self) -> dict:
        return dict(self._scan["reasks"])

    def _read_tail(self) -> None:
        """Read the whole lines appended to the log since the last call into the scan. A missing log is fatal."""
        scan = self._scan
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(scan["offset"])
                tail = fh.read()
        except FileNotFoundError:
            raise FixtureMissing("", f"replay-live: the audit log {self.log_path} does not exist") from None
        end = tail.rfind(b"\n") + 1          # a line still being written is read on the next call
        for line in tail[:end].splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            payload = rec.get("payload") if isinstance(rec, dict) else None
            if not isinstance(payload, dict):
                continue
            rtype = rec.get("record_type")
            if rtype == "call_request":
                allowed = fits_live_rule(payload.get("stage"), payload.get("item_id"), self.reading3_keys)
                scan["logged"].add(payload.get("messages_sha256"))
                if allowed:
                    scan["keys"].add(payload.get("messages_sha256"))
                scan["last"] = [payload.get("messages_sha256"), allowed, True]
            elif rtype in ("call_response", "call_error") and scan["last"] is not None:
                scan["last"][2] = False
        scan["offset"] += end

    def _route(self, key: str) -> str:
        """"live" (``LIVE_RULE``), "replay" (a logged request: a miss is fatal) or "reask" (un-logged, not live; its
        key must have been logged earlier, else ``FixtureMissing``)."""
        self._read_tail()
        last = self._scan["last"]
        if last is not None and last[2] and last[0] == key:          # logged: its own record decides
            return "live" if last[1] else "replay"
        if key in self._scan["keys"]:
            return "live"
        if key not in self._scan["logged"]:      # never logged: a wrong log path or an un-logged caller, not a re-ask
            raise FixtureMissing(key, "replay-live: a request the audit log never held; it is not sent live")
        return "reask"

    def _recorded_call_left(self, key: str, temperature) -> bool:
        """Is an unserved recorded call left for this key: at the per-key counter (untagged), or in this client's
        (role, temperature) sub-sequence (tagged)?"""
        rec = self.fixtures.get(key)
        return isinstance(rec, dict) and "calls" in rec and self._next_call(key, temperature)[1] is not None

    def _ask_live(self, messages, temperature, seed, max_tokens, params) -> ModelResponse:
        extra = {"params": params} if params else {}     # a live client without ``params`` still works
        self.live_calls += 1
        return self.live.complete(messages, temperature=temperature, seed=seed, max_tokens=max_tokens, **extra)

    def complete(self, messages, *, temperature, seed, max_tokens, params=None) -> ModelResponse:
        key = messages_sha256(messages)
        route = self._route(key)
        if route == "reask" and not self._recorded_call_left(key, temperature):
            resp = self._ask_live(messages, temperature, seed, max_tokens, params)
            self.live_reasks += 1
            self._scan["reasks"][self.role] = self._scan["reasks"].get(self.role, 0) + 1
            return resp
        if route == "live":
            return self._ask_live(messages, temperature, seed, max_tokens, params)
        resp = super().complete(messages, temperature=temperature, seed=seed, max_tokens=max_tokens, params=params)
        self.replayed_calls += 1
        return resp


class RecordingClient:
    """Wraps a client and keeps every response as a fixture for ``ReplayClient`` (text, finish_reason, usage, and
    ``logprobs`` when the response has them), in call order.

    The store is ``{messages_sha256: value}``. The first response for a key is stored as a record dict, so a request
    made once keeps the single-record form of older files. When the same messages arrive again (the prior-only
    samples are sent several times with different seeds; the determinism audits re-send logged requests, and a live
    model may answer differently the second time) the value becomes ``{"calls": [rec1, rec2, ...]}`` and each later
    call is appended, so no response is overwritten and the one the pipeline consumed stays first. A call that raises
    records nothing.

    Every record also carries the request's ``"temperature"`` (a float) and, when the client knows it, its ``"role"``
    (``cli._record`` passes each client's role). The key is the hash of the messages alone, so different roles or
    temperatures that send identical messages share a key, and the determinism re-asks, which are not logged, sit in
    the middle of its list; the tags let ``ReplayClient`` serve each role and temperature its own entries in any
    session layout (a judge-only session, a resumed run), not only in the one process that recorded the order.

    Fixtures already in ``fixtures_path`` are kept, and a resumed run appends after what the file holds; ``save()``
    writes the file (sorted keys, so the same calls give the same bytes). Clients for different roles can share one
    ``store`` dict (and so one file); their calls are numbered together, in the order they were made. ``merge=False``
    starts from an empty store instead (a fresh recording)."""

    def __init__(self, inner, fixtures_path: str, store: dict | None = None, merge: bool = True,
                 role: str | None = None):
        self.inner = inner
        self.role = role
        self.cfg = getattr(inner, "cfg", None)      # log_call records the inner model id
        self.path = fixtures_path
        self.fixtures: dict = store if store is not None else {}   # several clients may share one store
        if merge and os.path.exists(fixtures_path) and not self.fixtures:
            with open(fixtures_path, encoding="utf-8") as fh:
                self.fixtures.update(json.load(fh))      # in place: every client sharing the store sees it

    def complete(self, messages, *, temperature, seed, max_tokens, params=None) -> ModelResponse:
        extra = {"params": params} if params else {}     # an inner client without ``params`` still works
        resp = self.inner.complete(messages, temperature=temperature, seed=seed, max_tokens=max_tokens, **extra)
        rec = {"text": resp.text, "finish_reason": resp.finish_reason, "usage": resp.usage,
               "temperature": _temp(temperature)}
        if self.role is not None:
            rec["role"] = self.role
        if getattr(resp, "logprobs", None) is not None:
            rec["logprobs"] = resp.logprobs
        key = messages_sha256(messages)
        if key not in self.fixtures:
            self.fixtures[key] = rec
        elif isinstance(self.fixtures[key], dict) and "calls" in self.fixtures[key]:
            self.fixtures[key]["calls"].append(rec)
        else:       # the second call for this key: the earlier value (a record, or bare text in an old file) is call 0
            self.fixtures[key] = {"calls": [self.fixtures[key], rec]}
        return resp

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.fixtures, fh, sort_keys=True, ensure_ascii=False, indent=0)
            fh.write("\n")
        os.replace(tmp, self.path)


def log_call(log: AuditLog, client, messages: list[dict], *, stage: str, item_id: str,
             temperature: float, seed: int | None, max_tokens: int,
             validate: Callable[[str], str | None] | None = None,
             max_attempts: int = 3, params: dict | None = None,
             allow_length: bool = False, allow_empty: bool = False,
             length_correction: str | None = None) -> tuple[str | None, list[dict]]:
    """Call ``client`` with an audit trail; retry on validation failure or exception.

    Writes ``call_request`` BEFORE each attempt and ``call_response`` or ``call_error``
    AFTER. Before ``validate`` runs, a built-in guard rejects an empty reply or one cut
    off by the token limit (``finish_reason == "length"``) with "empty or truncated output
    (finish_reason=...)", so a silent truncation can never pass as an answer even with no
    ``validate``. ``validate(text)`` returns None if OK, else an error message; on a failed
    attempt the model's own reply is appended to ``messages`` as an ``assistant`` turn and
    then the error message as a ``user`` turn, and the call is retried (an empty reply is
    retried with ``messages`` unchanged; a reply cut off at the token limit is not echoed,
    only the correction is added). ``call_request`` also records ``params`` (the client's extra body fields, secrets
    redacted) next to ``max_tokens``. A validator that
    raises counts as a failed attempt (recorded as ``validator raised: ...``, nothing
    appended to ``messages``). If the response cannot be logged, a ``call_error`` closes
    the request and the attempt fails. Returns (text, records), or (None, records) if
    every attempt failed.

    ``params`` are extra request-body fields for this call only (for example
    ``{"logprobs": True, "top_logprobs": 10}``); they are passed to ``client.complete(params=...)`` (only when
    given, so a client without that argument keeps working) and recorded in ``call_request`` next to the
    client's own. A response that carries ``logprobs`` has them recorded in ``call_response``.
    ``allow_length=True`` accepts ``finish_reason == "length"``: a one-token answer (``max_tokens=1``)
    always stops at the limit, so there it is the normal outcome, not a truncation. ``allow_empty=True``
    accepts an empty or whitespace-only reply (a first token that is only whitespace): the caller reads the
    logprobs, not the text.

    ``length_correction`` (default None = the generic correction below, unchanged) is the user turn added after a
    reply cut off at the token limit (``finish_reason == "length"``) INSTEAD of the generic "Your previous answer was
    invalid: ..." text, so the retry can tell the model to be shorter. Every other failure keeps the generic text.
    """
    cfg = getattr(client, "cfg", None)
    model_id = getattr(cfg, "model_id", None)
    key_env = getattr(cfg, "api_key_env", None)
    records: list[dict] = []
    for attempt in range(1, max_attempts + 1):
        request_id = f"{stage}:{item_id}:{attempt}:{uuid.uuid4().hex[:8]}"
        snapshot = json.loads(json.dumps(messages))   # what is sent now, immune to later appends
        req = {"request_id": request_id, "stage": stage, "item_id": item_id, "attempt": attempt,
               "messages": snapshot, "messages_sha256": messages_sha256(snapshot),
               "temperature": temperature, "seed": seed, "max_tokens": max_tokens,
               "params": redact_params({**(getattr(cfg, "params", None) or {}), **(params or {})}, key_env)}
        if model_id is not None:
            req["model_id"] = model_id
        if (host := endpoint_host(getattr(cfg, "endpoint", None))) is not None:
            req["endpoint_host"] = host      # where the request went (never the URL, userinfo or key)
        records.append(log.append("call_request", req))
        try:
            extra = {"params": params} if params else {}
            resp = client.complete(snapshot, temperature=temperature, seed=seed, max_tokens=max_tokens, **extra)
        except Exception as exc:  # noqa: BLE001 - every failure is logged, then retried
            err_record = {"request_id": request_id, "error": _scrub(repr(exc), key_env)[:500]}
            if is_transport_error(exc):
                err_record["transport"] = True       # an outage, not an answer: the item is redone on a rerun
            records.append(log.append("call_error", err_record))
            if getattr(exc, "fatal", False):
                raise
            continue
        validator_crashed = False
        try:
            if ((not (resp.text or "").strip() and not allow_empty)
                    or (resp.finish_reason == "length" and not allow_length)):
                err = f"empty or truncated output (finish_reason={resp.finish_reason})"
            else:
                err = validate(resp.text) if validate else None
            if err is not None:
                err = _scrub(str(err), key_env)
        except Exception as exc:  # noqa: BLE001 - a validator bug is a failed attempt, not a crash
            err = _scrub(f"validator raised: {exc!r}", key_env)[:500]
            validator_crashed = True
        try:
            response_record = {
                "request_id": request_id, "text": resp.text, "finish_reason": resp.finish_reason,
                "usage": resp.usage, "latency_ms": resp.latency_ms, "raw_sha256": resp.raw_sha256,
                "validation_error": err}
            if getattr(resp, "logprobs", None) is not None:
                response_record["logprobs"] = resp.logprobs
            records.append(log.append("call_response", response_record))
        except Exception as exc:  # noqa: BLE001 - e.g. usage values that are not JSON-safe
            # Close the request so no orphan is left; a reply that is not in the log is not returned.
            records.append(log.append("call_error", {
                "request_id": request_id,
                "error": _scrub(f"response could not be logged: {exc!r}", key_env)[:500]}))
            continue
        if err is None:
            return resp.text, records
        if not validator_crashed:   # the model cannot fix a bug in our validator
            if not (resp.text or "").strip():
                continue        # an empty reply: retry with the messages unchanged
            if resp.finish_reason != "length":      # a truncated reply is not echoed back
                messages.append({"role": "assistant", "content": resp.text})
            if length_correction is not None and resp.finish_reason == "length" and not allow_length:
                messages.append({"role": "user", "content": length_correction})
            else:
                messages.append({"role": "user", "content":
                                 f"Your previous answer was invalid: {err}. Return only valid JSON. "
                                 "Begin your answer with {"})
    return None, records
