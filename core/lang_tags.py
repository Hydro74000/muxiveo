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

    # Index insensible à la casse : clé en minuscules → code canonique du registre
    _TAGS_LOWER: dict[str, str] = {}
    # Index des codes IETF courts (xx) vers leur variante régionale par défaut.
    # Construit dynamiquement depuis les mappings ISO 639-2.
    _IETF_SHORT_TO_REGIONAL_IETF: dict[str, str] = {}

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
        'mlt': 'mt', 'nob': 'nb', 'nld': 'nl', 'nno': 'nn', 'nso': 'ns',
        'pan': 'pa', 'pol': 'pl', 'pus': 'ps', 'por': 'pt', 'que': 'qu',
        'ron': 'ro',
        'rus': 'ru', 'san': 'sa', 'sme': 'se', 'slk': 'sk', 'slv': 'sl',
        'spa': 'es', 'sqi': 'sq', 'srp': 'sr', 'swe': 'sv', 'swa': 'sw',
        'syr': 'syr',
        'tam': 'ta', 'tel': 'te', 'tha': 'th', 'tgl': 'tl', 'tsn': 'tn',
        'tur': 'tr', 'tat': 'tt', 'tso': 'ts', 'ukr': 'uk', 'urd': 'ur',
        'uzb': 'uz', 'vie': 'vi', 'xho': 'xh', 'zho': 'zh', 'zul': 'zu',
        # Alias utiles rencontrés dans les pistes Matroska
        'nor': 'no',
        # ISO 639-2/B (bibliographique — variantes alternatives)
        'alb': 'sq', 'arm': 'hy', 'baq': 'eu', 'chi': 'zh', 'cze': 'cs',
        'dut': 'nl', 'fre': 'fr', 'geo': 'ka', 'ger': 'de', 'gre': 'el',
        'ice': 'is', 'mac': 'mk', 'may': 'ms', 'per': 'fa', 'rum': 'ro',
        'slo': 'sk', 'wel': 'cy',
        # Indéfini
        'und': 'und',
    }

    # Variante régionale canonique par défaut pour chaque code ISO 639-2.
    # Utilisée lors de la conversion de pistes de fichiers médias : on préfère
    # la forme xx-XX (langue + région) à la forme courte xx.
    # Lorsqu'aucune région évidente n'existe (eo, ts…), on garde le code court.
    _ISO639_2_TO_REGIONAL_IETF: dict[str, str] = {
        # ISO 639-2/T
        'afr': 'af-ZA', 'ara': 'ar-SA', 'aze': 'az-AZ', 'bel': 'be-BY',
        'bul': 'bg-BG', 'bos': 'bs-BA', 'cat': 'ca-ES', 'ces': 'cs-CZ',
        'cym': 'cy-GB', 'dan': 'da-DK', 'deu': 'de-DE', 'div': 'dv-MV',
        'ell': 'el-GR', 'eng': 'en-US', 'epo': 'eo',    'est': 'et-EE',
        'eus': 'eu-ES', 'fas': 'fa-IR', 'fin': 'fi-FI', 'fao': 'fo-FO',
        'fra': 'fr-FR', 'glg': 'gl-ES', 'guj': 'gu-IN', 'heb': 'he-IL',
        'hin': 'hi-IN', 'hrv': 'hr-HR', 'hun': 'hu-HU', 'hye': 'hy-AM',
        'ind': 'id-ID', 'isl': 'is-IS', 'ita': 'it-IT', 'jpn': 'ja-JP',
        'kat': 'ka-GE', 'kaz': 'kk-KZ', 'kan': 'kn-IN', 'kor': 'ko-KR',
        'kok': 'kok-IN','kir': 'ky-KG', 'lit': 'lt-LT', 'lav': 'lv-LV',
        'mri': 'mi-NZ', 'mkd': 'mk-MK', 'mon': 'mn-MN', 'mar': 'mr-IN',
        'msa': 'ms-MY', 'mlt': 'mt-MT', 'nob': 'nb-NO', 'nld': 'nl-NL',
        'nno': 'nn-NO', 'nso': 'ns-ZA', 'pan': 'pa-IN', 'pol': 'pl-PL',
        'pus': 'ps-AR',
        'por': 'pt-PT', 'que': 'qu-PE', 'ron': 'ro-RO', 'rus': 'ru-RU',
        'san': 'sa-IN', 'sme': 'se-NO', 'slk': 'sk-SK', 'slv': 'sl-SI',
        'spa': 'es-ES', 'sqi': 'sq-AL', 'srp': 'sr-SP', 'swe': 'sv-SE',
        'swa': 'sw-KE',
        'syr': 'syr-SY','tam': 'ta-IN', 'tel': 'te-IN', 'tha': 'th-TH',
        'tgl': 'tl-PH', 'tsn': 'tn-ZA', 'tur': 'tr-TR', 'tat': 'tt-RU',
        'tso': 'ts',    'ukr': 'uk-UA', 'urd': 'ur-PK', 'uzb': 'uz-UZ',
        'vie': 'vi-VN', 'xho': 'xh-ZA', 'zho': 'zh-CN', 'zul': 'zu-ZA',
        'nor': 'no',
        # ISO 639-2/B
        'alb': 'sq-AL', 'arm': 'hy-AM', 'baq': 'eu-ES', 'chi': 'zh-CN',
        'cze': 'cs-CZ', 'dut': 'nl-NL', 'fre': 'fr-FR', 'geo': 'ka-GE',
        'ger': 'de-DE', 'gre': 'el-GR', 'ice': 'is-IS', 'mac': 'mk-MK',
        'may': 'ms-MY', 'per': 'fa-IR', 'rum': 'ro-RO', 'slo': 'sk-SK',
        'wel': 'cy-GB',
        # Indéfini
        'und': 'und',
    }

    # Règles de déduction de la région depuis le titre d'une piste.
    # Format : { base_lang_2: [ (keywords_tuple, regional_tag), ... ] }
    # Les mots-clés sont recherchés en minuscules dans le titre normalisé.
    _TITLE_REGION_HINTS: dict[str, list[tuple[tuple[str, ...], str]]] = {
        'fr': [
            (('canad', 'québec', 'quebec'),                      'fr-CA'),
            (('belgi', 'belgique'),                              'fr-BE'),
            (('swiss', 'suisse', 'schweiz', 'switzerland'),      'fr-CH'),
            (('luxembourg',),                                    'fr-LU'),
            (('monaco',),                                        'fr-MC'),
        ],
        'pt': [
            (('brazil', 'brasil', 'brazilian', 'brésilier'),     'pt-BR'),
            (('portugal',),                                      'pt-PT'),
        ],
        'es': [
            (('latin', 'latino', 'latinoam', 'amérique latine'), 'es-MX'),
            (('mexico', 'méxico', 'mexique'),                    'es-MX'),
            (('argentin',),                                      'es-AR'),
            (('spain', 'españa', 'espagne', 'espagnol'),         'es-ES'),
        ],
        'zh': [
            (('traditional', 'traditionnel', '繁體', '繁体',
              'taiwan', 'hong kong', 'hk', 'macau', 'macao'),    'zh-TW'),
            (('simplified', 'simplifié', '简体', '简体',
              'mainland', 'china', 'chine', 'prc'),              'zh-CN'),
            (('hong kong', 'hk'),                                'zh-HK'),
            (('singapore', 'singapour'),                         'zh-SG'),
        ],
        'de': [
            (('austri', 'autriche', 'österreich'),               'de-AT'),
            (('swiss', 'suisse', 'schweiz', 'switzerland'),      'de-CH'),
        ],
        'en': [
            (('british', 'uk', 'united kingdom', 'england',
              'anglais britannique'),                             'en-GB'),
            (('australian', 'australia'),                        'en-AU'),
            (('canad',),                                         'en-CA'),
        ],
        'nl': [
            (('belgique', 'belgium', 'belgi'),                   'nl-BE'),
        ],
        'sv': [
            (('finland', 'finlande'),                            'sv-FI'),
        ],
        'ar': [
            (('egypt', 'egypte', 'égypte'),                      'ar-EG'),
            (('saudi', 'arabie', 'arabia'),                      'ar-SA'),
            (('morocco', 'maroc'),                               'ar-MA'),
        ],
        'sr': [
            (('cyrillic', 'cyrillique', 'cyrl'),                 'sr-Cyrl-SP'),
            (('latin',),                                         'sr-SP'),
        ],
        'az': [
            (('cyrillic', 'cyrillique', 'cyrl'),                 'az-Cyrl-AZ'),
        ],
        'uz': [
            (('cyrillic', 'cyrillique', 'cyrl'),                 'uz-Cyrl-UZ'),
        ],
    }

    @classmethod
    def infer_region_from_title(cls, base_ietf: str, title: str) -> str | None:
        """
        Tente de déduire une variante régionale depuis le titre d'une piste.

        Exemples :
            infer_region_from_title('fr', 'Français (Canadien)')  → 'fr-CA'
            infer_region_from_title('pt', 'Portuguese (Brazil)')  → 'pt-BR'
            infer_region_from_title('zh', 'Chinese Traditional')  → 'zh-TW'

        Retourne None si aucun indice n'est trouvé.
        """
        if not title:
            return None
        lang = base_ietf.split('-')[0].lower()
        hints = cls._TITLE_REGION_HINTS.get(lang)
        if not hints:
            return None
        title_lower = title.lower()
        for keywords, regional_tag in hints:
            if any(kw in title_lower for kw in keywords):
                return regional_tag
        return None

    @classmethod
    def _ensure_lower_index(cls) -> None:
        """Construit _TAGS_LOWER à la première utilisation."""
        if not cls._TAGS_LOWER:
            cls._TAGS_LOWER = {k.lower(): k for k in cls.TAGS}

    @classmethod
    def _ensure_short_regional_index(cls) -> None:
        """
        Construit _IETF_SHORT_TO_REGIONAL_IETF à la première utilisation.

        La logique reprend exactement les variantes régionales définies pour
        les codes ISO 639-2 afin d'assurer un comportement identique entre
        entrées 3 lettres (ISO 639-2) et 2 lettres (RFC 5646).
        """
        if cls._IETF_SHORT_TO_REGIONAL_IETF:
            return
        mapping: dict[str, str] = {}
        for iso_code, ietf_short in cls._ISO639_2_TO_IETF.items():
            if len(ietf_short) != 2 and ietf_short != "und":
                continue
            regional = cls._ISO639_2_TO_REGIONAL_IETF.get(iso_code)
            if not regional:
                continue
            mapping.setdefault(ietf_short, regional)
        cls._IETF_SHORT_TO_REGIONAL_IETF = mapping

    @classmethod
    def normalize(cls, tag: str) -> str | None:
        """
        Retourne la forme canonique (casse correcte) d'une balise.

        Recherche insensible à la casse dans le registre.
        Retourne None si la balise est absente du registre.
        """
        cls._ensure_lower_index()
        return cls._TAGS_LOWER.get(tag.lower())

    @classmethod
    def from_iso639_2(cls, code: str) -> str | None:
        """
        Convertit un code ISO 639-2 (/T ou /B) en balise IETF BCP 47 courte (xx).

        Retourne None si le code est inconnu.
        Retourne 'und' si le code est 'und'.
        """
        if not code:
            return None
        return cls._ISO639_2_TO_IETF.get(code.lower())

    @classmethod
    def from_iso639_2_regional(cls, code: str) -> str | None:
        """
        Convertit un code ISO 639-2 (/T ou /B) en balise IETF BCP 47 régionale (xx-XX).

        Retourne la forme régionale canonique (ex. 'fra' → 'fr-FR', 'spa' → 'es-ES').
        Retourne None si le code est inconnu.
        Retourne 'und' si le code est 'und'.
        """
        if not code:
            return None
        return cls._ISO639_2_TO_REGIONAL_IETF.get(code.lower())

    @classmethod
    def from_ietf_short_regional(cls, code: str) -> str | None:
        """
        Convertit une balise IETF courte (xx) en variante régionale par défaut.

        Exemples :
            'fr' -> 'fr-FR'
            'en' -> 'en-US'
            'eo' -> 'eo' (pas de région canonique)
        """
        if not code:
            return None
        cls._ensure_short_regional_index()
        return cls._IETF_SHORT_TO_REGIONAL_IETF.get(code.lower())

    @classmethod
    def regionalize_track_language(cls, tag: str, title: str | None = None) -> str | None:
        """
        Normalise un tag de piste en IETF, en privilégiant les formes régionales.

        Règles :
        - ISO 639-2 (xxx) : conversion vers xx-XX (avec inférence régionale via titre).
        - RFC 5646 court (xx) : conversion vers xx-XX selon la même table régionale.
        - Tag déjà régional (xx-XX / xx-Script-XX) : conservé tel quel (casse canonique si connue).
        - 'und' ou invalide : retourne 'und' ou None.
        """
        if not tag:
            return None

        raw = tag.strip()
        if not raw:
            return None

        canonical = cls.normalize(raw) or raw
        if canonical.lower() == "und":
            return "und"

        track_title = title or ""

        # ISO 639-2 (3 lettres) -> forme régionale canonique.
        if len(canonical) == 3 and "-" not in canonical:
            base_ietf = cls.from_iso639_2(canonical)
            if not base_ietf or base_ietf == "und":
                return None
            inferred = cls.infer_region_from_title(base_ietf, track_title)
            return inferred or cls.from_iso639_2_regional(canonical)

        # RFC 5646 court (2 lettres) -> même logique régionale que l'ISO 639-2.
        if len(canonical) == 2 and "-" not in canonical:
            base_ietf = canonical.lower()
            inferred = cls.infer_region_from_title(base_ietf, track_title)
            if inferred:
                return inferred
            default_regional = cls.from_ietf_short_regional(base_ietf)
            if default_regional:
                return default_regional
            if cls.is_valid(base_ietf):
                return cls.normalize(base_ietf) or base_ietf
            return None

        # Balise complète (régionale, script, etc.) : conserve si syntaxe valide.
        if cls.is_valid(canonical):
            return canonical

        return None

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
        raw = ietf.strip()
        if not raw:
            return None
        # Accepte aussi les entrées déjà en ISO 639-2 (/T ou /B).
        lang_part = raw.split("-")[0].lower()
        if lang_part in ("und", ""):
            return "und"
        if lang_part in cls._ISO639_2_TO_IETF:
            return {
                'alb': 'sqi', 'arm': 'hye', 'baq': 'eus', 'chi': 'zho',
                'cze': 'ces', 'dut': 'nld', 'fre': 'fra', 'geo': 'kat',
                'ger': 'deu', 'gre': 'ell', 'ice': 'isl', 'mac': 'mkd',
                'may': 'msa', 'per': 'fas', 'rum': 'ron', 'slo': 'slk',
                'wel': 'cym',
            }.get(lang_part, lang_part)
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
    def from_locale_name(cls, locale_name: str | None) -> str | None:
        """
        Convertit un nom de locale système (ex. ``fr_FR.UTF-8``) en ISO 639-2/T.
        """
        if not locale_name:
            return None
        normalized = locale_name.strip().split(".", 1)[0].replace("_", "-")
        return cls.to_iso639_2(normalized)

    @classmethod
    def iso639_2_name(cls, code: str) -> str | None:
        """Retourne le nom affichable d'un code ISO 639-2/T ou /B."""
        ietf = cls.from_iso639_2(code)
        if ietf is None:
            return None
        return cls.TAGS.get(ietf, ietf)

    @classmethod
    def iso639_2_items(cls) -> list[tuple[str, str]]:
        """
        Retourne les langues ISO 639-2 (3 lettres) triées par libellé puis code.

        Les alias bibliographiques (/B) sont conservés pour refléter fidèlement
        les entrées présentes dans ``_ISO639_2_TO_IETF``.
        """
        items: list[tuple[str, str]] = []
        for iso in cls._ISO639_2_TO_IETF:
            if len(iso) != 3:
                continue
            name = cls.iso639_2_name(iso)
            if name is None:
                continue
            items.append((iso, name))
        return sorted(items, key=lambda item: (item[1].lower(), item[0]))

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
          Si la casse diffère du registre, corrige le texte de l'item.
        - Si invalide : retourne False sans modifier item ni prev_lang.
        """
        row = item.row()
        tag = item.text().strip()
        if not tag:
            prev_lang[row] = tag
            return True
        canonical = cls.normalize(tag)
        if canonical is not None:
            if canonical != tag:
                item.setText(canonical)
            prev_lang[row] = canonical
            return True
        return False
