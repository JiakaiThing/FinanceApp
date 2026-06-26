"""Main window for the Finance app — the entry point for the whole UI.

Two top-level views, swapped via :meth:`MainWindow._show_home` /
``_show_project``:

    Home view
        Project picker — Create / Favourites / Recents lists, plus the
        "Backup database" action and a label showing the DB path.

    Project view (one project open at a time)
        Year + Month picker → Add Entry row → a vertically-scrollable
        column of collapsible sections:
            - Month Summary    totals + the editable 12-row grid
            - Month Breakdown  per-category totals for the picked month
            - Year Summary     12-row monthly totals across the year
            - Year Breakdown   per-category totals across the year
            - Charts           matplotlib panels, one per selected project

Money is parsed/formatted via the helpers near the top of this file
(``_parse_money_to_cents`` / ``_money_from_cents``) so the rest of the
class only deals with integer cents — same convention as the repository.
"""
from __future__ import annotations

import sys
from pathlib import Path
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import font as tkfont, messagebox, simpledialog, ttk
from typing import Optional, Sequence

# Allow running this file directly (e.g. `python finance_app/ui/main_window.py`)
# while still supporting normal package execution (`python main.py`).
if __name__ == "__main__" and (__package__ is None or __package__ == ""):
    repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root))

try:
    from ..app_paths import database_path, ensure_app_data_exists
    from ..csv_import import (
        ColumnMapping,
        assign_fingerprints,
        detect_mapping,
        file_signature,
        parse_with_mapping,
        read_raw_rows,
    )
    from ..models import CategoryKind, Project
    from ..repository import FinanceRepository
    from .charts import ChartsSection
    from .widgets import (
        CollapsibleSection,
        TreeviewCellEditor,
        TreeviewCellHighlight,
        TreeviewGridlines,
        TreeviewTempHeaders,
        VerticalScrolledFrame,
    )
except ImportError:  # running as a script (no package context)
    from finance_app.app_paths import database_path, ensure_app_data_exists
    from finance_app.csv_import import (
        ColumnMapping,
        assign_fingerprints,
        detect_mapping,
        file_signature,
        parse_with_mapping,
        read_raw_rows,
    )
    from finance_app.models import CategoryKind, Project
    from finance_app.repository import FinanceRepository
    from finance_app.ui.charts import ChartsSection
    from finance_app.ui.widgets import (
        CollapsibleSection,
        TreeviewCellEditor,
        TreeviewCellHighlight,
        TreeviewGridlines,
        TreeviewTempHeaders,
        VerticalScrolledFrame,
    )


# ----- Constants & money helpers -----------------------------------------
# Three short helpers used everywhere: integer cents <-> "$1,234.56" string.
# Keeping these private to this module means the repository never has to
# care about formatting and the UI never deals with floats internally.
EN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Set by ``_create_root`` at startup: True when tkinterdnd2 + the native
# tkdnd extension loaded, so the CSV import drop-zone can register a drop
# target. When False the importer shows a browse-button-only UI.
DND_AVAILABLE = False


def _money_from_cents(cents: int) -> str:
    # Format negatives as ``-$50.00`` rather than the default ``$-50.00``
    # so signed Investment values, negative Net, etc. stay readable.
    if cents < 0:
        return f"-${abs(cents) / 100.0:,.2f}"
    return f"${cents / 100.0:,.2f}"


def _money_input_from_cents(cents: int) -> str:
    # The string shown inside the edit Entry for a cell.
    return _money_from_cents(cents)


def _parse_money_to_cents(raw: str) -> Optional[int]:
    s = raw.strip()
    if s == "":
        return None
    # tolerate "$" prefix and common negative formats like "(12.34)"
    s = s.replace("$", "").strip()
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()
    try:
        # allow commas
        s = s.replace(",", "")
        value = float(s)
    except ValueError:
        return None
    if neg:
        value = -value
    return int(round(value * 100))


# ----- UI state -----------------------------------------------------------
# Tiny mutable container threaded through MainWindow. It tracks the
# currently-open project + the year/month that all dashboards & charts
# render against. Everything here is observable via simple attribute
# reads; we don't bother with a full reactive system.
@dataclass
class UiState:
    selected_project: Optional[Project] = None
    year: int = datetime.now().year
    month: int = datetime.now().month


