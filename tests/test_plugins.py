import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

import plugins
import polish


RATE = 48000


def sharp_voice(seconds=1.0, cents=40.0):
    t = np.arange(int(seconds * RATE)) / RATE
    f0 = 220.0 * 2.0 ** (cents / 1200.0)
    voice = sum(np.sin(2 * np.pi * n * f0 * t) / n for n in range(1, 12)) * 0.1
    return np.stack([voice, voice], axis=1).astype(np.float32)


def cents_from_a3(data):
    _, pitches, _ = polish._track_pitch(np.asarray(data)[:, 0].astype(np.float64), RATE)
    hz = float(np.median(pitches[np.isfinite(pitches)]))
    cents = 1200 * np.log2(hz / 220.0)
    return cents - 1200 * round(cents / 1200)


class FindInstalledTest(unittest.TestCase):
    def test_finds_bundles_and_files_without_descending_into_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "Auto-Tune_Access.vst3" / "Contents" / "x86_64-win"
            bundle.mkdir(parents=True)
            (bundle / "Auto-Tune_Access.vst3").write_bytes(b"")
            (root / "Antares").mkdir()
            (root / "Antares" / "Auto-Tune EFX+.vst3").write_bytes(b"")
            melda = root / "MeldaProduction" / "Pitch Shift"
            melda.mkdir(parents=True)
            (melda / "MAutoPitch.vst3").write_bytes(b"")
            (root / "ValhallaRoom.vst3").mkdir()
            (root / "Cymatics Memory.vst3").write_bytes(b"")

            found = plugins.find_installed([root, root / "missing"])

        self.assertEqual(set(found), {"auto-tune-access", "auto-tune-efx", "mautopitch",
                                      "valhalla-room"})
        self.assertEqual(Path(found["auto-tune-access"]).name, "Auto-Tune_Access.vst3")
        self.assertEqual(Path(found["auto-tune-access"]).parent, root)


class ResolveTest(unittest.TestCase):
    installed = {"auto-tune-efx": "efx", "mautopitch": "m", "valhalla-room": "v"}

    def test_auto_takes_the_first_installed_in_order(self):
        self.assertEqual(plugins.resolve("pitch", "auto", self.installed), "auto-tune-efx")
        self.assertEqual(plugins.resolve("room", "auto", self.installed), "valhalla-room")
        self.assertIsNone(plugins.resolve("pitch", "auto", {}))

    def test_auto_room_prefers_the_fast_mcharmverb(self):
        installed = {"valhalla-room": "v", "valhalla-vintageverb": "vv", "mcharmverb": "m"}
        self.assertEqual(plugins.resolve("room", "auto", installed), "mcharmverb")

    def test_builtin_and_explicit_choices(self):
        self.assertIsNone(plugins.resolve("pitch", "builtin", self.installed))
        self.assertEqual(plugins.resolve("pitch", "mautopitch", self.installed), "mautopitch")

    def test_missing_or_failed_choice_falls_back_to_the_next_best(self):
        self.assertEqual(plugins.resolve("pitch", "graillon", self.installed), "auto-tune-efx")
        self.assertEqual(plugins.resolve("pitch", "auto-tune-efx", self.installed,
                                         failed={"auto-tune-efx": "x"}), "mautopitch")
        self.assertIsNone(plugins.resolve("room", "auto", self.installed,
                                          failed={"valhalla-room": "x"}))

    def test_a_pitch_engine_is_never_used_as_a_room(self):
        self.assertEqual(plugins.resolve("room", "mautopitch", self.installed), "valhalla-room")

    def test_settings_list_only_installed_real_engines(self):
        self.assertEqual(plugins.choices("pitch", self.installed),
                         ["auto-tune-efx", "mautopitch"])
        self.assertEqual(plugins.choices("pitch", {"test-shift": "", **self.installed}),
                         ["auto-tune-efx", "mautopitch"])

    def test_only_engines_that_change_the_mode_are_kept(self):
        self.assertEqual(plugins.active_engines("lots-lots", "mautopitch", "valhalla-room"),
                         {"pitch": "mautopitch", "room": "valhalla-room"})
        self.assertEqual(plugins.active_engines("none-lots", "mautopitch", "valhalla-room"),
                         {"room": "valhalla-room"})
        self.assertEqual(plugins.active_engines("some-some", "mautopitch", "valhalla-room"),
                         {"pitch": "mautopitch"})
        self.assertEqual(plugins.active_engines("off", "mautopitch", "valhalla-room"), {})


