# -*- coding: utf-8 -*-
"""Retry policy of the RA model proxy (ra_proxy.py). Standard library only, Python 3.6 compatible.
Import with sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))).

open_with_retry(make_request, log, timeout, tag) -> (resp, attempts)
  make_request(): returns a fresh urllib.request.Request each attempt (body bytes reused; safe because nothing
  has been relayed to the client before the first upstream byte arrives).
  Retries on: HTTP 408/425/429/500/502/503/504/520-524, connection errors (refused/reset/DNS), socket timeouts,
  RemoteDisconnected. Also retries HTTP 400 whose body says data_inspection_failed (a content-inspection 400 seen on
  some gateways, which often passes on a second try) up to GW_RETRY_INSPECT times. Other 4xx pass through
  immediately (raised as HTTPError to the caller). A 5xx whose body names a misnamed model or provider
  (PERMANENT_BODY) is not retried.
  Backoff: GW_RETRY_BASE s * 2^k with +-30 % jitter, capped at GW_RETRY_CAP s; Retry-After honoured (capped);
  stops after GW_RETRY_MAX attempts or when GW_RETRY_BUDGET s would be exceeded. Every retry is logged.
Env: GW_RETRY_MAX=8 GW_RETRY_BASE=2 GW_RETRY_CAP=90 GW_RETRY_BUDGET=900 GW_RETRY_INSPECT=3 GW_RETRY=0 disables.
"""
import os, time, random, socket, urllib.request, urllib.error, http.client

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
# 5xx bodies that are really permanent client-side mistakes (misnamed model, missing provider): never retry.
PERMANENT_BODY = (b"unknown provider", b"not a valid model", b"model_not_found", b"does not exist", b"no such model", b"unsupported model")
MAX = int(os.environ.get("GW_RETRY_MAX", "8"))
BASE = float(os.environ.get("GW_RETRY_BASE", "2"))
CAP = float(os.environ.get("GW_RETRY_CAP", "90"))
BUDGET = float(os.environ.get("GW_RETRY_BUDGET", "900"))
INSPECT = int(os.environ.get("GW_RETRY_INSPECT", "3"))
ENABLED = os.environ.get("GW_RETRY", "1") != "0"


class RetryExhausted(Exception):
    def __init__(self, last, attempts):
        Exception.__init__(self, "gateway retry exhausted after %d attempts: %r" % (attempts, last))
        self.last = last
        self.attempts = attempts


def _sleep_for(k, retry_after):
    d = min(CAP, BASE * (2 ** k))
    d = d * (0.7 + 0.6 * random.random())
    if retry_after:
        try:
            d = max(d, min(CAP, float(retry_after)))
        except ValueError:
            pass
    return d


def open_with_retry(make_request, log=None, timeout=3600, tag=""):
    """Returns (response, attempts). Raises urllib.error.HTTPError for non-retryable HTTP errors (body preserved),
    RetryExhausted when the budget/attempts run out (last exception/HTTPError in .last)."""
    log = log or (lambda s: None)
    t0 = time.time()
    inspect_left = INSPECT
    last = None
    k = 0
    attempt = 0
    while True:
        attempt += 1
        req = make_request()
        try:
            return urllib.request.urlopen(req, timeout=timeout), attempt
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if e.code == 400 and b"data_inspection_failed" in body and inspect_left > 0 and ENABLED:
                inspect_left -= 1
                d = 5.0
                log("[gwretry]%s attempt %d: 400 data_inspection_failed -> retry in %.0fs (%d left)\n" % (tag, attempt, d, inspect_left))
                time.sleep(d)
                last = _HTTPErrorWithBody(e, body)
                continue
            if e.code not in RETRY_STATUS or not ENABLED or any(m in body.lower() for m in PERMANENT_BODY):
                raise _HTTPErrorWithBody(e, body)
            last = _HTTPErrorWithBody(e, body)
            reason = "HTTP %d" % e.code
        except (urllib.error.URLError, socket.timeout, http.client.RemoteDisconnected,
                http.client.IncompleteRead, ConnectionError, OSError) as e:
            if not ENABLED:
                raise
            last = e
            retry_after = None
            reason = "%s: %s" % (type(e).__name__, str(e)[:120])
        if attempt >= MAX:
            log("[gwretry]%s giving up after %d attempts (%s)\n" % (tag, attempt, reason))
            raise RetryExhausted(last, attempt)
        d = _sleep_for(k, retry_after)
        if time.time() - t0 + d > BUDGET:
            log("[gwretry]%s budget %.0fs exhausted after %d attempts (%s)\n" % (tag, BUDGET, attempt, reason))
            raise RetryExhausted(last, attempt)
        log("[gwretry]%s attempt %d/%d: %s -> retry in %.1fs\n" % (tag, attempt, MAX, reason, d))
        time.sleep(d)
        k += 1


class _HTTPErrorWithBody(urllib.error.HTTPError):
    """HTTPError whose body was already read: .body holds it, .read() returns it again."""
    def __init__(self, e, body):
        urllib.error.HTTPError.__init__(self, e.url if hasattr(e, "url") else None, e.code, e.msg if hasattr(e, "msg") else "", e.headers, None)
        self.body = body

    def read(self, *a):
        return self.body
