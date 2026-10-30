# 🎬 Muxiveo

Comprehensive software suite for preparing, zero-loss remuxing, and encoding video and audio in MKV and MP4 containers, featuring multi-source acoustic synchronization and a dedicated HDR / Dolby Vision inspection and injection studio.

> **The power of Muxiveo: a unified pipeline in a single click.**  
> Where video workflows typically require juggling 4 or 5 separate tools (a remuxer, an audio editor for delay calculation, a video encoder, a Dolby Vision injector, and an AI interpolation script), Muxiveo unifies the entire pipeline: **you can remux, reorder tracks, edit metadata, acoustically synchronize audio, inject or reconstruct Dolby Vision / HDR10+, transcode (GPU / CPU), and interpolate frames via AI in a single run with one click** — or perform each of these tasks standalone whenever needed.

<div align="center">

[Français](README.md) | **English**

[![Official Website](https://img.shields.io/badge/Website-muxiveo.fr-2ea44f?style=flat-square&logo=googlechrome&logoColor=white)](https://muxiveo.fr/)
[![Version](https://img.shields.io/badge/version-v5.0.3-blue.svg?style=flat-square)](https://github.com/Hydro74000/Muxiveo/releases)
[![Platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20Windows%20%7C%20macOS-informational.svg?style=flat-square)](#-quick-download--installation)
[![License](https://img.shields.io/badge/license-GPL--3.0-green.svg?style=flat-square)](LICENSE)
[![Wiki Documentation](https://img.shields.io/badge/Documentation-Wiki-orange?style=flat-square&logo=gitbook&logoColor=white)](https://github.com/Hydro74000/Muxiveo/wiki)

<br/>

💬 **Discord Community: [https://discord.gg/YGAjNjZHQC](https://discord.gg/YGAjNjZHQC)**  
🌐 **Official Website: [https://muxiveo.fr/](https://muxiveo.fr/)**

<br/>

[![Support via PayPal](https://img.shields.io/badge/Support%20the%20project-PayPal-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/muxiveo)  
*Muxiveo is 100% free, open-source, and ad-free. Your donations support its independent development.*

</div>

---

## 🧭 Quick Workflow Guide

| Your Goal | Muxiveo Module | Processing | Detailed Guide |
|---|---|---|---|
| **Clean up an MKV container** (tracks, languages, titles, chapters, subtitles) | **Container** | Zero-loss stream copy (*Copy*) | [Container & Remux Guide](https://github.com/Hydro74000/Muxiveo/wiki/Interface-et-usage#conteneur--encodage) |
| **Compress, convert codecs, or adjust audio** | **Encoding** | GPU / CPU Encoding | [Video Encoding & HDR Guide](https://github.com/Hydro74000/Muxiveo/wiki/Encodage-video-et-HDR) |
| **Merge a 4K video master with foreign audio/subs** | **Hybrid Studio** | Acoustic FFT alignment (*Zero Delay*) | [Hybridization & Sync Guide](https://github.com/Hydro74000/Muxiveo/wiki/Hybridation) |
| **Inspect, measure, or fix HDR (MaxCLL, DoVi)** | **HDR Studio** | Analysis & metadata edition without re-encoding | [HDR Studio Guide](https://github.com/Hydro74000/Muxiveo/wiki/HDR-Studio) |
| **Transfer Dolby Vision / HDR10+ between files** | **Merge DoVi / HDR10+** | Lossless metadata injection | [DoVi Merge Guide](https://github.com/Hydro74000/Muxiveo/wiki/Interface-et-usage#fusion-dovi--hdr10) |
| **Boost motion fluidity (24 → 60 fps)** | **MVO-RIFE** (Plugin) | GPU Neural Interpolation | [MVO-RIFE Guide](https://github.com/Hydro74000/Muxiveo/wiki/Plugin-MVO-RIFE) |
| **Automate decisions across series or batch folders** | **Profiles** / **CLI** | Decision profiles & scripts | [Profiles Guide](https://github.com/Hydro74000/Muxiveo/wiki/Profils) & [CLI Guide](https://github.com/Hydro74000/Muxiveo/wiki/CLI-headless) |

---

## 📦 Quick Download & Installation

Standalone packages bundling all required external tools (`ffmpeg`, `mediainfo`, `dovi_tool`, `hdr10plus_tool`) are hosted and available for download exclusively on GitHub via the [Releases page](https://github.com/Hydro74000/Muxiveo/releases):

| Operating System | Recommended Package | Getting Started |
|---|---|---|
| **Linux** | **`Muxiveo-x86_64_allinc-*.AppImage`** | Make executable (`chmod +x`) and run. All binaries are bundled. Compatible with *Gear Lever*. |
| **Windows** | **`Muxiveo-Setup-*.exe`** | Complete installer with automated tool environment configuration. |
| **macOS** | **`Muxiveo-*.dmg`** | Native disk image (Intel and Apple Silicon). System tools installed via Homebrew if needed. |
| **Homebrew** | Linux / macOS formula | `brew tap Hydro74000/muxiveo && brew install muxiveo` |

* **Built-in Updates**: A window automatically notifies you at startup when a new version is available (with detailed release notes). Updates can be installed directly with one click from within the application on Windows (installer), Linux (AppImage), macOS (.app in Applications), and from source archives, with automatic configuration backup and safe rollback upon failure. Homebrew, Flatpak, and git installations are updated through their respective package managers.

> [!TIP]
> **System Integration**: On Linux, Windows, and macOS, Muxiveo automatically registers into your file manager's *"Open With..."* context menu.
>
> 📖 For setup troubleshooting or Windows Controlled Folder Access resolution, see the [Installation Guide](https://github.com/Hydro74000/Muxiveo/wiki/Installation) and [Windows Troubleshooting](https://github.com/Hydro74000/Muxiveo/wiki/Troubleshooting-windows).

---

## 🎛️ Primary Usage: A Unified Pipeline or Modular Tools

Muxiveo gives you complete execution freedom based on your workflow needs:
* **All-in-one execution**: Prepare your tracks in **Container**, set video/audio options in **Encoding**, align audio offsets with FFT in **Sync Studio**, or configure Dolby Vision injection, then start the entire production in **a single click** using the unified action button.
* **Or modular standalone tasks**: Perform an instant zero-loss remux in 3 seconds without touching pixels, extract subtitles, transcode a compatibility audio track, or inspect HDR metadata completely independently.

![Muxiveo Container and Track Management](docs/assets/img/Muxiveo-fr-03.png)

### 1. Zero-Loss Remuxing
Prepare clean MKV files in seconds without quality loss:

* **Universal Drag-and-Drop**: Drop single or multiple files (MKV, MP4, MOV, TS, external `.srt` / `.ass` subtitles, standalone audio tracks) directly into the window.
* **Track Selection & Reordering**: Toggle and reorder video, audio, and subtitle tracks to set the exact stream order of the final file.
* **Standardized Metadata Editing**: Set standardized BCP47 language tags (e.g., `en-US`, `fr-FR`), track titles, and Matroska flags (*Default*, *Forced*, *Hearing Impaired*, *Commentary*, *Original*).
* **Native Matroska Engine**: Muxiveo includes its own native EBML muxer to assemble streams directly, preserve complex metadata structures, and optimize speed, with seamless fallback to FFmpeg when required.
* **EIA-608/708 Closed Captions Cleanup**: Detect and strip legacy embedded closed captions from video streams without re-encoding or affecting HDR metadata.
* **Quick Extraction**: Export any audio or subtitle track directly via right-click in the track table.

### 2. Custom Encoding & Hardware Acceleration
When recompression, codec conversion, or size reduction is needed, the Encoding panel applies per-track settings:

![Muxiveo Encoding Panel](docs/assets/img/Muxiveo-fr-08.png)

* **Full Hardware GPU Pipeline**:
  * Hardware decoding and encoding without loading the main CPU: `cuda` for NVENC, `qsv` for Intel QuickSync, `vaapi` for Linux VAAPI, `d3d11va` for AMD AMF.
  * Supported GPU backends: **NVIDIA NVENC / NVEncC** (presets P1 through P7, CQP/VBR rate control), **Intel QSV**, **AMD AMF**, and **Linux VAAPI** (optimized CQP modes and async depth).
* **Reference Software Encoders**: Full integration of `libx265`, `libx264`, and `libsvtav1` (AV1) for maximum compression efficiency.
* **Rate Control & File Size Budgeting**:
  * **Constant Quality**: Constant rate factor (CRF for x264/x265/SVT-AV1, CQP for NVENC/VAAPI).
  * **Target File Size (MiB)**: Set your exact desired final file size. The built-in bitrate calculator automatically budgets video and audio bitrates, distinguishing measured from estimated streams.
  * **Automated 2-Pass**: Automatically enabled in target size mode for software encoders (`x264`, `x265`, `SVT-AV1`).
* **Advanced Audio Management**:
  * Lossless passthrough of high-definition formats: Dolby TrueHD with Dolby Atmos spatial metadata, DTS-HD MA, DTS:X, FLAC.
  * High-quality multichannel transcoding to **E-AC-3**, **AAC**, or **FLAC**.
  * **Independent Audio Variants**: Generate stereo or AC-3 5.1 compatibility tracks from a master TrueHD Atmos track without altering the original source stream.

> 📖 For detailed encoding settings, filters, and quality parameters, see the [Video Encoding and HDR Guide on the Wiki](https://github.com/Hydro74000/Muxiveo/wiki/Encodage-video-et-HDR).

---

## 🔬 Specialized Modules

### 🎙️ Hybrid Studio & Acoustic Synchronization
*Combine the best video master (e.g., 4K UHD video) with audio tracks and subtitles from another release (e.g., localized audio from Blu-ray or TV broadcast).*

![Muxiveo Sync Studio Waveform Viewer](docs/assets/img/Muxiveo-fr-12.png)

* **FFT Acoustic Cross-Correlation**: Muxiveo analyzes the real audio content of both sources to calculate the exact time offset down to the millisecond, independent of container headers. References in any audio layout (stereo ↔ multichannel) are supported by automatically pairing matching languages.
* **Sync to Confirm & Imperceptible Drift**: When continuous analysis hits non-matching segments (differently dubbed scenes, silence gaps), an interactive dialog lets you inspect the zones in Sync Studio or confirm the proposed sync without blockage.
* **Chained References & Track-Specific Sync**: Cascade-synchronize multiple sources (adjusting a reference automatically updates dependent sources, including pitch). Subtitles can also receive their own cuts or cadence without altering audio.
* **Multiple Donors & Assembly Template**: Combine multiple donor files per episode (multilingual audio, various subtitles) and customize languages and titles on a reference episode template applied to the entire season.
* **Superimposed Waveform Viewer**: Dual-color zoomable waveforms (reference audio in green, donor audio in blue), transient timeline navigation, and instant preview playback to verify sync by ear.
* **Frame Rate Cadence Compensation**: Automatically detects and adjusts speed differences (e.g., PAL 25 fps ↔ Cinema 23.976 fps). Automatic mode analyzes pitch to choose between `asetrate` and `atempo`.
* **Cut Detection & Multi-Segment Calibration**: Automatically identifies commercial breaks or TV broadcast edits, slicing the timeline into independent segments calibrated individually.
* **Zero Delay Physical Synchronization**: Physically shifts the audio stream (inserting clean silence or precise trimming) and realigns text subtitles (SRT, ASS, VTT). This eliminates playback drift and desync on TVs and soundbars that ignore container delay tags.
* **Batch Hybridization**: Automatically match and calibrate full seasons of TV shows using filename pattern recognition (`S01E01`), with optimized parallel preparation, calibration, and batch execution.

> 📖 See the [Hybridization and Synchronization Guide](https://github.com/Hydro74000/Muxiveo/wiki/Hybridation) and [Audio Quality Troubleshooting](https://github.com/Hydro74000/Muxiveo/wiki/Synchronisation-qualite).

---

### 🌈 HDR Studio & Dolby Vision
*Inspect, audit, measure, and modify static and dynamic HDR streams.*

![HDR Studio Overview, Luminance, and FEL Analyzer](docs/assets/img/Muxiveo-fr-hdr-montage.png)

* **Full Metadata Audit**: Instant display of HDR10 mastering metadata (MDCV, MaxCLL/MaxFALL) and dynamic Dolby Vision (RPU) and HDR10+ frame data. Flags inconsistencies and generic mastering displays.
* **Real Picture Pixel Measurement**: Measure true MaxCLL and MaxFALL values by scanning video frames (fast keyframe scan in ~20 s, or complete analysis).
* **Luminance & TV Display Simulation**: Interactive luminance curves with simulated television display response across adjustable peak brightness levels (100 to 10,000 nits) to highlight clipping and tone darkening.
* **Lossless Metadata Editing**: Correct static HDR10 tags, realign Dolby Vision L6/L11 metadata, and convert Profile 7 to universal Profile 8.1 without re-encoding.
* **FEL Analyzer & FEL Integration (mvo-fel Plugin)**:
  * Analyze the true visual contribution of the Full Enhancement Layer (Profile 7 FEL) across tone, highlights, detail, and grain.
  * Full-resolution A/B split-screen viewer with zoom and pixel difference heatmaps.
  * Reconstruct the full 12-bit image (Base Layer + Enhancement Layer) during encoding to preserve all master details.
* **DoVi / HDR10+ Metadata Merge**: Inject dynamic metadata from one source into another HEVC video stream with strict frame-count validation.

> 📖 For full control references and measurement tools, see the [HDR Studio Guide](https://github.com/Hydro74000/Muxiveo/wiki/HDR-Studio) and the [Merge FEL Plugin Guide](https://github.com/Hydro74000/Muxiveo/wiki/Plugin-Merge-FEL).

---

### 🏎️ AI Frame Interpolation (MVO-RIFE)
*Boost video frame rates using deep learning neural networks with GPU acceleration.*

* **Target Frame Rates**: Fixed multipliers (×2, ×3, ×4) or cinema-to-high-rate conversion (23.976/24 → 59.94/60 fps via ×2.5 factor).
* **Hybrid Motion Engine**: Combines the RIFE v4 model, block motion compensation, and NVIDIA Optical Flow (NVOF). A trained selector chooses the best candidate per zone to prevent edge warping on repetitive patterns and fast pans.
* **TensorRT Acceleration**: Optional plugin harnessing NVIDIA RTX Tensor Cores for high-speed inference.

> 📖 See the [MVO-RIFE Interpolation Guide](https://github.com/Hydro74000/Muxiveo/wiki/Plugin-MVO-RIFE) and [TensorRT Acceleration](https://github.com/Hydro74000/Muxiveo/wiki/Plugin-TensorRT).

---

### 🧩 Extension Ecosystem (muxiveo-plugins)
*Independent distribution and lifecycle management of specialized modules.*

Advanced specialized features (including the **MVO-RIFE** frame interpolation engine, **NVIDIA TensorRT** acceleration, and the **Merge FEL** 12-bit reconstruction pipeline) are developed and maintained as modular extensions.

* **Official Repository**: All extensions are packaged, versioned, and hosted on the dedicated GitHub repository [Hydro74000/muxiveo-plugins](https://github.com/Hydro74000/muxiveo-plugins).
* **Integrated One-Click Management**: From the **Extensions** page in Muxiveo's sidebar, you can install, update, or remove these modules directly inside the GUI without restarting the application. Muxiveo automatically validates hardware compatibility and checks binary integrity using SHA-256 signatures.
* **CLI Management**:
  ```bash
  muxiveo --cli plugins list
  muxiveo --cli plugins install mvo-rife
  muxiveo --cli plugins install mvo-rife-trt --accept-license
  muxiveo --cli plugins update
  ```

> 📖 See the [Complete Plugins & Extensions Guide on the Wiki](https://github.com/Hydro74000/Muxiveo/wiki/Plugins).

---

### 🏷️ TMDB Metadata & NFO Files
* **Integrated TMDB Search**: Query The Movie Database directly for films and TV shows with automated episode identification.
* **Standardized Matroska Tags**: Automatically write official tag elements (`DATE_RELEASED`, `GENRE`, `DIRECTOR`, `CAST`, `SYNOPSIS`, `SEASON`, `EPISODE`).
* **High-Resolution Artwork**: Download and embed official cover posters directly as MKV attachments.
* **NFO File Generation**: Automatically output technical metadata `.nfo` files alongside exports, ready for Plex, Jellyfin, and Kodi.

---

### 🎯 Decision Profiles & Automation
*Automate track selection and renaming rules across directories.*

![Muxiveo Decision Profile Editor](docs/assets/img/Muxiveo-fr-14.png)

* **Reusable Conditional Rules**: Define custom selection criteria (e.g., *"Select highest quality VO Atmos and localized 5.1 audio, discard commentary tracks, flag matching forced subtitles"*).
* **Output Name Template (`output_template`)**: Define dynamic file naming templates (e.g., `{title} ({year}) [{resolution} {hdr_format}]`) that automatically adapt based on selected tracks and TMDB metadata, available in the editor, Container, Hybrid Studio, and CLI.
* **Pattern-Based Renaming**: Standardize track titles using dynamic tokens (e.g., `{lang_name} {codec} {channels}`).
* **Low-Code Visual Editor**: Build and test profiles with live preview in the GUI, or apply them via headless CLI.

> 📖 See the [Complete Profiles Guide](https://github.com/Hydro74000/Muxiveo/wiki/Profils).

---

## 💻 Headless Command-Line Interface (CLI)

Muxiveo includes a full-featured CLI without graphical interface, designed for NAS servers, Docker containers, and automated workflows:

![Muxiveo CLI Terminal](docs/assets/img/Muxiveo-cli-terminal.png)

```bash
# Clean remux with TMDB metadata and decision profile
muxiveo --cli remux -i source.mkv -o clean_remux.mkv --profile "Standard" --auto-tmdb

# Automated hybridization of two sources with acoustic FFT alignment
muxiveo --cli hybrid --ref video_4k.mkv --donor audio_vf.mkv -o output/ --auto-sync

# Batch processing across an entire TV season folder
muxiveo --cli batch --profile "Series" --input-dir "Season 01" --output-dir "Output" --recursive --auto-tmdb

# Detailed HDR inspection and Dolby Vision FEL analysis
muxiveo --cli inspect master.mkv --hdr measure --hdr-fel
```

> 📖 For the full argument reference and JSON schemas, see the [Headless CLI Documentation](https://github.com/Hydro74000/Muxiveo/wiki/CLI-headless).

---

## 🛠️ Running from Source

To run or develop Muxiveo from the Python source code:

```bash
# 1. Clone repository
git clone https://github.com/Hydro74000/Muxiveo.git
cd Muxiveo

# 2. Install Python dependencies and verify system tools
python3 setup.py

# 3. Launch application
python3 main.py
```

*Requirements: Python 3.10+ and PySide6 6.6.0+.*

---

## 💬 Community & Support

Muxiveo is an independent project, **100% free, open-source, and developed without any ads**.

<div align="center">

### Join the Discord Community

Questions, suggestions, remux workflows, or profile sharing:

[![Join Discord](https://img.shields.io/badge/Discord-Join%20the%20community-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/YGAjNjZHQC)

---

### Support the Project

If Muxiveo saves you time and you wish to support its ongoing development:

[![Support via PayPal](https://img.shields.io/badge/Support%20via-PayPal-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/muxiveo)

<a href="https://paypal.me/muxiveo">
  <img src="https://raw.githubusercontent.com/stefan-niedermann/paypal-donate-button/master/paypal-donate-button.png" alt="Donate with PayPal" width="160" />
</a>

</div>

---

## 📄 License & Credits

Distributed under the **GNU General Public License v3.0**. See [LICENSE](LICENSE) for details.  
Muxiveo relies on essential open-source tools: `FFmpeg`, `MediaInfo`, `dovi_tool`, `hdr10plus_tool`, `NVEncC`, and `PySide6`. See `NOTICE` and `SOURCES.md`.
