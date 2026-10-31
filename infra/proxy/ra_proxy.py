#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RA model proxy: a local OpenAI-compatible endpoint between dsh and the model provider.

In the paper's runs every dsh session talked to http://127.0.0.1:<port>/v1 with a dummy key, and this proxy forwarded
the requests to an OpenAI-compatible endpoint (OpenRouter, serving DeepSeek V4.1 Flash). The proxy is the only
process that holds the provider key; the agents never see it.

Requests. Only POST */chat/completions is served; GET */health answers {"ok": true, "port": ..., "nonce": ...}.
On the way up the proxy
  - replaces the client's Authorization header by "Bearer <key>", the key read once from RA_PROXY_KEY_FILE;
  - sets "model" to RA_PROXY_MODEL_FORCE when it is set (in proxy mode dsh sends the bare model id, for example
    deepseek-v4.1-flash, while the endpoint may need the full id, for example deepseek/deepseek-v4.1-flash);
  - with RA_PROXY_PROVIDER=<slug>, sets "provider" = {"only": [slug], "allow_fallbacks": false} (provider pinning in
    the OpenRouter request format); RA_PROXY_EXPECT_MODEL names the model id the answer must report (a mismatch is
    logged as ROUTE-MISMATCH, the answer is still passed on);
  - with RA_PROXY_INJECT_IDS=1 (the paper's setting) gives every message without a top-level "id" a stable
    content-hash id, msg_<sha1(sorted JSON of the message + NUL + occurrence index)[:24]>: the same message gets the
    same id in every request, so a replayed history serializes to the same bytes, and identical messages within one
    request get distinct ids. Some gateways reject messages without ids;
  - applies RA_PROXY_REWRITE (optional, see below);
  - serializes the body compactly (separators "," and ":", non-ASCII text kept as UTF-8).
On the way down (server-sent events) every line is forwarded byte for byte, with one repair: for each (choice index,
tool-call index) only the first non-empty tool-call "id" is passed on, and later "id" fields of that slot (repeats or
empty strings) are removed from the delta, because a client that overwrites the id with every delta would otherwise
end up with an empty id. Non-streamed answers and upstream errors (4xx, and 5xx after the retries) are passed back
unchanged.
Retries. 408/425/429/5xx and connection errors before the first byte of the answer are retried (gwretry.py).

RA_PROXY_REWRITE: items separated by ";": "path=value" (set), "path?=value" (set only when the field is present),
"-path" (delete). A path is a top-level field or a dotted path into nested objects. A value is parsed as JSON when it
is valid JSON, otherwise taken as a string. Example: "reasoning_effort?=high" for an endpoint that rejects another
value. The paper's runs used no rewrite.

Logs. One line per request on stderr. With RA_PROXY_USAGE_LOG, one JSON line per request: time, HTTP status,
attempts, latency, the model requested, sent and served, the provider, the system_fingerprint, the token counts the
answer reported (prompt, cached, completion, reasoning, total) and the raw usage object. The key is never logged.
This log is a cross-check; the paper's cost basis is the settlement of the dsh session files
(infra/accounting/settle_sessions.py).

Settings (environment variables):
  RA_PROXY_UPSTREAM       required: base URL of the endpoint, e.g. https://openrouter.ai/api/v1 (the proxy appends
                          /chat/completions)
  RA_PROXY_KEY_FILE       required: file holding the key (one token)
  RA_PROXY_PORT           listening port (default 8500)
  RA_PROXY_BIND           listening address (default 127.0.0.1)
  RA_PROXY_NONCE          string echoed by /health, so a launcher can tell its own proxy from a neighbour's
  RA_PROXY_MODEL_FORCE    model id sent upstream (default: the client's)
  RA_PROXY_PROVIDER       provider slug to pin (default: none)
  RA_PROXY_EXPECT_MODEL   model id the answer must report (default: not checked)
  RA_PROXY_INJECT_IDS     1 = inject message ids (default 0; the paper profile uses 1)
  RA_PROXY_REWRITE        field rewrites (default: none)
  RA_PROXY_USAGE_LOG      JSONL usage log (default: none); keep it outside the run folder
  RA_PROXY_TIMEOUT        seconds per upstream request (default 14400)
  GW_RETRY_*              retry policy, see gwretry.py
Standard library only; Python 3.6 compatible. Exit status: 2 configuration error, 3 cannot listen.
"""
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gwretry  # noqa: E402  (shared retry policy, next to this file)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class ConfigError(Exception):
    pass


CFG = {}                      # set by main() from the environment; read-only while serving
_USAGE_LOCK = threading.Lock()
_COUNTER_LOCK = threading.Lock()
_COUNTER = [0]


def _log(msg):
    """stderr, flushed at once (stderr redirected to a file is block-buffered on older Pythons)."""
    sys.stderr.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg)
    sys.stderr.flush()


def _next_request_no():
    with _COUNTER_LOCK:
        _COUNTER[0] += 1
        return _COUNTER[0]


# ---------------------------------------------------------------- configuration

def _parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_rewrite(spec):
    """'a=1;b.c?=x;-d' -> [("set", ["a"], 1), ("setif", ["b", "c"], "x"), ("del", ["d"], None)]."""
    ops = []
    for item in (spec or "").split(";"):
        item = item.strip()
        if not item:
            continue
        if item.startswith("-"):
            op, path, val = "del", item[1:], None
        else:
            i = item.find("=")
            if i <= 0:
                raise ConfigError("RA_PROXY_REWRITE item %r: expected path=value, path?=value or -path" % item)
            if item[i - 1] == "?":
                op, path = "setif", item[:i - 1]
            else:
                op, path = "set", item[:i]
            val = _parse_value(item[i + 1:].strip())
        keys = [k.strip() for k in path.strip().split(".")]
        if not keys or any(not k for k in keys):
            raise ConfigError("RA_PROXY_REWRITE item %r: empty field name" % item)
        ops.append((op, keys, val))
    return ops


def apply_rewrite(body, ops):
    """Apply parse_rewrite() operations to the request body in place; returns the number of changes."""
    n = 0
    for op, keys, val in ops:
        obj = body
        for k in keys[:-1]:
            nxt = obj.get(k) if isinstance(obj, dict) else None
            if not isinstance(nxt, dict):
                if op != "set" or not isinstance(obj, dict):
                    obj = None
                    break
                nxt = obj[k] = {}
            obj = nxt
        if not isinstance(obj, dict):
            continue
        last = keys[-1]
        if op == "del":
            if last in obj:
                del obj[last]
                n += 1
        elif op == "setif":
            if last in obj:
                obj[last] = val
                n += 1
        else:
            obj[last] = val
            n += 1
    return n


def _flag(env, name, default="0"):
    v = (env.get(name) or default).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off", ""):
        return False
    raise ConfigError("%s must be 0 or 1 (got %r)" % (name, v))


def load_config(env):
    """Settings from the environment (a mapping). Raises ConfigError; never echoes the key."""
    up = (env.get("RA_PROXY_UPSTREAM") or "").strip().rstrip("/")
    if not up:
        raise ConfigError("RA_PROXY_UPSTREAM is not set (base URL of an OpenAI-compatible endpoint, "
                          "e.g. https://openrouter.ai/api/v1)")
    if not (up.startswith("http://") or up.startswith("https://")):
        raise ConfigError("RA_PROXY_UPSTREAM must start with http:// or https://")
    parts = urllib.parse.urlsplit(up)
    if "@" in parts.netloc or parts.query or parts.fragment or not parts.hostname:
        raise ConfigError("RA_PROXY_UPSTREAM must be a plain base URL (no user:password@, query or fragment); "
                          "the key belongs in RA_PROXY_KEY_FILE")
    if up.endswith("/chat/completions"):
        up = up[:-len("/chat/completions")]
    kf = (env.get("RA_PROXY_KEY_FILE") or "").strip()
    if not kf:
        raise ConfigError("RA_PROXY_KEY_FILE is not set (a file holding the provider key)")
    try:
        with open(os.path.expanduser(kf), "r") as f:
            key = f.read().strip()
    except (IOError, OSError) as e:
        raise ConfigError("cannot read RA_PROXY_KEY_FILE %s (%s)" % (kf, e.__class__.__name__))
    if not key:
        raise ConfigError("RA_PROXY_KEY_FILE %s is empty" % kf)
    if any(c.isspace() for c in key):
        raise ConfigError("RA_PROXY_KEY_FILE %s must hold one token (no spaces or line breaks)" % kf)
    try:
        port = int(env.get("RA_PROXY_PORT") or "8500")
        timeout = float(env.get("RA_PROXY_TIMEOUT") or "14400")
    except ValueError:
        raise ConfigError("RA_PROXY_PORT and RA_PROXY_TIMEOUT must be numbers")
    if not 0 < port < 65536:
        raise ConfigError("RA_PROXY_PORT out of range: %d" % port)
    return {
        "upstream": up,
        "key": key,
        "port": port,
        "bind": (env.get("RA_PROXY_BIND") or "127.0.0.1").strip(),
        "nonce": env.get("RA_PROXY_NONCE") or "",
        "model_force": (env.get("RA_PROXY_MODEL_FORCE") or "").strip(),
        "provider": (env.get("RA_PROXY_PROVIDER") or "").strip(),
        "expect_model": (env.get("RA_PROXY_EXPECT_MODEL") or "").strip(),
        "inject_ids": _flag(env, "RA_PROXY_INJECT_IDS"),
        "rewrite": parse_rewrite(env.get("RA_PROXY_REWRITE") or ""),
        "rewrite_spec": (env.get("RA_PROXY_REWRITE") or "").strip(),
        "usage_log": (env.get("RA_PROXY_USAGE_LOG") or "").strip(),
        "timeout": timeout,
    }


# ---------------------------------------------------------------- request and stream transformations

def inject_ids(body):
    """Give every message without a top-level id a stable content-hash id; returns the number injected.

    The id is appended at the end of the message's key order and is deterministic, so the same history prefix
    serializes to the same bytes in every request."""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return 0
    seen = {}
    injected = 0
    for m in msgs:
        if not isinstance(m, dict) or "id" in m:
            continue
        basis = json.dumps(m, sort_keys=True, ensure_ascii=False)
        n = seen.get(basis, 0)
        seen[basis] = n + 1
        digest = hashlib.sha1((basis + u"\x00%d" % n).encode("utf-8")).hexdigest()
        m["id"] = "msg_" + digest[:24]
        injected += 1
    return injected


def scrub_chunk(chunk, state):
    """Repair one parsed SSE chunk: in tool-call deltas keep only the first non-empty id per slot.

    state: {(choice index, tool-call index): True} for the slots whose id was already passed.
    Returns True when the chunk was changed."""
    changed = False
    for choice in (chunk.get("choices") or []):
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        cidx = choice.get("index", 0)
        for tc in (delta.get("tool_calls") or []):
            if not isinstance(tc, dict) or "id" not in tc:
                continue
            key = (cidx, tc.get("index", -1))
            if tc["id"] and key not in state:
                state[key] = True          # first non-empty id of this slot: pass it on
            else:
                del tc["id"]               # empty or repeated: remove it
                changed = True
    return changed


def scrub_sse_line(line, state):
    """Repair one SSE line (bytes without the trailing newline); an unchanged line is returned as the same bytes.
    Also notes in `state` the served provider/model, the system_fingerprint and the last usage object."""
    core = line
    cr = b""
    if core.endswith(b"\r"):
        core = core[:-1]
        cr = b"\r"
    if not core.startswith(b"data:"):
        return line
    payload = core[5:]
    lead = b""
    if payload.startswith(b" "):
        lead = b" "
        payload = payload[1:]
    text = payload.decode("utf-8", "replace").strip()
    if not text or text == "[DONE]":
        return line
    try:
        chunk = json.loads(text)
    except Exception:
        return line
    if "served" not in state and isinstance(chunk, dict) and (chunk.get("provider") or chunk.get("model")):
        state["served"] = (chunk.get("provider"), chunk.get("model"))
    if "fp" not in state and isinstance(chunk, dict) and chunk.get("system_fingerprint"):
        state["fp"] = chunk.get("system_fingerprint")
    if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
        state["usage"] = chunk.get("usage")
    if not isinstance(chunk, dict) or not scrub_chunk(chunk, state):
        return line
    out = json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))
    return b"data:" + lead + out.encode("utf-8") + cr


def _int(x):
    return x if isinstance(x, int) and not isinstance(x, bool) else None


def norm_usage(u):
    """Token counts from an OpenAI-style usage object (OpenRouter and DeepSeek field names)."""
    if not isinstance(u, dict):
        return {"prompt_tokens": None, "cached_tokens": None, "completion_tokens": None,
                "reasoning_tokens": None, "total_tokens": None}
    ptd = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
    ctd = u.get("completion_tokens_details") if isinstance(u.get("completion_tokens_details"), dict) else {}
    cached = _int(ptd.get("cached_tokens"))
    if cached is None:
        cached = _int(u.get("prompt_cache_hit_tokens"))
    return {"prompt_tokens": _int(u.get("prompt_tokens")), "cached_tokens": cached,
            "completion_tokens": _int(u.get("completion_tokens")),
            "reasoning_tokens": _int(ctd.get("reasoning_tokens")), "total_tokens": _int(u.get("total_tokens"))}


def _norm_name(x):
    return "".join(ch for ch in (x or "").lower() if ch.isalnum())


def served_check(served, fp=None):
    """Log who answered; return True when the answer breaks the provider pin or the expected model."""
    if served:
        _log("[ra-proxy] <- served provider=%s model=%s%s\n" % (served[0], served[1], (" fp=%s" % fp) if fp else ""))
    else:
        _log("[ra-proxy] <- served: no provider/model field in the response\n")
    pin, expect = CFG.get("provider"), CFG.get("expect_model")
    bad = bool((pin and (not served or _norm_name(served[0]) != _norm_name(pin)))
               or (expect and (not served or served[1] != expect)))
    if bad:
        _log("[ra-proxy] ROUTE-MISMATCH pinned=%s expect_model=%s served=%r\n" % (pin or None, expect or "-", served))
    return bad


def write_usage(rec):
    path = CFG.get("usage_log")
    if not path:
        return
    line = json.dumps(rec, separators=(",", ":"), sort_keys=True)
    try:
        with _USAGE_LOCK:
            with open(path, "a") as f:
                f.write(line + "\n")
    except (IOError, OSError) as e:
        _log("[ra-proxy] usage log write failed: %s\n" % e)


# ---------------------------------------------------------------- HTTP handler

class Handler(BaseHTTPRequestHandler):
    server_version = "ra-proxy"

    def log_message(self, fmt, *a):
        _log("[ra-proxy] " + (fmt % a) + "\n")

    def _send_json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_raw(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype or "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("health"):
            return self._send_json(200, {"ok": True, "port": CFG["port"], "nonce": CFG["nonce"]})
        return self._send_json(404, {"error": {"message": "ra-proxy: POST */chat/completions"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("chat/completions"):
            return self._send_json(404, {"error": {"message": "ra-proxy: only chat/completions"}})
        t0 = time.time()
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)), "req": _next_request_no(), "status": None,
               "attempts": 0, "ms": None, "stream": None, "model_requested": None, "model_sent": None,
               "provider_pin": CFG["provider"] or None, "served_provider": None, "served_model": None,
               "system_fingerprint": None, "messages": None, "ids_injected": 0, "rewrites": 0, "bytes_up": None,
               "usage": None, "route_mismatch": False, "client_closed": False, "error": None}
        rec.update(norm_usage(None))
        try:
            self._relay(rec)
        except (BrokenPipeError, ConnectionResetError):
            rec["client_closed"] = True
        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)
            rec["status"] = rec["status"] or 500
            rec["error"] = "ra-proxy internal: %r" % (e,)
            try:
                self._send_json(500, {"error": {"message": "ra-proxy internal: %r" % (e,)}})
            except Exception:
                pass
        finally:
            rec["ms"] = int((time.time() - t0) * 1000)
            write_usage(rec)

    def _relay(self, rec):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = None
        if not isinstance(body, dict):
            rec["status"], rec["error"] = 400, "request body is not a JSON object"
            return self._send_json(400, {"error": {"message": "ra-proxy: request body is not a JSON object"}})
        rec["model_requested"] = body.get("model")
        rec["stream"] = bool(body.get("stream"))
        injected = inject_ids(body) if CFG["inject_ids"] else 0
        if CFG["model_force"]:
            body["model"] = CFG["model_force"]
        if CFG["provider"]:
            body["provider"] = {"only": [CFG["provider"]], "allow_fallbacks": False}
        rec["rewrites"] = apply_rewrite(body, CFG["rewrite"]) if CFG["rewrite"] else 0
        up_raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        nmsg = len(body.get("messages") or []) if isinstance(body.get("messages"), list) else 0
        rec.update({"model_sent": body.get("model"), "messages": nmsg, "ids_injected": injected, "bytes_up": len(up_raw)})
        _log("[ra-proxy] -> upstream: %d msgs, injected %d ids, %d bytes\n" % (nmsg, injected, len(up_raw)))
        headers = {
            "Authorization": "Bearer " + CFG["key"],
            "Content-Type": "application/json",
            "Accept": self.headers.get("Accept") or "text/event-stream",
        }
        ua = self.headers.get("User-Agent")
        if ua:
            headers["User-Agent"] = ua
        url = CFG["upstream"] + "/chat/completions"

        def _mk():
            return urllib.request.Request(url, data=up_raw, headers=headers)
        try:
            resp, att = gwretry.open_with_retry(_mk, log=_log, timeout=CFG["timeout"], tag="[ra-proxy]")
            rec["attempts"] = att
            if att > 1:
                _log("[ra-proxy] upstream ok after %d attempts\n" % att)
        except urllib.error.HTTPError as e:
            payload = e.read()
            rec.update({"status": e.code, "attempts": max(rec["attempts"], 1),
                        "error": payload[:300].decode("utf-8", "replace")})
            _log("[ra-proxy] upstream %d: %s\n" % (e.code, payload[:300].decode("utf-8", "replace")))
            return self._send_raw(e.code, e.headers.get("Content-Type") if e.headers else None, payload)
        except gwretry.RetryExhausted as e:
            last = e.last
            rec["attempts"] = e.attempts
            if isinstance(last, urllib.error.HTTPError):
                payload = last.read()
                rec.update({"status": last.code, "error": "retry exhausted: " + payload[:300].decode("utf-8", "replace")})
                _log("[ra-proxy] upstream %d after %d attempts (retry exhausted): %s\n"
                     % (last.code, e.attempts, payload[:300].decode("utf-8", "replace")))
                return self._send_raw(last.code, last.headers.get("Content-Type") if last.headers else None, payload)
            rec.update({"status": 502, "error": "upstream unreachable: %r" % (last,)})
            _log("[ra-proxy] upstream unreachable after %d attempts: %r\n" % (e.attempts, last))
            return self._send_json(502, {"error": {"message": "ra-proxy: upstream unreachable after %d attempts: %r"
                                                   % (e.attempts, last), "type": "gateway_retry_exhausted"}})
        status = getattr(resp, "status", 200)
        rec["status"] = status
        ctype = resp.headers.get("Content-Type", "")
        if "event-stream" not in ctype:
            data = resp.read()
            resp.close()
            try:
                j = json.loads(data.decode("utf-8", "replace"))
            except Exception:
                j = None
            if isinstance(j, dict):
                served = (j.get("provider"), j.get("model")) if (j.get("provider") or j.get("model")) else None
                rec.update({"served_provider": served[0] if served else None, "served_model": served[1] if served else None,
                            "system_fingerprint": j.get("system_fingerprint")})
                rec["route_mismatch"] = served_check(served, j.get("system_fingerprint"))
                if isinstance(j.get("usage"), dict):
                    rec["usage"] = j["usage"]
                    rec.update(norm_usage(j["usage"]))
            return self._send_raw(status, ctype, data)
        # server-sent events: line-buffered relay with the tool-call id repair
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        state = {}
        buf = b""
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                buf += chunk
                while True:
                    i = buf.find(b"\n")
                    if i < 0:
                        break
                    line = buf[:i]
                    buf = buf[i + 1:]
                    self.wfile.write(scrub_sse_line(line, state) + b"\n")
                self.wfile.flush()
            if buf:
                self.wfile.write(scrub_sse_line(buf, state))
                self.wfile.flush()
        finally:
            resp.close()
            served = state.get("served")
            rec.update({"served_provider": served[0] if served else None, "served_model": served[1] if served else None,
                        "system_fingerprint": state.get("fp")})
            rec["route_mismatch"] = served_check(served, state.get("fp"))
            if isinstance(state.get("usage"), dict):
                rec["usage"] = state["usage"]
                rec.update(norm_usage(state["usage"]))


def main():
    try:
        cfg = load_config(os.environ)
    except ConfigError as e:
        sys.stderr.write("ra_proxy: configuration error: %s\n" % e)
        return 2
    CFG.clear()
    CFG.update(cfg)
    if cfg["provider"] or cfg["expect_model"]:
        _log("[ra-proxy] provider pin: only=[%s] allow_fallbacks=false, expect model=%s\n"
             % (cfg["provider"] or "-", cfg["expect_model"] or "-"))
    try:
        srv = ThreadingHTTPServer((cfg["bind"], cfg["port"]), Handler)
    except (OSError, IOError) as e:
        _log("[ra-proxy] cannot listen on %s:%d: %s\n" % (cfg["bind"], cfg["port"], e))
        return 3
    _log("[ra-proxy] listening %s:%d -> %s/chat/completions (model force: %s, inject ids: %s, rewrite: %s)\n"
         % (cfg["bind"], cfg["port"], cfg["upstream"], cfg["model_force"] or "-", "on" if cfg["inject_ids"] else "off",
            cfg["rewrite_spec"] or "-"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
