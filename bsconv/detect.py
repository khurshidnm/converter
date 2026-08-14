"""Structure detection: header rows, column roles, account sections, layout.

The engine never asks "which bank is this?" before parsing. It asks:
  1. Which rows are table headers?  (a row whose cells match known labels)
  2. What role does each column play? (label match, then data-shape scoring)
  3. Where does each account section start and end?
  4. Is one transaction one row, or several?

Bank identification happens afterwards and is used only for reporting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .loaders import Grid
from .normalize import (
    clean_text, find_account, looks_like_amount, looks_like_date, parse_amount,
    parse_datetime,
)
from .vocabulary import (
    CLIENT_INN_PATTERNS, CLOSING_MARKERS, COUNT_MARKERS, DAILY_TOTAL_MARKERS,
    END_MARKERS, OPENING_MARKERS, PERIOD_PATTERNS, REQUIRED_HEADER_ROLES,
    PERIOD_TOTAL_MARKERS, SECTION_MARKERS, STRONG_HEADER_ROLES, TOTAL_MARKERS,
    has_marker, match_role, normalize_label, normalized_markers,
    starts_with_marker as _starts_with_marker,
)


@dataclass
class HeaderInfo:
    """A detected table header and its column-role mapping."""

    row: int
    columns: dict[str, int] = field(default_factory=dict)   # role -> col index
    score: int = 0
    last_row: int = -1      # headers may wrap over 2-3 physical rows

    def __post_init__(self) -> None:
        if self.last_row < 0:
            self.last_row = self.row

    def col(self, role: str) -> int | None:
        return self.columns.get(role)


@dataclass
class Section:
    """One account's block of the file: preamble, header, body, footer."""

    header: HeaderInfo
    preamble_start: int
    body_start: int
    body_end: int          # inclusive
    footer_end: int        # inclusive
    layout: str = "row"    # "row" | "block"


# --------------------------------------------------------------------------
# header detection
# --------------------------------------------------------------------------

def _row_roles(grid: Grid, r: int) -> dict[str, int]:
    """Assign a role to each cell of a candidate header row."""
    roles: dict[str, int] = {}
    ambiguous: list[int] = []
    for c in range(grid.ncols):
        value = grid.cell(r, c)
        if value is None or not str(value).strip():
            continue
        role = match_role(value)
        if role is None:
            continue
        if role == "account_ambiguous":
            ambiguous.append(c)
            continue
        if role not in roles:
            roles[role] = c

    # Resolve bare "Счет" columns: the first becomes the counterparty account
    # if none was found; a second one (or one alongside an explicit
    # corr_account) is the statement's own account column.
    for c in ambiguous:
        if "corr_account" not in roles and "corr_blob" not in roles:
            roles["corr_account"] = c
        elif "own_account" not in roles:
            roles["own_account"] = c
    return roles


def find_headers(grid: Grid) -> list[HeaderInfo]:
    """Locate every table header row in the sheet (one per account section)."""
    headers: list[HeaderInfo] = []
    for r in range(grid.nrows):
        roles = _row_roles(grid, r)
        if not REQUIRED_HEADER_ROLES.issubset(roles):
            continue
        strong = len(STRONG_HEADER_ROLES & set(roles))
        if strong < 3:
            continue
        # A header row must be mostly labels, not data.
        numeric = sum(
            1 for c in range(grid.ncols)
            if looks_like_amount(grid.cell(r, c)) and str(grid.cell(r, c)).strip()
        )
        if numeric > len(roles):
            continue
        headers.append(HeaderInfo(row=r, columns=roles, score=strong))

    return _merge_wrapped_headers(grid, headers)


def _merge_wrapped_headers(grid: Grid, headers: list[HeaderInfo]) -> list[HeaderInfo]:
    """Fold headers whose labels wrap over consecutive rows into one.

    Asia Alliance splits "МФО корреспондент" across three stacked rows, which
    would otherwise register as three separate account sections.
    """
    if len(headers) < 2:
        return headers
    merged: list[HeaderInfo] = []
    for header in headers:
        if merged and header.row - merged[-1].last_row <= 2:
            previous = merged[-1]
            for role, col in header.columns.items():
                previous.columns.setdefault(role, col)
            previous.score = max(previous.score, header.score)
            previous.last_row = header.row
            continue
        merged.append(header)

    # Also absorb non-header label rows immediately below (e.g. a row holding
    # only the continuation of a wrapped caption).
    for header in merged:
        r = header.last_row + 1
        while r < grid.nrows:
            roles = _row_roles(grid, r)
            texts = [c for c in range(grid.ncols) if clean_text(grid.cell(r, c))]
            if roles and len(texts) <= max(3, len(roles) + 1) and not any(
                looks_like_amount(grid.cell(r, c)) for c in texts
            ):
                header.last_row = r
                for role, col in roles.items():
                    header.columns.setdefault(role, col)
                r += 1
                continue
            break
    return merged


