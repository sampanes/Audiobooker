# Settings for a voice-model sample

What to record and which playback settings to export with, when the goal is a
few minutes of singing to build an AI voice model (for example a Suno Voice)
that sounds like the singer, only a bit more polished.

The goal is not a showcase take. It is the singer's ordinary voice, nudged
toward decent, with nothing the model could mistake for part of the voice.

## Short version

1. Record dry or over a backing track (the track plays only in the headphones).
2. Export with **Pitch Some** and **Tone None** (or Tone Some).
3. Never use Lots for a model sample, on either control.
4. Aim for 2 to 4 minutes of kept takes covering low, middle and high notes.

## Why these settings

### Pitch: Some, not Lots

The published guidance for natural, transparent correction is consistent:

- **Auto-Tune Access** has a three-position Retune Speed. Its manual says Fast
  gives the audible effect and Medium or Slow gives natural correction, and
  that Humanize lets sustained notes keep small natural deviations. Reviewers
  call Medium or Slow plus Humanize "quite natural" for a vocal that is
  already fairly in tune.
- For plugins that take a retune time in milliseconds, transparent correction
  sits around 20 to 60 ms. Antares suggests starting at 50 ms or slower and
  only tightening if the performance needs it, with Humanize around 20 to 40
  and Flex-Tune on. Hard-tune effects are 0 to 10 ms.
- **Graillon 2**: raise Smooth for natural transitions; its minimum is the
  robotic effect. Inertia makes notes "stick" and can change the melody, so it
  stays moderate.
- No published "natural" numbers were found for **MAutoPitch**; its Some
  values follow the same idea (partial depth, moderate speed).

What the recorder uses for Some and Lots (`plugins.py`):

| Plugin | Some | Lots |
|--------|------|------|
| Auto-Tune Access | Retune Medium, Humanize Max | Retune Fast, Humanize Off |
| Auto-Tune EFX+ | Retune 40 ms, Humanize 40 | Retune 0 ms, Humanize 0 |
| MAutoPitch | Depth 60, Speed 40 | Depth 100, Speed 100 |
| Graillon 2 | Correction 70, Smoothing 25 ms, Inertia 30 | Correction 100, Smoothing 1 ms, Inertia 0 |
| Built-in | Pulls each note halfway, eased | Full snap, fast |

Some already matches the recommended natural range, so there is nothing to
tune by ear before recording.

Lots is wrong for a model sample for two reasons. The model can learn the
hard-tune edge as part of the voice. And voice services may ask the singer to
record a live phrase that must match the sample (Suno does this to verify the
voice belongs to the person); a heavily corrected sample is less likely to
match.

On a long held note, Some and Lots can sound alike: natural settings still
land the note in tune. The difference is in slides, runs and vibrato, which
Some leaves more alone.

### The key matters more than the knobs

Every guide says to set the key first, because correction in the wrong key
pulls notes toward the wrong targets. Over a backing track the recorder
estimates the key from the track; you can override it with the Key control.
Dry takes default to Chromatic (nearest semitone), which is safe.

### Tone: None or Some

Tone Lots adds a small room. Guides for voice-model samples ask for one dry
voice with minimal reverb, so a room would be learned as part of the voice.
Tone Some (rumble cut, a little presence, gentle leveling) is fine.

## What to record

- A quiet room: no TV, fans, other voices.
- Range over polish: low, middle and high notes, soft and loud, different
  vowels and songs. A narrow sample makes a narrow voice.
- Same distance from the mic every take.
- Gain so the loudest notes peak between -18 and -6 dBFS; a take that touches
  0 dBFS (CLIP) will sound crunchy and should be redone.
- Length: third-party guides disagree (one gives 30 s minimum, 4 min maximum,
  90 s to 2 min as a sweet spot). 2 to 4 minutes of kept takes is safe; check
  the service's current limits before uploading.

## Exporting

Set Pitch and Tone, then click **Export**. The recorder joins the kept takes
(starred, or every take not trashed if nothing is starred) into one 24-bit WAV
in the song's `export` folder and opens Explorer on it. See the README.

## If something still sounds off

Describe it in plain words ("too breathy", "robotic on the slides", "boomy")
to whoever maintains the settings; the knobs live in `plugins.py` (plugin
engines) and `polish.py` (built-in pitch and tone). Bump `RECIPE_VERSION` in
`plugins.py` after changing plugin values so cached renders are redone.

If correction is working hard on every phrase, a new take usually beats more
correction.

## Sources

- Antares, [Auto-Tune Access manual](https://www.fullcompass.com/common/files/87680-AUTOTUNEACC10Manual.pdf)
  and [Auto-Tune Realtime Access manual](https://media.uaudio.com/support/manuals/dd/Auto-Tune%20Realtime%20Access%20Manual.pdf)
- Sound On Sound, [Auto-Tune Access review](https://www.soundonsound.com/node/4920055)
- Antares, [Pitch correction: the complete guide to tuning vocals](https://www.antarestech.com/blog/pitch-correction-the-complete-guide-to-tuning-vocals)
- Slate Digital, [Vocal tuning without the robot voice](https://slatedigital.com/vocal-tuning-without-the-robot-voice-a-producers-guide/)
- Produce Like A Pro, [How to use Auto-Tune](https://producelikeapro.com/blog/how-to-use-auto-tune-vocal-tuning/)
- Auburn Sounds, [Graillon 2 user's guide](https://www.auburnsounds.com/downloads/Graillon%202%20User's%20Guide.pdf);
  MusicRadar, [Tune vocals free with Graillon 2](https://www.musicradar.com/how-to/tune-vocals-free)
- MeldaProduction, [MAutoPitch](https://www.meldaproduction.com/MAutopitch)
- Sonilo, [Suno v5.5 Voices how-to](https://sonilo.com/blog/suno-v5-5-voices-how-to);
  Dubspot, [Suno Voices with your own voice](https://blog.dubspot.com/suno-voices-your-own-voice-2026);
  Jack Righteous, [Suno voice verification troubleshooting](https://jackrighteous.com/de-us/blogs/guides-using-suno-ai-music-creation/why-you-can-t-use-your-own-voice-in-suno-personas-real-explanation)
  and [Suno audio uploads and hybrid workflow](https://jackrighteous.com/es/blogs/guides-using-suno-ai-music-creation/audio-uploads-and-hybrid-workflow-suno-v5)

Guidance from third-party and vendor pages varies by version; treat numbers as
starting points.
