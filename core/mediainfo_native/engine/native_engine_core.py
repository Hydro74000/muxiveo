"""
Native Python MediaInfo-compatible engine.
"""

from __future__ import annotations

import html
import json
import os
import re
import struct
import time
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Iterable
from urllib.parse import urlparse
from ..options.store import (
    OPTION_ALIASES as _OPTION_ALIASES,
    OPTION_DEFAULTS as _OPTION_DEFAULTS,
    OUTPUT_ALIASES as _OUTPUT_ALIASES,
    info_options_text as _info_options_text,
    info_output_formats_text as _info_output_formats_text,
    info_parameters_text as _info_parameters_text,
    normalize_option as _normalize_option_value,
    normalize_output_mode as _normalize_output_mode_value,
    option_help_text as _option_help_text,
)
from ..renderers.text import render_text as _render_text_module
from ..renderers.json import render_json as _render_json_module
from ..renderers.xml import render_xml as _render_xml_module
from ..renderers.inform import parse_inform_expression as _parse_inform_expression_module
from ..renderers.inform import render_inform as _render_inform_module
from ..renderers.ebucore import render_ebucore as _render_ebucore_module
from ..renderers.pbcore import render_pbcore as _render_pbcore_module
from ..renderers.mpeg7 import render_mpeg7 as _render_mpeg7_module
from ..renderers.specialized_common import duration_iso8601_from_ms as _duration_iso8601_from_ms_module
from ..renderers.specialized_common import public_fields as _public_fields_module

VERSION_TEXT = "MediaInfoLib - v26.01"
CLI_VERSION_TEXT = f"MediaInfo Command line, {VERSION_TEXT}"

_CODEC_MAP: dict[str, str] = {
    "hevc": "HEVC",
    "h265": "HEVC",
    "h264": "AVC",
    "avc": "AVC",
    "av1": "AV1",
    "mpeg2video": "MPEG Video",
    "mpeg4": "MPEG-4 Visual",
    "vp9": "VP9",
    "vp8": "VP8",
    "truehd": "TrueHD",
    "eac3": "E-AC-3",
    "ac3": "AC-3",
    "aac": "AAC",
    "flac": "FLAC",
    "dts": "DTS",
    "opus": "Opus",
    "subrip": "SubRip",
    "ass": "ASS",
    "hdmv_pgs_subtitle": "PGS",
}

_STREAM_KIND_ORDER: tuple[str, ...] = ("General", "Video", "Audio", "Text", "Other", "Image", "Menu")

_INFORM_ALIASES: dict[str, str] = {
    "Complete name": "CompleteName",
    "File size": "FileSize",
    "Overall bit rate": "OverallBitRate",
    "Frame rate": "FrameRate",
    "Bit rate": "BitRate",
    "Sampling rate": "SamplingRate",
    "Channel(s)": "Channels",
    "HDR format": "HDR_Format",
    "HDR_Format_Compatibility": "HDR_Format_Compatibility",
    "Commercial name": "Format_AdditionalFeatures",
    "Format profile": "Format_Profile",
    "Format/String": "Format",
}

_TEXT_FIELD_ORDER: dict[str, list[str]] = {
    "General": [
        "UniqueID",
        "CompleteName",
        "Format",
        "Format_Profile",
        "Format_Version",
        "CodecID",
        "FileSize",
        "Duration",
        "OverallBitRate",
        "FrameRate",
        "Title",
        "Encoded_Application",
        "Encoded_Library",
        "Comment",
        "extra.ErrorDetectionType",
    ],
    "Video": [
        "ID",
        "Format",
        "Format_Info",
        "Format_Profile",
        "HDR_Format",
        "HDR_Format_Compatibility",
        "Format_Settings",
        "Format_Settings_CABAC",
        "Format_Settings_RefFrames",
        "CodecID",
        "CodecID_Info",
        "Duration",
        "BitRate",
        "Width",
        "Width_Original",
        "Height",
        "Height_Original",
        "DisplayAspectRatio",
        "FrameRate_Mode",
        "FrameRate",
        "ColorSpace",
        "ChromaSubsampling",
        "BitDepth",
        "ScanType",
        "Title",
        "Bits_Pixel_Frame",
        "StreamSize",
        "Encoded_Library",
        "Encoded_Library_Settings",
        "Default",
        "Forced",
        "colour_range",
        "colour_primaries",
        "transfer_characteristics",
        "matrix_coefficients",
        "MasteringDisplay_ColorPrimaries",
        "MasteringDisplay_Luminance",
        "MaxCLL",
        "MaxFALL",
        "CodecConfigurationBox",
    ],
    "Audio": [
        "ID",
        "Format",
        "Format_Info",
        "Format_Commercial_IfAny",
        "CodecID",
        "Duration",
        "Source_Duration",
        "Source_Duration_LastFrame",
        "BitRate_Mode",
        "BitRate",
        "Channels",
        "ChannelLayout",
        "SamplingRate",
        "FrameRate",
        "BitDepth",
        "Compression_Mode",
        "Title",
        "StreamSize",
        "Source_StreamSize",
        "Encoded_Library",
        "Language",
        "ServiceKind",
        "Default",
        "Forced",
        "AlternateGroup",
        "extra.dialnorm",
        "extra.dialnorm_Average",
        "extra.dialnorm_Minimum",
        "extra.dialnorm_Maximum",
    ],
    "Text": [
        "ID",
        "Format",
        "CodecID",
        "CodecID_Info",
        "Duration",
        "Duration_End",
        "Compression_Mode",
        "Events_Total",
        "Events_MinDuration",
        "Lines_Count",
        "Lines_MaxCountPerEvent",
        "Title",
        "Encoded_Library",
        "Language",
        "Default",
        "Forced",
    ],
    "Menu": [],
}

_TEXT_LABELS: dict[str, str] = {
    "CompleteName": "Complete name",
    "Format_Profile": "Format profile",
    "Format_Version": "Format version",
    "Format_Level": "Format Level",
    "CodecID": "Codec ID",
    "CodecID_Info": "Codec ID/Info",
    "UniqueID": "Unique ID",
    "FileSize": "File size",
    "OverallBitRate": "Overall bit rate",
    "FrameRate": "Frame rate",
    "BitRate": "Bit rate",
    "FrameCount": "Frame count",
    "Encoded_Application": "Writing application",
    "Encoded_Library": "Writing library",
    "Encoded_Library_Name": "Encoded library name",
    "Encoded_Library_Version": "Encoded library version",
    "Encoded_Library_Settings": "Encoding settings",
    "Format_Info": "Format/Info",
    "Format_Settings": "Format settings",
    "Format_Settings_CABAC": "Format settings, CABAC",
    "Format_Settings_RefFrames": "Format settings, Reference frames",
    "StreamOrder": "Stream order",
    "Width_Original": "Original width",
    "Height_Original": "Original height",
    "DisplayAspectRatio": "Display aspect ratio",
    "FrameRate_Mode": "Frame rate mode",
    "ColorSpace": "Color space",
    "ChromaSubsampling": "Chroma subsampling",
    "BitDepth": "Bit depth",
    "ScanType": "Scan type",
    "Bits_Pixel_Frame": "Bits/(Pixel*Frame)",
    "BitRate_Mode": "Bit rate mode",
    "Source_Duration": "Source duration",
    "Source_Duration_LastFrame": "Source_Duration_LastFrame",
    "Source_StreamSize": "Source stream size",
    "Channels": "Channel(s)",
    "SamplingRate": "Sampling rate",
    "Compression_Mode": "Compression mode",
    "Format_AdditionalFeatures": "Commercial name",
    "Format_Commercial_IfAny": "Commercial name",
    "ServiceKind": "Service kind",
    "colour_range": "Color range",
    "matrix_coefficients": "Matrix coefficients",
    "CodecConfigurationBox": "Codec configuration box",
    "HDR_Format": "HDR format",
    "HDR_Format_Compatibility": "HDR format compatibility",
    "MaxCLL": "Maximum Content Light Level",
    "MaxFALL": "Maximum Frame-Average Light Level",
    "colour_primaries": "Color primaries",
    "transfer_characteristics": "Transfer characteristics",
    "MasteringDisplay_ColorPrimaries": "Mastering display color primaries",
    "MasteringDisplay_Luminance": "Mastering display luminance",
    "Title": "Title",
    "Comment": "Comment",
    "Movie": "Movie",
    "Language": "Language",
    "MenuCount": "Menu count",
    "FileExtension": "File extension",
    "VideoCount": "Video count",
    "AudioCount": "Audio count",
    "TextCount": "Text count",
    "File_Created_Date": "File created date",
    "File_Created_Date_Local": "File created date (local)",
    "File_Modified_Date": "File modified date",
    "File_Modified_Date_Local": "File modified date (local)",
    "StreamSize": "Stream size",
    "Source_StreamSize": "Source stream size",
    "ChannelPositions": "Channel positions",
    "ChannelLayout": "Channel layout",
    "AlternateGroup": "Alternate group",
    "Duration_End": "End time",
    "Events_Total": "Count of events",
    "Events_MinDuration": "Minimum duration per event",
    "Lines_Count": "Count of lines",
    "Lines_MaxCountPerEvent": "Maximum count of lines per event",
    "extra.ErrorDetectionType": "ErrorDetectionType",
    "extra.dialnorm": "Dialog Normalization",
    "extra.dialnorm_Average": "dialnorm_Average",
    "extra.dialnorm_Minimum": "dialnorm_Minimum",
    "extra.dialnorm_Maximum": "dialnorm_Maximum",
}

_STRUCTURED_FIELD_ORDER: dict[str, list[str]] = {
    "General": [
        "UniqueID",
        "VideoCount",
        "AudioCount",
        "TextCount",
        "MenuCount",
        "FileExtension",
        "Format",
        "Format_Profile",
        "Format_Version",
        "CodecID",
        "CodecID_Compatible",
        "FileSize",
        "Duration",
        "OverallBitRate",
        "FrameRate",
        "FrameCount",
        "StreamSize",
        "Title",
        "Movie",
        "HeaderSize",
        "DataSize",
        "FooterSize",
        "IsStreamable",
        "File_Created_Date",
        "File_Created_Date_Local",
        "File_Modified_Date",
        "File_Modified_Date_Local",
        "Encoded_Application",
        "Encoded_Library",
        "Comment",
    ],
    "Video": [
        "StreamOrder",
        "ID",
        "UniqueID",
        "Format",
        "Format_Profile",
        "Format_Level",
        "Format_Tier",
        "HDR_Format",
        "HDR_Format_Compatibility",
        "Format_Settings_CABAC",
        "Format_Settings_RefFrames",
        "CodecID",
        "Duration",
        "BitRate",
        "Width",
        "Width_Original",
        "Height",
        "Height_Original",
        "Stored_Height",
        "Sampled_Width",
        "Sampled_Height",
        "PixelAspectRatio",
        "DisplayAspectRatio",
        "Rotation",
        "FrameRate_Mode",
        "FrameRate_Mode_Original",
        "FrameRate",
        "FrameRate_Num",
        "FrameRate_Den",
        "FrameCount",
        "ColorSpace",
        "ChromaSubsampling",
        "BitDepth",
        "ScanType",
        "Delay",
        "Delay_Source",
        "StreamSize",
        "Title",
        "Encoded_Library",
        "Encoded_Library_Name",
        "Encoded_Library_Version",
        "Encoded_Library_Settings",
        "Default",
        "Forced",
        "colour_description_present",
        "colour_description_present_Source",
        "colour_range",
        "colour_range_Source",
        "colour_primaries",
        "colour_primaries_Source",
        "transfer_characteristics",
        "transfer_characteristics_Source",
        "matrix_coefficients",
        "matrix_coefficients_Source",
        "MasteringDisplay_ColorPrimaries",
        "MasteringDisplay_ColorPrimaries_Source",
        "MasteringDisplay_Luminance",
        "MasteringDisplay_Luminance_Source",
        "MasteringDisplay_Luminance_Min",
        "MasteringDisplay_Luminance_Max",
        "MaxCLL",
        "MaxCLL_Source",
        "MaxFALL",
        "MaxFALL_Source",
    ],
    "Audio": [
        "StreamOrder",
        "ID",
        "UniqueID",
        "Format",
        "Format_Settings_SBR",
        "Format_AdditionalFeatures",
        "Format_Commercial_IfAny",
        "Format_Settings_Endianness",
        "CodecID",
        "Duration",
        "Source_Duration",
        "Source_Duration_LastFrame",
        "BitRate_Mode",
        "BitRate",
        "Channels",
        "ChannelPositions",
        "ChannelLayout",
        "SamplesPerFrame",
        "SamplingRate",
        "SamplingCount",
        "FrameRate",
        "FrameCount",
        "Source_FrameCount",
        "BitDepth",
        "Compression_Mode",
        "Delay",
        "Delay_Source",
        "Video_Delay",
        "StreamSize",
        "Source_StreamSize",
        "Title",
        "Encoded_Library",
        "Language",
        "ServiceKind",
        "Default",
        "Forced",
        "AlternateGroup",
    ],
    "Text": [
        "StreamOrder",
        "ID",
        "UniqueID",
        "Format",
        "CodecID",
        "Duration",
        "Duration_Start",
        "Duration_End",
        "Compression_Mode",
        "Events_Total",
        "Events_MinDuration",
        "Lines_Count",
        "Lines_MaxCountPerEvent",
        "Title",
        "Encoded_Library",
        "Language",
        "Default",
        "Forced",
    ],
    "Menu": [],
}

