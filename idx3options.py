#!/usr/bin/env python3
"""
Settings window for IDXAlert. Opened from the tray (Options...) or with:
    python idx3options.py

read_values / apply_values / validate are PURE functions of dicts, with no Tk
in sight, so the whole config<->form mapping is testable on a machine with no
display. That split is why test_options.py can be meaningful rather than a
smoke test.

The Speed tab shows the consequences of the
numbers as you type them - worst-case detection time and requests per hour -
because those two are in direct tension and neither is obvious from the raw
seconds. A setting that quietly breaks the one-minute promise should say so on
the same screen, not in a log file a day later.
"""

import json
import os
import re

import idx3
import idx3sched
import idx3startup

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# The high-volume filing types on IDX. Matching is case-insensitive substring,
# so the keyword only has to appear somewhere in the title.
EXCLUDE_PRESETS = [
    ("Structured Warrant", "Structured Warrant",
     "warrant issuance and exercise notices"),
    ("Laporan Kepemilikan", "Laporan Kepemilikan",
     "KSEI shareholding-change reports"),
    ("Bukti Iklan", "Bukti Iklan",
     "proof-of-newspaper-advertisement filings"),
    ("Obligasi / Sukuk", "Obligasi",
     "bond and sukuk announcements"),
    ("ETF daily NAV", "Nilai Aktiva Bersih",
     "daily NAV reports from X-code ETFs"),
    ("Penghentian Sementara", "Penghentian Sementara",
     "trading suspensions and resumptions"),
]
ALL_CLIENTS = ["keepalive", "urllib", "curl", "curl_cffi"]

AUTHOR = "Bill Grandy Tunjung"
LINKEDIN = "https://www.linkedin.com/in/bill-tunjung"
LINKEDIN_LABEL = "linkedin.com/in/bill-tunjung"
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _csv(items):
    return ", ".join(items or [])


def _list(text):
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def split_excludes(items):
    """exclude_keywords -> (ticked presets, free-text remainder)."""
    ticked, custom = set(), []
    lookup = {kw.lower(): kw for _, kw, _ in EXCLUDE_PRESETS}
    for entry in list(items or []):
        key = (entry or "").strip()
        if key.lower() in lookup:
            ticked.add(lookup[key.lower()])
        elif key:
            custom.append(key)
    return ticked, ", ".join(custom)


def merge_excludes(ticked, custom_text):
    """Ticked presets + free text -> exclude_keywords, presets first, no dupes."""
    out = [kw for _, kw, _ in EXCLUDE_PRESETS if kw in (ticked or set())]
    seen = {o.lower() for o in out}
    for extra in _list(custom_text):
        if extra.lower() not in seen:
            out.append(extra)
            seen.add(extra.lower())
    return out


# ------------------------------------------------------- config <-> form data

def read_values(cfg):
    ch = cfg.get("channels", {}) or {}
    pop = ch.get("popup", {}) or {}
    tg = ch.get("telegram", {}) or {}
    em = ch.get("email", {}) or {}
    hours = cfg.get("active_hours", {}) or {}
    poll = cfg.get("poll", {}) or {}
    presets, custom = split_excludes(cfg.get("exclude_keywords"))
    return {
        "popup_on": bool(pop.get("enabled", True)),
        "duration": int(pop.get("duration_seconds", 0)),
        "max_visible": int(pop.get("max_visible", 3)),

        "burst_interval": float(poll.get("burst_interval_seconds", 1.0)),
        "burst_window": float(poll.get("burst_window_seconds", 90.0)),
        "burst_lead": float(poll.get("burst_lead_seconds", 3.0)),
        "baseline": float(poll.get("baseline_interval_seconds", 22.0)),
        "prewarm_lead": float(poll.get("prewarm_lead_seconds", 10.0)),
        "budget": float(cfg.get("latency_budget_seconds", 60.0)),

        "page_size": int(cfg.get("page_size", 30)),
        "index_from": int(cfg.get("index_from", 0)),
        "timeout": float(cfg.get("request_timeout_seconds", 12.0)),
        "cooldown": float(cfg.get("refusal_cooldown_seconds", 300.0)),
        "stale_warn": int(cfg.get("stale_feed_warn_minutes", 90)),
        "clients": list(cfg.get("clients") or ALL_CLIENTS),

        "start": hours.get("start", "00:00"),
        "end": hours.get("end", "23:59"),
        "days": list(hours.get("days", [0, 1, 2, 3, 4, 5, 6])),
        "startup": bool(cfg.get("start_with_windows", True)),

        "data_dir": cfg.get("data_dir", ""),
        "retention": int(cfg.get("log_retention_days", 0)),
        "verbose": bool(cfg.get("log_routine_checks", False)),

        "watchlist": _csv(cfg.get("watchlist")),
        "keywords": _csv(cfg.get("keywords")),
        "exclude_presets": presets,
        "exclude": custom,

        "tg_on": bool(tg.get("enabled", False)),
        "tg_token": tg.get("bot_token", ""),
        "tg_chat": str(tg.get("chat_id", "")),
        "em_on": bool(em.get("enabled", False)),
        "em_host": em.get("smtp_host", "smtp.gmail.com"),
        "em_port": int(em.get("smtp_port", 587)),
        "em_user": em.get("username", ""),
        "em_pass": em.get("password", ""),
        "em_to": _csv(em.get("to")),
    }


