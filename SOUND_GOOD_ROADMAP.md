# Sound good: future directions

Sound good provides optional, offline vocal processing for recorded takes. The
current Pitch and Tone controls (None / Some / Lots each) are described in
the README. `probe-plugins.bat` tests which installed plugins can be hosted
outside a DAW (via pedalboard) before any plugin backend is built. This document
captures possible improvements, not promises or supported integrations.

## Goals

- Make it easy to compare the original recording with processed versions.
- Improve pitch correction and vocal polish while preserving a natural option.
- Keep processing separate from recording so the original take remains intact.
- Keep processed audio aligned with its backing track.

## Ideas to explore

- Evaluate alternative pitch correction and vocal processing algorithms using
  representative audio and listening tests.
- Explore optional support for compatible third-party audio plugins, including
  pitch correction plugins. Investigate hosting, platform compatibility,
  licensing, configuration, and failure handling before choosing an approach.
- Evaluate optional local AI-assisted processing where it offers a meaningful
  improvement. Consider model size, hardware requirements, licensing, and
  processing time. Any network-based service would require a separate, explicit
  product and privacy decision.
- Improve key detection and let listeners make corrections when an estimate is
  uncertain.
- Consider an export action for processed audio and more flexible comparisons
  between versions.

## Design requirements

- Never overwrite the original recording. Processed files should be separate
  and regenerable.
- Preserve the recording's start position, sample rate, and duration so it stays
  aligned with the backing track.
- Keep optional processing backends optional; unavailable plugins or models
  should not prevent access to the original recording.
- Include the relevant backend, version, and settings in cache invalidation so
  changes do not silently reuse an outdated render.
- Perform expensive work away from the interface thread and provide clear
  progress and error messages.
- Test alignment, original-file preservation, cache invalidation, and failure
  handling with synthetic audio. Assess sound quality through listening tests.
