"""CSV bank-statement parsing for the import feature.

Pure logic, no UI / DB dependencies, so it can be unit-tested in isolation.
Tuned for the CommBank transaction CSV export, which has **no header row**
and four columns:

    Date , Amount , Description , Balance
    01/01/2020,"+50.00","Transfer from xx0000 CommBank app","+150.00"
    02/01/2020,"-9.99","EXAMPLE STORE SYDNEY AU AUS Card xx0000 Value Date: 01/01/2020","+140.01"

Key behaviours (decided with the user):

* **Which month a transaction belongs to** comes from the ``Value Date:``
  embedded in the description (the date it actually happened) when present,
  otherwise the leading settled date.
* **Merchant key** — how rows are grouped and remembered across imports:
    - Normal rows: the leading words of the description, stopped at the
      first "noise" token (a number, store id, ``Card``, ``Value``, a
      state/country code…), capped at 3 words, upper-cased. So every
      ``MCDONALDS …`` line collapses to ``MCDONALDS`` and auto-matches a
      saved rule next time.
    - Transfer rows (description starts with ``Transfer from`` / ``Transfer
      To``): the **full** description is kept so the user can tell distinct
      transfers apart and categorise each one.
* Amount sign is preserved (``+`` inflow, ``-`` outflow) and surfaced to the
  user; the category *kind* they pick later decides how it's finally stored.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Optional


# Tokens that mark the end of the "merchant" portion of a CommBank
# description. Once we hit one of these (or any token containing a digit)
# we stop collecting words for the merchant key. Upper-cased for matching.
_NOISE_TOKENS = {
    "CARD", "VALUE", "DATE", "AUS", "AU", "USA", "GBR", "EN", "NZ", "UK",
    # Australian state / territory abbreviations that appear as location
    # codes in card transactions.
    "VIC", "VI", "NSW", "QLD", "WA", "SA", "TAS", "NT", "ACT",
}

_VALUE_DATE_RE = re.compile(r"Value\s+Date:\s*(\d{1,2})/(\d{1,2})/(\d{4})", re.IGNORECASE)
_LEADING_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
# Text dates like "18 MAY 2026" / "1 January 2026" used by the
# savings-account export layout.
_TEXT_DATE_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})$")
_MONTH_NAMES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_MAX_MERCHANT_WORDS = 3


@dataclass(frozen=True)
class ParsedTransaction:
    """One statement line, normalised for import."""

    year: int
    month: int
    amount_cents: int  # signed: positive = money in, negative = money out
    description: str  # full original description (whitespace-collapsed)
    merchant_key: str  # normalised key used for grouping + rule matching
    display_name: str  # column label for the temporary category
    is_transfer: bool


@dataclass
class ParseResult:
    transactions: list[ParsedTransaction] = field(default_factory=list)
    # (1-based line number, raw line text, reason) for rows we couldn't parse.
    errors: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.transactions)


def _parse_amount_to_cents(raw: str) -> Optional[int]:
    """``"+100.00"`` / ``"-14.99"`` → signed integer cents. None if unparseable."""
    s = (raw or "").strip().replace("$", "").replace(",", "")
    if s == "":
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()
    try:
        value = float(s)
    except ValueError:
        return None
    if neg:
        value = -value
    return int(round(value * 100))


def extract_value_date(description: str) -> Optional[tuple[int, int, int]]:
    """Return ``(year, month, day)`` from an embedded ``Value Date:`` or None."""
    m = _VALUE_DATE_RE.search(description or "")
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return year, month, day


def _parse_leading_date(raw: str) -> Optional[tuple[int, int, int]]:
    """Parse a row's date cell in either ``DD/MM/YYYY`` (transaction-account
    export) or ``DD MON YYYY`` (savings-account export) form."""
    s = (raw or "").strip()
    m = _LEADING_DATE_RE.match(s)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return year, month, day
        return None
    m = _TEXT_DATE_RE.match(s)
    if m:
        month = _MONTH_NAMES.get(m.group(2)[:3].upper())
        if month is None:
            return None
        day, year = int(m.group(1)), int(m.group(3))
        if 1 <= day <= 31:
            return year, month, day
        return None
    return None


def _interpret_row(row: list[str]) -> Optional[tuple[str, str, str]]:
    """Map a raw CSV row to ``(date_raw, amount_raw, description)`` across the
    two known CommBank layouts, or None if the row can't be interpreted.

    * Transaction account: ``Date, Amount, Description, Balance`` — amount in
      column 1 (index 1), one signed value.
    * Savings account: ``Date, , Description, , Amount, Balance`` — blank
      padding columns, description in column 2 and the signed amount in the
      column just before the balance.
    """
    cells = [(c or "").strip() for c in row]
    n = len(cells)

    # Savings layout: >= 6 columns, amount sits before the balance (last col),
    # description in column index 2. Detect by a parseable amount in index 4
    # (or the last non-empty numeric before the balance) while index 1 is not
    # itself the amount.
    if n >= 6:
        amt4 = _parse_amount_to_cents(cells[4])
        amt3 = _parse_amount_to_cents(cells[3])
        if amt4 is not None or amt3 is not None:
            amount_raw = cells[4] if amt4 is not None else cells[3]
            return cells[0], amount_raw, cells[2]

    # Transaction-account layout: amount in column 1.
    if n >= 4 and _parse_amount_to_cents(cells[1]) is not None:
        return cells[0], cells[1], cells[2]

    # Looser fallback: a 3-column signed row (Date, Amount, Description).
    if n >= 3 and _parse_amount_to_cents(cells[1]) is not None:
        return cells[0], cells[1], cells[2]

    return None


def is_transfer_description(description: str) -> bool:
    """For account-to-account transfer lines."""
    return (description or "").strip().lower().startswith("transfer")


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def merchant_key_for(description: str) -> tuple[str, str]:
    """Compute ``(merchant_key, display_name)`` for a description.

    * Transfers keep the full description (both key and display), so distinct
      transfers stay distinct.
    * Otherwise take leading words up to the first noise/numeric token,
      capped at 3 words. ``display_name`` preserves original case; the key
      is upper-cased for case-insensitive matching.
    """
    cleaned = _collapse_ws(description)
    if is_transfer_description(cleaned):
        return cleaned.upper(), cleaned

    words: list[str] = []
    for token in cleaned.split(" "):
        up = token.upper()
        # Stop at anything containing a digit (store ids, card numbers,
        # value dates) or a known location/noise token.
        if any(ch.isdigit() for ch in token) or up in _NOISE_TOKENS:
            break
        words.append(token)
        if len(words) >= _MAX_MERCHANT_WORDS:
            break

    if not words:
        # Description led with a number/noise token; fall back to the first
        # raw token so the merchant is never empty.
        first = cleaned.split(" ")[0] if cleaned else "UNKNOWN"
        return first.upper(), first

    display = " ".join(words)
    return display.upper(), display


# ----- Flexible column mapping (hybrid multi-bank import) ----------------
# A bank's CSV layout is captured as a :class:`ColumnMapping`: which column
# holds the date / description / amount(s), how amounts are signed, and the
# date format. Auto-detection produces a best-guess mapping; the user can
# correct it in the import dialog; the confirmed mapping is then saved per
# bank (keyed by :func:`file_signature`) so future imports are one-click.

# Amount modes:
#   "signed"        one column, sign in the value (- = money out)
#   "debit_credit"  two columns; debit = money out, credit = money in
DATE_FORMATS = ("auto", "dmy_slash", "text_month", "ymd_dash", "mdy_slash")


@dataclass
class ColumnMapping:
    date_col: int
    desc_col: int
    amount_mode: str = "signed"  # "signed" | "debit_credit"
    amount_col: Optional[int] = None
    debit_col: Optional[int] = None
    credit_col: Optional[int] = None
    has_header: bool = False
    date_format: str = "auto"
    # When True, a positive value in a single signed column means money OUT
    # (some banks list spending as positive). Default: negative = out.
    invert_sign: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(text: str) -> "ColumnMapping":
        data = json.loads(text)
        return ColumnMapping(**data)


def read_raw_rows(path: str) -> list[list[str]]:
    """Read a CSV into a list of string rows (no interpretation)."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return [list(r) for r in csv.reader(fh)]


