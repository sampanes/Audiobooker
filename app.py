"""Take Recorder: sing, keep the good takes, trash the bad ones without stopping."""

import atexit
import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import soundfile as sf

import engine
import plugins
import polish
import ui
from takes import TRACK_EXTENSIONS, Song

HERE = Path(__file__).resolve().parent
SETTINGS_PATH = HERE / "settings.json"
PID_PATH = HERE / "take-recorder.pid"
LOG_PATH = HERE / "take-recorder.log"
PROCESS_TITLE = "take-recorder"

DEFAULTS = {
    "song": "",
    "input": "1",
    "samplerate": "auto",
    "device_match": ["Studio 26c", "PreSonus"],
    "preroll_seconds": 0.3,
    "takes_dir": "takes",
    "latency_ms": None,
    "track_gain": 0.7,
    "polish": "off",
    "playback": "headphones",  # headphones | speakers | both
    "speakers": "",  # output name for "speakers"; empty = best guess
    "pitch_engine": "auto",  # auto | builtin | a plugin engine id (plugins.ENGINES)
    "room_engine": "auto",
}
INPUT_CHOICES = {"1": [0], "2": [1], "1+2": [0, 1]}
SAMPLE_RATES = ("auto", "44100", "48000", "96000")
KEY_CONFIDENCE_MIN = 0.12

METER_MIN_DB = -60.0
DECAY_DB_PER_S = 24.0
HOLD_S = 1.5
TICK_MS = 33
UNFOCUSED_ALPHA = 0.55  # window opacity while the keyboard goes somewhere else

C = dict(ui.C, stripe="#22252a")

REC_TEXT, STOP_TEXT = "\u25cf  Record   Space", "\u25a0  Stop   Space"
PLAY_TEXT, STOP_PLAY_TEXT = "\u25ba  Play   P", "\u25a0  Stop   P"

CALIBRATE_HELP = (
    "This measures the delay between the backing track and your recording, so takes "
    "line up with the beat.\n\n"
    "1. Hold one headphone ear cup right against the mic (touching the grille).\n"
    "2. Set the headphone volume to a normal listening level.\n"
    "3. Click OK and keep still and quiet for about 6 seconds -- you'll hear 8 clicks.\n\n"
    "You only need to do this once per setup (redo it if you change interface, "
    "headphones or the Hz setting).")


def load_settings():
    settings = dict(DEFAULTS)
    try:
        settings.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    try:
        settings["polish"] = polish.normalize_mode(settings.get("polish", "off"))
    except (TypeError, ValueError):
        settings["polish"] = "off"
    if settings.get("playback") not in engine.ROUTES:
        settings["playback"] = "headphones"
    return settings


SPEAKER_HINTS = ("speaker", "jack", "line out", "realtek", "headphone")
NOT_SPEAKERS = ("virtual", "streaming", "voice", "cable", "vb-audio")


def guess_speakers(names):
    """Pick the likeliest real speaker output from a list of output names."""
    def plain(name):
        return not any(w in name.lower() for w in NOT_SPEAKERS)
    for name in names:
        if plain(name) and any(h in name.lower() for h in SPEAKER_HINTS):
            return name
    return next((n for n in names if plain(n)), names[0] if names else "")


def save_settings(settings):
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def fmt_time(seconds):
    m, s = divmod(max(seconds, 0.0), 60)
    return f"{int(m):02d}:{s:04.1f}"


def fmt_clock(seconds):
    m, s = divmod(int(round(max(seconds, 0.0))), 60)
    return f"{m}:{s:02d}"


