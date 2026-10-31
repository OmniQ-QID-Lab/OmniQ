"""Offline tests of infra/proxy/ra_proxy.py against the mock upstream (no network beyond 127.0.0.1).

Run: python3 -m unittest discover -s infra/proxy/tests -p "test_*.py" -v
"""
import ast
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(os.path.dirname(HERE), "ra_proxy.py")
RETRY = os.path.join(os.path.dirname(HERE), "gwretry.py")
sys.path.insert(0, HERE)
from mock_upstream import MockUpstream  # noqa: E402

KEY = "testkey-42"          # a dummy value; it must never appear in a log
CLIENT_KEY = "shim-local"   # what dsh sends in proxy mode
FAST_RETRY = {"GW_RETRY_BASE": "0.05", "GW_RETRY_CAP": "0.2", "GW_RETRY_BUDGET": "30"}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ProxyTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ra_proxy_test_")
        self.key_file = os.path.join(self.tmp, "key")
        with open(self.key_file, "w") as f:
            f.write(KEY + "\n")
        self.procs = []
        self.mocks = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
        for m in self.mocks:
            m.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mock(self, **kw):
        m = MockUpstream(**kw).start()
        self.mocks.append(m)
        return m

    def start_proxy(self, upstream_port, extra=None, wait=True):
        port = free_port()
        log = os.path.join(self.tmp, "proxy_%d.log" % port)
        usage = os.path.join(self.tmp, "usage_%d.jsonl" % port)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "RA_PROXY_UPSTREAM": "http://127.0.0.1:%d/v1" % upstream_port,
               "RA_PROXY_KEY_FILE": self.key_file, "RA_PROXY_PORT": str(port), "RA_PROXY_NONCE": "n-%d" % port,
               "RA_PROXY_USAGE_LOG": usage}
        env.update(FAST_RETRY)
        env.update(extra or {})
        env = dict((k, v) for k, v in env.items() if v is not None)
        lf = open(log, "w")
        p = subprocess.Popen([sys.executable, "-I", "-u", PROXY], env=env, stdout=lf, stderr=subprocess.STDOUT)
        lf.close()
        self.procs.append(p)
        if wait:
            for _ in range(100):
                if p.poll() is not None:
                    break
                try:
                    urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1).read()
                    break
                except Exception:
                    time.sleep(0.1)
        return p, port, log, usage

    @staticmethod
    def post(port, body, path="/v1/chat/completions", raw=None, timeout=20):
        data = raw if raw is not None else json.dumps(body).encode()
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                     headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                                              "Authorization": "Bearer " + CLIENT_KEY, "User-Agent": "dsh-test"})
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    @staticmethod
    def text(path):
        time.sleep(0.2)
        with open(path, errors="replace") as f:
            return f.read()

    @staticmethod
    def usage_lines(path):
        time.sleep(0.2)
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(x) for x in f if x.strip()]


BODY = {"model": "deepseek-v4.1-flash", "stream": True, "messages": [{"role": "user", "content": "hi"}]}


class TestBasics(ProxyTestBase):
    def test_health_nonce_and_404(self):
        m = self.mock()
        p, port, log, _ = self.start_proxy(m.port)
        h = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=5).read())
        self.assertEqual(h, {"ok": True, "port": port, "nonce": "n-%d" % port})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen("http://127.0.0.1:%d/v1/models" % port, timeout=5)
        self.assertEqual(cm.exception.code, 404)
        code, _ = self.post(port, BODY, path="/v1/embeddings")
        self.assertEqual(code, 404)
        code, out = self.post(port, None, raw=b"not json")
        self.assertEqual(code, 400)
        code, out = self.post(port, None, raw=b"[1, 2]")
        self.assertEqual(code, 400)
        self.assertEqual(m.requests, [], "nothing may reach the upstream for refused requests")

    def test_minimal_forwarding_and_key_swap(self):
        m = self.mock(tool_ids="dup")
        p, port, log, usage = self.start_proxy(m.port)
        code, out = self.post(port, BODY)
        self.assertEqual(code, 200)
        rec = m.requests[-1]
        self.assertEqual(rec["body"], json.dumps(BODY, ensure_ascii=False, separators=(",", ":")),
                         "without options the body is only re-serialized compactly")
        self.assertEqual(rec["headers"]["Authorization"], "Bearer " + KEY)
        self.assertEqual(rec["headers"]["Accept"], "text/event-stream")
        self.assertEqual(rec["headers"]["User-Agent"], "dsh-test")
        self.assertIn(b"Hel", out)
        self.assertIn(b"[DONE]", out)
        self.assertEqual(out.count(b'"call_X"'), 1, "a repeated tool-call id must be removed")

    def test_empty_tool_call_id_removed(self):
        m = self.mock(tool_ids="empty")
        p, port, log, usage = self.start_proxy(m.port)
        code, out = self.post(port, BODY)
        self.assertEqual(code, 200)
        self.assertEqual(out.count(b'"call_X"'), 1)
        self.assertNotIn(b'"id":""', out.replace(b" ", b""))

    def test_unchanged_lines_pass_byte_for_byte(self):
        m = self.mock(tool_ids="clean", usage="always")
        p, port, log, usage = self.start_proxy(m.port)
        code, out = self.post(port, BODY)
        lines = [x for x in out.split(b"\n") if x.startswith(b"data: {")]
        self.assertTrue(lines)
        for x in lines:
            self.assertIn(b'", "', x, "untouched lines keep the upstream's own spacing")


