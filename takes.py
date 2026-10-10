"""Take files on disk: one folder per song, trashed takes moved (never deleted)."""

import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

TAKE_RE = re.compile(r"^take_(\d{3,})\.wav$")
POLISHED_RE = re.compile(r"^take_(\d{3,})\.(gentle|strong)\.wav$")
POLISHED_TMP_RE = re.compile(
    r"^\.?take_(\d{3,})\.(gentle|strong)\.[A-Za-z0-9_-]+\.tmp\.wav$")
POLISH_INDEX_TMP_RE = re.compile(r"^\.index\.[A-Za-z0-9_-]+\.tmp$")
POLISH_MODES = ("gentle", "strong")
POLISH_INDEX_VERSION = 2
HASH_CHUNK_SIZE = 1024 * 1024
UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9 _.'()-]+")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
TRACK_EXTENSIONS = (".wav", ".mp3", ".flac", ".ogg", ".aif", ".aiff")
SONG_DEFAULTS = {"track": "", "start_at": 0.0, "leadin": 2.0, "use_track": True}
_POLISH_LOCKS = {}
_POLISH_LOCKS_GUARD = threading.Lock()


def _polish_lock_for(path):
    """Return the process-wide lock for one canonical polished directory."""
    key = os.path.normcase(str(Path(path).resolve()))
    with _POLISH_LOCKS_GUARD:
        return _POLISH_LOCKS.setdefault(key, threading.Lock())


def safe_name(name):
    """Turn a song title into a folder name that is safe on Windows."""
    cleaned = UNSAFE_CHARS.sub("", name).strip(" .")[:80]
    if not cleaned:
        return "untitled"
    if cleaned.split(".")[0].upper() in RESERVED:
        cleaned = "_" + cleaned
    return cleaned


def repair_wav_header(path):
    """Fix the size fields of a WAV whose writer died before closing it.

    libsndfile writes a placeholder header (data size 0) when a file is
    opened and only fills in the real sizes on close, so a crash or power
    cut leaves all the audio on disk behind a header that says "empty".
    Returns True if the header was rewritten.
    """
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return False
        block_align = None
        pos = 12
        while pos + 8 <= size:
            f.seek(pos)
            chunk_id, chunk_size = struct.unpack("<4sI", f.read(8))
            if chunk_id == b"fmt ":
                fmt = f.read(min(chunk_size, 16))
                block_align = struct.unpack("<H", fmt[12:14])[0]
            if chunk_id == b"data":
                available = size - (pos + 8)
                if block_align:
                    available -= available % block_align
                broken = (chunk_size in (0, 0xFFFFFFFF) and available > 0) or chunk_size > available
                if not broken:
                    return False
                f.seek(pos + 4)
                f.write(struct.pack("<I", available))
                f.seek(4)
                f.write(struct.pack("<I", pos + 8 + available - 8))
                return True
            pos += 8 + chunk_size + (chunk_size & 1)
    return False


def scan_wav(path):
    """(seconds, peak 0..1) of a WAV, read in blocks so long takes don't load at once."""
    info = sf.info(path)
    peak = 0
    for block in sf.blocks(path, blocksize=65536, dtype="int32", always_2d=True):
        if len(block):
            peak = max(peak, int(block.max()), -int(block.min()))
    return info.frames / info.samplerate, peak / float(2 ** 31)


@dataclass
class Take:
    num: int
    path: Path
    trashed: bool
    starred: bool = False
    seconds: float = 0.0
    peak_db: float = -200.0
    clipped: bool = False
    dropouts: int = 0
    recovered: bool = False
    unaligned: bool = False
    track: str = ""
    track_start: float = 0.0

    @property
    def label(self):
        return f"{self.num:03d}"


