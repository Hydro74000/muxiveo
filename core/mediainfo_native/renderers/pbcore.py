"""PBCore renderer (report-driven)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


class _EngineLike(Protocol):
    def _public_fields(self, track: Any) -> dict[str, str]: ...


class _ReportLike(Protocol):
    source: str

    def first_track(self, kind: str) -> Any: ...
    def tracks_by_kind(self, kind: str) -> list[Any]: ...


def render_pbcore(
    engine: _EngineLike,
    report: _ReportLike,
    version: str,
    *,
    timestamp: datetime | None = None,
    version_text: str = "MediaInfoLib - v26.01",
    xml_escape: Callable[[str], str] | None = None,
    string: Callable[[Any], str] | None = None,
    utc_file_date_iso: Callable[[str], str] | None = None,
    mime_for_extension: Callable[[str], str] | None = None,
    pbcore_duration: Callable[[str, str], str] | None = None,
    video_profile_label: Callable[[dict[str, str]], str] | None = None,
    codec_label: Callable[[str | None], str] | None = None,
    seconds_to_ms: Callable[[str | int | float | None], int | None] | None = None,
    int_or_none: Callable[[Any], int | None] | None = None,
    pbcore_duration_ms: Callable[[str], str] | None = None,
    iso639_2_bibliographic: Callable[[str], str] | None = None,
    seconds_ms_to_int_if_needed: Callable[[str, str], str] | None = None,
) -> str:
    required = [
        xml_escape,
        string,
        utc_file_date_iso,
        mime_for_extension,
        pbcore_duration,
        video_profile_label,
        codec_label,
        seconds_to_ms,
        int_or_none,
        pbcore_duration_ms,
        iso639_2_bibliographic,
        seconds_ms_to_int_if_needed,
    ]
    if any(dep is None for dep in required):
        raise ValueError("Renderer dependencies missing for PBCore renderer")

    general = report.first_track("General")
    videos = report.tracks_by_kind("Video")
    audios = report.tracks_by_kind("Audio")
    texts = report.tracks_by_kind("Text")
    is_mp4_container = bool(general and string(general.fields.get("Format")) == "MPEG-4")
    ts = timestamp or datetime.now(timezone.utc)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<!-- Generated at {ts.strftime('%Y-%m-%dT%H:%M:%SZ')} by {version_text} -->",
        '<pbcoreInstantiationDocument xsi:schemaLocation="http://www.pbcore.org/PBCore/PBCoreNamespace.html https://raw.githubusercontent.com/WGBH/PBCore_2.1/master/pbcore-2.1.xsd" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns="http://www.pbcore.org/PBCore/PBCoreNamespace.html">',
        f'\t<instantiationIdentifier source="File Name">{xml_escape(Path(report.source).name)}</instantiationIdentifier>',
    ]

    if general:
        if general.fields.get("UniqueID"):
            lines.append(f'\t<instantiationIdentifier source="SegmentUID">{xml_escape(general.fields.get("UniqueID", ""))}</instantiationIdentifier>')
        mod = string(general.fields.get("File_Modified_Date", ""))
        if mod:
            lines.append(f'\t<instantiationDate dateType="file modification">{xml_escape(utc_file_date_iso(mod))}</instantiationDate>')
        lines.append(f'\t<instantiationDigital>{xml_escape(mime_for_extension(general.fields.get("FileExtension", "")))}</instantiationDigital>')
        version_ann = string(general.fields.get("Format_Version"))
        format_profile = string(general.fields.get("Format_Profile"))
        if is_mp4_container and format_profile:
            version_attr = f' profile="{xml_escape(format_profile)}"'
        else:
            version_attr = f' annotation="Version {xml_escape(version_ann)}"' if version_ann else ""
        lines.append(f'\t<instantiationStandard{version_attr}>{xml_escape(general.fields.get("Format", ""))}</instantiationStandard>')
        lines.append(f'\t<instantiationLocation>{xml_escape(report.source)}</instantiationLocation>')
        lines.append("\t<instantiationMediaType>Moving Image</instantiationMediaType>")
        if general.fields.get("FileSize"):
            lines.append(f'\t<instantiationFileSize unitsOfMeasure="byte">{xml_escape(general.fields.get("FileSize", ""))}</instantiationFileSize>')
        lines.append(
            f'\t<instantiationDuration>{xml_escape(pbcore_duration(general.fields.get("Duration", ""), general.fields.get("FrameRate", "")))}</instantiationDuration>'
        )
        if general.fields.get("OverallBitRate"):
            lines.append(f'\t<instantiationDataRate unitsOfMeasure="bit/second">{xml_escape(general.fields.get("OverallBitRate", ""))}</instantiationDataRate>')
        lines.append(f"\t<instantiationTracks>{len(videos) + len(audios) + len(texts)}</instantiationTracks>")
        if audios:
            configs: list[str] = []
            for audio_track in audios:
                af = engine._public_fields(audio_track)
                channel_word = "channel" if af.get("Channels") == "1" else "channels"
                configs.append(f'Track {af.get("ID", "1")}: {af.get("Channels", "")} {channel_word} ({af.get("ChannelLayout", "")})')
            if not is_mp4_container and string(general.fields.get("Format")) == "Matroska" and texts:
                if len(configs) == 1:
                    configs.append(configs[0])
                elif len(configs) >= 2:
                    configs.extend([configs[0], configs[0]])
            lines.append(f'\t<instantiationChannelConfiguration>{xml_escape(", ".join(configs))}</instantiationChannelConfiguration>')

    def append_video(track: Any) -> None:
        f = engine._public_fields(track)
        if f.get("HDR_Format_Compatibility") and not f.get("HDR_Format_Commercial"):
            f["HDR_Format_Commercial"] = f.get("HDR_Format_Compatibility", "")
        default_flag = string(f.get("Default", "No"))
        forced_flag = string(f.get("Forced", "No"))
        id_ann = ""
        if is_mp4_container:
            if default_flag != "No" or forced_flag != "No":
                bits: list[str] = []
                if default_flag != "No":
                    bits.append(f"default:{xml_escape(default_flag)}")
                if forced_flag != "No":
                    bits.append(f"forced:{xml_escape(forced_flag)}")
                id_ann = f' annotation="{" ".join(bits)}"'
        else:
            id_ann = f' annotation="default:{xml_escape(default_flag)} forced:{xml_escape(forced_flag)}"'
        ratio_num = xml_escape(f.get("FrameRate_Num", ""))
        ratio_den = xml_escape(f.get("FrameRate_Den", ""))
        scan = xml_escape(f.get("ScanType", ""))
        frame_annotation = f"rational_frame_rate:{ratio_num}/{ratio_den}" if ratio_num and ratio_den else ""
        if scan and (is_mp4_container or not frame_annotation):
            frame_annotation = (frame_annotation + " " if frame_annotation else "") + f"interlacement:{scan}"
        frame_rate_line = ""
        if f.get("FrameRate"):
            if frame_annotation:
                frame_rate_line = f'\t\t<essenceTrackFrameRate annotation="{frame_annotation}">{xml_escape(f.get("FrameRate", ""))}</essenceTrackFrameRate>'
            else:
                frame_rate_line = f'\t\t<essenceTrackFrameRate>{xml_escape(f.get("FrameRate", ""))}</essenceTrackFrameRate>'
        lines.extend([
            "\t<instantiationEssenceTrack>",
            "\t\t<essenceTrackType>Video</essenceTrackType>",
            f'\t\t<essenceTrackIdentifier source="ID"{id_ann}>{xml_escape(f.get("ID", ""))}</essenceTrackIdentifier>',
            f'\t\t<essenceTrackIdentifier source="UniqueID">{xml_escape(f.get("UniqueID", ""))}</essenceTrackIdentifier>' if f.get("UniqueID") else "",
            '\t\t<essenceTrackIdentifier source="StreamKindID (MediaInfo)">0</essenceTrackIdentifier>',
            f'\t\t<essenceTrackIdentifier source="StreamOrder (MediaInfo)">{xml_escape(f.get("StreamOrder", ""))}</essenceTrackIdentifier>',
            f'\t\t<essenceTrackEncoding source="codecid" ref="{xml_escape(f.get("CodecID", ""))}" annotation="profile:{xml_escape(video_profile_label(f))}">{xml_escape(f.get("Format", ""))}</essenceTrackEncoding>',
            f'\t\t<essenceTrackDataRate unitsOfMeasure="bit/second">{xml_escape(f.get("BitRate", ""))}</essenceTrackDataRate>' if f.get("BitRate") else "",
            frame_rate_line,
            f'\t\t<essenceTrackBitDepth>{xml_escape(f.get("BitDepth", ""))}</essenceTrackBitDepth>',
            f'\t\t<essenceTrackFrameSize>{xml_escape(f.get("Width", ""))}x{xml_escape(f.get("Height", ""))}</essenceTrackFrameSize>',
            f'\t\t<essenceTrackAspectRatio>{xml_escape(f.get("DisplayAspectRatio", ""))}</essenceTrackAspectRatio>',
            (
                '\t\t<essenceTrackTimeStart source="Container">00:00:00:00</essenceTrackTimeStart>'
                if (not is_mp4_container and string(f.get("FrameRate", "")))
                else ('\t\t<essenceTrackTimeStart source="Container">00:00:00.000</essenceTrackTimeStart>' if not is_mp4_container else "")
            ),
            f'\t\t<essenceTrackDuration>{xml_escape(pbcore_duration(f.get("Duration", ""), f.get("FrameRate", "")))}</essenceTrackDuration>',
        ])
        lines[:] = [line for line in lines if line != ""]
        for key in (
            "HDR_Format",
            "HDR_Format_Commercial",
            "HDR_Format_Compatibility",
            "Format_Settings_CABAC",
            "Format_Settings_RefFrames",
            "Stored_Height",
            "Rotation",
            "FrameRate_Mode_Original",
            "Width_Original",
            "Height_Original",
            "FrameCount",
            "ColorSpace",
            "ChromaSubsampling",
            "Title",
            "StreamSize",
            "Encoded_Library_Name",
            "Encoded_Library_Version",
            "Encoded_Library_Settings",
        ):
            if f.get(key):
                lines.append(f'\t\t<essenceTrackAnnotation annotationType="{key}">{xml_escape(f.get(key, ""))}</essenceTrackAnnotation>')
        for key in (
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
        ):
            if f.get(key):
                lines.append(f'\t\t<essenceTrackAnnotation annotationType="{key}">{xml_escape(f.get(key, ""))}</essenceTrackAnnotation>')
        for k, v in f.items():
            if not k.startswith("extra.") or not v:
                continue
            lines.append(f'\t\t<essenceTrackAnnotation annotationType="{xml_escape(k.split(".", 1)[1])}">{xml_escape(v)}</essenceTrackAnnotation>')
        lines.append("\t</instantiationEssenceTrack>")

    def append_audio(track: Any) -> None:
        f = engine._public_fields(track)
        audio_track_index = (audios.index(track) + 1) if track in audios else 1
        source_stream_prop = string(track.fields.get("Source_StreamSize_Proportion"))
        if not source_stream_prop:
            source_stream_size = int_or_none(f.get("Source_StreamSize"))
            general_size = int_or_none(general.fields.get("FileSize")) if general else None
            if source_stream_size and general_size:
                source_stream_prop = f"{(source_stream_size / general_size):.5f}"
        if source_stream_prop:
            f["Source_StreamSize_Proportion"] = source_stream_prop

        if string(track.fields.get("extra.dialnorm_Maximum")) == "" and string(track.fields.get("extra.dialnorm_Minimum")):
            f["extra.dialnorm_Maximum"] = string(track.fields.get("extra.dialnorm_Minimum"))
        if string(track.fields.get("extra.dialnorm")) and string(track.fields.get("extra.dialnorm_Count")) == "":
            duration_ms = seconds_to_ms(f.get("Duration"))
            sampling_rate = int_or_none(f.get("SamplingRate"))
            samples_per_frame = int_or_none(f.get("SamplesPerFrame"))
            if duration_ms and sampling_rate and samples_per_frame:
                count = int(((duration_ms / 1000.0) * sampling_rate + samples_per_frame - 1) // samples_per_frame)
                if count > 0:
                    f["extra.dialnorm_Count"] = str(count)

        default_flag = string(f.get("Default", "No"))
        forced_flag = string(f.get("Forced", "No"))
        if is_mp4_container:
            bits: list[str] = []
            if default_flag != "No":
                bits.append(f"default:{xml_escape(default_flag)}")
            if forced_flag != "No":
                bits.append(f"forced:{xml_escape(forced_flag)}")
            id_ann = f' annotation="{" ".join(bits)}"' if bits else ""
        else:
            id_ann = f' annotation="default:{xml_escape(default_flag)} forced:{xml_escape(forced_flag)}"'
        comp_ann = f'compression_mode:{xml_escape(f.get("Compression_Mode", ""))}'
        if f.get("Format_Settings_Endianness"):
            comp_ann = f'endianness:{xml_escape(f.get("Format_Settings_Endianness", ""))} {comp_ann}'
        lines.extend([
            "\t<instantiationEssenceTrack>",
            "\t\t<essenceTrackType>Audio</essenceTrackType>",
            f'\t\t<essenceTrackIdentifier source="ID"{id_ann}>{xml_escape(f.get("ID", ""))}</essenceTrackIdentifier>',
            f'\t\t<essenceTrackIdentifier source="UniqueID">{xml_escape(f.get("UniqueID", ""))}</essenceTrackIdentifier>' if f.get("UniqueID") else "",
            f'\t\t<essenceTrackIdentifier source="StreamKindID (MediaInfo)">{audio_track_index - 1}</essenceTrackIdentifier>',
            f'\t\t<essenceTrackIdentifier source="StreamOrder (MediaInfo)">{xml_escape(f.get("StreamOrder", ""))}</essenceTrackIdentifier>',
            f'\t\t<essenceTrackEncoding source="codecid" ref="{xml_escape(f.get("CodecID", ""))}" annotation="{comp_ann}">{xml_escape(codec_label(string(f.get("Format", "")).split(" ", 1)[0]))}</essenceTrackEncoding>',
            f'\t\t<essenceTrackDataRate unitsOfMeasure="bit/second" annotation="{xml_escape(f.get("BitRate_Mode", ""))}">{xml_escape(f.get("BitRate", ""))}</essenceTrackDataRate>' if f.get("BitRate") else "",
            f'\t\t<essenceTrackSamplingRate unitsOfMeasure="Hz">{xml_escape(f.get("SamplingRate", ""))}</essenceTrackSamplingRate>',
            f'\t\t<essenceTrackBitDepth>{xml_escape(f.get("BitDepth", ""))}</essenceTrackBitDepth>' if f.get("BitDepth") else "",
            '\t\t<essenceTrackTimeStart source="Container">00:00:00.000</essenceTrackTimeStart>' if not is_mp4_container else "",
            f'\t\t<essenceTrackDuration>{xml_escape(pbcore_duration_ms(f.get("Duration", "")))}</essenceTrackDuration>',
            f'\t\t<essenceTrackLanguage>{xml_escape(iso639_2_bibliographic(f.get("Language", "")))}</essenceTrackLanguage>' if f.get("Language") else "",
        ])
        lines[:] = [line for line in lines if line != ""]
        for key in ("Format_Commercial_IfAny", "Format_Settings_SBR", "Format_AdditionalFeatures", "Source_Duration", "Source_Duration_LastFrame", "SamplesPerFrame", "FrameCount", "Source_FrameCount", "StreamSize", "Source_StreamSize", "Source_StreamSize_Proportion", "Title", "AlternateGroup", "ServiceKind"):
            if f.get(key):
                lines.append(f'\t\t<essenceTrackAnnotation annotationType="{key}">{xml_escape(seconds_ms_to_int_if_needed(key, f.get(key, "")))}</essenceTrackAnnotation>')
        for k, v in f.items():
            if not k.startswith("extra.") or not v:
                continue
            lines.append(f'\t\t<essenceTrackAnnotation annotationType="{xml_escape(k.split(".", 1)[1])}">{xml_escape(v)}</essenceTrackAnnotation>')
        lines.append("\t</instantiationEssenceTrack>")

    for track in videos:
        append_video(track)
    for track in audios:
        append_audio(track)
    for track in texts:
        f = engine._public_fields(track)
        default_flag = string(f.get("Default", "No"))
        forced_flag = string(f.get("Forced", "No"))
        if is_mp4_container:
            bits: list[str] = []
            if default_flag != "No":
                bits.append(f"default:{xml_escape(default_flag)}")
            if forced_flag != "No":
                bits.append(f"forced:{xml_escape(forced_flag)}")
            id_ann = f' annotation="{" ".join(bits)}"' if bits else ""
        else:
            id_ann = f' annotation="default:{xml_escape(default_flag)} forced:{xml_escape(forced_flag)}"'
        lines.extend([
            "\t<instantiationEssenceTrack>",
            "\t\t<essenceTrackType>Text</essenceTrackType>",
            f'\t\t<essenceTrackIdentifier source="ID"{id_ann}>{xml_escape(f.get("ID", ""))}</essenceTrackIdentifier>',
            f'\t\t<essenceTrackIdentifier source="UniqueID">{xml_escape(f.get("UniqueID", ""))}</essenceTrackIdentifier>' if f.get("UniqueID") else "",
            '\t\t<essenceTrackIdentifier source="StreamKindID (MediaInfo)">0</essenceTrackIdentifier>',
            f'\t\t<essenceTrackIdentifier source="StreamOrder (MediaInfo)">{xml_escape(f.get("StreamOrder", ""))}</essenceTrackIdentifier>' if f.get("StreamOrder") else "",
            f'\t\t<essenceTrackEncoding source="codecid" ref="{xml_escape(f.get("CodecID", ""))}">{xml_escape(f.get("Format", ""))}</essenceTrackEncoding>',
            f'\t\t<essenceTrackDuration>{xml_escape(pbcore_duration_ms(f.get("Duration", "")))}</essenceTrackDuration>' if f.get("Duration") else "",
            f'\t\t<essenceTrackLanguage>{xml_escape(iso639_2_bibliographic(f.get("Language", "")))}</essenceTrackLanguage>' if f.get("Language") else "",
            f'\t\t<essenceTrackAnnotation annotationType="Title">{xml_escape(f.get("Title", ""))}</essenceTrackAnnotation>' if f.get("Title") else "",
            "\t</instantiationEssenceTrack>",
        ])
        lines[:] = [line for line in lines if line != ""]

    if general:
        if audios:
            total_channels = 0
            for audio_track in audios:
                af = engine._public_fields(audio_track)
                total_channels += int_or_none(af.get("Channels")) or 0
            lines.append(f'\t<instantiationAnnotation annotationType="Audio_Channels_Total">{total_channels}</instantiationAnnotation>')
        if general.fields.get("FrameCount"):
            lines.append(f'\t<instantiationAnnotation annotationType="FrameCount">{xml_escape(general.fields.get("FrameCount", ""))}</instantiationAnnotation>')
        if general.fields.get("Title"):
            lines.append(f'\t<instantiationAnnotation annotationType="Title">{xml_escape(general.fields.get("Title", ""))}</instantiationAnnotation>')
        if general.fields.get("Movie"):
            lines.append(f'\t<instantiationAnnotation annotationType="Movie">{xml_escape(general.fields.get("Movie", ""))}</instantiationAnnotation>')
        if general.fields.get("File_Created_Date"):
            lines.append(f'\t<instantiationAnnotation annotationType="File_Created_Date">{xml_escape(general.fields.get("File_Created_Date", ""))}</instantiationAnnotation>')
        if general.fields.get("File_Created_Date_Local"):
            lines.append(f'\t<instantiationAnnotation annotationType="File_Created_Date_Local">{xml_escape(general.fields.get("File_Created_Date_Local", ""))}</instantiationAnnotation>')
        if general.fields.get("Encoded_Application"):
            lines.append(f'\t<instantiationAnnotation annotationType="Encoded_Application">{xml_escape(general.fields.get("Encoded_Application", ""))}</instantiationAnnotation>')
        if general.fields.get("Encoded_Library"):
            lines.append(f'\t<instantiationAnnotation annotationType="Encoded_Library">{xml_escape(general.fields.get("Encoded_Library", ""))}</instantiationAnnotation>')
        if general.fields.get("Comment"):
            lines.append(f'\t<instantiationAnnotation annotationType="Comment">{xml_escape(general.fields.get("Comment", ""))}</instantiationAnnotation>')
        if general.fields.get("extra.ErrorDetectionType"):
            lines.append(f'\t<instantiationAnnotation annotationType="ErrorDetectionType">{xml_escape(general.fields.get("extra.ErrorDetectionType", ""))}</instantiationAnnotation>')
        for gk, gv in general.fields.items():
            if not gk.startswith("extra.") or gk == "extra.ErrorDetectionType" or not gv:
                continue
            lines.append(f'\t<instantiationAnnotation annotationType="{xml_escape(gk.split(".", 1)[1])}">{xml_escape(gv)}</instantiationAnnotation>')

    lines.extend(["</pbcoreInstantiationDocument>", ""])
    return "\n".join(lines) + "\n"
