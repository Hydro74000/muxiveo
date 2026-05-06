# NVEncC and automated audio synchronization

This release is led by two major additions: a new **NVEncC encode backend** for NVIDIA users, and a new **automated audio synchronization workflow** for remux projects.

NVEncC brings rigaya's standalone NVENC pipeline into Mediarecode with dedicated codec detection, progress reporting, HDR-aware command building and advanced encoder controls. The new audio sync feature analyzes real audio content with ffmpeg/ffprobe signatures, compares a target track to a selected reference, and applies the computed offset directly in the remux panel.

The release also hardens Dolby Vision / HDR Matroska handling across complex encode/remux paths: Profile 7 routing, native Matroska metadata patching, frame-count checks, static HDR SEI injection, and new audit/debug tools all landed together with expanded regression coverage.

## Highlights

- Add **NVEncC / rigaya backend support** for HEVC, H.264 and AV1, including GPU capability detection, pipeline construction, progress parsing, intermediate MKV handling and advanced NVEncC parameter sanitization
- Add **automated audio-content synchronization** from the remux panel, based on ffmpeg/ffprobe signatures, correlation against a selected reference track, confidence scoring and automatic offset application
- Add a reusable **advanced encoder parameters dialog** covering NVEnc, NVEncC, AMF, QSV, VAAPI, libx264, libx265 and SVT-AV1 with codec-specific serialization
- Improve **Dolby Vision profile detection and routing**, including Profile 7 decisions, RPU extraction/injection paths, compatibility ID handling and container-level Dolby Vision signaling
- Add pure-Python **EBML / Matroska tooling** for native muxing, HEVC access-unit splitting, timestamp reading and Dolby Vision `BlockAdditionMapping` injection
- Add **static HDR10 SEI injection** and HEVC SEI normalization helpers to preserve HDR metadata in encoded HEVC streams
- Add **frame-count guardrails** around encode metadata injection so mismatched source/output frame counts are detected early
- Improve remux UI behavior with editable track timing, audio sync actions, chapter updates and shared panel styling
- Add audit and repro utilities: `audit_mkv.py`, `scripts/patch_hevc_trail_n.py` and `scripts/prepare_plex_repro_variants.py`
- Extend setup/config/AppImage packaging to detect and bundle **NVEncC**, including Windows tool path handling and Linux package support
- Expand automated tests across NVEncC, HDR metadata, Dolby Vision Matroska mapping, native muxing, audio sync, remux UI, setup/config and packaging helpers

## Why it matters

- NVIDIA users get a dedicated NVEncC path for advanced encode controls and HDR-aware workflows.
- Remux projects with drifting or offset 5.1/7.1 audio can now compute offsets from the media itself instead of relying only on manual timing.
- Dolby Vision and HDR10+ projects are less likely to lose critical metadata during intermediate wraps, final muxing or ffmpeg copy stages.
- The new Matroska primitives reduce dependency on external muxing tools for targeted metadata fixes.
- The added tests make the HDR / DoVi encode path much safer to evolve.

## Included commits

- `4eb01ce` feat(tests): add comprehensive tests for Matroska HEVC AU splitter, native muxer, and timestamp reader
- `77f8f02` Refactor UI styles and components for better modularity
- `0236c7c` nvencc updates
- `acd17fe` Add audio synchronization workflow and related tests
