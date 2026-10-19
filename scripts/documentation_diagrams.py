#!/usr/bin/env python3
"""Build the user guide SVGs in French and English, without external assets."""
from __future__ import annotations

import argparse
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def text(x, y, lines, *, size=18, fill='#d9e2f5', weight=400, anchor='middle'):
    if isinstance(lines, str):
        lines = [lines]
    return ''.join(f'<text x="{x}" y="{y + i * (size + 9)}" text-anchor="{anchor}" '
                   f'font-size="{size}" font-weight="{weight}" fill="{fill}">{escape(line)}</text>'
                   for i, line in enumerate(lines))


def rect(x, y, w, h, color='#19243a', border='#33435e', radius=16):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{color}" stroke="{border}"/>'


def arrow(x1, y1, x2, y2, color='#95a8c9'):
    return (f'<path d="M{x1} {y1} L{x2} {y2}" fill="none" stroke="{color}" '
            'stroke-width="2.5" marker-end="url(#arrow)"/>')


def svg(title, description, content, height):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1100 {height}" role="img" aria-labelledby="title desc">'
            f'<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>'
            '<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="4" orient="auto">'
            '<path d="M0 0 L0 8 L8 4 Z" fill="#95a8c9"/></marker></defs>'
            f'<rect width="1100" height="{height}" rx="22" fill="#101827"/>'
            '<g font-family="DejaVu Sans, Segoe UI, Arial, sans-serif">'
            + text(36, 47, title, size=25, fill='#ffffff', weight=700, anchor='start')
            + content + '</g></svg>\n')


