"""Reusable Tk/ttk custom widgets used across the UI.

Contents:
    - :class:`CollapsibleSection`   header + body that can expand/collapse
    - :class:`VerticalScrolledFrame` an outer page that scrolls vertically
    - :class:`TreeviewGridlines`    overlay that paints cell borders + wheel scroll
    - :class:`TreeviewCellHighlight` single-cell blue highlight + context menu
    - :class:`TreeviewCellEditor`   double-click-to-edit Entry overlay

The Treeview helpers compose so a single ``ttk.Treeview`` can have all
three behaviours wired at once (gridlines + highlight + editor), as the
Month Breakdown grid does.

The module also contains a small block of "browser-like wheel scrolling"
helpers used by both the page-level scroll frame and the Treeview wheel
handlers — see the section banner below for details.
"""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional


# ----- Browser-like wheel scrolling helpers --------------------------------
# Tk has no compositor and no native scroll easing, so wheel events are
# instantaneous "jump to the next position" by default. These helpers
# approximate the way Chromium / a normal webpage maps wheel input so the
# app feels closer to scrolling a website (without adding momentum/easing,
# which can feel laggy). They are shared by:
#
#   * :class:`VerticalScrolledFrame` — the page-level scroll container
#   * :class:`TreeviewGridlines`     — wheel handler inside Treeviews
#
# What the helpers do:
#   * pixel-precise scrolling (paired with ``yscrollincrement=1`` on the
#     canvas) so one wheel notch ≈ 120 px, like a browser;
#   * cap a single event at one notch so a huge trackpad flick can't
#     teleport the view (``_WHEEL_DELTA_CAP``);
#   * flush pending Tk paints during *fast* wheel bursts only
#     (``_WheelScrollPaintGate``) so multiple wheel events don't collapse
#     into a single visible jump — mirrors how a browser shows
#     intermediate scroll positions without paying any cost when scrolling
#     at a normal speed;
#   * give Treeviews a *proportional* step: number of rows per notch is
#     derived from row height.

# One standard Windows wheel notch in ``event.delta`` units.
_WHEEL_NOTCH_DELTA = 120
# Cap a single event so a huge trackpad flick cannot teleport the view.
_WHEEL_DELTA_CAP = _WHEEL_NOTCH_DELTA
# Flush paints when events arrive faster than this (fast scroll burst).
_FAST_WHEEL_PAINT_GAP_S = 0.05
# Linux Button-4/5 has no ``event.delta`` — use ~⅓ notch per tick.
_LINUX_WHEEL_PIXELS = 40


def _clamp_wheel_delta(delta: int, cap: int = _WHEEL_DELTA_CAP) -> int:
    """Clamp a raw ``event.delta`` value to ``±cap`` pixels.

    Stops a single oversized event (e.g. a fast trackpad flick) from
    teleporting the view. Normal mouse wheels never exceed the cap, so
    this is a no-op for them.
    """
    if not delta:
        return 0
    cap = abs(cap)
    if abs(delta) <= cap:
        return int(delta)
    return cap if delta > 0 else -cap


def _wheel_event_pixels(event: tk.Event) -> int:
    """Signed pixel delta from a wheel event (0 if none)."""
    raw = getattr(event, "delta", 0) or 0
    if raw:
        return _clamp_wheel_delta(int(raw))
    return 0


class _WheelScrollPaintGate:
    """Force a paint flush *only* during rapid wheel bursts.

    Tk batches widget repaints onto an idle queue. When wheel events
    arrive faster than the next paint, several scroll steps can collapse
    into a single visible jump. We track the gap between consecutive
    wheel events and call ``update_idletasks`` only when that gap is
    smaller than ``_FAST_WHEEL_PAINT_GAP_S`` (default 50 ms) — so normal
    or slow scrolling pays no extra cost, but fast spinning gets one
    visible frame per event.
    """

    def __init__(self) -> None:
        self._last_wheel_mono: Optional[float] = None

    def after_wheel(self, widget: tk.Widget) -> None:
        now = time.monotonic()
        prev = self._last_wheel_mono
        self._last_wheel_mono = now
        if prev is not None and (now - prev) < _FAST_WHEEL_PAINT_GAP_S:
            try:
                widget.update_idletasks()
            except Exception:
                pass


