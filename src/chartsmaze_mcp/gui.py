"""
ChartsMaze AMOLED GUI — sector / industry tables + detachable chart window.

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
BG      = "#000000"
SURFACE = "#0F0F0F"
CARD    = "#1A1A1A"
BORDER  = "#2A2A2A"
FG      = "#FFFFFF"
FG2     = "#666666"
ACCENT  = "#00CFFF"
GREEN   = "#00FF7F"
YELLOW  = "#FFD700"
ORANGE  = "#FF8C00"
RED     = "#FF3D3D"

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

_MPL_RC: dict = {
    "figure.facecolor":  BG,
    "axes.facecolor":    SURFACE,
    "axes.edgecolor":    BORDER,
    "axes.labelcolor":   FG2,
    "grid.color":        BORDER,
    "grid.linewidth":    0.5,
    "text.color":        FG2,
    "xtick.color":       FG2,
    "ytick.color":       FG2,
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
    frame = tk.Frame(parent, bg=BG)
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=columns, show="headings", style=uid, selectmode="browse")
    for col, w in zip(columns, widths):
        anchor = "w" if col in ("Sector", "Industry") else "center"
        tree.heading(col, text=col)
        tree.column(col, width=w, minwidth=w, anchor=anchor, stretch=False)
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    for quad, color in _QUAD_FG.items():
        tree.tag_configure(quad, foreground=color)
    tree.tag_configure("dim", foreground=FG2)
    return tree


def _draw_period_chart(fig: "plt.Figure", pool: list, period: str,
                       get_rank, get_perf) -> None:
    """Render one rank-vs-performance chart into *fig* (clears first)."""
    from .chartsmaze.models import RRGQuadrant

    fig.clear()
    plt.rcParams.update(_MPL_RC)

    ax = fig.add_subplot(111)
    ax.set_facecolor(SURFACE)
    for spine in ax.spines.values():
        spine.set_edgecolor(BORDER)

    valid = [
        (i, get_rank(i), get_perf(i))
        for i in pool
        if get_rank(i) is not None and get_perf(i) is not None
    ]
    valid.sort(key=lambda t: t[1])   # sort ascending by rank (worst → best)

    if not valid:
        ax.set_title(f"{period}  —  no data", color=FG2)
        return

    xs     = list(range(len(valid)))
    ranks  = [t[1] for t in valid]
    perfs  = [t[2] for t in valid]
    colors = [
        GREEN if t[0].quadrant == RRGQuadrant.LEADING else YELLOW
        for t in valid
    ]
    labels = [t[0].name[:18] for t in valid]

    # Left Y: rank (inverted)
    ax.plot(xs, ranks, color=ACCENT, linewidth=2.0, marker="o",
            markersize=4, zorder=3, label="Rank")
    ax.invert_yaxis()
    ax.set_ylabel("Rank  (↑ = better)", color=ACCENT, fontsize=9)
    ax.tick_params(axis="y", colors=ACCENT)
    ax.grid(axis="y", color=BORDER, linewidth=0.5)

    # Right Y: performance %
    ax2 = ax.twinx()
    ax2.set_facecolor(SURFACE)
    for spine in ax2.spines.values():
        spine.set_edgecolor(BORDER)
    ax2.plot(xs, perfs, color=FG2, linewidth=1.2, linestyle="--", zorder=2, alpha=0.7)
    ax2.scatter(xs, perfs, color=colors, s=50, zorder=4)
    ax2.axhline(0, color=BORDER, linewidth=0.8, zorder=1)
    ax2.set_ylabel("Performance %", color=FG2, fontsize=9)
    ax2.tick_params(axis="y", colors=FG2)

    ax.set_xticks(xs)
    ax.set_xticklabels(labels, rotation=50, ha="right", color=FG2, fontsize=8)
    ax.tick_params(axis="x", colors=FG2)
    ax.set_xlim(-0.5, len(xs) - 0.5)

    leading_n  = sum(1 for t in valid if t[0].quadrant == RRGQuadrant.LEADING)
    improving_n = len(valid) - leading_n
    ax.set_title(
        f"{period}  Rank vs Performance  "
        f"({leading_n} Leading  {improving_n} Improving)  —  MCW",
        color=FG, fontsize=11,
    )

    fig.tight_layout(pad=1.8)


# ── single-chart popup ─────────────────────────────────────────────────────────

class SingleChartWindow(tk.Toplevel):
    """Full-screen view of one period chart."""

    def __init__(self, parent: tk.Widget, pool: list,
                 period: str, get_rank, get_perf) -> None:
        super().__init__(parent)
        self.title(f"ChartsMaze — {period} Rank vs Performance")
        self.configure(bg=BG)
        self.geometry("1280x720")
        self.state("zoomed") if os.name == "nt" else self.attributes("-zoomed", True)

        if not _HAS_MPL:
            tk.Label(self, text="matplotlib not installed", bg=BG, fg=RED,
                     font=_FONT).pack(expand=True)
            return

        fig = plt.Figure(facecolor=BG)
        _draw_period_chart(fig, pool, period, get_rank, get_perf)

        canvas = FigureCanvasTkAgg(fig, master=self)
        canvas.get_tk_widget().configure(bg=BG, highlightthickness=0)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw()


# ── charts notebook window ─────────────────────────────────────────────────────

class ChartsWindow(tk.Toplevel):
    """
    Separate window with a ttk.Notebook: one tab per period (1W / 1M / 3M).

    Each tab shows a full chart and an "⤢ Expand" button to open that
    chart in its own maximised window.
    """

    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent)
        self.title("ChartsMaze — Rank vs Performance")
        self.configure(bg=BG)
        self.geometry("1280x680")
        self.protocol("WM_DELETE_WINDOW", self.withdraw)   # hide, don't destroy

        self._pool: list = []
        self._figs:    list["plt.Figure"] = []
        self._canvases: list = []

        self._build()

    def _build(self) -> None:
        # header
        hdr = tk.Frame(self, bg=BG, pady=8)
        hdr.pack(fill="x", padx=14)
        tk.Label(hdr, text="Rank vs Performance  —  Leading & Improving  (MCW)",
                 bg=BG, fg=FG2, font=_FONT_SM).pack(side="left")
        for quad, color in [("Leading", GREEN), ("Improving", YELLOW)]:
            tk.Label(hdr, text="●", bg=BG, fg=color, font=_FONT_SM).pack(side="right", padx=(0, 2))
            tk.Label(hdr, text=quad, bg=BG, fg=FG2, font=_FONT_SM).pack(side="right")
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # style the notebook tabs
        s = ttk.Style()
        s.theme_use("default")
        s.configure("Amoled.TNotebook", background=BG, borderwidth=0)
        s.configure("Amoled.TNotebook.Tab",
            background=CARD, foreground=FG2,
            padding=[16, 6], font=_FONT_BOLD,
        )
        s.map("Amoled.TNotebook.Tab",
            background=[("selected", BORDER)],
            foreground=[("selected", ACCENT)],
        )

        nb = ttk.Notebook(self, style="Amoled.TNotebook")
        nb.pack(fill="both", expand=True, padx=0, pady=0)
        self._nb = nb

        if not _HAS_MPL:
            f = tk.Frame(nb, bg=BG)
            nb.add(f, text="Charts")
            tk.Label(f, text="Install matplotlib:  pip install matplotlib",
                     bg=BG, fg=RED, font=_FONT).pack(expand=True)
            return

        for period, get_rank, get_perf in _PERIODS:
            tab = tk.Frame(nb, bg=BG)
            nb.add(tab, text=f"   {period}   ")

            # Per-tab toolbar
            bar = tk.Frame(tab, bg=BG, pady=4)
            bar.pack(fill="x", padx=10)
            tk.Button(
                bar, text="⤢  Expand in new window",
                bg=CARD, fg=ACCENT, activebackground=BORDER, activeforeground=ACCENT,
                relief="flat", font=_FONT_SM, padx=10, pady=3, cursor="hand2",
                command=lambda p=period, gr=get_rank, gp=get_perf: self._expand(p, gr, gp),
            ).pack(side="right")

            fig = plt.Figure(facecolor=BG)
            canvas = FigureCanvasTkAgg(fig, master=tab)
            canvas.get_tk_widget().configure(bg=BG, highlightthickness=0)
            canvas.get_tk_widget().pack(fill="both", expand=True)

            self._figs.append(fig)
            self._canvases.append(canvas)

    def update(self, pool: list) -> None:  # type: ignore[override]
        self._pool = pool
        if not _HAS_MPL:
            return
        for fig, canvas, (period, get_rank, get_perf) in zip(
            self._figs, self._canvases, _PERIODS
        ):
            _draw_period_chart(fig, pool, period, get_rank, get_perf)
            canvas.draw()

    def _expand(self, period: str, get_rank, get_perf) -> None:
        SingleChartWindow(self, self._pool, period, get_rank, get_perf)


# ── main window ────────────────────────────────────────────────────────────────

class ChartsMazeGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ChartsMaze")
        self.configure(bg=BG)
        self.geometry("1280x700")
        self.minsize(900, 500)

        self._sectors:    list = []
        self._industries: dict[str, list] = {}
        self._charts_win: Optional[ChartsWindow] = None

        self._build()
        self.after(100, self._fetch)

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
        self._chart_btn = tk.Button(
            hdr, text="📊  Charts", bg=CARD, fg=ACCENT,
            activebackground=BORDER, activeforeground=ACCENT,
            relief="flat", font=_FONT_BOLD, padx=12, pady=4,
            cursor="hand2", command=self._show_charts,
            state="disabled",
        )
        self._chart_btn.pack(side="right", padx=(6, 0))
        self._btn = tk.Button(
            hdr, text="⟳  Refresh", bg=CARD, fg=ACCENT,
            activebackground=BORDER, activeforeground=ACCENT,
            relief="flat", font=_FONT_BOLD, padx=12, pady=4,
            cursor="hand2", command=self._fetch,
        )
        self._btn.pack(side="right")

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # body: sectors | industries
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(8, 0))

        # sector panel (fixed width)
        sec_pane = tk.Frame(body, bg=BG, width=390)
        sec_pane.pack(side="left", fill="y")
        sec_pane.pack_propagate(False)
        tk.Label(sec_pane, text="SECTORS", bg=BG, fg=FG2, font=_FONT_SM).pack(anchor="w", pady=(0, 4))
        self._sec_tree = _styled_tree(
            sec_pane,
            columns=["Sector",  "Quad",  "RS",   "Mom",  "Score"],
            widths= [160,        82,      52,     52,     60],
        )
        self._sec_tree.bind("<<TreeviewSelect>>", self._on_sector_select)

        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y", padx=6)

        # industry panel (expands)
        ind_pane = tk.Frame(body, bg=BG)
        ind_pane.pack(side="left", fill="both", expand=True)
        tk.Label(ind_pane, text="INDUSTRIES", bg=BG, fg=FG2, font=_FONT_SM).pack(anchor="w", pady=(0, 4))
        self._ind_tree = _styled_tree(
            ind_pane,
            columns=["Industry", "Quad",  "RS",  "Mom",  "Trend", "R1W", "R1M", "R3M", "P1W%", "P1M%", "P3M%"],
            widths= [180,         82,      52,    52,     60,      42,    42,    42,    60,     60,     60],
        )
        self._ind_tree.bind("<<TreeviewSelect>>", self._on_industry_select)

        # status bar
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")
        self._score_bar = tk.Label(
            self, text="", bg=BG, fg=FG2, font=_FONT_SM, anchor="w", pady=4,
        )
        self._score_bar.pack(fill="x", padx=16)

    # ── data loading ───────────────────────────────────────────────────────────

    def _fetch(self) -> None:
        self._btn.config(state="disabled")
        self._chart_btn.config(state="disabled")
        self._status_var.set("Loading data — this may take ~30 s…")
        self._sectors = []
        self._industries = {}
        self._sec_tree.delete(*self._sec_tree.get_children())
        self._ind_tree.delete(*self._ind_tree.get_children())
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        from .chartsmaze.client import ChartsMazeClient

        async def _run() -> tuple:
            async with ChartsMazeClient(
                session_cookie=os.environ.get("CHARTSMAZE_SESSION")
            ) as cm:
                sectors = await cm.get_sector_analysis()
                all_inds: dict[str, list] = {}
                for s in sectors:
                    all_inds[s.name] = await cm.get_industry_analysis(s.name)
                return sectors, all_inds

        try:
            sectors, all_inds = asyncio.run(_run())
            ranked = sorted(sectors, key=lambda s: s.rrg_score(), reverse=True)
            self.after(0, self._on_data, ranked, all_inds)
        except Exception as exc:
            self.after(0, self._on_error, str(exc))

    # ── UI updates ─────────────────────────────────────────────────────────────

    def _on_data(self, sectors: list, all_inds: dict) -> None:
        self._sectors    = sectors
        self._industries = all_inds

        self._sec_tree.delete(*self._sec_tree.get_children())
        for s in sectors:
            quad = s.quadrant.value if s.quadrant else "—"
            tag  = quad if quad in _QUAD_FG else "dim"
            self._sec_tree.insert("", "end", tags=(tag,), values=(
                s.name, quad,
                _fmt(s.rs_ratio), _fmt(s.rs_momentum), _fmt(s.rrg_score()),
            ))

        leading = sum(1 for s in sectors if s.quadrant and s.quadrant.value == "Leading")
        self._status_var.set(f"{len(sectors)} sectors  •  {leading} Leading")
        self._btn.config(state="normal")
        self._chart_btn.config(state="normal")

        # push data into charts window if already open
        self._push_charts()

        children = self._sec_tree.get_children()
        if children:
            self._sec_tree.selection_set(children[0])
            self._sec_tree.focus(children[0])

    def _on_error(self, msg: str) -> None:
        self._status_var.set(f"Error: {msg[:120]}")
        self._btn.config(state="normal")

    def _on_sector_select(self, _event: tk.Event) -> None:
        sel = self._sec_tree.selection()
        if not sel:
            return
        idx    = self._sec_tree.index(sel[0])
        sector = self._sectors[idx]
        inds   = self._industries.get(sector.name, [])

        self._ind_tree.delete(*self._ind_tree.get_children())
        for i in inds:
            quad = i.quadrant.value if i.quadrant else "—"
            tag  = quad if quad in _QUAD_FG else "dim"
            self._ind_tree.insert("", "end", tags=(tag,), values=(
                i.name, quad,
                _fmt(i.rs_ratio), _fmt(i.rs_momentum),
                _fmt(i.trend_score(), 0),
                i.rank_1w  if i.rank_1w  is not None else "—",
                i.rank_1m  if i.rank_1m  is not None else "—",
                i.rank_3m  if i.rank_3m  is not None else "—",
                _fmt(i.performance_1w, suffix="%"),
                _fmt(i.performance_1m, suffix="%"),
                _fmt(i.performance_3m, suffix="%"),
            ))

        self._score_bar.config(
            text=(
                f"{sector.name}  •  RRG score {_fmt(sector.rrg_score())}  "
                f"•  ind avg trend {_fmt(sector.industry_avg_trend, 0)}  "
                f"•  {len(inds)} industries"
            )
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
        i = inds[idx]
        self._score_bar.config(
            text=(
                f"{i.name}  •  trend {_fmt(i.trend_score(), 0)}  "
                f"•  RS {_fmt(i.rs_ratio)} / mom {_fmt(i.rs_momentum)}  "
                f"•  ranks 1W:{i.rank_1w or '—'} 1M:{i.rank_1m or '—'} 3M:{i.rank_3m or '—'}  "
                f"•  perf 1W:{_fmt(i.performance_1w, suffix='%')} "
                f"1M:{_fmt(i.performance_1m, suffix='%')} "
                f"3M:{_fmt(i.performance_3m, suffix='%')}"
            )
        )

    # ── charts window ──────────────────────────────────────────────────────────

    def _collect_pool(self) -> list:
        """Unique Leading + Improving industries across all sectors."""
        from .chartsmaze.models import RRGQuadrant
        seen: set[str] = set()
        pool: list = []
        for inds in self._industries.values():
            for i in inds:
                if (
                    i.quadrant in (RRGQuadrant.LEADING, RRGQuadrant.IMPROVING)
                    and i.name not in seen
                ):
                    seen.add(i.name)
                    pool.append(i)
        return pool

    def _push_charts(self) -> None:
        """Send current data to charts window if it exists."""
        if self._charts_win is not None:
            try:
                if self._charts_win.winfo_exists():
                    self._charts_win.update(self._collect_pool())
            except tk.TclError:
                self._charts_win = None

    def _show_charts(self) -> None:
        """Open (or raise) the charts window and populate with current data."""
        if self._charts_win is None or not self._charts_win.winfo_exists():
            self._charts_win = ChartsWindow(self)
        else:
            self._charts_win.deiconify()
            self._charts_win.lift()

        if self._industries:
            self._charts_win.update(self._collect_pool())


# ── entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    app = ChartsMazeGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
