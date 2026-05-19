"""
ChartsMaze AMOLED GUI

Layout (top pane, horizontal):
  Sectors  |  Industries  |  Detail chart (selected industry: rank+perf 1W/1M/3M)

Bottom pane (vertical PanedWindow sash):
  3-tab notebook: 1W | 1M | 3M  rank-vs-performance for ALL Leading+Improving industries

Usage:
    chartsmaze-gui
    python -m chartsmaze_mcp.gui
"""

from __future__ import annotations

import asyncio
import os
import threading
import traceback
import tkinter as tk
import webbrowser
from tkinter import ttk
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── optional matplotlib ────────────────────────────────────────────────────────
try:
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False

# ── AMOLED palette ─────────────────────────────────────────────────────────────
BG        = "#000000"
SURFACE   = "#0F0F0F"
CARD      = "#1A1A1A"
BORDER    = "#2A2A2A"
GRID_V    = "#1C1C1C"     # dim vertical grid lines
FG        = "#FFFFFF"
FG2       = "#666666"
ACCENT    = "#00CFFF"
GREEN     = "#00FF7F"
YELLOW    = "#FFD700"
ORANGE    = "#FF8C00"
RED       = "#FF3D3D"

_QUAD_FG = {
    "Leading":   GREEN,
    "Improving": YELLOW,
    "Weakening": ORANGE,
    "Lagging":   RED,
}

_FONT      = ("Segoe UI", 10)
_FONT_BOLD = ("Segoe UI", 10, "bold")
_FONT_SM   = ("Segoe UI", 9)
_FONT_H    = ("Segoe UI", 14, "bold")

# Bright-white tick labels; grids added explicitly per axes
_MPL_RC: dict = {
    "figure.facecolor":  BG,
    "axes.facecolor":    SURFACE,
    "axes.edgecolor":    BORDER,
    "axes.labelcolor":   FG2,
    "grid.color":        BORDER,
    "grid.linewidth":    0.5,
    "text.color":        FG2,
    "xtick.color":       FG,        # bright white
    "ytick.color":       FG,        # bright white
    "xtick.labelsize":   8,
    "ytick.labelsize":   8,
    "axes.titlesize":    11,
    "axes.titlecolor":   FG,
    "axes.titlepad":     10,
}

_PERIODS = [
    ("1W", lambda i: i.rank_1w, lambda i: i.performance_1w),
    ("1M", lambda i: i.rank_1m, lambda i: i.performance_1m),
    ("3M", lambda i: i.rank_3m, lambda i: i.performance_3m),
]

_SEC_SORT_KEYS: dict = {
    "Sector": lambda s: s.name.lower(),
    "Quad":   lambda s: s.quadrant.value if s.quadrant else "",
    "RS":     lambda s: s.rs_ratio    or 0.0,
    "Mom":    lambda s: s.rs_momentum or 0.0,
    "Score":  lambda s: s.rrg_score(),
}

_IND_SORT_KEYS: dict = {
    "Industry": lambda i: i.name.lower(),
    "Quad":     lambda i: i.quadrant.value if i.quadrant else "",
    "RS":       lambda i: i.rs_ratio    or 0.0,
    "Mom":      lambda i: i.rs_momentum or 0.0,
    "Trend":    lambda i: i.trend_score(),
    "R1W":      lambda i: i.rank_1w   if i.rank_1w  is not None else 9999,
    "R1M":      lambda i: i.rank_1m   if i.rank_1m  is not None else 9999,
    "R3M":      lambda i: i.rank_3m   if i.rank_3m  is not None else 9999,
    "P1W%":     lambda i: i.performance_1w  or 0.0,
    "P1M%":     lambda i: i.performance_1m  or 0.0,
    "P3M%":     lambda i: i.performance_3m  or 0.0,
    "52W Hi%":  lambda i: i.from_52w_high_pct or 0.0,
}

_SEC_COLS = ["Sector", "Quad", "RS", "Mom", "Score"]
_IND_COLS = ["Industry", "Quad", "RS", "Mom", "Trend",
             "R1W", "R1M", "R3M", "P1W%", "P1M%", "P3M%", "52W Hi%"]

_STK_SORT_KEYS: dict = {
    "Stock":    lambda s: s.ticker.lower(),
    "RS":       lambda s: s.rs_rating        or 0.0,
    "Industry": lambda s: (s.industry or "").lower(),
    "1M%":      lambda s: s.returns_1m       or 0.0,
    "3M%":      lambda s: s.returns_3m       or 0.0,
    "52W Hi%":  lambda s: s.from_52w_high_pct or 0.0,
    "Sector":   lambda s: (s.sector or "").lower(),
}
_STK_COLS = ["Stock", "RS", "Industry", "1M%", "3M%", "52W Hi%", "Sector"]
_QTR_COLS = ["Quarter", "EPS", "QoQ EPS", "YoY EPS", "Sales", "QoQ Sales", "YoY Sales", "OPM"]


# ── helpers ────────────────────────────────────────────────────────────────────

def _fmt(v: Optional[float], decimals: int = 1, suffix: str = "") -> str:
    return f"{v:.{decimals}f}{suffix}" if v is not None else "—"


def _value_color(text: str, key: str = "") -> str:
    """Return a colour code for a displayed value based on its key and content."""
    if text in ("—", "", "n/a"):
        return FG2
    if key == "name":
        return ACCENT
    if key == "quadrant":
        return _QUAD_FG.get(text, FG)
    if key in ("rs_ratio", "rs_momentum"):
        try:
            return GREEN if float(text) >= 100 else RED
        except ValueError:
            return FG
    if key in ("rank_1w", "rank_1m", "rank_3m", "stock_count"):
        return FG
    if key == "market_cap":
        try:
            float(text)
            return ACCENT
        except ValueError:
            return FG2
    if key == "from_52w_high_pct":
        try:
            v = float(text.rstrip("%"))
            return GREEN if v <= 5 else (YELLOW if v <= 15 else RED)
        except ValueError:
            return FG
    # Generic: try to interpret as a signed number / percentage
    try:
        v = float(text.rstrip("%"))
        return GREEN if v > 0 else (RED if v < 0 else FG)
    except ValueError:
        return FG


def _styled_tree(parent: tk.Widget, columns: list[str], widths: list[int]) -> ttk.Treeview:
    s = ttk.Style()
    s.theme_use("default")
    uid = f"A{id(parent)}.Treeview"
    s.configure(uid,
        background=SURFACE, foreground=FG, fieldbackground=SURFACE,
        rowheight=28, font=_FONT, borderwidth=0,
    )
    s.configure(f"{uid}.Heading",
        background=CARD, foreground=FG2, relief="flat", font=_FONT_SM,
    )
    s.map(uid,
        background=[("selected", BORDER)],
        foreground=[("selected", ACCENT)],
    )
    wrap = tk.Frame(parent, bg=BG)
    wrap.pack(fill="both", expand=True)
    tree = ttk.Treeview(wrap, columns=columns, show="headings", style=uid, selectmode="browse")
    for col, w in zip(columns, widths):
        anchor = "w" if col in ("Sector", "Industry") else "center"
        tree.heading(col, text=col)
        tree.column(col, width=w, minwidth=w, anchor=anchor, stretch=False)
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    for quad, color in _QUAD_FG.items():
        tree.tag_configure(quad, foreground=color)
    tree.tag_configure("dim", foreground=FG2)
    tree.tag_configure("all", foreground=ACCENT)
    return tree


def _style_axes(ax: "plt.Axes") -> None:
    ax.set_facecolor(SURFACE)
    for spine in ax.spines.values():
        spine.set_edgecolor(BORDER)
    ax.tick_params(colors=FG, labelsize=8)


