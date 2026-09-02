#!/usr/bin/env python3
"""
test_keepalive.py - proves the socket really is reused.

The whole 3.0 latency argument rests on one claim: after the first request,
a poll costs a round-trip and not a round-trip PLUS a TLS handshake. That is
easy to believe and easy to get wrong - drain the body incorrectly, or send a
header the server dislikes, and http.client quietly opens a new connection
every time while everything still "works".

IDX cannot be used as the test subject (it is a live third party and its IP is
not reachable from CI), so this stands a local HTTP server up instead and
counts how many TCP connections the server actually accepted. The transport is
plain HTTP rather than TLS, which is exactly the point: connection REUSE is
what is under test, and that is a property of HTTP/1.1 keep-alive, not of TLS.

Run:  python test_keepalive.py
"""

import http.client
import http.server
import json
import socket
import threading
import time

import idx3net

ACCEPTS = {"n": 0}


class CountingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        conn, addr = super().get_request()
        ACCEPTS["n"] += 1          # one bump per TCP connection, not per request
        return conn, addr


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"        # required for keep-alive
    served = {"n": 0}
    mode = {"refuse": False}

    def log_message(self, *_a):
        pass

    def do_GET(self):
        if self.mode["refuse"]:
            body = b"blocked"
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.served["n"] += 1
        payload = json.dumps({
            "ResultCount": 1,
            "Replies": [{
                "pengumuman": {"Id2": "k%d" % self.served["n"],
                               "Kode_Emiten": "AAA  ",
                               "JudulPengumuman": "t",
                               "TglPengumuman": "2026-09-01T00:00:00",
                               "CreatedDate": "2026-09-01T00:00:00"},
                "attachments": [],
            }],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class PlainConnection(http.client.HTTPConnection):
    """Stand-in for HTTPSConnection that speaks plain HTTP to the local server.

    This MUST be a real subclass, not a lambda returning a connection.

    An earlier version was a lambda and passed on Linux/3.10, where urllib does
    `h.set_debuglevel(...)` on the INSTANCE. On Windows/3.13 urllib reads
    `debuglevel` off the CLASS it was handed - and a function has no such
    attribute, so the urllib client died with "'function' object has no
    attribute 'debuglevel'" before it ever reached the server. The 403-failover
    assertions then failed for the wrong reason entirely.

    Subclassing keeps every class attribute urllib might reach for, on any
    version. Only the TLS layer is dropped, which is the point: connection
    REUSE is what is under test, and that is a property of HTTP/1.1 keep-alive,
    not of TLS.
    """

    def __init__(self, host, port=None, **kw):
        for tls_only in ("context", "check_hostname", "key_file", "cert_file"):
            kw.pop(tls_only, None)
        if kw.get("timeout") is None:
            kw.pop("timeout", None)          # None means "block forever", not "default"
        super().__init__(host, port, **kw)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    failures = []

    def check(name, cond, detail=""):
        print("  %-52s %s%s" % (name, "PASS" if cond else "FAIL",
                                "" if cond else "   <- " + str(detail)))
        if not cond:
            failures.append(name)

    port = free_port()
    srv = CountingServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # Point the client at the local server over plain HTTP. Only the transport
    # is swapped; every other line of KeepAliveClient is the shipping one.
    orig_host, orig_conn = idx3net.HOST, http.client.HTTPSConnection
    idx3net.HOST = "127.0.0.1:%d" % port
    http.client.HTTPSConnection = PlainConnection

    print("keep-alive test against 127.0.0.1:%d\n" % port)
    try:
        c = idx3net.KeepAliveClient(timeout=5)

        # Sanity-check the monkeypatch itself first. If urllib cannot reach
        # the stub server, every later assertion about failover is measuring
        # the harness rather than the code.
        probe = idx3net.UrllibClient(timeout=5)
        try:
            pbody, _, _, _ = probe.get(idx3net.build_url(5))
            patch_ok = json.loads(pbody)["ResultCount"] == 1
            patch_err = ""
        except Exception as exc:
            patch_ok, patch_err = False, str(exc)[:140]
        check("the HTTPSConnection stand-in works for urllib too",
              patch_ok, patch_err or "urllib could not use the patched class")
        # The probe above is a real request on a real connection. Zero the
        # counters so the reuse assertions count only what follows.
        ACCEPTS["n"] = 0
        Handler.served["n"] = 0

        body, date, sent, got = c.get(idx3net.build_url(5))
        check("first request succeeds", json.loads(body)["ResultCount"] == 1)
        check("Date header is returned for skew", bool(date), date)
        after_first = ACCEPTS["n"]

        for _ in range(24):
            c.get(idx3net.build_url(5))
        time.sleep(0.15)
        check("25 requests, still one connection", ACCEPTS["n"] == after_first,
              "%d connections for 25 requests" % ACCEPTS["n"])
        check("server saw all 25 requests", Handler.served["n"] == 25,
              Handler.served["n"])

        # A body left undrained poisons the socket; this is the usual bug.
        check("connection object survives reuse", c.conn is not None)
        check("request counter tracked", c.count == 25, c.count)

        # Skew: the server's Date is real, so the offset should be ~0.
        sk = idx3net.Skew()
        for _ in range(6):
            _, d, s0, g0 = c.get(idx3net.build_url(5))
            sk.observe(d, s0, g0)
        check("skew measured against a known-good clock",
              sk.confident and abs(sk.offset) < 2.0, sk.offset)

        # Recycling: force staleness and confirm a NEW connection is dialled.
        before = ACCEPTS["n"]
        c.opened_at = time.time() - (c.MAX_AGE + 1)
        c.get(idx3net.build_url(5))
        check("a stale connection is recycled, not reused",
              ACCEPTS["n"] == before + 1, (before, ACCEPTS["n"]))

        # Server drops the socket underneath us -> one silent retry, no error.
        c.conn.sock.close()
        try:
            c.get(idx3net.build_url(5))
            recovered = True
        except Exception as exc:
            recovered = False
            print("      %s" % exc)
        check("a dead socket is redialled transparently", recovered)

        # prewarm on a fresh client must open a connection before any request
        before = ACCEPTS["n"]
        c2 = idx3net.KeepAliveClient(timeout=5)
        opened = c2.prewarm()
        time.sleep(0.15)          # the server's accept loop is a separate thread
        check("prewarm opens the connection ahead of time",
              opened and ACCEPTS["n"] == before + 1, (opened, ACCEPTS["n"] - before))
        before = ACCEPTS["n"]
        c2.get(idx3net.build_url(5))
        time.sleep(0.15)
        check("the prewarmed socket is the one used",
              ACCEPTS["n"] == before, ACCEPTS["n"] - before)

        # 403 must surface as Refused so the Fetcher can cool this client.
        Handler.mode["refuse"] = True
        try:
            c.get(idx3net.build_url(5))
            refused = False
        except idx3net.Refused as exc:
            refused = exc.status == 403
        except Exception:
            refused = False
        check("a 403 raises Refused, not a generic error", refused)
        check("a refused connection is dropped, not reused", c.conn is None)

        # Fetcher-level failover: keepalive is blocked, urllib must take over.
        Handler.mode["refuse"] = False
        f = idx3net.Fetcher(timeout=5, refusal_cooldown=60,
                            allow=["keepalive", "urllib"], log=lambda m: None)
        Handler.mode["refuse"] = True
        try:
            f.get_json(5)
            got_data = True
        except Exception:
            got_data = False
        check("all clients blocked -> a clear failure, not a hang", not got_data)
        check("the blocked client went into cooldown",
              not f.by_name["keepalive"].available)
        check("every client that saw a 403 is cooling",
              all(not h.available for h in f.health),
              [(h.name, h.available) for h in f.health])
        Handler.mode["refuse"] = False
        # Both clients are still inside their cooldown here. The rule is that
        # a total block must not mean a total blackout: order() falls back to
        # whichever client is closest to being allowed back and tries it, so
        # the watcher recovers on its own rather than waiting out the full
        # cooldown with IDX already answering again.
        payload = f.get_json(5)
        check("recovery even while every client is still cooling",
              payload.get("ResultCount") == 1)
        check("recovery picks the client closest to available",
              f.last_client == "keepalive", f.last_client)

        # Selective case: only one client blocked, the other is healthy.
        f2 = idx3net.Fetcher(timeout=5, refusal_cooldown=60,
                             allow=["keepalive", "urllib"], log=lambda m: None)
        f2.by_name["keepalive"].lost(idx3net.Refused(403, "keepalive"), cool=60)
        f2.get_json(5)
        check("a single blocked client fails over to a healthy one",
              f2.last_client == "urllib", f2.last_client)
        f2.close()
        f.close()
        c.close()
        c2.close()
    finally:
        idx3net.HOST = orig_host
        http.client.HTTPSConnection = orig_conn
        srv.shutdown()

    print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
