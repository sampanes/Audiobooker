"""Look and feel for Take Recorder: palette, ttk theme and a few custom widgets.

Everything here only draws. The recorder logic lives in app.py.
"""

import tkinter as tk
from tkinter import ttk

# Surfaces step up in brightness: bg (window) < panel (cards) < raised (controls).
C = dict(
    bg="#141518", panel="#1e2024", raised="#2a2d33", hover="#353941", line="#33363d",
    fg="#e9eaec", dim="#8c929b", grey="#5c6370", ink="#0d1117",
    green="#3fb950", yellow="#d29922", red="#f85149", blue="#58a6ff", gold="#e3b341",
    target="#1d3524", warm="#33301f", hot="#3d2224", select="#2f3b52",
)
UI = "Segoe UI"
MONO = "Consolas"


def scale_for(root):
    """Screen pixels per 96-dpi pixel, so layout sizes follow Windows display scaling."""
    try:
        return max(1.0, root.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        return 1.0


def apply_theme(root, px):
    root.configure(bg=C["bg"])
    style = ttk.Style(root)
    style.theme_use("clam")
    flat = dict(bordercolor=C["line"], lightcolor=C["raised"], darkcolor=C["raised"])
    style.configure(".", background=C["bg"], foreground=C["fg"], fieldbackground=C["raised"],
                    font=(UI, 10), focuscolor=C["raised"])
    style.configure("TButton", background=C["raised"], foreground=C["fg"],
                    padding=(px(12), px(5)), relief="flat", **flat)
    style.map("TButton",
              background=[("disabled", C["panel"]), ("pressed", C["line"]),
                          ("active", C["hover"])],
              foreground=[("disabled", C["grey"])],
              lightcolor=[("active", C["hover"])], darkcolor=[("active", C["hover"])])
    style.configure("Small.TButton", padding=(px(8), px(2)))

    for name, bg in (("TCheckbutton", C["bg"]), ("Card.TCheckbutton", C["panel"])):
        style.configure(name, background=bg, foreground=C["fg"],
                        indicatorbackground=C["raised"], indicatorforeground=C["fg"],
                        indicatorsize=px(14), indicatormargin=(0, 0, px(6), 0),
                        upperbordercolor=C["line"], lowerbordercolor=C["line"])
        style.map(name, background=[("disabled", bg), ("active", bg)],
                  foreground=[("disabled", C["grey"])],
                  indicatorbackground=[("selected", C["blue"]), ("disabled", C["panel"])],
                  indicatorforeground=[("selected", C["ink"])])

    style.configure("TCombobox", fieldbackground=C["raised"], foreground=C["fg"],
                    background=C["raised"], arrowcolor=C["dim"], padding=(px(6), px(2)),
                    **flat)
    style.map("TCombobox",
              fieldbackground=[("readonly", C["raised"]), ("disabled", C["panel"])],
              foreground=[("readonly", C["fg"]), ("disabled", C["grey"])],
              background=[("active", C["hover"])], arrowcolor=[("active", C["fg"])],
              selectbackground=[("readonly", C["raised"])],
              selectforeground=[("readonly", C["fg"])])
    root.option_add("*TCombobox*Listbox.background", C["raised"])
    root.option_add("*TCombobox*Listbox.foreground", C["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["select"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["fg"])
    root.option_add("*TCombobox*Listbox.font", (UI, 10))

    style.configure("Treeview", background=C["panel"], fieldbackground=C["panel"],
                    foreground=C["fg"], rowheight=px(28), font=(UI, 11), borderwidth=0,
                    relief="flat")
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Treeview.Heading", background=C["panel"], foreground=C["dim"],
                    font=(UI, 9, "bold"), relief="flat", padding=(px(6), px(5)),
                    bordercolor=C["panel"], lightcolor=C["panel"], darkcolor=C["panel"])
    style.map("Treeview.Heading", background=[("active", C["panel"])])
    style.map("Treeview", background=[("selected", C["select"])],
              foreground=[("selected", C["fg"])])
    style.configure("Vertical.TScrollbar", background=C["raised"], troughcolor=C["panel"],
                    arrowcolor=C["dim"], gripcount=0, **flat)
    style.map("Vertical.TScrollbar", background=[("active", C["hover"])])


def label(parent, text="", fg=None, size=10, weight="normal", **kw):
    kw.setdefault("bg", parent.cget("bg"))
    return tk.Label(parent, text=text, fg=fg or C["dim"], font=(UI, size, weight), **kw)


def entry(parent, var, width, size=10, px=lambda n: n):
    return tk.Entry(parent, textvariable=var, width=width, font=(UI, size), bg=C["raised"],
                    fg=C["fg"], insertbackground=C["fg"], relief="flat",
                    disabledbackground=C["panel"], disabledforeground=C["grey"],
                    highlightthickness=px(1), highlightbackground=C["line"],
                    highlightcolor=C["blue"])


def card(parent, px):
    return tk.Frame(parent, bg=C["panel"], padx=px(12), pady=px(8))


class FlatButton(tk.Label):
    """A colored, borderless button. Disabled ones ignore clicks."""

    def __init__(self, parent, text, command, bg, fg, size=10, weight="bold",
                 hover=None, padx=12, pady=5):
        super().__init__(parent, text=text, bg=bg, fg=fg, font=(UI, size, weight),
                         padx=padx, pady=pady, cursor="hand2",
                         disabledforeground=C["grey"])
        self._command = command
        self._colors = (bg, fg, hover or bg)
        self.bind("<Button-1>", self._press)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))

    def _press(self, event):
        if str(self.cget("state")) != "disabled":
            self._command()

    def _hover(self, inside):
        if str(self.cget("state")) != "disabled":
            self.config(bg=self._colors[2] if inside else self._colors[0])

    def recolor(self, bg, fg, hover=None):
        self._colors = (bg, fg, hover or bg)
        self.config(bg=bg, fg=fg)


