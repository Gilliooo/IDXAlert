#!/usr/bin/env python3
"""
test_filters.py - does filtering actually work, end to end?

The existing suites test `matches()` in isolation and the Options form mapping
in isolation. Neither proves the thing a user cares about: I typed tickers into
the Options window, so does the popup now show ONLY those?

That chain has four links, and a break in any one of them looks identical from
the outside (alerts that are wrong, with nothing in the log):

    Options window  ->  config.json  ->  Watcher reads it  ->  poll() drops
    (apply_values)      (on disk)       (load_config)         the wrong ones

So this drives the whole chain with realistic filings and asserts on what
reaches the dispatcher, which is exactly what reaches the popup.

Run:  python test_filters.py
"""

import copy
import json
import os
import shutil
import tempfile
import time

import idx3
import idx3options as O

# Real titles from the live feed. Using invented ones would prove nothing:
# the filters match Indonesian wording that IDX writes in a fixed form.
FEED = [
    ("BBCA", "Penyampaian Laporan Keuangan"),
    ("BBRI", "Laporan Informasi atau Fakta Material"),
    ("BMRI", "Ringkasan Risalah Rapat Umum Para Pemegang Saham Luar Biasa"),
    ("BBNI", "Laporan Kepemilikan atau Setiap Perubahan Kepemilikan Saham Perusahaan Terbuka"),
    ("BBCA", "Penyampaian Bukti Iklan Informasi Laporan Keuangan"),
    ("BBRI", "Laporan Bulanan Registrasi Pemegang Efek"),
    ("ADRO", "Penyampaian Laporan Keuangan"),
    ("ITMG", "Laporan Informasi atau Fakta Material"),
    ("XMSK", "Laporan Harian atas Nilai Aktiva Bersih dan Komposisi Portofolio"),
    ("ZP",   "Structured Warrant Pemberitahuan Tanggal Pelaksanaan"),
    ("SOHO", "Perubahan Susunan Direksi dan Dewan Komisaris"),
    ("PTBA", "Penghentian Sementara Perdagangan Efek"),
]


def payload(rows, start=0):
    """Shaped exactly like the IDX response, so parse() is exercised for real."""
    replies = []
    for i, (ticker, title) in enumerate(rows):
        n = start + i
        replies.append({
            "pengumuman": {
                "Id2": "2026090108%04d-%03d/TEST/IX/2026_id-id" % (n, n),
                "Kode_Emiten": "%-5s" % ticker,     # API space-pads; must be stripped
                "JudulPengumuman": title,
                "TglPengumuman": "2026-09-01T08:%02d:00" % (n % 60),
                "CreatedDate": "2026-09-01T08:%02d:00" % (n % 60),
                "NoPengumuman": "%03d/TEST" % n,
            },
            "attachments": [{"OriginalFilename": "x.pdf",
                             "FullSavePath": "https://example.invalid/%d.pdf" % n}],
        })
    return {"ResultCount": len(replies), "Replies": replies}


