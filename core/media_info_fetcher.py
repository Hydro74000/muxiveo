"""
core/media_info_fetcher.py — Récupération de métadonnées film/série via l'API TMDB v3.

Aucune dépendance externe (urllib uniquement).
Authentification TMDB via :
    - clé API v3
    - ou token Bearer v4
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path

from core.logging import get_logger
from core.version import APP_ENV_PREFIX, APP_LOGGER_ROOT, APP_USER_AGENT


# =============================================================================
# Nettoyage du nom de fichier
# =============================================================================

# Mots-clés de release (codec, source, HDR, audio, groupes courants)
_RELEASE_TOKENS = re.compile(
    r"\b("
    r"2160p|1080[pi]|720p|480p|576p|4[kK]"
    r"|blu[.\-]?ray|bdrip|bdremux|remux|bdrip"
    r"|web[.\-]?dl|webrip|hdtv|dvdrip|dvdscr|pdvd|ts\b|cam\b"
    r"|x26[45]|hevc|avc|h[.\-]?26[45]|av1|vp9|mpeg2|mpeg4"
    r"|dts[.\-]?hd|truehd|atmos|ddp|eac3|ac3|aac|dts|flac|mp3|opus"
    r"|hdr10\+?|dolby[.\-]?vision|dovi|dv\b|sdr|hlg|pq\b"
    r"|proper|repack|extended|theatrical|directors?[.\-]?cut|unrated|hybrid"
    r"|multi|vff|vfq|vfi|vf2|vo|vostfr|french|english|german|spanish"
    r"|10bit|8bit|hi10p|hi444pp"
    r")\b",
    re.IGNORECASE,
)

# Tout ce qui suit un trait d'union précédé d'un espace (tag de groupe)
_GROUP_TAG_RE = re.compile(r"\s+-\s*\S+\s*$")

# Année à 4 chiffres (1900–2099)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# Indicateur de saison/épisode : S01E01, S01, 01x01…
_SEASON_EP_RE = re.compile(
    r"(?<!\d)(?:S\d{1,2}(?:[\s._-]*E\d{1,4})?|\d{1,2}\s*[xX]\s*\d{1,4})(?!\d)",
    re.IGNORECASE,
)

_BRACKETS_RE = re.compile(r"[\[\](){}]")


def clean_text_for_search(text: str) -> str:
    """
    Nettoie un texte libre pour une recherche TMDB.

    Utile quand la recherche est basée sur un Title déjà enrichi :
        - "Film (2024)"                 -> "Film"
        - "Série - S01E02 - Episode"    -> "Série"
        - "Serie.01x02.1080p.WEB-DL"    -> "Serie"
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    title = re.sub(r"[._\-]", " ", raw)

    cut_positions: list[int] = []

    year_match = _YEAR_RE.search(title)
    if year_match:
        cut_positions.append(year_match.start())

    season_match = _SEASON_EP_RE.search(title)
    if season_match:
        cut_positions.append(season_match.start())

    if cut_positions:
        title = title[: min(cut_positions)]

    title = _RELEASE_TOKENS.sub(" ", title)
    title = _GROUP_TAG_RE.sub("", title)
    title = _BRACKETS_RE.sub(" ", title)
    title = title.strip(" -_:.")

    return " ".join(title.split())


def extract_year_from_text(text: str) -> str:
    """
    Extrait l'année (4 chiffres, 1900–2099) depuis un texte libre.
    Retourne '' si aucune année n'est détectée.
    """
    normalized = re.sub(r"[._\-]", " ", (text or "").strip())
    m = _YEAR_RE.search(normalized)
    return m.group() if m else ""