# ----- MainWindow --------------------------------------------------------
# Owns both the Home view and the Project view, plus all callbacks that
# reach into FinanceRepository. Methods are grouped by responsibility via
# the section banners further down (Home view, Project view, dashboards,
# clipboard helpers, sorting, etc.).
class MainWindow(ttk.Frame):
    def __init__(self, master: tk.Tk, repo: FinanceRepository):
        super().__init__(master)
        self._repo = repo
        self._state = UiState()
        self._style = ttk.Style(master)
        self._red = "#d1242f"
        self._green = "#1a7f37"
        self._gridline_color = (
            self._style.lookup("Treeview.Heading", "bordercolor")
            or self._style.lookup("Treeview", "bordercolor")
            or "#d0d0d0"
        )
        # Colour of the native Treeview outer frame (a darker grey than the
        # interior gridlines), used for overlay borders on the table's edge.
        self._outer_border_color = "#82878f"
        self._categories: list = []

        # Undo stack for Month-grid cell edits. Each entry is a dict
        # describing one cell mutation: previous cents (None means
        # "cell was empty") plus the (project, year, month, category)
        # coordinates needed to restore it. Cleared whenever the
        # current project or year changes (different DB scope /
        # different per-year category list — see :meth:`_clear_undo`).
        self._undo_stack: list[dict] = []
        self._redo_stack: list[dict] = []
        # Re-entrancy guards so undo/redo replays don't push new history.
        self._undoing: bool = False
        self._redoing: bool = False

        self._container = ttk.Frame(self)
        self._container.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)

        self._home = self._build_home(self._container)
        self._project = self._build_project(self._container)

        self._show_home()
        self._reload_projects()

        # Global "clear selection" UX:
        # - Esc clears highlighted selections
        # - Clicking empty space clears highlighted selections
        self.bind_all("<Escape>", self._clear_selection_event, add=True)
        self.bind_all("<Button-1>", self._clear_selection_on_background_click, add=True)
        # Ctrl+Z undoes the most recent Month-grid cell edit. Bound on
        # ``bind_all`` so it works no matter which widget has focus,
        # except while a cell editor is open (handled inside the
        # callback) — there we let the Entry's own key handling win.
        for seq in ("<Control-z>", "<Control-Z>"):
            self.bind_all(seq, self._undo_grid_edit, add=True)
        for seq in ("<Control-y>", "<Control-Y>"):
            self.bind_all(seq, self._redo_grid_edit, add=True)

    # ----- Global selection clearing --------------------------------------
    # Pressing Esc or clicking on neutral background space clears all blue
    # selections (Listbox rows, Treeview rows, single-cell highlights) so
    # the user can "deselect" without having to navigate back to the list.
    def _clear_selection_event(self, _event: tk.Event) -> None:
        self._clear_all_selections()

    def _clear_selection_on_background_click(self, event: tk.Event) -> None:
        w = event.widget
        # `event.widget` can be a Tcl path string for internals like a Combobox
        # popdown listbox. In that case, never clear selection — those clicks
        # belong to the dropdown's own selection logic.
        if isinstance(w, str):
            return

        # Skip interactive widgets that own click semantics.
        if isinstance(
            w,
            (
                ttk.Treeview,
                tk.Listbox,
                tk.Entry,
                ttk.Entry,
                ttk.Combobox,
                ttk.Button,
                tk.Button,
                ttk.Scrollbar,
                ttk.Notebook,
                ttk.Menubutton,
                tk.Menu,
            ),
        ):
            return

        # If the click happened inside a Treeview or Combobox (e.g. on internal
        # child widget like cell highlight Label or popdown), don't clear.
        parent = w
        while parent is not None:
            if isinstance(parent, (ttk.Treeview, ttk.Combobox)):
                return
            parent = getattr(parent, "master", None)
        self._clear_all_selections()

    def _clear_all_selections(self) -> None:
        # Remove focus so widgets don't keep selection highlight.
        try:
            self.focus_set()
        except Exception:
            pass

        # Listboxes
        for lb_name in ("_favorites_list", "_recents_list"):
            lb = getattr(self, lb_name, None)
            if lb is not None:
                try:
                    lb.selection_clear(0, tk.END)
                except Exception:
                    pass

        # Treeviews row selection
        for tv_name in ("_grid_tree", "_year_tree", "_break_tree", "_month_break_tree"):
            tv = getattr(self, tv_name, None)
            if tv is not None:
                try:
                    sel = tv.selection()
                    if sel:
                        tv.selection_remove(sel)
                except Exception:
                    pass

        # Single-cell highlight overlays
        self._clear_cell_highlights()

    # =====================================================================
    # Home view
    # =====================================================================
    # Project picker: a "Create New Project" entry, two listboxes
    # (Favourites & Recents), and the DB path + Backup button at the
    # bottom. Right-click on a project gives Favourite/Delete actions.
    def _build_home(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent)

        title = ttk.Label(frame, text="Create New Project", font=("Segoe UI", 14, "bold"))
        title.pack(anchor="w")

        create_row = ttk.Frame(frame)
        create_row.pack(fill=tk.X, pady=(8, 18))
        self._new_project_var = tk.StringVar()
        entry = ttk.Entry(create_row, textvariable=self._new_project_var, justify="center")
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<Return>", lambda _e: self._create_project())
        ttk.Button(create_row, text="Create Project", command=self._create_project).pack(
            side=tk.LEFT, padx=(10, 0)
        )

        lists = ttk.Frame(frame)
        lists.pack(fill=tk.BOTH, expand=True)
        lists.columnconfigure(0, weight=1)
        lists.columnconfigure(1, weight=1)

        fav_box = ttk.Labelframe(lists, text="Favourites")
        fav_box.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        rec_box = ttk.Labelframe(lists, text="Recents")
        rec_box.grid(row=0, column=1, sticky="nsew")

        self._favorites_list = tk.Listbox(fav_box, height=12)
        self._favorites_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._recents_list = tk.Listbox(rec_box, height=12)
        self._recents_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        for lb in (self._favorites_list, self._recents_list):
            lb.bind("<Double-Button-1>", self._open_selected_project_from_list, add=True)
            lb.bind("<Button-3>", self._open_project_context_menu, add=True)

        db_row = ttk.Frame(frame)
        db_row.pack(fill=tk.X, pady=(12, 0))
        self._db_path_label = ttk.Label(db_row, text=f"DB: {database_path()}", foreground="#666")
        self._db_path_label.pack(side=tk.LEFT, anchor="w")
        ttk.Button(db_row, text="Backup database", command=self._backup_database).pack(
            side=tk.RIGHT
        )

        return frame

    def _backup_database(self) -> None:
        """Save a timestamped copy of the database next to the original."""
        src = database_path()
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = src.with_name(f"finance.backup-{ts}.db")
        try:
            self._repo.backup_to(dest)
        except Exception as exc:
            messagebox.showerror(
                title="Backup failed",
                message=f"Could not back up the database.\n\n{exc}",
            )
            return
        messagebox.showinfo(
            title="Backup created",
            message=f"Database backed up to:\n\n{dest}",
        )

    def _reload_projects(self) -> None:
        projects = self._repo.list_projects()
        self._projects = projects
        self._favorites = [p for p in projects if p.is_favorite]
        # Recents: most recently opened first. Projects that have never been
        # opened fall back to creation order (newest id first) at the bottom.
        # Two stable sorts: first by recency desc within their group, then
        # partition opened-vs-never-opened so opened projects float to top.
        self._recents = sorted(
            projects,
            key=lambda p: (p.last_opened_at or "", p.id),
            reverse=True,
        )
        self._recents.sort(key=lambda p: 0 if p.last_opened_at else 1)

        self._favorites_list.delete(0, tk.END)
        for p in self._favorites:
            self._favorites_list.insert(tk.END, p.name)

        self._recents_list.delete(0, tk.END)
        for p in self._recents:
            self._recents_list.insert(tk.END, p.name)

    def _create_project(self) -> None:
        name = self._new_project_var.get().strip()
        if not name:
            return
        pid = self._repo.create_project(name)
        self._new_project_var.set("")
        self._reload_projects()
        project = next((p for p in self._projects if p.id == pid), None)
        if project:
            self._open_project(project)

    def _open_selected_project_from_list(self, event: tk.Event) -> None:
        widget = event.widget
        if widget is self._favorites_list:
            idx = self._favorites_list.curselection()
            if not idx:
                return
            self._open_project(self._favorites[int(idx[0])])
            return
        if widget is self._recents_list:
            idx = self._recents_list.curselection()
            if not idx:
                return
            self._open_project(self._recents[int(idx[0])])

    def _open_project_context_menu(self, event: tk.Event) -> None:
        widget = event.widget
        if widget not in (self._favorites_list, self._recents_list):
            return

        index = widget.nearest(event.y)
        if index < 0:
            return

        project = self._favorites[index] if widget is self._favorites_list else self._recents[index]

        menu = tk.Menu(self, tearoff=0)
        fav_label = "Unfavourite" if project.is_favorite else "Favourite"
        menu.add_command(
            label=fav_label,
            command=lambda: (self._repo.set_project_favorite(project.id, not project.is_favorite), self._reload_projects()),
        )
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: self._delete_project(project))
        menu.tk_popup(event.x_root, event.y_root)

    def _delete_project(self, project: Project) -> None:
        ok = messagebox.askyesno(
            title="Confirm delete",
            message=f"Delete project \"{project.name}\"?\nThis cannot be undone.",
        )
        if not ok:
            return
        self._repo.delete_project(project.id)
        if self._state.selected_project and self._state.selected_project.id == project.id:
            self._state.selected_project = None
        self._reload_projects()
        self._show_home()

    # =====================================================================
    # Project view
    # =====================================================================
    # Header (Back + project title + year/month picker), the "Add Entry"
    # box, and a vertically-scrollable column of CollapsibleSections:
    # Month Summary -> Month Breakdown -> Year Summary -> Year Breakdown
    # -> Charts. Each section's body is built by its own _build_* method
    # below.
    def _build_project(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent)

        header = ttk.Frame(frame)
        header.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(header, text="← Back", command=self._show_home).pack(side=tk.LEFT)
        self._project_title = ttk.Label(header, text="", font=("Segoe UI", 16, "bold"))
        self._project_title.pack(side=tk.LEFT, padx=(12, 0))

        picker = ttk.Frame(header)
        picker.pack(side=tk.RIGHT)
        ttk.Label(picker, text="Year").pack(side=tk.LEFT, padx=(0, 6))
        self._year_var = tk.IntVar(value=self._state.year)
        self._year_combo = ttk.Combobox(
            picker, width=8, textvariable=self._year_var, state="readonly", justify="center"
        )
        self._year_combo.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(picker, text="Month").pack(side=tk.LEFT, padx=(0, 6))
        self._month_var = tk.StringVar(value=EN_MONTHS[self._state.month - 1])
        self._month_combo = ttk.Combobox(
            picker, width=6, values=EN_MONTHS, textvariable=self._month_var, state="readonly", justify="center"
        )
        self._month_combo.pack(side=tk.LEFT)

        self._year_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_year_month_changed(), add=True)
        self._month_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_year_month_changed(), add=True)

        add_box = ttk.Labelframe(frame, text="Add Entry")
        add_box.pack(fill=tk.X, pady=(0, 10))
        add_row = ttk.Frame(add_box)
        add_row.pack(fill=tk.X, padx=10, pady=10)

        self._new_cat_name = tk.StringVar()
        self._new_cat_kind = tk.StringVar(value="Expense")
        self._new_cat_value = tk.StringVar()

        ttk.Label(add_row, text="Category name").grid(row=0, column=0, sticky="w")
        ttk.Entry(add_row, textvariable=self._new_cat_name, width=24, justify="center").grid(
            row=1, column=0, sticky="w", padx=(0, 12)
        )

        ttk.Label(add_row, text="Type").grid(row=0, column=1, sticky="w")
        # Four kinds:
        # - Investment — signed; shown in its own header total and dark
        #   blue on charts.
        # - Discrepancy — signed; only nudges End of Month so the user
        #   can reconcile against an external bank balance. It does NOT
        #   appear in any header total or chart.
        ttk.Combobox(
            add_row,
            textvariable=self._new_cat_kind,
            values=["Expense", "Income", "Investment", "Discrepancy"],
            width=14,
            state="readonly",
            justify="center",
        ).grid(row=1, column=1, sticky="w", padx=(0, 12))

        ttk.Label(add_row, text="Value").grid(row=0, column=2, sticky="w")
        self._new_value_entry = ttk.Entry(add_row, textvariable=self._new_cat_value, width=14, justify="center")
        self._new_value_entry.grid(row=1, column=2, sticky="w", padx=(0, 12))
        self._new_value_entry.bind("<FocusOut>", lambda _e: self._format_value_entry_var(self._new_cat_value), add=True)
        self._new_value_entry.bind(
            "<Return>", lambda _e: (self._format_value_entry_var(self._new_cat_value), self._add_category_and_value()), add=True
        )

        ttk.Button(add_row, text="Add", command=self._add_category_and_value).grid(
            row=1, column=3, sticky="w"
        )

        # Right-hand area of the Add Entry box: CSV import controls plus the
        # "assign temporary categories" notice. Column 4 absorbs the leftover
        # width so this sits to the right of the entry fields.
        add_row.columnconfigure(4, weight=1)
        self._build_import_area(add_row)

        # ---- Stacked collapsible sections inside a vertically scrollable page ----
        # Each section packs fill=X (no vertical expand) and shows all of its
        # rows naturally via the Treeview's `height` parameter. The outer
        # canvas scrolls if total content exceeds window height.
        self._sections_scroll = VerticalScrolledFrame(frame)
        self._sections_scroll.pack(fill=tk.BOTH, expand=True)
        sections_root = self._sections_scroll.inner

        self._section_month_summary = CollapsibleSection(
            sections_root, "Month Summary", expanded=True, expand_when_open=False
        )
        self._section_month_summary.pack(fill=tk.X, pady=(0, 8))
        self._build_month_summary_section(self._section_month_summary.body)

        self._section_month_breakdown = CollapsibleSection(
            sections_root, "Month Breakdown", expanded=True, expand_when_open=False
        )
        self._section_month_breakdown.pack(fill=tk.X, pady=(0, 8))
        self._build_month_breakdown_section(self._section_month_breakdown.body)

        self._section_year_summary = CollapsibleSection(
            sections_root, "Year Summary", expanded=True, expand_when_open=False
        )
        self._section_year_summary.pack(fill=tk.X, pady=(0, 8))
        self._build_year_summary(self._section_year_summary.body)

        self._section_year_breakdown = CollapsibleSection(
            sections_root, "Year Breakdown", expanded=True, expand_when_open=False
        )
        self._section_year_breakdown.pack(fill=tk.X, pady=(0, 8))
        self._build_year_breakdown(self._section_year_breakdown.body)

        self._section_charts = CollapsibleSection(
            sections_root, "Charts", expanded=True, expand_when_open=False
        )
        self._section_charts.pack(fill=tk.X, pady=(0, 8))
        self._charts = ChartsSection(
            self._section_charts.body, self._repo, get_state=lambda: self._state
        )

        for section in (
            self._section_month_summary,
            self._section_month_breakdown,
            self._section_year_summary,
            self._section_year_breakdown,
            self._section_charts,
        ):
            section.set_on_toggle(lambda _exp: self._on_section_toggled())

        return frame

    def _on_section_toggled(self) -> None:
        """Keep scroll content anchored under Add Entry after collapse/expand."""
        if hasattr(self, "_sections_scroll"):
            self.after_idle(self._sections_scroll.snap_to_top)

    # ----- Month Summary section -----------------------------------------
    # Top: month dropdown + Total Expenses / Total Income / Net / End of
    # Month (four equal cells on the *left half* of the section width).
    # Bottom: the editable 12-row x N-category grid (the heart of the
    # data-entry experience).
    def _build_month_summary_section(self, parent: ttk.Frame) -> None:
        """Month Summary section: month picker + totals + 12-month editable grid."""
        # Top: month dropdown + live totals for the selected month (or All).
        summary_top = ttk.Frame(parent)
        summary_top.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(summary_top, text="Month").pack(side=tk.LEFT)
        self._summary_month_var = tk.StringVar(value=EN_MONTHS[self._state.month - 1])
        # "All" sums every month of the selected year for both the
        # totals strip AND the Month Breakdown table below; specific
        # months scope to that month only.
        self._summary_month_combo = ttk.Combobox(
            summary_top,
            width=6,
            values=["All"] + EN_MONTHS,
            textvariable=self._summary_month_var,
            state="readonly",
            justify="center",
        )
        self._summary_month_combo.pack(side=tk.LEFT, padx=(8, 0))
        self._summary_month_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_dashboards(), add=True)

        # ``sum_wrap`` splits the section into left + right halves so
        # the totals strip never spans the whole width. The split is
        # responsive: on narrow windows the strip claims 3/4 of the
        # row so titles stay readable; on wider windows it relaxes
        # back to a 1/2 / 1/2 split.
        sum_wrap = ttk.Frame(parent)
        sum_wrap.pack(fill=tk.X, pady=(0, 10))
        sum_row = ttk.Frame(sum_wrap)
        sum_row.grid(row=0, column=0, sticky="ew")
        self._enable_responsive_summary_split(sum_wrap, "month_summary_split")
        self._spend_var = tk.StringVar(value="0.00")
        self._income_var = tk.StringVar(value="0.00")
        self._net_var = tk.StringVar(value="0.00")
        # Investments are tracked separately from Expenses: they subtract
        # from End of Month (so the running balance reflects "money out
        # the door"), but do *not* count toward the Total Expenses cell
        # next door. Coloured dark blue on charts.
        self._invest_var = tk.StringVar(value="0.00")
        # ``Start of Month`` and ``End of Month`` are running
        # bank-balance figures that carry across years. Start of Month
        # is the running balance through the *end of the previous
        # month* (so May → April 30, January → December 31 of the
        # previous year). Both use the default text colour (they
        # aren't single-month signed totals, so red/green styling
        # doesn't apply).
        self._som_var = tk.StringVar(value="0.00")
        self._eom_var = tk.StringVar(value="0.00")
        self._spend_value_label: Optional[ttk.Label] = None
        self._income_value_label: Optional[ttk.Label] = None
        self._net_value_label: Optional[ttk.Label] = None
        self._invest_value_label: Optional[ttk.Label] = None
        self._som_value_label: Optional[ttk.Label] = None
        self._eom_value_label: Optional[ttk.Label] = None
        # Income leads, then Expenses, then Net / Investments / Start /
        # End of Month — keeps the "money in vs money out" reading
        # order natural and matches the order used in the pie charts.
        cells = [
            ("Total Income", self._income_var),
            ("Total Expenses", self._spend_var),
            ("Net", self._net_var),
            ("Investments", self._invest_var),
            ("Start of Month", self._som_var),
            ("End of Month", self._eom_var),
        ]
        (
            self._income_value_label,
            self._spend_value_label,
            self._net_value_label,
            self._invest_value_label,
            self._som_value_label,
            self._eom_value_label,
        ) = self._build_summary_metric_row(sum_row, cells)

        # Read-only report of import merchants for the selected month.
        ttk.Button(
            sum_row,
            text="Manage Month Mapping",
            command=self._open_manage_month_mapping_window,
        ).grid(row=0, column=len(cells), sticky="w", padx=(8, 0), pady=(20, 0))

        # Editable 12-month grid (former "Month Breakdown" tab table)
        grid_wrap = ttk.Frame(parent)
        grid_wrap.pack(fill=tk.BOTH, expand=True)
        self._build_month_grid(grid_wrap)

        return None

    # ----- Editable 12-month grid (inside Month Summary) -----------------
    # The Treeview is destroyed and rebuilt whenever the category list
    # changes (e.g. delete or rename) so deleted columns can't leave
    # ghost slots in Tcl's column registry.
    def _build_month_grid(self, parent: ttk.Frame) -> None:
        """Editable 12-month grid (lives inside the Month Summary section)."""
        self._grid_wrap = ttk.Frame(parent)
        self._grid_wrap.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
        self._grid_wrap.rowconfigure(0, weight=1)
        self._grid_wrap.columnconfigure(0, weight=1)

        self._apply_tree_gridlines("MonthGrid.Treeview")
        self._suppress_row_selection_color("MonthGrid.Treeview")
        self._style.configure("MonthGrid.Treeview", rowheight=30)

        self._grid_ysb = ttk.Scrollbar(self._grid_wrap, orient="vertical")
        self._grid_ysb.grid(row=0, column=1, sticky="ns")
        self._grid_xsb = ttk.Scrollbar(self._grid_wrap, orient="horizontal")
        self._grid_xsb.grid(row=1, column=0, sticky="ew")

        # Tree is recreated when columns change so deleted categories never leave
        # ghost columns in the ttk.Treeview widget.
        self._recreate_month_grid_tree(["month"])

    def _destroy_month_grid_overlays(self) -> None:
        """Remove gridline frames and the tree so we can rebuild a clean grid."""
        if hasattr(self, "_grid_lines"):
            for ln in self._grid_lines._v_lines + self._grid_lines._h_lines:
                try:
                    ln.destroy()
                except Exception:
                    pass
            self._grid_lines._v_lines.clear()
            self._grid_lines._h_lines.clear()
        if hasattr(self, "_grid_tree"):
            try:
                if self._grid_tree.winfo_exists():
                    self._grid_tree.destroy()
            except Exception:
                pass

    def _recreate_month_grid_tree(self, cols: list[str]) -> None:
        """Build a fresh Treeview with exactly ``cols`` — no stale Tcl columns."""
        wrap = self._grid_wrap
        self._destroy_month_grid_overlays()

        self._grid_tree = ttk.Treeview(
            wrap,
            columns=cols,
            show="headings",
            style="MonthGrid.Treeview",
            height=12,
        )
        self._grid_tree.grid(row=0, column=0, sticky="nsew")
        self._grid_tree["displaycolumns"] = cols
        self._configure_month_grid_columns(cols)

        self._grid_lines = TreeviewGridlines(wrap, self._grid_tree, self._gridline_color)
        # Light-red header overlay marking temporary (un-assigned import)
        # columns. ``#fdecea`` matches the import notice banner.
        self._temp_headers = TreeviewTempHeaders(
            wrap,
            self._grid_tree,
            "#fdecea",
            gridlines=self._grid_lines,
            on_rightclick=self._on_temp_header_rightclick,
            top_border_color=self._outer_border_color,
        )

        def _redraw_overlays() -> None:
            self._grid_lines.redraw()
            if hasattr(self, "_temp_headers"):
                self._temp_headers.redraw()
            if hasattr(self, "_grid_highlight"):
                self._grid_highlight.reposition()

        def on_yscroll(first: str, last: str) -> None:
            self._grid_ysb.set(first, last)
            _redraw_overlays()

        def on_xscroll(first: str, last: str) -> None:
            self._grid_xsb.set(first, last)
            _redraw_overlays()

        def yview_cmd(*args):
            self._grid_tree.yview(*args)
            _redraw_overlays()

        def xview_cmd(*args):
            self._grid_tree.xview(*args)
            _redraw_overlays()

        self._grid_ysb.configure(command=yview_cmd)
        self._grid_xsb.configure(command=xview_cmd)
        self._grid_tree.configure(yscrollcommand=on_yscroll, xscrollcommand=on_xscroll)

        self._grid_highlight = TreeviewCellHighlight(
            self._grid_tree,
            on_delete=self._delete_grid_cell,
            on_copy=self._copy_grid_cell,
            on_cut=self._cut_grid_cell,
            on_paste=self._paste_grid_cell,
        )
        self._grid_editor = TreeviewCellEditor(
            self._grid_tree,
            on_commit=self._commit_grid_cell,
            value_transform=self._normalize_money_input,
            highlight=self._grid_highlight,
            on_navigate=self._neighbor_grid_cell,
        )
        self._grid_highlight._on_double_click = self._grid_editor.start_edit_cell
        self._grid_highlight._on_typing = self._start_typing_grid_cell
        self._grid_tree.bind("<Button-3>", self._open_month_grid_context_menu, add=True)
        # Spreadsheet-style keyboard on the Month grid (when the in-cell
        # editor is not open): Enter -> edit, arrows -> move selection,
        # Tab / Shift+Tab -> move horizontally. Tab is also bound here
        # so a ``return "break"`` stops Tk's default focus-traversal
        # from moving keyboard focus out of the table. ``ISO_Left_Tab``
        # is an X11 keysym (used on Linux for Shift+Tab); silently
        # skipped on Windows where Tk rejects it as unknown.
        for keysym in ("Up", "Down", "Left", "Right", "Return", "Tab", "ISO_Left_Tab"):
            try:
                self._grid_tree.bind(
                    f"<KeyPress-{keysym}>",
                    self._on_grid_tree_key,
                    add=True,
                )
            except tk.TclError:
                continue

        # Drag-and-drop column reordering: press on a category heading
        # and drag to another to reshuffle. See
        # :meth:`_on_grid_heading_press` / ``_motion`` / ``_release``.
        self._grid_drag = {"col": None, "started": False, "start_x": 0}
        self._grid_tree.bind("<ButtonPress-1>", self._on_grid_heading_press, add=True)
        self._grid_tree.bind("<B1-Motion>", self._on_grid_heading_motion, add=True)
        self._grid_tree.bind("<ButtonRelease-1>", self._on_grid_heading_release, add=True)

        try:
            self._grid_tree.xview_moveto(0.0)
            self._grid_tree.yview_moveto(0.0)
        except Exception:
            pass

    def _configure_month_grid_columns(self, cols: list[str]) -> None:
        tree = self._grid_tree
        cats_by_col = {f"cat_{c.id}": c for c in self._categories}
        # Use the actual heading font to measure how much pixel-width each
        # category name needs, so long names like "Cash/Transfer to other
        # bank" don't get truncated by a fixed column width.
        try:
            heading_font = tkfont.nametofont("TkHeadingFont")
        except tk.TclError:
            heading_font = tkfont.nametofont("TkDefaultFont")
        # Each column also needs to fit a typical money value (e.g.
        # "$1,234.56"). 120 px is comfortable; we keep that as the floor.
        BASE_MIN = 120
        # Sort-arrow / cell-padding fudge factor on top of measured text.
        HEADING_PADDING = 28
        for col in cols:
            if col == "month":
                tree.heading("month", text="Month", anchor="center")
                # Month column is fixed-width (it's just labels Jan..Dec).
                tree.column("month", width=80, anchor="center", stretch=False, minwidth=80)
            elif col.startswith("cat_"):
                cat = cats_by_col.get(col)
                text = cat.name if cat else col
                tree.heading(col, text=text, anchor="center")
                measured = heading_font.measure(text) + HEADING_PADDING
                width = max(BASE_MIN, measured)
                # Category columns absorb leftover horizontal space so the
                # table never shows a blank slot after the last category.
                # `minwidth` is set to the same dynamic width so a long
                # category name is never truncated.
                tree.column(col, width=width, anchor="center", stretch=True, minwidth=width)

    # ----- Month Breakdown section ---------------------------------------
    # Read-only table of per-category totals for the currently-selected
    # month. Same layout as Year Breakdown but scoped to one month.
    def _build_month_breakdown_section(self, parent: ttk.Frame) -> None:
        """New Month Breakdown section: per-category totals for the selected month.

        Same layout as Year Breakdown but for a single month.
        """
        cols = ("name", "type", "total")
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self._month_break_tree = ttk.Treeview(
            wrap, columns=cols, show="headings", style="MonthBreakdown.Treeview", height=8
        )
        self._apply_tree_gridlines("MonthBreakdown.Treeview")
        self._suppress_row_selection_color("MonthBreakdown.Treeview")
        self._style.configure("MonthBreakdown.Treeview", rowheight=30)
        self._month_break_lines = TreeviewGridlines(wrap, self._month_break_tree, self._gridline_color)
        self._month_break_highlight = TreeviewCellHighlight(
            self._month_break_tree,
            on_copy=lambda iid, col: self._copy_tree_cell(self._month_break_tree, iid, col),
        )
        self._month_break_tree.heading(
            "name", text="Category", anchor="center",
            command=lambda: self._sort_month_breakdown("name"),
        )
        self._month_break_tree.heading(
            "type", text="Type", anchor="center",
            command=lambda: self._sort_month_breakdown("type"),
        )
        self._month_break_tree.heading(
            "total", text="Month total", anchor="center",
            command=lambda: self._sort_month_breakdown("total"),
        )
        self._month_break_tree.column("name", width=240, anchor="center")
        self._month_break_tree.column("type", width=100, anchor="center")
        self._month_break_tree.column("total", width=140, anchor="center")
        self._month_break_tree.grid(row=0, column=0, sticky="nsew")

        ysb = ttk.Scrollbar(
            wrap,
            orient="vertical",
            command=lambda *a: (
                self._month_break_tree.yview(*a),
                self._month_break_lines.redraw(),
                self._month_break_highlight.reposition(),
            ),
        )
        ysb.grid(row=0, column=1, sticky="ns")
        xsb = ttk.Scrollbar(
            wrap,
            orient="horizontal",
            command=lambda *a: (
                self._month_break_tree.xview(*a),
                self._month_break_lines.redraw(),
                self._month_break_highlight.reposition(),
            ),
        )
        xsb.grid(row=1, column=0, sticky="ew")
        self._month_break_tree.configure(
            yscrollcommand=lambda first, last: (
                ysb.set(first, last),
                self._month_break_lines.redraw(),
                self._month_break_highlight.reposition(),
            ),
            xscrollcommand=lambda first, last: (
                xsb.set(first, last),
                self._month_break_lines.redraw(),
                self._month_break_highlight.reposition(),
            ),
        )

        self._month_break_sort_col: Optional[str] = None
        self._month_break_sort_asc: bool = True

    # ----- Year Summary section ------------------------------------------
    # Top: Total Expenses / Total Income / Net across the whole year
    # (three equal cells on the *left half*, like Month Summary).
    # Bottom: read-only 12-row table with spending / income / net for
    # every month of the currently-selected year.
    def _build_year_summary(self, parent: ttk.Frame) -> None:
        # Same responsive split as Month Summary (3/4 narrow, 1/2 wide)
        # so the two strips behave identically when the window is
        # resized.
        sum_wrap = ttk.Frame(parent)
        sum_wrap.pack(fill=tk.X, pady=(0, 10))
        sum_row = ttk.Frame(sum_wrap)
        sum_row.grid(row=0, column=0, sticky="ew")
        self._enable_responsive_summary_split(sum_wrap, "year_summary_split")
        self._year_spend_var = tk.StringVar(value="$0.00")
        self._year_income_var = tk.StringVar(value="$0.00")
        self._year_net_var = tk.StringVar(value="$0.00")
        # Year-wide Investments total — same convention as the Month
        # Summary cell: separate from Expenses, subtracts from End of
        # Month elsewhere, dark blue on charts.
        self._year_invest_var = tk.StringVar(value="$0.00")
        self._year_spend_value_label: Optional[ttk.Label] = None
        self._year_income_value_label: Optional[ttk.Label] = None
        self._year_net_value_label: Optional[ttk.Label] = None
        self._year_invest_value_label: Optional[ttk.Label] = None
        # Same Income-first ordering as Month Summary / pie charts.
        year_cells = [
            ("Total Income", self._year_income_var),
            ("Total Expenses", self._year_spend_var),
            ("Net", self._year_net_var),
            ("Investments", self._year_invest_var),
        ]
        (
            self._year_income_value_label,
            self._year_spend_value_label,
            self._year_net_value_label,
            self._year_invest_value_label,
        ) = self._build_summary_metric_row(sum_row, year_cells)

        cols = ("month", "spending", "income", "net")
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self._year_tree = ttk.Treeview(
            wrap, columns=cols, show="headings", style="YearSummary.Treeview", height=12
        )
        self._apply_tree_gridlines("YearSummary.Treeview")
        self._suppress_row_selection_color("YearSummary.Treeview")
        self._style.configure("YearSummary.Treeview", rowheight=30)
        self._year_lines = TreeviewGridlines(wrap, self._year_tree, self._gridline_color)
        self._year_highlight = TreeviewCellHighlight(
            self._year_tree,
            on_copy=lambda iid, col: self._copy_tree_cell(self._year_tree, iid, col),
        )
        self._year_tree.heading("month", text="Month", anchor="center")
        self._year_tree.heading("spending", text="Expense", anchor="center")
        self._year_tree.heading("income", text="Income", anchor="center")
        self._year_tree.heading("net", text="Net", anchor="center")
        self._year_tree.column("month", width=80, anchor="center")
        self._year_tree.column("spending", width=140, anchor="center")
        self._year_tree.column("income", width=140, anchor="center")
        self._year_tree.column("net", width=140, anchor="center")
        self._year_tree.grid(row=0, column=0, sticky="nsew")
        ysb = ttk.Scrollbar(
            wrap,
            orient="vertical",
            command=lambda *a: (self._year_tree.yview(*a), self._year_lines.redraw(), self._year_highlight.reposition()),
        )
        ysb.grid(row=0, column=1, sticky="ns")

        xsb = ttk.Scrollbar(
            wrap,
            orient="horizontal",
            command=lambda *a: (self._year_tree.xview(*a), self._year_lines.redraw(), self._year_highlight.reposition()),
        )
        xsb.grid(row=1, column=0, sticky="ew")

        self._year_tree.configure(
            yscrollcommand=lambda first, last: (
                ysb.set(first, last),
                self._year_lines.redraw(),
                self._year_highlight.reposition(),
            ),
            xscrollcommand=lambda first, last: (
                xsb.set(first, last),
                self._year_lines.redraw(),
                self._year_highlight.reposition(),
            ),
        )

    # ----- Year Breakdown section ----------------------------------------
    # Read-only table of yearly totals per category. Sortable by name,
    # type (income/expense first), or total.
    def _build_year_breakdown(self, parent: ttk.Frame) -> None:
        cols = ("name", "type", "total")
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self._break_tree = ttk.Treeview(
            wrap, columns=cols, show="headings", style="YearBreakdown.Treeview", height=8
        )
        self._apply_tree_gridlines("YearBreakdown.Treeview")
        self._suppress_row_selection_color("YearBreakdown.Treeview")
        self._style.configure("YearBreakdown.Treeview", rowheight=30)
        self._break_lines = TreeviewGridlines(wrap, self._break_tree, self._gridline_color)
        self._break_highlight = TreeviewCellHighlight(
            self._break_tree,
            on_copy=lambda iid, col: self._copy_tree_cell(self._break_tree, iid, col),
        )
        self._break_tree.heading("name", text="Category", anchor="center", command=lambda: self._sort_year_breakdown("name"))
        self._break_tree.heading("type", text="Type", anchor="center", command=lambda: self._sort_year_breakdown("type"))
        self._break_tree.heading("total", text="Year total", anchor="center", command=lambda: self._sort_year_breakdown("total"))
        self._break_tree.column("name", width=240, anchor="center")
        self._break_tree.column("type", width=100, anchor="center")
        self._break_tree.column("total", width=140, anchor="center")
        self._break_tree.grid(row=0, column=0, sticky="nsew")

        ysb = ttk.Scrollbar(
            wrap,
            orient="vertical",
            command=lambda *a: (self._break_tree.yview(*a), self._break_lines.redraw(), self._break_highlight.reposition()),
        )
        ysb.grid(row=0, column=1, sticky="ns")
        xsb = ttk.Scrollbar(
            wrap,
            orient="horizontal",
            command=lambda *a: (self._break_tree.xview(*a), self._break_lines.redraw(), self._break_highlight.reposition()),
        )
        xsb.grid(row=1, column=0, sticky="ew")
        self._break_tree.configure(
            yscrollcommand=lambda first, last: (
                ysb.set(first, last),
                self._break_lines.redraw(),
                self._break_highlight.reposition(),
            ),
            xscrollcommand=lambda first, last: (
                xsb.set(first, last),
                self._break_lines.redraw(),
                self._break_highlight.reposition(),
            ),
        )

        self._break_sort_col: Optional[str] = None
        self._break_sort_asc: bool = True

    # =====================================================================
    # Project lifecycle & data flow
    # =====================================================================
    # _open_project / _on_year_month_changed / _add_category_and_value /
    # _reload_project_view all mutate UiState and then call
    # _refresh_dashboards or rebuild the grid. _ensure_year_categories is
    # the seam that copies a previous year's category list into the
    # current year so a fresh year doesn't start empty.
    def _open_project(self, project: Project) -> None:
        # Mark this project as just-opened so it floats to the top of Recents.
        try:
            self._repo.touch_project_opened(project.id)
        except Exception:
            pass
        self._reload_projects()
        # Pick up the refreshed Project (with updated last_opened_at).
        project = next((p for p in self._projects if p.id == project.id), project)
        # Switching projects invalidates the per-project undo history.
        self._clear_undo()
        self._state.selected_project = project
        self._project_title.config(text=project.name)

        y = datetime.now().year
        years = [str(yy) for yy in range(y - 5, y + 6)]
        self._year_combo["values"] = years
        if str(self._state.year) not in years:
            self._state.year = y
        self._year_var.set(self._state.year)

        self._month_var.set(EN_MONTHS[self._state.month - 1])
        if hasattr(self, "_summary_month_var"):
            self._summary_month_var.set(EN_MONTHS[self._state.month - 1])

        self._reload_project_view()
        self._show_project()

        # focus category name for fast entry like Avalonia version
        self.after(10, lambda: self.focus_force() or None)

    def _on_year_month_changed(self) -> None:
        if not self._state.selected_project:
            return
        try:
            new_year = int(self._year_combo.get())
        except ValueError:
            return
        year_changed = new_year != self._state.year
        self._state.year = new_year

        month_label = self._month_combo.get()
        if month_label in EN_MONTHS:
            self._state.month = EN_MONTHS.index(month_label) + 1
            # Keep the Month Summary dropdown in sync with the top Month picker.
            if hasattr(self, "_summary_month_var"):
                self._summary_month_var.set(month_label)

        if year_changed:
            # Categories are year-scoped. Reload the entire project view so
            # the per-year category list (seeded from another year if empty)
            # is fetched fresh and column headers in the editable grid match.
            # Undo entries reference (year, category_id) pairs that aren't
            # valid in the new year, so wipe the stack on year switches.
            self._clear_undo()
            self._reload_project_view()
        else:
            self._refresh_dashboards()

    def _add_category_and_value(self) -> None:
        project = self._state.selected_project
        if not project:
            return

        name = self._new_cat_name.get().strip()
        if not name:
            return
        kind_label = self._new_cat_kind.get()
        if kind_label == "Income":
            kind = CategoryKind.INCOME
        elif kind_label == "Investment":
            kind = CategoryKind.INVESTMENT
        elif kind_label == "Discrepancy":
            kind = CategoryKind.DISCREPANCY
        else:
            kind = CategoryKind.EXPENSE

        cat_id = self._repo.create_category(project.id, name, kind, self._state.year)

        cents = _parse_money_to_cents(self._new_cat_value.get())
        if cents is not None and cents != 0:
            self._repo.set_monthly_amount(project.id, cat_id, self._state.year, self._state.month, cents)

        self._new_cat_name.set("")
        self._new_cat_value.set("")
        self._reload_project_view()

    # ----- CSV statement import: UI entry point + notice -----------------
    def _build_import_area(self, parent: ttk.Frame) -> None:
        """Build the import controls (button + optional drop-zone) and the
        hidden 'assign temporary categories' notice, to the right of the
        Add Entry fields."""
        # Row 1 is the entry/Add-button row, so the import button, drop-zone
        # and notice share that baseline.
        # Manage mappings sits in the empty top-right corner (the label row),
        # above the import controls / notice. Same width as Undo last import.
        self._import_action_btn_width = 18
        self._manage_mappings_btn = ttk.Button(
            parent,
            text="Manage mappings",
            command=self._open_manage_mappings_window,
            width=self._import_action_btn_width,
        )
        self._manage_mappings_btn.grid(row=0, column=4, sticky="e", padx=(16, 0))

        area = ttk.Frame(parent)
        area.grid(row=1, column=4, sticky="ew", padx=(16, 0))
        self._import_area = area
        # col0 = Import CSV button; col1 = drop-zone (grows with the window,
        # sized in _resize_drop_zone); col2 = Undo last import (to the right of
        # the drop-zone); col3 = notice, pinned to the right edge.
        area.columnconfigure(1, weight=1)

        self._import_csv_btn = ttk.Button(
            area, text="Import CSV\u2026", command=self._choose_import_file
        )
        self._import_csv_btn.grid(row=0, column=0, sticky="w")

        # Reverses the most recent import for the current project. Disabled
        # when there's nothing to undo.
        self._undo_import_btn = ttk.Button(
            area,
            text="Undo last import",
            command=self._undo_last_import,
            width=self._import_action_btn_width,
        )

        # Optional drag-and-drop zone (only when tkinterdnd2 loaded). Its width
        # scales with the window via _resize_drop_zone.
        if DND_AVAILABLE:
            drop = tk.Label(
                area,
                text="Drop .CSV file here",
                relief=tk.RIDGE,
                borderwidth=1,
                padx=10,
                pady=6,
                width=16,
                foreground="#666666",
            )
            drop.grid(row=0, column=1, sticky="ew", padx=(10, 0))
            try:
                from tkinterdnd2 import DND_FILES

                drop.drop_target_register(DND_FILES)
                drop.dnd_bind("<<Drop>>", self._on_csv_drop)
                # Light visual feedback while a file hovers over the zone.
                drop.dnd_bind(
                    "<<DropEnter>>",
                    lambda _e: drop.configure(background="#e6f2ff"),
                )
                drop.dnd_bind(
                    "<<DropLeave>>",
                    lambda _e: drop.configure(background=self._default_bg()),
                )
                self._import_drop_label = drop
            except Exception:
                pass
            # Undo sits to the right of the drop zone, same width as Manage mappings.
            self._undo_import_btn.grid(row=0, column=2, sticky="ew", padx=(10, 0))
        else:
            # No drop zone — Undo sits beside the Import CSV button.
            self._undo_import_btn.grid(row=0, column=2, sticky="ew", padx=(8, 0))

        self._refresh_undo_import_button()

        # Rescale the drop zone as the window width changes.
        area.bind("<Configure>", lambda _e: self._resize_drop_zone(), add=True)

        # Notice (hidden until there are unassigned temporary merchants),
        # pinned to the right so the message sits beside the Assign now button.
        notice = tk.Frame(area, background="#fdecea", highlightbackground="#f5c2c0", highlightthickness=1)
        msg = tk.Label(
            notice,
            text="Please assign temporary categories",
            background="#fdecea",
            foreground="#8a1f1a",
            anchor="w",
            justify="left",
            wraplength=240,
        )
        msg.grid(row=0, column=0, sticky="w", padx=(8, 8), pady=6)
        ttk.Button(notice, text="Assign now", command=self._open_assign_window).grid(
            row=0, column=1, sticky="e", padx=(0, 8), pady=6
        )
        self._import_notice = notice
        self._refresh_import_notice()

    def _default_bg(self) -> str:
        try:
            return self._style.lookup("TFrame", "background") or "SystemButtonFace"
        except Exception:
            return "SystemButtonFace"

    def _choose_import_file(self) -> None:
        if not self._state.selected_project:
            return
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title="Select bank statement CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self._import_statement_file(path)

    def _on_csv_drop(self, event: tk.Event) -> None:
        if not self._state.selected_project:
            return
        data = getattr(event, "data", "") or ""
        try:
            paths = list(self.tk.splitlist(data))
        except Exception:
            paths = [data.strip().strip("{}")]
        if not paths:
            return
        path = paths[0]
        if not path.lower().endswith(".csv"):
            messagebox.showwarning(
                title="Not a CSV",
                message="Please drop a .csv bank-statement file.",
            )
            return
        self._import_statement_file(path)

    def _refresh_undo_import_button(self) -> None:
        """Enable 'Undo last import' only when the current project has an
        import batch to reverse."""
        btn = getattr(self, "_undo_import_btn", None)
        if btn is None:
            return
        project = self._state.selected_project
        has_batch = bool(project) and self._repo.last_import_batch_id(project.id) is not None
        btn.configure(state=(tk.NORMAL if has_batch else tk.DISABLED))

    def _undo_last_import(self) -> None:
        """Reverse the most recent CSV import for the current project."""
        project = self._state.selected_project
        if not project:
            return
        batch_id = self._repo.last_import_batch_id(project.id)
        if batch_id is None:
            return
        if not messagebox.askyesno(
            title="Undo last import",
            message=(
                "Reverse the most recent import? This subtracts the amounts it "
                "added and removes any temporary columns it created. Merchants "
                "you've already assigned to a category since importing can't be "
                "reversed and will be left as-is."
            ),
        ):
            return
        self._repo.undo_import_batch(project.id, batch_id)
        self._reload_project_view()
        self._refresh_import_notice()
        self._refresh_undo_import_button()

    def _refresh_import_notice(self) -> None:
        """Show the assign-notice iff the current project has unassigned
        temporary merchant categories."""
        if not hasattr(self, "_import_notice"):
            return
        project = self._state.selected_project
        show = bool(project) and self._repo.has_temp_categories(project.id)
        if show:
            # Pin to the right edge so the message sits beside the Assign now
            # button.
            self._import_notice.grid(row=0, column=3, sticky="e", padx=(12, 0))
        else:
            self._import_notice.grid_remove()
        # Notice presence changes how much room the drop-zone gets.
        self._resize_drop_zone()

    # Drop-zone base width (chars). Scales slightly with the window but stays
    # compact so the Undo button beside it can match Manage mappings above.
    _DROP_BASE_CHARS = 16

    def _resize_drop_zone(self) -> None:
        """Recompute the drag-and-drop label width.

        The zone is sized to be exactly triple its base width when the window
        fills the screen, and scales linearly with the window/screen width
        ratio for any smaller size. A safety cap still keeps it from pushing
        the notice off the right edge on very narrow windows.
        """
        drop = getattr(self, "_import_drop_label", None)
        area = getattr(self, "_import_area", None)
        if drop is None or area is None:
            return
        try:
            top = self.winfo_toplevel()
            win_w = top.winfo_width()
            screen_w = top.winfo_screenwidth()
        except Exception:
            return
        if win_w <= 1 or screen_w <= 1:
            return

        # Compact drop zone: ~1.5× base at full screen, proportional below.
        ratio = min(win_w / screen_w, 1.0)
        chars = self._DROP_BASE_CHARS * 1.5 * ratio

        # Safety: never let the zone crowd out the Import button, the Undo
        # button (fixed width, to the zone's right), and the notice.
        avail = area.winfo_width()
        if avail > 1:
            reserve = 260
            if hasattr(self, "_import_notice") and self._import_notice.winfo_ismapped():
                reserve += 280
            chars = min(chars, (avail - reserve) / 7)

        try:
            drop.configure(width=int(max(12, chars)))
        except Exception:
            pass

    # ----- CSV statement import ------------------------------------------
    # Hybrid multi-bank flow:
    #   read rows -> fingerprint the layout -> if we've seen this bank before
    #   (saved profile) parse straight away; otherwise open the mapping dialog
    #   pre-filled with an auto-detected guess for the user to confirm. The
    #   confirmed mapping is saved per bank so it's one-click next time.
    def _import_statement_file(self, path: str) -> None:
        project = self._state.selected_project
        if not project:
            return

        try:
            rows = read_raw_rows(path)
        except (OSError, UnicodeDecodeError) as exc:
            messagebox.showerror(title="Could not read file", message=str(exc))
            return
        if not any(any((c or "").strip() for c in r) for r in rows):
            messagebox.showwarning(
                title="Empty file", message="That CSV file has no data rows."
            )
            return

        signature = file_signature(rows)
        profile = self._repo.get_format_profile(signature)
        if profile is not None:
            # Known bank layout — parse directly, no dialog.
            _name, mapping_json = profile
            try:
                mapping = ColumnMapping.from_json(mapping_json)
            except Exception:
                mapping = detect_mapping(rows)
            self._apply_import_with_mapping(rows, mapping)
        else:
            # New layout — let the user confirm the auto-detected mapping.
            self._open_import_mapping_dialog(rows, signature)

    def _apply_import_with_mapping(
        self, rows: list[list[str]], mapping: "ColumnMapping"
    ) -> None:
        """Parse rows with a confirmed mapping and feed them into the
        merchant grouping / temp-category / assign flow."""
        project = self._state.selected_project
        if not project:
            return
        result = parse_with_mapping(rows, mapping)
        if not result.transactions:
            detail = ""
            if result.errors:
                detail = f"\n\nFirst issue: {result.errors[0][2]}"
            messagebox.showwarning(
                title="Nothing imported",
                message="No transactions could be read with this column mapping. "
                "Try adjusting the columns." + detail,
            )
            return

        # Fingerprint every transaction and drop any already imported into this
        # project, so re-importing the same (or an overlapping) file doesn't
        # double-count. Only genuinely-new transactions proceed.
        fingerprinted = assign_fingerprints(result.transactions)
        already = self._repo.existing_import_fingerprints(
            project.id, [fp for _txn, fp in fingerprinted]
        )
        new_items = [(txn, fp) for txn, fp in fingerprinted if fp not in already]
        skipped_dupes = len(fingerprinted) - len(new_items)

        if not new_items:
            messagebox.showinfo(
                title="Already imported",
                message=(
                    f"All {len(fingerprinted)} transaction(s) in this file have "
                    "already been imported into this project, so nothing was added."
                ),
            )
            return

        # Confirm before changing anything, surfacing the new-vs-skipped split.
        confirm_lines = [f"{len(new_items)} new transaction(s) will be imported."]
        if skipped_dupes:
            confirm_lines.append(
                f"{skipped_dupes} already-imported transaction(s) will be skipped."
            )
        confirm_lines.append("\nImport now?")
        if not messagebox.askyesno(
            title="Confirm import", message="\n".join(confirm_lines)
        ):
            return

        batch_id = self._repo.begin_import_batch(project.id)

        auto_keys: set[str] = set()
        new_keys: set[str] = set()
        transfer_keys: set[str] = set()
        # Cache lookups so each merchant resolves its rule / target once.
        rule_cache: dict[str, Optional[tuple]] = {}
        real_target: dict[tuple[str, int], int] = {}
        temp_target: dict[tuple[str, int], int] = {}
        touched_categories: set[tuple[int, int]] = set()  # (category_id, year)

        for txn, fp in new_items:
            key = txn.merchant_key
            if key not in rule_cache:
                rule_cache[key] = self._repo.get_merchant_rule(project.id, key)
            rule = rule_cache[key]

            if rule is not None:
                kind, final_name, _scope = rule
                if kind == self._repo.TRANSFER_KIND:
                    transfer_keys.add(key)
                    continue
                cache_key = (key, txn.year)
                target_id = real_target.get(cache_key)
                if target_id is None:
                    target_id = self._repo.get_or_create_real_category(
                        project.id, final_name, kind, txn.year
                    )
                    real_target[cache_key] = target_id
                auto_keys.add(key)
            else:
                # Unknown merchant -> reuse its temporary column for the year if
                # one already exists, otherwise create it.
                cache_key = (key, txn.year)
                target_id = temp_target.get(cache_key)
                if target_id is None:
                    target_id = self._repo.find_temp_category(
                        project.id, key, txn.year
                    ) or self._repo.create_temp_category(
                        project.id, txn.display_name, key, txn.year
                    )
                    temp_target[cache_key] = target_id
                new_keys.add(key)

            self._repo.import_transaction(
                batch_id,
                project.id,
                target_id,
                txn.year,
                txn.month,
                txn.amount_cents,
                fp,
                txn.merchant_key,
                txn.display_name,
            )
            touched_categories.add((target_id, txn.year))

        # If the current view month has no imported row for a touched category,
        # pad that cell with $0 (undo removes this padding).
        view_year = self._state.year
        view_month = self._state.month
        touched_in_view_year = {
            cat_id for cat_id, year in touched_categories if year == view_year
        }
        for cat_id in touched_in_view_year:
            self._repo.zero_fill_import_category_month(
                project.id, cat_id, view_year, view_month, batch_id
            )

        self._reload_project_view()
        self._refresh_import_notice()
        self._refresh_undo_import_button()

        summary = [
            f"Imported {len(new_items)} transaction(s) "
            f"across {len(auto_keys) + len(new_keys) + len(transfer_keys)} merchant(s)."
        ]
        if skipped_dupes:
            summary.append(f"{skipped_dupes} duplicate transaction(s) skipped (already imported).")
        if auto_keys:
            summary.append(f"{len(auto_keys)} merchant(s) auto-matched to saved categories.")
        if transfer_keys:
            summary.append(f"{len(transfer_keys)} known transfer merchant(s) skipped.")
        if new_keys:
            summary.append(
                f"{len(new_keys)} new merchant(s) need assigning — use \u201cAssign now\u201d."
            )
        if result.errors:
            summary.append(f"{len(result.errors)} row(s) skipped (unreadable).")
        summary.append("\nUse \u201cUndo last import\u201d to reverse this if needed.")
        messagebox.showinfo(title="Import complete", message="\n".join(summary))

    # ----- Import mapping dialog (hybrid: confirm auto-detected layout) ---
    def _open_import_mapping_dialog(self, rows: list[list[str]], signature: str) -> None:
        """Show a preview of the CSV with column-role pickers, pre-filled with
        an auto-detected guess. On confirm, save the mapping as a per-bank
        profile and run the import."""
        mapping = detect_mapping(rows)
        data_rows = [r for r in rows if any((c or "").strip() for c in r)]
        ncols = max((len(r) for r in data_rows), default=0)
        if ncols == 0:
            messagebox.showwarning(title="Empty file", message="No columns found.")
            return

        win = tk.Toplevel(self)
        win.title("Set up bank CSV format")
        win.transient(self.winfo_toplevel())
        win.geometry("860x600")
        win.minsize(720, 480)

        ttk.Label(
            win,
            text="We don't recognise this bank's CSV yet. Confirm which column is "
            "which below — the preview updates live. We'll remember this layout "
            "for next time.",
            wraplength=820,
            justify="left",
        ).pack(fill=tk.X, padx=12, pady=(12, 8))

        # ---- Preview of the first rows ----
        prev_wrap = ttk.LabelFrame(win, text="File preview (first rows)")
        prev_wrap.pack(fill=tk.BOTH, expand=False, padx=12, pady=(0, 8))
        colnames = [f"c{i}" for i in range(ncols)]
        prev = ttk.Treeview(
            prev_wrap, columns=colnames, show="headings", height=6
        )
        for i in range(ncols):
            prev.heading(f"c{i}", text=f"Column {i}")
            prev.column(f"c{i}", width=130, anchor="center", stretch=True)
        for r in data_rows[:8]:
            vals = [(r[i] if i < len(r) else "") for i in range(ncols)]
            prev.insert("", tk.END, values=vals)
        prev.pack(fill=tk.X, padx=8, pady=8)

        # ---- Column-role controls ----
        ctrl = ttk.LabelFrame(win, text="Column mapping")
        ctrl.pack(fill=tk.X, padx=12, pady=(0, 8))
        col_choices = [str(i) for i in range(ncols)]

        has_header_var = tk.BooleanVar(value=mapping.has_header)
        date_col_var = tk.StringVar(value=str(mapping.date_col))
        desc_col_var = tk.StringVar(value=str(mapping.desc_col))
        amount_mode_var = tk.StringVar(value=mapping.amount_mode)
        amount_col_var = tk.StringVar(
            value=str(mapping.amount_col if mapping.amount_col is not None else 0)
        )
        debit_col_var = tk.StringVar(
            value=str(mapping.debit_col if mapping.debit_col is not None else 0)
        )
        credit_col_var = tk.StringVar(
            value=str(mapping.credit_col if mapping.credit_col is not None else 0)
        )
        date_fmt_var = tk.StringVar(value=mapping.date_format)
        invert_var = tk.BooleanVar(value=mapping.invert_sign)
        name_var = tk.StringVar(value=self._guess_bank_name(rows))

        def labeled_combo(parent, label, var, values, row, colp, width=10):
            ttk.Label(parent, text=label).grid(row=row, column=colp, sticky="w", padx=(8, 4), pady=4)
            cb = ttk.Combobox(
                parent, textvariable=var, values=values, width=width, state="readonly"
            )
            cb.grid(row=row, column=colp + 1, sticky="w", padx=(0, 12), pady=4)
            cb.bind("<<ComboboxSelected>>", lambda _e: _update_preview(), add=True)
            return cb

        ttk.Checkbutton(
            ctrl, text="First row is a header", variable=has_header_var,
            command=lambda: _update_preview(),
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=4)

        labeled_combo(ctrl, "Date column", date_col_var, col_choices, 1, 0)
        labeled_combo(ctrl, "Description column", desc_col_var, col_choices, 1, 2)
        labeled_combo(
            ctrl, "Date format", date_fmt_var,
            ["auto", "dmy_slash", "mdy_slash", "ymd_dash", "text_month"], 1, 4, width=12
        )

        ttk.Label(ctrl, text="Amount style").grid(row=2, column=0, sticky="w", padx=(8, 4), pady=4)
        mode_frame = ttk.Frame(ctrl)
        mode_frame.grid(row=2, column=1, columnspan=3, sticky="w")
        ttk.Radiobutton(
            mode_frame, text="Single signed column", value="signed",
            variable=amount_mode_var, command=lambda: _on_mode_change(),
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            mode_frame, text="Separate debit & credit", value="debit_credit",
            variable=amount_mode_var, command=lambda: _on_mode_change(),
        ).pack(side=tk.LEFT, padx=(10, 0))

        signed_frame = ttk.Frame(ctrl)
        signed_frame.grid(row=3, column=0, columnspan=4, sticky="w")
        labeled_combo(signed_frame, "Amount column", amount_col_var, col_choices, 0, 0)
        ttk.Checkbutton(
            signed_frame, text="Positive = money out (invert)", variable=invert_var,
            command=lambda: _update_preview(),
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        dc_frame = ttk.Frame(ctrl)
        dc_frame.grid(row=4, column=0, columnspan=4, sticky="w")
        labeled_combo(dc_frame, "Debit (out) column", debit_col_var, col_choices, 0, 0)
        labeled_combo(dc_frame, "Credit (in) column", credit_col_var, col_choices, 0, 2)

        name_frame = ttk.Frame(ctrl)
        name_frame.grid(row=5, column=0, columnspan=4, sticky="w", pady=(2, 6))
        ttk.Label(name_frame, text="Remember as (bank name)").grid(row=0, column=0, sticky="w", padx=(8, 4))
        ttk.Entry(name_frame, textvariable=name_var, width=28).grid(row=0, column=1, sticky="w")

        # ---- Live parse preview ----
        result_lbl = ttk.Label(win, text="", wraplength=820, justify="left")
        result_lbl.pack(fill=tk.X, padx=12, pady=(0, 6))

        def _current_mapping() -> "ColumnMapping":
            def as_int(v, default=0):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return default
            return ColumnMapping(
                date_col=as_int(date_col_var.get()),
                desc_col=as_int(desc_col_var.get()),
                amount_mode=amount_mode_var.get(),
                amount_col=as_int(amount_col_var.get()),
                debit_col=as_int(debit_col_var.get()),
                credit_col=as_int(credit_col_var.get()),
                has_header=bool(has_header_var.get()),
                date_format=date_fmt_var.get(),
                invert_sign=bool(invert_var.get()),
            )

        def _on_mode_change():
            if amount_mode_var.get() == "signed":
                signed_frame.grid()
                dc_frame.grid_remove()
            else:
                signed_frame.grid_remove()
                dc_frame.grid()
            _update_preview()

        def _update_preview():
            m = _current_mapping()
            res = parse_with_mapping(rows, m)
            n = len(res.transactions)
            examples = []
            for t in res.transactions[:3]:
                sign = "+" if t.amount_cents >= 0 else "-"
                examples.append(
                    f"{EN_MONTHS[t.month - 1]} {t.year}: {sign}${abs(t.amount_cents) / 100:,.2f} — {t.display_name}"
                )
            txt = f"Preview: {n} transaction(s) would import."
            if res.errors:
                txt += f"  ({len(res.errors)} row(s) skipped)"
            if examples:
                txt += "\n" + "\n".join(examples)
            result_lbl.configure(
                text=txt, foreground=(self._green if n else self._red)
            )

        # ---- Buttons ----
        btns = ttk.Frame(win)
        btns.pack(fill=tk.X, padx=12, pady=(0, 12))

        def _do_import():
            m = _current_mapping()
            res = parse_with_mapping(rows, m)
            if not res.transactions:
                messagebox.showwarning(
                    title="Nothing to import",
                    message="This mapping produced no transactions. Adjust the columns and try again.",
                    parent=win,
                )
                return
            name = name_var.get().strip() or "Bank CSV"
            self._repo.save_format_profile(signature, name, m.to_json())
            win.destroy()
            self._apply_import_with_mapping(rows, m)

        ttk.Button(btns, text="Import", command=_do_import).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side=tk.RIGHT, padx=(0, 8))

        _on_mode_change()
        _update_preview()

    def _guess_bank_name(self, rows: list[list[str]]) -> str:
        """Best-effort bank label from the file's first cells (falls back to a
        generic name). Purely cosmetic — used to pre-fill the profile name."""
        for r in rows:
            for c in r:
                s = (c or "").strip()
                if s and not any(ch.isdigit() for ch in s) and len(s) > 2:
                    return s[:24]
        return "Bank CSV"

    # ----- Assign window: map temporary merchants -> real categories -----
    # Tk on Windows treats StringVar("") as matching every Radiobutton in a
    # group, so "no selection" uses a sentinel that no radio value uses.
    _ASSIGN_TYPE_UNSET = "__type_unset__"
    _ASSIGN_SCOPE_UNSET = "__scope_unset__"
    # Final-category dropdown sentinel: picking this swaps the box to a
    # free-text entry for a brand-new category name.
    _ASSIGN_OTHER = "Other (type a new name)"

    def _assign_type_options(self) -> list[tuple[str, int]]:
        return [
            ("Expense", int(CategoryKind.EXPENSE)),
            ("Income", int(CategoryKind.INCOME)),
            ("Investment", int(CategoryKind.INVESTMENT)),
            ("Discrepancy", int(CategoryKind.DISCREPANCY)),
            ("Transfer", self._repo.TRANSFER_KIND),
        ]

    def _kind_to_type_label(self, kind: Optional[int]) -> str:
        if kind is None:
            return self._ASSIGN_TYPE_UNSET
        for label, k in self._assign_type_options():
            if k == kind:
                return label
        return self._ASSIGN_TYPE_UNSET

    def _type_label_to_kind(self, label: str) -> Optional[int]:
        if not label or label == self._ASSIGN_TYPE_UNSET:
            return None
        for lbl, k in self._assign_type_options():
            if lbl == label:
                return k
        return None

    def _open_assign_window(self) -> None:
        project = self._state.selected_project
        if not project:
            return
        if not self._repo.has_temp_categories(project.id):
            messagebox.showinfo(
                title="Nothing to assign",
                message="There are no temporary categories to assign.",
            )
            self._refresh_import_notice()
            return

        # Reuse an open window if present.
        existing = getattr(self, "_assign_win", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        win = tk.Toplevel(self)
        win.title("Assign temporary categories")
        win.transient(self.winfo_toplevel())
        # Sized so the whole table (name + 5 type radios + name entry + 2
        # mapping radios + separators + scrollbar) fits without horizontal
        # scrolling, even at the minimum size.
        win.geometry("1285x600")
        win.minsize(1275, 380)
        self._assign_win = win

        header = ttk.Label(
            win,
            text="Map each merchant to a Type and a final category name. "
            "Same name + type merges into one column.",
            wraplength=1020,
            justify="left",
        )
        header.pack(fill=tk.X, padx=12, pady=(12, 6))

        # Headers share the same grid as the rows (see _populate_assign_rows)
        # so they line up with their columns.
        scroll = VerticalScrolledFrame(win)
        scroll.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 6))
        self._assign_rows_root = scroll.inner

        btns = ttk.Frame(win)
        btns.pack(fill=tk.X, padx=12, pady=(0, 12))
        ttk.Button(btns, text="Save", command=self._save_assignments).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Close", command=win.destroy).pack(side=tk.RIGHT, padx=(0, 8))

        def _on_close() -> None:
            # Persist whatever the user has selected so far as drafts.
            self._persist_assign_drafts()
            win.destroy()
            self._assign_win = None

        win.protocol("WM_DELETE_WINDOW", _on_close)
        self._populate_assign_rows()

    # Shared column layout for the assign grid. Data columns are 1/3/5/7/9;
    # the even gaps (2/4/6/8) hold vertical separators.
    _ASSIGN_COL_NAME = 1
    _ASSIGN_COL_AMOUNT = 3
    _ASSIGN_COL_TYPE = 5
    _ASSIGN_COL_FINAL = 7
    _ASSIGN_COL_MAPPING = 9

    def _populate_assign_rows(self) -> None:
        """(Re)build the merchant rows from the current temp categories.

        Headers, data and the vertical separators all share one grid so the
        column titles always line up with the cells beneath them.
        """
        project = self._state.selected_project
        if not project or not hasattr(self, "_assign_rows_root"):
            return
        root = self._assign_rows_root
        for child in root.winfo_children():
            child.destroy()

        self._assign_row_widgets = []  # list of dicts
        # Drop stale scope-only drafts so Mapping radios don't reopen filled.
        self._repo.clear_scope_only_merchant_drafts(project.id)
        merchants = self._repo.list_temp_merchants(project.id)
        if not merchants:
            ttk.Label(
                root, text="All merchants assigned. You can close this window."
            ).grid(row=0, column=0, sticky="w", pady=8)
            return

        # Fixed minimum widths keep headers and cells aligned; the name column
        # is widest so long "Transfer ..." descriptions wrap.
        root.columnconfigure(self._ASSIGN_COL_NAME, minsize=220)
        root.columnconfigure(self._ASSIGN_COL_AMOUNT, minsize=90)
        root.columnconfigure(self._ASSIGN_COL_TYPE, minsize=420)
        root.columnconfigure(self._ASSIGN_COL_FINAL, minsize=200)
        root.columnconfigure(self._ASSIGN_COL_MAPPING, minsize=200)

        # Existing/saved category names to offer in each row's dropdown.
        suggestions = self._repo.suggested_category_names(project.id)

        # Header row.
        for col, txt in (
            (self._ASSIGN_COL_NAME, "Temporary category"),
            (self._ASSIGN_COL_AMOUNT, "Amount"),
            (self._ASSIGN_COL_TYPE, "Type"),
            (self._ASSIGN_COL_FINAL, "Final category name"),
            (self._ASSIGN_COL_MAPPING, "Mapping"),
        ):
            ttk.Label(root, text=txt, font=("", 9, "bold")).grid(
                row=0, column=col, sticky="w", padx=(4, 8), pady=(0, 4)
            )

        for idx, (key, display, amount_cents) in enumerate(merchants):
            self._build_assign_row(idx + 1, key, display, amount_cents, suggestions)

        # Vertical separators between sections, spanning header + all rows.
        total_rows = len(merchants) + 1
        for sep_col in (
            self._ASSIGN_COL_NAME + 1,
            self._ASSIGN_COL_AMOUNT + 1,
            self._ASSIGN_COL_TYPE + 1,
            self._ASSIGN_COL_FINAL + 1,
        ):
            ttk.Separator(root, orient="vertical").grid(
                row=0, column=sep_col, rowspan=total_rows, sticky="ns", padx=4
            )

    def _build_assign_row(
        self, grid_row: int, key: str, display: str, amount_cents: int, suggestions: list
    ) -> None:
        project = self._state.selected_project
        draft = self._repo.get_merchant_draft(project.id, key) if project else None
        draft_kind = draft[0] if draft else None
        draft_name = draft[1] if draft else None
        draft_scope = draft[2] if draft else None
        # A draft only counts as real progress if a Type or final name was
        # entered. A scope-only draft would otherwise pre-select a Mapping the
        # user never chose, so discard it and start the row blank.
        if draft is not None and draft_kind is None and not (draft_name or "").strip():
            if project:
                self._repo.delete_merchant_draft(project.id, key)
            draft_kind = draft_name = draft_scope = None
        root = self._assign_rows_root

        # wraplength lets long transfer descriptions span multiple lines.
        name_lbl = tk.Label(root, text=display, anchor="w", justify="left", wraplength=220)
        name_lbl.grid(row=grid_row, column=self._ASSIGN_COL_NAME, sticky="w", padx=(4, 8), pady=3)

        amount_lbl = tk.Label(
            root,
            text=_money_from_cents(amount_cents),
            anchor="center",
            justify="center",
        )
        # Signed sum of every imported cent for this merchant in the file.
        amount_lbl.grid(row=grid_row, column=self._ASSIGN_COL_AMOUNT, sticky="ew", padx=(4, 8), pady=3)

        type_var = tk.StringVar(value=self._kind_to_type_label(draft_kind))
        type_frame = tk.Frame(root)
        type_frame.grid(row=grid_row, column=self._ASSIGN_COL_TYPE, sticky="w", padx=(4, 8))
        for label, _k in self._assign_type_options():
            tk.Radiobutton(
                type_frame, text=label, value=label, variable=type_var
            ).pack(side=tk.LEFT)

        # Final category: a dropdown of saved/existing names, with "Other" at
        # the top to swap in a free-text entry for a brand-new name.
        final_frame = tk.Frame(root)
        final_frame.grid(row=grid_row, column=self._ASSIGN_COL_FINAL, sticky="ew", padx=(4, 8), pady=3)
        final_frame.columnconfigure(0, weight=1)
        combo_var = tk.StringVar()
        name_var = tk.StringVar()
        mode_var = tk.StringVar(value="list")

        combo = ttk.Combobox(
            final_frame,
            textvariable=combo_var,
            values=[self._ASSIGN_OTHER] + list(suggestions),
            state="readonly",
            justify="center",
        )
        entry_wrap = tk.Frame(final_frame)
        entry_wrap.columnconfigure(0, weight=1)
        entry = tk.Entry(entry_wrap, textvariable=name_var, justify="center")
        entry.grid(row=0, column=0, sticky="ew")

        def show_list() -> None:
            mode_var.set("list")
            entry_wrap.grid_remove()
            combo.grid(row=0, column=0, sticky="ew")
            combo_var.set("")

        def show_other(focus: bool = True) -> None:
            mode_var.set("other")
            combo.grid_remove()
            entry_wrap.grid(row=0, column=0, sticky="ew")
            if focus:
                entry.focus_set()

        # Small button to return from the free-text entry to the dropdown.
        ttk.Button(entry_wrap, text="\u25be", width=2, command=show_list).grid(
            row=0, column=1, sticky="w", padx=(2, 0)
        )

        def _on_combo(_e=None) -> None:
            if combo_var.get() == self._ASSIGN_OTHER:
                show_other()

        combo.bind("<<ComboboxSelected>>", _on_combo, add=True)

        combo.grid(row=0, column=0, sticky="ew")
        if draft_name:
            if draft_name in suggestions:
                combo_var.set(draft_name)
            else:
                name_var.set(draft_name)
                show_other(focus=False)

        if draft_scope == "global":
            scope_default = "Global"
        elif draft_scope == "project":
            scope_default = "Per-Project"
        else:
            scope_default = self._ASSIGN_SCOPE_UNSET
        scope_var = tk.StringVar(value=scope_default)
        scope_frame = tk.Frame(root)
        scope_frame.grid(row=grid_row, column=self._ASSIGN_COL_MAPPING, sticky="w", padx=(4, 8))
        for label in ("Per-Project", "Global"):
            tk.Radiobutton(
                scope_frame, text=label, value=label, variable=scope_var
            ).pack(side=tk.LEFT)

        self._assign_row_widgets.append(
            {
                "key": key,
                "display": display,
                "name_lbl": name_lbl,
                "amount_lbl": amount_lbl,
                "type_var": type_var,
                "final_combo_var": combo_var,
                "final_name_var": name_var,
                "final_mode_var": mode_var,
                "scope_var": scope_var,
            }
        )

    def _assign_scope_value(self, scope_label: str) -> Optional[str]:
        """Map the Mapping radio label to a stored scope ('project'/'global'),
        or None when nothing is selected yet."""
        if scope_label == "Global":
            return "global"
        if scope_label == "Per-Project":
            return "project"
        return None

    def _assign_final_name(self, w: dict) -> str:
        """The chosen final category name for a row: the typed text in 'Other'
        mode, otherwise the dropdown selection (empty if nothing picked)."""
        if w["final_mode_var"].get() == "other":
            return w["final_name_var"].get().strip()
        value = w["final_combo_var"].get()
        if not value or value == self._ASSIGN_OTHER:
            return ""
        return value.strip()

    def _row_is_complete(self, w: dict) -> bool:
        type_label = w["type_var"].get()
        if not type_label or type_label == self._ASSIGN_TYPE_UNSET:
            return False
        # A Mapping (Per-Project / Global) must be chosen too.
        if self._assign_scope_value(w["scope_var"].get()) is None:
            return False
        # Transfer drops the column, so a final name isn't required.
        if type_label == "Transfer":
            return True
        return bool(self._assign_final_name(w))

    def _persist_assign_drafts(self) -> None:
        """Save every row's current selection as a draft (so reopening the
        window restores in-progress work)."""
        project = self._state.selected_project
        if not project or not hasattr(self, "_assign_row_widgets"):
            return
        for w in self._assign_row_widgets:
            type_label = w["type_var"].get()
            kind = self._type_label_to_kind(type_label) if type_label else None
            name = self._assign_final_name(w) or None
            scope = self._assign_scope_value(w["scope_var"].get())
            # Only keep a draft with real progress (a Type or name); a
            # scope-only selection isn't worth restoring.
            if kind is None and not name:
                self._repo.delete_merchant_draft(project.id, w["key"])
            else:
                self._repo.save_merchant_draft(project.id, w["key"], kind, name, scope)

    def _save_assignments(self) -> None:
        project = self._state.selected_project
        if not project or not hasattr(self, "_assign_row_widgets"):
            return

        incomplete = 0
        assigned = 0
        for w in self._assign_row_widgets:
            type_label = w["type_var"].get()
            name = self._assign_final_name(w)
            scope = self._assign_scope_value(w["scope_var"].get())
            kind = self._type_label_to_kind(type_label) if type_label else None

            if not self._row_is_complete(w):
                # Keep a draft only when there's real progress (Type or name);
                # a scope-only selection isn't worth restoring.
                if kind is None and not name:
                    self._repo.delete_merchant_draft(project.id, w["key"])
                else:
                    self._repo.save_merchant_draft(
                        project.id, w["key"], kind, name or None, scope
                    )
                incomplete += 1
                continue

            # Complete -> assign + remember the rule.
            self._repo.assign_merchant(project.id, w["key"], kind, name)
            rule_project = project.id if scope == "project" else None
            self._repo.upsert_merchant_rule(
                scope, rule_project, w["key"], kind, name or "Transfer"
            )
            assigned += 1

        self._reload_project_view()

        # Always close the window. Incomplete merchants stay temporary (the
        # notice remains) and reappear in the window on reopen.
        win = getattr(self, "_assign_win", None)
        if win is not None and win.winfo_exists():
            win.destroy()
        self._assign_win = None

        if incomplete:
            messagebox.showwarning(
                title="Some merchants are unassigned",
                message=(
                    f"{assigned} assigned. {incomplete} merchant(s) are missing a Type, "
                    "Mapping or Name. Their selections were saved — finish them anytime "
                    "via Assign now."
                ),
            )

    # ----- Manage month mapping (read-only view for selected month) --------
    def _summary_scope_month(self) -> Optional[int]:
        """The single month the Month Summary dropdown is scoped to, or None
        when it is set to All."""
        if hasattr(self, "_summary_month_var"):
            label = self._summary_month_var.get()
            if label and label != "All":
                try:
                    return EN_MONTHS.index(label) + 1
                except ValueError:
                    pass
        return None

    def _open_manage_month_mapping_window(self) -> None:
        """Show merchant import mappings for the Month Summary's selected month."""
        project = self._state.selected_project
        if not project:
            return

        month = self._summary_scope_month()
        if month is None:
            messagebox.showinfo(
                title="Manage Month Mapping",
                message=(
                    "Select a single month in the Month Summary dropdown "
                    "(not All) to view that month's import mappings."
                ),
            )
            return

        year = self._state.year
        month_label = EN_MONTHS[month - 1]

        existing = getattr(self, "_month_mapping_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = tk.Toplevel(self)
        win.title(f"Month mapping — {month_label} {year}")
        win.transient(self.winfo_toplevel())
        win.geometry("920x420")
        win.minsize(760, 280)
        self._month_mapping_win = win

        ttk.Label(
            win,
            text=(
                f"Import mappings for {month_label} {year}. Shows each merchant "
                "with activity in this month, its amount, and any saved rule or "
                "in-progress assignment from the temporary-category step. "
                "Use Manage mappings to edit rules."
            ),
            wraplength=880,
            justify="left",
        ).pack(fill=tk.X, padx=12, pady=(12, 8))

        rows = self._repo.list_month_merchant_mappings(project.id, year, month)

        table_wrap = ttk.Frame(win)
        table_wrap.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        cols = ("merchant", "amount", "type", "final", "scope")
        tree = ttk.Treeview(
            table_wrap, columns=cols, show="headings", height=min(12, max(4, len(rows)))
        )
        tree.heading("merchant", text="Temporary category")
        tree.heading("amount", text="Amount")
        tree.heading("type", text="Type")
        tree.heading("final", text="Final category")
        tree.heading("scope", text="Mapping")
        tree.column("merchant", width=260, anchor="w", stretch=True)
        tree.column("amount", width=100, anchor="center", stretch=False)
        tree.column("type", width=110, anchor="center", stretch=False)
        tree.column("final", width=180, anchor="center", stretch=True)
        tree.column("scope", width=110, anchor="center", stretch=False)

        if not rows:
            tree.insert(
                "",
                tk.END,
                values=(
                    f"No import merchants for {month_label} {year}.",
                    "",
                    "",
                    "",
                    "",
                ),
            )
        else:
            for row in rows:
                type_lbl = self._kind_to_type_label(row["type_kind"])
                if type_lbl == self._ASSIGN_TYPE_UNSET:
                    type_lbl = "—"
                tree.insert(
                    "",
                    tk.END,
                    values=(
                        row["display_name"],
                        _money_from_cents(row["amount_cents"]),
                        type_lbl,
                        row["final_name"],
                        row["scope_label"],
                    ),
                )

        scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        btns = ttk.Frame(win)
        btns.pack(fill=tk.X, padx=12, pady=(0, 12))
        ttk.Button(
            btns, text="Manage mappings", command=self._open_manage_mappings_window
        ).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Close", command=win.destroy).pack(side=tk.RIGHT, padx=(0, 8))

        def _on_close() -> None:
            win.destroy()
            self._month_mapping_win = None

        win.protocol("WM_DELETE_WINDOW", _on_close)

    # ----- Manage saved merchant mappings --------------------------------
    # Column layout for the editable mappings grid (data cols 0/2/4/6/8; the
    # odd-numbered gaps hold vertical separators).
    _MAP_COL_MERCHANT = 0
    _MAP_COL_TYPE = 2
    _MAP_COL_FINAL = 4
    _MAP_COL_SCOPE = 6
    _MAP_COL_DELETE = 8

    def _open_manage_mappings_window(self) -> None:
        """Show every saved merchant->category rule and let the user edit its
        Type, final name, and scope (Per-Project / Global) directly, or delete
        it. Edits only affect future auto-assignment; amounts already imported
        into categories are untouched."""
        existing = getattr(self, "_mappings_win", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        win = tk.Toplevel(self)
        win.title("Manage merchant mappings")
        win.transient(self.winfo_toplevel())
        # Wide enough for merchant + 5 type radios + name + 2 scope radios +
        # the Delete button + separators + scrollbar with no horizontal scroll.
        win.geometry("1240x560")
        win.minsize(1180, 320)
        self._mappings_win = win

        ttk.Label(
            win,
            text="Edit a saved rule's Type, final category, or Mapping scope, then "
            "click Save. Switching to Per-Project applies the rule to the current "
            "project. Delete a rule to make that merchant a temporary category again "
            "on the next import. Amounts already imported are not affected.",
            wraplength=1000,
            justify="left",
        ).pack(fill=tk.X, padx=12, pady=(12, 6))

        scroll = VerticalScrolledFrame(win)
        scroll.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 6))
        self._mappings_rows_root = scroll.inner

        btns = ttk.Frame(win)
        btns.pack(fill=tk.X, padx=12, pady=(0, 12))
        ttk.Button(btns, text="Save", command=self._save_mapping_edits).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Close", command=win.destroy).pack(side=tk.RIGHT, padx=(0, 8))

        def _on_close() -> None:
            win.destroy()
            self._mappings_win = None

        win.protocol("WM_DELETE_WINDOW", _on_close)
        self._populate_mappings_rows()

    def _populate_mappings_rows(self) -> None:
        root = getattr(self, "_mappings_rows_root", None)
        if root is None or not root.winfo_exists():
            return
        for child in root.winfo_children():
            child.destroy()

        self._mapping_row_widgets = []  # list of dicts
        rules = self._repo.list_merchant_rules()
        if not rules:
            ttk.Label(root, text="No saved mappings yet.").grid(
                row=0, column=0, sticky="w", pady=8
            )
            return

        root.columnconfigure(self._MAP_COL_MERCHANT, minsize=240)
        root.columnconfigure(self._MAP_COL_TYPE, minsize=420)
        root.columnconfigure(self._MAP_COL_FINAL, minsize=200)
        root.columnconfigure(self._MAP_COL_SCOPE, minsize=180)

        # Existing/saved category names to offer in each row's dropdown.
        project = self._state.selected_project
        suggestions = self._repo.suggested_category_names(project.id) if project else []

        for col, txt in (
            (self._MAP_COL_MERCHANT, "Merchant"),
            (self._MAP_COL_TYPE, "Type"),
            (self._MAP_COL_FINAL, "Final category"),
            (self._MAP_COL_SCOPE, "Mapping"),
        ):
            ttk.Label(root, text=txt, font=("", 9, "bold")).grid(
                row=0, column=col, sticky="w", padx=(4, 8), pady=(0, 4)
            )

        for idx, rule in enumerate(rules):
            self._build_mapping_row(idx + 1, rule, suggestions)

        total_rows = len(rules) + 1
        for sep_col in (
            self._MAP_COL_MERCHANT + 1,
            self._MAP_COL_TYPE + 1,
            self._MAP_COL_FINAL + 1,
            self._MAP_COL_SCOPE + 1,
        ):
            ttk.Separator(root, orient="vertical").grid(
                row=0, column=sep_col, rowspan=total_rows, sticky="ns", padx=4
            )

    def _build_mapping_row(self, grid_row: int, rule: dict, suggestions: list) -> None:
        root = self._mappings_rows_root

        name_lbl = tk.Label(
            root, text=rule["merchant_key"], anchor="w", justify="left", wraplength=230
        )
        name_lbl.grid(row=grid_row, column=self._MAP_COL_MERCHANT, sticky="w", padx=(4, 8), pady=3)

        type_var = tk.StringVar(value=self._kind_to_type_label(rule["kind"]))
        type_frame = tk.Frame(root)
        type_frame.grid(row=grid_row, column=self._MAP_COL_TYPE, sticky="w", padx=(4, 8))
        for label, _k in self._assign_type_options():
            tk.Radiobutton(
                type_frame, text=label, value=label, variable=type_var
            ).pack(side=tk.LEFT)

        # Final category: a dropdown of saved/existing names, with "Other" at
        # the top to swap in a free-text entry for a brand-new name.
        final_frame = tk.Frame(root)
        final_frame.grid(row=grid_row, column=self._MAP_COL_FINAL, sticky="ew", padx=(4, 8), pady=3)
        final_frame.columnconfigure(0, weight=1)
        combo_var = tk.StringVar()
        name_var = tk.StringVar()
        mode_var = tk.StringVar(value="list")

        combo = ttk.Combobox(
            final_frame,
            textvariable=combo_var,
            values=[self._ASSIGN_OTHER] + list(suggestions),
            state="readonly",
            justify="center",
        )
        entry_wrap = tk.Frame(final_frame)
        entry_wrap.columnconfigure(0, weight=1)
        entry = tk.Entry(entry_wrap, textvariable=name_var, justify="center")
        entry.grid(row=0, column=0, sticky="ew")

        def show_list() -> None:
            mode_var.set("list")
            entry_wrap.grid_remove()
            combo.grid(row=0, column=0, sticky="ew")
            combo_var.set("")

        def show_other(focus: bool = True) -> None:
            mode_var.set("other")
            combo.grid_remove()
            entry_wrap.grid(row=0, column=0, sticky="ew")
            if focus:
                entry.focus_set()

        # Small button to return from the free-text entry to the dropdown.
        ttk.Button(entry_wrap, text="\u25be", width=2, command=show_list).grid(
            row=0, column=1, sticky="w", padx=(2, 0)
        )

        def _on_combo(_e=None) -> None:
            if combo_var.get() == self._ASSIGN_OTHER:
                show_other()

        combo.bind("<<ComboboxSelected>>", _on_combo, add=True)

        combo.grid(row=0, column=0, sticky="ew")
        current_name = (rule["final_name"] or "").strip()
        if current_name and current_name.lower() != "transfer":
            if current_name in suggestions:
                combo_var.set(current_name)
            else:
                name_var.set(current_name)
                show_other(focus=False)

        scope_var = tk.StringVar(
            value="Global" if rule["scope"] == "global" else "Per-Project"
        )
        scope_frame = tk.Frame(root)
        scope_frame.grid(row=grid_row, column=self._MAP_COL_SCOPE, sticky="w", padx=(4, 8))
        for label in ("Per-Project", "Global"):
            tk.Radiobutton(
                scope_frame, text=label, value=label, variable=scope_var
            ).pack(side=tk.LEFT)

        ttk.Button(
            root,
            text="Delete",
            command=lambda rid=rule["id"]: self._delete_one_mapping(rid),
        ).grid(row=grid_row, column=self._MAP_COL_DELETE, sticky="w", padx=(4, 4), pady=3)

        self._mapping_row_widgets.append(
            {
                "id": rule["id"],
                "merchant_key": rule["merchant_key"],
                "orig_project_id": rule["project_id"],
                "type_var": type_var,
                "final_combo_var": combo_var,
                "final_name_var": name_var,
                "final_mode_var": mode_var,
                "scope_var": scope_var,
            }
        )

    def _delete_one_mapping(self, rule_id: int) -> None:
        if not messagebox.askyesno(
            title="Delete mapping",
            message=(
                "Delete this saved mapping? The merchant will become a temporary "
                "category again on the next import. Amounts already imported are not "
                "affected."
            ),
            parent=getattr(self, "_mappings_win", None) or self,
        ):
            return
        self._repo.delete_merchant_rule(rule_id)
        self._populate_mappings_rows()

    def _save_mapping_edits(self) -> None:
        if not hasattr(self, "_mapping_row_widgets"):
            return
        project = self._state.selected_project
        skipped = 0
        for w in self._mapping_row_widgets:
            type_label = w["type_var"].get()
            kind = self._type_label_to_kind(type_label) if type_label else None
            name = self._assign_final_name(w)
            scope = "global" if w["scope_var"].get() == "Global" else "project"

            # A rule must keep a Type and (unless Transfer) a final name.
            if kind is None or (type_label != "Transfer" and not name):
                skipped += 1
                continue

            if scope == "project":
                # Keep the rule's own project if it had one; otherwise apply it
                # to the project we're currently viewing.
                target_project = w["orig_project_id"] or (project.id if project else None)
                if target_project is None:
                    skipped += 1
                    continue
            else:
                target_project = None

            self._repo.update_merchant_rule(
                w["id"], scope, target_project, kind, name or "Transfer"
            )

        self._populate_mappings_rows()
        win = getattr(self, "_mappings_win", None)
        if win is not None and win.winfo_exists():
            win.destroy()
        self._mappings_win = None

        if skipped:
            messagebox.showwarning(
                title="Some rules not saved",
                message=(
                    f"{skipped} rule(s) were missing a Type or final category name "
                    "and were left unchanged."
                ),
            )

    def _ensure_year_categories(self) -> None:
        """If the current year has no categories yet but another year does,
        seed the current year by copying that year's category list. Edits
        and deletes after this point only affect the current year.
        """
        project = self._state.selected_project
        if not project:
            return
        existing = self._repo.list_categories(project.id, self._state.year)
        if existing:
            return
        src_year = self._repo.find_year_with_categories(project.id, self._state.year)
        if src_year is None or src_year == self._state.year:
            return
        self._repo.copy_categories_to_year(project.id, src_year, self._state.year)

    def _reload_project_view(self) -> None:
        self._categories = []
        project = self._state.selected_project
        if not project:
            return
        self._ensure_year_categories()
        self._categories = self._repo.list_categories(
            project.id, self._state.year, include_temporary=True
        )
        self._rebuild_grid_columns()
        self._populate_grid_rows()
        self._refresh_dashboards()
        if hasattr(self, "_charts"):
            self._charts.reload_year_options()
            self._charts.reload_project_options()
            self._charts.refresh_charts()
        self._refresh_import_notice()
        self._refresh_undo_import_button()
        if hasattr(self, "_sections_scroll"):
            self.after_idle(self._sections_scroll.snap_to_top)

    def _rebuild_grid_columns(self) -> None:
        cols = ["month"] + [f"cat_{c.id}" for c in self._categories]
        self._recreate_month_grid_tree(cols)

    # ----- Money formatting on user entry --------------------------------
    # Used by both the "Add Entry" Value box and the in-cell editor to
    # pretty-format whatever the user types ("1234.5" -> "$1,234.50") at
    # commit time, and bail gracefully on unparseable input.
    # ----- Summary metric strip (Month / Year Summary headers) -----------
    # Window-width breakpoint between the wide (1/2) and narrow (3/4)
    # summary strip layouts. ``1300`` covers the default 1040-px
    # window plus everything up to a typical maximised laptop screen,
    # so any time the window isn't on a wide desktop monitor the
    # strip claims 3/4 of the row to avoid clipping titles like
    # "Start of Month". On true desktop widths it relaxes back to
    # the 1/2 split that lines up with the table below.
    _SUMMARY_NARROW_WIDTH = 1300

    def _enable_responsive_summary_split(
        self, sum_wrap: ttk.Frame, uniform_tag: str
    ) -> None:
        """Configure ``sum_wrap`` so column 0 (the totals strip) takes
        3/4 of the width on narrow windows and 1/2 on wide windows.

        Switches by re-applying ``columnconfigure(weight=...)`` on
        every ``<Configure>`` event with a width crossing the
        breakpoint. The ``state`` cache prevents touching grid each
        frame of a drag.
        """
        # Default to the narrow (3/4) layout; the first ``<Configure>``
        # callback corrects it to 1/2 if the window is actually wide.
        state = {"narrow": True}  # type: dict[str, Optional[bool]]

        def _apply(narrow: bool) -> None:
            if narrow:
                sum_wrap.columnconfigure(0, weight=3, uniform=uniform_tag)
                sum_wrap.columnconfigure(1, weight=1, uniform=uniform_tag)
            else:
                sum_wrap.columnconfigure(0, weight=1, uniform=uniform_tag)
                sum_wrap.columnconfigure(1, weight=1, uniform=uniform_tag)

        def _on_configure(_event: tk.Event) -> None:
            try:
                # Use the toplevel window's width, not ``sum_wrap``'s
                # measured width. The wrap takes its size *from* the
                # column weights we set, so reading it back creates a
                # feedback loop where the layout never settles.
                w = sum_wrap.winfo_toplevel().winfo_width()
            except Exception:
                return
            narrow = w < self._SUMMARY_NARROW_WIDTH
            if state["narrow"] is not narrow:
                state["narrow"] = narrow
                _apply(narrow)

        # Apply once now (initial layout) and on every later resize.
        _apply(True)
        sum_wrap.bind("<Configure>", _on_configure, add=True)

    def _build_summary_metric_row(
        self,
        row: ttk.Frame,
        cells: Sequence[tuple[str, tk.StringVar]],
    ) -> tuple[ttk.Label, ...]:
        """Build a horizontal row of summary tiles with equal-weight
        columns (every tile gets the same fraction of the row's width)
        and centred title + value text. Same look at every window
        size — the row just scales with the left half of the section.
        """
        value_labels: list[ttk.Label] = []
        for i in range(len(cells)):
            row.columnconfigure(i, weight=1, uniform="summary_metric_cells")
        for i, (title, var) in enumerate(cells):
            cell = ttk.Frame(row)
            cell.grid(row=0, column=i, sticky="ew")
            ttk.Label(
                cell,
                text=title,
                foreground="#666",
                font=("Segoe UI", 9, "bold"),
                anchor="center",
            ).pack(fill=tk.X)
            value_lbl = ttk.Label(
                cell,
                textvariable=var,
                font=("Segoe UI", 12, "bold"),
                anchor="center",
            )
            value_lbl.pack(fill=tk.X, pady=(4, 0))
            value_labels.append(value_lbl)
        return tuple(value_labels)

    def _normalize_money_input(self, s: str) -> str:
        s = s.strip()
        if s == "":
            return ""
        cents = _parse_money_to_cents(s)
        if cents is None:
            return s
        return _money_input_from_cents(cents)

    def _format_value_entry_var(self, var: tk.StringVar) -> None:
        raw = var.get()
        if raw.strip() == "":
            return
        cents = _parse_money_to_cents(raw)
        if cents is None:
            return
        var.set(_money_input_from_cents(cents))

    # ----- Month grid: populate / commit / delete ------------------------
    # _populate_grid_rows fills the 12 month rows from the database.
    # _commit_grid_cell handles the in-cell editor's final value (parse,
    # validate, store, refresh). _delete_grid_cell handles
    # Backspace/Delete from the highlight overlay.
    def _populate_grid_rows(self) -> None:
        if hasattr(self, "_grid_highlight"):
            self._grid_highlight.clear()
        self._grid_tree.delete(*self._grid_tree.get_children())
        project = self._state.selected_project
        if not project:
            return
        amounts = self._repo.get_month_grid(project.id, self._state.year)
        cols = list(self._grid_tree["columns"])
        for m in range(1, 13):
            row = {"month": EN_MONTHS[m - 1]}
            for c in self._categories:
                col = f"cat_{c.id}"
                cents = amounts.get((m, c.id))
                row[col] = "" if cents is None else _money_from_cents(cents)
            self._grid_tree.insert(
                "", tk.END, iid=f"m{m}", values=[row.get(col, "") for col in cols]
            )
        # Always 12 rows; ensure every month is visible without internal scrolling.
        self._grid_tree.configure(height=12)
        self._grid_lines.redraw()
        self._refresh_temp_header_marks()

    def _refresh_temp_header_marks(self) -> None:
        """Tell the header overlay which columns are temporary import
        columns so it can tint them light red."""
        if not hasattr(self, "_temp_headers"):
            return
        cols = list(self._grid_tree["columns"])
        mapping: dict[str, str] = {}
        for i, colname in enumerate(cols):
            if not colname.startswith("cat_"):
                continue
            try:
                cid = int(colname.split("_", 1)[1])
            except ValueError:
                continue
            cat = next((c for c in self._categories if c.id == cid), None)
            if cat is not None and getattr(cat, "is_temporary", False):
                mapping[f"#{i + 1}"] = cat.name
        self._temp_headers.set_temp_columns(mapping)

    def _on_temp_header_rightclick(self, col_id: str, event: tk.Event) -> None:
        """Right-click menu for a temporary column: jump to Assign, or
        delete the merchant column outright."""
        try:
            col_index = int(col_id[1:]) - 1
        except (ValueError, IndexError):
            return
        cols = list(self._grid_tree["columns"])
        if col_index < 0 or col_index >= len(cols):
            return
        col_name = cols[col_index]
        if not col_name.startswith("cat_"):
            return
        category_id = int(col_name.split("_", 1)[1])
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Assign now\u2026", command=self._open_assign_window)
        menu.add_separator()
        menu.add_command(
            label="Delete this merchant column",
            command=lambda cid=category_id: self._delete_category(cid),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _commit_grid_cell(self, item_id: str, col_id: str, value: str) -> bool:
        """Persist one Month-grid cell. Returns ``False`` when the value
        could not be parsed (grid is refreshed to the old display)."""
        project = self._state.selected_project
        if not project:
            return False
        # don't allow editing Month column
        if col_id == "#1":
            return False

        month = int(item_id[1:])  # "m{month}"
        col_index = int(col_id[1:]) - 1
        columns = list(self._grid_tree["columns"])
        if col_index < 0 or col_index >= len(columns):
            return False
        col_name = columns[col_index]
        if not col_name.startswith("cat_"):
            return False
        category_id = int(col_name.split("_", 1)[1])

        # Snapshot the cell's existing cents *before* the mutation so
        # undo/redo have both endpoints. None means the cell was empty.
        old_cents = self._repo.get_monthly_amount_cents(
            category_id, self._state.year, month
        )

        new_cents: Optional[int]
        if value.strip() == "":
            new_cents = None
            self._repo.delete_monthly_amount(category_id, self._state.year, month)
        else:
            cents = _parse_money_to_cents(value)
            if cents is None:
                # invalid input -> revert to previous display
                self._populate_grid_rows()
                return False
            new_cents = cents
            self._repo.set_monthly_amount(
                project.id, category_id, self._state.year, month, cents
            )

        if not self._undoing and not self._redoing:
            self._push_undo(
                project.id,
                self._state.year,
                month,
                category_id,
                old_cents,
                new_cents,
            )

        self._populate_grid_rows()
        self._refresh_dashboards()
        return True

    def _delete_grid_cell(self, item_id: str, col_id: str) -> None:
        """Delete the value at the given Month-breakdown cell (Backspace/Delete from highlight)."""
        project = self._state.selected_project
        if not project:
            return
        if col_id == "#1":
            return  # Month column is read-only
        if not item_id.startswith("m"):
            return
        try:
            month = int(item_id[1:])
        except ValueError:
            return
        col_index = int(col_id[1:]) - 1
        columns = list(self._grid_tree["columns"])
        if col_index < 0 or col_index >= len(columns):
            return
        col_name = columns[col_index]
        if not col_name.startswith("cat_"):
            return
        category_id = int(col_name.split("_", 1)[1])

        # Mirror the snapshot logic in ``_commit_grid_cell`` so a
        # Backspace/Delete or Cut can be undone with Ctrl+Z.
        old_cents = self._repo.get_monthly_amount_cents(
            category_id, self._state.year, month
        )

        self._repo.delete_monthly_amount(category_id, self._state.year, month)

        if not self._undoing and not self._redoing:
            self._push_undo(
                project.id,
                self._state.year,
                month,
                category_id,
                old_cents,
                None,
            )

        self._populate_grid_rows()
        self._refresh_dashboards()

    # ----- Month-grid keyboard navigation --------------------------------
    def _is_editable_grid_cell(self, item_id: str, col_id: str) -> bool:
        if not item_id.startswith("m"):
            return False
        try:
            col_index = int(col_id[1:]) - 1
        except (ValueError, TypeError):
            return False
        columns = list(self._grid_tree["columns"])
        if col_index < 0 or col_index >= len(columns):
            return False
        return columns[col_index].startswith("cat_")

    def _first_editable_col_id(self) -> Optional[str]:
        columns = list(self._grid_tree["columns"])
        for i, name in enumerate(columns):
            if name.startswith("cat_"):
                return f"#{i + 1}"
        return None

    def _grid_col_index(self, col_id: str) -> int:
        return int(col_id[1:]) - 1

    def _grid_col_id(self, col_index: int) -> str:
        return f"#{col_index + 1}"

    def _neighbor_grid_cell(
        self, item_id: str, col_id: str, direction: str
    ) -> Optional[tuple[str, str]]:
        """Next cell for Tab / arrows / Enter-down. Skips the read-only
        Month column. Returns ``None`` at the grid edge."""
        columns = list(self._grid_tree["columns"])
        rows = list(self._grid_tree.get_children(""))
        if not columns or not rows or item_id not in rows:
            return None

        row = rows.index(item_id)
        col = self._grid_col_index(col_id)
        if not self._is_editable_grid_cell(item_id, col_id):
            first = self._first_editable_col_id()
            if first is None:
                return None
            col = self._grid_col_index(first)

        if direction == "right":
            for i in range(col + 1, len(columns)):
                if columns[i].startswith("cat_"):
                    return (item_id, self._grid_col_id(i))
        elif direction == "left":
            for i in range(col - 1, -1, -1):
                if columns[i].startswith("cat_"):
                    return (item_id, self._grid_col_id(i))
        elif direction == "down":
            for r in range(row + 1, len(rows)):
                if columns[col].startswith("cat_"):
                    return (rows[r], self._grid_col_id(col))
        elif direction == "up":
            for r in range(row - 1, -1, -1):
                if columns[col].startswith("cat_"):
                    return (rows[r], self._grid_col_id(col))
        return None

    def _select_grid_cell(self, item_id: str, col_id: str) -> None:
        """Highlight one editable cell and scroll it into view."""
        if not self._is_editable_grid_cell(item_id, col_id):
            snapped = self._neighbor_grid_cell(item_id, col_id, "right")
            if not snapped:
                return
            item_id, col_id = snapped
        self._grid_highlight.select_cell(item_id, col_id)
        try:
            self._grid_tree.see(item_id)
        except Exception:
            pass

    def _on_grid_tree_key(self, event: tk.Event) -> str:
        """Spreadsheet keys on the Month grid (selection mode only)."""
        if self._grid_editor.is_editing:
            return ""
        keysym = getattr(event, "keysym", "") or ""
        if keysym == "Return":
            cell = self._grid_highlight.get_cell()
            if cell and self._is_editable_grid_cell(*cell):
                self._grid_editor.start_edit_cell(*cell)
            return "break"
        if keysym in ("Up", "Down", "Left", "Right"):
            self._move_grid_selection(
                {"Up": "up", "Down": "down", "Left": "left", "Right": "right"}[keysym]
            )
            return "break"
        if keysym in ("Tab", "ISO_Left_Tab"):
            # On Windows, Shift+Tab can come through as either Tab with
            # the Shift bit set in ``state`` or as ``ISO_Left_Tab``.
            backward = keysym == "ISO_Left_Tab" or bool(
                int(getattr(event, "state", 0)) & 0x1
            )
            self._move_grid_selection("left" if backward else "right")
            return "break"
        return ""

    def _move_grid_selection(self, direction: str) -> None:
        cell = self._grid_highlight.get_cell()
        if not cell:
            rows = list(self._grid_tree.get_children(""))
            first_col = self._first_editable_col_id()
            if rows and first_col:
                self._select_grid_cell(rows[0], first_col)
            return
        nxt = self._neighbor_grid_cell(*cell, direction)
        if nxt:
            self._select_grid_cell(*nxt)

    # ----- Undo / redo: Month-grid cell edits ----------------------------
    # The undo stack holds one entry per cell mutation triggered by the
    # user (typed value, Backspace/Delete, Cut, Paste). Ctrl+Z pops the
    # last entry and writes the previous cents back through the same
    # repository methods, then refreshes the grid and dashboards.
    def _push_undo(
        self,
        project_id: int,
        year: int,
        month: int,
        category_id: int,
        old_cents: Optional[int],
        new_cents: Optional[int],
    ) -> None:
        """Record an undoable cell change. A fresh edit clears the redo
        stack (standard editor behaviour). Capped to 100 entries."""
        self._redo_stack.clear()
        self._undo_stack.append({
            "project_id": project_id,
            "year": year,
            "month": month,
            "category_id": category_id,
            "old_cents": old_cents,
            "new_cents": new_cents,
        })
        if len(self._undo_stack) > 100:
            del self._undo_stack[:-100]

    def _clear_undo(self) -> None:
        """Drop undo and redo history (project / year / category scope)."""
        self._undo_stack.clear()
        self._redo_stack.clear()

    def _apply_grid_cents(
        self,
        project_id: int,
        year: int,
        month: int,
        category_id: int,
        cents: Optional[int],
    ) -> None:
        if cents is None:
            self._repo.delete_monthly_amount(category_id, year, month)
        else:
            self._repo.set_monthly_amount(
                project_id, category_id, year, month, cents
            )

    def _undo_grid_edit(self, _event: tk.Event) -> str:
        """Ctrl+Z handler — restore the last edited cell to its
        previous value. Returns ``"break"`` to stop other handlers from
        running, ``""`` when there's nothing to do (so other Ctrl+Z
        handlers, e.g. inside a focused Entry, can run instead)."""
        # When the focus is on a tk.Entry (e.g. the cell editor or
        # the "Add Entry" Value field), let that widget's own keys win.
        # ttk.Combobox is excluded because Ctrl+Z is meaningful only
        # inside text fields, not pickers.
        try:
            focus = self.focus_get()
        except Exception:
            focus = None
        if isinstance(focus, (tk.Entry, ttk.Entry)):
            return ""

        if not self._undo_stack:
            return "break"
        action = self._undo_stack.pop()

        # Skip the action if it belongs to a different project / year
        # than the one currently shown — _clear_undo *should* have
        # caught this on context-switch but the guard keeps the user
        # from seeing edits land on the wrong screen if it ever didn't.
        project = self._state.selected_project
        if (
            project is None
            or project.id != action["project_id"]
            or self._state.year != action["year"]
        ):
            return "break"

        self._undoing = True
        try:
            self._apply_grid_cents(
                project.id,
                action["year"],
                action["month"],
                action["category_id"],
                action["old_cents"],
            )
        finally:
            self._undoing = False

        self._redo_stack.append(action)
        self._populate_grid_rows()
        self._refresh_dashboards()
        return "break"

    def _redo_grid_edit(self, _event: tk.Event) -> str:
        """Ctrl+Y — re-apply the last undone cell edit."""
        try:
            focus = self.focus_get()
        except Exception:
            focus = None
        if isinstance(focus, (tk.Entry, ttk.Entry)):
            return ""

        if not self._redo_stack:
            return "break"
        action = self._redo_stack.pop()

        project = self._state.selected_project
        if (
            project is None
            or project.id != action["project_id"]
            or self._state.year != action["year"]
        ):
            return "break"

        self._redoing = True
        try:
            self._apply_grid_cents(
                project.id,
                action["year"],
                action["month"],
                action["category_id"],
                action["new_cents"],
            )
        finally:
            self._redoing = False

        self._undo_stack.append(action)
        self._populate_grid_rows()
        self._refresh_dashboards()
        return "break"

    # =====================================================================
    # Clipboard / Copy / Cut / Paste
    # =====================================================================
    # Each Treeview's TreeviewCellHighlight is wired to lambdas that call
    # back into this section. The Month Breakdown grid supports all four
    # operations; the read-only tables (Year Summary etc.) only support
    # Copy because their values are computed.
    def _set_clipboard_text(self, text: str) -> None:
        """Put ``text`` on the system clipboard (best-effort)."""
        try:
            self.clipboard_clear()
            if text:
                self.clipboard_append(text)
            # Force Tk to push the new selection to the OS clipboard.
            self.update()
        except Exception:
            pass

    def _get_clipboard_text(self) -> Optional[str]:
        try:
            return self.clipboard_get()
        except Exception:
            return None

    def _copy_tree_cell(self, tree: ttk.Treeview, item_id: str, col_id: str) -> None:
        """Copy the displayed text of a tree cell to the clipboard."""
        try:
            text = tree.set(item_id, col_id)
        except Exception:
            return
        self._set_clipboard_text(str(text))

    def _copy_grid_cell(self, item_id: str, col_id: str) -> None:
        self._copy_tree_cell(self._grid_tree, item_id, col_id)

    def _cut_grid_cell(self, item_id: str, col_id: str) -> None:
        # Month column is a label, not a value — refuse to cut.
        if col_id == "#1":
            return
        self._copy_tree_cell(self._grid_tree, item_id, col_id)
        self._delete_grid_cell(item_id, col_id)

    def _paste_grid_cell(self, item_id: str, col_id: str) -> None:
        if col_id == "#1":
            return
        text = self._get_clipboard_text()
        if text is None:
            return
        # _commit_grid_cell handles parsing + invalid-input revert + empty-delete.
        self._commit_grid_cell(item_id, col_id, text.strip())

    def _start_typing_grid_cell(self, item_id: str, col_id: str, char: str) -> None:
        """Open the cell editor pre-filled with ``char`` for direct typing."""
        # Month column is read-only, so don't enter edit mode there.
        if col_id == "#1":
            return
        col_index = int(col_id[1:]) - 1
        columns = list(self._grid_tree["columns"])
        if col_index < 0 or col_index >= len(columns):
            return
        col_name = columns[col_index]
        if not col_name.startswith("cat_"):
            return
        self._grid_editor.start_edit_cell(item_id, col_id, initial_text=char)

    # =====================================================================
    # Dashboards refresh
    # =====================================================================
    # Single big method that recomputes every read-only table (totals,
    # Month Breakdown, Year Summary, Year Breakdown) from the repository
    # and re-renders the charts. Called whenever a value or selection
    # changes.
    def _refresh_dashboards(self) -> None:
        project = self._state.selected_project
        if not project:
            return

        # Resolve the Month-Summary dropdown selection into a list of
        # months to aggregate over. "All" → every month of the year,
        # any single month name → just that month, anything else (e.g.
        # the dropdown wasn't built yet) → the project's current month.
        if hasattr(self, "_summary_month_var"):
            label = self._summary_month_var.get()
            if label == "All":
                summary_months = list(range(1, 13))
            elif label in EN_MONTHS:
                summary_months = [EN_MONTHS.index(label) + 1]
            else:
                summary_months = [self._state.month]
        else:
            summary_months = [self._state.month]

        # Treat magnitudes as the canonical amount: a category's *kind*
        # decides if it adds or subtracts in Net, regardless of how the
        # user typed it. ``abs`` guards against any stray signed value.
        #
        # Investment-kind cells contribute to ``exp`` (Total Expenses)
        # *only* when their stored value is negative (a loss / withdrawal):
        # the magnitude of that loss is folded into spending. Positive
        # investments (gains) stay out of Total Expenses. The dedicated
        # Investments cell still shows the signed sum either way.
        exp = sum(
            self._repo.get_project_month_expense_total(
                project.id, self._state.year, m
            )
            for m in summary_months
        )
        inc = sum(
            abs(self._repo.get_project_month_total_by_kind(
                project.id, self._state.year, m, CategoryKind.INCOME
            ))
            for m in summary_months
        )
        self._spend_var.set(_money_from_cents(exp))
        self._income_var.set(_money_from_cents(inc))
        net = inc - exp
        self._net_var.set(_money_from_cents(net))
        # Investments behaves like End of Month: cumulative signed
        # Investment cents from the very first month of the project up
        # to the latest month in the current selection. Positive = net
        # gain, negative = net loss across the whole life of the
        # project. Computed once below using the shared cutoff.
        if self._spend_value_label:
            self._spend_value_label.configure(foreground=self._red)
        if self._income_value_label:
            self._income_value_label.configure(foreground=self._green)
        if self._net_value_label:
            self._net_value_label.configure(foreground=self._green if net >= 0 else self._red)

        # Investments + End of Month share the same cutoff: latest
        # month of the current selection (or December when "All" is
        # picked). Both queries reach back through every prior
        # month/year of this project.
        cutoff_month = max(summary_months) if summary_months else self._state.month
        if hasattr(self, "_invest_var"):
            invest_total = self._repo.get_project_investment_through(
                project.id, self._state.year, cutoff_month
            )
            self._invest_var.set(_money_from_cents(invest_total))
        # Start of Month = running balance through the END of the
        # *previous* month. So "June" → through May 31; "January" →
        # through December 31 of the previous year; "All" → through
        # December 31 of the previous year (since the earliest month
        # in the selection is January). When the previous month falls
        # before any data the SQL returns 0, which is correct.
        if hasattr(self, "_som_var"):
            start_month_in_view = min(summary_months) if summary_months else self._state.month
            if start_month_in_view <= 1:
                som_year, som_month = self._state.year - 1, 12
            else:
                som_year, som_month = self._state.year, start_month_in_view - 1
            som_cents = self._repo.get_project_running_balance_through(
                project.id, som_year, som_month
            )
            self._som_var.set(_money_from_cents(som_cents))
        if hasattr(self, "_eom_var"):
            # Cumulative (income − expense + signed investment +
            # signed discrepancy) cents up to and including the cutoff.
            # The kind decides sign in the SQL, matching the Net rule
            # above.
            eom_cents = self._repo.get_project_running_balance_through(
                project.id, self._state.year, cutoff_month
            )
            self._eom_var.set(_money_from_cents(eom_cents))

        if hasattr(self, "_year_highlight"):
            self._year_highlight.clear()
        if hasattr(self, "_break_highlight"):
            self._break_highlight.clear()
        if hasattr(self, "_month_break_highlight"):
            self._month_break_highlight.clear()

        # Month Breakdown: per-category totals across the months
        # currently driving the Month Summary (single month, or all 12
        # if the user picked "All"). Investment and Discrepancy
        # categories preserve the signed sum so the user can see
        # gains/losses or up/down adjustments; Expense and Income
        # always display magnitude.
        signed_kinds = (CategoryKind.INVESTMENT, CategoryKind.DISCREPANCY)
        if hasattr(self, "_month_break_tree"):
            self._month_break_tree.delete(*self._month_break_tree.get_children())
            # Exclude temporary import columns — they aren't real categories
            # yet, so they stay out of every breakdown / total until assigned.
            real_cats = [c for c in self._categories if not getattr(c, "is_temporary", False)]
            for c in real_cats:
                raw = sum(
                    (self._repo.get_monthly_amount_cents(c.id, self._state.year, m) or 0)
                    for m in summary_months
                )
                cents = raw if c.kind in signed_kinds else abs(raw)
                self._month_break_tree.insert(
                    "", tk.END, values=(c.name, c.kind.label(), _money_from_cents(cents))
                )
            self._apply_month_breakdown_sort()
            # Show every category without internal scrolling.
            self._month_break_tree.configure(height=max(len(real_cats), 1))
            self._month_break_lines.redraw()

        self._year_tree.delete(*self._year_tree.get_children())
        # Mirror the Month Summary rule: ``e`` (the per-month "Spending"
        # column and the year-wide Total Expenses figure) is Expense
        # magnitudes plus the magnitude of any *negative* Investment
        # cells (losses). Positive investments (gains) stay out — they
        # show up only in the Investments column / cell. We also keep
        # running totals here so the Year Summary header strip can show
        # whole-year Total Expenses / Total Income / Net / Investments
        # without re-querying.
        year_exp_total = 0
        year_inc_total = 0
        year_invest_total = 0
        for m in range(1, 13):
            e = self._repo.get_project_month_expense_total(project.id, self._state.year, m)
            i = abs(self._repo.get_project_month_total_by_kind(project.id, self._state.year, m, CategoryKind.INCOME))
            # Investment kept signed (gains add to year total, losses subtract).
            inv = self._repo.get_project_month_total_by_kind(project.id, self._state.year, m, CategoryKind.INVESTMENT)
            year_exp_total += e
            year_inc_total += i
            year_invest_total += inv
            self._year_tree.insert(
                "",
                tk.END,
                values=(EN_MONTHS[m - 1], _money_from_cents(e), _money_from_cents(i), _money_from_cents(i - e)),
            )
        self._year_tree.configure(height=12)
        self._year_lines.redraw()

        # Year Summary header totals: red expense, green income, green
        # / red Net depending on sign — same convention as Month Summary.
        # Investments stay in default text colour (matches the Month
        # Summary cell next to End of Month).
        if hasattr(self, "_year_spend_var"):
            self._year_spend_var.set(_money_from_cents(year_exp_total))
            self._year_income_var.set(_money_from_cents(year_inc_total))
            year_net_total = year_inc_total - year_exp_total
            self._year_net_var.set(_money_from_cents(year_net_total))
            if hasattr(self, "_year_invest_var"):
                self._year_invest_var.set(_money_from_cents(year_invest_total))
            if self._year_spend_value_label:
                self._year_spend_value_label.configure(foreground=self._red)
            if self._year_income_value_label:
                self._year_income_value_label.configure(foreground=self._green)
            if self._year_net_value_label:
                self._year_net_value_label.configure(
                    foreground=self._green if year_net_total >= 0 else self._red
                )

        self._break_tree.delete(*self._break_tree.get_children())
        # Temporary import columns are excluded until the user assigns them.
        year_real_cats = [c for c in self._categories if not getattr(c, "is_temporary", False)]
        for c in year_real_cats:
            # Per-category yearly total. Expense / Income show the
            # magnitude (``abs``) since their sign is fixed by kind.
            # Investment and Discrepancy keep the signed sum: Investment
            # so gains and losses cancel out (true ROI), Discrepancy so
            # the cumulative reconciliation nudge is visible.
            raw = self._repo.get_year_total_cents(c.id, self._state.year)
            total = raw if c.kind in (CategoryKind.INVESTMENT, CategoryKind.DISCREPANCY) else abs(raw)
            self._break_tree.insert("", tk.END, values=(c.name, c.kind.label(), _money_from_cents(total)))

        self._apply_year_breakdown_sort()
        self._break_tree.configure(height=max(len(year_real_cats), 1))
        self._break_lines.redraw()

        if hasattr(self, "_charts"):
            self._charts.refresh_charts()

    # =====================================================================
    # Tree styling helpers
    # =====================================================================
    # Row height management, gridline colour, hiding the default full-row
    # blue selection so TreeviewCellHighlight can paint the per-cell one
    # instead.
    def _bind_dynamic_rowheight(self, tree: ttk.Treeview, style_name: str, rows: Optional[int]) -> None:
        # Adjust row height so content uses available vertical space.
        tree.bind(
            "<Configure>",
            lambda _e, t=tree, sn=style_name, r=rows: self._update_dynamic_rowheight(t, sn, r),
            add=True,
        )
        self._update_dynamic_rowheight(tree, style_name, rows)

    def _update_dynamic_rowheight(self, tree: ttk.Treeview, style_name: str, rows: Optional[int]) -> None:
        # rows:
        # - fixed 12 for month-based tables
        # - None for variable tables (use current item count, at least 1)
        visible_rows = rows if rows is not None else max(len(tree.get_children()), 1)
        height_px = tree.winfo_height()
        if height_px <= 1:
            return

        # roughly account for heading area so rows fill remaining space better
        heading_px = 28
        available = max(height_px - heading_px, 1)
        row_h = max(int(available / visible_rows), 18)
        self._style.configure(style_name, rowheight=row_h)

    def _apply_tree_gridlines(self, style_name: str) -> None:
        # Treeview doesn't have true cell borders across all themes, but setting these
        # style properties produces visible separators on Windows themes.
        c = self._gridline_color
        self._style.configure(
            style_name,
            bordercolor=c,
            lightcolor=c,
            darkcolor=c,
            relief="solid",
            borderwidth=1,
        )
        self._style.configure(
            f"{style_name}.Heading",
            bordercolor=c,
            lightcolor=c,
            darkcolor=c,
            relief="solid",
            borderwidth=1,
        )

    def _suppress_row_selection_color(self, style_name: str) -> None:
        # Hide the default full-row blue selection so we can paint a single-cell
        # highlight via TreeviewCellHighlight instead.
        self._style.map(
            style_name,
            background=[("selected", "#ffffff")],
            foreground=[("selected", "#000000")],
        )

    def _clear_cell_highlights(self) -> None:
        for name in ("_grid_highlight", "_year_highlight", "_break_highlight", "_month_break_highlight"):
            h = getattr(self, name, None)
            if h is not None:
                try:
                    h.clear()
                except Exception:
                    pass

    # =====================================================================
    # Category management (drag/right-click on grid heading)
    # =====================================================================
    # Right-clicking a category column in the Month grid opens a menu
    # with Rename, change kind (Expense / Income / Investment), and
    # Delete actions; left-press-and-drag on a heading lets the user
    # reorder categories. Both flows mutate the DB and reload the view.
    def _on_grid_heading_press(self, event: tk.Event) -> None:
        """Begin a potential drag-reorder when the press lands on a category heading."""
        try:
            region = self._grid_tree.identify("region", event.x, event.y)
        except Exception:
            return
        if region != "heading":
            return
        col_id = self._grid_tree.identify_column(event.x)
        if not col_id or col_id == "#1":
            return  # Month column is fixed
        cols = list(self._grid_tree["displaycolumns"])
        try:
            idx = int(col_id[1:]) - 1
        except ValueError:
            return
        if idx < 0 or idx >= len(cols):
            return
        col_name = cols[idx]
        if not col_name.startswith("cat_"):
            return
        self._grid_drag = {"col": col_name, "started": False, "start_x": event.x}

    def _on_grid_heading_motion(self, event: tk.Event) -> None:
        """Switch the cursor to a drag glyph once the press has clearly moved."""
        if not self._grid_drag.get("col"):
            return
        if self._grid_drag.get("started"):
            return
        # Small dead-zone so a mis-click doesn't initiate a drag.
        if abs(event.x - int(self._grid_drag["start_x"])) > 4:
            self._grid_drag["started"] = True
            try:
                self._grid_tree.configure(cursor="fleur")
            except Exception:
                pass

    def _on_grid_heading_release(self, event: tk.Event) -> None:
        """Drop the dragged column at the heading currently under the cursor."""
        src = self._grid_drag.get("col")
        started = bool(self._grid_drag.get("started"))
        # Reset state regardless of outcome so the cursor never gets stuck.
        self._grid_drag = {"col": None, "started": False, "start_x": 0}
        try:
            self._grid_tree.configure(cursor="")
        except Exception:
            pass
        if not src or not started:
            return

        target_col = self._grid_tree.identify_column(event.x)
        if not target_col or target_col == "#1":
            return
        cols = list(self._grid_tree["displaycolumns"])
        try:
            target_idx = int(target_col[1:]) - 1
        except ValueError:
            return
        if target_idx < 0 or target_idx >= len(cols):
            return
        target = cols[target_idx]
        if target == src or not target.startswith("cat_"):
            return

        # Compute the new order by lifting ``src`` out and re-inserting it
        # next to ``target``. Dragging right inserts AFTER target so the
        # dropped column visually lands where the cursor is.
        new_cols = [c for c in cols if c != src]
        insert_at = new_cols.index(target)
        if target_idx > cols.index(src):
            insert_at += 1
        new_cols.insert(insert_at, src)

        cat_ids = [int(c.split("_", 1)[1]) for c in new_cols if c.startswith("cat_")]
        try:
            self._repo.set_category_sort_orders(cat_ids)
        except Exception:
            return
        self._reload_project_view()

    def _open_month_grid_context_menu(self, event: tk.Event) -> None:
        # Right-click on a column header to delete that category.
        region = self._grid_tree.identify("region", event.x, event.y)
        if region != "heading":
            return
        col_id = self._grid_tree.identify_column(event.x)  # "#1", "#2", ...
        if col_id == "#1":
            return  # Month column
        col_index = int(col_id[1:]) - 1
        columns = list(self._grid_tree["columns"])
        if col_index < 0 or col_index >= len(columns):
            return
        col_name = columns[col_index]
        if not col_name.startswith("cat_"):
            return
        category_id = int(col_name.split("_", 1)[1])
        cat = next((c for c in getattr(self, "_categories", []) if c.id == category_id), None)
        if not cat:
            return

        # With four kinds we surface each "other" kind as its own
        # menu entry rather than a single toggle. Order is fixed
        # (Expense → Income → Investment → Discrepancy) so the menu
        # doesn't reshuffle as the user changes a category's kind.
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label="Rename category",
            command=lambda cid=category_id: self._rename_category(cid),
        )
        for k in (
            CategoryKind.EXPENSE,
            CategoryKind.INCOME,
            CategoryKind.INVESTMENT,
            CategoryKind.DISCREPANCY,
        ):
            if k == cat.kind:
                continue
            menu.add_command(
                label=f"Change to {k.label()}",
                command=lambda cid=category_id, kk=k: self._change_category_kind(cid, kk),
            )
        menu.add_separator()
        menu.add_command(
            label=f"Delete category “{cat.name}”",
            command=lambda: self._delete_category(category_id),
        )
        menu.tk_popup(event.x_root, event.y_root)

    def _rename_category(self, category_id: int) -> None:
        project = self._state.selected_project
        if not project:
            return
        cat = next((c for c in getattr(self, "_categories", []) if c.id == category_id), None)
        if not cat:
            return
        new_name = simpledialog.askstring(
            "Rename category",
            f"New name for “{cat.name}”:",
            initialvalue=cat.name,
            parent=self,
        )
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name or new_name == cat.name:
            return
        self._repo.rename_category(category_id, new_name)
        self._reload_project_view()

    def _change_category_kind(self, category_id: int, new_kind: CategoryKind) -> None:
        project = self._state.selected_project
        if not project:
            return
        self._repo.set_category_kind(category_id, new_kind)
        self._reload_project_view()

    def _delete_category(self, category_id: int) -> None:
        project = self._state.selected_project
        if not project:
            return
        cat = next((c for c in getattr(self, "_categories", []) if c.id == category_id), None)
        name = cat.name if cat else "this category"
        ok = messagebox.askyesno(
            title="Confirm delete",
            message=f"Delete category \"{name}\"?\nAll monthly values for it will be removed.",
        )
        if not ok:
            return
        self._repo.delete_category(category_id)
        # Pending undo entries may still reference this category. Drop
        # them all rather than try to filter — simpler and matches what
        # users expect after a destructive action.
        self._clear_undo()
        self._reload_project_view()

    # =====================================================================
    # Header-click sorting (Year Breakdown & Month Breakdown)
    # =====================================================================
    # Clicking a column header sorts ascending; clicking the same column
    # again toggles to descending. Sort state is remembered per-table so
    # a refresh keeps the user's chosen ordering.
    def _sort_year_breakdown(self, col: str) -> None:
        # Toggle if clicking same column again, otherwise default to ascending.
        if getattr(self, "_break_sort_col", None) == col:
            self._break_sort_asc = not getattr(self, "_break_sort_asc", True)
        else:
            self._break_sort_col = col
            self._break_sort_asc = True
        self._apply_year_breakdown_sort()

    def _apply_year_breakdown_sort(self) -> None:
        if not hasattr(self, "_break_tree"):
            return
        col = getattr(self, "_break_sort_col", None)
        if not col:
            return

        items = list(self._break_tree.get_children(""))
        if not items:
            return

        def item_values(iid: str) -> tuple[str, str, str]:
            v = self._break_tree.item(iid, "values")
            return (str(v[0]), str(v[1]), str(v[2])) if v else ("", "", "")

        asc = bool(getattr(self, "_break_sort_asc", True))

        if col == "name":
            key = lambda iid: item_values(iid)[0].lower()
            reverse = not asc
        elif col == "type":
            # Income/Expense ordering (toggle flips which comes first)
            income_first = asc

            def key(iid: str) -> tuple[int, str]:
                typ = item_values(iid)[1]
                rank = 0 if typ == "Income" else 1
                if not income_first:
                    rank = 1 - rank
                return (rank, item_values(iid)[0].lower())

            reverse = False
        elif col == "total":
            def cents(iid: str) -> int:
                c = _parse_money_to_cents(item_values(iid)[2])
                return 0 if c is None else c

            key = cents
            reverse = not asc
        else:
            return

        sorted_items = sorted(items, key=key, reverse=reverse)
        for idx, iid in enumerate(sorted_items):
            self._break_tree.move(iid, "", idx)
        if hasattr(self, "_break_lines"):
            self._break_lines.redraw()

    def _sort_month_breakdown(self, col: str) -> None:
        if getattr(self, "_month_break_sort_col", None) == col:
            self._month_break_sort_asc = not getattr(self, "_month_break_sort_asc", True)
        else:
            self._month_break_sort_col = col
            self._month_break_sort_asc = True
        self._apply_month_breakdown_sort()

    def _apply_month_breakdown_sort(self) -> None:
        if not hasattr(self, "_month_break_tree"):
            return
        col = getattr(self, "_month_break_sort_col", None)
        if not col:
            return

        items = list(self._month_break_tree.get_children(""))
        if not items:
            return

        def item_values(iid: str) -> tuple[str, str, str]:
            v = self._month_break_tree.item(iid, "values")
            return (str(v[0]), str(v[1]), str(v[2])) if v else ("", "", "")

        asc = bool(getattr(self, "_month_break_sort_asc", True))

        if col == "name":
            key = lambda iid: item_values(iid)[0].lower()
            reverse = not asc
        elif col == "type":
            income_first = asc

            def key(iid: str) -> tuple[int, str]:
                typ = item_values(iid)[1]
                rank = 0 if typ == "Income" else 1
                if not income_first:
                    rank = 1 - rank
                return (rank, item_values(iid)[0].lower())

            reverse = False
        elif col == "total":
            def cents(iid: str) -> int:
                c = _parse_money_to_cents(item_values(iid)[2])
                return 0 if c is None else c

            key = cents
            reverse = not asc
        else:
            return

        sorted_items = sorted(items, key=key, reverse=reverse)
        for idx, iid in enumerate(sorted_items):
            self._month_break_tree.move(iid, "", idx)
        if hasattr(self, "_month_break_lines"):
            self._month_break_lines.redraw()

    # =====================================================================
    # View switching
    # =====================================================================
    # Both views are pre-built once in __init__; switching between them
    # is just a pack_forget / pack pair. Keeps state between visits so
    # users return to the same project / scroll position.
    def _show_home(self) -> None:
        self._project.pack_forget()
        self._home.pack(fill=tk.BOTH, expand=True)

    def _show_project(self) -> None:
        self._home.pack_forget()
        self._project.pack(fill=tk.BOTH, expand=True)