class Segmented(tk.Frame):
    """Pick one of a few values, shown as joined buttons; the chosen one is lit."""

    def __init__(self, parent, variable, values, command, labels=None, px=lambda n: n,
                 accent=None):
        super().__init__(parent, bg=C["line"], padx=px(1), pady=px(1))
        self.variable = variable
        self.command = command
        self.accent = accent or C["blue"]
        self.state = "normal"
        self.cells = {}
        for i, value in enumerate(values):
            text = (labels or {}).get(value, str(value))
            cell = tk.Label(self, text=text, font=(UI, 10), padx=px(10), pady=px(2),
                            cursor="hand2")
            cell.grid(row=0, column=i, padx=(px(1) if i else 0, 0), sticky="nsew")
            cell.bind("<Button-1>", lambda e, v=value: self._pick(v))
            self.cells[value] = cell
        variable.trace_add("write", lambda *a: self.refresh())
        self.refresh()

    def _pick(self, value):
        if self.state == "disabled":
            return
        self.variable.set(value)
        self.command()

    def set_state(self, state):
        self.state = state
        self.refresh()

    def config(self, cnf=None, **kw):  # lets callers treat it like a ttk widget
        if "state" in kw:
            self.set_state(kw.pop("state"))
        if kw or cnf:
            super().config(cnf, **kw)

    configure = config

    def refresh(self):
        chosen = self.variable.get()
        off = self.state == "disabled"
        for value, cell in self.cells.items():
            if str(value) == str(chosen):
                bg, fg = (C["line"], C["dim"]) if off else (self.accent, C["ink"])
            else:
                bg, fg = C["raised"], (C["grey"] if off else C["fg"])
            cell.config(bg=bg, fg=fg)