# ----- CollapsibleSection -------------------------------------------------
# Header (chevron + title) + body. Clicking anywhere on the header toggles
# the body's visibility. Used to stack Month Summary / Month Breakdown /
# Year Summary / Year Breakdown / Charts down the project page.
class CollapsibleSection(ttk.Frame):
    """A vertically stacked section with a clickable header that toggles its body.

    Use the ``body`` attribute as the parent for content widgets. The header
    shows a chevron + title and can be clicked anywhere to expand/collapse.

    When ``expand_when_open`` is True (default), the section claims a share
    of vertical space when expanded (``pack`` with ``fill=BOTH, expand=True``)
    and shrinks to just the header height when collapsed
    (``pack`` with ``fill=X, expand=False``). The parent should pack the
    section once after construction; the section will re-configure its own
    pack options on toggle.
    """

    CHEVRON_DOWN = "\u25be"   # ▾
    CHEVRON_RIGHT = "\u25b8"  # ▸

    def __init__(
        self,
        parent: tk.Widget,
        title: str,
        *,
        expanded: bool = True,
        expand_when_open: bool = True,
    ):
        super().__init__(parent)
        self._title = title
        self._expanded = bool(expanded)
        self._expand_when_open = bool(expand_when_open)
        self._on_toggle: Optional[Callable[[bool], None]] = None

        self._header = tk.Frame(self, bg="#f0f0f0", cursor="hand2")
        self._header.pack(fill=tk.X)

        self._chevron_lbl = tk.Label(
            self._header,
            text=self._chevron_text(),
            bg="#f0f0f0",
            font=("Segoe UI", 10, "bold"),
            padx=8,
            pady=6,
            cursor="hand2",
        )
        self._chevron_lbl.pack(side=tk.LEFT)

        self._title_lbl = tk.Label(
            self._header,
            text=title,
            bg="#f0f0f0",
            font=("Segoe UI", 10, "bold"),
            padx=0,
            pady=6,
            cursor="hand2",
        )
        self._title_lbl.pack(side=tk.LEFT)

        for w in (self._header, self._chevron_lbl, self._title_lbl):
            w.bind("<Button-1>", self._on_header_click, add=True)

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X)

        self.body = ttk.Frame(self)
        if self._expanded:
            self.body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 8))

    def _chevron_text(self) -> str:
        return self.CHEVRON_DOWN if self._expanded else self.CHEVRON_RIGHT

    def _on_header_click(self, _event: tk.Event) -> None:
        self.toggle()

    def set_on_toggle(self, callback: Optional[Callable[[bool], None]]) -> None:
        self._on_toggle = callback

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def _apply_pack_for_state(self) -> None:
        """Reconfigure our own pack options so collapsed = header-only.

        Only attempts pack_configure if the section is currently packed
        (otherwise raises TclError), so it's safe before parent has
        positioned the section.
        """
        try:
            if self._expanded and self._expand_when_open:
                self.pack_configure(fill=tk.BOTH, expand=True)
            else:
                self.pack_configure(fill=tk.X, expand=False)
        except tk.TclError:
            pass

    def set_expanded(self, expanded: bool) -> None:
        if expanded == self._expanded:
            return
        self._expanded = bool(expanded)
        self._chevron_lbl.configure(text=self._chevron_text())
        if self._expanded:
            self.body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 8))
        else:
            self.body.pack_forget()
        self._apply_pack_for_state()
        if self._on_toggle is not None:
            try:
                self._on_toggle(self._expanded)
            except Exception:
                pass

    @property
    def expanded(self) -> bool:
        return self._expanded


# ----- VerticalScrolledFrame ---------------------------------------------
# A canvas-backed scrollable container. The project page wraps all of the
# stacked CollapsibleSections in one of these so the page can scroll if
# the combined section heights exceed the window.
#
# Wheel scrolling here uses the browser-like helpers above:
#   * raw ``event.delta`` pixels straight to ``yview_scroll`` (paired
#     with ``yscrollincrement=1`` on the canvas);
#   * single-event clamp so trackpad flicks don't teleport the view;
#   * paint-flush gate so fast wheel bursts render every step instead of
#     batching multiple jumps into one frame.
class VerticalScrolledFrame(ttk.Frame):
    """A vertically scrollable container.

    Add children to ``self.inner``. A bind_all hook forwards wheel events
    anywhere in the app to this canvas, but only when the event widget is
    a descendant of ``self.inner`` (so wheel events on other parts of the
    application — e.g. a home page — are unaffected).

    Inner widgets that have their own wheel handling (e.g. ``ttk.Treeview``
    via ``TreeviewGridlines``) should return ``"break"`` only when they
    actually consumed the scroll (i.e. have something to scroll). When
    they have nothing to scroll, return ``None`` so the outer page scrolls
    instead.
    """

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)

        # ``yscrollincrement=1`` makes one "unit" of ``yview_scroll`` equal
        # exactly 1 pixel. Without it, Tk falls back to ~10% of the
        # viewport height per unit, which produces a jumpy ~30%-per-notch
        # feel. With it, the wheel handler below can pass raw
        # ``event.delta`` pixel counts straight through and get smooth,
        # browser-like scrolling that also reacts well to high-precision
        # touchpads.
        self._canvas = tk.Canvas(
            self, highlightthickness=0, borderwidth=0, yscrollincrement=1
        )
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self.inner = ttk.Frame(self._canvas)
        self._inner_id = self._canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._wheel_paint_gate = _WheelScrollPaintGate()

        # Catch wheel events anywhere in the app; only act when over our
        # inner frame.
        self.bind_all("<MouseWheel>", self._on_global_wheel, add=True)
        self.bind_all("<Button-4>", self._on_global_button4, add=True)
        self.bind_all("<Button-5>", self._on_global_button5, add=True)

    def _refresh_scrollregion(self) -> None:
        """Sync scrollregion with the inner frame's actual content height.

        When content fits inside the viewport (content_h <= canvas_h), force
        the scroll position back to the top so the user can never reveal
        empty canvas space by scrolling.
        """
        try:
            self.inner.update_idletasks()
            w = max(self.inner.winfo_reqwidth(), self._canvas.winfo_width())
            h = self.inner.winfo_reqheight()
            canvas_h = self._canvas.winfo_height()
            self._canvas.configure(scrollregion=(0, 0, w, h))
            if h <= canvas_h:
                self._canvas.yview_moveto(0.0)
        except Exception:
            pass

    def _on_inner_configure(self, _event: tk.Event) -> None:
        self._refresh_scrollregion()

    def _on_canvas_configure(self, event: tk.Event) -> None:
        # Make inner frame match canvas width so children fill horizontally.
        self._canvas.itemconfigure(self._inner_id, width=event.width)
        self._refresh_scrollregion()

    def snap_to_top(self) -> None:
        """Reset scrollregion + scroll position after layout changes.

        Called twice (idle + after a short delay) because Tk may still be
        finalising geometry the first time around.
        """
        def _do() -> None:
            self._refresh_scrollregion()
            try:
                self._canvas.yview_moveto(0.0)
            except Exception:
                pass

        try:
            self.after_idle(_do)
            self.after(50, _do)
        except Exception:
            _do()

    def _is_descendant_of_inner(self, widget) -> bool:
        # widget can be a Tk path string (popdowns, etc.).
        if isinstance(widget, str):
            try:
                widget = self.nametowidget(widget)
            except Exception:
                return False
        w = widget
        while w is not None:
            if w is self.inner:
                return True
            w = getattr(w, "master", None)
        return False

    def _can_scroll_vertically(self) -> bool:
        """True iff content height exceeds the viewport height."""
        try:
            return self.inner.winfo_reqheight() > self._canvas.winfo_height()
        except Exception:
            return True

    def _on_global_wheel(self, event: tk.Event) -> None:
        if not self._is_descendant_of_inner(event.widget):
            return None
        if not self._can_scroll_vertically():
            return None
        pixels = _wheel_event_pixels(event)
        if pixels:
            self._canvas.yview_scroll(-pixels, "units")
            self._wheel_paint_gate.after_wheel(self._canvas)
        return None

    def _on_global_button4(self, event: tk.Event) -> None:
        if not self._is_descendant_of_inner(event.widget):
            return None
        if not self._can_scroll_vertically():
            return None
        self._canvas.yview_scroll(-_LINUX_WHEEL_PIXELS, "units")
        self._wheel_paint_gate.after_wheel(self._canvas)
        return None

    def _on_global_button5(self, event: tk.Event) -> None:
        if not self._is_descendant_of_inner(event.widget):
            return None
        if not self._can_scroll_vertically():
            return None
        self._canvas.yview_scroll(_LINUX_WHEEL_PIXELS, "units")
        self._wheel_paint_gate.after_wheel(self._canvas)
        return None


