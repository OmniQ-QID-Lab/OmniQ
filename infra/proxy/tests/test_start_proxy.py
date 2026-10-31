"""Offline tests of infra/proxy/start_proxy.sh (Linux: reads /proc). Run:
    python3 -m unittest discover -s infra/proxy/tests -p "test_*.py" -v
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
START = os.path.join(os.path.dirname(HERE), "start_proxy.sh")
sys.path.insert(0, HERE)
from mock_upstream import MockUpstream  # noqa: E402

KEY = "testkey-42"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@unittest.skipUnless(sys.platform.startswith("linux"), "start_proxy.sh is tested on Linux")
class TestStartProxy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ra_start_proxy_test_")
        self.key_file = os.path.join(self.tmp, "key")
        with open(self.key_file, "w") as f:
            f.write(KEY + "\n")
        self.mock = MockUpstream(usage="always").start()
        self.pids = []

    def tearDown(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.mock.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_start(self, state, extra_args=(), env_extra=None, port_range=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "UNRELATED_SECRET": "must-not-leak",
               "PYTHON_SENTINEL": "x"}
        env.update(env_extra or {})
        if port_range is None:
            lo = free_port()
            port_range = "%d-%d" % (lo, lo)
        args = ["bash", START, "--state-dir", state, "--upstream", "http://127.0.0.1:%d/v1" % self.mock.port,
                "--key-file", self.key_file, "--port-range", port_range, "--tries", "3",
                "--infra-flag", os.path.join(self.tmp, ".infra_failure")] + list(extra_args)
        r = subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if r.returncode == 0:
            with open(os.path.join(state, "pid")) as f:
                self.pids.append(int(f.read()))
        return r

    def test_success_and_isolation(self):
        state = os.path.join(self.tmp, "state")
        r = self.run_start(state, ["--inject-ids", "1", "--model-force", "deepseek/deepseek-v4.1-flash",
                                   "--usage-log", os.path.join(self.tmp, "usage.jsonl")])
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        port = int(r.stdout.decode().strip())
        self.assertEqual(r.stdout.decode().strip().splitlines(), [str(port)], "stdout holds the port only")
        with open(os.path.join(state, "nonce")) as f:
            nonce = f.read().strip()
        with open(os.path.join(state, "port")) as f:
            self.assertEqual(int(f.read()), port)
        h = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=5).read())
        self.assertEqual(h["nonce"], nonce)
        body = {"model": "deepseek-v4.1-flash", "stream": False, "messages": [{"role": "user", "content": "hi"}]}
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % port, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer shim-local"})
        self.assertEqual(urllib.request.urlopen(req, timeout=10).status, 200)
        rec = self.mock.requests[-1]
        self.assertEqual(rec["headers"]["Authorization"], "Bearer " + KEY)
        sent = json.loads(rec["body"])
        self.assertEqual(sent["model"], "deepseek/deepseek-v4.1-flash")
        self.assertTrue(sent["messages"][0]["id"].startswith("msg_"))
        pid = self.pids[-1]
        with open("/proc/%d/environ" % pid, "rb") as f:
            environ = f.read()
        self.assertNotIn(KEY.encode(), environ, "the key is never in the proxy's environment")
        self.assertNotIn(b"UNRELATED_SECRET", environ, "the proxy starts with a clean environment")
        self.assertIn(b"RA_PROXY_KEY_FILE=", environ)
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmdline = f.read().replace(b"\0", b" ")
        self.assertTrue(cmdline.startswith(os.path.join(state, ".ra_proxy_bin").encode()), cmdline)
        if b"python" not in START.encode():
            self.assertNotIn(b"python", cmdline, "an agent's pkill -f python3 must not match the proxy")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".infra_failure")))

    def test_missing_key_file(self):
        state = os.path.join(self.tmp, "state")
        os.remove(self.key_file)
        r = self.run_start(state)
        self.assertEqual(r.returncode, 66)
        self.assertEqual(r.stdout, b"")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, ".infra_failure")))

    def test_busy_port_is_skipped(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        p = s.getsockname()[1]
        try:
            r = self.run_start(os.path.join(self.tmp, "state"), port_range="%d-%d" % (p, p))
        finally:
            s.close()
        self.assertEqual(r.returncode, 66)
        self.assertIn(b"in use", r.stderr)

    def test_neighbour_proxy_never_adopted(self):
        p = free_port()
        r1 = self.run_start(os.path.join(self.tmp, "a"), port_range="%d-%d" % (p, p))
        self.assertEqual(r1.returncode, 0, r1.stderr.decode())
        self.assertEqual(int(r1.stdout), p)
        r2 = self.run_start(os.path.join(self.tmp, "b"), port_range="%d-%d" % (p, p))
        self.assertEqual(r2.returncode, 66, "the second launch must not take over the first launch's proxy")
        self.assertEqual(r2.stdout, b"")

    def test_bad_settings_fail_fast(self):
        r = self.run_start(os.path.join(self.tmp, "state"), ["--inject-ids", "yes"])
        self.assertEqual(r.returncode, 66)
        r = self.run_start(os.path.join(self.tmp, "state"), ["--rewrite", "novalue"])
        self.assertEqual(r.returncode, 66, "a proxy that exits at once (configuration error) is not accepted")
        self.assertIn(b"configuration error", r.stderr)

    def test_help(self):
        r = subprocess.run(["bash", START, "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"--state-dir", r.stdout)


if __name__ == "__main__":
    unittest.main()
