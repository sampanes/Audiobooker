# Take Recorder

> **Plugin probe results** are in `plugin-probe/report.7z`, password-protected
> because the report lists every plugin installed on that PC. Extract with
> `"C:\Program Files\7-Zip\7z.exe" x plugin-probe\report.7z -oplugin-probe`
> (it asks for the password).
>
> To probe again, double-click `probe-plugins.bat` on the PC with the plugins.
> It checks whether Auto-Tune, Graillon, MAutoPitch, Melda, Valhalla and
> iZotope plugins work *outside* FL Studio, writes `plugin-probe/report-<date>.md`
> (git-ignored), and saves what each plugin did to a test voice in
> `plugin-probe/out/`. To share a new report, encrypt it (names hidden too) and
> commit only the `.7z`:
> `cd plugin-probe` then
> `"C:\Program Files\7-Zip\7z.exe" a -t7z -mhe=on -p report.7z report-<date>.md`

Record yourself singing, keep the good takes, trash the bad ones -- all from the
keyboard, without stopping to fiddle with a DAW.

Takes are 24-bit WAV files written sample-for-sample as the audio interface
delivers them (WASAPI exclusive mode: no Windows mixer, no resampling, no
"enhancements"). Built and tested with a PreSonus Studio 26c, but any interface
works -- see `device_match` below.

## Run it

Double-click `start.bat` (or run it from cmd). The first run creates `.venv` and
installs the pinned dependencies; after that it just launches.

| Script       | What it does                                                        |
|--------------|---------------------------------------------------------------------|
| `start.bat`  | Starts the app. Running it again replaces the old copy.             |
| `stop.bat`   | Closes the app (finishing any take in progress), force-kills if hung. |
| `status.bat` | Says whether it's running. Exit code 0 = running.                   |

The app runs in a minimized console window titled `take-recorder`.

## Keys

| Key    | Action                                                                     |
|--------|----------------------------------------------------------------------------|
| Space  | Record / stop                                                              |
| G      | GOOD: star the take (press again to unstar; on a trashed take, restores it)  |
| B      | BAD: trash the take                                                        |
| P      | Play / stop the selected take (newest if none selected)                    |
| Ctrl+Z | Undo the last trash                                                        |
| V      | Cycle pitch correction for playback: None / Some / Lots                    |
| T      | Cycle tone polish for playback: None / Some / Lots                         |
| O      | Cycle where playback goes: Headphones / Speakers / Both                    |

G and B judge, in order: the take you are recording (stopping it first), the
take selected in the list, or the take you just finished. A new take is
selected as soon as it stops, so Space then G or B always judges what you just
sang. Clicking an older take lets G or B judge that one instead.

The big status band says what is happening (with a thin progress bar during
playback, the count-in and a take over a track), and the line under it names
the take G or B will judge.

Things you set once (record input, Hz, where playback goes, latency
calibration, interface details) live behind **Settings**. The dot beside it is
green when the interface is connected bit-perfect; if the interface needs
attention, a line under the header says why.

The bar at the top of the window says where your keystrokes go: green **KEYS ON**
means they control the recorder, yellow means you are typing in a text box, and
when another window has the keyboard the recorder fades out.

Raw recordings are never deleted: trashed takes move to `takes/<song>/trash/`.
Take numbers are never reused, so `take_007.wav` always means the same take.
Generated pitch/tone cache files are disposable and may be pruned or regenerated.

**Play on** (in Settings, or O) sends playback to the interface's headphones, to other speakers
(say, a motherboard line-out), or to both at once. Pick the speakers once from
the list; switching while a take plays restarts it on the new output.
Recording over a backing track always plays the track in the headphones, so
it never leaks into the mic.

Each take starts 0.3 s *before* you press Space (pre-roll), so the breath or
pickup isn't cut off. (Takes over a backing track use the count-in instead.)

## Singing over a backing track

1. **Load track...** a track (WAV, MP3, FLAC, OGG or AIFF). It is copied into the
   song folder, so the folder holds everything for that song.
2. Set **from** (e.g. `1:05` to work on the second verse) and **count-in**
   (seconds of track you hear before that point) on the track line.
3. Press Space. The track plays in your headphones from the count-in; the
   status bar counts down, then shows where you are in the track. The take
   stops by itself when the track ends.

Takes are saved **on the track's timeline**: the WAV starts with silence up to
where you began singing, so dropping the track and any take into FL Studio (or
any DAW) at 0:00 lines them up. Playing a take back (P) plays it over the
track. Untick **Play while recording** to record dry for a moment.

**Calibrate once.** The app can measure when the track left the computer and
when your voice arrived, but not the time spent inside the interface and its
converters. Click **Calibrate...** in Settings, hold one headphone ear cup against the mic,
and it plays 8 clicks and measures the true delay. Redo it if you change the
interface, headphones, or the Hz setting. Until then the track line shows "not
calibrated" and takes may sit a few milliseconds late.