_TEXT_RAW_FIELD_MAP: dict[str, list[tuple[str, str]]] = {
    "General": [
        ("UniqueID/String", "UniqueID"),
        ("CompleteName", "CompleteName"),
        ("Format/String", "Format"),
        ("Format_Profile", "Format_Profile"),
        ("Format_Version", "Format_Version"),
        ("CodecID/String", "CodecID"),
        ("FileSize/String", "FileSize"),
        ("Duration/String", "Duration"),
        ("OverallBitRate/String", "OverallBitRate"),
        ("FrameRate/String", "FrameRate"),
        ("Title", "Title"),
        ("Encoded_Application/String", "Encoded_Application"),
        ("Encoded_Library/String", "Encoded_Library"),
        ("Comment", "Comment"),
        ("ErrorDetectionType", "extra.ErrorDetectionType"),
    ],
    "Video": [
        ("ID/String", "ID"),
        ("Format/String", "Format"),
        ("Format/Info", "Format_Info"),
        ("Format_Profile", "Format_Profile"),
        ("HDR_Format/String", "HDR_Format"),
        ("Format_Settings", "Format_Settings"),
        ("Format_Settings_CABAC/String", "Format_Settings_CABAC"),
        ("Format_Settings_RefFrames/String", "Format_Settings_RefFrames"),
        ("CodecID", "CodecID"),
        ("CodecID/Info", "CodecID_Info"),
        ("Duration/String", "Duration"),
        ("BitRate/String", "BitRate"),
        ("Width/String", "Width"),
        ("Width_Original/String", "Width_Original"),
        ("Height/String", "Height"),
        ("Height_Original/String", "Height_Original"),
        ("DisplayAspectRatio/String", "DisplayAspectRatio"),
        ("FrameRate_Mode/String", "FrameRate_Mode"),
        ("FrameRate/String", "FrameRate"),
        ("ColorSpace", "ColorSpace"),
        ("ChromaSubsampling/String", "ChromaSubsampling"),
        ("BitDepth/String", "BitDepth"),
        ("ScanType/String", "ScanType"),
        ("Title", "Title"),
        ("Bits-(Pixel*Frame)", "Bits_Pixel_Frame"),
        ("StreamSize/String", "StreamSize"),
        ("Encoded_Library/String", "Encoded_Library"),
        ("Encoded_Library_Settings", "Encoded_Library_Settings"),
        ("Default/String", "Default"),
        ("Forced/String", "Forced"),
        ("colour_range", "colour_range"),
        ("colour_primaries", "colour_primaries"),
        ("transfer_characteristics", "transfer_characteristics"),
        ("matrix_coefficients", "matrix_coefficients"),
        ("MasteringDisplay_ColorPrimaries", "MasteringDisplay_ColorPrimaries"),
        ("MasteringDisplay_Luminance", "MasteringDisplay_Luminance"),
        ("MaxCLL/String", "MaxCLL"),
        ("MaxFALL/String", "MaxFALL"),
        ("CodecConfigurationBox", "CodecConfigurationBox"),
    ],
    "Audio": [
        ("ID/String", "ID"),
        ("Format/String", "Format"),
        ("Format/Info", "Format_Info"),
        ("Format_Commercial_IfAny", "Format_Commercial_IfAny"),
        ("CodecID", "CodecID"),
        ("Duration/String", "Duration"),
        ("Source_Duration/String", "Source_Duration"),
        ("Source_Duration_LastFrame/String", "Source_Duration_LastFrame"),
        ("BitRate_Mode/String", "BitRate_Mode"),
        ("BitRate/String", "BitRate"),
        ("Channel(s)/String", "Channels"),
        ("ChannelLayout", "ChannelLayout"),
        ("SamplingRate/String", "SamplingRate"),
        ("FrameRate/String", "FrameRate"),
        ("BitDepth/String", "BitDepth"),
        ("Compression_Mode/String", "Compression_Mode"),
        ("Title", "Title"),
        ("StreamSize/String", "StreamSize"),
        ("Source_StreamSize/String", "Source_StreamSize"),
        ("Encoded_Library/String", "Encoded_Library"),
        ("Language/String", "Language"),
        ("ServiceKind/String", "ServiceKind"),
        ("Default/String", "Default"),
        ("Forced/String", "Forced"),
        ("AlternateGroup/String", "AlternateGroup"),
        ("dialnorm", "extra.dialnorm"),
        ("dialnorm_Average", "extra.dialnorm_Average"),
        ("dialnorm_Minimum", "extra.dialnorm_Minimum"),
        ("dialnorm_Maximum", "extra.dialnorm_Maximum"),
    ],
    "Text": [
        ("ID/String", "ID"),
        ("Format/String", "Format"),
        ("CodecID", "CodecID"),
        ("CodecID/Info", "CodecID_Info"),
        ("Duration/String", "Duration"),
        ("Duration_End/String", "Duration_End"),
        ("Compression_Mode/String", "Compression_Mode"),
        ("Events_Total", "Events_Total"),
        ("Events_MinDuration/String", "Events_MinDuration"),
        ("Lines_Count", "Lines_Count"),
        ("Lines_MaxCountPerEvent", "Lines_MaxCountPerEvent"),
        ("Title", "Title"),
        ("Encoded_Library/String", "Encoded_Library"),
        ("Language/String", "Language"),
        ("Default/String", "Default"),
        ("Forced/String", "Forced"),
    ],
    "Menu": [],
}


class MediaInfoNativeError(RuntimeError):
    """Raised when native mediainfo parsing failed."""


@dataclass(slots=True)
class MediaTrack:
    kind: str
    fields: OrderedDict[str, str] = field(default_factory=OrderedDict)



@dataclass(slots=True)
class SubripStats:
    duration_end_ms: int
    events_total: int
    events_min_duration_ms: int
    lines_count: int
    lines_max_count_per_event: int


@dataclass(slots=True)
class MediaReport:
    source: str
    tracks: list[MediaTrack] = field(default_factory=list)

    def tracks_by_kind(self, kind: str) -> list[MediaTrack]:
        kind_lower = kind.lower()
        return [track for track in self.tracks if track.kind.lower() == kind_lower]

    def first_track(self, kind: str) -> MediaTrack | None:
        tracks = self.tracks_by_kind(kind)
        return tracks[0] if tracks else None

    def render_text(self) -> str:
        from ..api.model import from_report, to_report_view

        report_view = to_report_view(from_report(self))
        return _render_text_module(
            report_view,
            raw=False,
            text_field_order=_TEXT_FIELD_ORDER,
            text_labels=_TEXT_LABELS,
            text_raw_field_map=_TEXT_RAW_FIELD_MAP,
            int_or_none=_int_or_none,
            should_display_text_field=_should_display_text_field,
            format_text_field_value=_format_text_field_value,
            format_text_raw_value=_format_text_raw_value,
        )

    def render_text_raw(self) -> str:
        from ..api.model import from_report, to_report_view

        report_view = to_report_view(from_report(self))
        return _render_text_module(
            report_view,
            raw=True,
            text_field_order=_TEXT_FIELD_ORDER,
            text_labels=_TEXT_LABELS,
            text_raw_field_map=_TEXT_RAW_FIELD_MAP,
            int_or_none=_int_or_none,
            should_display_text_field=_should_display_text_field,
            format_text_field_value=_format_text_field_value,
            format_text_raw_value=_format_text_raw_value,
        )

    def render_json(self) -> str:
        from ..api.model import from_report, to_report_view

        report_view = to_report_view(from_report(self))
        return _render_json_module(
            report_view,
            structured_order_for_track=_structured_order_for_track,
            render_oracle_json_track=_render_oracle_json_track,
            string=_string,
        )

    def render_xml(self) -> str:
        from ..api.model import from_report, to_report_view

        report_view = to_report_view(from_report(self))
        return _render_xml_module(
            report_view,
            structured_order_for_track=_structured_order_for_track,
            xml_escape=_xml_escape,
            string=_string,
        )

    def render_html(self) -> str:
        rows: list[str] = []
        for track in self.tracks:
            rows.append(f"<h2>{html.escape(track.kind)}</h2>")
            rows.append("<table border='1' cellspacing='0' cellpadding='4'>")
            for key, value in track.fields.items():
                rows.append(
                    f"<tr><th align='left'>{html.escape(key)}</th><td>{html.escape(value)}</td></tr>"
                )
            rows.append("</table>")
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>MediaInfo</title></head><body>"
            + "".join(rows)
            + "</body></html>\n"
        )

    def render_csv(self) -> str:
        lines = ["Kind,Field,Value"]
        for track in self.tracks:
            for key, value in track.fields.items():
                escaped = value.replace('"', '""')
                lines.append(f'{track.kind},{key},"{escaped}"')
        return "\n".join(lines) + "\n"



