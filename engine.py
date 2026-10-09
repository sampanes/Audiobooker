"""Audio engine: always-on input stream, bit-exact take writing, playback.

The input stream stays open for the life of the app so the meter is always
live and a take starts on the very next audio block. Audio arrives as int32
(24-bit samples left-justified, low byte zero) and is written to 24-bit WAV,
which libsndfile does with a plain shift -- the file holds exactly the
samples the interface delivered.
"""

import queue
import threading
from collections import deque
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
import soundfile as sf

FULL_SCALE = float(2 ** 31)
CLIP_LEVEL = 10 ** (-0.1 / 20)  # anything at or above -0.1 dBFS counts as a clip
METER_CHANNELS = 2


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


class TakeWriter:
    """Streams int32 blocks to a 24-bit WAV on a background thread."""

    def __init__(self, path, samplerate, channels):
        self.path = path
        self.samplerate = samplerate
        self.frames = 0
        self.peak = 0.0
        self.overflows = 0
        self.error = None
        self._queue = queue.Queue()
        self._file = sf.SoundFile(path, "w", samplerate=samplerate, channels=channels,
                                  format="WAV", subtype="PCM_24")
        self._thread = threading.Thread(target=self._run, name="take-writer", daemon=True)
        self._thread.start()

    def put(self, block):
        self._queue.put_nowait(block)

    def finish(self):
        self._queue.put(None)
        self._thread.join()

    def _run(self):
        try:
            while True:
                block = self._queue.get()
                if block is None:
                    break
                if self.error is not None:
                    continue  # keep draining so finish() never hangs
                try:
                    self._file.write(block)
                except Exception as exc:
                    self.error = exc
                    continue
                self.frames += len(block)
                if len(block):
                    peak = max(int(block.max()), -int(block.min())) / FULL_SCALE
                    self.peak = max(self.peak, peak)
        finally:
            self._file.close()

    @property
    def seconds(self):
        return self.frames / self.samplerate


@dataclass
class TakeResult:
    path: str
    seconds: float
    peak_db: float
    clipped: bool
    dropouts: int
    error: str


class Player:
    """Plays one float32 array through an output stream; stops itself at the end."""

    def __init__(self, data, samplerate, device, hostapi):
        self.data = data
        self.samplerate = samplerate
        self.pos = 0
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
        chunk = self.data[self.pos:self.pos + frames]
        n = len(chunk)
        outdata[:n] = chunk
        self.pos += n
        if n < frames:
            outdata[n:] = 0
            raise sd.CallbackStop

    def stop(self):
        self._stream.abort()
        self._stream.close()

    @property
    def active(self):
        return self._stream.active

    @property
    def seconds(self):
        return self.pos / self.samplerate

    @property
    def length(self):
        return len(self.data) / self.samplerate


class Engine:
    def __init__(self, device_match, samplerate=48000, preroll_seconds=0.3):
        self.device_match = list(device_match)
        self.samplerate = int(samplerate)
        self.preroll_frames = int(preroll_seconds * self.samplerate)
        self.backend = None
        self.error = None
        self.lost = False
        self.input_paused = False
        self.channels = [0]
        self._stream = None
        self._closing = False
        self._lock = threading.Lock()
        self._writer = None
        self._preroll = deque()
        self._preroll_len = 0
        self._peaks = np.zeros(METER_CHANNELS)
        self._player = None
        self._out_device = None
        self._out_hostapi = None

    # ---- input stream -------------------------------------------------

    def open(self):
        """Open the best available input. Returns True on success; sets self.error otherwise."""
        self.close()
        self.error = None
        self.lost = False
        self.input_paused = False
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
        with self._lock:
            writer = self._writer
            if writer is not None:
                if status.input_overflow:
                    writer.overflows += 1
                writer.put(block)
            elif self.preroll_frames > 0:
                self._preroll.append(block)
                self._preroll_len += frames
                while self._preroll and self._preroll_len - len(self._preroll[0]) >= self.preroll_frames:
                    self._preroll_len -= len(self._preroll.popleft())

    def _finished(self):
        if not self._closing:
            self.lost = True

    def take_peaks(self):
        """Peak level per input since the last call (0..1 of full scale)."""
        peaks, self._peaks = self._peaks, np.zeros(METER_CHANNELS)
        return peaks

    # ---- takes --------------------------------------------------------

    @property
    def recording(self):
        return self._writer is not None

    def start_take(self, path):
        writer = TakeWriter(path, self.samplerate, len(self.channels))
        with self._lock:
            # Pre-roll: the moment before the key press, so a breath or pickup isn't lost.
            for block in self._preroll:
                writer.put(block)
            self._preroll.clear()
            self._preroll_len = 0
            self._writer = writer

    def stop_take(self):
        with self._lock:
            writer, self._writer = self._writer, None
        if writer is None:
            return None
        writer.finish()
        return TakeResult(
            path=writer.path, seconds=writer.seconds, peak_db=round(to_dbfs(writer.peak), 1),
            clipped=writer.peak >= CLIP_LEVEL, dropouts=writer.overflows,
            error=str(writer.error) if writer.error else "")

    def take_seconds(self):
        writer = self._writer
        return writer.seconds if writer is not None else 0.0

    # ---- playback -----------------------------------------------------

    def play(self, path):
        self._stop_player()
        data, rate = sf.read(path, dtype="float32", always_2d=True)
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)  # mono take to both ears
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
        player, self._player = self._player, None
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