# ----- TreeviewGridlines --------------------------------------------------
# ttk.Treeview has no real cell borders on most themes. This helper paints
# 1px Frame overlays at column / row boundaries to simulate a grid, and
# adds mouse-wheel + Shift-mouse-wheel scrolling for free.
#
# Wheel handling reuses the browser-like helpers (clamped delta, paint
# gate). The vertical step is *proportional* — derived from the actual
# row height and the wheel event's pixel delta — so a single small
# trackpad nudge advances by roughly the same fraction of a row that the
# main page would scroll.
class TreeviewGridlines:
    """
    Draw lightweight gridlines for a ttk.Treeview without hiding content.

    Implemented by placing thin 1px Frames over the Treeview. Also provides
    mousewheel scrolling convenience:
    - Wheel: vertical scroll (proportional to the wheel pixel delta)
    - Shift+Wheel: horizontal scroll (also proportional)
    """

    def __init__(self, container: tk.Widget, tree: ttk.Treeview, color: str, *, enable_wheel: bool = True):
        self._container = container
        self._tree = tree
        self._color = color
        self._enable_wheel = enable_wheel
        self._v_lines: list[tk.Frame] = []
        self._h_lines: list[tk.Frame] = []
        self._wheel_paint_gate = _WheelScrollPaintGate()

        tree.bind("<Configure>", lambda _e: self.redraw(), add=True)
        tree.bind("<<TreeviewSelect>>", lambda _e: self.redraw(), add=True)

        if self._enable_wheel:
            # Mouse wheel scrolling (Windows/macOS)
            tree.bind("<MouseWheel>", self._on_mousewheel, add=True)
            tree.bind("<Shift-MouseWheel>", self._on_shift_mousewheel, add=True)
            # Linux wheel events
            tree.bind("<Button-4>", self._on_button4, add=True)
            tree.bind("<Button-5>", self._on_button5, add=True)
            tree.bind("<Shift-Button-4>", self._on_shift_button4, add=True)
            tree.bind("<Shift-Button-5>", self._on_shift_button5, add=True)

        self.redraw()

    def _bind_line_events(self, w: tk.Widget) -> None:
        # Ensure wheel works when hovering over the 1px line overlays.
        if not self._enable_wheel:
            return
        w.bind("<MouseWheel>", self._on_mousewheel, add=True)
        w.bind("<Shift-MouseWheel>", self._on_shift_mousewheel, add=True)
        w.bind("<Button-4>", self._on_button4, add=True)
        w.bind("<Button-5>", self._on_button5, add=True)
        w.bind("<Shift-Button-4>", self._on_shift_button4, add=True)
        w.bind("<Shift-Button-5>", self._on_shift_button5, add=True)

    def _tree_row_height(self) -> int:
        """Best-effort row height in pixels for proportional wheel steps."""
        try:
            for item_id in self._tree.get_children(""):
                bb = self._tree.bbox(item_id)
                if bb:
                    return max(int(bb[3]), 1)
        except Exception:
            pass
        return 24

    def _tree_wheel_direction(self, event: tk.Event) -> int:
        """+1 scroll down, -1 scroll up, 0 if no wheel input."""
        pixels = _wheel_event_pixels(event)
        if pixels:
            return -1 if pixels > 0 else 1
        num = getattr(event, "num", None)
        if num == 4:
            return -1
        if num == 5:
            return 1
        return 0

    def _tree_vertical_pixels(self, event: tk.Event) -> int:
        pixels = _wheel_event_pixels(event)
        if pixels:
            return abs(pixels)
        num = getattr(event, "num", None)
        if num in (4, 5):
            return _LINUX_WHEEL_PIXELS
        return 0

    def _tree_can_scroll_vertically(self) -> bool:
        try:
            yv = self._tree.yview()
        except Exception:
            return False
        if not yv:
            return False
        # If yview spans the full range (0.0 .. 1.0) the tree has nothing
        # to scroll vertically, so let the outer page handle the wheel.
        return not (abs(yv[0]) < 1e-9 and abs(yv[1] - 1.0) < 1e-9)

    def _on_mousewheel(self, event: tk.Event):
        # If the tree has nothing to scroll vertically, don't consume the
        # event — let the outer page scroll handler take it.
        if not self._tree_can_scroll_vertically():
            return None
        direction = self._tree_wheel_direction(event)
        pixels = self._tree_vertical_pixels(event)
        if direction and pixels:
            row_h = self._tree_row_height()
            units = max(1, round(pixels / row_h))
            self._tree.yview_scroll(direction * units, "units")
            self.redraw()
            self._wheel_paint_gate.after_wheel(self._tree)
        return "break"

    def _on_shift_mousewheel(self, event: tk.Event) -> str:
        direction = self._tree_wheel_direction(event)
        pixels = self._tree_vertical_pixels(event)
        if direction and pixels:
            # Convert wheel pixel-delta into Treeview "x-scroll units".
            # ttk.Treeview has no pixel-based xview, so we approximate
            # ~3 px per unit — a single 120-px Windows notch now advances
            # ~40 units, so wide tables don't take a dozen notches to
            # cross. Still proportional to the wheel pixel delta, so a
            # tiny trackpad nudge stays small.
            col_steps = max(1, round(pixels / 3))
            self._tree.xview_scroll(direction * col_steps, "units")
            self.redraw()
            self._wheel_paint_gate.after_wheel(self._tree)
        return "break"

    def _on_button4(self, event: tk.Event) -> str:
        return self._on_mousewheel(event)

    def _on_button5(self, event: tk.Event) -> str:
        return self._on_mousewheel(event)

    def _on_shift_button4(self, event: tk.Event) -> str:
        return self._on_shift_mousewheel(event)

    def _on_shift_button5(self, event: tk.Event) -> str:
        return self._on_shift_mousewheel(event)

    def _ensure_lines(self, want_v: int, want_h: int) -> None:
        while len(self._v_lines) < want_v:
            f = tk.Frame(self._container, bg=self._color)
            self._bind_line_events(f)
            self._v_lines.append(f)
        while len(self._h_lines) < want_h:
            f = tk.Frame(self._container, bg=self._color)
            self._bind_line_events(f)
            self._h_lines.append(f)

        for i, ln in enumerate(self._v_lines):
            if i >= want_v:
                ln.place_forget()
        for i, ln in enumerate(self._h_lines):
            if i >= want_h:
                ln.place_forget()

    def redraw(self) -> None:
        t = self._tree
        cols = list(t["columns"])
        if not cols:
            self._ensure_lines(0, 0)
            return

        widths = [int(t.column(col, "width")) for col in cols]
        total_w = max(sum(widths), 1)
        xfrac = t.xview()[0] if t.xview() else 0.0
        xoff = int(total_w * xfrac)

        x_positions: list[int] = []
        x = -xoff
        for cw in widths[:-1]:
            x += cw
            if x >= 0:
                x_positions.append(x)

        y_positions: list[int] = []
        first_row_top: Optional[int] = None
        for item_id in t.get_children(""):
            bb = t.bbox(item_id)
            if not bb:
                continue
            _x, y, _iw, ih = bb
            if first_row_top is None:
                first_row_top = y
            y_positions.append(y + ih)

        header_sep_y = first_row_top if first_row_top is not None else 28
        if header_sep_y not in y_positions:
            y_positions.insert(0, header_sep_y)

        self._ensure_lines(len(x_positions), len(y_positions))

        ox = t.winfo_x()
        oy = t.winfo_y()
        w = t.winfo_width()
        h = t.winfo_height()

        for i, xp in enumerate(x_positions):
            self._v_lines[i].place(x=ox + xp, y=oy, width=1, height=h)
        for i, yp in enumerate(y_positions):
            self._h_lines[i].place(x=ox, y=oy + yp, width=w, height=1)


