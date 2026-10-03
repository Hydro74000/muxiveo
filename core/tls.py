"""
core/tls.py — Ouverture HTTPS vérifiée, mode non vérifié uniquement sur demande explicite.

La vérification TLS n'est jamais désactivée automatiquement après un échec :
un attaquant réseau pourrait provoquer ce repli en présentant un faux
certificat. Le contexte par défaut respecte ``SSL_CERT_FILE`` (positionné sur
certifi par ``launcher.py`` en mode frozen) et le magasin système, donc les
autorités d'entreprise installées par l'utilisateur restent reconnues.
"""

from __future__ import annotations

import os
import ssl
import urllib.request


class TlsVerificationError(OSError):
    """Certificat TLS refusé alors que la vérification est active."""


def insecure_tls_opt_in(env_name: str) -> bool:
    """Vrai si la variable d'environnement ``env_name`` autorise le TLS non vérifié."""
    return os.environ.get(env_name, "").strip().lower() in {"1", "true", "yes", "on"}


def unverified_ssl_context() -> ssl.SSLContext:
    """Contexte TLS sans vérification (opt-in explicite uniquement)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def is_ssl_error(exc: BaseException) -> bool:
    """Vrai si ``exc`` (ou une cause chaînée) est une erreur TLS."""
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ssl.SSLError):
            return True
        if isinstance(getattr(cur, "reason", None), ssl.SSLError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def urlopen_tls(req: urllib.request.Request, timeout: float, *, insecure_env: str):
    """``urlopen`` vérifié ; non vérifié seulement si ``insecure_env`` est positionnée.

    Un certificat refusé lève :class:`TlsVerificationError` (aucune nouvelle
    tentative sans vérification).
    """
    if insecure_tls_opt_in(insecure_env):
        return urllib.request.urlopen(req, timeout=timeout, context=unverified_ssl_context())  # nosec B310
    try:
        return urllib.request.urlopen(req, timeout=timeout)  # nosec B310  # URL HTTPS fournie par l'appelant
    except OSError as exc:
        if is_ssl_error(exc):
            raise TlsVerificationError(
                f"certificat TLS refusé pour {req.host} (proxy d'inspection ou réseau compromis) ; "
                f"forçable à vos risques via {insecure_env}=1"
            ) from exc
        raise


__all__ = [
    "TlsVerificationError",
    "insecure_tls_opt_in",
    "is_ssl_error",
    "unverified_ssl_context",
    "urlopen_tls",
]
