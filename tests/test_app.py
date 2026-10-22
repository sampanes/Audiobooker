import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import soundfile as sf

import app as recorder_app
import polish
from takes import Song


class FakeVariable:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class FakeSong:
    def __init__(self, key="A major"):
        self.config = {"key": key}
        self.saved = 0

    def save_config(self):
        self.saved += 1


class PlaybackStateTest(unittest.TestCase):
    def test_start_playback_is_rejected_while_recording(self):
        instance = recorder_app.App.__new__(recorder_app.App)
        instance._closing = False
        instance._play_request_id = 7
        instance.calibration = None
        instance.engine = mock.Mock(recording=True)
        instance.song = mock.Mock()
        take = SimpleNamespace(num=1)

        instance._start_playback(take, "unused.wav", "strong", 7, instance.song)

        instance.engine.play.assert_not_called()
        instance.song.get.assert_not_called()

    def test_render_job_current_honors_token_and_cancel_event(self):
        instance = recorder_app.App.__new__(recorder_app.App)
        instance._closing = False
        instance._play_request_id = 3
        instance.song = object()
        job = {"request_id": 3, "song": instance.song, "cancel": threading.Event()}
        instance._pending_play = job

        self.assertTrue(instance._render_job_current(job))
        job["cancel"].set()
        self.assertFalse(instance._render_job_current(job))
        job["cancel"].clear()
        instance._play_request_id += 1
        self.assertFalse(instance._render_job_current(job))

    def test_cancelled_key_estimate_leaves_key_absent_for_retry(self):
        instance = recorder_app.App.__new__(recorder_app.App)
        instance._key_request_id = 0
        instance._estimating_key = None
        instance.track_data = (np.zeros((32, 2), dtype=np.float32), 48000)
        instance.song = FakeSong()
        instance.key_var = FakeVariable()
        instance._work_queue = queue.Queue()

        instance._queue_key_estimate()

        job = instance._work_queue.get_nowait()
        self.assertNotIn("key", instance.song.config)
        self.assertEqual(instance.key_var.value, "Chromatic")
        self.assertEqual(instance.song.saved, 1)
        self.assertIs(instance._estimating_key, job)

        instance._cancel_key_estimate()
        self.assertTrue(job["cancel"].is_set())
        self.assertIsNone(instance._estimating_key)
        self.assertNotIn("key", instance.song.config)

    def test_trashed_take_is_not_sent_to_polish_worker(self):
        instance = recorder_app.App.__new__(recorder_app.App)
        instance.engine = mock.Mock(recording=False)
        instance.calibration = None
        instance.stop_playback = mock.Mock()
        instance.flash = mock.Mock()
        instance._play_request_id = 4
        instance.song = mock.Mock()
        take = SimpleNamespace(label="009", trashed=True)

        instance._request_play(take, "gentle")

        instance.stop_playback.assert_called_once_with()
        instance.flash.assert_called_once_with("Restore take 009 before polishing it")
        instance.song.cached_polished.assert_not_called()

    def test_worker_renders_publishes_and_returns_cache_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = Song(Path(tmp), "demo")
            num, raw_path = song.next_path()
            rate = 16000
            time = np.arange(rate // 4) / rate
            source = (0.15 * np.sin(2.0 * np.pi * 431.2 * time)).astype(np.float32)
            sf.write(raw_path, source, rate, subtype="PCM_24")
            song.record(num, len(source) / rate, -16.0, False, 0)
            take = song.get(num)
            raw_before = raw_path.read_bytes()

            instance = recorder_app.App.__new__(recorder_app.App)
            instance._closing = False
            instance._play_request_id = 2
            instance.song = song
            instance._work_queue = queue.Queue()
            instance._work_results = queue.SimpleQueue()
            job = {
                "kind": "render",
                "request_id": 2,
                "song": song,
                "take": take,
                "mode": "strong",
                "key": None,
                "recipe": polish.recipe_hash("strong", None),
                "cancel": threading.Event(),
            }
            instance._pending_play = job
            instance._work_queue.put(job)
            instance._work_queue.put(None)

            instance._work_loop()

            made_job, path, error = instance._work_results.get_nowait()
            self.assertIs(made_job, job)
            self.assertIsNone(error)
            self.assertTrue(path.is_file())
            self.assertEqual(raw_path.read_bytes(), raw_before)
            self.assertEqual(song.cached_polished(take, "strong", job["recipe"]), path)

    def test_worker_does_not_read_raw_after_cache_check_is_cancelled(self):
        instance = recorder_app.App.__new__(recorder_app.App)
        instance._closing = False
        instance._play_request_id = 8
        instance._work_queue = queue.Queue()
        instance._work_results = queue.SimpleQueue()
        song = mock.Mock()
        instance.song = song
        take = SimpleNamespace(path=Path("unused.wav"))
        job = {
            "kind": "render",
            "request_id": 8,
            "song": song,
            "take": take,
            "mode": "gentle",
            "key": None,
            "recipe": "recipe",
            "cancel": threading.Event(),
        }
        instance._pending_play = job

        def cancel_during_lookup(*args):
            job["cancel"].set()
            return None

        song.cached_polished.side_effect = cancel_during_lookup
        instance._work_queue.put(job)
        instance._work_queue.put(None)

        with mock.patch("app.sf.read") as read:
            instance._work_loop()

        _, path, error = instance._work_results.get_nowait()
        self.assertIsNone(path)
        self.assertIsNone(error)
        read.assert_not_called()
        song.polish_source_stamp.assert_not_called()

    def test_abort_calibration_removes_partial_and_resets_ui(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".calibration.wav"
            path.write_bytes(b"partial")
            instance = recorder_app.App.__new__(recorder_app.App)
            instance.calibration = {"path": path, "times": [0.1]}
            instance.engine = mock.Mock()
            instance._set_recording_ui = mock.Mock()

            self.assertTrue(instance._abort_calibration())

            self.assertIsNone(instance.calibration)
            self.assertFalse(path.exists())
            instance.engine.stop_take.assert_called_once_with()
            instance._set_recording_ui.assert_called_once_with(False)
            self.assertFalse(instance._abort_calibration())


class RetakeGuardTest(unittest.TestCase):
    def app_with_last_take(self, seconds, starred=False):
        instance = recorder_app.App.__new__(recorder_app.App)
        instance.engine = mock.Mock(recording=False)
        instance.last_recorded = 4
        take = SimpleNamespace(num=4, label="004", seconds=seconds, starred=starred,
                               trashed=False)
        instance.song = mock.Mock()
        instance.song.get.return_value = take
        instance._trash_num = mock.Mock()
        instance.toggle_record = mock.Mock()
        instance.flash = mock.Mock()
        return instance

    def test_starred_take_is_never_retaken(self):
        instance = self.app_with_last_take(5.0, starred=True)
        with mock.patch.object(recorder_app.messagebox, "askyesno") as ask:
            instance.retake()
        ask.assert_not_called()
        instance._trash_num.assert_not_called()
        instance.toggle_record.assert_not_called()

    def test_short_take_retakes_without_asking(self):
        instance = self.app_with_last_take(5.0)
        with mock.patch.object(recorder_app.messagebox, "askyesno") as ask:
            instance.retake()
        ask.assert_not_called()
        instance._trash_num.assert_called_once_with(4)
        instance.toggle_record.assert_called_once_with()

    def test_long_take_asks_and_no_keeps_it(self):
        instance = self.app_with_last_take(85.0)
        with mock.patch.object(recorder_app.messagebox, "askyesno", return_value=False) as ask:
            instance.retake()
        ask.assert_called_once()
        instance._trash_num.assert_not_called()
        instance.toggle_record.assert_not_called()

    def test_long_take_asks_and_yes_retakes(self):
        instance = self.app_with_last_take(85.0)
        with mock.patch.object(recorder_app.messagebox, "askyesno", return_value=True):
            instance.retake()
        instance._trash_num.assert_called_once_with(4)
        instance.toggle_record.assert_called_once_with()


class SpeakerGuessTest(unittest.TestCase):
    def test_skips_virtual_outputs_and_prefers_a_jack(self):
        names = ["Speakers (Game Stream Virtual)", "Monitor (Display Audio)",
                 "Line Out (Onboard Audio)"]
        self.assertEqual(recorder_app.guess_speakers(names), "Line Out (Onboard Audio)")

    def test_falls_back_to_first_real_output(self):
        self.assertEqual(recorder_app.guess_speakers(["Monitor (Display Audio)"]),
                         "Monitor (Display Audio)")
        self.assertEqual(recorder_app.guess_speakers([]), "")


if __name__ == "__main__":
    unittest.main()