def _looks_like_header(cells: list[str]) -> bool:
    """A header row has no date and no numeric amount in any cell, but does
    have some non-empty text (column titles)."""
    nonempty = [c for c in cells if c.strip()]
    if not nonempty:
        return False
    for c in nonempty:
        if _parse_date_any(c) is not None or _parse_amount_to_cents(c) is not None:
            return False
    return True


def _parse_date_any(raw: str) -> Optional[tuple[int, int, int]]:
    """Parse a date in any supported format; returns (year, month, day)."""
    s = (raw or "").strip()
    if not s:
        return None
    # DD/MM/YYYY (au) — also tolerate '-' separators.
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a <= 31 and b <= 12:
            return y, b, a
        return None
    # YYYY-MM-DD (iso)
    m = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return y, mo, d
        return None
    # DD MON YYYY
    m = _TEXT_DATE_RE.match(s)
    if m:
        mo = _MONTH_NAMES.get(m.group(2)[:3].upper())
        if mo is not None:
            return int(m.group(3)), mo, int(m.group(1))
    return None


def _parse_date_with_format(raw: str, fmt: str) -> Optional[tuple[int, int, int]]:
    s = (raw or "").strip()
    if fmt == "auto":
        return _parse_date_any(s)
    if fmt == "dmy_slash":
        m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
        if m:
            return int(m.group(3)), int(m.group(2)), int(m.group(1))
        return None
    if fmt == "mdy_slash":
        m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
        if m:
            return int(m.group(3)), int(m.group(1)), int(m.group(2))
        return None
    if fmt == "ymd_dash":
        m = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", s)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
        return None
    if fmt == "text_month":
        m = _TEXT_DATE_RE.match(s)
        if m:
            mo = _MONTH_NAMES.get(m.group(2)[:3].upper())
            if mo is not None:
                return int(m.group(3)), mo, int(m.group(1))
        return None
    return _parse_date_any(s)


