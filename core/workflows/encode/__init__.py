"""
core/workflows/encode — Encode workflow package.

Re-exports all public symbols so that existing imports of the form
    from core.workflows.encode import X
continue to work unchanged.
"""

from core.workflows.encode.catalog import (
    AudioCodecSpec,
    SOFTWARE_VIDEO_CODECS,
    HARDWARE_VIDEO_CODECS,
    AUDIO_CODECS,
    X265_PRESETS,
    X264_PRESETS,
    SVTAV1_PRESETS,
    NVENC_PRESETS,
    VAAPI_PRESETS,
    QSV_PRESETS,
    AMF_PRESETS,
    TONEMAP_ALGORITHMS,
    VideoCodecFamily,
    VideoCodecSpec,
    audio_codec_spec,
    encoder_badge,
    is_h264_video_codec,
    is_hardware_video_codec,
    presets_for_codec,
    supports_dynamic_hdr,
    supports_force_8bit,
    video_codec_family,
    video_codec_spec,
)
from core.workflows.encode.models import (
    QualityMode,
    VideoEncodeSettings,
    AudioTrackSettings,
    VideoTrackEncodePlan,
    TrackMetaPatch,
    TrackOffset,
    TrackMetaEdit,
    TrackTimeOffset,
    EncodeConfig,
    EncodePreset,
    EncodeError,
)
from core.workflows.encode.hardware import HardwareEncoderDetector
from core.workflows.encode.profiles import ProfileManager
from core.workflows.encode.workflow import EncodeWorkflow

__all__ = [
    "QualityMode",
    "VideoCodecFamily",
    "VideoCodecSpec",
    "AudioCodecSpec",
    "SOFTWARE_VIDEO_CODECS",
    "HARDWARE_VIDEO_CODECS",
    "AUDIO_CODECS",
    "X265_PRESETS",
    "X264_PRESETS",
    "SVTAV1_PRESETS",
    "NVENC_PRESETS",
    "VAAPI_PRESETS",
    "QSV_PRESETS",
    "AMF_PRESETS",
    "TONEMAP_ALGORITHMS",
    "presets_for_codec",
    "video_codec_spec",
    "audio_codec_spec",
    "video_codec_family",
    "is_h264_video_codec",
    "is_hardware_video_codec",
    "supports_dynamic_hdr",
    "supports_force_8bit",
    "encoder_badge",
    "VideoEncodeSettings",
    "AudioTrackSettings",
    "VideoTrackEncodePlan",
    "TrackMetaPatch",
    "TrackOffset",
    "TrackMetaEdit",
    "TrackTimeOffset",
    "EncodeConfig",
    "EncodePreset",
    "EncodeError",
    "HardwareEncoderDetector",
    "ProfileManager",
    "EncodeWorkflow",
]