# --------------------------------------------------------------------------
# row classification
# --------------------------------------------------------------------------

ALL_CONTROL_MARKERS = (
    TOTAL_MARKERS + COUNT_MARKERS + OPENING_MARKERS + CLOSING_MARKERS
    + END_MARKERS + DAILY_TOTAL_MARKERS
)


def label_cells(grid: Grid, r: int, header: HeaderInfo) -> list[str]:
    """Cells of a row that can carry a control label.

    Excludes the payment-purpose and amount columns: a purpose line may
    legitimately contain the word "итого", and an amount is never a label.
    Also excludes copies of the purpose text left in neighbouring columns by a
    horizontal merge, which would otherwise smuggle the purpose back in.
    """
    skip = {header.col("purpose"), header.col("debit"), header.col("credit")}
    purpose_col = header.col("purpose")
    purpose_text = clean_text(grid.cell(r, purpose_col)) if purpose_col is not None else ""
    out: list[str] = []
    for c in range(grid.ncols):
        if c in skip:
            continue
        text = clean_text(grid.cell(r, c))
        if not text or (purpose_text and text == purpose_text):
            continue
        out.append(text)
    return out


def is_control_row(grid: Grid, r: int, header: HeaderInfo) -> bool:
    """True for totals, balances, page footers and end-of-report markers.

    The marker must begin the cell. Matching anywhere would misclassify a
    counterparty called "Итого-Сервис" or a purpose mentioning a balance.
    """
    normalized = normalized_markers(ALL_CONTROL_MARKERS)
    for text in label_cells(grid, r, header):
        norm = normalize_label(text)
        if norm and any(norm.startswith(nm) for nm in normalized):
            return True
    return False


def is_section_header_row(grid: Grid, r: int) -> bool:
    return has_marker(grid.row_text(r)[:160], SECTION_MARKERS)


def is_numbering_row(grid: Grid, r: int, header: HeaderInfo) -> bool:
    """Some exports insert a "1 2 3 4 ..." column-number row under the header."""
    values = [clean_text(grid.cell(r, c)) for c in range(grid.ncols)]
    values = [v for v in values if v]
    if len(values) < 3:
        return False
    return all(v.isdigit() and len(v) <= 2 for v in values)


def row_has_data(grid: Grid, r: int, header: HeaderInfo) -> bool:
    """A transaction row carries an amount and some identifying content."""
    d, c = header.col("debit"), header.col("credit")
    has_amount = False
    for col in (d, c):
        if col is None:
            continue
        value = grid.cell(r, col)
        if value is None or str(value).strip() == "":
            continue
        if looks_like_amount(value):
            has_amount = True
    if not has_amount:
        return False
    for role in ("date", "doc_number", "corr_account", "corr_name",
                 "corr_blob", "purpose"):
        col = header.col(role)
        if col is not None and clean_text(grid.cell(r, col)):
            return True
    return False


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

