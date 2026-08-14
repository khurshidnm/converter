"""Value normalisation: text, amounts, dates, accounts, composite fields.

Uzbek bank exports are inconsistent in almost every way a tabular file can be:
amounts arrive as "1 234,56", "1,234.56", ".00" or a native float; dates as
Excel serials, "dd.mm.yy", "dd.mm.yyyy" or "dd.mm.yyyy HH:MM:SS"; counterparty
identity is sometimes three columns and sometimes one string with slashes or
"MFO:/Schet:/INN:" prefixes. Everything that has to cope with that lives here.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal, InvalidOperation

ZERO = Decimal("0")

# Non-breaking spaces, narrow no-break spaces and friends.
_SPACES = "       \t"
_SPACE_RE = re.compile(f"[{_SPACES}]")
_WS_RE = re.compile(r"\s+")

# ISO 4217 numeric codes that appear in positions 6-8 of a UZ account number.
CURRENCY_BY_NUMERIC = {
    "000": "UZS", "860": "UZS", "840": "USD", "978": "EUR", "826": "GBP",
    "392": "JPY", "643": "RUB", "156": "CNY", "756": "CHF", "398": "KZT",
    "944": "AZN", "949": "TRY", "784": "AED", "410": "KRW", "356": "INR",
}

CURRENCY_WORDS = {
    "UZS": "UZS", "СУМ": "UZS", "СЎМ": "UZS", "SUM": "UZS", "SO'M": "UZS",
    "USD": "USD", "ДОЛЛАР": "USD", "EUR": "EUR", "ЕВРО": "EUR",
    "RUB": "RUB", "РУБ": "RUB", "GBP": "GBP", "JPY": "JPY", "CNY": "CNY",
}

ACCOUNT_RE = re.compile(r"\b\d{20}\b")
INN_RE = re.compile(r"\b\d{9}\b")


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------

def clean_text(value: object) -> str:
    """Collapse whitespace and strip. Never returns None."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    else:
        text = str(value)
    text = _SPACE_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def strip_trailing_inn(name: str) -> tuple[str, str]:
    """Split "Some Company Name    201122919" into (name, inn).

    Several banks pad the counterparty name to a fixed width and append the
    INN. Some append it twice. Returns ("", "") safely for empty input.
    """
    text = clean_text(name)
    if not text:
        return "", ""
    inn = ""
    # Strip repeatedly: "Kaznacheystvo ... 201122919 201122919"
    for _ in range(3):
        m = re.search(r"^(.*?)[\s.,]*(\d{9})$", text)
        if not m or not m.group(1).strip():
            break
        candidate_name = m.group(1).strip(" .,")
        # A 20-digit account ending the string is not an INN.
        if re.search(r"\d{12,}$", text):
            break
        inn = inn or m.group(2)
        text = candidate_name
    return text.strip(" .,"), inn


def normalize_document_number(value: object) -> str:
    """Doc numbers arrive as floats from .xls; 3857225216.0 must not stay so."""
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return clean_text(value)


# --------------------------------------------------------------------------
# amounts
# --------------------------------------------------------------------------

_AMOUNT_CHARS_RE = re.compile(r"^[\s\d.,' +\-()]+$")


