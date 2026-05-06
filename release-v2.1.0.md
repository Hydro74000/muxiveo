# NVEncC, Dolby Vision Matroska hardening and audio sync

This release extends Mediarecode with a new **NVEncC encode backend**, deeper **Dolby Vision / HDR Matroska handling**, an explicit **audio synchronization workflow** for remux, and a broader advanced-parameters UI for encoder tuning.

The core work is focused on keeping HDR metadata and track timing intact through complex encode/remux paths: Dolby Vision profile routing, native Matroska metadata patching, frame-count checks, static HDR SEI injection, and new audit/debug tools all landed together with expanded regression coverage.

## Highlights

- Add **NVEncC / rigaya backend support** for HEVC, H.264 and AV1, including GPU capability detection, pipeline construction, progress parsing, intermediate MKV handling and advanced NVEncC parameter sanitization
- Add a reusable **advanced encoder parameters dialog** covering NVEnc, NVEncC, AMF, QSV, VAAPI, libx264, libx265 and SVT-AV1 with codec-specific serialization
- Improve **Dolby Vision profile detection and routing**, including Profile 7 decisions, RPU extraction/injection paths, compatibility ID handling and container-level Dolby Vision signaling
- Add pure-Python **EBML / Matroska tooling** for native muxing, HEVC access-unit splitting, timestamp reading and Dolby Vision `BlockAdditionMapping` injection
- Add **static HDR10 SEI injection** and HEVC SEI normalization helpers to preserve HDR metadata in encoded HEVC streams
- Add **frame-count guardrails** around encode metadata injection so mismatched source/output frame counts are detected early
- Add explicit **audio-content synchronization** from the remux panel, based on ffmpeg/ffprobe signatures and correlation against a selected reference track
- Improve remux UI behavior with editable track timing, audio sync actions, chapter updates and shared panel styling
- Add audit and repro utilities: `audit_mkv.py`, `scripts/patch_hevc_trail_n.py` and `scripts/prepare_plex_repro_variants.py`
- Extend setup/config/AppImage packaging to detect and bundle **NVEncC**, including Windows tool path handling and Linux package support
- Expand automated tests across NVEncC, HDR metadata, Dolby Vision Matroska mapping, native muxing, audio sync, remux UI, setup/config and packaging helpers

## Why it matters

- NVIDIA users get a dedicated NVEncC path for advanced encode controls and HDR-aware workflows.
- Dolby Vision and HDR10+ projects are less likely to lose critical metadata during intermediate wraps, final muxing or ffmpeg copy stages.
- Remux projects with drifting or offset 5.1/7.1 audio can now compute offsets from the media itself instead of relying only on manual timing.
- The new Matroska primitives reduce dependency on external muxing tools for targeted metadata fixes.
- The added tests make the HDR / DoVi encode path much safer to evolve.

## Included commits

- `4eb01ce` feat(tests): add comprehensive tests for Matroska HEVC AU splitter, native muxer, and timestamp reader
- `77f8f02` Refactor UI styles and components for better modularity
- `0236c7c` nvencc updates
- `acd17fe` Add audio synchronization workflow and related tests
