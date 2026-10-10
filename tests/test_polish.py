import unittest

import numpy as np

import polish


def dominant_frequency(signal, rate, expected):
    signal = np.asarray(signal, dtype=np.float64)
    signal = signal - np.mean(signal)
    window = np.hanning(len(signal))
    spectrum = np.abs(np.fft.rfft(signal * window))
    frequencies = np.fft.rfftfreq(len(signal), 1.0 / rate)
    band = (frequencies > expected - 30.0) & (frequencies < expected + 30.0)
    indexes = np.flatnonzero(band)
    peak = indexes[int(np.argmax(spectrum[band]))]
    if 0 < peak < len(spectrum) - 1:
        left, center, right = np.log(np.maximum(spectrum[peak - 1:peak + 2], 1e-20))
        divisor = left - 2.0 * center + right
        offset = 0.5 * (left - right) / divisor if abs(divisor) > 1e-12 else 0.0
    else:
        offset = 0.0
    return (peak + offset) * rate / len(signal)


class SettingsTest(unittest.TestCase):
    def test_modes_and_keys_are_normalized(self):
        self.assertEqual(polish.normalize_mode(" Strong "), "strong")
        self.assertEqual(polish.parse_key("a MINOR").name, "A minor")
        self.assertIsNone(polish.parse_key("Chromatic"))
        self.assertEqual(polish.parse_key("Db major").root, 1)
        with self.assertRaises(ValueError):
            polish.normalize_mode("medium")
        with self.assertRaises(ValueError):
            polish.parse_key("H major")

    def test_scale_snapping(self):
        self.assertEqual(polish.snap_midi(61.1, "Chromatic"), 61.0)
        self.assertEqual(polish.snap_midi(61.1, "C major"), 62.0)
        self.assertEqual(polish.snap_midi(68.8, "A minor"), 69.0)

    def test_recipe_hash_is_stable_and_sensitive(self):
        first = polish.recipe_hash("gentle", "A minor")
        self.assertEqual(first, polish.recipe_hash("GENTLE", polish.parse_key("A minor")))
        self.assertNotEqual(first, polish.recipe_hash("strong", "A minor"))
        self.assertNotEqual(first, polish.recipe_hash("gentle", "A major"))


class RenderTest(unittest.TestCase):
    RATE = 24000

    @staticmethod
    def tone(frequency, seconds=1.2, rate=RATE):
        time = np.arange(int(seconds * rate)) / rate
        return (0.2 * np.sin(2.0 * np.pi * frequency * time)).astype(np.float32)

    def test_off_is_an_exact_copy(self):
        rng = np.random.default_rng(4)
        source = rng.normal(0.0, 0.1, (1000, 2)).astype(np.float32)
        made = polish.render(source, self.RATE, "off", "C major")
        self.assertIsNot(made, source)
        np.testing.assert_array_equal(made, source)

    def test_length_dtype_channels_and_leading_silence_are_preserved(self):
        silence = np.zeros((2400, 2), dtype=np.float32)
        tone = self.tone(220.0, 0.5)[:, None]
        tail = np.zeros((1200, 2), dtype=np.float32)
        source = np.concatenate((silence, np.repeat(tone, 2, axis=1), tail))
        original = source.copy()
        made = polish.render(source, self.RATE, "gentle", "Chromatic")
        self.assertEqual(made.shape, source.shape)
        self.assertEqual(made.dtype, np.float32)
        np.testing.assert_array_equal(made[:len(silence)], silence)
        np.testing.assert_array_equal(made[-len(tail):], tail)
        np.testing.assert_array_equal(source, original)
        self.assertLessEqual(float(np.max(np.abs(made))), polish.LIMIT + 1e-6)

    def test_flat_sine_is_corrected_by_mode_strength(self):
        target = 440.0
        flat = target * 2.0 ** (-35.0 / 1200.0)
        source = self.tone(flat)
        gentle = polish.render(source, self.RATE, "gentle", "Chromatic")
        strong = polish.render(source, self.RATE, "strong", "Chromatic")
        trim = int(0.15 * self.RATE)
        gentle_hz = dominant_frequency(gentle[trim:-trim], self.RATE, target)
        strong_hz = dominant_frequency(strong[trim:-trim], self.RATE, target)
        gentle_cents = 1200.0 * np.log2(gentle_hz / target)
        strong_cents = 1200.0 * np.log2(strong_hz / target)
        self.assertAlmostEqual(strong_cents, 0.0, delta=5.0)
        self.assertAlmostEqual(gentle_cents, -17.5, delta=7.0)

    def test_long_stereo_render_is_finite_bounded_and_same_shape(self):
        seconds = 6.0
        time = np.arange(int(seconds * self.RATE)) / self.RATE
        envelope = 0.75 + 0.25 * np.sin(2.0 * np.pi * 1.7 * time)
        left = 0.16 * envelope * np.sin(2.0 * np.pi * 431.2 * time)
        right = (0.11 * envelope * np.sin(2.0 * np.pi * 431.2 * time + 0.4) +
                 0.025 * np.sin(2.0 * np.pi * 862.4 * time))
        source = np.column_stack((left, right)).astype(np.float32)
        source[:1200] = 0.0
        source[-1200:] = 0.0

        made = polish.render(source, self.RATE, "strong", "A major")

        self.assertEqual(made.shape, source.shape)
        self.assertEqual(made.dtype, np.float32)
        self.assertTrue(np.isfinite(made).all())
        self.assertLessEqual(float(np.max(np.abs(made))), polish.LIMIT + 1e-6)
        np.testing.assert_array_equal(made[:1200], source[:1200])
        np.testing.assert_array_equal(made[-1200:], source[-1200:])

    def test_render_can_be_cancelled_inside_processing(self):
        source = self.tone(220.0, 2.0)
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 8

        with self.assertRaises(polish.RenderCancelled):
            polish.render(source, self.RATE, "strong", "Chromatic", cancel=cancel)
        self.assertEqual(calls, 8)


class KeyEstimateTest(unittest.TestCase):
    RATE = 16000

    def chord(self, frequencies, seconds=1.0):
        time = np.arange(int(self.RATE * seconds)) / self.RATE
        signal = sum(np.sin(2.0 * np.pi * frequency * time) for frequency in frequencies)
        return (0.08 * signal).astype(np.float32)

    def test_c_major_progression_clears_acceptance_threshold(self):
        progression = (
            (261.6256, 329.6276, 391.9954),
            (349.2282, 440.0000, 523.2511),
            (391.9954, 493.8833, 587.3295),
            (261.6256, 329.6276, 391.9954),
        )
        signal = np.concatenate([self.chord(notes) for notes in progression])
        name, confidence = polish.estimate_key(signal, self.RATE)
        self.assertEqual(name, "C major")
        self.assertGreater(confidence, 0.12)

    def test_single_triad_is_treated_as_ambiguous(self):
        signal = self.chord((261.6256, 329.6276, 391.9954), seconds=2.0)
        name, confidence = polish.estimate_key(signal, self.RATE)
        self.assertEqual(name, "C major")
        self.assertGreater(confidence, 0.0)
        self.assertLess(confidence, 0.12)

    def test_key_estimate_can_be_cancelled_inside_analysis(self):
        rate = 16000
        signal = self.chord((261.6256, 329.6276, 391.9954), seconds=4.0)
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 4

        with self.assertRaises(polish.RenderCancelled):
            polish.estimate_key(signal, rate, cancel=cancel)
        self.assertEqual(calls, 4)


if __name__ == "__main__":
    unittest.main()