def parse_clock(text):
    """'1:05', '1:05.5' or '65' -> seconds. Raises ValueError."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 2 or not parts[-1]:
        raise ValueError(text)
    seconds = float(parts[-1])
    if len(parts) == 2:
        seconds += 60 * int(parts[0] or 0)
    if seconds < 0:
        raise ValueError(text)
    return seconds


class App:
    def __init__(self, root, settings):
        self.root = root
        self.settings = settings
        self.takes_root = (HERE / settings["takes_dir"]).resolve()
        self.engine = engine.Engine(settings["device_match"], settings["samplerate"],
                                    settings["preroll_seconds"])
        self.song = Song(self.takes_root, settings["song"] or date.today().isoformat())
        self.track_data = None
        self.current_num = None
        self.current_cue = None
        self.last_recorded = None
        self.calibration = None
        self._flash = None
        self._focus_state = None
        self.playing_num = None
        self.playing_mode = None
        self.playing_engines = {}
        self.playing_fallback = None
        self.plugins_found = plugins.find_installed()
        self._failed_engines = {}
        self._play_request_id = 0
        self._pending_play = None
        self._polishing = None
        self._key_request_id = 0
        self._estimating_key = None
        self._closing = False
        self._work_queue = queue.Queue()
        self._work_results = queue.SimpleQueue()
        self._worker = threading.Thread(target=self._work_loop, name="polish-worker",
                                        daemon=True)
        self._worker.start()
        self.undo_stack = []
        self.display_db = [METER_MIN_DB] * engine.METER_CHANNELS
        self.hold_db = [METER_MIN_DB] * engine.METER_CHANNELS
        self.hold_age = [0.0] * engine.METER_CHANNELS
        self.clipped = False

        self._build()
        self._bind_keys()
        self.engine.set_channels(INPUT_CHOICES[self.input_var.get()])
        self.connect()
        self._open_song_extras()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(TICK_MS, self.tick)

    # ---- layout ---------------------------------------------------------

    def _build(self):
        root = self.root
        root.title("Take Recorder")
        self.scale = ui.scale_for(root)
        px = self.px
        ui.apply_theme(root, px)
        root.minsize(px(760), px(640))

        # Says whether keystrokes are going to the recorder right now
        self.focus_label = tk.Label(root, text="", font=("Segoe UI", 9, "bold"), pady=px(3))
        self.focus_label.pack(fill="x")

        body = tk.Frame(root, bg=C["bg"], padx=px(14), pady=px(10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        gap = dict(sticky="ew", pady=(0, px(8)))

        # ---- header: song, interface, settings; then the backing track -------
        head = ui.card(body, px)
        head.grid(row=0, column=0, **gap)
        head.columnconfigure(0, weight=1)
        top = tk.Frame(head, bg=C["panel"])
        top.grid(row=0, column=0, sticky="ew")
        self.song_var = tk.StringVar(value=self.song.name)
        self.song_entry = ui.entry(top, self.song_var, 22, size=13, px=px)
        self.song_entry.pack(side="left", ipady=px(2))
        self.song_entry.bind("<Return>", self.on_song_enter)
        self.song_entry.bind("<Escape>", self.on_song_escape)
        ttk.Button(top, text="Folder", style="Small.TButton", takefocus=0,
                   command=self.open_folder).pack(side="left", padx=(px(6), 0))
        ttk.Button(top, text="\u2699  Settings", style="Small.TButton", takefocus=0,
                   command=self.open_settings).pack(side="right")
        self.device_short = ui.label(top, "", size=9, cursor="hand2")
        self.device_short.pack(side="right", padx=(0, px(12)))
        self.device_dot = ui.label(top, "\u25cf", fg=C["grey"], cursor="hand2")
        self.device_dot.pack(side="right", padx=(0, px(4)))
        for w in (self.device_short, self.device_dot):
            w.bind("<Button-1>", lambda e: self.open_settings())

        self.track_widgets = []
        # With a track: name, where to start, count-in. Without: one quiet line.
        self.track_row = tk.Frame(head, bg=C["panel"])
        self.track_row.grid(row=1, column=0, sticky="ew", pady=(px(8), 0))
        self.track_label = ui.label(self.track_row, "", fg=C["fg"], anchor="w")
        self.track_label.pack(side="left", padx=(0, px(14)))
        self.start_var = tk.StringVar()
        self.leadin_var = tk.StringVar()
        for text, var, width, unit in (("from", self.start_var, 6, ""),
                                       ("count-in", self.leadin_var, 3, "s")):
            ui.label(self.track_row, text).pack(side="left", padx=(0, px(4)))
            e = ui.entry(self.track_row, var, width, px=px)
            e.pack(side="left", ipady=px(1), padx=(0, px(2) if unit else px(12)))
            e.bind("<Return>", self.on_track_fields)
            e.bind("<FocusOut>", self.on_track_fields)
            self.track_widgets.append(e)
            if unit:
                ui.label(self.track_row, unit).pack(side="left", padx=(0, px(12)))
        self.use_track_var = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(self.track_row, text="Play while recording",
                             variable=self.use_track_var, style="Card.TCheckbutton",
                             takefocus=0, command=self.on_track_fields)
        cb.pack(side="left")
        self.track_widgets.append(cb)
        for text, cmd in (("Clear", self.clear_track), ("Change...", self.load_track_dialog)):
            b = ttk.Button(self.track_row, text=text, style="Small.TButton", takefocus=0,
                           command=cmd)
            b.pack(side="right", padx=(px(4), 0))
            self.track_widgets.append(b)
        self.latency_warning = ui.label(self.track_row, "not calibrated", fg=C["yellow"],
                                        size=9, cursor="hand2")
        self.latency_warning.bind("<Button-1>", lambda e: self.open_settings())

        self.dry_row = tk.Frame(head, bg=C["panel"])
        self.dry_row.grid(row=1, column=0, sticky="ew", pady=(px(8), 0))
        ui.label(self.dry_row, "No backing track  \u00b7  records dry",
                 anchor="w").pack(side="left")
        b = ttk.Button(self.dry_row, text="Load track...", style="Small.TButton",
                       takefocus=0, command=self.load_track_dialog)
        b.pack(side="right")
        self.track_widgets.append(b)

        # Only shown when the interface needs attention
        self.device_warning = tk.Label(body, text="", bg=C["bg"], fg=C["yellow"],
                                       font=("Segoe UI", 9), anchor="w", justify="left")

        # ---- input meter -----------------------------------------------------
        self.meter = ui.Meter(body, engine.METER_CHANNELS, METER_MIN_DB, px,
                              on_clip_click=self.reset_clip)
        self.meter.grid(row=2, column=0, sticky="ew", pady=(px(6), px(10)))

        # ---- status band: what is happening, and which take G/B will judge ----
        hero = tk.Frame(body, bg=C["panel"])
        hero.grid(row=3, column=0, **gap)
        hero.columnconfigure(0, weight=1)
        self.status = tk.Label(hero, text="", bg=C["panel"], fg=C["fg"],
                               font=("Segoe UI", 22, "bold"), pady=0)
        self.status.grid(row=0, column=0, sticky="ew", pady=(px(8), 0))
        self.verdict_label = tk.Label(hero, text="", bg=C["panel"], fg=C["dim"],
                                      font=("Segoe UI", 11))
        self.verdict_label.grid(row=1, column=0, sticky="ew", pady=(0, px(6)))
        self.progress = ui.Progress(hero, px)
        self.progress.grid(row=2, column=0, sticky="ew")
        self._status_shown = None
        self._verdict_live = False
        self._verdict_text = ""
        self._hearing_shown = ""

        # ---- transport, verdict, and how playback should sound -----------------
        deck = ui.card(body, px)
        deck.grid(row=4, column=0, **gap)
        deck.columnconfigure(0, weight=1)
        transport = tk.Frame(deck, bg=C["panel"])
        transport.grid(row=0, column=0, sticky="ew")
        self.buttons = {}
        for key, text, cmd in (("rec", REC_TEXT, self.toggle_record),
                               ("play", PLAY_TEXT, self.toggle_play),
                               ("undo", "Undo trash   Ctrl+Z", self.undo_trash)):
            b = ttk.Button(transport, text=text, takefocus=0, command=self._act(cmd))
            b.pack(side="left", padx=(0, px(6)))
            self.buttons[key] = b
        self.verdict_buttons = []
        for text, color, hover, cmd in (("BAD   B", C["red"], "#ff6b63", self.mark_bad),
                                        ("GOOD   G", C["green"], "#4fd062", self.mark_good)):
            b = ui.FlatButton(transport, text, self._act(cmd), bg=color, fg=C["ink"], size=13,
                              hover=hover, padx=px(18), pady=px(4))
            b.pack(side="right", padx=(px(6), 0))
            self.verdict_buttons.append((b, color, hover))

        sound = tk.Frame(deck, bg=C["panel"])
        sound.grid(row=1, column=0, sticky="ew", pady=(px(10), 0))
        self.polish_var = tk.StringVar(value=self.settings["polish"])
        pitch, tone = polish.split_mode(self.settings["polish"])
        self.pitch_var = tk.StringVar(value=pitch)
        self.tone_var = tk.StringVar(value=tone)
        labels = {level: level.title() for level in polish.LEVELS}
        for text, var, accent in (("Pitch  V", self.pitch_var, C["gold"]),
                                  ("Tone  T", self.tone_var, C["blue"])):
            ui.label(sound, text).pack(side="left", padx=(0, px(6)))
            ui.Segmented(sound, var, polish.LEVELS, self.on_sound, labels=labels, px=px,
                         accent=accent).pack(side="left", padx=(0, px(18)))
        self.key_var = tk.StringVar(value=self._song_key_name())
        self.key_box = ttk.Combobox(sound, textvariable=self.key_var,
                                    values=tuple(polish.KEY_NAMES), width=12,
                                    state="readonly", takefocus=0)
        self.key_box.pack(side="right")
        self.key_box.bind("<<ComboboxSelected>>", self.on_key)
        ui.label(sound, "Key").pack(side="right", padx=(px(10), px(4)))

        # ---- takes list ---------------------------------------------------------
        body.rowconfigure(5, weight=1)
        listing = tk.Frame(body, bg=C["panel"], padx=px(4), pady=px(4))
        listing.grid(row=5, column=0, sticky="nsew")
        lhead = tk.Frame(listing, bg=C["panel"])
        lhead.pack(side="top", fill="x", padx=px(8), pady=(px(2), 0))
        self.takes_title = ui.label(lhead, "TAKES", size=8, weight="bold")
        self.takes_title.pack(side="left")
        self.show_trash = tk.BooleanVar(value=False)
        ttk.Checkbutton(lhead, text="Show trash", variable=self.show_trash, takefocus=0,
                        style="Card.TCheckbutton",
                        command=self.on_show_trash).pack(side="right")
        cols = ("take", "length", "peak", "notes")
        self.tree = ttk.Treeview(listing, columns=cols, show="headings", selectmode="browse",
                                 height=7)
        for col, text, width, anchor in (("take", "TAKE", 64, "center"),
                                         ("length", "LENGTH", 84, "center"),
                                         ("peak", "PEAK dBFS", 92, "center"),
                                         ("notes", "NOTES", 380, "w")):
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=px(width), anchor=anchor, stretch=(col == "notes"),
                             minwidth=px(120) if col == "notes" else px(width))
        self.tree.tag_configure("trashed", foreground=C["grey"])
        self.tree.tag_configure("starred", foreground=C["gold"])
        self.tree.tag_configure("bad", foreground=C["red"])
        self.tree.tag_configure("stripe", background=C["stripe"])
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self._act(self.toggle_play)())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_verdict())

        self._build_settings()

    def _build_settings(self):
        """Set-once controls, in their own window so the main one stays calm."""
        px = self.px
        win = self.settings_win = tk.Toplevel(self.root, bg=C["bg"], padx=px(18),
                                              pady=px(14))
        win.title("Take Recorder settings")
        win.withdraw()
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self.close_settings)
        win.bind("<Escape>", lambda e: self.close_settings())
        win.columnconfigure(1, weight=1)

        def section(row, title):
            ui.label(win, title, size=8, weight="bold").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(px(14) if row else 0, px(4)))

        def field(row, text):
            ui.label(win, text).grid(row=row, column=0, sticky="w", padx=(0, px(14)),
                                     pady=px(3))
            cell = tk.Frame(win, bg=C["bg"])
            cell.grid(row=row, column=1, sticky="w", pady=px(3))
            return cell

        section(0, "RECORDING")
        cell = field(1, "Record input")
        self.input_var = tk.StringVar(value=self.settings["input"]
                                      if self.settings["input"] in INPUT_CHOICES else "1")
        self.input_select = ui.Segmented(cell, self.input_var, list(INPUT_CHOICES),
                                         self.on_input, px=px)
        self.input_select.pack(side="left")
        self.input_buttons = [self.input_select]
        cell = field(2, "Sample rate")
        self.rate_var = tk.StringVar(value=str(self.settings["samplerate"]))
        self.rate_box = ttk.Combobox(cell, textvariable=self.rate_var, values=SAMPLE_RATES,
                                     width=7, state="readonly", takefocus=0)
        self.rate_box.pack(side="left")
        self.rate_box.bind("<<ComboboxSelected>>", self.on_rate)
        ui.label(cell, "Hz   (leave on auto)", size=9).pack(side="left", padx=(px(6), 0))
        ui.label(win, "Loudest notes should peak in the shaded zone of the meter "
                      "(-18 to -6 dBFS). Set gain on the interface.", size=9,
                 wraplength=px(460), justify="left").grid(row=3, column=0, columnspan=2,
                                                          sticky="w", pady=(px(2), 0))

        section(4, "PLAYBACK")
        cell = field(5, "Play on  (O)")
        self.route_var = tk.StringVar(value=self.settings["playback"])
        ui.Segmented(cell, self.route_var, engine.ROUTES, self.on_route,
                     labels={r: r.title() for r in engine.ROUTES}, px=px).pack(side="left")
        cell = field(6, "Speakers")
        self.speakers_var = tk.StringVar()
        self.speakers_box = ttk.Combobox(cell, textvariable=self.speakers_var, width=34,
                                         state="readonly", takefocus=0,
                                         postcommand=self._refresh_speakers)
        self.speakers_box.pack(side="left")
        self.speakers_box.bind("<<ComboboxSelected>>", self.on_speakers)
        self._refresh_speakers()

        section(7, "BACKING TRACK LATENCY")
        cell = field(8, "Latency")
        self.latency_label = ui.label(cell, "", fg=C["fg"])
        self.latency_label.pack(side="left")
        cal = ttk.Button(cell, text="Calibrate...", style="Small.TButton", takefocus=0,
                         command=self._act(self.calibrate))
        cal.pack(side="left", padx=(px(10), 0))
        self.track_widgets.append(cal)

        section(9, "PLUGINS")
        self.engine_vars, self.engine_boxes = {}, {}
        for row, kind, text in ((10, "pitch", "Pitch by"), (11, "room", "Room by")):
            cell = field(row, text)
            var = self.engine_vars[kind] = tk.StringVar()
            box = self.engine_boxes[kind] = ttk.Combobox(
                cell, textvariable=var, width=26, state="readonly", takefocus=0)
            box.pack(side="left")
            box.bind("<<ComboboxSelected>>", lambda e, k=kind: self.on_engine(k))
            if kind == "room":
                ui.label(cell, "Tone Lots only", size=9).pack(side="left", padx=(px(6), 0))
        self.plugins_label = ui.label(win, "", size=9, anchor="w", justify="left",
                                      wraplength=px(460))
        self.plugins_label.grid(row=12, column=0, columnspan=2, sticky="w")
        self._refresh_engine_boxes()

        section(13, "INTERFACE")
        self.device_label = ui.label(win, "", size=9, anchor="w", justify="left",
                                     wraplength=px(460))
        self.device_label.grid(row=14, column=0, columnspan=2, sticky="w")
        buttons = tk.Frame(win, bg=C["bg"])
        buttons.grid(row=15, column=0, columnspan=2, sticky="ew", pady=(px(8), 0))
        ttk.Button(buttons, text="Reconnect", style="Small.TButton", takefocus=0,
                   command=self._act(lambda: self.connect(refresh=True))).pack(side="left")
        ttk.Button(buttons, text="Close", takefocus=0,
                   command=self.close_settings).pack(side="right")

    def open_settings(self):
        win = self.settings_win
        if win.state() == "withdrawn":
            win.update_idletasks()
            x = self.root.winfo_rootx() + self.px(60)
            y = self.root.winfo_rooty() + self.px(60)
            win.geometry(f"+{x}+{y}")
        win.deiconify()
        win.lift()
        win.focus_set()

    def close_settings(self):
        self.settings_win.withdraw()
        self.root.focus_set()

    def px(self, n):
        """A layout size in 96-dpi pixels, scaled for this screen."""
        return int(round(n * self.scale))

    def _act(self, fn):
        """Wrap a button/hotkey action so it does nothing while calibrating."""
        def run():
            if self.calibration is None:
                print(f"[action] {fn.__name__}")
                fn()
        return run

    def _bind_keys(self):
        for seq, fn in (("<space>", self.toggle_record),
                        ("<Control-z>", self.undo_trash), ("<Control-Z>", self.undo_trash),
                        ("<KeyPress-p>", self.toggle_play), ("<KeyPress-P>", self.toggle_play),
                        ("<KeyPress-v>", self.cycle_pitch),
                        ("<KeyPress-V>", self.cycle_pitch),
                        ("<KeyPress-t>", self.cycle_tone),
                        ("<KeyPress-T>", self.cycle_tone),
                        ("<KeyPress-o>", self.cycle_route),
                        ("<KeyPress-O>", self.cycle_route),
                        ("<KeyPress-g>", self.mark_good), ("<KeyPress-G>", self.mark_good),
                        ("<KeyPress-b>", self.mark_bad), ("<KeyPress-B>", self.mark_bad)):
            self.root.bind_all(seq, self._hotkey(self._act(fn)))

    def _hotkey(self, fn):
        def handler(event):
            if isinstance(event.widget, (tk.Entry, ttk.Entry)):
                return None  # typing in a text field
            fn()
            return "break"
        return handler

    # ---- background work -------------------------------------------------

    def _render_job_current(self, job):
        cancel = job.get("cancel")
        return (not self._closing and cancel is not None and not cancel.is_set() and
                self._pending_play is job and job["request_id"] == self._play_request_id and
                job["song"] is self.song)

    def _key_job_current(self, job):
        cancel = job.get("cancel")
        return (not self._closing and cancel is not None and not cancel.is_set() and
                self._estimating_key is job and job["request_id"] == self._key_request_id and
                job["song"] is self.song)

    def _work_loop(self):
        """Run polish renders and key estimates without touching Tk."""
        while True:
            job = self._work_queue.get()
            if job is None:
                return
            try:
                if job["kind"] == "render":
                    if not self._render_job_current(job):
                        result = None
                    else:
                        song = job["song"]
                        take = job["take"]
                        path = song.cached_polished(take, job["mode"], job["recipe"])
                        if not self._render_job_current(job):
                            path = None
                        elif path is None:
                            path = self._render_take(job)
                        result = path
                elif job["kind"] == "key":
                    if self._key_job_current(job):
                        result = polish.estimate_key(
                            job["data"], job["rate"],
                            cancel=lambda: not self._key_job_current(job))
                    else:
                        result = None
                elif job["kind"] == "prune":
                    result = job["song"].prune_polished()
                else:
                    raise ValueError(f"Unknown background job {job['kind']!r}")
                error = None
            except Exception as exc:
                result, error = None, exc
            self._work_results.put((job, result, error))
            del job, result, error

    def _render_take(self, job):
        """Render and cache a take (worker thread). Plugins fall back to built-in."""
        song, take, mode = job["song"], job["take"], job["mode"]
        recipe = job["recipe"]
        current = lambda: self._render_job_current(job)  # noqa: E731
        source_stamp = song.polish_source_stamp(take)
        source, rate = sf.read(str(take.path), dtype="float32", always_2d=True)
        data = None
        if job.get("engines"):
            try:
                data = plugins.render_isolated(source, rate, mode, job["key"], job["engines"],
                                               job.get("plugins", {}),
                                               cancel=lambda: not current())
            except plugins.PluginFailed as exc:
                job["fallback"] = (exc.engine or next(iter(job["engines"].values())), str(exc))
                recipe = polish.recipe_hash(mode, job["key"])
                cached = song.cached_polished(take, mode, recipe)
                if cached is not None:
                    return cached
        if data is None:
            data = polish.render(source, rate, mode, job["key"], cancel=lambda: not current())
        if not current():
            return None
        return song.publish_polished(take, mode, recipe, data, rate,
                                     expected_source=source_stamp)

    def _poll_work_results(self):
        while True:
            try:
                job, result, error = self._work_results.get_nowait()
            except queue.Empty:
                return
            if job["kind"] == "render":
                self._finish_render(job, result, error)
            elif job["kind"] == "key":
                self._finish_key_estimate(job, result, error)
            elif error is not None and not self._closing:
                print(f"[{PROCESS_TITLE}] could not prune polished cache: {error}")

    def _finish_render(self, job, path, error):
        if (self._closing or self._pending_play is not job or
                job["request_id"] != self._play_request_id or job["song"] is not self.song):
            return
        self._pending_play = None
        self._polishing = None
        fallback = job.get("fallback")
        if fallback is not None:
            engine_id, why = fallback
            print(f"[{PROCESS_TITLE}] plugin fell back to built-in: {why}")
            self._failed_engines[engine_id] = why
            self._refresh_engine_boxes()
        if error is not None:
            messagebox.showerror("Take Recorder",
                                 f"Could not polish take {job['take'].label}:\n{error}")
            return
        take = self.song.get(job["take"].num)
        if take is None or take.trashed:
            return
        self._start_playback(take, path, job["mode"], job["request_id"], job["song"])
        if self.playing_num == take.num:
            self.playing_engines = {} if fallback else dict(job.get("engines") or {})
            self.playing_fallback = fallback[0] if fallback else None

    def _finish_key_estimate(self, job, result, error):
        if (self._closing or self._estimating_key is not job or
                job["request_id"] != self._key_request_id or job["song"] is not self.song):
            return
        self._estimating_key = None
        fallback = polish.KEY_NAMES[0]
        if error is not None:
            name = fallback
            notice = "Key estimate failed -- using Chromatic"
        else:
            name, confidence = result
            if name not in polish.KEY_NAMES or confidence < KEY_CONFIDENCE_MIN:
                name = fallback
                notice = f"Key uncertain ({confidence:.2f}) -- using Chromatic"
            else:
                notice = f"Estimated key: {name} ({confidence:.2f})"
        active = self._active_audition()
        old_name = self._song_key_name()
        self.song.config["key"] = name
        self.song.save_config()
        self.key_var.set(name)
        self.flash(notice)
        if active is not None and active[1] != "off" and name != old_name:
            take = self.song.get(active[0])
            if take is not None and not take.trashed:
                self._request_play(take, active[1])

    def _cancel_key_estimate(self):
        if self._estimating_key is not None:
            self._estimating_key["cancel"].set()
        self._key_request_id += 1
        self._estimating_key = None

    def _queue_key_estimate(self):
        if self.track_data is None:
            return
        self._cancel_key_estimate()
        fallback = polish.KEY_NAMES[0]
        self.song.config.pop("key", None)
        self.song.save_config()
        self.key_var.set(fallback)
        data, rate = self.track_data
        job = {"kind": "key", "request_id": self._key_request_id,
               "song": self.song, "data": data, "rate": rate,
               "cancel": threading.Event()}
        self._estimating_key = job
        self._work_queue.put(job)

    def _song_key_name(self):
        name = self.song.config.get("key", polish.KEY_NAMES[0])
        return name if name in polish.KEY_NAMES else polish.KEY_NAMES[0]

    # ---- device -----------------------------------------------------------

    def connect(self, refresh=False):
        if self.engine.recording:
            self.finish_take()
        self.stop_playback()
        self.engine.open(refresh=refresh)
        self._update_device_label()

    def _update_device_label(self):
        b = self.engine.backend
        warning = None
        if b is None:
            color, short = C["red"], "No interface"
            warning = self.engine.error or "No interface"
            details = warning
        else:
            quality = ("bit-perfect" if b.bit_perfect
                       else "NOT bit-perfect -- Windows is processing the signal")
            auto = " (auto)" if self.engine.requested_rate == "auto" else ""
            color = C["green"] if b.bit_perfect else C["yellow"]
            short = f"{b.name}  \u00b7  {self.engine.samplerate / 1000:g} kHz"
            details = (f"{b.describe()}\n{self.engine.samplerate} Hz{auto} / 24-bit WAV  "
                       f"\u00b7  {quality}\nHeadphones: {self.engine.output_name()}\n"
                       f"Takes: {self.song.dir}")
            if not b.bit_perfect:
                warning = "Not bit-perfect: Windows is processing the signal (see Settings)"
        if self.engine.input_paused:
            color = C["yellow"]
            warning = ("Input paused during playback: the interface can't run input and "
                       "output at different rates. Set Hz to auto in Settings.")
        self.device_dot.config(fg=color)
        self.device_short.config(text=short, fg=C["red"] if b is None else C["dim"])
        self.device_label.config(text=details, fg=C["dim"] if b is not None else C["red"])
        if warning:
            self.device_warning.config(text=warning, fg=C["red"] if b is None else C["yellow"])
            self.device_warning.grid(row=1, column=0, sticky="ew", pady=(0, self.px(4)))
        else:
            self.device_warning.grid_remove()

    # ---- song / settings --------------------------------------------------

    def _open_song_extras(self):
        """Recover crashed takes and load the backing track of the current song."""
        fixed = self.song.recover()
        if fixed:
            labels = ", ".join(f"{n:03d}" for n in fixed)
            print(f"[{PROCESS_TITLE}] recovered take(s) {labels} in '{self.song.name}'")
            self.flash(f"Recovered take {labels} after a crash")
        self._work_queue.put({"kind": "prune", "song": self.song})
        estimate_key = self.song.config.get("key") not in polish.KEY_NAMES
        self._load_track_data()
        self._show_track_fields()
        self.refresh_takes()
        if estimate_key and self.track_data is not None:
            self._queue_key_estimate()

    def _load_track_data(self):
        self.track_data = None
        path = self.song.track_path
        if path is None:
            return
        try:
            self.track_data = engine.load_track(path)
        except Exception as exc:
            messagebox.showerror("Take Recorder", f"Could not read the backing track:\n{exc}")

    def _show_track_fields(self):
        cfg = self.song.config
        latency = self.settings.get("latency_ms")
        if self.track_data is not None:
            name = cfg["track"].removeprefix("backing - ")
            length = len(self.track_data[0]) / self.track_data[1]
            self.track_label.config(text=f"\u266a  {name}  ({fmt_clock(length)})")
            self.dry_row.grid_remove()
            self.track_row.grid()
            if latency is None:
                self.latency_warning.pack(side="right", padx=(0, self.px(8)))
            else:
                self.latency_warning.pack_forget()
        else:
            self.track_row.grid_remove()
            self.dry_row.grid()
        self.use_track_var.set(bool(cfg.get("use_track", True)))
        self.start_var.set(fmt_clock(cfg.get("start_at", 0.0)))
        self.leadin_var.set(f"{cfg.get('leadin', 2.0):g}")
        if latency is None:
            self.latency_label.config(text="not calibrated", fg=C["yellow"])
        else:
            self.latency_label.config(text=f"{latency:.1f} ms", fg=C["fg"])
        self.key_var.set(self._song_key_name())

    def on_track_fields(self, event=None):
        cfg = self.song.config
        try:
            start = parse_clock(self.start_var.get())
            if self.track_data is not None:
                start = min(start, len(self.track_data[0]) / self.track_data[1])
            cfg["start_at"] = round(start, 2)
        except ValueError:
            pass
        try:
            cfg["leadin"] = max(0.0, min(30.0, float(self.leadin_var.get())))
        except ValueError:
            pass
        cfg["use_track"] = bool(self.use_track_var.get())
        self.song.save_config()
        self._show_track_fields()
        if event is None or event.type != tk.EventType.FocusOut:
            self.root.focus_set()
        return "break" if event is not None else None

    def load_track_dialog(self):
        if self.engine.recording:
            return
        pattern = " ".join(f"*{ext}" for ext in TRACK_EXTENSIONS)
        path = filedialog.askopenfilename(title="Choose a backing track",
                                          filetypes=[("Audio", pattern), ("All files", "*.*")])
        self.root.focus_set()
        if not path:
            return
        self.stop_playback()
        self._cancel_key_estimate()
        try:
            self.song.set_track(path)
        except Exception as exc:
            messagebox.showerror("Take Recorder", f"Could not use that track:\n{exc}")
            return
        self.song.config.pop("key", None)
        self.song.config["start_at"] = 0.0
        self.song.save_config()
        self._load_track_data()
        self._show_track_fields()
        if self.track_data is not None:
            self._queue_key_estimate()

    def clear_track(self):
        if self.engine.recording:
            return
        self.stop_playback()
        self._cancel_key_estimate()
        self.song.clear_track()
        self.song.config["key"] = polish.KEY_NAMES[0]
        self.song.save_config()
        self.track_data = None
        self._show_track_fields()
        self.root.focus_set()

    def on_song_enter(self, event=None):
        name = self.song_var.get().strip()
        if name and not self.engine.recording:
            self.stop_playback()
            self._cancel_key_estimate()
            self.song = Song(self.takes_root, name)
            self.undo_stack.clear()
            self.last_recorded = None
            self.settings["song"] = self.song.name
            save_settings(self.settings)
            self._open_song_extras()
            self._update_device_label()
        self.song_var.set(self.song.name)
        self.root.focus_set()
        return "break"

    def on_song_escape(self, event=None):
        self.song_var.set(self.song.name)
        self.root.focus_set()
        return "break"

    def on_input(self):
        self.engine.set_channels(INPUT_CHOICES[self.input_var.get()])
        self.settings["input"] = self.input_var.get()
        save_settings(self.settings)
        self.root.focus_set()

    def on_rate(self, event=None):
        choice = self.rate_var.get()
        rate = choice if choice == "auto" else int(choice)
        if self.engine.recording or rate == self.engine.requested_rate:
            self.rate_var.set(str(self.engine.requested_rate))
            self.root.focus_set()
            return
        self.settings["samplerate"] = rate
        save_settings(self.settings)
        self.stop_playback()
        self.engine.close()
        self.engine = engine.Engine(self.settings["device_match"], rate,
                                    self.settings["preroll_seconds"])
        self.engine.set_channels(INPUT_CHOICES[self.input_var.get()])
        self.connect()
        self.root.focus_set()

    def on_polish_mode(self):
        try:
            mode = polish.normalize_mode(self.polish_var.get())
        except (TypeError, ValueError):
            mode = "off"
        self.polish_var.set(mode)
        pitch, tone = polish.split_mode(mode)
        if self.pitch_var.get() != pitch:
            self.pitch_var.set(pitch)
        if self.tone_var.get() != tone:
            self.tone_var.set(tone)
        active = self._active_audition()
        changed = self.settings.get("polish") != mode
        self.settings["polish"] = mode
        save_settings(self.settings)
        self.root.focus_set()
        if active is not None and active[1] != mode:
            take = self.song.get(active[0])
            if take is not None:
                self._request_play(take, mode)
        elif changed:
            self.flash(f"Pitch {pitch}  \u00b7  tone {tone}")

    def on_sound(self):
        self.polish_var.set(polish.join_mode(self.pitch_var.get(), self.tone_var.get()))
        self.on_polish_mode()

    def _refresh_speakers(self):
        try:
            names = engine.list_speakers(self.settings["device_match"])
        except Exception:
            names = []
        saved = self.settings.get("speakers", "")
        if saved and saved not in names:
            names.append(saved)  # unplugged right now; keep the choice
        self.speakers_box.config(values=names)
        self.speakers_var.set(saved or guess_speakers(names))

    def on_speakers(self, event=None):
        self.settings["speakers"] = self.speakers_var.get()
        save_settings(self.settings)
        self.root.focus_set()
        if self.route_var.get() != "headphones":
            self._replay_on_new_route()

    # ---- plugin engines ------------------------------------------------------

    def _engine_choice_labels(self, kind):
        """[(setting value, label)] for a kind's Settings box."""
        best = plugins.resolve(kind, plugins.AUTO, self.plugins_found, self._failed_engines)
        auto = f"Auto  ({plugins.short_name(best) if best else 'built-in'})"
        rows = [(plugins.AUTO, auto), (plugins.BUILTIN, "Built-in")]
        rows += [(e, plugins.short_name(e) + ("  (failed)" if e in self._failed_engines else ""))
                 for e in plugins.choices(kind, self.plugins_found)]
        return rows

    def _refresh_engine_boxes(self):
        if not hasattr(self, "engine_boxes"):
            return
        for kind, box in self.engine_boxes.items():
            rows = self._engine_choice_labels(kind)
            box.config(values=[label for _, label in rows])
            wanted = self.settings.get(f"{kind}_engine", plugins.AUTO)
            label = next((l for v, l in rows if v == wanted), rows[0][1])
            self.engine_vars[kind].set(label)
        found = [plugins.short_name(e) for e in plugins.ENGINES
                 if e in self.plugins_found and not plugins.ENGINES[e].hidden]
        lines = [("Found: " + ", ".join(found)) if found else
                 "No supported plugins found in Common Files\\VST3, so the built-in "
                 "sound is used."]
        lines += [f"Skipped until you pick it again: {why}"
                  for why in self._failed_engines.values()]
        self.plugins_label.config(text="\n".join(lines))

    def on_engine(self, kind):
        label = self.engine_vars[kind].get()
        value = next((v for v, l in self._engine_choice_labels(kind) if l == label),
                     plugins.AUTO)
        self._failed_engines.pop(value, None)  # picking it again means: try it again
        self.settings[f"{kind}_engine"] = value
        save_settings(self.settings)
        self._refresh_engine_boxes()
        self.root.focus_set()
        self._replay_on_new_route()

    def _engines_for(self, mode):
        """{kind: engine id} of the plugins a render of mode would use."""
        pick = {kind: plugins.resolve(kind, self.settings.get(f"{kind}_engine", plugins.AUTO),
                                      self.plugins_found, self._failed_engines)
                for kind in plugins.KINDS}
        return plugins.active_engines(mode, pick["pitch"], pick["room"])

    def _describe(self, mode, engines=None, fallback=None):
        pitch, tone = polish.split_mode(mode)
        if (pitch, tone) == ("none", "none"):
            return "raw"
        engines = engines or {}
        text = f"pitch {pitch}"
        if engines.get("pitch"):
            text += f" by {plugins.short_name(engines['pitch'])}"
        text += f", tone {tone}"
        if engines.get("room"):
            text += f" + {plugins.short_name(engines['room'])}"
        if fallback:
            text += f"  (built-in: {plugins.short_name(fallback)} failed)"
        return text

    def on_route(self):
        route = self.route_var.get()
        if route not in engine.ROUTES:
            route = "headphones"
            self.route_var.set(route)
        self.settings["playback"] = route
        save_settings(self.settings)
        self.root.focus_set()
        if not self._replay_on_new_route():
            self.flash(f"Play on: {route.title()}")

    def cycle_route(self):
        routes = engine.ROUTES
        current = self.route_var.get()
        index = routes.index(current) if current in routes else -1
        self.route_var.set(routes[(index + 1) % len(routes)])
        self.on_route()

    def _replay_on_new_route(self):
        """Restart the take that is playing so it comes out of the new output."""
        active = self._active_audition()
        take = self.song.get(active[0]) if active is not None else None
        if take is None:
            return False
        self._request_play(take, active[1])
        return True

    def _cycle_level(self, var):
        levels = polish.LEVELS
        current = var.get() if var.get() in levels else levels[0]
        var.set(levels[(levels.index(current) + 1) % len(levels)])
        self.on_sound()

    def cycle_pitch(self):
        self._cycle_level(self.pitch_var)

    def cycle_tone(self):
        self._cycle_level(self.tone_var)

    def on_key(self, event=None):
        name = self.key_var.get()
        if name not in polish.KEY_NAMES:
            name = polish.KEY_NAMES[0]
            self.key_var.set(name)
        self._cancel_key_estimate()
        active = self._active_audition()
        changed = self._song_key_name() != name
        self.song.config["key"] = name
        self.song.save_config()
        self.root.focus_set()
        if changed and active is not None and active[1] != "off":
            take = self.song.get(active[0])
            if take is not None and not take.trashed:
                self._request_play(take, active[1])
        elif changed:
            self.flash(f"Key: {name}")

    def on_show_trash(self):
        self.refresh_takes()
        self.root.focus_set()

    def open_folder(self):
        os.startfile(self.song.dir)

    # ---- takes -------------------------------------------------------------

    def target_take(self):
        """The selected take, else the newest take that isn't trashed."""
        sel = self.tree.selection()
        if sel:
            return self.song.get(int(sel[0]))
        live = [t for t in self.song.takes() if not t.trashed]
        return live[-1] if live else None

    def _make_cue(self):
        cfg = self.song.config
        if self.track_data is None or not cfg.get("use_track", True):
            return None
        data, rate = self.track_data
        length = len(data) / rate
        play_from = max(0.0, min(cfg["start_at"] - cfg["leadin"], length - 1.0))
        latency = (self.settings.get("latency_ms") or 0.0) / 1000
        return engine.TrackCue(data, rate, play_from, latency)

    def toggle_record(self):
        if self.engine.recording:
            self.finish_take()
            return
        self.stop_playback()
        if self.engine.backend is None or self.engine.lost:
            self.flash("Interface not connected -- click Reconnect")
            return
        cue = self._make_cue()
        num, path = self.song.next_path()
        extra = {}
        if cue is not None:
            extra = {"track": self.song.config["track"], "track_start": round(cue.play_from, 3),
                     "latency_ms": self.settings.get("latency_ms")}
        self.song.begin(num, **extra)
        self._cancel_key_estimate()
        try:
            self.engine.start_take(str(path), cue)
        except Exception as exc:
            self.song.forget(num)
            if (self.track_data is not None and
                    self.song.config.get("key") not in polish.KEY_NAMES):
                self._queue_key_estimate()
            messagebox.showerror("Take Recorder", f"Could not start take:\n{exc}")
            return
        self.current_num = num
        self.current_cue = cue
        self.reset_clip()
        self._set_recording_ui(True)

    def finish_take(self):
        result = self.engine.stop_take()
        num, self.current_num = self.current_num, None
        self.current_cue = None
        self._set_recording_ui(False)
        if result is None:
            return None
        extra = {"unaligned": True} if result.align_failed else {}
        self.song.record(num, result.seconds, result.peak_db, result.clipped, result.dropouts,
                         **extra)
        if result.error:
            messagebox.showerror("Take Recorder", f"Take {num:03d} hit a disk error:\n{result.error}")
        self.last_recorded = num
        self.refresh_takes(select=num)
        return num

    def _set_recording_ui(self, recording):
        if recording:
            self._cancel_key_estimate()
        elif (not self._closing and self.track_data is not None and
              self.song.config.get("key") not in polish.KEY_NAMES):
            self._queue_key_estimate()
        state = "disabled" if recording else "normal"
        self.song_entry.config(state=state)
        self.rate_box.config(state="disabled" if recording else "readonly")
        for w in self.input_buttons + self.track_widgets:
            w.config(state=state)
        self.buttons["rec"].config(text=STOP_TEXT if recording else REC_TEXT)
        self._update_verdict()

    def _judge_num(self):
        """G/B judge the take being recorded, else the selected take, else the last one."""
        if self.engine.recording:
            return self.current_num
        sel = self.tree.selection()
        if sel:
            return int(sel[0])
        return self.last_recorded

    def _verdict_take(self):
        if self.engine.recording:
            return self.finish_take()  # stop it, then judge it
        return self._judge_num()

    def mark_good(self):
        recording = self.engine.recording
        num = self._verdict_take()
        take = self.song.get(num) if num is not None else None
        if take is None:
            return
        if take.trashed:
            self.song.restore(num)  # GOOD on a trashed take brings it back
        elif take.starred and not recording:
            self.song.set_star(num, False)  # G again takes the star back off
            self.flash(f"Take {num:03d}  no longer GOOD")
            self.refresh_takes(select=num)
            return
        self.song.set_star(num, True)
        self.flash(f"Take {num:03d}  GOOD")
        self.refresh_takes(select=num)

    def mark_bad(self):
        num = self._verdict_take()
        take = self.song.get(num) if num is not None else None
        if take is None or take.trashed:
            return
        if self.playing_num == num:
            self.stop_playback()
        self.song.set_star(num, False)
        self._trash_num(num)
        self.flash(f"Take {num:03d}  BAD -- trashed  (Ctrl+Z to undo)")

    def _update_verdict(self):
        if self.engine.recording and self.current_num is not None:
            text, fg, state = (f"Recording take {self.current_num:03d}  --  "
                               "G keeps it, B scraps it", C["fg"], "normal")
        elif self._judge_num() is not None:
            num = self._judge_num()
            take = self.song.get(num)
            which = "Last take" if num == self.last_recorded else "Selected take"
            mark = ("  (trash)" if take is not None and take.trashed else
                    "  GOOD" if take is not None and take.starred else "")
            text, fg, state = f"{which} {num:03d}{mark}", C["fg"], "normal"
        else:
            text, fg, state = "No take to judge yet", C["dim"], "disabled"
        self._verdict_live = fg != C["dim"]
        self._verdict_text = text
        self._paint_verdict()
        for b, color, hover in self.verdict_buttons:
            b.config(state=state)
            if state == "normal":
                b.recolor(color, C["ink"], hover)
            else:
                b.recolor(C["panel"], C["grey"])

    def _trash_num(self, num):
        active = self._active_audition()
        if active is not None and active[0] == num:
            self.stop_playback()
        if num == self.last_recorded:
            self.last_recorded = None
        if self.song.trash(num):
            self.undo_stack.append(num)
            self.flash(f"Take {num:03d} trashed  (Ctrl+Z to undo)")
        self.tree.selection_set(())
        self.refresh_takes()

    def undo_trash(self):
        while self.undo_stack:
            num = self.undo_stack.pop()
            if self.song.restore(num):
                newest = max(t.num for t in self.song.takes())
                if num == newest:
                    self.last_recorded = num  # undoing a BAD makes it judgeable again
                self.flash(f"Take {num:03d} restored")
                self.refresh_takes(select=num)
                return

    def refresh_takes(self, select=None):
        keep = select if select is not None else (
            int(self.tree.selection()[0]) if self.tree.selection() else None)
        self.tree.delete(*self.tree.get_children())
        row = 0
        for t in reversed(self.song.takes()):
            if t.trashed and not self.show_trash.get():
                continue
            notes = []
            if t.starred:
                notes.append("\u2605 KEEPER")
            if t.track:
                notes.append(f"over track from {fmt_clock(t.track_start)}")
            if t.clipped:
                notes.append("CLIPPED")
            if t.dropouts:
                notes.append(f"DROPOUT x{t.dropouts}")
            if t.unaligned:
                notes.append("NOT ALIGNED")
            if t.recovered:
                notes.append("RECOVERED")
            if t.trashed:
                notes.append("(trash)")
            tag = "trashed" if t.trashed else "bad" if (t.clipped or t.dropouts or t.unaligned) \
                else "starred" if t.starred else ""
            peak = f"{t.peak_db:.1f}" if t.peak_db > -199 else "--"
            tags = (tag, "stripe") if row % 2 else (tag,)
            row += 1
            self.tree.insert("", "end", iid=str(t.num), tags=tags,
                             values=(t.label, fmt_time(t.seconds), peak, "   ".join(notes)))
        live = [t for t in self.song.takes() if not t.trashed]
        keepers = sum(t.starred for t in live)
        self.takes_title.config(text=f"TAKES   {len(live)}" +
                                (f"   \u00b7   \u2605 {keepers}" if keepers else ""))
        if keep is not None and self.tree.exists(str(keep)):
            self.tree.selection_set(str(keep))
            self.tree.see(str(keep))
        self._update_verdict()

    # ---- playback ------------------------------------------------------------

    def _active_audition(self):
        job = self._pending_play
        if (job is not None and job["request_id"] == self._play_request_id and
                job["song"] is self.song):
            return job["take"].num, job["mode"]
        if self.playing_num is not None:
            return self.playing_num, self.playing_mode
        return None

    def toggle_play(self):
        if self.engine.recording:
            return
        take = self.target_take()
        if take is None:
            return
        active = self._active_audition()
        if active is not None and active[0] == take.num:
            self.stop_playback()
            return
        self._request_play(take, self.polish_var.get())

    def _request_play(self, take, mode):
        if self.engine.recording or self.calibration is not None:
            return
        try:
            mode = polish.normalize_mode(mode)
        except (TypeError, ValueError):
            mode = "off"
        self.stop_playback()
        request_id = self._play_request_id
        song = self.song
        if mode == "off":
            self._start_playback(take, take.path, mode, request_id, song)
            return
        if take.trashed:
            self.flash(f"Restore take {take.label} before polishing it")
            return
        try:
            key = polish.parse_key(self._song_key_name())
            engines = self._engines_for(mode)
            recipe = polish.recipe_hash(mode, key, plugins.recipe(engines))
        except Exception as exc:
            messagebox.showerror("Take Recorder",
                                 f"Could not prepare take {take.label}:\n{exc}")
            return
        job = {"kind": "render", "request_id": request_id, "song": song,
               "take": take, "mode": mode, "key": key, "recipe": recipe,
               "engines": engines, "plugins": dict(self.plugins_found),
               "cancel": threading.Event()}
        self._pending_play = job
        self._polishing = job
        self._work_queue.put(job)

    def _start_playback(self, take, path, mode, request_id, song):
        if (self._closing or self.engine.recording or self.calibration is not None or
                request_id != self._play_request_id or song is not self.song):
            return
        current = self.song.get(take.num)
        if current is None or (current.trashed and mode != "off"):
            return
        backing = None
        if current.track and current.track == self.song.config.get("track"):
            backing = self.track_data
        try:
            self.engine.play(str(path),
                             start_s=current.track_start if current.track else 0.0,
                             backing=backing, track_gain=self.settings.get("track_gain", 0.7),
                             route=self.route_var.get(), speakers=self.speakers_var.get())
        except Exception as exc:
            self._update_device_label()
            messagebox.showerror("Take Recorder", f"Could not play take {current.label}:\n{exc}")
            return
        self._pending_play = None
        self._polishing = None
        self.playing_num = current.num
        self.playing_mode = mode
        self.playing_engines = {}
        self.playing_fallback = None
        self._update_device_label()

    def stop_playback(self):
        if self._pending_play is not None:
            self._pending_play["cancel"].set()
        self._play_request_id += 1
        self._pending_play = None
        self._polishing = None
        paused = self.engine.input_paused
        self.engine.stop_playback()
        self.playing_num = None
        self.playing_mode = None
        if paused:
            self._update_device_label()

    # ---- latency calibration ------------------------------------------------

    def calibrate(self):
        if self.engine.recording or self.engine.backend is None:
            return
        if not messagebox.askokcancel("Calibrate latency", CALIBRATE_HELP):
            self.root.focus_set()
            return
        self.root.focus_set()
        self.stop_playback()
        self._cancel_key_estimate()
        data, times = engine.click_track(self.engine.samplerate)
        path = self.takes_root / ".calibration.wav"
        try:
            self.engine.start_take(str(path), engine.TrackCue(data, self.engine.samplerate, 0.0))
        except Exception as exc:
            if (self.track_data is not None and
                    self.song.config.get("key") not in polish.KEY_NAMES):
                self._queue_key_estimate()
            messagebox.showerror("Take Recorder", f"Could not start calibration:\n{exc}")
            return
        self.calibration = {"path": path, "times": times}
        self._set_recording_ui(True)

    def _abort_calibration(self):
        """Stop and remove an unfinished latency-calibration recording."""
        cal, self.calibration = self.calibration, None
        if cal is None:
            return False
        self.engine.stop_take()
        try:
            cal["path"].unlink(missing_ok=True)
        except OSError:
            pass
        self._set_recording_ui(False)
        return True

    def _finish_calibration(self):
        cal, self.calibration = self.calibration, None
        result = self.engine.stop_take()
        try:
            if result is None or result.align_failed:
                raise ValueError("The track and the input never lined up. Try again, or "
                                 "click Reconnect first.")
            audio, rate = sf.read(str(cal["path"]), dtype="float32", always_2d=True)
            delays = engine.detect_click_delays(audio.mean(axis=1), rate, cal["times"])
            latency, spread = engine.calibration_result(delays)
        except Exception as exc:
            messagebox.showwarning("Calibration didn't work", str(exc))
            return
        finally:
            cal["path"].unlink(missing_ok=True)
            self._set_recording_ui(False)
        self.settings["latency_ms"] = round(latency * 1000, 1)
        save_settings(self.settings)
        self._show_track_fields()
        messagebox.showinfo("Calibrated",
                            f"Latency: {latency * 1000:.1f} ms  (clicks agreed within "
                            f"{spread * 1000:.1f} ms).\n\nNew takes over a backing track are "
                            "shifted by this much so they line up with the beat.")

    # ---- meter / status -----------------------------------------------------

    def reset_clip(self):
        self.clipped = False
        self.meter.set_clip(False)

    def flash(self, text):
        print(f"[flash] {text}")
        self._flash = (text, 2.5)

    def tick(self):
        try:
            self._tick()
        except Exception:
            traceback.print_exc()  # keep the meter alive; the console shows what broke
        self.root.after(TICK_MS, self.tick)

    def _update_focus(self):
        """Crisp with a green banner when hotkeys land here; faded when they don't."""
        try:
            widget = self.root.focus_get()
        except KeyError:
            widget = self.root  # a combobox list is open: still this window
        if widget is None:
            state = "away"
        elif isinstance(widget, (tk.Entry, ttk.Entry)):
            state = "typing"
        else:
            state = "keys"
        if state == self._focus_state:
            return
        self._focus_state = state
        text, bg, fg = {
            "keys": ("KEYS ON  --  your keystrokes control the recorder", C["green"], "#0d1117"),
            "typing": ("TYPING  --  hotkeys off until you press Enter or Esc", C["yellow"],
                       "#0d1117"),
            "away": ("Keys go to another window  --  click here to use the hotkeys",
                     C["panel"], C["dim"]),
        }[state]
        self.focus_label.config(text=text, bg=bg, fg=fg)
        try:
            self.root.attributes("-alpha", UNFOCUSED_ALPHA if state == "away" else 1.0)
        except tk.TclError:
            pass

    def _tick(self):
        dt = TICK_MS / 1000
        self._update_focus()
        self._poll_work_results()
        peaks = self.engine.take_peaks()
        selected = INPUT_CHOICES[self.input_var.get()]
        for ch in range(engine.METER_CHANNELS):
            active = ch < self.engine.input_count
            db = engine.to_dbfs(peaks[ch]) if active else METER_MIN_DB
            self.display_db[ch] = max(db, self.display_db[ch] - DECAY_DB_PER_S * dt)
            self.hold_age[ch] += dt
            if db >= self.hold_db[ch] or self.hold_age[ch] > HOLD_S:
                self.hold_db[ch] = db
                self.hold_age[ch] = 0.0
            self.meter.show(ch, self.display_db[ch], self.hold_db[ch], ch in selected)
            if active and peaks[ch] >= engine.CLIP_LEVEL and not self.clipped:
                self.clipped = True
                self.meter.set_clip(True)

        if self.engine.lost:
            if self.calibration is not None:
                self._abort_calibration()
                messagebox.showwarning(
                    "Calibration stopped",
                    "The interface disconnected during calibration. Reconnect it and try again.")
            if self.engine.recording:
                num = self.finish_take()
                messagebox.showwarning("Take Recorder",
                                       f"The interface disconnected. Take {num:03d} was saved up "
                                       f"to that point. Click Reconnect when it's back.")
            self.engine.backend = None
            self.engine.error = "Interface disconnected -- click Reconnect"
            self.engine.lost = False
            self._update_device_label()

        track = self.engine.track
        if self.engine.recording and track is not None and not track.active:
            if self.calibration is not None:
                self._finish_calibration()
            else:
                num = self.finish_take()
                self.flash(f"Track ended -- take {num:03d} saved")
            track = None

        player = self.engine.player
        if self.playing_num is not None and (player is None or not player.active):
            self.stop_playback()

        flash = self._flash
        status = self._status_for(track, player, flash)
        if flash and flash[1] > 0 and status[1] == "flash":
            self._flash = (flash[0], flash[1] - dt)
        self._show_status(*status)
        self._update_play_button()

    STATUS_TONES = {  # tone: (background, text)
        "idle": (C["panel"], C["fg"]), "rec": (C["red"], "white"),
        "count": (C["yellow"], C["ink"]), "busy": (C["yellow"], C["ink"]),
        "play": (C["blue"], C["ink"]), "flash": (C["panel"], C["gold"]),
        "error": (C["panel"], C["red"]),
    }

    def _status_for(self, track, player, flash):
        """(text, tone, progress 0..1 or None) for the big status line."""
        dot = "\u25cf"
        if self.calibration is not None:
            return "CALIBRATING  \u00b7  keep the ear cup on the mic", "busy", None
        if self.engine.recording and track is not None:
            start_at = self.song.config["start_at"]
            if track.seconds < start_at:
                cue = self.current_cue
                lead = start_at - cue.play_from if cue is not None else 0.0
                done = 1 - (start_at - track.seconds) / lead if lead > 0 else None
                return (f"{dot} REC  take {self.current_num:03d}   "
                        f"in {start_at - track.seconds:.1f}", "count", done)
            length = len(self.track_data[0]) / self.track_data[1] if self.track_data else 0
            return (f"{dot} REC  take {self.current_num:03d}   @ {fmt_time(track.seconds)}",
                    "rec", track.seconds / length if length else None)
        if self.engine.recording:
            return (f"{dot} REC  take {self.current_num:03d}   "
                    f"{fmt_time(self.engine.take_seconds())}", "rec", None)
        if self._polishing is not None:
            job = self._polishing
            return f"POLISHING  take {job['take'].num:03d}...", "busy", None
        if self._estimating_key is not None:
            return "ANALYZING TRACK KEY...", "busy", None
        if self.playing_num is not None and player is not None:
            return (f"\u25ba PLAY  take {self.playing_num:03d}   "
                    f"{fmt_time(player.seconds)} / {fmt_time(player.length)}", "play",
                    player.seconds / player.length if player.length else None)
        if flash and flash[1] > 0:
            return flash[0], "flash", None
        if self.engine.backend is None:
            return "NO INTERFACE", "error", None
        return "READY  \u00b7  Space to record", "idle", None

    def _show_status(self, text, tone, progress):
        bg, fg = self.STATUS_TONES[tone]
        hearing = self._hearing()
        if self._status_shown != (text, tone) or self._hearing_shown != hearing:
            self._status_shown = (text, tone)
            self._hearing_shown = hearing
            self.status.config(text=text, bg=bg, fg=fg)
            self.status.master.config(bg=bg)
            self._paint_verdict()
        bar = {"rec": "white", "count": C["ink"], "play": C["ink"]}.get(tone)
        self.progress.show(progress, color=bar, track=bg if progress is not None else None)

    def _paint_verdict(self):
        tone = self._status_shown[1] if self._status_shown else "idle"
        bg, fg = self.STATUS_TONES[tone]
        if tone in ("idle", "flash", "error"):
            fg = C["fg"] if self._verdict_live else C["dim"]
        text = self._verdict_text
        hearing = self._hearing()
        if hearing:
            text = f"{text}   \u00b7   {hearing}" if text else hearing
        self.verdict_label.config(text=text, bg=bg, fg=fg)

    def _hearing(self):
        """Which version is playing or being prepared, in words."""
        job = self._polishing
        if job is not None:
            return self._describe(job["mode"], job.get("engines"))
        if self.playing_num is not None and self.playing_mode is not None:
            return "hearing " + self._describe(self.playing_mode, self.playing_engines,
                                               self.playing_fallback)
        return ""

    def _update_play_button(self):
        playing = self._active_audition() is not None
        text = STOP_PLAY_TEXT if playing else PLAY_TEXT
        if self.buttons["play"].cget("text") != text:
            self.buttons["play"].config(text=text)

    def on_close(self):
        self._closing = True
        if self._pending_play is not None:
            self._pending_play["cancel"].set()
        self._play_request_id += 1
        self._pending_play = None
        self._polishing = None
        self._cancel_key_estimate()
        self._work_queue.put(None)
        if self.calibration is not None:
            self._abort_calibration()
        if self.engine.recording:
            self.finish_take()
        self.engine.stop_playback()
        self.engine.close()
        self.settings["song"] = self.song.name
        save_settings(self.settings)
        self.root.destroy()