def budget_summary(v):
    """What these numbers actually cost and buy. Used live in the Speed tab and
    by validate(), so the warning and the readout can never disagree."""
    p = idx3sched.Planner({
        "burst_lead_seconds": v["burst_lead"],
        "burst_window_seconds": v["burst_window"],
        "burst_interval_seconds": v["burst_interval"],
        "baseline_interval_seconds": v["baseline"],
        "prewarm_lead_seconds": v["prewarm_lead"],
        "baseline_jitter_seconds": 0.4,
    })
    worst = p.worst_case_detection()
    per_hour = p.requests_per_hour()
    return {
        "worst_case": worst,
        "per_hour": per_hour,
        "per_day": per_hour * 24,
        "within_budget": worst < float(v["budget"]),
        "batch_case": v["burst_interval"] + 1.0,
    }


def validate(v):
    """Human-readable problems, empty list if fine."""
    bad = []
    if not TIME_RE.match(v["start"] or ""):
        bad.append("Active from must look like 07:00")
    if not TIME_RE.match(v["end"] or ""):
        bad.append("Active until must look like 21:00")
    if not v["days"]:
        bad.append("Pick at least one day")

    if not 0.2 <= float(v["burst_interval"]) <= 60:
        bad.append("Burst interval must be between 0.2 and 60 seconds")
    if not 1 <= float(v["baseline"]) <= 3600:
        bad.append("Baseline interval must be between 1 and 3600 seconds")
    if not 0 <= float(v["burst_window"]) <= 900:
        bad.append("Burst window must be between 0 and 900 seconds")
    if not 5 <= float(v["budget"]) <= 3600:
        bad.append("Latency budget must be between 5 and 3600 seconds")
    if float(v["prewarm_lead"]) <= float(v["burst_lead"]):
        bad.append("Pre-warm must happen BEFORE the burst starts - "
                   "give it a longer lead than the burst lead")

    s = budget_summary(v)
    if not s["within_budget"]:
        bad.append("Baseline of %.0fs means an off-cycle filing can take %.0fs "
                   "to spot, over your %.0fs budget. Lower the baseline."
                   % (float(v["baseline"]), s["worst_case"], float(v["budget"])))

    if not 1 <= int(v["page_size"]) <= 500:
        bad.append("Page size must be between 1 and 500")
    if int(v["page_size"]) < 30:
        bad.append("Page size below 30 risks missing part of a batch - "
                   "IDX publishes up to 27 filings at once")
    if int(v["index_from"]) != 0:
        bad.append("indexFrom must be 0. It is a page number, not a row offset "
                   "- any other value reads an older page and alerts arrive "
                   "late")
    if not v["clients"]:
        bad.append("Enable at least one HTTP client")
    if not 0 <= int(v["max_visible"]) <= 20:
        bad.append("Rows on screen must be between 0 and 20")
    if not 0 <= int(v["duration"]) <= 300:
        bad.append("Seconds on screen must be between 0 and 300")

    if v["tg_on"] and not (v["tg_token"].strip() and str(v["tg_chat"]).strip()):
        bad.append("Telegram needs both a bot token and a chat id")
    if v["em_on"] and not (v["em_user"].strip() and _list(v["em_to"])):
        bad.append("Email needs a username and at least one recipient")

    d = (v.get("data_dir") or "").strip()
    if d:
        expanded = os.path.expandvars(os.path.expanduser(d))
        try:
            os.makedirs(expanded, exist_ok=True)
            probe = os.path.join(expanded, ".idx3_write_test")
            open(probe, "w").close()
            try:
                os.remove(probe)
            except OSError:
                pass          # create-but-not-unlink is still writable enough
        except OSError as exc:
            bad.append("Cannot write to that folder (%s)" % type(exc).__name__)
    return bad