class RecipeTest(unittest.TestCase):
    def test_builtin_hash_is_unchanged(self):
        for mode in ("some-some", "lots-none", "none-lots"):
            self.assertEqual(polish.recipe_hash(mode, "A minor"),
                             polish.recipe_hash(mode, "A minor", plugins.recipe({})))

    def test_each_engine_gets_its_own_hash(self):
        hashes = {polish.recipe_hash("lots-lots", None, plugins.recipe({"pitch": e}))
                  for e in ("auto-tune-access", "auto-tune-efx", "mautopitch", "graillon")}
        hashes.add(polish.recipe_hash("lots-lots", None))
        self.assertEqual(len(hashes), 5)


class PluginValuesTest(unittest.TestCase):
    def test_auto_tune_access_follows_the_key(self):
        values = dict(plugins.plugin_values("auto-tune-access", "lots", "A minor"))
        self.assertEqual((values["key"], values["scale"]), ("A", "Minor"))
        self.assertEqual((values["retune_speed"], values["humanize"]), ("Fast", "Off"))
        on = {k for k in plugins._NOTE_KEYS if values[k]}
        self.assertEqual(on, {"a", "b", "c", "d", "e", "f", "g"})

    def test_access_uses_its_flat_key_names_and_efx_its_sharp_ones(self):
        self.assertEqual(dict(plugins.plugin_values("auto-tune-access", "some", "Eb major"))
                         ["key"], "Eb")
        self.assertEqual(dict(plugins.plugin_values("auto-tune-efx", "some", "Eb major"))
                         ["auto_tune_key"], "D#")

    def test_chromatic_allows_every_note(self):
        for engine in ("auto-tune-access", "mautopitch"):
            values = dict(plugins.plugin_values(engine, "some", None))
            self.assertTrue(all(values[k] for k in plugins._NOTE_KEYS))
        values = dict(plugins.plugin_values("graillon", "lots", "Chromatic"))
        self.assertTrue(all(values[f"allow_{k}"] for k in plugins._NOTE_KEYS))
        self.assertEqual(dict(plugins.plugin_values("auto-tune-efx", "lots", None))
                         ["auto_tune_scale"], "Chromatic")

    def test_some_is_gentler_than_lots(self):
        some = dict(plugins.plugin_values("auto-tune-efx", "some", None))
        lots = dict(plugins.plugin_values("auto-tune-efx", "lots", None))
        self.assertGreater(some["retune_speed_ms"], lots["retune_speed_ms"])
        some = dict(plugins.plugin_values("mautopitch", "some", None))
        lots = dict(plugins.plugin_values("mautopitch", "lots", None))
        self.assertLess(some["depth_automatic_tuning"], lots["depth_automatic_tuning"])

    def test_text_only_knobs_take_the_nearest_option(self):
        options = ["0.10 s ", "0.90 s ", "1.00 s ", "2.00 s "]
        self.assertEqual(plugins.nearest_choice(options, 1.02), "1.00 s ")
        self.assertEqual(plugins.nearest_choice(["Large Room", "Medium Room"], 1.0), None)


class IsolatedRenderTest(unittest.TestCase):
    """Runs real child processes, using pedalboard's built-in effects as stand-ins."""

    def test_plugin_replaces_the_builtin_pitch_stage(self):
        data = sharp_voice()
        out = plugins.render_isolated(data, RATE, "lots-none", None, {"pitch": "test-shift"}, {})
        self.assertEqual(out.shape, data.shape)
        self.assertAlmostEqual(cents_from_a3(out), 140.0, delta=15.0)  # up 1 semitone

    def test_crash_names_the_engine(self):
        with self.assertRaises(plugins.PluginFailed) as caught:
            plugins.render_isolated(sharp_voice(0.3), RATE, "lots-none", None,
                                    {"pitch": "test-crash"}, {})
        self.assertEqual(caught.exception.engine, "test-crash")

    def test_silence_names_the_room_not_the_pitch_engine(self):
        with self.assertRaises(plugins.PluginFailed) as caught:
            plugins.render_isolated(sharp_voice(0.3), RATE, "lots-lots", None,
                                    {"pitch": "test-shift", "room": "test-silent"}, {})
        self.assertEqual(caught.exception.engine, "test-silent")
        self.assertIn("silence", str(caught.exception))

    def test_hang_times_out_and_cancel_stops_it(self):
        started = time.monotonic()
        with self.assertRaises(plugins.PluginFailed):
            plugins.render_isolated(sharp_voice(0.3), RATE, "lots-none", None,
                                    {"pitch": "test-hang"}, {}, timeout=2)
        with self.assertRaises(polish.RenderCancelled):
            plugins.render_isolated(sharp_voice(0.3), RATE, "lots-none", None,
                                    {"pitch": "test-hang"}, {},
                                    cancel=lambda: time.monotonic() - started > 3.0)
        self.assertLess(time.monotonic() - started, 30.0)


if __name__ == "__main__":
    unittest.main()
