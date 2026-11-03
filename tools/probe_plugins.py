"""Can this PC's vocal plugins run outside a DAW? Load each one, push a test
voice through it, and say what came out.

Run probe-plugins.bat (double-click), or:
    .venv\\Scripts\\python.exe tools\\probe_plugins.py [--all] [--selftest]

Each plugin loads in its own process, so a crash or a hung license window
only loses that one plugin. If a license or activation window appears, deal
with it (or close it); the probe waits up to 10 minutes per plugin.

Writes plugin-probe/report-<date>.md (commit it so other machines can read it)
and the processed audio to plugin-probe/out/ (listen to judge quality and to
hear demo-mode noise or silence).
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import polish  # noqa: E402  (pitch tracker for measuring results)

OUT_DIR = ROOT / "plugin-probe"
RATE = 48000
TARGET_HZ = 220.0                       # A3
SHARP_CENTS = 40.0                      # the test voice sings A3 this sharp
PER_PLUGIN_TIMEOUT_S = 600

# Plugins worth knowing about for this app (substrings of the file name)
WANTED = ("auto-tune", "autotune", "graillon", "mautopitch", "mcompressor", "mequalizer",
          "msaturator", "mcharmverb", "valhalla", "doubler", "vocalsynth", "neutron",
          "mtuner")
PITCH_WORDS = ("auto-tune", "autotune", "graillon", "mautopitch")
VST3_DIRS = (Path(os.environ.get("CommonProgramFiles", r"C:\Program Files\Common Files"))
             / "VST3",
             Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Common" / "VST3")


# ---- test voice and measurements -----------------------------------------------

def test_voice(seconds=4.0):
    """A sung 'ah' on A3, 40 cents sharp: harmonics shaped by three vowel formants."""
    t = np.arange(int(seconds * RATE)) / RATE
    f0 = TARGET_HZ * 2.0 ** (SHARP_CENTS / 1200.0)
    f0_curve = f0 * (1.0 + 0.002 * np.sin(2 * np.pi * 0.7 * t))  # a little drift
    phase = 2 * np.pi * np.cumsum(f0_curve) / RATE
    voice = np.zeros_like(t)
    for n in range(1, 40):
        hz = n * f0
        if hz > RATE / 2.2:
            break
        formants = sum(g * np.exp(-0.5 * ((hz - c) / w) ** 2)
                       for c, w, g in ((700, 130, 1.0), (1220, 160, 0.6), (2600, 260, 0.3)))
        voice += (0.15 + formants) / n * np.sin(n * phase)
    edge = int(0.05 * RATE)
    env = np.ones_like(t)
    env[:edge] = np.linspace(0, 1, edge)
    env[-edge:] = np.linspace(1, 0, edge)
    voice *= env
    voice *= 10 ** (-12 / 20) / np.max(np.abs(voice))
    return np.stack([voice, voice]).astype(np.float32)  # (channels, samples)


def db(x):
    return float(20 * np.log10(max(float(x), 1e-12)))


def pitch_cents(audio):
    """Median pitch of the middle of the clip, in cents from A3; None if unvoiced."""
    mono = np.asarray(audio, dtype=np.float64).mean(axis=0)
    middle = mono[len(mono) // 4: 3 * len(mono) // 4]
    _, pitches, _ = polish._track_pitch(middle, RATE)
    voiced = pitches[np.isfinite(pitches)]
    if len(voiced) < 10:
        return None
    hz = float(np.median(voiced))
    cents = 1200 * np.log2(hz / TARGET_HZ)
    return cents - 1200 * round(cents / 1200)  # fold octave jumps


def measure(dry, wet):
    n = min(dry.shape[1], wet.shape[1])
    wet_mono = wet[:, :n].mean(axis=0)
    dry_mono = dry[:, :n].mean(axis=0)
    win = RATE // 4
    active = [i for i in range(0, n - win, win)
              if np.sqrt(np.mean(dry_mono[i:i + win] ** 2)) > 1e-3]
    gaps = sum(1 for i in active if np.sqrt(np.mean(wet_mono[i:i + win] ** 2)) < 1e-4)
    return {
        "out_rms_db": round(db(np.sqrt(np.mean(wet_mono ** 2))), 1),
        "in_rms_db": round(db(np.sqrt(np.mean(dry_mono ** 2))), 1),
        "unchanged": bool(np.max(np.abs(wet[:, :n] - dry[:, :n])) < 1e-4),
        "silent": bool(np.max(np.abs(wet)) < 1e-5),
        "gaps": gaps,
        "pitch_cents": (lambda c: None if c is None else round(c, 1))(pitch_cents(wet)),
        "finite": bool(np.isfinite(wet).all()),
    }


# ---- one plugin (runs in a child process) -------------------------------------------

def describe_params(plugin):
    rows = []
    for key, p in plugin.parameters.items():
        row = {"key": key, "name": getattr(p, "name", key)}
        for attr in ("string_value", "raw_value", "units", "valid_values", "range"):
            try:
                value = getattr(p, attr)
            except Exception:
                continue
            if attr == "valid_values" and value is not None and len(value) > 40:
                value = list(value[:40]) + ["..."]
            row[attr] = value if isinstance(value, (int, float, str, list, type(None))) \
                else repr(value)
        rows.append(row)
    return rows


def set_raw(plugin, words, raw, changed):
    """Set every parameter whose name has one of words to a 0..1 raw value."""
    for key, p in plugin.parameters.items():
        name = str(getattr(p, "name", key)).lower()
        if any(w in name for w in words):
            try:
                p.raw_value = raw
                changed.append(f"{getattr(p, 'name', key)} = {p.string_value}")
            except Exception as exc:
                changed.append(f"{getattr(p, 'name', key)}: could not set ({exc})")


def set_choice(plugin, words, choice, changed):
    for key, p in plugin.parameters.items():
        name = str(getattr(p, "name", key)).lower()
        values = getattr(p, "valid_values", None) or []
        if any(w in name for w in words) and choice in values:
            try:
                setattr(plugin, key, choice)
                changed.append(f"{getattr(p, 'name', key)} = {choice}")
            except Exception as exc:
                changed.append(f"{getattr(p, 'name', key)}: could not set ({exc})")


def run_variant(plugin, dry, label, out_stem):
    plugin.reset()
    started = time.perf_counter()
    try:
        wet = plugin.process(dry, RATE, reset=True)
    except Exception:  # some plugins only take mono
        wet = plugin.process(dry[:1], RATE, reset=True)
        wet = np.repeat(wet, 2, axis=0) if wet.shape[0] == 1 else wet
    seconds = time.perf_counter() - started
    path = OUT_DIR / "out" / f"{out_stem}.{label}.wav"
    sf.write(str(path), wet.T, RATE, subtype="PCM_24")
    result = measure(dry, wet)
    result.update(variant=label, seconds=round(seconds, 2), wav=str(path.relative_to(ROOT)))
    return result


def probe_one(path, sub_name, selftest=None):
    import pedalboard
    report = {"path": path, "plugin_name": sub_name}
    if selftest is not None:
        plugin = selftest
        report["name"] = type(plugin).__name__
    else:
        plugin = pedalboard.load_plugin(path, plugin_name=sub_name, initialization_timeout=60)
        report["name"] = plugin.name
    for attr in ("manufacturer_name", "version", "category", "is_instrument",
                 "reported_latency_samples"):
        try:
            report[attr] = getattr(plugin, attr)
        except Exception:
            pass
    report["params"] = describe_params(plugin) if selftest is None else []

    dry = test_voice()
    stem = "".join(c if c.isalnum() else "-" for c in report["name"]).strip("-").lower()
    report["variants"] = [run_variant(plugin, dry, "default", stem)]
    is_pitch = any(w in (report["name"] + " " + path).lower() for w in PITCH_WORDS)
    report["kind"] = "pitch" if is_pitch else "tone"
    if is_pitch and selftest is None:
        # Ask for hard correction. Speed knobs run either way round depending on the
        # vendor, so try both ends and let the measurement say which one corrects.
        for end, raw in (("speed-min", 0.0), ("speed-max", 1.0)):
            changed = []
            set_choice(plugin, ("scale",), "Chromatic", changed)
            set_raw(plugin, ("retune", "speed"), raw, changed)
            set_raw(plugin, ("amount", "depth", "correction", "strength"), 1.0, changed)
            set_raw(plugin, ("mix", "dry/wet", "wet"), 1.0, changed)
            variant = run_variant(plugin, dry, end, stem)
            variant["changed"] = changed
            report["variants"].append(variant)
    return report


# ---- discovery and the parent run ----------------------------------------------------

def find_vst3():
    found = set()
    try:
        import pedalboard
        found.update(str(p) for p in pedalboard.VST3Plugin.installed_plugins)
    except Exception:
        pass
    for base in VST3_DIRS:
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            for d in list(dirnames):
                if d.lower().endswith(".vst3"):  # a bundle: the plugin, don't descend
                    found.add(str(Path(dirpath) / d))
                    dirnames.remove(d)
            for f in filenames:
                if f.lower().endswith(".vst3"):
                    found.add(str(Path(dirpath) / f))
    return sorted(found, key=lambda p: Path(p).name.lower())


def wanted(path):
    name = Path(path).name.lower().replace(" ", "")
    return any(w.replace(" ", "") in name for w in WANTED)


def run_child(path, sub_name):
    cmd = [sys.executable, __file__, "--one", path]
    if sub_name:
        cmd += ["--plugin-name", sub_name]
    started = time.time()
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=PER_PLUGIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"path": path, "plugin_name": sub_name, "error":
                f"no answer after {PER_PLUGIN_TIMEOUT_S} s (a hidden license window?)"}
    for line in reversed(done.stdout.splitlines()):
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    tail = (done.stderr or done.stdout).strip().splitlines()[-3:]
    return {"path": path, "plugin_name": sub_name,
            "error": f"crashed after {time.time() - started:.0f} s (exit {done.returncode}): "
                     + " | ".join(tail)}


def verdict(r):
    if "error" in r:
        return "FAILED", r["error"]
    default = r["variants"][0]
    if default["silent"]:
        return "SILENT", "loaded but output silence (unlicensed, demo, or needs MIDI?)"
    if not default["finite"]:
        return "BROKEN", "output has NaN/inf"
    notes = []
    if default["gaps"]:
        notes.append(f"{default['gaps']} silent gaps (demo mode?)")
    if r["kind"] == "pitch":
        best = min((v for v in r["variants"] if v["pitch_cents"] is not None),
                   key=lambda v: abs(v["pitch_cents"]), default=None)
        if best is None:
            notes.append("no pitch measured in the output")
        else:
            notes.append(f"pitch +{SHARP_CENTS:.0f} -> {best['pitch_cents']:+.1f} cents "
                         f"({best['variant']})")
            if abs(best["pitch_cents"]) < 10:
                return "WORKS", "; ".join(notes)
            return "LOADS", "; ".join(notes + ["did not correct the test note"])
    if default["unchanged"]:
        return "LOADS", "; ".join(notes + ["output identical to input (bypassed?)"])
    return "WORKS", "; ".join(notes + [f"output {default['out_rms_db']} dB rms "
                                       f"(in {default['in_rms_db']})"])


def write_report(results, everything, probed):
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"report-{datetime.date.today().isoformat()}.md"
    lines = [f"# Plugin probe {datetime.datetime.now():%Y-%m-%d %H:%M}", "",
             f"pedalboard {__import__('pedalboard').__version__}, Python "
             f"{sys.version.split()[0]}. Test voice: A3 sung {SHARP_CENTS:.0f} cents sharp, "
             f"{RATE} Hz.", "",
             "| Plugin | Verdict | What happened |", "|---|---|---|"]
    for r in results:
        v, why = verdict(r)
        name = r.get("name") or Path(r["path"]).stem
        lines.append(f"| {name} | **{v}** | {why} |")
    lines += ["", f"## All VST3 plugins found ({len(everything)})", ""]
    lines += [f"- {'(probed) ' if p in probed else ''}`{p}`" for p in everything]
    lines += ["", "## Details", ""]
    for r in results:
        lines += [f"### {r.get('name') or Path(r['path']).stem}", "",
                  "```json", json.dumps({k: v for k, v in r.items() if k != "params"},
                                        indent=1, default=str), "```", ""]
        if r.get("params"):
            lines += ["<details><summary>Parameters</summary>", "", "```json",
                      json.dumps(r["params"], indent=1, default=str), "```", "</details>", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def selftest():
    """Check the measuring code with pedalboard's own effects (no VST3 needed)."""
    import pedalboard
    fixes = pedalboard.PitchShift(semitones=-SHARP_CENTS / 100)  # a perfect 'corrector'
    tone = pedalboard.Reverb(room_size=0.4, wet_level=0.3)
    results = []
    for plugin, kind in ((fixes, "pitch"), (tone, "tone")):
        r = probe_one("(built-in)", None, selftest=plugin)
        r["kind"] = kind
        results.append(r)
        print(f"  {r['name']:<28} {' '.join(verdict(r))}")
    dry_cents = pitch_cents(test_voice())
    print(f"  test voice measures {dry_cents:+.1f} cents (expected +{SHARP_CENTS:.0f})")
    ok = (abs(dry_cents - SHARP_CENTS) < 3 and verdict(results[0])[0] == "WORKS"
          and verdict(results[1])[0] == "WORKS")
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="probe every VST3, not just vocal ones")
    ap.add_argument("--selftest", action="store_true", help="check the probe itself")
    ap.add_argument("--one", help=argparse.SUPPRESS)
    ap.add_argument("--plugin-name", help=argparse.SUPPRESS)
    args = ap.parse_args()
    (OUT_DIR / "out").mkdir(parents=True, exist_ok=True)

    if args.selftest:
        return selftest()
    if args.one:
        result = probe_one(args.one, args.plugin_name)
        print("RESULT " + json.dumps(result, default=str))
        return 0

    everything = find_vst3()
    targets = everything if args.all else [p for p in everything if wanted(p)]
    print(f"Found {len(everything)} VST3 plugins; probing {len(targets)}.")
    if not targets:
        print("Nothing to probe. Looked in: " + ", ".join(str(d) for d in VST3_DIRS))
    import pedalboard
    results = []
    for path in targets:
        try:
            names = pedalboard.VST3Plugin.get_plugin_names_for_file(path)
        except Exception:
            names = [None]
        for sub in (names if len(names) > 1 else [None]):
            label = Path(path).stem + (f" / {sub}" if sub else "")
            print(f"- {label}: loading (answer any license window that opens)...", flush=True)
            r = run_child(path, sub)
            results.append(r)
            v, why = verdict(r)
            print(f"    {v}: {why}", flush=True)
    report = write_report(results, everything, set(targets))
    print(f"\nReport: {report}")
    print(f"Audio:  {OUT_DIR / 'out'}  (listen for quality, noise bursts, silence)")
    print("To get the results to the other PC: git add plugin-probe/report-*.md, "
          "commit, push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