# ----- TreeviewTempHeaders -----------------------------------------------
# Overlays a coloured Label over the header cell of each "temporary"
# (un-assigned CSV import) column. The label mirrors the heading text on a
# light-red background and forwards wheel scrolling + right-click so the grid
# stays usable underneath. Repositioned on every scroll/resize via ``redraw``.
class TreeviewTempHeaders:
    def __init__(
        self,
        container: tk.Widget,
        tree: ttk.Treeview,
        color: str,
        *,
        gridlines: "TreeviewGridlines",
        on_rightclick=None,
        top_border_color: Optional[str] = None,
    ):
        self._container = container
        self._tree = tree
        self._color = color
        self._gridlines = gridlines
        # Colour for the header's top edge (the table's outer border). Falls
        # back to the gridline colour if not supplied.
        self._line_color = top_border_color or getattr(gridlines, "_color", "#d0d0d0")
        self._on_rightclick = on_rightclick
        # col_id ("#2", ...) -> heading text, for columns to tint.
        self._cols: dict[str, str] = {}
        # Each tinted column is a clip Frame (placed at the visible slice of
        # the column) holding a Label sized to the full column width, so the
        # title stays centred on the column and overflow is clipped.
        self._cells: list[tuple[tk.Frame, tk.Label]] = []
        # 1px line frames drawn along the top edge of each tinted header cell.
        self._top_lines: list[tk.Frame] = []
        tree.bind("<Configure>", lambda _e: self.redraw(), add=True)

    def set_temp_columns(self, mapping: dict[str, str]) -> None:
        self._cols = dict(mapping)
        self.redraw()

    def _make_cell(self) -> tuple[tk.Frame, tk.Label]:
        clip = tk.Frame(self._container, background=self._color, borderwidth=0, highlightthickness=0)
        lbl = tk.Label(
            clip,
            background=self._color,
            anchor="center",
            borderwidth=0,
            highlightthickness=0,
        )
        # Keep wheel scrolling alive when the cursor is over the overlay.
        for w in (clip, lbl):
            w.bind("<MouseWheel>", self._gridlines._on_mousewheel, add=True)
            w.bind("<Shift-MouseWheel>", self._gridlines._on_shift_mousewheel, add=True)
            w.bind("<Button-4>", self._gridlines._on_button4, add=True)
            w.bind("<Button-5>", self._gridlines._on_button5, add=True)
        return clip, lbl

    def _ensure_cells(self, n: int) -> None:
        while len(self._cells) < n:
            self._cells.append(self._make_cell())
        for i, (clip, _lbl) in enumerate(self._cells):
            if i >= n:
                clip.place_forget()

    def _ensure_top_lines(self, n: int) -> None:
        while len(self._top_lines) < n:
            self._top_lines.append(
                tk.Frame(self._container, background=self._line_color, height=1, borderwidth=0)
            )
        for i, ln in enumerate(self._top_lines):
            if i >= n:
                ln.place_forget()

    def redraw(self) -> None:
        t = self._tree
        cols = list(t["columns"])
        if not cols or not self._cols:
            self._ensure_cells(0)
            self._ensure_top_lines(0)
            return

        widths = [int(t.column(col, "width")) for col in cols]
        total_w = max(sum(widths), 1)
        xfrac = t.xview()[0] if t.xview() else 0.0
        xoff = int(total_w * xfrac)

        # Header height ~ top of the first data row.
        header_h = 28
        for item_id in t.get_children(""):
            bb = t.bbox(item_id)
            if bb:
                header_h = max(int(bb[1]), 1)
                break

        ox = t.winfo_x()
        oy = t.winfo_y()
        view_w = t.winfo_width()

        # Collect (col_id, text, left_x, width) for each temp column.
        placements: list[tuple[str, str, int, int]] = []
        x = -xoff
        for i, col in enumerate(cols):
            col_id = f"#{i + 1}"
            cw = widths[i]
            if col_id in self._cols:
                placements.append((col_id, self._cols[col_id], x, cw))
            x += cw

        self._ensure_cells(len(placements))
        self._ensure_top_lines(len(placements))
        for i, (col_id, text, left, cw) in enumerate(placements):
            clip, lbl = self._cells[i]
            top_line = self._top_lines[i]
            # The clip frame covers the visible slice of the column.
            vis_left = max(left, 0)
            vis_right = min(left + cw, view_w)
            if vis_right <= vis_left:
                clip.place_forget()
                top_line.place_forget()
                continue
            lbl.configure(text=text)
            if self._on_rightclick is not None:
                for w in (clip, lbl):
                    w.bind(
                        "<Button-3>",
                        lambda e, c=col_id: self._on_rightclick(c, e),
                    )
            clip.place(
                x=ox + vis_left, y=oy, width=(vis_right - vis_left), height=header_h
            )
            # The label keeps the full column width inside the clip, so the
            # title is centred on the column and overflow is clipped.
            lbl.place(x=left - vis_left, y=0, width=cw, height=header_h)
            top_line.place(
                x=ox + vis_left, y=oy, width=(vis_right - vis_left), height=1
            )

        # Raise the grid lines and top-edge lines above the coloured labels so
        # the table outline stays visible through the red tint.
        try:
            for ln in self._gridlines._v_lines + self._gridlines._h_lines:
                ln.lift()
            for ln in self._top_lines:
                ln.lift()
        except Exception:
            pass


