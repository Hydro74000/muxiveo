"""
core/lang_tags.py — Registre des balises de langue RFC 5646 / IETF BCP 47.

Usage :
    from core.lang_tags import Rfc5646LanguageTags

    Rfc5646LanguageTags.TAGS          # dict[str, str]  code → nom
    Rfc5646LanguageTags.items()       # list[tuple[str, str]]
    Rfc5646LanguageTags.is_known(tag) # True si la balise est dans le registre
    Rfc5646LanguageTags.is_valid(tag) # True si la syntaxe RFC 5646 est respectée
"""

from __future__ import annotations

import re


class Rfc5646LanguageTags:
    """
    Registre statique des balises de langue RFC 5646 (IETF BCP 47).

    Les entrées couvrent :
    - « und » (indéfini)
    - toutes les balises ISO 639-1 simples (xx)
    - les principales variantes régionales (xx-XX / xx-Scpt-XX)

    La validation syntaxique accepte n'importe quelle balise conforme à la
    grammaire RFC 5646, même si elle ne figure pas dans ce registre.
    """

    _RE = re.compile(r'^[a-zA-Z]{2,8}(-[a-zA-Z0-9]{1,8})*$')

    # ------------------------------------------------------------------
    # Registre principal  (code → nom anglais, ordre : und puis alpha)
    # ------------------------------------------------------------------

    TAGS: dict[str, str] = {
        'und':         'Undefined',
        'af':          'Afrikaans',
        'af-ZA':       'Afrikaans (South Africa)',
        'ar':          'Arabic',
        'ar-AE':       'Arabic (U.A.E.)',
        'ar-BH':       'Arabic (Bahrain)',
        'ar-DZ':       'Arabic (Algeria)',
        'ar-EG':       'Arabic (Egypt)',
        'ar-IQ':       'Arabic (Iraq)',
        'ar-JO':       'Arabic (Jordan)',
        'ar-KW':       'Arabic (Kuwait)',
        'ar-LB':       'Arabic (Lebanon)',
        'ar-LY':       'Arabic (Libya)',
        'ar-MA':       'Arabic (Morocco)',
        'ar-OM':       'Arabic (Oman)',
        'ar-QA':       'Arabic (Qatar)',
        'ar-SA':       'Arabic (Saudi Arabia)',
        'ar-SY':       'Arabic (Syria)',
        'ar-TN':       'Arabic (Tunisia)',
        'ar-YE':       'Arabic (Yemen)',
        'az':          'Azeri (Latin)',
        'az-AZ':       'Azeri (Latin) (Azerbaijan)',
        'az-Cyrl-AZ':  'Azeri (Cyrillic) (Azerbaijan)',
        'be':          'Belarusian',
        'be-BY':       'Belarusian (Belarus)',
        'bg':          'Bulgarian',
        'bg-BG':       'Bulgarian (Bulgaria)',
        'bs-BA':       'Bosnian (Bosnia and Herzegovina)',
        'ca':          'Catalan',
        'ca-ES':       'Catalan (Spain)',
        'cs':          'Czech',
        'cs-CZ':       'Czech (Czech Republic)',
        'cy':          'Welsh',
        'cy-GB':       'Welsh (United Kingdom)',
        'da':          'Danish',
        'da-DK':       'Danish (Denmark)',
        'de':          'German',
        'de-AT':       'German (Austria)',
        'de-CH':       'German (Switzerland)',
        'de-DE':       'German (Germany)',
        'de-LI':       'German (Liechtenstein)',
        'de-LU':       'German (Luxembourg)',
        'dv':          'Divehi',
        'dv-MV':       'Divehi (Maldives)',
        'el':          'Greek',
        'el-GR':       'Greek (Greece)',
        'en':          'English',
        'en-AU':       'English (Australia)',
        'en-BZ':       'English (Belize)',
        'en-CA':       'English (Canada)',
        'en-CB':       'English (Caribbean)',
        'en-GB':       'English (United Kingdom)',
        'en-IE':       'English (Ireland)',
        'en-JM':       'English (Jamaica)',
        'en-NZ':       'English (New Zealand)',
        'en-PH':       'English (Republic of the Philippines)',
        'en-TT':       'English (Trinidad and Tobago)',
        'en-US':       'English (United States)',
        'en-ZA':       'English (South Africa)',
        'en-ZW':       'English (Zimbabwe)',
        'eo':          'Esperanto',
        'es':          'Spanish',
        'es-AR':       'Spanish (Argentina)',
        'es-BO':       'Spanish (Bolivia)',
        'es-CL':       'Spanish (Chile)',
        'es-CO':       'Spanish (Colombia)',
        'es-CR':       'Spanish (Costa Rica)',
        'es-DO':       'Spanish (Dominican Republic)',
        'es-EC':       'Spanish (Ecuador)',
        'es-ES':       'Spanish (Spain)',
        'es-GT':       'Spanish (Guatemala)',
        'es-HN':       'Spanish (Honduras)',
        'es-MX':       'Spanish (Mexico)',
        'es-NI':       'Spanish (Nicaragua)',
        'es-PA':       'Spanish (Panama)',
        'es-PE':       'Spanish (Peru)',
        'es-PR':       'Spanish (Puerto Rico)',
        'es-PY':       'Spanish (Paraguay)',
        'es-SV':       'Spanish (El Salvador)',
        'es-UY':       'Spanish (Uruguay)',
        'es-VE':       'Spanish (Venezuela)',
        'et':          'Estonian',
        'et-EE':       'Estonian (Estonia)',
        'eu':          'Basque',
        'eu-ES':       'Basque (Spain)',
        'fa':          'Farsi',
        'fa-IR':       'Farsi (Iran)',
        'fi':          'Finnish',
        'fi-FI':       'Finnish (Finland)',
        'fo':          'Faroese',
        'fo-FO':       'Faroese (Faroe Islands)',
        'fr':          'French',
        'fr-BE':       'French (Belgium)',
        'fr-CA':       'French (Canada)',
        'fr-CH':       'French (Switzerland)',
        'fr-FR':       'French (France)',
        'fr-LU':       'French (Luxembourg)',
        'fr-MC':       'French (Principality of Monaco)',
        'gl':          'Galician',
        'gl-ES':       'Galician (Spain)',
        'gu':          'Gujarati',
        'gu-IN':       'Gujarati (India)',
        'he':          'Hebrew',
        'he-IL':       'Hebrew (Israel)',
        'hi':          'Hindi',
        'hi-IN':       'Hindi (India)',
        'hr':          'Croatian',
        'hr-BA':       'Croatian (Bosnia and Herzegovina)',
        'hr-HR':       'Croatian (Croatia)',
        'hu':          'Hungarian',
        'hu-HU':       'Hungarian (Hungary)',
        'hy':          'Armenian',
        'hy-AM':       'Armenian (Armenia)',
        'id':          'Indonesian',
        'id-ID':       'Indonesian (Indonesia)',
        'is':          'Icelandic',
        'is-IS':       'Icelandic (Iceland)',
        'it':          'Italian',
        'it-CH':       'Italian (Switzerland)',
        'it-IT':       'Italian (Italy)',
        'ja':          'Japanese',
        'ja-JP':       'Japanese (Japan)',
        'ka':          'Georgian',
        'ka-GE':       'Georgian (Georgia)',
        'kk':          'Kazakh',
        'kk-KZ':       'Kazakh (Kazakhstan)',
        'kn':          'Kannada',
        'kn-IN':       'Kannada (India)',
        'ko':          'Korean',
        'ko-KR':       'Korean (Korea)',
        'kok':         'Konkani',
        'kok-IN':      'Konkani (India)',
        'ky':          'Kyrgyz',
        'ky-KG':       'Kyrgyz (Kyrgyzstan)',
        'lt':          'Lithuanian',
        'lt-LT':       'Lithuanian (Lithuania)',
        'lv':          'Latvian',
        'lv-LV':       'Latvian (Latvia)',
        'mi':          'Maori',
        'mi-NZ':       'Maori (New Zealand)',
        'mk':          'FYRO Macedonian',
        'mk-MK':       'FYRO Macedonian (Former Yugoslav Republic of Macedonia)',
        'mn':          'Mongolian',
        'mn-MN':       'Mongolian (Mongolia)',
        'mr':          'Marathi',
        'mr-IN':       'Marathi (India)',
        'ms':          'Malay',
        'ms-BN':       'Malay (Brunei Darussalam)',
        'ms-MY':       'Malay (Malaysia)',
        'mt':          'Maltese',
        'mt-MT':       'Maltese (Malta)',
        'nb':          'Norwegian (Bokmål)',
        'nb-NO':       'Norwegian (Bokmål) (Norway)',
        'nl':          'Dutch',
        'nl-BE':       'Dutch (Belgium)',
        'nl-NL':       'Dutch (Netherlands)',
        'nn-NO':       'Norwegian (Nynorsk) (Norway)',
        'ns':          'Northern Sotho',
        'ns-ZA':       'Northern Sotho (South Africa)',
        'pa':          'Punjabi',
        'pa-IN':       'Punjabi (India)',
        'pl':          'Polish',
        'pl-PL':       'Polish (Poland)',
        'ps':          'Pashto',
        'ps-AR':       'Pashto (Afghanistan)',
        'pt':          'Portuguese',
        'pt-BR':       'Portuguese (Brazil)',
        'pt-PT':       'Portuguese (Portugal)',
        'qu':          'Quechua',
        'qu-BO':       'Quechua (Bolivia)',
        'qu-EC':       'Quechua (Ecuador)',
        'qu-PE':       'Quechua (Peru)',
        'ro':          'Romanian',
        'ro-RO':       'Romanian (Romania)',
        'ru':          'Russian',
        'ru-RU':       'Russian (Russia)',
        'sa':          'Sanskrit',
        'sa-IN':       'Sanskrit (India)',
        'se':          'Sami',
        'se-FI':       'Sami (Finland)',
        'se-NO':       'Sami (Norway)',
        'se-SE':       'Sami (Sweden)',
        'sk':          'Slovak',
        'sk-SK':       'Slovak (Slovakia)',
        'sl':          'Slovenian',
        'sl-SI':       'Slovenian (Slovenia)',
        'sq':          'Albanian',
        'sq-AL':       'Albanian (Albania)',
        'sr-BA':       'Serbian (Latin) (Bosnia and Herzegovina)',
        'sr-Cyrl-BA':  'Serbian (Cyrillic) (Bosnia and Herzegovina)',
        'sr-SP':       'Serbian (Latin) (Serbia and Montenegro)',
        'sr-Cyrl-SP':  'Serbian (Cyrillic) (Serbia and Montenegro)',
        'sv':          'Swedish',
        'sv-FI':       'Swedish (Finland)',
        'sv-SE':       'Swedish (Sweden)',
        'sw':          'Swahili',
        'sw-KE':       'Swahili (Kenya)',
        'syr':         'Syriac',
        'syr-SY':      'Syriac (Syria)',
        'ta':          'Tamil',
        'ta-IN':       'Tamil (India)',
        'te':          'Telugu',
        'te-IN':       'Telugu (India)',
        'th':          'Thai',
        'th-TH':       'Thai (Thailand)',
        'tl':          'Tagalog',
        'tl-PH':       'Tagalog (Philippines)',
        'tn':          'Tswana',
        'tn-ZA':       'Tswana (South Africa)',
        'tr':          'Turkish',
        'tr-TR':       'Turkish (Turkey)',
        'tt':          'Tatar',
        'tt-RU':       'Tatar (Russia)',
        'ts':          'Tsonga',
        'uk':          'Ukrainian',
        'uk-UA':       'Ukrainian (Ukraine)',
        'ur':          'Urdu',
        'ur-PK':       'Urdu (Islamic Republic of Pakistan)',
        'uz':          'Uzbek (Latin)',
        'uz-UZ':       'Uzbek (Latin) (Uzbekistan)',
        'uz-Cyrl-UZ':  'Uzbek (Cyrillic) (Uzbekistan)',
        'vi':          'Vietnamese',
        'vi-VN':       'Vietnamese (Viet Nam)',
        'xh':          'Xhosa',
        'xh-ZA':       'Xhosa (South Africa)',
        'zh':          'Chinese',
        'zh-CN':       'Chinese (Simplified)',
        'zh-HK':       'Chinese (Hong Kong)',
        'zh-MO':       'Chinese (Macau)',
        'zh-SG':       'Chinese (Singapore)',
        'zh-TW':       'Chinese (Traditional)',
        'zu':          'Zulu',
        'zu-ZA':       'Zulu (South Africa)',
    }

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Mapping ISO 639-2 (/T et /B) → IETF BCP 47 (ISO 639-1 2-letter)
    # Utilisé pour convertir les codes retournés par ffprobe sur les
    # fichiers non-MKV (MP4, TS…) en balises IETF.
    # ------------------------------------------------------------------

    _ISO639_2_TO_IETF: dict[str, str] = {
        # ISO 639-2/T (terminologique)
        'afr': 'af', 'ara': 'ar', 'aze': 'az', 'bel': 'be', 'bul': 'bg',
        'bos': 'bs', 'cat': 'ca', 'ces': 'cs', 'cym': 'cy', 'dan': 'da',
        'deu': 'de', 'div': 'dv', 'ell': 'el', 'eng': 'en', 'epo': 'eo',
        'est': 'et', 'eus': 'eu', 'fas': 'fa', 'fin': 'fi', 'fao': 'fo',
        'fra': 'fr', 'glg': 'gl', 'guj': 'gu', 'heb': 'he', 'hin': 'hi',
        'hrv': 'hr', 'hun': 'hu', 'hye': 'hy', 'ind': 'id', 'isl': 'is',
        'ita': 'it', 'jpn': 'ja', 'kat': 'ka', 'kaz': 'kk', 'kan': 'kn',
        'kor': 'ko', 'kok': 'kok', 'kir': 'ky', 'lit': 'lt', 'lav': 'lv',
        'mri': 'mi', 'mkd': 'mk', 'mon': 'mn', 'mar': 'mr', 'msa': 'ms',
        'mlt': 'mt', 'nob': 'nb', 'nld': 'nl', 'nno': 'nn', 'pan': 'pa',
        'pol': 'pl', 'pus': 'ps', 'por': 'pt', 'que': 'qu', 'ron': 'ro',
        'rus': 'ru', 'san': 'sa', 'sme': 'se', 'slk': 'sk', 'slv': 'sl',
        'sqi': 'sq', 'srp': 'sr', 'swe': 'sv', 'swa': 'sw', 'syr': 'syr',
        'tam': 'ta', 'tel': 'te', 'tha': 'th', 'tgl': 'tl', 'tsn': 'tn',
        'tur': 'tr', 'tat': 'tt', 'tso': 'ts', 'ukr': 'uk', 'urd': 'ur',
        'uzb': 'uz', 'vie': 'vi', 'xho': 'xh', 'zho': 'zh', 'zul': 'zu',
        # ISO 639-2/B (bibliographique — variantes alternatives)
        'alb': 'sq', 'arm': 'hy', 'baq': 'eu', 'chi': 'zh', 'cze': 'cs',
        'dut': 'nl', 'fre': 'fr', 'geo': 'ka', 'ger': 'de', 'gre': 'el',
        'ice': 'is', 'mac': 'mk', 'may': 'ms', 'per': 'fa', 'rum': 'ro',
        'slo': 'sk', 'wel': 'cy',
        # Indéfini
        'und': 'und',
    }

    @classmethod
    def from_iso639_2(cls, code: str) -> str | None:
        """
        Convertit un code ISO 639-2 (/T ou /B) en balise IETF BCP 47.

        Retourne None si le code est inconnu.
        Retourne 'und' si le code est 'und'.
        """
        if not code:
            return None
        return cls._ISO639_2_TO_IETF.get(code.lower())
    @classmethod
    def to_iso639_2(cls, ietf: str) -> str | None:
        """
        Convertit une balise IETF BCP 47 en code ISO 639-2/T (3 lettres).

        Tronque le sous-tag région avant la recherche (ex : 'en-US' → 'en').
        Retourne None si la balise est inconnue.
        Retourne 'und' si la balise est 'und' ou vide.
        """
        if not ietf:
            return None
        # Normalise : prend seulement la partie langue (avant le premier '-')
        lang_part = ietf.split("-")[0].lower()
        if lang_part in ("und", ""):
            return "und"
        # Recherche inverse dans _ISO639_2_TO_IETF (première correspondance)
        for iso, tag in cls._ISO639_2_TO_IETF.items():
            if tag == lang_part and len(iso) == 3 and iso not in (
                # Exclure les variantes /B (bibliographiques) en faveur de /T
                'alb', 'arm', 'baq', 'chi', 'cze', 'dut', 'fre', 'geo',
                'ger', 'gre', 'ice', 'mac', 'may', 'per', 'rum', 'slo', 'wel',
            ):
                return iso
        return None

    @classmethod
    def items(cls) -> list[tuple[str, str]]:
        """Retourne la liste des (code, nom) dans l'ordre de déclaration."""
        return list(cls.TAGS.items())

    @classmethod
    def is_known(cls, tag: str) -> bool:
        """True si la balise figure dans le registre."""
        return tag in cls.TAGS

    @classmethod
    def is_valid(cls, tag: str) -> bool:
        """
        True si la balise respecte la syntaxe RFC 5646.

        Accepte « und » et les chaînes vides ; valide également les balises
        hors registre (ex : sous-tags privés, nouvelles entrées IANA).
        """
        if not tag or tag == 'und':
            return True
        return bool(cls._RE.match(tag))

    @classmethod
    def validate_item(cls, item, prev_lang: dict) -> bool:
        """
        Valide le tag de langue d'un QTableWidgetItem.

        - Si valide : met à jour prev_lang[row] et retourne True.
        - Si invalide : retourne False sans modifier item ni prev_lang.
        """
        row = item.row()
        tag = item.text().strip()
        if not tag or cls.is_known(tag):
            prev_lang[row] = tag
            return True
        return False