# =====================================================================
# App bootstrap
# =====================================================================
# Creates the per-user app folder, opens the SQLite repository, builds
# the Tk root window, and wires the WM close protocol so the connection
# is closed cleanly on quit.
def _project_root() -> Path:
    """Project root that ``FALogo.png`` / ``.ico`` live in.

    Resolves to ``sys._MEIPASS`` when running as a PyInstaller
    one-file build (where bundled data is unpacked at runtime),
    otherwise to the directory three levels above this file
    (``finance_app/ui/main_window.py`` → project root).
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent.parent.parent


def _apply_window_icon(root: tk.Tk) -> None:
    """Apply ``FALogo`` as the title-bar + taskbar icon.

    Uses **only** ``iconbitmap(default=…)`` with the multi-size
    ``FALogo.ico``. On Windows that file holds proper 16/24/32/48/
    64/128/256 px frames so the OS picks the closest size for each
    surface (title bar, taskbar, alt-tab) without rescaling — that
    keeps it crisp at every DPI.

    On non-Windows fall back to the PNG via ``iconphoto`` (Linux /
    macOS Tk wants a PhotoImage there, and the title-bar geometry
    is more forgiving so a single mid-size image is fine).
    """
    root_dir = _project_root()
    ico = root_dir / "FALogo.ico"
    png = root_dir / "FALogo.png"

    if sys.platform == "win32":
        if ico.exists():
            try:
                root.iconbitmap(default=str(ico))
            except Exception:
                pass
        return

    if png.exists():
        try:
            from PIL import Image, ImageTk  # type: ignore

            src = Image.open(str(png)).convert("RGBA").resize(
                (64, 64), Image.LANCZOS
            )
            img = ImageTk.PhotoImage(src, master=root)
            root.iconphoto(True, img)
            root._app_icon_image = img  # type: ignore[attr-defined]
        except Exception:
            pass


def _set_windows_app_id() -> None:
    """Give the running process its own Windows taskbar group so the
    OS uses our icon there instead of inheriting ``python.exe``'s.

    No-op outside Windows or when the API isn't available.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "FinanceApp.FA.1"
        )
    except Exception:
        pass


