#!/usr/bin/env python3
"""
IDXAlert - speed-first engine.

Target: no more than 60 seconds between IDX publishing a disclosure and this
process alerting on it. Note what that clock measures. A company files at
14:12; IDX publishes it in the 14:30 batch. The ~18 minutes in between is IDX's
own batching and no polling rate on earth shortens it. The 60 seconds is
publish -> alert, and idx3lat.py records both gaps separately so the two never
get confused.

How the budget is met:

  * The batch case (the large majority). IDX dumps 4-27 filings within a
    second or two of :00 and :30. 3.0 pre-warms its socket at B-10s and then
    polls once a second from B-3s to B+90s, so these are typically caught in
    under five seconds - not under sixty.

  * The off-cycle case. A minority publish at arbitrary times (a KSEI filing
    was measured at +13s from its own filing timestamp). Nothing clever helps
    here; only a short baseline interval does. 22s baseline => 22.4s worst
    case, which leaves comfortable headroom under the 60s budget even when a
    request is slow.

Everything else in the design is about not spending that budget carelessly:
sockets held open (idx3net), absolute tick targets that cannot drift
(idx3sched), and alerts dispatched to a queue BEFORE state is written or logs
are formatted, so no disk I/O sits between the bytes arriving and the alert
firing.

Standard library only. Runs alongside IDXAlert 2.x - separate folder, separate
config, separate seen.json. Do not point the two at the same data_dir.

  python idx3.py --plan       show the tick schedule and request budget
  python idx3.py --probe      measure real request latency, per client
  python idx3.py --init       seed state so the first run doesn't alert on old news
  python idx3.py --once       one poll, then exit
  python idx3.py              watch forever (the main mode)
  python idx3.py --stats      the latency report
  python idx3.py --selftest   offline harness, no network
"""

import argparse
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime, time as dtime

import idx3lat
import idx3net
import idx3sched

# The single place the version lives. Every window title, tray tooltip, About
# box and log line reads it from here. Internal identifiers (the .exe filename,
# the registry Run key, the mutex names) deliberately do NOT carry the version:
# bumping them would orphan an existing autostart entry and let a second copy
# run alongside the first.
VERSION = "3.3"
APP = "IDXAlert " + VERSION

if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(HERE, "config.json")
DATA_DIR = HERE
STATE_PATH = os.path.join(HERE, "seen.json")
LOG_DIR = os.path.join(HERE, "logs")
LATENCY_PATH = os.path.join(HERE, "latency.csv")

MAX_STATE = 6000
STATE_HEARTBEAT = 45.0      # seconds between liveness writes when nothing is new

DEFAULT_CONFIG = {
    "_comment": "IDXAlert. Keep this folder separate from any other copy "
                "- two watchers sharing one seen.json means duplicate alerts "
                "and twice the requests.",

    "_comment_poll": "Absolute tick grid, anchored to IDX's clock. Bursts run "
                     "from burst_lead_seconds BEFORE each :00/:30 until "
                     "burst_window_seconds after. baseline_interval_seconds is "
                     "the worst-case detection time for an off-cycle filing, so "
                     "it is the number that has to stay well under 60.",
    "poll": {
        "burst_lead_seconds": 3.0,
        "burst_window_seconds": 90.0,
        "burst_interval_seconds": 1.0,
        "baseline_interval_seconds": 22.0,
        "prewarm_lead_seconds": 10.0,
        "baseline_jitter_seconds": 0.4,
    },

    "_comment_page": "IDX batches 4-27 filings at once. page_size must exceed a "
                     "normal batch or we only see the newest few. If a page "
                     "comes back saturated (nearly everything on it is new) the "
                     "alert fires anyway and a second, larger fetch backfills "
                     "the rest - correctness never delays the notification.",
    "page_size": 30,
    "escalate_page_size": 100,

    "_comment_index_from": "indexFrom is a 0-based PAGE NUMBER, not a row "
                           "offset. Leave it at 0 - any other value fetches an "
                           "older page and alerts arrive late.",
    "index_from": 0,

    "_comment_freshness": "The feed's newest item should never be old during "
                          "trading hours. If it is, something upstream changed "
                          "(pagination, sort, a filter) and alerts are silently "
                          "stale. Warn rather than fail quietly. 0 disables.",
    "stale_feed_warn_minutes": 90,

    "_comment_clients": "Tried in health order, fastest first. A 403 cools that "
                        "client and falls through to the next one within the "
                        "same tick, so a block costs milliseconds, not an alert. "
                        "curl_cffi is optional (pip install curl_cffi).",
    "clients": ["keepalive", "urllib", "curl", "curl_cffi"],
    "request_timeout_seconds": 12.0,
    "refusal_cooldown_seconds": 300.0,

    "lang": "id",
    "emiten_type": "*",
    "data_dir": "",

    "_comment_filters": "Empty watchlist = every listed company. exclude_keywords "
                        "is left EMPTY by default on purpose: filtering changes "
                        "what reaches the latency log. Add exclusions once the "
                        "numbers are trusted.",
    "watchlist": [],
    "keywords": [],
    "exclude_keywords": [],

    "active_hours": {"start": "00:00", "end": "23:59",
                     "days": [0, 1, 2, 3, 4, 5, 6]},

    "_comment_channels": "The desktop popup is drawn by the tray app itself. "
                         "Telegram and email are optional side channels and go "
                         "out on the dispatcher thread, never the poll thread.",
    "channels": {
        "popup": {"enabled": True, "duration_seconds": 0, "max_visible": 3},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "email": {"enabled": False, "smtp_host": "smtp.gmail.com",
                  "smtp_port": 587, "username": "", "password": "", "to": []},
    },

    "start_with_windows": True,
    "latency_budget_seconds": 60.0,
    "log_routine_checks": False,
    "log_summary_minutes": 30,
    "log_retention_days": 0,
}


# --------------------------------------------------------------------- paths

def resolve_data_dir(raw):
    """First location we can actually WRITE to: the configured folder, then
    beside the app, then LocalAppData.

    The probe deliberately only requires a successful write. An earlier version
    also required the probe file to delete cleanly, which quietly exiled the
    whole app to LocalAppData on any filesystem that permits creating files but
    not unlinking them (network shares, sandboxed mounts, some sync folders).
    Losing the data folder is a much worse outcome than leaving one stray
    hidden byte behind, so cleanup is best-effort.
    """
    candidates = []
    if raw:
        candidates.append(os.path.expandvars(os.path.expanduser(raw)))
    candidates.append(HERE)
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    candidates.append(os.path.join(local, "IDXAlert3"))
    for path in candidates:
        probe = os.path.join(path, ".idx3_write_test")
        try:
            os.makedirs(path, exist_ok=True)
            with open(probe, "w") as fh:
                fh.write("x")
        except OSError:
            continue
        try:
            os.remove(probe)
        except OSError:
            pass
        return path
    return HERE


def _apply_data_dir(cfg):
    global DATA_DIR, STATE_PATH, LOG_DIR, LATENCY_PATH
    DATA_DIR = resolve_data_dir((cfg or {}).get("data_dir", ""))
    STATE_PATH = os.path.join(DATA_DIR, "seen.json")
    LATENCY_PATH = os.path.join(DATA_DIR, "latency.csv")
    logs = os.path.join(DATA_DIR, "logs")
    try:
        os.makedirs(logs, exist_ok=True)
        LOG_DIR = logs
    except OSError:
        LOG_DIR = DATA_DIR
    return DATA_DIR


