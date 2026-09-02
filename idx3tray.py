#!/usr/bin/env python3
"""
IDXAlert - tray app. The thing that actually runs.

    python idx3tray.py          run it
    build3.bat                  make IDXAlert3.exe

The 2.x tray owned its own poll loop and called into the engine. That is
inverted here: the engine owns the loop, because the whole latency argument
lives in WHEN it polls - an absolute tick grid aligned to IDX's clock - and a
tray that re-implemented "sleep a bit, then poll" would quietly throw that away.

So this file is only a face: it starts a Watcher on a background thread, hangs
a popup channel off its dispatcher, and turns menu clicks into requests the
Watcher honours on its own schedule. Nothing here decides when to fetch.

The tray icon is the status indicator:
    green   running, last poll fine
    amber   paused
    blue    running, but the feed looks stale - the newest item is old, which
            is what a wrong page or a changed API looks like from the inside
    red     last poll failed
"""

import os
import queue
import sys
import threading
import time
import webbrowser

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("Missing dependencies. Run:  pip install pystray pillow")

import idx3
import idx3options
import idx3popup
import idx3startup

idx3popup.TRAY_MODE = True     # sticky popups must not block the notify thread

state = {"watcher": None, "alerts": 0, "last_shown": None}
outbox = queue.Queue()


# ------------------------------------------------------------------------ icon

def make_icon(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill=color)
    for x, h in ((16, 26), (28, 36), (40, 46)):          # little bar chart
        d.rectangle([x, 58 - h, x + 8, 50], fill=(255, 255, 255, 235))
    return img


ICONS = {
    "ok":     make_icon((34, 139, 84, 255)),
    "paused": make_icon((190, 140, 20, 255)),
    "stale":  make_icon((40, 105, 190, 255)),
    "error":  make_icon((176, 42, 42, 255)),
}


def refresh_icon(icon):
    w = state["watcher"]
    if w is None:
        icon.icon, icon.title = ICONS["ok"], idx3.APP + " - starting"
        return
    stale_limit = float(w.cfg.get("stale_feed_warn_minutes", 90) or 0)
    age = w.feed_age_minutes
    if w.paused:
        key, tip = "paused", idx3.APP + " - paused"
    elif w.last_error:
        key, tip = "error", idx3.APP + " - error: %s" % w.last_error[:60]
    elif stale_limit and age is not None and age > stale_limit:
        key = "stale"
        tip = (idx3.APP + " - feed's newest item is %.0f min old" % age)
    else:
        key = "ok"
        last = w.last_poll_at
        tip = (idx3.APP + " - last check %s"
               % (time.strftime("%H:%M:%S", time.localtime(last)) if last
                  else "pending"))
    icon.icon = ICONS[key]
    icon.title = tip


# --------------------------------------------------------------------- plumbing

def popup_channel(items, ctx):
    """Hung off Watcher.dispatcher.channels. Runs on the dispatcher thread, so
    it must not block the poll thread - it only queues."""
    state["alerts"] += len(items)
    outbox.put(list(items))


def show(icon, items):
    """Draw the popup, with the tray balloon as a last resort. Never fail
    silently: a notification nobody sees is the same as no notification."""
    cfg = {}
    try:
        cfg = (state["watcher"].cfg if state["watcher"] else idx3.load_config())
    except Exception:
        pass
    opts = ((cfg.get("channels") or {}).get("popup") or {})
    if opts.get("enabled", True) is False:
        return True
    try:
        idx3popup.show(items, opts)
        state["last_shown"] = "popup"
        return True
    except Exception as exc:
        idx3.log("popup failed: %s" % str(exc)[:160])
    try:
        head = items[0]
        title = ("IDX: %s" % (head.get("ticker") or "new disclosure")
                 if len(items) == 1
                 else "IDX: %d new disclosures" % len(items))
        icon.notify("\n".join((i.get("title") or "")[:70] for i in items[:3]),
                    title)
        idx3.log("  alert shown via tray balloon (popup was unavailable)")
        time.sleep(6)
        try:
            icon.remove_notification()
        except Exception:
            pass
        state["last_shown"] = "tray balloon"
        return True
    except Exception as exc:
        idx3.log("ALL notification methods failed: %s" % exc)
        return False


