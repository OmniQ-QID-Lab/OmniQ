#!/usr/bin/env python3
"""Fault-injecting mock of an OpenAI-compatible endpoint, for the offline proxy tests. No network beyond 127.0.0.1.

As a program (used by test_retry.sh):
    MOCK_PORT=<port> MOCK_SEQ=502,drop,ok [MOCK_RECORD=<file.jsonl>] python3 mock_upstream.py
In-process (used by test_proxy.py):
    m = MockUpstream(seq=["502", "ok"]); m.start(); ... m.port, m.requests ...; m.stop()

Each POST takes the next action of the sequence; when the sequence is used up, the action is "ok" (a JSON answer, or
an SSE stream when the request asks for stream=true). Actions:
    ok        200 answer (SSE when the request has stream=true)
    oksse     200 SSE stream whatever the request says
    500 502 503  HTML error page with that status (retryable)
    perm502   502 whose body names an unknown provider (permanent: not retried)
    429       rate limited, Retry-After: 1
    drop      close the connection without an answer
    inspect   400 data_inspection_failed (retried a few times)
    bad400    400 that is not retried
Settings (attributes, or MOCK_* variables for the program): provider, model, fingerprint (fields of every answer;
empty = left out), usage ("asked": usage chunk only when stream_options.include_usage is set; "always"; "never"),
tool_ids ("clean": one tool-call id; "dup": the id repeated in a later delta; "empty": a later delta with "id": "").
Every request is recorded: path, the headers Authorization/Accept/User-Agent/Content-Type, and the raw body.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

USAGE = {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500,
         "prompt_tokens_details": {"cached_tokens": 1000}, "completion_tokens_details": {"reasoning_tokens": 120}}


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class MockUpstream(object):
    def __init__(self, seq=None, provider="MockProvider", model="mock/model-1", fingerprint="fpTEST123", usage="asked",
                 tool_ids="clean", record_file=None, port=0):
        self.seq = list(seq or [])
        self.provider, self.model, self.fingerprint = provider, model, fingerprint
        self.usage, self.tool_ids = usage, tool_ids
        self.record_file = record_file
        self.requests = []
        self.lock = threading.Lock()
        self.srv = _Server(("127.0.0.1", port), _make_handler(self))
        self.port = self.srv.server_address[1]
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def next_action(self):
        with self.lock:
            return self.seq.pop(0) if self.seq else "ok"

    def record(self, rec):
        with self.lock:
            self.requests.append(rec)
            if self.record_file:
                with open(self.record_file, "a") as f:
                    f.write(json.dumps(rec) + "\n")

    def base(self, obj):
        if self.provider:
            obj["provider"] = self.provider
        if self.model:
            obj["model"] = self.model
        if self.fingerprint:
            obj["system_fingerprint"] = self.fingerprint
        return obj


def _make_handler(mock):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj, extra=None):
            data = json.dumps(obj).encode()
            self.send_response(code)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.endswith("/models"):
                return self._json(200, {"object": "list", "data": [{"id": "mock/model-1"}]})
            return self._json(404, {"error": {"message": "not found"}})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            hdr = dict((k, self.headers.get(k)) for k in ("Authorization", "Accept", "User-Agent", "Content-Type")
                       if self.headers.get(k) is not None)
            mock.record({"path": self.path, "headers": hdr, "body": raw.decode("utf-8", "replace")})
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                body = {}
            act = mock.next_action()
            if act == "drop":
                self.connection.close()
                return
            if act == "perm502":
                return self._json(502, {"error": {"message": "unknown provider for model x"}})
            if act in ("500", "502", "503"):
                data = b"<html><body>Bad Gateway</body></html>"
                self.send_response(int(act))
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if act == "429":
                return self._json(429, {"error": {"message": "rate limited"}}, {"Retry-After": "1"})
            if act == "inspect":
                return self._json(400, {"error": {"code": "data_inspection_failed", "message": "inappropriate content"}})
            if act == "bad400":
                return self._json(400, {"error": {"message": "bad request (non-retryable)"}})
            if act == "oksse" or (act == "ok" and body.get("stream")):
                return self._sse(body)
            msg = {"role": "assistant", "content": "ok"}
            out = mock.base({"id": "gen-1", "object": "chat.completion",
                             "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]})
            if mock.usage != "never":
                out["usage"] = USAGE
            return self._json(200, out)

        def _sse(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def send(obj):
                self.wfile.write(b"data: " + json.dumps(mock.base(dict(obj, id="gen-1", object="chat.completion.chunk"))).encode() + b"\n\n")
                self.wfile.flush()
            send({"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]})
            send({"choices": [{"index": 0, "delta": {"content": "lo"}}]})
            send({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_X", "type": "function",
                                                                     "function": {"name": "f", "arguments": ""}}]}}]})
            later = {"index": 0, "function": {"arguments": "{}"}}
            if mock.tool_ids == "dup":
                later["id"] = "call_X"
            elif mock.tool_ids == "empty":
                later["id"] = ""
            send({"choices": [{"index": 0, "delta": {"tool_calls": [later]}, "finish_reason": "tool_calls"}]})
            asked = bool((body.get("stream_options") or {}).get("include_usage"))
            if mock.usage == "always" or (mock.usage == "asked" and asked):
                send({"choices": [], "usage": USAGE})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
    return H


def main():
    seq = [x for x in os.environ.get("MOCK_SEQ", "").split(",") if x]
    m = MockUpstream(seq=seq, usage=os.environ.get("MOCK_USAGE", "asked"),
                     tool_ids=os.environ.get("MOCK_TOOL_IDS", "clean"),
                     record_file=os.environ.get("MOCK_RECORD") or None, port=int(os.environ["MOCK_PORT"]))
    sys.stderr.write("[mock] listening 127.0.0.1:%d seq=%s\n" % (m.port, ",".join(seq)))
    sys.stderr.flush()
    m.srv.serve_forever()


if __name__ == "__main__":
    main()
