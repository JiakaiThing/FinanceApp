"""Charts section for the finance app.

Builds a collapsible "Charts" container with four chart panels:

- Month Summary: expense / income / net for the selected month(s) and year(s).
- Month Breakdown: per-category amounts for the selected month(s) and year(s).
- Year Summary: per-month totals across the selected year(s).
- Year Breakdown: per-category yearly totals across the selected year(s).

Each panel has its own chart-type dropdown (pie, bar, line, ...). The user
also has shared "Years" and "Months" multi-select boxes that drive every
panel.

This module is self-contained so ``main_window.py`` only has to construct
``ChartsSection`` and call ``refresh()`` whenever underlying data changes.
"""
from __future__ import annotations

import math
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from typing import Callable, Iterable, Optional, Sequence

import matplotlib

matplotlib.use("TkAgg")  # ensure Tk backend is loaded before pyplot is imported elsewhere
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.ticker import MaxNLocator

from ..models import CategoryKind
from ..repository import FinanceRepository


EN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ----- chart-type catalogues -----
# Each entry: a chart "kind" string mapped to a friendly label shown in the
# dropdown. Each panel exposes a subset that makes sense for its data shape.
PIE = ("pie", "Pie")
DONUT = ("donut", "Donut")
BAR = ("bar", "Bar")
HBAR = ("hbar", "Horizontal bar")
LINE = ("line", "Line")
STACKED = ("stacked", "Stacked bar")

CHARTS_MONTH_SUMMARY: list[tuple[str, str]] = [PIE, DONUT, BAR]
CHARTS_MONTH_BREAKDOWN: list[tuple[str, str]] = [PIE, DONUT, BAR, HBAR]
CHARTS_YEAR_SUMMARY: list[tuple[str, str]] = [BAR, LINE, STACKED, PIE]
CHARTS_YEAR_BREAKDOWN: list[tuple[str, str]] = [PIE, DONUT, BAR, HBAR]


# ----- helpers ------------------------------------------------------------
def _money_label(cents: int) -> str:
    return f"${cents / 100.0:,.2f}"


def _autopct_with_sign(
    values_cents: Sequence[int], kinds: Sequence[int]
) -> Callable[[float], str]:
    """Format pie-wedge text as ``"NN%  [-]$AMOUNT"`` on a single line.

    Only Expense categories get a ``-`` prefix before the ``$`` — Income
    is always positive cash-in, and Investment carries its own signed
    semantics (positive = gain, negative = loss) which the dedicated
    Investments header total surfaces. Pie slices use magnitude only
    so a negative investment is rendered like a positive one of the
    same size; the slice colour (dark blue) identifies the bucket.
    Tiny slices (< 4%) return an empty string so labels don't pile on
    top of each other.

    Uses a closure counter because matplotlib's ``autopct`` callback only
    receives the percentage; it is, however, invoked once per wedge in the
    same order as ``values_cents``, so an index works reliably.
    """
    total = sum(values_cents)
    counter = {"i": -1}
    expense_kind = int(CategoryKind.EXPENSE)

    def fmt(pct: float) -> str:
        counter["i"] += 1
        i = counter["i"]
        if pct < 4:
            return ""
        amount = int(round(total * pct / 100.0))
        sign = "-" if i < len(kinds) and kinds[i] == expense_kind else ""
        return f"{pct:.0f}%  {sign}${amount / 100.0:,.2f}"

    return fmt


# Single uniform font size used for every pie/donut chart's autopct
# labels and legend, so the four chart rows all read the same. Bumping
# this one constant adjusts every pie chart in lockstep.
_PIE_FONT_SIZE = 8

# Smaller legend font used by breakdown charts in *compact* mode —
# i.e. when the panel is too narrow to show outside autopct labels
# (multiple projects selected, narrow window). Shrinking the legend
# frees up margin width so the pie itself can grow.
_PIE_FONT_SIZE_COMPACT = 6

# Single uniform pie geometry: every pie/donut chart in the section
# uses the same radius and label distance, regardless of which row it
# belongs to (summary vs breakdown) or how many projects are selected.
_PIE_RADIUS = 0.85
_PIE_PCT_DISTANCE = 1.15

# Larger pie radius used in *compact* mode (no outside labels). With
# ``_PIE_VIEW=1.5`` this fills the axes much more aggressively, making
# the pie visibly bigger when the panel is narrow.
_PIE_RADIUS_COMPACT = 1.35

# Pie axes' data view limits. Square (``±_PIE_VIEW``) so ``set_aspect
# equal`` keeps the pie circular. Slightly larger than ``radius +
# pctdistance`` so rotated outside-labels have whitespace to extend
# into without getting clipped.
_PIE_VIEW = 1.5


def _summary_pie_params() -> tuple[int, float, float]:
    """``(fontsize, pctdistance, radius)`` for summary pie/donut charts.

    Returned as a tuple so the call sites stay symmetric with the
    breakdown helper. The values are deliberately identical to the
    breakdown helper — every pie chart in the Charts section uses one
    uniform geometry regardless of which row it belongs to or how many
    projects are selected.
    """
    return _PIE_FONT_SIZE, _PIE_PCT_DISTANCE, _PIE_RADIUS


def _breakdown_pie_params() -> tuple[int, float, float]:
    """``(fontsize, pctdistance, radius)`` for breakdown pie/donut charts.

    Currently identical to :func:`_summary_pie_params`; kept as a
    separate helper so a future requirement (e.g. a different breakdown
    radius) only needs to touch one place.
    """
    return _PIE_FONT_SIZE, _PIE_PCT_DISTANCE, _PIE_RADIUS


def _legend_width_frac(
    fig: Figure,
    labels: Sequence[str],
    font_size: int = _PIE_FONT_SIZE,
) -> float:
    """Estimate the fractional figure width occupied by the pie legend.

    Used by :func:`_setup_pie_axes` to size the symmetric left/right
    margins around the pie so the legend (anchored at the figure's
    left edge) never bleeds onto the pie wedges. Re-evaluated on every
    draw so resizing the window automatically reshapes the layout.

    Components of the estimate:

    * ``max_chars × char_w_in`` — text width of the longest label.
    * ``handle_w_in``           — colour swatch + gap before the text
      (matplotlib defaults: ``handlelength=2`` font units +
      ``handletextpad=0.8`` font units, plus a small fudge).
    * trailing ``0.15`` inches  — a hard gap between the legend and
      the pie's bounding box so they don't visually touch.

    ``font_size`` lets compact-mode breakdown charts pass the smaller
    legend font (``_PIE_FONT_SIZE_COMPACT``) so the margin shrinks
    proportionally and the pie can grow.
    """
    fig_w_in = fig.get_size_inches()[0]
    max_chars = max((len(label) for label in labels), default=10)
    char_w_in = (font_size / 72.0) * 0.6
    handle_w_in = (font_size / 72.0) * 3.0
    legend_w_in = max_chars * char_w_in + handle_w_in + 0.15
    return max(0.0, legend_w_in / max(fig_w_in, 0.01))