def normalize_tmdb_search_query(text: str) -> tuple[str, str]:
    """
    Retourne (query, year) nettoyés pour une recherche TMDB.

    Si le nettoyage vide complètement la requête, on retombe sur le texte
    d'origine afin d'éviter une recherche vide ou trop agressive.
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""

    query = clean_text_for_search(raw)
    year = extract_year_from_text(raw)
    if not query:
        return raw, ""
    return query, year


def clean_filename_for_search(path: Path) -> str:
    """
    Extrait un titre propre depuis un nom de fichier pour une recherche TMDB.

    Étapes :
        1. Retrait de l'extension.
        2. Remplacement des séparateurs (., _) par des espaces.
        3. Détection de l'année → tronque le titre avant l'année.
        4. Suppression des mots-clés de release (codec, source, HDR…).
        5. Suppression du tag de groupe résiduel en fin de chaîne.
        6. Normalisation des espaces.
    """
    return clean_text_for_search(path.stem)


def extract_year_from_filename(path: Path) -> str:
    """
    Extrait l'année (4 chiffres, 1900–2099) depuis un nom de fichier.
    Retourne '' si aucune année n'est détectée.
    """
    return extract_year_from_text(path.stem)


# =============================================================================
# Dataclasses
# =============================================================================

@dataclass
class MediaSearchResult:
    """Résultat d'une recherche TMDB (film ou série)."""

    tmdb_id:  int
    title:    str
    year:     str   # "2010" ou ""
    kind:     str   # "movie" | "tv"
    overview: str = ""


@dataclass
class MediaDetails:
    """
    Métadonnées enrichies d'un film ou d'une série.
    Prêtes à être converties en balises MKV via to_mkv_tags().
    """

    imdb_id:    str = ""   # ex. "tt1375666"
    title:      str = ""
    year:       str = ""
    genre:      str = ""
    director:   str = ""
    cast:       str = ""
    synopsis:   str = ""
    country:    str = ""
    url:        str = ""
    collection: str = ""   # nom de la franchise/collection (films uniquement)
    season:     str = ""   # numéro de saison (séries uniquement)
    episode:    str = ""   # numéro d'épisode (séries uniquement)
    episode_title: str = ""  # titre de l'épisode (séries uniquement)
    cover_url: str = ""      # URL TMDB de la cover (téléchargement différé au lancement du workflow)
    cover_bytes: bytes = b""  # réservé — n'est plus rempli par get_details() (téléchargement différé)
    cover_mimetype: str = ""
    cover_filename: str = ""

    def _season_episode_code(self) -> str:
        """
        Retourne SxxExx si saison/épisode sont numériques et > 0, sinon ''.
        """
        try:
            s = int(self.season.strip()) if self.season else 0
            e = int(self.episode.strip()) if self.episode else 0
        except (TypeError, ValueError):
            return ""
        if s <= 0 or e <= 0:
            return ""
        return f"S{s:02d}E{e:02d}"

    def formatted_container_title(self) -> str:
        """
        Titre conseillé pour la balise Title du conteneur.

        - Film  : "Titre (Année)"
        - Série : "Titre - SxxExx - Titre épisode" (si titre épisode disponible)
        """
        title = (self.title or "").strip()
        if not title:
            return ""

        code = self._season_episode_code()
        if code:
            ep_title = (self.episode_title or "").strip()
            return f"{title} - {code} - {ep_title}" if ep_title else f"{title} - {code}"

        year = (self.year or "").strip()
        return f"{title} ({year})" if year else title

    def formatted_subtitle_tag(self) -> str:
        """
        Valeur du tag MKV SUBTITLE.

        - Film  : "Titre"
        - Série : "Titre - SxxExx - Titre épisode" (si titre épisode disponible)
        """
        title = (self.title or "").strip()
        if not title:
            return ""

        code = self._season_episode_code()
        if not code:
            return title

        ep_title = (self.episode_title or "").strip()
        return f"{title} - {code} - {ep_title}" if ep_title else f"{title} - {code}"

    def to_mkv_tags(self) -> dict[str, str]:
        """
        Retourne le dict {NOM_BALISE: valeur} à écrire dans les tags MKV.

        - DESCRIPTION reçoit toujours l'identifiant IMDb quand il est disponible.
        - Les clés à valeur vide sont exclues.
        """
        mapping: dict[str, str] = {
            "DATE_RELEASED": self.year,
            "GENRE":         self.genre,
            "DIRECTOR":      self.director,
            "CAST":          self.cast,
            "SUBTITLE":      self.formatted_subtitle_tag(),
            "SYNOPSIS":      self.synopsis,
            "COUNTRY":       self.country,
            "URL":           self.url,
            "DESCRIPTION":   self.imdb_id,
            "COLLECTION":    self.collection,
            "SEASON":        self.season,
            "EPISODE":       self.episode,
        }
        return {k: v for k, v in mapping.items() if v}


