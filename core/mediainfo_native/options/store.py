"""Runtime option store and metadata for mediainfo_native."""

from __future__ import annotations

from dataclasses import dataclass, field

OPTION_DEFAULTS: dict[str, str] = {
    "inform": "",
    "output": "",
    "language": "",
    "complete": "0",
    "readbyhuman": "1",
    "legacystreamdisplay": "0",
    "cover_data": "",
    "trace_level": "",
    "details": "0",
    "parse_speed": "0.5",
    "demux": "",
    "file_testcontinuousfilenames": "1",
    "inform_version": "0",
    "inform_timestamp": "0",
    "externalmetadata": "",
    "externalmetadataconfig": "",
    "acquisitiondataoutputmode": "segmentParameter",
    "bom": "0",
}

OPTION_ALIASES: dict[str, str] = {
    "complete_get": "complete_get",
    "read_by_human": "readbyhuman",
    "readbyhuman_get": "readbyhuman_get",
    "legacy_stream_display": "legacystreamdisplay",
    "legacy_stream_display_get": "legacystreamdisplay_get",
    "legacystreamdisplay_get": "legacystreamdisplay_get",
    "coverdata": "cover_data",
    "cover_data_get": "cover_data_get",
    "parsespeed": "parse_speed",
    "parsespeed_get": "parse_speed_get",
    "parse_speed_get": "parse_speed_get",
    "file_testcontinuousfilenames_get": "file_testcontinuousfilenames_get",
    "external_metadata": "externalmetadata",
    "external_metadata_config": "externalmetadataconfig",
    "acquisition_data_output_mode": "acquisitiondataoutputmode",
    "info_parameters": "info_parameters",
    "info_outputformats": "info_outputformats",
    "info_output_formats": "info_outputformats",
    "info_options": "info_options",
    "info_option": "info_options",
    "option_help": "help",
    "help_anoption": "help_anoption",
}

OUTPUT_ALIASES: dict[str, str] = {
    "TEXT": "TEXT",
    "HTML": "HTML",
    "XML": "XML",
    "OLDXML": "OLDXML",
    "JSON": "JSON",
    "CSV": "CSV",
    "EBUCORE": "EBUCORE_1.8_PS",
    "EBUCORE_1.5": "EBUCORE_1.5",
    "EBUCORE_1.6": "EBUCORE_1.6",
    "EBUCORE_1.8": "EBUCORE_1.8_PS",
    "EBUCORE_1.8_PS": "EBUCORE_1.8_PS",
    "EBUCORE_1.8_SP": "EBUCORE_1.8_SP",
    "EBUCORE_JSON": "EBUCORE_1.8_PS_JSON",
    "EBUCORE_1.8_JSON": "EBUCORE_1.8_PS_JSON",
    "EBUCORE_1.8_PS_JSON": "EBUCORE_1.8_PS_JSON",
    "EBUCORE_1.8_SP_JSON": "EBUCORE_1.8_SP_JSON",
    "PBCORE": "PBCORE",
    "PBCORE2": "PBCORE2",
    "MPEG-7": "MPEG-7_STRICT",
    "MPEG_7": "MPEG-7_STRICT",
    "MPEG-7_STRICT": "MPEG-7_STRICT",
    "MPEG-7_RELAXED": "MPEG-7_RELAXED",
    "MPEG-7_EXTENDED": "MPEG-7_EXTENDED",
    "FIMS_1.1": "FIMS_1.1",
    "FIMS_1.2": "FIMS_1.2",
    "FIMS_1.3": "FIMS_1.3",
    "NISO_Z39.87": "NISO_Z39.87",
    "GRAPH_SVG": "GRAPH_SVG",
    "GRAPH_DOT": "GRAPH_DOT",
}

_INFO_PARAMETERS_LINES: tuple[str, ...] = (
    "General_CompleteName            : Complete name",
    "General_Format                  : Format",
    "General_FileSize                : File size",
    "General_Duration                : Duration",
    "General_OverallBitRate          : Overall bit rate",
    "Video_Format                    : Video format",
    "Video_Format/String             : Video format (string)",
    "Video_HDR_Format                : HDR format",
    "Video_HDR_Format_Compatibility  : HDR compatibility",
    "Video_FrameCount                : Frame count",
    "Video_FrameRate                 : Frame rate",
    "Video_Duration                  : Duration",
    "Video_BitRate                   : Bit rate",
    "Audio_Format                    : Audio format",
    "Audio_BitRate                   : Audio bit rate",
    "Audio_Channel(s)                : Channels",
    "Text_Format                     : Text format",
    "Text_Forced                     : Forced subtitle flag",
)

