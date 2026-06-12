# FinanceApp

A local Windows desktop app for tracking monthly expenses and income across one or more "projects" (e.g. a bank account, a household budget, etc.). Built with Python + Tkinter and a local SQLite database — nothing leaves your computer.

---

## Quick start

### Option A — Use the prebuilt executable (recommended for non-developers)

1. Get `FinanceApp.exe` (from the `dist/` folder after a build, or wherever it was sent to you).
2. Double-click it. No install, no Python required.
3. Your data is saved to `%LOCALAPPDATA%\FinanceApp\finance.db` (the app creates this on first launch).

### Option B — Run from source

Requires Python 3.9+.

```powershell
cd "<path to>\FinanceApp_Python"
pip install -r requirements.txt
python main.py
```

`requirements.txt` pulls in **matplotlib** (charts) and **tkinterdnd2** (drag-and-drop for CSV import). If `tkinterdnd2` is missing the app still runs — you just lose the drag-and-drop zone and use the **Import statement…** button instead.

---

## How to use

### 1. Home screen

When you launch the app you land on the **Home** screen.

| Action | How |
|---|---|
| **Create a project** | Type a name into the box at the top, click **Create Project** (or press Enter). |
| **Open a project** | Double-click it in **Favourites** or **Recents**. |
| **Favourite / Unfavourite a project** | Right-click it → choose **Favourite** / **Unfavourite**. |
| **Delete a project** | Right-click it → **Delete**. Confirmation dialog. *Cannot be undone.* |
| **Backup the database** | Click **Backup database** (bottom-right). Creates a timestamped `finance.backup-YYYYMMDD-HHMMSS.db` next to your live database. |

The **DB:** label shows where your database file lives. Recents are sorted by most-recently-opened first.

### 2. Project view

Opening a project takes you to the main screen. The top header has:

- **← Back** — return to Home.
- **Project title** — the project's name.
- **Year** and **Month** dropdowns — drive every section below.

Underneath that is the **Add Entry** box, then a vertically-scrollable column of collapsible sections (click the chevron / title to expand or collapse).

#### Add Entry

Fastest way to add a new category and a value at once:

1. Type a **Category name** (e.g. `Rent`).
2. Pick **Expense**, **Income**, **Investment**, or **Discrepancy**.
3. Type a **Value** (e.g. `1200`, `1234.56`, `$1,234.56`). For Expense / Income the sign is ignored; for Investment and Discrepancy the sign is **kept** (e.g. `-50` records a loss / a downward adjustment). See *How values & signs work* below.
4. Click **Add** or press Enter.

The category is created for the *currently selected year* and the value is stored under the *currently selected month*.

> **Investment vs Expense:** Both feed into your **End of Month** running balance, but they behave differently:
> - An **Expense** is always an outflow — typing `50` or `-50` both record $50 spent.
> - An **Investment** keeps its sign so you can track whether you're up or down. `50` is a gain (+$50 toward End of Month), `-50` is a loss (−$50 toward End of Month).
>
> Investments get their own bold figure beside Net and a dark-blue series on every chart. They don't roll into Total Income or Net, but a *negative* investment (loss) does fold into **Total Expenses**.

> **Discrepancy:** A bookkeeper-style reconciliation bucket. Use it when your **End of Month** doesn't quite match your real bank balance and you just need a number to plug the gap. It keeps its sign (positive nudges End of Month up, negative nudges it down) and **only** affects End of Month — it does NOT show up in Total Expenses, Total Income, Net, Investments, or any chart. Add a single Discrepancy-kind category called e.g. `Adjustment` and drop the missing amount in the relevant month.