def main():
    failures = []

    def check(name, cond, detail=""):
        print("  %-58s %s%s" % (name, "PASS" if cond else "FAIL",
                                "" if cond else "\n      -> " + str(detail)))
        if not cond:
            failures.append(name)

    print("filter chain test: Options window -> config.json -> engine -> popup\n")

    tmp = tempfile.mkdtemp(prefix="idx3filt-")
    idx3.CONFIG_PATH = os.path.join(tmp, "config.json")
    idx3.DATA_DIR = tmp
    idx3.STATE_PATH = os.path.join(tmp, "seen.json")
    idx3.LOG_DIR = os.path.join(tmp, "logs")
    idx3.LATENCY_PATH = os.path.join(tmp, "latency.csv")
    quiet = []
    real_log = idx3.log
    idx3.log = lambda m: quiet.append(m)

    def run_with(form_changes, rows, seed_first=True):
        """Save settings the way the Options window does, then run a real poll.
        Returns the tickers+titles that reached the dispatcher."""
        with open(idx3.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(copy.deepcopy(idx3.DEFAULT_CONFIG), fh, indent=2)
        vals = O.read_values(copy.deepcopy(idx3.DEFAULT_CONFIG))
        # load_config() re-resolves the data dir from the config itself, so
        # pointing CONFIG_PATH at a temp folder is NOT enough: without this the
        # test writes seen.json and latency.csv into the real app folder and
        # stamps on the live state. Set it the way the Options window would.
        vals["data_dir"] = tmp
        vals.update(form_changes)
        problems = O.validate(vals)
        if problems:
            return None, problems
        O.save(idx3.CONFIG_PATH, vals)                 # <- the Save button

        cfg = idx3.load_config()                       # <- what the engine reads
        for p in (idx3.STATE_PATH, idx3.LATENCY_PATH):
            if os.path.exists(p):
                os.remove(p)
        w = idx3.Watcher(cfg)
        got = []
        w.dispatcher.channels.append(lambda items, ctx: got.extend(
            (i["ticker"], i["title"]) for i in items))
        served = {"p": [payload(rows)]}
        w.fetcher.get_json = lambda *a, **k: served["p"].pop(0)
        w.poll()
        for _ in range(60):
            if w.dispatcher.q.empty():
                break
            time.sleep(0.05)
        time.sleep(0.3)
        return got, None

    # ── 1. no filters: everything comes through ────────────────────────
    got, err = run_with({}, FEED)
    check("with empty filters every filing reaches the popup",
          err is None and len(got) == len(FEED), err or len(got))

    # ── 2. watchlist only ──────────────────────────────────────────────
    got, err = run_with({"watchlist": "BBCA, BBRI"}, FEED)
    tickers = sorted({t for t, _ in got})
    check("a ticker list admits only those companies",
          err is None and tickers == ["BBCA", "BBRI"], err or tickers)
    check("the space-padded API ticker still matches the watchlist",
          err is None and len(got) == 4, len(got) if got is not None else err)

    # ── 3. lower-case typing must work (Gill asked) ────────────────────
    got, err = run_with({"watchlist": "bbca, bBrI"}, FEED)
    check("typing the tickers in lower case behaves identically",
          err is None and sorted({t for t, _ in got}) == ["BBCA", "BBRI"],
          err or got)

    # ── 4. the exclude presets, as ticked in the Options window ────────
    got, err = run_with({"exclude_presets": {"Laporan Kepemilikan", "Bukti Iklan",
                                             "Structured Warrant",
                                             "Nilai Aktiva Bersih"}}, FEED)
    titles = [ti for _, ti in (got or [])]
    check("ticking the four presets drops exactly those filings",
          err is None and len(got) == len(FEED) - 4, err or len(got))
    check("  no Laporan Kepemilikan survives",
          all("Kepemilikan" not in t for t in titles), titles)
    check("  no Bukti Iklan survives",
          all("Bukti Iklan" not in t for t in titles), titles)
    check("  no Structured Warrant survives",
          all("Structured Warrant" not in t for t in titles), titles)
    check("  no ETF daily NAV survives",
          all("Nilai Aktiva Bersih" not in t for t in titles), titles)
    check("  a suspension still gets through (that box is left unticked)",
          any("Penghentian Sementara" in t for t in titles), titles)

    # ── 5. the Also exclude free-text box ──────────────────────────────
    got, err = run_with({"exclude": "Laporan Bulanan Registrasi"}, FEED)
    check("a phrase typed into Also exclude drops that filing",
          err is None and len(got) == len(FEED) - 1
          and all("Bulanan Registrasi" not in t for _, t in got), err or got)

    # ── 6. Title must contain ──────────────────────────────────────────
    got, err = run_with({"keywords": "Fakta Material"}, FEED)
    check("Title must contain keeps only matching titles",
          err is None and len(got) == 2
          and all("Fakta Material" in t for _, t in got), err or got)

    # ── 7. all three boxes together, the realistic setup ───────────────
    got, err = run_with({
        "watchlist": "BBCA, BBRI, BMRI, BBNI",
        "exclude_presets": {"Laporan Kepemilikan", "Bukti Iklan"},
        "exclude": "Laporan Bulanan Registrasi",
    }, FEED)
    expected = [("BBCA", "Penyampaian Laporan Keuangan"),
                ("BBRI", "Laporan Informasi atau Fakta Material"),
                ("BMRI", "Ringkasan Risalah Rapat Umum Para Pemegang Saham Luar Biasa")]
    check("a realistic bank setup yields exactly the right three filings",
          err is None and sorted(got) == sorted(expected), err or got)

    # ── 8. the coal setup from the guide ───────────────────────────────
    got, err = run_with({"watchlist": "ADRO, ITMG, PTBA, HRUM, INCO, ANTM, MDKA, TINS",
                         "exclude_presets": {"Nilai Aktiva Bersih"}}, FEED)
    check("the coal ticker list from the guide yields its three filings",
          err is None and sorted({t for t, _ in got}) == ["ADRO", "ITMG", "PTBA"],
          err or got)

    # ── 9. the substring trap, documented in the guide ─────────────────
    got, err = run_with({"exclude": "RUPS"},
                        [("AAAA", "Ringkasan Risalah RUPST Tahunan"),
                         ("BBBB", "Penyampaian Laporan Keuangan")])
    check("excluding RUPS really does also kill RUPST, as the guide warns",
          err is None and len(got) == 1 and got[0][0] == "BBBB", err or got)

    # ── 10. filters must not leak across a save ────────────────────────
    got, err = run_with({"watchlist": "BBCA"}, FEED)
    n_first = len(got or [])
    got, err = run_with({}, FEED)
    check("clearing the ticker box restores the whole feed",
          err is None and n_first == 2 and len(got) == len(FEED),
          (n_first, len(got) if got else err))

    # ── 11. what the engine reads back is what the form wrote ──────────
    with open(idx3.CONFIG_PATH, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    check("config.json holds the engine's own field names",
          "watchlist" in on_disk and "exclude_keywords" in on_disk
          and isinstance(on_disk["exclude_keywords"], list), sorted(on_disk))

    idx3.log = real_log
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
