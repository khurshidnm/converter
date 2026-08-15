# bsconv — universal bank statement → JSON converter

Converts Uzbek bank statement exports into one normalised JSON schema,
whatever the bank, file format or language, and checks the result against the
control totals printed on the statement itself.

Verified against 14 real statements from 11 banks: Asia Alliance, Agrobank,
Aloqabank, Davr Bank, Hamkorbank, Ipak Yo'li, Ipoteka Bank (two export
generations), Mikrokreditbank, MKB, Tenge Bank and Xalq Bank.

---

## Install

```bash
pip install -r requirements.txt      # openpyxl, xlrd, beautifulsoup4
pip install -r requirements-ai.txt   # + anthropic  (for the AI engine)
pip install -r requirements-api.txt  # + fastapi, uvicorn (for the REST API)
export ANTHROPIC_API_KEY=sk-ant-...  # required by the AI engine
```

## Three modes

| | `ai` | `offline` | `auto` |
|---|---|---|---|
| Reads the sheet with | Claude | column-detection rules | chooses offline for simple files, AI for harder ones |
| Needs an API key | yes | no | only if the file is escalated to AI |
| Cost per file | tokens | free | usually free, but may use AI on complex files |
| Same input, same output | not guaranteed | guaranteed | deterministic by default, AI fallback when needed |
| Unfamiliar bank layout | usually still works | may fail | adaptive |

Both `ai` and `offline` produce the same JSON schema and both go through the
same reconciliation checks against the totals the bank printed, so an AI
conversion is checked, not trusted.

```bash
python -m bsconv statement.xlsx -o out/ --engine ai       # force AI
python -m bsconv statement.xlsx -o out/ --engine offline   # force offline
python -m bsconv statement.xlsx -o out/ --engine auto      # simple -> offline, hard -> AI
```

The AI engine cross-checks itself against the rule engine by default and
records any disagreement in the report — differing transaction counts or
totals. Turn it off with `--no-verify`.

The spreadsheet is still opened locally in both cases: the Messages API cannot
read a binary `.xls`/`.xlsx`, so the file is converted to a cell grid first and
that grid is what Claude sees.

## Use it three ways

**Library**

```python
from bsconv import parse_file

statement = parse_file("ipotekabank.xlsx")
for account in statement.active_accounts:
    print(account.account_number, account.currency, len(account.transactions))
    print(account.reconciliation.checks)   # {'turnover': 'pass', ...}
    payload = account.to_json()            # the schema below
```

**CLI**

```bash
python -m bsconv statement.xlsx -o out/
python -m bsconv *.xls *.xlsx -o out/ --report      # + per-file audit report
python -m bsconv statement.xls --stdout             # pipe it somewhere
python -m bsconv statement.xls -o out/ --strict     # exit 1 if totals disagree
```

**REST API**

```bash
uvicorn bsconv.api:app --host 0.0.0.0 --port 8000
curl -F "file=@statement.xlsx" "http://localhost:8000/convert?mode=offline"
curl -F "file=@statement.xlsx" "http://localhost:8000/convert?mode=ai"
curl -F "file=@statement.xlsx" "http://localhost:8000/convert?mode=auto"
```

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness |
| `GET /formats` | accepted formats, recognised banks |
| `POST /convert` | one file → JSON |
| `POST /convert/batch` | many files → keyed JSON |

Query flags: `extended`, `include_empty`, `name_style`, `currency`, `strict`.

---

## Output schema

Every bank produces the identical shape — the reference file
`bank_statement.json`, down to value style:

* `counterparty_name` is written `account/INN/name` (segments omitted when the
  bank does not supply them)
* whole amounts serialise as `0` / `300`, fractional ones as `116999945.4`
* `document_number` is always a string, `transaction_date` always
  `DD.MM.YYYY HH:MM:SS`

One statement file in, one JSON file out. A statement holding several accounts
yields a JSON array of these same objects (one per account) — pass `--split` to
get one file per account instead, in which case every file is a single bare
object.

```json
{
  "account_number": "20208000505703819001",
  "currency": "UZS",
  "transaction_count": 149,
  "total_credit": 2000467243.2,
  "total_debit": 1994471044.12,
  "transactions": [
    {
      "transaction_date": "06.01.2025 08:54:22",
      "document_number": "79",
      "credit_amount": 0,
      "debit_amount": 1735000,
      "counterparty_name": "20208000000966048001/305975326/ООО Дирекция ...",
      "counterparty_account": "20208000000966048001",
      "bank_code": "00401",
      "payment_purpose": "00668~Офертага асосан ..."
    }
  ]
}
```

`--extended` adds `account_name`, `client_inn`, `period_from`, `period_to`,
`opening_balance`, `closing_balance`, and `counterparty_inn` / `operation_code`
per transaction.