# "startup" is a registry entry, not a config value, so it stays with the
# machine rather than travelling inside config.json. The tick is mirrored into
# start_with_windows only so a fresh copy of the app knows what to do on first
# launch.
def apply_values(cfg, v):
    """Write form values back, preserving every other key (the _comment_ ones
    included - they are the only documentation inside config.json)."""
    cfg.setdefault("channels", {})
    pop = cfg["channels"].setdefault("popup", {})
    pop["enabled"] = bool(v["popup_on"])
    pop["duration_seconds"] = int(v["duration"])
    pop["max_visible"] = int(v["max_visible"])

    poll = cfg.setdefault("poll", {})
    poll["burst_interval_seconds"] = float(v["burst_interval"])
    poll["burst_window_seconds"] = float(v["burst_window"])
    poll["burst_lead_seconds"] = float(v["burst_lead"])
    poll["baseline_interval_seconds"] = float(v["baseline"])
    poll["prewarm_lead_seconds"] = float(v["prewarm_lead"])
    cfg["latency_budget_seconds"] = float(v["budget"])

    cfg["page_size"] = int(v["page_size"])
    cfg["index_from"] = int(v["index_from"])
    cfg["request_timeout_seconds"] = float(v["timeout"])
    cfg["refusal_cooldown_seconds"] = float(v["cooldown"])
    cfg["stale_feed_warn_minutes"] = int(v["stale_warn"])
    cfg["clients"] = [c for c in ALL_CLIENTS if c in set(v["clients"])]

    if "startup" in v:
        cfg["start_with_windows"] = bool(v["startup"])
    cfg["active_hours"] = {"start": v["start"], "end": v["end"],
                           "days": sorted(int(d) for d in v["days"])}
    cfg["data_dir"] = (v.get("data_dir") or "").strip()
    cfg["log_retention_days"] = int(v.get("retention", 0))
    cfg["log_routine_checks"] = bool(v.get("verbose", False))

    cfg["watchlist"] = _list(v["watchlist"])
    cfg["keywords"] = _list(v["keywords"])
    cfg["exclude_keywords"] = merge_excludes(v.get("exclude_presets"),
                                             v.get("exclude"))

    tg = cfg["channels"].setdefault("telegram", {})
    tg["enabled"] = bool(v["tg_on"])
    tg["bot_token"] = v["tg_token"].strip()
    tg["chat_id"] = str(v["tg_chat"]).strip()

    em = cfg["channels"].setdefault("email", {})
    em["enabled"] = bool(v["em_on"])
    em["smtp_host"] = v["em_host"].strip()
    em["smtp_port"] = int(v["em_port"])
    em["username"] = v["em_user"].strip()
    em["password"] = v["em_pass"]
    em["to"] = _list(v["em_to"])
    return cfg


def save(path, v):
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    apply_values(cfg, v)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return cfg


# ------------------------------------------------------------------- the window

_open = {"win": None}


