"""Setup step 6, the models (Task 34; spec 6.5 step 6, 8, 8.1, 2.1-2.2).

For each of the reconstructor (``recon``) and the judge (``judge``): the address class, model discovery on a local
server, the family, the cloud-model refusal, a timed connection test with the thinking check, the LAN/Tailscale
opt-in and the remote declaration. Keys stay in ``api.Secrets``; nothing here writes one.

**Contact rule.** The app contacts a model address only when it may: a loopback address at once; a LAN or Tailscale
address after the role's logged ``optin``; a remote address after the role's logged ``declaration`` (and only over a
scheme in ``llm.REMOTE_SCHEMES``, https). ``contact_problem`` says why not; the routes call it before every request.

**The project's ``models`` data** (``project.data["models"]``)::

    {"roles": {"recon": ROLE, "judge": ROLE},          # judge may be absent: AI maps only
     "confirmed": {"recon": family, "judge": family} | None,   # the families the user confirmed, as confirmed
     "pipeline_mode": "two_family" | "ai_maps_only" | None}

    ROLE = {"kind": "local" | "openai_compat" | "anthropic",
            "provider": "openai" | "anthropic",          # llm.ModelConfig.provider (SS_<P>_PROVIDER)
            "url": endpoint base for the kit (SS_<P>_URL; a local server's ends in /v1),
            "host": llm.endpoint_host(url), "address_class": "loopback" | "lan" | "tailscale" | "remote",
            "model": id, "family": confirmed-by-user family (lower case), "family_detected": str | None,
            "digest": 64-hex | None, "server": "ollama" | "lmstudio" | "openai" | None,
            "connection": None | {"ok", "ms", "thinking_seen", "reply_ok", "error"}}

The opt-ins and declarations are NOT copied into the project file: the app log is their only record, and
``endpoint_optins`` / ``remote_endpoints`` read them from it, matched against the roles as they are set now.

For Tasks 35 and 38: ``problems`` / ``require_ready`` (the refusals), ``tool_fields`` (the ``tool`` block's model
keys), ``remote_endpoints`` (the K13 allow-list ``[{role, host, provider, declaration_sha256}]``),
``endpoint_optins``, ``role_env`` (the kit subprocess env for one role), ``pipeline_mode`` and ``pair_sha256``.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .. import certificate, llm
from ..anthropic_client import ANTHROPIC_VERSION
from ..families import known_family

ROLES = ("recon", "judge")
KINDS = ("local", "openai_compat", "anthropic")
KIND_PROVIDER = {"local": "openai", "openai_compat": "openai", "anthropic": "anthropic"}
ADDRESS_CLASSES = ("loopback", "lan", "tailscale", "remote", "invalid")
ROLE_FIELDS = ("kind", "provider", "url", "host", "address_class", "model", "family", "family_detected", "digest",
               "server", "connection")
DISCOVERY_TIMEOUT_S = 3.0
CONNECTION_TIMEOUT_S = 120.0       # a local server may load the model on this call
PING_PROMPT = 'Reply with exactly this JSON and nothing else: {"ok": true}'
PING_MAX_TOKENS = 64
DECLARATION_SCHEMA = "declaration/1"
# What the address field starts with, per kind (served by GET /api/models: no URL is written in the static files).
DEFAULT_URLS = {"local": "http://127.0.0.1:11434", "openai_compat": "", "anthropic": "https://api.anthropic.com/v1"}
MAX_BASIS = 1000
MAX_MODEL = 200
MAX_FAMILY = 40

# The kit's nets (``llm._LOCAL_NETS``), split by class; a test pins that together they are exactly the kit's.
LOOPBACK_NETS = (llm._net(127, 0, 0, 0, 8), ipaddress.ip_network("::1/128"))
LAN_NETS = (llm._net(10, 0, 0, 0, 8), llm._net(172, 16, 0, 0, 12), llm._net(192, 168, 0, 0, 16))
TAILSCALE_NETS = (llm._net(100, 64, 0, 0, 10),)
_RANK = {"loopback": 0, "lan": 1, "tailscale": 2, "remote": 3}

# Error codes the routes raise (each has ``models.error.<code>`` in en.json).
ERROR_CODES = ("bad_role", "bad_kind", "bad_url", "name_not_found", "bad_provider", "bad_model", "family_missing", "model_not_found",
               "unreachable", "no_models_listed", "cloud_model", "anthropic_remote_only", "remote_needs_https",
               "optin_missing", "declaration_missing", "optin_not_needed", "declaration_not_needed", "declaration_needed",
               "not_understood", "not_ticked", "basis_missing", "bad_host", "key_missing", "bad_key",
               "bad_env_name", "role_not_set", "families_changed", "recon_missing", "families_unconfirmed",
               "connection_untested", "connection_failed", "thinking_on")
# Connection-test error codes (``models.connection.<code>``); ``http_<n>`` other than these reads ``http``.
CONNECTION_ERRORS = ("timeout", "unreachable", "malformed", "auth", "not_found", "http", "redirect")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FAMILY = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ModelsError(Exception):
    """A refusal with a stable ``code`` (``models.error.<code>``); ``detail`` is for tests and the terminal."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# --- addresses --------------------------------------------------------------------------------------------------