def _pie_panel_too_narrow_for_outside_text(
    fig: Figure, labels: Sequence[str]
) -> bool:
    """Decide whether a breakdown panel is too narrow to show its
    angled outside autopct (``%, $value``) labels alongside the
    legend.

    Returns ``True`` when the legend's estimated width takes 40% or
    more of the figure width. At that point :func:`_setup_pie_axes`
    has already capped the legend margin, the pie is at its minimum
    size, and there's no breathing room left for the outside label
    tails — drawing them anyway looks cramped (they collide with the
    legend or run off the right edge). When this returns ``True``
    the breakdown chart drops its autopct text and shows just the
    pie + legend, which the user can still cross-reference by colour.

    Triggered in practice when more than one project is selected so
    each panel only gets a fraction of the chart row's width.
    """
    return _legend_width_frac(fig, labels) >= 0.40


def _setup_pie_axes(
    fig: Figure,
    labels: Sequence[str],
    font_size: int = _PIE_FONT_SIZE,
):
    """Position a pie/donut Axes so it sits horizontally centred in the
    figure with whitespace on both sides for the left-anchored legend
    (see :func:`_draw_pie_legend`) and the outside labels.

    The left and right margins are computed from the legend's
    estimated width (see :func:`_legend_width_frac`) so the layout
    adapts to window size: a wide window gives the pie a big symmetric
    box; a narrow window shrinks the pie symmetrically so the legend
    on the left and the wedges in the middle never overlap.

    The margin is capped at ``0.40`` (40% of figure width) to keep at
    least a 20%-wide window for the pie itself on very narrow figures.

    ``font_size`` is forwarded to :func:`_legend_width_frac` so
    compact-mode panels (smaller legend font) get a proportionally
    smaller margin.

    Disables the figure's layout engine because matplotlib's
    constrained layout would otherwise fight the manual placement.
    """
    try:
        fig.set_layout_engine("none")
    except Exception:
        pass
    margin = min(0.40, _legend_width_frac(fig, labels, font_size))
    width = max(0.10, 1.0 - 2 * margin)
    return fig.add_axes([margin, 0.05, width, 0.90])


def _draw_pie_legend(
    ax,
    fig: Figure,
    wedges,
    labels,
    font_size: int = _PIE_FONT_SIZE,
) -> None:
    """Anchor the pie legend at the figure's top-left, growing
    downward, and only wrap to a second column once the column is
    actually full.

    Layout rules:

    * Top edge of the legend sits at fig y = 7/8 (i.e. 1/8 of the
      figure height of padding above it).
    * Single column (``ncol=1``) is preferred so the user can read the
      categories top-to-bottom in one stack.
    * The wrap-to-2-columns threshold is computed from the actual
      figure height: the legend may extend down through the bottom
      1/8 padding zone before we're forced to split into two columns.
      This is what stops a 16-entry breakdown from snapping into two
      half-height columns when there's plenty of vertical space left
      to fill.

    ``font_size`` lets compact-mode breakdown charts pass the smaller
    legend font (``_PIE_FONT_SIZE_COMPACT``) so legend entries shrink
    along with the margin computed by :func:`_setup_pie_axes`.
    """
    fig_h_in = fig.get_size_inches()[1]
    # ``labelspacing`` is in font units; ``0.6`` is slightly looser
    # than matplotlib's default of ``0.5`` so categories are easier to
    # tell apart at a glance without the legend feeling sparse.
    label_spacing = 0.6
    entry_h_in = (font_size / 72.0) * (1.0 + label_spacing)
    # Allow the legend to extend from the top anchor down through
    # most of the figure (only a sliver of bottom padding) before
    # being forced to wrap. The bottom 1/8 padding zone still exists
    # whenever the legend doesn't need all of it.
    avail_h_in = fig_h_in * 0.90
    max_rows = max(4, int(avail_h_in / entry_h_in))
    ncol = 1 if len(labels) <= max_rows else 2

    ax.legend(
        wedges, labels,
        loc="upper left",
        bbox_to_anchor=(0.01, 0.875),
        bbox_transform=fig.transFigure,
        fontsize=font_size,
        frameon=False,
        ncol=ncol,
        labelspacing=label_spacing,
    )


def _restore_constrained_layout(fig: Figure) -> None:
    """Re-enable constrained layout for non-pie chart kinds in the same
    panel, after a previous draw may have disabled it."""
    try:
        fig.set_layout_engine("constrained")
    except Exception:
        pass


def _align_autopct_outside(
    autotexts,
    wedges,
    values: Optional[Sequence[int]] = None,
    *,
    horizontal_threshold: float = 0.25,
) -> None:
    """Place each autopct text along the radial direction of its wedge.

    Small wedges have their text rotated to match the wedge centroid angle
    so it follows the slice it belongs to. Slices that take up at least
    ``horizontal_threshold`` of the pie (default 25%) are kept horizontal
    instead, since their angled label would otherwise be hard to read
    when the wedge is near the bottom of the chart.

    Text is always anchored to grow OUTWARD from the pie. Left-half wedges
    have their rotation flipped 180° so text never reads upside-down.

    matplotlib's default for autopct is ``ha="center"`` and ``rotation=0``.
    Both are overridden here.
    """
    total = sum(values) if values else 0
    if values is None:
        values = [0] * len(wedges)

    for at, w, v in zip(autotexts, wedges, values):
        ang_deg = (w.theta1 + w.theta2) / 2.0
        ang_rad = math.radians(ang_deg)
        share = (v / total) if total > 0 else 0.0
        is_big = share >= horizontal_threshold

        cos_a = math.cos(ang_rad)
        # Wedges that point roughly straight up or down get ``ha="center"``
        # so the label sits visually centred above/below the slice
        # instead of being anchored to one side.
        if abs(cos_a) < 0.08:
            ha = "center"
        elif cos_a > 0:
            ha = "left"
        else:
            ha = "right"

        if is_big:
            rotation = 0.0
        elif cos_a >= 0:
            rotation = ang_deg
        else:
            rotation = ang_deg - 180.0

        # Keep rotation in a tidy range for matplotlib's text rendering.
        while rotation > 180.0:
            rotation -= 360.0
        while rotation < -180.0:
            rotation += 360.0

        at.set_rotation(rotation)
        at.set_horizontalalignment(ha)
        at.set_verticalalignment("center")
        at.set_rotation_mode("anchor")


# Income (positive flow) keeps a single recognisable green.
_INCOME_COLOR = "#1a7f37"
# Expenses (the dominant outflow category) keep a single recognisable
# red — used by summary bar/line/stacked charts and as the bar colour
# for any Expense-kind category.
_EXPENSE_COLOR = "#d1242f"
# Investments are a third bucket that subtracts from End of Month like
# expenses, but tracked separately. Dark blue stands clearly apart from
# Income's green and Expense's red.
_INVESTMENT_COLOR = "#1f3a8a"

# Expenses (negative flow) cycle through this distinct palette in
# breakdown charts so each individual expense category gets its own
# slice colour. Hues are spread around the colour wheel so adjacent
# (and non-adjacent) slices in a single chart are easy to tell apart in
# the legend and on the pie. Greens and dark blues are intentionally
# avoided because Income owns green and Investments own dark blue.
_EXPENSE_PALETTE: list[str] = [
    "#e6194B",  # red
    "#f58231",  # orange
    "#911eb4",  # purple
    "#4363d8",  # blue
    "#9A6324",  # brown
    "#42d4f4",  # cyan
    "#f032e6",  # magenta
    "#000075",  # navy
    "#a9a9a9",  # grey
    "#800000",  # maroon
    "#469990",  # teal
    "#fabed4",  # light pink
]


