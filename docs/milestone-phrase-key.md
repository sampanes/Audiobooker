# Milestone: phrase loops and per-phrase key

Status: planned, not implemented.

Make it easy to sing the same part of a backing track several times, and tune
each take to the key of the part that was sung instead of one key for the
whole song.

## How it works today

- **Picking a spot.** The track line has two fields: **from** (where singing
  starts, typed as `m:ss`, e.g. `1:05`) and **count-in** (seconds of track
  heard before that point). There is no timeline to click or scrub.
- **Repeating a phrase.** Press Space, sing, press Space. Every take starts
  from the same **from** with the same count-in, so repeating a phrase is
  just Space again. A take runs until Space is pressed or the track ends; it
  does not stop at the end of the phrase by itself.
- **Key.** One key per song, stored in `song.json`. Loading a track estimates
  it once from the whole track (`polish.estimate_key`: a pitch-class profile
  matched against major and minor key profiles). An uncertain estimate falls
  back to Chromatic. The Key control overrides it. Dry songs use Chromatic.
- **Rendering.** Pitch correction uses the song's key at render time. The key
  is part of the cache hash, so changing it re-renders.

## Problems

1. A song that changes key, or a bridge that borrows from another key, gets
   one compromise key. Correction in the wrong key pulls notes toward wrong
   targets, which is the most audible failure of pitch correction.
2. Finding a phrase means typing times. There is no way to hear the track and
   mark "here" while listening.
3. A take never ends at the phrase end, so a repeat-the-phrase session needs
   a Space press at the right moment every time.

## Where the key should come from

The idea of reading the key from the phrase is right; the better source is
the **backing track under that phrase**, with the voice as a backup:

- The track has chords and bass that spell the key clearly. A short sung
  phrase has a handful of notes, and an unsteady singer's off notes would vote
  on the key, which is circular when the point is to correct those notes.
- The track section is the same for every take of a phrase, so every repeat
  is tuned to the same key, and the estimate can be computed once per section.
- Yes, this handles key changes: a take over the last chorus after a
  modulation is analyzed from the last chorus, not the whole song.
- Major versus relative minor (C major and A minor) is the classic
  confusion, and it is harmless here: both use the same seven notes, so
  correction targets are identical.

Dry takes (no track) keep using Chromatic; reading a key from the voice alone
was considered and left out (see Not in scope).

The track never ends up in a recording: it plays only in the headphones, so
the per-take key and the loop change how takes are tuned and captured, not
what the raw take contains.

## Plan

### Step 1: per-take key from the track section

- When a take over a track is saved, estimate the key from the track between
  the take's start (`track_start`) and its end, widened by a few seconds on
  each side when the section is short. Store `key` and `key_confidence` in the
  take's metadata in `takes.json`.
- Below a confidence threshold, use the song key; if that is also uncertain,
  Chromatic.
- Key control gains an **Auto (per take)** choice, the default for songs with
  a track. Picking a specific key keeps meaning "use this for every take".
- The take list gets a **KEY** column (e.g. `A minor`, `Chromatic`), so a wrong
  estimate is easy to spot; the line under the status band also names the key
  a take is played in.
- Run the estimate on the existing background worker, never the Tk thread.
  Cache by (track file, section start, section end) so repeats of a phrase
  cost nothing.
- Takes recorded before this lands keep using the song key.

### Step 2: scrub timeline

A track strip across the window, shown when a song has a track. Interaction
model borrowed from an existing waveform timing-review tool (a separate
browser app); rebuilt here on a Tk `Canvas`, no shared code.

- **Overview strip:** the whole track as a waveform (peaks precomputed once
  with numpy, about 1000 buckets), a playhead line, the count-in shaded before
  **from**, the **from/to** region highlighted, and faint marks where existing
  takes start.
- **Click** anywhere outside the region handles: seek there and play the track
  (preview only; nothing is recorded). Click again or P stops.
- **Drag** the region's start or end handle to set **from** / **to**. Handles
  get a wide grab zone (about 18 px) so they are easy to catch.
- **Zoom strip** below it: just the selected region plus a little either side,
  with the same draggable handles, for placing them precisely between words.
- **Set from the playhead:** `[` sets **from** and `]` sets **to** at the
  current play position, so a phrase can be marked by ear while listening.
- The typed **from** field stays and stays in sync, for exact times.
- A phrase region needs at least a second or two; dragging handles past each
  other swaps them instead of failing.

### Step 3: hands-free phrase loop

With **from** and **to** set, Space starts a loop instead of one take:

1. count-in, record from **from**, stop by itself at **to** plus a short tail
   (so the last note is not clipped);
2. a short pause, then count-in again and the next take;
3. repeat until Space is pressed.

- Every pass is its own numbered take, judged later (or during) with G / B as
  usual. G or B pressed during a pass judges that pass, ends it early, and the
  loop carries on with the next one.
- Space during a pass ends the loop; the take in progress is kept, like any
  take stopped with Space.
- Safety: the loop ends by itself after a set number of passes (e.g. 20) or
  after two passes in a row with no singing, so walking away does not fill the
  disk with silent takes.
- The status band shows the pass number (`LOOP 3`) and the count-in.

## Acceptance tests (synthetic audio)

- A track in one key for its first half and a different key for its second
  half: a take over the second half is tuned to the second key, a take over the
  first half to the first.
- A section too short or too ambiguous to judge uses the song key, and a fully
  ambiguous song uses Chromatic.
- Relative major and minor estimates produce identical correction targets.
- A manually picked key wins over every estimate, per take and per song.
- Repeating the same from/to region estimates once and reuses the result.
- A take with **to** set stops by itself, keeps its tail, and stays aligned to
  the track timeline exactly as takes do today.
- A loop of N passes makes N takes, each aligned to the track; Space mid-pass
  ends the loop and keeps that take; two silent passes end the loop.
- Timeline mapping is exact both ways (time to pixel to time) at overview and
  zoom scales; dragged handles write the same values as typed times.
- Changing a take's key re-renders its polished file; raw takes are never
  touched.

## Risks and open questions

- How short a section can still give a reliable key, and how much context to
  borrow from either side. Measure with real tracks before fixing numbers.
- Songs with heavy borrowed chords or modal passages may never be confident;
  falling back is the right answer there, not forcing a guess.
- Previewing the track while the mic is held in exclusive mode uses the same
  output path as playback over a track today; confirm the strip's click-to-play
  does not interrupt the input stream.
- The window grows by two strips; check it still fits on a laptop screen, and
  hide the strips for songs without a track.

## Not in scope

- Bundled music. Tracks are always imported by the user.
- Estimating the key from the singer's voice for dry takes. Chromatic with
  Pitch Some is good enough there.
- Note-by-note melody targets (correcting toward the song's actual melody
  rather than the nearest note of the scale).
- Any network service.
