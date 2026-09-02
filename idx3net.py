#!/usr/bin/env python3
"""
idx3net - the speed-critical half of IDXAlert.

Everything here exists to shorten the gap between "IDX publishes" and
"the bytes are in our hands". Three ideas do most of the work:

  1. KEEP THE SOCKET OPEN.  IDXAlert 2.x opened a fresh TLS connection for
     every single poll: DNS + TCP + TLS is 200-400ms of pure overhead paid
     before the request is even sent. http.client can hold one connection
     open across hundreds of polls, so a burst-window tick costs only the
     server round-trip. It also means FEWER connections per day than 2.x
     despite ~6x the requests, which is what fingerprinting tends to key on.

  2. PRE-WARM BEFORE THE BOUNDARY.  A socket idle for 20 minutes is usually
     dead. Discovering that at 30:00.0 costs a reconnect exactly when it is
     most expensive. The scheduler calls prewarm() ~10s before each boundary
     so the first burst tick lands on a hot, proven connection.

  3. NEVER LET ONE REFUSAL COST AN ALERT.  Clients are tried in health order
     and a 403 puts that client in cooldown and immediately falls through to
     the next one within the SAME tick. Cloudflare blocking urllib should
     cost milliseconds, not a missed disclosure.

HTTP/1.1 only, and never "br" in Accept-Encoding - both are measured
constraints, see the project notes. http.client is HTTP/1.1 by construction,
which is a happy accident.
"""

import http.client
import json
import os
import shutil
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

HOST = "www.idx.co.id"
API_PATH = "/primary/ListedCompany/GetAnnouncement"
PAGE = "https://www.idx.co.id/id/perusahaan-tercatat/keterbukaan-informasi/"

# Identical to the 2.x header set, which is the one measured to pass Cloudflare.
# Do not "tidy" this. Do not add "br": the stdlib cannot inflate brotli and you
# get a 200 with an undecodable body, which reads like a JSON bug, not a network
# one. Connection: keep-alive is the one deliberate change from 2.x.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Referer": PAGE,
    "Origin": "https://www.idx.co.id",
    "sec-ch-ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
}


class Refused(Exception):
    """The server answered, and the answer was no (403/429). Cool this client."""
    def __init__(self, status, client):
        Exception.__init__(self, "HTTP %s" % status)
        self.status = status
        self.client = client


