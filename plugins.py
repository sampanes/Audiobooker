"""Optional plugin engines for Pitch and Tone playback.

The built-in DSP in polish.py always works. When a supported VST3 plugin is
installed (Auto-Tune, MAutoPitch, Graillon for pitch; Valhalla or Melda for the
room in Tone "Lots"), a render can use it instead.

Plugins never load inside the recorder's own process: render_isolated() runs
the whole render in a child Python process, so a plugin that crashes, hangs on
a license window or plays silence costs one render (the caller falls back to
the built-in code), never the app or a take in progress.

Which plugins work outside a DAW was measured by tools/probe_plugins.py
(plugin-probe/report.7z). The knob settings below are starting points: adjust
them by ear and bump RECIPE_VERSION so cached renders are redone.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import polish

RECIPE_VERSION = 1
RENDER_TIMEOUT_S = 300          # a hidden license window should not hang a render forever
LOAD_TIMEOUT_S = 60.0
AUTO, BUILTIN = "auto", "builtin"

VST3_DIRS = (Path(os.environ.get("CommonProgramFiles", r"C:\Program Files\Common Files"))
             / "VST3",
             Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Common" / "VST3")


class PluginFailed(RuntimeError):
    """A plugin render did not produce usable audio. .engine names the culprit."""

    def __init__(self, message, engine=None):
        super().__init__(message)
        self.engine = engine


@dataclass(frozen=True)
class Engine:
    id: str
    name: str            # shown in Settings and the status line
    kind: str            # "pitch" or "room"
    match: str           # file name, lowercase, letters and digits only
    settings: dict = field(default_factory=dict)   # goes into the cache hash
    hidden: bool = False  # test engines: never offered or auto-picked


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


ENGINES = {e.id: e for e in (
    # Pitch, in auto-pick order. All four were measured by the probe; Graillon's
    # probe run had its correction knob off, so it is last and least proven.
    Engine("auto-tune-access", "Auto-Tune Access", "pitch", "autotuneaccess",
           {"some": {"retune_speed": "Medium", "humanize": "Max"},
            "lots": {"retune_speed": "Fast", "humanize": "Off"}}),
    Engine("auto-tune-efx", "Auto-Tune EFX+", "pitch", "autotuneefx",
           {"some": {"retune_speed_ms": 40.0, "humanize": 40.0},
            "lots": {"retune_speed_ms": 0.0, "humanize": 0.0}}),
    Engine("mautopitch", "MAutoPitch", "pitch", "mautopitch",
           {"some": {"depth_automatic_tuning": 60.0, "speed_automatic_tuning": 40.0},
            "lots": {"depth_automatic_tuning": 100.0, "speed_automatic_tuning": 100.0}}),
    Engine("graillon", "Graillon 2", "pitch", "graillon",
           {"some": {"correction": 70.0, "pitch_smoothing_ms": 25.0, "inertia": 30.0},
            "lots": {"correction": 100.0, "pitch_smoothing_ms": 1.0, "inertia": 0.0}}),
    # Room (replaces the built-in echo in Tone "Lots"), in auto-pick order.
    # MCharmVerb first: the 11-03 probe rendered it in 0.8 s, against 11-19 s
    # for the Valhallas, and a light room on a voice gains little from them.
    Engine("mcharmverb", "MCharmVerb", "room", "mcharmverb",
           {"lots": {"dry_wet_globals": 12.0, "length_globals": 1000.0,
                     "predelay_globals": 10.0}}),
    Engine("valhalla-room", "ValhallaRoom", "room", "valhallaroom",
           {"lots": {"type": "Medium Room", "mix": 14.0, "predelay": 12.0, "decay": 1.0,
                     "hicut": 7000.0, "locut": 120.0}}),
    Engine("valhalla-vintageverb", "ValhallaVintageVerb", "room", "valhallavintageverb",
           {"lots": {"reverbmode": "Room", "colormode": "now", "mix": 12.0,
                     "predelay": 10.0, "decay": 1.0, "highcut": 7000.0,
                     "lowcut": 120.0}}),
    # Test engines built from pedalboard's own effects (no VST3 needed).
    Engine("test-shift", "test: up 1 semitone", "pitch", "", {"semitones": 1.0}, True),
    Engine("test-crash", "test: crash", "pitch", "", {}, True),
    Engine("test-hang", "test: hang", "pitch", "", {}, True),
    Engine("test-silent", "test: silence", "room", "", {}, True),
)}
KINDS = ("pitch", "room")


# ---- what is installed, and which engine a render uses ---------------------------------

def find_installed(dirs=VST3_DIRS):
    """{engine id: plugin path} for every supported plugin found in the VST3 folders."""
    found = {}
    for base in dirs:
        base = Path(base)
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            names = [d for d in dirnames if d.lower().endswith(".vst3")]
            dirnames[:] = [d for d in dirnames if not d.lower().endswith(".vst3")]
            names += [f for f in filenames if f.lower().endswith(".vst3")]
            for name in names:
                stem = _norm(name[:-5])
                for engine in ENGINES.values():
                    if engine.match and engine.id not in found and engine.match in stem:
                        found[engine.id] = str(Path(dirpath) / name)
    return found


def choices(kind, installed):
    """Engine ids offered in Settings for a kind, in auto-pick order."""
    return [e.id for e in ENGINES.values()
            if e.kind == kind and not e.hidden and e.id in installed]


def resolve(kind, wanted, installed, failed=()):
    """The engine id a render should use, or None for the built-in code.

    wanted is "auto", "builtin" or an engine id. A wanted plugin that is missing
    or failed this session falls back to the best other one.
    """
    if wanted == BUILTIN:
        return None
    engine = ENGINES.get(wanted)
    if engine is not None and engine.kind == kind and wanted not in failed and (
            wanted in installed or engine.hidden):
        return wanted
    for engine_id in choices(kind, installed):
        if engine_id not in failed:
            return engine_id
    return None


def active_engines(mode, pitch_engine=None, room_engine=None):
    """The engines that actually change a render of mode (others are dropped)."""
    pitch, tone = polish.split_mode(mode)
    used = {}
    if pitch_engine and pitch != "none":
        used["pitch"] = pitch_engine
    if room_engine and tone == "lots":
        used["room"] = room_engine
    return used


def recipe(engines):
    """What goes into the cache hash for the engines in use."""
    return {kind: {"id": engine_id, "v": RECIPE_VERSION,
                   "settings": ENGINES[engine_id].settings}
            for kind, engine_id in sorted(engines.items())}


def short_name(engine_id):
    return ENGINES[engine_id].name if engine_id in ENGINES else str(engine_id)


# ---- knob settings per engine --------------------------------------------------------------

_ACCESS_KEYS = ("C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
_EFX_KEYS = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_NOTE_KEYS = ("c", "c_sharp", "d", "d_sharp", "e", "f", "f_sharp", "g", "g_sharp", "a",
              "a_sharp", "b")
_EFX_NOTE_KEYS = ("note_c", "note_c_sharp_db", "note_d", "note_d_sharp_eb", "note_e",
                  "note_f", "note_f_sharp_gb", "note_g", "note_g_sharp_ab", "note_a",
                  "note_a_sharp_bb", "note_b")


def allowed_notes(key):
    """Pitch classes (C=0) a corrector may land on for a key; all 12 for Chromatic."""
    spec = polish.parse_key(key)
    if spec is None:
        return set(range(12))
    return {(spec.root + step) % 12 for step in polish._SCALE_STEPS[spec.scale]}


def plugin_values(engine_id, level, key):
    """Ordered (parameter, value) pairs to set on the plugin for a level and key.

    level is the pitch level for pitch engines and the tone level for rooms.
    """
    engine = ENGINES[engine_id]
    spec = polish.parse_key(key)
    notes = allowed_notes(spec)
    values = []
    if engine_id == "auto-tune-access":
        values += [("master_bypass", False),
                   ("key", _ACCESS_KEYS[spec.root] if spec else "C"),
                   ("scale", spec.scale.title() if spec else "Chromatic")]
        values += [(k, i in notes) for i, k in enumerate(_NOTE_KEYS)]
    elif engine_id == "auto-tune-efx":
        values += [("master_bypass", False), ("auto_tune_bypass", False),
                   ("dry_wet_mix", 100.0), ("fx1", "No Effect"), ("fx2", "No Effect"),
                   ("fx3", "No Effect"), ("fx4", "No Effect"), ("breath_mix", 0.0),
                   ("duet_enable", False), ("p_t_shift_st", 0.0), ("motion_trigger", False),
                   ("auto_tune_key", _EFX_KEYS[spec.root] if spec else "C"),
                   ("auto_tune_scale", spec.scale.title() if spec else "Chromatic")]
        values += [(k, i in notes) for i, k in enumerate(_EFX_NOTE_KEYS)]
    elif engine_id == "mautopitch":
        values += [("keep_formants_effects", 100.0)]
        values += [(k, i in notes) for i, k in enumerate(_NOTE_KEYS)]
    elif engine_id == "graillon":
        values += [("bypass", False), ("dry_wet", 100.0), ("bitdepth", 0.0),
                   ("quantize", 0.0), ("pitch_shift_st", 0.0)]
        values += [(f"allow_{k}", i in notes) for i, k in enumerate(_NOTE_KEYS)]
    values += list(engine.settings.get(level, {}).items())
    return values


def nearest_choice(options, value):
    """For a knob that only takes text ("1.00 s "), the option nearest a number."""
    def number(text):
        found = re.search(r"-?\d+(?:\.\d+)?", str(text))
        return float(found.group()) if found else None
    scored = [(abs(number(o) - value), o) for o in options if number(o) is not None]
    return min(scored)[1] if scored else None


# ---- the child process --------------------------------------------------------------------

def _load(engine_id, path):
    import pedalboard
    engine = ENGINES[engine_id]
    if engine_id == "test-shift":
        return pedalboard.PitchShift(semitones=engine.settings["semitones"])
    if engine_id == "test-crash":
        os._exit(3)
    if engine_id == "test-hang":
        time.sleep(3600)
    if engine_id == "test-silent":
        return pedalboard.Gain(gain_db=-200.0)
    try:
        return pedalboard.load_plugin(path, initialization_timeout=LOAD_TIMEOUT_S)
    except Exception:
        # A file holding several plugins needs a name: take the one that matches.
        names = pedalboard.VST3Plugin.get_plugin_names_for_file(path)
        pick = next((n for n in names if engine.match in _norm(n)), names[0] if names else None)
        if pick is None:
            raise
        return pedalboard.load_plugin(path, plugin_name=pick,
                                      initialization_timeout=LOAD_TIMEOUT_S)


def _configure(plugin, engine_id, level, key, log):
    if ENGINES[engine_id].hidden:
        return
    params = plugin.parameters
    for name, value in plugin_values(engine_id, level, key):
        param = params.get(name)
        if param is None:
            log.append(f"{engine_id}: no parameter {name!r}")
            continue
        try:
            options = list(getattr(param, "valid_values", None) or [])
            if (isinstance(value, (int, float)) and not isinstance(value, bool) and options
                    and all(isinstance(o, str) for o in options)):
                value = nearest_choice(options, value)
            setattr(plugin, name, value)
        except Exception as exc:
            log.append(f"{engine_id}: could not set {name} = {value!r} ({exc})")


def _run_plugin(plugin, data, rate):
    """data is (frames, channels); pedalboard wants (channels, frames)."""
    block = np.ascontiguousarray(data.T, dtype=np.float32)
    try:
        wet = plugin.process(block, rate, reset=True)
    except Exception:  # some plugins take mono only
        wet = plugin.process(block[:1], rate, reset=True)
    wet = np.asarray(wet, dtype=np.float32)
    if wet.shape[0] != block.shape[0]:
        wet = np.repeat(wet[:1], block.shape[0], axis=0)
    if wet.shape[1] < block.shape[1]:
        wet = np.pad(wet, ((0, 0), (0, block.shape[1] - wet.shape[1])))
    return wet[:, :block.shape[1]].T.astype(np.float64)


def _check_output(engine_id, dry, wet):
    if not np.isfinite(wet).all():
        raise PluginFailed(f"{short_name(engine_id)} produced invalid audio (NaN)", engine_id)
    if np.max(np.abs(dry)) > 1e-4 and np.max(np.abs(wet)) < 1e-6:
        raise PluginFailed(f"{short_name(engine_id)} played silence (license or demo mode?)",
                           engine_id)


def worker_main(job_path):
    """Render a job in this (child) process. Writes out.npy or error.json."""
    job_dir = Path(job_path).parent
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    log = []
    stage = {"engine": None}
    try:
        data = np.load(job_dir / "in.npy")
        rate = int(job["rate"])
        loaded = {}
        for kind, engine_id in job["engines"].items():
            stage["engine"] = engine_id
            plugin = _load(engine_id, job["paths"].get(engine_id))
            level = polish.split_mode(job["mode"])[0 if kind == "pitch" else 1]
            _configure(plugin, engine_id, level, job["key"], log)
            loaded[kind] = plugin
        stage["engine"] = None

        def stage_fn(kind):
            def run(audio):
                stage["engine"] = job["engines"][kind]
                wet = _run_plugin(loaded[kind], audio, rate)
                _check_output(stage["engine"], audio, wet)
                stage["engine"] = None
                return wet
            return run

        out = polish.render(data, rate, job["mode"], job["key"],
                            stages={kind: stage_fn(kind) for kind in loaded})
        np.save(job_dir / "out.npy", out)
        (job_dir / "log.json").write_text(json.dumps(log), encoding="utf-8")
        return 0
    except Exception as exc:
        engine_id = getattr(exc, "engine", None) or stage["engine"]
        message = str(exc) if isinstance(exc, PluginFailed) else (
            f"{short_name(engine_id) if engine_id else 'Plugin render'} failed: "
            f"{type(exc).__name__}: {exc}")
        (job_dir / "error.json").write_text(json.dumps(
            {"engine": engine_id, "error": message, "log": log}), encoding="utf-8")
        return 1


# ---- the parent side ------------------------------------------------------------------------

def render_isolated(data, rate, mode, key, engines, installed, cancel=None,
                    timeout=RENDER_TIMEOUT_S):
    """polish.render() with plugin engines, run in a child process.

    Raises PluginFailed (fall back to the built-in render) or
    polish.RenderCancelled (cancel() turned true; the child is killed).
    """
    mode = polish.normalize_mode(mode)
    spec = polish.parse_key(key)
    first = next(iter(engines.values()), None)
    job_dir = Path(tempfile.mkdtemp(prefix="take-recorder-plugin-"))
    try:
        np.save(job_dir / "in.npy", np.asarray(data, dtype=np.float32))
        job = {"rate": int(rate), "mode": mode, "key": spec.name if spec else None,
               "engines": engines, "paths": {e: installed.get(e) for e in engines.values()}}
        (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with open(job_dir / "child.log", "wb") as child_log:
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker",
                 str(job_dir / "job.json")],
                stdin=subprocess.DEVNULL, stdout=child_log, stderr=subprocess.STDOUT,
                cwd=str(Path(__file__).resolve().parent), creationflags=flags)
            started = time.monotonic()
            try:
                while child.poll() is None:
                    if cancel is not None and cancel():
                        raise polish.RenderCancelled("audio processing cancelled")
                    if time.monotonic() - started > timeout:
                        raise PluginFailed(f"{short_name(first)} gave no answer after "
                                           f"{timeout:.0f} s (a license window?)", first)
                    time.sleep(0.05)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
        error_path = job_dir / "error.json"
        if error_path.exists():
            info = json.loads(error_path.read_text(encoding="utf-8"))
            engine_id = info.get("engine") or first
            raise PluginFailed(info.get("error") or f"{short_name(engine_id)} failed",
                               engine_id)
        if child.returncode != 0 or not (job_dir / "out.npy").exists():
            tail = (job_dir / "child.log").read_text(encoding="utf-8", errors="replace")
            tail = " | ".join(tail.strip().splitlines()[-2:])
            raise PluginFailed(f"{short_name(first)} crashed (exit {child.returncode})"
                               + (f": {tail}" if tail else ""), first)
        return np.load(job_dir / "out.npy")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        sys.exit(worker_main(sys.argv[2]))
    print(json.dumps(find_installed(), indent=1))