def _address_class(addr: str) -> str:
    try:
        ipobj = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return "remote"
    if getattr(ipobj, "ipv4_mapped", None) is not None:
        ipobj = ipobj.ipv4_mapped
    for cls, nets in (("loopback", LOOPBACK_NETS), ("lan", LAN_NETS), ("tailscale", TAILSCALE_NETS)):
        if any(ipobj in n for n in nets if n.version == ipobj.version):
            return cls
    return "remote"


def classify_address(url: Any, resolver: Callable[[str, int | None], list[str]] | None = None) -> str:
    """``"loopback" | "lan" | "tailscale" | "remote" | "invalid"`` for a model URL, on the kit's nets (127/8 and ::1
    loopback; RFC 1918 lan; 100.64/10 tailscale; anything else remote). An IP literal is read as is; a name is
    resolved (``resolver(host, port)``, default the kit's ``llm._resolve``) and the widest class of its addresses
    wins, so a name that resolves to one public address is remote. Not http(s), no host, or no lookup: invalid."""
    if not isinstance(url, str):
        return "invalid"
    try:
        p = urllib.parse.urlsplit(url.strip())
        host, port = p.hostname, p.port
    except ValueError:
        return "invalid"
    if p.scheme not in ("http", "https") or not host:
        return "invalid"
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        addrs = [host]
    except ValueError:
        try:
            addrs = list((resolver or llm._resolve)(host, port))
        except (OSError, UnicodeError):
            return "invalid"
    if not addrs:
        return "invalid"
    return max((_address_class(a) for a in addrs), key=_RANK.__getitem__)


def endpoint_url(url: Any, kind: str) -> str:
    """The kit's endpoint base for what the user typed. A local server: its root plus ``/v1`` (typed with or
    without it). An API: as typed, without a trailing slash. ``ModelsError("bad_url")`` for anything that is not a
    plain http(s) URL with a host, or that carries a user name, password, query or fragment (a credential in a URL
    would reach the project file)."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2000 or _CONTROL.search(url) or " " in url.strip():
        raise ModelsError("bad_url", "not one line")
    raw = url.strip()
    try:
        p = urllib.parse.urlsplit(raw)
        host, _port = p.hostname, p.port
    except ValueError:
        raise ModelsError("bad_url", "cannot be parsed") from None
    if p.scheme not in ("http", "https") or not host:
        raise ModelsError("bad_url", "not http(s) with a host")
    if p.username or p.password or p.query or p.fragment or raw.endswith(("?", "#")):
        raise ModelsError("bad_url", "user name, password, query or fragment")
    path = p.path.rstrip("/")
    if kind == "local":
        if path.endswith("/v1"):
            path = path[:-3]
        path = path.rstrip("/") + "/v1"
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), path, "", ""))


def server_root(endpoint: str) -> str:
    """A local server's root (the endpoint base without its trailing ``/v1``)."""
    return endpoint[:-3] if endpoint.endswith("/v1") else endpoint


def https_ok(url: str) -> bool:
    try:
        return urllib.parse.urlsplit(url).scheme.lower() in llm.REMOTE_SCHEMES
    except ValueError:
        return False


# --- HTTP -------------------------------------------------------------------------------------------------------

# No proxy (ProxyHandler({}) ignores http_proxy and the system settings) and no redirects: the same rules as the
# kit's model calls, so a request goes only to the address the user typed and the app classified.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), llm._RefuseRedirects)


