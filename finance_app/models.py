"""Plain-data types shared by the repository and UI layers.

Everything here is a frozen dataclass or simple enum so values can be passed
across layers without the UI accidentally mutating database state.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional


# ----- Category kind ------------------------------------------------------
# Stored as the integer column ``categories.kind`` so it round-trips cleanly
# through SQLite. ``label()`` is the human-readable form used in the UI.
#
# ``INVESTMENT`` is a third bucket that carries a *signed* value and only
# affects the running-balance / End-of-Month figure plus its own
# Investments cell. A negative investment (loss) is folded into Total
# Expenses; a positive one (gain) is not.
#
# ``DISCREPANCY`` is a free-form reconciliation bucket: signed cents that
# *only* nudge the End-of-Month running balance so the user can match it
# to their actual bank balance. It deliberately does NOT show up in any
# header total (Total Expenses / Total Income / Net / Investments) nor
# in any chart — it's purely a balance-fixer, not a financial event.
class CategoryKind(IntEnum):
    EXPENSE = 0
    INCOME = 1
    INVESTMENT = 2
    DISCREPANCY = 3

    def label(self) -> str:
        if self == CategoryKind.INCOME:
            return "Income"
        if self == CategoryKind.INVESTMENT:
            return "Investment"
        if self == CategoryKind.DISCREPANCY:
            return "Discrepancy"
        return "Expense"


# ----- Project ------------------------------------------------------------
# A "Project" is a self-contained finance ledger (e.g. one bank account).
# A user can have many projects; each owns its own categories and amounts.
@dataclass(frozen=True)
class Project:
    id: int
    name: str
    currency: str
    created_at: str
    is_favorite: bool
    last_opened_at: Optional[str] = None


# ----- Category -----------------------------------------------------------
# Categories are year-scoped — each year owns its own copy so edits in one
# year don't bleed into another. ``sort_order`` controls left-to-right order
# in the Month grid.
@dataclass(frozen=True)
class Category:
    id: int
    project_id: int
    group_id: Optional[int]
    name: str
    kind: CategoryKind
    sort_order: int
    # Temporary categories are merchant columns created by a CSV import
    # that the user hasn't yet mapped to a real category. They show in the
    # editable Month grid (flagged with a red header) but are excluded from
    # every total, breakdown, and chart until assigned. ``merchant_key`` is
    # the normalised key used to match saved import rules.
    is_temporary: bool = False
    merchant_key: Optional[str] = None
