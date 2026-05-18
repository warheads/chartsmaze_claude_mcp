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
import tkinter as tk
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


# ── helpers ────────────────────────────────────────────────────────────────────

def _fmt(v: Optional[float], decimals: int = 1, suffix: str = "") -> str:
    return f"{v:.{decimals}f}{suffix}" if v is not None else "—"


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

    # Left Y: rank (low = best, normal direction)
    ax.plot(xs, ranks, color=ACCENT, linewidth=2.0, marker="o", markersize=4, zorder=3)
    ax.set_ylabel("Rank  (low = best)", color=ACCENT, fontsize=9)
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

    periods = ["1W", "1M", "3M"]
    ranks   = [ind.rank_1w,         ind.rank_1m,         ind.rank_3m]
    perfs   = [ind.performance_1w,  ind.performance_1m,  ind.performance_3m]
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

    ax.set_ylabel("Rank", color=ACCENT, fontsize=9)
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
        canvas = FigureCanvasTkAgg(fig, master=self)
        canvas.get_tk_widget().configure(bg=BG, highlightthickness=0)
        canvas.get_tk_widget().pack(fill="both", expand=True)

        def _on_resize(e, f=fig, c=canvas):
            if e.width < 10 or e.height < 10:
                return
            f.set_size_inches(e.width / f.dpi, e.height / f.dpi, forward=False)
            c.draw_idle()

        canvas.get_tk_widget().bind("<Configure>", _on_resize)
        canvas.draw()


# ── main window ────────────────────────────────────────────────────────────────

class ChartsMazeGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ChartsMaze")
        self.configure(bg=BG)
        self.geometry("1400x860")

        self._sectors:     list = []
        self._industries:  dict[str, list] = {}
        self._pool:        list = []
        self._chart_figs:     list = []
        self._chart_canvases: list = []
        self._detail_fig:    Optional["plt.Figure"] = None
        self._detail_canvas = None
        self._sash_placed   = False

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
        if not self._sash_placed:
            h = self.winfo_height()
            if h > 300:
                self._sash_placed = True
                self._paned.sash_place(0, 0, int(h * 0.40))

    # ── layout ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # header
        hdr = tk.Frame(self, bg=BG, pady=10)
        hdr.pack(fill="x", padx=16)
        tk.Label(hdr, text="ChartsMaze", bg=BG, fg=ACCENT, font=_FONT_H).pack(side="left")
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

        # vertical PanedWindow
        self._paned = tk.PanedWindow(
            self, orient="vertical", bg=BG,
            sashwidth=6, sashrelief="flat", sashpad=0,
        )
        self._paned.pack(fill="both", expand=True, padx=10, pady=(8, 0))

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

        # status bar
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")
        self._score_bar = tk.Label(
            self, text="", bg=BG, fg=FG2, font=_FONT_SM, anchor="w", pady=4,
        )
        self._score_bar.pack(fill="x", padx=16)

    def _build_sector_panel(self, parent: tk.Widget) -> None:
        pane = tk.Frame(parent, bg=BG, width=390)
        pane.pack(side="left", fill="y")
        pane.pack_propagate(False)
        tk.Label(pane, text="SECTORS", bg=BG, fg=FG2, font=_FONT_SM).pack(anchor="w", pady=(0, 4))
        self._sec_tree = _styled_tree(
            pane,
            columns=["Sector",  "Quad",  "RS",   "Mom",  "Score"],
            widths= [160,        82,      52,     52,     60],
        )
        self._sec_tree.bind("<<TreeviewSelect>>", self._on_sector_select)

    def _build_industry_panel(self, parent: tk.Widget) -> None:
        pane = tk.Frame(parent, bg=BG, width=820)
        pane.pack(side="left", fill="y")
        pane.pack_propagate(False)
        tk.Label(pane, text="INDUSTRIES", bg=BG, fg=FG2, font=_FONT_SM).pack(anchor="w", pady=(0, 4))
        self._ind_tree = _styled_tree(
            pane,
            columns=["Industry", "Quad",  "RS",  "Mom", "Trend", "R1W", "R1M", "R3M",
                     "P1W%", "P1M%", "P3M%", "52W Hi%"],
            widths= [180,         82,      52,    52,    58,      42,    42,    42,
                     58,    58,    58,    72],
        )
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

        fig = plt.Figure(facecolor=BG)
        # placeholder annotation
        plt.rcParams.update(_MPL_RC)
        ax = fig.add_subplot(111)
        _style_axes(ax)
        ax.text(0.5, 0.5, "Select an industry", transform=ax.transAxes,
                ha="center", va="center", color=FG2, fontsize=13)
        ax.set_xticks([]); ax.set_yticks([])

        canvas = FigureCanvasTkAgg(fig, master=pane)
        canvas.get_tk_widget().configure(bg=BG, highlightthickness=0)
        canvas.get_tk_widget().pack(fill="both", expand=True)

        def _on_resize(e, f=fig, c=canvas):
            if e.width < 10 or e.height < 10:
                return
            f.set_size_inches(e.width / f.dpi, e.height / f.dpi, forward=False)
            c.draw_idle()

        canvas.get_tk_widget().bind("<Configure>", _on_resize)
        canvas.draw()

        self._detail_fig    = fig
        self._detail_canvas = canvas

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

            fig = plt.Figure(facecolor=BG)
            canvas = FigureCanvasTkAgg(fig, master=tab)
            canvas.get_tk_widget().configure(bg=BG, highlightthickness=0)
            canvas.get_tk_widget().pack(fill="both", expand=True)

            def _on_resize(e, f=fig, c=canvas):
                if e.width < 10 or e.height < 10:
                    return
                f.set_size_inches(e.width / f.dpi, e.height / f.dpi, forward=False)
                c.draw_idle()

            canvas.get_tk_widget().bind("<Configure>", _on_resize)

            self._chart_figs.append(fig)
            self._chart_canvases.append(canvas)

    # ── data loading ───────────────────────────────────────────────────────────

    def _fetch(self) -> None:
        self._btn.config(state="disabled")
        self._status_var.set("Loading data — this may take ~30 s…")
        self._sectors = []; self._industries = {}; self._pool = []
        self._sec_tree.delete(*self._sec_tree.get_children())
        self._ind_tree.delete(*self._ind_tree.get_children())
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        from .chartsmaze.client import ChartsMazeClient

        async def _run() -> tuple:
            async with ChartsMazeClient(session_cookie=os.environ.get("CHARTSMAZE_SESSION")) as cm:
                sectors  = await cm.get_sector_analysis()
                all_inds = {s.name: await cm.get_industry_analysis(s.name) for s in sectors}
                return sectors, all_inds

        try:
            sectors, all_inds = asyncio.run(_run())
            self.after(0, self._on_data,
                       sorted(sectors, key=lambda s: s.rrg_score(), reverse=True),
                       all_inds)
        except Exception as exc:
            self.after(0, self._on_error, str(exc))

    # ── UI updates ─────────────────────────────────────────────────────────────

    def _on_data(self, sectors: list, all_inds: dict) -> None:
        self._sectors    = sectors
        self._industries = all_inds

        self._sec_tree.delete(*self._sec_tree.get_children())
        for s in sectors:
            quad = s.quadrant.value if s.quadrant else "—"
            self._sec_tree.insert("", "end",
                tags=(quad if quad in _QUAD_FG else "dim",),
                values=(s.name, quad, _fmt(s.rs_ratio), _fmt(s.rs_momentum), _fmt(s.rrg_score())),
            )

        leading = sum(1 for s in sectors if s.quadrant and s.quadrant.value == "Leading")
        self._status_var.set(f"{len(sectors)} sectors  •  {leading} Leading")
        self._btn.config(state="normal")

        self._pool = self._collect_pool()
        self._redraw_tab_charts()

        ch = self._sec_tree.get_children()
        if ch:
            self._sec_tree.selection_set(ch[0])
            self._sec_tree.focus(ch[0])

    def _on_error(self, msg: str) -> None:
        self._status_var.set(f"Error: {msg[:120]}")
        self._btn.config(state="normal")

    def _on_sector_select(self, _event: tk.Event) -> None:
        sel = self._sec_tree.selection()
        if not sel:
            return
        sector = self._sectors[self._sec_tree.index(sel[0])]
        inds   = self._industries.get(sector.name, [])

        self._ind_tree.delete(*self._ind_tree.get_children())
        for i in inds:
            quad = i.quadrant.value if i.quadrant else "—"
            hi   = _fmt(i.from_52w_high_pct, suffix="%") if i.from_52w_high_pct is not None else "—"
            self._ind_tree.insert("", "end",
                tags=(quad if quad in _QUAD_FG else "dim",),
                values=(
                    i.name, quad,
                    _fmt(i.rs_ratio), _fmt(i.rs_momentum),
                    _fmt(i.trend_score(), 0),
                    i.rank_1w  if i.rank_1w  is not None else "—",
                    i.rank_1m  if i.rank_1m  is not None else "—",
                    i.rank_3m  if i.rank_3m  is not None else "—",
                    _fmt(i.performance_1w, suffix="%"),
                    _fmt(i.performance_1m, suffix="%"),
                    _fmt(i.performance_3m, suffix="%"),
                    hi,
                ),
            )

        self._score_bar.config(
            text=(f"{sector.name}  •  RRG score {_fmt(sector.rrg_score())}  "
                  f"•  ind avg trend {_fmt(sector.industry_avg_trend, 0)}  "
                  f"•  {len(inds)} industries")
        )

    def _on_industry_select(self, _event: tk.Event) -> None:
        sel = self._ind_tree.selection()
        if not sel:
            return
        idx     = self._ind_tree.index(sel[0])
        sec_sel = self._sec_tree.selection()
        if not sec_sel:
            return
        sector = self._sectors[self._sec_tree.index(sec_sel[0])]
        inds   = self._industries.get(sector.name, [])
        if idx >= len(inds):
            return
        ind = inds[idx]

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
    app = ChartsMazeGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