_INFO_OUTPUT_FORMATS_LINES: tuple[str, ...] = (
    "Text                 : Text",
    "HTML                 : HTML",
    "XML                  : MediaInfo XML",
    "OLDXML               : MediaInfo XML (legacy)",
    "JSON                 : MediaInfo JSON",
    "CSV                  : MediaInfo CSV",
    "EBUCore              : EBUCore 1.8 (XML, part=ps)",
    "EBUCore_JSON         : EBUCore 1.8 (JSON)",
    "EBUCore_1.8_ps       : EBUCore 1.8 (XML, part=ps)",
    "EBUCore_1.8_sp       : EBUCore 1.8 (XML, part=sp)",
    "EBUCore_1.8_ps_JSON  : EBUCore 1.8 (JSON, part=ps)",
    "EBUCore_1.8_sp_JSON  : EBUCore 1.8 (JSON, part=sp)",
    "PBCore               : PBCore",
    "PBCore2              : PBCore 2.0",
    "MPEG-7_Strict        : MPEG-7 strict",
    "MPEG-7_Relaxed       : MPEG-7 relaxed",
    "MPEG-7_Extended      : MPEG-7 extended",
    "FIMS_1.1             : FIMS 1.1",
    "FIMS_1.2             : FIMS 1.2",
    "FIMS_1.3             : FIMS 1.3",
    "NISO_Z39.87          : NISO Z39.87",
    "Graph_Svg            : Graph SVG",
    "Graph_Dot            : Graph DOT",
)

_INFO_OPTIONS_LINES: tuple[str, ...] = (
    "Inform                     : Set/Get Inform template",
    "Output                     : Set/Get output format",
    "Language                   : Set/Get language mode",
    "Complete                   : Set/Get complete report mode",
    "ReadByHuman                : Set/Get human-readable output mode",
    "LegacyStreamDisplay        : Set/Get legacy stream compatibility display",
    "ParseSpeed                 : Set/Get parsing speed [0..1]",
    "Details                    : Set/Get trace details",
    "Trace_Level                : Set/Get trace level",
    "Demux                      : Set/Get demux mode",
    "File_TestContinuousFileNames : Set/Get image sequence detection",
    "inform_version             : Add MediaInfoLib version to text output",
    "inform_timestamp           : Add report creation timestamp to text output",
    "BOM                        : UTF-8 BOM for CLI output",
    "Info_Parameters            : List Inform fields",
    "Info_OutputFormats         : List output formats",
    "Info_Options               : List supported options",
    "Reset                      : Reset runtime options to defaults",
)

_OPTION_HELP_MAP: dict[str, str] = {
    "inform": "Inform: template expression e.g. Video;%FrameCount%",
    "output": "Output: Text, JSON, XML, EBUCore, PBCore2, MPEG-7_Strict, ...",
    "language": "Language: raw or localized labels (currently raw pass-through)",
    "complete": "Complete: 1 enables full-style report fields",
    "readbyhuman": "ReadByHuman: 1 human labels, 0 machine-style output",
    "legacystreamdisplay": "LegacyStreamDisplay: compatibility display hints",
    "parse_speed": "ParseSpeed: value from 0 to 1",
    "details": "Details: trace/detail output level",
    "trace_level": "Trace_Level: MediaTrace granularity",
    "demux": "Demux: demux mode selection",
    "file_testcontinuousfilenames": "File_TestContinuousFileNames: 0 disables image sequence probing",
    "inform_version": "inform_version: prepend MediaInfoLib version on text output",
    "inform_timestamp": "inform_timestamp: prepend report creation timestamp on text output",
    "bom": "BOM: prepend UTF-8 BOM in CLI output",
}


def normalize_option(option: str) -> str:
    normalized = option.strip().lower().replace("-", "_")
    return OPTION_ALIASES.get(normalized, normalized)


def normalize_output_mode(mode: str) -> str:
    normalized = mode.strip().upper().replace(" ", "_")
    return OUTPUT_ALIASES.get(normalized, normalized)


def info_parameters_text() -> str:
    return "\n".join(_INFO_PARAMETERS_LINES)


def info_output_formats_text() -> str:
    return "\n".join(_INFO_OUTPUT_FORMATS_LINES)


def info_options_text() -> str:
    return "\n".join(_INFO_OPTIONS_LINES)


def option_help_text(option_name: str) -> str:
    key = normalize_option(option_name)
    return _OPTION_HELP_MAP.get(key, f"No detailed help for option: {option_name}")


@dataclass(slots=True)
class OptionStore:
    values: dict[str, str] = field(default_factory=lambda: dict(OPTION_DEFAULTS))

    def normalize(self, option: str) -> str:
        return normalize_option(option)

    def get(self, option: str) -> str:
        key = self.normalize(option)
        if key.endswith("_get"):
            key = key[:-4]
        return self.values.get(key, "")

    def set(self, option: str, value: str) -> str:
        key = self.normalize(option)
        if key in self.values:
            self.values[key] = str(value)
            return self.values[key]
        return ""

    def reset(self) -> None:
        self.values = dict(OPTION_DEFAULTS)
