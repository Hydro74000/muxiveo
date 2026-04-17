"""MPEG-7 renderer (report-driven)."""

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


def render_mpeg7(
    engine: _EngineLike,
    report: _ReportLike,
    profile: str,
    *,
    timestamp: datetime | None = None,
    version_text: str = "MediaInfoLib - v26.01",
    xml_escape: Callable[[str], str] | None = None,
    string: Callable[[Any], str] | None = None,
    codec_label: Callable[[str | None], str] | None = None,
    mpeg7_avc_level_term_id: Callable[[str], str] | None = None,
    mpeg7_audio_presentation_name: Callable[[str], str] | None = None,
    mpeg7_duration: Callable[[str, str], str] | None = None,
) -> str:
    required = [
        xml_escape,
        string,
        codec_label,
        mpeg7_avc_level_term_id,
        mpeg7_audio_presentation_name,
        mpeg7_duration,
    ]
    if any(dep is None for dep in required):
        raise ValueError("Renderer dependencies missing for MPEG-7 renderer")

    general = report.first_track("General")
    videos = report.tracks_by_kind("Video")
    audios = report.tracks_by_kind("Audio")
    texts = report.tracks_by_kind("Text")
    ts = timestamp or datetime.now(timezone.utc)
    is_mp4_container = bool(general and string(general.fields.get("Format")) == "MPEG-4")
    is_video_only = bool(videos) and not audios and not texts
    is_extended = profile == "MPEG-7_EXTENDED"
    use_extended_ns = is_extended or (profile == "MPEG-7_RELAXED" and not is_mp4_container and bool(texts))
    use_locator_style = is_extended or (profile == "MPEG-7_RELAXED" and bool(texts))
    default_ns = "urn:mpeg:mpeg7-extended:schema:2023" if use_extended_ns else "urn:mpeg:mpeg7:schema:2004"
    schema_location = (
        "urn:mpeg:mpeg7-extended:schema:2023 https://mediaarea.net/xsd/mpeg7-v2-extended.xsd"
        if use_extended_ns
        else "urn:mpeg:mpeg7:schema:2004 http://standards.iso.org/ittf/PubliclyAvailableStandards/MPEG-7_schema_files/mpeg7-v2.xsd"
    )

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<!-- Generated at {ts.strftime('%Y-%m-%dT%H:%M:%SZ')} by {version_text} -->",
        f'<mpeg7:Mpeg7 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns="{default_ns}" xmlns:mpeg7="{default_ns}" xsi:schemaLocation="{schema_location}">',
        "\t<mpeg7:DescriptionMetadata>",
        f"\t\t<mpeg7:PrivateIdentifier>{xml_escape(Path(report.source).name)}</mpeg7:PrivateIdentifier>",
        f"\t\t<mpeg7:CreationTime>{ts.strftime('%Y-%m-%dT%H:%M:%S+00:00')}</mpeg7:CreationTime>",
        "\t\t<mpeg7:Instrument>",
        "\t\t\t<mpeg7:Tool>",
        f"\t\t\t\t<mpeg7:Name>{xml_escape(version_text)}</mpeg7:Name>",
        "\t\t\t</mpeg7:Tool>",
        "\t\t</mpeg7:Instrument>",
        "\t</mpeg7:DescriptionMetadata>",
        '\t<mpeg7:Description xsi:type="ContentEntityType">',
        f'\t\t<mpeg7:MultimediaContent xsi:type="{"VideoType" if is_video_only else "AudioVisualType"}">',
        "\t\t\t<mpeg7:Video>" if is_video_only else "\t\t\t<mpeg7:AudioVisual>",
        "\t\t\t\t<mpeg7:MediaInformation>",
        "\t\t\t\t\t<mpeg7:MediaProfile>",
        "\t\t\t\t\t\t<mpeg7:MediaFormat>",
        f'\t\t\t\t\t\t\t<mpeg7:Content href="{"urn:mpeg:mpeg7:cs:ContentCS:2001:4.2" if is_video_only else "urn:mpeg:mpeg7:cs:ContentCS:2001:2"}">',
        f'\t\t\t\t\t\t\t\t<mpeg7:Name xml:lang="en">{"Video" if is_video_only else "Audiovisual"}</mpeg7:Name>',
        "\t\t\t\t\t\t\t</mpeg7:Content>",
    ]

    ext = string((general.fields.get("FileExtension") if general else Path(report.source).suffix.lstrip("."))).lower()
    fmt_name = string(general.fields.get("Format") if general else "")
    file_fmt_href = "urn:mpeg:mpeg7:cs:FileFormatCS:2001:5" if is_mp4_container else "urn:x-mpeg7-mediainfo:cs:FileFormatCS:2009:unknown"
    file_fmt_name = ext if is_mp4_container else fmt_name
    lines.extend([
        f'\t\t\t\t\t\t\t<mpeg7:FileFormat href="{file_fmt_href}">',
        f'\t\t\t\t\t\t\t\t<mpeg7:Name xml:lang="en">{xml_escape(file_fmt_name)}</mpeg7:Name>',
    ])
    if is_mp4_container:
        compat = string(general.fields.get("CodecID_Compatible") if general else "")
        if compat:
            compat_head = xml_escape(compat.split("/", 1)[0])
            lines.extend([
                '\t\t\t\t\t\t\t\t<mpeg7:Term termID="5.1">',
                f'\t\t\t\t\t\t\t\t\t<mpeg7:Name xml:lang="en">{xml_escape(ext)} {compat_head}</mpeg7:Name>',
                "\t\t\t\t\t\t\t\t</mpeg7:Term>",
            ])
    lines.append("\t\t\t\t\t\t\t</mpeg7:FileFormat>")

    if general:
        if general.fields.get("FileSize"):
            lines.append(f'\t\t\t\t\t\t\t<mpeg7:FileSize>{xml_escape(general.fields.get("FileSize", ""))}</mpeg7:FileSize>')
        if general.fields.get("OverallBitRate"):
            lines.append(f'\t\t\t\t\t\t\t<mpeg7:BitRate>{xml_escape(general.fields.get("OverallBitRate", ""))}</mpeg7:BitRate>')

    for idx, video in enumerate(videos, start=1):
        f = engine._public_fields(video)
        stream_id = xml_escape(f.get("ID", str(idx)))
        format_name = xml_escape(f.get("Format", ""))
        if string(f.get("Format")) == "AVC":
            visual_href = "urn:x-mpeg7-mediainfo:cs:VisualCodingFormatCS:2009:50"
        elif string(f.get("Format")) == "HEVC":
            visual_href = "urn:x-mpeg7-mediainfo:cs:VisualCodingFormatCS:2009:51"
        else:
            visual_href = "urn:x-mpeg7-mediainfo:cs:VisualCodingFormatCS:2009:unknown"

        vc_open = f'\t\t\t\t\t\t\t<mpeg7:VisualCoding id="visual.{idx}">' if use_locator_style else "\t\t\t\t\t\t\t<mpeg7:VisualCoding>"
        lines.append(vc_open)
        if use_locator_style:
            lines.extend([
                "\t\t\t\t\t\t\t\t<mpeg7:MediaLocator>",
                f"\t\t\t\t\t\t\t\t\t<mpeg7:StreamID>{stream_id}</mpeg7:StreamID>",
                "\t\t\t\t\t\t\t\t</mpeg7:MediaLocator>",
            ])
        else:
            lines.append(f"\t\t\t\t\t\t\t\t<!-- StreamID: {stream_id} -->")
        color_domain_attr = ' colorDomain="color"' if visual_href != "urn:x-mpeg7-mediainfo:cs:VisualCodingFormatCS:2009:unknown" else ""
        lines.extend([
            f'\t\t\t\t\t\t\t\t<mpeg7:Format href="{visual_href}"{color_domain_attr}>',
            f'\t\t\t\t\t\t\t\t\t<mpeg7:Name xml:lang="en">{format_name}</mpeg7:Name>',
        ])
        if string(f.get("Format")) == "AVC":
            profile_name = xml_escape(string(f.get("Format_Profile")).split("@", 1)[0])
            level_value = string(f.get("Format_Level"))
            level_term = mpeg7_avc_level_term_id(level_value)
            lines.extend([
                '\t\t\t\t\t\t\t\t\t<mpeg7:Term termID="50.4">',
                f'\t\t\t\t\t\t\t\t\t\t<mpeg7:Name xml:lang="en">AVC {profile_name}</mpeg7:Name>',
                f'\t\t\t\t\t\t\t\t\t\t<mpeg7:Term termID="50.4.{level_term}">',
                f'\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Name xml:lang="en">AVC {profile_name} @ Level {xml_escape(level_value)}</mpeg7:Name>',
                "\t\t\t\t\t\t\t\t\t\t</mpeg7:Term>",
                "\t\t\t\t\t\t\t\t\t</mpeg7:Term>",
            ])
        lines.append("\t\t\t\t\t\t\t\t</mpeg7:Format>")
        lines.append(
            f'\t\t\t\t\t\t\t\t<mpeg7:Pixel aspectRatio="{xml_escape(f.get("PixelAspectRatio", ""))}" bitsPer="{xml_escape(f.get("BitDepth", ""))}" />'
        )
        frame_attrs_parts = [
            f'aspectRatio="{xml_escape(f.get("DisplayAspectRatio", ""))}"',
            f'height="{xml_escape(f.get("Height", ""))}"',
            f'width="{xml_escape(f.get("Width", ""))}"',
        ]
        frame_rate_value = string(f.get("FrameRate", "")).strip()
        if frame_rate_value:
            frame_attrs_parts.append(f'rate="{xml_escape(frame_rate_value)}"')
        scan_type = string(f.get("ScanType", "")).lower()
        if scan_type:
            frame_attrs_parts.append(f'structure="{xml_escape(scan_type)}"')
        if is_extended and not frame_rate_value:
            frame_attrs_parts.append('variableRate="true"')
        frame_attrs = " ".join(frame_attrs_parts)
        frame_line = f"\t\t\t\t\t\t\t\t<mpeg7:Frame {frame_attrs} />"
        if profile in {"MPEG-7_STRICT", "MPEG-7_RELAXED"} and not frame_rate_value:
            frame_line += " <!-- variableRate: true -->"
        lines.append(frame_line)

        color_space = string(f.get("ColorSpace", "")).upper()
        chroma = string(f.get("ChromaSubsampling", ""))
        if color_space == "YUV" and chroma == "4:2:0":
            if use_extended_ns:
                lines.append("\t\t\t\t\t\t\t\t<mpeg7:ColorSampling>")
                lines.append("\t\t\t\t\t\t\t\t\t<mpeg7:Name>YUV 4:2:0 Interlaced</mpeg7:Name>")
            else:
                lines.append("\t\t\t\t\t\t\t\t<mpeg7:ColorSampling> <!-- YUV 4:2:0 Interlaced -->")
            lines.extend([
                '\t\t\t\t\t\t\t\t\t<mpeg7:Lattice height="720" width="486" />',
                '\t\t\t\t\t\t\t\t\t<mpeg7:Field temporalOrder="0" positionalOrder="0">',
                "\t\t\t\t\t\t\t\t\t\t<mpeg7:Component>",
                "\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Name>Luminance</mpeg7:Name>",
                '\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Offset horizontal="0.0" vertical="0.0" />',
                '\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Period horizontal="1.0" vertical="2.0" />',
                "\t\t\t\t\t\t\t\t\t\t</mpeg7:Component>",
                "\t\t\t\t\t\t\t\t\t\t<mpeg7:Component>",
                "\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Name>ChrominanceBlueDifference</mpeg7:Name>",
                '\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Offset horizontal="0.0" vertical="0.5" />',
                '\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Period horizontal="2.0" vertical="4.0" />',
                "\t\t\t\t\t\t\t\t\t\t</mpeg7:Component>",
                "\t\t\t\t\t\t\t\t\t\t<mpeg7:Component>",
                "\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Name>ChrominanceRedDifference</mpeg7:Name>",
                '\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Offset horizontal="0.0" vertical="0.5" />',
                '\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Period horizontal="2.0" vertical="4.0" />',
                "\t\t\t\t\t\t\t\t\t\t</mpeg7:Component>",
                "\t\t\t\t\t\t\t\t\t</mpeg7:Field>",
                '\t\t\t\t\t\t\t\t\t<mpeg7:Field temporalOrder="1" positionalOrder="1">',
                "\t\t\t\t\t\t\t\t\t\t<mpeg7:Component>",
                "\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Name>Luminance</mpeg7:Name>",
                '\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Offset horizontal="0.0" vertical="1.0" />',
                '\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Period horizontal="1.0" vertical="2.0" />',
                "\t\t\t\t\t\t\t\t\t\t</mpeg7:Component>",
                "\t\t\t\t\t\t\t\t\t\t<mpeg7:Component>",
                "\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Name>ChrominanceBlueDifference</mpeg7:Name>",
                '\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Offset horizontal="0.0" vertical="2.5" />',
                '\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Period horizontal="2.0" vertical="4.0" />',
                "\t\t\t\t\t\t\t\t\t\t</mpeg7:Component>",
                "\t\t\t\t\t\t\t\t\t\t<mpeg7:Component>",
                "\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Name>ChrominanceRedDifference</mpeg7:Name>",
                '\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Offset horizontal="0.0" vertical="2.5" />',
                '\t\t\t\t\t\t\t\t\t\t\t<mpeg7:Period horizontal="4.0" vertical="2.0" />',
                "\t\t\t\t\t\t\t\t\t\t</mpeg7:Component>",
                "\t\t\t\t\t\t\t\t\t</mpeg7:Field>",
                "\t\t\t\t\t\t\t\t</mpeg7:ColorSampling>",
            ])
        video_lang = string(f.get("Language", "")).strip()
        if not video_lang and audios:
            video_lang = string(engine._public_fields(audios[0]).get("Language", "")).strip()
        if not video_lang and texts:
            video_lang = string(engine._public_fields(texts[0]).get("Language", "")).strip()
        if video_lang:
            lines.append(f"\t\t\t\t\t\t\t\t<!-- Language: {xml_escape(video_lang)} -->")
        lines.append("\t\t\t\t\t\t\t</mpeg7:VisualCoding>")

    for idx, audio in enumerate(audios, start=1):
        f = engine._public_fields(audio)
        stream_id = xml_escape(f.get("ID", str(idx)))
        format_name = xml_escape(codec_label(string(f.get("Format", "")).split(" ", 1)[0]))
        audio_href = "urn:x-mpeg7-mediainfo:cs:AudioCodingFormatCS:2009:53" if string(f.get("Format")) == "AAC" else "urn:x-mpeg7-mediainfo:cs:AudioCodingFormatCS:2009:unknown"
        if profile == "MPEG-7_STRICT" and idx > 1:
            lines.append("\t\t\t\t\t\t\t<!-- More than 1 track")
        ac_open = f'\t\t\t\t\t\t\t<mpeg7:AudioCoding id="audio.{idx}">' if use_locator_style else "\t\t\t\t\t\t\t<mpeg7:AudioCoding>"
        lines.append(ac_open)
        if use_locator_style or (profile == "MPEG-7_STRICT" and idx > 1):
            lines.extend([
                "\t\t\t\t\t\t\t\t<mpeg7:MediaLocator>",
                f"\t\t\t\t\t\t\t\t\t<mpeg7:StreamID>{stream_id}</mpeg7:StreamID>",
                "\t\t\t\t\t\t\t\t</mpeg7:MediaLocator>",
            ])
        else:
            lines.append(f"\t\t\t\t\t\t\t\t<!-- StreamID: {stream_id} -->")
        sample_rate_line = f'\t\t\t\t\t\t\t\t<mpeg7:Sample rate="{xml_escape(f.get("SamplingRate", ""))}" />'
        if f.get("BitDepth"):
            sample_rate_line = (
                f'\t\t\t\t\t\t\t\t<mpeg7:Sample rate="{xml_escape(f.get("SamplingRate", ""))}" '
                f'bitsPer="{xml_escape(f.get("BitDepth", ""))}" />'
            )
        audio_lang = string(f.get("Language", "")).strip()
        lines.extend([
            f'\t\t\t\t\t\t\t\t<mpeg7:Format href="{audio_href}">',
            f'\t\t\t\t\t\t\t\t\t<mpeg7:Name xml:lang="en">{format_name}</mpeg7:Name>',
            "\t\t\t\t\t\t\t\t</mpeg7:Format>",
            f'\t\t\t\t\t\t\t\t<mpeg7:AudioChannels>{xml_escape(f.get("Channels", ""))}</mpeg7:AudioChannels>',
            sample_rate_line,
            '\t\t\t\t\t\t\t\t<mpeg7:Presentation href="urn:mpeg:mpeg7:cs:AudioPresentationCS:2001:2">',
            f'\t\t\t\t\t\t\t\t\t<mpeg7:Name xml:lang="en">{xml_escape(mpeg7_audio_presentation_name(f.get("Channels", "")))}</mpeg7:Name>',
            "\t\t\t\t\t\t\t\t</mpeg7:Presentation>",
            (
                f'\t\t\t\t\t\t\t\t<mpeg7:language>{xml_escape(audio_lang)}</mpeg7:language>'
                if profile == "MPEG-7_STRICT" and idx > 1 and audio_lang
                else ""
            ),
            (
                f"\t\t\t\t\t\t\t\t<!-- Language: {xml_escape(audio_lang)} -->"
                if (profile != "MPEG-7_STRICT" and audio_lang) or (profile == "MPEG-7_STRICT" and idx == 1 and audio_lang)
                else ""
            ),
            "\t\t\t\t\t\t\t</mpeg7:AudioCoding>",
        ])
        lines[:] = [line for line in lines if line != ""]
        if profile == "MPEG-7_STRICT" and idx > 1:
            lines.append("\t\t\t\t\t\t\t-->")

    if texts:
        if profile == "MPEG-7_STRICT":
            strict_text = engine._public_fields(texts[0])
            lines.extend([
                "\t\t\t\t\t\t\t<!-- No Textual track in strict MPEG-7",
                "\t\t\t\t\t\t\t<mpeg7:TextualCoding>",
                "\t\t\t\t\t\t\t\t<mpeg7:MediaLocator>",
                f"\t\t\t\t\t\t\t\t\t<mpeg7:StreamID>{xml_escape(texts[0].fields.get('ID', '1'))}</mpeg7:StreamID>",
                "\t\t\t\t\t\t\t\t</mpeg7:MediaLocator>",
                '\t\t\t\t\t\t\t\t<mpeg7:Format href="urn:x-mpeg7-mediainfo:cs:TextualCodingFormatCS:2009:unknown">',
                f"\t\t\t\t\t\t\t\t\t<mpeg7:Name xml:lang=\"en\">{xml_escape(texts[0].fields.get('Format', ''))}</mpeg7:Name>",
                "\t\t\t\t\t\t\t\t</mpeg7:Format>",
                (
                    f'\t\t\t\t\t\t\t\t<mpeg7:language closed="false">{xml_escape(string(strict_text.get("Language", "")))}</mpeg7:language>'
                    if string(strict_text.get("Language", ""))
                    else ""
                ),
                "\t\t\t\t\t\t\t</mpeg7:TextualCoding>",
                "\t\t\t\t\t\t\t-->",
            ])
            lines[:] = [line for line in lines if line != ""]
        elif use_locator_style:
            for idx, text in enumerate(texts, start=1):
                f = engine._public_fields(text)
                lines.extend([
                    f'\t\t\t\t\t\t\t<mpeg7:TextualCoding id="textual.{idx}">',
                    "\t\t\t\t\t\t\t\t<mpeg7:MediaLocator>",
                    f"\t\t\t\t\t\t\t\t\t<mpeg7:StreamID>{xml_escape(f.get('ID', str(idx)))}</mpeg7:StreamID>",
                    "\t\t\t\t\t\t\t\t</mpeg7:MediaLocator>",
                    '\t\t\t\t\t\t\t\t<mpeg7:Format href="urn:x-mpeg7-mediainfo:cs:TextualCodingFormatCS:2009:unknown">',
                    f'\t\t\t\t\t\t\t\t\t<mpeg7:Name xml:lang="en">{xml_escape(f.get("Format", ""))}</mpeg7:Name>',
                    "\t\t\t\t\t\t\t\t</mpeg7:Format>",
                    (
                        f"\t\t\t\t\t\t\t\t<!-- Language: {xml_escape(string(f.get('Language', '')))} -->"
                        if string(f.get("Language", ""))
                        else ""
                    ),
                    "\t\t\t\t\t\t\t</mpeg7:TextualCoding>",
                ])
                lines[:] = [line for line in lines if line != ""]

    lines.extend([
        "\t\t\t\t\t\t</mpeg7:MediaFormat>",
        "\t\t\t\t\t</mpeg7:MediaProfile>",
        "\t\t\t\t</mpeg7:MediaInformation>",
        "\t\t\t\t<mpeg7:CreationInformation>",
        "\t\t\t\t\t<mpeg7:Creation>",
        (
            f'\t\t\t\t\t\t<mpeg7:Title type="main">{xml_escape(string(general.fields.get("Title", "")))}</mpeg7:Title>'
            if general and string(general.fields.get("Title", ""))
            else f"\t\t\t\t\t\t<mpeg7:Title>{xml_escape(Path(report.source).name)}</mpeg7:Title>"
        ),
        "\t\t\t\t\t\t<mpeg7:CreationTool>",
        "\t\t\t\t\t\t\t<mpeg7:Tool>",
        "\t\t\t\t\t\t\t\t<!-- Writing application -->",
        f"\t\t\t\t\t\t\t\t<mpeg7:Name>{xml_escape((general.fields.get('Encoded_Application') if general else '') or '')}</mpeg7:Name>",
        "\t\t\t\t\t\t\t</mpeg7:Tool>",
        "\t\t\t\t\t\t</mpeg7:CreationTool>",
        "\t\t\t\t\t</mpeg7:Creation>",
    ])

    audio_langs = [string(engine._public_fields(a).get("Language", "")).strip() for a in audios]
    audio_langs = [lang for lang in audio_langs if lang]
    text_langs = [string(engine._public_fields(t).get("Language", "")).strip() for t in texts]
    text_langs = [lang for lang in text_langs if lang]
    if audio_langs or text_langs:
        lines.append("\t\t\t\t\t<mpeg7:Classification>")
        if profile == "MPEG-7_STRICT":
            for lang in audio_langs:
                lines.append("\t\t\t\t\t\t<!-- below is audio languages with same order as in MediaFormat -->")
                lines.append(f"\t\t\t\t\t\t<mpeg7:Language>{xml_escape(lang)}</mpeg7:Language>")
            for lang in text_langs:
                lines.append(f'\t\t\t\t\t\t<mpeg7:CaptionLanguage closed="false">{xml_escape(lang)}</mpeg7:CaptionLanguage>')
        else:
            for idx, lang in enumerate(audio_langs, start=1):
                lines.append(f'\t\t\t\t\t\t<mpeg7:Language ref="audio.{idx}">{xml_escape(lang)}</mpeg7:Language>')
            for idx, lang in enumerate(text_langs, start=1):
                lines.append(f'\t\t\t\t\t\t<mpeg7:CaptionLanguage closed="false" ref="textual.{idx}">{xml_escape(lang)}</mpeg7:CaptionLanguage>')
        lines.append("\t\t\t\t\t</mpeg7:Classification>")

    lines.extend([
        "\t\t\t\t</mpeg7:CreationInformation>",
        "\t\t\t\t<mpeg7:MediaTime>",
        f"\t\t\t\t\t<mpeg7:MediaTimePoint>{'T00:00:00' if is_mp4_container else 'T00:00:00:0F1000'}</mpeg7:MediaTimePoint>",
        (
            f"\t\t\t\t\t<mpeg7:MediaDuration>{xml_escape(mpeg7_duration((general.fields.get('Duration') if general else ''), general.fields.get('FrameRate') if general else ''))}</mpeg7:MediaDuration>"
            if not (is_video_only and not string(general.fields.get('FrameRate') if general else '').strip())
            else ""
        ),
        "\t\t\t\t</mpeg7:MediaTime>",
        "\t\t\t</mpeg7:Video>" if is_video_only else "\t\t\t</mpeg7:AudioVisual>",
        "\t\t</mpeg7:MultimediaContent>",
        "\t</mpeg7:Description>",
        "</mpeg7:Mpeg7>",
        "",
    ])
    lines[:] = [line for line in lines if line != ""]
    return "\n".join(lines) + "\n\n"