def pump(icon):
    """Drain queued alerts on their own thread."""
    while not getattr(icon, "visible", False):     # notify() needs a live icon
        time.sleep(0.3)
    while True:
        try:
            items = outbox.get(timeout=1)
        except queue.Empty:
            if state["watcher"] is not None:
                refresh_icon(icon)
            continue
        show(icon, items)
        refresh_icon(icon)


def engine(icon):
    w = state["watcher"]
    try:
        w.run()
    except Exception as exc:
        idx3.log("engine stopped: %s" % exc)
        w.last_error = str(exc)[:200]
        refresh_icon(icon)


# ------------------------------------------------------------------ menu items

def do_check(icon, _item):
    w = state["watcher"]
    if w:
        w.request_check()


def do_pause(icon, _item):
    w = state["watcher"]
    if w:
        w.set_paused(not w.paused)
    refresh_icon(icon)


def do_status(icon, _item):
    w = state["watcher"]
    if w is None:
        icon.notify("starting up", idx3.APP)
        return
    s = w.stats()
    age = s.get("feed_age_min")
    icon.notify(
        "checks %d   alerts %d   failures %d\n"
        "worst our-lag %.1fs   over budget %d\n"
        "feed newest: %s\n"
        "via %s %sms   skew %+.1fs\n"
        "%s"
        % (s["polls"], s["alerts"], s["failures"],
           s["worst_our_lag_s"], s["over_budget"],
           ("%d min old" % age) if age is not None else "unknown",
           s["client"] or "-", s["fetch_ms"], s["clock_skew_s"],
           w.last_error or "no errors"),
        idx3.APP + " status")


def do_verify(icon, _item):
    """The check that would have caught the page-two bug on day one."""
    def run():
        w = state["watcher"]
        if w is None:
            return
        try:
            ok, detail = w.verify_feed()
        except Exception as exc:
            icon.notify("could not check: %s" % str(exc)[:120], idx3.APP)
            return
        idx3.log("verify-feed: %s\n  %s" % ("OK" if ok else "BROKEN", detail))
        icon.notify(("Reading the newest page." if ok else
                     "NOT reading the newest page - see the log.")
                    + "\n" + detail.replace("  ", " ")[:180],
                    idx3.APP + " - feed check")
    threading.Thread(target=run, daemon=True).start()


def do_test(icon, _item):
    idx3.log("test notification requested from the tray menu")
    demo = [{"key": "test%d" % n, "ticker": t, "title": ti,
             "posted": "2026-09-01 08:5%d:00" % n,
             "files": [{"name": "example.pdf", "url": idx3popup.PAGE}]}
            for n, (t, ti) in enumerate([
                ("TEST", "If you can read this, alerts are working."),
                ("BBCA", "Penyampaian Laporan Keuangan Interim"),
                ("TAPG", "Penjelasan atas Volatilitas Transaksi"),
                ("SMMA", "Penyampaian Bukti Iklan Informasi Laporan Keuangan"),
                ("ENRG", "Laporan Kepemilikan Saham Perusahaan Terbuka")])]
    ok = show(icon, demo)
    idx3.log("test notification %s (via %s)"
             % ("delivered" if ok else "FAILED", state.get("last_shown") or "-"))