def parse_amount(value: object) -> Decimal:
    """Parse any amount representation these banks produce into a Decimal.

    Handles: native int/float, "1 234,56", "1,234.56", "1234.56", ".00",
    "(1 234.56)" for negatives, empty and dash placeholders.
    """
    if value is None or value == "":
        return ZERO
    if isinstance(value, bool):
        return ZERO
    if isinstance(value, (int, Decimal)):
        return Decimal(str(value))
    if isinstance(value, float):
        return Decimal(repr(value))

    text = _SPACE_RE.sub("", str(value)).strip()
    if not text or text in {"-", "--", "—", "."}:
        return ZERO

    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace("'", "").replace(" ", "")
    if not _AMOUNT_CHARS_RE.match(text or "0"):
        # Not numeric at all (a label leaked into an amount column).
        return ZERO

    if "," in text and "." in text:
        # Whichever separator comes last is the decimal separator.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        # "1,234" with a 3-digit tail is a thousands separator, not decimals.
        text = (head + tail) if (len(tail) == 3 and head) else (head + "." + tail)
    else:
        # Only dots: treat all but a final 1-2 digit group as thousands sep.
        parts = text.split(".")
        if len(parts) > 2:
            text = "".join(parts[:-1]) + "." + parts[-1]

    text = text.lstrip("+")
    try:
        amount = Decimal(text or "0")
    except InvalidOperation:
        return ZERO
    return -amount if negative else amount


def looks_like_amount(value: object) -> bool:
    """True if the cell plausibly holds a number (used for column scoring).

    Must accept everything parse_amount accepts, including plain ASCII spaces
    used as thousands separators ("3 378 943 223,69"). A stricter test here
    than in parse_amount silently drops real figures.
    """
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return True
    text = _SPACE_RE.sub("", str(value or "")).replace(" ", "").strip()
    if not text:
        return False
    return bool(re.fullmatch(r"[\d.,'()+\-]+", text)) and any(c.isdigit() for c in text)


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------

_EXCEL_EPOCH = _dt.datetime(1899, 12, 30)
_DATE_PATTERNS = (
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
    "%d.%m.%y %H:%M:%S", "%d.%m.%y %H:%M", "%d.%m.%y",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%d-%m-%Y", "%Y.%m.%d",
)
DATE_TOKEN_RE = re.compile(r"\b(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})\b")
TIME_TOKEN_RE = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b")


