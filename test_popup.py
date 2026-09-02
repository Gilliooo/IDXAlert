#!/usr/bin/env python3
"""
test_popup.py - renders the popup against a fake tkinter.

Why a fake rather than "skip if tkinter is missing": a skipped test looks
identical to a passing one. Worse, in 2.x a headless run passed VACUOUSLY -
the code raised at `import tkinter` long before it reached the layout bug the
test was supposed to catch, and reported success.

So this substitutes a stub widget toolkit into sys.modules and drives the real
render() to completion, recording every binding and every geometry call. That
turns the two expensive 2.x bugs into assertions:

  * Tk does not bubble events -> every descendant of a row must carry the
    click binding, not just the row frame.
  * Rows are variable height -> the canvas height must come from the top edge
    of the first hidden row, NOT from summed requested heights, or rows clip
    mid-way.

Run:  python test_popup.py
"""

import sys
import types

ROW_H = 37          # pretend every row is this tall; inner.winfo_reqheight sums them
SCREEN_W, SCREEN_H = 1920, 1080


class W:
    """One stub widget. Records bindings, config calls and its own geometry."""

    def __init__(self, parent=None, **kw):
        self.parent = parent
        self.kw = dict(kw)
        self.children = []
        self.bindings = {}
        self.packed = False
        self.configs = []
        if parent is not None and hasattr(parent, "children"):
            self._y = ROW_H * len(parent.children)
            parent.children.append(self)
        else:
            self._y = 0

    # --- geometry manager
    def pack(self, **kw):
        self.packed = True
    def pack_forget(self):
        self.packed = False
    def grid(self, **kw):
        self.packed = True
    def place(self, **kw):
        self.packed = True

    # --- config
    def config(self, **kw):
        self.kw.update(kw)
        self.configs.append(kw)
    configure = config

    # --- events
    def bind(self, seq, fn, add=None):
        self.bindings.setdefault(seq, []).append(fn)

    # --- introspection used by render()
    def winfo_children(self):
        return list(self.children)
    def winfo_reqheight(self):
        return ROW_H * max(1, len(self.children))
    def winfo_y(self):
        return self._y
    def winfo_ismapped(self):
        return self.packed
    def winfo_screenheight(self):
        return SCREEN_H
    def winfo_screenwidth(self):
        return SCREEN_W
    def winfo_rootx(self):
        return 0
    def winfo_rooty(self):
        return 0

    # --- no-ops
    def update_idletasks(self):
        pass
    def geometry(self, spec=None):
        self.kw["geometry"] = spec
    def attributes(self, *a, **k):
        pass
    def overrideredirect(self, *a):
        pass
    def withdraw(self):
        pass
    def lift(self):
        pass
    def focus_force(self):
        pass
    def destroy(self):
        pass
    def after(self, ms, fn=None, *a):
        if callable(ms):
            ms()
        return "timer1"
    def after_cancel(self, tid):
        pass
    def mainloop(self):
        pass                     # return immediately; the window is "shown"

    # --- canvas
    def create_window(self, *a, **k):
        return "win1"
    def itemconfig(self, *a, **k):
        pass
    def yview(self, *a):
        pass
    def yview_moveto(self, *a):
        pass
    def yview_scroll(self, *a):
        pass

    def set(self, *a, **k):
        pass

    def __getattr__(self, name):
        """Any Tk method this stub does not model becomes a no-op.

        Defined methods still win (__getattr__ only fires on lookup failure),
        so the assertions below still see real recorded state - this only stops
        an unmodelled cosmetic call from aborting the render we are trying to
        exercise."""
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: None

    # --- menu
    def add_command(self, **k):
        self.children.append(("cmd", k))
    def add_separator(self):
        pass
    def tk_popup(self, *a):
        pass
    def grab_release(self):
        pass


def install_fake_tkinter():
    tk = types.ModuleType("tkinter")
    for name in ("Tk", "Toplevel", "Frame", "Label", "Canvas", "Scrollbar",
                 "Menu", "Button"):
        setattr(tk, name, W)
    tk.BooleanVar = tk.StringVar = tk.IntVar = W
    sys.modules["tkinter"] = tk
    wb = types.ModuleType("webbrowser")
    wb.opened = []
    wb.open = lambda u: wb.opened.append(u)
    sys.modules["webbrowser"] = wb
    return tk, wb