def diagrams(en=False):
    def t(fr, eng):
        return eng if en else fr
    result = {}
    content = text(36, 79, t('Choisir le traitement selon ce qui doit changer.', 'Choose processing based on what needs to change.'), size=16, fill='#9aacc9', anchor='start')
    rows = [
        (t('Organiser les pistes', 'Organize tracks'), ['Conteneur + Copy'], t('Image sans recompression', 'Picture without recompression'), '#74e3b3'),
        (t('Modifier les pixels', 'Change pixels'), [t('Encodage', 'Encoding')], t('Image réencodée', 'Re-encoded picture'), '#96abff'),
        (t('Corriger le HDR déclaré', 'Correct declared HDR'), ['HDR Studio · ' + t('Édition', 'Editing')], t('Métadonnées corrigées', 'Corrected metadata'), '#f6c770'),
    ]
    for i, (need, action, output, color) in enumerate(rows):
        y = 112 + i * 100
        content += rect(36, y, 292, 76) + rect(398, y, 302, 76) + rect(770, y, 294, 76)
        content += text(182, y + 45, need, size=18, fill=color, weight=600)
        content += text(549, y + 45, action, size=18, weight=600)
        content += text(917, y + 45, output, size=17)
        content += arrow(340, y + 38, 386, y + 38) + arrow(712, y + 38, 758, y + 38)
    content += text(550, 440, t('Filtres, interpolation et reconstruction FEL demandent un réencodage vidéo.', 'Filters, interpolation and FEL reconstruction require video re-encoding.'), size=17, fill='#9aacc9')
    result['workflow-choice'] = svg(t('Copier, encoder ou corriger les métadonnées ?', 'Copy, encode or correct metadata?'),
        t('Trois parcours : organiser les pistes en copie, modifier les pixels par encodage ou corriger les métadonnées dans HDR Studio.', 'Three workflows: organize tracks with stream copy, encode to change pixels, or correct metadata in HDR Studio.'), content, 470)

    content = text(36, 79, t('Le résiduel FEL est intégré à l’image avant ses transformations.', 'The FEL residual is integrated into the picture before transformations.'), size=16, fill='#9aacc9', anchor='start')
    for y, label, subtitle, color in [
        (112, 'BL', t('Couche de base', 'Base layer'), '#96abff'),
        (213, 'RPU', t('Courbes Dolby Vision', 'Dolby Vision curves'), '#f6c770'),
        (314, 'FEL', t('Résiduel d’amélioration', 'Enhancement residual'), '#74e3b3'),
    ]:
        content += rect(36, y, 246, 77) + text(159, y + 29, label, fill=color, weight=700)
        content += text(159, y + 57, subtitle, size=15)
        content += arrow(294, y + 38, 362, 252)
    content += rect(374, 190, 220, 124, border='#74e3b3')
    content += text(484, 231, t(['Reconstruction', 'de l’image'], ['Picture', 'reconstruction']), size=21, weight=700)
    content += text(484, 289, 'CPU / GPU', size=15, fill='#74e3b3')
    content += arrow(607, 252, 654, 252) + rect(668, 190, 172, 124)
    content += text(754, 230, t(['Filtres', 'Interpolation'], ['Filters', 'Interpolation']), size=18, weight=600)
    content += arrow(853, 252, 891, 252) + rect(904, 190, 160, 124)
    content += text(984, 230, t(['Encodage', 'vidéo'], ['Video', 'encoding']), size=18, weight=600)
    content += text(675, 367, t('Conserver le RPU en sortie reste un choix séparé.', 'Retaining the output RPU remains a separate choice.'), size=17, fill='#f6c770')
    result['fel-pipeline'] = svg(t('Où intervient Merge FEL ?', 'Where does Merge FEL fit?'),
        t('La couche de base, les courbes RPU et le résiduel FEL servent à reconstruire l’image avant les filtres, l’interpolation et l’encodage.', 'The base layer, RPU curves and FEL residual reconstruct the picture before filters, interpolation and encoding.'), content, 424)

    content = text(36, 79, t('Chaque niveau complète le précédent ; choisissez le travail nécessaire.', 'Each level adds information; choose the work you need.'), size=16, fill='#9aacc9', anchor='start')
    levels = [
        (t('Ouvrir la source', 'Open the source'), t(['Formats HDR, piste', 'et en-têtes déclarés'], ['HDR formats, track', 'and declared headers']), t(['Lecture initiale', 'Sans extension FEL'], ['Initial inspection', 'No FEL extension needed']), '#96abff'),
        (t('Lire les métadonnées', 'Read metadata'), t(['RPU, HDR10+', 'courbes et cohérence'], ['RPU, HDR10+', 'curves and consistency']), t(['Parcourt le fichier', 'Sans décoder l’image'], ['Reads through the file', 'Without decoding pictures']), '#f6c770'),
        (t('Mesurer l’image', 'Measure the picture'), t(['Luminance des pixels', 'MaxCLL / MaxFALL'], ['Pixel luminance', 'MaxCLL / MaxFALL']), ['HEVC PQ', 'mvo-fel ≥ 0.4.0'], '#74e3b3'),
    ]
    for i, (title, body, note, color) in enumerate(levels):
        x = 36 + i * 365
        content += rect(x, 117, 298, 240)
        content += text(x + 149, 154, f'0{i+1}', size=22, fill=color, weight=700)
        content += text(x + 149, 192, title, size=19, fill='#ffffff', weight=600)
        content += text(x + 149, 232, body, size=17)
        content += text(x + 149, 307, note, size=15, fill=color)
        if i < 2:
            content += arrow(x + 310, 237, x + 350, 237)
    result['hdr-inspection-levels'] = svg(t('HDR Studio : trois niveaux de lecture', 'HDR Studio: three inspection levels'),
        t('Ouvrir les en-têtes, lire les métadonnées dynamiques puis mesurer les pixels avec mvo-fel compatible.', 'Open headers, read dynamic metadata, then measure pixels with a compatible mvo-fel extension.'), content, 389)

    content = text(36, 79, t('Exemple ×2 : une image créée entre deux images source.', 'A ×2 example: one new frame between each pair of source frames.'), size=16, fill='#9aacc9', anchor='start')
    content += text(38, 127, t('Source', 'Source'), size=17, fill='#9aacc9', anchor='start')
    content += text(38, 261, t('Sortie ×2', 'Output ×2'), size=17, fill='#9aacc9', anchor='start')
    for i in range(3):
        x = 245 + i * 340
        content += rect(x - 66, 103, 132, 80, border='#96abff')
        content += f'<circle cx="{x - 35 + i*35}" cy="143" r="13" fill="#96abff"/>'
        content += text(x, 207, f'{i}t', size=14, fill='#9aacc9')
    for i in range(5):
        x = 245 + i * 170
        color = '#96abff' if i % 2 == 0 else '#74e3b3'
        content += rect(x - 66, 236, 132, 80, border=color)
        content += f'<circle cx="{x - 35 + i*17.5}" cy="276" r="13" fill="{color}"/>'
        if i % 2:
            content += text(x, 338, t('Créée', 'Created'), size=14, fill=color)
    content += text(550, 391, t('Même durée du film et de l’audio ; davantage d’images.', 'Same movie and audio duration; more frames.'), size=19, weight=600)
    result['interpolation-timeline'] = svg(t('Plus d’images, sans accélérer le film', 'More frames, without speeding up the movie'),
        t('Une suite d’images source et les images intermédiaires ajoutées entre elles sur la même durée.', 'A sequence of source frames and intermediate frames added between them over the same duration.'), content, 422)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site-root', type=Path, default=ROOT.parent / 'muxiveo_web')
    parser.add_argument('--wiki-root', type=Path, default=ROOT.parent / 'mediarecode.wiki')
    args = parser.parse_args()
    for locale in ('fr', 'en'):
        for name, content in diagrams(locale == 'en').items():
            targets = [args.site_root / 'assets/img/guides' / locale / f'{name}.svg']
            if locale == 'fr':
                targets += [args.wiki_root / 'assets/guides' / f'{name}.svg', ROOT / 'docs/assets/guides' / f'{name}.svg']
            for target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
    print('4 schémas SVG générés en français et en anglais')


if __name__ == '__main__':
    main()
