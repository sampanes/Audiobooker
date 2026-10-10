import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

from takes import Song, safe_name


class SafeNameTest(unittest.TestCase):
    def test_strips_path_and_reserved_names(self):
        self.assertEqual(safe_name("..\\..\\evil/song"), "evilsong")
        self.assertEqual(safe_name("  "), "untitled")
        self.assertEqual(safe_name("con"), "_con")
        self.assertEqual(safe_name("My Song (demo)"), "My Song (demo)")


class SongTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def add_take(self, song):
        num, path = song.next_path()
        path.write_bytes(b"RIFF")
        song.record(num, seconds=1.5, peak_db=-12.0, clipped=False, dropouts=0)
        return num

    def test_numbers_never_reused_after_trash(self):
        song = Song(self.root, "demo")
        self.assertEqual([self.add_take(song) for _ in range(3)], [1, 2, 3])
        self.assertTrue(song.trash(3))
        self.assertEqual(song.next_path()[0], 4)

    def test_trash_and_restore_move_files(self):
        song = Song(self.root, "demo")
        num = self.add_take(song)
        song.trash(num)
        take = song.get(num)
        self.assertTrue(take.trashed)
        self.assertEqual(take.path.parent.name, "trash")
        self.assertFalse(song.trash(num))
        self.assertTrue(song.restore(num))
        self.assertFalse(song.get(num).trashed)

    def test_set_star_is_idempotent(self):
        song = Song(self.root, "demo")
        num = self.add_take(song)
        song.set_star(num, True)
        song.set_star(num, True)
        self.assertTrue(song.get(num).starred)
        song.set_star(num, False)
        self.assertFalse(song.get(num).starred)

    def test_metadata_and_star_survive_reload(self):
        song = Song(self.root, "demo")
        num = self.add_take(song)
        song.toggle_star(num)
        take = Song(self.root, "demo").get(num)
        self.assertTrue(take.starred)
        self.assertEqual(take.seconds, 1.5)
        self.assertEqual(take.peak_db, -12.0)


