#!/usr/bin/env python3
"""
idx3sched - when to pull.

IDXAlert 2.x slept for N seconds and woke up wherever it landed. That drifts:
each cycle adds the request duration to the wait, so after an hour the "5s
burst" is landing at arbitrary offsets from the boundary it was aimed at.

It schedules against ABSOLUTE targets on a fixed grid instead. Every tick
knows the exact instant it is due; the loop sleeps the remainder. Drift cannot
accumulate because nothing is ever measured relative to the previous tick.

The grid is anchored to IDX's clock, not the PC's - see Skew in idx3net. IDX
publishes on the half hour, and epoch-seconds mod 1800 lines up with :00/:30
in any whole-hour timezone, which WIB (UTC+7) is.

Shape of one 30-minute cycle:

    B-10s   prewarm      open/refresh the socket so the burst pays no handshake
    B-3s    burst starts poll every 1s - IDX has published as early as B+0.5s
    B        <-- IDX publishes its batch here
    B+90s   burst ends   the tail catches stragglers in the same dump
    ...     baseline     every ~22s, so an OFF-CYCLE filing is still caught
            within 60s   well inside the one-minute budget
    B'-10s  prewarm      never sleep past this
"""

import random
import time

PERIOD = 1800.0          # IDX publishes on the hour and the half hour

DEFAULTS = {
    "burst_lead_seconds": 3.0,
    "burst_window_seconds": 90.0,
    "burst_interval_seconds": 1.0,
    "baseline_interval_seconds": 22.0,
    "prewarm_lead_seconds": 10.0,
    "baseline_jitter_seconds": 0.4,
}

PREWARM = "prewarm"
BURST = "burst"
BASELINE = "baseline"


def _num(cfg, key):
    try:
        v = float((cfg or {}).get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return DEFAULTS[key]
    return v if v > 0 else DEFAULTS[key] if key != "baseline_jitter_seconds" else 0.0


class Planner:
    """Pure function of time - no internal cursor, so it cannot get out of step
    with reality after a long stall, a laptop sleep or a clock correction."""

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.lead = _num(cfg, "burst_lead_seconds")
        self.window = _num(cfg, "burst_window_seconds")
        self.fast = _num(cfg, "burst_interval_seconds")
        self.base = _num(cfg, "baseline_interval_seconds")
        self.prewarm_lead = _num(cfg, "prewarm_lead_seconds")
        self.jitter = _num(cfg, "baseline_jitter_seconds")
        # A burst that opens before the socket is warm defeats the point.
        if self.prewarm_lead <= self.lead:
            self.prewarm_lead = self.lead + 5.0

    # ------------------------------------------------------------ geometry

    def boundaries(self, t):
        """(previous boundary, next boundary) around IDX-time t."""
        prev = (t // PERIOD) * PERIOD
        return prev, prev + PERIOD

    def phase_at(self, t):
        prev, nxt = self.boundaries(t)
        if t <= prev + self.window:
            return BURST
        if t >= nxt - self.lead:
            return BURST
        return BASELINE

    def seconds_since_boundary(self, t):
        """Signed: negative in the pre-boundary lead-in, positive after."""
        prev, nxt = self.boundaries(t)
        after = t - prev
        before = t - nxt
        return after if abs(after) <= abs(before) else before

    # ----------------------------------------------------------- the grid

    def _burst_grid_after(self, t, boundary):
        """First burst instant strictly after t, inside this boundary's burst."""
        start = boundary - self.lead
        end = boundary + self.window
        if t < start:
            return start
        if t >= end:
            return None
        steps = int((t - start) / self.fast) + 1
        candidate = start + steps * self.fast
        return candidate if candidate <= end else None

    def next_event(self, now):
        """Returns (due_at, kind) for the next thing that should happen.

        Prewarm is a first-class event rather than something bolted onto the
        sleep, so a 22-minute quiet stretch cannot swallow it.
        """
        prev, nxt = self.boundaries(now)

        # Still inside the burst that follows the previous boundary?
        due = self._burst_grid_after(now, prev)
        if due is not None:
            return due, BURST

        prewarm_at = nxt - self.prewarm_lead
        burst_start = nxt - self.lead

        if now < prewarm_at:
            due = now + self.base
            if self.jitter:
                # Perfectly regular 22.000s ticks are a fingerprint. A few
                # hundred ms of noise costs nothing and looks less like a bot.
                due += random.uniform(0.0, self.jitter)
            if due >= prewarm_at:
                return prewarm_at, PREWARM
            return due, BASELINE

        if now < burst_start:
            return prewarm_at if now < prewarm_at else burst_start, (
                PREWARM if now < prewarm_at else BURST)

        return self._burst_grid_after(now, nxt) or burst_start, BURST

    # ------------------------------------------------------------- budget

    def requests_per_hour(self):
        """Sanity number for the config: how hard are we actually pulling?

        Worth printing at startup - the difference between 'aggressive' and
        'guaranteed to get blocked' is a factor of five, and it is easy to set
        a burst interval that quietly triples the day's request count.
        """
        per_burst = int((self.lead + self.window) / self.fast) + 1
        quiet = PERIOD - (self.lead + self.window) - self.prewarm_lead
        per_quiet = max(0, int(quiet / self.base))
        return int((per_burst + per_quiet) * (3600.0 / PERIOD))

    def worst_case_detection(self):
        """Upper bound on publish -> detect for an OFF-CYCLE filing, which is
        the case the batch schedule does nothing for. This is the number that
        has to stay under 60s."""
        return self.base + self.jitter


def sleep_until(target, clock, stop=None, max_slice=1.0):
    """Sleep to an absolute instant on the given clock.

    Waking in slices matters for two reasons: a stop event stays responsive
    (Ctrl+C, tray quit), and a mid-sleep clock correction - a laptop resuming
    from suspend, an NTP step - is noticed within a second instead of being
    slept straight through.
    """
    while True:
        remaining = target - clock()
        if remaining <= 0:
            return True
        if stop is not None and stop.wait(min(remaining, max_slice)):
            return False
        if stop is None:
            time.sleep(min(remaining, max_slice))