def _inflate(raw, encoding):
    encoding = (encoding or "").lower()
    if raw[:2] == b"\x1f\x8b" or "gzip" in encoding:
        import gzip
        return gzip.decompress(raw)
    if "deflate" in encoding:
        import zlib
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def _run_hidden(cmd, timeout=30):
    """No console flash. CREATE_NO_WINDOW alone is not always enough on
    Windows - some builds still flash unless SW_HIDE is set as well."""
    kwargs = {"capture_output": True, "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kwargs["startupinfo"] = si
    return subprocess.run(cmd, **kwargs)


# MEASURED 2026-09-01, from inside the IDX page's own origin:
#
#   indexFrom is a 0-BASED PAGE NUMBER, not a row offset.
#
# Proof: with pageSize=10, pages 0+1+2 concatenated are byte-identical to
# page 0 at pageSize=30, with zero overlap. indexFrom=1 does NOT mean "start
# at row 1".
#
# IDXAlert 1.x and 2.x both sent indexFrom=1, so both were reading PAGE TWO -
# rows 30-59 - and never saw the newest 30 disclosures at all. An item only
# became visible to them once enough newer filings had pushed it off page one,
# which is why alerts arrived hours late and always fired on some OTHER
# document than the one that had just been posted.
#
# Do not "tidy" this back to 1.
FIRST_PAGE = 0


def build_url(page_size, lang="id", emiten_type="*", keyword="",
              index_from=FIRST_PAGE):
    """The endpoint advertises Cache-Control: max-age=300 and offers no ETag,
    so a unique query string per request is the only way to defeat caches."""
    params = [
        ("indexFrom", int(index_from)),
        ("pageSize", int(page_size)),
        ("dateFrom", ""),
        ("dateTo", ""),
        ("lang", lang),
        ("keyword", keyword),
        ("emitenType", emiten_type),
        ("_", int(time.time() * 1000)),
    ]
    return API_PATH + "?" + urllib.parse.urlencode(params)


# --------------------------------------------------------------- clock skew

class Skew:
    """IDX stamps CreatedDate with ITS clock; we schedule bursts with OURS.

    A Windows box that has not synced in a while can sit 10-30s off, which is
    enough to miss the batch window entirely - the app would poll hard at what
    it thinks is :30 while IDX published at what we call :29:45.

    Every response carries a Date header. Subtracting the midpoint of our own
    request window (a crude but effective NTP) gives the offset. Date has only
    1-second resolution, so we keep a rolling median rather than trusting any
    single sample.
    """

    def __init__(self, keep=25):
        self.samples = []
        self.keep = keep
        self.lock = threading.Lock()

    def observe(self, date_header, sent_at, received_at):
        if not date_header:
            return
        try:
            server = parsedate_to_datetime(date_header).timestamp()
        except (TypeError, ValueError):
            return
        midpoint = sent_at + (received_at - sent_at) / 2.0
        with self.lock:
            self.samples.append(server - midpoint)
            del self.samples[:-self.keep]

    @property
    def offset(self):
        """Seconds to ADD to local time to get IDX time."""
        with self.lock:
            if not self.samples:
                return 0.0
            ordered = sorted(self.samples)
            mid = len(ordered) // 2
            if len(ordered) % 2:
                return ordered[mid]
            return (ordered[mid - 1] + ordered[mid]) / 2.0

    def now(self):
        return time.time() + self.offset

    @property
    def confident(self):
        return len(self.samples) >= 5


# ------------------------------------------------------------------ clients
# A client is just: given a path, return (body_text, date_header). Each has a
# different TLS/HTTP fingerprint, which is the point - if Cloudflare decides it
# dislikes one, the others are a different-looking visitor.


class KeepAliveClient:
    """The fast path. One TLS connection reused for hundreds of requests.

    http.client is unforgiving about a half-dead socket: the failure surfaces
    as BadStatusLine, RemoteDisconnected or CannotSendRequest rather than
    anything descriptive. All of those mean the same thing - throw the
    connection away and dial again - so they are handled identically, with one
    automatic retry on a fresh connection before the caller ever sees an error.
    """

    name = "keepalive"

    # A socket the server has quietly reaped looks fine until you write to it.
    # Recycling on our own schedule is cheaper than discovering it mid-burst.
    MAX_AGE = 240.0
    MAX_REQUESTS = 400
    IDLE_LIMIT = 45.0

    def __init__(self, timeout=12.0):
        self.timeout = timeout
        self.conn = None
        self.opened_at = 0.0
        self.last_used = 0.0
        self.count = 0
        self.lock = threading.Lock()
        # MEASURED 2026-09-01: this client was 403'd on Gill's machine while
        # urllib, with an identical header set, sailed through. The difference
        # was not HTTP at all - it was the TLS handshake.
        #
        # http.client only sets the ALPN extension when it builds the context
        # ITSELF (see HTTPSConnection.__init__: `if context is None: ...
        # context.set_alpn_protocols(['http/1.1'])`). Passing our own
        # ssl.create_default_context() skipped that block, so the ClientHello
        # advertised no ALPN whatsoever. Every real browser sends ALPN, so its
        # absence is about as loud a "not a browser" signal as a TLS
        # fingerprint carries - and Cloudflare reads it.
        #
        # So: build the context the same way, ALPN included, rather than
        # letting an innocent-looking default quietly unmask the client.
        self.ctx = ssl.create_default_context()
        try:
            self.ctx.set_alpn_protocols(["http/1.1"])
        except NotImplementedError:
            pass
        if getattr(self.ctx, "post_handshake_auth", None) is not None:
            self.ctx.post_handshake_auth = True

    def _stale(self):
        if self.conn is None:
            return True
        now = time.time()
        return (now - self.opened_at > self.MAX_AGE
                or now - self.last_used > self.IDLE_LIMIT
                or self.count >= self.MAX_REQUESTS)

    def _dial(self):
        self.close()
        self.conn = http.client.HTTPSConnection(
            HOST, timeout=self.timeout, context=self.ctx)
        self.conn.connect()
        self.opened_at = time.time()
        self.last_used = self.opened_at
        self.count = 0

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def prewarm(self):
        """Called before a boundary so the burst never pays for a handshake.
        Silent on failure - the next real request will dial anyway."""
        with self.lock:
            try:
                if self._stale():
                    self._dial()
                    return True
            except Exception:
                self.conn = None
            return False

    def get(self, path):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self._stale():
                        self._dial()
                    sent = time.time()
                    self.conn.request("GET", path, headers=HEADERS)
                    resp = self.conn.getresponse()
                    raw = resp.read()               # must drain to reuse the socket
                    got = time.time()
                    self.count += 1
                    self.last_used = got
                    if resp.status in (403, 429, 503):
                        self.close()                # do not reuse a refused socket
                        raise Refused(resp.status, self.name)
                    if resp.status != 200:
                        self.close()
                        raise RuntimeError("HTTP %s" % resp.status)
                    body = _inflate(raw, resp.headers.get("Content-Encoding"))
                    return (body.decode("utf-8", "replace"),
                            resp.headers.get("Date"), sent, got)
                except Refused:
                    raise
                except (http.client.HTTPException, OSError):
                    # Half-open socket, reaped by the server or a proxy. One
                    # silent retry on a brand-new connection, then give up and
                    # let the caller fall through to the next client.
                    self.close()
                    if attempt:
                        raise


class UrllibClient:
    """2.x's proven path. New connection each time, so it is the slow one, but
    it is also the one with the longest measured track record here."""

    name = "urllib"

    def __init__(self, timeout=15.0):
        self.timeout = timeout

    def prewarm(self):
        return False

    def close(self):
        pass

    def get(self, path):
        req = urllib.request.Request("https://" + HOST + path, headers=HEADERS)
        sent = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                got = time.time()
                body = _inflate(raw, resp.headers.get("Content-Encoding"))
                return (body.decode("utf-8", "replace"),
                        resp.headers.get("Date"), sent, got)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429, 503):
                raise Refused(exc.code, self.name)
            raise