def _apply_value_axis_grid(ax, *, axis: str = "y", nbins: int = 10) -> None:
    """Add finer ticks + a faint grid on the value axis of bar/line charts.

    ``axis`` is "y" for vertical bar charts (and lines), "x" for horizontal
    bar charts. ``nbins`` controls the maximum number of major tick
    intervals — matplotlib still picks "nice" boundaries.
    """
    locator = MaxNLocator(nbins=nbins)
    if axis == "y":
        ax.yaxis.set_major_locator(locator)
    else:
        ax.xaxis.set_major_locator(locator)
    ax.set_axisbelow(True)
    ax.grid(axis=axis, linewidth=0.5, color="#dddddd")


def _category_colors(kinds: Sequence[int]) -> list[str]:
    """Return one colour per category, by kind.

    * Income (``kind=1``)     → flat green.
    * Investment (``kind=2``) → flat dark blue.
    * Expense (``kind=0``)    → cycles through ``_EXPENSE_PALETTE`` so
      each expense slice in the same chart gets its own hue.
    """
    income_kind = int(CategoryKind.INCOME)
    investment_kind = int(CategoryKind.INVESTMENT)
    out: list[str] = []
    exp_idx = 0
    for k in kinds:
        if k == income_kind:
            out.append(_INCOME_COLOR)
        elif k == investment_kind:
            out.append(_INVESTMENT_COLOR)
        else:
            out.append(_EXPENSE_PALETTE[exp_idx % len(_EXPENSE_PALETTE)])
            exp_idx += 1
    return out


def _bar_color_for_kind(k: int) -> str:
    """Solid bar/line colour for one category kind."""
    if k == int(CategoryKind.INCOME):
        return _INCOME_COLOR
    if k == int(CategoryKind.INVESTMENT):
        return _INVESTMENT_COLOR
    return _EXPENSE_COLOR


