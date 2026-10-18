"""Rapport HDR exporté : JSON (machines, CLI) ou page HTML autonome (lecture, partage).

Même contenu que le HDR Studio : piste, HDR10 statique, Dolby Vision, HDR10+, mesure de l'image,
contrôles, courbes de luminance réduites à ``columns`` colonnes, FEL Analyzer et comparaison.
"""
from __future__ import annotations

import base64
import html
import json
import struct
import zlib
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from core.atomic_io import atomic_write_text
from core.hdr_inspection import axes, summary
from core.hdr_inspection.compare import SOURCE_LABELS, Comparison
from core.hdr_inspection.fel_analyzer import CATEGORIES, FelReport, recommendation
from core.hdr_inspection.inspector import InspectionReport
from core.hdr_inspection.measure import ImageMeasurement
from core.hdr_inspection.pq import pq12_to_nits
from core.hdr_inspection.snapshot import HdrSnapshot
from core.i18n import current_language, translate_text
from core.version import APP_NAME, APP_VERSION

REPORT_FORMAT = "muxiveo-hdr-report"
REPORT_VERSION = 1
#: Colonnes des courbes exportées (maximum ou moyenne par colonne).
COLUMNS = 1200
_SERIES_COLORS = {"peak": "#4f8cff", "average": "#3ecf8e", "l1_max": "#a970ff", "l1_avg": "#6fa8ff",
                  "maxscl": "#f2b33d", "avgrgb": "#e07ad8", "a": "#4f8cff", "b": "#f2b33d"}


# ── Données ─────────────────────────────────────────────────────────────────
def _number(value: float | int | None, digits: int = 3) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return int(value)
    value = float(value)
    return round(value, digits) if np.isfinite(value) else None


def _column_values(values: np.ndarray, columns: int, mode: str) -> list[float | None]:
    aggregated = summary.bucket(values, 0, values.size, columns, mode)
    return [round(float(v), 2) if np.isfinite(v) else None for v in aggregated]


def series(report: InspectionReport, measurement: ImageMeasurement | None) -> list[tuple[str, str, str, np.ndarray]]:
    """Courbes par image (clé, libellé, agrégation, nits), dans l'ordre de l'onglet Luminance."""
    found: list[tuple[str, str, str, np.ndarray]] = []
    if measurement is not None and measurement.frames:
        found.append(("peak", translate_text("Pic mesuré"), "max", measurement.peaks().astype(np.float64)))
        found.append(("average", translate_text("Moyenne mesurée"), "mean", measurement.average.astype(np.float64)))
    dovi = report.dovi
    if dovi is not None and dovi.frames:
        found.append(("l1_max", translate_text("L1 max"), "max", pq12_to_nits(dovi.l1_max)))
        found.append(("l1_avg", translate_text("L1 moyenne"), "mean", pq12_to_nits(dovi.l1_avg)))
    hdr10plus = report.hdr10plus
    if hdr10plus is not None and hdr10plus.frames:
        found.append(("maxscl", translate_text("MaxScl HDR10+"), "max", hdr10plus.max_scl.astype(np.float64)))
        found.append(("avgrgb", translate_text("AverageRGB HDR10+"), "mean", hdr10plus.average_rgb.astype(np.float64)))
    return found


def _source(snapshot: HdrSnapshot) -> dict:
    return {
        "path": snapshot.path, "name": Path(snapshot.path).name, "stream_index": snapshot.stream_index,
        "codec": snapshot.codec, "width": snapshot.width, "height": snapshot.height,
        "fps": _number(snapshot.fps, 6), "bit_depth": snapshot.bit_depth, "transfer": snapshot.transfer,
        "primaries": snapshot.primaries, "matrix": snapshot.matrix, "duration_s": _number(snapshot.duration_s),
        "frames": snapshot.frames, "frames_exact": snapshot.frames_exact, "formats": list(snapshot.formats),
    }


