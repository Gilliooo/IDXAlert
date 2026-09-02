# IDXAlert

Desktop watcher for Indonesia Stock Exchange company disclosures. Polls the
[Keterbukaan Informasi](https://www.idx.co.id/id/perusahaan-tercatat/keterbukaan-informasi/)
feed and pops a window the moment something from a company you cover appears.
Click the row, the PDF opens.

Windows tray app. Standard library only for the engine; `pystray` and `pillow`
for the tray, `tkinter` for the popup.

**Measured:** 38 alerts over one trading day, median 22.3s and worst case 22.7s
from "the app last looked" to "the alert is on screen", 100% inside a 60-second
budget.

---

## Contents

- [Setup](#setup) — the one-page guide
- [How it works](#how-it-works)
- [Two bugs worth reading about](#two-bugs-worth-reading-about)
- [Testing](#testing)
- [Building](#building)
- [Project layout](#project-layout)

---

# Setup

## 1. Find it in the tray

Run `IDXAlert3.exe`. Nothing visible happens. Click the chevron beside the
clock; IDXAlert is the green bar chart. Keep the folder somewhere permanent,
not Downloads or a syncing folder, because the app writes its settings and log
beside itself.

## 2. Right-click the icon

```
Check now
Status
Verify feed          <- run this once
Send test notification
─────────────────────
Options...           <- then this
─────────────────────
Pause
─────────────────────
Open today's log
Open latency.csv
Edit config.json by hand
Open folder
Open IDX page
─────────────────────
Quit
```

**Verify feed** should say it is reading the newest page. That is the one check
that proves it is on live data.

## 3. Options, Filters tab

Type your tickers, tick four boxes, Save.

```
Only these tickers   [ BBCA, BBRI, BMRI, BBNI, BRIS, BTPS, ARTO        ]
                       comma separated. empty = every company on IDX

Title must contain   [                                                  ]
                       leave empty. you want everything these names file

Never alert on       [x] Structured Warrant
                     [x] Laporan Kepemilikan
                     [x] Bukti Iklan
                     [ ] Obligasi / Sukuk
                     [x] ETF daily NAV
                     [ ] Penghentian Sementara

Also exclude         [ Laporan Bulanan Registrasi                       ]
                       comma separated. optional
```

A coal desk instead: `ADRO, ITMG, PTBA, HRUM, INCO, ANTM, MDKA, TINS`

Those four ticked boxes remove about a third of the feed, measured across 800
filings over 8 days: ETF daily NAV 21.9%, Laporan Kepemilikan 7.5%, Structured
Warrant 4.0%, Bukti Iklan 3.4%. Leave the last two unticked. Bonds and trading
suspensions are news on most desks.

Then **Schedule** tab, tick *Start automatically when I log in*. Settings save
instantly, no restart.

### How matching works

- **Not case sensitive.** `bbca`, `BBCA` and `BbCa` are identical.
- **Matches inside words.** Excluding `RUPS` also kills `RUPST`. Use long phrases.
- **The ticker is searched by the keyword boxes too**, not just the title, so a
  short exclude word can wipe out a company by accident.

## 4. What the icon means

| | |
|---|---|
| 🟢 Green | Running. Nothing to do. |
| 🟠 Amber | Paused. Right-click, Resume. |
| 🔵 Blue | Feed looks stale. Right-click, Verify feed. |
| 🔴 Red | Last check failed. Right-click, Status. |

Blue is the one worth knowing. It means the app is working normally but the
newest thing in the feed is suspiciously old, which is what a wrong page or a
changed API looks like from the inside and has no other symptom.

## 5. When it fires

| | |
|---|---|
| Left-click a row | opens the PDF. The window stays open. |
| Right-click or ✕ | closes it. It never closes by itself. |
| Several files | click to pick one, or Open all. |

New alerts merge into the window already open, newest at the top, rather than
stacking up as separate windows.

## If it goes wrong

| | |
|---|---|
| No alerts | Filters too tight. Empty both boxes, confirm alerts start, then add back one at a time. |
| Too many | Tick the four boxes. Narrow your ticker list. |
| Red icon | Usually a VPN. IDX refuses traffic from data centres and some VPNs look like one. |
| Double alerts | Two copies running. Quit both, start one. |
| Nothing on launch | Already running. Check the tray first. |

## The one thing to know

The time on each row is **when the company filed**, not when you were told. IDX
takes a few minutes to publish after a company submits, median 190 seconds
measured. IDXAlert reaches you under a minute after that. A row showing 19:13
at 19:16 is working correctly.

Every alert also logs the full trail:

```
[2026-09-01 15:06:01] 1 new disclosure  [baseline B+361s via keepalive, 111ms, ours +22s]
  DAYA   15:05:00  (ours +22.4s)
         Perubahan Komite Audit
         filed 14:59:56   published 15:05:00 (+5m04s idx)   alerted 15:06:01 (+1m01s)
         https://www.idx.co.id/StaticData/...pdf
```

---

# How it works

## The metric

The obvious measure, `CreatedDate → alerted`, is wrong. `CreatedDate` is when
IDX created the *record*, not when it began serving the item, and no field
exposes the latter. Measuring against it charges IDX's internal lag to the app.

The accountable number is **`our_lag`: the interval between the previous
successful poll and this one.** If an item was absent 22 seconds ago and present
now, the app delayed it by at most 22 seconds, whatever any timestamp claims.
That bound is exact, needs no trust in IDX's clock, and is the only quantity
the polling schedule can move. `idx3.py --stats` reports it.

Every alert is classified `ok`, `slow` (ours) or `late_visible` (IDX served it
long after stamping it). A *flood* of `late_visible` is the signature of reading
a stale slice of the feed, not an exchange quirk.

## The engine

**Sockets stay open.** One HTTP/1.1 connection reused across hundreds of polls
rather than a fresh TLS handshake each time. Measured only ~60ms faster per poll
than a reconnecting client, because TLS 1.3 resumption absorbs most of the
handshake, but it opens far fewer connections per day, which is what
rate-limiting notices.

**Ticks are absolute, not relative.** `sleep(N)` drifts: each cycle adds the
request duration to the wait. The scheduler computes the exact instant each tick
is due and sleeps the remainder, so drift cannot accumulate.

**The grid is anchored to IDX's clock, not the PC's.** Every response carries a
`Date` header; a rolling median of the offset is the correction. A machine
running 20 seconds fast would otherwise poll hard at its own idea of the
half-hour and miss the real one, invisibly.

**Nothing slow sits between the bytes and the alert.** The poll thread does one
thing with a new disclosure: `queue.put()`. Log formatting, the CSV row and the
state write all happen on a dispatcher thread afterwards.

**Four HTTP clients rotate by health.** A 403 cools that client for five minutes
and falls through to the next one inside the same tick, so a block costs
milliseconds rather than a missed disclosure.

## The tray owns no timing

The engine owns the poll loop; the tray is only a face. It starts a `Watcher` on
a background thread, hangs a popup channel off its dispatcher, and turns menu
clicks into requests the engine honours on its own schedule. "Check now" sets a
flag the loop reads. It never polls from the click thread, because two threads
polling at once double-alert.

Config is re-read every tick, so Options changes apply with no restart. Code
changes do not: a `.py` edit reaches nothing already running.

---

# Two bugs worth reading about

## `indexFrom` is a page number, not a row offset

Every version of this app sent `indexFrom=1`, believing it meant "start at row
1". It is a **0-based page number**, so the app was reading page *two* and had
never seen the newest 30 disclosures.

Nothing errored. The responses were valid JSON in the documented shape, just the
wrong slice. The symptom was distinctive and easy to misread: alerts arrived
hours late, and every new upload produced an alert naming some *other, older*
document, the one that had just been pushed across the page boundary. It was
diagnosed as a latency problem for two days.

Proof: with `pageSize=10`, pages 0+1+2 concatenated are byte-identical to page 0
at `pageSize=30`, overlap zero.

Two guards now exist so the class of failure cannot hide again:

- `Watcher._check_freshness()` warns when the newest item in the feed is older
  than `stale_feed_warn_minutes`. A healthy feed's newest item is never old.
- `idx3.py --verify-feed` asserts page 0's newest beats page 1's, with no overlap.

Either would have caught it on day one.

## A missing TLS extension reads as "not a browser"

The keep-alive client was refused with 403 while `urllib`, sending an identical
header set to the same host, sailed through. The difference was not HTTP at all.

`http.client.HTTPSConnection` only sets the TLS **ALPN** extension when it builds
the `SSLContext` *itself*. Passing your own `ssl.create_default_context()` skips
that block, and the ClientHello then advertises no ALPN whatsoever. Every real
browser sends it, so its absence is a loud fingerprint.

Verified directly: the old context negotiated `None`, the fixed one negotiates
`http/1.1`.

**If you hand a custom `SSLContext` to `http.client`, set
`set_alpn_protocols(["http/1.1"])` on it.**

---

# Testing

Seven suites, 160+ checks. None need a network or a display.

```
python idx3.py --selftest    parser, filters, scheduler, pagination, freshness,
                             the poll path end to end
python test_keepalive.py     socket reuse, counted as real TCP accepts
python test_popup.py         the popup rendered against a fake tkinter
python test_options.py       config <-> form round trip and validation
python test_window.py        the whole Options window built against a fake toolkit
python test_tray.py          tray wiring against a fake pystray
python test_filters.py       Options form -> config.json -> engine -> popup
```

or `test_all.bat`.

**Two patterns here are the point.**

*Fake toolkits, not skips.* An earlier headless test passed **vacuously**: the
code raised at `import tkinter` long before reaching the layout bug the test was
meant to catch, and reported success. A skipped test looks identical to a passing
one. So the GUI suites inject stub toolkits into `sys.modules` and drive the real
render to completion, recording every binding and geometry call. Two expensive
bugs are now assertions rather than folklore: that Tk does not bubble events to
parents, and that variable row heights make summed `winfo_reqheight()` the wrong
way to size a scroll area.

*Test the chain, not just the units.* `matches()` was covered and the Options
form mapping was covered, yet nothing proved the thing a user cares about: I
typed tickers into Options, does the popup now show only those? That chain is
form → `config.json` on disk → `load_config()` → `poll()` drops the rest, and a
break in any link looks identical from outside. `test_filters.py` drives the
whole chain with real filing titles and asserts on what reaches the dispatcher.

---

# Building

```
build3.bat        ->  dist\IDXAlert3.exe
```

Runs all seven suites first and refuses to build if any fail. Single file, no
console.

The .exe reads `config.json`, `seen.json`, `latency.csv` and `logs\` from **its
own** folder, not from the source tree. Editing Options in the .exe does not
change the source config, and vice versa; that two-config split caused duplicate
notifications in different styles once. `build3.bat` will not overwrite an
existing `dist\config.json`.

Running from source instead:

```
pip install pystray pillow
python idx3tray.py        the tray app
python idx3.py            headless, alerts to console and today's log
python idx3.py --plan     the tick schedule and request budget
python idx3.py --probe    measure request latency per client
python idx3.py --verify-feed
python idx3.py --stats [PATH]
```

---

# Project layout

| | |
|---|---|
| `idx3.py` | engine, poll loop, dispatcher, side channels, CLI, selftest |
| `idx3net.py` | HTTP: keep-alive pool, client rotation, cooldowns, clock skew |
| `idx3sched.py` | the absolute tick grid |
| `idx3lat.py` | latency CSV and the `--stats` report |
| `idx3popup.py` | the stacking desktop popup |
| `idx3tray.py` | tray app; a face over the engine, owns no timing |
| `idx3options.py` | settings window; the mapping half is pure and testable |
| `idx3startup.py` | run-at-login via the per-user Run key |
| `config.example.json` | copy to `config.json`, or let the app write its own |

Version lives in one place, `idx3.VERSION`. A selftest check fails the build if
a version string is hardcoded anywhere else.

---

Made by Bill Grandy Tunjung · [linkedin.com/in/bill-tunjung](https://www.linkedin.com/in/bill-tunjung)