class CurlClient:
    """curl.exe ships with Windows 10+. Slower (a process spawn is ~80-150ms)
    but a genuinely different TLS fingerprint, which is the whole value.
    --http1.1 is mandatory: HTTP/2 is refused outright."""

    name = "curl"

    def __init__(self, timeout=20.0):
        self.timeout = timeout
        self.exe = shutil.which("curl")

    def prewarm(self):
        return False

    def close(self):
        pass

    def get(self, path):
        if not self.exe:
            raise RuntimeError("curl not found")
        cmd = [self.exe, "-sS", "--compressed", "--http1.1",
               "-m", str(int(self.timeout)), "-D", "-", "-o", "-"]
        for k, v in HEADERS.items():
            if k != "Accept-Encoding":
                cmd += ["-H", "%s: %s" % (k, v)]
        cmd.append("https://" + HOST + path)
        sent = time.time()
        p = _run_hidden(cmd, timeout=self.timeout + 10)
        got = time.time()
        if p.returncode != 0:
            raise RuntimeError("curl exit %s: %s"
                               % (p.returncode, p.stderr.decode()[:120]))
        out = p.stdout.decode("utf-8", "replace")
        head, _, body = out.partition("\r\n\r\n")
        if not body:
            head, _, body = out.partition("\n\n")
        date = None
        status = 0
        for line in head.splitlines():
            if line.lower().startswith("http/"):
                try:
                    status = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
            elif line.lower().startswith("date:"):
                date = line.split(":", 1)[1].strip()
        if status in (403, 429, 503):
            raise Refused(status, self.name)
        return body, date, sent, got


class CurlCffiClient:
    """Optional (pip install curl_cffi). Replays Chrome's real TLS
    fingerprint, so it is the last line of defence if the others are refused."""

    name = "curl_cffi"

    def __init__(self, timeout=20.0):
        self.timeout = timeout

    def prewarm(self):
        return False

    def close(self):
        pass

    def get(self, path):
        from curl_cffi import requests as creq
        sent = time.time()
        r = creq.get("https://" + HOST + path, impersonate="chrome",
                     timeout=self.timeout)
        got = time.time()
        if r.status_code in (403, 429, 503):
            raise Refused(r.status_code, self.name)
        if r.status_code != 200:
            raise RuntimeError("HTTP %s" % r.status_code)
        return r.text, r.headers.get("Date"), sent, got


# ------------------------------------------------------------------ fetcher

class Health:
    """Per-client scoreboard. Cooldown is the important part: a client that
    just got a 403 is not merely 'unhealthy', it is actively counterproductive
    to keep poking, and hammering it is how a soft block becomes a hard one."""

    def __init__(self, client):
        self.client = client
        self.name = client.name
        self.ok = 0
        self.fail = 0
        self.refusals = 0
        self.cool_until = 0.0
        self.last_error = ""
        self.last_ms = None

    @property
    def available(self):
        return time.time() >= self.cool_until

    def score(self):
        """Lower sorts first. Refusals dominate, then recent failures, then
        measured latency - so the fastest healthy client naturally wins."""
        return (self.refusals * 10 + self.fail,
                self.last_ms if self.last_ms is not None else 9.9)

    @property
    def blocked(self):
        """Refused repeatedly, not just once. Worth demoting persistently."""
        return self.refusals >= 3

    def won(self, ms):
        self.ok += 1
        self.fail = 0
        self.last_ms = ms
        self.last_error = ""
        # One clean success is not proof the block is gone, but it is enough
        # to stop treating this client as radioactive.
        if self.refusals:
            self.refusals = max(0, self.refusals - 1)

    def lost(self, exc, cool=0.0):
        self.fail += 1
        self.last_error = str(exc)[:100]
        if cool:
            self.refusals += 1
            self.cool_until = time.time() + cool


