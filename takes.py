"""Take files on disk: one folder per song, trashed takes moved (never deleted)."""

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

TAKE_RE = re.compile(r"^take_(\d{3,})\.wav$")
UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9 _.'()-]+")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name):
    """Turn a song title into a folder name that is safe on Windows."""
    cleaned = UNSAFE_CHARS.sub("", name).strip(" .")[:80]
    if not cleaned:
        return "untitled"
    if cleaned.split(".")[0].upper() in RESERVED:
        cleaned = "_" + cleaned
    return cleaned


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

    @property
    def label(self):
        return f"{self.num:03d}"


class Song:
    def __init__(self, root, name):
        self.name = safe_name(name)
        self.dir = Path(root) / self.name
        self.trash_dir = self.dir / "trash"
        self.meta_path = self.dir / "takes.json"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._meta = self._load_meta()

    def _load_meta(self):
        try:
            return json.loads(self.meta_path.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return {}

    def _save_meta(self):
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._meta, indent=1, sort_keys=True), encoding="ascii")
        tmp.replace(self.meta_path)

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

    def record(self, num, seconds, peak_db, clipped, dropouts):
        self._meta[str(num)] = {
            "seconds": round(seconds, 3), "peak_db": peak_db, "clipped": clipped,
            "dropouts": dropouts, "starred": self._meta.get(str(num), {}).get("starred", False),
        }
        self._save_meta()

    def takes(self):
        result = []
        for num, (path, trashed) in sorted(self._scan().items()):
            meta = self._meta.get(str(num), {})
            take = Take(num, path, trashed)
            for key in ("starred", "seconds", "peak_db", "clipped", "dropouts"):
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

