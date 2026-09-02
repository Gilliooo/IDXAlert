#!/usr/bin/env python3
"""
test_tray.py - the tray's wiring, with pystray stubbed out.

The tray is only a face over the engine, so what is worth testing is exactly
that: does the popup channel get attached to the dispatcher, do menu clicks
turn into requests the Watcher honours, and does the icon report the state the
Watcher is actually in. None of that needs a screen.

The icon colours matter more than they look. A blue "stale feed" icon is the
visible half of the guard added after indexFrom=1 spent two versions reading
page two - a wrong-but-valid feed has no other symptom.

Run:  python test_tray.py
"""

import sys
import types


def install_fake_pystray():
    ps = types.ModuleType("pystray")

    class Menu:
        SEPARATOR = object()
        def __init__(self, *items):
            self.items = items

    class MenuItem:
        def __init__(self, text, action=None, default=False):
            self.text = text
            self.action = action

    class Icon:
        def __init__(self, name, image=None, title=None, menu=None):
            self.name, self.icon, self.title, self.menu = name, image, title, menu
            self.visible = True
            self.notifications = []
        def notify(self, message, title=None):
            self.notifications.append((title, message))
        def remove_notification(self):
            pass
        def run(self):
            pass
        def stop(self):
            pass

    ps.Menu, ps.MenuItem, ps.Icon = Menu, MenuItem, Icon
    sys.modules["pystray"] = ps
    return ps


def main():
    install_fake_pystray()
    import os
    import idx3
    import idx3tray
    import idx3popup

    failures = []

    def check(name, cond, detail=""):
        print("  %-56s %s%s" % (name, "PASS" if cond else "FAIL",
                                "" if cond else "   <- " + str(detail)))
        if not cond:
            failures.append(name)

    print("tray wiring test (fake pystray)\n")

    check("the tray puts the popup into tray mode so it cannot block",
          idx3popup.TRAY_MODE is True)

    icon = sys.modules["pystray"].Icon("t", None, None, idx3tray.build_menu())
    check("the menu builds", icon.menu is not None
          and len(icon.menu.items) > 8, len(icon.menu.items))
    labels = [i.text for i in icon.menu.items
              if hasattr(i, "text") and isinstance(i.text, str)]
    for wanted in ("Check now", "Status", "Verify feed", "Options...", "Quit"):
        check("menu has %r" % wanted, wanted in labels, labels)

    # --- the engine seam ------------------------------------------------
    # Redirect every output path FIRST. A Watcher's Recorder captures the
    # module-global LATENCY_PATH at construction, so building one without this
    # appends test rows to the user's real latency.csv and quietly corrupts the
    # measurements it exists to record. Same trap as test_filters.py.
    import tempfile
    sandbox = tempfile.mkdtemp(prefix="idx3tray-")
    idx3.DATA_DIR = sandbox
    idx3.STATE_PATH = os.path.join(sandbox, "seen.json")
    idx3.LATENCY_PATH = os.path.join(sandbox, "latency.csv")
    idx3.LOG_DIR = os.path.join(sandbox, "logs")

    cfg = idx3._merge(idx3.DEFAULT_CONFIG, {"log_summary_minutes": 0,
                                            "data_dir": sandbox})
    w = idx3.Watcher(cfg)
    w.dispatcher.channels.append(idx3tray.popup_channel)
    idx3tray.state["watcher"] = w
    check("the popup channel is attached to the dispatcher",
          idx3tray.popup_channel in w.dispatcher.channels)

    # an alert dispatched by the engine must reach the tray's queue
    items = idx3.parse(idx3._fake_payload(2, start=700))
    w.dispatcher.submit(items, {"phase": "burst", "since_boundary": 1.0,
                                "client": "fake", "fetch_ms": 0.02,
                                "page_size": 30, "skew": 0.0, "our_lag": 1.0})
    import time
    got = None
    for _ in range(40):
        if not idx3tray.outbox.empty():
            got = idx3tray.outbox.get()
            break
        time.sleep(0.05)
    check("an engine alert lands in the tray's outbox", got is not None
          and len(got) == 2, got and len(got))

    # --- menu clicks become requests, not direct polls -------------------
    w.wake.clear()
    idx3tray.do_check(icon, None)
    check("'Check now' asks the engine rather than polling itself",
          w.wake.is_set() and w._interrupt.is_set())
    w.wake.clear()
    w._interrupt.clear()

    idx3tray.do_pause(icon, None)
    check("'Pause' pauses the engine", w.paused is True)
    idx3tray.do_pause(icon, None)
    check("'Pause' again resumes it", w.paused is False)

    # --- icon reflects real engine state ---------------------------------
    def colour():
        idx3tray.refresh_icon(icon)
        for k, v in idx3tray.ICONS.items():
            if icon.icon is v:
                return k
        return "?"

    w.paused = False
    w.last_error = None
    w.feed_age_minutes = 2.0
    check("healthy engine shows green", colour() == "ok", colour())
    w.feed_age_minutes = 13 * 60.0
    check("a stale feed shows blue, not green", colour() == "stale", colour())
    w.feed_age_minutes = 2.0
    w.last_error = "boom"
    check("a failed poll shows red", colour() == "error", colour())
    w.last_error = None
    w.paused = True
    check("paused shows amber", colour() == "paused", colour())
    # Precedence matters: a paused watcher is not reporting a stale feed, it is
    # simply not looking. Showing blue there would be a lie.
    w.feed_age_minutes = 13 * 60.0
    w.last_error = "boom"
    check("paused takes precedence over stale and error",
          colour() == "paused", colour())
    w.paused = False
    w.last_error = "boom"
    check("an error takes precedence over a stale feed",
          colour() == "error", colour())
    w.last_error = None
    w.feed_age_minutes = 2.0

    # --- status text draws from the engine's own numbers ------------------
    icon.notifications.clear()
    idx3tray.do_status(icon, None)
    check("Status reports engine stats", icon.notifications
          and "our-lag" in icon.notifications[-1][1],
          icon.notifications[-1] if icon.notifications else None)

    # --- quit must not leave a popup behind -------------------------------
    check("quit closes any live popup", hasattr(idx3popup, "close_live"))

    import shutil
    shutil.rmtree(sandbox, ignore_errors=True)
    print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