def build_sections(grid: Grid, headers: list[HeaderInfo]) -> list[Section]:
    """Split the sheet into one Section per header row."""
    sections: list[Section] = []
    for i, header in enumerate(headers):
        # The preamble of a section starts right after the previous section's
        # last transaction, so it spans that section's footer too. Metadata
        # lookups are label-anchored, which keeps that overlap harmless, and
        # it is the only way to see a preamble that sits between two tables.
        #
        # One thing must be skipped though: in a block layout the previous
        # transaction's continuation lines (its counterparty name and payment
        # purpose) sit below its amount row. Left in the window, a purpose
        # reading "...обслуживание счета 2020... с 27.12.2025 по 29.12.2025"
        # would be mined for this account's number and period.
        prev_end = 0
        if sections:
            prev_end = sections[-1].body_end + 1
            while prev_end < header.row and not (
                is_control_row(grid, prev_end, sections[-1].header)
                or is_section_header_row(grid, prev_end)
            ):
                prev_end += 1
        next_header_row = headers[i + 1].row if i + 1 < len(headers) else grid.nrows

        # Advance to the first genuine data row. Agrobank and Davr Bank print
        # the bank, account and period lines *below* the column header, so
        # those rows have to fall in the metadata window, not the body.
        body_start = header.last_row + 1
        cursor = body_start
        while cursor < next_header_row:
            if row_has_data(grid, cursor, header) and not is_control_row(
                grid, cursor, header
            ):
                body_start = cursor
                break
            cursor += 1
        else:
            body_start = header.last_row + 1

        # The body ends at the last genuine transaction row before the next
        # header. Everything after it (totals, balances, the next account's
        # preamble) belongs to the footer.
        body_end = body_start - 1
        for cursor in range(next_header_row - 1, body_start - 1, -1):
            if is_control_row(grid, cursor, header) or is_section_header_row(
                grid, cursor
            ):
                continue
            if row_has_data(grid, cursor, header):
                body_end = cursor
                break

        sections.append(Section(
            header=header,
            preamble_start=prev_end,
            body_start=body_start,
            body_end=body_end,
            footer_end=next_header_row - 1 if i + 1 < len(headers) else grid.nrows - 1,
        ))
    return sections


def detect_layout(grid: Grid, section: Section) -> str:
    """Decide whether one transaction occupies one row or a multi-row block.

    Block layouts (Ipoteka Bank's DBO export) put the date, document number and
    amounts on the first line, the time and counterparty name on the second,
    and the payment purpose on the third. The tell is that most non-blank body
    rows carry no amount at all.
    """
    header = section.header
    total = 0
    with_amount = 0
    for r in range(section.body_start, section.body_end + 1):
        if grid.is_blank(r) or is_control_row(grid, r, header):
            continue
        total += 1
        if row_has_data(grid, r, header):
            with_amount += 1
    if total == 0:
        return "row"
    # ipakyuli spaces transactions with blank rows but still puts each on one
    # row, so blanks are excluded above; only genuine continuation lines count.
    return "block" if with_amount * 1.6 < total else "row"


# --------------------------------------------------------------------------
# metadata extraction from a section's preamble / footer
# --------------------------------------------------------------------------

# A strong label is a caption that introduces the statement's own account.
# A weak one is the word "счет" appearing anywhere - including in the middle
# of a payment purpose such as "...обслуживание счета 20208... с 27.12.2025",
# which is why the two are ranked rather than merged.
_ACCOUNT_STRONG_RE = re.compile(
    r"(?:лицевой\s+счет|лицевой\s+счёт|история\s+по\s+счету|"
    r"справка\s+(?:о|по)\s+работе\s+счета|сведения\s+о\s+работе\s+счета|"
    r"выписка\s+по\s+счету|hisob\s+qaydnomasi|hisob\s+raqami|"
    r"[cсCС]\s*ч\s*[её]\s*т\s*[:№])"
    r"[^0-9]{0,30}(\d{20})", re.I)
_ACCOUNT_WEAK_RE = re.compile(
    r"(?:счет|счёт|cчет|schet)[^0-9]{0,30}(\d{20})", re.I)


def extract_metadata(grid: Grid, section: Section) -> dict[str, str]:
    """Pull account number, name, INN, currency and period out of a preamble."""
    meta: dict[str, str] = {}
    lines: list[str] = []
    windows = [
        range(section.preamble_start, section.header.row),
        range(section.header.last_row + 1, section.body_start),
    ]
    for window in windows:
        for r in window:
            if r < 0 or r >= grid.nrows:
                continue
            text = grid.row_text(r)
            if text:
                lines.append(text)
    blob = "\n".join(lines)

    flat = blob.replace("\xa0", " ")
    # Take the LAST strong match: in a multi-account workbook the window
    # begins inside the previous account's footer, and the caption nearest the
    # table is the one that belongs to it.
    strong = _ACCOUNT_STRONG_RE.findall(flat)
    weak = _ACCOUNT_WEAK_RE.findall(flat)
    if strong:
        meta["account_number"] = strong[-1]
    elif weak:
        meta["account_number"] = weak[-1]
    else:
        acct = find_account(flat)
        if acct:
            meta["account_number"] = acct

    for pattern in CLIENT_INN_PATTERNS:
        mi = pattern.search(blob)
        if mi:
            meta["client_inn"] = mi.group(1)
            break

    for pattern in PERIOD_PATTERNS:
        mp = pattern.search(blob)
        if mp:
            d1, d2 = parse_datetime(mp.group(1)), parse_datetime(mp.group(2))
            if d1 and d2 and d1 <= d2:
                meta["period_from"] = d1.strftime("%d.%m.%Y")
                meta["period_to"] = d2.strftime("%d.%m.%Y")
                break

    name = _extract_account_name(lines, meta.get("account_number", ""))
    if name:
        meta["account_name"] = name

    meta["preamble"] = blob
    return meta