# ----- multi-select widget -----------------------------------------------
class MultiSelectListbox(ttk.Frame):
    """A small listbox + select-all/none buttons for picking multiple items.

    ``values`` is a sequence of ``(key, label)`` pairs. ``get_selected()``
    returns the list of keys currently selected. ``set_values()`` replaces
    the available items and tries to preserve the current selection.
    """

    def __init__(
        self,
        parent: tk.Widget,
        title: str,
        values: Sequence[tuple[object, str]] = (),
        *,
        height: int = 8,
        on_change: Optional[Callable[[], None]] = None,
        default_keys: Optional[Sequence[object]] = None,
        default_all: bool = True,
    ):
        super().__init__(parent)
        self._on_change = on_change
        self._keys: list[object] = []

        ttk.Label(self, text=title, font=("Segoe UI", 9, "bold")).pack(anchor="w")

        list_wrap = ttk.Frame(self)
        list_wrap.pack(fill=tk.BOTH, expand=True)
        self._listbox = tk.Listbox(
            list_wrap,
            selectmode=tk.EXTENDED,
            exportselection=False,
            height=height,
            activestyle="none",
        )
        self._listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self._listbox.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._listbox.configure(yscrollcommand=sb.set)

        btn_row = ttk.Frame(self)
        btn_row.pack(fill=tk.X, pady=(2, 0))
        ttk.Button(btn_row, text="All", width=5, command=self._select_all).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="None", width=5, command=self._select_none).pack(side=tk.LEFT, padx=(4, 0))

        self._listbox.bind("<<ListboxSelect>>", lambda _e: self._fire())

        self.set_values(values, default_keys=default_keys, default_all=default_all)

    def _fire(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass

    def _select_all(self) -> None:
        self.select_all()

    def _select_none(self) -> None:
        self.select_none()

    # Public counterparts so callers (e.g. the Charts "Clear filters"
    # button) can clear the listbox without firing ``on_change`` —
    # useful when several listboxes are reset together and the caller
    # only wants a single refresh at the end.
    def select_all(self, *, silent: bool = False) -> None:
        self._listbox.selection_set(0, tk.END)
        if not silent:
            self._fire()

    def select_none(self, *, silent: bool = False) -> None:
        self._listbox.selection_clear(0, tk.END)
        if not silent:
            self._fire()

    def set_values(
        self,
        values: Sequence[tuple[object, str]],
        *,
        default_all: bool = True,
        default_keys: Optional[Sequence[object]] = None,
    ) -> None:
        """Replace the available items.

        Selection behaviour:
        - If ``default_keys`` is provided, exactly those keys are selected.
        - Else, the previous selection (intersected with the new keys) is
          preserved.
        - Else, if ``default_all`` is True, every item is selected.
        """
        prev = set(self.get_selected())
        self._keys = [v[0] for v in values]
        self._listbox.delete(0, tk.END)
        for _, label in values:
            self._listbox.insert(tk.END, label)
        if not values:
            self._fire()
            return

        if default_keys is not None:
            wanted = set(default_keys)
            for i, k in enumerate(self._keys):
                if k in wanted:
                    self._listbox.selection_set(i)
        else:
            keep = [i for i, k in enumerate(self._keys) if k in prev]
            if keep:
                for i in keep:
                    self._listbox.selection_set(i)
            elif default_all:
                self._listbox.selection_set(0, tk.END)
        self._fire()

    def get_selected(self) -> list:
        idxs = list(self._listbox.curselection())
        return [self._keys[i] for i in idxs]


# ----- a single chart panel ----------------------------------------------
_CHART_PANEL_STYLE = "ChartPanel.TLabelframe"
_chart_panel_style_ready = False


def _ensure_chart_panel_style() -> None:
    """Register a bold Labelframe label style once (Tk Style is global)."""
    global _chart_panel_style_ready
    if _chart_panel_style_ready:
        return
    try:
        style = ttk.Style()
        style.configure(f"{_CHART_PANEL_STYLE}.Label", font=("Segoe UI", 10, "bold"))
        _chart_panel_style_ready = True
    except Exception:
        pass


class ChartPanel(ttk.Labelframe):
    """A titled frame that holds a chart-type dropdown + a matplotlib canvas."""

    def __init__(
        self,
        parent: tk.Widget,
        title: str,
        chart_types: Sequence[tuple[str, str]],
        *,
        on_change: Optional[Callable[[], None]] = None,
        show_combo: bool = True,
    ):
        _ensure_chart_panel_style()
        super().__init__(parent, text=title, style=_CHART_PANEL_STYLE)
        self._on_change = on_change
        self._chart_types = list(chart_types)
        self._kind_to_label = dict(self._chart_types)
        self._label_to_kind = {label: kind for kind, label in self._chart_types}

        labels = [label for _, label in self._chart_types]
        self._chart_var = tk.StringVar(value=labels[0] if labels else "")

        if show_combo:
            top = ttk.Frame(self)
            top.pack(fill=tk.X, padx=8, pady=(6, 0))
            ttk.Label(top, text="Chart").pack(side=tk.LEFT)
            self._chart_combo: Optional[ttk.Combobox] = ttk.Combobox(
                top,
                textvariable=self._chart_var,
                values=labels,
                state="readonly",
                width=18,
                justify="center",
            )
            self._chart_combo.pack(side=tk.LEFT, padx=(8, 0))
            self._chart_combo.bind("<<ComboboxSelected>>", lambda _e: self._fire(), add=True)
        else:
            self._chart_combo = None

        # Bold subtitle that lives BELOW the panel's title (project name)
        # and shows the year/period for the chart. Lets us keep the period
        # off the matplotlib axes so it doesn't crowd the chart.
        self._subtitle_var = tk.StringVar(value="")
        self._subtitle_lbl = ttk.Label(
            self,
            textvariable=self._subtitle_var,
            font=("Segoe UI", 9, "bold"),
            anchor="center",
        )
        self._subtitle_lbl.pack(fill=tk.X, padx=8, pady=(4, 0))

        # ``constrained_layout`` re-runs on every draw, so when the panel
        # shrinks (e.g. small window or many projects side-by-side) labels
        # and the legend stay inside the canvas instead of being clipped.
        # When the window is large the result is visually identical to
        # the previous one-shot ``tight_layout``.
        self._fig = Figure(figsize=(4.6, 3.4), dpi=100, constrained_layout=True)
        self._fig.patch.set_facecolor("#f7f7f7")
        self._canvas = FigureCanvasTkAgg(self._fig, master=self)
        self._canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=8, pady=(2, 8))

        # Resize listener: fires when the matplotlib canvas widget changes
        # size so the section can re-render with size-aware parameters
        # (e.g. shrink summary-pie fonts/radius when the window narrows).
        self._resize_listener: Optional[Callable[[], None]] = None
        self._last_canvas_w = 0
        self._canvas.get_tk_widget().bind(
            "<Configure>", self._on_canvas_configure, add=True
        )

    def set_resize_listener(self, fn: Optional[Callable[[], None]]) -> None:
        self._resize_listener = fn

    def _on_canvas_configure(self, event: tk.Event) -> None:
        # Filter sub-pixel / single-pixel jitter so the listener doesn't
        # run for changes that wouldn't visibly affect the chart. The
        # threshold is intentionally larger than 1px so a continuous
        # window-drag fires many fewer (re-)schedules — keeping the
        # redraw work to one shot per noticeable size step.
        if abs(event.width - self._last_canvas_w) < 12:
            return
        self._last_canvas_w = event.width
        fn = self._resize_listener
        if fn is None:
            return
        try:
            fn()
        except Exception:
            pass

    @property
    def kind(self) -> str:
        return self._label_to_kind.get(self._chart_var.get(), self._chart_types[0][0] if self._chart_types else "")

    def set_kind(self, kind: str) -> None:
        label = self._kind_to_label.get(kind)
        if label:
            self._chart_var.set(label)

    def _fire(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass

    def figure(self) -> Figure:
        return self._fig

    def set_subtitle(self, text: str) -> None:
        """Set the bold subtitle line shown below the panel's title."""
        self._subtitle_var.set(text or "")

    def clear(self) -> None:
        self._fig.clear()
        self._canvas.draw_idle()
        self._subtitle_var.set("")

    def draw(self) -> None:
        self._canvas.draw_idle()


# ----- a single horizontal "row" (one chart category, N panels) ----------
class _ChartRow:
    """A row inside ChartsSection: a header (title + chart-type combobox)
    above a horizontal strip of one ChartPanel per selected project.

    The chart-type combobox is shared across all panels in the row so the
    same chart type is used when comparing projects side-by-side.
    """

    def __init__(
        self,
        parent: tk.Widget,
        section: "ChartsSection",
        row_id: str,
        title: str,
        chart_types: Sequence[tuple[str, str]],
        default_kind: str,
    ):
        self._section = section
        self._row_id = row_id
        self._chart_types = list(chart_types)
        self._kind_to_label = dict(self._chart_types)
        self._label_to_kind = {label: kind for kind, label in self._chart_types}

        self.frame = ttk.Frame(parent)

        header = ttk.Frame(self.frame)
        header.pack(fill=tk.X)
        ttk.Label(header, text=title, font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        ttk.Label(header, text="Chart").pack(side=tk.LEFT, padx=(16, 4))

        labels = [label for _, label in self._chart_types]
        initial_label = self._kind_to_label.get(default_kind) or (labels[0] if labels else "")
        self._kind_var = tk.StringVar(value=initial_label)
        self._combo = ttk.Combobox(
            header,
            textvariable=self._kind_var,
            values=labels,
            state="readonly",
            width=18,
            justify="center",
        )
        self._combo.pack(side=tk.LEFT)
        self._combo.bind("<<ComboboxSelected>>", lambda _e: self._on_kind_change(), add=True)

        self.panels_container = ttk.Frame(self.frame)
        self.panels_container.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.panels_container.rowconfigure(0, weight=1)

        self.panels: list[ChartPanel] = []

    def _on_kind_change(self) -> None:
        kind = self._label_to_kind.get(self._kind_var.get())
        if kind:
            self._section.set_row_kind(self._row_id, kind)

    @property
    def kind(self) -> str:
        return self._label_to_kind.get(self._kind_var.get(), "")

    def rebuild_panels(self, project_pairs: Sequence[tuple[int, str]]) -> None:
        """Tear down existing panels and create exactly one per project."""
        for p in self.panels:
            try:
                p.destroy()
            except Exception:
                pass
        self.panels = []

        # Reset prior column weights from any larger previous layout.
        for i in range(64):
            try:
                self.panels_container.columnconfigure(i, weight=0, uniform="")
            except Exception:
                break

        if not project_pairs:
            return

        for col, (_pid, name) in enumerate(project_pairs):
            self.panels_container.columnconfigure(col, weight=1, uniform="proj_cols")
            panel = ChartPanel(
                self.panels_container,
                title=name,
                chart_types=self._chart_types,
                show_combo=False,
            )
            panel.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0))
            # Redraw the whole section (debounced) when this panel's
            # canvas changes size so size-dependent layout (summary pie
            # font/radius) re-applies with the new figure dimensions.
            panel.set_resize_listener(self._section.schedule_refresh)
            self.panels.append(panel)