class _Log:
    """Copy console output to take-recorder.log, one timestamped line at a time."""

    def __init__(self, console, file):
        self.console, self.file, self._line_start = console, file, True

    def write(self, text):
        if self.console is not None:
            try:
                self.console.write(text)
            except (OSError, ValueError):
                pass
        for part in text.splitlines(keepends=True):
            if self._line_start:
                self.file.write(time.strftime("%H:%M:%S "))
            self.file.write(part)
            self._line_start = part.endswith("\n")
        self.file.flush()
        return len(text)

    def flush(self):
        self.file.flush()


def start_log():
    """Send prints, warnings and every uncaught error (any thread) to the log."""
    file = open(LOG_PATH, "a", encoding="ascii", errors="backslashreplace", buffering=1)
    sys.stdout = _Log(sys.stdout, file)
    sys.stderr = _Log(sys.stderr, file)
    sys.excepthook = lambda *exc: traceback.print_exception(*exc)
    threading.excepthook = lambda args: (
        print(f"[thread error] {args.thread.name if args.thread else '?'}", file=sys.stderr),
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback))


def main():
    start_log()
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(PROCESS_TITLE)
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    PID_PATH.write_text(str(os.getpid()), encoding="ascii")
    atexit.register(lambda: PID_PATH.unlink(missing_ok=True))
    print(f"[{PROCESS_TITLE}] pid {os.getpid()} -- close the window or run stop.bat to quit")

    root = tk.Tk()
    root.report_callback_exception = lambda *exc: traceback.print_exception(*exc)
    App(root, load_settings())
    root.mainloop()


if __name__ == "__main__":
    main()