def current_log_path(when=None):
    stamp = (when or datetime.now()).strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, "idx3-%s.log" % stamp)


_log_lock = threading.Lock()


def log(msg):
    """One file per calendar day. Rolls over on its own when the date changes -
    no rotation step, no restart."""
    now = datetime.now()
    line = "[%s] %s" % (now.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with _log_lock:
        try:
            path = current_log_path(now)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


# -------------------------------------------------------------- config/state

def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    """Writes a default config on first launch rather than exiting, so the
    folder can be copied somewhere else and just run."""
    if not os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(DEFAULT_CONFIG, fh, indent=2)
        except OSError:
            pass
        cfg = dict(DEFAULT_CONFIG)
        _apply_data_dir(cfg)
        log("no config.json - wrote defaults to %s" % CONFIG_PATH)
        return cfg
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = _merge(DEFAULT_CONFIG, json.load(fh))
    except (OSError, ValueError) as exc:
        log("config.json unreadable (%s) - running on defaults" % exc)
        cfg = dict(DEFAULT_CONFIG)
    _apply_data_dir(cfg)
    return cfg


def load_state():
    if not os.path.exists(STATE_PATH):
        return [], None
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return d.get("seen", []), d.get("client")
    except (OSError, ValueError):
        return [], None


def save_state(seen, client=None, stats=None):
    """Written atomically. Also the liveness heartbeat - the log is deliberately
    quiet, so `updated` here is how you tell a wedged process from an idle one."""
    payload = {
        "updated": datetime.now().isoformat(" ", "seconds"),
        "client": client,
        "stats": stats or {},
        "seen": list(seen)[-MAX_STATE:],
    }
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
        os.replace(tmp, STATE_PATH)
    except OSError as exc:
        log("could not write state: %s" % exc)


# ------------------------------------------------------------------- parsing

def parse(payload):
    items = []
    for reply in payload.get("Replies", []) or []:
        p = reply.get("pengumuman") or {}
        key = p.get("Id2")
        if not key:
            continue
        items.append({
            "key": key,
            # the API space-pads this to a fixed width; a watchlist match
            # against the raw value silently never fires
            "ticker": (p.get("Kode_Emiten") or "").strip(),
            "title": (p.get("JudulPengumuman") or "").strip(),
            "posted": (p.get("TglPengumuman") or "").replace("T", " "),
            "created": (p.get("CreatedDate") or "").replace("T", " "),
            "number": (p.get("NoPengumuman") or "").strip(),
            "files": [
                {"name": a.get("OriginalFilename") or a.get("PDFFilename"),
                 "url": a.get("FullSavePath")}
                for a in (reply.get("attachments") or [])
                if a.get("FullSavePath")
            ],
        })
    return items


def matches(item, cfg):
    watch = [t.upper() for t in cfg.get("watchlist") or []]
    if watch and item["ticker"].upper() not in watch:
        return False
    hay = (item["title"] + " " + item["ticker"]).lower()
    include = [k.lower() for k in cfg.get("keywords") or []]
    if include and not any(k in hay for k in include):
        return False
    for k in cfg.get("exclude_keywords") or []:
        if k and k.lower() in hay:
            return False
    return True


def in_active_hours(cfg):
    win = cfg.get("active_hours")
    if not win:
        return True
    now = datetime.now()
    if now.weekday() not in win.get("days", [0, 1, 2, 3, 4, 5, 6]):
        return False
    try:
        start = dtime(*map(int, win.get("start", "00:00").split(":")))
        end = dtime(*map(int, win.get("end", "23:59").split(":")))
    except ValueError:
        return True
    return start <= now.time() <= end


# ------------------------------------------------------------ side channels
# Telegram and email are straight lifts from 2.x. They live behind the
# dispatcher, never on the poll thread, because an SMTP handshake takes
# seconds and the burst grid budgets one second per tick.

def fmt_text(item):
    lines = ["%s  -  %s" % (item.get("ticker") or "IDX", item.get("posted", "")),
             item.get("title", "")]
    for f in (item.get("files") or [])[:6]:
        lines.append(f.get("url", ""))
    return "\n".join(lines)


def fmt_html(item):
    files = "".join('<li><a href="%s">%s</a></li>' % (f.get("url"), f.get("name"))
                    for f in (item.get("files") or []))
    return ("<p><b>%s</b> &middot; %s<br>%s</p><ul>%s</ul>"
            % (item.get("ticker") or "IDX", item.get("posted", ""),
               item.get("title", ""), files))


def send_telegram(conf, items):
    import urllib.request
    base = "https://api.telegram.org/bot%s/sendMessage" % conf["bot_token"]
    for item in items:
        body = urllib.parse.urlencode({
            "chat_id": conf["chat_id"],
            "text": fmt_text(item),
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(base, data=body)
        with urllib.request.urlopen(req, timeout=20):
            pass


def send_email(conf, items):
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = ("IDX: %d new disclosure%s"
                      % (len(items), "" if len(items) == 1 else "s"))
    msg["From"] = conf["username"]
    msg["To"] = ", ".join(conf["to"])
    msg.set_content("\n\n".join(fmt_text(i) for i in items))
    msg.add_alternative("".join(fmt_html(i) for i in items), subtype="html")
    with smtplib.SMTP(conf["smtp_host"], conf.get("smtp_port", 587),
                      timeout=30) as s:
        s.starttls()
        s.login(conf["username"], conf["password"])
        s.send_message(msg)


EXTERNAL_CHANNELS = {"telegram": send_telegram, "email": send_email}


def send_external(items, cfg):
    """Fan out to whatever is enabled in config. One failing channel must never
    take down the others, or the run."""
    for name, conf in (cfg.get("channels") or {}).items():
        # tolerate _comment_* keys and any other non-dict junk in config.json
        if name.startswith("_") or not isinstance(conf, dict):
            continue
        if not conf.get("enabled") or name not in EXTERNAL_CHANNELS:
            continue
        try:
            EXTERNAL_CHANNELS[name](conf, items)
            log("  sent via %s" % name)
        except Exception as exc:
            log("  %s FAILED: %s" % (name, str(exc)[:120]))


# ---------------------------------------------------------------- dispatcher

def _mins(seconds):
    if seconds is None:
        return "?"
    if seconds < 90:
        return "%.0fs" % seconds
    if seconds < 5400:
        return "%.0fm" % (seconds / 60.0)
    return "%.1fh" % (seconds / 3600.0)


def _gap(seconds):
    """Signed, compact: +48s / +3m48s / +12h50m."""
    if seconds is None:
        return "?"
    sign = "-" if seconds < 0 else "+"
    s = abs(int(seconds))
    if s < 60:
        return "%s%ds" % (sign, s)
    if s < 3600:
        return "%s%dm%02ds" % (sign, s // 60, s % 60)
    return "%s%dh%02dm" % (sign, s // 3600, (s % 3600) // 60)


def trail(item, detected_at):
    """The three timestamps and the two gaps, on one line.

        filed 14:31:12   published 14:35:00 (+3m48s idx)   alerted 14:36:02 (+1m02s)

    Both gaps matter and they belong to different people. filed -> published is
    IDX's; published -> alerted is what the feed made visible to us. Neither is
    the accountable number (that is `ours`, on the line above, the interval
    since the previous successful poll) but seeing all three is how you tell a
    slow app from a slow exchange without opening a spreadsheet.
    """
    filed = idx3lat._parse(item.get("posted"))
    pub = idx3lat._parse(item.get("created"))
    seen = datetime.fromtimestamp(detected_at)
    bits = []
    if filed:
        bits.append("filed %s" % filed.strftime("%H:%M:%S"))
    if pub:
        bits.append("published %s%s" % (
            pub.strftime("%H:%M:%S"),
            " (%s idx)" % _gap((pub - filed).total_seconds()) if filed else ""))
    if pub or filed:
        ref = pub or filed
        bits.append("alerted %s (%s)" % (seen.strftime("%H:%M:%S"),
                                         _gap((seen - ref).total_seconds())))
    return "   ".join(bits)


def fmt_alert(item, our_lag, publish_gap, verdict, detected_at=None):
    when = (item.get("created") or item.get("posted") or "")[-8:]
    lag = "" if our_lag is None else "  (ours +%.1fs)" % our_lag
    lines = ["  %-6s %s%s" % (item.get("ticker") or "IDX", when, lag),
             "         " + (item.get("title") or "")]
    if detected_at is not None:
        line = trail(item, detected_at)
        if line:
            lines.append("         " + line)
    if verdict == "late_visible":
        # Not a miss. IDX stamped CreatedDate long before it served the item.
        lines.append("         IDX served this %s after its CreatedDate, "
                     "not our delay" % _mins(publish_gap))
    for f in (item.get("files") or [])[:4]:
        lines.append("         " + (f.get("url") or ""))
    return "\n".join(lines)


class Dispatcher:
    """Alerts leave the poll thread through this queue and nothing else.

    The reason is latency, not tidiness. Formatting a log line, writing a CSV
    row and fsyncing seen.json are each only a few milliseconds, but they are
    milliseconds spent between "the bytes arrived" and "the user is told", and
    on the burst grid the poll thread has a one-second budget to be back at the
    scheduler. So the hot path does exactly one thing with a new disclosure -
    put() - and everything slower happens over here.

    It is also the seam where a real channel (popup, Telegram) drops in without
    touching a line of timing-sensitive code: add it to `channels`.
    """

    def __init__(self, recorder, budget=60.0, channels=None, cfg_provider=None):
        self.q = queue.Queue()
        self.cfg_provider = cfg_provider
        self.recorder = recorder
        self.budget = budget
        self.channels = list(channels or [])
        self.sent = 0
        self.over_budget = 0
        self.late_visible = 0
        self.worst = 0.0
        self._recent = {}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, items, ctx):
        """Called on the hot path. Must stay O(1) and never touch the disk."""
        if items:
            self.q.put((items, ctx, time.time()))

    def _duplicate(self, items):
        """Two copies of the app, or a retry crossing a save, both end as the
        same batch arriving twice. Suppress within a short window."""
        sig = tuple(sorted(i.get("key", "") for i in items))
        now = time.time()
        for old, when in list(self._recent.items()):
            if now - when > 180:
                self._recent.pop(old, None)
        if sig in self._recent:
            return True
        self._recent[sig] = now
        return False

    def _run(self):
        while True:
            try:
                items, ctx, detected_at = self.q.get()
            except Exception:
                continue
            try:
                self._handle(items, ctx, detected_at)
            except Exception as exc:
                log("dispatcher error: %s" % exc)

    def _handle(self, items, ctx, detected_at):
        if self._duplicate(items):
            log("  duplicate batch suppressed (%d item(s))" % len(items))
            return
        skew = ctx.get("skew", 0.0) or 0.0
        our_lag = ctx.get("our_lag")
        ctx = dict(ctx, budget=self.budget)
        log("%d new disclosure%s  [%s B%+.0fs via %s, %.0fms, ours +%s]"
            % (len(items), "" if len(items) == 1 else "s",
               ctx.get("phase", "?"), ctx.get("since_boundary", 0.0),
               ctx.get("client", "?"), (ctx.get("fetch_ms") or 0) * 1000,
               _mins(our_lag)))
        for item in items:
            gap, _ = idx3lat.gaps(item, detected_at, skew)
            verdict = idx3lat.classify(gap, our_lag, self.budget)
            log(fmt_alert(item, our_lag, gap, verdict, detected_at))
            if our_lag is not None:
                self.worst = max(self.worst, our_lag)
            if verdict == "slow":
                # OUR failure: we did not look for longer than the budget
                # allows. Nothing to do with what CreatedDate says.
                self.over_budget += 1
                log("         ** %.0fs since our previous successful poll - "
                    "OVER the %ds budget **" % (our_lag, int(self.budget)))
            elif verdict == "late_visible":
                self.late_visible += 1
            self.recorder.record(item, detected_at, ctx)
            self.sent += 1
        for ch in list(self.channels):
            try:
                ch(items, ctx)
            except Exception as exc:
                log("  channel %s failed: %s" % (getattr(ch, "__name__", ch), exc))
        if self.cfg_provider is not None:
            try:
                send_external(items, self.cfg_provider() or {})
            except Exception as exc:
                log("  external channels failed: %s" % str(exc)[:120])


# ------------------------------------------------------------------- watcher

class Watcher:
    def __init__(self, cfg):
        self.cfg = cfg
        seen, prefer = load_state()
        self.seen = list(seen)
        self.known = set(seen)
        self.fetcher = idx3net.Fetcher(
            timeout=float(cfg.get("request_timeout_seconds", 12.0)),
            refusal_cooldown=float(cfg.get("refusal_cooldown_seconds", 300.0)),
            log=log, prefer=prefer, allow=cfg.get("clients"))
        self.planner = idx3sched.Planner(cfg.get("poll"))
        self.recorder = idx3lat.Recorder(LATENCY_PATH)
        self.dispatcher = Dispatcher(
            self.recorder, float(cfg.get("latency_budget_seconds", 60.0)),
            cfg_provider=lambda: self.cfg)
        self.stop = threading.Event()
        # "Check now" and "Quit" must both cut a 22-second sleep short.
        # threading has no wait-on-any-of, and a watcher thread per sleep would
        # cost more than it saves, so both paths set one shared interrupt and
        # the loop asks afterwards which it was.
        self.wake = threading.Event()
        self._interrupt = threading.Event()
        self.paused = False
        self.last_poll_at = None
        self.last_error = None
        self.polls = 0
        self.failures = 0
        self.consecutive_failures = 0
        self._last_state_write = 0.0
        self._last_summary = time.time()
        self._summary_polls = 0
        # The instant of the previous SUCCESSFUL poll. An item absent then and
        # present now was delayed by us by at most this interval - which is the
        # only latency figure we can state without trusting IDX's clock.
        self.prev_poll_at = None
        self._stale_warned = 0.0
        self.feed_age_minutes = None

    def request_stop(self):
        self.stop.set()
        self._interrupt.set()

    def request_check(self):
        """Poll on the next tick of the loop rather than from the caller's
        thread - two threads polling at once would double-alert."""
        self.wake.set()
        self._interrupt.set()

    def set_paused(self, paused):
        self.paused = bool(paused)
        self._interrupt.set()

    # --------------------------------------------------------------- clock

    def idx_now(self):
        """Local time corrected onto IDX's clock. The burst grid is aimed with
        this, so a PC that has drifted still hits the batch."""
        return self.fetcher.skew.now()

    # ---------------------------------------------------------------- poll

    def poll(self, phase=idx3sched.BASELINE, seed_only=False):
        """One tick. Ordering here is the whole point - see Dispatcher."""
        page = int(self.cfg.get("page_size", 30))
        payload = self.fetcher.get_json(page, self.cfg.get("lang", "id"),
                                        self.cfg.get("emiten_type", "*"),
                                        int(self.cfg.get("index_from", 0)))
        detected_at = time.time()           # stamp BEFORE any parsing work
        items = parse(payload)
        if not items:
            raise RuntimeError("response parsed but contained no items")

        self._check_freshness(items)

        fresh = [i for i in items if i["key"] not in self.known]
        # oldest first, so a batch is reported in the order IDX published it
        fresh.reverse()

        if seed_only:
            self.prev_poll_at = detected_at
            self._remember(fresh)
            save_state(self.seen, self.fetcher.last_client, self.stats())
            return len(fresh), 0

        our_lag = (None if self.prev_poll_at is None
                   else detected_at - self.prev_poll_at)
        ctx = {
            "phase": phase,
            "since_boundary": self.planner.seconds_since_boundary(self.idx_now()),
            "client": self.fetcher.last_client,
            "fetch_ms": self.fetcher.last_ms,
            "page_size": page,
            "skew": self.fetcher.skew.offset,
            "our_lag": our_lag,
        }
        hits = [i for i in fresh if matches(i, self.cfg)]

        # THE HOT LINE. Everything after it is bookkeeping.
        self.dispatcher.submit(hits, ctx)

        self.prev_poll_at = detected_at

        self._remember(fresh)
        if fresh:
            save_state(self.seen, self.fetcher.last_client, self.stats())
            self._last_state_write = time.time()
        elif time.time() - self._last_state_write > STATE_HEARTBEAT:
            save_state(self.seen, self.fetcher.last_client, self.stats())
            self._last_state_write = time.time()

        # A page that came back almost entirely new means the batch was bigger
        # than the page - there are filings we have not seen. The alert for
        # what we DID see has already gone out, so the backfill costs latency
        # for nobody.
        if len(fresh) >= page - 1:
            self._escalate(ctx)

        return len(fresh), len(hits)

    def _remember(self, fresh):
        for i in fresh:
            if i["key"] not in self.known:
                self.known.add(i["key"])
                self.seen.append(i["key"])
        if len(self.seen) > MAX_STATE:
            drop = self.seen[:-MAX_STATE]
            self.seen = self.seen[-MAX_STATE:]
            self.known.difference_update(drop)

    def _escalate(self, ctx):
        big = int(self.cfg.get("escalate_page_size", 100))
        log("page saturated - refetching %d items to backfill the batch" % big)
        try:
            payload = self.fetcher.get_json(big, self.cfg.get("lang", "id"),
                                            self.cfg.get("emiten_type", "*"),
                                            int(self.cfg.get("index_from", 0)))
            detected_at = time.time()
            items = parse(payload)
            extra = [i for i in items if i["key"] not in self.known]
            extra.reverse()
            if extra:
                ctx = dict(ctx, page_size=big, phase=ctx.get("phase"),
                           client=self.fetcher.last_client,
                           fetch_ms=self.fetcher.last_ms)
                self.dispatcher.submit([i for i in extra if matches(i, self.cfg)],
                                       ctx)
                self._remember(extra)
                save_state(self.seen, self.fetcher.last_client, self.stats())
        except Exception as exc:
            log("backfill failed: %s" % str(exc)[:140])


    def _check_freshness(self, items):
        """Is the feed we are reading actually the current one?

        This exists because of a bug that hid for two versions: `indexFrom=1`
        was fetching page TWO, so the watcher never saw the newest 30 filings
        and every alert was hours stale. Nothing errored - the responses were
        perfectly valid, just the wrong slice - and the only visible symptom
        was alerts naming the wrong document.

        A single cheap invariant catches that whole class of failure: the
        newest thing in the feed should not be old. If it is, say so loudly
        rather than continuing to report confidently on a stale window.
        """
        limit = float(self.cfg.get("stale_feed_warn_minutes", 90) or 0)
        if not limit or not items:
            return
        newest = max((i.get("posted") or "") for i in items)
        stamp = idx3lat._parse(newest)
        if stamp is None:
            return
        age = (datetime.now() - stamp).total_seconds() / 60.0
        self.feed_age_minutes = age
        if age <= limit:
            self._stale_warned = 0.0
            return
        if time.time() - self._stale_warned < 600:
            return
        self._stale_warned = time.time()
        log("WARNING: the newest item in the feed is %.0f min old (%s). "
            "Either IDX has published nothing recently, or we are reading the "
            "wrong slice - check index_from (must be 0) and page_size, and run "
            "'python idx3.py --verify-feed'." % (age, newest))

    def verify_feed(self):
        """Live check that page 0 really is the newest page.

        Two requests. If page 1's newest item is not older than page 0's, our
        understanding of `indexFrom` is wrong and every alert is suspect.
        """
        size = int(self.cfg.get("page_size", 30))
        lang = self.cfg.get("lang", "id")
        et = self.cfg.get("emiten_type", "*")
        p0 = parse(self.fetcher.get_json(size, lang, et, 0))
        p1 = parse(self.fetcher.get_json(size, lang, et, 1))
        if not p0 or not p1:
            return False, "one of the pages came back empty"
        n0 = max(i["posted"] for i in p0)
        n1 = max(i["posted"] for i in p1)
        keys0, keys1 = {i["key"] for i in p0}, {i["key"] for i in p1}
        overlap = len(keys0 & keys1)
        ok = n0 > n1 and overlap == 0
        detail = ("page 0 newest %s\n  page 1 newest %s\n  overlap %d item(s)"
                  % (n0, n1, overlap))
        if not ok:
            detail += ("\n  -> indexFrom does NOT behave as a 0-based page "
                       "number here. Alerts cannot be trusted until this is "
                       "understood.")
        else:
            age = (datetime.now() - idx3lat._parse(n0)).total_seconds() / 60.0
            detail += ("\n  page 0 is the newest page, pages do not overlap"
                       "\n  newest item is %.0f min old" % age)
        return ok, detail

    def stats(self):
        return {
            "polls": self.polls,
            "failures": self.failures,
            "alerts": self.dispatcher.sent,
            "over_budget": self.dispatcher.over_budget,
            "late_visible": self.dispatcher.late_visible,
            "worst_our_lag_s": round(self.dispatcher.worst, 1),
            "clock_skew_s": round(self.fetcher.skew.offset, 2),
            "feed_age_min": (None if self.feed_age_minutes is None
                             else round(self.feed_age_minutes)),
            "client": self.fetcher.last_client,
            "fetch_ms": None if self.fetcher.last_ms is None
                        else round(self.fetcher.last_ms * 1000),
        }

    def backoff(self):
        """Everything is being refused. Slowing down is the only move that can
        actually help; hammering turns a soft block into a hard one."""
        if self.consecutive_failures <= 0:
            return 0.0
        return min(3600.0, 5.0 * (2 ** min(self.consecutive_failures, 10)))  # cap 1h: a 10-min retry kept the Cloudflare block alive

    # ---------------------------------------------------------------- loop

    def run(self):
        p = self.planner
        log("%s watching IDX" % APP)
        log("  burst   %.0fs before -> %.0fs after each :00/:30, every %.1fs"
            % (p.lead, p.window, p.fast))
        log("  baseline %.0fs  (worst-case off-cycle detection %.1fs, budget %ds)"
            % (p.base, p.worst_case_detection(),
               int(self.cfg.get("latency_budget_seconds", 60))))
        log("  ~%d requests/hour   clients: %s"
            % (p.requests_per_hour(),
               ", ".join(h.name for h in self.fetcher.health)))
        log("  data: %s" % DATA_DIR)

        while not self.stop.is_set():
            due, kind = p.next_event(self.idx_now())
            # sleep_until runs on the IDX clock too, so a skew correction
            # mid-sleep re-aims the burst instead of missing it
            if not idx3sched.sleep_until(due, self.idx_now, self._interrupt):
                if self.stop.is_set():
                    break
                self._interrupt.clear()
                if self.wake.is_set():
                    self.wake.clear()
                    kind = idx3sched.BASELINE      # a manual "Check now"
                else:
                    continue                        # pause/resume - re-plan
            elif kind == idx3sched.PREWARM:
                warmed = self.fetcher.prewarm()
                if warmed and self.cfg.get("log_routine_checks"):
                    log("prewarmed: %s" % ", ".join(warmed))
                continue

            try:
                # re-read config every tick so Options changes apply with no
                # restart, exactly as 2.x did
                self.cfg = load_config()
                # load_config re-resolves data_dir, so seen.json and the log
                # follow a folder change immediately. The recorder holds its
                # path from construction, so it has to be told.
                if self.recorder.path != LATENCY_PATH:
                    log("latency log moved to %s" % LATENCY_PATH)
                    self.recorder = idx3lat.Recorder(LATENCY_PATH)
            except Exception:
                pass

            if self.paused or not in_active_hours(self.cfg):
                continue

            try:
                fresh, hits = self.poll(phase=kind)
                self.polls += 1
                self._summary_polls += 1
                self.consecutive_failures = 0
                self.last_poll_at = time.time()
                self.last_error = None
                if self.cfg.get("log_routine_checks"):
                    log("%s tick: %d new, %d matched, %.0fms via %s"
                        % (kind, fresh, hits, (self.fetcher.last_ms or 0) * 1000,
                           self.fetcher.last_client))
            except Exception as exc:
                self.failures += 1
                self.consecutive_failures += 1
                self.last_error = str(exc)[:200]
                log("ERROR: %s" % str(exc)[:400])
                wait = self.backoff()
                if wait:
                    log("backing off %.0fs after %d consecutive failures"
                        % (wait, self.consecutive_failures))
                    if self.stop.wait(wait):
                        break

            self._heartbeat()

        self.fetcher.close()

    def _heartbeat(self):
        """The log stays quiet on purpose, so it needs one periodic line that
        proves the process is alive and says whether the budget is holding."""
        every = float(self.cfg.get("log_summary_minutes", 30) or 0) * 60.0
        if not every or time.time() - self._last_summary < every:
            return
        s = self.stats()
        log("still running - %d checks in %.0f min, %d alert(s), %d over budget, "
            "%d late-visible, worst ours %.1fs, skew %+.1fs, %s %sms"
            % (self._summary_polls, every / 60.0, s["alerts"], s["over_budget"],
               s["late_visible"], s["worst_our_lag_s"], s["clock_skew_s"],
               s["client"] or "-", s["fetch_ms"]))
        self._summary_polls = 0
        self._last_summary = time.time()


# ------------------------------------------------------------------- selftest

def _fake_payload(n, start=0, when=None, ticker="TEST"):
    """A response shaped exactly like IDX's, so the parser is exercised for
    real rather than against a convenient stub."""
    when = when or datetime.now()
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S")
    replies = []
    for i in range(n):
        idx = start + i
        replies.append({
            "pengumuman": {
                "Id2": "2026090100000%d-%03d/ECD-TEST/IX/2026_id-id" % (idx, idx),
                # deliberately space-padded, exactly as the live API does
                "Kode_Emiten": "%-5s" % ("%s%d" % (ticker, idx % 10)),
                "JudulPengumuman": "Penyampaian Laporan Test nomor %d" % idx,
                "TglPengumuman": stamp,
                "CreatedDate": stamp,
                "NoPengumuman": "%03d/TEST" % idx,
            },
            "attachments": [
                {"OriginalFilename": "test_%d.pdf" % idx,
                 "FullSavePath": "https://example.invalid/test_%d.pdf" % idx},
            ],
        })
    return {"ResultCount": n, "Replies": replies}


_re_version = __import__("re").compile(r"IDXAlert\s+\d+\.\d")


def selftest():
    """Offline end-to-end harness. No network, no clock waiting.

    Exists because the expensive failures in 2.x were not network failures -
    they were a rewrite that silently deleted a helper, and a headless test
    that passed vacuously. So this exercises the real poll path with the real
    parser and asserts on observable outcomes, not on 'it imported'.
    """
    import shutil
    import tempfile

    global CONFIG_PATH, DATA_DIR, STATE_PATH, LOG_DIR, LATENCY_PATH
    tmp = tempfile.mkdtemp(prefix="idx3test-")
    CONFIG_PATH = os.path.join(tmp, "config.json")
    DATA_DIR = tmp
    STATE_PATH = os.path.join(tmp, "seen.json")
    LOG_DIR = os.path.join(tmp, "logs")
    LATENCY_PATH = os.path.join(tmp, "latency.csv")

    failures = []

    def check(name, cond, detail=""):
        print("  %-52s %s%s" % (name, "PASS" if cond else "FAIL",
                                "" if cond else "   <- " + str(detail)))
        if not cond:
            failures.append(name)

    print("idx3 selftest (offline)\n")

    # -- parser --------------------------------------------------------
    items = parse(_fake_payload(3))
    check("parser returns every reply", len(items) == 3, len(items))
    check("ticker is stripped of API padding", items[0]["ticker"] == "TEST0",
          repr(items[0]["ticker"]))
    check("attachment url carried through",
          items[0]["files"][0]["url"].endswith("test_0.pdf"))
    check("missing Id2 is skipped",
          len(parse({"Replies": [{"pengumuman": {}}]})) == 0)

    # -- filters -------------------------------------------------------
    it = {"ticker": "BBCA", "title": "Laporan Kepemilikan Saham"}
    check("watchlist admits a listed ticker",
          matches(it, {"watchlist": ["BBCA"]}))
    check("watchlist rejects an unlisted ticker",
          not matches(it, {"watchlist": ["TAPG"]}))
    check("exclude_keywords drops a match",
          not matches(it, {"exclude_keywords": ["kepemilikan"]}))
    check("empty filters admit everything", matches(it, {}))

    # -- latency arithmetic --------------------------------------------
    now = datetime.now()
    item = {"posted": (now.replace(microsecond=0)).strftime("%Y-%m-%d %H:%M:%S"),
            "created": now.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")}
    gap, _ = idx3lat.gaps(item, time.time() + 12.0, 0.0)
    check("publish->detect gap is measured", gap is not None and 11.0 < gap < 13.5,
          gap)
    gap_skewed, _ = idx3lat.gaps(item, time.time() + 12.0, -10.0)
    check("clock skew is applied to the gap",
          gap_skewed is not None and abs(gap_skewed - (gap - 10.0)) < 0.6,
          (gap, gap_skewed))

    # -- scheduler -----------------------------------------------------
    pl = idx3sched.Planner()
    B = 5000 * idx3sched.PERIOD
    t, seen_kinds, ticks, guard = B - 500.0, set(), [], 0
    while t < B + 300 and guard < 5000:
        guard += 1
        due, kind = pl.next_event(t)
        if due <= t:
            break
        seen_kinds.add(kind)
        ticks.append((due - B, kind))
        t = due
    check("scheduler always moves forward", guard < 5000 and t >= B + 300)
    polls = [d for d, k in ticks if k != idx3sched.PREWARM]
    worst = max(polls[i + 1] - polls[i] for i in range(len(polls) - 1))
    check("no gap between polls exceeds the 60s budget", worst < 60.0,
          "%.1fs" % worst)
    burst = [d for d, k in ticks if k == idx3sched.BURST and -5 <= d <= 95]
    check("burst covers the boundary at 1s spacing", len(burst) >= 90, len(burst))
    check("prewarm is scheduled before the burst",
          any(k == idx3sched.PREWARM for _, k in ticks)
          and min(d for d, k in ticks if k == idx3sched.PREWARM) < min(burst),
          ticks[:3])

    # -- client health / failover --------------------------------------
    f = idx3net.Fetcher(allow=["keepalive", "urllib"], log=lambda m: None)
    f.by_name["keepalive"].lost(idx3net.Refused(403, "keepalive"), cool=300)
    check("a refused client is taken out of rotation",
          f.order()[0].name == "urllib", [h.name for h in f.order()])
    for h in f.health:
        h.lost(idx3net.Refused(403, h.name), cool=300)
    check("all-cooling still yields a client to try", len(f.order()) == 1)

    # -- end to end ----------------------------------------------------
    cfg = _merge(DEFAULT_CONFIG, {"page_size": 10, "escalate_page_size": 20,
                                  "log_summary_minutes": 0})
    w = Watcher(cfg)
    served = {"payloads": []}

    def fake_get(page_size, *a, **k):
        w.fetcher.last_client = "fake"
        w.fetcher.last_ms = 0.031
        return served["payloads"].pop(0)

    w.fetcher.get_json = fake_get

    served["payloads"] = [_fake_payload(10, start=100)]
    fresh, hits = w.poll(seed_only=True)
    check("seeding records everything", fresh == 10, fresh)
    check("seeding alerts on nothing", w.dispatcher.sent == 0)

    served["payloads"] = [_fake_payload(10, start=100)]
    fresh, hits = w.poll()
    check("a repeat page produces no new items", fresh == 0 and hits == 0,
          (fresh, hits))

    served["payloads"] = [_fake_payload(10, start=104)]   # 4 genuinely new
    fresh, hits = w.poll()
    check("only unseen keys are treated as new", fresh == 4 and hits == 4,
          (fresh, hits))

    # a page where nearly everything is new must trigger the backfill fetch
    served["payloads"] = [_fake_payload(10, start=200), _fake_payload(20, start=200)]
    fresh, _ = w.poll()
    check("saturated page triggers a backfill", fresh == 10 and not served["payloads"],
          (fresh, len(served["payloads"])))

    for _ in range(60):
        if w.dispatcher.q.empty():
            break
        time.sleep(0.05)
    time.sleep(0.35)
    check("dispatcher delivered every alert", w.dispatcher.sent >= 14,
          w.dispatcher.sent)
    check("latency rows were written", os.path.exists(LATENCY_PATH))
    if os.path.exists(LATENCY_PATH):
        with open(LATENCY_PATH, encoding="utf-8") as fh:
            lines = fh.read().strip().splitlines()
        check("one CSV row per alert plus a header",
              len(lines) == w.dispatcher.sent + 1,
              (len(lines), w.dispatcher.sent))
        report = idx3lat.summarise(LATENCY_PATH)
        check("stats report renders", "OUR LAG" in report and "verdict:" in report,
              report[:80])

    # -- the three timestamps must reach the log ------------------------
    # A stated requirement from day one: log filed -> published -> alerted with
    # both gaps. It was silently lost in the 3.0 rewrite and nothing caught it.
    import time as _t
    from datetime import timedelta as _td
    _now = datetime.now()
    _it = {"ticker": "AAAA", "title": "t",
           "posted":  (_now - _td(minutes=4)).strftime("%Y-%m-%d %H:%M:%S"),
           "created": (_now - _td(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
           "files": []}
    rendered = fmt_alert(_it, 22.3, 60.0, "ok", _t.time())
    check("the alert line shows when it was filed",
          "filed " in rendered, rendered)
    check("the alert line shows when IDX published it",
          "published " in rendered, rendered)
    check("the alert line shows when the alert fired",
          "alerted " in rendered, rendered)
    check("it shows IDX's own gap between the two",
          "idx)" in rendered and "+3m" in rendered, rendered)
    check("our own poll interval is still on the header line",
          "ours +22.3s" in rendered, rendered)
    check("a missing filed timestamp does not break the line",
          "published " in fmt_alert(dict(_it, posted=""), 1.0, 1.0, "ok", _t.time()))
    check("gap formatting spans seconds, minutes and hours",
          (_gap(48), _gap(228), _gap(46200)) == ("+48s", "+3m48s", "+12h50m"),
          (_gap(48), _gap(228), _gap(46200)))

    # -- pagination + freshness -----------------------------------------
    # The bug that made every alert stale for two versions. Pinned here so a
    # future "tidy-up" of build_url cannot quietly reintroduce it.
    check("indexFrom defaults to page 0, not 1",
          "indexFrom=0" in idx3net.build_url(30),
          idx3net.build_url(30)[:60])
    check("FIRST_PAGE is 0", idx3net.FIRST_PAGE == 0)
    check("config default index_from is 0",
          DEFAULT_CONFIG["index_from"] == 0)

    warned = []
    _real_log = globals()["log"]
    globals()["log"] = lambda m: warned.append(m)
    try:
        wf = Watcher(_merge(DEFAULT_CONFIG, {"log_summary_minutes": 0}))
        old_stamp = (datetime.now() - __import__("datetime").timedelta(hours=13)
                     ).strftime("%Y-%m-%d %H:%M:%S")
        wf._check_freshness([{"posted": old_stamp}])
        stale_warned = any("newest item in the feed is" in m for m in warned)
        warned.clear()
        wf._stale_warned = 0.0
        wf._check_freshness([{"posted": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S")}])
        fresh_quiet = not any("newest item in the feed is" in m for m in warned)
    finally:
        globals()["log"] = _real_log
    check("a 13h-stale feed raises a warning", stale_warned)
    check("a current feed stays quiet", fresh_quiet)

    # -- verdict classification -----------------------------------------
    # This is the logic that stopped 3.0 blaming itself for IDX's twelve-hour
    # visibility delay, so it is worth pinning down explicitly.
    check("on-time poll with a fresh item is ok",
          idx3lat.classify(3.0, 3.0, 60.0) == "ok")
    check("a long IDX gap on a prompt poll is IDX's, not ours",
          idx3lat.classify(46196.0, 22.0, 60.0) == "late_visible",
          idx3lat.classify(46196.0, 22.0, 60.0))
    check("a poll gap over budget is ours, whatever IDX says",
          idx3lat.classify(46196.0, 900.0, 60.0) == "slow")
    check("our own gap over budget with no IDX stamp is still ours",
          idx3lat.classify(None, 900.0, 60.0) == "slow")
    check("no previous poll yet is not scored",
          idx3lat.classify(30.0, None, 60.0) == "unknown")
    check("slack absorbs a normal poll interval",
          idx3lat.classify(24.0, 22.0, 60.0) == "ok")

    # our_lag must be the interval between successful polls, not wall clock
    w2 = Watcher(_merge(DEFAULT_CONFIG, {"page_size": 10,
                                         "log_summary_minutes": 0}))
    served2 = {"p": []}

    def fake2(page_size, *a, **k):
        w2.fetcher.last_client = "fake"
        w2.fetcher.last_ms = 0.02
        return served2["p"].pop(0)
    w2.fetcher.get_json = fake2
    served2["p"] = [_fake_payload(10, start=500)]
    w2.poll(seed_only=True)
    check("seeding sets the lag baseline", w2.prev_poll_at is not None)
    first = w2.prev_poll_at
    time.sleep(0.25)
    served2["p"] = [_fake_payload(10, start=501)]
    w2.poll()
    check("our lag advances with each successful poll",
          w2.prev_poll_at > first, (first, w2.prev_poll_at))
    time.sleep(0.4)
    captured = {}
    orig_submit = w2.dispatcher.submit

    def spy(items, ctx):
        captured.update(ctx)
        orig_submit(items, ctx)
    w2.dispatcher.submit = spy
    served2["p"] = [_fake_payload(10, start=502)]
    w2.poll()
    check("our_lag reaches the dispatcher and matches the real interval",
          captured.get("our_lag") is not None
          and 0.3 < captured["our_lag"] < 2.0, captured.get("our_lag"))

    # duplicate suppression
    before = w.dispatcher.sent
    batch = parse(_fake_payload(2, start=900))
    ctx = {"phase": "burst", "since_boundary": 1.0, "client": "fake",
           "fetch_ms": 0.03, "page_size": 10, "skew": 0.0}
    w.dispatcher.submit(batch, ctx)
    w.dispatcher.submit(batch, ctx)
    time.sleep(0.5)
    check("the same batch twice alerts once",
          w.dispatcher.sent == before + 2, w.dispatcher.sent - before)

    # state survives a restart
    save_state(w.seen, "fake", w.stats())
    seen2, client2 = load_state()
    check("state round-trips through disk",
          len(seen2) == len(w.seen) and client2 == "fake")

    # -- the version lives in exactly one place -------------------------
    check("VERSION is set", bool(VERSION) and VERSION[0].isdigit(), VERSION)
    check("APP is built from VERSION", APP.endswith(VERSION), APP)
    stray = []
    for mod in ("idx3.py", "idx3net.py", "idx3sched.py", "idx3lat.py",
                "idx3options.py", "idx3popup.py", "idx3tray.py",
                "idx3startup.py"):
        p = os.path.join(HERE, mod)
        if not os.path.exists(p):
            continue
        t = _ast.parse(open(p, encoding="utf-8").read()) if False else None
        import ast as _a2
        tree_v = _a2.parse(open(p, encoding="utf-8").read())
        docs = {_a2.get_docstring(n, clean=False) for n in _a2.walk(tree_v)
                if isinstance(n, (_a2.Module, _a2.FunctionDef, _a2.ClassDef))}
        for node in _a2.walk(tree_v):
            if (isinstance(node, _a2.Constant) and isinstance(node.value, str)
                    and node.value not in docs
                    and _re_version.search(node.value)):
                stray.append("%s:%d %r" % (mod, node.lineno, node.value[:40]))
    check("no hardcoded IDXAlert version outside idx3.VERSION", not stray, stray)

    # -- nothing user-facing recites version history --------------------
    # config.json is one click away in the tray menu and --probe prints to the
    # console, so both are UI. Engineering history belongs in comments and
    # docstrings, where it helps whoever edits the code next.
    import ast as _a
    leaks = []
    for mod in ("idx3.py", "idx3net.py", "idx3sched.py", "idx3lat.py",
                "idx3options.py", "idx3popup.py", "idx3tray.py",
                "idx3startup.py"):
        p = os.path.join(HERE, mod)
        if not os.path.exists(p):
            continue
        tree_m = _a.parse(open(p, encoding="utf-8").read())
        docs = set()
        for node in _a.walk(tree_m):
            if isinstance(node, (_a.Module, _a.FunctionDef, _a.ClassDef)):
                d = _a.get_docstring(node, clean=False)
                if d:
                    docs.add(d)
        for node in _a.walk(tree_m):
            # len() guard: a bare "2.x" is a search token (this check's own
            # tuple, for one), not a sentence shown to anybody.
            if (isinstance(node, _a.Constant) and isinstance(node.value, str)
                    and len(node.value) > 12 and node.value not in docs
                    and any(t in node.value for t in ("2.x", "1.x"))):
                leaks.append("%s:%d" % (mod, node.lineno))
    check("no version history in user-facing strings", not leaks, leaks)

    # -- no stray undefined names --------------------------------------
    # The 2.x rewrite that silently deleted a helper passed a syntax check.
    import ast as _ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = _ast.parse(src)
    defined = {n.name for n in _ast.walk(tree)
               if isinstance(n, (_ast.FunctionDef, _ast.ClassDef))}
    called = {n.func.id for n in _ast.walk(tree)
              if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)}
    # A name that is assigned anywhere - a local holding a callable, a loop
    # variable, a comprehension target - is not a missing helper.
    bound = {n.id for n in _ast.walk(tree)
             if isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Store)}
    bound |= {a.arg for n in _ast.walk(tree)
              if isinstance(n, (_ast.FunctionDef, _ast.Lambda))
              for a in n.args.args + n.args.kwonlyargs}
    # names bound by an import anywhere, including imports inside a function
    for n in _ast.walk(tree):
        if isinstance(n, (_ast.Import, _ast.ImportFrom)):
            bound |= {(a.asname or a.name).split(".")[0] for a in n.names}
    import builtins as _bi
    unknown = sorted(called - defined - bound - set(dir(_bi)) - set(globals()))
    check("no calls to undefined helpers", not unknown, unknown)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
    return 1 if failures else 0


# ------------------------------------------------------------------------ cli

def cmd_plan(cfg):
    p = idx3sched.Planner(cfg.get("poll"))
    now = time.time()
    prev, nxt = p.boundaries(now)
    print("tick schedule, one 30-minute cycle")
    print("  prewarm          B-%.0fs" % p.prewarm_lead)
    print("  burst            B-%.0fs .. B+%.0fs  every %.1fs   (%d ticks)"
          % (p.lead, p.window, p.fast, int((p.lead + p.window) / p.fast) + 1))
    print("  baseline         every %.0fs (+ up to %.1fs jitter)" % (p.base, p.jitter))
    print()
    print("  requests/hour            ~%d      (~%d/day)"
          % (p.requests_per_hour(), p.requests_per_hour() * 24))
    print("  worst-case detection     %.1fs   for an off-cycle filing"
          % p.worst_case_detection())
    print("  batch-case detection     <%.1fs  for a :00/:30 batch" % (p.fast + 1.0))
    print("  budget                   %.0fs"
          % float(cfg.get("latency_budget_seconds", 60)))
    ok = p.worst_case_detection() < float(cfg.get("latency_budget_seconds", 60))
    print("  -> %s" % ("within budget" if ok else
                       "OVER BUDGET: lower baseline_interval_seconds"))
    print()
    print("  next boundary            %s (in %.0fs)"
          % (datetime.fromtimestamp(nxt).strftime("%H:%M:%S"), nxt - now))
    print("  next 6 events from now:")
    t = now
    for _ in range(6):
        due, kind = p.next_event(t)
        print("     %s  %-8s  B%+.1fs"
              % (datetime.fromtimestamp(due).strftime("%H:%M:%S"), kind,
                 p.seconds_since_boundary(due)))
        t = due
    return 0


def cmd_probe(cfg, rounds):
    f = idx3net.Fetcher(timeout=float(cfg.get("request_timeout_seconds", 12.0)),
                        log=log, allow=cfg.get("clients"))
    print("probing %s, %d requests each ...\n" % (idx3net.HOST, rounds))
    res = f.probe(rounds=rounds, page_size=10)
    f.close()
    if not res:
        print("nothing got through. Every client was refused or errored.")
        return 1
    print("  %-12s %6s %9s %9s %9s %9s" % ("client", "n", "first", "min",
                                           "median", "max"))
    for name, r in sorted(res.items(), key=lambda kv: kv[1]["median"]):
        print("  %-12s %6d %8.0fms %8.0fms %8.0fms %8.0fms"
              % (name, r["n"], r["first"] * 1000, r["min"] * 1000,
                 r["median"] * 1000, r["max"] * 1000))
    if "keepalive" in res:
        k = res["keepalive"]
        rivals = {n: r for n, r in res.items() if n != "keepalive"}
        print()
        if rivals:
            best = min(rivals.items(), key=lambda kv: kv[1]["median"])
            saved = best[1]["median"] - k["median"]
            print("  keep-alive vs %s (which reconnects every time): "
                  "%+.0fms per poll, steady state."
                  % (best[0], -saved * 1000 if saved < 0 else -saved * 1000))
            print("  %.0fms -> %.0fms, %.0f%% faster."
                  % (best[1]["median"] * 1000, k["median"] * 1000,
                     100.0 * saved / best[1]["median"]))
        print("  Note the bigger win is not the milliseconds: reusing one socket")
        print("  means far fewer TCP/TLS connections per day than reconnecting")
        print("  each time, and connection churn is what rate-limiting notices.")
    print("  clock skew vs IDX: %+.2fs%s"
          % (f.skew.offset, "" if f.skew.confident else "  (few samples)"))
    return 0


def cmd_status(cfg):
    seen, client = load_state()
    print("data dir     %s" % DATA_DIR)
    print("seen keys    %d" % len(seen))
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        print("last update  %s" % d.get("updated"))
        print("last client  %s" % d.get("client"))
        for k, v in (d.get("stats") or {}).items():
            print("  %-24s %s" % (k, v))
    except (OSError, ValueError):
        print("no state file yet - run --init")
    return 0


def _report_fetch_failure(exc):
    """A one-shot command that cannot reach IDX should say so in a sentence and
    point at the next step, not print a stack trace. The watcher loop keeps its
    own handling - there, a refusal is a backoff, not an exit."""
    log("could not reach IDX: %s" % str(exc)[:400])
    print("\nNothing got through. Usual causes, in order of likelihood:")
    print("  * no internet, or a proxy/VPN in the way")
    print("  * Cloudflare is refusing this machine - run  python idx3.py --probe")
    print("  * a datacenter/cloud IP: IDX refuses those regardless of client,")
    print("    so this has to run on a residential or office connection")
    return 1


def main():
    ap = argparse.ArgumentParser(
        description="%s - speed-first IDX disclosure watcher" % APP)
    ap.add_argument("--init", action="store_true",
                    help="seed state from what is already posted, alert on nothing")
    ap.add_argument("--once", action="store_true", help="one poll, then exit")
    ap.add_argument("--plan", action="store_true",
                    help="print the tick schedule and request budget")
    ap.add_argument("--probe", nargs="?", const=8, type=int, metavar="N",
                    help="measure request latency per client")
    ap.add_argument("--stats", nargs="?", const="", metavar="PATH",
                    help="latency report; give a path to read another copy's "
                         "latency.csv (the .exe keeps its own beside itself)")
    ap.add_argument("--status", action="store_true", help="what the watcher is doing")
    ap.add_argument("--selftest", action="store_true",
                    help="offline end-to-end harness, no network")
    ap.add_argument("--verify-feed", action="store_true", dest="verify_feed",
                    help="live check that we are reading the NEWEST page")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    cfg = load_config()

    if args.plan:
        return cmd_plan(cfg)
    if args.probe is not None:
        return cmd_probe(cfg, max(2, args.probe))
    if args.stats is not None:
        path = args.stats or LATENCY_PATH
        if os.path.isdir(path):
            path = os.path.join(path, "latency.csv")
        print(idx3lat.summarise(path,
                                float(cfg.get("latency_budget_seconds", 60))))
        return 0
    if args.status:
        return cmd_status(cfg)

    if args.verify_feed:
        w = Watcher(cfg)
        try:
            ok, detail = w.verify_feed()
        except Exception as exc:
            return _report_fetch_failure(exc)
        finally:
            w.fetcher.close()
        print("index_from = %s, page_size = %s\n  %s"
              % (cfg.get("index_from"), cfg.get("page_size"), detail))
        print("\n  %s" % ("OK - reading the newest page" if ok
                          else "BROKEN - not reading the newest page"))
        return 0 if ok else 1

    if args.init:
        w = Watcher(cfg)
        try:
            n, _ = w.poll(seed_only=True)
            log("seeded %d existing disclosures - nothing sent" % n)
        except Exception as exc:
            return _report_fetch_failure(exc)
        finally:
            w.fetcher.close()
        return 0

    if args.once:
        w = Watcher(cfg)
        try:
            fresh, hits = w.poll()
            log("one poll: %d new, %d matched, %.0fms via %s"
                % (fresh, hits, (w.fetcher.last_ms or 0) * 1000,
                   w.fetcher.last_client))
            time.sleep(0.6)          # let the dispatcher drain
        except Exception as exc:
            return _report_fetch_failure(exc)
        finally:
            w.fetcher.close()
        return 0

    # A second watcher on the same seen.json means duplicate alerts and double
    # the request rate into Cloudflare. Distinct mutex from 2.x on purpose:
    # the two are meant to run side by side, out of their own folders.
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\IDXAlert3Alpha")
            if ctypes.windll.kernel32.GetLastError() == 183:
                sys.exit("%s is already running. Close it first, or " % APP +
                         "use --once / --probe, which are safe alongside it.")
        except OSError:
            pass

    seen, _ = load_state()
    if not seen:
        log("no state yet - seeding so the first tick does not alert on old filings")
        try:
            Watcher(cfg).poll(seed_only=True)
        except Exception as exc:
            log("seed failed: %s" % exc)

    w = Watcher(cfg)
    try:
        w.run()
    except KeyboardInterrupt:
        w.request_stop()
        log("stopped. %s" % json.dumps(w.stats()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