# ----- the Charts section glue --------------------------------------------
class ChartsSection:
    """Builds & maintains the bottom Charts container."""

    # row_id, title, chart-type catalogue, default chart kind
    ROW_DEFS: list[tuple[str, str, list[tuple[str, str]], str]] = [
        ("month_summary",   "Month Summary",   CHARTS_MONTH_SUMMARY,   "pie"),
        ("month_breakdown", "Month Breakdown", CHARTS_MONTH_BREAKDOWN, "pie"),
        ("year_summary",    "Year Summary",    CHARTS_YEAR_SUMMARY,    "pie"),
        ("year_breakdown",  "Year Breakdown",  CHARTS_YEAR_BREAKDOWN,  "pie"),
    ]

    def __init__(
        self,
        parent: tk.Widget,
        repo: FinanceRepository,
        get_state: Callable[[], object],
    ):
        """``get_state`` returns the live ``UiState`` so we always read the
        currently-selected project / year / month.
        """
        self._parent = parent
        self._repo = repo
        self._get_state = get_state

        self._frame = ttk.Frame(parent)
        self._frame.pack(fill=tk.BOTH, expand=True)

        # Top row: shared selectors
        selectors = ttk.Frame(self._frame)
        selectors.pack(fill=tk.X, pady=(0, 8))

        self._years_select = MultiSelectListbox(
            selectors, "Years", values=(), height=6, on_change=self.refresh_charts,
            default_all=False,
        )
        self._years_select.pack(side=tk.LEFT, padx=(0, 12))

        month_values = [(i + 1, m) for i, m in enumerate(EN_MONTHS)]
        current_month = datetime.now().month
        self._months_select = MultiSelectListbox(
            selectors, "Months", values=month_values, height=6,
            on_change=self.refresh_charts,
            # Default to today's calendar month only.
            default_keys=[current_month], default_all=False,
        )
        self._months_select.pack(side=tk.LEFT, padx=(0, 12))

        self._projects_select = MultiSelectListbox(
            selectors, "Projects", values=(), height=6,
            on_change=self.refresh_charts,
            default_all=False,
        )
        self._projects_select.pack(side=tk.LEFT, padx=(0, 12))

        # ``Clear filters`` button — bottom-aligned so it lines up with
        # each listbox's All / None buttons. Clears all three selectors
        # in silent mode so the redraw only happens once at the end.
        clear_wrap = ttk.Frame(selectors)
        clear_wrap.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 12))
        ttk.Button(
            clear_wrap, text="Clear filters", width=14,
            command=self._clear_filters,
        ).pack(side=tk.BOTTOM, pady=(0, 2))

        # Vertical stack of horizontal rows. Each row becomes:
        # [Month Summary] across all selected projects, then
        # [Year Summary]  across all selected projects, etc.
        rows_root = ttk.Frame(self._frame)
        rows_root.pack(fill=tk.BOTH, expand=True)

        self._row_kinds: dict[str, str] = {
            row_id: default for row_id, _, _, default in self.ROW_DEFS
        }
        self._rows: dict[str, _ChartRow] = {}
        for row_id, title, types, default_kind in self.ROW_DEFS:
            row = _ChartRow(rows_root, self, row_id, title, types, default_kind)
            row.frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
            self._rows[row_id] = row

        # Memoise the panel layout so we only rebuild ChartPanel widgets
        # when the project selection actually changes.
        self._cur_project_pairs: list[tuple[int, str]] = []

        # Debounce id for resize-driven refreshes so a window drag doesn't
        # trigger a redraw on every <Configure> event.
        self._refresh_after_id: Optional[str] = None

    @property
    def frame(self) -> ttk.Frame:
        return self._frame

    # ----- public API ----------------------------------------------------
    def schedule_refresh(self) -> None:
        """Debounced version of :meth:`refresh_charts` for resize events.

        The debounce window is intentionally generous (350 ms): every
        ``<Configure>`` from any panel cancels and reschedules the
        timer, so a continuous window-drag never triggers an actual
        redraw — the (expensive) matplotlib rebuild only runs once
        after the drag settles. Pair this with the ``12``-px filter
        in :meth:`ChartPanel._on_canvas_configure` so the timer also
        isn't restarted by sub-pixel jitter.
        """
        try:
            if self._refresh_after_id is not None:
                self._frame.after_cancel(self._refresh_after_id)
        except Exception:
            pass
        try:
            self._refresh_after_id = self._frame.after(350, self._do_scheduled_refresh)
        except Exception:
            # Frame is gone; just do nothing.
            self._refresh_after_id = None

    def _do_scheduled_refresh(self) -> None:
        self._refresh_after_id = None
        self.refresh_charts()

    def reload_year_options(self) -> None:
        """Refresh the year multi-select to reflect the current project."""
        state = self._get_state()
        project = getattr(state, "selected_project", None)
        years_with_data: list[int] = []
        if project is not None:
            years_with_data = self._repo.list_years_with_data(project.id)

        cur_year = int(getattr(state, "year", 0)) if state else 0
        all_years = sorted(set(years_with_data) | ({cur_year} if cur_year else set()))
        if not all_years and cur_year:
            all_years = [cur_year]

        values = [(y, str(y)) for y in all_years]
        self._years_select.set_values(
            values,
            default_keys=[cur_year] if cur_year and cur_year in all_years else None,
            default_all=False,
        )

    def reload_project_options(self) -> None:
        """Refresh the project multi-select; ensure the current project is
        always part of the selection."""
        state = self._get_state()
        project = getattr(state, "selected_project", None)
        projects = self._repo.list_projects()
        values = [(p.id, p.name) for p in projects]

        # Preserve previously-selected projects, but always include the
        # currently-open project so its charts render by default.
        prev = set(self._projects_select.get_selected())
        if project is not None:
            prev.add(project.id)
        available = {pid for pid, _ in values}
        selected = [pid for pid in prev if pid in available]
        if not selected and project is not None:
            selected = [project.id]

        self._projects_select.set_values(
            values,
            default_keys=selected,
            default_all=False,
        )

    def set_row_kind(self, row_id: str, kind: str) -> None:
        if row_id in self._row_kinds:
            self._row_kinds[row_id] = kind
            self.refresh_charts()

    def _clear_filters(self) -> None:
        """Deselect every item in Years, Months, and Projects, then redraw."""
        self._years_select.select_none(silent=True)
        self._months_select.select_none(silent=True)
        self._projects_select.select_none(silent=True)
        # Skip the usual fallbacks (current project / year / January)
        # so the charts actually reflect the cleared selectors.
        self.refresh_charts(allow_fallbacks=False)

    def refresh_charts(self, *, allow_fallbacks: bool = True) -> None:
        """Re-render all panels using current state + selectors."""
        state = self._get_state()

        # Resolve which projects to draw.
        project_ids = list(self._projects_select.get_selected())
        if not project_ids and allow_fallbacks:
            cur = getattr(state, "selected_project", None)
            project_ids = [cur.id] if cur is not None else []

        # Look up names for the currently-selected projects (preserve order).
        all_projects = {p.id: p.name for p in self._repo.list_projects()}
        project_pairs: list[tuple[int, str]] = [
            (pid, all_projects[pid]) for pid in project_ids if pid in all_projects
        ]

        # Rebuild panel widgets only when the project set really changes.
        if project_pairs != self._cur_project_pairs:
            self._cur_project_pairs = list(project_pairs)
            for row in self._rows.values():
                row.rebuild_panels(project_pairs)

        if not project_pairs:
            return

        years = sorted(self._years_select.get_selected())
        months = sorted(self._months_select.get_selected())
        # Fallbacks so a chart never silently goes empty when the user
        # accidentally deselects everything — unless they hit Clear filters.
        if not years:
            years = [int(getattr(state, "year"))] if allow_fallbacks else []
        if not months:
            months = [1] if allow_fallbacks else []  # default fallback: January

        for col_index, (pid, _name) in enumerate(project_pairs):
            self._draw_month_summary(self._rows["month_summary"].panels[col_index], pid, years, months)
            self._draw_year_summary(self._rows["year_summary"].panels[col_index], pid, years)
            self._draw_month_breakdown(self._rows["month_breakdown"].panels[col_index], pid, years, months)
            self._draw_year_breakdown(self._rows["year_breakdown"].panels[col_index], pid, years)

    # --- Month Summary: aggregate expense vs income over selected (year,month) pairs.
    def _draw_month_summary(
        self, panel: ChartPanel, project_id: int, years: Iterable[int], months: Iterable[int]
    ) -> None:
        fig = panel.figure()
        fig.clear()

        years = list(years)
        months = list(months)
        exp = inc = invest = 0
        for y in years:
            for m in months:
                exp += self._repo.get_project_month_total_by_kind(project_id, y, m, CategoryKind.EXPENSE)
                inc += self._repo.get_project_month_total_by_kind(project_id, y, m, CategoryKind.INCOME)
                # ``abs`` for chart display only: pie/bar visuals need
                # non-negative magnitudes. The signed truth lives in the
                # Month Summary header strip's Investments cell.
                invest += abs(self._repo.get_project_month_total_by_kind(
                    project_id, y, m, CategoryKind.INVESTMENT
                ))
        net = inc - exp

        title_periods = self._format_period_title(years, months)
        kind = self._row_kinds["month_summary"]

        if kind in ("pie", "donut"):
            # Three-slice summary: Income vs Expense vs Investment over
            # selected periods. Income leads (matches the Month Summary
            # tile order: Total Income, Total Expenses, …); Investment
            # is the dark-blue tail slice (same outflow direction as
            # Expense, but tracked separately).
            values = [inc, exp, invest]
            labels = ["Income", "Expense", "Investment"]
            kinds = [int(CategoryKind.INCOME), int(CategoryKind.EXPENSE), int(CategoryKind.INVESTMENT)]
            colors = [_INCOME_COLOR, _EXPENSE_COLOR, _INVESTMENT_COLOR]
            non_zero = [
                (v, l, c, k)
                for v, l, c, k in zip(values, labels, colors, kinds)
                if v > 0
            ]
            if not non_zero:
                _restore_constrained_layout(fig)
                ax = fig.add_subplot(111)
                ax.set_axis_off()
                ax.text(0.5, 0.5, "No data", ha="center", va="center", color="#666")
            else:
                vs = [v for v, *_ in non_zero]
                ls = [l for _, l, *_ in non_zero]
                cs = [c for _, _, c, _ in non_zero]
                ks = [k for _, _, _, k in non_zero]
                wedge_kw: dict[str, object] = {"linewidth": 2, "edgecolor": "white"}
                if kind == "donut":
                    wedge_kw["width"] = 0.45
                fontsize, pctdistance, radius = _summary_pie_params()
                ax = _setup_pie_axes(fig, ls)
                wedges, _t, autotexts = ax.pie(
                    vs,
                    labels=[""] * len(vs),
                    colors=cs,
                    autopct=_autopct_with_sign(vs, ks),
                    pctdistance=pctdistance,
                    startangle=90,
                    radius=radius,
                    wedgeprops=wedge_kw,
                    textprops={"fontsize": fontsize},
                )
                # Summary pies always use horizontal labels (regardless
                # of slice size) — there are only ever 3 buckets here
                # so we don't need rotation to disambiguate which label
                # belongs to which wedge, and horizontal text is
                # easier to read.
                _align_autopct_outside(autotexts, wedges, vs, horizontal_threshold=0.0)
                ax.set_xlim(-_PIE_VIEW, _PIE_VIEW)
                ax.set_ylim(-_PIE_VIEW, _PIE_VIEW)
                ax.set_aspect("equal", adjustable="box")
                _draw_pie_legend(ax, fig, wedges, ls)
        elif kind == "bar":
            _restore_constrained_layout(fig)
            ax = fig.add_subplot(111)
            # Four bars: Income / Expense / Investment / Net. Income
            # leads to match the summary-tile order. Net is blue when
            # ≥ 0, red when negative — that hue is independent of the
            # Investment dark-blue and uses the lighter UI accent blue
            # instead.
            ax.bar(
                ["Income", "Expense", "Investment", "Net"],
                [inc / 100.0, exp / 100.0, invest / 100.0, net / 100.0],
                color=[
                    _INCOME_COLOR,
                    _EXPENSE_COLOR,
                    _INVESTMENT_COLOR,
                    "#1976d2" if net >= 0 else _EXPENSE_COLOR,
                ],
            )
            ax.set_ylabel("Amount")
            ax.axhline(0, color="#888", linewidth=0.6)
            ax.tick_params(axis="x", labelsize=9)
            _apply_value_axis_grid(ax, axis="y")

        panel.set_subtitle(title_periods)
        panel.draw()

    # --- Month Breakdown: per-category totals across (year, month) pairs.
    def _draw_month_breakdown(
        self, panel: ChartPanel, project_id: int, years: Iterable[int], months: Iterable[int]
    ) -> None:
        fig = panel.figure()
        fig.clear()
        # Axes are created per-branch below: pie/donut may use manual
        # positioning (single project mode) or the auto-layout
        # ``add_subplot`` (multi project), and bar/hbar always use
        # auto-layout. Picking the right one matters because pie mode
        # disables the figure's layout engine.

        years = list(years)
        months = list(months)

        # Aggregate per-category cents across selected periods. We aggregate
        # by category NAME so categories cloned across years are merged.
        # Investment cells preserve a signed value in the DB (gain vs
        # loss); we take ``abs`` here so pie/bar visuals don't break on
        # negatives. The Investments header total still reports the
        # signed truth. Discrepancy categories are skipped entirely —
        # they're a balance-reconciliation nudge, not a financial event.
        totals: dict[str, list[int]] = {}  # name -> [cents, kind_int]
        invest_kind = int(CategoryKind.INVESTMENT)
        discrepancy_kind = int(CategoryKind.DISCREPANCY)
        for y in years:
            cats = self._repo.list_categories(project_id, y)
            for c in cats:
                if int(c.kind) == discrepancy_kind:
                    continue
                cents = 0
                for m in months:
                    v = self._repo.get_monthly_amount_cents(c.id, y, m)
                    if v is not None:
                        cents += int(v)
                if int(c.kind) == invest_kind:
                    cents = abs(cents)
                if cents == 0:
                    continue
                slot = totals.setdefault(c.name, [0, int(c.kind)])
                slot[0] += cents

        title_periods = self._format_period_title(years, months)
        kind_name = self._row_kinds["month_breakdown"]

        if not totals:
            _restore_constrained_layout(fig)
            ax = fig.add_subplot(111)
            ax.set_axis_off()
            ax.text(0.5, 0.5, "No data", ha="center", va="center", color="#666")
            panel.set_subtitle(title_periods)
            panel.draw()
            return

        items = sorted(totals.items(), key=lambda kv: kv[1][0], reverse=True)
        names = [n for n, _ in items]
        values = [v[0] for _, v in items]
        kinds = [v[1] for _, v in items]
        # Pie/donut: per-category palette so each expense slice has its
        # own colour, mirrored in the legend. Bars: solid kind colour
        # (red Expense / green Income / dark blue Investment).
        bar_colors = [_bar_color_for_kind(k) for k in kinds]

        if kind_name in ("pie", "donut"):
            pie_colors = _category_colors(kinds)
            wedge_kw: dict[str, object] = {"linewidth": 2, "edgecolor": "white"}
            if kind_name == "donut":
                wedge_kw["width"] = 0.45
            fontsize, pctdistance, radius = _breakdown_pie_params()
            # Decide *before* building the axes whether this panel is
            # too narrow for outside autopct labels (typically when
            # multiple projects are selected and each panel only gets
            # a slice of the row). If so, switch to *compact* mode:
            # smaller legend font, smaller margin → bigger pie radius.
            hide_text = _pie_panel_too_narrow_for_outside_text(fig, names)
            legend_font = (
                _PIE_FONT_SIZE_COMPACT if hide_text else _PIE_FONT_SIZE
            )
            if hide_text:
                radius = _PIE_RADIUS_COMPACT
            ax = _setup_pie_axes(fig, names, legend_font)
            autopct_fn = (
                (lambda _pct: "")
                if hide_text
                else _autopct_with_sign(values, kinds)
            )
            wedges, _t, autotexts = ax.pie(
                values,
                labels=[""] * len(values),
                colors=pie_colors,
                autopct=autopct_fn,
                pctdistance=pctdistance,
                startangle=90,
                radius=radius,
                wedgeprops=wedge_kw,
                textprops={"fontsize": fontsize},
            )
            # Breakdowns keep their angled outside-labels (the slices
            # belong to many distinct categories, so following the
            # wedge angle helps tie label to slice). Skipped on very
            # narrow panels where ``hide_text`` blanked the strings.
            if not hide_text:
                _align_autopct_outside(autotexts, wedges, values)
            ax.set_xlim(-_PIE_VIEW, _PIE_VIEW)
            ax.set_ylim(-_PIE_VIEW, _PIE_VIEW)
            ax.set_aspect("equal", adjustable="box")
            _draw_pie_legend(ax, fig, wedges, names, legend_font)
        elif kind_name == "bar":
            _restore_constrained_layout(fig)
            ax = fig.add_subplot(111)
            ax.bar(names, [v / 100.0 for v in values], color=bar_colors)
            ax.set_ylabel("Amount")
            for label in ax.get_xticklabels():
                label.set_rotation(30)
                label.set_horizontalalignment("right")
            _apply_value_axis_grid(ax, axis="y")
        elif kind_name == "hbar":
            _restore_constrained_layout(fig)
            ax = fig.add_subplot(111)
            ax.barh(names[::-1], [v / 100.0 for v in values[::-1]], color=bar_colors[::-1])
            ax.set_xlabel("Amount")
            _apply_value_axis_grid(ax, axis="x")

        panel.set_subtitle(title_periods)
        panel.draw()

    # --- Year Summary: 12 months of expense+income+net for selected years.
    def _draw_year_summary(self, panel: ChartPanel, project_id: int, years: Iterable[int]) -> None:
        fig = panel.figure()
        fig.clear()

        years = list(years)
        if not years:
            _restore_constrained_layout(fig)
            ax = fig.add_subplot(111)
            ax.set_axis_off()
            ax.text(0.5, 0.5, "No data", ha="center", va="center", color="#666")
            panel.set_subtitle("")
            panel.draw()
            return

        # Collect expense/income/investment per month, summed across the
        # selected years. Investments are tracked as their own series so
        # bar/line/stacked charts can show them in dark blue alongside
        # expense (red) and income (green). Investment values are
        # magnitude-only here (``abs``) so pie/bar/stacked don't break
        # on negative values; the signed truth lives in the header
        # strip's Investments cell.
        exp_per_month = [0] * 12
        inc_per_month = [0] * 12
        invest_per_month = [0] * 12
        for y in years:
            for m in range(1, 13):
                exp_per_month[m - 1] += self._repo.get_project_month_total_by_kind(
                    project_id, y, m, CategoryKind.EXPENSE
                )
                inc_per_month[m - 1] += self._repo.get_project_month_total_by_kind(
                    project_id, y, m, CategoryKind.INCOME
                )
                invest_per_month[m - 1] += abs(self._repo.get_project_month_total_by_kind(
                    project_id, y, m, CategoryKind.INVESTMENT
                ))
        net_per_month = [i - e for i, e in zip(inc_per_month, exp_per_month)]

        years_label = ", ".join(str(y) for y in years)
        kind_name = self._row_kinds["year_summary"]

        if kind_name == "pie":
            total_exp = sum(exp_per_month)
            total_inc = sum(inc_per_month)
            total_invest = sum(invest_per_month)
            # Income leads (matches the Year Summary tile order:
            # Total Income, Total Expenses, …); Investment is the
            # dark-blue tail slice.
            values = [total_inc, total_exp, total_invest]
            labels = ["Income", "Expense", "Investment"]
            kinds = [int(CategoryKind.INCOME), int(CategoryKind.EXPENSE), int(CategoryKind.INVESTMENT)]
            colors = [_INCOME_COLOR, _EXPENSE_COLOR, _INVESTMENT_COLOR]
            non_zero = [
                (v, l, c, k)
                for v, l, c, k in zip(values, labels, colors, kinds)
                if v > 0
            ]
            if not non_zero:
                _restore_constrained_layout(fig)
                ax = fig.add_subplot(111)
                ax.set_axis_off()
                ax.text(0.5, 0.5, "No data", ha="center", va="center", color="#666")
            else:
                vs = [v for v, *_ in non_zero]
                ls = [l for _, l, *_ in non_zero]
                cs = [c for _, _, c, _ in non_zero]
                ks = [k for _, _, _, k in non_zero]
                fontsize, pctdistance, radius = _summary_pie_params()
                ax = _setup_pie_axes(fig, ls)
                wedges, _t, autotexts = ax.pie(
                    vs,
                    labels=[""] * len(vs),
                    colors=cs,
                    autopct=_autopct_with_sign(vs, ks),
                    pctdistance=pctdistance,
                    startangle=90,
                    radius=radius,
                    wedgeprops={"linewidth": 2, "edgecolor": "white"},
                    textprops={"fontsize": fontsize},
                )
                # Horizontal labels always (see Month Summary).
                _align_autopct_outside(autotexts, wedges, vs, horizontal_threshold=0.0)
                ax.set_xlim(-_PIE_VIEW, _PIE_VIEW)
                ax.set_ylim(-_PIE_VIEW, _PIE_VIEW)
                ax.set_aspect("equal", adjustable="box")
                _draw_pie_legend(ax, fig, wedges, ls)
        elif kind_name == "bar":
            _restore_constrained_layout(fig)
            ax = fig.add_subplot(111)
            x = range(12)
            # Three side-by-side bars per month: Income / Expense /
            # Investment. Width chosen so 3*width ≈ 0.85 of the slot.
            # Income takes the left slot to match the summary-tile
            # ordering and the pie's category order.
            width = 0.27
            ax.bar([i - width for i in x], [v / 100.0 for v in inc_per_month],
                   width=width, label="Income", color=_INCOME_COLOR)
            ax.bar(list(x), [v / 100.0 for v in exp_per_month],
                   width=width, label="Expense", color=_EXPENSE_COLOR)
            ax.bar([i + width for i in x], [v / 100.0 for v in invest_per_month],
                   width=width, label="Investment", color=_INVESTMENT_COLOR)
            ax.set_xticks(list(x))
            ax.set_xticklabels(EN_MONTHS, fontsize=8)
            ax.set_ylabel("Amount")
            ax.legend(fontsize=8)
            ax.axhline(0, color="#888", linewidth=0.6)
            _apply_value_axis_grid(ax, axis="y")
        elif kind_name == "stacked":
            _restore_constrained_layout(fig)
            ax = fig.add_subplot(111)
            x = range(12)
            # Stack order matches the header strip: Income → Expense →
            # Investment. ``bottom`` accumulates the previous totals.
            ax.bar(x, [v / 100.0 for v in inc_per_month],
                   label="Income", color=_INCOME_COLOR)
            ax.bar(x, [v / 100.0 for v in exp_per_month],
                   bottom=[v / 100.0 for v in inc_per_month],
                   label="Expense", color=_EXPENSE_COLOR)
            ax.bar(x, [v / 100.0 for v in invest_per_month],
                   bottom=[(i + e) / 100.0 for i, e in zip(inc_per_month, exp_per_month)],
                   label="Investment", color=_INVESTMENT_COLOR)
            ax.set_xticks(list(x))
            ax.set_xticklabels(EN_MONTHS, fontsize=8)
            ax.set_ylabel("Amount")
            ax.legend(fontsize=8)
            _apply_value_axis_grid(ax, axis="y")
        elif kind_name == "line":
            _restore_constrained_layout(fig)
            ax = fig.add_subplot(111)
            x = range(12)
            ax.plot(x, [v / 100.0 for v in inc_per_month], marker="o", label="Income", color=_INCOME_COLOR)
            ax.plot(x, [v / 100.0 for v in exp_per_month], marker="o", label="Expense", color=_EXPENSE_COLOR)
            ax.plot(x, [v / 100.0 for v in invest_per_month], marker="o", label="Investment", color=_INVESTMENT_COLOR)
            ax.plot(x, [v / 100.0 for v in net_per_month], marker="o", label="Net", color="#1976d2", linestyle="--")
            ax.axhline(0, color="#888", linewidth=0.6)
            ax.set_xticks(list(x))
            ax.set_xticklabels(EN_MONTHS, fontsize=8)
            ax.set_ylabel("Amount")
            ax.legend(fontsize=8)
            _apply_value_axis_grid(ax, axis="y")

        panel.set_subtitle(years_label)
        panel.draw()

    # --- Year Breakdown: per-category yearly totals across selected years.
    def _draw_year_breakdown(self, panel: ChartPanel, project_id: int, years: Iterable[int]) -> None:
        fig = panel.figure()
        fig.clear()
        # Axes are created per-branch below — see ``_draw_month_breakdown``.

        years = list(years)
        # See ``_draw_month_breakdown`` for the rationale: investment
        # totals are taken as magnitudes so charts render cleanly while
        # the header strip still surfaces the signed result. Discrepancy
        # categories are skipped — they only nudge End of Month and
        # have no place on a financial breakdown chart.
        totals: dict[str, list[int]] = {}  # name -> [cents, kind_int]
        invest_kind = int(CategoryKind.INVESTMENT)
        discrepancy_kind = int(CategoryKind.DISCREPANCY)
        for y in years:
            cats = self._repo.list_categories(project_id, y)
            for c in cats:
                if int(c.kind) == discrepancy_kind:
                    continue
                cents = self._repo.get_year_total_cents(c.id, y)
                if int(c.kind) == invest_kind:
                    cents = abs(cents)
                if cents == 0:
                    continue
                slot = totals.setdefault(c.name, [0, int(c.kind)])
                slot[0] += cents

        years_label = ", ".join(str(y) for y in years) if years else "(no year)"
        kind_name = self._row_kinds["year_breakdown"]

        if not totals:
            _restore_constrained_layout(fig)
            ax = fig.add_subplot(111)
            ax.set_axis_off()
            ax.text(0.5, 0.5, "No data", ha="center", va="center", color="#666")
            panel.set_subtitle(years_label)
            panel.draw()
            return

        items = sorted(totals.items(), key=lambda kv: kv[1][0], reverse=True)
        names = [n for n, _ in items]
        values = [v[0] for _, v in items]
        kinds = [v[1] for _, v in items]
        bar_colors = [_bar_color_for_kind(k) for k in kinds]

        if kind_name in ("pie", "donut"):
            pie_colors = _category_colors(kinds)
            wedge_kw: dict[str, object] = {"linewidth": 2, "edgecolor": "white"}
            if kind_name == "donut":
                wedge_kw["width"] = 0.45
            fontsize, pctdistance, radius = _breakdown_pie_params()
            # See _draw_month_breakdown above — same narrow-panel
            # rule: hide the angled outside ``%, $value`` text and
            # switch to compact mode (smaller legend font + bigger
            # pie radius) when the legend already occupies the full
            # margin budget.
            hide_text = _pie_panel_too_narrow_for_outside_text(fig, names)
            legend_font = (
                _PIE_FONT_SIZE_COMPACT if hide_text else _PIE_FONT_SIZE
            )
            if hide_text:
                radius = _PIE_RADIUS_COMPACT
            ax = _setup_pie_axes(fig, names, legend_font)
            autopct_fn = (
                (lambda _pct: "")
                if hide_text
                else _autopct_with_sign(values, kinds)
            )
            wedges, _t, autotexts = ax.pie(
                values,
                labels=[""] * len(values),
                colors=pie_colors,
                autopct=autopct_fn,
                pctdistance=pctdistance,
                startangle=90,
                radius=radius,
                wedgeprops=wedge_kw,
                textprops={"fontsize": fontsize},
            )
            if not hide_text:
                _align_autopct_outside(autotexts, wedges, values)
            ax.set_xlim(-_PIE_VIEW, _PIE_VIEW)
            ax.set_ylim(-_PIE_VIEW, _PIE_VIEW)
            ax.set_aspect("equal", adjustable="box")
            _draw_pie_legend(ax, fig, wedges, names, legend_font)
        elif kind_name == "bar":
            _restore_constrained_layout(fig)
            ax = fig.add_subplot(111)
            ax.bar(names, [v / 100.0 for v in values], color=bar_colors)
            ax.set_ylabel("Amount")
            for label in ax.get_xticklabels():
                label.set_rotation(30)
                label.set_horizontalalignment("right")
            _apply_value_axis_grid(ax, axis="y")
        elif kind_name == "hbar":
            _restore_constrained_layout(fig)
            ax = fig.add_subplot(111)
            ax.barh(names[::-1], [v / 100.0 for v in values[::-1]], color=bar_colors[::-1])
            ax.set_xlabel("Amount")
            _apply_value_axis_grid(ax, axis="x")

        panel.set_subtitle(years_label)
        panel.draw()

    # ---------------------------------------------------------------
    @staticmethod
    def _format_period_title(years: list[int], months: list[int]) -> str:
        if not years:
            years_str = "(no year)"
        elif len(years) <= 3:
            years_str = ", ".join(str(y) for y in years)
        else:
            years_str = f"{years[0]}\u2013{years[-1]} ({len(years)} yrs)"

        if not months or len(months) == 12:
            months_str = "all months"
        elif len(months) <= 3:
            months_str = ", ".join(EN_MONTHS[m - 1] for m in months)
        else:
            months_str = f"{len(months)} months"
        return f"{years_str} \u00b7 {months_str}"
