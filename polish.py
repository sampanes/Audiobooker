"""Offline pitch correction and light vocal polish for recorded takes.

The functions in this module have no audio-device or UI dependencies. Audio is
frame-major float32 data, either ``(frames,)`` or ``(frames, channels)``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np


MODES = ("off", "gentle", "strong")
NOTE_NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
KEY_NAMES = ("Chromatic",) + tuple(
    f"{name} {scale}" for name in NOTE_NAMES for scale in ("major", "minor")
)

CHAIN_VERSION = 2
MIN_F0 = 65.0
MAX_F0 = 1000.0
FRAME_SECONDS = 0.050
HOP_SECONDS = 0.010
LIMIT = 10.0 ** (-1.0 / 20.0)
EQ_BLOCK_SECONDS = 0.25
EQ_MIN_BLOCK = 4096
EQ_MAX_BLOCK = 32768

_SCALE_STEPS = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
}
_ROOT_ALIASES = {
    "c": 0, "b#": 0,
    "c#": 1, "db": 1,
    "d": 2,
    "d#": 3, "eb": 3,
    "e": 4, "fb": 4,
    "f": 5, "e#": 5,
    "f#": 6, "gb": 6,
    "g": 7,
    "g#": 8, "ab": 8,
    "a": 9,
    "a#": 10, "bb": 10,
    "b": 11, "cb": 11,
}

_RECIPES = {
    "gentle": {
        "pitch_pull": 0.50,
        "error_smooth_frames": 11,
        "highpass_hz": 85.0,
        "presence_db": 1.5,
        "compress_threshold_db": -17.0,
        "compress_ratio": 2.2,
        "reverb_mix": 0.0,
    },
    "strong": {
        "pitch_pull": 1.0,
        "error_smooth_frames": 1,
        "highpass_hz": 95.0,
        "presence_db": 2.5,
        "compress_threshold_db": -20.0,
        "compress_ratio": 3.5,
        "reverb_mix": 0.055,
    },
}


class RenderCancelled(RuntimeError):
    """Raised when a caller cooperatively cancels offline DSP work."""


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise RenderCancelled("audio processing cancelled")


@dataclass(frozen=True)
class KeySpec:
    """A root pitch class (C=0) and a major or minor scale."""

    root: int
    scale: str

    def __post_init__(self):
        if not 0 <= self.root < 12:
            raise ValueError("key root must be in the range 0..11")
        if self.scale not in _SCALE_STEPS:
            raise ValueError("key scale must be 'major' or 'minor'")

    @property
    def name(self):
        return f"{NOTE_NAMES[self.root]} {self.scale}"


def normalize_mode(value):
    """Return a canonical playback mode or raise ValueError."""
    mode = "off" if value is None else str(value).strip().lower()
    if mode not in MODES:
        raise ValueError(f"unknown polish mode: {value!r}")
    return mode


def parse_key(value):
    """Parse a display key. None and Chromatic both mean unrestricted notes."""
    if value is None or isinstance(value, KeySpec):
        return value
    text = " ".join(str(value).strip().split())
    if not text or text.lower() in ("chromatic", "none"):
        return None
    parts = text.lower().split(" ")
    if len(parts) != 2 or parts[0] not in _ROOT_ALIASES or parts[1] not in _SCALE_STEPS:
        raise ValueError(f"unknown key: {value!r}")
    return KeySpec(_ROOT_ALIASES[parts[0]], parts[1])


def snap_midi(midi, key=None):
    """Snap MIDI-note values to the nearest chromatic or in-key note."""
    spec = parse_key(key)
    values = np.asarray(midi, dtype=np.float64)
    flat = values.reshape(-1)
    out = np.empty_like(flat)
    if spec is None:
        out[:] = np.rint(flat)
    else:
        allowed = {(spec.root + step) % 12 for step in _SCALE_STEPS[spec.scale]}
        for i, value in enumerate(flat):
            center = int(round(float(value)))
            candidates = [note for note in range(center - 7, center + 8)
                          if note % 12 in allowed]
            out[i] = min(candidates, key=lambda note: (abs(note - value), note))
    result = out.reshape(values.shape)
    return float(result) if values.ndim == 0 else result


def recipe_hash(mode, key=None):
    """Return a stable cache key for every setting that changes a render."""
    mode = normalize_mode(mode)
    spec = parse_key(key)
    payload = {
        "chain": CHAIN_VERSION,
        "key": spec.name if spec is not None else "Chromatic",
        "mode": mode,
        "recipe": _RECIPES.get(mode, {}),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _analysis_channel(data):
    powers = np.mean(np.asarray(data, dtype=np.float64) ** 2, axis=0)
    return np.asarray(data[:, int(np.argmax(powers))], dtype=np.float64)


def _next_power_of_two(value):
    return 1 << max(1, int(value - 1).bit_length())


def _pitch_frame(frame, rate, min_lag, max_lag):
    frame = np.asarray(frame, dtype=np.float64)
    frame = frame - np.mean(frame)
    frame *= np.hanning(len(frame))
    if np.sqrt(np.mean(frame * frame)) < 1e-7:
        return np.nan, 0.0

    nfft = _next_power_of_two(2 * len(frame))
    spectrum = np.fft.rfft(frame, n=nfft)
    corr = np.fft.irfft(spectrum * spectrum.conjugate(), n=nfft).real[:max_lag + 2]
    squared = frame * frame
    cumulative = np.concatenate(([0.0], np.cumsum(squared)))
    lags = np.arange(min_lag, max_lag + 1)
    left_energy = cumulative[len(frame) - lags]
    right_energy = cumulative[len(frame)] - cumulative[lags]
    denom = np.sqrt(np.maximum(left_energy * right_energy, 1e-30))
    scores = corr[lags] / denom
    if len(scores) < 3:
        return np.nan, 0.0

    local = np.flatnonzero((scores[1:-1] > scores[:-2]) &
                           (scores[1:-1] >= scores[2:])) + 1
    if not len(local):
        best_index = int(np.argmax(scores))
    else:
        peak_score = float(np.max(scores[local]))
        strong = local[scores[local] >= max(0.55, peak_score * 0.94)]
        best_index = int(strong[0] if len(strong) else local[np.argmax(scores[local])])
    quality = float(scores[best_index])
    if quality < 0.52:
        return np.nan, quality

    lag = float(lags[best_index])
    if 0 < best_index < len(scores) - 1:
        y0, y1, y2 = scores[best_index - 1:best_index + 2]
        divisor = y0 - 2.0 * y1 + y2
        if abs(divisor) > 1e-12:
            lag += float(np.clip(0.5 * (y0 - y2) / divisor, -0.5, 0.5))
    return rate / lag, quality


def _median_track(values, width=5):
    result = values.copy()
    radius = width // 2
    for i, value in enumerate(values):
        if not np.isfinite(value):
            continue
        nearby = values[max(0, i - radius):i + radius + 1]
        nearby = nearby[np.isfinite(nearby)]
        if len(nearby):
            result[i] = np.median(nearby)
    return result


def _track_pitch(signal, rate, cancel=None):
    n = len(signal)
    window = max(64, int(round(FRAME_SECONDS * rate)))
    hop = max(1, int(round(HOP_SECONDS * rate)))
    if n < window:
        padded = np.pad(signal, (0, window - n))
        starts = np.array([0], dtype=int)
    else:
        padded = signal
        starts = np.arange(0, n - window + 1, hop, dtype=int)
        if starts[-1] != n - window:
            starts = np.append(starts, n - window)

    min_lag = max(2, int(rate / MAX_F0))
    max_lag = min(window - 3, int(np.ceil(rate / MIN_F0)))
    global_rms = float(np.sqrt(np.mean(np.asarray(signal, dtype=np.float64) ** 2)))
    floor = max(2e-6, global_rms * 0.025)
    pitches = np.full(len(starts), np.nan, dtype=np.float64)
    qualities = np.zeros(len(starts), dtype=np.float64)
    for i, start in enumerate(starts):
        _check_cancel(cancel)
        frame = padded[start:start + window]
        if np.sqrt(np.mean(np.asarray(frame, dtype=np.float64) ** 2)) < floor:
            continue
        pitches[i], qualities[i] = _pitch_frame(frame, rate, min_lag, max_lag)
    pitches = _median_track(pitches)
    centers = np.minimum(starts + window // 2, max(n - 1, 0)).astype(np.float64)
    return centers, pitches, qualities


def _moving_average(values, width):
    width = max(1, int(width))
    if width <= 1 or len(values) <= 1:
        return values.copy()
    if width % 2 == 0:
        width += 1
    radius = width // 2
    padded = np.pad(values, (radius, radius), mode="edge")
    total = np.concatenate(([0.0], np.cumsum(padded, dtype=np.float64)))
    return (total[width:] - total[:-width]) / width


def _fill_track(values):
    good = np.flatnonzero(np.isfinite(values))
    if not len(good):
        return None
    indexes = np.arange(len(values))
    return np.interp(indexes, good, values[good])


def _pitch_curves(centers, pitches, mode, key, length, rate):
    filled = _fill_track(pitches)
    if filled is None:
        return None
    midi = 69.0 + 12.0 * np.log2(filled / 440.0)
    target = snap_midi(midi, key)
    error = target - midi
    params = _RECIPES[mode]
    if params["error_smooth_frames"] > 1:
        error = _moving_average(error, params["error_smooth_frames"])
    corrected_midi = midi + params["pitch_pull"] * error
    corrected = 440.0 * (2.0 ** ((corrected_midi - 69.0) / 12.0))

    samples = np.arange(length, dtype=np.float64)
    original_curve = np.interp(samples, centers, filled)
    corrected_curve = np.interp(samples, centers, corrected)
    voiced = np.interp(samples, centers, np.isfinite(pitches).astype(np.float64))
    fade = max(1, int(round(0.006 * rate)))
    fade = min(fade, max(1, length // 8))
    if fade > 1:
        voiced = _moving_average(voiced, 2 * fade + 1)
    return original_curve, corrected_curve, np.clip(voiced, 0.0, 1.0)


def _snap_peak(signal, predicted, radius, polarity):
    lo = max(0, int(round(predicted)) - radius)
    hi = min(len(signal), int(round(predicted)) + radius + 1)
    if hi <= lo:
        return int(np.clip(round(predicted), 0, len(signal) - 1))
    part = signal[lo:hi]
    return lo + int(np.argmax(part) if polarity > 0 else np.argmin(part))


def _pitch_marks(signal, f0, rate, cancel=None):
    if len(signal) < 3:
        return np.arange(len(signal), dtype=int)
    middle = len(signal) // 2
    period = float(rate / np.clip(f0[middle], MIN_F0, MAX_F0))
    radius = max(1, int(round(0.45 * period)))
    lo, hi = max(0, middle - radius), min(len(signal), middle + radius + 1)
    part = signal[lo:hi]
    positive = float(np.max(part))
    negative = float(-np.min(part))
    polarity = 1 if positive >= negative else -1
    seed = lo + int(np.argmax(part) if polarity > 0 else np.argmin(part))

    forward = [seed]
    mark = seed
    while True:
        _check_cancel(cancel)
        period = float(rate / np.clip(f0[mark], MIN_F0, MAX_F0))
        predicted = mark + period
        if predicted >= len(signal):
            break
        candidate = _snap_peak(signal, predicted, max(1, int(0.28 * period)), polarity)
        if candidate <= mark:
            candidate = min(len(signal) - 1, mark + max(1, int(round(period))))
        if candidate <= mark:
            break
        forward.append(candidate)
        mark = candidate

    backward = []
    mark = seed
    while True:
        _check_cancel(cancel)
        period = float(rate / np.clip(f0[mark], MIN_F0, MAX_F0))
        predicted = mark - period
        if predicted < 0:
            break
        candidate = _snap_peak(signal, predicted, max(1, int(0.28 * period)), polarity)
        if candidate >= mark:
            candidate = max(0, mark - max(1, int(round(period))))
        if candidate >= mark:
            break
        backward.append(candidate)
        mark = candidate
    return np.asarray(backward[::-1] + forward, dtype=int)


def _td_psola(data, analysis, original_f0, corrected_f0, rate, cancel=None):
    marks = _pitch_marks(analysis, original_f0, rate, cancel)
    if len(marks) < 3:
        return data.copy()

    synth = []
    position = float(marks[0])
    while position < len(data):
        _check_cancel(cancel)
        synth.append(int(round(position)))
        index = min(len(data) - 1, max(0, int(round(position))))
        position += rate / np.clip(corrected_f0[index], MIN_F0, MAX_F0)
    if not synth:
        return data.copy()

    output = np.zeros_like(data, dtype=np.float64)
    weights = np.zeros(len(data), dtype=np.float64)
    marks_float = marks.astype(np.float64)
    for destination in synth:
        _check_cancel(cancel)
        where = int(np.searchsorted(marks, destination))
        if where == 0:
            source = int(marks[0])
        elif where == len(marks):
            source = int(marks[-1])
        else:
            before, after = int(marks[where - 1]), int(marks[where])
            source = before if destination - before <= after - destination else after
        source_index = int(np.searchsorted(marks_float, source))
        if source_index <= 0:
            period = marks[1] - marks[0]
        elif source_index >= len(marks) - 1:
            period = marks[-1] - marks[-2]
        else:
            period = (marks[source_index + 1] - marks[source_index - 1]) / 2.0
        half = max(8, int(round(period)))
        offsets = np.arange(-half, half + 1)
        src = source + offsets
        dst = destination + offsets
        valid = (src >= 0) & (src < len(data)) & (dst >= 0) & (dst < len(data))
        if not np.any(valid):
            continue
        window = np.hanning(2 * half + 1)[valid]
        output[dst[valid]] += data[src[valid]] * window[:, None]
        weights[dst[valid]] += window
    covered = weights > 1e-8
    output[covered] /= weights[covered, None]
    output[~covered] = data[~covered]
    return output


def _pitch_correct(data, rate, mode, key, cancel=None):
    analysis = _analysis_channel(data)
    centers, pitches, _ = _track_pitch(analysis, rate, cancel)
    if np.count_nonzero(np.isfinite(pitches)) < 2:
        return data.copy()
    curves = _pitch_curves(centers, pitches, mode, key, len(data), rate)
    if curves is None:
        return data.copy()
    original, corrected, voiced = curves
    _check_cancel(cancel)
    shifted = _td_psola(data, analysis, original, corrected, rate, cancel)
    return data * (1.0 - voiced[:, None]) + shifted * voiced[:, None]


def _spectral_polish(data, rate, mode, cancel=None):
    params = _RECIPES[mode]
    if len(data) < 8:
        return data.copy()

    target = max(EQ_MIN_BLOCK, int(round(EQ_BLOCK_SECONDS * rate)))
    block = min(EQ_MAX_BLOCK, _next_power_of_two(target))
    hop = block // 2
    edge = block // 2
    padded = np.pad(data, ((edge, edge), (0, 0)), mode="reflect")
    frame_count = max(1, int(np.ceil(max(0, len(padded) - block) / hop)) + 1)
    total = (frame_count - 1) * hop + block
    if total > len(padded):
        padded = np.pad(padded, ((0, total - len(padded)), (0, 0)), mode="reflect")

    frequencies = np.fft.rfftfreq(block, 1.0 / rate)
    cutoff = params["highpass_hz"]
    highpass = frequencies * frequencies / (frequencies * frequencies + cutoff * cutoff)
    center = 4200.0
    octaves = np.log2(np.maximum(frequencies, 1.0) / center)
    bell = np.exp(-0.5 * (octaves / 0.78) ** 2)
    presence = 10.0 ** ((params["presence_db"] * bell) / 20.0)
    response = highpass * presence
    window = np.sqrt(np.hanning(block + 1)[:-1])
    output = np.zeros_like(padded, dtype=np.float64)
    weights = np.zeros(len(padded), dtype=np.float64)
    for start in range(0, total - block + 1, hop):
        _check_cancel(cancel)
        frame = padded[start:start + block] * window[:, None]
        spectrum = np.fft.rfft(frame, axis=0)
        filtered = np.fft.irfft(spectrum * response[:, None], n=block, axis=0)
        output[start:start + block] += filtered * window[:, None]
        weights[start:start + block] += window * window
    covered = weights > 1e-12
    output[covered] /= weights[covered, None]
    output[~covered] = padded[~covered]
    return output[edge:edge + len(data)]


def _compress(data, rate, mode):
    params = _RECIPES[mode]
    detector = np.max(np.abs(data), axis=1)
    window = max(3, int(round(0.012 * rate)))
    if window % 2 == 0:
        window += 1
    envelope = np.sqrt(_moving_average(detector * detector, window) + 1e-20)
    threshold = 10.0 ** (params["compress_threshold_db"] / 20.0)
    ratio = params["compress_ratio"]
    gain = np.ones_like(envelope)
    above = envelope > threshold
    gain[above] = (threshold / envelope[above]) ** (1.0 - 1.0 / ratio)
    gain = _moving_average(gain, max(3, int(round(0.008 * rate)) | 1))
    return data * gain[:, None]


def _add_reverb(data, rate, mix):
    if mix <= 0.0:
        return data
    wet = np.zeros_like(data)
    taps = ((0.019, 0.55), (0.031, 0.40), (0.043, 0.28), (0.059, 0.20))
    for seconds, gain in taps:
        delay = max(1, int(round(seconds * rate)))
        if delay < len(data):
            wet[delay:] += gain * data[:-delay]
    return data * (1.0 - mix) + wet * mix


def _process_active(data, rate, mode, key, cancel=None):
    raw = np.asarray(data, dtype=np.float64)
    _check_cancel(cancel)
    processed = _pitch_correct(raw, rate, mode, key, cancel)
    _check_cancel(cancel)
    processed = _spectral_polish(processed, rate, mode, cancel)
    _check_cancel(cancel)
    processed = _compress(processed, rate, mode)
    _check_cancel(cancel)
    processed = _add_reverb(processed, rate, _RECIPES[mode]["reverb_mix"])
    _check_cancel(cancel)

    raw_rms = float(np.sqrt(np.mean(raw * raw)))
    made_rms = float(np.sqrt(np.mean(processed * processed)))
    if raw_rms > 0.0 and made_rms > 1e-12:
        processed *= raw_rms / made_rms
    peak = float(np.max(np.abs(processed), initial=0.0))
    if peak > LIMIT:
        processed *= LIMIT / peak

    edge = min(len(raw) // 2, max(1, int(round(0.008 * rate))))
    if edge > 1:
        ramp = np.linspace(0.0, 1.0, edge, endpoint=False)[:, None]
        processed[:edge] = raw[:edge] * (1.0 - ramp) + processed[:edge] * ramp
        processed[-edge:] = raw[-edge:] * ramp + processed[-edge:] * (1.0 - ramp)
        peak = float(np.max(np.abs(processed), initial=0.0))
        if peak > LIMIT:
            processed *= LIMIT / peak
    return processed


def render(data, rate, mode, key=None, cancel=None):
    """Render a mode without changing its timeline.

    cancel may be a zero-argument callback. A truthy result stops work by
    raising RenderCancelled.
    """
    mode = normalize_mode(mode)
    spec = parse_key(key)
    source = np.asarray(data, dtype=np.float32)
    if source.ndim not in (1, 2):
        raise ValueError("audio must have shape (frames,) or (frames, channels)")
    if source.ndim == 2 and source.shape[1] < 1:
        raise ValueError("audio must have at least one channel")
    if not isinstance(rate, (int, np.integer)) or rate <= 0:
        raise ValueError("sample rate must be a positive integer")
    _check_cancel(cancel)
    if mode == "off" or source.size == 0:
        return source.copy()

    was_mono = source.ndim == 1
    frames = source[:, None] if was_mono else source
    result = frames.copy()
    peak = float(np.max(np.abs(frames), initial=0.0))
    if peak == 0.0:
        return source.copy()
    silence = max(1e-7, peak * 1e-6)
    active_rows = np.flatnonzero(np.max(np.abs(frames), axis=1) > silence)
    if not len(active_rows):
        return source.copy()
    first, last = int(active_rows[0]), int(active_rows[-1]) + 1
    _check_cancel(cancel)
    result[first:last] = _process_active(
        frames[first:last], int(rate), mode, spec, cancel).astype(np.float32)
    _check_cancel(cancel)
    return result[:, 0] if was_mono else result


def estimate_key(data, rate, cancel=None):
    """Estimate a major/minor key and return (display name, confidence 0..1)."""
    source = np.asarray(data, dtype=np.float32)
    _check_cancel(cancel)
    if source.ndim == 1:
        signal = source.astype(np.float64)
    elif source.ndim == 2 and source.shape[1]:
        signal = _analysis_channel(source)
    else:
        raise ValueError("audio must have shape (frames,) or (frames, channels)")
    if rate <= 0:
        raise ValueError("sample rate must be positive")
    if len(signal) < 32 or not np.any(signal):
        return "Chromatic", 0.0

    window = min(8192, max(1024, _next_power_of_two(min(len(signal), 4096))))
    hop = max(1, window // 2)
    if len(signal) < window:
        signal = np.pad(signal, (0, window - len(signal)))
    starts = np.arange(0, len(signal) - window + 1, hop)
    if len(starts) > 240:
        starts = starts[np.linspace(0, len(starts) - 1, 240).astype(int)]
    chroma = np.zeros(12, dtype=np.float64)
    frequencies = np.fft.rfftfreq(window, 1.0 / rate)
    usable = (frequencies >= 55.0) & (frequencies <= 5000.0)
    usable_frequencies = frequencies[usable]
    pitch_classes = np.rint(69.0 + 12.0 * np.log2(usable_frequencies / 440.0)).astype(int) % 12
    taper = np.hanning(window)
    for start in starts:
        _check_cancel(cancel)
        frame = signal[start:start + window]
        frame = (frame - np.mean(frame)) * taper
        magnitudes = np.abs(np.fft.rfft(frame))[usable]
        for pitch_class in range(12):
            chroma[pitch_class] += float(np.sum(magnitudes[pitch_classes == pitch_class]))
    if not np.any(chroma):
        return "Chromatic", 0.0
    chroma /= np.linalg.norm(chroma) + 1e-12

    profiles = {
        "major": np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                           2.52, 5.19, 2.39, 3.66, 2.29, 2.88]),
        "minor": np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                           2.54, 4.75, 3.98, 2.69, 3.34, 3.17]),
    }
    centered_chroma = chroma - np.mean(chroma)
    scores = []
    for scale, profile in profiles.items():
        _check_cancel(cancel)
        centered_profile = profile - np.mean(profile)
        for root in range(12):
            _check_cancel(cancel)
            rolled = np.roll(centered_profile, root)
            score = float(np.dot(centered_chroma, rolled) /
                          ((np.linalg.norm(centered_chroma) * np.linalg.norm(rolled)) + 1e-12))
            scores.append((score, KeySpec(root, scale)))
    scores.sort(key=lambda item: item[0], reverse=True)
    best_score, best_key = scores[0]
    second_score = scores[1][0]
    confidence = float(np.clip((best_score - second_score) /
                               max(abs(best_score), 1e-9), 0.0, 1.0))
    return best_key.name, confidence