class TestRouting(ProxyTestBase):
    PIN = {"RA_PROXY_MODEL_FORCE": "deepseek/deepseek-v4.1-flash-20260910", "RA_PROXY_PROVIDER": "novita",
           "RA_PROXY_EXPECT_MODEL": "deepseek/deepseek-v4.1-flash"}

    def test_pin_force_and_served_line(self):
        m = self.mock(provider="Novita", model="deepseek/deepseek-v4.1-flash")
        p, port, log, usage = self.start_proxy(m.port, self.PIN)
        code, out = self.post(port, BODY)
        b = json.loads(m.requests[-1]["body"])
        self.assertEqual(b["provider"], {"only": ["novita"], "allow_fallbacks": False})
        self.assertEqual(b["model"], "deepseek/deepseek-v4.1-flash-20260910")
        t = self.text(log)
        self.assertIn("<- served provider=Novita model=deepseek/deepseek-v4.1-flash fp=fpTEST123", t)
        self.assertNotIn("ROUTE-MISMATCH", t)
        self.assertIn("provider pin: only=[novita]", t)
        u = self.usage_lines(usage)[-1]
        self.assertEqual((u["model_requested"], u["model_sent"], u["served_model"], u["served_provider"],
                          u["system_fingerprint"], u["provider_pin"], u["route_mismatch"]),
                         ("deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash-20260910", "deepseek/deepseek-v4.1-flash",
                          "Novita", "fpTEST123", "novita", False))

    def test_route_mismatch_logged(self):
        m = self.mock(provider="DeepSeek", model="deepseek/deepseek-v4.1-flash-1017")
        p, port, log, usage = self.start_proxy(m.port, self.PIN)
        code, out = self.post(port, BODY)
        self.assertEqual(code, 200, "a mismatch is logged, the answer is still passed on")
        self.assertIn("ROUTE-MISMATCH pinned=novita", self.text(log))
        self.assertTrue(self.usage_lines(usage)[-1]["route_mismatch"])

    def test_expect_model_without_pin(self):
        m = self.mock(provider="DeepSeek", model="deepseek/deepseek-v4.1-flash-1017")
        p, port, log, usage = self.start_proxy(m.port, {"RA_PROXY_EXPECT_MODEL": "deepseek/deepseek-v4.1-flash"})
        self.post(port, BODY)
        self.assertIn("ROUTE-MISMATCH pinned=None", self.text(log))
        self.assertNotIn("provider", json.loads(m.requests[-1]["body"]))

    def test_non_streamed_answer(self):
        m = self.mock(provider="Novita", model="deepseek/deepseek-v4.1-flash")
        p, port, log, usage = self.start_proxy(m.port, self.PIN)
        code, out = self.post(port, dict(BODY, stream=False))
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(out)["choices"][0]["message"]["content"], "ok")
        self.assertIn("<- served provider=Novita", self.text(log))
        u = self.usage_lines(usage)[-1]
        self.assertEqual((u["stream"], u["prompt_tokens"], u["cached_tokens"], u["completion_tokens"],
                          u["reasoning_tokens"], u["total_tokens"]), (False, 1200, 1000, 300, 120, 1500))