_NOISE_PREFIXES = (
    "лицевой счет", "счет", "счёт", "cчет", "клиент", "период", "за ",
    "остаток", "входящий", "исходящий", "справка", "история", "сведения",
    "выписка", "ед.изм", "бик", "дата", "hisob", "dan boshlab", "iabs",
    "подпись", "страница", "конец", "отчет", "количество", "сумма", "итого",
    "валюта", "ответисполнитель", "группа", "id",
)
_NOISE_CONTAINS = ("справка о работе", "cправка о работе", "выписка", "клиент банк")
_MONTH_LINE_RE = re.compile(
    r"^\s*\d{1,2}\s+(янв|фев|мар|апр|ма|июн|июл|авг|сен|окт|ноя|дек)", re.I)


_BANK_BRANCH_RE = re.compile(
    r"(филиали|филиал\b|бош офиси|бошофиси|головной офис|отделение|бўлими|"
    r"^\s*\d{5}\s*[-/\s]\s*[А-ЯЎҚҒҲA-Z])", re.I)


def _is_noise_line(line: str) -> bool:
    low = normalize_label(line)
    if not low:
        return True
    # "00419 ТОШКЕНТ Ш., "ИПОТЕКА-БАНК" АТИБ ... ФИЛИАЛИ" is the bank's own
    # branch line, not the client's name.
    if _BANK_BRANCH_RE.search(line):
        return True
    if any(low.startswith(normalize_label(p)) for p in _NOISE_PREFIXES):
        return True
    if any(normalize_label(p) in low for p in _NOISE_CONTAINS):
        return True
    # A bare date, timestamp or "06 января 2026 г." header line.
    if _MONTH_LINE_RE.match(line) or parse_datetime(line) is not None:
        return True
    if re.match(r"^\s*\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}", line):
        return True
    return False


_NAME_LABEL_RE = re.compile(
    r"^\s*(наименование\s*(счёта|счета|клиента)?|название\s*(счёта|счета)?|"
    r"клиент|nomi|hisob\s+nomi)\s*[:\-]?\s*", re.I)


def _clean_name_candidate(text: str) -> str:
    candidate = clean_text(text)
    candidate = _NAME_LABEL_RE.sub("", candidate)
    candidate = re.sub(r"^\d{5}\s+", "", candidate)
    candidate = re.split(
        r"САЛЬДО|Остаток|ИНН\b|за период|\bза\s+\d{2}\.", candidate)[0]
    # Trailing zeros and stray digits left by neighbouring numeric columns.
    candidate = re.sub(r"[\s,;|/-]*\b\d+([.,]\d+)?\s*$", "", candidate)
    return clean_text(candidate).strip(" -/|,:")


def _extract_account_name(lines: list[str], account: str) -> str:
    """The client name is usually a standalone line, or sits beside the account."""
    for line in lines:
        if not account or account not in line.replace(" ", ""):
            continue
        flat = line.replace("\xa0", " ")
        head, _, tail = flat.partition(account)
        for part in (tail, head):
            candidate = _clean_name_candidate(part)
            if (len(candidate) > 3 and not candidate[:1].isdigit()
                    and not _is_noise_line(candidate)):
                return candidate
    for line in lines:
        if _is_noise_line(line) or re.search(r"\d{9,}", line):
            continue
        candidate = _clean_name_candidate(line)
        if 3 < len(candidate) < 120 and not _is_noise_line(candidate):
            return candidate
    return ""


