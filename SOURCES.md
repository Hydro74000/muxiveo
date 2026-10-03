# Source Code Availability — GPL / LGPL Compliance

This document fulfills the written offer requirement of the GNU General Public
License (GPL) and GNU Lesser General Public License (LGPL) for bundled binaries
distributed with Muxiveo.

---

## FFmpeg / FFprobe (GPL v2 or later)

The AppImage and Windows All-in-One distributions (`--allinc` mode) bundle
statically compiled or dynamic FFmpeg binaries that include GPL-licensed
components (libx264, libx265, and others). As required by GPL §3, the complete
corresponding source code is available at:

- **FFmpeg source code:** https://ffmpeg.org/download.html
- **Build scripts and configuration:** https://github.com/BtbN/FFmpeg-Builds
  (the exact build used is the `gpl` static variant from BtbN/FFmpeg-Builds)

The bundled FFmpeg binary version can be identified by running:
```
ffmpeg -version
```
or via the Muxiveo dashboard (tool badges).

---

## Other bundled tools (MIT / BSD — no source distribution required)

The following tools have permissive licenses that do not require source
distribution, but their source code is publicly available:

| Tool | Repository | Notes |
|---|---|---|
| dovi_tool | https://github.com/quietvoid/dovi_tool | Dolby Vision metadata tool (MIT) |
| hdr10plus_tool | https://github.com/quietvoid/hdr10plus_tool | HDR10+ metadata tool (MIT) |
| MediaInfo CLI | https://github.com/MediaArea/MediaInfo | Media analysis engine (BSD-2-Clause) |
| curl / libcurl | https://github.com/curl/curl | Network library bundled with MediaInfo Windows CLI (curl/MIT) |
| NVEncC | https://github.com/rigaya/NVEnc | Hardware encoder (MIT, contains NVIDIA and LGPL libplacebo/FFmpeg components) |
| muxiveo-rife | `native/muxiveo-rife/` in this repository | RIFE frame interpolation (MIT), derived from https://github.com/nihui/rife-ncnn-vulkan and https://github.com/TNTwise/rife-ncnn-vulkan (MIT) — see `native/muxiveo-rife/UPSTREAM.md` |
| ncnn | https://github.com/Tencent/ncnn | Neural network inference library statically linked into muxiveo-rife (BSD-3-Clause) |
| RIFE models | https://github.com/hzwer/Practical-RIFE | Model weights bundled with muxiveo-rife (MIT), pinned in `native/muxiveo-rife/models.json` |
| MoltenVK (macOS) | https://github.com/KhronosGroup/MoltenVK | Vulkan-on-Metal runtime shipped with muxiveo-rife on macOS (Apache-2.0) |

---

## Python dependencies (MIT / LGPL)

| Package | Repository |
|---|---|
| PySide6 | https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| pymediainfo | https://github.com/sbraz/pymediainfo |

PySide6 source code is also mirrored at: https://github.com/PySide/pyside-setup
