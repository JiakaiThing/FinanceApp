"""SQLite persistence layer for the Finance app.

All database access goes through :class:`FinanceRepository`. The UI never
talks to ``sqlite3`` directly; it asks the repository for typed
:class:`Project` / :class:`Category` objects and integer cents.

Money is stored as integer cents to avoid floating-point rounding errors,
and converted to / from ``"$1,234.56"`` strings only in the UI layer.

Schema (kept in sync by :meth:`FinanceRepository.ensure_created`):
    - ``projects``        one row per finance ledger
    - ``categories``      one row per (project, year, name) — year-scoped
    - ``monthly_amounts`` one row per (category, year, month) — the values
    - ``category_groups`` reserved for future grouping; not used by the UI yet
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .models import Category, CategoryKind, Project


# ----- Aggregate types ----------------------------------------------------
# Lightweight return type for "totals for one (project, year, month)".
# Kept here rather than in models.py because it's an output shape of the
# repository, not a stored entity.
@dataclass(frozen=True)
class MonthTotals:
    expense_cents: int
    income_cents: int

    @property
    def net_cents(self) -> int:
        return self.income_cents - self.expense_cents


class FinanceRepository:
    """Single owner of the SQLite connection.

    Lifetime: created once in :func:`finance_app.ui.main_window.run_app`,
    closed when the window is destroyed. All public methods are safe to
    call from the Tk main thread.
    """

    # ----- Connection lifecycle -------------------------------------------
    def __init__(self, database_path: Path):
        self._conn = sqlite3.connect(str(database_path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON;")

    def close(self) -> None:
        self._conn.close()

    def backup_to(self, dest: Path) -> None:
        """Write a consistent snapshot of the database to *dest*.

        Uses SQLite's online backup API rather than a file copy so the
        backup is safe even while the main connection is open and
        possibly mid-write. Overwrites *dest* if it already exists.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        with sqlite3.connect(str(dest)) as bck:
            self._conn.backup(bck)

    # ----- Schema creation & migrations -----------------------------------
    # ``ensure_created`` runs once at startup. It creates any missing
    # tables/indexes and applies idempotent ALTER TABLE / data migrations
    # so an old database upgrades cleanly to the current schema.
    def ensure_created(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS projects (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          currency TEXT NOT NULL DEFAULT 'AUD',
          is_favorite INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS category_groups (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL,
          name TEXT NOT NULL,
          section INTEGER NOT NULL DEFAULT 0,
          sort_order INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS categories (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL,
          group_id INTEGER,
          name TEXT NOT NULL,
          kind INTEGER NOT NULL,
          sort_order INTEGER NOT NULL DEFAULT 0,
          year INTEGER,
          FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY (group_id) REFERENCES category_groups(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS monthly_amounts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL,
          category_id INTEGER NOT NULL,
          year INTEGER NOT NULL,
          month INTEGER NOT NULL,
          amount_cents INTEGER NOT NULL DEFAULT 0,
          UNIQUE(category_id, year, month),
          FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_monthly_project ON monthly_amounts(project_id, year, month);
        CREATE INDEX IF NOT EXISTS idx_categories_project ON categories(project_id);

        -- Remembered merchant -> category mappings for CSV import. ``scope``
        -- is 'project' (project_id set) or 'global' (project_id NULL, applies
        -- to every project). On a later import a merchant key that matches a
        -- rule is auto-assigned instead of becoming a temporary category.
        CREATE TABLE IF NOT EXISTS merchant_rules (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          scope TEXT NOT NULL DEFAULT 'project',
          project_id INTEGER,
          merchant_key TEXT NOT NULL,
          kind INTEGER NOT NULL,
          final_name TEXT NOT NULL,
          FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_merchant_rules_key ON merchant_rules(merchant_key);

        -- Partially-filled assignments saved from the Assign window so the
        -- user's in-progress Type / name / scope selections survive closing
        -- and reopening it. One row per (project, merchant_key); cleared once
        -- the merchant is fully assigned into a real category.
        CREATE TABLE IF NOT EXISTS merchant_drafts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL,
          merchant_key TEXT NOT NULL,
          kind INTEGER,
          final_name TEXT,
          scope TEXT,
          UNIQUE(project_id, merchant_key),
          FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        -- Remembered CSV column layouts ("format profiles"), one per bank
        -- export style. ``signature`` fingerprints a file's structure so the
        -- same bank's CSV is recognised on future imports and parsed without
        -- re-asking. ``mapping_json`` is a serialised ColumnMapping. Profiles
        -- are global (shared across projects) since a bank's format doesn't
        -- change between accounts.
        CREATE TABLE IF NOT EXISTS csv_format_profiles (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          signature TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          mapping_json TEXT NOT NULL
        );
        """
        with self._conn:
            self._conn.executescript(schema)

        # Temporary-category columns (added by the CSV import feature). Older
        # databases predate these, so add them idempotently.
        cat_cols0 = {row["name"].lower() for row in self._conn.execute("PRAGMA table_info(categories);")}
        if "is_temporary" not in cat_cols0:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE categories ADD COLUMN is_temporary INTEGER NOT NULL DEFAULT 0;"
                )
        if "merchant_key" not in cat_cols0:
            with self._conn:
                self._conn.execute("ALTER TABLE categories ADD COLUMN merchant_key TEXT;")

        cols = {row["name"].lower() for row in self._conn.execute("PRAGMA table_info(projects);")}
        if "is_favorite" not in cols:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE projects ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0;"
                )
        if "last_opened_at" not in cols:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE projects ADD COLUMN last_opened_at TEXT;"
                )

        # Ensure categories have a `year` column (each year is independent),
        # then split any category spanning multiple years into one row per year.
        cat_cols = {row["name"].lower() for row in self._conn.execute("PRAGMA table_info(categories);")}
        if "year" not in cat_cols:
            with self._conn:
                self._conn.execute("ALTER TABLE categories ADD COLUMN year INTEGER;")
        self._migrate_categories_to_year_scope()

        with self._conn:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_categories_project_year ON categories(project_id, year);"
            )

        # Flip any USD project rows to the AUD default.
        # Idempotent: after the first run this matches no rows.
        with self._conn:
            self._conn.execute(
                "UPDATE projects SET currency = 'AUD' WHERE currency = 'USD';"
            )

        # Normalise Expense/Income cents to their magnitude so the kind alone
        # determines sign. Idempotent: matches no rows after the first run.
        # Investment and Discrepancy categories are *exempt* — they carry the
        # user's signed value (positive vs negative) so End of Month tracks
        # gains/losses and reconciliation adjustments correctly.
        invest_kind = int(CategoryKind.INVESTMENT)
        discrepancy_kind = int(CategoryKind.DISCREPANCY)
        with self._conn:
            self._conn.execute(
                """
                UPDATE monthly_amounts
                SET amount_cents = ABS(amount_cents)
                WHERE amount_cents < 0
                  AND category_id IN (
                      SELECT id FROM categories WHERE kind NOT IN (?, ?)
                  );
                """,
                (invest_kind, discrepancy_kind),
            )

    def _migrate_categories_to_year_scope(self) -> None:
        """Ensure every category has a year. A category used in multiple years
        is cloned for each extra year, with the corresponding monthly_amounts
        re-pointed at the clone, so each year owns its own categories.
        """
        unscoped = self._conn.execute(
            "SELECT id, project_id, group_id, name, kind, sort_order FROM categories WHERE year IS NULL;"
        ).fetchall()
        if not unscoped:
            return

        current_year = datetime.now().year
        with self._conn:
            for r in unscoped:
                cat_id = int(r["id"])
                year_rows = self._conn.execute(
                    "SELECT DISTINCT year FROM monthly_amounts WHERE category_id = ? ORDER BY year;",
                    (cat_id,),
                ).fetchall()
                years = [int(yr["year"]) for yr in year_rows]
                if not years:
                    self._conn.execute(
                        "UPDATE categories SET year = ? WHERE id = ?;",
                        (current_year, cat_id),
                    )
                    continue

                # Keep the original row attached to the first year; clone
                # for each additional year and move that year's amounts.
                first_year = years[0]
                self._conn.execute(
                    "UPDATE categories SET year = ? WHERE id = ?;",
                    (first_year, cat_id),
                )
                for yr in years[1:]:
                    cur = self._conn.execute(
                        """
                        INSERT INTO categories
                            (project_id, group_id, name, kind, sort_order, year)
                        VALUES (?, ?, ?, ?, ?, ?);
                        """,
                        (
                            int(r["project_id"]),
                            None if r["group_id"] is None else int(r["group_id"]),
                            str(r["name"]),
                            int(r["kind"]),
                            int(r["sort_order"]),
                            yr,
                        ),
                    )
                    new_id = int(cur.lastrowid)
                    self._conn.execute(
                        "UPDATE monthly_amounts SET category_id = ? WHERE category_id = ? AND year = ?;",
                        (new_id, cat_id, yr),
                    )

    # ----- Projects -------------------------------------------------------
    # CRUD + favourite/recents bookkeeping for the Project entity. The Home
    # view's Favourites & Recents lists are driven entirely off these.
    def list_projects(self) -> list[Project]:
        rows = self._conn.execute(
            "SELECT id, name, currency, created_at, is_favorite, last_opened_at FROM projects ORDER BY id;"
        ).fetchall()
        return [
            Project(
                id=int(r["id"]),
                name=str(r["name"]),
                currency=str(r["currency"]),
                created_at=str(r["created_at"]),
                is_favorite=int(r["is_favorite"]) != 0,
                last_opened_at=None if r["last_opened_at"] is None else str(r["last_opened_at"]),
            )
            for r in rows
        ]

    def create_project(self, name: str, currency: str = "AUD") -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO projects (name, currency, is_favorite, created_at) VALUES (?, ?, 0, ?);",
                (name, currency, now),
            )
        return int(cur.lastrowid)

    def set_project_favorite(self, project_id: int, is_favorite: bool) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE projects SET is_favorite = ? WHERE id = ?;",
                (1 if is_favorite else 0, project_id),
            )

    def touch_project_opened(self, project_id: int) -> None:
        """Stamp ``last_opened_at`` to now so this project floats to the top
        of the Recents list."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                "UPDATE projects SET last_opened_at = ? WHERE id = ?;",
                (now, project_id),
            )

    def delete_project(self, project_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM projects WHERE id = ?;", (project_id,))

    # ----- Categories (year-scoped) ---------------------------------------
    # Each category row belongs to (project_id, year). The same logical
    # "Rent" category in 2025 and 2026 is two database rows so they can be
    # renamed/deleted independently. ``copy_categories_to_year`` seeds a
    # new year by cloning an existing one's category list.
    def list_categories(
        self, project_id: int, year: int, include_temporary: bool = False
    ) -> list[Category]:
        """List categories for a (project, year).

        Temporary import categories are excluded by default so dashboards,
        breakdowns, and charts never count un-assigned merchant columns. The
        editable Month grid passes ``include_temporary=True`` so the user can
        see and assign them.
        """
        temp_clause = "" if include_temporary else " AND is_temporary = 0"
        # Finalized categories first, then temporary import columns, each group
        # ordered by the user's sort order.
        rows = self._conn.execute(
            f"""
            SELECT id, project_id, group_id, name, kind, sort_order,
                   is_temporary, merchant_key
            FROM categories
            WHERE project_id = ? AND year = ?{temp_clause}
            ORDER BY is_temporary, sort_order, id;
            """,
            (project_id, year),
        ).fetchall()
        return [
            Category(
                id=int(r["id"]),
                project_id=int(r["project_id"]),
                group_id=None if r["group_id"] is None else int(r["group_id"]),
                name=str(r["name"]),
                kind=CategoryKind(int(r["kind"])),
                sort_order=int(r["sort_order"]),
                is_temporary=int(r["is_temporary"]) != 0,
                merchant_key=None if r["merchant_key"] is None else str(r["merchant_key"]),
            )
            for r in rows
        ]

    def create_category(
        self,
        project_id: int,
        name: str,
        kind: CategoryKind,
        year: int,
        group_id: Optional[int] = None,
    ) -> int:
        next_row = self._conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_ord FROM categories WHERE project_id = ? AND year = ?;",
            (project_id, year),
        ).fetchone()
        next_order = int(next_row["next_ord"]) if next_row else 0
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO categories (project_id, group_id, name, kind, sort_order, year)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (project_id, group_id, name, int(kind), next_order, year),
            )
        return int(cur.lastrowid)

    def delete_category(self, category_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM categories WHERE id = ?;", (category_id,))

    def set_category_kind(self, category_id: int, kind: CategoryKind) -> None:
        """Switch a category between Expense and Income."""
        with self._conn:
            self._conn.execute(
                "UPDATE categories SET kind = ? WHERE id = ?;",
                (int(kind), category_id),
            )

    def rename_category(self, category_id: int, new_name: str) -> None:
        """Rename a category (whitespace trimmed; empty names are ignored)."""
        name = (new_name or "").strip()
        if not name:
            return
        with self._conn:
            self._conn.execute(
                "UPDATE categories SET name = ? WHERE id = ?;",
                (name, category_id),
            )

    def set_category_sort_orders(self, category_ids: Sequence[int]) -> None:
        """Bulk-assign ``sort_order`` to match the given id order.

        The first id in ``category_ids`` becomes ``sort_order = 0``, the
        second ``= 1``, and so on. Used by the Month grid drag-and-drop
        reorder so the new column order persists across project loads
        (and into the year-copy logic that uses ``ORDER BY sort_order``).
        """
        if not category_ids:
            return
        with self._conn:
            self._conn.executemany(
                "UPDATE categories SET sort_order = ? WHERE id = ?;",
                [(idx, int(cid)) for idx, cid in enumerate(category_ids)],
            )

    def find_year_with_categories(self, project_id: int, prefer_year: int) -> Optional[int]:
        """Return a year that has any categories for this project, closest to
        ``prefer_year`` (preferring earlier years on tie). Returns ``None`` if
        the project has no categories in any year.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT year FROM categories WHERE project_id = ? AND year IS NOT NULL;",
            (project_id,),
        ).fetchall()
        years = [int(r["year"]) for r in rows]
        if not years:
            return None
        # closest by absolute distance, earlier wins on tie
        years.sort(key=lambda y: (abs(y - prefer_year), y))
        return years[0]

    def copy_categories_to_year(
        self, project_id: int, src_year: int, dst_year: int
    ) -> int:
        """Clone the category list (name/kind/group/order) from ``src_year``
        to ``dst_year`` for the given project. Monthly amounts are NOT copied.
        Returns the number of categories copied.
        """
        if src_year == dst_year:
            return 0
        existing_dst = self._conn.execute(
            "SELECT 1 FROM categories WHERE project_id = ? AND year = ? LIMIT 1;",
            (project_id, dst_year),
        ).fetchone()
        if existing_dst:
            return 0

        src_rows = self._conn.execute(
            """
            SELECT group_id, name, kind, sort_order
            FROM categories
            WHERE project_id = ? AND year = ?
            ORDER BY sort_order, id;
            """,
            (project_id, src_year),
        ).fetchall()
        if not src_rows:
            return 0
        with self._conn:
            for r in src_rows:
                self._conn.execute(
                    """
                    INSERT INTO categories
                        (project_id, group_id, name, kind, sort_order, year)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (
                        project_id,
                        None if r["group_id"] is None else int(r["group_id"]),
                        str(r["name"]),
                        int(r["kind"]),
                        int(r["sort_order"]),
                        dst_year,
                    ),
                )
        return len(src_rows)

    # ----- Monthly amounts & summaries -----------------------------------
    # The cell-level data: one (category, year, month) → cents row. Plus
    # the aggregate queries the dashboards & charts use to render summaries
    # without pulling raw rows into Python.
    def get_project_month_total_by_kind(
        self, project_id: int, year: int, month: int, kind: CategoryKind
    ) -> int:
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(ma.amount_cents), 0) AS total
            FROM monthly_amounts ma
            INNER JOIN categories c ON c.id = ma.category_id
            WHERE ma.project_id = ? AND ma.year = ? AND ma.month = ? AND c.kind = ?
              AND c.is_temporary = 0;
            """,
            (project_id, year, month, int(kind)),
        ).fetchone()
        return int(row["total"]) if row else 0

    def get_project_month_expense_total(
        self, project_id: int, year: int, month: int
    ) -> int:
        """Total *spending* cents for a (project, year, month).

        Combines two buckets so the Month / Year Summary "Total Expenses"
        figure reflects everything that flowed *out* in the period:

        * Every Expense-kind cell, taken as ``ABS(amount_cents)``.
        * Every Investment-kind cell whose stored value is **negative**
          (i.e. a loss / withdrawal), taken as ``ABS(amount_cents)``.
          Positive Investment cells (gains) are ignored — they're an
          inflow, not an expense.

        Returns a non-negative int.
        """
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE
                    WHEN c.kind = ? THEN ABS(ma.amount_cents)
                    WHEN c.kind = ? AND ma.amount_cents < 0 THEN ABS(ma.amount_cents)
                    ELSE 0
                END
            ), 0) AS total
            FROM monthly_amounts ma
            INNER JOIN categories c ON c.id = ma.category_id
            WHERE ma.project_id = ? AND ma.year = ? AND ma.month = ?
              AND c.is_temporary = 0;
            """,
            (
                int(CategoryKind.EXPENSE),
                int(CategoryKind.INVESTMENT),
                project_id,
                year,
                month,
            ),
        ).fetchone()
        return int(row["total"]) if row else 0

    def get_year_total_cents(self, category_id: int, year: int) -> int:
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM monthly_amounts
            WHERE category_id = ? AND year = ?;
            """,
            (category_id, year),
        ).fetchone()
        return int(row["total"]) if row else 0

    def get_monthly_amount_cents(self, category_id: int, year: int, month: int) -> Optional[int]:
        row = self._conn.execute(
            """
            SELECT amount_cents FROM monthly_amounts
            WHERE category_id = ? AND year = ? AND month = ?;
            """,
            (category_id, year, month),
        ).fetchone()
        if not row:
            return None
        return int(row["amount_cents"])

    def set_monthly_amount(
        self, project_id: int, category_id: int, year: int, month: int, amount_cents: int
    ) -> None:
        if month < 1 or month > 12:
            raise ValueError("month must be 1..12")
        # Sign rule depends on the category's kind:
        # - Expense / Income       → stored as non-negative cents. The
        #   kind alone decides if the value adds or subtracts in
        #   totals/net, so the user can type ``50`` or ``-50`` in an
        #   Expense cell and get the same answer.
        # - Investment / Discrepancy → the *signed* value is preserved.
        #   Investment lets the user record gains (``+``) and losses
        #   (``-``); Discrepancy is a free-form reconciliation
        #   adjustment that nudges End of Month up or down to match an
        #   external balance.
        amount_cents = int(amount_cents)
        kind_row = self._conn.execute(
            "SELECT kind, is_temporary FROM categories WHERE id = ?;", (category_id,)
        ).fetchone()
        cat_kind = int(kind_row["kind"]) if kind_row else int(CategoryKind.EXPENSE)
        is_temp = bool(kind_row["is_temporary"]) if kind_row else False
        signed_kinds = (int(CategoryKind.INVESTMENT), int(CategoryKind.DISCREPANCY))
        # Temporary import cells keep the raw sign (so the grid mirrors the
        # statement); otherwise the kind decides the sign.
        if not is_temp and cat_kind not in signed_kinds:
            amount_cents = abs(amount_cents)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO monthly_amounts (project_id, category_id, year, month, amount_cents)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(category_id, year, month) DO UPDATE SET
                  project_id = excluded.project_id,
                  amount_cents = excluded.amount_cents;
                """,
                (project_id, category_id, year, month, amount_cents),
            )

    def delete_monthly_amount(self, category_id: int, year: int, month: int) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM monthly_amounts WHERE category_id = ? AND year = ? AND month = ?;",
                (category_id, year, month),
            )

    def get_project_investment_through(
        self, project_id: int, year: int, month: int
    ) -> int:
        """Cumulative signed Investment cents through end of (year, month).

        Mirrors :meth:`get_project_running_balance_through` but only
        sums Investment-kind cells. Used by the Month Summary
        **Investments** header total so it tracks the *lifetime*
        investment outcome (every gain minus every loss across every
        year of the project) the same way End of Month carries the
        running bank balance across years.

        Returns ``0`` if the project has no Investment data on or
        before that date.
        """
        if month < 1 or month > 12:
            raise ValueError("month must be 1..12")
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(ma.amount_cents), 0) AS total
            FROM monthly_amounts ma
            INNER JOIN categories c ON c.id = ma.category_id
            WHERE ma.project_id = ?
              AND c.kind = ?
              AND c.is_temporary = 0
              AND (ma.year < ? OR (ma.year = ? AND ma.month <= ?));
            """,
            (project_id, int(CategoryKind.INVESTMENT), year, year, month),
        ).fetchone()
        return int(row["total"]) if row else 0

    def get_project_running_balance_through(
        self, project_id: int, year: int, month: int
    ) -> int:
        """Cumulative net cents through end of (year, month).

        Sums every monthly amount in the project whose ``(year, month)``
        is on or before the given point in time, applying these per-kind
        rules so the result tracks an actual bank-account balance:

        * Income       → ``+ABS(amount)`` (always inflow)
        * Expense      → ``-ABS(amount)`` (always outflow, sign-stripped)
        * Investment   → ``+amount`` (kept *signed*; positive = gain
          flowing in, negative = loss flowing out)
        * Discrepancy  → ``+amount`` (kept *signed*; a free-form
          reconciliation nudge so End of Month matches an external
          bank balance)

        Used by the Month Summary **End of Month** header total to show a
        running bank-balance-style figure that carries over between
        years. Returns ``0`` if the project has no data on or before
        that date.
        """
        if month < 1 or month > 12:
            raise ValueError("month must be 1..12")
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE c.kind
                    WHEN ? THEN ABS(ma.amount_cents)
                    WHEN ? THEN ma.amount_cents
                    WHEN ? THEN ma.amount_cents
                    ELSE -ABS(ma.amount_cents)
                END
            ), 0) AS balance
            FROM monthly_amounts ma
            INNER JOIN categories c ON c.id = ma.category_id
            WHERE ma.project_id = ?
              AND c.is_temporary = 0
              AND (ma.year < ? OR (ma.year = ? AND ma.month <= ?));
            """,
            (
                int(CategoryKind.INCOME),
                int(CategoryKind.INVESTMENT),
                int(CategoryKind.DISCREPANCY),
                project_id,
                year,
                year,
                month,
            ),
        ).fetchone()
        return int(row["balance"]) if row else 0

    def list_years_with_data(self, project_id: int) -> list[int]:
        """Years that have either monthly amounts OR categories defined.

        Useful for populating the multi-year selector in charts.
        """
        rows = self._conn.execute(
            """
            SELECT year FROM monthly_amounts WHERE project_id = ?
            UNION
            SELECT year FROM categories WHERE project_id = ? AND year IS NOT NULL
            ORDER BY year;
            """,
            (project_id, project_id),
        ).fetchall()
        return sorted({int(r["year"]) for r in rows})

    # ----- CSV import: temp categories, merchant rules & drafts ----------
    # The import feature drops each unknown merchant into a *temporary*
    # category column, then lets the user map it to a real category. A
    # saved mapping (``merchant_rules``) auto-assigns the same merchant on
    # future imports. ``TRANSFER_KIND`` is a sentinel meaning "this merchant
    # is an account transfer — drop it, don't create a category" so we don't
    # have to add a real CategoryKind that would leak into totals/charts.
    TRANSFER_KIND = -1

    def _raw_set_amount(
        self, project_id: int, category_id: int, year: int, month: int, cents: int
    ) -> None:
        """Upsert a cell's cents verbatim — no per-kind sign processing.

        Used by the importer, which has already applied the correct sign for
        the target category's kind. Deletes the row when the result is 0 so
        empty cells stay empty.
        """
        if cents == 0:
            self._conn.execute(
                "DELETE FROM monthly_amounts WHERE category_id = ? AND year = ? AND month = ?;",
                (category_id, year, month),
            )
            return
        self._conn.execute(
            """
            INSERT INTO monthly_amounts (project_id, category_id, year, month, amount_cents)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(category_id, year, month) DO UPDATE SET
              project_id = excluded.project_id,
              amount_cents = excluded.amount_cents;
            """,
            (project_id, category_id, year, month, cents),
        )

    def _kind_signed_contribution(
        self, kind: int, is_temporary: bool, signed_cents: int
    ) -> int:
        """How a signed CSV amount contributes to a cell of the given kind.

        Temporary cells and Investment/Discrepancy keep the raw sign so the
        grid shows +/- exactly as the statement did. Expense/Income store
        magnitude (the kind alone decides in/out), matching manual entry.
        """
        signed_kinds = (int(CategoryKind.INVESTMENT), int(CategoryKind.DISCREPANCY))
        if is_temporary or kind in signed_kinds:
            return signed_cents
        return abs(signed_cents)

    def create_temp_category(
        self, project_id: int, name: str, merchant_key: str, year: int
    ) -> int:
        """Create a temporary (un-assigned) merchant category for a year."""
        next_row = self._conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_ord FROM categories WHERE project_id = ? AND year = ?;",
            (project_id, year),
        ).fetchone()
        next_order = int(next_row["next_ord"]) if next_row else 0
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO categories
                    (project_id, group_id, name, kind, sort_order, year, is_temporary, merchant_key)
                VALUES (?, NULL, ?, ?, ?, ?, 1, ?);
                """,
                (project_id, name, int(CategoryKind.EXPENSE), next_order, year, merchant_key),
            )
        return int(cur.lastrowid)

    def find_temp_category(
        self, project_id: int, merchant_key: str, year: int
    ) -> Optional[int]:
        row = self._conn.execute(
            """
            SELECT id FROM categories
            WHERE project_id = ? AND merchant_key = ? AND year = ? AND is_temporary = 1
            LIMIT 1;
            """,
            (project_id, merchant_key, year),
        ).fetchone()
        return int(row["id"]) if row else None

    def find_real_category(
        self, project_id: int, name: str, kind: int, year: int
    ) -> Optional[int]:
        """Find an existing non-temporary category by (name, kind) for a year,
        case-insensitive, so imports merge into a matching column."""
        row = self._conn.execute(
            """
            SELECT id FROM categories
            WHERE project_id = ? AND year = ? AND is_temporary = 0
              AND kind = ? AND LOWER(name) = LOWER(?)
            ORDER BY id LIMIT 1;
            """,
            (project_id, year, kind, name),
        ).fetchone()
        return int(row["id"]) if row else None

    def import_amount_into_category(
        self, project_id: int, category_id: int, year: int, month: int, signed_cents: int
    ) -> None:
        """Add a signed CSV amount to a cell, accumulating onto any existing
        value and applying the target category's sign rule."""
        kind_row = self._conn.execute(
            "SELECT kind, is_temporary FROM categories WHERE id = ?;", (category_id,)
        ).fetchone()
        if not kind_row:
            return
        kind = int(kind_row["kind"])
        is_temp = int(kind_row["is_temporary"]) != 0
        contribution = self._kind_signed_contribution(kind, is_temp, signed_cents)
        existing = self.get_monthly_amount_cents(category_id, year, month) or 0
        with self._conn:
            self._raw_set_amount(project_id, category_id, year, month, existing + contribution)

    def get_or_create_real_category(
        self, project_id: int, name: str, kind: int, year: int
    ) -> int:
        existing = self.find_real_category(project_id, name, kind, year)
        if existing is not None:
            return existing
        return self.create_category(project_id, name, CategoryKind(kind), year)

    # ----- Merchant rules (remembered mappings) --------------------------
    def suggested_category_names(self, project_id: int) -> list[str]:
        """Category names to offer when assigning a merchant: the project's own
        finalized category names plus every final name saved in a merchant rule
        (global, or scoped to this project). Distinct, case-insensitive sorted,
        excluding the 'Transfer' placeholder."""
        names: dict[str, str] = {}  # lower -> original casing

        for r in self._conn.execute(
            "SELECT DISTINCT name FROM categories WHERE project_id = ? AND is_temporary = 0;",
            (project_id,),
        ):
            n = str(r["name"]).strip()
            if n:
                names.setdefault(n.lower(), n)

        for r in self._conn.execute(
            """
            SELECT DISTINCT final_name FROM merchant_rules
            WHERE scope = 'global' OR (scope = 'project' AND project_id = ?);
            """,
            (project_id,),
        ):
            n = str(r["final_name"]).strip()
            if n and n.lower() != "transfer":
                names.setdefault(n.lower(), n)

        return sorted(names.values(), key=str.lower)

    def get_merchant_rule(
        self, project_id: int, merchant_key: str
    ) -> Optional[tuple[int, str, str]]:
        """Return ``(kind, final_name, scope)`` for a merchant, preferring a
        project-scoped rule over a global one. None if no rule is saved."""
        row = self._conn.execute(
            """
            SELECT kind, final_name, scope FROM merchant_rules
            WHERE merchant_key = ? AND scope = 'project' AND project_id = ?
            LIMIT 1;
            """,
            (merchant_key, project_id),
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                """
                SELECT kind, final_name, scope FROM merchant_rules
                WHERE merchant_key = ? AND scope = 'global'
                LIMIT 1;
                """,
                (merchant_key,),
            ).fetchone()
        if row is None:
            return None
        return int(row["kind"]), str(row["final_name"]), str(row["scope"])

    def upsert_merchant_rule(
        self, scope: str, project_id: Optional[int], merchant_key: str, kind: int, final_name: str
    ) -> None:
        """Save / replace a merchant mapping. ``scope`` is 'project' or
        'global'; ``project_id`` is ignored (stored NULL) for global."""
        with self._conn:
            if scope == "global":
                self._conn.execute(
                    "DELETE FROM merchant_rules WHERE merchant_key = ? AND scope = 'global';",
                    (merchant_key,),
                )
                self._conn.execute(
                    "INSERT INTO merchant_rules (scope, project_id, merchant_key, kind, final_name) VALUES ('global', NULL, ?, ?, ?);",
                    (merchant_key, kind, final_name),
                )
            else:
                self._conn.execute(
                    "DELETE FROM merchant_rules WHERE merchant_key = ? AND scope = 'project' AND project_id = ?;",
                    (merchant_key, project_id),
                )
                self._conn.execute(
                    "INSERT INTO merchant_rules (scope, project_id, merchant_key, kind, final_name) VALUES ('project', ?, ?, ?, ?);",
                    (project_id, merchant_key, kind, final_name),
                )

    def list_merchant_rules(self) -> list[dict]:
        """Every saved merchant mapping rule, with the owning project's name
        for project-scoped rules (None for global). Ordered global-first then
        by merchant key."""
        rows = self._conn.execute(
            """
            SELECT r.id, r.scope, r.project_id, r.merchant_key, r.kind,
                   r.final_name, p.name AS project_name
            FROM merchant_rules r
            LEFT JOIN projects p ON p.id = r.project_id
            ORDER BY r.scope DESC, r.merchant_key COLLATE NOCASE;
            """
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "scope": str(r["scope"]),
                "project_id": None if r["project_id"] is None else int(r["project_id"]),
                "project_name": None if r["project_name"] is None else str(r["project_name"]),
                "merchant_key": str(r["merchant_key"]),
                "kind": int(r["kind"]),
                "final_name": str(r["final_name"]),
            }
            for r in rows
        ]

    def delete_merchant_rule(self, rule_id: int) -> None:
        """Forget a saved merchant mapping so the merchant prompts again on the
        next import. Does not touch already-assigned amounts."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM merchant_rules WHERE id = ?;", (rule_id,)
            )

    def update_merchant_rule(
        self,
        rule_id: int,
        scope: str,
        project_id: Optional[int],
        kind: int,
        final_name: str,
    ) -> None:
        """Edit an existing rule in place (Type, final name, and/or scope).
        For a global rule ``project_id`` is stored NULL."""
        proj = None if scope == "global" else project_id
        with self._conn:
            self._conn.execute(
                """
                UPDATE merchant_rules
                SET scope = ?, project_id = ?, kind = ?, final_name = ?
                WHERE id = ?;
                """,
                (scope, proj, kind, final_name, rule_id),
            )

    # ----- Merchant drafts (in-progress Assign-window selections) --------
    def get_merchant_draft(
        self, project_id: int, merchant_key: str
    ) -> Optional[tuple[Optional[int], Optional[str], Optional[str]]]:
        row = self._conn.execute(
            "SELECT kind, final_name, scope FROM merchant_drafts WHERE project_id = ? AND merchant_key = ?;",
            (project_id, merchant_key),
        ).fetchone()
        if row is None:
            return None
        return (
            None if row["kind"] is None else int(row["kind"]),
            None if row["final_name"] is None else str(row["final_name"]),
            None if row["scope"] is None else str(row["scope"]),
        )

    def save_merchant_draft(
        self,
        project_id: int,
        merchant_key: str,
        kind: Optional[int],
        final_name: Optional[str],
        scope: Optional[str],
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO merchant_drafts (project_id, merchant_key, kind, final_name, scope)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, merchant_key) DO UPDATE SET
                  kind = excluded.kind,
                  final_name = excluded.final_name,
                  scope = excluded.scope;
                """,
                (project_id, merchant_key, kind, final_name, scope),
            )

    def delete_merchant_draft(self, project_id: int, merchant_key: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM merchant_drafts WHERE project_id = ? AND merchant_key = ?;",
                (project_id, merchant_key),
            )

    def clear_scope_only_merchant_drafts(self, project_id: int) -> None:
        """Remove drafts that only stored a Mapping scope with no Type or name."""
        with self._conn:
            self._conn.execute(
                """
                DELETE FROM merchant_drafts
                WHERE project_id = ?
                  AND kind IS NULL
                  AND (final_name IS NULL OR TRIM(final_name) = '');
                """,
                (project_id,),
            )

    # ----- Temp-category listing & assignment ----------------------------
    def list_temp_merchants(self, project_id: int) -> list[tuple[str, str]]:
        """Distinct ``(merchant_key, display_name)`` for every temporary
        category in this project (collapsed across years)."""
        rows = self._conn.execute(
            """
            SELECT merchant_key, name, MIN(year) AS y
            FROM categories
            WHERE project_id = ? AND is_temporary = 1 AND merchant_key IS NOT NULL
            GROUP BY merchant_key
            ORDER BY LOWER(name);
            """,
            (project_id,),
        ).fetchall()
        return [(str(r["merchant_key"]), str(r["name"])) for r in rows]

    def has_temp_categories(self, project_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM categories WHERE project_id = ? AND is_temporary = 1 LIMIT 1;",
            (project_id,),
        ).fetchone()
        return row is not None

    def assign_merchant(
        self, project_id: int, merchant_key: str, kind: int, final_name: str
    ) -> None:
        """Finalise a temporary merchant into a real category (or drop it for
        a Transfer). Applies across every year the merchant appears in,
        merging into an existing matching column and summing values. Saving
        the remembered rule is the caller's responsibility.
        """
        temp_rows = self._conn.execute(
            """
            SELECT id, year FROM categories
            WHERE project_id = ? AND merchant_key = ? AND is_temporary = 1;
            """,
            (project_id, merchant_key),
        ).fetchall()

        for tr in temp_rows:
            temp_id = int(tr["id"])
            year = int(tr["year"])

            if kind == self.TRANSFER_KIND:
                # Transfer: drop the temp category and its amounts entirely.
                with self._conn:
                    self._conn.execute("DELETE FROM categories WHERE id = ?;", (temp_id,))
                continue

            target_id = self.get_or_create_real_category(project_id, final_name, kind, year)
            # Move each month's stored (signed) temp value into the target,
            # re-applying the target kind's sign rule and accumulating.
            amounts = self._conn.execute(
                "SELECT month, amount_cents FROM monthly_amounts WHERE category_id = ?;",
                (temp_id,),
            ).fetchall()
            for am in amounts:
                self.import_amount_into_category(
                    project_id, target_id, year, int(am["month"]), int(am["amount_cents"])
                )
            with self._conn:
                self._conn.execute("DELETE FROM categories WHERE id = ?;", (temp_id,))

        self.delete_merchant_draft(project_id, merchant_key)

    # ----- CSV format profiles (remembered per-bank column layouts) ------
    def get_format_profile(self, signature: str) -> Optional[tuple[str, str]]:
        """Return ``(name, mapping_json)`` for a known file signature, or None."""
        row = self._conn.execute(
            "SELECT name, mapping_json FROM csv_format_profiles WHERE signature = ?;",
            (signature,),
        ).fetchone()
        if row is None:
            return None
        return str(row["name"]), str(row["mapping_json"])

    def save_format_profile(self, signature: str, name: str, mapping_json: str) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO csv_format_profiles (signature, name, mapping_json)
                VALUES (?, ?, ?)
                ON CONFLICT(signature) DO UPDATE SET
                  name = excluded.name,
                  mapping_json = excluded.mapping_json;
                """,
                (signature, name, mapping_json),
            )

    def list_format_profiles(self) -> list[tuple[str, str, str]]:
        rows = self._conn.execute(
            "SELECT signature, name, mapping_json FROM csv_format_profiles ORDER BY name;"
        ).fetchall()
        return [(str(r["signature"]), str(r["name"]), str(r["mapping_json"])) for r in rows]

    def get_month_grid(self, project_id: int, year: int) -> dict[tuple[int, int], int]:
        rows = self._conn.execute(
            """
            SELECT month, category_id, amount_cents
            FROM monthly_amounts
            WHERE project_id = ? AND year = ?;
            """,
            (project_id, year),
        ).fetchall()
        out: dict[tuple[int, int], int] = {}
        for r in rows:
            out[(int(r["month"]), int(r["category_id"]))] = int(r["amount_cents"])
        return out