class _HttpStatus(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def _auth_headers(provider: str, key: str | None) -> dict[str, str]:
    h = {"Accept": "application/json"}
    if provider == "anthropic":
        h["anthropic-version"] = ANTHROPIC_VERSION
        if key:
            h["x-api-key"] = key
    elif key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _request(opener, method: str, url: str, headers: dict, body: dict | None, timeout: float) -> Any:
    """The parsed JSON reply. ``_HttpStatus`` for an HTTP error, ``ValueError`` for a reply that is not JSON, and
    the transport's own exceptions (``URLError``, ``TimeoutError``, ``OSError``) otherwise."""
    data = None
    headers = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with (opener or _OPENER).open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise _HttpStatus(status) from None
    return json.loads(raw.decode("utf-8"))


# --- discovery --------------------------------------------------------------------------------------------------

def _digest(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.strip().lower()
    if v.startswith("sha256:"):
        v = v[7:]
    return v if _HEX64.match(v) else None


def _str(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def is_cloud_model(entry: dict) -> bool:
    """True for a model that runs in someone's cloud though a local server lists it: a non-empty ``remote_host`` or
    ``remote_model`` (Ollama), a ``remote`` flag from ``discover``, or a name ending ``:cloud`` / ``-cloud``."""
    return cloud_reason(entry) is not None


def cloud_reason(entry: dict) -> str | None:
    if not isinstance(entry, dict):
        return None
    if _str(entry.get("remote_host")):
        return "remote_host"
    if _str(entry.get("remote_model")):
        return "remote_model"
    if entry.get("remote") is True:
        return entry.get("cloud_reason") or "remote_host"
    name = (_str(entry.get("name")) or "").lower()
    if name.endswith((":cloud", "-cloud")):
        return "cloud_name"
    return None


def family_hint(name: str | None, provider: str = "openai") -> str | None:
    """A family read from a model id, for the user to confirm: the kit's ``FAMILY_TABLE`` family, else ``claude``
    for the Anthropic provider, else the vendor of an OpenRouter-style ``vendor/model`` id; None otherwise (an alias
    such as ``judge`` says nothing about the family)."""
    fam = known_family(name)
    if fam:
        return fam
    if provider == "anthropic":
        return "claude"
    if isinstance(name, str) and "/" in name:
        vendor = name.strip().lower().split("/", 1)[0]
        return vendor if _FAMILY.match(vendor) else None
    return None


def _entry(name: str, server: str, *, family=None, families=(), digest=None, source=None, raw=None,
           provider="openai") -> dict:
    fam = _str(family)
    e = {"name": name, "server": server, "family": fam.lower() if fam else None,
         "families": [f.lower() for f in families if _str(f)], "family_source": source if fam else None,
         "family_hint": family_hint(name, provider), "digest": _digest(digest)}
    reason = cloud_reason({**(raw or {}), "name": name})
    e["remote"] = reason is not None
    e["cloud_reason"] = reason
    return e


def discover(base_url: str, *, kind: str = "local", key: str | None = None, opener=None,
             timeout: float = DISCOVERY_TIMEOUT_S) -> list[dict]:
    """The models a server offers, ``[{name, server, family, families, family_source, family_hint, digest, remote,
    cloud_reason}]``. A local server (``kind == "local"``): Ollama ``GET /api/tags`` then ``POST /api/show`` per
    model (``model_info["general.architecture"]``); else LM Studio ``GET /api/v1/models`` (``architecture``), then
    ``/api/v0/models`` (``arch``); else ``GET /v1/models`` names with ``family`` None (the user types it). An API
    (``openai_compat``, ``anthropic``): only ``GET <endpoint>/models``, with the key. ``timeout`` seconds per
    request. ``ModelsError("unreachable")`` when nothing answers, ``("no_models_listed")`` when the server answers
    but lists nothing. Never call this for an address ``contact_problem`` refuses."""
    endpoint = endpoint_url(base_url, kind)
    provider = KIND_PROVIDER.get(kind, "openai")
    headers = _auth_headers(provider, key)
    answered = False

    def get(url, body=None):
        nonlocal answered
        try:
            doc = _request(opener, "POST" if body is not None else "GET", url, headers, body, timeout)
            answered = True
            return doc
        except _HttpStatus:
            answered = True
        except ValueError:
            answered = True
        except (urllib.error.URLError, OSError, TimeoutError, ConnectionError):
            pass
        except Exception:  # noqa: BLE001 - a malformed reply from a server is "no list", never a crash
            answered = True
        return None

    if kind == "local":
        root = server_root(endpoint)
        tags = get(root + "/api/tags")
        if isinstance(tags, dict) and isinstance(tags.get("models"), list):
            out = []
            for m in tags["models"]:
                if not isinstance(m, dict) or not (_str(m.get("name")) or _str(m.get("model"))):
                    continue
                name = _str(m.get("name")) or _str(m.get("model"))
                show = get(root + "/api/show", {"model": name})
                show = show if isinstance(show, dict) else {}
                info = show.get("model_info") if isinstance(show.get("model_info"), dict) else {}
                details = m.get("details") if isinstance(m.get("details"), dict) else (
                    show.get("details") if isinstance(show.get("details"), dict) else {})
                arch = _str(info.get("general.architecture"))
                fams = details.get("families") if isinstance(details.get("families"), list) else []
                raw = {"remote_host": m.get("remote_host") or show.get("remote_host"),
                       "remote_model": m.get("remote_model") or show.get("remote_model")}
                out.append(_entry(name, "ollama", family=arch or _str(details.get("family")),
                                  families=fams, digest=m.get("digest") or show.get("digest"),
                                  source="architecture" if arch else "details", raw=raw))
            if out:
                return out
        for path, list_key, name_key, arch_key in (("/api/v1/models", "models", "key", "architecture"),
                                                   ("/api/v0/models", "data", "id", "arch")):
            doc = get(root + path)
            items = doc.get(list_key) if isinstance(doc, dict) else None
            if isinstance(items, list):
                out = [_entry(_str(i.get(name_key)), "lmstudio", family=_str(i.get(arch_key)), source="architecture")
                       for i in items if isinstance(i, dict) and _str(i.get(name_key))
                       and i.get("type", "llm") in ("llm", "vlm")]
                if out:
                    return out
    doc = get(endpoint + "/models")
    items = doc.get("data") if isinstance(doc, dict) else None
    if isinstance(items, list):
        out = [_entry(_str(i.get("id")), "openai", raw=i, provider=provider)
               for i in items if isinstance(i, dict) and _str(i.get("id"))]
        if out:
            return out
    raise ModelsError("no_models_listed" if answered else "unreachable")


# --- families ---------------------------------------------------------------------------------------------------

_TRAILING = re.compile(r"[\d._-]+$")


def normalize_family(v: Any) -> str | None:
    """A family as stored: lower case, trimmed, ``[a-z0-9][a-z0-9._-]*``, at most 40 characters; else None."""
    if not isinstance(v, str):
        return None
    v = v.strip().lower()
    return v if v and len(v) <= MAX_FAMILY and _FAMILY.match(v) else None


def families_differ(a: Any, b: Any) -> bool:
    """True when two families are different families. False when either is missing, or when one lower-cased family
    is a prefix of the other after trailing digits, dots, dashes and underscores are stripped (``gemma3`` and
    ``gemma2`` are one family; so are ``qwen2`` and ``qwen3moe``). Errs toward "same": a same-family pair runs AI
    maps only, which is the safe side."""
    sa = _TRAILING.sub("", a.strip().lower()) if isinstance(a, str) else ""
    sb = _TRAILING.sub("", b.strip().lower()) if isinstance(b, str) else ""
    if not sa or not sb:
        return False
    return not (sa.startswith(sb) or sb.startswith(sa))


# --- connection test --------------------------------------------------------------------------------------------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_THINK = re.compile(r"</?think>", re.IGNORECASE)


def _reply_ok(text: str) -> bool:
    try:
        return json.loads(_FENCE.sub("", text.strip())) == {"ok": True}
    except ValueError:
        return False


def connection_test(role: str, cfg: dict, *, key: str | None = None, opener=None,
                    timeout: float = CONNECTION_TIMEOUT_S) -> dict:
    """One tiny request to ``cfg`` (a ROLE dict: ``url``, ``model``, ``provider``, ``address_class``), timed:
    ``{ok, ms, thinking_seen, reply_ok, error}``. ``ok``: a well-formed reply came back. ``thinking_seen``: it carried
    ``reasoning_content`` (or the kit's other reasoning fields), a ``<think>`` tag, or a thinking block, although a
    local server was asked with ``chat_template_kwargs.enable_thinking`` false (the kit's switch; never sent to a
    remote API). ``reply_ok``: the text was the JSON asked for. ``error``: a ``CONNECTION_ERRORS`` code or
    ``http_<status>``, never server text. The prompt holds no participant text."""
    provider = cfg.get("provider", "openai")
    url = str(cfg.get("url") or "").rstrip("/")
    messages = [{"role": "user", "content": PING_PROMPT}]
    if provider == "anthropic":
        target = url + "/messages"
        body = {"model": cfg.get("model"), "max_tokens": PING_MAX_TOKENS, "temperature": 0, "messages": messages}
    else:
        target = url + "/chat/completions"
        body = {"model": cfg.get("model"), "messages": messages, "temperature": 0, "max_tokens": PING_MAX_TOKENS}
        if cfg.get("address_class") != "remote":
            body["chat_template_kwargs"] = {"enable_thinking": False}
    out = {"ok": False, "ms": None, "thinking_seen": False, "reply_ok": False, "error": None}
    t0 = time.monotonic()
    try:
        doc = _request(opener, "POST", target, _auth_headers(provider, key), body, timeout)
    except _HttpStatus as e:
        out["error"] = {401: "auth", 403: "auth", 404: "not_found"}.get(e.status, f"http_{e.status}")
    except ValueError:
        out["error"] = "malformed"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        out["error"] = "timeout" if isinstance(reason, TimeoutError) or "timed out" in str(reason) else "unreachable"
    except TimeoutError:
        out["error"] = "timeout"
    except (OSError, ConnectionError):
        out["error"] = "unreachable"
    out["ms"] = int((time.monotonic() - t0) * 1000)
    if out["error"]:
        return out
    try:
        if provider == "anthropic":
            blocks = doc["content"]
            text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
            thinking = any(isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking") for b in blocks)
            thinking = thinking or bool(_THINK.search(text))
        else:
            message = doc["choices"][0]["message"]
            text = message.get("content") or ""
            usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
            thinking = llm.thinking_evidence(message, usage, text) is not None
        if not isinstance(text, str):
            raise TypeError("text")
    except (KeyError, IndexError, TypeError, AttributeError):
        out["error"] = "malformed"
        return out
    out.update(ok=True, thinking_seen=bool(thinking), reply_ok=_reply_ok(text))
    return out


def connection_error_key(code: str | None) -> str | None:
    """The ``models.connection.<x>`` strings key for a connection error code."""
    if code is None:
        return None
    return f"models.connection.{code if code in CONNECTION_ERRORS else 'http'}"


# --- the project's models data ----------------------------------------------------------------------------------

def data(project) -> dict:
    """``project.data["models"]``, with its keys filled in."""
    m = project.data.setdefault("models", {})
    if not isinstance(m.get("roles"), dict):
        m["roles"] = {}
    m.setdefault("confirmed", None)
    m.setdefault("pipeline_mode", None)
    return m


def role(project, r: str) -> dict | None:
    return data(project)["roles"].get(r)


def current_families(project) -> dict[str, str]:
    return {r: e["family"] for r, e in data(project)["roles"].items() if r in ROLES and e.get("family")}


def set_role(project, r: str, entry: dict) -> None:
    """Store ``entry`` (ROLE fields only) for role ``r`` and drop the family confirmation (call ``save()``)."""
    m = data(project)
    m["roles"][r] = {k: entry.get(k) for k in ROLE_FIELDS}
    m["confirmed"] = None
    m["pipeline_mode"] = None


def clear_role(project, r: str) -> None:
    m = data(project)
    m["roles"].pop(r, None)
    m["confirmed"] = None
    m["pipeline_mode"] = None


def confirm_families(project, families: dict[str, Any]) -> str:
    """Confirm the roles' families as they stand; returns the pipeline mode. ``ModelsError("families_changed")``
    unless ``families`` names exactly the set roles with their stored families; ``recon_missing`` without a
    reconstructor. One family (or no judge): ``ai_maps_only``; there is no override."""
    cur = current_families(project)
    if "recon" not in cur:
        raise ModelsError("recon_missing")
    given = {r: normalize_family(families.get(r)) for r in ROLES if families.get(r) is not None}
    if given != cur:
        raise ModelsError("families_changed")
    mode = "two_family" if "judge" in cur and families_differ(cur["recon"], cur["judge"]) else "ai_maps_only"
    m = data(project)
    m["confirmed"] = dict(cur)
    m["pipeline_mode"] = mode
    return mode


def families_confirmed(project) -> bool:
    m = data(project)
    return bool(m.get("confirmed")) and m["confirmed"] == current_families(project)


def pipeline_mode(project) -> str | None:
    """``"two_family"``, ``"ai_maps_only"`` (one family or no judge; the app runs Q and M only), or None while the
    families are not confirmed as they stand."""
    return data(project).get("pipeline_mode") if families_confirmed(project) else None


def same_family(project) -> bool | None:
    cur = current_families(project)
    if "recon" not in cur or "judge" not in cur:
        return None
    return not families_differ(cur["recon"], cur["judge"])


# --- opt-ins and declarations (the app log is their record) -----------------------------------------------------

def check_host(host: Any) -> str:
    """``host`` or ``host:port`` as ``llm.endpoint_host`` writes it (lower case); ``ModelsError("bad_host")``."""
    if not isinstance(host, str) or not host.strip() or len(host) > 300 or _CONTROL.search(host):
        raise ModelsError("bad_host")
    h = host.strip().lower()
    if any(c in h for c in "/@?#\\ "):
        raise ModelsError("bad_host")
    if llm.endpoint_host(f"http://{h}/") != h:
        raise ModelsError("bad_host")
    return h


def host_class(host: str, resolver=None) -> str:
    return classify_address(f"http://{host}/", resolver)


def optins(project) -> list[dict]:
    return [r["payload"] for r in project.log.records("optin") if isinstance(r.get("payload"), dict)]


def declarations(project) -> list[dict]:
    return [r["payload"] for r in project.log.records("declaration") if isinstance(r.get("payload"), dict)]


def has_optin(project, r: str, host: str) -> bool:
    return any(o.get("role") == r and o.get("host") == host for o in optins(project))


def find_declaration(project, r: str, host: str, allow_provider: str) -> dict | None:
    """The latest logged declaration for ``(role, host, provider)``; ``allow_provider`` in the allow-list's words
    (``toolcfg.PROVIDERS``). A record whose text does not hash to its own ``sha256`` does not count (audit K5)."""
    found = None
    for d in declarations(project):
        if d.get("role") == r and d.get("host") == host and d.get("provider") == allow_provider \
                and isinstance(d.get("sha256"), str) and _HEX64.match(d["sha256"]) \
                and certificate.declaration_record_sha256(d) == d["sha256"]:
            found = d
    return found


def require_declarations(project, tool: dict) -> None:
    """Audit K5/S4: refuse a run config whose ``tool.remote_endpoints`` names a declaration the app log does not hold
    before the run: ``api.ApiError(409, "declaration_missing")`` unless every entry's ``declaration_sha256`` is a
    logged ``declaration`` record for the same role, host and provider whose own text hashes to it
    (``certificate.declaration_check``, the check the certificate prints), and, when ``tool.app_log_head`` is set,
    one written below that head. Call it after ``app_log_head`` is set and before the config is written or a
    ``run_started`` record is appended; None when it passes (and for a config with no remote endpoint)."""
    eps = (tool or {}).get("remote_endpoints") or []
    if not eps:
        return
    head = (tool or {}).get("app_log_head")
    if head is not None and not (isinstance(head, dict) and isinstance(head.get("records"), int)
                                 and isinstance(head.get("head"), str) and _HEX64.match(head["head"])):
        head = {"records": -1, "head": ""}                            # a malformed head proves no order
    recs = project.log.records()
    for ep in eps:
        check = certificate.declaration_check(recs, ep if isinstance(ep, dict) else {}, head,
                                              require_order=head is not None)
        if check["ok"] is not True:
            from . import strings
            from .api import ApiError
            raise ApiError(409, "declaration_missing", strings.t("models.error.declaration_missing"))


def log_optin(project, r: str, host: str, resolver=None) -> dict:
    """Log ``optin{role, host, address_class}`` for a LAN or Tailscale host (once per role and host)."""
    if r not in ROLES:
        raise ModelsError("bad_role")
    host = check_host(host)
    cls = host_class(host, resolver)
    if cls == "loopback":
        raise ModelsError("optin_not_needed")
    if cls == "remote":
        raise ModelsError("declaration_needed")
    if cls == "invalid":
        raise ModelsError("bad_host")
    payload = {"role": r, "host": host, "address_class": cls}
    for o in optins(project):
        if o.get("role") == r and o.get("host") == host:
            return o
    project.log.append("optin", payload)
    return payload


def allow_provider(provider: Any) -> str:
    """The allow-list's provider word (``toolcfg.PROVIDERS``) for a ``ModelConfig`` provider or for itself."""
    if provider in llm.ALLOWLIST_PROVIDER:
        return llm.ALLOWLIST_PROVIDER[provider]
    if provider in llm.ALLOWLIST_PROVIDER.values():
        return provider
    raise ModelsError("bad_provider")


def declaration_doc(r: str, provider: str, host: str, basis: str) -> dict:
    return {"schema": DECLARATION_SCHEMA, "role": r, "provider": provider, "host": host, "basis": basis}


def declaration_sha256(doc: dict) -> str:
    """sha256 of the canonical declaration (sorted keys, no spaces, UTF-8) over ``declaration_doc``'s keys: the kit's
    ``certificate.declaration_record_sha256``, so the certificate recomputes the same value from the record."""
    return certificate.declaration_record_sha256(doc)


def log_declaration(project, r: str, provider: Any, host: Any, basis: Any, ticked: Any, resolver=None) -> dict:
    """Log ``declaration{schema, role, provider, host, basis, sha256}`` for a remote host; returns ``{role, provider,
    host, sha256}``. The same declaration twice is logged once."""
    if r not in ROLES:
        raise ModelsError("bad_role")
    prov = allow_provider(provider)
    host = check_host(host)
    cls = host_class(host, resolver)
    if cls == "invalid":
        raise ModelsError("bad_host")
    if cls != "remote":
        raise ModelsError("declaration_not_needed")
    if ticked is not True:
        raise ModelsError("not_ticked")
    if not isinstance(basis, str) or not basis.strip() or len(basis) > MAX_BASIS or _CONTROL.search(basis):
        raise ModelsError("basis_missing")
    doc = declaration_doc(r, prov, host, basis.strip())
    sha = declaration_sha256(doc)
    if not any(d.get("sha256") == sha for d in declarations(project)):
        project.log.append("declaration", {**doc, "sha256": sha})
    return {"role": r, "provider": prov, "host": host, "sha256": sha}


def contact_problem(project, r: str, url: str, kind: str, resolver=None) -> tuple[str, dict]:
    """(problem code or "", address info) for contacting ``url`` as role ``r``: ``bad_url``, ``name_not_found`` (a
    well-formed address whose name cannot be looked up), ``remote_needs_https``,
    ``optin_missing`` or ``declaration_missing``. Info: ``{address_class, host, endpoint, needs, allowed, https_ok}``
    (``needs``: "optin", "declaration" or None)."""
    try:
        endpoint = endpoint_url(url, kind)
    except ModelsError:
        return "bad_url", {"address_class": "invalid", "host": None, "endpoint": None, "needs": None,
                           "allowed": False, "https_ok": False}
    cls = classify_address(endpoint, resolver)
    host = llm.endpoint_host(endpoint)
    info = {"address_class": cls, "host": host, "endpoint": endpoint, "needs": None, "allowed": False,
            "https_ok": https_ok(endpoint)}
    if cls == "invalid":
        # ``endpoint_url`` accepted it (http(s), a host, nothing else), so the address is well formed: the name could
        # not be looked up (no network, or no such name). Still refused (fail closed), but with its own message.
        return "name_not_found", info
    if cls in ("lan", "tailscale"):
        info["needs"] = "optin"
        if not has_optin(project, r, host):
            return "optin_missing", info
    elif cls == "remote":
        info["needs"] = "declaration"
        if not info["https_ok"]:
            return "remote_needs_https", info
        if find_declaration(project, r, host, allow_provider(KIND_PROVIDER.get(kind, "openai"))) is None:
            return "declaration_missing", info
    info["allowed"] = True
    return "", info


# --- what Tasks 35 and 38 read ----------------------------------------------------------------------------------

def endpoint_optins(project) -> list[dict]:
    """``tool.endpoint_optins``: ``[{role, host}]`` for each set role on a LAN or Tailscale address with its opt-in."""
    out = []
    for r in ROLES:
        e = role(project, r)
        if e and e.get("address_class") in ("lan", "tailscale") and has_optin(project, r, e.get("host")):
            out.append({"role": r, "host": e["host"]})
    return out


def remote_endpoints(project) -> list[dict]:
    """``tool.remote_endpoints`` (the K13 allow-list): ``[{role, host, provider, declaration_sha256}]`` for each set
    role on a remote address with a logged declaration for that role, host and provider."""
    out = []
    for r in ROLES:
        e = role(project, r)
        if not e or e.get("address_class") != "remote":
            continue
        prov = llm.ALLOWLIST_PROVIDER.get(e.get("provider"))
        d = find_declaration(project, r, e.get("host"), prov) if prov else None
        if d is not None:
            out.append({"role": r, "host": e["host"], "provider": prov, "declaration_sha256": d["sha256"]})
    return out


def tool_fields(project) -> dict:
    """The model keys of a tool config's ``tool`` block: ``judge_rule`` ("two_family"), ``model_families`` and
    ``model_digests`` (roles with a 64-hex digest), and ``remote_endpoints`` / ``endpoint_optins`` when not empty.
    Readiness is ``require_ready``'s job, not this function's."""
    out: dict[str, Any] = {"judge_rule": "two_family", "model_families": current_families(project),
                           "model_digests": {r: e["digest"] for r, e in data(project)["roles"].items()
                                             if r in ROLES and isinstance(e.get("digest"), str)
                                             and _HEX64.match(e["digest"])}}
    if (eps := remote_endpoints(project)):
        out["remote_endpoints"] = eps
    if (ops := endpoint_optins(project)):
        out["endpoint_optins"] = ops
    return out


THINKING_OFF = {"chat_template_kwargs": {"enable_thinking": False}}


def role_env(project, secrets, r: str, prefix: str | None = None) -> dict[str, str]:
    """The kit subprocess env for role ``r`` under model prefix ``prefix`` (default the role, upper case):
    ``SS_<P>_URL``, ``_MODEL``, ``_PROVIDER``, ``_PARAMS`` (thinking off for every address but a remote one, where
    ``chat_template_kwargs`` is not a field; ``{}`` there) and, when a key is set, ``SS_<P>_KEY_ENV`` and the
    variable it names (``Secrets.env_for``). Pass it to the subprocess only; never write it."""
    e = role(project, r)
    if not e:
        raise ModelsError("role_not_set", r)
    p = (prefix or r).upper()
    params = {} if e.get("address_class") == "remote" else THINKING_OFF
    env = {f"SS_{p}_URL": e["url"], f"SS_{p}_MODEL": e["model"], f"SS_{p}_PROVIDER": e["provider"],
           f"SS_{p}_PARAMS": json.dumps(params, sort_keys=True)}
    if secrets is not None:
        env.update(secrets.env_for(r, p))
    return env


def pair_sha256(project) -> str:
    """sha256 of the set roles' ``{provider, host, model, digest}`` (canonical JSON): the model pair's identity, so
    a model check (Task 35) can be keyed to the pair it ran on."""
    pair = {r: {k: e.get(k) for k in ("provider", "host", "model", "digest")}
            for r, e in sorted(data(project)["roles"].items()) if r in ROLES}
    return hashlib.sha256(json.dumps(pair, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def problems(project, secrets=None, resolver=None) -> list[dict]:
    """Why the models are not ready, ``[{code, role}]`` in a fixed order (role None for the pair):
    ``recon_missing``; per set role ``optin_missing`` (LAN/Tailscale without its logged opt-in),
    ``remote_needs_https`` and ``declaration_missing`` (remote), ``cloud_model``, ``key_missing`` (an API kind with no
    key in memory; checked only when ``secrets`` is given), ``connection_untested`` / ``connection_failed`` /
    ``thinking_on``; then ``families_unconfirmed``. Empty when a config may be written."""
    out: list[dict] = []
    roles = data(project)["roles"]
    if "recon" not in roles:
        out.append({"code": "recon_missing", "role": None})
    for r in ROLES:
        e = roles.get(r)
        if not e:
            continue
        cls, host = e.get("address_class"), e.get("host")
        if cls in ("lan", "tailscale") and not has_optin(project, r, host):
            out.append({"code": "optin_missing", "role": r})
        if cls == "remote":
            if not https_ok(e.get("url") or ""):
                out.append({"code": "remote_needs_https", "role": r})
            prov = llm.ALLOWLIST_PROVIDER.get(e.get("provider"))
            if not prov or find_declaration(project, r, host, prov) is None:
                out.append({"code": "declaration_missing", "role": r})
        if cls not in ("loopback", "lan", "tailscale", "remote"):
            out.append({"code": "bad_url", "role": r})
        if is_cloud_model({"name": e.get("model")}):
            out.append({"code": "cloud_model", "role": r})
        if secrets is not None and e.get("kind") != "local" and not secrets.has(r):
            out.append({"code": "key_missing", "role": r})
        c = e.get("connection")
        if not isinstance(c, dict):
            out.append({"code": "connection_untested", "role": r})
        elif not c.get("ok"):
            out.append({"code": "connection_failed", "role": r})
        elif c.get("thinking_seen"):
            out.append({"code": "thinking_on", "role": r})
    if "recon" in roles and not families_confirmed(project):
        out.append({"code": "families_unconfirmed", "role": None})
    return out


def require_ready(project, secrets=None) -> None:
    """``api.ApiError(409, <code>, <message>)`` for the first of ``problems``; None when ready."""
    found = problems(project, secrets)
    if found:
        from . import strings
        from .api import ApiError
        code = found[0]["code"]
        raise ApiError(409, code, strings.t(f"models.error.{code}"))