def do_options(icon, _item):
    """tkinter needs its own thread with its own mainloop."""
    def run():
        try:
            idx3options.open_window(
                idx3.CONFIG_PATH,
                on_saved=lambda: (idx3.log("settings saved from Options"),
                                  state["watcher"]
                                  and state["watcher"].request_check()),
                on_test=lambda: outbox.put([
                    {"key": "test", "ticker": "TEST",
                     "title": "If you can read this, alerts are working.",
                     "posted": "", "files": []}]),
                on_verify=lambda: do_verify(icon, None),
            )
        except Exception as exc:
            idx3.log("Options window failed: %s" % exc)
    threading.Thread(target=run, daemon=True).start()


def do_open_log(_icon, _item):
    try:
        target = idx3.current_log_path()
        os.startfile(target if os.path.exists(target) else idx3.LOG_DIR)
    except Exception as exc:
        idx3.log("could not open the log: %s" % exc)


def do_open_latency(_icon, _item):
    try:
        if os.path.exists(idx3.LATENCY_PATH):
            os.startfile(idx3.LATENCY_PATH)
        else:
            os.startfile(idx3.DATA_DIR)
    except Exception as exc:
        idx3.log("could not open latency.csv: %s" % exc)


def do_open(path):
    def handler(_icon, _item):
        target = os.path.join(idx3.HERE, path) if path else idx3.HERE
        try:
            os.startfile(target if os.path.exists(target) else idx3.HERE)
        except Exception:
            pass
    return handler


def do_site(_icon, _item):
    webbrowser.open(idx3popup.PAGE)


def do_quit(icon, _item):
    w = state["watcher"]
    if w:
        w.request_stop()
    idx3popup.close_live()
    icon.visible = False
    icon.stop()
    os._exit(0)


def build_menu():
    return pystray.Menu(
        pystray.MenuItem("Check now", do_check, default=True),
        pystray.MenuItem("Status", do_status),
        pystray.MenuItem("Verify feed", do_verify),
        pystray.MenuItem("Send test notification", do_test),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Options...", do_options),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            lambda i: "Resume" if (state["watcher"] and state["watcher"].paused)
            else "Pause", do_pause),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open today" + chr(39) + "s log", do_open_log),
        pystray.MenuItem("Open latency.csv", do_open_latency),
        pystray.MenuItem("Edit config.json by hand", do_open("config.json")),
        pystray.MenuItem("Open folder", do_open(None)),
        pystray.MenuItem("Open IDX page", do_site),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", do_quit),
    )


# -------------------------------------------------------------------- singleton

def already_running():
    """Two watchers sharing one seen.json means duplicate alerts and double the
    request rate into Cloudflare. Distinct name from 2.x on purpose - the two
    are allowed to run side by side out of their own folders."""
    if os.name != "nt":
        return False
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\IDXAlert3Tray")
    return ctypes.windll.kernel32.GetLastError() == 183      # ALREADY_EXISTS


def main():
    if already_running():
        idx3.log("another copy of %s is already running - exiting" % idx3.APP)
        sys.exit(0)

    cfg = idx3.load_config()
    try:
        if cfg.get("start_with_windows") and idx3startup.supported() \
                and not idx3startup.is_enabled():
            ok, detail = idx3startup.set_enabled(True)
            idx3.log("start with Windows: %s"
                     % (detail if ok else "failed - " + detail))
    except Exception as exc:
        idx3.log("could not apply start_with_windows: %s" % exc)

    w = idx3.Watcher(cfg)
    w.dispatcher.channels.append(popup_channel)
    state["watcher"] = w

    # Seed with the SAME watcher rather than a throwaway one - a second
    # Watcher would start a second dispatcher thread that never stops.
    if not w.seen:
        idx3.log("no state yet - seeding so the first tick does not alert on "
                 "everything already posted")
        try:
            w.poll(seed_only=True)
        except Exception as exc:
            idx3.log("seed failed: %s" % exc)

    icon = pystray.Icon("IDXAlert3", ICONS["ok"], idx3.APP + " - starting",
                        build_menu())
    threading.Thread(target=engine, args=(icon,), daemon=True).start()
    threading.Thread(target=pump, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
