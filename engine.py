"""Audio engine: always-on input stream, bit-exact take writing, backing track, playback.

The input stream stays open for the life of the app so the meter is always
live and a take starts on the very next audio block. Audio arrives as int32
(24-bit samples left-justified, low byte zero) and is written to 24-bit WAV,
which libsndfile does with a plain shift -- the file holds exactly the
samples the interface delivered.

Backing-track takes are written on the track's timeline: the file starts
with silence up to the point the vocal begins, so every take lines up with
the track when both are dropped into a DAW at 0:00.
"""

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
import soundfile as sf

FULL_SCALE = float(2 ** 31)
CLIP_LEVEL = 10 ** (-0.1 / 20)  # anything at or above -0.1 dBFS counts as a clip
METER_CHANNELS = 2

ALIGN_TIMEOUT_S = 3.0
DAC_SAMPLES = 64      # output callbacks used to pin down when the track reached the headphones
MIN_DAC_SAMPLES = 16

CAL_LEAD_S = 1.0
CAL_CLICKS = 8
CAL_SPACING_S = 0.5
CAL_MAX_SPREAD_S = 0.004


def to_dbfs(level):
    return 20 * np.log10(max(level, 1e-10))


@dataclass
class Candidate:
    index: int
    name: str
    hostapi: str
    exclusive: bool
    bit_perfect: bool
    max_inputs: int

    def describe(self):
        if "WASAPI" in self.hostapi:
            mode = "WASAPI exclusive" if self.exclusive else "WASAPI shared"
        else:
            mode = self.hostapi
        return f"{self.name} | {mode}"


def find_inputs(match, devices=None, hostapis=None):
    """Return input candidates for the interface, best first.

    Order: WASAPI exclusive (bit-perfect), WASAPI shared, then anything else
    (MME/DirectSound). ASIO is deliberately not used: PortAudio loads every
    installed ASIO driver at startup, and generic wrappers like ASIO4ALL can
    hang the process when the interface is busy. WASAPI exclusive is already
    bit-perfect, and latency doesn't matter with hardware monitoring.
    """
    devices = sd.query_devices() if devices is None else devices
    hostapis = sd.query_hostapis() if hostapis is None else hostapis
    terms = [t.lower() for t in match]
    exclusive, shared, other = [], [], []
    for index, dev in enumerate(devices):
        if dev["max_input_channels"] < 1:
            continue
        name = dev["name"].lower()
        if not any(t in name for t in terms):
            continue
        api = hostapis[dev["hostapi"]]["name"]
        args = (index, dev["name"], api)
        inputs = dev["max_input_channels"]
        if "WASAPI" in api:
            exclusive.append(Candidate(*args, True, True, inputs))
            shared.append(Candidate(*args, False, False, inputs))
        elif "WDM-KS" in api:
            continue  # PortAudio's kernel-streaming backend is too flaky to trust
        else:
            other.append(Candidate(*args, False, False, inputs))
    return exclusive + shared + other


def find_output(match, prefer_hostapi=None):
    """Return (device index, hostapi name) for the interface's output, or (None, None)."""
    terms = [t.lower() for t in match]
    hostapis = sd.query_hostapis()
    found = []
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_output_channels"] < 1:
            continue
        name = dev["name"].lower()
        if not any(t in name for t in terms):
            continue
        api = hostapis[dev["hostapi"]]["name"]
        if "WDM-KS" in api:
            continue
        found.append((0 if api == prefer_hostapi else 1, index, api))
    if not found:
        return None, None
    found.sort()
    return found[0][1], found[0][2]


ROUTES = ("headphones", "speakers", "both")


def list_speakers(match, devices=None, hostapis=None):
    """Names of WASAPI outputs other than the interface (motherboard jack, monitors...)."""
    devices = sd.query_devices() if devices is None else devices
    hostapis = sd.query_hostapis() if hostapis is None else hostapis
    terms = [t.lower() for t in match]
    names = []
    for dev in devices:
        if dev["max_output_channels"] < 1 or "WASAPI" not in hostapis[dev["hostapi"]]["name"]:
            continue
        if any(t in dev["name"].lower() for t in terms) or dev["name"] in names:
            continue
        names.append(dev["name"])
    return names


def find_speakers(name, devices=None, hostapis=None):
    """Return (device index, hostapi name) for an output by exact name, preferring WASAPI."""
    devices = sd.query_devices() if devices is None else devices
    hostapis = sd.query_hostapis() if hostapis is None else hostapis
    found = []
    for index, dev in enumerate(devices):
        if dev["max_output_channels"] < 1 or dev["name"] != name:
            continue
        api = hostapis[dev["hostapi"]]["name"]
        if "WDM-KS" in api:
            continue
        found.append((0 if "WASAPI" in api else 1, index, api))
    if not found:
        return None, None
    found.sort()
    return found[0][1], found[0][2]


