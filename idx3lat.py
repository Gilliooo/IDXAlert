#!/usr/bin/env python3
"""
idx3lat - the proof.

The one-minute requirement is a claim about a distribution, not about a lucky
poll, so every alert writes a row and `idx3.py --stats` reads them back. If the
p95 is not under 60s, the config is wrong and this file is how you find out.

Three timestamps per disclosure, and it matters which is which:

    filed      TglPengumuman - when the company submitted. IDX's problem.
    published  CreatedDate   - when IDX put it on the wire. The clock starts here.
    detected   ours          - when our fetch returned it.

filed -> published is IDX's internal batching, measured at a ~18 minute median
and completely untouchable by any polling rate. published -> detected is the
only gap this app controls, and it is the one the requirement is about. Keeping
them in separate columns stops a good result being buried under IDX's delay.

CreatedDate is stamped by IDX's clock at 1-second resolution. We measure our
offset from that clock (Skew, in idx3net) and store it per row, so a PC running
20 seconds fast cannot silently flatter - or slander - the numbers.
"""

import csv
import os
from datetime import datetime

COLUMNS = [
    "detected_at",          # local wall clock, ISO
    "key",
    "ticker",
    "filed",                # TglPengumuman
    "published",            # CreatedDate
    "our_lag_s",            # THE number we are accountable for - see below
    "publish_to_detect_s",  # CreatedDate -> us. Contaminated by IDX; see below
    "file_to_publish_s",    # IDX's own batching lag, for context
    "verdict",              # ok | slow | late_visible
    "phase",                # burst | baseline
    "since_boundary_s",     # where in the 30-min cycle it landed
    "client",               # which HTTP client won the tick
    "fetch_ms",
    "page_size",
    "clock_skew_s",
    "matched",              # did it pass the filters (1) or is it context (0)
    "title",
]

# WHY THERE ARE TWO LATENCY COLUMNS
#
# The obvious metric, CreatedDate -> detected, turned out to be wrong. On the
# first live run (2026-09-01) it reported gaps of twelve hours. Nothing was
# broken: those filings carried a CreatedDate from the previous evening but
# only ENTERED the feed the next morning. IDXAlert 2.x, polling the same API,
# picked them up in the same two-minute window - so the twelve hours were IDX's
# visibility delay, not our polling delay.
#
# CreatedDate is when IDX created the record. It is NOT reliably when the item
# became fetchable. Treating it as the start of our clock measures IDX and
# blames us.
#
# our_lag_s is the honest one: the interval between the previous SUCCESSFUL
# poll and this one. If an item was absent 22 seconds ago and present now, our
# contribution to its delay is at most 22 seconds - whatever CreatedDate says.
# That bound is exact, needs no trust in IDX's clock, and is the only number
# the polling schedule can actually move. It is what the budget is checked
# against.
LATE_VISIBLE_SLACK = 120.0


