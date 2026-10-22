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
import polish
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
}
INPUT_CHOICES = {"1": [0], "2": [1], "1+2": [0, 1]}
SAMPLE_RATES = ("auto", "44100", "48000", "96000")
KEY_CONFIDENCE_MIN = 0.12

METER_W = 640
BAR_H = 26
METER_MIN_DB = -60.0
DECAY_DB_PER_S = 24.0
HOLD_S = 1.5
TICK_MS = 33
RETAKE_CONFIRM_S = 20.0  # R asks before throwing away a take at least this long
UNFOCUSED_ALPHA = 0.55  # window opacity while the keyboard goes somewhere else

C = dict(bg="#1e1f22", panel="#2b2d31", fg="#e6e6e6", dim="#8a8f98", green="#3fb950",
         yellow="#d29922", red="#f85149", blue="#58a6ff", gold="#e3b341", grey="#5c6370",
         target="#24382a")

HELP = ("Space record / stop   G / B good / bad   R retake   Backspace trash   "
        "Ctrl+Z undo   P play   V sound good   O headphones / speakers   S star")

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


def db_to_x(db):
    frac = (db - METER_MIN_DB) / -METER_MIN_DB
    return max(0.0, min(1.0, frac)) * METER_W


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
        root.configure(bg=C["bg"])
        root.minsize(760, 600)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", background=C["bg"], foreground=C["fg"], fieldbackground=C["panel"])
        style.configure("Treeview", background=C["panel"], fieldbackground=C["panel"],
                        foreground=C["fg"], rowheight=26, font=("Segoe UI", 11))
        style.configure("Treeview.Heading", background=C["bg"], foreground=C["dim"],
                        font=("Segoe UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#3b4252")])
        style.configure("TButton", background=C["panel"], foreground=C["fg"], padding=(10, 6))
        style.map("TButton", background=[("active", "#3b4252"), ("disabled", C["bg"])])
        style.configure("Small.TButton", padding=(6, 2))
        style.configure("TRadiobutton", background=C["bg"], foreground=C["fg"])
        style.map("TRadiobutton", background=[("disabled", C["bg"]), ("active", C["bg"])],
                  foreground=[("disabled", C["dim"])])
        style.configure("TCheckbutton", background=C["bg"], foreground=C["fg"])
        style.map("TCheckbutton", background=[("disabled", C["bg"]), ("active", C["bg"])],
                  foreground=[("disabled", C["dim"])])
        style.configure("TCombobox", fieldbackground=C["panel"], foreground=C["fg"],
                        background=C["panel"], arrowcolor=C["fg"])
        style.map("TCombobox",
                  fieldbackground=[("readonly", C["panel"]), ("disabled", C["bg"])],
                  foreground=[("readonly", C["fg"]), ("disabled", C["dim"])],
                  selectbackground=[("readonly", C["panel"])],
                  selectforeground=[("readonly", C["fg"])])

        pad = dict(padx=14)
        entry_opts = dict(bg=C["panel"], fg=C["fg"], insertbackground=C["fg"], relief="flat",
                          disabledbackground=C["panel"], disabledforeground=C["dim"])

        # Says whether keystrokes are going to the recorder right now
        self.focus_label = tk.Label(root, text="", font=("Segoe UI", 9, "bold"), pady=3)
        self.focus_label.pack(fill="x")

        # Song + input selection
        top = tk.Frame(root, bg=C["bg"])
        top.pack(fill="x", pady=(12, 4), **pad)
        tk.Label(top, text="Song", bg=C["bg"], fg=C["dim"], font=("Segoe UI", 10)).pack(side="left")
        self.song_var = tk.StringVar(value=self.song.name)
        self.song_entry = tk.Entry(top, textvariable=self.song_var, width=28,
                                   font=("Segoe UI", 12), **entry_opts)
        self.song_entry.pack(side="left", padx=(8, 6), ipady=3)
        self.song_entry.bind("<Return>", self.on_song_enter)
        self.song_entry.bind("<Escape>", self.on_song_escape)
        ttk.Button(top, text="Open folder", takefocus=0, command=self.open_folder).pack(side="left")

        self.rate_var = tk.StringVar(value=str(self.settings["samplerate"]))
        self.rate_box = ttk.Combobox(top, textvariable=self.rate_var, values=SAMPLE_RATES,
                                     width=7, state="readonly", takefocus=0)
        self.rate_box.pack(side="right")
        self.rate_box.bind("<<ComboboxSelected>>", self.on_rate)
        tk.Label(top, text="Hz", bg=C["bg"], fg=C["dim"]).pack(side="right", padx=(10, 4))
        self.input_var = tk.StringVar(value=self.settings["input"]
                                      if self.settings["input"] in INPUT_CHOICES else "1")
        self.input_buttons = []
        for label in reversed(list(INPUT_CHOICES)):
            b = ttk.Radiobutton(top, text=label, value=label, variable=self.input_var,
                                takefocus=0, command=self.on_input)
            b.pack(side="right", padx=2)
            self.input_buttons.append(b)
        tk.Label(top, text="Record input", bg=C["bg"], fg=C["dim"]).pack(side="right", padx=(0, 4))

        # Backing track
        trk = tk.Frame(root, bg=C["bg"])
        trk.pack(fill="x", pady=(2, 4), **pad)
        tk.Label(trk, text="Track", bg=C["bg"], fg=C["dim"], font=("Segoe UI", 10)).pack(side="left")
        self.track_label = tk.Label(trk, text="none", bg=C["bg"], fg=C["fg"], anchor="w",
                                    width=26, font=("Segoe UI", 10))
        self.track_label.pack(side="left", padx=(8, 4))
        self.track_widgets = []
        for text, cmd in (("Load...", self.load_track_dialog), ("Clear", self.clear_track)):
            b = ttk.Button(trk, text=text, style="Small.TButton", takefocus=0, command=cmd)
            b.pack(side="left", padx=(0, 4))
            self.track_widgets.append(b)
        self.use_track_var = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(trk, text="Play while recording", variable=self.use_track_var,
                             takefocus=0, command=self.on_track_fields)
        cb.pack(side="left", padx=(6, 10))
        self.track_widgets.append(cb)
        self.start_var = tk.StringVar()
        self.leadin_var = tk.StringVar()
        for label, var, width in (("Start at", self.start_var, 6), ("Count-in s", self.leadin_var, 4)):
            tk.Label(trk, text=label, bg=C["bg"], fg=C["dim"]).pack(side="left", padx=(4, 4))
            e = tk.Entry(trk, textvariable=var, width=width, font=("Segoe UI", 10), **entry_opts)
            e.pack(side="left", ipady=2)
            e.bind("<Return>", self.on_track_fields)
            e.bind("<FocusOut>", self.on_track_fields)
            self.track_widgets.append(e)
        self.latency_label = tk.Label(trk, text="", bg=C["bg"], fg=C["dim"], font=("Segoe UI", 9))
        cal = ttk.Button(trk, text="Calibrate", style="Small.TButton", takefocus=0,
                         command=self._act(self.calibrate))
        cal.pack(side="right")
        self.track_widgets.append(cal)
        self.latency_label.pack(side="right", padx=(0, 6))

        # Meter
        meter_frame = tk.Frame(root, bg=C["bg"])
        meter_frame.pack(fill="x", pady=(6, 0), **pad)
        self.meter = tk.Canvas(meter_frame, width=METER_W + 130,
                               height=BAR_H * engine.METER_CHANNELS + 30,
                               bg=C["bg"], highlightthickness=0)
        self.meter.pack(side="left")
        self._build_meter()
        self.clip_label = tk.Label(meter_frame, text="CLIP", width=6, bg=C["panel"], fg=C["dim"],
                                   font=("Segoe UI", 12, "bold"), cursor="hand2")
        self.clip_label.pack(side="left", padx=(10, 0), ipady=8)
        self.clip_label.bind("<Button-1>", lambda e: self.reset_clip())
        tk.Label(root, text="Aim for loudest peaks inside the shaded zone (-18 to -6 dBFS). "
                            "Set gain on the 26c, not here.",
                 bg=C["bg"], fg=C["dim"], font=("Segoe UI", 9)).pack(anchor="w", **pad)

        # Big status
        self.status = tk.Label(root, text="", bg=C["panel"], fg=C["fg"],
                               font=("Segoe UI", 26, "bold"), pady=10)
        self.status.pack(fill="x", pady=(10, 6), **pad)

        # Verdict on the take just recorded
        verdict = tk.Frame(root, bg=C["bg"])
        verdict.pack(fill="x", pady=(0, 10), **pad)
        self.verdict_label = tk.Label(verdict, text="", bg=C["bg"], fg=C["dim"], anchor="w",
                                      font=("Segoe UI", 12))
        self.verdict_label.pack(side="left", fill="x", expand=True)
        self.verdict_buttons = []
        for text, color, cmd in (("BAD  (B)", C["red"], self.mark_bad),
                                 ("GOOD  (G)", C["green"], self.mark_good)):
            b = tk.Button(verdict, text=text, command=self._act(cmd), takefocus=0, width=12,
                          font=("Segoe UI", 15, "bold"), bg=color, fg="#0d1117",
                          activebackground=C["fg"], disabledforeground="#3a3d44",
                          relief="flat", cursor="hand2")
            b.pack(side="right", padx=(8, 0), ipady=4)
            self.verdict_buttons.append(b)

        # Buttons
        bar = tk.Frame(root, bg=C["bg"])
        bar.pack(fill="x", **pad)
        self.buttons = {}
        for key, text, cmd in (
                ("rec", "Record (Space)", self.toggle_record),
                ("retake", "Retake (R)", self.retake),
                ("trash", "Trash (Bksp)", self.trash),
                ("undo", "Undo trash (Ctrl+Z)", self.undo_trash),
                ("star", "Star (S)", self.toggle_star)):
            b = ttk.Button(bar, text=text, takefocus=0, command=self._act(cmd))
            b.pack(side="left", padx=(0, 6))
            self.buttons[key] = b
        self.show_trash = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Show trash", variable=self.show_trash, takefocus=0,
                        command=self.on_show_trash).pack(side="right")

        # Playback options get their own compact row so the controls still fit
        # at the minimum window width.
        playback = tk.Frame(root, bg=C["bg"])
        playback.pack(fill="x", pady=(6, 0), **pad)
        b = ttk.Button(playback, text="Play (P)", takefocus=0,
                       command=self._act(self.toggle_play))
        b.pack(side="left", padx=(0, 10))
        self.buttons["play"] = b
        tk.Label(playback, text="Sound good (V)", bg=C["bg"], fg=C["dim"]).pack(side="left")
        self.polish_var = tk.StringVar(value=self.settings["polish"])
        self.polish_buttons = []
        for mode in polish.MODES:
            button = ttk.Radiobutton(playback, text=mode.title(), value=mode,
                                     variable=self.polish_var, takefocus=0,
                                     command=self.on_polish_mode)
            button.pack(side="left", padx=(6, 0))
            self.polish_buttons.append(button)
        self.key_var = tk.StringVar(value=self._song_key_name())
        self.key_box = ttk.Combobox(playback, textvariable=self.key_var,
                                    values=tuple(polish.KEY_NAMES), width=13,
                                    state="readonly", takefocus=0)
        self.key_box.pack(side="right")
        self.key_box.bind("<<ComboboxSelected>>", self.on_key)
        tk.Label(playback, text="Key", bg=C["bg"], fg=C["dim"]).pack(side="right",
                 padx=(10, 4))

        # Where playback goes. Recording over a track always uses the headphones.
        route = tk.Frame(root, bg=C["bg"])
        route.pack(fill="x", pady=(6, 0), **pad)
        tk.Label(route, text="Play on (O)", bg=C["bg"], fg=C["dim"]).pack(side="left")
        self.route_var = tk.StringVar(value=self.settings["playback"])
        for value in engine.ROUTES:
            ttk.Radiobutton(route, text=value.title(), value=value, variable=self.route_var,
                            takefocus=0, command=self.on_route).pack(side="left", padx=(6, 0))
        self.speakers_var = tk.StringVar()
        self.speakers_box = ttk.Combobox(route, textvariable=self.speakers_var, width=34,
                                         state="readonly", takefocus=0,
                                         postcommand=self._refresh_speakers)
        self.speakers_box.pack(side="right")
        self.speakers_box.bind("<<ComboboxSelected>>", self.on_speakers)
        tk.Label(route, text="Speakers", bg=C["bg"], fg=C["dim"]).pack(side="right",
                 padx=(10, 4))
        self._refresh_speakers()

        # Takes list
        cols = ("take", "length", "peak", "notes")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", selectmode="browse")
        for col, text, width, anchor in (("take", "Take", 70, "center"),
                                         ("length", "Length", 90, "center"),
                                         ("peak", "Peak dBFS", 100, "center"),
                                         ("notes", "Notes", 380, "w")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "notes"))
        self.tree.tag_configure("trashed", foreground=C["grey"])
        self.tree.tag_configure("starred", foreground=C["gold"])
        self.tree.tag_configure("bad", foreground=C["red"])
        self.tree.pack(fill="both", expand=True, pady=(10, 6), **pad)
        self.tree.bind("<Double-1>", lambda e: self._act(self.toggle_play)())

        tk.Label(root, text=HELP, bg=C["bg"], fg=C["dim"], font=("Segoe UI", 9)).pack(**pad)

        # Device status
        foot = tk.Frame(root, bg=C["bg"])
        foot.pack(fill="x", pady=(4, 10), **pad)
        self.device_label = tk.Label(foot, text="", bg=C["bg"], fg=C["dim"], anchor="w",
                                     justify="left", font=("Segoe UI", 9))
        self.device_label.pack(side="left", fill="x", expand=True)
        ttk.Button(foot, text="Reconnect", takefocus=0,
                   command=self._act(lambda: self.connect(refresh=True))).pack(side="right")

    def _build_meter(self):
        cv = self.meter
        self.bars = []
        for ch in range(engine.METER_CHANNELS):
            y0 = ch * BAR_H + 2
            y1 = y0 + BAR_H - 6
            cv.create_rectangle(0, y0, METER_W, y1, fill=C["panel"], width=0)
            cv.create_rectangle(db_to_x(-18), y0, db_to_x(-6), y1, fill=C["target"], width=0)
            level = cv.create_rectangle(0, y0, 0, y1, fill=C["green"], width=0)
            hold = cv.create_line(0, y0, 0, y1, fill=C["fg"], width=2)
            name = cv.create_text(METER_W + 10, (y0 + y1) / 2, anchor="w", fill=C["dim"],
                                  font=("Segoe UI", 10, "bold"), text=f"IN {ch + 1}")
            value = cv.create_text(METER_W + 125, (y0 + y1) / 2, anchor="e", fill=C["fg"],
                                   font=("Consolas", 11), text="-inf")
            self.bars.append((level, hold, name, value, y0, y1))
        y = engine.METER_CHANNELS * BAR_H + 4
        for db in (-48, -36, -24, -18, -12, -6, -3, 0):
            x = db_to_x(db)
            cv.create_line(x, y - 4, x, y, fill=C["dim"])
            cv.create_text(x, y + 2, anchor="n", fill=C["dim"], font=("Segoe UI", 8), text=str(db))

    def _act(self, fn):
        """Wrap a button/hotkey action so it does nothing while calibrating."""
        def run():
            if self.calibration is None:
                print(f"[action] {fn.__name__}")
                fn()
        return run

    def _bind_keys(self):
        for seq, fn in (("<space>", self.toggle_record),
                        ("<KeyPress-r>", self.retake), ("<KeyPress-R>", self.retake),
                        ("<BackSpace>", self.trash), ("<Delete>", self.trash),
                        ("<Control-z>", self.undo_trash), ("<Control-Z>", self.undo_trash),
                        ("<KeyPress-p>", self.toggle_play), ("<KeyPress-P>", self.toggle_play),
                        ("<Return>", self.toggle_play),
                        ("<KeyPress-v>", self.cycle_polish),
                        ("<KeyPress-V>", self.cycle_polish),
                        ("<KeyPress-o>", self.cycle_route),
                        ("<KeyPress-O>", self.cycle_route),
                        ("<KeyPress-s>", self.toggle_star), ("<KeyPress-S>", self.toggle_star),
                        ("<KeyPress-g>", self.mark_good), ("<KeyPress-G>", self.mark_good),
                        ("<KeyPress-b>", self.mark_bad), ("<KeyPress-B>", self.mark_bad),
                        ("<Escape>", self.stop_playback)):
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
                            source_stamp = song.polish_source_stamp(take)
                            data, rate = sf.read(str(take.path), dtype="float32", always_2d=True)
                            data = polish.render(
                                data, rate, job["mode"], job["key"],
                                cancel=lambda: not self._render_job_current(job))
                            if self._render_job_current(job):
                                path = song.publish_polished(
                                    take, job["mode"], job["recipe"], data, rate,
                                    expected_source=source_stamp)
                            else:
                                path = None
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
        if error is not None:
            messagebox.showerror("Take Recorder",
                                 f"Could not polish take {job['take'].label}:\n{error}")
            return
        take = self.song.get(job["take"].num)
        if take is None or take.trashed:
            return
        self._start_playback(take, path, job["mode"], job["request_id"], job["song"])

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
        if self.engine.input_paused:
            self.device_label.config(
                text="Input paused during playback: the 26c can't run input and output at "
                     "different rates.\nSet Hz to auto to avoid this.", fg=C["yellow"])
            return
        if b is None:
            self.device_label.config(text=self.engine.error or "No interface", fg=C["red"])
            return
        quality = ("bit-perfect" if b.bit_perfect
                   else "NOT bit-perfect -- Windows is processing the signal")
        auto = " (auto)" if self.engine.requested_rate == "auto" else ""
        self.device_label.config(
            text=f"{b.describe()}  |  {self.engine.samplerate} Hz{auto} / 24-bit WAV  |  {quality}\n"
                 f"Headphones: {self.engine.output_name()}   |   Takes: {self.song.dir}",
            fg=C["green"] if b.bit_perfect else C["yellow"])

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
        if self.track_data is not None:
            name = cfg["track"].removeprefix("backing - ")
            length = len(self.track_data[0]) / self.track_data[1]
            self.track_label.config(text=f"{name}  ({fmt_clock(length)})", fg=C["fg"])
        else:
            self.track_label.config(text="none (records dry)", fg=C["dim"])
        self.use_track_var.set(bool(cfg.get("use_track", True)))
        self.start_var.set(fmt_clock(cfg.get("start_at", 0.0)))
        self.leadin_var.set(f"{cfg.get('leadin', 2.0):g}")
        latency = self.settings.get("latency_ms")
        if latency is None:
            self.latency_label.config(text="Latency: not calibrated", fg=C["yellow"])
        else:
            self.latency_label.config(text=f"Latency: {latency:.1f} ms", fg=C["dim"])
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
            self.flash(f"Sound good: {mode.title()}")

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

    def cycle_polish(self):
        try:
            current = polish.normalize_mode(self.polish_var.get())
        except (TypeError, ValueError):
            current = "off"
        modes = tuple(polish.MODES)
        self.polish_var.set(modes[(modes.index(current) + 1) % len(modes)])
        self.on_polish_mode()

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
        self.buttons["rec"].config(text="Stop (Space)" if recording else "Record (Space)")
        self.buttons["trash"].config(text="Abort take (Bksp)" if recording else "Trash (Bksp)")
        self._update_verdict()

    def retake(self):
        if self.engine.recording:
            if not self._confirm_retake(self.current_num, self.engine.take_seconds()):
                return
            num = self.finish_take()
            if num is not None:
                self._trash_num(num)
        elif self.last_recorded is not None:
            take = self.song.get(self.last_recorded)
            if take is not None and not take.trashed:
                if take.starred:
                    self.flash(f"Take {take.label} is starred -- R won't trash a keeper")
                    return
                if not self._confirm_retake(take.num, take.seconds):
                    return
                self._trash_num(take.num)
        self.toggle_record()

    def _confirm_retake(self, num, seconds):
        if seconds < RETAKE_CONFIRM_S:
            return True
        ok = messagebox.askyesno(
            "Take Recorder", f"Trash take {num:03d} ({fmt_clock(seconds)} long) and "
            "record it again?", default=messagebox.NO)
        print(f"[retake] take {num:03d} {seconds:.1f}s: {'yes' if ok else 'kept'}")
        return ok

    def trash(self):
        if self.engine.recording:
            num = self.finish_take()
            if num is not None:
                self._trash_num(num)
            return
        take = self.target_take()
        if take is None or take.trashed:
            return
        if self.playing_num == take.num:
            self.stop_playback()
        self._trash_num(take.num)

    def _verdict_take(self):
        """G/B act on the take being recorded (stopping it) or the one just recorded."""
        if self.engine.recording:
            return self.finish_take()
        return self.last_recorded

    def mark_good(self):
        num = self._verdict_take()
        if num is None:
            return
        self.song.set_star(num, True)
        self.flash(f"Take {num:03d}  GOOD")
        self.refresh_takes(select=num)

    def mark_bad(self):
        num = self._verdict_take()
        if num is None:
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
        elif self.last_recorded is not None:
            take = self.song.get(self.last_recorded)
            mark = "  GOOD" if take is not None and take.starred else ""
            text, fg, state = f"Last take {self.last_recorded:03d}{mark}", C["fg"], "normal"
        else:
            text, fg, state = "No take to judge yet", C["dim"], "disabled"
        self.verdict_label.config(text=text, fg=fg)
        for b, color in zip(self.verdict_buttons, (C["red"], C["green"])):
            b.config(state=state, bg=color if state == "normal" else C["panel"])

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

    def toggle_star(self):
        take = self.target_take()
        if take is not None:
            self.song.toggle_star(take.num)
            self.refresh_takes(select=take.num)

    def refresh_takes(self, select=None):
        keep = select if select is not None else (
            int(self.tree.selection()[0]) if self.tree.selection() else None)
        self.tree.delete(*self.tree.get_children())
        for t in reversed(self.song.takes()):
            if t.trashed and not self.show_trash.get():
                continue
            notes = []
            if t.starred:
                notes.append("* KEEPER")
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
            self.tree.insert("", "end", iid=str(t.num), tags=(tag,),
                             values=(t.label, fmt_time(t.seconds), peak, "   ".join(notes)))
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
            recipe = polish.recipe_hash(mode, key)
        except Exception as exc:
            messagebox.showerror("Take Recorder",
                                 f"Could not prepare take {take.label}:\n{exc}")
            return
        job = {"kind": "render", "request_id": request_id, "song": song,
               "take": take, "mode": mode, "key": key, "recipe": recipe,
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
        self.clip_label.config(bg=C["panel"], fg=C["dim"])

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
        for ch, (level, hold, name, value, y0, y1) in enumerate(self.bars):
            active = ch < self.engine.input_count
            db = engine.to_dbfs(peaks[ch]) if active else METER_MIN_DB
            self.display_db[ch] = max(db, self.display_db[ch] - DECAY_DB_PER_S * dt)
            self.hold_age[ch] += dt
            if db >= self.hold_db[ch] or self.hold_age[ch] > HOLD_S:
                self.hold_db[ch] = db
                self.hold_age[ch] = 0.0
            shown = self.display_db[ch]
            color = C["red"] if shown > -1 else C["yellow"] if shown > -6 else C["green"]
            self.meter.coords(level, 0, y0, db_to_x(shown), y1)
            self.meter.itemconfig(level, fill=color)
            hx = db_to_x(self.hold_db[ch])
            self.meter.coords(hold, hx, y0, hx, y1)
            self.meter.itemconfig(name, fill=C["fg"] if ch in selected else C["dim"])
            self.meter.itemconfig(value, text=f"{self.hold_db[ch]:6.1f}"
                                  if self.hold_db[ch] > METER_MIN_DB else "-inf")
            if active and peaks[ch] >= engine.CLIP_LEVEL and not self.clipped:
                self.clipped = True
                self.clip_label.config(bg=C["red"], fg="white")

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
        if self.calibration is not None:
            self.status.config(text="CALIBRATING   keep the ear cup on the mic",
                               bg=C["yellow"], fg="#0d1117")
        elif self.engine.recording and track is not None:
            start_at = self.song.config["start_at"]
            if track.seconds < start_at:
                self.status.config(text=f"REC   take {self.current_num:03d}   "
                                        f"in {start_at - track.seconds:.1f}",
                                   bg=C["yellow"], fg="#0d1117")
            else:
                self.status.config(text=f"REC   take {self.current_num:03d}   "
                                        f"@ {fmt_time(track.seconds)}",
                                   bg=C["red"], fg="white")
        elif self.engine.recording:
            self.status.config(text=f"REC   take {self.current_num:03d}   "
                                    f"{fmt_time(self.engine.take_seconds())}",
                               bg=C["red"], fg="white")
        elif self._polishing is not None:
            job = self._polishing
            self.status.config(text=f"POLISHING {job['mode'].upper()}   "
                                    f"take {job['take'].num:03d}...",
                               bg=C["yellow"], fg="#0d1117")
        elif self._estimating_key is not None:
            self.status.config(text="ANALYZING TRACK KEY...",
                               bg=C["yellow"], fg="#0d1117")
        elif self.playing_num is not None and player is not None:
            self.status.config(text=f"PLAY {self.playing_mode.upper()}   "
                                    f"take {self.playing_num:03d}   "
                                    f"{fmt_time(player.seconds)} / {fmt_time(player.length)}",
                               bg=C["blue"], fg="#0d1117")
        elif flash and flash[1] > 0:
            self.status.config(text=flash[0], bg=C["panel"], fg=C["gold"])
            self._flash = (flash[0], flash[1] - dt)
        elif self.engine.backend is None:
            self.status.config(text="NO INTERFACE", bg=C["panel"], fg=C["red"])
        else:
            self.status.config(text="READY   Space to record", bg=C["panel"], fg=C["fg"])

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