class Fetcher:
    """Owns the clients, picks one per request, and records what happened.

    Ordering rule: the healthiest, fastest client goes first, and a Refused
    falls straight through to the next one inside the same call. A tick should
    never end in "no data" just because one fingerprint is out of favour.
    """

    def __init__(self, timeout=12.0, refusal_cooldown=300.0, log=None,
                 prefer=None, allow=None):
        self.log = log or (lambda _m: None)
        self.refusal_cooldown = refusal_cooldown
        self.skew = Skew()
        self.last_client = None
        self.last_ms = None
        self.last_bytes = 0

        candidates = [KeepAliveClient(timeout), UrllibClient(timeout + 3),
                      CurlClient(timeout + 8), CurlCffiClient(timeout + 8)]
        if allow:
            allow = set(allow)
            candidates = [c for c in candidates if c.name in allow] or candidates
        self.health = [Health(c) for c in candidates]
        self.by_name = {h.name: h for h in self.health}
        if prefer and prefer in self.by_name:
            # A remembered winner starts with a small head start, not immunity.
            self.by_name[prefer].ok = 1

    def prewarm(self):
        """Open/refresh sockets ahead of a boundary. Only the keep-alive client
        has anything to warm; the rest are no-ops by design."""
        warmed = []
        for h in self.health:
            if h.available:
                try:
                    if h.client.prewarm():
                        warmed.append(h.name)
                except Exception:
                    pass
        return warmed

    def order(self):
        usable = [h for h in self.health if h.available]
        if not usable:
            # Everything is cooling. Rather than sit out the batch entirely,
            # take the one closest to being allowed back and try it anyway.
            usable = [min(self.health, key=lambda h: h.cool_until)]
        return sorted(usable, key=lambda h: h.score())

    def get_json(self, page_size, lang="id", emiten_type="*",
                 index_from=FIRST_PAGE):
        """Returns the parsed payload. Raises only if EVERY client failed."""
        errors = []
        for h in self.order():
            # fresh cache-buster per attempt
            path = build_url(page_size, lang, emiten_type, "", index_from)
            try:
                body, date, sent, got = h.client.get(path)
                payload = json.loads(body)
                # A silently reshaped response is worse than a failure: it
                # would read as "zero disclosures today" forever. Treat a
                # missing Replies key as a hard error, same as 2.x.
                if "Replies" not in payload:
                    raise RuntimeError("unexpected response shape")
                ms = got - sent
                h.won(ms)
                self.skew.observe(date, sent, got)
                self.last_client = h.name
                self.last_ms = ms
                self.last_bytes = len(body)
                if errors:
                    self.log("fetched via %s after: %s"
                             % (h.name, "; ".join(errors)))
                return payload
            except Refused as exc:
                h.lost(exc, cool=self.refusal_cooldown)
                self.log("%s refused (HTTP %s) - cooling it for %ds, falling "
                         "through" % (h.name, exc.status, self.refusal_cooldown))
                errors.append("%s: HTTP %s" % (h.name, exc.status))
            except Exception as exc:
                h.lost(exc)
                errors.append("%s: %s" % (h.name, str(exc)[:70]))
        raise RuntimeError("every client failed - " + "; ".join(errors))

    def probe(self, rounds=8, page_size=10):
        """Measure what a single request actually costs, keep-alive vs fresh.

        This is the number the whole design turns on, so it is measurable
        rather than asserted: run `idx3.py --probe` and read it off.
        """
        out = {}
        for h in self.health:
            if not h.available:
                continue
            times = []
            for _ in range(rounds):
                try:
                    t0 = time.time()
                    h.client.get(build_url(page_size))
                    times.append(time.time() - t0)
                except Refused:
                    times = []
                    break
                except Exception:
                    pass
                time.sleep(0.4)
            if times:
                s = sorted(times)
                out[h.name] = {
                    "n": len(s),
                    "min": s[0],
                    "median": s[len(s) // 2],
                    "max": s[-1],
                    "first": times[0],       # includes the handshake
                }
        return out

    def close(self):
        for h in self.health:
            try:
                h.client.close()
            except Exception:
                pass
