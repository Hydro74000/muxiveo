# Multi-track encode orchestration, verbose logging and remux hardening

This release brings a major upgrade to the encode pipeline with **multi-track video orchestration**, stronger **hardware encoder handling**, richer **tool logging**, and a solid **remux workflow refactor**.

From encode planning to final muxing, Mediarecode now handles complex video setups more cleanly, with better per-track visibility, safer resource management, and improved metadata consistency across the workflow.

## Highlights

- Add **multi-track video encode orchestration** with per-track plans, previews and final mux ordering
- Improve parallel video preparation with **resource and RAM guards**
- Add clearer **per-track progress reporting** and UI plan badges
- Strengthen hardware encoder support for **VAAPI, QSV, NVENC and AMF**
- Improve HDR detection and keep HDR-related options scoped per video track
- Add **verbose file logging**, rotation and external tool output capture
- Refactor remux internals to better align attachments, metadata and offsets
- Expand test coverage across encode, remux, config, packaging and runtime services

## Why it matters

- Better support for advanced projects with multiple video tracks
- Easier troubleshooting when ffmpeg, mkvmerge or hardware tools behave unexpectedly
- More consistent results between encode preparation and remux output
- Stronger confidence in future changes thanks to much broader automated test coverage

## Included commits

- `eaa2b45` feat: add multi-track video encode orchestration and verbose tool logging
- `da4260e` feat: Add tests for encode runtime services and big refactor remux workflow