# ----- TreeviewCellHighlight ---------------------------------------------
# Replaces ttk.Treeview's default full-row blue selection with a single
# clicked cell. Owns Copy/Cut/Paste/Delete keyboard shortcuts and the
# right-click context menu for a cell. Wires through optional callbacks
# (``on_copy``, ``on_paste``, ``on_typing``, ...) which the host (e.g.
# main_window.py) implements.
class TreeviewCellHighlight:
    """
    Highlight only the clicked cell of a ttk.Treeview (instead of the whole row).

    A small Label is overlaid on top of the clicked cell and forwards
    Double-Click to the Treeview so the cell editor still works. Pair this
    with a style.map override that hides the default row-selection blue.
    """

    def __init__(
        self,
        tree: ttk.Treeview,
        *,
        bg: str = "#1976d2",
        fg: str = "white",
        on_double_click: Optional[Callable[[str, str], None]] = None,
        on_delete: Optional[Callable[[str, str], None]] = None,
        on_typing: Optional[Callable[[str, str, str], None]] = None,
        on_copy: Optional[Callable[[str, str], None]] = None,
        on_cut: Optional[Callable[[str, str], None]] = None,
        on_paste: Optional[Callable[[str, str], None]] = None,
    ):
        self._tree = tree
        self._bg = bg
        self._fg = fg
        self._on_double_click = on_double_click
        self._on_delete = on_delete
        self._on_typing = on_typing
        self._on_copy = on_copy
        self._on_cut = on_cut
        self._on_paste = on_paste
        self._label: Optional[tk.Label] = None
        self._cell: Optional[tuple[str, str]] = None  # (item_id, col_id)
        self._pending_show: Optional[str] = None

        tree.bind("<Button-1>", self._on_click, add=True)
        tree.bind("<Double-1>", self._on_tree_double_click, add=True)
        tree.bind("<Configure>", lambda _e: self._tree.after_idle(self.reposition), add=True)
        tree.bind("<KeyPress-BackSpace>", self._on_delete_key, add=True)
        tree.bind("<KeyPress-Delete>", self._on_delete_key, add=True)
        tree.bind("<KeyPress>", self._on_key_press, add=True)
        # Copy / Cut / Paste keyboard shortcuts (no-op if no callback wired).
        for seq in ("<Control-c>", "<Control-C>"):
            tree.bind(seq, self._on_copy_key, add=True)
        for seq in ("<Control-x>", "<Control-X>"):
            tree.bind(seq, self._on_cut_key, add=True)
        for seq in ("<Control-v>", "<Control-V>"):
            tree.bind(seq, self._on_paste_key, add=True)
        # Right-click to open the per-cell context menu.
        tree.bind("<Button-3>", self._on_tree_right_click, add=True)

    def cancel_pending(self) -> None:
        if self._pending_show:
            try:
                self._tree.after_cancel(self._pending_show)
            except Exception:
                pass
            self._pending_show = None

    def _on_tree_double_click(self, _event: tk.Event) -> None:
        # Let the cell editor open without the highlight overlay blocking it.
        self.cancel_pending()
        self.clear()

    def _on_click(self, event: tk.Event) -> None:
        region = self._tree.identify("region", event.x, event.y)
        if region != "cell":
            self.clear()
            return
        item_id = self._tree.identify_row(event.y)
        col_id = self._tree.identify_column(event.x)
        if not item_id or not col_id:
            self.clear()
            return
        self.cancel_pending()
        self._cell = (item_id, col_id)
        # Make sure key events (Backspace/Delete) target the tree.
        try:
            self._tree.focus_set()
        except Exception:
            pass
        # Delay highlight so a double-click can reach the editor first.
        self._pending_show = self._tree.after(200, self._show)

    def _on_delete_key(self, _event: tk.Event) -> str:
        if not self._cell or self._on_delete is None:
            return ""
        item_id, col_id = self._cell
        self._on_delete(item_id, col_id)
        return "break"

    def _on_key_press(self, event: tk.Event) -> str:
        # Only react when a cell is currently highlighted and the caller
        # registered a typing handler.
        if not self._cell or self._on_typing is None:
            return ""
        # Skip navigation / control keys explicitly. Anything else with a
        # printable character will pass through.
        keysym = getattr(event, "keysym", "") or ""
        if keysym in (
            "Tab", "Return", "Escape", "BackSpace", "Delete",
            "Up", "Down", "Left", "Right",
            "Home", "End", "Prior", "Next", "Insert",
            "Shift_L", "Shift_R", "Control_L", "Control_R",
            "Alt_L", "Alt_R", "Caps_Lock", "Num_Lock", "Scroll_Lock",
            "Super_L", "Super_R", "Win_L", "Win_R", "Menu",
        ):
            return ""
        char = getattr(event, "char", "") or ""
        # Single printable character only. Ctrl+letter produces a control
        # char (\x01-\x1A) which fails isprintable(); Alt combos usually
        # have empty char. So this filter is sufficient.
        if len(char) != 1 or not char.isprintable():
            return ""
        item_id, col_id = self._cell
        self._on_typing(item_id, col_id, char)
        return "break"

    def _show(self) -> None:
        if not self._cell:
            return
        item_id, col_id = self._cell
        bbox = self._tree.bbox(item_id, col_id)
        if not bbox:
            self.clear()
            return
        x, y, w, h = bbox
        text = ""
        try:
            text = self._tree.set(item_id, col_id)
        except Exception:
            text = ""
        if self._label is None:
            self._label = tk.Label(
                self._tree, bg=self._bg, fg=self._fg, bd=0, anchor="center",
            )
            self._label.bind("<Double-1>", self._on_label_dbl_click, add=True)
            self._label.bind("<Button-3>", self._on_label_right_click, add=True)
            # Forward Backspace/Delete from the label to the tree handler.
            self._label.bind("<KeyPress-BackSpace>", self._on_delete_key, add=True)
            self._label.bind("<KeyPress-Delete>", self._on_delete_key, add=True)
        self._label.configure(text=text)
        self._label.place(x=x, y=y, width=w, height=h)
        self._label.lift()

    def reposition(self) -> None:
        if not self._cell:
            return
        item_id, col_id = self._cell
        try:
            bbox = self._tree.bbox(item_id, col_id)
        except Exception:
            bbox = None
        if bbox:
            self._show()
        else:
            self.clear()

    def clear(self) -> None:
        self.cancel_pending()
        self._cell = None
        if self._label is not None:
            self._label.place_forget()

    def get_cell(self) -> Optional[tuple[str, str]]:
        """Return the currently highlighted ``(item_id, col_id)``, or
        ``None`` when no cell is selected."""
        return self._cell

    def select_cell(
        self, item_id: str, col_id: str, *, immediate: bool = True
    ) -> None:
        """Programmatically highlight a cell (keyboard navigation, Enter
        moving down, etc.).

        ``immediate=True`` paints the blue overlay right away instead of
        waiting 200 ms (the click path delays so double-click can reach
        the editor first).
        """
        self.cancel_pending()
        self._cell = (item_id, col_id)
        try:
            self._tree.focus_set()
        except Exception:
            pass
        if immediate:
            self._show()
        else:
            self._pending_show = self._tree.after(200, self._show)

    def _on_label_dbl_click(self, _event: tk.Event) -> None:
        if not self._cell:
            return
        item_id, col_id = self._cell
        if self._label is not None:
            self._label.place_forget()
        self._cell = None
        if self._on_double_click:
            self._on_double_click(item_id, col_id)

    def _on_label_right_click(self, event: tk.Event) -> None:
        if not self._cell:
            return
        self._show_context_menu(event.x_root, event.y_root)

    # ----- Copy / Cut / Paste handlers ------------------------------------
    # Each handler is a no-op when the host didn't wire a callback, so the
    # same widget is reusable in read-only views (Year Breakdown etc.) and
    # the editable Month Breakdown grid.
    def _on_copy_key(self, _event: tk.Event) -> str:
        if self._cell and self._on_copy is not None:
            self._on_copy(*self._cell)
            return "break"
        return ""

    def _on_cut_key(self, _event: tk.Event) -> str:
        if self._cell and self._on_cut is not None:
            self._on_cut(*self._cell)
            return "break"
        return ""

    def _on_paste_key(self, _event: tk.Event) -> str:
        if self._cell and self._on_paste is not None:
            self._on_paste(*self._cell)
            return "break"
        return ""

    def _on_tree_right_click(self, event: tk.Event) -> None:
        """Right-click in a body cell: select that cell and open the menu.

        Returns silently for clicks on headings/separators so other handlers
        bound on the tree (e.g. a header context menu) can still run.
        """
        region = self._tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        item_id = self._tree.identify_row(event.y)
        col_id = self._tree.identify_column(event.x)
        if not item_id or not col_id:
            return
        self.cancel_pending()
        self._cell = (item_id, col_id)
        try:
            self._tree.focus_set()
        except Exception:
            pass
        # Show the highlight overlay immediately at this cell, then the menu.
        self._show()
        self._show_context_menu(event.x_root, event.y_root)

    def _show_context_menu(self, x_root: int, y_root: int) -> None:
        if not self._cell:
            return
        menu = tk.Menu(self._tree, tearoff=0)
        added = False
        if self._on_copy is not None:
            menu.add_command(label="Copy", command=self._do_copy)
            added = True
        if self._on_cut is not None:
            menu.add_command(label="Cut", command=self._do_cut)
            added = True
        if self._on_paste is not None:
            menu.add_command(label="Paste", command=self._do_paste)
            added = True
        if self._on_delete is not None:
            if added:
                menu.add_separator()
            menu.add_command(label="Delete", command=self._do_delete)
            added = True
        if not added:
            return
        try:
            menu.tk_popup(x_root, y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _do_copy(self) -> None:
        if self._cell and self._on_copy is not None:
            self._on_copy(*self._cell)

    def _do_cut(self) -> None:
        if self._cell and self._on_cut is not None:
            self._on_cut(*self._cell)

    def _do_paste(self) -> None:
        if self._cell and self._on_paste is not None:
            self._on_paste(*self._cell)

    def _do_delete(self) -> None:
        if self._cell and self._on_delete is not None:
            self._on_delete(*self._cell)


# ----- TreeviewCellEditor ------------------------------------------------
# Lightweight double-click-to-edit overlay. Places a tk.Entry on top of
# the clicked cell, pre-fills it with the current value, and calls back
# into the host on Return / FocusOut so the host can validate and commit
# to the database. Composes with TreeviewCellHighlight so the highlight
# overlay is dismissed before the editor opens.
class TreeviewCellEditor:
    """
    Small helper to make a ttk.Treeview feel like an editable grid:
    double-click a cell -> Entry overlay -> commit on Enter / focus out.
    """

    def __init__(
        self,
        tree: ttk.Treeview,
        on_commit: Callable[[str, str, str], bool],
        value_transform: Optional[Callable[[str], str]] = None,
        highlight: Optional["TreeviewCellHighlight"] = None,
        on_navigate: Optional[
            Callable[[str, str, str], Optional[tuple[str, str]]]
        ] = None,
    ):
        """``on_commit`` should return ``True`` when the value was saved
        (``False`` on validation failure so navigation is skipped).

        ``on_navigate`` (optional): after a successful commit triggered
        by Tab / Enter / arrow-style keys inside the editor, called as
        ``(item_id, col_id, direction)`` where ``direction`` is one of
        ``"left"``, ``"right"``, ``"up"``, ``"down"``. Returns the next
        ``(item_id, col_id)`` or ``None`` at the grid edge.
        """
        self._tree = tree
        self._on_commit = on_commit
        self._value_transform = value_transform
        self._highlight = highlight
        self._on_navigate = on_navigate
        self._entry: Optional[tk.Entry] = None
        self._editing: Optional[tuple[str, str]] = None  # (item_id, column_id)
        # While ``True``, ``<FocusOut>`` must not commit — we are
        # destroying the Entry on purpose during Tab / Enter navigation.
        self._navigating: bool = False

        tree.bind("<Double-1>", self._start_edit, add=True)

    @property
    def is_editing(self) -> bool:
        return self._entry is not None

    def _safe_bbox(self, item_id: str, col_id: str):
        try:
            return self._tree.bbox(item_id, col_id)
        except Exception:
            return None

    def _prepare_edit(self) -> None:
        if self._highlight is not None:
            self._highlight.cancel_pending()
            self._highlight.clear()

    def start_edit_cell(self, item_id: str, col_id: str, initial_text: Optional[str] = None) -> None:
        """Open the editor overlay for a specific cell (used by cell highlight).

        If ``initial_text`` is provided, the editor opens pre-filled with that
        text (cursor at the end) instead of the current value with the value
        selected. This lets a user start typing right after a single-click
        cell highlight to replace the value.

        Tab navigation can hop to a column that's currently scrolled off
        screen, in which case ``bbox`` returns an empty tuple. We scroll
        the row into view and try again so the editor reliably opens on
        the new cell instead of silently failing.
        """
        self._prepare_edit()
        try:
            self._tree.see(item_id)
        except Exception:
            pass
        bbox = self._safe_bbox(item_id, col_id)
        if not bbox:
            try:
                self._tree.update_idletasks()
            except Exception:
                pass
            bbox = self._safe_bbox(item_id, col_id)
        if not bbox:
            return
        x, y, w, h = bbox
        if w <= 0 or h <= 0:
            return
        self._destroy_entry()
        self._editing = (item_id, col_id)
        self._entry = tk.Entry(self._tree, justify="center")
        if initial_text is None:
            current = self._tree.set(item_id, col_id)
            self._entry.insert(0, current)
            self._entry.select_range(0, tk.END)
        else:
            self._entry.insert(0, initial_text)
            self._entry.icursor(tk.END)
        self._entry.focus_set()
        self._entry.place(x=x, y=y, width=w, height=h)
        self._entry.lift()
        self._install_entry_bindings()

    def _start_edit(self, event: tk.Event) -> None:
        region = self._tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        item_id = self._tree.identify_row(event.y)
        col_id = self._tree.identify_column(event.x)  # "#1", "#2", ...
        if not item_id or not col_id:
            return

        self._prepare_edit()

        x, y, w, h = self._tree.bbox(item_id, col_id)
        if w <= 0 or h <= 0:
            return

        self._destroy_entry()

        current = self._tree.set(item_id, col_id)
        self._editing = (item_id, col_id)
        self._entry = tk.Entry(self._tree, justify="center")
        self._entry.insert(0, current)
        self._entry.select_range(0, tk.END)
        self._entry.focus_set()
        self._entry.place(x=x, y=y, width=w, height=h)
        self._entry.lift()
        self._install_entry_bindings()

    def _install_entry_bindings(self) -> None:
        """Wire editor keys. Tab uses ``KeyPress`` + ``break`` so Tk's
        default focus-traversal (which was swallowing Tab before our
        handler ran) never steals focus away from the grid."""
        if not self._entry:
            return
        e = self._entry
        e.bind("<Return>", self._on_return_key)
        e.bind("<Escape>", self._on_escape_key)
        e.bind("<FocusOut>", self._on_focus_out)
        # ``Tab`` covers plain Tab; ``Shift-Tab`` is how Windows Tk
        # reports Shift+Tab. ``ISO_Left_Tab`` is X11-only (Linux) — on
        # Windows the keysym is rejected as unknown, so we wrap the
        # bind in try/except and silently skip it on platforms where
        # Tk doesn't recognise it.
        e.bind("<KeyPress-Tab>", self._on_tab_key)
        e.bind("<Shift-KeyPress-Tab>", self._on_tab_key)
        try:
            e.bind("<KeyPress-ISO_Left_Tab>", self._on_tab_key)
        except tk.TclError:
            pass

    def _on_escape_key(self, _event: tk.Event) -> str:
        self._destroy_entry()
        return "break"

    def _on_focus_out(self, _event: tk.Event) -> None:
        if self._navigating:
            return
        self._commit_value()

    def _on_return_key(self, _event: tk.Event) -> str:
        # Spreadsheet Enter: save and move selection down (stay in
        # select mode — do not auto-open the editor on the next row).
        self._commit_and_navigate("down", edit_next=False)
        return "break"

    def _on_tab_key(self, event: tk.Event) -> str:
        keysym = getattr(event, "keysym", "") or ""
        backward = keysym == "ISO_Left_Tab" or bool(int(getattr(event, "state", 0)) & 0x1)
        self._commit_and_navigate("left" if backward else "right", edit_next=True)
        return "break"

    def _commit_and_navigate(self, direction: str, *, edit_next: bool) -> None:
        if not self._entry or not self._editing:
            return
        cur_item, cur_col = self._editing
        if not self._commit_value():
            return
        if self._on_navigate is None:
            return
        try:
            nxt = self._on_navigate(cur_item, cur_col, direction)
        except Exception:
            nxt = None
        if not nxt:
            return
        nxt_item, nxt_col = nxt

        # Force pending redraws (the host's ``on_commit`` typically
        # repopulates the whole table) so ``bbox`` returns valid
        # coordinates synchronously below. Without this, a Tab right
        # after the value-format step landed on stale geometry and
        # ``start_edit_cell`` would silently bail.
        try:
            self._tree.update_idletasks()
        except Exception:
            pass

        if edit_next:
            self.start_edit_cell(nxt_item, nxt_col)
        elif self._highlight is not None:
            self._highlight.select_cell(nxt_item, nxt_col)

    def _commit_value(self) -> bool:
        """Save the in-flight edit. Returns ``False`` when validation
        failed (caller should not navigate away)."""
        if not self._entry or not self._editing:
            return False
        item_id, col_id = self._editing
        value = self._entry.get()
        if self._value_transform:
            value = self._value_transform(value)
        self._navigating = True
        try:
            self._destroy_entry()
            return bool(self._on_commit(item_id, col_id, value))
        finally:
            self._navigating = False

    def _destroy_entry(self) -> None:
        if self._entry:
            try:
                self._entry.unbind("<FocusOut>")
            except Exception:
                pass
            self._entry.destroy()
        self._entry = None
        self._editing = None

