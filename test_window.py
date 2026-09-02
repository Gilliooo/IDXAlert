#!/usr/bin/env python3
"""
test_window.py - builds the whole Options window against a fake toolkit.

open_window() is several hundred lines of Tk that no machine without a display
can run, which normally means it is the least-tested code in the project and
the easiest place for a typo to survive. A stub toolkit fixes that: every
widget call is recorded instead of drawn, so the window can be built, its
buttons pressed, and its layout inspected, with no display anywhere.

Two things this is specifically here to catch:

  * The button bar being squeezed off-screen. The bar MUST be packed against
    the bottom before the notebook claims the rest with expand=True, or a tab
    taller than the window pushes Save and Cancel out of reach - which is
    exactly what happened at the original default size.
  * The About box wiring: the right name, and a LinkedIn link that actually
    opens the right URL when clicked.

Run:  python test_window.py
"""

import sys
import types

CREATED = {"widgets": [], "buttons": {}, "labels": [], "links": [],
           "vars": []}


class FakeVar:
    def __init__(self, value=None, **kw):
        self._v = value
        self._cbs = []
        self.master = kw.get("master")
        CREATED["vars"].append(self)
    def get(self):
        return self._v
    def set(self, v):
        self._v = v
        for cb in self._cbs:
            cb()
    def trace_add(self, mode, cb):
        self._cbs.append(lambda *a: cb())


class FW:
    """One stub widget: records what it was asked to do."""

    def __init__(self, parent=None, **kw):
        self.parent = parent
        self.kw = dict(kw)
        self.children = []
        self.bindings = {}
        self.pack_args = None
        self.tabs = []
        self.text = ""
        if isinstance(parent, FW):
            parent.children.append(self)
        CREATED["widgets"].append(self)
        label = kw.get("text")
        if isinstance(label, str) and label:
            CREATED["labels"].append(label)
            if kw.get("command") is not None:
                CREATED["buttons"][label] = kw["command"]
            if kw.get("cursor") == "hand2":
                CREATED["links"].append(self)

    def pack(self, **kw):
        self.pack_args = kw
    def grid(self, **kw):
        self.pack_args = kw
    def pack_forget(self):
        self.pack_args = None
    def bind(self, seq, fn, add=None):
        self.bindings.setdefault(seq, []).append(fn)
    def config(self, **kw):
        self.kw.update(kw)
        if "command" in kw and isinstance(self.kw.get("text"), str):
            CREATED["buttons"][self.kw["text"]] = kw["command"]
    configure = config
    def cget(self, key):
        return self.kw.get(key, "")
    def add(self, frame, text=None):
        self.tabs.append(text)
    def insert(self, *a):
        self.text = a[-1] if a else ""
    def delete(self, *a):
        self.text = ""
    def title(self, *a):
        pass
    def after(self, ms, fn=None, *a):
        if callable(ms):
            ms()
        return "t"
    def winfo_children(self):
        return list(self.children)
    def winfo_width(self):
        return 660
    def winfo_height(self):
        return 640
    def winfo_rootx(self):
        return 100
    def winfo_rooty(self):
        return 100
    def winfo_reqheight(self):
        return 40
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: None


def install():
    tk = types.ModuleType("tkinter")
    for n in ("Tk", "Toplevel", "Frame", "Label", "Canvas", "Text", "Menu",
              "Button", "Entry"):
        setattr(tk, n, FW)
    for n in ("BooleanVar", "StringVar", "IntVar", "DoubleVar"):
        setattr(tk, n, FakeVar)

    ttk = types.ModuleType("tkinter.ttk")
    for n in ("Frame", "Label", "Entry", "Button", "Checkbutton", "Spinbox",
              "Combobox", "Notebook", "Separator"):
        setattr(ttk, n, FW)

    mb = types.ModuleType("tkinter.messagebox")
    mb.shown = []
    mb.showerror = lambda *a, **k: mb.shown.append(("error", a))
    mb.showwarning = lambda *a, **k: mb.shown.append(("warning", a))

    fd = types.ModuleType("tkinter.filedialog")
    fd.askdirectory = lambda **k: ""

    tk.ttk, tk.messagebox, tk.filedialog = ttk, mb, fd
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.messagebox"] = mb
    sys.modules["tkinter.filedialog"] = fd

    wb = types.ModuleType("webbrowser")
    wb.opened = []
    wb.open = lambda u: wb.opened.append(u)
    sys.modules["webbrowser"] = wb
    return tk, mb, wb


