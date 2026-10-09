import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

import engine


class TakeWriterTest(unittest.TestCase):
    def test_24bit_round_trip_is_bit_exact(self):
        # 24-bit samples left-justified in int32, exactly how PortAudio hands them over.
        rng = np.random.default_rng(1)
        samples = rng.integers(-2 ** 23, 2 ** 23, size=(48000, 1), dtype=np.int64)
        blocks = (samples << 8).astype(np.int32)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "take_001.wav")
            writer = engine.TakeWriter(path, 48000, 1)
            for i in range(0, len(blocks), 480):
                writer.put(blocks[i:i + 480])
            writer.finish()
            info = sf.info(path)
            back, rate = sf.read(path, dtype="int32", always_2d=True)
        self.assertEqual(info.subtype, "PCM_24")
        self.assertEqual(rate, 48000)
        np.testing.assert_array_equal(back, blocks)
        self.assertEqual(writer.frames, 48000)

    def test_peak_and_full_scale_negative(self):
        block = np.array([[0], [1 << 30], [-(2 ** 31)]], dtype=np.int32)
        with tempfile.TemporaryDirectory() as tmp:
            writer = engine.TakeWriter(str(Path(tmp) / "t.wav"), 48000, 1)
            writer.put(block)
            writer.finish()
        self.assertEqual(writer.peak, 1.0)  # int32 min must not overflow the abs()
        self.assertGreaterEqual(writer.peak, engine.CLIP_LEVEL)


class FindInputsTest(unittest.TestCase):
    HOSTAPIS = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"},
                {"name": "Windows WDM-KS"}]

    @staticmethod
    def dev(name, api, inputs=2):
        return {"name": name, "hostapi": api, "max_input_channels": inputs}

    def test_prefers_wasapi_exclusive_then_shared_then_others(self):
        devices = [self.dev("Microphone (Studio 26c)", 0),
                   self.dev("Microphone (Studio 26c)", 1),
                   self.dev("Microphone (Studio 26c)", 2),
                   self.dev("Microphone (Studio 26c)", 3),
                   self.dev("Microphone (Webcam)", 2)]
        found = engine.find_inputs(["Studio 26c", "PreSonus"], devices, self.HOSTAPIS)
        self.assertEqual([(c.index, c.exclusive, c.bit_perfect) for c in found],
                         [(2, True, True), (2, False, False), (0, False, False), (1, False, False)])
        self.assertEqual(found[0].describe(), "Microphone (Studio 26c) | WASAPI exclusive")

    def test_ignores_output_only_devices(self):
        devices = [self.dev("Speakers (Studio 26c)", 2, inputs=0)]
        self.assertEqual(engine.find_inputs(["Studio 26c"], devices, self.HOSTAPIS), [])


if __name__ == "__main__":
    unittest.main()