class Song:
    def __init__(self, root, name):
        self.name = safe_name(name)
        self.dir = Path(root) / self.name
        self.trash_dir = self.dir / "trash"
        self.polished_dir = self.dir / "polished"
        self.polish_index_path = self.polished_dir / "index.json"
        self.meta_path = self.dir / "takes.json"
        self.config_path = self.dir / "song.json"
        self._polish_lock = _polish_lock_for(self.polished_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._meta = self._load_json(self.meta_path, {})
        self.config = dict(SONG_DEFAULTS)
        self.config.update(self._load_json(self.config_path, {}))

    @staticmethod
    def _load_json(path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    @staticmethod
    def _save_json(path, data):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def _save_meta(self):
        self._save_json(self.meta_path, self._meta)

    def save_config(self):
        self._save_json(self.config_path, self.config)

    def _scan(self):
        found = {}
        for folder, trashed in ((self.dir, False), (self.trash_dir, True)):
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                m = TAKE_RE.match(path.name)
                if m:
                    found[int(m.group(1))] = (path, trashed)
        return found

    def next_path(self):
        """Path for the next take. Numbers are never reused, even for trashed takes."""
        nums = self._scan()
        num = max(nums, default=0) + 1
        return num, self.dir / f"take_{num:03d}.wav"

    def begin(self, num, **extra):
        """Note a take as in progress, so a crash leaves a record of what it was."""
        self._meta[str(num)] = {"incomplete": True, **extra}
        self._save_meta()

    def forget(self, num):
        """Drop the metadata of a take that never got a file."""
        if self._meta.pop(str(num), None) is not None:
            self._save_meta()

    def record(self, num, seconds, peak_db, clipped, dropouts, **extra):
        entry = self._meta.get(str(num), {})
        entry.pop("incomplete", None)
        entry.update(seconds=round(seconds, 3), peak_db=peak_db, clipped=clipped,
                     dropouts=dropouts, **extra)
        entry.setdefault("starred", False)
        self._meta[str(num)] = entry
        self._save_meta()

    def recover(self):
        """Repair takes left behind by a crash. Returns the take numbers fixed."""
        fixed = []
        for num, (path, _) in sorted(self._scan().items()):
            entry = self._meta.get(str(num))
            if entry is not None and not entry.get("incomplete"):
                continue
            try:
                repair_wav_header(path)
                seconds, peak = scan_wav(path)
            except (OSError, RuntimeError, sf.LibsndfileError):
                continue
            entry = dict(entry or {})
            seconds = max(0.0, seconds - entry.get("track_start", 0.0))
            peak_db = round(20 * np.log10(max(peak, 1e-10)), 1)
            stats = ("incomplete", "seconds", "peak_db", "clipped", "dropouts", "recovered")
            self.record(num, seconds, peak_db, peak >= 10 ** (-0.1 / 20), 0, recovered=True,
                        **{k: v for k, v in entry.items() if k not in stats})
            fixed.append(num)
        return fixed

    def takes(self):
        result = []
        for num, (path, trashed) in sorted(self._scan().items()):
            meta = self._meta.get(str(num), {})
            take = Take(num, path, trashed)
            for key in ("starred", "seconds", "peak_db", "clipped", "dropouts", "recovered",
                        "unaligned", "track", "track_start"):
                if key in meta:
                    setattr(take, key, meta[key])
            result.append(take)
        return result

    def get(self, num):
        return next((t for t in self.takes() if t.num == num), None)

    def trash(self, num):
        take = self.get(num)
        if take is None or take.trashed:
            return False
        self.trash_dir.mkdir(exist_ok=True)
        shutil.move(str(take.path), str(self.trash_dir / take.path.name))
        return True

    def restore(self, num):
        take = self.get(num)
        if take is None or not take.trashed:
            return False
        shutil.move(str(take.path), str(self.dir / take.path.name))
        return True

    # ---- polished playback cache ---------------------------------------

    @staticmethod
    def _validate_polish_mode(mode):
        if mode not in POLISH_MODES:
            raise ValueError(f"Invalid polish mode {mode!r}; use gentle or strong")

    @staticmethod
    def _validate_recipe_hash(recipe_hash):
        if not isinstance(recipe_hash, str) or not recipe_hash:
            raise ValueError("recipe_hash must be a non-empty string")

    @staticmethod
    def _source_stamp(path):
        stat = path.stat()
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    @staticmethod
    def _normalize_source_stamp(stamp):
        if not isinstance(stamp, dict) or set(stamp) != {"size", "mtime_ns"}:
            raise ValueError("expected_source must be a source stamp")
        if any(not isinstance(stamp[key], int) or isinstance(stamp[key], bool)
               or stamp[key] < 0 for key in ("size", "mtime_ns")):
            raise ValueError("expected_source must be a source stamp")
        return {"size": stamp["size"], "mtime_ns": stamp["mtime_ns"]}

    @staticmethod
    def _file_sha256(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(HASH_CHUNK_SIZE)
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)

    @staticmethod
    def _empty_polish_index():
        return {"version": POLISH_INDEX_VERSION, "renders": {}}

    def _load_polish_index(self):
        index = self._load_json(self.polish_index_path, None)
        if not isinstance(index, dict) or index.get("version") != POLISH_INDEX_VERSION:
            return self._empty_polish_index()
        if not isinstance(index.get("renders"), dict):
            return self._empty_polish_index()
        return index

    def _save_polish_index(self, index):
        """Atomically save the cache index without sharing a fixed temp name."""
        self.polished_dir.mkdir(exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".index.", suffix=".tmp",
                                         dir=self.polished_dir)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                fd = None
                json.dump(index, stream, indent=1, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.polish_index_path)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                temp_path.unlink()
            except OSError:
                pass

    def polished_path(self, num, mode):
        """Return the reserved cache path for a take and polish mode."""
        self._validate_polish_mode(mode)
        if not isinstance(num, int) or isinstance(num, bool) or num < 1:
            raise ValueError("take number must be a positive integer")
        return self.polished_dir / f"take_{num:03d}.{mode}.wav"

    def _active_take_path(self, take):
        if not isinstance(take, Take) or take.trashed:
            return None
        expected = self.dir / f"take_{take.num:03d}.wav"
        if take.path != expected:
            return None
        return expected

    def polish_source_stamp(self, take):
        """Return the current stamp for an active raw take in this song."""
        source_path = self._active_take_path(take)
        if source_path is None:
            raise ValueError("take must be an active take from this song")
        try:
            return self._source_stamp(source_path)
        except OSError as exc:
            raise ValueError(f"Could not inspect source take: {exc}") from exc

    @staticmethod
    def _audio_matches(source_path, polished_path):
        try:
            source = sf.info(source_path)
            polished = sf.info(polished_path)
        except (OSError, RuntimeError, sf.LibsndfileError):
            return False
        return (source.frames == polished.frames and
                source.samplerate == polished.samplerate and
                source.channels == polished.channels)

    def cached_polished(self, take, mode, recipe_hash):
        """Return a valid cached render, or None when it must be regenerated."""
        self._validate_polish_mode(mode)
        self._validate_recipe_hash(recipe_hash)
        source_path = self._active_take_path(take)
        if source_path is None:
            return None
        path = self.polished_path(take.num, mode)
        with self._polish_lock:
            try:
                source_stamp = self._source_stamp(source_path)
            except OSError:
                return None
            index = self._load_polish_index()
            entry = index["renders"].get(path.name)
            if not isinstance(entry, dict):
                return None
            if entry.get("recipe_hash") != recipe_hash or entry.get("source") != source_stamp:
                return None
            if not path.is_file() or not self._audio_matches(source_path, path):
                return None
            render_sha256 = entry.get("render_sha256")
            if not isinstance(render_sha256, str) or len(render_sha256) != 64:
                return None
            try:
                if self._file_sha256(path) != render_sha256:
                    return None
                if self._source_stamp(source_path) != source_stamp:
                    return None
            except OSError:
                return None
            return path

    def publish_polished(self, take, mode, recipe_hash, data, rate, expected_source=None):
        """Validate and atomically publish a 24-bit polished WAV render."""
        self._validate_polish_mode(mode)
        self._validate_recipe_hash(recipe_hash)
        if expected_source is not None:
            expected_source = self._normalize_source_stamp(expected_source)
        source_path = self._active_take_path(take)
        if source_path is None:
            raise ValueError("take must be an active take from this song")
        if not isinstance(rate, (int, np.integer)) or isinstance(rate, (bool, np.bool_)):
            raise ValueError("rate must be an integer")
        rate = int(rate)
        audio = np.asarray(data)
        if audio.ndim == 1:
            channels = 1
        elif audio.ndim == 2 and audio.shape[1] > 0:
            channels = audio.shape[1]
        else:
            raise ValueError("data must have shape (frames,) or (frames, channels)")
        if audio.dtype.kind not in "iuf":
            raise ValueError("data must contain real numeric audio samples")

        path = self.polished_path(take.num, mode)
        temp_path = None
        with self._polish_lock:
            try:
                source_stamp = self._source_stamp(source_path)
            except OSError as exc:
                if expected_source is not None:
                    raise RuntimeError("Source take changed while polishing") from exc
                raise ValueError(f"Could not inspect source take: {exc}") from exc
            if expected_source is not None and source_stamp != expected_source:
                raise RuntimeError("Source take changed while polishing")
            try:
                source_info = sf.info(source_path)
            except (OSError, RuntimeError, sf.LibsndfileError) as exc:
                raise ValueError(f"Could not inspect source take: {exc}") from exc
            if rate != source_info.samplerate:
                raise ValueError("polished rate must match the source take")
            if len(audio) != source_info.frames:
                raise ValueError("polished frame count must match the source take")
            if channels != source_info.channels:
                raise ValueError("polished channel count must match the source take")

            self.polished_dir.mkdir(exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp.wav",
                                             dir=self.polished_dir)
            os.close(fd)
            temp_path = Path(temp_name)
            try:
                sf.write(temp_path, audio, rate, format="WAV", subtype="PCM_24")
                written = sf.info(temp_path)
                if (written.frames != source_info.frames or
                        written.samplerate != source_info.samplerate or
                        written.channels != source_info.channels):
                    raise RuntimeError("Polished WAV validation failed")
                render_sha256 = self._file_sha256(temp_path)
                try:
                    current_stamp = self._source_stamp(source_path)
                except OSError as exc:
                    raise RuntimeError("Source take changed while polishing") from exc
                if (current_stamp != source_stamp or
                        (expected_source is not None and current_stamp != expected_source)):
                    raise RuntimeError("Source take changed while polishing")

                os.replace(temp_path, path)
                temp_path = None
                index = self._load_polish_index()
                index["renders"][path.name] = {
                    "recipe_hash": recipe_hash,
                    "render_sha256": render_sha256,
                    "source": source_stamp,
                }
                self._save_polish_index(index)
                return path
            finally:
                if temp_path is not None:
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass

    def prune_polished(self):
        """Remove invalid cache renders and abandoned render temp files."""
        if not self.polished_dir.is_dir():
            return []
        removed = []
        with self._polish_lock:
            index = self._load_polish_index()
            renders = index["renders"]
            kept = {}
            try:
                children = sorted(self.polished_dir.iterdir(), key=lambda item: item.name)
            except OSError:
                return []
            for path in children:
                if not path.is_file() or path == self.polish_index_path:
                    continue
                if POLISHED_TMP_RE.match(path.name) or POLISH_INDEX_TMP_RE.match(path.name):
                    try:
                        path.unlink()
                    except OSError:
                        continue
                    removed.append(path)
                    continue
                match = POLISHED_RE.match(path.name)
                if not match:
                    continue
                num = int(match.group(1))
                source_path = self.dir / f"take_{num:03d}.wav"
                entry = renders.get(path.name)
                valid = (isinstance(entry, dict) and
                         isinstance(entry.get("recipe_hash"), str) and
                         isinstance(entry.get("render_sha256"), str) and
                         len(entry["render_sha256"]) == 64)
                try:
                    valid = (valid and
                             entry.get("source") == self._source_stamp(source_path) and
                             entry["render_sha256"] == self._file_sha256(path))
                except OSError:
                    valid = False
                if valid:
                    kept[path.name] = entry
                    continue
                try:
                    path.unlink()
                except OSError:
                    continue
                removed.append(path)

            clean_index = {"version": POLISH_INDEX_VERSION, "renders": kept}
            if clean_index != index or self.polish_index_path.is_file():
                try:
                    self._save_polish_index(clean_index)
                except OSError:
                    pass
        return removed

    def set_star(self, num, starred):
        self._meta.setdefault(str(num), {})["starred"] = bool(starred)
        self._save_meta()

    def toggle_star(self, num):
        starred = not self._meta.get(str(num), {}).get("starred", False)
        self.set_star(num, starred)
        return starred

    # ---- backing track ---------------------------------------------------

    def set_track(self, source):
        """Copy a backing track into the song folder so the folder is self-contained."""
        source = Path(source)
        if source.suffix.lower() not in TRACK_EXTENSIONS:
            raise ValueError(f"Unsupported track type {source.suffix} "
                             f"(use {', '.join(TRACK_EXTENSIONS)})")
        sf.info(str(source))  # fail now, not mid-take, if it can't be decoded
        dest = self.dir / f"backing - {source.name}"
        if source.resolve() != dest.resolve():
            shutil.copy2(source, dest)
        self.config["track"] = dest.name
        self.save_config()
        return dest

    def clear_track(self):
        self.config["track"] = ""
        self.save_config()

    @property
    def track_path(self):
        name = self.config.get("track")
        if not name:
            return None
        path = self.dir / name
        return path if path.is_file() else None