## Crash safety

Audio is written to disk as you sing. If the app, Windows or the power dies
mid-take, the WAV is left with a header that says "empty" -- the next time the
song is opened, the app repairs the header, rebuilds the take's stats and
marks it `RECOVERED` in the list.

## Getting the best sound

- **Gain:** set it on the interface so your loudest notes peak in the shaded
  zone of the meter (-18 to -6 dBFS). 24-bit has plenty of headroom; a take
  that peaks at -12 loses nothing. The CLIP light latches if anything hits
  -0.1 dBFS -- click it to reset.
- **Monitoring:** listen through the interface's hardware monitor/mix knob.
  It is zero-latency; the app does no software monitoring.
- **Sample rate:** leave Hz on `auto`. The interface runs one clock for
  input and output, so the app records at whatever rate Windows runs the
  interface at (Sound settings, `mmsys.cpl` -> device -> Advanced); that is
  what lets the track play while the mic is held exclusively. 44.1 and 48 kHz
  at 24-bit are both full quality. A fixed rate that differs from Windows
  works for dry takes, but the input pauses during playback and backing
  tracks can't play. If you change the Windows format, click Reconnect in Settings.
- Keep "Allow applications to take exclusive control" checked on the
  interface's recording device in Windows Sound settings.
- **Dropout flag:** a take marked `DROPOUT` lost audio (the computer couldn't
  keep up). Redo it.
- **Cloudlifter-style inline preamps** run on the interface's 48V phantom power
  (leave it on) and turn it into ~25 dB of clean gain for a dynamic mic. The
  48V stops at the Cloudlifter and never reaches the mic, so use one with
  dynamic mics only -- a condenser needs phantom at the mic itself.

## Pitch and tone playback

Two controls under Play change how a take *plays back*; the recording itself
is never touched.

- **Pitch** (V): **None** leaves your notes alone. **Some** pulls each note
  halfway to the nearest note in the Key, and eases into it, so it still sounds
  like you. **Lots** snaps fully and fast.
- **Tone** (T): **None** leaves the sound alone. **Some** cuts low rumble, lifts
  presence a little and compresses gently. **Lots** does more of each and adds
  a small amount of room.

Both on None plays the untouched recording. Any other mix is rendered once,
cached, and played; while it renders the status band says `POLISHING`, and
the line under it names what you are hearing. All of this processing is done
by this app's own code (`polish.py`), offline: no plugins, no network, no AI.

The Key control limits pitch correction to a major or minor scale. Loading a
backing track estimates its key once; uncertain estimates fall back to
Chromatic. You can override the estimate at any time. Dry songs default to
Chromatic.

Polishing never writes to a raw `take_NNN.wav`. It renders a separate 24-bit
file under `takes/<song>/polished/`, keeping the exact frame count, sample rate,
channel count and leading timeline silence so playback stays aligned with the
backing track. The cache is regenerated when the take, pitch or tone level,
key or processing recipe changes, and orphaned cache files are removed when the
song opens.

## Settings

`settings.json` is created on first run (and is git-ignored):

| Key               | Default                       | Meaning                                    |
|-------------------|-------------------------------|--------------------------------------------|
| `song`            | today's date                  | Current song folder                        |
| `input`           | `"1"`                         | Record input `1`, `2`, or `1+2` (stereo)   |
| `samplerate`      | `"auto"`                      | `auto` (match Windows) or 44100 / 48000 / 96000 |
| `device_match`    | `["Studio 26c", "PreSonus"]`  | Substrings that identify your interface    |
| `preroll_seconds` | `0.3`                         | Audio kept from before you press Space     |
| `takes_dir`       | `"takes"`                     | Where song folders go (relative or absolute) |
| `latency_ms`      | `null`                        | Set by Calibrate                           |
| `track_gain`      | `0.7`                         | Track level under the vocal when playing back |
| `polish`          | `"off"`                       | Playback pitch and tone: `off`, or `<pitch>-<tone>` with each `none`/`some`/`lots` (e.g. `lots-none`). Old `gentle`/`strong` still load. |
| `playback`        | `"headphones"`                | Play on: `headphones`, `speakers`, or `both` |
| `speakers`        | `""` (best guess)             | Output name used for speakers              |

Per-song track settings (track file, start point, count-in and vocal key) live
in `takes/<song>/song.json`; per-take details live in
`takes/<song>/takes.json`.

## Layout

```
app.py       Tkinter window, hotkeys, meter
engine.py    Input stream, take writer, backing track + alignment, playback
polish.py    Offline pitch correction, key estimation and vocal polish
takes.py     Song folders, metadata, crash recovery and polished cache
tests/       python -m unittest discover -s tests -t .
```

Dependencies are pinned exactly in `requirements.txt`.
