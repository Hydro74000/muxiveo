"""
Standalone CLI compatible shim for mediainfo.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from ..engine import CLI_VERSION_TEXT, MediaInfoEngine
from .helptext import help_output_text, help_text


@dataclass(slots=True)
class CliResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, prog="minfo")
    parser.add_argument("files", nargs="*")
    parser.add_argument("--Help", "--help", "-h", dest="help", action="store_true")
    parser.add_argument("--Help-Output", dest="help_output", action="store_true")
    parser.add_argument("--Help-AnOption", dest="help_an_option")
    parser.add_argument("--Version", dest="version", action="store_true")
    parser.add_argument("--Info-Parameters", "--Info_Parameters", dest="info_parameters", action="store_true")
    parser.add_argument("--Info-OutputFormats", "--Info_OutputFormats", dest="info_outputformats", action="store_true")
    parser.add_argument("--Output", dest="output")
    parser.add_argument("--Inform", dest="inform")
    parser.add_argument("--Language", dest="language")
    parser.add_argument("--LogFile", dest="logfile")
    parser.add_argument("--Full", "-f", dest="full", action="store_true")
    parser.add_argument("--BOM", dest="bom", action="store_true")
    return parser


def _split_dynamic_option(token: str) -> tuple[str, str] | None:
    if not token.startswith("--"):
        return None
    body = token[2:]
    if not body:
        return None
    if "=" in body:
        key, value = body.split("=", 1)
        return key, value
    return body, "1"


def run_cli(argv: list[str]) -> CliResult:
    parser = _build_parser()
    try:
        ns, unknown = parser.parse_known_args(argv)
    except SystemExit as exc:
        return CliResult(returncode=int(exc.code or 1), stderr=help_text())

    engine = MediaInfoEngine()

    if ns.help:
        return CliResult(0, help_text(), "")
    if ns.help_output:
        return CliResult(0, help_output_text(), "")
    if ns.help_an_option:
        return CliResult(0, engine.option_help(ns.help_an_option) + "\n", "")
    if ns.version:
        return CliResult(0, f"{CLI_VERSION_TEXT}\n", "")
    if ns.info_parameters:
        return CliResult(0, engine.info_parameters() + "\n", "")
    if ns.info_outputformats:
        return CliResult(0, engine.info_output_formats() + "\n", "")

    extra_files: list[str] = []
    for token in unknown:
        split = _split_dynamic_option(token)
        if split is None:
            extra_files.append(token)
            continue
        key, value = split
        engine.option(key, value)

    if ns.inform:
        engine.option("inform", ns.inform)
    if ns.output:
        # MediaInfo CLI: --Output can be format name or template expression.
        if ";" in ns.output or ns.output.lower().startswith("file://"):
            engine.option("inform", ns.output)
        else:
            engine.option("output", ns.output)
    if ns.language:
        engine.option("language", ns.language)
    if ns.full:
        engine.option("complete", "1")
    if ns.bom:
        engine.option("bom", "1")

    files = [*ns.files, *extra_files]
    if not files:
        return CliResult(1, "", 'Usage: "minfo [-Options...] FileName1 [Filename2...]"\n')

    inform_expr = engine.option("inform_get")
    output_mode = engine.option("output_get") or "Text"

    out_chunks: list[str] = []
    errors: list[str] = []
    for item in files:
        try:
            if inform_expr:
                content = engine.query_inform(item, inform_expr)
                out_chunks.append(content + ("\n" if content else ""))
            else:
                rendered = engine.render(item, output_mode=output_mode)
                out_chunks.append(rendered)
        except Exception as exc:
            errors.append(f"{item}: {exc}")

    stdout = "".join(out_chunks)
    stderr = "\n".join(errors) + ("\n" if errors else "")

    if engine.option("bom_get") == "1" and stdout and not stdout.startswith("\ufeff"):
        stdout = "\ufeff" + stdout

    if ns.logfile and stdout:
        try:
            Path(ns.logfile).write_text(stdout, encoding="utf-8")
        except OSError as exc:
            stderr += f"LogFile write failed: {exc}\n"

    return CliResult(1 if errors else 0, stdout, stderr)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    result = run_cli(args)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
