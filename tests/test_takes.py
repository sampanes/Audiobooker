import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