class Meter(tk.Canvas):
    """Input level bars that stretch with the window.

    Each bar lights green up to -6 dBFS, yellow to -1, red above. The shaded
    target zone (-18 to -6) is where the loudest notes should land.
    """

    TICKS = (-48, -36, -24, -18, -12, -6, -3, 0)

    def __init__(self, parent, channels, min_db, px, on_clip_click):
        self.px = px
        self.channels = channels
        self.min_db = min_db
        self.bar_h = px(18)
        self.gap = px(6)
        self.label_w = px(44)
        self.value_w = px(58)
        self.clip_w = px(64)
        height = channels * self.bar_h + (channels - 1) * self.gap + px(20)
        super().__init__(parent, height=height, bg=parent.cget("bg"), highlightthickness=0)
        self._on_clip_click = on_clip_click
        self._state = [(min_db, min_db, False)] * channels
        self._clipped = False
        self.bind("<Configure>", lambda e: self._layout())

    def _x(self, db):
        frac = max(0.0, min(1.0, (db - self.min_db) / -self.min_db))
        return self.x0 + frac * (self.x1 - self.x0)

    def _layout(self):
        self.delete("all")
        px = self.px
        width = max(self.winfo_width(), px(300))
        self.x0 = self.label_w
        self.x1 = width - self.value_w - self.clip_w - px(10)
        self.bars = []
        for ch in range(self.channels):
            y0 = ch * (self.bar_h + self.gap)
            y1 = y0 + self.bar_h
            self.create_rectangle(self.x0, y0, self.x1, y1, fill=C["raised"], width=0)
            for lo, hi, color in ((-18, -6, C["target"]), (-6, -1, C["warm"]),
                                  (-1, 0, C["hot"])):
                self.create_rectangle(self._x(lo), y0, self._x(hi), y1, fill=color, width=0)
            segs = [self.create_rectangle(self.x0, y0, self.x0, y1, fill=c, width=0)
                    for c in (C["green"], C["yellow"], C["red"])]
            hold = self.create_line(self.x0, y0, self.x0, y1, fill=C["fg"], width=px(2))
            name = self.create_text(0, (y0 + y1) / 2, anchor="w", fill=C["dim"],
                                    font=(UI, 9, "bold"), text=f"IN {ch + 1}")
            value = self.create_text(self.x1 + self.value_w, (y0 + y1) / 2, anchor="e",
                                     fill=C["fg"], font=(MONO, 11), text="-inf")
            self.bars.append((segs, hold, name, value, y0, y1))
        y = self.channels * (self.bar_h + self.gap) - self.gap + px(4)
        for db in self.TICKS:
            x = self._x(db)
            self.create_line(x, y - px(3), x, y, fill=C["grey"])
            self.create_text(x, y + px(1), anchor="n", fill=C["dim"], font=(UI, 8),
                             text=str(db))
        bottom = self.channels * (self.bar_h + self.gap) - self.gap
        cx0 = width - self.clip_w
        self.clip_box = self.create_rectangle(cx0, 0, width, bottom, width=0)
        self.clip_text = self.create_text((cx0 + width) / 2, bottom / 2, text="CLIP",
                                          font=(UI, 11, "bold"))
        for item in (self.clip_box, self.clip_text):
            self.tag_bind(item, "<Button-1>", lambda e: self._on_clip_click())
            self.tag_bind(item, "<Enter>", lambda e: self.config(cursor="hand2"))
            self.tag_bind(item, "<Leave>", lambda e: self.config(cursor=""))
        self.set_clip(self._clipped)
        for ch, args in enumerate(self._state):
            self.show(ch, *args)

    def set_clip(self, clipped):
        self._clipped = clipped
        if not hasattr(self, "clip_box"):
            return
        self.itemconfig(self.clip_box, fill=C["red"] if clipped else C["raised"])
        self.itemconfig(self.clip_text, fill="white" if clipped else C["grey"])

    def show(self, ch, level_db, hold_db, selected):
        self._state[ch] = (level_db, hold_db, selected)
        if not hasattr(self, "bars"):
            return
        segs, hold, name, value, y0, y1 = self.bars[ch]
        level = self._x(level_db)
        for seg, (lo, hi) in zip(segs, ((self.min_db, -6), (-6, -1), (-1, 0))):
            a, b = self._x(lo), min(level, self._x(hi))
            if b > a:  # Tk draws a zero-width rectangle as a hairline, so hide it
                self.coords(seg, a, y0, b, y1)
                self.itemconfig(seg, state="normal")
            else:
                self.itemconfig(seg, state="hidden")
        hx = self._x(hold_db)
        self.coords(hold, hx, y0, hx, y1)
        self.itemconfig(hold, state="normal" if hold_db > self.min_db else "hidden")
        self.itemconfig(name, fill=C["fg"] if selected else C["grey"])
        self.itemconfig(value, text=f"{hold_db:.1f}" if hold_db > self.min_db else "-inf",
                        fill=C["fg"] if selected else C["grey"])


class Progress(tk.Canvas):
    """A thin bar under the status line: playback position, count-in, track position."""

    def __init__(self, parent, px):
        super().__init__(parent, height=px(4), bg=parent.cget("bg"), highlightthickness=0)
        self._bar = self.create_rectangle(0, 0, 0, px(4), width=0)
        self._shown = None

    def show(self, fraction, color=None, track=None):
        key = (None if fraction is None else round(fraction, 3), color, track)
        if key == self._shown:
            return
        self._shown = key
        if fraction is None:
            self.config(bg=self.master.cget("bg"))
            self.coords(self._bar, 0, 0, 0, 0)
            return
        width = self.winfo_width()
        self.config(bg=track or C["raised"])
        self.coords(self._bar, 0, 0, max(0.0, min(1.0, fraction)) * width,
                    self.winfo_height())
        self.itemconfig(self._bar, fill=color or C["fg"])