def _dovi_data(report: InspectionReport) -> dict | None:
    record, dovi = report.snapshot.dovi, report.dovi
    if record is None and dovi is None:
        return None
    data: dict = {"record": None if record is None else {
        "profile": record.profile, "level": record.level, "compat_id": record.compat_id,
        "bl": record.bl, "el": record.el, "rpu": record.rpu}}
    if dovi is not None:
        data["rpu"] = {
            "frames": dovi.frames, "profile": dovi.profile, "el_type": dovi.el_type,
            "residual_active_frames": dovi.residual_active_frames, "cm_version": dovi.cm_version,
            "scenes": dovi.scenes, "l1_missing": dovi.l1_missing,
            "l1_peak_nits": _number(dovi.l1_peak_nits(), 1), "l1_max_average_nits": _number(dovi.l1_fall_nits(), 2),
            "l6": [asdict(item) for item in dovi.l6],
            "active_areas": [asdict(item) for item in dovi.active_areas],
            "source_display": [{"min_nits": lo, "max_nits": hi} for lo, hi in dovi.source_display],
            "l2_targets": list(dovi.l2_targets), "l8_targets": list(dovi.l8_targets),
            "l10_targets": list(dovi.l10_targets), "l9_primaries": list(dovi.l9_primaries),
            "l11_content": list(dovi.l11_content), "reused_rpu_frames": dovi.reused_rpu_frames,
        }
    return data


def _hdr10plus_data(report: InspectionReport) -> dict | None:
    hdr10plus = report.hdr10plus
    if hdr10plus is None:
        return {"present": True} if report.snapshot.hdr10plus else None
    return {
        "present": True, "frames": hdr10plus.frames, "profile": hdr10plus.profile, "scenes": hdr10plus.scenes,
        "peak_nits": _number(hdr10plus.peak_nits(), 1), "credible_peak_nits": _number(hdr10plus.credible_peak_nits(), 1),
        "saturated_frames": int(hdr10plus.saturated().size),
        "max_average_nits": _number(hdr10plus.max_average_nits(), 2),
        "p99_98_max_nits": _number(float(hdr10plus.p99.max()) if hdr10plus.frames else None, 1),
        "windows": list(hdr10plus.windows), "targeted_display": list(hdr10plus.targeted_display),
        "bezier_frames": hdr10plus.bezier_frames,
    }


def _fel_data(fel: FelReport) -> dict:
    verdict, detail = recommendation(fel)
    facts = fel.facts
    labels = {key: (label, unit) for key, label, unit, _ in CATEGORIES}
    return {
        "verdict": verdict, "detail": detail, "samples": len(fel.samples),
        "categories": [{"key": item.key, "label": translate_text(labels[item.key][0]),
                        "unit": translate_text(labels[item.key][1]), "median": _number(item.median),
                        "p90": _number(item.p90), "level": translate_text(item.level)} for item in fel.summary],
        "layers": None if facts is None else {
            "frames": facts.frames, "duration_s": _number(facts.duration_s),
            "bl_bitrate": _number(facts.bitrate(facts.bl_bytes), 0), "el_bitrate": _number(facts.bitrate(facts.el_bytes), 0),
            "rpu_bitrate": _number(facts.bitrate(facts.rpu_bytes), 0), "el_share": _number(facts.el_share, 4),
            "el_size": list(facts.el_size) if facts.el_size else None},
        "survival": None if fel.survival is None else {k: _number(v) if isinstance(v, float) else v
                                                       for k, v in asdict(fel.survival).items()},
        "moments": [{"time_s": _number(m.time_s), "score": _number(m.score)} for m in fel.moments],
    }


def _comparison_data(comparison: Comparison, other: InspectionReport, fps: float) -> dict:
    return {
        "other": _source(other.snapshot), "offset_frames": comparison.offset,
        "offset_s": _number(comparison.offset / fps), "alignment": comparison.alignment,
        "alignment_score": _number(comparison.alignment_score),
        "fields": [{"label": f.label, "a": f.a, "b": f.b, "same": f.same} for f in comparison.fields],
        "curves": [{**{k: _number(v) if isinstance(v, float) else v for k, v in asdict(c).items() if k != "worst"},
                    "worst_s": [_number(t) for t in c.worst]} for c in comparison.curves],
    }


