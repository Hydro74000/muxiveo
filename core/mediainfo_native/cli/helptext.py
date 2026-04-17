"""CLI help texts."""

from __future__ import annotations


def help_text() -> str:
    return "\n".join([
        'Usage: "minfo [-Options...] FileName1 [Filename2...]"',
        "",
        "Options:",
        "--Help, -h",
        "                    Display this help and exit",
        "--Help-Output",
        "                    Display help for Output= option",
        "--Help-AnOption=OptionName",
        "                    Display help for one option",
        "--Version",
        "                    Display MediaInfo version and exit",
        "--Full, -f",
        "                    Full information display (shim mode)",
        "--Output=JSON|XML|HTML|CSV|EBUCore|PBCore2|MPEG-7_Strict",
        "                    Select output format",
        "--Inform=Video;%FrameCount%",
        "                    Query specific fields",
        "--Info-Parameters",
        "                    Display list of Inform parameters",
        "--Info-OutputFormats",
        "                    Display list of output formats",
        "--Language=raw",
        "                    Internal identifiers",
        "--ParseSpeed=0.5",
        "                    Parsing speed hint",
        "--inform_version=1 / --inform_timestamp=1",
        "                    Add metadata header lines to text output",
    ]) + "\n"


def help_output_text() -> str:
    return "\n".join([
        "--Output=...  Specify an output format or template",
        'Usage: "minfo --Output=JSON FileName"',
        'Usage: "minfo --Output=Video;%FrameCount% FileName"',
        "",
        "Supported formats: Text, HTML, XML, OLDXML, JSON, CSV,",
        "EBUCore, EBUCore_JSON, EBUCore_1.8_ps, EBUCore_1.8_sp,",
        "PBCore, PBCore2, MPEG-7_Strict, MPEG-7_Relaxed, MPEG-7_Extended,",
        "FIMS_1.1, FIMS_1.2, FIMS_1.3, NISO_Z39.87, Graph_Svg, Graph_Dot",
    ]) + "\n"
