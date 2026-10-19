"""Champs compatibles avec le moteur de nommage des fichiers de sortie."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QMenu, QSizePolicy, QVBoxLayout, QWidget

from cli.output_template import render_output_filename
from core.i18n import translate_text
from ui.design_system import colors as C, font_px, scale
from ui.widgets.token_line_edit import TokenChoice, TokenLineEdit, add_token_submenu, populate_token_menu


def output_template_choices(*, hybrid: bool = False) -> list[TokenChoice]:
    """Les critères de profil non préfixés ne sont pas des champs de nommage."""
    choices = [TokenChoice((category,), label, text, description) for category, label, text, description in (
        ("Identité", "Titre normalisé", "{title:release}", "Titre TMDB normalisé en ASCII avec des points."),
        ("Identité", "Titre TMDB", "{title}", "Titre du film ou de la série, avec ses espaces et accents."),
        ("Identité", "Année", "{year}", "Année de sortie fournie par TMDB."),
        ("Identité", "Nom du fichier source", "{source_name}", "Nom du fichier de référence sans son extension."),
        ("Identité", "Groupe de release", "{release_group}", "Groupe de release configuré, sinon extrait du nom source."),
        ("Identité", "Groupe du nom source", "{group}", "Groupe extrait du nom source, remplacé par le groupe configuré dans Hybrid."),
        ("Identité", "Assemblage multi-sources", "{hybrid_tag}", "Insère Hybrid si les pistes finales viennent de plusieurs sources."),
        ("Épisode", "Saison et épisode (S01E02)", "{season_episode}", "Code saison/épisode du plan Hybrid ou de TMDB, par exemple S01E02."),
        ("Épisode", "Titre d'épisode normalisé", "{episode_title:release}", "Titre d'épisode TMDB normalisé en ASCII avec des points."),
        ("Épisode", "Titre d'épisode TMDB", "{episode_title}", "Titre d'épisode TMDB, avec ses espaces et accents."),
        ("Épisode", "Saison (01)", "{season}", "Numéro de saison du plan Hybrid ou de TMDB sur deux chiffres, sans le préfixe S."),
        ("Épisode", "Épisode (02)", "{episode}", "Numéro d'épisode du plan Hybrid ou de TMDB sur deux chiffres, sans le préfixe E."),
        ("Épisode", "Saison numérique (01)", "{season_num:02d}", "Numéro de saison du plan Hybrid ou de TMDB, formatable avec :02d ou :03d."),
        ("Épisode", "Épisode numérique (02)", "{episode_num:02d}", "Numéro d'épisode du plan Hybrid ou de TMDB, formatable avec :02d ou :03d."),
        ("Audio", "Plusieurs langues (MULTi)", "{audio-multi}", "Insère MULTi si plusieurs familles de langues audio sont conservées. Pour MULTI en majuscules, utilisez le texte fixe."),
        ("Audio", "Variante française (VFF, VFQ…)", "{audio-fr-tag}", "Détecte la variante française des pistes audio finales : VFF, VFQ, VF2…"),
        ("Audio", "Audio immersif (Atmos, DTS-X)", "{audio-immersive}", "Insère Atmos ou DTS-X si détecté. Pour ATMOS en majuscules, utilisez le texte fixe."),
        ("Vidéo", "Source détectée (WEB, BluRay…)", "{video-source}", "Source déduite du nom de référence : WEB, BluRay, HDTV… WEB-DL doit être ajouté en texte fixe."),
        ("Vidéo", "Profondeur 10 bits", "{video-10bit}", "Insère 10Bits si détecté, ou implicite avec HDR10+ ou Dolby Vision."),
        ("Vidéo", "Dolby Vision (DV)", "{video-dolby-vision}", "Insère DV si Dolby Vision est présent sur une piste vidéo finale."),
        ("Sous-titres", "Version originale sous-titrée FR", "{sub-vostfr}", "Insère VOSTFR si des sous-titres français sont présents sans audio français."),
        ("Texte fixe", "WEB-DL", "WEB-DL", "Texte toujours ajouté, sans détection de la source."),
        ("Texte fixe", "Hybrid", "Hybrid", "Texte toujours ajouté, même si une seule source fournit les pistes finales."),
        ("Texte fixe", "MULTI en majuscules", "MULTI", "Texte toujours ajouté, sans détection du nombre de langues."),
        ("Texte fixe", "ATMOS en majuscules", "ATMOS", "Texte toujours ajouté, sans détection de l'audio immersif."),
        ("Texte fixe", "Point", ".", "Sépare deux éléments du nom avec un point."),
        ("Texte fixe", "Tiret", "-", "Sépare deux éléments du nom avec un tiret."),
    )]
    if hybrid:
        choices.append(TokenChoice(("Épisode",), "Titre d'épisode du fichier source", "{ref_episode_title}",
                                   "Titre d'épisode extrait du nom de référence, sans requête TMDB."))
    for category, label, field, description in (
        ("Audio", "Codec audio release (DDP…)", "audio-codec-release", "Codec audio normalisé release : DDP pour E-AC-3, TrueHD, DTSHD-MA… best retient le codec le mieux classé."),
        ("Audio", "Codec audio brut", "audio-codec", "Codec audio brut en majuscules, par exemple EAC3. Les alias du profil restent applicables."),
        ("Audio", "Canaux audio (5.1, 7.1…)", "audio-channels", "Canaux des pistes audio finales. best retient le plus grand nombre de canaux."),
        ("Audio", "Langues audio", "audio-lang", "Langues audio finales au format régionalisé, par exemple fr-FR et en-US."),
        ("Audio", "Noms des langues audio", "audio-lang-name", "Noms lisibles des langues audio finales, avec leur variante régionale."),
        ("Vidéo", "Codec vidéo brut (HEVC…)", "video-codec", "Codec vidéo brut en majuscules, par exemple HEVC. Les alias du profil restent applicables."),
        ("Vidéo", "Codec vidéo release (x265…)", "video-codec-release", "Codec vidéo normalisé release : x265 pour HEVC, x264 pour AVC."),
        ("Vidéo", "Résolution (2160p…)", "video-resolution", "Résolution vidéo au format release. best retient la meilleure résolution."),
        ("Vidéo", "HDR (HDR, HDR10P…)", "video-hdr", "Insère HDR10P, HDR ou HLG selon les pistes vidéo finales ; reste vide pour une vidéo SDR."),
        ("Sous-titres", "Langues des sous-titres", "sub-lang", "Langues des sous-titres conservés, par exemple fr-FR et en-US."),
        ("Sous-titres", "Noms des langues des sous-titres", "sub-lang-name", "Noms lisibles des langues des sous-titres conservés."),
        ("Sous-titres", "Codec des sous-titres", "sub-codec", "Codecs des sous-titres conservés, par exemple SUBRIP ou PGS."),
    ):
        for mode, mode_label, hint in (
            ("best", "Automatique (best)", "Valeur la mieux classée pour ce champ, sinon première valeur non vide."),
            ("first", "Première valeur (first)", "Première valeur non vide, selon l'ordre final des pistes."),
            ("all", "Toutes les valeurs (all)", "Valeurs distinctes des pistes finales, séparées par +."),
        ):
            explanation = translate_text(description) + "\n\n" + translate_text(hint)
            choices.append(TokenChoice((category, label), mode_label, f"{{{field}:{mode}}}", explanation))
    return choices


class OutputTemplateLineEdit(TokenLineEdit):
    def __init__(self, *, hybrid: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hybrid = hybrid
        self.setToolTip(translate_text("Clic droit pour insérer un champ de nommage ou du texte fixe."))

    def _populate_context_menu(self, menu: QMenu) -> None:
        fields = add_token_submenu(menu, "Insérer un champ ou du texte")
        populate_token_menu(fields, output_template_choices(hybrid=self._hybrid), self.insert)


class OutputTemplatePreview(QWidget):
    """Rendu immédiat et copiable, sans lecture de média ni requête réseau."""

    def __init__(self, field: QLineEdit, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._field = field
        self._context: dict[str, Any] | None = None
        self._default_name = ""
        self._note = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(scale(3))
        self.caption = QLabel(translate_text("Aperçu du nom final"))
        self.caption.setStyleSheet(f"color:{C.TEXT_SEC};font-size:{font_px(10)}px;")
        self.name = QLabel()
        self.name.setTextFormat(Qt.TextFormat.PlainText)
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.name.setWordWrap(True)
        self.name.setMinimumWidth(0)
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.note = QLabel()
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color:{C.TEXT_SEC};font-size:{font_px(10)}px;")
        for widget in (self.caption, self.name, self.note):
            layout.addWidget(widget)
        field.textChanged.connect(self._refresh)

    def set_context(self, context: dict[str, Any] | None, *, default_name: str = "", episode: str = "", note: str = "") -> None:
        self._context, self._default_name, self._note = context, default_name, note
        self.caption.setText(translate_text("Aperçu du nom final") + (f" · {episode}" if episode else ""))
        self._refresh()

    def _refresh(self, *_args) -> None:
        template = self._field.text().strip()
        try:
            # Une saisie incomplète ne doit pas faire planter la page.
            rendered = render_output_filename(template, self._context or {}) if template else self._default_name
        except (ValueError, KeyError, IndexError, AttributeError) as exc:
            self.name.setText(translate_text("Template invalide : {error}", error=translate_text(str(exc))))
            self.name.setStyleSheet(f"color:{C.WARN};")
            self.name.setToolTip(self.name.text())
            self.note.setText("")
            return
        if self._context is None and "{" in template:
            rendered = ""
        self.name.setStyleSheet(f"color:{C.ACCENT};")
        self.name.setText(rendered or "—")
        self.name.setToolTip(rendered)
        self.note.setText(self._note)