def windows_mix_rate(match):
    """The rate Windows runs the interface's output at, or None.

    The interface has one clock for input and output. Recording at this rate
    lets the backing track (shared-mode output) play while the input is held
    exclusively; any other rate and the output can't open.
    """
    hostapis = sd.query_hostapis()
    terms = [t.lower() for t in match]
    for dev in sd.query_devices():
        if (dev["max_output_channels"] > 0 and any(t in dev["name"].lower() for t in terms)
                and "WASAPI" in hostapis[dev["hostapi"]]["name"]):
            return int(dev["default_samplerate"])
    return None


# ---- backing track helpers ------------------------------------------------

@dataclass
class TrackCue:
    data: np.ndarray       # float32 (frames, 2)
    samplerate: int
    play_from: float       # track seconds where playback (including lead-in) starts
    latency: float = 0.0   # calibrated correction, seconds; positive = recording lands late


def load_track(path):
    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = np.ascontiguousarray(data[:, :2])
    return data, rate


def aligned_index(first_frame, input_origin, track_origin, play_from, latency, samplerate):
    """Where the first recorded input frame belongs on the track's timeline, in frames.

    input_origin is the clock time of input frame 0; track_origin is the clock
    time at which the track's play_from position reached the headphones.
    Singing in time with what you hear puts input captured at clock time t at
    track time play_from + (t - track_origin), less the calibrated latency
    the timestamps don't account for.
    """
    t = input_origin + first_frame / samplerate
    return int(round((play_from + (t - track_origin) - latency) * samplerate))


def mix_audition(vocal, rate, track, track_rate, start_s, track_gain):
    """Vocal (frames, 2) from start_s with the backing track underneath, for listening back."""
    n = len(vocal)
    out = vocal.copy()
    if track_rate == rate:
        s = int(round(start_s * rate))
        seg = track[s:s + n]
        out[:len(seg)] += track_gain * seg
    else:
        idx = start_s * track_rate + np.arange(n) * (track_rate / rate)
        xp = np.arange(len(track))
        for ch in range(2):
            out[:, ch] += track_gain * np.interp(idx, xp, track[:, ch], right=0.0).astype(np.float32)
    np.clip(out, -1.0, 1.0, out=out)
    return out


def click_track(samplerate):
    """Calibration clicks: short noise bursts, returned with their times in seconds."""
    frames = int((CAL_LEAD_S + CAL_CLICKS * CAL_SPACING_S + 0.5) * samplerate)
    data = np.zeros((frames, 2), dtype=np.float32)
    burst_len = max(8, int(0.002 * samplerate))
    rng = np.random.default_rng(7)
    burst = (rng.uniform(-1, 1, burst_len) * np.hanning(burst_len) * 0.6).astype(np.float32)
    times = [CAL_LEAD_S + i * CAL_SPACING_S for i in range(CAL_CLICKS)]
    for t in times:
        s = int(round(t * samplerate))
        data[s:s + burst_len] = burst[:, None]
    return data, times


def detect_click_delays(signal, samplerate, click_times):
    """How late each click shows up in the recording (seconds); undetected clicks are skipped."""
    x = np.abs(np.asarray(signal, dtype=np.float64))
    quiet = x[:max(0, int((click_times[0] - 0.2) * samplerate))]
    noise = float(np.percentile(quiet, 99)) if len(quiet) else 0.0
    delays = []
    for c in click_times:
        a = max(0, int((c - 0.1) * samplerate))
        b = min(len(x), int((c + 0.4) * samplerate))
        seg = x[a:b]
        if not len(seg):
            continue
        peak = float(seg.max())
        if peak < max(noise * 4, 1e-4):
            continue
        hit = int(np.argmax(seg >= max(noise * 2, peak * 0.3)))
        delays.append((a + hit) / samplerate - c)
    return delays


def calibration_result(delays):
    """(latency seconds, spread seconds) from click delays, or ValueError saying what went wrong."""
    if len(delays) < CAL_CLICKS - 2:
        raise ValueError(f"Only heard {len(delays)} of {CAL_CLICKS} clicks. Hold an ear cup right "
                         "against the mic and turn the headphone volume up a bit.")
    d = np.sort(np.array(delays))
    core = d[1:-1] if len(d) > 4 else d   # drop the single most extreme on each side
    spread = float(core[-1] - core[0])
    if spread > CAL_MAX_SPREAD_S:
        raise ValueError(f"Clicks were inconsistent ({spread * 1000:.1f} ms apart). Keep the "
                         "ear cup still against the mic and keep the room quiet, then retry.")
    return float(np.median(d)), spread