---

## What it handles

**Formats.** `.xlsx`, `.xlsm`, real BIFF `.xls`, `.xls` that is actually HTML
(Xalq/Ipoteka iBank exports), `.htm`, `.html`, `.csv`, `.tsv`. The format is
sniffed from magic bytes, not the extension, because these exports lie about
their extension routinely.

**Layouts.** Three families, detected automatically:

- *row* — one transaction per row (most banks)
- *block* — one transaction spread over three rows: amounts, then time and
  counterparty, then purpose (Ipoteka Bank DBO)
- *multi-account* — several account sections stacked in one sheet, each with
  its own preamble, header and totals (Ipoteka, MKB)

**Languages.** Column headers are matched against a synonym table covering
Russian, Uzbek Latin and Uzbek Cyrillic. A bank not in the list parses
correctly as long as its headers use familiar words — add a synonym to
`vocabulary.py` rather than writing a new parser.

**Messy values.**

| Problem | Handled |
|---|---|
| `1 234,56` / `1,234.56` / `.00` / `(1 234.56)` | all parse to Decimal |
| Excel date serials (`43476.0`) | converted |
| `dd.mm.yy` vs `dd.mm.yyyy` vs `dd.mm.yyyy HH:MM:SS` | all parse |
| `Cчет` with a Latin C | homoglyph folding during header matching |
| `№` (NFKC-expands to "No") | stripped before normalisation |
| Name padded with a trailing INN | split into name + INN |
| `acct/INN/name` in one cell | split into three fields |
| `МФО:x Счет:y ИНН:z` in one cell | split into three fields |
| `<br>`-separated name and purpose in one HTML cell | split |
| Merged cells | spread horizontally only — never down, which would duplicate rows |

**Currency.** Read from digits 6–8 of the account number (ISO 4217 numeric:
`000`→UZS, `840`→USD, `978`→EUR), falling back to a stated currency line, then
to `--currency`.

---

## Reconciliation

Every conversion is checked against the figures the bank printed:

| Check | Compares |
|---|---|
| `turnover` | summed debits/credits vs *Сумма оборотов* / *Итого* / *Всего за период* |
| `count` | parsed transaction count vs *Количество оборотов* / *Итого документов* |
| `balance` | opening + credits − debits vs stated closing balance |

Each returns `pass`, `fail` or `skipped` (the statement didn't print it).
A failure never blocks conversion — the JSON is still produced and the
mismatch is reported, because a mismatch usually means the *export* is
incomplete, not that parsing went wrong.

That is exactly what happens with `ipakyuli.xlsx` in the sample set: its rows
sum to 1,426,425,860.32 but its own footer claims 3,378,943,223.69. The row
counter starts at 216, so roughly 215 earlier transactions were left out of
the export. The tool flags it rather than silently returning a short file.

---

## Layout of the code

```
bsconv/
  loaders.py     bytes -> Grid (xlsx / xls / html / csv), format sniffing
  vocabulary.py  RU/UZ synonym tables, markers, normalisation  <- edit this first
  normalize.py   amounts, dates, accounts, composite field splitting
  detect.py      header detection, column roles, sections, control totals
  engine.py      row and block parsers -> Statement
  reconcile.py   the three checks
  cli.py         command line
  api.py         FastAPI service
  ai.py          Claude engine: metadata pass + batched row extraction
tests/
  test_bsconv.py  corpus + unit tests
  test_ai.py      AI engine, driven by a fake client (no key needed)
  test_api.py     endpoint tests            (157 tests total)
```

## Adding a bank

Usually nothing is needed — try it first. If the headers aren't recognised:

1. Add the bank's column captions to `COLUMN_SYNONYMS` in `vocabulary.py`.
2. If its totals row uses new wording, add it to `TOTAL_MARKERS` or
   `COUNT_MARKERS`.
3. Add the file to `tests/test_bsconv.py::EXPECTED` with its expected
   transaction count and run `pytest`.

Optionally add a name to `BANK_FINGERPRINTS` so reports label it. Detection is
cosmetic and never affects parsing.

## Tests

```bash
pytest -q                                     # unit tests only
BSCONV_SAMPLES=/path/to/statements pytest -q  # + corpus tests

# opt in to one real API call against the smallest sample
BSCONV_LIVE=1 ANTHROPIC_API_KEY=sk-ant-... BSCONV_SAMPLES=... pytest -q
```

The AI engine is tested with a stub client, so its batching, schema use,
amount parsing and cross-check logic are covered without a key or network.

The corpus tests assert transaction counts, reconciliation status, schema key
order, value types, that no posting carries both a debit and a credit, and
that dates fall inside the statement period.