_AMOUNT_IN_TEXT_RE = re.compile(r"(-?\d[\d  ,.']*\d|\d)")
_COUNT_AFTER_MARKER_RE = re.compile(
    r"(?:итого\s+документов|количество\s+документов|кол[\s.\-]*(?:во)?|"
    r"hujjatlar\s+soni|soni)\s*[:\-]?\s*(\d{1,6})", re.I)


_DATE_PREFIX_RE = re.compile(
    r"^\s*(?:на|за|from|dan|s|с|c)?\s*\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}\s*",
    re.I)
_DATE_LIKE_RE = re.compile(r"^\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}$")


def _amount_after_marker(text: str, markers: tuple[str, ...]):
    """Pull a balance out of "Остаток на начало периода   442,944.55".

    The tricky part is that the label often carries a date first, as in
    "Входящий остаток на 01.01.2025   6,470,145.25". Taking the first number
    would silently return 101.2025 as the balance, so date tokens are consumed
    before the amount is read.
    """
    norm_text = clean_text(text)
    for marker in markers:
        m = re.search(re.escape(marker).replace(r"\ ", r"\s+"), norm_text, re.I)
        if not m:
            continue
        tail = norm_text[m.end():].lstrip(" :=-—")
        # Drop "периода", a following date, and an Актив/Пассив qualifier.
        tail = re.sub(r"^(периода|период|davri|uchun)\s*", "", tail, flags=re.I)
        tail = _DATE_PREFIX_RE.sub("", tail)
        tail = re.sub(r"^(пассив|актив|passiv|aktiv)\s*", "", tail, flags=re.I)
        tail = tail.lstrip(" :=-—")
        for found in _AMOUNT_IN_TEXT_RE.finditer(tail):
            token = found.group(1).strip()
            if _DATE_LIKE_RE.match(token):
                continue
            return parse_amount(token)
    return None


def extract_control_totals(grid: Grid, section: Section) -> dict[str, object]:
    """Read stated turnover, document count and balances for a section.

    Banks print these in three different places: as their own labelled row
    with the figures in the amount columns, as a labelled row with the figure
    in a neighbouring cell, or inline inside a single text line. All three are
    handled, first match wins, so a section's own preamble beats the footer of
    the section above it.
    """
    header = section.header
    out: dict[str, object] = {}

    def amounts_in_row(r: int) -> list:
        values = []
        for c in range(grid.ncols):
            if c == header.col("purpose"):
                continue
            value = grid.cell(r, c)
            if value is None or str(value).strip() == "":
                continue
            if looks_like_amount(value):
                values.append(parse_amount(value))
        return values

    # Window priority differs per figure, and getting it wrong is how a
    # multi-account workbook ends up attributing account N-1's numbers to
    # account N:
    #   opening balance -> preamble first (it is printed above the table)
    #   everything else -> footer first  (printed below the table)
    # The two windows overlap by design, because one account's footer and the
    # next account's preamble are the same stretch of rows.
    footer_window = [
        r for r in range(
            max(section.body_end + 1, section.body_start),
            min(section.footer_end, grid.nrows - 1) + 1,
        )
    ]
    preamble_window = [
        r for r in range(
            max(0, section.preamble_start),
            max(0, min(section.body_start, grid.nrows)),
        )
    ]
    body_control_rows = [
        r for r in range(section.body_start, section.body_end + 1)
        if 0 <= r < grid.nrows and is_control_row(grid, r, header)
    ]

    footer_first = footer_window + body_control_rows + preamble_window
    preamble_first = preamble_window + footer_window + body_control_rows

    # The preamble pass is confined to the preamble so that an embedded
    # "САЛЬДО КОН." belonging to the *next* account cannot be picked up from
    # the shared footer region.
    _scan_rows(grid, section, preamble_window, out, "preamble")
    _scan_rows(grid, section, footer_first, out, "footer")
    return out


