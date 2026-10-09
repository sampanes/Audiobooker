import os
import struct
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from takes import Song, repair_wav_header


def crash_mid_take(path, frames):
    """Write a 24-bit WAV in a child process that dies without closing the file."""
    script = textwrap.dedent(f"""
        import os, numpy as np, soundfile as sf
        f = sf.SoundFile({str(path)!r}, "w", samplerate=48000, channels=1,
                         format="WAV", subtype="PCM_24")
        f.write((np.arange({frames}, dtype=np.int64) << 8).astype(np.int32).reshape(-1, 1))
        os._exit(0)
    """)
    subprocess.run([sys.executable, "-c", script], check=True)


def data_size_field(path):
    raw = Path(path).read_bytes()
    pos = 12
    while pos + 8 <= len(raw):
        cid, size = struct.unpack("<4sI", raw[pos:pos + 8])
        if cid == b"data":
            return size
        pos += 8 + size + (size & 1)
    return None


class RepairTest(unittest.TestCase):
    def test_crashed_file_gets_real_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "take_001.wav"
            crash_mid_take(path, 4800)
            self.assertEqual(data_size_field(path), 0)  # what a crash really leaves
            self.assertTrue(repair_wav_header(path))
            self.assertEqual(data_size_field(path), 4800 * 3)
            back, _ = sf.read(str(path), dtype="int32")
            np.testing.assert_array_equal(back, np.arange(4800) << 8)
            self.assertFalse(repair_wav_header(path))  # second pass: nothing to do

    def test_closed_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.wav"
            sf.write(str(path), np.zeros((100, 1), dtype=np.int32), 48000, subtype="PCM_24")
            before = path.read_bytes()
            self.assertFalse(repair_wav_header(path))
            self.assertEqual(path.read_bytes(), before)

    def test_not_a_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.wav"
            path.write_bytes(b"hello world, not audio")
            self.assertFalse(repair_wav_header(path))


class RecoverTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_crash_during_track_take_keeps_track_info(self):
        song = Song(self.root, "demo")
        num, path = song.next_path()
        song.begin(num, track="backing - beat.wav", track_start=0.05)
        crash_mid_take(path, 48000)

        fixed = Song(self.root, "demo").recover()
        self.assertEqual(fixed, [num])
        take = Song(self.root, "demo").get(num)
        self.assertTrue(take.recovered)
        self.assertEqual(take.track, "backing - beat.wav")
        self.assertAlmostEqual(take.seconds, 0.95, places=3)  # minus the track offset
        self.assertGreater(take.peak_db, -60)
        self.assertEqual(Song(self.root, "demo").recover(), [])  # idempotent

    def test_take_with_no_metadata_is_rebuilt(self):
        song = Song(self.root, "demo")
        num, path = song.next_path()
        sf.write(str(path), np.full((24000, 1), 0.5, dtype=np.float32), 48000, subtype="PCM_24")
        self.assertEqual(song.recover(), [num])
        take = song.get(num)
        self.assertAlmostEqual(take.seconds, 0.5, places=3)
        self.assertAlmostEqual(take.peak_db, -6.0, places=1)

    def test_finished_takes_untouched(self):
        song = Song(self.root, "demo")
        num, path = song.next_path()
        sf.write(str(path), np.zeros((10, 1), dtype=np.float32), 48000, subtype="PCM_24")
        song.record(num, 9.0, -12.0, False, 0)
        self.assertEqual(song.recover(), [])
        self.assertEqual(song.get(num).seconds, 9.0)


class TrackConfigTest(unittest.TestCase):
    def test_track_is_copied_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "beat.wav"
            sf.write(str(src), np.zeros((4410, 2), dtype=np.float32), 44100)
            song = Song(Path(tmp) / "takes", "demo")
            dest = song.set_track(src)
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.parent, song.dir)
            song.config["start_at"] = 62.5
            song.save_config()
            again = Song(Path(tmp) / "takes", "demo")
            self.assertEqual(again.track_path, dest)
            self.assertEqual(again.config["start_at"], 62.5)
            again.clear_track()
            self.assertIsNone(Song(Path(tmp) / "takes", "demo").track_path)

    def test_rejects_unsupported_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "beat.m4a"
            bad.write_bytes(b"x")
            with self.assertRaises(ValueError):
                Song(Path(tmp) / "takes", "demo").set_track(bad)

    def test_forget_drops_unstarted_take(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = Song(tmp, "demo")
            song.begin(1, track="t.wav")
            song.forget(1)
            self.assertEqual(Song(tmp, "demo").recover(), [])
            self.assertFalse(os.path.exists(song.dir / "take_001.wav"))


if __name__ == "__main__":
    unittest.main()
