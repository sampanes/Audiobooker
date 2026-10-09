"""Take files on disk: one folder per song, trashed takes moved (never deleted)."""

import json
import os
import re
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

TAKE_RE = re.compile(r"^take_(\d{3,})\.wav$")
UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9 _.'()-]+")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
TRACK_EXTENSIONS = (".wav", ".mp3", ".flac", ".ogg", ".aif", ".aiff")
SONG_DEFAULTS = {"track": "", "start_at": 0.0, "leadin": 2.0, "use_track": True}


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
        self.meta_path = self.dir / "takes.json"
        self.config_path = self.dir / "song.json"
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