# ---- streams ----------------------------------------------------------------

class TakeWriter:
    """Streams int32 blocks to a 24-bit WAV on a background thread.

    With align, the first block's input frame index is mapped onto the track
    timeline: the file gets that much leading silence (or the first frames
    are dropped if they belong before 0:00).
    """

    def __init__(self, path, samplerate, channels, align=None):
        self.path = path
        self.samplerate = samplerate
        self.frames = 0      # frames in the file, padding included
        self.recorded = 0    # input frames actually written
        self.pad = 0         # leading silence written for track alignment
        self.peak = 0.0
        self.overflows = 0
        self.align_failed = False
        self.error = None
        self._align = align
        self._drop = 0
        self._queue = queue.Queue()
        self._file = sf.SoundFile(path, "w", samplerate=samplerate, channels=channels,
                                  format="WAV", subtype="PCM_24")
        self._thread = threading.Thread(target=self._run, name="take-writer", daemon=True)
        self._thread.start()

    def put(self, block, index=0):
        self._queue.put_nowait((block, index))

    def finish(self):
        self._queue.put(None)
        self._thread.join()

    def _run(self):
        first = True
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                if self.error is not None:
                    continue  # keep draining so finish() never hangs
                block, index = item
                try:
                    if first:
                        first = False
                        if self._align is not None:
                            self._start_aligned(index, block.shape[1])
                    if self._drop:
                        n = min(self._drop, len(block))
                        block = block[n:]
                        self._drop -= n
                    if not len(block):
                        continue
                    self._file.write(block)
                except Exception as exc:
                    self.error = exc
                    continue
                self.frames += len(block)
                self.recorded += len(block)
                peak = max(int(block.max()), -int(block.min())) / FULL_SCALE
                self.peak = max(self.peak, peak)
        finally:
            self._file.close()

    def _start_aligned(self, index, channels):
        deadline = time.monotonic() + ALIGN_TIMEOUT_S
        start = self._align(index)
        while start is None and time.monotonic() < deadline:
            time.sleep(0.005)
            start = self._align(index)
        if start is None:
            self.align_failed = True
            return
        if start < 0:
            self._drop = -start
            return
        silence = np.zeros((min(start, self.samplerate) or 1, channels), dtype=np.int32)
        left = start
        while left > 0:
            n = min(left, len(silence))
            self._file.write(silence[:n])
            left -= n
        self.pad = start
        self.frames += start

    @property
    def seconds(self):
        return self.recorded / self.samplerate


@dataclass
class TakeResult:
    path: str
    seconds: float
    peak_db: float
    clipped: bool
    dropouts: int
    error: str
    pad_seconds: float = 0.0
    align_failed: bool = False


class Player:
    """Plays a float32 array from `start` through an output stream; stops itself at the end.

    Also measures when data[start] reached the output, on the same clock the
    input stream's timestamps use -- that is what aligns takes to a track.
    """

    def __init__(self, data, samplerate, device, hostapi, start=0):
        self.data = data
        self.samplerate = samplerate
        self.start = start
        self.pos = start
        self._origins = []
        self._stopped = False
        channels = data.shape[1]
        errors = []
        settings = [None]
        if hostapi and "WASAPI" in hostapi:
            settings = [sd.WasapiSettings(auto_convert=True), None]
        for extra in settings:
            try:
                self._stream = sd.OutputStream(
                    device=device, samplerate=samplerate, channels=channels, dtype="float32",
                    callback=self._callback, extra_settings=extra)
                break
            except Exception as exc:
                errors.append(str(exc))
        else:
            raise RuntimeError("; ".join(errors))
        self._stream.start()

    def _callback(self, outdata, frames, time_info, status):
        dac = time_info.outputBufferDacTime or time_info.currentTime
        if dac > 0 and len(self._origins) < DAC_SAMPLES:
            # Late callbacks overstate the time, never understate it: keep the minimum.
            self._origins.append(dac - (self.pos - self.start) / self.samplerate)
        chunk = self.data[self.pos:self.pos + frames]
        n = len(chunk)
        outdata[:n] = chunk
        self.pos += n
        if n < frames:
            outdata[n:] = 0
            raise sd.CallbackStop

    def origin(self):
        """Clock time at which data[start] reached the output, or None until measured."""
        origins = self._origins[:]
        if len(origins) >= MIN_DAC_SAMPLES or (origins and self._stopped):
            return min(origins)
        return None

    def stop(self):
        self._stopped = True
        self._stream.abort()
        self._stream.close()

    @property
    def active(self):
        return not self._stopped and self._stream.active

    @property
    def seconds(self):
        return self.pos / self.samplerate

    @property
    def length(self):
        return len(self.data) / self.samplerate


