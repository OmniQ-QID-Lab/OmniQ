#!/usr/bin/env python3
"""Allow-list forward proxy for outbound HTTPS (CONNECT) and plain HTTP. Standard library only, Python 3.6 or newer.

Usage:
    egress_proxy.py --listen HOST:PORT --allow HOST [--allow HOST ...] [--allow-port 443] [--http-port 80]
                    [--idle-timeout 3600] [--log FILE]
An --allow entry is a host name (exact, case-insensitive) or "*.example.org" (any subdomain of example.org).
  CONNECT host:port         allowed when the host is listed and the port is an --allow-port (default 443)
  GET/HEAD http://host/...  forwarded when the host is listed and the port is an --http-port (default 80)
Everything else is answered with 403 (405 for other plain-HTTP methods). Every decision is logged on stderr (and in
--log). Clients find the proxy through HTTPS_PROXY / HTTP_PROXY.

Uses in this repository:
  - compose.yaml (optional "egress" profile): pip may reach PyPI only (pypi.org, files.pythonhosted.org). This
    approximates the paper's limited internet access; it does not stop a program that opens sockets directly.
  - judge.sh: the judge container sits on a Docker network with no route out, and this proxy, attached to that
    network and to the default one, lets it reach the model API only.
"""
import argparse
import select
import socket
import sys
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit

OPTS = {"allow": [], "ports": {443}, "http_ports": {80}, "idle": 3600.0, "log": None}
_LOG_LOCK = threading.Lock()


def log(msg):
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n"
    with _LOG_LOCK:
        sys.stderr.write(line)
        sys.stderr.flush()
        if OPTS["log"]:
            with open(OPTS["log"], "a") as f:
                f.write(line)


def host_allowed(host):
    h = (host or "").lower().rstrip(".")
    for a in OPTS["allow"]:
        a = a.lower().rstrip(".")
        if a.startswith("*.") and (h.endswith(a[1:]) and len(h) > len(a) - 1):
            return True
        if h == a:
            return True
    return False


def split_host_port(hostport, default_port):
    hp = hostport.strip()
    if hp.startswith("["):                       # [ipv6]:port
        end = hp.find("]")
        host, rest = hp[1:end], hp[end + 1:]
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host, port
    if hp.count(":") == 1:
        host, port = hp.split(":")
        return host, int(port)
    return hp, default_port


def relay(a, b, idle):
    socks = [a, b]
    while True:
        r, _, x = select.select(socks, [], socks, idle)
        if x or not r:
            return
        for s in r:
            try:
                data = s.recv(65536)
            except OSError:
                return
            if not data:
                return
            (b if s is a else a).sendall(data)


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "ra-egress"

    def log_message(self, fmt, *a):
        pass

    def deny(self, code, what):
        log("DENY %s %s" % (self.command, what))
        body = ("egress proxy: %s is not on the allow list\n" % what).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        try:
            host, port = split_host_port(self.path, 443)
        except ValueError:
            return self.deny(400, self.path)
        if not host_allowed(host) or port not in OPTS["ports"]:
            return self.deny(403, "%s:%d" % (host, port))
        try:
            upstream = socket.create_connection((host, port), timeout=30)
        except OSError as e:
            log("FAIL CONNECT %s:%d: %s" % (host, port, e))
            self.send_error(502)
            return
        log("ALLOW CONNECT %s:%d" % (host, port))
        self.send_response(200, "Connection established")
        self.end_headers()
        self.wfile.flush()
        try:
            upstream.settimeout(None)
            relay(self.connection, upstream, OPTS["idle"])
        finally:
            upstream.close()
        self.close_connection = True

    def _plain(self, with_body):
        u = urlsplit(self.path)
        if u.scheme != "http" or not u.hostname:
            return self.deny(403, self.path[:200])
        port = u.port or 80
        if not host_allowed(u.hostname) or port not in OPTS["http_ports"]:
            return self.deny(403, "%s:%d" % (u.hostname, port))
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        hdrs = dict((k, v) for k, v in self.headers.items()
                    if k.lower() not in ("proxy-connection", "connection", "keep-alive", "proxy-authorization"))
        try:
            conn = HTTPConnection(u.hostname, port, timeout=60)
            conn.request(self.command, path, headers=hdrs)
            resp = conn.getresponse()
        except OSError as e:
            log("FAIL %s %s: %s" % (self.command, self.path[:200], e))
            self.send_error(502)
            return
        log("ALLOW %s %s -> %d" % (self.command, self.path[:200], resp.status))
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in ("connection", "transfer-encoding", "keep-alive"):
                self.send_header(k, v)
        self.end_headers()
        while with_body:
            data = resp.read(65536)
            if not data:
                break
            self.wfile.write(data)
        conn.close()

    def do_GET(self):
        self._plain(True)

    def do_HEAD(self):
        self._plain(False)

    def _not_allowed(self):
        self.deny(405, "%s %s" % (self.command, self.path[:200]))

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed


def main(argv=None):
    ap = argparse.ArgumentParser(description="Allow-list forward proxy (HTTPS CONNECT and plain HTTP GET/HEAD).")
    ap.add_argument("--listen", default="127.0.0.1:3128", help="HOST:PORT to listen on (default 127.0.0.1:3128)")
    ap.add_argument("--allow", action="append", default=[], help="allowed host (repeatable); *.domain for subdomains")
    ap.add_argument("--allow-port", action="append", type=int, default=[], help="CONNECT port (repeatable; default 443)")
    ap.add_argument("--http-port", action="append", type=int, default=[], help="plain-HTTP port (repeatable; default 80)")
    ap.add_argument("--idle-timeout", type=float, default=3600.0, help="close a tunnel idle this long (seconds)")
    ap.add_argument("--log", help="also append decisions to this file")
    a = ap.parse_args(argv)
    if not a.allow:
        sys.stderr.write("egress_proxy: give at least one --allow host\n")
        return 2
    OPTS.update({"allow": a.allow, "ports": set(a.allow_port or [443]), "http_ports": set(a.http_port or [80]),
                 "idle": a.idle_timeout, "log": a.log})
    host, port = split_host_port(a.listen, 3128)
    srv = Server((host, port), Handler)
    log("egress proxy listening %s:%d; allowed hosts: %s; CONNECT ports: %s"
        % (host, srv.server_address[1], ", ".join(a.allow), ",".join(str(p) for p in sorted(OPTS["ports"]))))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