class MediaInfoEngine:
    """
    Native MediaInfo runtime engine used by mediarecode.
    """

    _static_options: ClassVar[dict[str, str]] = dict(_OPTION_DEFAULTS)

    def __init__(self, cache_size: int = 128) -> None:
        self._cache_size = max(1, cache_size)
        self._cache: OrderedDict[str, tuple[float, MediaReport]] = OrderedDict()
        self._options: dict[str, str] = dict(_OPTION_DEFAULTS)
        self._options.update(self._static_options)

    # ------------------------------------------------------------------
    # Public API - parsing and rendering
    # ------------------------------------------------------------------

    def report(self, source: str) -> MediaReport:
        raise MediaInfoNativeError(
            "Base MediaInfoEngine.report() is not wired for direct runtime use. "
            "Use core.mediainfo_native.api.engine.MediaInfoEngine."
        )

    def query_inform(self, source: str, inform_expr: str) -> str:
        report = self.report(source)
        selector, template = _parse_inform_expression_module(inform_expr)
        tracks = report.tracks_by_kind(selector)
        if not tracks:
            return ""
        values = [_render_inform_module(track.fields, template) for track in tracks]
        return "\n".join(values).strip()

    def render(self, source: str, output_mode: str = "text") -> str:
        render_started = datetime.now(timezone.utc)
        report = self.report(source)
        from ..api.model import from_report, to_report_view

        report_view = to_report_view(from_report(report))

        raw_mode = (output_mode or self._options.get("output", "") or "Text").strip()
        if ";" in raw_mode or raw_mode.lower().startswith("file://"):
            return self.query_inform(source, raw_mode) + "\n"

        mode = self._normalize_output_mode(raw_mode)
        if mode == "TEXT":
            if self._normalize_option(self._options.get("language", "")) == "raw":
                return self._apply_text_report_options(report.render_text_raw(), timestamp=render_started)
            return self._apply_text_report_options(report.render_text(), timestamp=render_started)
        if mode == "JSON":
            return _render_json_module(
                report_view,
                structured_order_for_track=_structured_order_for_track,
                render_oracle_json_track=_render_oracle_json_track,
                string=_string,
            )
        if mode in {"XML", "OLDXML"}:
            return _render_xml_module(
                report_view,
                structured_order_for_track=_structured_order_for_track,
                xml_escape=_xml_escape,
                string=_string,
            )
        if mode == "HTML":
            return report.render_html()
        if mode == "CSV":
            return report.render_csv()

        if mode in {
            "EBUCORE_1.5",
            "EBUCORE_1.6",
            "EBUCORE_1.8_PS",
            "EBUCORE_1.8_SP",
            "EBUCORE_1.8_PS_JSON",
            "EBUCORE_1.8_SP_JSON",
        }:
            return _render_ebucore_module(
                self,
                report_view,
                mode,
                timestamp=render_started,
                version_text=VERSION_TEXT,
                xml_escape=_xml_escape,
                display_ratio_parts=_display_ratio_parts,
                string=_string,
                trim_float=_trim_float,
                int_or_none=_int_or_none,
                video_profile_label=_video_profile_label,
                scan_type_ebucore=_scan_type_ebucore,
                bitrate_mode_ebucore=_bitrate_mode_ebucore,
                format_text_field_value=_format_text_field_value,
                should_display_text_field=_should_display_text_field,
                seconds_to_ms=_seconds_to_ms,
                codec_label=_codec_label,
                ebucore_xml_to_json=_ebucore_xml_to_json,
            )

        if mode in {"PBCORE", "PBCORE2"}:
            return _render_pbcore_module(
                self,
                report_view,
                version=mode,
                timestamp=render_started,
                version_text=VERSION_TEXT,
                xml_escape=_xml_escape,
                string=_string,
                utc_file_date_iso=_utc_file_date_iso,
                mime_for_extension=_mime_for_extension,
                pbcore_duration=_pbcore_duration,
                video_profile_label=_video_profile_label,
                codec_label=_codec_label,
                seconds_to_ms=_seconds_to_ms,
                int_or_none=_int_or_none,
                pbcore_duration_ms=_pbcore_duration_ms,
                iso639_2_bibliographic=_iso639_2_bibliographic,
                seconds_ms_to_int_if_needed=_seconds_ms_to_int_if_needed,
            )

        if mode in {"MPEG-7_STRICT", "MPEG-7_RELAXED", "MPEG-7_EXTENDED"}:
            return _render_mpeg7_module(
                self,
                report_view,
                profile=mode,
                timestamp=render_started,
                version_text=VERSION_TEXT,
                xml_escape=_xml_escape,
                string=_string,
                codec_label=_codec_label,
                mpeg7_avc_level_term_id=_mpeg7_avc_level_term_id,
                mpeg7_audio_presentation_name=_mpeg7_audio_presentation_name,
                mpeg7_duration=_mpeg7_duration,
            )

        if mode in {"GRAPH_SVG", "GRAPH_DOT"}:
            return self._render_graph(report, mode)

        if mode in {"FIMS_1.1", "FIMS_1.2", "FIMS_1.3"}:
            return self._render_fims(report, mode)

        if mode == "NISO_Z39.87":
            return self._render_niso(report)

        return self._apply_text_report_options(report.render_text(), timestamp=render_started)

    # ------------------------------------------------------------------
    # Options API (advanced Option / Option_Static support)
    # ------------------------------------------------------------------

    def option(self, option: str, value: str = "") -> str:
        key = self._normalize_option(option)

        # Informational read-only options.
        if key == "info_version":
            return VERSION_TEXT
        if key == "info_url":
            return "https://mediaarea.net/MediaInfo"
        if key == "info_parameters":
            return self.info_parameters()
        if key == "info_outputformats":
            return self.info_output_formats()
        if key == "info_options":
            return self.info_options()
        if key == "help":
            if value:
                return self.option_help(value)
            return CLI_VERSION_TEXT
        if key == "help_anoption":
            return self.option_help(value)

        if key in {"reset", "clear"}:
            self._options = dict(_OPTION_DEFAULTS)
            self._options.update(self._static_options)
            return ""

        if key.endswith("_get"):
            base = key[:-4]
            return self._options.get(base, "")

        if value != "":
            self._options[key] = value
            return ""

        return self._options.get(key, "")

    @classmethod
    def option_static(cls, option: str, value: str = "") -> str:
        key = cls._normalize_option(option)

        if key == "info_version":
            return VERSION_TEXT
        if key == "info_url":
            return "https://mediaarea.net/MediaInfo"
        if key == "info_parameters":
            return cls.info_parameters()
        if key == "info_outputformats":
            return cls.info_output_formats()
        if key == "info_options":
            return cls.info_options()
        if key == "help":
            if value:
                return cls.option_help(value)
            return CLI_VERSION_TEXT
        if key == "help_anoption":
            return cls.option_help(value)

        if key in {"reset", "clear"}:
            cls._static_options = dict(_OPTION_DEFAULTS)
            return ""

        if key.endswith("_get"):
            base = key[:-4]
            return cls._static_options.get(base, "")

        if value != "":
            cls._static_options[key] = value
            return ""

        return cls._static_options.get(key, "")

    # ------------------------------------------------------------------
    # Option and output metadata
    # ------------------------------------------------------------------

    @staticmethod
    def info_parameters() -> str:
        return _info_parameters_text()

    @staticmethod
    def info_output_formats() -> str:
        return _info_output_formats_text()

    @staticmethod
    def info_options() -> str:
        return _info_options_text()

    @staticmethod
    def option_help(option_name: str) -> str:
        return _option_help_text(option_name)

    # ------------------------------------------------------------------
    # Internal - probes and model building
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_option(option: str) -> str:
        return _normalize_option_value(option)

    @staticmethod
    def _normalize_output_mode(mode: str) -> str:
        return _normalize_output_mode_value(mode)

    @staticmethod
    def _is_truthy(value: str | None) -> bool:
        if value is None:
            return False
        return str(value).strip().lower() not in {"", "0", "false", "no", "off"}

    def _apply_text_report_options(self, text_report: str, timestamp: datetime | None = None) -> str:
        out = text_report.rstrip("\n")
        has_version = self._is_truthy(self._options.get("inform_version"))
        has_timestamp = self._is_truthy(self._options.get("inform_timestamp"))
        if has_version:
            out += f"\n\n{'ReportBy':<40} : {VERSION_TEXT}"
        if has_timestamp:
            stamp = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")
            out += f"\n\n{'CreatedOn':<40} : UTC {stamp}"
        return out + ("\n\n" if (has_version or has_timestamp) else "\n\n\n")

    def _cache_key(self, source: str) -> str:
        if _is_url(source):
            return f"url:{source}"
        path = Path(source).expanduser()
        if path.exists():
            stat = path.stat()
            return f"path:{path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"
        return f"path:{path.resolve()}"

    def _probe(self, source: str) -> dict[str, Any]:
        raise MediaInfoNativeError("Deprecated internal probe path removed.")

    def _build_general_track(
        self,
        source: str,
        fmt: dict[str, Any],
        streams: list[Any],
    ) -> MediaTrack:
        size_bytes = self._int_or_none(fmt.get("size"))
        duration_ms = _duration_ms(fmt.get("duration"))
        bit_rate = self._int_or_none(fmt.get("bit_rate"))
        tags = fmt.get("tags", {}) if isinstance(fmt.get("tags"), dict) else {}

        video_streams = [s for s in streams if isinstance(s, dict) and _string(s.get("codec_type")).lower() == "video" and not (s.get("disposition") or {}).get("attached_pic")]
        audio_streams = [s for s in streams if isinstance(s, dict) and _string(s.get("codec_type")).lower() == "audio"]
        text_streams = [s for s in streams if isinstance(s, dict) and _string(s.get("codec_type")).lower() == "subtitle"]
        first_video = video_streams[0] if video_streams else None
        fps = _parse_ratio(_string((first_video or {}).get("avg_frame_rate") or (first_video or {}).get("r_frame_rate")))
        frame_count = self._compute_frame_count(first_video or {}, fps, duration_ms) if first_video else None

        format_name = _string(fmt.get("format_name"))
        format_label = _container_format_label(format_name, source)
        format_profile = _container_format_profile(format_name, source)
        codec_id = _string(tags.get("major_brand")) or _string(fmt.get("format_name")).split(",", 1)[0]
        codec_compatible = _format_compatible_brands(_string(tags.get("compatible_brands")))

        encoded_application = _string(tags.get("encoder") or tags.get("ENCODER"))
        is_streamable = "No" if "mov,mp4" in format_name else "Yes"

        stream_sizes_sum = 0
        if duration_ms is not None:
            duration_s = duration_ms / 1000.0
            for stream in (*video_streams, *audio_streams, *text_streams):
                stream_br = self._int_or_none(stream.get("bit_rate"))
                if stream_br:
                    stream_sizes_sum += int(round(stream_br * duration_s / 8))
        stream_size = max(0, (size_bytes or 0) - stream_sizes_sum) if size_bytes is not None else None

        fields = _ordered_dict()
        fields["CompleteName"] = source
        fields["Format"] = format_label
        fields["Format_Profile"] = format_profile
        fields["CodecID"] = codec_id
        if codec_compatible:
            fields["CodecID_Compatible"] = codec_compatible
        fields["FileSize"] = _string(size_bytes or "")
        fields["Duration"] = _seconds_string(duration_ms, digits=3)
        fields["OverallBitRate"] = _string(bit_rate or "")
        fields["FrameRate"] = f"{fps:.3f}" if fps is not None else ""
        fields["FrameCount"] = _string(frame_count or "")
        fields["StreamSize"] = _string(stream_size if stream_size is not None else "")
        fields["IsStreamable"] = is_streamable
        fields["VideoCount"] = _string(len(video_streams))
        fields["AudioCount"] = _string(len(audio_streams))
        if text_streams:
            fields["TextCount"] = _string(len(text_streams))
        suffix = Path(source).suffix.lower().lstrip(".")
        fields["FileExtension"] = suffix
        if encoded_application:
            fields["Encoded_Application"] = encoded_application
            if "matroska" in format_name or "webm" in format_name:
                fields["Encoded_Library"] = encoded_application
        title = _string(tags.get("title") or tags.get("TITLE"))
        if title:
            fields["Title"] = title

        if not _is_url(source):
            p = Path(source).expanduser()
            if p.exists():
                stat = p.stat()
                mtime_ms = (
                    int(stat.st_mtime_ns / 1_000_000)
                    if hasattr(stat, "st_mtime_ns")
                    else int(stat.st_mtime * 1000.0)
                )
                created_utc = datetime.fromtimestamp(mtime_ms / 1000.0, tz=timezone.utc)
                created_local = datetime.fromtimestamp(mtime_ms / 1000.0)
                if os.name == "nt":
                    ms_suffix = f".{mtime_ms % 1000:03d}"
                    fields["File_Modified_Date"] = created_utc.strftime("%Y-%m-%d %H:%M:%S") + ms_suffix + " UTC"
                    fields["File_Modified_Date_Local"] = created_local.strftime("%Y-%m-%d %H:%M:%S") + ms_suffix
                else:
                    fields["File_Modified_Date"] = created_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
                    fields["File_Modified_Date_Local"] = created_local.strftime("%Y-%m-%d %H:%M:%S")
        return MediaTrack(kind="General", fields=fields)

    def _apply_subrip_general_track(self, general: MediaTrack, stats: "SubripStats") -> None:
        size_bytes = _int_or_none(general.fields.get("FileSize"))
        general.fields.pop("CodecID", None)
        general.fields.pop("CodecID_Compatible", None)
        general.fields.pop("Format_Profile", None)
        general.fields.pop("FrameRate", None)
        general.fields.pop("FrameCount", None)
        general.fields.pop("VideoCount", None)
        general.fields.pop("AudioCount", None)
        general.fields.pop("IsStreamable", None)
        general.fields["TextCount"] = "1"
        general.fields["Duration"] = _seconds_string(stats.duration_end_ms, digits=3)
        if size_bytes is not None and stats.duration_end_ms > 0:
            overall = int(round((size_bytes * 8 * 1000) / stats.duration_end_ms))
            general.fields["OverallBitRate"] = _string(overall)
        else:
            general.fields.pop("OverallBitRate", None)
        if size_bytes is not None:
            general.fields["StreamSize"] = _string(size_bytes)

    def _build_subrip_text_track(self, stats: "SubripStats") -> MediaTrack:
        fields = _ordered_dict()
        fields["Format"] = "SubRip"
        fields["Duration"] = _seconds_string(stats.duration_end_ms, digits=3)
        fields["Duration_Start"] = "0.000"
        fields["Duration_End"] = _seconds_string(stats.duration_end_ms, digits=3)
        fields["Compression_Mode"] = "Lossless"
        fields["Events_Total"] = _string(stats.events_total)
        fields["Events_MinDuration"] = _seconds_string(stats.events_min_duration_ms, digits=3)
        fields["Lines_Count"] = _string(stats.lines_count)
        fields["Lines_MaxCountPerEvent"] = _string(stats.lines_max_count_per_event)
        return MediaTrack(kind="Text", fields=fields)

    def _build_video_track(self, stream: dict[str, Any], fmt: dict[str, Any]) -> MediaTrack:
        fields = _ordered_dict()
        tags = stream.get("tags", {}) if isinstance(stream.get("tags"), dict) else {}
        codec_name = _string(stream.get("codec_name"))
        bit_rate = self._int_or_none(stream.get("bit_rate")) or self._int_or_none(fmt.get("bit_rate"))
        fps = _parse_ratio(_string(stream.get("avg_frame_rate") or stream.get("r_frame_rate")))
        duration_ms = _duration_ms(stream.get("duration") or fmt.get("duration"))
        frame_count = self._compute_frame_count(stream, fps, duration_ms)
        hdr_format, hdr_compat, cll, fall = self._hdr_fields(stream)
        stream_order = self._int_or_none(stream.get("index"))
        stream_id = _decode_stream_id(stream.get("id"), fallback=(stream_order + 1) if stream_order is not None else None)

        fields["StreamOrder"] = _string(stream_order if stream_order is not None else "")
        fields["ID"] = _string(stream_id or "")
        fields["Format"] = _codec_label(codec_name)
        fields["Format_Info"] = _codec_info_short(codec_name, _string(stream.get("codec_long_name")))
        fields["Format_Profile"] = _string(stream.get("profile"))
        level = self._int_or_none(stream.get("level"))
        if level is not None and level > 0:
            fields["Format_Level"] = f"{level/10:.1f}".rstrip("0").rstrip(".")
        refs = self._int_or_none(stream.get("refs"))
        if refs is not None and refs > 0:
            fields["Format_Settings_RefFrames"] = _string(refs)
            fields["Format_Settings"] = f"CABAC / {refs} Ref Frames" if _string(stream.get("is_avc")) == "true" else ""
        if _string(stream.get("is_avc")) == "true":
            fields["Format_Settings_CABAC"] = "Yes"
        fields["CodecID"] = _video_codec_id(codec_name, _string(fmt.get("format_name")), _string(stream.get("codec_tag_string")))
        fields["CodecID_Info"] = _codec_info_short(codec_name, _string(stream.get("codec_long_name")), codec_id=True)
        fields["Duration"] = _seconds_string(duration_ms, digits=9 if "matroska" in _string(fmt.get("format_name")) else 3)
        fields["BitRate"] = _string(bit_rate or "")
        fields["Width"] = _string(stream.get("width") or "")
        fields["Height"] = _string(stream.get("height") or "")
        fields["Stored_Height"] = _string(stream.get("coded_height") or "")
        fields["Sampled_Width"] = _string(stream.get("coded_width") or "")
        fields["Sampled_Height"] = _string(stream.get("coded_height") or "")
        fields["PixelAspectRatio"] = _ratio_to_decimal(_string(stream.get("sample_aspect_ratio")))
        dar = _ratio_to_decimal(_string(stream.get("display_aspect_ratio")))
        if dar:
            fields["DisplayAspectRatio"] = dar
        fields["FrameRate_Mode"] = _frame_rate_mode(_string(stream.get("avg_frame_rate")), _string(stream.get("r_frame_rate")))
        fields["FrameRate"] = f"{fps:.3f}" if fps is not None else ""
        fields["FrameRate_Num"] = _string(_split_ratio(_string(stream.get("avg_frame_rate") or stream.get("r_frame_rate")))[0])
        fields["FrameRate_Den"] = _string(_split_ratio(_string(stream.get("avg_frame_rate") or stream.get("r_frame_rate")))[1])
        fields["FrameCount"] = _string(frame_count or "")
        color_space = _video_color_space(_string(stream.get("pix_fmt")))
        if color_space:
            fields["ColorSpace"] = color_space
        chroma = _video_chroma_subsampling(_string(stream.get("pix_fmt")))
        if chroma:
            fields["ChromaSubsampling"] = chroma
        bit_depth = self._int_or_none(stream.get("bits_per_raw_sample")) or _bit_depth_from_pix_fmt(_string(stream.get("pix_fmt")))
        if bit_depth is not None:
            fields["BitDepth"] = _string(bit_depth)
        fields["ScanType"] = "Progressive" if _string(stream.get("field_order")) in {"progressive", ""} else _string(stream.get("field_order"))
        bppf = _bits_per_pixel_frame(bit_rate, fps, self._int_or_none(stream.get("width")), self._int_or_none(stream.get("height")))
        if bppf is not None:
            fields["Bits_Pixel_Frame"] = f"{bppf:.3f}"
        fields["StreamSize"] = _string(_estimate_stream_size(bit_rate, duration_ms) or "")
        encoded_lib = _string(tags.get("encoder"))
        if encoded_lib:
            fields["Encoded_Library"] = encoded_lib
            fields["Encoded_Library_Name"] = encoded_lib.split(" ", 1)[0]
            fields["Encoded_Library_Version"] = encoded_lib
        if _string(stream.get("codec_tag_string")).lower() == "avc1":
            fields["extra.CodecConfigurationBox"] = "avcC"
            fields["CodecConfigurationBox"] = "avcC"
        disposition = stream.get("disposition", {}) if isinstance(stream.get("disposition"), dict) else {}
        fields["Default"] = "Yes" if disposition.get("default") else "No"
        fields["Forced"] = "Yes" if disposition.get("forced") else "No"
        colour_range = _color_range_label(_string(stream.get("color_range")))
        if colour_range:
            fields["colour_range"] = colour_range
        matrix = _matrix_coefficients_label(_string(stream.get("color_space")))
        if matrix:
            fields["matrix_coefficients"] = matrix

        # Inform-compatible HDR aliases.
        fields["HDR_Format"] = hdr_format
        fields["HDR_Format_Compatibility"] = hdr_compat
        fields["MaxCLL"] = cll
        fields["MaxFALL"] = fall
        title = _string(tags.get("title"))
        if title:
            fields["Title"] = title
        language = _string(tags.get("language"))
        if language:
            fields["Language"] = language
        return MediaTrack(kind="Video", fields=fields)

    def _build_audio_track(self, stream: dict[str, Any]) -> MediaTrack:
        fields = _ordered_dict()
        codec_name = _string(stream.get("codec_name"))
        tags = stream.get("tags", {}) if isinstance(stream.get("tags"), dict) else {}
        bit_rate = self._int_or_none(stream.get("bit_rate"))
        stream_order = self._int_or_none(stream.get("index"))
        stream_id = _decode_stream_id(stream.get("id"), fallback=(stream_order + 1) if stream_order is not None else None)
        duration_ms = _duration_ms(stream.get("duration"))
        sample_rate = self._int_or_none(stream.get("sample_rate"))
        channels = self._int_or_none(stream.get("channels"))
        samples_per_frame = _samples_per_frame_for_codec(codec_name)
        sampling_count = _estimate_sampling_count(sample_rate, duration_ms)

        fields["StreamOrder"] = _string(stream_order if stream_order is not None else "")
        fields["ID"] = _string(stream_id or "")
        base_format = _codec_label(codec_name)
        fields["Format"] = base_format
        fields["Format_Info"] = _codec_info_short(codec_name, _string(stream.get("codec_long_name")))
        profile = _string(stream.get("profile"))
        if profile:
            fields["Format_AdditionalFeatures"] = profile
            fields["Format_Commercial_IfAny"] = _audio_commercial_name(codec_name, profile)
            if codec_name.lower() == "aac":
                fields["Format"] = f"{base_format} {profile}"
        fields["CodecID"] = _audio_codec_id(codec_name, _string(stream.get("codec_tag_string")))
        fields["Duration"] = _seconds_string(duration_ms, digits=3)
        fields["BitRate_Mode"] = "CBR" if bit_rate else "VBR"
        fields["BitRate"] = _string(bit_rate or "")
        fields["Channels"] = _string(channels or "")
        fields["ChannelPositions"] = _channel_positions(_string(stream.get("channel_layout")), channels)
        fields["ChannelLayout"] = _channel_layout_short(_string(stream.get("channel_layout")), channels)
        if samples_per_frame:
            fields["SamplesPerFrame"] = _string(samples_per_frame)
        fields["SamplingRate"] = _string(sample_rate or "")
        if sampling_count is not None:
            fields["SamplingCount"] = _string(sampling_count)
        if sample_rate and samples_per_frame:
            fields["FrameRate"] = f"{sample_rate / samples_per_frame:.3f}"
        frame_count = self._int_or_none(stream.get("nb_frames"))
        if frame_count is not None:
            fields["FrameCount"] = _string(frame_count)
        bit_depth = self._int_or_none(stream.get("bits_per_sample"))
        if bit_depth:
            fields["BitDepth"] = _string(bit_depth)
        fields["Compression_Mode"] = "Lossy"
        fields["StreamSize"] = _string(_estimate_stream_size(bit_rate, duration_ms) or "")
        encoded_lib = _string(tags.get("encoder"))
        if encoded_lib:
            fields["Encoded_Library"] = encoded_lib
        disposition = stream.get("disposition", {}) if isinstance(stream.get("disposition"), dict) else {}
        fields["Default"] = "Yes" if disposition.get("default") else "No"
        fields["Forced"] = "Yes" if disposition.get("forced") else "No"
        if _string(stream.get("codec_tag_string")).lower() == "mp4a":
            fields["AlternateGroup"] = "1"
        title = _string(tags.get("title"))
        if title:
            fields["Title"] = title
        language = _string(tags.get("language"))
        if language:
            fields["Language"] = language
        return MediaTrack(kind="Audio", fields=fields)

    def _build_text_track(self, stream: dict[str, Any]) -> MediaTrack:
        fields = _ordered_dict()
        codec_name = _string(stream.get("codec_name"))
        tags = stream.get("tags", {}) if isinstance(stream.get("tags"), dict) else {}
        disp = stream.get("disposition", {}) if isinstance(stream.get("disposition"), dict) else {}
        stream_order = self._int_or_none(stream.get("index"))
        stream_id = _decode_stream_id(stream.get("id"), fallback=(stream_order + 1) if stream_order is not None else None)
        duration_ms = _duration_ms(stream.get("duration"))
        fields["StreamOrder"] = _string(stream_order if stream_order is not None else "")
        fields["ID"] = _string(stream_id or "")
        fields["Format"] = "UTF-8" if codec_name == "subrip" else _codec_label(codec_name)
        fields["CodecID"] = _text_codec_id(codec_name)
        fields["CodecID_Info"] = "UTF-8 Plain Text" if codec_name == "subrip" else ""
        fields["Duration"] = _seconds_string(duration_ms, digits=3)
        encoded_lib = _string(tags.get("encoder"))
        if encoded_lib:
            fields["Encoded_Library"] = encoded_lib
        fields["Default"] = "Yes" if disp.get("default") else "No"
        fields["Forced"] = "Yes" if disp.get("forced") else "No"
        title = _string(tags.get("title"))
        if title:
            fields["Title"] = title
        language = _string(tags.get("language"))
        if language:
            fields["Language"] = language
        return MediaTrack(kind="Text", fields=fields)

    # ------------------------------------------------------------------
    # Specialized output renderers
    # ------------------------------------------------------------------

    @staticmethod
    def _public_fields(track: MediaTrack) -> dict[str, str]:
        return _public_fields_module(
            track,
            structured_field_order=_STRUCTURED_FIELD_ORDER,
        )

    @staticmethod
    def _duration_iso8601_from_ms(duration_ms: int | None) -> str:
        return _duration_iso8601_from_ms_module(duration_ms)

    def _render_ebucore(self, report: MediaReport, mode: str, timestamp: datetime | None = None) -> str:
        return _render_ebucore_module(
            self,
            report,
            mode,
            timestamp=timestamp,
            version_text=VERSION_TEXT,
            xml_escape=_xml_escape,
            display_ratio_parts=_display_ratio_parts,
            string=_string,
            trim_float=_trim_float,
            int_or_none=_int_or_none,
            video_profile_label=_video_profile_label,
            scan_type_ebucore=_scan_type_ebucore,
            bitrate_mode_ebucore=_bitrate_mode_ebucore,
            format_text_field_value=_format_text_field_value,
            should_display_text_field=_should_display_text_field,
            seconds_to_ms=_seconds_to_ms,
            codec_label=_codec_label,
            ebucore_xml_to_json=_ebucore_xml_to_json,
        )

    def _render_pbcore(self, report: MediaReport, version: str, timestamp: datetime | None = None) -> str:
        return _render_pbcore_module(
            self,
            report,
            version=version,
            timestamp=timestamp,
            version_text=VERSION_TEXT,
            xml_escape=_xml_escape,
            string=_string,
            utc_file_date_iso=_utc_file_date_iso,
            mime_for_extension=_mime_for_extension,
            pbcore_duration=_pbcore_duration,
            video_profile_label=_video_profile_label,
            codec_label=_codec_label,
            seconds_to_ms=_seconds_to_ms,
            int_or_none=_int_or_none,
            pbcore_duration_ms=_pbcore_duration_ms,
            iso639_2_bibliographic=_iso639_2_bibliographic,
            seconds_ms_to_int_if_needed=_seconds_ms_to_int_if_needed,
        )

    def _render_mpeg7(self, report: MediaReport, profile: str, timestamp: datetime | None = None) -> str:
        return _render_mpeg7_module(
            self,
            report,
            profile=profile,
            timestamp=timestamp,
            version_text=VERSION_TEXT,
            xml_escape=_xml_escape,
            string=_string,
            codec_label=_codec_label,
            mpeg7_avc_level_term_id=_mpeg7_avc_level_term_id,
            mpeg7_audio_presentation_name=_mpeg7_audio_presentation_name,
            mpeg7_duration=_mpeg7_duration,
        )

    def _render_graph(self, report: MediaReport, mode: str) -> str:
        if mode == "GRAPH_DOT":
            lines = ["digraph MediaInfo {", '  rankdir="LR";', '  node [shape=box];']
            for index, track in enumerate(report.tracks, start=1):
                label = track.kind
                lines.append(f'  n{index} [label="{label}"];')
                if index > 1:
                    lines.append(f"  n{index-1} -> n{index};")
            lines.append("}")
            return "\n".join(lines) + "\n"

        # SVG fallback for graph mode.
        width = 180
        row_height = 40
        total_height = max(80, 20 + row_height * len(report.tracks))
        rows: list[str] = []
        y = 20
        for track in report.tracks:
            rows.append(f"<rect x='10' y='{y}' width='{width}' height='28' fill='#f5f5f5' stroke='#444' />")
            rows.append(f"<text x='20' y='{y+19}' font-family='monospace' font-size='12'>{html.escape(track.kind)}</text>")
            y += row_height
        return (
            "<svg xmlns='http://www.w3.org/2000/svg' "
            f"width='{width+20}' height='{total_height}'>"
            + "".join(rows)
            + "</svg>\n"
        )

    def _render_fims(self, report: MediaReport, mode: str) -> str:
        root = ET.Element("fims:Asset", {"xmlns:fims": "http://www.fims.tv"})
        ET.SubElement(root, "fims:Version").text = mode.replace("_", ".")
        ET.SubElement(root, "fims:Identifier").text = report.source
        for track in report.tracks:
            node = ET.SubElement(root, "fims:Track", {"kind": track.kind})
            for key, value in self._public_fields(track).items():
                ET.SubElement(node, "fims:Field", {"name": key}).text = value
        return ET.tostring(root, encoding="utf-8").decode("utf-8") + "\n"

    def _render_niso(self, report: MediaReport) -> str:
        root = ET.Element("mix:mix", {"xmlns:mix": "http://www.loc.gov/mix/v20"})
        basic = ET.SubElement(root, "mix:BasicDigitalObjectInformation")
        general = report.first_track("General")
        if general:
            ET.SubElement(basic, "mix:ObjectIdentifierValue").text = general.fields.get("Complete name", report.source)
            ET.SubElement(basic, "mix:FormatDesignation").text = general.fields.get("Format", "")
            ET.SubElement(basic, "mix:ByteOrder").text = "little-endian"
            ET.SubElement(basic, "mix:Compression").text = general.fields.get("Format", "")

        tech = ET.SubElement(root, "mix:BasicImageInformation")
        for video in report.tracks_by_kind("Video"):
            fields = self._public_fields(video)
            ET.SubElement(tech, "mix:imageWidth").text = fields.get("Width", "")
            ET.SubElement(tech, "mix:imageHeight").text = fields.get("Height", "")
            break

        return ET.tostring(root, encoding="utf-8").decode("utf-8") + "\n"

    # ------------------------------------------------------------------
    # Internal utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_frame_count(stream: dict[str, Any], fps: float | None, duration_ms: int | None) -> int | None:
        nb_frames_raw = _string(stream.get("nb_frames")).strip()
        if nb_frames_raw.isdigit():
            return int(nb_frames_raw)
        tags = stream.get("tags", {}) if isinstance(stream.get("tags"), dict) else {}
        number_of_frames = _string(tags.get("NUMBER_OF_FRAMES")).strip()
        if number_of_frames.isdigit():
            return int(number_of_frames)
        if fps is not None and duration_ms is not None and duration_ms > 0:
            return int(round((duration_ms / 1000.0) * fps))
        return None

    @staticmethod
    def _hdr_fields(stream: dict[str, Any]) -> tuple[str, str, str, str]:
        side_data = stream.get("side_data_list", []) if isinstance(stream.get("side_data_list"), list) else []
        has_dovi = False
        has_hdr10plus = False
        cll = ""
        fall = ""

        for entry in side_data:
            if not isinstance(entry, dict):
                continue
            sd_type = _string(entry.get("side_data_type"))
            if sd_type == "DOVI configuration record":
                has_dovi = True
            if sd_type == "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)":
                has_hdr10plus = True
            if sd_type == "Content light level metadata":
                cll = _string(entry.get("max_content") or entry.get("max_content_light_level"))
                fall = _string(entry.get("max_average") or entry.get("max_frame_average_light_level"))

        hdr_parts: list[str] = []
        if has_dovi:
            hdr_parts.append("Dolby Vision")
        if has_hdr10plus:
            hdr_parts.append("SMPTE ST 2094 App 4")

        transfer = _string(stream.get("color_transfer")).lower()
        if transfer in {"smpte2084", "smpte2084le"} and not hdr_parts:
            hdr_parts.append("HDR10")

        hdr_format = ", ".join(hdr_parts)
        hdr_compat = "HDR10+" if has_hdr10plus else ""
        return hdr_format, hdr_compat, cll, fall

    @staticmethod
    def _split_inform_expression(inform_expr: str) -> tuple[str, str]:
        return _parse_inform_expression_module(inform_expr)

    @staticmethod
    def _render_template(fields: OrderedDict[str, str], template: str) -> str:
        return _render_inform_module(fields, template)

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return None