class Engine:
    def __init__(self, device_match, samplerate="auto", preroll_seconds=0.3):
        self.device_match = list(device_match)
        self.requested_rate = samplerate
        self.samplerate = 48000 if samplerate == "auto" else int(samplerate)
        self.preroll_seconds = preroll_seconds
        self.preroll_frames = 0
        self.backend = None
        self.error = None
        self.lost = False
        self.input_paused = False
        self.channels = [0]
        self._stream = None
        self._closing = False
        self._lock = threading.Lock()
        self._writer = None
        self._track = None
        self._preroll = deque()
        self._preroll_len = 0
        self._in_frames = 0
        self._in_origins = deque(maxlen=256)
        self._peaks = np.zeros(METER_CHANNELS)
        self._player = None
        self._extra_players = []  # speakers, when playing to both
        self._out_device = None
        self._out_hostapi = None

    # ---- input stream -------------------------------------------------

    def open(self, refresh=False):
        """Open the best available input. Returns True on success; sets self.error otherwise.

        refresh re-scans the audio devices (after a replug, or a Windows format change).
        """
        self.close()
        self.error = None
        self.lost = False
        self.input_paused = False
        if refresh and self._player is None:
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        if self.requested_rate == "auto":
            self.samplerate = windows_mix_rate(self.device_match) or 48000
        self.preroll_frames = int(self.preroll_seconds * self.samplerate)
        with self._lock:
            self._preroll.clear()
            self._preroll_len = 0
            self._in_frames = 0
            self._in_origins.clear()
        errors = []
        candidates = find_inputs(self.device_match)
        if not candidates:
            self.error = f"No input device matching {self.device_match} found. Is the interface plugged in?"
            return False
        for cand in candidates:
            extra = None
            if "WASAPI" in cand.hostapi:
                extra = sd.WasapiSettings(exclusive=cand.exclusive)
            try:
                stream = sd.InputStream(
                    device=cand.index, samplerate=self.samplerate,
                    channels=min(METER_CHANNELS, cand.max_inputs), dtype="int32",
                    latency="high",  # nothing is monitored through software, so favor safety
                    extra_settings=extra, callback=self._callback,
                    finished_callback=self._finished)
                stream.start()
            except Exception as exc:
                errors.append(f"{cand.describe()}: {exc}")
                continue
            self._stream = stream
            self._closing = False
            self.backend = cand
            self._out_device, self._out_hostapi = find_output(self.device_match, cand.hostapi)
            return True
        self.error = "Could not open the interface:\n" + "\n".join(errors)
        return False

    def close(self):
        if self._stream is not None:
            self._closing = True
            self._stream.abort()
            self._stream.close()
            self._stream = None
        self.backend = None

    @property
    def input_count(self):
        return self._stream.channels if self._stream is not None else 0

    def set_channels(self, channels):
        with self._lock:
            self.channels = list(channels)
            self._preroll.clear()
            self._preroll_len = 0

    def _callback(self, indata, frames, time_info, status):
        peak = np.maximum(indata.max(axis=0).astype(np.float64),
                          -indata.min(axis=0).astype(np.float64)) / FULL_SCALE
        self._peaks[:len(peak)] = np.maximum(self._peaks[:len(peak)], peak)
        block = indata[:, self.channels]  # fancy indexing copies, so the block outlives indata
        adc = time_info.inputBufferAdcTime or time_info.currentTime
        with self._lock:
            index = self._in_frames
            self._in_frames += frames
            self._in_origins.append(adc - index / self.samplerate)
            writer = self._writer
            if writer is not None:
                if status.input_overflow:
                    writer.overflows += 1
                writer.put(block, index)
            elif self.preroll_frames > 0:
                self._preroll.append((index, block))
                self._preroll_len += frames
                while (self._preroll and
                       self._preroll_len - len(self._preroll[0][1]) >= self.preroll_frames):
                    self._preroll_len -= len(self._preroll.popleft()[1])

    def _finished(self):
        if not self._closing:
            self.lost = True

    def input_origin(self):
        """Clock time of input frame 0 (median over recent blocks), or None."""
        with self._lock:
            origins = list(self._in_origins)
        return float(np.median(origins)) if len(origins) >= 8 else None

    def take_peaks(self):
        """Peak level per input since the last call (0..1 of full scale)."""
        peaks, self._peaks = self._peaks, np.zeros(METER_CHANNELS)
        return peaks

    # ---- takes --------------------------------------------------------

    @property
    def recording(self):
        return self._writer is not None

    @property
    def track(self):
        """The backing-track player of the take in progress, if any."""
        return self._track

    def start_take(self, path, cue=None):
        self.stop_playback()
        if self._stream is None:
            raise RuntimeError("The interface isn't connected.")
        track = None
        if cue is not None:
            start = int(round(cue.play_from * cue.samplerate))
            try:
                track = Player(cue.data, cue.samplerate, self._out_device, self._out_hostapi,
                               start=start)
            except Exception as exc:
                raise RuntimeError(
                    f"Can't play the backing track while recording at {self.samplerate} Hz "
                    f"({exc}). Set Hz to auto so it matches Windows.") from exc
        align = None
        if track is not None:
            def align(index):
                t0, a0 = track.origin(), self.input_origin()
                if t0 is None or a0 is None:
                    return None
                return aligned_index(index, a0, t0, cue.play_from, cue.latency, self.samplerate)
        try:
            writer = TakeWriter(path, self.samplerate, len(self.channels), align)
        except Exception:
            if track is not None:
                track.stop()
            raise
        with self._lock:
            if track is None:
                # Pre-roll: the moment before the key press, so a breath or pickup isn't lost.
                for index, block in self._preroll:
                    writer.put(block, index)
            self._preroll.clear()
            self._preroll_len = 0
            self._writer = writer
            self._track = track

    def stop_take(self):
        with self._lock:
            writer, self._writer = self._writer, None
            track, self._track = self._track, None
        if track is not None:
            track.stop()
        if writer is None:
            return None
        writer.finish()
        return TakeResult(
            path=writer.path, seconds=writer.seconds, peak_db=round(to_dbfs(writer.peak), 1),
            clipped=writer.peak >= CLIP_LEVEL, dropouts=writer.overflows,
            error=str(writer.error) if writer.error else "",
            pad_seconds=writer.pad / self.samplerate, align_failed=writer.align_failed)

    def take_seconds(self):
        writer = self._writer
        return writer.seconds if writer is not None else 0.0

    # ---- playback -----------------------------------------------------

    def play(self, path, start_s=0.0, backing=None, track_gain=0.7,
             route="headphones", speakers=None):
        """Play a take from start_s; with backing=(data, rate), mix the track underneath.

        route is "headphones" (the interface), "speakers" (the output named by
        `speakers`) or "both" at once.
        """
        self._stop_player()
        if route not in ROUTES:
            raise ValueError(f"unknown playback route {route!r}")
        spk_device = spk_hostapi = None
        if route != "headphones":
            spk_device, spk_hostapi = find_speakers(speakers) if speakers else (None, None)
            if spk_device is None:
                raise RuntimeError(f"Speakers not found: {speakers or 'none chosen'}. "
                                   "Pick them in the Speakers list.")
        data, rate = sf.read(path, dtype="float32", always_2d=True)
        data = data[int(round(start_s * rate)):]
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)  # mono take to both ears
        if backing is not None:
            data = mix_audition(data, rate, backing[0], backing[1], start_s, track_gain)
        if route == "speakers":
            self._player = Player(data, rate, spk_device, spk_hostapi)
            return
        self._play_headphones(data, rate)
        if route == "both":
            try:
                self._extra_players.append(Player(data, rate, spk_device, spk_hostapi))
            except Exception:
                self._stop_player()
                raise

    def _play_headphones(self, data, rate):
        try:
            self._player = Player(data, rate, self._out_device, self._out_hostapi)
        except Exception:
            if self._stream is None or self.recording:
                raise
            # Input and output share one clock on the interface. If the input is
            # held exclusively at a rate Windows' output isn't set to, the shared
            # output can't open -- so let go of the input until playback ends.
            out_device, out_hostapi = self._out_device, self._out_hostapi
            self.close()
            self.input_paused = True
            try:
                self._player = Player(data, rate, out_device, out_hostapi)
            except Exception:
                self.open()
                raise

    def _stop_player(self):
        players = [self._player] + self._extra_players
        self._player, self._extra_players = None, []
        for player in players:
            if player is not None:
                player.stop()

    def stop_playback(self):
        """Stop playback and bring the input back if playback had to pause it."""
        self._stop_player()
        if self.input_paused:
            self.open()

    @property
    def player(self):
        return self._player

    def output_name(self):
        if self._out_device is None:
            return "system default output"
        return sd.query_devices(self._out_device)["name"]