def parse_datetime(value: object) -> _dt.datetime | None:
    """Best-effort conversion of any cell into a datetime, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    if isinstance(value, _dt.time):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        serial = float(value)
        # Excel serials for 1990-2100. Anything outside is not a date.
        if 25000 <= serial <= 80000:
            return _EXCEL_EPOCH + _dt.timedelta(days=serial)
        return None

    text = clean_text(value)
    if not text:
        return None
    for pattern in _DATE_PATTERNS:
        try:
            return _dt.datetime.strptime(text, pattern)
        except ValueError:
            continue
    # Fall back to pulling a date (and maybe a time) out of a longer string.
    dm = DATE_TOKEN_RE.search(text)
    if not dm:
        return None
    tm = TIME_TOKEN_RE.search(text)
    joined = dm.group(1) + (" " + tm.group(1) if tm else "")
    for pattern in _DATE_PATTERNS:
        try:
            return _dt.datetime.strptime(joined, pattern)
        except ValueError:
            continue
    return None


def parse_time(value: object) -> _dt.time | None:
    if isinstance(value, _dt.time):
        return value
    if isinstance(value, _dt.datetime):
        return value.time()
    text = clean_text(value)
    m = TIME_TOKEN_RE.fullmatch(text) or TIME_TOKEN_RE.search(text)
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(":")]
    while len(parts) < 3:
        parts.append(0)
    try:
        return _dt.time(*parts)
    except ValueError:
        return None


def format_datetime(dt: _dt.datetime | None) -> str:
    return "" if dt is None else dt.strftime("%d.%m.%Y %H:%M:%S")


def format_date(dt: _dt.datetime | None) -> str:
    return "" if dt is None else dt.strftime("%d.%m.%Y")


def looks_like_date(value: object) -> bool:
    return parse_datetime(value) is not None


# --------------------------------------------------------------------------
# accounts, currency, composite counterparty fields
# --------------------------------------------------------------------------

def currency_from_account(account: str) -> str | None:
    """UZ account numbers encode ISO 4217 numeric currency at positions 6-8."""
    digits = re.sub(r"\D", "", account or "")
    if len(digits) != 20:
        return None
    return CURRENCY_BY_NUMERIC.get(digits[5:8])


def currency_from_text(text: str) -> str | None:
    upper = (text or "").upper()
    m = re.search(r"ВАЛЮТА[^:]*:\s*([A-Z]{3})", upper)
    if m and m.group(1) in CURRENCY_WORDS:
        return CURRENCY_WORDS[m.group(1)]
    for word, code in CURRENCY_WORDS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", upper):
            return code
    return None


def find_account(text: str) -> str:
    m = ACCOUNT_RE.search(re.sub(r"[\s ]", "", text or ""))
    return m.group(0) if m else ""


# "МФО:00419 Счет:16401000605703819001 ИНН:310841562"
_TAGGED_RE = re.compile(
    r"(?:МФО|МФ0|MFO|БИК|BIK)\s*[:\-]?\s*(?P<mfo>\d*)"
    r".*?(?:Счет|Счёт|Schet|Hisob)\s*[:\-]?\s*(?P<acct>\d*)"
    r"(?:.*?(?:ИНН|INN|СТИР|STIR)\s*[:\-]?\s*(?P<inn>\d*))?",
    re.IGNORECASE | re.DOTALL,
)


def parse_counterparty_blob(text: str) -> dict[str, str]:
    """Decompose a combined counterparty cell into account / INN / name / MFO.

    Recognises three shapes seen in the wild:
      1. "МФО:00419 Счет:1640100... ИНН:310841562"   (Ipoteka, Xalq HTML)
      2. "20214000205122905002/306712636/АО HAYOT BIRJA"  (Aloqa, MKB)
      3. "Some Name                      201122919"   (padded name + INN)

    Xalq Bank's HTML export packs the correspondent header, the name and the
    payment purpose into one cell separated by <br>. When newlines survive,
    the trailing lines are returned as "purpose" so the caller can use them.
    """
    out = {"account": "", "inn": "", "name": "", "bank_code": "", "purpose": ""}
    if text is None:
        return out

    lines = [ln.strip() for ln in str(text).split("\n") if ln.strip()]
    if len(lines) > 1:
        head = lines[0]
        if _TAGGED_RE.search(head):
            # line 0 = МФО/Счет/ИНН, line 1 = name, remainder = purpose
            sub = parse_counterparty_blob(head)
            out.update({k: sub[k] for k in ("account", "inn", "bank_code")})
            out["name"] = clean_text(lines[1]) if len(lines) > 1 else ""
            out["purpose"] = clean_text(" ".join(lines[2:]))
            if not out["name"] and out["purpose"]:
                out["name"], out["purpose"] = out["purpose"], ""
            return out

    raw = clean_text(text)
    if not raw:
        return out

    tagged = _TAGGED_RE.search(raw)
    if tagged and (tagged.group("acct") or tagged.group("mfo")):
        out["bank_code"] = tagged.group("mfo") or ""
        out["account"] = tagged.group("acct") or ""
        out["inn"] = tagged.group("inn") or ""
        tail = raw[tagged.end():].strip(" /|-")
        out["name"] = clean_text(tail)
        return out

    if "/" in raw:
        parts = [p.strip() for p in raw.split("/")]
        if len(parts) >= 3 and re.fullmatch(r"\d{16,20}", parts[0]):
            out["account"] = parts[0]
            if re.fullmatch(r"\d{7,12}", parts[1]):
                out["inn"] = parts[1]
                out["name"] = clean_text("/".join(parts[2:]))
            else:
                out["name"] = clean_text("/".join(parts[1:]))
            return out
        if len(parts) == 2 and re.fullmatch(r"\d{16,20}", parts[0]):
            out["account"] = parts[0]
            out["name"] = clean_text(parts[1])
            return out

    name, inn = strip_trailing_inn(raw)
    out["name"] = name
    out["inn"] = inn
    out["account"] = find_account(raw) if find_account(raw) != inn else ""
    return out