def item(n, title="Penyampaian Laporan Keuangan", files=1):
    return {"key": "k%d" % n, "ticker": "AAA%d" % n, "title": title,
            "posted": "2026-09-01 08:%02d:00" % n,
            "files": [{"name": "f%d_%d.pdf" % (n, i),
                       "url": "https://x.invalid/%d_%d.pdf" % (n, i)}
                      for i in range(files)]}


def walk(w, out=None):
    out = [] if out is None else out
    out.append(w)
    for c in getattr(w, "children", []):
        if isinstance(c, W):
            walk(c, out)
    return out


def main():
    tk, wb = install_fake_tkinter()
    import idx3popup
    idx3popup.TRAY_MODE = True          # do not block on the daemon thread

    failures = []

    def check(name, cond, detail=""):
        print("  %-54s %s%s" % (name, "PASS" if cond else "FAIL",
                                "" if cond else "   <- " + str(detail)))
        if not cond:
            failures.append(name)

    print("popup render test (fake tkinter)\n")

    # five rows, max_visible 3 -> the classic clipping case
    rows = [item(n) for n in range(5)]
    idx3popup._live["root"] = None
    idx3popup._live["add"] = None
    idx3popup.show(rows, {"duration_seconds": 0, "max_visible": 3})

    import time
    for _ in range(40):
        if idx3popup._live.get("root") is not None:
            break
        time.sleep(0.05)

    root = idx3popup._live.get("root")
    check("the popup actually rendered", root is not None)
    if root is None:
        print("\n1 checks failed")
        return 1

    every = walk(root)
    check("widget tree was built", len(every) > 20, len(every))

    # --- the height bug -------------------------------------------------
    canvases = [w for w in every if any("scrollregion" in c for c in w.configs)]
    check("the canvas was sized", len(canvases) == 1, len(canvases))
    if canvases:
        heights = [c["height"] for c in canvases[0].configs if "height" in c]
        expected_clip = ROW_H * 3          # top edge of the 4th row (index 3)
        check("canvas height comes from the first hidden row's top edge, "
              "not summed heights", heights and heights[-1] == expected_clip,
              "got %s, wanted %d (summed would be %d)"
              % (heights, expected_clip, ROW_H * 5))

    # --- the bubbling bug -----------------------------------------------
    clickable = [w for w in every if "<Button-1>" in w.bindings]
    check("click binding reaches nested children, not just the row frame",
          len(clickable) >= 10, len(clickable))
    closers = [w for w in every if "<Button-3>" in w.bindings]
    check("right-click-to-close is bound recursively", len(closers) >= 10,
          len(closers))

    # --- the window must not close on its own ---------------------------
    esc = [w for w in every if any(k.lower().startswith("<escape")
                                   for k in w.bindings)]
    check("Escape is NOT bound (window closes only on X or right-click)",
          not esc, esc)
    wheel = [w for w in every if "<MouseWheel>" in w.bindings]
    check("scroll wheel is bound once the list overflows", len(wheel) >= 5,
          len(wheel))

    # --- merging --------------------------------------------------------
    add = idx3popup._live.get("add")
    check("a merge callback is published for later batches", callable(add))
    if callable(add):
        before = len(walk(root))
        add([item(9, "A brand new filing")])
        after = len(walk(root))
        check("a later batch merges into the open window", after > before,
              (before, after))
        add([item(9, "A brand new filing")])
        check("re-merging the same item is a no-op",
              len(walk(root)) == after, (after, len(walk(root))))

    # --- attachment handling --------------------------------------------
    idx3popup._live["root"] = None
    idx3popup._live["add"] = None
    idx3popup.show([item(1, files=1)], {"duration_seconds": 0, "max_visible": 3})
    for _ in range(40):
        if idx3popup._live.get("root") is not None:
            break
        time.sleep(0.05)
    r2 = idx3popup._live.get("root")
    tree = walk(r2)
    row_click = [w for w in tree if "<Button-1>" in w.bindings]
    wb.opened.clear()
    row_click[-1].bindings["<Button-1>"][0](None)
    check("a single attachment opens directly",
          wb.opened == ["https://x.invalid/1_0.pdf"], wb.opened)

    check("long filenames keep their distinguishing tail",
          idx3popup.file_label("20260831_ZP_Pemberitahuan Tanggal "
                               "Pelaksanaan_32143667_lamp2.pdf").endswith("lamp2.pdf"),
          idx3popup.file_label("20260831_ZP_Pemberitahuan Tanggal "
                               "Pelaksanaan_32143667_lamp2.pdf"))
    check("short filenames are untouched",
          idx3popup.file_label("a.pdf") == "a.pdf")

    print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