# =============================================================================
# Erreur
# =============================================================================

class TmdbError(RuntimeError):
    """Erreur levée par TmdbFetcher (réseau, auth, parsing…)."""


# =============================================================================
# Correspondances langue ISO 639-2 → locale TMDB (BCP-47)
# =============================================================================

_LANG_TO_TMDB: dict[str, str] = {
    "fra": "fr-FR",
    "fre": "fr-FR",
    "deu": "de-DE",
    "ger": "de-DE",
    "spa": "es-ES",
    "ita": "it-IT",
    "por": "pt-BR",
    "jpn": "ja-JP",
    "kor": "ko-KR",
    "zho": "zh-CN",
    "chi": "zh-CN",
    "rus": "ru-RU",
    "nld": "nl-NL",
    "pol": "pl-PL",
    "swe": "sv-SE",
    "dan": "da-DK",
    "nor": "nb-NO",
    "fin": "fi-FI",
}


def iso639_2_to_tmdb_lang(code: str) -> str:
    """
    Convertit un code ISO 639-2 en locale TMDB (BCP-47).
    Retourne 'en-US' si le code n'est pas reconnu.
    """
    return _LANG_TO_TMDB.get((code or "").lower(), "en-US")


# =============================================================================
# TmdbFetcher
# =============================================================================

_BASE    = "https://api.themoviedb.org/3"
_URL_MV  = "https://www.themoviedb.org/movie/{id}"
_URL_TV  = "https://www.themoviedb.org/tv/{id}"
_URL_IMD = "https://www.imdb.com/title/{imdb_id}/"
_TMDB_DEBUG_ENV = f"{APP_ENV_PREFIX}_TMDB_DEBUG"
_TMDB_LOGGER = get_logger(f"{APP_LOGGER_ROOT}.tmdb")
_TMDB_BEARER_TOKEN_ENV = f"{APP_ENV_PREFIX}_TMDB_BEARER_TOKEN"
_TMDB_INSECURE_SSL_ENV = f"{APP_ENV_PREFIX}_TMDB_INSECURE_SSL"