def detect_date_format(rows: list[list[str]], date_col: int) -> str:
    for row in rows:
        if date_col >= len(row):
            continue
        s = (row[date_col] or "").strip()
        if not s:
            continue
        if re.match(r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}$", s):
            return "ymd_dash"
        if _TEXT_DATE_RE.match(s):
            return "text_month"
        if re.match(r"^\d{1,2}[/-]\d{1,2}[/-]\d{4}$", s):
            return "dmy_slash"
    return "auto"


def detect_mapping(rows: list[list[str]]) -> ColumnMapping:
    """Best-effort guess of a bank CSV's column layout.

    Strategy: find the first row carrying a date (skipping header/junk),
    treat the widest column count as canonical, then classify columns:
    the date column, the numeric columns (the last is the running balance,
    the one(s) before it are the amount or debit/credit), and the longest
    free-text column as the description.
    """
    data_rows = [r for r in rows if any((c or "").strip() for c in r)]
    has_header = bool(data_rows) and _looks_like_header(data_rows[0])
    body = data_rows[1:] if has_header else data_rows

    ncols = max((len(r) for r in body), default=0)
    if ncols == 0:
        return ColumnMapping(date_col=0, desc_col=1)

    def col(r, i):
        return (r[i] if i < len(r) else "").strip()

    # Date column: the one where the most body rows parse as a date.
    date_scores = [0] * ncols
    for i in range(ncols):
        for r in body:
            if _parse_date_any(col(r, i)) is not None:
                date_scores[i] += 1
    date_col = max(range(ncols), key=lambda i: date_scores[i]) if ncols else 0

    # Numeric columns (exclude the date column).
    numeric_cols = []
    for i in range(ncols):
        if i == date_col:
            continue
        hits = sum(1 for r in body if _parse_amount_to_cents(col(r, i)) is not None)
        if hits and hits >= max(1, len(body) // 3):
            numeric_cols.append(i)

    amount_mode = "signed"
    amount_col = None
    debit_col = None
    credit_col = None
    if len(numeric_cols) >= 2:
        # Last numeric col is the running balance; the one(s) before it are
        # the money column(s). If exactly one money col -> signed; if two,
        # treat as debit + credit (in column order).
        balance_col = numeric_cols[-1]
        money_cols = [c for c in numeric_cols if c != balance_col]
        if len(money_cols) == 1:
            amount_col = money_cols[0]
        elif len(money_cols) >= 2:
            # Heuristic: do any rows have BOTH of the first two money cols
            # filled? If not, they're mutually-exclusive debit/credit columns.
            d, c = money_cols[0], money_cols[1]
            both = sum(
                1 for r in body
                if _parse_amount_to_cents(col(r, d)) is not None
                and _parse_amount_to_cents(col(r, c)) is not None
            )
            if both == 0:
                amount_mode = "debit_credit"
                debit_col, credit_col = d, c
            else:
                amount_col = money_cols[-1]
    elif len(numeric_cols) == 1:
        # Only one numeric column total — no separate balance; it's the amount.
        amount_col = numeric_cols[0]
    else:
        amount_col = min((i for i in range(ncols) if i != date_col), default=1)

    # Description: the non-date, non-numeric column with the longest text.
    used = {date_col, amount_col, debit_col, credit_col}
    best_desc, best_len = None, -1
    for i in range(ncols):
        if i in used:
            continue
        avg = sum(len(col(r, i)) for r in body) / max(len(body), 1)
        if avg > best_len:
            best_len, best_desc = avg, i
    if best_desc is None:
        best_desc = next((i for i in range(ncols) if i not in used), 0)

    mapping = ColumnMapping(
        date_col=date_col,
        desc_col=best_desc,
        amount_mode=amount_mode,
        amount_col=amount_col,
        debit_col=debit_col,
        credit_col=credit_col,
        has_header=has_header,
    )
    mapping.date_format = detect_date_format(body, date_col)
    return mapping


def file_signature(rows: list[list[str]]) -> str:
    """A stable fingerprint of a file's layout used to recognise the same
    bank's export on future imports.

    Combines the column count, whether there's a header (and its joined
    titles), and the detected date format — enough to tell two banks apart
    without depending on transaction content.
    """
    data_rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not data_rows:
        return "empty"
    has_header = _looks_like_header(data_rows[0])
    body = data_rows[1:] if has_header else data_rows
    ncols = max((len(r) for r in body), default=0)
    header_sig = ""
    if has_header:
        header_sig = "|".join(c.strip().lower() for c in data_rows[0])
    # date format from the first body row's date column guess.
    m = detect_mapping(rows)
    return f"cols={ncols};hdr={int(has_header)}:{header_sig};date={m.date_format};amt={m.amount_mode}"


def parse_with_mapping(rows: list[list[str]], mapping: ColumnMapping) -> ParseResult:
    """Parse raw CSV rows into transactions using an explicit column mapping."""
    result = ParseResult()
    body = list(rows)
    # Drop a header row if the mapping says there is one (first non-empty row).
    if mapping.has_header:
        for idx, r in enumerate(body):
            if any((c or "").strip() for c in r):
                body = body[idx + 1:]
                break

    def col(r, i):
        if i is None or i < 0 or i >= len(r):
            return ""
        return (r[i] or "").strip()

    for line_no, row in enumerate(body, start=1):
        if not row or all((c or "").strip() == "" for c in row):
            continue

        date_raw = col(row, mapping.date_col)
        description = _collapse_ws(col(row, mapping.desc_col))

        if mapping.amount_mode == "debit_credit":
            debit = _parse_amount_to_cents(col(row, mapping.debit_col))
            credit = _parse_amount_to_cents(col(row, mapping.credit_col))
            if debit is None and credit is None:
                # No money on this row — skip silently unless it has a date.
                if _parse_date_with_format(date_raw, mapping.date_format) is not None:
                    result.errors.append((line_no, ",".join(row), "No debit/credit amount"))
                continue
            cents = (credit or 0) - abs(debit or 0)
        else:
            cents = _parse_amount_to_cents(col(row, mapping.amount_col))
            if cents is None:
                if _parse_date_with_format(date_raw, mapping.date_format) is not None:
                    result.errors.append((line_no, ",".join(row), "Bad/missing amount"))
                continue
            if mapping.invert_sign:
                cents = -cents

        # Value Date in the description wins (actual transaction date),
        # else the row's date column.
        ymd = extract_value_date(description) or _parse_date_with_format(
            date_raw, mapping.date_format
        )
        if ymd is None:
            result.errors.append((line_no, ",".join(row), f"Bad date: {date_raw!r}"))
            continue
        year, month, _day = ymd

        key, display = merchant_key_for(description)
        result.transactions.append(
            ParsedTransaction(
                year=year,
                month=month,
                amount_cents=cents,
                description=description,
                merchant_key=key,
                display_name=display,
                is_transfer=is_transfer_description(description),
            )
        )

    return result


def parse_statement_csv(path: str) -> ParseResult:
    """Convenience: read a file, auto-detect its layout, and parse it.

    Used as a fallback / by tests. The interactive importer reads the rows,
    lets the user confirm the detected mapping, then calls
    :func:`parse_with_mapping` directly.
    """
    result = ParseResult()
    try:
        rows = read_raw_rows(path)
    except (OSError, UnicodeDecodeError) as exc:
        result.errors.append((0, "", f"Could not read file: {exc}"))
        return result
    mapping = detect_mapping(rows)
    return parse_with_mapping(rows, mapping)


@dataclass
class MerchantGroup:
    """All transactions sharing one merchant key, summed per (year, month)."""

    merchant_key: str
    display_name: str
    is_transfer: bool
    # (year, month) -> signed cents summed across this merchant's rows.
    monthly_cents: dict[tuple[int, int], int] = field(default_factory=dict)
    txn_count: int = 0

    @property
    def total_cents(self) -> int:
        return sum(self.monthly_cents.values())


def group_by_merchant(transactions: list[ParsedTransaction]) -> dict[str, MerchantGroup]:
    """Collapse parsed transactions into one :class:`MerchantGroup` per key,
    summing signed cents into each ``(year, month)`` bucket."""
    groups: dict[str, MerchantGroup] = {}
    for txn in transactions:
        grp = groups.get(txn.merchant_key)
        if grp is None:
            grp = MerchantGroup(
                merchant_key=txn.merchant_key,
                display_name=txn.display_name,
                is_transfer=txn.is_transfer,
            )
            groups[txn.merchant_key] = grp
        ym = (txn.year, txn.month)
        grp.monthly_cents[ym] = grp.monthly_cents.get(ym, 0) + txn.amount_cents
        grp.txn_count += 1
    return groups