def report_data(report: InspectionReport, *, measurement: ImageMeasurement | None = None,
                fel: FelReport | None = None, comparison: Comparison | None = None,
                other: InspectionReport | None = None, columns: int = COLUMNS) -> dict:
    """Rapport sérialisable (``json.dumps(..., allow_nan=False)``)."""
    snapshot = report.snapshot
    fps = snapshot.fps or 24000 / 1001
    curves = series(report, measurement)
    frames = max((values.size for *_, values in curves), default=0)
    mastering = snapshot.mastering
    return {
        "format": REPORT_FORMAT, "version": REPORT_VERSION, "generator": f"{APP_NAME} {APP_VERSION}",
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": _source(snapshot), "dynamic_metadata_read": report.dynamic,
        "static": {"mastering": None if mastering is None else asdict(mastering),
                   "max_cll": snapshot.max_cll, "max_fall": snapshot.max_fall},
        "dolby_vision": _dovi_data(report), "hdr10plus": _hdr10plus_data(report),
        "measurement": None if measurement is None else {
            "layer": measurement.layer, "frames": measurement.frames, "max_cll": measurement.max_cll(),
            "max_fall": measurement.max_fall(), "window": list(measurement.window),
            "sampled_frames": measurement.sampled or None},
        "findings": [{"severity": f.severity.value, "category": f.category, "title": f.title, "detail": f.detail}
                     for f in report.findings],
        "errors": list(report.errors),
        "curves": {"frames": frames, "fps": _number(fps, 6), "columns": min(columns, frames),
                   "series": [{"key": key, "label": label, "mode": mode,
                               "values": _column_values(values, columns, mode)} for key, label, mode, values in curves]},
        "fel": None if fel is None else _fel_data(fel),
        "comparison": None if comparison is None or other is None else _comparison_data(comparison, other, fps),
    }