> **Note:** Categories are scoped to a year. Switching years gets you a fresh slate (the first time you open a year that has no categories yet, the app copies the previous year's category list as a starting point — only the names/types are copied, not the values).

#### Import bank statements (CSV)

Instead of typing every transaction by hand, you can import a bank-statement CSV and let the app fill the grid for you. The controls live on the right-hand side of the **Add Entry** box:

- **Import CSV…** — opens a file picker for a `.csv` file.
- **Drop .CSV file here** — drag a `.csv` straight from File Explorer onto this zone. *(Drag-and-drop needs the optional `tkinterdnd2` package — see Quick start. Without it you still get the button.)*
- **Undo last import** — sits to the right of the drop zone; reverses the most recent import for the current project (see *Duplicate-import safeguards* below). Disabled when there's nothing to undo.
- **Manage mappings…** — top-right corner of the Add Entry box; opens the saved-rules editor (see *Managing saved rules* below).

**What happens on import:**

1. **Format detection.** The first time you import a given bank's export, the app auto-detects the layout (which columns hold the date / description / amount, whether there's a header row, the date format, and whether amounts are one signed column or separate debit/credit columns) and shows a **Set up bank CSV format** dialog with a live preview. Confirm or correct it, give it a name, and import.
2. **Remembered formats.** That layout is saved as a *format profile* keyed by the file's structure, so the next CSV from the same bank imports in one click — no dialog.
3. **Rows land in the right month/year.** Every transaction is placed into the correct month and year cell based on its date (the *Value Date* inside the description if present, otherwise the row's leading date). A statement spanning multiple months/years is split correctly.
4. **Merchants become temporary categories.** Each distinct merchant gets its own column, flagged with a **light-red header** so you can tell it apart. Until you assign it, a temporary category is **excluded from every total, breakdown, and chart** (Total Income/Expenses, Net, Investments, Start/End of Month) — it only shows in the editable grid.
5. **A notice appears** in the Add Entry box: *"Please assign temporary categories"* with an **Assign now** button.

**Assigning merchants** (the **Assign now** window):

A table lists every unassigned merchant. For each one you choose:

| Column | What it does |
|---|---|
| **Type** | `Expense`, `Income`, `Investment`, `Discrepancy`, or `Transfer`. Drives the sign rules exactly like a normal category (see *How values & signs work*). |
| **Final category name** | A dropdown of your existing and saved category names — pick one to fold the merchant into it. Choose **Other (type a new name)** at the top of the list to switch the box to free text and create a brand-new category (use the ▾ button to switch back to the dropdown). |
| **Mapping** | `Per-Project` (remembered only in this project) or `Global` (remembered across all projects). |

- **Merging:** several merchants given the **same name + type** merge into a single column, with their values summed — exactly like the manual grid.
- **Transfer type:** money moved between your own accounts. Assigning a merchant to **Transfer** removes its column from the grid entirely (it's neither income nor expense). Transfer-style rows keep their *full* description as the temporary name (not shortened) so you can tell which transfer is which.
- **Remembered rules:** once assigned, the same merchant in a future CSV auto-assigns straight to its final category — no temporary step. `Global` rules apply in every project; `Per-Project` rules only in the project you saved them in.
- **Managing saved rules:** click **Manage mappings…** (top-right of the Add Entry box) to see every saved merchant rule. You can edit each rule's **Type**, **Final category** (the same dropdown of existing names, with **Other** to type a new one), and **Mapping** scope inline, then click **Save** (switching a rule to *Per-Project* applies it to the project you're viewing). Or click **Delete** on a row to forget it — that merchant will become a temporary category again on the next import. Editing or deleting a rule does **not** touch amounts already imported into your categories.
- **Partial saves:** click **Save** and any fully-filled rows are assigned immediately. The window closes either way. Rows missing a Type, Mapping, or name stay temporary, and your in-progress selections are saved as a draft so they're restored when you reopen the window via **Assign now**.

**Duplicate-import safeguards:**

The app remembers every transaction it has imported into a project, so importing carefully never silently double-counts:

- **Confirmation first.** Before anything changes, a dialog shows how many transactions are new versus already-imported, and you confirm the import.
- **Duplicate detection.** Each transaction is identified by its date, amount and description. Re-importing the **same file** adds nothing (every row is recognised as already imported); importing an **overlapping** statement only adds the transactions it hasn't seen before. Two genuinely-identical transactions (e.g. two same-priced coffees on the same day) are both kept.
- **No duplicate columns.** Re-importing an unassigned merchant reuses its existing temporary column instead of creating a second one.
- **Undo last import.** Click **Undo last import** to roll back the most recent import for the project — it subtracts the amounts that import added and removes any temporary columns it created. Merchants you've already assigned to a real category since importing can't be reversed and are left as-is, so undo right after importing if you want a clean rollback.
- **Manual edits win.** The app stores one value per cell with no record of which part came from a CSV, so a manual edit simply overrides the cell. Future imports only add genuinely-new transactions on top of whatever is there.

> **Tip:** Right-click a light-red temporary column header in the grid to jump straight to **Assign now**, or to delete that merchant column outright.

#### Month Summary

- A **Month** dropdown that scopes this section. Pick a single month, or **All** to aggregate every month of the currently-selected year.
- Six live totals at the top section:
  - **Total Expenses** (red) — sum of Expense-kind cents across the selected months, **plus the magnitude of any Investment-kind cells whose value is negative** (i.e. investment losses count as money out). Positive investments (gains) are not included here.
  - **Total Income** (green) — sum of Income-kind cents across the selected months.
  - **Net** (green if ≥ 0, red if < 0) — `Total Income − Total Expenses` for the selected months only. Because investment **losses** are folded into Total Expenses, they pull Net down; investment **gains** stay out of Net entirely (they only show up in End of Month).
  - **Investments** (default text colour) — **cumulative signed** sum of every Investment-kind cell in the project from the very first month up to the latest month in your current selection. Tracks the *lifetime* investment outcome: positive = net gain over time, negative = net loss. Carries across years exactly like End of Month. A loss also folds into Total Expenses for the period it occurred — that's the only place an investment appears outside this cell and End of Month.
  - **Start of Month**  — running bank-balance figure carried across years, computed through the **end of the month *before*** the earliest month in your selection. In other words, it's "what you walked into the month with":
    - Pick **June** → Start of Month is the End of Month value for May (i.e. through May 31).
    - Pick **January** → Start of Month wraps to December 31 of the previous year.
    - Pick **All** → Start of Month is December 31 of the previous year (since the earliest month is January).
    - If there's no project data at all before that point, this reads `$0.00`.
  - **End of Month** — running bank-balance figure carried across years, computed through the **end of the latest month** in your selection: a single month → through end of that month; "All" → through end of December. Formula: `Income − Expense + Investment + Discrepancy` (Investment / Discrepancy kept signed).

  Example: in January 2025 you record `+$500` of income and a `+$200` Investment gain. With **Month** set to **January 2025**, **Start of Month** reads `$0.00` (nothing before Jan 1), **Investments** reads `$200.00`, and **End of Month** reads `$700.00`. Switch to **February 2025** (no new entries) — Start of Month becomes `$700.00` (you walked in with January's closing balance), End of Month stays `$700.00`. Switch to **All 2025** — Start of Month wraps back to `$0.00` (Dec 31 2024), End of Month stays `$700.00`. If in March 2026 you record a `-$50` Investment loss and view **March 2026**, Start of Month is `$700.00` (Feb 28 2026 closing), End of Month is `$650.00`.

- The **editable 12-row grid** — one row per month, one column per category. This is where you do most of your data entry.

> **Note:** The Month dropdown also drives the **Month Breakdown** section below — its per-category totals follow the same single-month / "All" choice you make here.

**Editing the grid:**

| Action | How |
|---|---|
| Select a cell | Click it (highlights in blue). |
| Edit a value | Double-click a cell, OR start typing while a cell is selected. Press Enter to commit, Esc to cancel. |
| Clear a value | Select the cell and press **Backspace** or **Delete**. |
| Copy / Cut / Paste | Select a cell, use **Ctrl+C / Ctrl+X / Ctrl+V**, or right-click → menu. (The Month column itself is read-only.) |
| Rename a category | Right-click on the **column header** → **Rename category**. |
| Change kind | Right-click on the **column header** → **Change to Expense** / **Change to Income** / **Change to Investment** / **Change to Discrepancy** (only the *other* three kinds appear). |
| Reorder a category | **Click and drag** the column header sideways onto another column header. Drop it left of a header to insert before, right of it to insert after. The Month column stays fixed at the leftmost position. The new order is saved per-year. |
| Delete a category | Right-click on the **column header** → **Delete category "name"**. *Removes all values for that category in this year.* |

Money is auto-formatted on commit: `1234` becomes `$1,234.00`. Empty / unparseable input is rejected (the cell reverts).

**Clear selection:** press **Esc** or click an empty area of the window.

**Keyboard shortcuts (Month grid):**

When a cell is **selected** (single-click highlight, editor not open):

| Key | Action |
|---|---|
| **Enter** | Start editing the selected cell |
| **↑ ↓ ← →** | Move the selection between cells (skips the read-only Month column) |
| **Tab** / **Shift+Tab** | Move the selection right / left |
| **Ctrl+C** / **Ctrl+X** / **Ctrl+V** | Copy / cut / paste |
| **Backspace** / **Delete** | Clear the cell |
| **Ctrl+Z** / **Ctrl+Y** | Undo / redo the most recent cell edit |
| **Esc** | Clear the selection |

When **editing** a cell (the Entry overlay is open):

| Key | Action |
|---|---|
| **Enter** | Save and move the selection **down** (cell stays highlighted, editor closes — like Excel) |
| **Tab** / **Shift+Tab** | Save and **open the editor** on the next / previous category cell on the same row |
| **Esc** | Cancel without saving |
| Click outside | Save and close |

> **Undo / redo scope:** `Ctrl+Z` and `Ctrl+Y` only cover Month-grid cell edits (typing, paste, Backspace/Delete, Cut). The stack is cleared automatically when you switch project, switch year, or delete a category — those actions change which cells exist, so replaying old edits would land on the wrong place.

#### How values & signs work

The app applies the sign rule based on the category's kind:

> **Expense / Income** — the kind decides the sign. Whatever sign you type is normalised away.
> **Investment / Discrepancy** — the sign you type is **kept**.

| You type | Category kind | Cell shows | Counts as |
|---|---|---|---|
| `50`     | Expense     | `$50.00`  | $50 spent |
| `-50`    | Expense     | `$50.00`  | $50 spent |
| `(50)`   | Expense     | `$50.00`  | $50 spent |
| `50`     | Income      | `$50.00`  | $50 received |
| `-50`    | Income      | `$50.00`  | $50 received |
| `50`     | Investment  | `$50.00`  | +$50 gain (adds to End of Month) |
| `-50`    | Investment  | `-$50.00` | $50 loss  (subtracts from End of Month, also folds into Total Expenses) |
| `50`     | Discrepancy | `$50.00`  | +$50 reconciliation nudge (adds to End of Month only) |
| `-50`    | Discrepancy | `-$50.00` | −$50 reconciliation nudge (subtracts from End of Month only) |

**Recording a refund** (e.g. you spent $50 and later got $50 back):

- Add an Income-kind category called something like `Refunds` and put the $50 there, **or**
- Right-click an existing category header → **Change to Income** if it should switch sides permanently.

You can't record a refund as a negative inside an Expense category — the sign gets normalised away.

**Recording an investment loss / withdrawal:** type a negative value (e.g. `-200`) directly into an Investment-kind cell. The cell will show `-$200.00`, and:

- Your **End of Month** running balance drops by $200.
- The **Investments** header total drops by $200 (the signed sum reflects the loss).
- Your **Total Expenses** figure goes *up* by $200 — a loss is treated as money out, so it folds into spending. As a result **Net** drops by $200 too.

A positive investment (a gain) only affects **Investments** and **End of Month**; it does not touch Total Expenses or Net.

**Reconciling End of Month with your bank balance:** add a category like `Adjustment` of kind **Discrepancy**. Type whatever signed value brings End of Month in line with your real bank statement (e.g. `-3.27` if the app reads $3.27 high). Discrepancy entries don't appear anywhere except in their cell and in End of Month — they won't pollute Total Expenses, Net, Investments, or any chart.

#### Month Breakdown

A read-only table of per-category totals for the **currently-selected month**. Click any column header to sort (ascending → descending → ascending). Right-click a cell to copy its value.

#### Year Summary

- Four live totals on the **left half** of the section (same layout as Month Summary, minus End of Month): **Total Expenses** (red, **including investment losses**), **Total Income** (green), **Net** (green / red), **Investments** (default, **signed** — gains positive, losses negative), summed across the entire selected year. The per-month *Expense* column in the table below uses the same rule.
- A 12-row table of **Expense / Income / Net** for every month of that year. Right-click a cell to copy.

> The cross-year running balance lives in **End of Month** in *Month Summary* above, not in this table. The Investments figure here is year-scoped on purpose — for the lifetime cumulative figure, look at the Investments cell in **Month Summary**.

#### Year Breakdown

Per-category yearly totals across the currently-selected year. Sortable by name, type, or total. Right-click a cell to copy.

#### Charts

The bottom section. Three multi-select boxes drive everything:

- **Years** — which years to include in the data.
- **Months** — which months (default: current calendar month).
- **Projects** — which projects to compare. *Multiple projects render side-by-side as a row of charts.*

Each list has **All** / **None** buttons underneath. **Clear filters** (to the right of the three lists) deselects every year, month, and project in one click and refreshes the charts with nothing selected (no fallback to the current project or year).

Four chart rows, in order:

1. **Month Summary** (Pie / Donut / Bar) — Expense vs Income for the selected months.
2. **Month Breakdown** (Pie / Donut / Bar / Horizontal bar) — per-category totals for the selected months.
3. **Year Summary** (Bar / Line / Stacked bar / Pie) — month-by-month totals across the selected years.
4. **Year Breakdown** (Pie / Donut / Bar / Horizontal bar) — per-category yearly totals.

Each row has its own **Chart** dropdown so you can pick the chart type independently. Colours are consistent across every chart:

- **Income** — flat green.
- **Investment** — flat dark blue. Charts show investment **magnitude** (so a `-$50` loss and a `$50` gain look the same size on a pie / bar). The signed up-or-down truth lives in the **Investments** header total above the grid.
- **Expense** (single bar / line / summary slice) — flat red.
- **Expense** (per-category breakdown pie / donut) — cycles through a distinct palette so each expense category has its own hue.
- **Discrepancy** — never drawn. Discrepancy cells are excluded from every chart so they can't skew a pie or bar.

Charts redraw automatically as you change selectors, edit values, or resize the window.

### 3. Year & month navigation

The Year and Month dropdowns at the top of the Project view determine what every dashboard, table, and chart shows.

- Switching **Year** reloads the section because categories are year-scoped.
- Switching **Month** updates the Month Summary totals + Month Breakdown.

### 4. Selecting / clearing highlights

- **Click** a cell → blue single-cell highlight.
- **Double-click** → opens the editor.
- **Esc** or click empty background → clears all selections / highlights.

### 5. Scrolling

The project view scrolls like a normal webpage:

- **Wheel inside a table** (Month Breakdown, Year Breakdown, etc.) scrolls *just that table* if it has more rows than fit; otherwise the wheel scrolls the page underneath it.
- **Shift + Wheel** inside a wide table scrolls horizontally.

---

## Where is my data?

| | |
|---|---|
| Database file | `%LOCALAPPDATA%\FinanceApp\finance.db` |
| Backups | Same folder, named `finance.backup-YYYYMMDD-HHMMSS.db` |

Type the path into File Explorer's address bar to open the folder.

The app uses one SQLite file per machine — there's no cloud sync, no account, and no network traffic. To move your data to another PC, copy `finance.db` to the same path on the other machine.

To restore from a backup, close the app, delete `finance.db`, then rename your chosen `finance.backup-...db` to `finance.db`.

---

## Building the executable

If you've changed the source and want a new `.exe`:

1. Install PyInstaller once: `pip install pyinstaller`
2. Build: `pyinstaller FinanceApp.spec`
3. Distribute `dist\FinanceApp.exe`

The `.spec` file already encodes all options (`--onefile`, `--noconsole`, matplotlib bundled, the native `tkinterdnd2`/`tkdnd` extension bundled for drag-and-drop, app icon, $image resources).

> ⚠️ **Always rebuild via the spec file.** Running the full one-line command (e.g. `pyinstaller --noconsole --onefile --name FinanceApp ... main.py`) regenerates `FinanceApp.spec` from scratch and wipes both the icon (`icon=':$image.ico'`) and the bundled `$image` resources. Use the full command **once** to seed the spec on a fresh machine, then always rebuild with `pyinstaller FinanceApp.spec`.

### Replacing the app icon

The logo lives at the project root:

| File | Purpose |
|---|---|
| `$image.png` | 1024×1024 source artwork (square; transparent background recommended) |
| `$image.ico` | Multi-size Windows icon built from the PNG (16/24/32/48/64/128/256 px) |

To swap in your own logo:

1. Replace `$image.png` with your square PNG (1024×1024 ideal, RGBA preferred).
2. Regenerate the `.ico` from it with Pillow:

   ```python
   from PIL import Image
   src = Image.open('$image.png').convert('RGBA')
   sizes = [(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
   src.save('$image.ico', format='ICO', sizes=sizes)
   ```

3. Run `pyinstaller FinanceApp.spec` to rebuild the exe with the new icon embedded in its Win32 resources and bundled inside the one-file archive.

---

## Tech notes

- Python 3.9+ • Tkinter (built into Python on Windows) • SQLite (built into Python) • matplotlib for charts • tkinterdnd2 (optional) for file drag-and-drop.
- Money is stored as integer cents in SQLite; formatted on display only.
- All data access goes through `finance_app/repository.py`.
- The UI is split into `finance_app/ui/main_window.py` (views & app glue), `widgets.py` (reusable Tk widgets), and `charts.py` (matplotlib panels).
- Wheel scrolling is tuned to feel browser-like: pixel-precise canvas (`yscrollincrement=1`), capped per-event delta, proportional steps inside Treeviews, and a paint-flush gate that only fires during fast wheel bursts (see `_WheelScrollPaintGate` in `widgets.py`). Shift+wheel inside a wide table scrolls horizontally at roughly 3× the per-tick column step of vertical wheel.
- **Cell navigation & undo/redo:** the Month-grid editor is a custom `TreeviewCellEditor` in `widgets.py` that intercepts `KeyPress-Tab` / `KeyPress-Shift-Tab` / `KeyPress-Return` to override Tk's default focus traversal, calls `tree.see(...)` + retries `tree.bbox(...)` so an off-screen target cell still gets a valid Entry overlay, and reports back via an `on_navigate(direction)` callback. Undo/redo is two stacks of `(item_id, col_id, old_cents, new_cents)` snapshots maintained by `main_window.py` and bound globally to `Ctrl+Z` / `Ctrl+Y`.
- **CSV import** lives in `finance_app/csv_import.py`: a `ColumnMapping` dataclass plus `read_raw_rows` → `detect_mapping` (heuristic auto-detect of date/description/amount columns, header, date format, signed vs debit/credit) → `parse_with_mapping`, with `file_signature` fingerprinting a layout so the same bank is recognised next time. Merchant keys are the first 1–3 description words (transfers keep the full description); `group_by_merchant` sums cents per `(year, month)`.
- **Import persistence** adds five tables in `repository.py`: `csv_format_profiles` (signature → saved `ColumnMapping`), `merchant_rules` (remembered merchant → category mappings, `project`- or `global`-scoped), `merchant_drafts` (in-progress Assign-window selections), `import_batches` (one row per import run, so an import can be undone as a unit), and `imported_transactions` (a per-project ledger of every imported transaction, keyed by a date+amount+description fingerprint, used to skip duplicates on re-import and to reverse a batch). Categories carry `is_temporary` / `merchant_key` flags; all aggregate queries filter out `is_temporary` rows so unassigned merchants never hit a total or chart.
---

## Troubleshooting

| Problem | Fix |
|---|---|
| App won't start, no error visible | Run `python main.py` from a terminal to see the traceback. |
| Charts look clipped in a small window | Resize the window — charts redraw and shrink labels dynamically. |
| Lost data after editing the wrong year | Categories are year-scoped, so 2025 and 2026 are independent. Switch the Year dropdown back. |
| A previously negative value (e.g. `-$50` in an expense) now shows as `$50` | Expected. The first launch of an updated build runs a one-time migration that normalises signed values in **Expense / Income** rows to their magnitude. The kind alone now decides if it adds or subtracts. **Investment and Discrepancy rows are exempt** so signed gains/losses and reconciliation nudges are preserved. See *How values & signs work*. |
| Wheel inside a small table doesn't scroll the *page* | Expected when the table has its own scrollable rows — the wheel scrolls the table first. Move the cursor onto an area outside the table (e.g. between sections) and the page will scroll. |
| Fast wheel scrolling feels rougher when **Charts** is expanded | Charts are heavier to repaint than the rest of the UI. Collapse the Charts section while you're scrolling around. |
| `Ctrl+Z` does nothing after switching project / year | Expected. The undo stack is per-(project, year, category set), so any of those changes clears it. Make a single edit, then `Ctrl+Z` will pick up from there. |
| `Tab` while editing moves focus to a button instead of the next cell | Make sure the cursor is inside the Entry overlay when you press Tab (the overlay appears on top of the cell once you start editing). The grid binds `KeyPress-Tab` directly on the editor to override Tk's default focus traversal — clicking back into the Entry restores it. |
| No **drop a .csv file here** zone appears | `tkinterdnd2` isn't installed. Run `pip install -r requirements.txt` (source) — or just use the **Import statement…** button, which always works. |
| Imported values don't show in totals / charts | Expected until you assign them. Merchant columns from an import are *temporary* (light-red header) and are excluded from every total, breakdown, and chart until you map them via **Assign now**. |
| A merchant keeps coming back as temporary on each import | Its rule was saved **Per-Project** and you're importing into a different project, or it was never fully assigned. Re-assign it and pick **Global** to share the mapping across all projects. |
| The **Set up bank CSV format** dialog opens every time for the same bank | The file's structure is changing between exports (different column count / header), so it doesn't match the saved profile's signature. Confirm the mapping once more and it'll be remembered for that new layout. |
| An import column vanished after assigning it | You set its **Type** to **Transfer** — transfers are money moved between your own accounts, so the column is removed from the grid by design. |
| I imported the same file twice but values didn't double | Expected — the app remembers imported transactions and skips duplicates. The confirmation dialog shows how many were new vs already-imported. |
| **Undo last import** didn't fully reverse an import | Merchants you'd already assigned to a real category since importing can't be reversed (their amounts moved into the assigned column). Undo right after importing, before assigning, for a clean rollback. |
| Want to start fresh | Close the app, delete `%LOCALAPPDATA%\FinanceApp\finance.db`, relaunch. (Make a backup first if you might want it back.) |
