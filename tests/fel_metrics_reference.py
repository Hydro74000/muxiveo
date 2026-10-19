"""Référence des mesures du FEL Analyzer : implémentation d'origine, gardée pour vérifier les versions accélérées."""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np

import core.hdr_inspection.fel_analyzer as fa
from core.hdr_inspection.fel_analyzer import Frame


def reference_frame_metrics(reconstructed: Frame, mapped: Frame, rows: slice) -> dict[str, float]:
    """Implémentation d'origine (float64, un seul fil) : référence des mesures parallèles."""
    r = reconstructed.y[rows].astype(np.float64)
    m = mapped.y[rows].astype(np.float64)
    d = r - m
    low = fa._box(d, 8)
    metrics = {
        "tone": float(np.abs(low).mean()),
        "detail": float(np.sqrt(((d - fa._box(d, 2)) ** 2).mean())),
        "mean": float(d.mean()),
    }
    # Hautes lumières : percentile 99,9 % de la luma, en nits (images sans hautes lumières écartées).
    pr, pm = float(fa._nits(np.percentile(r, 99.9))), float(fa._nits(np.percentile(m, 99.9)))
    metrics["highlights"] = 100.0 * (pr - pm) / pm if pm >= fa._HIGHLIGHT_NITS else 0.0
    # Grain : bruit haute fréquence dans les aplats (faible variance locale de la couche mapped).
    mean_m = fa._box(m, 2)
    variance = fa._box(m * m, 2) - mean_m ** 2
    flat = variance < 36.0
    if flat.mean() > 0.05:
        noise_r = float(np.std((r - fa._box(r, 2))[flat]))
        noise_m = float(np.std((m - mean_m)[flat]))
        metrics["grain"] = 100.0 * (noise_r / noise_m - 1.0) if noise_m > 0.05 else 0.0
    else:
        metrics["grain"] = 0.0
    chroma_rows = slice(rows.start // 2, (rows.stop + 1) // 2)
    colour = [float(np.abs(fa._box(a[chroma_rows].astype(np.float64) - b[chroma_rows].astype(np.float64), 4)).mean())
              for a, b in ((reconstructed.cb, mapped.cb), (reconstructed.cr, mapped.cr))]
    metrics["colour"] = sum(colour) / 2
    dark = m < 300  # ≈ 10 nits en PQ 10 bits plage limitée
    metrics["shadows"] = float(np.abs(low[dark]).mean()) if dark.mean() > 0.02 else 0.0
    return metrics


def reference_survival(reconstructed: Sequence[Frame], mapped: Sequence[Frame], encoded: Sequence[Frame], *,
             encoder: str, bitrate_kbps: int, rows: slice) -> fa.Survival:
    """Part de l'apport du FEL conservée après encodage (projection de E − M sur R − M).

    Images alignées rang par rang : en pratique celles de ``survival_indices`` (l'encodage, lui,
    porte sur la rafale entière).
    """
    tone_num = tone_den = detail_num = detail_den = error = 0.0
    count = min(len(reconstructed), len(mapped), len(encoded))
    if count == 0:
        raise ValueError("Aucune image décodée après l'encodage de test.")
    for r_frame, m_frame, e_frame in zip(reconstructed, mapped, encoded):
        r = r_frame.y[rows].astype(np.float64)
        m = m_frame.y[rows].astype(np.float64)
        e = e_frame.y[rows].astype(np.float64)
        contribution, kept = r - m, e - m
        low_c, low_k = fa._box(contribution, 8), fa._box(kept, 8)
        high_c, high_k = contribution - fa._box(contribution, 2), kept - fa._box(kept, 2)
        tone_num += float((low_c * low_k).sum())
        tone_den += float((low_c * low_c).sum())
        detail_num += float((high_c * high_k).sum())
        detail_den += float((high_c * high_c).sum())
        # Bruit d'encodage haute fréquence : (E − R) filtré = high_k − high_c (filtre linéaire).
        error += float(np.sqrt(((high_k - high_c) ** 2).mean()))
    detail = float(np.sqrt(detail_den / (count * reconstructed[0].y[rows].size)))
    return fa.Survival(encoder, bitrate_kbps,
                    tone_kept=max(0.0, min(1.0, tone_num / tone_den)) if tone_den > 0 else 1.0,
                    detail_kept=max(0.0, min(1.0, detail_num / detail_den)) if detail_den > 0 else 1.0,
                    encode_error=error / count, detail=detail)