def _create_root() -> tk.Tk:
    """Create the Tk root, preferring TkinterDnD's subclass so the CSV
    import drop-zone can receive files dragged from the OS file manager.

    Returns a plain ``tk.Tk()`` if ``tkinterdnd2`` (or its bundled native
    ``tkdnd`` extension) can't be loaded, so the app always launches.
    The drop-zone checks ``DND_AVAILABLE`` and degrades to button-only.
    """
    global DND_AVAILABLE
    try:
        from tkinterdnd2 import TkinterDnD

        root = TkinterDnD.Tk()
        DND_AVAILABLE = True
        return root
    except Exception:
        DND_AVAILABLE = False
        return tk.Tk()


def run_app() -> None:
    ensure_app_data_exists()
    repo = FinanceRepository(database_path())
    repo.ensure_created()

    # Must run *before* ``tk.Tk()`` so the taskbar entry is registered
    # under our own group rather than under python.exe.
    _set_windows_app_id()

    # Use TkinterDnD's Tk subclass so the CSV import drop-zone can accept
    # files dragged from File Explorer. Falls back to a plain Tk root if
    # the optional dependency / native tkdnd extension isn't available, so
    # the app still launches (drag-and-drop just silently disables).
    root = _create_root()
    root.title("Finance App")
    root.minsize(720, 560)
    # Open wide enough that the Add Entry row (incl. the import buttons) fits
    # without overflow, but never larger than the screen; centre the window.
    want_w, want_h = 1240, 840
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    win_w = min(want_w, screen_w - 80)
    win_h = min(want_h, screen_h - 80)
    pos_x = max((screen_w - win_w) // 2, 0)
    pos_y = max((screen_h - win_h) // 2 - 20, 0)
    root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
    _apply_window_icon(root)

    app = MainWindow(root, repo)
    app.pack(fill=tk.BOTH, expand=True)

    def on_close() -> None:
        try:
            repo.close()
        finally:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()