def _duration_from_stream_tags(stream: dict[str, Any]) -> int | None:
    tags = stream.get("tags", {}) if isinstance(stream.get("tags"), dict) else {}
    raw = _string(tags.get("DURATION"))
    if not raw:
        return None
    m = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$", raw.strip())
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    ss = float(m.group(3))
    total = (hh * 3600.0) + (mm * 60.0) + ss
    return int(round(total * 1000))


def _round_kbps(value: int | None) -> int:
    if value is None:
        return 0
    if value <= 0:
        return 0
    return int(round(value / 1000.0) * 1000)


def _round_aac_cbr_bitrate(value: int | None) -> int:
    if value is None or value <= 0:
        return 0
    return int(round(value / 8000.0) * 8000)


def _parse_time_base(value: str) -> float | None:
    if not value or "/" not in value:
        return None
    n, d = value.split("/", 1)
    try:
        nf = float(n)
        df = float(d)
    except ValueError:
        return None
    if df == 0:
        return None
    return nf / df


def _packet_source_duration_seconds(packets: list[dict[str, Any]], time_base: str) -> float | None:
    if not packets:
        return None
    tb = _parse_time_base(time_base)
    if tb is None:
        # Fallback on packet duration_time sum.
        total = 0.0
        for pkt in packets:
            try:
                total += float(_string(pkt.get("duration_time") or "0"))
            except ValueError:
                pass
        return total if total > 0 else None

    first_pts = _int_or_none(packets[0].get("pts"))
    if first_pts is None:
        first_pts = _int_or_none(packets[0].get("dts"))
    last_pts = _int_or_none(packets[-1].get("pts"))
    if last_pts is None:
        last_pts = _int_or_none(packets[-1].get("dts"))
    last_dur = _int_or_none(packets[-1].get("duration"))
    if first_pts is None or last_pts is None or last_dur is None:
        return None
    span_ticks = (last_pts + last_dur) - first_pts
    if span_ticks <= 0:
        return None
    return span_ticks * tb