def main():
    tk, mb, wb = install()

    import json
    import os
    import tempfile
    import idx3
    import idx3options as O

    failures = []

    def check(name, cond, detail=""):
        print("  %-56s %s%s" % (name, "PASS" if cond else "FAIL",
                                "" if cond else "   <- " + str(detail)))
        if not cond:
            failures.append(name)

    print("options window build test (fake toolkit)\n")

    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(idx3.DEFAULT_CONFIG, fh, indent=2)

    saved = {"n": 0}
    O._open["win"] = None
    O.open_window(path, on_saved=lambda: saved.__setitem__("n", saved["n"] + 1),
                  on_test=lambda: None, on_verify=lambda: None)

    check("the window built without raising", len(CREATED["widgets"]) > 100,
          len(CREATED["widgets"]))

    nb = [w for w in CREATED["widgets"] if w.tabs]
    check("the notebook has every tab", nb and len(nb[0].tabs) == 7,
          nb[0].tabs if nb else None)
    if nb:
        check("tabs are in a sensible order",
              nb[0].tabs[:3] == ["Speed", "Alerts", "Filters"], nb[0].tabs)

    # --- the squeezed-buttons bug ---------------------------------------
    bottom = [w for w in CREATED["widgets"]
              if w.pack_args and w.pack_args.get("side") == "bottom"]
    check("a frame is reserved against the bottom edge", len(bottom) == 1,
          len(bottom))
    expanding = [w for w in CREATED["widgets"]
                 if w.pack_args and w.pack_args.get("expand")]
    check("the notebook is the widget that expands", len(expanding) >= 1)
    if bottom and expanding:
        order = CREATED["widgets"].index
        check("the bottom bar is packed BEFORE the expanding notebook, so the "
              "buttons can never be pushed off-screen",
              order(bottom[0]) < order(expanding[0]),
              (order(bottom[0]), order(expanding[0])))

    # --- the discarded-edits bug ----------------------------------------
    # A tk variable built without master= attaches to whichever Tk interpreter
    # was created first in the process. The alert popup makes its own Tk and,
    # at duration 0, keeps it alive, so it often wins - and then the Entry the
    # user types into and the variable Save reads are two different Tcl
    # variables in two different interpreters. The window looks normal and
    # every edit is dropped. Nothing but an explicit master prevents this.
    roots = [w for w in CREATED["widgets"] if w.parent is None]
    root_w = roots[0] if roots else None
    orphans = [v for v in CREATED["vars"] if v.master is None]
    check("every form variable is bound to an explicit master",
          CREATED["vars"] and not orphans,
          "%d of %d have none" % (len(orphans), len(CREATED["vars"])))
    check("they are bound to THIS window's root, not the default one",
          root_w is not None
          and all(v.master is root_w for v in CREATED["vars"]),
          len({id(v.master) for v in CREATED["vars"]}))

    for wanted in ("About", "Save", "Cancel", "Apply"):
        check("the %r button exists" % wanted, wanted in CREATED["buttons"],
              sorted(CREATED["buttons"]))

    # --- About ----------------------------------------------------------
    CREATED["links"].clear()
    CREATED["buttons"]["About"]()
    credit = [t for t in CREATED["labels"] if "Bill Grandy Tunjung" in t]
    check("About credits the author by name", credit, credit)
    check("About shows a clickable LinkedIn link",
          any(O.LINKEDIN_LABEL in (w.kw.get("text") or "")
              for w in CREATED["links"]),
          [w.kw.get("text") for w in CREATED["links"]])
    link = next((w for w in CREATED["links"]
                 if O.LINKEDIN_LABEL in (w.kw.get("text") or "")), None)
    if link is not None:
        wb.opened.clear()
        link.bindings["<Button-1>"][0](None)
        check("clicking the link opens the right profile",
              wb.opened == [O.LINKEDIN], wb.opened)
    check("About has a Close button", "Close" in CREATED["buttons"])

    # --- saving ----------------------------------------------------------
    mb.shown.clear()
    CREATED["buttons"]["Apply"]()
    check("Apply saves without complaint", not mb.shown and saved["n"] == 1,
          (mb.shown, saved["n"]))
    with open(path, encoding="utf-8") as fh:
        written = json.load(fh)
    check("the saved file is still loadable by the engine",
          idx3._merge(idx3.DEFAULT_CONFIG, written)["poll"]
          ["baseline_interval_seconds"] == 22.0,
          written.get("poll"))

    # a bad value must be refused, not written
    O._open["win"] = None
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