def _add_grid(ax: "plt.Axes") -> None:
    ax.grid(axis="y", color=BORDER, linewidth=0.5, zorder=0)
    ax.grid(axis="x", color=GRID_V, linewidth=0.5, linestyle="--", alpha=0.6, zorder=0)


# ── rank-vs-performance chart (tab charts) ─────────────────────────────────────

def _draw_rank_perf_chart(fig: "plt.Figure", pool: list,
                          period: str, get_rank, get_perf) -> None:
    """Full-width rank-vs-performance for all Leading+Improving industries."""
    from .chartsmaze.models import RRGQuadrant

    plt.rcParams.update(_MPL_RC)
    fig.clear()
    fig.set_facecolor(BG)

    ax = fig.add_subplot(111)
    _style_axes(ax)

    valid = [
        (i, get_rank(i), get_perf(i))
        for i in pool
        if get_rank(i) is not None and get_perf(i) is not None
    ]
    valid.sort(key=lambda t: t[1])   # ascending rank: best=left, worst=right

    if not valid:
        ax.set_title(f"{period}  —  no data", color=FG2)
        _add_grid(ax)
        return

    xs     = list(range(len(valid)))
    ranks  = [t[1] for t in valid]
    perfs  = [t[2] for t in valid]
    colors = [GREEN if t[0].quadrant == RRGQuadrant.LEADING else YELLOW for t in valid]
    labels = [t[0].name[:18] for t in valid]

    # Left Y: rank — invert so rank 1 (best) is at the top
    ax.plot(xs, ranks, color=ACCENT, linewidth=2.0, marker="o", markersize=4, zorder=3)
    ax.invert_yaxis()
    ax.set_ylabel("Rank  (1 = best, top)", color=ACCENT, fontsize=9)
    ax.tick_params(axis="y", colors=FG, labelsize=8)

    # Right Y: performance %
    ax2 = ax.twinx()
    _style_axes(ax2)
    ax2.plot(xs, perfs, color=FG2, linewidth=1.2, linestyle="--", zorder=2, alpha=0.7)
    ax2.scatter(xs, perfs, color=colors, s=55, zorder=4)
    ax2.axhline(0, color=BORDER, linewidth=0.8, zorder=1)
    ax2.set_ylabel("Performance %", color=FG2, fontsize=9)
    ax2.tick_params(axis="y", colors=FG, labelsize=8)

    _add_grid(ax)

    ax.set_xticks(xs)
    ax.set_xticklabels(labels, rotation=50, ha="right", color=FG, fontsize=8)
    ax.tick_params(axis="x", colors=FG)
    ax.set_xlim(-0.5, len(xs) - 0.5)

    leading_n   = sum(1 for t in valid if t[0].quadrant == RRGQuadrant.LEADING)
    improving_n = len(valid) - leading_n
    ax.set_title(
        f"{period}  Rank vs Performance  ({leading_n} Leading  {improving_n} Improving)  —  MCW",
        color=FG,
    )
    fig.subplots_adjust(left=0.06, right=0.94, top=0.88, bottom=0.30)


# ── detail chart (selected industry: rank + perf across 1W/1M/3M) ─────────────

def _draw_detail_chart(fig: "plt.Figure", ind) -> None:
    """Compact chart: rank and performance for 1W / 1M / 3M for one industry."""
    plt.rcParams.update(_MPL_RC)
    fig.clear()
    fig.set_facecolor(BG)

    ax = fig.add_subplot(111)
    _style_axes(ax)

    periods = ["3M", "1M", "1W"]
    ranks   = [ind.rank_3m,         ind.rank_1m,         ind.rank_1w]
    perfs   = [ind.performance_3m,  ind.performance_1m,  ind.performance_1w]
    xs      = [0, 1, 2]

    # Left Y: rank
    valid_r = [(x, r) for x, r in zip(xs, ranks) if r is not None]
    if valid_r:
        ax.plot([v[0] for v in valid_r], [v[1] for v in valid_r],
                color=ACCENT, linewidth=2.5, marker="o", markersize=9, zorder=3)
        for x, r in valid_r:
            ax.annotate(str(int(r)), (x, r),
                        xytext=(0, 10), textcoords="offset points",
                        ha="center", color=FG, fontsize=9, fontweight="bold")

    ax.invert_yaxis()
    ax.set_ylabel("Rank  (1 = best, top)", color=ACCENT, fontsize=9)
    ax.tick_params(axis="y", colors=FG, labelsize=9)

    # Right Y: performance
    ax2 = ax.twinx()
    _style_axes(ax2)
    quad = ind.quadrant.value if ind.quadrant else ""
    dot_color = GREEN if quad == "Leading" else YELLOW
    valid_p = [(x, p) for x, p in zip(xs, perfs) if p is not None]
    if valid_p:
        ax2.plot([v[0] for v in valid_p], [v[1] for v in valid_p],
                 color=dot_color, linewidth=2.0, marker="s",
                 markersize=8, linestyle="--", zorder=4)
        for x, p in valid_p:
            ax2.annotate(f"{p:.1f}%", (x, p),
                         xytext=(0, -16), textcoords="offset points",
                         ha="center", color=dot_color, fontsize=9)
    ax2.axhline(0, color=BORDER, linewidth=0.8)
    ax2.set_ylabel("Performance %", color=FG2, fontsize=9)
    ax2.tick_params(axis="y", colors=FG, labelsize=9)

    _add_grid(ax)

    ax.set_xticks(xs)
    ax.set_xticklabels(periods, color=FG, fontsize=11, fontweight="bold")
    ax.tick_params(axis="x", colors=FG)
    ax.set_xlim(-0.3, 2.3)

    rs_s  = f"{ind.rs_ratio:.1f}"    if ind.rs_ratio    is not None else "—"
    mom_s = f"{ind.rs_momentum:.1f}" if ind.rs_momentum is not None else "—"
    hi_s  = (f"  ·  52W Hi {ind.from_52w_high_pct:.1f}%"
             if ind.from_52w_high_pct is not None else "")
    ax.set_title(
        f"{ind.name}  ·  {quad or '—'}  ·  RS {rs_s}  ·  Mom {mom_s}{hi_s}",
        color=FG, fontsize=10,
    )
    fig.subplots_adjust(left=0.12, right=0.88, top=0.84, bottom=0.12)


# ── single-chart popup (⤢ Expand) ─────────────────────────────────────────────

class SingleChartWindow(tk.Toplevel):
    def __init__(self, parent: tk.Widget, pool: list,
                 period: str, get_rank, get_perf) -> None:
        super().__init__(parent)
        self.title(f"ChartsMaze — {period}")
        self.configure(bg=BG)
        self.geometry("1280x720")
        try:
            self.state("zoomed")
        except tk.TclError:
            self.attributes("-zoomed", True)

        if not _HAS_MPL:
            tk.Label(self, text="pip install matplotlib", bg=BG, fg=RED, font=_FONT).pack(expand=True)
            return

        fig = plt.Figure(facecolor=BG)
        _draw_rank_perf_chart(fig, pool, period, get_rank, get_perf)

        chart_frame = tk.Frame(self, bg=BG)
        chart_frame.pack(fill="both", expand=True)
        canvas = FigureCanvasTkAgg(fig, master=chart_frame)
        canvas.get_tk_widget().configure(bg=BG, highlightthickness=0)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw()

        _resize_timer = [None]

        def _on_resize(e, f=fig, c=canvas, fr=chart_frame):
            if _resize_timer[0]:
                self.after_cancel(_resize_timer[0])
            def _do():
                w, h = fr.winfo_width(), fr.winfo_height()
                if w > 20 and h > 20:
                    f.set_size_inches(w / f.dpi, h / f.dpi, forward=False)
                    c.draw()
            _resize_timer[0] = self.after(80, _do)

        chart_frame.bind("<Configure>", _on_resize)


# ── main window ────────────────────────────────────────────────────────────────

class ChartsMazeGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ChartsMaze")
        self.configure(bg=BG)
        self.geometry("1400x860")

        self._sectors:              list = []
        self._industries:           dict[str, list] = {}
        self._pool:                 list = []
        self._displayed_inds:       list = []
        self._stocks_by_industry:   dict[str, list] = {}
        self._quarterly:            dict[str, list] = {}
        self._displayed_stocks:     list = []
        self._chart_figs:           list = []
        self._chart_canvases:       list = []
        self._chart_frames:         list = []
        self._detail_fig:           Optional["plt.Figure"] = None
        self._detail_canvas         = None
        self._detail_frame:         Optional[tk.Frame] = None
        self._sash_placed           = False
        self._resize_timer          = None
        self._sec_sort:             tuple = ("Score", True)
        self._ind_sort:             tuple = ("Trend", True)
        self._stk_sort:             tuple = ("RS",    True)
        # Stocks-tab widget handles (set in _build_stocks_tab)
        self._stk_sec_tree          = None
        self._stk_ind_tree          = None
        self._stk_tree              = None
        self._qtr_tree              = None
        self._ind_fund_vars:        dict  = {}
        self._stk_metric_vars:      dict  = {}
        self._stk_vpane             = None
        self._stk_sash_placed       = False
        self._growth_fig            = None
        self._growth_canvas         = None
        self._growth_chart_frame    = None
        self._qtr_web_cache: set    = set()   # tickers already fetched from web

        self._build()

        # Maximise then set sash once geometry is known
        try:
            self.state("zoomed")
        except tk.TclError:
            self.attributes("-zoomed", True)
        self.update()
        self.update_idletasks()
        self.bind("<Configure>", self._on_win_configure)
        self.after(200, self._fetch)

    def _on_win_configure(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        h = self.winfo_height()
        if not self._sash_placed:
            if h > 300:
                self._sash_placed = True
                self._paned.sash_place(0, 0, int(h * 0.40))
                self.after(120, self._sync_chart_sizes)  # sync after sash settles
        else:
            # debounced sync on every window resize
            if self._resize_timer:
                self.after_cancel(self._resize_timer)
            self._resize_timer = self.after(80, self._sync_chart_sizes)
        # Stocks-tab vertical sash: stock list 55%, fundamentals 45%
        if not self._stk_sash_placed and self._stk_vpane and h > 300:
            self._stk_sash_placed = True
            self.after(150, lambda: self._stk_vpane.sash_place(
                0, 0, int(self._stk_vpane.winfo_height() * 0.55)
            ))

    # ── layout ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # ── header row (title + nav buttons + status + refresh) ──────────────
        hdr = tk.Frame(self, bg=BG, pady=8)
        hdr.pack(fill="x", padx=16)

        tk.Label(hdr, text="ChartsMaze", bg=BG, fg=ACCENT, font=_FONT_H).pack(side="left")

        # Nav buttons sit right next to the title in the same row
        nav = tk.Frame(hdr, bg=BG)
        nav.pack(side="left", padx=16)
        self._nav_market = tk.Button(
            nav, text="Market",
            bg=BORDER, fg=ACCENT, activebackground=CARD, activeforeground=ACCENT,
            relief="flat", font=_FONT_BOLD, padx=14, pady=4,
            cursor="hand2", command=self._show_market,
        )
        self._nav_market.pack(side="left", padx=(0, 2))
        self._nav_stocks = tk.Button(
            nav, text="Stocks",
            bg=CARD, fg=FG2, activebackground=CARD, activeforeground=ACCENT,
            relief="flat", font=_FONT_BOLD, padx=14, pady=4,
            cursor="hand2", command=self._show_stocks,
        )
        self._nav_stocks.pack(side="left")

        self._status_var = tk.StringVar(value="Loading…")
        tk.Label(hdr, textvariable=self._status_var, bg=BG, fg=FG2, font=_FONT_SM).pack(
            side="left", padx=14,
        )
        self._btn = tk.Button(
            hdr, text="⟳  Refresh", bg=CARD, fg=ACCENT,
            activebackground=BORDER, activeforeground=ACCENT,
            relief="flat", font=_FONT_BOLD, padx=12, pady=4,
            cursor="hand2", command=self._fetch,
        )
        self._btn.pack(side="right")
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # ── content area — full height below the header ───────────────────────
        self._content = tk.Frame(self, bg=BG)
        self._content.pack(fill="both", expand=True, padx=10, pady=(6, 0))

        self._market_frame = tk.Frame(self._content, bg=BG)
        self._stocks_frame = tk.Frame(self._content, bg=BG)

        self._build_market_tab(self._market_frame)
        self._build_stocks_tab(self._stocks_frame)

        # status bar
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")
        self._score_bar = tk.Label(
            self, text="", bg=BG, fg=FG2, font=_FONT_SM, anchor="w", pady=4,
        )
        self._score_bar.pack(fill="x", padx=16)

        # Show Market by default
        self._market_frame.pack(fill="both", expand=True)

    def _show_market(self) -> None:
        self._stocks_frame.pack_forget()
        self._market_frame.pack(fill="both", expand=True)
        self._nav_market.config(bg=BORDER, fg=ACCENT)
        self._nav_stocks.config(bg=CARD,   fg=FG2)

    def _show_stocks(self) -> None:
        self._market_frame.pack_forget()
        self._stocks_frame.pack(fill="both", expand=True)
        self._nav_stocks.config(bg=BORDER, fg=ACCENT)
        self._nav_market.config(bg=CARD,   fg=FG2)

    def _build_market_tab(self, parent: tk.Frame) -> None:
        # vertical PanedWindow
        self._paned = tk.PanedWindow(
            parent, orient="vertical", bg=BG,
            sashwidth=6, sashrelief="flat", sashpad=0,
        )
        self._paned.pack(fill="both", expand=True)

        # ── top pane: 3 columns ──
        top = tk.Frame(self._paned, bg=BG)
        self._paned.add(top, stretch="always", minsize=160)

        self._build_sector_panel(top)
        tk.Frame(top, bg=BORDER, width=1).pack(side="left", fill="y", padx=4)
        self._build_industry_panel(top)
        tk.Frame(top, bg=BORDER, width=1).pack(side="left", fill="y", padx=4)
        self._build_detail_panel(top)

        # ── bottom pane: 3-tab charts ──
        bot = tk.Frame(self._paned, bg=BG)
        self._paned.add(bot, stretch="always", minsize=200)
        self._build_charts(bot)

    def _build_stocks_tab(self, parent: tk.Frame) -> None:
        hpane = tk.PanedWindow(parent, orient="horizontal", bg=BG,
                               sashwidth=5, sashrelief="flat", sashpad=0)
        hpane.pack(fill="both", expand=True)

        # Left: sector list
        sf = tk.Frame(hpane, bg=BG, width=200)
        hpane.add(sf, minsize=140, stretch="never")
        tk.Label(sf, text="SECTORS", bg=BG, fg=FG2, font=_FONT_SM).pack(
            anchor="w", padx=6, pady=(4, 2))
        self._stk_sec_tree = _styled_tree(sf, ["Sector", "Quad"], [130, 72])
        self._stk_sec_tree.bind("<<TreeviewSelect>>", self._on_stk_sector_select)

        # Middle: vertical split — industry list (top) + sector/industry fundamental (bottom)
        mid_vpane = tk.PanedWindow(hpane, orient="vertical", bg=BG,
                                   sashwidth=5, sashrelief="flat", width=240)
        hpane.add(mid_vpane, minsize=170, stretch="never")

        inf = tk.Frame(mid_vpane, bg=BG)
        mid_vpane.add(inf, stretch="always", minsize=120)
        tk.Label(inf, text="INDUSTRIES", bg=BG, fg=FG2, font=_FONT_SM).pack(
            anchor="w", padx=6, pady=(4, 2))
        self._stk_ind_tree = _styled_tree(inf, ["Industry", "Trend"], [160, 66])
        self._stk_ind_tree.bind("<<TreeviewSelect>>", self._on_stk_industry_select)

        fund_mid = tk.Frame(mid_vpane, bg=BG)
        mid_vpane.add(fund_mid, stretch="always", minsize=120)
        self._build_ind_fund_panel(fund_mid)

        # Right: vertical PanedWindow
        #   Top:    horizontal split — stock list (left) | quarterly table + cards (right)
        #   Bottom: growth charts — full width, spacious
        rf = tk.Frame(hpane, bg=BG)
        hpane.add(rf, stretch="always")
        self._stk_vpane = tk.PanedWindow(rf, orient="vertical", bg=BG,
                                          sashwidth=5, sashrelief="flat")
        self._stk_vpane.pack(fill="both", expand=True)

        # Top: stocks | fundamentals side-by-side
        top_hpane = tk.PanedWindow(self._stk_vpane, orient="horizontal", bg=BG,
                                   sashwidth=5, sashrelief="flat")
        self._stk_vpane.add(top_hpane, stretch="always", minsize=180)

        sl = tk.Frame(top_hpane, bg=BG)
        top_hpane.add(sl, stretch="always", minsize=300)
        self._build_stock_list(sl)

        qf = tk.Frame(top_hpane, bg=BG)
        top_hpane.add(qf, stretch="never", minsize=500)
        self._build_stock_qtr_cards(qf)

        # Bottom: growth charts, takes the rest of the vertical space
        gf = tk.Frame(self._stk_vpane, bg=BG)
        self._stk_vpane.add(gf, stretch="always", minsize=200)
        self._build_growth_panel(gf)

    def _build_stock_list(self, parent: tk.Frame) -> None:
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill="x", padx=6, pady=(4, 0))
        tk.Label(bar, text="STOCKS", bg=BG, fg=FG2, font=_FONT_SM).pack(side="left")
        tk.Button(
            bar, text="⤢  Open Chart",
            bg=CARD, fg=ACCENT, activebackground=BORDER, activeforeground=ACCENT,
            relief="flat", font=_FONT_SM, padx=10, pady=3, cursor="hand2",
            command=self._open_tv_chart,
        ).pack(side="right")
        self._stk_tree = _styled_tree(
            parent, _STK_COLS,
            widths=[105, 44, 210, 60, 60, 72, 140],
        )
        self._stk_tree.tag_configure("rs_strong", foreground=GREEN)
        self._stk_tree.tag_configure("rs_good",   foreground="#80EE80")
        self._stk_tree.tag_configure("rs_mid",     foreground=YELLOW)
        self._stk_tree.tag_configure("rs_weak",    foreground=ORANGE)
        self._stk_tree.tag_configure("rs_low",     foreground=FG2)
        for col in _STK_COLS:
            self._stk_tree.heading(col, command=lambda c=col: self._sort_stock(c))
        self._stk_tree.bind("<<TreeviewSelect>>", self._on_stock_select)
        self._stk_tree.bind("<Double-1>", self._on_stock_dbl)
        # heading indicator for default sort
        self._stk_tree.heading("RS", text="RS ▼")

    def _build_ind_fund_panel(self, parent: tk.Frame) -> None:
        tk.Label(parent, text="SECTOR / INDUSTRY FUNDAMENTAL",
                 bg=BG, fg=FG2, font=_FONT_SM).pack(anchor="w", padx=6, pady=(6, 4))
        scroll_wrap = tk.Frame(parent, bg=SURFACE)
        scroll_wrap.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        canvas = tk.Canvas(scroll_wrap, bg=SURFACE, highlightthickness=0)
        sb = ttk.Scrollbar(scroll_wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=SURFACE)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        # Two separate bindings: inner resize → scrollregion; canvas resize → frame width.
        # (winfo_width() is unreliable during init — use the event's width instead.)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win, width=e.width))

        fields = [
            ("Name",         "name"),
            ("Sector",       "sector"),
            ("Quadrant",     "quadrant"),
            ("RS Ratio",     "rs_ratio"),
            ("RS Momentum",  "rs_momentum"),
            ("Perf 1W%",     "perf_1w"),
            ("Perf 1M%",     "perf_1m"),
            ("Perf 3M%",     "perf_3m"),
            ("Rank 1W",      "rank_1w"),
            ("Rank 1M",      "rank_1m"),
            ("Rank 3M",      "rank_3m"),
            ("Market Cap",   "market_cap"),
            ("Stocks",       "stock_count"),
            ("52W Hi%",      "from_52w_high_pct"),
        ]
        self._ind_fund_vars = {}
        for label, key in fields:
            row = tk.Frame(inner, bg=SURFACE)
            row.pack(fill="x", padx=10, pady=2)
            tk.Label(row, text=label + ":", bg=SURFACE, fg=FG2,
                     font=_FONT_SM, width=14, anchor="w").pack(side="left")
            var = tk.StringVar(value="—")
            val_lbl = tk.Label(row, textvariable=var, bg=SURFACE, fg=FG2,
                               font=_FONT_SM, anchor="w")
            val_lbl.pack(side="left")
            self._ind_fund_vars[key] = (var, val_lbl)

    def _build_stock_qtr_cards(self, parent: tk.Frame) -> None:
        """Quarterly earnings table + metric cards — sits top-right beside the stock list."""
        tk.Label(parent, text="QUARTERLY FUNDAMENTALS",
                 bg=BG, fg=ACCENT, font=_FONT_BOLD).pack(anchor="w", padx=8, pady=(6, 4))

        # Quarterly treeview (fixed height = 4 rows)
        qtree_wrap = tk.Frame(parent, bg=BG)
        qtree_wrap.pack(fill="x", padx=6, pady=(0, 6))
        uid = f"QTR{id(qtree_wrap)}.Treeview"
        s = ttk.Style()
        s.configure(uid, background=CARD, foreground=FG, fieldbackground=CARD,
                    rowheight=26, font=_FONT_SM, borderwidth=0)
        s.configure(f"{uid}.Heading", background=SURFACE, foreground=ACCENT,
                    relief="flat", font=_FONT_SM)
        s.map(uid, background=[("selected", BORDER)], foreground=[("selected", ACCENT)])
        qwidths = [72, 58, 68, 68, 62, 75, 75, 60]
        self._qtr_tree = ttk.Treeview(
            qtree_wrap, columns=_QTR_COLS, show="headings",
            style=uid, height=4, selectmode="none",
        )
        for col, w in zip(_QTR_COLS, qwidths):
            self._qtr_tree.heading(col, text=col)
            self._qtr_tree.column(col, width=w, minwidth=w, anchor="center", stretch=True)
        self._qtr_tree.tag_configure("q_pos",  foreground=GREEN)
        self._qtr_tree.tag_configure("q_neg",  foreground=RED)
        self._qtr_tree.tag_configure("q_mix",  foreground=YELLOW)
        self._qtr_tree.pack(fill="x")

        # Metric cards — 2 rows × 2 cols for a compact square layout
        cards = tk.Frame(parent, bg=BG)
        cards.pack(fill="x", padx=6, pady=6)
        self._stk_metric_vars = {}
        metrics = [
            ("Market Cap (Cr)",  "market_cap"),
            ("% from 52W High",  "from_52w_high_pct"),
            ("1M Returns%",      "returns_1m"),
            ("3M Returns%",      "returns_3m"),
        ]
        for i, (label, key) in enumerate(metrics):
            row_i, col_i = divmod(i, 2)
            card = tk.Frame(cards, bg=CARD, padx=10, pady=8)
            card.grid(row=row_i, column=col_i, sticky="nsew", padx=3, pady=3)
            cards.columnconfigure(col_i, weight=1)
            tk.Label(card, text=label, bg=CARD, fg=FG2, font=_FONT_SM).pack()
            var = tk.StringVar(value="—")
            val_lbl = tk.Label(card, textvariable=var, bg=CARD, fg=FG2, font=_FONT_BOLD)
            val_lbl.pack()
            self._stk_metric_vars[key] = (var, val_lbl)

    def _build_growth_panel(self, parent: tk.Frame) -> None:
        """2×2 growth-rate bar charts — occupies the full bottom-right area."""
        if not _HAS_MPL:
            tk.Label(parent, text="pip install matplotlib",
                     bg=BG, fg=RED, font=_FONT).pack(expand=True)
            return
        self._growth_chart_frame = tk.Frame(parent, bg=BG)
        self._growth_chart_frame.pack(fill="both", expand=True, padx=4, pady=4)
        plt.rcParams.update(_MPL_RC)
        self._growth_fig, axes = plt.subplots(2, 2, facecolor=BG)
        self._growth_fig.subplots_adjust(hspace=0.50, wspace=0.28,
                                          left=0.07, right=0.98,
                                          top=0.90, bottom=0.14)
        titles = ["QoQ EPS %", "YoY EPS %", "QoQ Sales %", "YoY Sales %"]
        for ax, title in zip(axes.flat, titles):
            _style_axes(ax)
            ax.set_title(title, fontsize=9, color=FG2, pad=5)
            ax.set_xticks([]); ax.set_yticks([])
            ax.text(0.5, 0.5, "—", transform=ax.transAxes,
                    ha="center", va="center", color=FG2, fontsize=12)
        self._growth_canvas = FigureCanvasTkAgg(self._growth_fig,
                                                 master=self._growth_chart_frame)
        self._growth_canvas.get_tk_widget().configure(bg=BG, highlightthickness=0)
        self._growth_canvas.get_tk_widget().pack(fill="both", expand=True)
        self._growth_canvas.draw()

    def _draw_growth_charts(self, qtrs: list) -> None:
        """Redraw the 2×2 growth bar charts for the selected stock's quarterly data."""
        if not _HAS_MPL or self._growth_fig is None:
            return
        axes = self._growth_fig.axes
        if len(axes) < 4:
            return
        datasets = [
            (axes[0], "QoQ EPS %",   [q.qoq_eps   for q in qtrs]),
            (axes[1], "YoY EPS %",   [q.yoy_eps   for q in qtrs]),
            (axes[2], "QoQ Sales %", [q.qoq_sales for q in qtrs]),
            (axes[3], "YoY Sales %", [q.yoy_sales for q in qtrs]),
        ]
        # Quarters ordered oldest→newest (left→right) for the charts
        qtrs_ordered = list(reversed(qtrs))
        labels = [q.quarter for q in qtrs_ordered]
        for ax, title, _ in datasets:
            ax.clear()
            _style_axes(ax)
            ax.set_title(title, fontsize=8, color=FG2, pad=4)
        for ax, title, raw_vals in datasets:
            vals_ordered = list(reversed(raw_vals))
            has_data = any(v is not None for v in vals_ordered)
            if not has_data:
                ax.set_xticks([]); ax.set_yticks([])
                ax.text(0.5, 0.5, "—", transform=ax.transAxes,
                        ha="center", va="center", color=FG2, fontsize=11)
                continue
            x = range(len(labels))
            colors = [GREEN if (v is not None and v > 0) else RED
                      for v in vals_ordered]
            heights = [v if v is not None else 0.0 for v in vals_ordered]
            ax.bar(x, heights, color=colors, width=0.6, zorder=2)
            ax.axhline(0, color=BORDER, linewidth=0.7, zorder=1)
            ax.set_xticks(list(x))
            ax.set_xticklabels(labels, fontsize=7, rotation=30, ha="right", color=FG)
            ax.tick_params(axis="y", labelsize=7, colors=FG)
            ax.yaxis.grid(True, color=GRID_V, linewidth=0.4, zorder=0)
            ax.set_axisbelow(True)
        self._growth_canvas.draw_idle()

    def _build_sector_panel(self, parent: tk.Widget) -> None:
        pane = tk.Frame(parent, bg=BG, width=390)
        pane.pack(side="left", fill="y")
        pane.pack_propagate(False)
        tk.Label(pane, text="SECTORS", bg=BG, fg=FG2, font=_FONT_SM).pack(anchor="w", pady=(0, 4))
        self._sec_tree = _styled_tree(
            pane,
            columns=_SEC_COLS,
            widths= [160, 82, 52, 52, 60],
        )
        for col in _SEC_COLS:
            self._sec_tree.heading(col, command=lambda c=col: self._sort_sector(c))
        self._sec_tree.bind("<<TreeviewSelect>>", self._on_sector_select)

    def _build_industry_panel(self, parent: tk.Widget) -> None:
        pane = tk.Frame(parent, bg=BG, width=820)
        pane.pack(side="left", fill="y")
        pane.pack_propagate(False)
        tk.Label(pane, text="INDUSTRIES", bg=BG, fg=FG2, font=_FONT_SM).pack(anchor="w", pady=(0, 4))
        self._ind_tree = _styled_tree(
            pane,
            columns=_IND_COLS,
            widths= [180, 82, 52, 52, 58, 42, 42, 42, 58, 58, 58, 72],
        )
        for col in _IND_COLS:
            self._ind_tree.heading(col, command=lambda c=col: self._sort_industry(c))
        self._ind_tree.bind("<<TreeviewSelect>>", self._on_industry_select)

    def _build_detail_panel(self, parent: tk.Widget) -> None:
        pane = tk.Frame(parent, bg=BG)
        pane.pack(side="left", fill="both", expand=True)
        tk.Label(pane, text="INDUSTRY DETAIL", bg=BG, fg=FG2, font=_FONT_SM).pack(
            anchor="w", pady=(0, 4),
        )

        if not _HAS_MPL:
            tk.Label(pane, text="pip install matplotlib", bg=BG, fg=RED, font=_FONT).pack(expand=True)
            return

        # chart_frame is the resize anchor — canvas fills it, we query its winfo size
        chart_frame = tk.Frame(pane, bg=BG)
        chart_frame.pack(fill="both", expand=True)

        fig = plt.Figure(facecolor=BG)
        plt.rcParams.update(_MPL_RC)
        ax = fig.add_subplot(111)
        _style_axes(ax)
        ax.text(0.5, 0.5, "Select an industry", transform=ax.transAxes,
                ha="center", va="center", color=FG2, fontsize=13)
        ax.set_xticks([]); ax.set_yticks([])

        canvas = FigureCanvasTkAgg(fig, master=chart_frame)
        canvas.get_tk_widget().configure(bg=BG, highlightthickness=0)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw()

        self._detail_fig    = fig
        self._detail_canvas = canvas
        self._detail_frame  = chart_frame

    def _build_charts(self, parent: tk.Widget) -> None:
        lbl = tk.Frame(parent, bg=BG)
        lbl.pack(fill="x")
        tk.Label(lbl, text="RANK vs PERFORMANCE  —  Leading & Improving  (MCW)",
                 bg=BG, fg=FG2, font=_FONT_SM).pack(side="left", pady=(0, 4))
        for quad, color in [("Leading", GREEN), ("Improving", YELLOW)]:
            tk.Label(lbl, text="●", bg=BG, fg=color, font=_FONT_SM).pack(side="right", padx=(0, 2))
            tk.Label(lbl, text=quad, bg=BG, fg=FG2, font=_FONT_SM).pack(side="right")

        if not _HAS_MPL:
            tk.Label(parent, text="pip install matplotlib", bg=BG, fg=RED, font=_FONT).pack(expand=True)
            return

        s = ttk.Style()
        s.theme_use("default")
        s.configure("Amoled.TNotebook", background=BG, borderwidth=0)
        s.configure("Amoled.TNotebook.Tab",
            background=CARD, foreground=FG2, padding=[18, 6], font=_FONT_BOLD,
        )
        s.map("Amoled.TNotebook.Tab",
            background=[("selected", BORDER)],
            foreground=[("selected", ACCENT)],
        )

        nb = ttk.Notebook(parent, style="Amoled.TNotebook")
        nb.pack(fill="both", expand=True)

        self._chart_figs.clear()
        self._chart_canvases.clear()
        self._chart_frames.clear()

        for period, get_rank, get_perf in _PERIODS:
            tab = tk.Frame(nb, bg=BG)
            nb.add(tab, text=f"   {period}   ")

            bar = tk.Frame(tab, bg=BG, pady=3)
            bar.pack(fill="x", padx=10)
            tk.Button(
                bar, text="⤢  Expand",
                bg=CARD, fg=ACCENT, activebackground=BORDER, activeforeground=ACCENT,
                relief="flat", font=_FONT_SM, padx=10, pady=3, cursor="hand2",
                command=lambda p=period, gr=get_rank, gp=get_perf: self._expand(p, gr, gp),
            ).pack(side="left")

            # chart_frame is the resize anchor
            chart_frame = tk.Frame(tab, bg=BG)
            chart_frame.pack(fill="both", expand=True)

            fig = plt.Figure(facecolor=BG)
            canvas = FigureCanvasTkAgg(fig, master=chart_frame)
            canvas.get_tk_widget().configure(bg=BG, highlightthickness=0)
            canvas.get_tk_widget().pack(fill="both", expand=True)

            self._chart_figs.append(fig)
            self._chart_canvases.append(canvas)
            self._chart_frames.append(chart_frame)

    # ── data loading ───────────────────────────────────────────────────────────

    def _fetch(self) -> None:
        self._btn.config(state="disabled")
        self._status_var.set("Loading data — this may take ~30 s…")
        self._sectors = []; self._industries = {}; self._pool = []; self._displayed_inds = []
        self._stocks_by_industry = {}; self._quarterly = {}; self._displayed_stocks = []
        self._qtr_web_cache.clear()
        self._sec_tree.delete(*self._sec_tree.get_children())
        self._ind_tree.delete(*self._ind_tree.get_children())
        if self._stk_sec_tree:
            self._stk_sec_tree.delete(*self._stk_sec_tree.get_children())
        if self._stk_ind_tree:
            self._stk_ind_tree.delete(*self._stk_ind_tree.get_children())
        if self._stk_tree:
            self._stk_tree.delete(*self._stk_tree.get_children())
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        from .chartsmaze.client import ChartsMazeClient

        async def _run() -> tuple:
            async with ChartsMazeClient(session_cookie=os.environ.get("CHARTSMAZE_SESSION")) as cm:
                sectors  = await cm.get_sector_analysis()
                all_inds = {s.name: await cm.get_industry_analysis(s.name) for s in sectors}
                stocks   = await cm.get_stocks_grouped_by_industry()
                qtrs     = await cm.get_all_quarterly_data()
                return sectors, all_inds, stocks, qtrs

        try:
            sectors, all_inds, stocks, qtrs = asyncio.run(_run())
            self.after(0, self._on_data,
                       sorted(sectors, key=lambda s: s.rrg_score(), reverse=True),
                       all_inds, stocks, qtrs)
        except Exception as exc:
            tb = traceback.format_exc()
            print(tb, flush=True)   # visible in the terminal window
            self.after(0, self._on_error, str(exc))

    # ── UI updates ─────────────────────────────────────────────────────────────

    def _on_data(self, sectors: list, all_inds: dict,
                 stocks: dict, qtrs: dict) -> None:
        self._sectors              = sectors
        self._industries           = all_inds
        self._stocks_by_industry   = stocks
        self._quarterly            = qtrs

        self._populate_sector_tree(sectors)

        leading = sum(1 for s in sectors if s.quadrant and s.quadrant.value == "Leading")
        self._status_var.set(f"{len(sectors)} sectors  •  {leading} Leading")
        self._btn.config(state="normal")

        self._pool = self._collect_pool()
        self._redraw_tab_charts()
        self.after(150, self._sync_chart_sizes)

        # Populate Stocks-tab sector/industry lists
        self._populate_stk_sector_tree(sectors)

        # Select first real sector (index 1, after the "ALL" row)
        ch = self._sec_tree.get_children()
        target = ch[1] if len(ch) > 1 else (ch[0] if ch else None)
        if target:
            self._sec_tree.selection_set(target)
            self._sec_tree.focus(target)

    def _on_error(self, msg: str) -> None:
        self._status_var.set(f"Error: {msg[:120]}")
        self._btn.config(state="normal")

    def _on_sector_select(self, _event: tk.Event) -> None:
        sel = self._sec_tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid == "__ALL__":
            seen: set[str] = set()
            inds: list = []
            for inds_list in self._industries.values():
                for i in inds_list:
                    if i.name not in seen:
                        seen.add(i.name)
                        inds.append(i)
            self._populate_ind_tree(inds)
            self._score_bar.config(
                text=f"All sectors  •  {len(self._displayed_inds)} industries"
            )
        else:
            sector_name = iid[4:]  # strip leading "sec_"
            sector = next((s for s in self._sectors if s.name == sector_name), None)
            if not sector:
                return
            inds = self._industries.get(sector.name, [])
            self._populate_ind_tree(inds)
            self._score_bar.config(
                text=(f"{sector.name}  •  RRG score {_fmt(sector.rrg_score())}  "
                      f"•  ind avg trend {_fmt(sector.industry_avg_trend, 0)}  "
                      f"•  {len(inds)} industries")
            )

    def _on_industry_select(self, _event: tk.Event) -> None:
        sel = self._ind_tree.selection()
        if not sel:
            return
        idx = self._ind_tree.index(sel[0])
        if idx >= len(self._displayed_inds):
            return
        ind = self._displayed_inds[idx]

        self._score_bar.config(
            text=(
                f"{ind.name}  •  trend {_fmt(ind.trend_score(), 0)}  "
                f"•  RS {_fmt(ind.rs_ratio)} / mom {_fmt(ind.rs_momentum)}  "
                f"•  ranks 1W:{ind.rank_1w or '—'} 1M:{ind.rank_1m or '—'} 3M:{ind.rank_3m or '—'}  "
                f"•  perf 1W:{_fmt(ind.performance_1w, suffix='%')} "
                f"1M:{_fmt(ind.performance_1m, suffix='%')} "
                f"3M:{_fmt(ind.performance_3m, suffix='%')}"
            )
        )

        if _HAS_MPL and self._detail_fig is not None and self._detail_canvas is not None:
            _draw_detail_chart(self._detail_fig, ind)
            self._detail_canvas.draw()
            self.after(80, self._sync_chart_sizes)

    # ── stocks tab helpers ─────────────────────────────────────────────────────

    def _populate_stk_sector_tree(self, sectors: list) -> None:
        if not self._stk_sec_tree:
            return
        self._stk_sec_tree.delete(*self._stk_sec_tree.get_children())
        self._stk_sec_tree.insert("", "end", iid="__ALL__", tags=("all",),
                                  values=("▶  ALL", "—"))
        for s in sectors:
            quad = s.quadrant.value if s.quadrant else "—"
            self._stk_sec_tree.insert("", "end", iid=f"ss_{s.name}",
                tags=(quad if quad in _QUAD_FG else "dim",),
                values=(s.name, quad))
        ch = self._stk_sec_tree.get_children()
        if len(ch) > 1:
            self._stk_sec_tree.selection_set(ch[1])

    def _on_stk_sector_select(self, _e: tk.Event) -> None:
        sel = self._stk_sec_tree.selection() if self._stk_sec_tree else ()
        if not sel:
            return
        iid = sel[0]
        if self._stk_ind_tree:
            self._stk_ind_tree.delete(*self._stk_ind_tree.get_children())
        if iid == "__ALL__":
            inds: list = []
            seen: set[str] = set()
            for lst in self._industries.values():
                for i in lst:
                    if i.name not in seen:
                        seen.add(i.name); inds.append(i)
        else:
            sector_name = iid[3:]
            inds = self._industries.get(sector_name, [])
            # Show sector fundamentals when a sector (not ALL) is selected
            sec_obj = next((s for s in self._sectors if s.name == sector_name), None)
            if sec_obj:
                self._update_ind_fundamentals(sec_obj)
        if self._stk_ind_tree:
            for i in sorted(inds, key=lambda x: x.trend_score(), reverse=True):
                quad = i.quadrant.value if i.quadrant else "—"
                self._stk_ind_tree.insert("", "end", iid=f"si_{i.name}",
                    tags=(quad if quad in _QUAD_FG else "dim",),
                    values=(i.name, _fmt(i.trend_score(), 0)))
            ch2 = self._stk_ind_tree.get_children()
            if ch2:
                self._stk_ind_tree.selection_set(ch2[0])

    def _on_stk_industry_select(self, _e: tk.Event) -> None:
        sel = self._stk_ind_tree.selection() if self._stk_ind_tree else ()
        if not sel:
            return
        iid = sel[0]
        ind_name = iid[3:]
        # Gather stocks for this industry
        stocks = self._stocks_by_industry.get(ind_name, [])
        self._populate_stock_list(stocks)
        # Update industry fundamentals — search all sectors for the industry object
        ind_obj = None
        for inds in self._industries.values():
            ind_obj = next((i for i in inds if i.name == ind_name), None)
            if ind_obj:
                break
        if ind_obj:
            self._update_ind_fundamentals(ind_obj)

    def _populate_stock_list(self, stocks: list) -> None:
        if not self._stk_tree:
            return
        col, rev = self._stk_sort
        key = _STK_SORT_KEYS.get(col)
        ordered = sorted(stocks, key=key, reverse=rev) if key else list(stocks)
        self._displayed_stocks = ordered
        self._stk_tree.delete(*self._stk_tree.get_children())
        for s in ordered:
            perf1m = _fmt(s.returns_1m, suffix="%")
            perf3m = _fmt(s.returns_3m, suffix="%")
            hi52   = _fmt(s.from_52w_high_pct, suffix="%")
            rs = s.rs_rating or 0
            rs_tag = ("rs_strong" if rs >= 80 else
                      "rs_good"   if rs >= 65 else
                      "rs_mid"    if rs >= 50 else
                      "rs_weak"   if rs >= 35 else "rs_low")
            self._stk_tree.insert("", "end",
                tags=(rs_tag,),
                values=(
                    s.ticker,
                    _fmt(s.rs_rating, 0) if s.rs_rating is not None else "—",
                    s.industry or "—",
                    perf1m, perf3m, hi52,
                    s.sector or "—",
                ),
            )
        # Refresh heading indicators
        for c in _STK_COLS:
            ind = (" ▼" if rev else " ▲") if c == col else ""
            self._stk_tree.heading(c, text=c + ind,
                                   command=lambda cc=c: self._sort_stock(cc))

    def _sort_stock(self, col: str) -> None:
        cur_col, cur_rev = self._stk_sort
        self._stk_sort = (col, not cur_rev if col == cur_col else True)
        self._populate_stock_list(self._displayed_stocks)

    def _on_stock_select(self, _e: tk.Event) -> None:
        if not self._stk_tree:
            return
        sel = self._stk_tree.selection()
        if not sel:
            return
        idx = self._stk_tree.index(sel[0])
        if idx >= len(self._displayed_stocks):
            return
        stock = self._displayed_stocks[idx]
        self._update_stock_fundamentals(stock)

        # Fetch full 4-quarter history from the stock detail page if not cached yet
        if stock.ticker not in self._qtr_web_cache:
            self._qtr_web_cache.add(stock.ticker)
            threading.Thread(
                target=self._fetch_stock_quarters,
                args=(stock,),
                daemon=True,
            ).start()

        self._score_bar.config(
            text=(f"{stock.ticker}  •  {stock.name or ''}  •  "
                  f"RS {_fmt(stock.rs_rating, 0)}  •  "
                  f"1M {_fmt(stock.returns_1m, suffix='%')}  "
                  f"3M {_fmt(stock.returns_3m, suffix='%')}  "
                  f"52W Hi {_fmt(stock.from_52w_high_pct, suffix='%')}")
        )

    def _fetch_stock_quarters(self, stock) -> None:
        """Background thread: scrape 4-quarter history from the ChartsMaze stock page."""
        from .chartsmaze.client import ChartsMazeClient

        async def _run():
            async with ChartsMazeClient(
                session_cookie=os.environ.get("CHARTSMAZE_SESSION")
            ) as cm:
                return await cm.get_quarterly_from_page(stock.ticker, stock.exchange)

        try:
            qtrs = asyncio.run(_run())
        except Exception:
            qtrs = []

        if qtrs:
            self.after(0, self._on_qtrs_fetched, stock.ticker, qtrs)
        else:
            # Remove from cache so the user can retry by re-selecting the stock
            self._qtr_web_cache.discard(stock.ticker)

    def _on_qtrs_fetched(self, ticker: str, qtrs: list) -> None:
        """UI-thread callback: store web-fetched quarters and refresh if still selected."""
        self._quarterly[ticker] = qtrs
        if not self._stk_tree:
            return
        sel = self._stk_tree.selection()
        if not sel:
            return
        idx = self._stk_tree.index(sel[0])
        if idx < len(self._displayed_stocks) and self._displayed_stocks[idx].ticker == ticker:
            self._update_stock_fundamentals(self._displayed_stocks[idx])

    def _on_stock_dbl(self, event: tk.Event) -> None:
        self._open_tv_chart()

    def _open_tv_chart(self) -> None:
        if not self._stk_tree:
            return
        sel = self._stk_tree.selection()
        if not sel:
            return
        idx = self._stk_tree.index(sel[0])
        if idx >= len(self._displayed_stocks):
            return
        stock = self._displayed_stocks[idx]
        url = f"https://www.tradingview.com/chart/?symbol={stock.tv_symbol()}"
        webbrowser.open(url)

    def _update_ind_fundamentals(self, obj) -> None:
        if not self._ind_fund_vars:
            return
        is_ind = hasattr(obj, "rank_1w")
        vals = {
            "name":             obj.name,
            "sector":           obj.sector if is_ind else "—",
            "quadrant":         obj.quadrant.value if obj.quadrant else "—",
            "rs_ratio":         _fmt(obj.rs_ratio),
            "rs_momentum":      _fmt(obj.rs_momentum),
            "perf_1w":          _fmt(getattr(obj, "performance_1w", None), suffix="%"),
            "perf_1m":          _fmt(getattr(obj, "performance_1m", None), suffix="%"),
            "perf_3m":          _fmt(getattr(obj, "performance_3m", None), suffix="%"),
            "rank_1w":          str(obj.rank_1w) if is_ind and obj.rank_1w else "—",
            "rank_1m":          str(obj.rank_1m) if is_ind and obj.rank_1m else "—",
            "rank_3m":          str(obj.rank_3m) if is_ind and obj.rank_3m else "—",
            "market_cap":       _fmt(getattr(obj, "market_cap", None), 0),
            "stock_count":      str(obj.stock_count) if obj.stock_count else "—",
            "from_52w_high_pct": _fmt(getattr(obj, "from_52w_high_pct", None), suffix="%"),
        }
        for key, (var, lbl) in self._ind_fund_vars.items():
            text = vals.get(key, "—")
            var.set(text)
            lbl.config(fg=_value_color(text, key))

    def _update_stock_fundamentals(self, stock) -> None:
        # Metric cards
        for key, (var, lbl) in self._stk_metric_vars.items():
            val = getattr(stock, key, None)
            if val is None:
                var.set("—"); lbl.config(fg=FG2)
            elif key == "market_cap":
                var.set(_fmt(val, 0)); lbl.config(fg=ACCENT)
            elif key == "from_52w_high_pct":
                var.set(_fmt(val, 1, "%"))
                lbl.config(fg=GREEN if val <= 5 else (YELLOW if val <= 15 else RED))
            else:   # returns_1m, returns_3m
                var.set(_fmt(val, 1, "%"))
                lbl.config(fg=GREEN if val > 0 else RED)
        # Quarterly table
        if not self._qtr_tree:
            return
        self._qtr_tree.delete(*self._qtr_tree.get_children())
        qtrs = self._quarterly.get(stock.ticker, [])
        for q in qtrs:
            pos = sum(1 for x in [q.yoy_eps, q.yoy_sales] if x is not None and x > 0)
            neg = sum(1 for x in [q.yoy_eps, q.yoy_sales] if x is not None and x < 0)
            qtag = "q_pos" if pos > neg else ("q_neg" if neg > pos else "q_mix")
            self._qtr_tree.insert("", "end", tags=(qtag,), values=(
                q.quarter,
                _fmt(q.eps, 2) if q.eps is not None else "—",
                _fmt(q.qoq_eps, 1) if q.qoq_eps is not None else "—",
                _fmt(q.yoy_eps, 1) if q.yoy_eps is not None else "—",
                _fmt(q.sales, 2) if q.sales is not None else "—",
                _fmt(q.qoq_sales, 1) if q.qoq_sales is not None else "—",
                _fmt(q.yoy_sales, 1) if q.yoy_sales is not None else "—",
                _fmt(q.opm, 2) if q.opm is not None else "—",
            ))
        self._draw_growth_charts(qtrs)

    # ── sort / populate helpers ────────────────────────────────────────────────

    def _populate_sector_tree(self, sectors: list) -> None:
        col, rev = self._sec_sort
        key = _SEC_SORT_KEYS.get(col)
        ordered = sorted(sectors, key=key, reverse=rev) if key else sectors
        self._sec_tree.delete(*self._sec_tree.get_children())
        self._sec_tree.insert("", "end", iid="__ALL__",
            tags=("all",),
            values=("▶  ALL SECTORS", "—", "—", "—", "—"))
        for s in ordered:
            quad = s.quadrant.value if s.quadrant else "—"
            self._sec_tree.insert("", "end", iid=f"sec_{s.name}",
                tags=(quad if quad in _QUAD_FG else "dim",),
                values=(s.name, quad, _fmt(s.rs_ratio), _fmt(s.rs_momentum), _fmt(s.rrg_score())),
            )
        for c in _SEC_COLS:
            ind = (" ▼" if rev else " ▲") if c == col else ""
            self._sec_tree.heading(c, text=c + ind,
                                   command=lambda cc=c: self._sort_sector(cc))

    def _populate_ind_tree(self, inds: list) -> None:
        col, rev = self._ind_sort
        key = _IND_SORT_KEYS.get(col)
        ordered = sorted(inds, key=key, reverse=rev) if key else list(inds)
        self._displayed_inds = ordered
        self._ind_tree.delete(*self._ind_tree.get_children())
        for i in ordered:
            quad = i.quadrant.value if i.quadrant else "—"
            hi   = _fmt(i.from_52w_high_pct, suffix="%") if i.from_52w_high_pct is not None else "—"
            self._ind_tree.insert("", "end",
                tags=(quad if quad in _QUAD_FG else "dim",),
                values=(
                    i.name, quad,
                    _fmt(i.rs_ratio), _fmt(i.rs_momentum),
                    _fmt(i.trend_score(), 0),
                    i.rank_1w if i.rank_1w is not None else "—",
                    i.rank_1m if i.rank_1m is not None else "—",
                    i.rank_3m if i.rank_3m is not None else "—",
                    _fmt(i.performance_1w, suffix="%"),
                    _fmt(i.performance_1m, suffix="%"),
                    _fmt(i.performance_3m, suffix="%"),
                    hi,
                ),
            )
        for c in _IND_COLS:
            ind = (" ▼" if rev else " ▲") if c == col else ""
            self._ind_tree.heading(c, text=c + ind,
                                   command=lambda cc=c: self._sort_industry(cc))

    def _sort_sector(self, col: str) -> None:
        cur_col, cur_rev = self._sec_sort
        self._sec_sort = (col, not cur_rev if col == cur_col else True)
        # remember current selection iid, restore after repopulate
        sel = self._sec_tree.selection()
        self._populate_sector_tree(self._sectors)
        if sel:
            try:
                self._sec_tree.selection_set(sel[0])
                self._sec_tree.focus(sel[0])
            except tk.TclError:
                pass

    def _sort_industry(self, col: str) -> None:
        cur_col, cur_rev = self._ind_sort
        self._ind_sort = (col, not cur_rev if col == cur_col else True)
        self._populate_ind_tree(self._displayed_inds)

    # ── charts ─────────────────────────────────────────────────────────────────

    def _collect_pool(self) -> list:
        from .chartsmaze.models import RRGQuadrant
        seen: set[str] = set()
        pool: list = []
        for inds in self._industries.values():
            for i in inds:
                if i.quadrant in (RRGQuadrant.LEADING, RRGQuadrant.IMPROVING) and i.name not in seen:
                    seen.add(i.name)
                    pool.append(i)
        return pool

    def _sync_chart_sizes(self) -> None:
        if not _HAS_MPL:
            return
        for fig, canvas, frame in zip(self._chart_figs, self._chart_canvases, self._chart_frames):
            w, h = frame.winfo_width(), frame.winfo_height()
            if w > 20 and h > 20:
                fig.set_size_inches(w / fig.dpi, h / fig.dpi, forward=False)
                canvas.draw()
        if self._detail_fig and self._detail_canvas and self._detail_frame:
            w, h = self._detail_frame.winfo_width(), self._detail_frame.winfo_height()
            if w > 20 and h > 20:
                self._detail_fig.set_size_inches(w / self._detail_fig.dpi, h / self._detail_fig.dpi, forward=False)
                self._detail_canvas.draw()

    def _redraw_tab_charts(self) -> None:
        if not _HAS_MPL:
            return
        for fig, canvas, (period, get_rank, get_perf) in zip(
            self._chart_figs, self._chart_canvases, _PERIODS
        ):
            _draw_rank_perf_chart(fig, self._pool, period, get_rank, get_perf)
            canvas.draw()

    def _expand(self, period: str, get_rank, get_perf) -> None:
        SingleChartWindow(self, self._pool, period, get_rank, get_perf)


# ── entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    p = argparse.ArgumentParser(
        prog="chartsmaze-gui",
        description=(
            "ChartsMaze AMOLED GUI — sector/industry analysis and ranking.\n\n"
            "Layout:\n"
            "  Top-left   : Sectors table, sortable by any column\n"
            "  Top-middle : Industries table (select a sector or ALL SECTORS)\n"
            "  Top-right  : Industry detail chart (3M → 1M → 1W rank + perf)\n"
            "  Bottom     : Rank vs Performance charts for Leading & Improving "
            "industries (tabs: 1W / 1M / 3M)\n\n"
            "Tips:\n"
            "  • Click any column header to sort; click again to reverse.\n"
            "  • Select '▶ ALL SECTORS' in the sector list to see every industry.\n"
            "  • Use '⤢ Expand' on a chart tab to open a full-screen popup.\n"
            "  • Set CHARTSMAZE_SESSION in .env for authenticated data.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.parse_args()   # handles --help / -h, exits on unknown flags
    app = ChartsMazeGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