def _parse(text):
    if not text:
        return None
    try:
        return datetime.strptime(str(text)[:19].replace("T", " "),
                                 "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def classify(publish_to_detect, our_lag, budget=60.0):
    """Which of the two of us was slow, IDX or this app."""
    if our_lag is None:
        return "unknown"
    if our_lag > budget:
        return "slow"                       # genuinely ours: down, blocked, or too slow a loop
    if publish_to_detect is None:
        return "ok"
    if publish_to_detect > our_lag + LATE_VISIBLE_SLACK:
        return "late_visible"               # IDX published the record long before serving it
    return "ok"


def gaps(item, detected_at, skew=0.0):
    """(publish->detect, file->publish) in seconds, either may be None.

    `detected_at` is a local epoch; `skew` converts it onto IDX's clock, which
    is the clock `published` was written with. Comparing the two without that
    correction is the classic way to report a negative latency.
    """
    filed = _parse(item.get("posted"))
    pub = _parse(item.get("created"))
    to_detect = None
    to_publish = None
    if pub is not None:
        idx_now = datetime.fromtimestamp(detected_at + skew)
        to_detect = (idx_now - pub).total_seconds()
    if pub is not None and filed is not None:
        to_publish = (pub - filed).total_seconds()
    return to_detect, to_publish


class Recorder:
    def __init__(self, path):
        self.path = path
        self._ready = False
        self._checked = False

    def _retire_if_stale(self):
        """The column set changed once already (when our_lag replaced
        publish_to_detect as the budget metric). Appending new rows under an
        old header silently misaligns every column, and the misalignment only
        shows up as nonsense in a report you were relying on. So: if the header
        on disk is not the current one, move that file aside and start clean.
        """
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            return
        try:
            with open(self.path, newline="", encoding="utf-8") as fh:
                header = next(csv.reader(fh), [])
        except OSError:
            return
        if header == COLUMNS:
            return
        for n in range(1, 50):
            retired = "%s.v%d" % (self.path, n)
            if not os.path.exists(retired):
                break
        else:
            return
        try:
            os.replace(self.path, retired)
            print("latency.csv used an older column set - moved it to %s "
                  "and started a fresh one" % os.path.basename(retired))
        except OSError:
            pass

    def _open(self):
        if not self._checked:
            self._checked = True
            self._retire_if_stale()
        new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError:
            pass
        fh = open(self.path, "a", newline="", encoding="utf-8")
        w = csv.writer(fh)
        if new:
            w.writerow(COLUMNS)
        return fh, w

    def record(self, item, detected_at, ctx):
        """Never allowed to raise. A full disk must not cost an alert."""
        try:
            to_detect, to_publish = gaps(item, detected_at,
                                         ctx.get("skew", 0.0) or 0.0)
            our_lag = ctx.get("our_lag")
            fh, w = self._open()
            with fh:
                w.writerow([
                    datetime.fromtimestamp(detected_at).isoformat(" ", "seconds"),
                    item.get("key", ""),
                    item.get("ticker", ""),
                    item.get("posted", ""),
                    item.get("created", ""),
                    "" if our_lag is None else "%.2f" % our_lag,
                    "" if to_detect is None else "%.2f" % to_detect,
                    "" if to_publish is None else "%.0f" % to_publish,
                    classify(to_detect, our_lag, ctx.get("budget", 60.0)),
                    ctx.get("phase", ""),
                    "%.1f" % (ctx.get("since_boundary", 0.0) or 0.0),
                    ctx.get("client", ""),
                    "" if ctx.get("fetch_ms") is None else "%.0f" % (ctx["fetch_ms"] * 1000),
                    ctx.get("page_size", ""),
                    "%.2f" % (ctx.get("skew", 0.0) or 0.0),
                    1 if ctx.get("matched", True) else 0,
                    (item.get("title", "") or "")[:160],
                ])
            self._ready = True
        except Exception:
            pass


# ------------------------------------------------------------------- report

def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = min(len(sorted_vals) - 1, max(0, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def summarise(path, budget=60.0):
    if not os.path.exists(path):
        return "no measurements yet - %s does not exist" % path
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
    if header != COLUMNS and "our_lag_s" not in header:
        return ("%s was written by an older version with a different column "
                "set.\nIt will be moved aside automatically on the next alert; "
                "delete it now if you want a clean start." % path)
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append(r)
    if not rows:
        return "no rows in %s" % path

    def col(subset, name):
        out = []
        for r in subset:
            try:
                out.append(float(r.get(name) or ""))
            except ValueError:
                pass
        return sorted(out)

    def block(label, subset, name):
        vals = col(subset, name)
        if not vals:
            return "  %-22s (none)" % label
        under = sum(1 for v in vals if v <= budget)
        return ("  %-22s n=%-4d  median %6.1fs   p90 %6.1fs   p95 %6.1fs   "
                "max %7.1fs   within %ds: %d/%d (%.0f%%)"
                % (label, len(vals), _pct(vals, 50), _pct(vals, 90),
                   _pct(vals, 95), vals[-1], int(budget), under, len(vals),
                   100.0 * under / len(vals)))

    burst = [r for r in rows if r.get("phase") == "burst"]
    base = [r for r in rows if r.get("phase") == "baseline"]
    late = [r for r in rows if r.get("verdict") == "late_visible"]
    slow = [r for r in rows if r.get("verdict") == "slow"]

    out = [
        "OUR LAG  (previous successful poll -> this one)",
        "the interval this app is accountable for, and what the budget checks",
        block("all", rows, "our_lag_s"),
        block("batch window", burst, "our_lag_s"),
        block("off-cycle", base, "our_lag_s"),
        "",
        "  verdict: %d ok, %d slow (ours), %d late-visible (IDX served it late)"
        % (len(rows) - len(late) - len(slow), len(slow), len(late)),
    ]
    if slow:
        out.append("  slowest ours: " + ", ".join(
            "%s %ss" % (r.get("ticker") or "-", r.get("our_lag_s"))
            for r in sorted(slow, key=lambda r: -float(r.get("our_lag_s") or 0))[:5]))

    out += ["",
            "CreatedDate -> alert  (context only - contaminated by IDX's",
            "visibility delay, which is why it is not the budget metric)",
            block("all", rows, "publish_to_detect_s")]
    if late:
        lv = col(late, "publish_to_detect_s")
        out.append("  of which late-visible: n=%d, median %.0f min, max %.0f min"
                   % (len(lv), _pct(lv, 50) / 60.0, lv[-1] / 60.0))

    by_client = {}
    for r in rows:
        by_client.setdefault(r.get("client", "?"), []).append(r)
    out += ["", "by HTTP client  (our lag)"]
    for name, subset in sorted(by_client.items()):
        out.append(block(name, subset, "our_lag_s"))

    idx_lag = col(rows, "file_to_publish_s")
    if idx_lag:
        out += ["",
                "filed -> CreatedDate  (IDX's own batching - not ours to fix)",
                "  median %.0f min   p95 %.0f min   n=%d"
                % (_pct(idx_lag, 50) / 60.0, _pct(idx_lag, 95) / 60.0, len(idx_lag))]

    fetch = col(rows, "fetch_ms")
    if fetch:
        out += ["",
                "fetch round-trip   median %.0fms   p95 %.0fms"
                % (_pct(fetch, 50), _pct(fetch, 95))]
    return "\n".join(out)
