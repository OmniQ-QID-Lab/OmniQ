"""Offline tests of infra/run/egress_proxy.py. Run: python3 -m unittest discover -s infra/run/tests -v"""
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
EGRESS = os.path.join(os.path.dirname(HERE), "egress_proxy.py")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Echo(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self, daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]

    def run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            data = c.recv(1024)
            c.sendall(data)
            c.close()


class Page(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"hello from origin"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestEgressProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.echo = Echo()
        cls.echo.start()
        cls.http = HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.port = free_port()
        cls.proc = subprocess.Popen([sys.executable, "-I", "-u", EGRESS, "--listen", "127.0.0.1:%d" % cls.port,
                                     "--allow", "127.0.0.1", "--allow", "*.example.org",
                                     "--allow-port", str(cls.echo.port), "--http-port", str(cls.http.server_address[1])],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        cls.proc.stderr.close()
        cls.http.shutdown()
        cls.echo.sock.close()

    def raw(self, request, then=None):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(request)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(1)
            if not chunk:
                break
            head += chunk
        out = b""
        if then is not None:
            s.sendall(then)
            out = s.recv(1024)
        else:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                out += chunk
        s.close()
        return head, out

    def test_connect_allowed_tunnel(self):
        head, out = self.raw(b"CONNECT 127.0.0.1:%d HTTP/1.1\r\nHost: x\r\n\r\n" % self.echo.port, then=b"ping")
        self.assertIn(b" 200 ", head.split(b"\r\n")[0])
        self.assertEqual(out, b"ping")

    def test_connect_denied(self):
        for target in (b"127.0.0.1:%d" % free_port(), b"files.pythonhosted.org:443", b"example.org:443"):
            head, out = self.raw(b"CONNECT " + target + b" HTTP/1.1\r\nHost: x\r\n\r\n")
            self.assertIn(b" 403 ", head.split(b"\r\n")[0], target)

    def test_wildcard(self):
        sys.path.insert(0, os.path.dirname(EGRESS))
        import egress_proxy as ep
        ep.OPTS["allow"] = ["*.example.org", "pypi.org"]
        self.assertTrue(ep.host_allowed("a.example.org"))
        self.assertTrue(ep.host_allowed("PyPI.org"))
        self.assertFalse(ep.host_allowed("example.org"))
        self.assertFalse(ep.host_allowed("evilexample.org"))
        self.assertFalse(ep.host_allowed("pypi.org.evil.net"))

    def test_plain_http(self):
        hp = self.http.server_address[1]
        head, out = self.raw(b"GET http://127.0.0.1:%d/x HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" % hp)
        self.assertIn(b" 200 ", head.split(b"\r\n")[0])
        self.assertEqual(out, b"hello from origin")
        head, out = self.raw(b"GET http://denied.invalid/ HTTP/1.1\r\nHost: denied.invalid\r\n\r\n")
        self.assertIn(b" 403 ", head.split(b"\r\n")[0])
        head, out = self.raw(b"POST http://127.0.0.1:%d/x HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n" % hp)
        self.assertIn(b" 405 ", head.split(b"\r\n")[0])


if __name__ == "__main__":
    unittest.main()