class TestTransforms(ProxyTestBase):
    def test_inject_ids(self):
        m = self.mock()
        p, port, log, usage = self.start_proxy(m.port, {"RA_PROXY_INJECT_IDS": "1"})
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
                {"role": "user", "content": "q"}, {"role": "assistant", "content": "a", "id": "keep_me"}]
        body = dict(BODY, messages=msgs)
        self.post(port, body)
        self.post(port, body)
        b1, b2 = [json.loads(r["body"]) for r in m.requests[-2:]]
        self.assertEqual(b1, b2, "ids are deterministic")
        ids = [x.get("id") for x in b1["messages"]]
        for i in ids[:3]:
            self.assertRegex(i, r"^msg_[0-9a-f]{24}$")
        self.assertNotEqual(ids[1], ids[2], "identical messages get distinct ids")
        self.assertEqual(ids[3], "keep_me")
        self.assertEqual(self.usage_lines(usage)[-1]["ids_injected"], 3)

    def test_no_injection_by_default(self):
        m = self.mock()
        p, port, log, usage = self.start_proxy(m.port)
        self.post(port, BODY)
        self.assertNotIn("id", json.loads(m.requests[-1]["body"])["messages"][0])

    def test_rewrite(self):
        m = self.mock()
        spec = "reasoning_effort?=high;-dsh_session_log;extra.depth=3;temperature?=0"
        p, port, log, usage = self.start_proxy(m.port, {"RA_PROXY_REWRITE": spec})
        self.post(port, dict(BODY, reasoning_effort="max", dsh_session_log={"x": 1}))
        b = json.loads(m.requests[-1]["body"])
        self.assertEqual(b["reasoning_effort"], "high")
        self.assertNotIn("dsh_session_log", b)
        self.assertEqual(b["extra"], {"depth": 3})
        self.assertNotIn("temperature", b, "?= sets only fields that are present")
        self.assertEqual(self.usage_lines(usage)[-1]["rewrites"], 3)

    def test_usage_log_streamed(self):
        m = self.mock(usage="always")
        p, port, log, usage = self.start_proxy(m.port)
        self.post(port, BODY)
        u = self.usage_lines(usage)[-1]
        self.assertEqual((u["status"], u["attempts"], u["stream"], u["prompt_tokens"], u["cached_tokens"],
                          u["completion_tokens"], u["reasoning_tokens"], u["total_tokens"]),
                         (200, 1, True, 1200, 1000, 300, 120, 1500))
        self.assertEqual(u["usage"]["prompt_tokens_details"], {"cached_tokens": 1000})
        self.assertIsInstance(u["ms"], int)

    def test_key_never_logged(self):
        m = self.mock(usage="always")
        p, port, log, usage = self.start_proxy(m.port, {"RA_PROXY_INJECT_IDS": "1"})
        self.post(port, BODY)
        self.post(port, dict(BODY, stream=False))
        health = urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=5).read().decode()
        for blob in (self.text(log), self.text(usage), health):
            self.assertNotIn(KEY, blob)


class TestRetries(ProxyTestBase):
    def run_seq(self, seq, stream=False, extra=None):
        m = self.mock(seq=seq)
        p, port, log, usage = self.start_proxy(m.port, extra)
        code, out = self.post(port, dict(BODY, stream=stream))
        return code, out, self.usage_lines(usage)[-1], self.text(log), m

    def test_transient_errors_then_ok(self):
        code, out, u, t, m = self.run_seq(["502", "drop", "429", "ok"])
        self.assertEqual((code, u["status"], u["attempts"]), (200, 200, 4))
        self.assertEqual(len(m.requests), 4)
        self.assertEqual(t.count("[gwretry]"), 3)

    def test_permanent_5xx_not_retried(self):
        code, out, u, t, m = self.run_seq(["perm502", "ok"])
        self.assertEqual((code, u["attempts"], len(m.requests)), (502, 1, 1))
        self.assertIn(b"unknown provider", out)

    def test_plain_4xx_passed_through(self):
        code, out, u, t, m = self.run_seq(["bad400", "ok"])
        self.assertEqual((code, len(m.requests)), (400, 1))
        self.assertIn(b"non-retryable", out)

    def test_retry_then_stream(self):
        code, out, u, t, m = self.run_seq(["503", "oksse"], stream=True)
        self.assertEqual(code, 200)
        self.assertIn(b"[DONE]", out)
        self.assertEqual(u["attempts"], 2)

    def test_retry_exhausted(self):
        code, out, u, t, m = self.run_seq(["502", "502", "502", "502"], extra={"GW_RETRY_MAX": "3"})
        self.assertEqual((code, u["attempts"], len(m.requests)), (502, 3, 3))
        self.assertIn("giving up after 3 attempts", t)

    def test_retry_disabled(self):
        code, out, u, t, m = self.run_seq(["502", "ok"], extra={"GW_RETRY": "0"})
        self.assertEqual((code, len(m.requests)), (502, 1))

    def test_upstream_unreachable(self):
        dead = free_port()
        p, port, log, usage = self.start_proxy(dead, {"GW_RETRY_MAX": "2"})
        code, out = self.post(port, BODY)
        self.assertEqual(code, 502)
        self.assertEqual(json.loads(out)["error"]["type"], "gateway_retry_exhausted")
        self.assertEqual(self.usage_lines(usage)[-1]["attempts"], 2)