class PolishedCacheTest(unittest.TestCase):
    RATE = 48000
    FRAMES = 480

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.song = Song(self.root, "demo")

    def tearDown(self):
        self._tmp.cleanup()

    def add_audio_take(self, channels=1):
        num, path = self.song.next_path()
        shape = (self.FRAMES,) if channels == 1 else (self.FRAMES, channels)
        raw = np.linspace(-0.25, 0.25, np.prod(shape), dtype=np.float32).reshape(shape)
        sf.write(path, raw, self.RATE, subtype="PCM_24")
        self.song.record(num, seconds=self.FRAMES / self.RATE, peak_db=-12.0,
                         clipped=False, dropouts=0)
        return self.song.get(num), raw

    def test_paths_are_strict_and_do_not_become_takes(self):
        take, data = self.add_audio_take()
        path = self.song.publish_polished(take, "gentle", "recipe-a", data, self.RATE)
        self.assertEqual(path, self.song.dir / "polished" / "take_001.gentle.wav")
        self.assertEqual([item.num for item in self.song.takes()], [1])
        self.assertEqual(self.song.next_path()[0], 2)
        for mode in ("off", "Gentle", "../gentle", ""):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.song.polished_path(1, mode)

    def test_publish_is_24_bit_cached_and_leaves_raw_unchanged(self):
        take, data = self.add_audio_take(channels=2)
        raw_before = take.path.read_bytes()
        expected = self.song.polish_source_stamp(take)
        polished = self.song.publish_polished(take, "strong", "recipe-a",
                                               data * 0.5, self.RATE,
                                               expected_source=expected)

        self.assertEqual(take.path.read_bytes(), raw_before)
        self.assertEqual(self.song.polish_source_stamp(take), expected)
        info = sf.info(polished)
        self.assertEqual(info.frames, self.FRAMES)
        self.assertEqual(info.samplerate, self.RATE)
        self.assertEqual(info.channels, 2)
        self.assertEqual(info.subtype, "PCM_24")
        self.assertEqual(self.song.cached_polished(take, "strong", "recipe-a"), polished)
        self.assertIsNone(self.song.cached_polished(take, "strong", "recipe-b"))
        self.assertIsNone(self.song.cached_polished(take, "gentle", "recipe-a"))
        self.assertEqual(list(self.song.polished_dir.glob("*.tmp.wav")), [])

    def test_source_change_invalidates_cache(self):
        take, data = self.add_audio_take()
        self.song.publish_polished(take, "gentle", "recipe-a", data, self.RATE)
        with take.path.open("ab") as stream:
            stream.write(b"x")
        self.assertIsNone(self.song.cached_polished(take, "gentle", "recipe-a"))

    def test_song_instances_serialize_cache_transactions(self):
        take, data = self.add_audio_take()
        other = Song(self.root, "demo")
        other_take = other.get(take.num)
        self.assertIs(self.song._polish_lock, other._polish_lock)

        with ThreadPoolExecutor(max_workers=2) as pool:
            gentle = pool.submit(self.song.publish_polished, take, "gentle",
                                  "recipe-g", data, self.RATE)
            strong = pool.submit(other.publish_polished, other_take, "strong",
                                 "recipe-s", data * 0.5, self.RATE)
            gentle_path = gentle.result()
            strong_path = strong.result()

        self.assertEqual(self.song.cached_polished(take, "gentle", "recipe-g"), gentle_path)
        self.assertEqual(other.cached_polished(other_take, "strong", "recipe-s"), strong_path)

    def test_expected_source_rejects_change_before_publish(self):
        take, data = self.add_audio_take()
        expected = self.song.polish_source_stamp(take)
        with take.path.open("ab") as stream:
            stream.write(b"x")

        with self.assertRaisesRegex(RuntimeError, "Source take changed"):
            self.song.publish_polished(take, "gentle", "recipe-a", data, self.RATE,
                                       expected_source=expected)
        self.assertFalse(self.song.polished_path(take.num, "gentle").exists())
        self.assertTrue(self.song.trash(take.num))
        with self.assertRaises(ValueError):
            self.song.polish_source_stamp(self.song.get(take.num))

    def test_publish_rejects_mismatched_audio(self):
        take, data = self.add_audio_take(channels=2)
        cases = (
            (data[:-1], self.RATE),
            (data, 44100),
            (data[:, 0], self.RATE),
        )
        for rendered, rate in cases:
            with self.subTest(shape=rendered.shape, rate=rate), self.assertRaises(ValueError):
                self.song.publish_polished(take, "gentle", "recipe-a", rendered, rate)
        self.assertFalse(self.song.polished_path(take.num, "gentle").exists())

    def test_publish_discards_render_if_source_changes(self):
        take, data = self.add_audio_take()
        expected = self.song.polish_source_stamp(take)
        real_write = sf.write

        def write_and_change_source(*args, **kwargs):
            real_write(*args, **kwargs)
            with take.path.open("ab") as stream:
                stream.write(b"x")

        with mock.patch("takes.sf.write", side_effect=write_and_change_source):
            with self.assertRaisesRegex(RuntimeError, "Source take changed"):
                self.song.publish_polished(take, "strong", "recipe-a", data, self.RATE,
                                           expected_source=expected)
        self.assertFalse(self.song.polished_path(take.num, "strong").exists())
        self.assertEqual(list(self.song.polished_dir.glob("*.tmp.wav")), [])

    def test_failed_index_save_cannot_validate_replaced_render(self):
        take, data = self.add_audio_take()
        path = self.song.publish_polished(take, "gentle", "recipe-a",
                                           np.zeros_like(data), self.RATE)
        old_bytes = path.read_bytes()

        with mock.patch.object(self.song, "_save_polish_index",
                               side_effect=OSError("index failed")):
            with self.assertRaisesRegex(OSError, "index failed"):
                self.song.publish_polished(take, "gentle", "recipe-b",
                                           np.full_like(data, 0.125), self.RATE)

        self.assertNotEqual(path.read_bytes(), old_bytes)
        self.assertIsNone(self.song.cached_polished(take, "gentle", "recipe-a"))
        self.assertIsNone(self.song.cached_polished(take, "gentle", "recipe-b"))
        self.assertEqual(self.song.prune_polished(), [path])

    def test_prune_removes_orphans_and_temps_but_preserves_unknown_files(self):
        first, first_data = self.add_audio_take()
        second, second_data = self.add_audio_take()
        first_render = self.song.publish_polished(first, "gentle", "recipe-a",
                                                   first_data, self.RATE)
        second_render = self.song.publish_polished(second, "strong", "recipe-b",
                                                    second_data, self.RATE)
        self.assertTrue(self.song.trash(first.num))
        orphan = self.song.polished_dir / "take_999.gentle.wav"
        orphan.write_bytes(b"cache")
        abandoned = self.song.polished_dir / ".take_999.strong.deadbeef.tmp.wav"
        abandoned.write_bytes(b"partial")
        abandoned_index = self.song.polished_dir / ".index.deadbeef.tmp"
        abandoned_index.write_bytes(b"partial")
        unknown = self.song.polished_dir / "keep-me.txt"
        unknown.write_text("not a cache file", encoding="ascii")

        removed = self.song.prune_polished()

        expected = [abandoned, abandoned_index, first_render, orphan]
        self.assertEqual(removed, sorted(expected, key=lambda p: p.name))
        self.assertFalse(first_render.exists())
        self.assertTrue(second_render.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(self.song.restore(first.num))
        restored = self.song.get(first.num)
        self.assertIsNone(self.song.cached_polished(restored, "gentle", "recipe-a"))
        self.assertEqual(self.song.cached_polished(second, "strong", "recipe-b"), second_render)

    def test_corrupt_index_is_a_safe_miss_and_prunes_only_known_cache_files(self):
        take, data = self.add_audio_take()
        render = self.song.publish_polished(take, "gentle", "recipe-a", data, self.RATE)
        unknown = self.song.polished_dir / "notes.wav"
        unknown.write_bytes(b"keep")
        self.song.polish_index_path.write_text("{not json", encoding="ascii")

        self.assertIsNone(self.song.cached_polished(take, "gentle", "recipe-a"))
        self.assertEqual(self.song.prune_polished(), [render])
        self.assertFalse(render.exists())
        self.assertTrue(unknown.exists())


if __name__ == "__main__":
    unittest.main()