def _packet_last_frame_delta_seconds(packets: list[dict[str, Any]], time_base: str) -> float | None:
    if not packets:
        return None
    tb = _parse_time_base(time_base)
    if tb is None:
        return None
    durations = [_int_or_none(pkt.get("duration")) for pkt in packets if _int_or_none(pkt.get("duration")) is not None]
    if len(durations) < 2:
        return None
    nominal = durations[0]
    actual = durations[-1]
    if nominal is None or actual is None:
        return None
    return (actual - nominal) * tb


class _BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.bit_pos = 0

    def read_bits(self, n: int) -> int:
        out = 0
        for _ in range(n):
            byte_index = self.bit_pos // 8
            if byte_index >= len(self.data):
                raise EOFError("bitstream exhausted")
            bit_index = 7 - (self.bit_pos % 8)
            bit = (self.data[byte_index] >> bit_index) & 1
            out = (out << 1) | bit
            self.bit_pos += 1
        return out

    def read_bit(self) -> int:
        return self.read_bits(1)

    def read_ue(self) -> int:
        zeros = 0
        while self.read_bit() == 0:
            zeros += 1
        if zeros == 0:
            return 0
        return ((1 << zeros) - 1) + self.read_bits(zeros)

    def read_se(self) -> int:
        v = self.read_ue()
        return -(v // 2) if v % 2 == 0 else (v + 1) // 2


def _remove_h264_emulation_prevention(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        if i + 2 < len(data) and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            out.extend((0, 0))
            i += 3
            continue
        out.append(data[i])
        i += 1
    return bytes(out)


def _extract_h264_sps_from_extradata(extradata: bytes) -> bytes:
    if not extradata:
        return b""
    # avcC record
    if len(extradata) > 7 and extradata[0] == 1:
        pos = 5
        if pos >= len(extradata):
            return b""
        num_sps = extradata[pos] & 0x1F
        pos += 1
        for _ in range(num_sps):
            if pos + 2 > len(extradata):
                return b""
            sps_len = (extradata[pos] << 8) | extradata[pos + 1]
            pos += 2
            if pos + sps_len > len(extradata):
                return b""
            sps = extradata[pos:pos + sps_len]
            if sps:
                return sps
            pos += sps_len
    # Annex B fallback
    starts = [m.start() for m in re.finditer(b"\x00\x00\x01", extradata)]
    for st in starts:
        nal = extradata[st + 3:]
        if not nal:
            continue
        nal_type = nal[0] & 0x1F
        if nal_type == 7:
            return nal
    return b""


def _parse_h264_sps(sps: bytes) -> dict[str, int]:
    if not sps:
        return {}
    # Strip NAL header.
    rbsp = _remove_h264_emulation_prevention(sps[1:] if len(sps) > 1 else b"")
    br = _BitReader(rbsp)
    out: dict[str, int] = {}
    try:
        profile_idc = br.read_bits(8)
        _ = br.read_bits(8)  # constraint flags + reserved
        _ = br.read_bits(8)  # level_idc
        _ = br.read_ue()  # sps id

        if profile_idc in {
            100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135,
        }:
            chroma_format_idc = br.read_ue()
            if chroma_format_idc == 3:
                _ = br.read_bit()
            _ = br.read_ue()
            _ = br.read_ue()
            _ = br.read_bit()
            scaling = br.read_bit()
            if scaling:
                # Skip scaling list payload conservatively.
                max_lists = 8 if chroma_format_idc != 3 else 12
                for i in range(max_lists):
                    if br.read_bit():
                        size = 16 if i < 6 else 64
                        last_scale = 8
                        next_scale = 8
                        for _ in range(size):
                            if next_scale != 0:
                                delta = br.read_se()
                                next_scale = (last_scale + delta + 256) % 256
                            last_scale = next_scale if next_scale != 0 else last_scale

        _ = br.read_ue()  # log2_max_frame_num_minus4
        poc_type = br.read_ue()
        if poc_type == 0:
            _ = br.read_ue()
        elif poc_type == 1:
            _ = br.read_bit()
            _ = br.read_se()
            _ = br.read_se()
            cycle = br.read_ue()
            for _ in range(cycle):
                _ = br.read_se()

        ref_frames = br.read_ue()
        out["ref_frames"] = ref_frames
        _ = br.read_bit()  # gaps flag
        pic_width_mbs_minus1 = br.read_ue()
        pic_height_map_units_minus1 = br.read_ue()
        frame_mbs_only_flag = br.read_bit()
        if not frame_mbs_only_flag:
            _ = br.read_bit()
        _ = br.read_bit()  # direct_8x8_inference_flag
        frame_cropping_flag = br.read_bit()
        crop_left = crop_right = crop_top = crop_bottom = 0
        if frame_cropping_flag:
            crop_left = br.read_ue()
            crop_right = br.read_ue()
            crop_top = br.read_ue()
            crop_bottom = br.read_ue()

        width = (pic_width_mbs_minus1 + 1) * 16
        height = (pic_height_map_units_minus1 + 1) * 16 * (2 - frame_mbs_only_flag)
        # 4:2:0 default crop units.
        crop_unit_x = 1
        crop_unit_y = 2 - frame_mbs_only_flag
        visible_width = width - (crop_left + crop_right) * crop_unit_x
        visible_height = height - (crop_top + crop_bottom) * crop_unit_y * 2
        if visible_height <= 0:
            visible_height = height
        if visible_width <= 0:
            visible_width = width
        out["stored_width"] = width
        out["stored_height"] = height
        out["visible_width"] = visible_width
        out["visible_height"] = visible_height
    except (EOFError, ValueError):
        return out
    return out


def _extract_x26x_metadata(packet_blobs: list[bytes], marker: str) -> dict[str, str]:
    for blob in packet_blobs:
        if not blob:
            continue
        text = "".join(chr(c) if 32 <= c < 127 else "\x00" for c in blob)
        if marker not in text.lower():
            continue
        start = text.lower().find(marker.lower())
        sub = text[start:start + 5000]
        sub = sub.split("\x00", 1)[0]
        result: dict[str, str] = {}
        if marker == "x264":
            m = re.search(r"x264(?:\s*-\s*|\s+)core\s+([0-9]+)", sub)
            if m:
                result["core"] = m.group(1)
            mopt = re.search(r"options:\s*(.+)$", sub)
            if mopt:
                result["options"] = mopt.group(1).strip()
        elif marker == "x265":
            m = re.search(r"x265\s*\(build\s*([^)]+)\)\s*-\s*([^-\r\n][^\r\n]*?)\s*-\s*H\.265/HEVC", sub)
            if m:
                result["build"] = m.group(1).strip()
                result["version"] = m.group(2).strip()
            else:
                m2 = re.search(r"x265\s*\(build\s*([^)]+)\)", sub)
                if m2:
                    result["version"] = m2.group(1).strip()
            mopt = re.search(r"options:\s*(.+)$", sub)
            if mopt:
                result["options"] = mopt.group(1).strip()
        if result:
            return result
    return {}


def _extract_option_int(options: str, key: str) -> int | None:
    pattern = rf"(?:^|[ /]){re.escape(key)}=([0-9]+)(?:$|[ /])"
    m = re.search(pattern, options)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _normalize_x26x_options(options: str, drop_prefixes: tuple[str, ...] = ()) -> str:
    raw = _string(options).strip()
    if not raw:
        return ""
    if " / " in raw:
        compact = re.sub(r"\s*/\s*", " / ", raw).strip()
        normalized = re.sub(r"\s+", " ", compact)
        if drop_prefixes:
            parts = [p.strip() for p in normalized.split(" / ")]
            parts = [p for p in parts if not any(p.startswith(prefix) for prefix in drop_prefixes)]
            return " / ".join(parts)
        return normalized
    tokens = [token for token in re.split(r"\s+", raw) if token]
    if drop_prefixes:
        tokens = [token for token in tokens if not any(token.startswith(prefix) for prefix in drop_prefixes)]
    return " / ".join(tokens)


def _is_url(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in {"http", "https", "ftp", "sftp", "file"}


def _is_plain_subrip_input(source: str, fmt: dict[str, Any], streams: list[Any]) -> bool:
    if Path(source).suffix.lower() != ".srt":
        return False
    format_name = _string(fmt.get("format_name")).lower()
    if "srt" not in format_name and "subrip" not in format_name:
        return False
    if not streams:
        return True
    stream_kinds = {_string(s.get("codec_type")).lower() for s in streams if isinstance(s, dict)}
    if stream_kinds - {"subtitle"}:
        return False
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        if _string(stream.get("codec_name")).lower() not in {"subrip", "srt"}:
            return False
    return True


def _parse_subrip_stats(path: Path) -> SubripStats:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return SubripStats(0, 0, 0, 0, 0)

    text = raw.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = [block for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    events_total = 0
    duration_end_ms = 0
    min_duration_ms: int | None = None
    lines_count = 0
    lines_max_count_per_event = 0

    time_re = re.compile(
        r"^\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})"
    )

    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip() != ""]
        if not lines:
            continue
        tc_index = 0
        if tc_index < len(lines) and lines[tc_index].strip().isdigit():
            tc_index += 1
        if tc_index >= len(lines):
            continue
        m = time_re.match(lines[tc_index].strip())
        if not m:
            continue
        start_ms = (
            int(m.group(1)) * 3_600_000
            + int(m.group(2)) * 60_000
            + int(m.group(3)) * 1000
            + int(m.group(4).ljust(3, "0")[:3])
        )
        end_ms = (
            int(m.group(5)) * 3_600_000
            + int(m.group(6)) * 60_000
            + int(m.group(7)) * 1000
            + int(m.group(8).ljust(3, "0")[:3])
        )
        event_duration = max(0, end_ms - start_ms)
        event_text_lines = [line for line in lines[tc_index + 1:] if line.strip() != ""]

        events_total += 1
        duration_end_ms = max(duration_end_ms, end_ms)
        min_duration_ms = event_duration if min_duration_ms is None else min(min_duration_ms, event_duration)
        lines_count += len(event_text_lines)
        lines_max_count_per_event = max(lines_max_count_per_event, len(event_text_lines))

    if min_duration_ms is None:
        min_duration_ms = 0
    return SubripStats(
        duration_end_ms=duration_end_ms,
        events_total=events_total,
        events_min_duration_ms=min_duration_ms,
        lines_count=lines_count,
        lines_max_count_per_event=lines_max_count_per_event,
    )


def _human_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return ""
    value = float(size_bytes)
    for unit in ("Bytes", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            if unit == "Bytes":
                return f"{int(value)} Bytes"
            if value >= 100:
                return f"{value:.0f} {unit}"
            if unit in {"MiB", "GiB", "TiB"} and value < 10:
                return f"{value:.2f} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return ""


def _human_size_raw(size_bytes: int | None) -> str:
    if size_bytes is None:
        return ""
    value = float(size_bytes)
    for unit in ("Byte", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            if unit == "Byte":
                return f"{int(value)} Byte"
            if value >= 100:
                return f"{value:.0f} {unit}"
            if unit in {"MiB", "GiB", "TiB"} and value < 10:
                return f"{value:.2f} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return ""


def _human_bitrate(bit_rate: int | None) -> str:
    if bit_rate is None:
        return ""
    if bit_rate >= 10_000_000:
        mbps = bit_rate / 1_000_000
        if mbps >= 100:
            return f"{mbps:.0f} Mb/s"
        return f"{mbps:.1f} Mb/s"
    if bit_rate >= 1_000:
        kbps = bit_rate / 1_000
        if kbps >= 100:
            return f"{kbps:,.0f}".replace(",", " ") + " kb/s"
        return f"{kbps:.1f} kb/s"
    return f"{bit_rate} b/s"


def _parse_ratio(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if ":" in value and "/" not in value:
        left, right = value.split(":", 1)
        try:
            den_f = float(right)
            if den_f == 0:
                return None
            return float(left) / den_f
        except ValueError:
            return None
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            den_f = float(den)
            if den_f == 0:
                return None
            return float(num) / den_f
        except ValueError:
            return None
    try:
        return float(value)
    except ValueError:
        return None


def _duration_ms(seconds: str | int | float | None) -> int | None:
    if seconds is None:
        return None
    try:
        return max(0, int(round(float(seconds) * 1000)))
    except (TypeError, ValueError):
        return None


def _duration_human(ms: int | None) -> str:
    if ms is None:
        return ""
    total_s = ms // 1000
    hours = total_s // 3600
    minutes = (total_s % 3600) // 60
    seconds = total_s % 60
    if hours > 0:
        return f"{hours} h {minutes} min"
    return f"{minutes} min {seconds} s"


def _codec_label(codec_name: str | None) -> str:
    if not codec_name:
        return ""
    return _CODEC_MAP.get(codec_name.lower(), codec_name.upper())


def _string(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _ordered_dict() -> OrderedDict[str, str]:
    return OrderedDict()


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _seconds_string(duration_ms: int | None, digits: int = 3) -> str:
    if duration_ms is None:
        return ""
    seconds = duration_ms / 1000.0
    return f"{seconds:.{digits}f}"


def _seconds_to_ms(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    s = _string(value).strip()
    if not s:
        return None
    try:
        return int(round(float(s) * 1000))
    except ValueError:
        return None


def _duration_media_text(ms: int | None) -> str:
    if ms is None:
        return ""
    sign = "-" if ms < 0 else ""
    total = abs(ms)
    hours = total // 3_600_000
    minutes = (total % 3_600_000) // 60_000
    seconds = (total % 60_000) // 1000
    millis = total % 1000
    if hours > 0:
        if millis:
            return f"{sign}{hours} h {minutes} min {seconds} s {millis} ms"
        return f"{sign}{hours} h {minutes} min {seconds} s"
    if minutes > 0:
        return f"{sign}{minutes} min {seconds} s {millis} ms"
    return f"{sign}{seconds} s {millis} ms"


def _should_display_text_field(track: MediaTrack, key: str) -> bool:
    fmt = _string(track.fields.get("Format")).upper()
    codec_id = _string(track.fields.get("CodecID"))
    in_mp4 = codec_id in {"avc1", "hev1", "mp4a-40-2", "mp4a"}
    if key == "Format_Info":
        if track.kind == "Video":
            return fmt in {"AVC", "HEVC"}
        if track.kind == "Audio":
            return fmt not in {"OPUS"}
    if key == "CodecID_Info":
        if track.kind == "Video":
            return fmt == "AVC"
        return track.kind == "Text"
    if key == "Bits_Pixel_Frame" and track.kind == "Video":
        return fmt in {"AVC", "HEVC"}
    if key in {"Default", "Forced"} and track.kind == "Video":
        return not in_mp4
    if key == "Forced" and track.kind == "Audio":
        return codec_id not in {"mp4a-40-2", "mp4a"}
    if key == "colour_range" and track.kind == "Video":
        return fmt in {"AVC", "VP9", "HEVC"}
    if key in {"matrix_coefficients", "colour_primaries", "transfer_characteristics"} and track.kind == "Video":
        return fmt in {"VP9", "HEVC"}
    if key in {"MasteringDisplay_ColorPrimaries", "MasteringDisplay_Luminance", "MaxCLL", "MaxFALL", "HDR_Format", "HDR_Format_Compatibility"} and track.kind == "Video":
        return fmt in {"HEVC"}
    if key == "Format_Commercial_IfAny" and track.kind == "Audio":
        return _string(track.fields.get("Format_Commercial_IfAny")) != ""
    if key.startswith("extra.dialnorm"):
        if key == "extra.dialnorm_Maximum":
            return _string(track.fields.get(key)) != "" or _string(track.fields.get("extra.dialnorm_Minimum")) != ""
        return _string(track.fields.get(key)) != ""
    return True


def _render_oracle_json_track(track_payload: dict[str, Any]) -> str:
    items = list(track_payload.items())
    if not items:
        return "{}"

    def render_pair(k: str, v: Any) -> str:
        return f"{json.dumps(k, ensure_ascii=False, separators=(',', ':'))}:{json.dumps(v, ensure_ascii=False, separators=(',', ':'))}"

    pairs = [render_pair(k, v) for k, v in items]
    if len(pairs) == 1:
        return "{" + pairs[0] + "}"
    if len(pairs) == 2:
        return "{" + pairs[0] + "," + pairs[1] + "}"
    head_count = 2
    if len(items) >= 3 and items[0][0] == "@type" and items[1][0] == "@typeorder":
        head_count = 3
    if len(pairs) <= head_count:
        return "{" + ",".join(pairs) + "}"
    first_line = "{" + ",".join(pairs[:head_count]) + ","
    body = "\n".join(
        (p + ",") if idx < len(pairs) - 1 else p for idx, p in enumerate(pairs[head_count:], start=head_count)
    )
    return first_line + "\n" + body + "}"


def _structured_order_for_track(track: MediaTrack) -> list[str]:
    ordered = list(_STRUCTURED_FIELD_ORDER.get(track.kind, list(track.fields.keys())))
    if track.kind != "General":
        return ordered
    has_title = _string(track.fields.get("Title")) != "" or _string(track.fields.get("Movie")) != ""
    has_box_sizes = any(_string(track.fields.get(k)) != "" for k in ("HeaderSize", "DataSize", "FooterSize"))
    if has_title and not has_box_sizes and "IsStreamable" in ordered and "Title" in ordered:
        ordered.remove("IsStreamable")
        ordered.insert(ordered.index("Title"), "IsStreamable")
    return ordered


def _display_ratio_parts(value: str) -> tuple[str, str]:
    ratio = _parse_ratio(value)
    if ratio is None or ratio <= 0:
        return ("", "")
    for num, den in ((16, 9), (4, 3), (1, 1), (21, 9)):
        if abs((num / den) - ratio) < 0.01:
            return (str(num), str(den))
    return ("", "")


def _format_text_field_value(
    key: str,
    value: str,
    track: MediaTrack,
    general_file_size: int | None,
) -> str:
    if value == "":
        return ""

    if key == "CodecID":
        if track.kind == "General":
            compat = _string(track.fields.get("CodecID_Compatible"))
            if compat:
                return f"{value} ({compat})"
        return value

    if key == "CodecID_Compatible":
        return ""

    if key == "UniqueID":
        try:
            raw_uid = int(_string(value))
        except ValueError:
            raw_uid = None
        if track.kind == "General" and raw_uid is not None:
            return f"{value} (0x{raw_uid:X})"
        return value

    if key == "Format":
        if track.kind == "Audio":
            additional = _string(track.fields.get("Format_AdditionalFeatures"))
            if _string(value).upper() == "AAC" and additional:
                return f"{value} {additional}"
        return value

    if key == "Language":
        mapping = {
            "en": "English",
            "fr": "French",
            "de": "German",
            "es": "Spanish",
            "it": "Italian",
            "pt": "Portuguese",
            "ja": "Japanese",
            "zh": "Chinese",
            "ru": "Russian",
        }
        return mapping.get(value.lower(), value)

    if key in {"FileSize", "StreamSize", "Source_StreamSize"}:
        raw = _int_or_none(value)
        if raw is None:
            return value
        human = _human_size(raw)
        if key != "FileSize" and general_file_size and general_file_size > 0:
            pct = int(round((raw / general_file_size) * 100))
            return f"{human} ({pct}%)"
        return human

    if key in {"Duration", "Source_Duration", "Duration_End"}:
        return _duration_media_text(_seconds_to_ms(value))

    if key == "Events_MinDuration":
        ms = _seconds_to_ms(value)
        if ms is None:
            return value
        sign = "-" if ms < 0 else ""
        total = abs(ms)
        seconds = total // 1000
        millis = total % 1000
        return f"{sign}{seconds} s {millis} ms"

    if key == "Source_Duration_LastFrame":
        ms = _seconds_to_ms(value)
        if ms is None:
            return value
        sign = "-" if ms < 0 else ""
        return f"{sign}{abs(ms)} ms"

    if key in {"OverallBitRate", "BitRate"}:
        human = _human_bitrate(_int_or_none(value))
        return human or value

    if key == "HDR_Format":
        compat = _string(track.fields.get("HDR_Format_Compatibility"))
        if compat:
            return f"{value}, {compat} compatible"
        return value

    if key == "HDR_Format_Compatibility":
        return ""

    if key == "FrameRate":
        try:
            fps_txt = f"{float(value):.3f} FPS"
            if track.kind == "Video":
                num = _int_or_none(track.fields.get("FrameRate_Num"))
                den = _int_or_none(track.fields.get("FrameRate_Den"))
                if num and den and den > 0 and den != 1:
                    fps_txt = f"{fps_txt[:-4]} ({num}/{den}) FPS"
            elif track.kind == "Audio":
                spf = _string(track.fields.get("SamplesPerFrame"))
                if spf:
                    return f"{fps_txt} ({spf} SPF)"
            return fps_txt
        except ValueError:
            return value

    if key == "SamplingRate":
        sr = _int_or_none(value)
        if sr is None:
            return value
        if sr >= 1000:
            return f"{sr / 1000.0:.1f} kHz"
        return f"{sr} Hz"

    if key == "Width":
        raw = _int_or_none(value)
        if raw is not None:
            return f"{raw:,}".replace(",", " ") + " pixels"
        return f"{value} pixels"

    if key == "Height":
        raw = _int_or_none(value)
        if raw is not None:
            return f"{raw:,}".replace(",", " ") + " pixels"
        return f"{value} pixels"

    if key == "BitDepth":
        return f"{value} bits"

    if key in {"Width_Original", "Height_Original"}:
        raw = _int_or_none(value)
        if raw is not None:
            return f"{raw:,}".replace(",", " ") + " pixels"
        return value

    if key == "FrameRate_Mode":
        mapping = {"CFR": "Constant", "VFR": "Variable"}
        return mapping.get(value, value)

    if key == "BitRate_Mode":
        mapping = {"CBR": "Constant", "VBR": "Variable"}
        return mapping.get(value, value)

    if key == "Format_Profile" and track.kind == "Video":
        level = _string(track.fields.get("Format_Level"))
        if level and "@" not in value:
            suffix = ""
            if _string(track.fields.get("Format")) == "HEVC":
                tier = _string(track.fields.get("Format_Tier"))
                if tier:
                    suffix = f"@{tier}"
            return f"{value}@L{level}{suffix}"
        return value

    if key == "Format_Version" and track.kind == "General":
        stripped = value.strip()
        if stripped.isdigit():
            return f"Version {stripped}"
        return value

    if key == "DisplayAspectRatio":
        try:
            val = float(value)
        except ValueError:
            return value
        for num, den in ((16, 9), (4, 3), (1, 1), (21, 9)):
            if abs((num / den) - val) < 0.01:
                return f"{num}:{den}"
        return f"{val:.3f}"

    if key == "Channels":
        ch = _int_or_none(value)
        if ch is None:
            return value
        if ch == 1:
            return "1 channel"
        return f"{ch} channels"

    if key == "Format_Settings_RefFrames":
        return f"{value} frames"

    if key in {"Default", "Forced"}:
        return "Yes" if value.lower() in {"yes", "1", "true"} else "No"

    if key == "ServiceKind":
        if value == "CM":
            return "Complete Main"
        return value

    if key in {"MaxCLL", "MaxFALL"}:
        raw = _int_or_none(value)
        if raw is None:
            return value
        return f"{raw:,}".replace(",", " ") + " cd/m2"

    if key.startswith("extra.dialnorm"):
        return f"{value} dB"

    if key == "Encoded_Library":
        if value.startswith("x264 - core "):
            return value.replace("x264 - core ", "x264 core ", 1)
        if value.startswith("x265 - "):
            return value.replace("x265 - ", "x265 ", 1)

    return value


def _format_text_raw_value(
    display_key: str,
    source_key: str,
    value: str,
    track: MediaTrack,
    general_file_size: int | None,
) -> str:
    if source_key == "UniqueID":
        return _format_text_field_value(source_key, value, track, general_file_size)
    if source_key == "Format" and track.kind == "Audio":
        additional = _string(track.fields.get("Format_AdditionalFeatures"))
        if _string(value).upper() == "AAC" and additional:
            return f"{value} {additional}"
        return value
    if source_key == "CodecID" and display_key == "CodecID/String" and track.kind == "General":
        compat = _string(track.fields.get("CodecID_Compatible"))
        if compat:
            return f"{value} ({compat})"
        return value
    if source_key in {"FileSize", "StreamSize", "Source_StreamSize"}:
        raw = _int_or_none(value)
        if raw is None:
            return value
        human = _human_size_raw(raw)
        if source_key != "FileSize" and general_file_size and general_file_size > 0:
            pct = int(round((raw / general_file_size) * 100))
            return f"{human} ({pct}%)"
        return human
    if source_key in {"Duration", "Source_Duration", "Duration_End"}:
        ms = _seconds_to_ms(value)
        if ms is None:
            return value
        sign = "-" if ms < 0 else ""
        total = abs(ms)
        s = total // 1000
        msec = total % 1000
        return f"{sign}{s}s {msec}ms"
    if source_key == "Events_MinDuration":
        ms = _seconds_to_ms(value)
        if ms is None:
            return value
        sign = "-" if ms < 0 else ""
        total = abs(ms)
        s = total // 1000
        msec = total % 1000
        return f"{sign}{s}s {msec}ms"
    if source_key == "Source_Duration_LastFrame":
        ms = _seconds_to_ms(value)
        if ms is None:
            return value
        return f"{ms}ms"
    if source_key in {"OverallBitRate", "BitRate"}:
        br = _int_or_none(value)
        if br is None:
            return value
        if br >= 1000:
            kbps = br / 1000.0
            if kbps >= 100:
                return f"{kbps:.0f} Kbps"
            return f"{kbps:.1f} Kbps"
        return f"{br} bps"
    if source_key == "FrameRate":
        try:
            fps = float(value)
        except ValueError:
            return value
        base = f"{fps:.3f} fps"
        if track.kind == "Video":
            num = _int_or_none(track.fields.get("FrameRate_Num"))
            den = _int_or_none(track.fields.get("FrameRate_Den"))
            if num and den and den > 0 and den != 1:
                return f"{base[:-4]} ({num}/{den}) fps"
        if track.kind == "Audio":
            spf = _string(track.fields.get("SamplesPerFrame"))
            if spf:
                return f"{base} ({spf} SPF)"
        return base
    if source_key == "SamplingRate":
        sr = _int_or_none(value)
        if sr is None:
            return value
        return f"{sr/1000.0:.1f} KHz"
    if source_key == "Width":
        return f"{value} pixel"
    if source_key == "Height":
        return f"{value} pixel"
    if source_key == "DisplayAspectRatio":
        return _format_text_field_value(source_key, value, track, general_file_size)
    if source_key == "Encoded_Library":
        return _format_text_field_value(source_key, value, track, general_file_size)
    if source_key == "Format_Version":
        return _format_text_field_value(source_key, value, track, general_file_size)
    if source_key == "FrameRate_Mode":
        return _string(track.fields.get("FrameRate_Mode", value))
    if source_key == "BitDepth":
        return f"{value} bit"
    if source_key in {"Width_Original", "Height_Original"}:
        raw = _int_or_none(value)
        if raw is not None:
            return f"{raw} pixel"
        return value
    if source_key == "Format_Settings_RefFrames":
        return f"{value} frame"
    if source_key == "Format_Profile" and track.kind == "Video":
        return _format_text_field_value("Format_Profile", value, track, general_file_size)
    if source_key in {"MaxCLL", "MaxFALL"}:
        raw = _int_or_none(value)
        if raw is None:
            return value
        return f"{raw} cd/m2"
    if source_key in {
        "HDR_Format",
        "MasteringDisplay_ColorPrimaries",
        "MasteringDisplay_Luminance",
    }:
        return _format_text_field_value(source_key, value, track, general_file_size)
    if source_key == "BitRate_Mode":
        return value
    if source_key in {"Default", "Forced"}:
        return _format_text_field_value(source_key, value, track, general_file_size)
    if source_key == "ServiceKind":
        return _format_text_field_value(source_key, value, track, general_file_size)
    if source_key.startswith("extra.dialnorm"):
        return _format_text_field_value(source_key, value, track, general_file_size)
    if source_key == "Channels":
        ch = _int_or_none(value)
        if ch is None:
            return value
        if ch == 1:
            return "1 channel"
        return f"{ch} channels"
    return value


def _container_format_label(format_name: str, source: str = "") -> str:
    fmt = format_name.lower()
    if "mov,mp4" in fmt:
        return "MPEG-4"
    if "matroska" in fmt and Path(source).suffix.lower() == ".webm":
        return "WebM"
    if "matroska" in fmt:
        return "Matroska"
    if "webm" in fmt:
        return "WebM"
    if "subrip" in fmt or Path(source).suffix.lower() == ".srt":
        return "SubRip"
    token = format_name.split(",", 1)[0].strip()
    if not token:
        return ""
    return token.upper() if len(token) <= 4 else token.title()


def _container_format_profile(format_name: str, source: str = "") -> str:
    fmt = format_name.lower()
    if "mov,mp4" in fmt:
        return "Base Media"
    if "matroska" in fmt and Path(source).suffix.lower() == ".webm":
        return ""
    if "matroska" in fmt:
        return "Version 4"
    return ""


def _format_compatible_brands(value: str) -> str:
    compact = value.strip()
    if not compact:
        return ""
    if "/" in compact:
        return compact
    if " " not in compact and len(compact) % 4 == 0 and compact.isalnum():
        parts = [compact[i:i + 4] for i in range(0, len(compact), 4)]
        return "/".join(parts)
    parts = [p for p in compact.split() if p]
    return "/".join(parts)


def _decode_stream_id(value: Any, fallback: int | None = None) -> int | None:
    if value is None or value == "":
        return fallback
    raw = _string(value).strip()
    if raw.startswith("0x"):
        try:
            return int(raw, 16)
        except ValueError:
            return fallback
    if ":" in raw:
        raw = raw.rsplit(":", 1)[-1].strip()
    try:
        return int(raw)
    except ValueError:
        return fallback


def _video_codec_id(codec_name: str, container_name: str, codec_tag: str) -> str:
    tag = codec_tag.strip()
    if tag in {"[0][0][0][0]", "0x0000", "0"}:
        tag = ""
    if tag and tag.lower() != "0x0000":
        return tag
    codec = codec_name.lower()
    container = container_name.lower()
    in_mp4 = "mov,mp4" in container
    if codec in {"h264", "avc"}:
        return "avc1" if in_mp4 else "V_MPEG4/ISO/AVC"
    if codec in {"hevc", "h265"}:
        return "hev1" if in_mp4 else "V_MPEGH/ISO/HEVC"
    if codec == "av1":
        return "av01" if in_mp4 else "V_AV1"
    if codec == "vp9":
        return "vp09" if in_mp4 else "V_VP9"
    if codec == "vp8":
        return "vp08" if in_mp4 else "V_VP8"
    if codec == "mpeg2video":
        return "mp2v" if in_mp4 else "V_MPEG2"
    return codec_name


def _audio_codec_id(codec_name: str, codec_tag: str) -> str:
    tag = codec_tag.strip()
    if tag in {"[0][0][0][0]", "0x0000", "0"}:
        tag = ""
    if tag and tag.lower() != "0x0000":
        if codec_name.lower() == "aac":
            if tag.upper().startswith("A_AAC"):
                return "A_AAC-2" if "-" not in tag else tag
            return "mp4a-40-2"
        return tag
    codec = codec_name.lower()
    mapping = {
        "aac": "mp4a-40-2",
        "ac3": "A_AC3",
        "eac3": "A_EAC3",
        "truehd": "A_TRUEHD",
        "dts": "A_DTS",
        "opus": "A_OPUS",
        "flac": "A_FLAC",
    }
    return mapping.get(codec, codec_name)


def _text_codec_id(codec_name: str) -> str:
    codec = codec_name.lower()
    mapping = {
        "subrip": "S_TEXT/UTF8",
        "ass": "S_TEXT/ASS",
        "ssa": "S_TEXT/SSA",
        "mov_text": "tx3g",
        "hdmv_pgs_subtitle": "S_HDMV/PGS",
    }
    return mapping.get(codec, codec_name)


def _ratio_to_decimal(value: str) -> str:
    ratio = _parse_ratio(value)
    if ratio is None:
        return ""
    return f"{ratio:.3f}"


def _split_ratio(value: str) -> tuple[int, int]:
    if "/" in value:
        left, right = value.split("/", 1)
        try:
            num = int(float(left))
            den = int(float(right))
            if den == 0:
                return (0, 1)
            return (num, den)
        except ValueError:
            return (0, 1)
    try:
        num = int(round(float(value)))
    except ValueError:
        return (0, 1)
    return (num, 1)


def _frame_rate_mode(avg_frame_rate: str, r_frame_rate: str) -> str:
    avg = _parse_ratio(avg_frame_rate)
    ref = _parse_ratio(r_frame_rate)
    if avg is None and ref is None:
        return ""
    if avg is None or ref is None:
        return "VFR"
    return "CFR" if abs(avg - ref) < 1e-3 else "VFR"


def _video_color_space(pix_fmt: str) -> str:
    pix = pix_fmt.lower()
    if pix.startswith("yuv") or pix.startswith("nv"):
        return "YUV"
    if pix.startswith("gbr") or pix.startswith("rgb"):
        return "RGB"
    return ""


def _video_chroma_subsampling(pix_fmt: str) -> str:
    pix = pix_fmt.lower()
    if "420" in pix:
        return "4:2:0"
    if "422" in pix:
        return "4:2:2"
    if "444" in pix:
        return "4:4:4"
    if "411" in pix:
        return "4:1:1"
    if "410" in pix:
        return "4:1:0"
    return ""


def _bit_depth_from_pix_fmt(pix_fmt: str) -> int | None:
    if not pix_fmt:
        return None
    m = re.search(r"(\d+)(?:le|be)?$", pix_fmt.lower())
    if m:
        value = int(m.group(1))
        if value > 0:
            return value
    lowered = pix_fmt.lower()
    if lowered.startswith("yuv") or lowered.startswith("nv") or lowered.startswith("gbr") or lowered.startswith("rgb"):
        return 8
    return None


def _bits_per_pixel_frame(bit_rate: int | None, fps: float | None, width: int | None, height: int | None) -> float | None:
    if not bit_rate or not fps or not width or not height:
        return None
    if fps <= 0 or width <= 0 or height <= 0:
        return None
    return bit_rate / (fps * width * height)


def _estimate_stream_size(bit_rate: int | None, duration_ms: int | None) -> int | None:
    if not bit_rate or duration_ms is None:
        return None
    if bit_rate <= 0 or duration_ms < 0:
        return None
    return int(round((bit_rate * duration_ms) / 8000.0))


def _mkv_stream_bitrate(stream_size: int, duration_ms: int) -> int:
    if stream_size <= 0 or duration_ms <= 0:
        return 0
    # MediaInfo MKV bitrate on this corpus is slightly below the direct size/duration rounding.
    return max(0, int((stream_size * 8000) / duration_ms) - 2)


def _color_range_label(value: str) -> str:
    v = value.lower()
    if v in {"tv", "mpeg"}:
        return "Limited"
    if v in {"pc", "jpeg"}:
        return "Full"
    return ""


def _matrix_coefficients_label(value: str) -> str:
    v = value.lower()
    mapping = {
        "bt709": "BT.709",
        "bt2020nc": "BT.2020 non-constant",
        "bt2020c": "BT.2020 constant",
        "smpte170m": "BT.601 NTSC",
        "bt470bg": "BT.601 PAL",
        "gbr": "Identity",
    }
    return mapping.get(v, "")


def _samples_per_frame_for_codec(codec_name: str) -> int | None:
    mapping = {
        "aac": 1024,
        "ac3": 1536,
        "eac3": 1536,
        "truehd": 1536,
        "opus": 960,
    }
    return mapping.get(codec_name.lower())


def _estimate_sampling_count(sample_rate: int | None, duration_ms: int | None) -> int | None:
    if not sample_rate or duration_ms is None:
        return None
    if sample_rate <= 0 or duration_ms < 0:
        return None
    return int(round(sample_rate * duration_ms / 1000.0))


def _audio_commercial_name(codec_name: str, profile: str) -> str:
    codec = codec_name.lower()
    prof = profile.lower()
    if codec == "aac":
        if "lc" in prof:
            return "AAC LC"
        if "he-aacv2" in prof or "hev2" in prof:
            return "HE-AACv2"
        if "he-aac" in prof or "hev1" in prof:
            return "HE-AAC"
        return "AAC"
    if codec == "ac3":
        return "Dolby Digital"
    if codec == "eac3":
        return "Dolby Digital Plus"
    if codec == "truehd":
        return "Dolby TrueHD"
    if codec == "dts":
        return "DTS"
    if codec == "opus":
        return "Opus"
    if codec == "flac":
        return "FLAC"
    return profile


def _channel_positions(layout: str, channels: int | None) -> str:
    known = {
        "mono": "Front: C",
        "stereo": "Front: L R",
        "2.1": "Front: L R, LFE",
        "3.0": "Front: L C R",
        "3.1": "Front: L C R, LFE",
        "4.0": "Front: L C R, Back: C",
        "4.1": "Front: L C R, Back: C, LFE",
        "5.0": "Front: L C R, Side: L R",
        "5.1": "Front: L C R, Side: L R, LFE",
        "5.1(side)": "Front: L C R, Side: L R, LFE",
        "7.1": "Front: L C R, Side: L R, Back: L R, LFE",
    }
    if layout in known:
        return known[layout]
    if channels == 1:
        return "Front: C"
    if channels == 2:
        return "Front: L R"
    return ""


def _channel_layout_short(layout: str, channels: int | None) -> str:
    if layout:
        mapping = {
            "mono": "M",
            "stereo": "L R",
            "5.1": "L R C LFE Ls Rs",
            "5.1(side)": "L R C LFE Ls Rs",
            "7.1": "L R C LFE Lb Rb Ls Rs",
        }
        if layout in mapping:
            return mapping[layout]
        return layout
    if channels == 1:
        return "M"
    if channels == 2:
        return "L R"
    return ""


def _trim_float(value: str) -> str:
    raw = _string(value).strip()
    if not raw:
        return ""
    try:
        return str(int(float(raw))) if float(raw).is_integer() else str(float(raw))
    except ValueError:
        return raw


def _video_profile_label(fields: dict[str, str]) -> str:
    profile = _string(fields.get("Format_Profile"))
    level = _string(fields.get("Format_Level"))
    if profile and level and "@" not in profile:
        tier = _string(fields.get("Format_Tier"))
        suffix = f"@{tier}" if tier else ""
        return f"{profile}@L{level}{suffix}"
    return profile


def _scan_type_ebucore(scan_type: str) -> str:
    value = _string(scan_type).strip().lower()
    if value == "progressive":
        return "progressive"
    if value:
        return value
    return "unknown"


def _bitrate_mode_ebucore(mode: str) -> str:
    m = _string(mode).strip().upper()
    if m == "CBR":
        return "constant"
    if m == "VBR":
        return "variable"
    return _string(mode).lower()


def _utc_file_date_iso(value: str) -> str:
    raw = _string(value).strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f UTC", "%Y-%m-%d %H:%M:%S UTC"):
        try:
            dt = datetime.strptime(raw, fmt)
            if "%f" in fmt:
                ms = int(dt.microsecond / 1000)
                return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return raw


def _mime_for_extension(ext: str) -> str:
    key = _string(ext).strip().lower().lstrip(".")
    mapping = {
        "mp4": "video/mp4",
        "mkv": "video/x-matroska",
        "webm": "video/webm",
        "srt": "text/plain",
    }
    return mapping.get(key, f"application/{key}" if key else "application/octet-stream")


def _iso639_2_bibliographic(value: str) -> str:
    code = _string(value).strip().lower()
    if not code:
        return ""
    mapping = {
        "en": "eng",
        "fr": "fra",
        "de": "ger",
        "es": "spa",
        "it": "ita",
        "pt": "por",
        "ja": "jpn",
        "zh": "chi",
        "ru": "rus",
    }
    if len(code) == 3:
        return code
    return mapping.get(code, code)


def _mpeg7_avc_level_term_id(level_value: str) -> str:
    level = _string(level_value).strip()
    mapping = {
        "1.2": "3",
        "3.1": "9",
        "6.2": "20",
    }
    if level in mapping:
        return mapping[level]
    return "3"


def _pbcore_duration(seconds_value: str, frame_rate_value: str | None = None) -> str:
    ms = _seconds_to_ms(seconds_value)
    if ms is None:
        return "00:00:00:00"
    total_s = ms / 1000.0
    h = int(total_s // 3600)
    m = int((total_s % 3600) // 60)
    s = int(total_s % 60)
    frac = total_s - int(total_s)
    fps = 24
    has_frame_rate = False
    try:
        parsed_fps = float(_string(frame_rate_value))
        if parsed_fps > 0:
            fps = int(round(parsed_fps))
            has_frame_rate = True
    except ValueError:
        pass
    if not has_frame_rate:
        millis = ms % 1000
        return f"{h:02d}:{m:02d}:{s:02d}.{millis:03d}"
    if frac > 0:
        frames = int(frac * fps)
        if frames >= fps:
            s += 1
            frames = 0
        return f"{h:02d}:{m:02d}:{s:02d}:{frames:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}:00"


def _pbcore_duration_ms(seconds_value: str) -> str:
    ms = _seconds_to_ms(seconds_value)
    if ms is None:
        return "00:00:00.000"
    total_s = ms // 1000
    millis = ms % 1000
    h = total_s // 3600
    m = (total_s % 3600) // 60
    s = total_s % 60
    return f"{h:02d}:{m:02d}:{s:02d}.{millis:03d}"


def _seconds_ms_to_int_if_needed(key: str, value: str) -> str:
    if key in {"Source_Duration", "Source_Duration_LastFrame"}:
        ms = _seconds_to_ms(value)
        if ms is not None:
            return str(ms)
    return value


def _mpeg7_duration(seconds_value: str, frame_rate_value: str | None = None) -> str:
    ms = _seconds_to_ms(seconds_value)
    if ms is None:
        return "PT0H0M0S0N0F"
    total_s = ms / 1000.0
    h = int(total_s // 3600)
    m = int((total_s % 3600) // 60)
    s = int(total_s % 60)
    frac = total_s - int(total_s)
    fps = 24
    try:
        parsed_fps = float(_string(frame_rate_value))
        if parsed_fps > 0:
            fps = int(round(parsed_fps))
    except ValueError:
        pass
    n = int(frac * fps)
    if n >= fps:
        s += 1
        n = 0
    return f"PT{h}H{m}M{s}S{n}N{fps}F"


def _mpeg7_audio_presentation_name(channels_value: str) -> str:
    channels = _int_or_none(channels_value)
    if channels is None:
        return "unknown"
    if channels == 1:
        return "mono"
    if channels == 2:
        return "stereo"
    return f"{channels} channels"


def _ebucore_xml_to_json(xml_text: str) -> str:
    parsed = minidom.parseString(xml_text.encode("utf-8"))
    root = parsed.documentElement

    def convert(node: Any) -> OrderedDict[str, Any]:
        result: OrderedDict[str, Any] = OrderedDict()
        if node.attributes:
            for i in range(node.attributes.length):
                attr = node.attributes.item(i)
                if attr is None:
                    continue
                name = attr.name
                if name.startswith("xmlns") or name == "xsi:schemaLocation":
                    continue
                result[f"@{name}"] = attr.value

        children = [child for child in node.childNodes if child.nodeType == child.ELEMENT_NODE]
        text_chunks = [
            child.data.strip()
            for child in node.childNodes
            if child.nodeType in (child.TEXT_NODE, child.CDATA_SECTION_NODE) and child.data.strip()
        ]
        if text_chunks:
            result["#value"] = "".join(text_chunks)

        for child in children:
            payload = convert(child)
            existing = result.get(child.nodeName)
            if existing is None:
                result[child.nodeName] = [payload]
            else:
                existing.append(payload)
        return result

    payload: OrderedDict[str, Any] = OrderedDict()
    payload[root.nodeName] = convert(root)
    rendered = json.dumps(payload, ensure_ascii=False, indent="\t")
    rendered = re.sub(r"(?m)^(\s*)\{\}$", r"\1{\n\1}", rendered)
    return rendered + "\n\n"


def _codec_info_short(codec_name: str, long_name: str, codec_id: bool = False) -> str:
    codec = codec_name.lower()
    if codec in {"h264", "avc"}:
        return "Advanced Video Coding" if codec_id else "Advanced Video Codec"
    if codec in {"hevc", "h265"}:
        return "High Efficiency Video Coding"
    if codec == "aac":
        return "Advanced Audio Codec Low Complexity"
    if codec == "ac3":
        return "Dolby AC-3"
    if codec == "eac3":
        return "Enhanced AC-3"
    if codec == "opus":
        return "Opus"
    return long_name


def _xml_escape(value: str) -> str:
    return html.escape(value, quote=True)
