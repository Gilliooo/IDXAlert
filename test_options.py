#!/usr/bin/env python3
"""
test_options.py - the config <-> form mapping, with no display required.

read_values / apply_values / validate are pure dict functions precisely so this
can be a real test rather than a smoke test. The failure mode being guarded
against is a silent one: an Options window that saves a config the engine then
reads differently, or that quietly drops a key it did not know about.

Run:  python test_options.py
"""

import copy
import json
import os
import tempfile

import idx3
import idx3options as O


def main():
    failures = []

    def check(name, cond, detail=""):
        print("  %-56s %s%s" % (name, "PASS" if cond else "FAIL",
                                "" if cond else "   <- " + str(detail)))
        if not cond:
            failures.append(name)

    print("options mapping test\n")

    base = copy.deepcopy(idx3.DEFAULT_CONFIG)

    # -- round trip ------------------------------------------------------
    v = O.read_values(base)
    out = O.apply_values(copy.deepcopy(base), v)
    check("defaults survive a read/apply round trip unchanged",
          out == base,
          [k for k in set(out) | set(base) if out.get(k) != base.get(k)])
    check("no validation complaints about the shipped defaults",
          O.validate(v) == [], O.validate(v))

    # -- the _comment_ keys ARE the documentation inside config.json -----
    check("comment keys are preserved through a save",
          all(k in out for k in base if k.startswith("_comment")),
          [k for k in base if k.startswith("_comment") and k not in out])

    # -- an unknown key the UI does not model must not be dropped --------
    extended = copy.deepcopy(base)
    extended["some_future_key"] = {"a": 1}
    kept = O.apply_values(extended, O.read_values(extended))
    check("keys the form does not know about are left alone",
          kept.get("some_future_key") == {"a": 1})

    # -- validation ------------------------------------------------------
    def bad(**over):
        w = dict(v)
        w.update(over)
        return O.validate(w)

    check("indexFrom other than 0 is rejected",
          any("page number" in p for p in bad(index_from=1)), bad(index_from=1))
    check("a baseline that breaks the budget is rejected",
          any("over your" in p for p in bad(baseline=120.0)), bad(baseline=120.0))
    check("a baseline inside the budget is accepted",
          not bad(baseline=30.0), bad(baseline=30.0))
    check("pre-warm must precede the burst",
          any("BEFORE" in p for p in bad(prewarm_lead=1.0, burst_lead=3.0)))
    check("a page smaller than a batch is rejected",
          any("part of a batch" in p for p in bad(page_size=10)))
    check("a malformed time is rejected",
          any("07:00" in p for p in bad(start="7am")))
    check("no days selected is rejected", any("day" in p for p in bad(days=[])))
    check("no HTTP clients selected is rejected",
          any("client" in p for p in bad(clients=[])))
    check("Telegram on without credentials is rejected",
          any("Telegram" in p for p in bad(tg_on=True, tg_token="", tg_chat="")))
    check("email on without a recipient is rejected",
          any("Email" in p for p in bad(em_on=True, em_user="a@b.c", em_to="")))

    # -- credits ---------------------------------------------------------
    check("the About box credits the author",
          O.AUTHOR == "Bill Grandy Tunjung", O.AUTHOR)
    check("the LinkedIn link points at the right profile",
          O.LINKEDIN == "https://www.linkedin.com/in/bill-tunjung", O.LINKEDIN)

    # -- no internal build history leaks onto the screen ------------------
    # Version archaeology and endpoint post-mortems belong in the code and the
    # README, not in a hint under a spinbox.
    import io
    ui_text = []
    for line in open(os.path.join(os.path.dirname(os.path.abspath(O.__file__)),
                                  "idx3options.py"), encoding="utf-8"):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue                      # comments are for us, not the user
        ui_text.append(line)
    blob = "".join(ui_text)
    leaks = [t for t in ("2.x", "1.x", "every earlier version", "--probe",
                         "403", "page two", "burst grid") if t in blob]
    check("no version history or jargon in the visible strings", not leaks, leaks)

    # -- budget readout agrees with the scheduler ------------------------
    import idx3sched
    s = O.budget_summary(v)
    p = idx3sched.Planner(base["poll"])
    check("the Speed tab readout matches the real scheduler",
          abs(s["worst_case"] - p.worst_case_detection()) < 0.01
          and s["per_hour"] == p.requests_per_hour(),
          (s, p.worst_case_detection(), p.requests_per_hour()))
    check("the shipped defaults are reported as within budget",
          s["within_budget"], s)

    # -- exclude presets -------------------------------------------------
    merged = O.merge_excludes({"Structured Warrant", "Obligasi"},
                              "Bukti Iklan, Something Else, obligasi")
    check("presets come first and duplicates are dropped, case-insensitively",
          merged == ["Structured Warrant", "Obligasi", "Bukti Iklan",
                     "Something Else"], merged)
    ticked, custom = O.split_excludes(merged)
    check("splitting recovers the ticked presets",
          ticked == {"Structured Warrant", "Obligasi", "Bukti Iklan"}, ticked)
    check("splitting recovers the free text", custom == "Something Else", custom)

    # -- save writes something the engine can actually load --------------
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(base, fh, indent=2)
    changed = dict(v)
    changed["baseline"] = 15.0
    changed["watchlist"] = "BBCA, TAPG"
    changed["exclude_presets"] = {"Structured Warrant"}
    O.save(path, changed)
    with open(path, encoding="utf-8") as fh:
        reloaded = json.load(fh)
    check("saved config keeps the engine's nested shape",
          reloaded["poll"]["baseline_interval_seconds"] == 15.0, reloaded["poll"])
    check("saved filters are in the engine's format",
          reloaded["watchlist"] == ["BBCA", "TAPG"]
          and reloaded["exclude_keywords"] == ["Structured Warrant"],
          (reloaded["watchlist"], reloaded["exclude_keywords"]))

    merged_cfg = idx3._merge(idx3.DEFAULT_CONFIG, reloaded)
    planner = idx3sched.Planner(merged_cfg["poll"])
    check("the engine reads back the interval the form wrote",
          planner.base == 15.0, planner.base)
    check("filters written by the form actually filter",
          idx3.matches({"ticker": "BBCA", "title": "Laporan"}, merged_cfg)
          and not idx3.matches({"ticker": "ZZZZ", "title": "Laporan"},
                               merged_cfg))

    # --- a save that did not happen must not look like one that did -----
    stubborn = os.path.join(tmp, "stubborn.json")
    with open(stubborn, "w", encoding="utf-8") as fh:
        json.dump(idx3.DEFAULT_CONFIG, fh)
    real_replace, tries = os.replace, {"n": 0}

    def always_busy(src, dst):
        tries["n"] += 1
        raise OSError(32, "being used by another process")

    os.replace = always_busy
    try:
        O.save(stubborn, changed)
        raised = False
    except OSError:
        raised = True
    finally:
        os.replace = real_replace
    check("a config.json that cannot be replaced raises, it does not "
          "report success", raised)
    check("and it retried first rather than giving up on one collision",
          tries["n"] > 1, tries["n"])
    check("no .tmp litter is left behind when the save fails",
          not os.path.exists(stubborn + ".tmp"))

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
