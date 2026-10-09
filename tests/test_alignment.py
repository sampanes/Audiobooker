import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

import engine


class AlignedIndexTest(unittest.TestCase):
    def test_same_instant_lands_at_play_from(self):
        # Input frame 0 captured exactly when play_from reached the headphones.
        self.assertEqual(engine.aligned_index(0, 100.0, 100.0, 10.0, 0.0, 48000), 480000)

    def test_later_input_and_latency(self):
        # Frame 4800 (0.1 s after input origin), input origin 0.05 s after the track
        # origin, 0.02 s of uncaptured latency: 10 + 0.15 - 0.02 = 10.13 s.
        idx = engine.aligned_index(4800, 100.05, 100.0, 10.0, 0.02, 48000)
        self.assertEqual(idx, round(10.13 * 48000))

    def test_can_go_negative_at_track_start(self):
        self.assertLess(engine.aligned_index(0, 99.9, 100.0, 0.0, 0.0, 48000), 0)


class AlignedWriterTest(unittest.TestCase):
    def write(self, start):
        block = (np.arange(1, 481, dtype=np.int64) << 8).astype(np.int32).reshape(-1, 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t.wav")
            writer = engine.TakeWriter(path, 48000, 1, align=lambda index: start)
            writer.put(block, 0)
            writer.put(block, 480)
            writer.finish()
            data, _ = sf.read(path, dtype="int32")
        return writer, data, block.ravel()

    def test_positive_offset_pads_with_silence(self):
        writer, data, block = self.write(1000)
        self.assertEqual(writer.pad, 1000)
        self.assertEqual(len(data), 1000 + 960)
        self.assertFalse(data[:1000].any())
        np.testing.assert_array_equal(data[1000:1480], block)
        self.assertEqual(writer.seconds, 960 / 48000)  # sung length excludes padding

    def test_negative_offset_drops_across_blocks(self):
        writer, data, block = self.write(-600)
        self.assertEqual(len(data), 960 - 600)
        np.testing.assert_array_equal(data, np.concatenate([block, block])[600:])

    def test_alignment_that_never_resolves_is_flagged(self):
        engine_timeout = engine.ALIGN_TIMEOUT_S
        engine.ALIGN_TIMEOUT_S = 0.05
        try:
            with tempfile.TemporaryDirectory() as tmp:
                writer = engine.TakeWriter(str(Path(tmp) / "t.wav"), 48000, 1, align=lambda i: None)
                writer.put(np.zeros((10, 1), dtype=np.int32), 0)
                writer.finish()
            self.assertTrue(writer.align_failed)
            self.assertEqual(writer.recorded, 10)
        finally:
            engine.ALIGN_TIMEOUT_S = engine_timeout


class CalibrationTest(unittest.TestCase):
    def simulate(self, delay, rate=44100, noise=0.002, missing=()):
        clicks, times = engine.click_track(rate)
        rng = np.random.default_rng(3)
        rec = rng.normal(0, noise, len(clicks))
        shift = int(round(delay * rate))
        mono = clicks[:, 0] * 0.3
        moved = np.zeros_like(rec)
        moved[shift:] = mono[:len(mono) - shift] if shift else mono
        for i in missing:
            s = int((times[i] + delay - 0.01) * rate)
            moved[s:s + int(0.05 * rate)] = 0
        return rec + moved, rate, times

    def test_measures_known_delay(self):
        x, rate, times = self.simulate(0.0173)
        latency, spread = engine.calibration_result(engine.detect_click_delays(x, rate, times))
        self.assertAlmostEqual(latency, 0.0173, delta=0.0006)
        self.assertLess(spread, 0.001)

    def test_too_few_clicks_heard(self):
        x, rate, times = self.simulate(0.01, missing=range(4))
        with self.assertRaises(ValueError):
            engine.calibration_result(engine.detect_click_delays(x, rate, times))

    def test_silence_fails(self):
        clicks, times = engine.click_track(44100)
        silent = np.random.default_rng(1).normal(0, 0.001, len(clicks))
        with self.assertRaises(ValueError):
            engine.calibration_result(engine.detect_click_delays(silent, 44100, times))

    def test_inconsistent_clicks_fail(self):
        delays = [0.010, 0.011, 0.030, 0.012, 0.045, 0.010, 0.025, 0.040]
        with self.assertRaises(ValueError):
            engine.calibration_result(delays)


class MixAuditionTest(unittest.TestCase):
    def test_same_rate_adds_track_region(self):
        vocal = np.zeros((100, 2), dtype=np.float32)
        track = np.tile(np.arange(1000, dtype=np.float32)[:, None] / 1000, (1, 2))
        out = engine.mix_audition(vocal, 1000, track, 1000, 0.5, 1.0)
        np.testing.assert_allclose(out[:, 0], np.arange(500, 600) / 1000, atol=1e-6)

    def test_resamples_and_clips(self):
        vocal = np.full((480, 2), 0.9, dtype=np.float32)
        track = np.ones((441, 2), dtype=np.float32)
        out = engine.mix_audition(vocal, 48000, track, 44100, 0.0, 1.0)
        self.assertEqual(out.shape, (480, 2))
        self.assertLessEqual(out.max(), 1.0)


if __name__ == "__main__":
    unittest.main()
