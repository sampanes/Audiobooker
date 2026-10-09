# Take Recorder

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

| Key          | Action                                                         |
|--------------|----------------------------------------------------------------|
| Space        | Record / stop                                                  |
| G            | Last take was GOOD: star it (while recording: stop + star)     |
| B            | Last take was BAD: trash it (while recording: stop + trash)    |
| R            | Retake: trash the take you just did and start recording again  |
| Backspace    | Trash the selected take (while recording: abort this take)     |
| Ctrl+Z       | Undo trash                                                     |
| P / Enter    | Play / stop the selected take (newest if none selected)        |
| S            | Star a keeper                                                  |
| Esc          | Stop playback                                                  |

Nothing is ever deleted: trashed takes move to `takes/<song>/trash/`. Take
numbers are never reused, so `take_007.wav` always means the same take.

Each take starts 0.3 s *before* you press Space (pre-roll), so the breath or
pickup isn't cut off.

## Getting the best sound

- **Gain:** set it on the interface so your loudest notes peak in the shaded
  zone of the meter (-18 to -6 dBFS). 24-bit has plenty of headroom; a take
  that peaks at -12 loses nothing. The CLIP light latches if anything hits
  -0.1 dBFS -- click it to reset.
- **Monitoring:** listen through the interface's hardware monitor/mix knob.
  It is zero-latency; the app does no software monitoring.
- **Windows format (recommended):** in Sound settings (`mmsys.cpl`), set both
  the interface's playback and recording devices to 24 bit at the same sample
  rate the app uses (48000 Hz by default) under Properties -> Advanced. The
  interface runs one clock for input and output, so if Windows' output is at a
  different rate, the app has to pause the input meter while playing a take back.
  Keep "Allow applications to take exclusive control" checked on the
  recording device.
- **Dropout flag:** a take marked `DROPOUT` lost audio (the computer couldn't
  keep up). Redo it.
- **Cloudlifter-style inline preamps** run on the interface's 48V phantom power
  (leave it on) and turn it into ~25 dB of clean gain for a dynamic mic. The
  48V stops at the Cloudlifter and never reaches the mic, so use one with
  dynamic mics only -- a condenser needs phantom at the mic itself.

## Settings

`settings.json` is created on first run (and is git-ignored):

| Key               | Default                       | Meaning                                    |
|-------------------|-------------------------------|--------------------------------------------|
| `song`            | today's date                  | Current song folder                        |
| `input`           | `"1"`                         | Record input `1`, `2`, or `1+2` (stereo)   |
| `samplerate`      | `48000`                       | 44100 / 48000 / 96000                      |
| `device_match`    | `["Studio 26c", "PreSonus"]`  | Substrings that identify your interface    |
| `preroll_seconds` | `0.3`                         | Audio kept from before you press Space     |
| `takes_dir`       | `"takes"`                     | Where song folders go (relative or absolute) |

## Layout

```
app.py       Tkinter window, hotkeys, meter
engine.py    Input stream, take writer, playback
takes.py     Song folders, trash/restore, take metadata (takes.json)
tests/       python -m unittest discover -s tests -t .
```

Dependencies are pinned exactly in `requirements.txt`.