class TestConfiguration(ProxyTestBase):
    def run_bad(self, extra):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "RA_PROXY_UPSTREAM": "http://127.0.0.1:9/v1",
               "RA_PROXY_KEY_FILE": self.key_file, "RA_PROXY_PORT": str(free_port())}
        env.update(extra)
        env = dict((k, v) for k, v in env.items() if v is not None)
        r = subprocess.run([sys.executable, "-I", "-u", PROXY], env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=20)
        return r.returncode, r.stdout.decode()

    def test_upstream_required_no_default(self):
        rc, out = self.run_bad({"RA_PROXY_UPSTREAM": None})
        self.assertEqual(rc, 2)
        self.assertIn("RA_PROXY_UPSTREAM is not set", out)
        rc, out = self.run_bad({"RA_PROXY_UPSTREAM": "ftp://x"})
        self.assertEqual(rc, 2)
        for url in ("https://user:secretpw@example.invalid/v1", "https://example.invalid/v1?key=abc", "https:///v1"):
            rc, out = self.run_bad({"RA_PROXY_UPSTREAM": url})
            self.assertEqual(rc, 2, url)
            self.assertNotIn("secretpw", out)

    def test_key_file_required(self):
        rc, out = self.run_bad({"RA_PROXY_KEY_FILE": None})
        self.assertEqual(rc, 2)
        rc, out = self.run_bad({"RA_PROXY_KEY_FILE": os.path.join(self.tmp, "missing")})
        self.assertEqual(rc, 2)
        empty = os.path.join(self.tmp, "empty")
        open(empty, "w").close()
        rc, out = self.run_bad({"RA_PROXY_KEY_FILE": empty})
        self.assertEqual(rc, 2)
        two = os.path.join(self.tmp, "two")
        with open(two, "w") as f:
            f.write("part-one secretpart\n")
        rc, out = self.run_bad({"RA_PROXY_KEY_FILE": two})
        self.assertEqual(rc, 2)
        self.assertNotIn("secretpart", out, "the key file's content is never echoed")

    def test_bad_settings(self):
        for extra in ({"RA_PROXY_REWRITE": "novalue"}, {"RA_PROXY_REWRITE": "=x"}, {"RA_PROXY_INJECT_IDS": "maybe"},
                      {"RA_PROXY_PORT": "99999"}, {"RA_PROXY_PORT": "x"}):
            rc, out = self.run_bad(extra)
            self.assertEqual(rc, 2, extra)

    def test_port_in_use(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            rc, out = self.run_bad({"RA_PROXY_PORT": str(s.getsockname()[1])})
        finally:
            s.close()
        self.assertEqual(rc, 3)

    def test_full_endpoint_url_accepted(self):
        m = self.mock()
        p, port, log, usage = self.start_proxy(m.port, {"RA_PROXY_UPSTREAM": "http://127.0.0.1:%d/v1/chat/completions" % m.port})
        code, out = self.post(port, BODY)
        self.assertEqual(code, 200)
        self.assertEqual(m.requests[-1]["path"], "/v1/chat/completions")


class TestSource(unittest.TestCase):
    def test_python36_grammar(self):
        """The proxy may run on a host whose python3 is old: no syntax newer than 3.6 (checked down to the oldest
        grammar this interpreter supports)."""
        for path in (PROXY, RETRY):
            with open(path) as f:
                src = f.read()
            for v in ((3, 6), (3, 7)):
                try:
                    ast.parse(src, feature_version=v)
                    break
                except ValueError:
                    continue      # this interpreter does not know that old grammar; try the next one
            self.assertNotRegex(src, r"(?m)^\s*from __future__ import annotations")
            self.assertIsNone(re.search(r":=", src), "no assignment expressions")

    def test_rewrite_parser(self):
        sys.path.insert(0, os.path.dirname(PROXY))
        import ra_proxy
        ops = ra_proxy.parse_rewrite(" a=1 ; b.c?=x;-d;e={\"k\": [1]}")
        self.assertEqual(ops, [("set", ["a"], 1), ("setif", ["b", "c"], "x"), ("del", ["d"], None),
                               ("set", ["e"], {"k": [1]})])
        body = {"b": {"c": "y"}, "d": 0}
        self.assertEqual(ra_proxy.apply_rewrite(body, ops), 4)
        self.assertEqual(body, {"a": 1, "b": {"c": "x"}, "e": {"k": [1]}})
        self.assertEqual(ra_proxy.apply_rewrite({"b": 5}, ra_proxy.parse_rewrite("b.c?=1")), 0)


if __name__ == "__main__":
    unittest.main()