# ── HTML ────────────────────────────────────────────────────────────────────
_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--dim:#5d6672;--line:#dde1e6;--ok:#1f9d5b;--info:#2d6fd6;
--warn:#b7791f;--error:#d64545;--grid:#e3e6ea}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--text:#e8eaed;--dim:#9aa3ad;
--line:#2c3138;--ok:#3ecf8e;--info:#6fa8ff;--warn:#f2b33d;--error:#ff6b6b;--grid:#2a2f36}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);
margin:0 0 10px}.sub{color:var(--dim);margin:0 0 18px;word-break:break-all}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:0 0 14px;
overflow-x:auto}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:4px 10px 4px 0;
vertical-align:top}th{color:var(--dim);font-weight:600}.kv td:first-child{color:var(--dim);width:38%}
.badges span{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:1px 10px;margin:0 6px 6px 0}
.f{display:flex;gap:8px;padding:3px 0}.f b{width:18px;flex:none;text-align:center}.ok b{color:var(--ok)}
.info b{color:var(--info)}.warn b{color:var(--warn)}.error b{color:var(--error)}.f small{display:block;color:var(--dim)}
.diff td{color:var(--warn);font-weight:600}svg{width:100%;height:auto;display:block}.legend span{margin-right:14px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
.moment{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 14px}.moment figure{margin:0;flex:1 1 220px}
.moment img{width:100%;border-radius:4px}.moment figcaption{color:var(--dim);font-size:12px}
footer{color:var(--dim);font-size:12px;text-align:center;margin:18px 0}
section h2:not(:first-child){margin-top:18px}
"""
_ICONS = {"ok": "✓", "info": "i", "warn": "!", "error": "✕"}


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _kv(rows: list[tuple[str, str]]) -> str:
    return "<table class='kv'>" + "".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in rows) + "</table>"


def _table(headers: list[str], rows: list[list[str]], marked: list[bool] | None = None) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join(f"<tr{' class=diff' if marked and marked[i] else ''}>" + "".join(f"<td>{_e(c)}</td>" for c in row)
                   + "</tr>" for i, row in enumerate(rows))
    return f"<table><tr>{head}</tr>{body}</table>"


def _section(title: str, body: str) -> str:
    return f"<section><h2>{_e(title)}</h2>{body}</section>"


def svg_chart(curves: list[tuple[str, str, list[float | None], str]], *, frames: int, fps: float,
              width: int = 1000, height: int = 300) -> str:
    """Courbes (libellé, couleur, valeurs par colonne, agrégation) en SVG, échelle PQ à plage ajustée.

    Pics (agrégation « max ») sur l'axe gauche, moyennes (« mean ») sur l'axe droit, chacun à sa plage.
    """
    left, top_margin, plot_h = 52, 18, height - 42
    dual = any(mode == "mean" for *_, mode in curves) and any(mode != "mean" for *_, mode in curves)
    plot_w = width - left - (56 if dual else 8)

    def axis_of(mode: str) -> str:
        return "second" if dual and mode == "mean" else "main"

    ranges: dict[str, tuple[float, float]] = {}
    for axis in ("main", "second"):
        values = np.array([v for *_, column, mode in curves if axis_of(mode) == axis for v in column if v is not None],
                          dtype=np.float64)
        ranges[axis] = axes.fit_range(values, "pq") if values.size else (0.1, 1000.0)

    def y_of(value: float, axis: str) -> float:
        low, high = ranges[axis]
        return top_margin + plot_h * (1 - float(axes.position(value, low, high, "pq")))

    parts = [f"<svg viewBox='0 0 {width} {height}' role='img' xmlns='http://www.w3.org/2000/svg'>"]
    for tick in axes.ticks(*ranges["main"], "pq", 18 / plot_h):
        y = y_of(tick, "main")
        parts.append(f"<line x1='{left}' x2='{left + plot_w}' y1='{y:.1f}' y2='{y:.1f}' style='stroke:var(--grid)'/>"
                     f"<text x='{left - 6}' y='{y + 4:.1f}' font-size='11' text-anchor='end' style='fill:var(--dim)'>"
                     f"{tick:g}</text>")
    if dual:
        for tick in axes.ticks(*ranges["second"], "pq", 18 / plot_h):
            y = y_of(tick, "second")
            parts.append(f"<text x='{left + plot_w + 6}' y='{y + 4:.1f}' font-size='11' style='fill:var(--dim)'>"
                         f"{tick:g}</text>")
        parts.append(f"<text x='{left}' y='12' font-size='10' style='fill:var(--dim)'>"
                     f"{_e(translate_text('← pics (nits)'))}</text>"
                     f"<text x='{left + plot_w}' y='12' font-size='10' text-anchor='end' style='fill:var(--dim)'>"
                     f"{_e(translate_text('moyennes (nits) →'))}</text>")
    duration = frames / fps if fps > 0 else 0
    step = next((s for s in (60, 300, 600, 900, 1800, 3600) if duration / s <= 10), 3600) if duration > 60 else 10
    seconds = 0
    while duration and seconds <= duration:
        x = left + plot_w * seconds / duration
        parts.append(f"<text x='{x:.1f}' y='{height - 4}' font-size='11' text-anchor='middle' style='fill:var(--dim)'>"
                     f"{summary.format_time(seconds)}</text>")
        seconds += step
    for _label, color, column, mode in curves:
        if not column:
            continue
        path, pen = [], "M"
        for index, value in enumerate(column):
            if value is None:
                pen = "M"
                continue
            x = left + plot_w * (index + 0.5) / len(column)
            path.append(f"{pen}{x:.1f},{y_of(value, axis_of(mode)):.1f}")
            pen = "L"
        parts.append(f"<path d='{' '.join(path)}' fill='none' stroke='{color}' stroke-width='1.2'/>")
    parts.append(f"<rect x='{left}' y='{top_margin}' width='{plot_w}' height='{plot_h}' "
                 f"style='fill:none;stroke:var(--line)'/></svg>")
    legend = "".join(f"<span><i style='background:{color}'></i>{_e(label)}</span>" for label, color, *_ in curves)
    return "".join(parts) + f"<div class='legend'>{legend}</div>"


def png_data_uri(image: np.ndarray) -> str:
    """Image RGB 8 bits (H × W × 3) → PNG en data URI (sans dépendance d'imagerie)."""
    height, width = image.shape[:2]
    raw = b"".join(b"\x00" + np.ascontiguousarray(image[row], dtype=np.uint8).tobytes() for row in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _findings_html(report: InspectionReport) -> str:
    items = [(f.severity.value, f.title, f.detail) for f in report.findings]
    items += [("error", message, "") for message in report.errors]
    if not items:
        return f"<p>{_e(translate_text('Aucune remarque.'))}</p>"
    return "".join(f"<div class='f {level}'><b>{_ICONS[level]}</b><div>{_e(title)}"
                   f"{f'<small>{_e(detail)}</small>' if detail else ''}</div></div>" for level, title, detail in items)


def _fel_html(fel: FelReport) -> str:
    data = _fel_data(fel)
    parts = [f"<p><b>{_e(data['verdict'])}</b><br>{_e(data['detail'])}</p>"]
    parts.append(_table([translate_text("Catégorie"), translate_text("Médiane"), translate_text("p90"),
                         translate_text("Niveau")],
                        [[c["label"], f"{c['median']:.2f} {c['unit']}", f"{c['p90']:.2f} {c['unit']}", c["level"]]
                         for c in data["categories"]]))
    survival = fel.survival
    if survival is not None:
        parts.append("<p>" + _e(translate_text(
            "{encoder} à {bitrate} Mbit/s : ton conservé à {tone} %, détail à {detail} % "
            "(erreur d'encodage {error} codes RMS pour un apport de {contribution} codes RMS).",
            encoder=survival.encoder, bitrate=round(survival.bitrate_kbps / 1000), tone=round(survival.tone_kept * 100),
            detail=round(survival.detail_kept * 100), error=f"{survival.encode_error:.2f}",
            contribution=f"{survival.detail:.2f}")) + "</p>")
    for moment in fel.moments:
        parts.append(f"<h2>{_e(translate_text('{time} · indice {score}', time=summary.format_time(moment.time_s), score=f'{moment.score:.2f}'))}</h2>")
        parts.append("<div class='moment'>" + "".join(
            f"<figure><img alt='' src='{png_data_uri(image)}'><figcaption>{_e(title)}</figcaption></figure>"
            for title, image in ((translate_text("Sans FEL (P8.1)"), moment.mapped),
                                 (translate_text("Avec FEL"), moment.reconstructed),
                                 (translate_text("Écart (16 codes = jaune)"), moment.difference))) + "</div>")
    return "".join(parts)


def _comparison_html(comparison: Comparison, report: InspectionReport, other: InspectionReport) -> str:
    fps = report.snapshot.fps or 24000 / 1001
    alignment = {"dovi": translate_text("plans du RPU"), "hdr10plus": translate_text("plans HDR10+"),
                 "measure": translate_text("plans mesurés"), "curve": translate_text("courbes de luminance"),
                 "manual": translate_text("décalage saisi"), "none": translate_text("aucun repère commun")}
    parts = [f"<p>{_e(translate_text('B : {name}', name=Path(other.snapshot.path).name))}<br>"
             + _e(translate_text("Décalage : {frames} images ({seconds} s), d'après : {basis}.",
                                 frames=f"{comparison.offset:+d}", seconds=f"{comparison.offset / fps:+.3f}",
                                 basis=alignment.get(comparison.alignment, comparison.alignment))) + "</p>"]
    parts.append(_table([translate_text("Champ"), "A", "B"], [[f.label, f.a, f.b] for f in comparison.fields],
                        [not f.same for f in comparison.fields]))
    names = {"peak": translate_text("Pic"), "average": translate_text("Moyenne")}
    rows = [[names.get(c.kind, c.kind),
             f"{translate_text(SOURCE_LABELS[c.source_a])} / {translate_text(SOURCE_LABELS[c.source_b])}",
             f"{(c.median_ratio - 1) * 100:+.1f} %", f"{c.median_delta:.1f} / {c.p99_delta:.1f}", f"{c.correlation:.3f}",
             ", ".join(summary.format_time(t) for t in c.worst) or "—"] for c in comparison.curves]
    if rows:
        parts.append(f"<h2>{_e(translate_text('Courbes'))}</h2>")
        parts.append(_table([translate_text("Courbe"), translate_text("Sources"), translate_text("B face à A (médiane)"),
                             translate_text("Écart PQ médian / 99 % (codes)"), translate_text("Corrélation"),
                             translate_text("Écarts les plus forts")], rows))
    if comparison.peak_a is not None and comparison.peak_b is not None:
        a = comparison.peak_a.values
        b = np.full(a.size, np.nan)
        start, end = max(0, comparison.offset), min(a.size, comparison.peak_b.values.size + comparison.offset)
        if end > start:
            b[start:end] = comparison.peak_b.values[start - comparison.offset:end - comparison.offset]
        columns = min(COLUMNS, a.size)
        parts.append(svg_chart([("A", _SERIES_COLORS["a"], _column_values(a, columns, "max"), "max"),
                                ("B", _SERIES_COLORS["b"], _column_values(b, columns, "max"), "max")],
                               frames=a.size, fps=fps))
    return "".join(parts)


def render_html(report: InspectionReport, *, measurement: ImageMeasurement | None = None,
                fel: FelReport | None = None, comparison: Comparison | None = None,
                other: InspectionReport | None = None) -> str:
    """Page HTML autonome (aucune ressource externe), thème clair ou sombre selon le système."""
    snapshot = report.snapshot
    fps = snapshot.fps or 24000 / 1001
    name = Path(snapshot.path).name
    language = (current_language() or "eng")[:3]
    lang = {"fra": "fr", "eng": "en", "deu": "de", "spa": "es", "ita": "it"}.get(language, "en")
    body = [f"<h1>{_e(translate_text('Rapport HDR'))} · {_e(name)}</h1>",
            f"<p class='sub'>{_e(snapshot.path)} · #{snapshot.stream_index} · "
            f"{_e(datetime.now().strftime('%Y-%m-%d %H:%M'))}</p>",
            "<div class='badges'>" + "".join(f"<span>{_e(f)}</span>" for f in snapshot.formats) + "</div>"]
    body.append(_section(translate_text("Contrôles"), _findings_html(report)))
    body.append(_section(translate_text("Piste"), _kv(summary.track_rows(snapshot))))
    body.append(_section(translate_text("HDR10 statique"), _kv(summary.static_rows(snapshot, measurement))))
    if snapshot.dovi is not None:
        body.append(_section(translate_text("Dolby Vision (conteneur)"), _kv(summary.container_dovi_rows(snapshot.dovi))))
    curves = series(report, measurement)
    frames = max((values.size for *_, values in curves), default=0)
    if curves:
        columns = min(COLUMNS, frames)
        body.append(_section(translate_text("Luminance"), svg_chart(
            [(label, _SERIES_COLORS.get(key, "#888"), _column_values(values, columns, mode), mode)
             for key, label, mode, values in curves], frames=frames, fps=fps)))
    if measurement is not None:
        body.append(_section(translate_text("Mesure de l'image"), _kv(summary.measurement_rows(measurement, fps))))
    if report.dovi is not None:
        dovi_body = _kv(summary.dovi_rows(report.dovi, fps))
        headers, rows = summary.l6_table(report.dovi)
        if rows:
            dovi_body += f"<h2>{_e(translate_text('L6 (HDR10 du RPU)'))}</h2>" + _table(headers, rows)
        headers, rows = summary.active_area_table(report)
        if rows:
            dovi_body += f"<h2>{_e(translate_text('Zones actives L5'))}</h2>" + _table(headers, rows)
        body.append(_section(translate_text("Dolby Vision (RPU)"), dovi_body))
    if report.hdr10plus is not None:
        body.append(_section(translate_text("HDR10+"), _kv(summary.hdr10plus_rows(report.hdr10plus))))
    if fel is not None:
        body.append(_section(translate_text("FEL Analyzer"), _fel_html(fel)))
    if comparison is not None and other is not None:
        body.append(_section(translate_text("Comparaison"), _comparison_html(comparison, report, other)))
    body.append(f"<footer>{_e(APP_NAME)} {_e(APP_VERSION)}</footer>")
    return (f"<!doctype html><html lang='{lang}'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_e(translate_text('Rapport HDR'))} · {_e(name)}</title><style>{_CSS}</style></head>"
            f"<body><main>{''.join(body)}</main></body></html>")


def write_report(path: Path, report: InspectionReport, *, measurement: ImageMeasurement | None = None,
                 fel: FelReport | None = None, comparison: Comparison | None = None,
                 other: InspectionReport | None = None) -> Path:
    """Écrit le rapport : HTML si l'extension est ``.html``/``.htm``, JSON sinon (écriture atomique)."""
    path = Path(path)
    if path.suffix.lower() in (".html", ".htm"):
        content = render_html(report, measurement=measurement, fel=fel, comparison=comparison, other=other)
    else:
        content = json.dumps(report_data(report, measurement=measurement, fel=fel, comparison=comparison, other=other),
                             ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    atomic_write_text(path, content, mode=0o644)  # document à partager
    return path
