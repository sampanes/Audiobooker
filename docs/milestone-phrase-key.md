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

The voice is still useful for dry takes, where there is no track: a pitch
track of the take (note histogram weighted by duration) can suggest a key when
it is confident, and fall back to Chromatic when it is not.

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
- The line under the status band names the key a take is played in.
- Run the estimate on the existing background worker, never the Tk thread.
  Cache by (track file, section start, section end) so repeats of a phrase
  cost nothing.

### Step 2: loop a phrase

- Add a **to** time beside **from**. With both set, a take stops by itself at
  **to** (plus a short tail so the last note is not clipped), and the next
  Space records the same phrase again.
- While the track plays, hotkeys set **from** and **to** at the current
  position (e.g. `[` and `]`), so a phrase is found by ear instead of typing.
- Optional: a thin track strip above the takes showing the whole track, the
  from/to region and each take's span; clicking the strip sets **from**.

### Step 3: key from the voice for dry takes

- For takes without a track, estimate the key from the take's own pitch track.
  Use it only above a stricter confidence threshold and only when the take is
  long enough; otherwise Chromatic.
- Never override a key the user picked.

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
- Changing a take's key re-renders its polished file; raw takes are never
  touched.

## Risks and open questions

- How short a section can still give a reliable key, and how much context to
  borrow from either side. Measure with real tracks before fixing numbers.
- Songs with heavy borrowed chords or modal passages may never be confident;
  falling back is the right answer there, not forcing a guess.
- Whether the track strip earns its space, or the `[` `]` hotkeys are enough.
- Older takes recorded before this lands have no stored key: compute it on
  first play, or keep using the song key for them.

## Not in scope

- Bundled music. Tracks are always imported by the user.
- Note-by-note melody targets (correcting toward the song's actual melody
  rather than the nearest note of the scale).
- Any network service.
