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
PLAY_TEXT, STOP_PLAY_TEXT = "\u25b6  Play   P", "\u25a0  Stop   P"

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
        root.minsize(px(780), px(700))

        # Says whether keystrokes are going to the recorder right now
        self.focus_label = tk.Label(root, text="", font=("Segoe UI", 9, "bold"), pady=px(3))
        self.focus_label.pack(fill="x")

        # Device status. Packed before the body so a short window squeezes the
        # takes list, never this line.
        foot = tk.Frame(root, bg=C["panel"], padx=px(14), pady=px(6))
        foot.pack(fill="x", side="bottom")
        self.device_dot = tk.Label(foot, text="\u25cf", bg=C["panel"], fg=C["grey"],
                                   font=("Segoe UI", 10))
        ttk.Button(foot, text="Reconnect", style="Small.TButton", takefocus=0,
                   command=self._act(lambda: self.connect(refresh=True))).pack(side="right")
        self.device_dot.pack(side="left", anchor="n")
        self.device_label = ui.label(foot, "", size=9, anchor="w", justify="left")
        self.device_label.pack(side="left", fill="x", expand=True, padx=(px(6), 0))

        body = tk.Frame(root, bg=C["bg"], padx=px(14), pady=px(10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        gap = dict(sticky="ew", pady=(0, px(8)))

        # ---- setup card: song, input, track --------------------------------
        setup = ui.card(body, px)
        setup.grid(row=0, column=0, **gap)
        setup.columnconfigure(1, weight=1)

        ui.label(setup, "SONG", size=8, weight="bold").grid(row=0, column=0, sticky="w")
        song_row = tk.Frame(setup, bg=C["panel"])
        song_row.grid(row=0, column=1, sticky="ew", padx=(px(10), 0))
        self.song_var = tk.StringVar(value=self.song.name)
        self.song_entry = ui.entry(song_row, self.song_var, 22, size=13, px=px)
        self.song_entry.pack(side="left", ipady=px(2))
        self.song_entry.bind("<Return>", self.on_song_enter)
        self.song_entry.bind("<Escape>", self.on_song_escape)
        ttk.Button(song_row, text="Open folder", takefocus=0,
                   command=self.open_folder).pack(side="left", padx=(px(6), 0))

        self.rate_var = tk.StringVar(value=str(self.settings["samplerate"]))
        self.rate_box = ttk.Combobox(song_row, textvariable=self.rate_var, values=SAMPLE_RATES,
                                     width=6, state="readonly", takefocus=0)
        self.rate_box.pack(side="right")
        self.rate_box.bind("<<ComboboxSelected>>", self.on_rate)
        ui.label(song_row, "Hz").pack(side="right", padx=(px(12), px(4)))
        self.input_var = tk.StringVar(value=self.settings["input"]
                                      if self.settings["input"] in INPUT_CHOICES else "1")
        self.input_select = ui.Segmented(song_row, self.input_var, list(INPUT_CHOICES),
                                         self.on_input, px=px)
        self.input_select.pack(side="right")
        self.input_buttons = [self.input_select]
        ui.label(song_row, "Input").pack(side="right", padx=(px(12), px(4)))

        ui.label(setup, "TRACK", size=8, weight="bold").grid(row=1, column=0, sticky="w",
                                                             pady=(px(8), 0))
        trk = tk.Frame(setup, bg=C["panel"])
        trk.grid(row=1, column=1, sticky="ew", padx=(px(10), 0), pady=(px(8), 0))
        self.track_label = ui.label(trk, "none", fg=C["fg"], anchor="w")
        self.track_label.pack(side="left", padx=(0, px(8)))
        self.track_widgets = []
        for text, cmd in (("Load...", self.load_track_dialog), ("Clear", self.clear_track)):
            b = ttk.Button(trk, text=text, style="Small.TButton", takefocus=0, command=cmd)
            b.pack(side="left", padx=(0, px(4)))
            self.track_widgets.append(b)
        cal = ttk.Button(trk, text="Calibrate", style="Small.TButton", takefocus=0,
                         command=self._act(self.calibrate))
        cal.pack(side="right")
        self.track_widgets.append(cal)
        self.latency_label = ui.label(trk, "", size=9)
        self.latency_label.pack(side="right", padx=(px(8), px(6)))

        cue = tk.Frame(setup, bg=C["panel"])
        cue.grid(row=2, column=1, sticky="ew", padx=(px(10), 0), pady=(px(6), 0))
        self.use_track_var = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(cue, text="Play while recording", variable=self.use_track_var,
                             style="Card.TCheckbutton", takefocus=0,
                             command=self.on_track_fields)
        cb.pack(side="left", padx=(0, px(16)))
        self.track_widgets.append(cb)
        self.start_var = tk.StringVar()
        self.leadin_var = tk.StringVar()
        for text, var, width in (("Start at", self.start_var, 6),
                                 ("Count-in s", self.leadin_var, 4)):
            ui.label(cue, text).pack(side="left", padx=(0, px(4)))
            e = ui.entry(cue, var, width, px=px)
            e.pack(side="left", ipady=px(1), padx=(0, px(16)))
            e.bind("<Return>", self.on_track_fields)
            e.bind("<FocusOut>", self.on_track_fields)
            self.track_widgets.append(e)

        # ---- input meter -----------------------------------------------------
        self.meter = ui.Meter(body, engine.METER_CHANNELS, METER_MIN_DB, px,
                              on_clip_click=self.reset_clip)
        self.meter.grid(row=1, column=0, sticky="ew", pady=(px(6), 0))
        ui.label(body, "Loudest notes should peak in the shaded zone (-18 to -6 dBFS). "
                       "Set gain on the interface, not here.", size=9
                 ).grid(row=2, column=0, sticky="w", pady=(0, px(10)))

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

        # ---- transport + playback card ----------------------------------------
        deck = ui.card(body, px)
        deck.grid(row=4, column=0, **gap)
        deck.columnconfigure(1, weight=1)
        transport = tk.Frame(deck, bg=C["panel"])
        transport.grid(row=0, column=0, columnspan=2, sticky="ew")
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

        ui.label(deck, "Sound good  V").grid(row=1, column=0, sticky="w", pady=(px(10), 0))
        sound = tk.Frame(deck, bg=C["panel"])
        sound.grid(row=1, column=1, sticky="ew", padx=(px(10), 0), pady=(px(10), 0))
        self.polish_var = tk.StringVar(value=self.settings["polish"])
        self.polish_select = ui.Segmented(
            sound, self.polish_var, polish.MODES, self.on_polish_mode, px=px,
            labels={m: m.title() for m in polish.MODES}, accent=C["gold"])
        self.polish_select.pack(side="left")
        self.polish_buttons = [self.polish_select]
        self.key_var = tk.StringVar(value=self._song_key_name())
        self.key_box = ttk.Combobox(sound, textvariable=self.key_var,
                                    values=tuple(polish.KEY_NAMES), width=12,
                                    state="readonly", takefocus=0)
        self.key_box.pack(side="right")
        self.key_box.bind("<<ComboboxSelected>>", self.on_key)
        ui.label(sound, "Key").pack(side="right", padx=(px(10), px(4)))

        # Where playback goes. Recording over a track always uses the headphones.
        ui.label(deck, "Play on  O").grid(row=2, column=0, sticky="w", pady=(px(6), 0))
        route = tk.Frame(deck, bg=C["panel"])
        route.grid(row=2, column=1, sticky="ew", padx=(px(10), 0), pady=(px(6), 0))
        self.route_var = tk.StringVar(value=self.settings["playback"])
        self.route_select = ui.Segmented(route, self.route_var, engine.ROUTES, self.on_route,
                                         labels={r: r.title() for r in engine.ROUTES}, px=px)
        self.route_select.pack(side="left")
        self.speakers_var = tk.StringVar()
        self.speakers_box = ttk.Combobox(route, textvariable=self.speakers_var, width=26,
                                         state="readonly", takefocus=0,
                                         postcommand=self._refresh_speakers)
        self.speakers_box.pack(side="right")
        self.speakers_box.bind("<<ComboboxSelected>>", self.on_speakers)
        ui.label(route, "Speakers").pack(side="right", padx=(px(10), px(4)))
        self._refresh_speakers()

        # ---- takes list ---------------------------------------------------------
        body.rowconfigure(5, weight=1)
        listing = tk.Frame(body, bg=C["panel"], padx=px(4), pady=px(4))
        listing.grid(row=5, column=0, sticky="nsew")
        head = tk.Frame(listing, bg=C["panel"])
        head.pack(side="top", fill="x", padx=px(8), pady=(px(2), 0))
        self.takes_title = ui.label(head, "TAKES", size=8, weight="bold")
        self.takes_title.pack(side="left")
        self.show_trash = tk.BooleanVar(value=False)
        ttk.Checkbutton(head, text="Show trash", variable=self.show_trash, takefocus=0,
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
                        ("<KeyPress-v>", self.cycle_polish),
                        ("<KeyPress-V>", self.cycle_polish),
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
            self.device_dot.config(fg=C["yellow"])
            self.device_label.config(
                text="Input paused during playback: the 26c can't run input and output at "
                     "different rates.\nSet Hz to auto to avoid this.", fg=C["yellow"])
            return
        if b is None:
            self.device_dot.config(fg=C["red"])
            self.device_label.config(text=self.engine.error or "No interface", fg=C["red"])
            return
        quality = ("bit-perfect" if b.bit_perfect
                   else "NOT bit-perfect -- Windows is processing the signal")
        auto = " (auto)" if self.engine.requested_rate == "auto" else ""
        self.device_dot.config(fg=C["green"] if b.bit_perfect else C["yellow"])
        self.device_label.config(
            text=f"{b.describe()}   \u00b7   {self.engine.samplerate} Hz{auto} / 24-bit WAV   "
                 f"\u00b7   {quality}\n"
                 f"Headphones: {self.engine.output_name()}   \u00b7   Takes: {self.song.dir}",
            fg=C["dim"] if b.bit_perfect else C["yellow"])

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
        self.verdict_label.config(text=text)
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
            return (f"POLISHING {job['mode'].upper()}  \u00b7  take {job['take'].num:03d}...",
                    "busy", None)
        if self._estimating_key is not None:
            return "ANALYZING TRACK KEY...", "busy", None
        if self.playing_num is not None and player is not None:
            mode = "" if self.playing_mode == "off" else f" {self.playing_mode.upper()}"
            return (f"\u25b6 PLAY{mode}  take {self.playing_num:03d}   "
                    f"{fmt_time(player.seconds)} / {fmt_time(player.length)}", "play",
                    player.seconds / player.length if player.length else None)
        if flash and flash[1] > 0:
            return flash[0], "flash", None
        if self.engine.backend is None:
            return "NO INTERFACE", "error", None
        return "READY  \u00b7  Space to record", "idle", None

    def _show_status(self, text, tone, progress):
        bg, fg = self.STATUS_TONES[tone]
        if self._status_shown != (text, tone):
            self._status_shown = (text, tone)
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
        self.verdict_label.config(bg=bg, fg=fg)

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