def _scan_rows(grid, section, scan_order, out, pass_name) -> None:
    """One extraction pass. `pass_name` selects which figures may be set."""
    header = section.header
    want_opening = pass_name == "preamble"
    want_rest = pass_name == "footer"
    # Closing balances are printed below the table by most banks, but Ipoteka
    # v2 prints "САЛЬДО НАЧ. / САЛЬДО КОН." together on one line above it.
    want_closing = True

    def amounts_in_row(r: int) -> list:
        values = []
        for c in range(grid.ncols):
            if c == header.col("purpose"):
                continue
            value = grid.cell(r, c)
            if value is None or str(value).strip() == "":
                continue
            if looks_like_amount(value):
                values.append(parse_amount(value))
        return values

    seen: set[int] = set()
    for r in scan_order:
        if r in seen or not (0 <= r < grid.nrows):
            continue
        seen.add(r)
        if not grid.row_text(r):
            continue
        labels = label_cells(grid, r, header)
        lead = " ".join(labels)

        # Daily subtotals ("Итого за 20.02.2023") must not be read as the
        # period total.
        daily = any(
            _starts_with_marker(t, DAILY_TOTAL_MARKERS)
            and re.search(r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}", t)
            for t in labels
        )

        for key, markers in (("opening_balance", OPENING_MARKERS),
                             ("closing_balance", CLOSING_MARKERS)):
            if key in out:
                continue
            if key == "opening_balance" and not want_opening:
                continue
            if key == "closing_balance" and not want_closing:
                continue
            if key == "closing_balance" and pass_name == "preamble":
                # Above the table, a closing balance is only trustworthy when
                # it shares the line with the opening one ("САЛЬДО НАЧ. x
                # САЛЬДО КОН. y"). A lone "Исходящий остаток" up there belongs
                # to the previous account, whose footer this window overlaps.
                if not has_marker(lead, OPENING_MARKERS):
                    continue
            leads = any(_starts_with_marker(t, markers) for t in labels)
            # Ipoteka v2 buries the balance mid-line:
            # "SPACEZERO 20208... 00419 САЛЬДО НАЧ. 12,466,344.33"
            embedded = (not leads) and has_marker(lead, markers)
            if not (leads or embedded):
                continue
            inline = _amount_after_marker(lead, markers)
            if inline is not None:
                out[key] = inline
                continue
            if leads:
                values = amounts_in_row(r)
                if values:
                    out[key] = values[-1]

        if (want_rest and not daily
                and any(_starts_with_marker(t, TOTAL_MARKERS) for t in labels)):
            d_col, c_col = header.col("debit"), header.col("credit")
            debit = credit = None
            if d_col is not None and looks_like_amount(grid.cell(r, d_col)):
                debit = parse_amount(grid.cell(r, d_col))
            if c_col is not None and looks_like_amount(grid.cell(r, c_col)):
                credit = parse_amount(grid.cell(r, c_col))
            if debit is None or credit is None:
                values = amounts_in_row(r)
                # "Итого документов | 101 | 6609113.09 | 6726332.4": the
                # count leaks in first; the last two values are the sums.
                if len(values) >= 2:
                    debit, credit = values[-2], values[-1]
            if debit is not None and credit is not None:
                # Aloqa Bank prints a page subtotal ("ИТОГО") immediately
                # above the real period total ("ВСЕГО ЗА ПЕРИОД"). Rank the
                # candidates so the period-scoped one always wins.
                rank = 2 if has_marker(lead, PERIOD_TOTAL_MARKERS) else 1
                best = out.get("_total_rank", 0)
                if rank > best:
                    out["_total_rank"] = rank
                    out["stated_debit"] = debit
                    out["stated_credit"] = credit

        if want_rest and "stated_count" not in out and any(
            _starts_with_marker(t, COUNT_MARKERS) for t in labels
        ):
            # "Количество оборотов | 135 | 14" splits debit and credit counts.
            if any(_starts_with_marker(t, ("количество оборотов",)) for t in labels):
                d_col, c_col = header.col("debit"), header.col("credit")
                pair = []
                for col in (d_col, c_col):
                    if col is not None and looks_like_amount(grid.cell(r, col)):
                        pair.append(int(parse_amount(grid.cell(r, col))))
                if len(pair) == 2:
                    out["stated_count"] = pair[0] + pair[1]
                    out["stated_count_split"] = (pair[0], pair[1])
                    continue
            m = _COUNT_AFTER_MARKER_RE.search(lead)
            if m:
                out["stated_count"] = int(m.group(1))
                continue
            # Otherwise take the only small integer sitting beside the label.
            smalls = [
                int(v) for v in amounts_in_row(r)
                if v == v.to_integral_value() and 0 < v < 100000
            ]
            if len(smalls) == 1:
                out["stated_count"] = smalls[0]