def _tmdb_insecure_ssl_enabled() -> bool:
    raw = os.environ.get(_TMDB_INSECURE_SSL_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _unverified_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _is_ssl_error(exc: BaseException) -> bool:
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ssl.SSLError):
            return True
        reason = getattr(cur, "reason", None)
        if isinstance(reason, ssl.SSLError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


_TMDB_DEFAULT_BEARER_TOKEN = (
    "eyJhbGciOiJIUzI1NiJ9."
    "eyJhdWQiOiI3MWYxZWFlYTU3MmVlNmNhNTg0OTRmNzMxMjg5ODhhZiIs"
    "Im5iZiI6MTc3NTU3OTA1NS4yOTc5OTk5LCJzdWIiOiI2OWQ1MmZhZjMyZGYxMmRkOTZmOGE2NDYiLCJzY29wZXMiOlsiYXBpX3JlYWQiXSwidmVyc2lvbiI6MX0."
    "-Uv04JF4wgu7NPJKWXnPY6OuXn4sWxcurBzeszGfnos"
)


def default_tmdb_bearer_token() -> str:
    """
    Retourne le token Bearer TMDB:
    1) variable d'environnement MUXIVEO_TMDB_BEARER_TOKEN
    2) token par défaut embarqué
    """
    return os.environ.get(_TMDB_BEARER_TOKEN_ENV, "").strip() or _TMDB_DEFAULT_BEARER_TOKEN


class TmdbFetcher:
    """
    Client léger pour l'API TMDB v3 (urllib stdlib, aucune dépendance externe).

    Paramètres :
        api_key      — clé API TMDB v3 (optionnelle si bearer_token est fourni).
        bearer_token — token Bearer TMDB v4 (optionnel si api_key est fourni).
        language — locale BCP-47 utilisée pour les titres et synopsis
                   (ex. 'fr-FR', 'en-US').
    """

    def __init__(
        self,
        api_key: str = "",
        *,
        language: str = "en-US",
        bearer_token: str = "",
    ) -> None:
        self._key = api_key.strip()
        self._bearer_token = bearer_token.strip()
        if not self._key and not self._bearer_token:
            raise TmdbError("Authentification TMDB manquante (clé API ou token Bearer).")
        self._lang = language or "en-US"
        self._image_config_cache: dict | None = None

    def _debug_enabled(self) -> bool:
        raw = os.environ.get(_TMDB_DEBUG_ENV, "")
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def _debug_log(self, message: str, **fields: object) -> None:
        if not self._debug_enabled():
            return
        parts = [message]
        for key, value in fields.items():
            parts.append(f"{key}={value!r}")
        line = " | ".join(parts)
        if _TMDB_LOGGER.hasHandlers():
            _TMDB_LOGGER.debug(line)
            return
        print(f"[TMDB DEBUG] {line}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Requête HTTP interne
    # ------------------------------------------------------------------

    def _urlopen_with_ssl_fallback(self, req: urllib.request.Request, timeout: int):
        """
        urlopen avec fallback automatique en SSL non-vérifié si la vérification
        échoue (pratique quand le bundle CA du frozen ne matche pas l'hôte).
        Forçable via MUXIVEO_TMDB_INSECURE_SSL=1.
        """
        if _tmdb_insecure_ssl_enabled():
            return urllib.request.urlopen(req, timeout=timeout, context=_unverified_ssl_context())
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.URLError as exc:
            if not _is_ssl_error(exc):
                raise
            self._debug_log("SSL verification failed; retrying insecure", url=req.full_url)
            return urllib.request.urlopen(req, timeout=timeout, context=_unverified_ssl_context())

    def _get(self, endpoint: str, extra: dict | None = None) -> dict:
        params: dict[str, str] = {"language": self._lang}
        if self._key:
            params["api_key"] = self._key
        if extra:
            params.update(extra)
        query = urllib.parse.urlencode(params)
        url = f"{_BASE}{endpoint}?{query}" if query else f"{_BASE}{endpoint}"
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": APP_USER_AGENT,
        }
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with self._urlopen_with_ssl_fallback(req, timeout=12) as resp:
                body = resp.read()
                content_encoding = str(resp.headers.get("Content-Encoding", "")).strip().lower()
                if content_encoding:
                    body = self._decode_http_body(body, content_encoding)
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                if self._key and not self._bearer_token:
                    raise TmdbError("Clé API TMDB invalide ou expirée (401).") from exc
                if self._bearer_token and not self._key:
                    raise TmdbError("Token Bearer TMDB invalide ou expiré (401).") from exc
                raise TmdbError("Authentification TMDB invalide ou expirée (401).") from exc
            if exc.code == 404:
                raise TmdbError(f"Ressource introuvable sur TMDB (404) : {endpoint}") from exc
            raise TmdbError(f"Erreur HTTP {exc.code} lors de la requête TMDB.") from exc
        except urllib.error.URLError as exc:
            raise TmdbError(f"Impossible de contacter TMDB : {exc.reason}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, OSError, zlib.error) as exc:
            raise TmdbError(f"Réponse TMDB invalide : {exc}") from exc

    def _decode_http_body(self, body: bytes, content_encoding: str) -> bytes:
        """
        Décompresse un payload HTTP selon Content-Encoding.

        TMDB renvoie désormais fréquemment du JSON compressé en gzip.
        urllib ne le décompresse pas automatiquement, y compris dans
        certains environnements packagés (AppImage).
        """
        decoded = body
        encodings = [token.strip().lower() for token in content_encoding.split(",") if token.strip()]
        for encoding in reversed(encodings):
            if encoding == "identity":
                continue
            if encoding == "gzip":
                # Certains environnements/proxys peuvent déjà fournir le corps
                # décompressé tout en conservant l'en-tête Content-Encoding.
                if decoded[:2] == b"\x1f\x8b":
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    decoded = decoder.decompress(decoded) + decoder.flush()
                continue
            if encoding == "deflate":
                try:
                    decoded = zlib.decompress(decoded)
                except zlib.error:
                    try:
                        decoded = zlib.decompress(decoded, -zlib.MAX_WBITS)
                    except zlib.error:
                        if decoded[:1] not in {b"{", b"[", b" ", b"\n", b"\r", b"\t"}:
                            raise
                continue
            raise TmdbError(f"Encodage HTTP TMDB non supporté : {encoding}")
        return decoded

    def _get_binary(self, url: str) -> tuple[bytes, str]:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "image/*,*/*;q=0.8",
                "User-Agent": APP_USER_AGENT,
            },
        )
        try:
            with self._urlopen_with_ssl_fallback(req, timeout=20) as resp:
                content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].strip()
                return resp.read(), content_type
        except urllib.error.HTTPError as exc:
            raise TmdbError(f"Erreur HTTP {exc.code} lors du téléchargement TMDB.") from exc
        except urllib.error.URLError as exc:
            raise TmdbError(f"Impossible de télécharger l'image TMDB : {exc.reason}") from exc

    def _image_configuration(self) -> dict:
        if self._image_config_cache is None:
            self._image_config_cache = self._get("/configuration")
        return self._image_config_cache

    def _build_image_url(self, file_path: str, *, image_kind: str = "poster") -> str:
        clean_path = (file_path or "").strip()
        if not clean_path:
            return ""

        secure_base_url = "https://image.tmdb.org/t/p/"
        sizes: list[str] = ["original"]
        try:
            config = self._image_configuration()
            images = config.get("images", {}) if isinstance(config, dict) else {}
            secure_base_url = (
                str(images.get("secure_base_url") or images.get("base_url") or secure_base_url).strip()
            )
            size_key = "backdrop_sizes" if image_kind == "backdrop" else "poster_sizes"
            raw_sizes = images.get(size_key, [])
            if isinstance(raw_sizes, list) and raw_sizes:
                sizes = [str(size).strip() for size in raw_sizes if str(size).strip()]
        except TmdbError:
            pass

        preferred_order = ("w780", "w500", "original")
        chosen = next((size for size in preferred_order if size in sizes), sizes[-1] if sizes else "original")
        return f"{secure_base_url}{chosen}{clean_path}"

    def _cover_filename(self, file_path: str, mimetype: str = "") -> str:
        suffix = Path(file_path).suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            return "cover.jpg"
        if suffix:
            return f"cover{suffix}"
        guessed = mimetypes.guess_extension(mimetype or "") or ".jpg"
        if guessed == ".jpe":
            guessed = ".jpg"
        return f"cover{guessed}"

    def _language_parts(self, language: str | None = None) -> tuple[str, str]:
        """
        Découpe une locale TMDB en (langue, région), ex. "fr-FR" -> ("fr", "FR").
        """
        raw = (language or self._lang or "").strip()
        if not raw:
            return "", ""
        parts = raw.split("-", 1)
        lang = parts[0].lower()
        region = parts[1].upper() if len(parts) > 1 else ""
        return lang, region

    def _pick_translation_data(self, payload: dict, *, language: str | None = None) -> tuple[str, str]:
        """
        Extrait (name, overview) depuis une réponse `/translations`.

        Stratégie :
        1. correspondance exacte langue+région (ex. fr-FR)
        2. première correspondance sur la langue seule (ex. fr-*)
        """
        lang, region = self._language_parts(language)
        if not lang:
            return "", ""

        fallback_name = ""
        fallback_overview = ""
        for item in payload.get("translations", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("iso_639_1", "")).lower() != lang:
                continue
            data = item.get("data")
            if not isinstance(data, dict):
                continue
            name = str(data.get("name") or "").strip()
            overview = str(data.get("overview") or "").strip()
            item_region = str(item.get("iso_3166_1", "")).upper()
            if region and item_region == region:
                return name, overview
            if not fallback_name and name:
                fallback_name = name
            if not fallback_overview and overview:
                fallback_overview = overview
        return fallback_name, fallback_overview

    # ------------------------------------------------------------------
    # Recherche
    # ------------------------------------------------------------------

    def _search_raw(self, query: str, kind: str, year: str = "") -> list[MediaSearchResult]:
        """
        Appel API brut sans fallback.

        Paramètres TMDB pour le filtrage par année :
            movie → primary_release_year
            tv    → first_air_date_year
            all   → /search/multi ne supporte pas le filtre année (ignoré ici)
        """
        endpoint = "/search/multi" if kind == "all" else f"/search/{kind}"
        extra: dict[str, str] = {"query": query, "include_adult": "false"}
        if year:
            if kind == "movie":
                extra["primary_release_year"] = year
            elif kind == "tv":
                extra["first_air_date_year"] = year
        data = self._get(endpoint, extra)

        out: list[MediaSearchResult] = []
        for item in data.get("results", []):
            media_type = item.get("media_type", kind if kind != "all" else "")
            if media_type not in ("movie", "tv"):
                continue
            title    = item.get("title") or item.get("name") or ""
            raw_date = item.get("release_date") or item.get("first_air_date") or ""
            out.append(MediaSearchResult(
                tmdb_id  = int(item["id"]),
                title    = title,
                year     = raw_date[:4],
                kind     = media_type,
                overview = item.get("overview", ""),
            ))
        return out[:25]

    def search(self, query: str, *, kind: str = "all", year: str = "") -> list[MediaSearchResult]:
        """
        Recherche films et/ou séries sur TMDB.

        kind : "movie" | "tv" | "all"
        year : année optionnelle (ex. "2026") pour affiner les résultats.
               Si fournie et aucun résultat trouvé, relance automatiquement
               sans filtre d'année (fallback).

        /search/multi ne supporte pas le filtre année : quand kind="all" et
        year est fourni, deux requêtes séparées (movie + tv) sont effectuées
        pour bénéficier du filtre côté API.

        Retourne au maximum 25 résultats.
        """
        if not query.strip():
            return []

        if kind == "all" and year:
            # /search/multi n'accepte pas le paramètre année → split movie + tv
            movie_res = self._search_raw(query, "movie", year)
            tv_res    = self._search_raw(query, "tv",    year)
            results   = (movie_res + tv_res)[:25]
        else:
            results = self._search_raw(query, kind, year)

        # Fallback : si aucun résultat avec l'année, réessayer sans
        if not results and year:
            results = self._search_raw(query, "all" if kind == "all" else kind, "")

        return results

    # ------------------------------------------------------------------
    # Détails complets
    # ------------------------------------------------------------------

    def get_details(
        self,
        result: MediaSearchResult,
        *,
        season:  str = "",
        episode: str = "",
    ) -> MediaDetails:
        """
        Récupère les détails complets d'un film ou d'une série.

        append_to_response=credits,external_ids regroupe trois appels en un seul.
        Les champs season/episode sont passés tels quels dans les balises MKV.
        """
        data = self._get(
            f"/{result.kind}/{result.tmdb_id}",
            {"append_to_response": "credits,external_ids"},
        )

        # Titre & année
        title    = data.get("title") or data.get("name") or result.title
        raw_date = data.get("release_date") or data.get("first_air_date") or ""
        year     = raw_date[:4] if raw_date else result.year

        season_no = 0
        episode_no = 0
        if result.kind == "tv":
            try:
                season_no = int(season.strip()) if season.strip() else 0
                episode_no = int(episode.strip()) if episode.strip() else 0
            except ValueError:
                season_no = 0
                episode_no = 0

            if season_no > 0:
                season_endpoint = f"/tv/{result.tmdb_id}/season/{season_no}"
                try:
                    season_data = self._get(season_endpoint)
                    season_air_date = str(season_data.get("air_date") or "").strip()
                    if season_air_date:
                        year = season_air_date[:4]
                except TmdbError:
                    self._debug_log(
                        "TV season metadata fetch failed; keeping series release year",
                        tmdb_id=result.tmdb_id,
                        language=self._lang,
                        season=season_no,
                        endpoint=season_endpoint,
                    )

        # Genres
        genres = [g["name"] for g in data.get("genres", [])]

        # Réalisateur (film) / Créateur (série)
        credits = data.get("credits", {})
        if result.kind == "movie":
            directors = [
                p["name"] for p in credits.get("crew", [])
                if p.get("job") == "Director"
            ]
        else:
            directors = [p.get("name", "") for p in data.get("created_by", [])]

        # Casting (10 premiers noms)
        cast = [p["name"] for p in credits.get("cast", [])[:10]]

        # Pays de production
        countries = [c["name"] for c in data.get("production_countries", [])]
        if not countries:
            # Séries : origin_country est une liste de codes ISO
            countries = data.get("origin_country", [])

        # Synopsis global (série/film)
        synopsis = data.get("overview", "")

        episode_title = ""

        # Pour les séries, si saison/épisode sont fournis et valides,
        # on tente de récupérer le synopsis de l'épisode en priorité.
        # Si indisponible (404, overview vide, etc.), on conserve le synopsis global.
        if result.kind == "tv":
            if season_no > 0 and episode_no > 0:
                ep_endpoint = f"/tv/{result.tmdb_id}/season/{season_no}/episode/{episode_no}"
                overview_source = "series"
                episode_title_source = "series"
                attempts: list[str] = []
                try:
                    ep_data = self._get(ep_endpoint)
                    attempts.append(f"episode_detail[{self._lang}]")
                    ep_overview = (ep_data.get("overview") or "").strip()
                    episode_title = (ep_data.get("name") or "").strip()
                    if ep_overview:
                        overview_source = f"episode_detail:{self._lang}"
                    if episode_title:
                        episode_title_source = f"episode_detail:{self._lang}"

                    # L'endpoint détail d'épisode peut parfois être moins
                    # complet que les traductions explicites disponibles.
                    # On consulte donc `/translations` pour la locale active
                    # avant de tomber sur le synopsis global de la série.
                    if not ep_overview or not episode_title:
                        try:
                            tr_data = self._get(f"{ep_endpoint}/translations")
                            attempts.append(f"episode_translations[{self._lang}]")
                        except TmdbError:
                            tr_data = {}
                        tr_title, tr_overview = self._pick_translation_data(tr_data)
                        if not ep_overview and tr_overview:
                            ep_overview = tr_overview
                            overview_source = f"episode_translations:{self._lang}"
                        if not episode_title and tr_title:
                            episode_title = tr_title
                            episode_title_source = f"episode_translations:{self._lang}"

                    # Certaines locales TMDB ne renseignent pas encore les
                    # fiches d'épisodes. On retente en anglais pour récupérer
                    # un synopsis/titre plus précis plutôt que de retomber
                    # directement sur le synopsis global de la série.
                    if self._lang.lower() != "en-us" and (not ep_overview or not episode_title):
                        try:
                            fallback_ep_data = self._get(ep_endpoint, {"language": "en-US"})
                            attempts.append("episode_detail[en-US]")
                        except TmdbError:
                            fallback_ep_data = {}
                        if not ep_overview:
                            fallback_overview = (fallback_ep_data.get("overview") or "").strip()
                            if fallback_overview:
                                ep_overview = fallback_overview
                                overview_source = "episode_detail:en-US"
                        if not episode_title:
                            fallback_title = (fallback_ep_data.get("name") or "").strip()
                            if fallback_title:
                                episode_title = fallback_title
                                episode_title_source = "episode_detail:en-US"
                        if not ep_overview or not episode_title:
                            try:
                                fallback_tr_data = self._get(
                                    f"{ep_endpoint}/translations",
                                    {"language": "en-US"},
                                )
                                attempts.append("episode_translations[en-US]")
                            except TmdbError:
                                fallback_tr_data = {}
                            tr_title, tr_overview = self._pick_translation_data(
                                fallback_tr_data,
                                language="en-US",
                            )
                            if not ep_overview and tr_overview:
                                ep_overview = tr_overview
                                overview_source = "episode_translations:en-US"
                            if not episode_title and tr_title:
                                episode_title = tr_title
                                episode_title_source = "episode_translations:en-US"

                    if ep_overview:
                        synopsis = ep_overview
                    self._debug_log(
                        "TV episode metadata resolved",
                        tmdb_id=result.tmdb_id,
                        language=self._lang,
                        season=season_no,
                        episode=episode_no,
                        overview_source=overview_source,
                        episode_title_source=episode_title_source,
                        synopsis_found=bool(ep_overview),
                        episode_title_found=bool(episode_title),
                        attempts=" -> ".join(attempts),
                    )
                except TmdbError:
                    # Fallback silencieux vers le synopsis global de la série.
                    self._debug_log(
                        "TV episode metadata fetch failed; keeping series overview",
                        tmdb_id=result.tmdb_id,
                        language=self._lang,
                        season=season_no,
                        episode=episode_no,
                        endpoint=ep_endpoint,
                    )
                    pass

        # Identifiants externes (IMDb)
        ext     = data.get("external_ids", {})
        imdb_id = ext.get("imdb_id") or ""

        # URL : IMDb si disponible, sinon TMDB
        if imdb_id:
            url = _URL_IMD.format(imdb_id=imdb_id)
        elif result.kind == "movie":
            url = _URL_MV.format(id=result.tmdb_id)
        else:
            url = _URL_TV.format(id=result.tmdb_id)

        # Collection / franchise (films uniquement)
        collection = ""
        col = data.get("belongs_to_collection")
        if isinstance(col, dict):
            collection = col.get("name", "")

        # Téléchargement différé : on construit uniquement l'URL de la cover,
        # le téléchargement réel est effectué au lancement du workflow.
        cover_url = ""
        cover_filename = ""
        poster_path = str(data.get("poster_path") or "").strip()
        backdrop_path = str(data.get("backdrop_path") or "").strip()
        cover_path_str = poster_path or backdrop_path
        cover_kind = "poster" if poster_path else "backdrop"
        if cover_path_str:
            try:
                built_url = self._build_image_url(cover_path_str, image_kind=cover_kind)
                if built_url:
                    cover_url = built_url
                    cover_filename = self._cover_filename(cover_path_str)
            except TmdbError:
                pass

        out_season = season if result.kind == "tv" else ""
        out_episode = episode if result.kind == "tv" else ""

        return MediaDetails(
            imdb_id    = imdb_id,
            title      = title,
            year       = year,
            genre      = ", ".join(genres),
            director   = ", ".join(d for d in directors if d),
            cast       = ", ".join(cast),
            synopsis   = synopsis,
            country    = ", ".join(c for c in countries if c),
            url        = url,
            collection = collection,
            season     = out_season,
            episode    = out_episode,
            episode_title=episode_title,
            cover_url  = cover_url,
            cover_filename=cover_filename,
        )
