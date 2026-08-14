"""Multilingual vocabulary for header, metadata and control-row recognition.

Uzbek bank exports are written in Russian, Uzbek Latin and Uzbek Cyrillic,
often mixed inside one file. Rather than hard-coding one layout per bank, the
engine matches column headers against these synonym sets, so an unseen bank
using familiar labels parses without new code.

Add a synonym here rather than a new parser whenever possible.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# --------------------------------------------------------------------------
# canonical column roles
# --------------------------------------------------------------------------
# Order matters: earlier entries win when a header cell matches several roles.
# Each entry is (role, tuple of exact normalised labels, tuple of substrings).

COLUMN_SYNONYMS: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    # --- amounts -----------------------------------------------------------
    ("debit",
     ("дебет", "оборот дебет", "обороты по дебету", "оборотыподебету",
      "сумма дебет", "сумма дебита", "сумма дебета", "дебетовый оборот",
      "debet", "debit", "chiqim", "rasxod"),
     ("оборот дебет", "обороты по дебет", "сумма дебет", "сумма дебит",
      "дебетовый оборот", "дебет")),
    ("credit",
     ("кредит", "оборот кредит", "обороты по кредиту", "оборотыпокредиту",
      "сумма кредит", "сумма кредита", "кредитовый оборот",
      "kredit", "credit", "kirim", "prixod"),
     ("оборот кредит", "обороты по кредит", "сумма кредит",
      "кредитовый оборот", "кредит")),
    # --- narrative ---------------------------------------------------------
    ("purpose",
     ("назначение платежа", "назначение", "detali", "tolov tafsilotlari",
      "to'lov tafsilotlari", "tafsilotlar", "payment purpose", "purpose",
      "izoh", "описание"),
     ("назначение", "tafsilot", "purpose", "detali")),
    # --- counterparty ------------------------------------------------------
    ("corr_blob",
     ("счет/инн", "cчет/инн", "счёт/инн", "корреспондент банк счет инн",
      "корреспондент: банк / счет / инн наименование назначение платежа",
      "корреспондент: банк/счет/инн наименование назначение платежа"),
     ("счет/инн", "cчет/инн", "счёт/инн", "корреспондент: банк")),
    ("corr_account",
     ("счет корреспондента", "счёт корреспондента", "счет кореспондента",
      "счет-корреспондент", "счет-корреспондента", "корреспондентский счет",
      "vakil hisob raqam", "hisob raqam", "korr schet"),
     ("счет корреспондент", "счёт корреспондент", "счет кореспондент",
      "счет-корреспондент", "vakil hisob raqam")),
    ("corr_name",
     ("наименование корреспондента", "наименование кореспондента",
      "наименование контрагента", "наименование счета", "наименование счёта",
      "наименование", "vakil hisob raqam nomi", "korrespondent nomi",
      "контрагент", "имя"),
     ("наименование корреспондент", "наименование кореспондент",
      "наименование контрагент", "наименование счет", "наименование счёт",
      "hisob raqam nomi", "контрагент")),
    ("corr_bank_name",
     ("наименование банка корреспондента", "банк корреспондента", "банк"),
     ("наименование банка", "банк корреспондент")),
    ("inn",
     ("инн", "стир", "inn", "stir", "инн клиента", "инн контрагента"),
     ("инн", "стир", "stir")),
    # --- identifiers -------------------------------------------------------
    ("mfo",
     ("мфо", "мфо банка", "бик", "бик/swift", "бик / swift", "mfo", "bik",
      "филиал", "filial", "код банка"),
     ("мфо", "бик", "swift", "mfo")),
    ("doc_number",
     ("№ док", "№ док.", "№ док-та", "n документа", "№ документа",
      "номер документа", "номер док-та", "№документа", "док", "dok",
      "hujjat raqami", "транзакционный номер", "номер"),
     ("док", "документа", "документ", "hujjat", "транзакцион")),
    ("op_code",
     ("оп", "вид операции", "вид док-та", "тип документа", "во", "op",
      "amaliyot", "код операции"),
     ("вид операции", "вид док", "тип документа", "код операции")),
    # --- dates -------------------------------------------------------------
    ("doc_date",
     ("дата документа", "дата док-та", "hujjat sanasi"),
     ("дата документа", "дата док")),
    ("date",
     ("дата", "дата проводки", "дата/времяпроводки", "дата/время проводки",
      "дата операции", "дата и время", "sana", "date", "sanasi", "kun"),
     ("дата проводки", "дата/время", "дата операции", "дата", "sana", "date")),
    # --- own account / misc ------------------------------------------------
    ("own_account",
     ("счет клиента", "счёт клиента", "лицевой счет", "мой счет"),
     ("счет клиента", "счёт клиента", "лицевой счет")),
    ("own_name",
     ("наименование клиента", "название счета", "название счёта"),
     ("наименование клиента", "название счет", "название счёт")),
    ("cash_symbol",
     ("кассовый символ", "кас.смв", "кассовый симв", "kassa simvoli"),
     ("кассовый символ", "кас.смв", "кассовый симв")),
    ("code",
     ("код", "code", "kod"),
     ()),
    ("region", ("регион", "viloyat"), ("регион",)),
    ("row_no", ("№ пп", "№пп", "no", "n", "№", "т/р", "no."), ()),
    # Bare "счет" is ambiguous; it is resolved contextually in detect.py.
    # The genitive "№ счёта" is included: Ipak Yo'li heads its counterparty
    # account column that way. No substring rules here - "наименование счёта"
    # must keep matching corr_name, which is ranked above.
    ("account_ambiguous",
     ("счет", "счёт", "cчет", "cчёт", "schet", "hisob",
      "счета", "счёта", "№ счета", "№ счёта", "no счета", "no счёта"),
     ()),
]

ROLE_ORDER = [role for role, _, _ in COLUMN_SYNONYMS]

# Roles that must be present for a row to qualify as a table header.
REQUIRED_HEADER_ROLES = {"debit", "credit"}
STRONG_HEADER_ROLES = {"debit", "credit", "purpose", "date", "doc_number",
                       "mfo", "corr_account", "corr_name", "corr_blob"}

# --------------------------------------------------------------------------
# control / terminator rows
# --------------------------------------------------------------------------

TOTAL_MARKERS = (
    "итого", "итог", "всего", "umumiy", "jami", "jami:", "оборот за период",
    "сумма оборотов", "обороты за период", "итоговый оборот",
)
COUNT_MARKERS = ("количество оборотов", "кол-во", "кол.", "итого документов",
                 "количество документов", "soni", "hujjatlar soni")
OPENING_MARKERS = (
    "остаток на начало", "остаток на начала", "входящий остаток",
    "входящее сальдо", "сальдо нач", "boshlangich qoldiq", "ochilish qoldigi",
    "остаток на начало периода",
)
CLOSING_MARKERS = (
    "остаток на конец", "исходящий остаток", "исходящее сальдо", "сальдо кон",
    "yakuniy qoldiq", "oxirgi qoldiq",
)
DAILY_TOTAL_MARKERS = ("итого за", "jami:", "итого по дню")
# Totals explicitly scoped to the whole period outrank a bare "ИТОГО", which
# in some exports is only a page or per-day subtotal.
PERIOD_TOTAL_MARKERS = (
    "за период", "весь период", "всего за период", "всего", "итоговый оборот",
    "оборот за период", "обороты за период", "сумма оборотов", "umumiy",
    "davr uchun",
)
END_MARKERS = ("конец отчета", "конец выписки", "hisobot oxiri", "*****конец")
SECTION_MARKERS = (
    "лицевой счет", "справка о работе счета", "справка по работе счета",
    "история по счету", "сведения о работе счета", "выписка лицевых счетов",
    "hisob qaydnomasi", "выписка по счету",
)

# --------------------------------------------------------------------------
# metadata patterns
# --------------------------------------------------------------------------

PERIOD_PATTERNS = (
    re.compile(r"(?:с|c|за|from)\s*(\d{2}[.\-/]\d{2}[.\-/]\d{2,4})\s*"
               r"(?:по|-|—|до|to)\s*(\d{2}[.\-/]\d{2}[.\-/]\d{2,4})", re.I),
    re.compile(r"(\d{2}[.\-/]\d{2}[.\-/]\d{2,4})\s*dan\s*"
               r"(\d{2}[.\-/]\d{2}[.\-/]\d{2,4})", re.I),
    re.compile(r"(\d{2}[.\-/]\d{2}[.\-/]\d{2,4})\s*[-–—]\s*"
               r"(\d{2}[.\-/]\d{2}[.\-/]\d{2,4})"),
)

CLIENT_INN_PATTERNS = (
    re.compile(r"ИНН\s*(?:клиента)?\s*[:\-]?\s*(\d{9})", re.I),
    re.compile(r"СТИР\s*[:\-]?\s*(\d{9})", re.I),
)

BANK_FINGERPRINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ipoteka",     ("ипотека-банк", "ipoteka")),
    ("aloqabank",   ("алокабанк", "aloqabank")),
    ("ipakyuli",    ("ipak yo", "ипак йул", "ипак йўл")),
    ("hamkorbank",  ("hamkorbank", "хамкорбанк", "ҳамкорбанк")),
    ("xalqbank",    ("халк банки", "халқ банки", "xalq bank")),
    ("agrobank",    ("агробанк", "agrobank")),
    ("mikrokredit", ("микрокредитбанк", "mikrokredit")),
    ("davrbank",    ("davr bank", "давр банк", "davrbank")),
    ("tengebank",   ("tenge bank", "тенге банк", "tengebank")),
    ("asiaalliance", ("asia alliance", "азия альянс")),
    ("kapitalbank", ("kapitalbank", "капиталбанк")),
    ("nbu",         ("узнацбанк", "national bank", "нбу")),
    ("sqb",         ("узсаноаткурилишбанк", "sqb")),
    ("trastbank",   ("trastbank", "трастбанк")),
    ("infinbank",   ("infinbank", "инфинбанк")),
    ("orientfinans", ("orient finans", "ориент финанс")),
    ("anorbank",    ("anorbank", "анорбанк")),
    ("turonbank",   ("turonbank", "туронбанк")),
    ("aab",         ("asia alliance bank",)),
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[\s  -​.,:;()\[\]«»\"'`_/\\|№#*-]+")


def normalize_label(text: object) -> str:
    """Lowercase, strip punctuation and unify the Cyrillic/Latin 'c' problem.

    Some exports write "Cчет" with a Latin C. Normalising to NFKC does not fix
    that, so homoglyphs are folded explicitly.

    Memoised: on a 500-row statement this is called millions of times, and it
    is by far the hottest function in the parser.
    """
    if text is None:
        return ""
    return _normalize_cached(str(text))


# NFKC expands these into letters ("№" -> "No", "℗" -> "P"), which would put
# spurious word characters at the front of a label. Strip them first.
_PRE_NFKC_STRIP_RE = re.compile(r"[№#⁋§℮™©®]")


@lru_cache(maxsize=16384)
def _normalize_cached(text: str) -> str:
    s = _PRE_NFKC_STRIP_RE.sub(" ", text)
    s = unicodedata.normalize("NFKC", s).lower()
    s = _PUNCT_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


_HOMOGLYPHS = {"c": "с", "a": "а", "e": "е", "o": "о", "p": "р",
               "x": "х", "y": "у", "k": "к", "m": "м", "t": "т",
               "h": "н", "b": "в"}


@lru_cache(maxsize=8192)
def fold_homoglyphs(text: str) -> str:
    """Map Latin lookalikes onto Cyrillic.

    Several exports write "Cчет" with a Latin C, so a header cell that looks
    identical to a human does not compare equal. Folding is applied only when
    matching column labels - never to free text, where it would corrupt
    genuinely Latin content such as "ASIA ALLIANCE BANK".
    """
    return "".join(_HOMOGLYPHS.get(ch, ch) for ch in text)


@lru_cache(maxsize=512)
def normalized_markers(markers: tuple[str, ...]) -> tuple[str, ...]:
    """Normalise a marker tuple once instead of on every comparison."""
    return tuple(m for m in (normalize_label(x) for x in markers) if m)


# Pre-normalised role table, built once at import.
_ROLE_TABLE: list[tuple[str, frozenset, tuple[str, ...]]] = [
    (role,
     frozenset(fold_homoglyphs(normalize_label(e)) for e in exact),
     tuple(n for n in (fold_homoglyphs(normalize_label(s)) for s in subs) if n))
    for role, exact, subs in COLUMN_SYNONYMS
]


@lru_cache(maxsize=4096)
def _match_role_cached(norm: str) -> str | None:
    if not norm or len(norm) > 90:
        return None
    folded = fold_homoglyphs(norm)
    for role, exact, _ in _ROLE_TABLE:
        if folded in exact:
            return role
    for role, _, substrings in _ROLE_TABLE:
        if any(sub in folded for sub in substrings):
            return role
    return None


def match_role(label: object) -> str | None:
    """Map one header cell to a canonical role, or None."""
    return _match_role_cached(normalize_label(label))


def has_marker(text: object, markers: tuple[str, ...]) -> bool:
    norm = normalize_label(text)
    return any(m in norm for m in normalized_markers(tuple(markers)))


def starts_with_marker(text: object, markers: tuple[str, ...]) -> bool:
    norm = normalize_label(text)
    return any(norm.startswith(m) for m in normalized_markers(tuple(markers)))


def detect_bank(text: str) -> str:
    low = normalize_label(text)
    for name, needles in BANK_FINGERPRINTS:
        if any(n in low for n in normalized_markers(tuple(needles))):
            return name
    return "unknown"