def open_window(config_path, on_saved=None, on_test=None, on_verify=None):
    """Build and run the settings window. Blocks until closed, so the tray
    calls it on its own thread with its own mainloop."""
    import tkinter as tk
    from tkinter import ttk, messagebox

    if _open["win"] is not None:                    # already showing
        try:
            _open["win"].deiconify()
            _open["win"].lift()
            return
        except Exception:
            _open["win"] = None

    try:
        with open(config_path, encoding="utf-8") as fh:
            _cfg = json.load(fh)
    except (OSError, ValueError):
        _cfg = {}
    v = read_values(_cfg)

    root = tk.Tk()
    _open["win"] = root
    root.title(idx3.APP + " - Options")
    root.geometry("660x640")
    root.minsize(600, 560)
    try:
        root.attributes("-topmost", True)
        root.after(400, lambda: root.attributes("-topmost", False))
    except Exception:
        pass

    # PACK ORDER MATTERS. The button bar is created and packed against the
    # bottom edge FIRST, before the notebook claims the rest with expand=True.
    # Packed the other way round, a tab whose content is taller than the window
    # squeezes Save and Cancel straight off the screen, and the only way to
    # reach them is to drag the window bigger - which is exactly what happened
    # at the old default size.
    bar = ttk.Frame(root)
    bar.pack(side="bottom", fill="x", padx=14, pady=(6, 12))

    nb = ttk.Notebook(root)
    nb.pack(side="top", fill="both", expand=True, padx=10, pady=(10, 0))
    V = {}
    MUTED = "#6b7280"

    def page(name):
        f = ttk.Frame(nb, padding=14)
        nb.add(f, text=name)
        f.columnconfigure(1, weight=1)
        return f

    def row(parent, r, label, widget, hint=None):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=4)
        widget.grid(row=r, column=1, sticky="ew", pady=4, padx=(10, 0))
        if hint:
            ttk.Label(parent, text=hint, foreground=MUTED,
                      font=("Segoe UI", 8), wraplength=340).grid(
                          row=r + 1, column=1, sticky="w", padx=(10, 0))

    # ---------------------------------------------------------- Speed
    p = page("Speed")
    ttk.Label(p, text="How hard to poll around IDX's :00 / :30 batches",
              font=("Segoe UI", 9, "bold")).grid(row=0, column=0, columnspan=2,
                                                 sticky="w", pady=(0, 8))
    V["burst_interval"] = tk.DoubleVar(value=v["burst_interval"])
    row(p, 1, "Burst every (s)",
        ttk.Spinbox(p, from_=0.2, to=60, increment=0.5,
                    textvariable=V["burst_interval"]),
        "keep this above the time one check takes, or checks pile up")
    V["burst_window"] = tk.DoubleVar(value=v["burst_window"])
    row(p, 3, "Burst lasts (s)",
        ttk.Spinbox(p, from_=0, to=900, increment=10,
                    textvariable=V["burst_window"]),
        "how long to stay fast after each :00 / :30")
    V["baseline"] = tk.DoubleVar(value=v["baseline"])
    row(p, 5, "Otherwise every (s)",
        ttk.Spinbox(p, from_=1, to=3600, increment=1,
                    textvariable=V["baseline"]),
        "THE number that matters: an off-cycle filing takes at most this long "
        "to spot")
    V["budget"] = tk.DoubleVar(value=v["budget"])
    row(p, 7, "Alert budget (s)",
        ttk.Spinbox(p, from_=5, to=3600, increment=5, textvariable=V["budget"]),
        "anything slower than this is flagged in the log and in --stats")

    ttk.Separator(p, orient="horizontal").grid(row=9, column=0, columnspan=2,
                                               sticky="ew", pady=(14, 8))
    readout = tk.Text(p, height=5, width=52, borderwidth=0,
                      background=root.cget("background"), font=("Consolas", 9),
                      highlightthickness=0)
    readout.grid(row=10, column=0, columnspan=2, sticky="ew")
    readout.configure(state="disabled")

    def refresh_readout(*_a):
        """The two numbers in tension, recomputed as you type. A config that
        breaks the promise should say so here, not in tomorrow's log."""
        try:
            s = budget_summary({k: V[k].get() for k in
                                ("burst_interval", "burst_window", "burst_lead",
                                 "baseline", "prewarm_lead", "budget")})
        except Exception:
            return
        lines = [
            "batch filings      caught in under %.1fs" % s["batch_case"],
            "off-cycle filings  caught in under %.1fs   <- worst case" % s["worst_case"],
            "request load       ~%d/hour  (~%d/day)" % (s["per_hour"], s["per_day"]),
            "",
            ("OK - inside your %.0fs budget" % V["budget"].get())
            if s["within_budget"] else
            ("OVER BUDGET - lower the baseline interval"),
        ]
        readout.configure(state="normal")
        readout.delete("1.0", "end")
        readout.insert("1.0", "\n".join(lines))
        readout.configure(state="disabled")

    # ---------------------------------------------------------- Notifications
    p = page("Alerts")
    V["popup_on"] = tk.BooleanVar(value=v["popup_on"])
    ttk.Checkbutton(p, text="Show the desktop popup",
                    variable=V["popup_on"]).grid(row=0, column=0, columnspan=2,
                                                 sticky="w", pady=(0, 8))
    V["duration"] = tk.IntVar(value=v["duration"])
    row(p, 1, "Seconds on screen",
        ttk.Spinbox(p, from_=0, to=300, textvariable=V["duration"]),
        "0 = stays until you close it with the X or a right-click")
    V["max_visible"] = tk.IntVar(value=v["max_visible"])
    row(p, 3, "Rows visible",
        ttk.Spinbox(p, from_=1, to=20, textvariable=V["max_visible"]),
        "more than this and the list scrolls; new alerts merge into the window "
        "already open")
    ttk.Label(p, text=("IDXAlert draws this popup itself. Windows toasts and "
                       "tray balloons can be silenced by Do Not Disturb and by "
                       "per-app notification settings, and when they are, "
                       "nothing appears and nothing reports a problem."),
              foreground=MUTED, font=("Segoe UI", 8), wraplength=430).grid(
                  row=5, column=0, columnspan=2, sticky="w", pady=(14, 0))
    if on_test:
        ttk.Button(p, text="Send test alert", command=on_test).grid(
            row=6, column=1, sticky="e", pady=(14, 0))

    # ---------------------------------------------------------- Filters
    p = page("Filters")
    V["watchlist"] = tk.StringVar(value=v["watchlist"])
    row(p, 0, "Only these tickers", ttk.Entry(p, textvariable=V["watchlist"]),
        "comma separated, e.g. BBCA, TAPG. Empty = every company")
    V["keywords"] = tk.StringVar(value=v["keywords"])
    row(p, 2, "Title must contain", ttk.Entry(p, textvariable=V["keywords"]),
        "empty = any title, e.g. Laporan Keuangan, RUPS, Dividen")
    ttk.Separator(p, orient="horizontal").grid(row=4, column=0, columnspan=2,
                                               sticky="ew", pady=(12, 8))
    ttk.Label(p, text="Never alert on").grid(row=5, column=0, sticky="nw")
    box = ttk.Frame(p)
    box.grid(row=5, column=1, sticky="ew", padx=(10, 0))
    V["exclude_presets"] = {}
    for i, (label, keyword, why) in enumerate(EXCLUDE_PRESETS):
        var = tk.BooleanVar(value=keyword in v["exclude_presets"])
        V["exclude_presets"][keyword] = var
        ttk.Checkbutton(box, text=label, variable=var).grid(row=i, column=0,
                                                            sticky="w")
        ttk.Label(box, text=why, foreground=MUTED, font=("Segoe UI", 8)).grid(
            row=i, column=1, sticky="w", padx=(8, 0))
    V["exclude"] = tk.StringVar(value=v["exclude"])
    row(p, 6, "Also exclude", ttk.Entry(p, textvariable=V["exclude"]),
        "anything else, comma separated")

    # ---------------------------------------------------------- Schedule
    p = page("Schedule")
    V["start"] = tk.StringVar(value=v["start"])
    row(p, 0, "Active from", ttk.Entry(p, textvariable=V["start"]), "HH:MM")
    V["end"] = tk.StringVar(value=v["end"])
    row(p, 2, "Active until", ttk.Entry(p, textvariable=V["end"]), "HH:MM")
    ttk.Label(p, text="Days").grid(row=4, column=0, sticky="nw", pady=(8, 0))
    dayf = ttk.Frame(p)
    dayf.grid(row=4, column=1, sticky="w", padx=(10, 0), pady=(8, 0))
    V["days"] = {}
    for i, d in enumerate(DAYS):
        V["days"][i] = tk.BooleanVar(value=i in v["days"])
        ttk.Checkbutton(dayf, text=d, variable=V["days"][i]).grid(
            row=0, column=i, padx=(0, 4))
    ttk.Separator(p, orient="horizontal").grid(row=6, column=0, columnspan=2,
                                               sticky="ew", pady=(16, 8))
    V["startup"] = tk.BooleanVar(value=idx3startup.is_enabled() or v["startup"])
    ttk.Checkbutton(p, text="Start automatically when I log in",
                    variable=V["startup"]).grid(row=7, column=0, columnspan=2,
                                                sticky="w")
    ttk.Label(p, text=("adds IDXAlert to your user's startup list. "
                       "No admin rights needed."),
              foreground=MUTED, font=("Segoe UI", 8), wraplength=430).grid(
                  row=8, column=0, columnspan=2, sticky="w", pady=(2, 0))

    # ---------------------------------------------------------- Files
    p = page("Files")
    V["data_dir"] = tk.StringVar(value=v["data_dir"])
    ttk.Label(p, text="Data folder  (logs and latency.csv go here)").grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
    ttk.Entry(p, textvariable=V["data_dir"]).grid(row=1, column=0, columnspan=2,
                                                   sticky="ew")
    ttk.Label(p, text="leave empty to keep them next to the app",
              foreground=MUTED, font=("Segoe UI", 8)).grid(
                  row=2, column=0, columnspan=2, sticky="w", pady=(2, 10))

    def browse():
        from tkinter import filedialog
        chosen = filedialog.askdirectory(parent=root, title="Choose a folder")
        if chosen:
            V["data_dir"].set(os.path.normpath(chosen))

    def open_data_folder():
        raw = V["data_dir"].get().strip()
        target = (os.path.expandvars(os.path.expanduser(raw)) if raw
                  else os.path.dirname(config_path))
        try:
            os.makedirs(target, exist_ok=True)
            os.startfile(os.path.normpath(target))
        except Exception:
            pass

    btns = ttk.Frame(p)
    btns.grid(row=3, column=0, columnspan=2, sticky="w")
    ttk.Button(btns, text="Browse...", command=browse).pack(side="left")
    ttk.Button(btns, text="Open folder", command=open_data_folder).pack(
        side="left", padx=6)
    V["retention"] = tk.IntVar(value=v["retention"])
    ttk.Label(p, text="Delete logs older than (days)").grid(row=5, column=0,
                                                            sticky="w",
                                                            pady=(14, 0))
    ttk.Spinbox(p, from_=0, to=3650, textvariable=V["retention"], width=8).grid(
        row=5, column=1, sticky="w", padx=(10, 0), pady=(14, 0))
    ttk.Label(p, text="0 = never delete anything (default)", foreground=MUTED,
              font=("Segoe UI", 8)).grid(row=6, column=0, columnspan=2,
                                         sticky="w")
    V["verbose"] = tk.BooleanVar(value=v["verbose"])
    ttk.Checkbutton(p, text="Log every check, not just real events",
                    variable=V["verbose"]).grid(row=7, column=0, columnspan=2,
                                                sticky="w", pady=(10, 0))
    ttk.Label(p, text="off by default - it would add thousands of lines a day",
              foreground=MUTED, font=("Segoe UI", 8)).grid(
                  row=8, column=0, columnspan=2, sticky="w")
    ttk.Label(p, text="Settings always live beside the app:", foreground=MUTED,
              font=("Segoe UI", 8)).grid(row=9, column=0, columnspan=2,
                                         sticky="w", pady=(14, 0))
    ttk.Label(p, text=config_path, foreground=MUTED, font=("Segoe UI", 8),
              wraplength=440).grid(row=10, column=0, columnspan=2, sticky="w")

    # ---------------------------------------------------------- Phone / Email
    p = page("Phone / Email")
    V["tg_on"] = tk.BooleanVar(value=v["tg_on"])
    ttk.Checkbutton(p, text="Send to Telegram", variable=V["tg_on"]).grid(
        row=0, column=0, columnspan=2, sticky="w")
    V["tg_token"] = tk.StringVar(value=v["tg_token"])
    row(p, 1, "Bot token", ttk.Entry(p, textvariable=V["tg_token"]),
        "from @BotFather")
    V["tg_chat"] = tk.StringVar(value=v["tg_chat"])
    row(p, 3, "Chat id", ttk.Entry(p, textvariable=V["tg_chat"]),
        "api.telegram.org/bot<TOKEN>/getUpdates")
    ttk.Separator(p, orient="horizontal").grid(row=5, column=0, columnspan=2,
                                               sticky="ew", pady=10)
    V["em_on"] = tk.BooleanVar(value=v["em_on"])
    ttk.Checkbutton(p, text="Send email", variable=V["em_on"]).grid(
        row=6, column=0, columnspan=2, sticky="w")
    V["em_host"] = tk.StringVar(value=v["em_host"])
    row(p, 7, "SMTP host", ttk.Entry(p, textvariable=V["em_host"]))
    V["em_port"] = tk.IntVar(value=v["em_port"])
    row(p, 8, "Port", ttk.Spinbox(p, from_=1, to=65535, textvariable=V["em_port"]))
    V["em_user"] = tk.StringVar(value=v["em_user"])
    row(p, 9, "Username", ttk.Entry(p, textvariable=V["em_user"]))
    V["em_pass"] = tk.StringVar(value=v["em_pass"])
    row(p, 10, "App password", ttk.Entry(p, textvariable=V["em_pass"], show="*"))
    V["em_to"] = tk.StringVar(value=v["em_to"])
    row(p, 11, "Send to", ttk.Entry(p, textvariable=V["em_to"]), "comma separated")

    # ---------------------------------------------------------- Advanced
    p = page("Advanced")
    V["page_size"] = tk.IntVar(value=v["page_size"])
    row(p, 0, "Items per fetch",
        ttk.Spinbox(p, from_=1, to=500, textvariable=V["page_size"]),
        "must exceed a normal batch - IDX publishes up to 27 at once")
    V["index_from"] = tk.IntVar(value=v["index_from"])
    row(p, 2, "indexFrom", ttk.Spinbox(p, from_=0, to=50,
                                       textvariable=V["index_from"]),
        "Leave at 0. This is a page number, not a row offset - any other value "
        "fetches an older page and alerts arrive late.")
    V["stale_warn"] = tk.IntVar(value=v["stale_warn"])
    row(p, 4, "Warn if feed older than (min)",
        ttk.Spinbox(p, from_=0, to=1440, textvariable=V["stale_warn"]),
        "a healthy feed's newest item is never old; 0 disables the check")
    V["timeout"] = tk.DoubleVar(value=v["timeout"])
    row(p, 6, "Request timeout (s)",
        ttk.Spinbox(p, from_=2, to=120, increment=1, textvariable=V["timeout"]))
    V["cooldown"] = tk.DoubleVar(value=v["cooldown"])
    row(p, 8, "Cool a refused client for (s)",
        ttk.Spinbox(p, from_=10, to=3600, increment=30,
                    textvariable=V["cooldown"]),
        "if a connection is refused it sits out this long while the others "
        "take over")
    ttk.Label(p, text="Connection methods (fastest working one first)").grid(
        row=10, column=0, sticky="nw", pady=(10, 0))
    cbox = ttk.Frame(p)
    cbox.grid(row=10, column=1, sticky="w", padx=(10, 0), pady=(10, 0))
    V["clients"] = {}
    for i, name in enumerate(ALL_CLIENTS):
        var = tk.BooleanVar(value=name in v["clients"])
        V["clients"][name] = var
        ttk.Checkbutton(cbox, text=name, variable=var).grid(row=i, column=0,
                                                            sticky="w")
    V["burst_lead"] = tk.DoubleVar(value=v["burst_lead"])
    row(p, 12, "Start burst before :00/:30 (s)",
        ttk.Spinbox(p, from_=0, to=60, increment=1,
                    textvariable=V["burst_lead"]))
    V["prewarm_lead"] = tk.DoubleVar(value=v["prewarm_lead"])
    row(p, 14, "Pre-warm socket before (s)",
        ttk.Spinbox(p, from_=1, to=120, increment=1,
                    textvariable=V["prewarm_lead"]),
        "opens the connection early so the first check is instant; keep this "
        "longer than the burst lead")
    if on_verify:
        ttk.Button(p, text="Verify feed now", command=on_verify).grid(
            row=16, column=1, sticky="e", pady=(12, 0))

    for key in ("burst_interval", "burst_window", "burst_lead", "baseline",
                "prewarm_lead", "budget"):
        V[key].trace_add("write", refresh_readout)
    refresh_readout()

    # ---------------------------------------------------------- buttons
    status = ttk.Label(bar, text="", foreground="#2f7d4f")

    def collect():
        out = {}
        for k, var in V.items():
            if k == "exclude_presets":
                out[k] = {kw for kw, b in var.items() if b.get()}
            elif k == "clients":
                out[k] = [n for n, b in var.items() if b.get()]
            elif isinstance(var, dict):              # the day checkboxes
                out[k] = [i for i, b in var.items() if b.get()]
            else:
                out[k] = var.get()
        return out

    def close_win():
        _open["win"] = None
        root.destroy()

    def do_save(close=True):
        vals = collect()
        problems = validate(vals)
        if problems:
            messagebox.showerror("Check these",
                                 "\n".join("- " + p for p in problems),
                                 parent=root)
            return
        save(config_path, vals)
        if idx3startup.supported():
            want = bool(vals.get("startup"))
            if want != idx3startup.is_enabled():
                ok, detail = idx3startup.set_enabled(want)
                if not ok:
                    messagebox.showwarning(
                        "Startup",
                        "Could not change the startup setting:\n%s" % detail,
                        parent=root)
        if on_saved:
            on_saved()
        if close:
            close_win()
        else:
            status.config(text="Saved")
            root.after(1800, lambda: status.config(text=""))

    def show_about():
        win = tk.Toplevel(root)
        win.title("About IDXAlert")
        win.resizable(False, False)
        win.transient(root)
        body = ttk.Frame(win, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=idx3.APP,
                  font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(body, text="Watches IDX for new disclosures and tells you "
                             "within a minute.",
                  foreground=MUTED, wraplength=340).pack(anchor="w",
                                                         pady=(2, 16))
        ttk.Label(body, text="Made by " + AUTHOR,
                  font=("Segoe UI", 10)).pack(anchor="w")

        link = tk.Label(body, text=LINKEDIN_LABEL,
                        foreground="#2563eb", cursor="hand2",
                        font=("Segoe UI", 9, "underline"))
        link.pack(anchor="w", pady=(4, 0))

        def open_link(_e=None):
            import webbrowser
            webbrowser.open(LINKEDIN)

        link.bind("<Button-1>", open_link)
        link.bind("<Enter>", lambda _e: link.config(foreground="#1d4ed8"))
        link.bind("<Leave>", lambda _e: link.config(foreground="#2563eb"))

        ttk.Button(body, text="Close", command=win.destroy).pack(
            anchor="e", pady=(20, 0))
        win.update_idletasks()
        # centre on the Options window rather than the screen, so it lands
        # where the user is already looking
        x = root.winfo_rootx() + (root.winfo_width() - win.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - win.winfo_height()) // 3
        win.geometry("+%d+%d" % (max(0, x), max(0, y)))
        try:
            win.grab_set()
        except Exception:
            pass

    ttk.Button(bar, text="About", command=show_about).pack(side="left")
    status.pack(side="left", padx=(10, 0))
    ttk.Button(bar, text="Cancel", command=close_win).pack(side="right")
    ttk.Button(bar, text="Save", command=do_save).pack(side="right", padx=6)
    ttk.Button(bar, text="Apply", command=lambda: do_save(False)).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", close_win)
    root.mainloop()
    _open["win"] = None


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    open_window(os.path.join(here, "config.json"))
