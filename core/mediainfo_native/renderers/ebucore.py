"""EBUCore renderer (report-driven)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


class _EngineLike(Protocol):
    def _public_fields(self, track: Any) -> dict[str, str]: ...
    def _duration_iso8601_from_ms(self, duration_ms: int | None) -> str: ...


class _ReportLike(Protocol):
    source: str

    def first_track(self, kind: str) -> Any: ...
    def tracks_by_kind(self, kind: str) -> list[Any]: ...


def render_ebucore(
    engine: _EngineLike,
    report: _ReportLike,
    mode: str,
    *,
    timestamp: datetime | None = None,
    version_text: str = "MediaInfoLib - v26.01",
    xml_escape: Callable[[str], str] | None = None,
    display_ratio_parts: Callable[[str], tuple[str, str]] | None = None,
    string: Callable[[Any], str] | None = None,
    trim_float: Callable[[str], str] | None = None,
    int_or_none: Callable[[Any], int | None] | None = None,
    video_profile_label: Callable[[dict[str, str]], str] | None = None,
    scan_type_ebucore: Callable[[str], str] | None = None,
    bitrate_mode_ebucore: Callable[[str], str] | None = None,
    format_text_field_value: Callable[[str, str, Any, int | None], str] | None = None,
    should_display_text_field: Callable[[Any, str], bool] | None = None,
    seconds_to_ms: Callable[[str | int | float | None], int | None] | None = None,
    codec_label: Callable[[str | None], str] | None = None,
    ebucore_xml_to_json: Callable[[str], str] | None = None,
) -> str:
    required = [
        xml_escape,
        display_ratio_parts,
        string,
        trim_float,
        int_or_none,
        video_profile_label,
        scan_type_ebucore,
        bitrate_mode_ebucore,
        format_text_field_value,
        should_display_text_field,
        seconds_to_ms,
        codec_label,
        ebucore_xml_to_json,
    ]
    if any(dep is None for dep in required):
        raise ValueError("Renderer dependencies missing for EBUCore renderer")

    is_json = mode.endswith("_JSON")
    ts = timestamp or datetime.now(timezone.utc)
    date_stamp = ts.strftime("%Y-%m-%d")
    time_stamp = ts.strftime("%H:%M:%S")

    general = report.first_track("General")
    videos = report.tracks_by_kind("Video")
    audios = report.tracks_by_kind("Audio")
    texts = report.tracks_by_kind("Text")

    if is_json:
        xml_mode = mode[:-5] if mode.endswith("_JSON") else "EBUCORE_1.8_PS"
        xml_payload = render_ebucore(
            engine,
            report,
            xml_mode,
            timestamp=ts,
            version_text=version_text,
            xml_escape=xml_escape,
            display_ratio_parts=display_ratio_parts,
            string=string,
            trim_float=trim_float,
            int_or_none=int_or_none,
            video_profile_label=video_profile_label,
            scan_type_ebucore=scan_type_ebucore,
            bitrate_mode_ebucore=bitrate_mode_ebucore,
            format_text_field_value=format_text_field_value,
            should_display_text_field=should_display_text_field,
            seconds_to_ms=seconds_to_ms,
            codec_label=codec_label,
            ebucore_xml_to_json=ebucore_xml_to_json,
        )
        return ebucore_xml_to_json(xml_payload)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<!-- Generated at {ts.strftime('%Y-%m-%dT%H:%M:%SZ')} by {version_text} -->",
        '<ebucore:ebuCoreMain xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:ebucore="urn:ebu:metadata-schema:ebucore" xmlns:xalan="http://xml.apache.org/xalan" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="urn:ebu:metadata-schema:ebucore https://www.ebu.ch/metadata/schemas/EBUCore/20171009/ebucore.xsd" version="1.8" writingLibraryName="MediaInfoLib" writingLibraryVersion="26.01" dateLastModified="{}" timeLastModified="{}">'.format(date_stamp, time_stamp),
        "\t<ebucore:coreMetadata>",
        "\t\t<ebucore:format>",
    ]
    tail_lines: list[str] = []

    for index, track in enumerate(videos, start=1):
        f = engine._public_fields(track)
        dar_num, dar_den = display_ratio_parts(f.get("DisplayAspectRatio", ""))
        width_value = f.get("Width", "")
        height_value = f.get("Height", "")
        if string(f.get("Format")) == "VP9":
            width_value = f.get("Width_Original", "") or width_value
            height_value = f.get("Height_Original", "") or height_value

        lines.extend([
            f'\t\t\t<ebucore:videoFormat videoFormatName="{xml_escape(f.get("Format", ""))}">',
            f'\t\t\t\t<ebucore:width unit="pixel">{xml_escape(width_value)}</ebucore:width>',
            f'\t\t\t\t<ebucore:height unit="pixel">{xml_escape(height_value)}</ebucore:height>',
        ])
        if string(f.get("Format")) == "VP9" and f.get("Height_Original"):
            lines.append(f'\t\t\t\t<ebucore:lines>{xml_escape(f.get("Height_Original", ""))}</ebucore:lines>')
        frame_rate_val = string(f.get("FrameRate", "")).strip()
        if frame_rate_val:
            frame_rate_line = f'\t\t\t\t<ebucore:frameRate>{xml_escape(trim_float(frame_rate_val))}</ebucore:frameRate>'
            fr_num = int_or_none(f.get("FrameRate_Num"))
            fr_den = int_or_none(f.get("FrameRate_Den"))
            if fr_num and fr_den and fr_den > 1:
                fr_rounded = int(round(fr_num / fr_den))
                if fr_rounded > 0 and fr_num % fr_rounded == 0:
                    fac_num = fr_num // fr_rounded
                else:
                    fac_num = fr_num
                frame_rate_line = (
                    f'\t\t\t\t<ebucore:frameRate factorNumerator="{fac_num}" '
                    f'factorDenominator="{fr_den}">{fr_rounded}</ebucore:frameRate>'
                )
            lines.append(frame_rate_line)
        if dar_num and dar_den:
            lines.extend([
                '\t\t\t\t<ebucore:aspectRatio typeLabel="display">',
                f"\t\t\t\t\t<ebucore:factorNumerator>{dar_num}</ebucore:factorNumerator>",
                f"\t\t\t\t\t<ebucore:factorDenominator>{dar_den}</ebucore:factorDenominator>",
                "\t\t\t\t</ebucore:aspectRatio>",
            ])
        lines.extend([
            f'\t\t\t\t<ebucore:videoEncoding typeLabel="{xml_escape(video_profile_label(f))}" />',
            "\t\t\t\t<ebucore:codec>",
            "\t\t\t\t\t<ebucore:codecIdentifier>",
            f'\t\t\t\t\t\t<dc:identifier>{xml_escape(f.get("CodecID", ""))}</dc:identifier>',
            "\t\t\t\t\t</ebucore:codecIdentifier>",
            "\t\t\t\t</ebucore:codec>",
        ])
        if f.get("BitRate"):
            lines.append(f'\t\t\t\t<ebucore:bitRate>{xml_escape(f.get("BitRate", ""))}</ebucore:bitRate>')
        scan_val = scan_type_ebucore(f.get("ScanType", ""))
        if scan_val and scan_val != "unknown":
            lines.append(f'\t\t\t\t<ebucore:scanningFormat>{xml_escape(scan_val)}</ebucore:scanningFormat>')
        video_track_attrs = [f'trackId="{xml_escape(f.get("ID") or str(index))}"']
        if f.get("Title"):
            video_track_attrs.append(f'trackName="{xml_escape(f.get("Title", ""))}"')
        lines.append(f'\t\t\t\t<ebucore:videoTrack {" ".join(video_track_attrs)} />')
        if f.get("ColorSpace"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="ColorSpace">{xml_escape(f.get("ColorSpace", ""))}</ebucore:technicalAttributeString>')
        if f.get("ChromaSubsampling"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="ChromaSubsampling">{xml_escape(f.get("ChromaSubsampling", ""))}</ebucore:technicalAttributeString>')
        if f.get("colour_primaries"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="colour_primaries">{xml_escape(f.get("colour_primaries", ""))}</ebucore:technicalAttributeString>')
        if f.get("transfer_characteristics"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="transfer_characteristics">{xml_escape(f.get("transfer_characteristics", ""))}</ebucore:technicalAttributeString>')
        if f.get("matrix_coefficients"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="matrix_coefficients">{xml_escape(f.get("matrix_coefficients", ""))}</ebucore:technicalAttributeString>')
        if f.get("colour_range"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="colour_range">{xml_escape(f.get("colour_range", ""))}</ebucore:technicalAttributeString>')
        if f.get("Encoded_Library"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="WritingLibrary">{xml_escape(format_text_field_value("Encoded_Library", f.get("Encoded_Library", ""), track, None))}</ebucore:technicalAttributeString>')
        if f.get("Default") and should_display_text_field(track, "Default"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="Default">{xml_escape(f.get("Default", ""))}</ebucore:technicalAttributeString>')
        if f.get("Forced") and should_display_text_field(track, "Forced"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="Forced">{xml_escape(f.get("Forced", ""))}</ebucore:technicalAttributeString>')
        if f.get("BitDepth"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeInteger typeLabel="BitDepth" unit="bit">{xml_escape(f.get("BitDepth", ""))}</ebucore:technicalAttributeInteger>')
        if f.get("StreamSize"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeInteger typeLabel="StreamSize" unit="byte">{xml_escape(f.get("StreamSize", ""))}</ebucore:technicalAttributeInteger>')
        if f.get("Format_Settings_CABAC"):
            val = "true" if f.get("Format_Settings_CABAC") == "Yes" else "false"
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeBoolean typeLabel="CABAC">{val}</ebucore:technicalAttributeBoolean>')
            lines.append('\t\t\t\t<ebucore:technicalAttributeBoolean typeLabel="MBAFF">false</ebucore:technicalAttributeBoolean>')
        lines.extend(["\t\t\t</ebucore:videoFormat>"])

    for index, track in enumerate(audios, start=1):
        f = engine._public_fields(track)
        audio_encoding = ""
        if f.get("Format") == "E-AC-3":
            audio_encoding = ' typeLabel="E-AC3" typeLink="http://www.ebu.ch/metadata/cs/ebu_AudioCompressionCodeCS.xml#4.3"'
        lines.extend([
            f'\t\t\t<ebucore:audioFormat audioFormatName="{xml_escape(codec_label(string(f.get("Format", "")).split(" ", 1)[0]))}">',
            f"\t\t\t\t<ebucore:audioEncoding{audio_encoding} />",
            "\t\t\t\t<ebucore:codec>",
            "\t\t\t\t\t<ebucore:codecIdentifier>",
            f'\t\t\t\t\t\t<dc:identifier>{xml_escape(f.get("CodecID", ""))}</dc:identifier>',
            "\t\t\t\t\t</ebucore:codecIdentifier>",
            f'\t\t\t\t\t<ebucore:name>{xml_escape(f.get("Format_Commercial_IfAny", ""))}</ebucore:name>' if f.get("Format_Commercial_IfAny") else "",
            "\t\t\t\t</ebucore:codec>",
            f'\t\t\t\t<ebucore:samplingRate>{xml_escape(f.get("SamplingRate", ""))}</ebucore:samplingRate>',
            f'\t\t\t\t<ebucore:sampleSize>{xml_escape(f.get("BitDepth", ""))}</ebucore:sampleSize>' if f.get("BitDepth") else "",
            f'\t\t\t\t<ebucore:bitRate>{xml_escape(f.get("BitRate", ""))}</ebucore:bitRate>' if f.get("BitRate") else "",
            f'\t\t\t\t<ebucore:bitRateMode>{xml_escape(bitrate_mode_ebucore(f.get("BitRate_Mode", "")))}</ebucore:bitRateMode>' if f.get("BitRate_Mode") else "",
            (
                '\t\t\t\t<ebucore:audioTrack '
                + " ".join(
                    [
                        f'trackId="{xml_escape(f.get("ID") or str(index))}"',
                        *([f'trackName="{xml_escape(f.get("Title", ""))}"'] if f.get("Title") else []),
                        *([f'trackLanguage="{xml_escape(f.get("Language", ""))}"'] if f.get("Language") else []),
                    ]
                )
                + " />"
            ),
            f'\t\t\t\t<ebucore:channels>{xml_escape(f.get("Channels", ""))}</ebucore:channels>',
        ])
        lines[:] = [line for line in lines if line != ""]
        if f.get("ChannelPositions"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="ChannelPositions">{xml_escape(f.get("ChannelPositions", ""))}</ebucore:technicalAttributeString>')
        if f.get("ChannelLayout"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="ChannelLayout">{xml_escape(f.get("ChannelLayout", ""))}</ebucore:technicalAttributeString>')
        if f.get("Format_Settings_Endianness"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="Endianness">{xml_escape(f.get("Format_Settings_Endianness", ""))}</ebucore:technicalAttributeString>')
        if f.get("StreamSize"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeInteger typeLabel="StreamSize" unit="byte">{xml_escape(f.get("StreamSize", ""))}</ebucore:technicalAttributeInteger>')
        lines.extend(["\t\t\t</ebucore:audioFormat>"])

    if general:
        container_attrs = [f'containerFormatName="{xml_escape(general.fields.get("Format", ""))}"']
        if general.fields.get("Format_Version"):
            container_attrs.append(f'containerFormatVersionId="{xml_escape(general.fields.get("Format_Version", ""))}"')
        if general.fields.get("UniqueID"):
            container_attrs.append(f'containerFormatId="{xml_escape(general.fields.get("UniqueID", ""))}"')
        lines.extend([
            f'\t\t\t<ebucore:containerFormat {" ".join(container_attrs)}>',
            f'\t\t\t\t<ebucore:containerEncoding formatLabel="{xml_escape(general.fields.get("Format", ""))}" />',
        ])
        if general.fields.get("CodecID"):
            lines.extend([
                "\t\t\t\t<ebucore:codec>",
                "\t\t\t\t\t<ebucore:codecIdentifier>",
                f'\t\t\t\t\t\t<dc:identifier>{xml_escape(general.fields.get("CodecID", ""))}</dc:identifier>',
                "\t\t\t\t\t</ebucore:codecIdentifier>",
                "\t\t\t\t</ebucore:codec>",
            ])
        if general.fields.get("Format_Profile"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="FormatProfile">{xml_escape(general.fields.get("Format_Profile", ""))}</ebucore:technicalAttributeString>')
        if general.fields.get("Encoded_Application"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="WritingApplication">{xml_escape(general.fields.get("Encoded_Application", ""))}</ebucore:technicalAttributeString>')
        if general.fields.get("Encoded_Library"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="WritingLibrary">{xml_escape(general.fields.get("Encoded_Library", ""))}</ebucore:technicalAttributeString>')
        if general.fields.get("extra.ErrorDetectionType"):
            lines.append(f'\t\t\t\t<ebucore:technicalAttributeString typeLabel="ErrorDetectionType">{xml_escape(general.fields.get("extra.ErrorDetectionType", ""))}</ebucore:technicalAttributeString>')
        lines.append("\t\t\t</ebucore:containerFormat>")
        duration_iso = engine._duration_iso8601_from_ms(seconds_to_ms(general.fields.get("Duration")))
        if duration_iso:
            tail_lines.extend([
                "\t\t\t<ebucore:duration>",
                f"\t\t\t\t<ebucore:normalPlayTime>{xml_escape(duration_iso)}</ebucore:normalPlayTime>",
                "\t\t\t</ebucore:duration>",
            ])
        if general.fields.get("FileSize"):
            tail_lines.append(f'\t\t\t<ebucore:fileSize>{xml_escape(general.fields.get("FileSize", ""))}</ebucore:fileSize>')
        tail_lines.append(f'\t\t\t<ebucore:fileName>{xml_escape(Path(report.source).name)}</ebucore:fileName>')
        tail_lines.append(f'\t\t\t<ebucore:locator>{xml_escape(report.source)}</ebucore:locator>')
        if general.fields.get("OverallBitRate"):
            tail_lines.append(f'\t\t\t<ebucore:technicalAttributeInteger typeLabel="OverallBitRate" unit="bps">{xml_escape(general.fields.get("OverallBitRate", ""))}</ebucore:technicalAttributeInteger>')

    if texts:
        for index, track in enumerate(texts, start=1):
            f = engine._public_fields(track)
            lines.extend([
                f'\t\t\t<ebucore:dataFormat dataFormatName="{xml_escape(f.get("Format", ""))}" dataTrackId="{xml_escape(f.get("ID") or str(index))}">',
                (
                    '\t\t\t\t<ebucore:captioningFormat '
                    + " ".join(
                        [
                            f'captioningFormatName="{xml_escape(f.get("Format", ""))}"',
                            f'trackId="{xml_escape(f.get("ID") or str(index))}"',
                            *([f'typeLabel="{xml_escape(f.get("Title", ""))}"'] if f.get("Title") else []),
                            *([f'language="{xml_escape(f.get("Language", ""))}"'] if f.get("Language") else []),
                        ]
                    )
                    + " />"
                ),
                "\t\t\t\t<ebucore:codec>",
                "\t\t\t\t\t<ebucore:codecIdentifier>",
                f'\t\t\t\t\t\t<dc:identifier>{xml_escape(f.get("CodecID", ""))}</dc:identifier>',
                "\t\t\t\t\t</ebucore:codecIdentifier>",
                "\t\t\t\t</ebucore:codec>",
                "\t\t\t</ebucore:dataFormat>",
            ])
    lines.extend(tail_lines)

    lines.extend([
        "\t\t</ebucore:format>",
        "\t</ebucore:coreMetadata>",
        "</ebucore:ebuCoreMain>",
        "",
    ])
    return "\n".join(lines) + "\n"
