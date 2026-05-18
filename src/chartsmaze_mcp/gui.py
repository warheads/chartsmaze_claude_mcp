"""
ChartsMaze AMOLED GUI — sector / industry tables + rank-vs-performance charts.

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

# ── matplotlib AMOLED style ────────────────────────────────────────────────────
_MPL_RC: dict = {
    "figure.facecolor":    BG,
    "axes.facecolor":      SURFACE,
    "axes.edgecolor":      BORDER,
    "axes.labelcolor":     FG2,
    "grid.color":          BORDER,
    "grid.linewidth":      0.5,
    "text.color":          FG2,
    "xtick.color":         FG2,
    "ytick.color":         FG2,
    "xtick.labelsize":     7,
    "ytick.labelsize":     7,
    "axes.titlesize":      9,
    "axes.titlecolor":     FG,
    "axes.titlepad":       6,
}

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


# ── main window ────────────────────────────────────────────────────────────────

class ChartsMazeGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ChartsMaze")
        self.configure(bg=BG)
        self.geometry("1400x860")
        self.minsize(1000, 600)

        self._sectors:    list = []
        self._industries: dict[str, list] = {}

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
        self._btn = tk.Button(
            hdr, text="⟳  Refresh", bg=CARD, fg=ACCENT,
            activebackground=BORDER, activeforeground=ACCENT,
            relief="flat", font=_FONT_BOLD, padx=12, pady=4,
            cursor="hand2", command=self._fetch,
        )
        self._btn.pack(side="right")

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # vertical PanedWindow: tables on top, charts on bottom
        paned = tk.PanedWindow(
            self, orient="vertical", bg=BG,
            sashwidth=5, sashrelief="flat", sashpad=0,
        )
        paned.pack(fill="both", expand=True, padx=12, pady=(8, 0))

        # ── top pane: sector + industry tables ──
        tables = tk.Frame(paned, bg=BG)
        paned.add(tables, stretch="always", minsize=180)

        self._build_sector_panel(tables)
        tk.Frame(tables, bg=BORDER, width=1).pack(side="left", fill="y", padx=6)
        self._build_industry_panel(tables)

        # ── bottom pane: rank-vs-performance charts ──
        charts = tk.Frame(paned, bg=BG)
        paned.add(charts, stretch="always", minsize=220)

        self._build_charts(charts)

        # status bar
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")
        self._score_bar = tk.Label(
            self, text="", bg=BG, fg=FG2, font=_FONT_SM, anchor="w", pady=4,
        )
        self._score_bar.pack(fill="x", padx=16)

    def _build_sector_panel(self, parent: tk.Widget) -> None:
        pane = tk.Frame(parent, bg=BG, width=380)
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
        pane = tk.Frame(parent, bg=BG)
        pane.pack(side="left", fill="both", expand=True)
        tk.Label(pane, text="INDUSTRIES", bg=BG, fg=FG2, font=_FONT_SM).pack(anchor="w", pady=(0, 4))
        self._ind_tree = _styled_tree(
            pane,
            columns=["Industry", "Quad",  "RS",  "Mom",  "Trend", "R1W", "R1M", "R3M", "P1W%", "P1M%", "P3M%"],
            widths= [180,         82,      52,    52,     60,      42,    42,    42,    60,     60,     60],
        )
        self._ind_tree.bind("<<TreeviewSelect>>", self._on_industry_select)

    def _build_charts(self, parent: tk.Widget) -> None:
        """Bottom pane: 3 dual-axis line charts — 1W / 1M / 3M rank vs performance."""
        label_row = tk.Frame(parent, bg=BG)
        label_row.pack(fill="x")
        tk.Label(
            label_row,
            text="RANK vs PERFORMANCE  —  Leading & Improving industries  (MCW)",
            bg=BG, fg=FG2, font=_FONT_SM,
        ).pack(side="left", pady=(2, 4))

        # Legend
        for quad, color in [("Leading", GREEN), ("Improving", YELLOW)]:
            tk.Label(label_row, text="●", bg=BG, fg=color, font=_FONT_SM).pack(side="right", padx=(0, 2))
            tk.Label(label_row, text=quad, bg=BG, fg=FG2, font=_FONT_SM).pack(side="right")

        if not _HAS_MPL:
            tk.Label(
                parent,
                text="matplotlib not installed — run: pip install matplotlib",
                bg=BG, fg=RED, font=_FONT,
            ).pack(expand=True)
            self._mpl_canvas = None
            return

        plt.rcParams.update(_MPL_RC)

        fig = plt.Figure(figsize=(14, 3.4), facecolor=BG)
        fig.subplots_adjust(left=0.055, right=0.97, top=0.86, bottom=0.36, wspace=0.5)
        self._mpl_fig = fig

        canvas = FigureCanvasTkAgg(fig, master=parent)
        canvas.get_tk_widget().configure(bg=BG, highlightthickness=0)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self._mpl_canvas = canvas

    # ── data loading ───────────────────────────────────────────────────────────

    def _fetch(self) -> None:
        self._btn.config(state="disabled")
        self._status_var.set("Loading data — this may take ~30 s…")
        self._sectors = []
        self._industries = {}
        self._sec_tree.delete(*self._sec_tree.get_children())
        self._ind_tree.delete(*self._ind_tree.get_children())
        if self._mpl_canvas:
            self._mpl_fig.clear()
            self._mpl_canvas.draw()
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

    # ── UI updates (always on main thread via after()) ─────────────────────────

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

        self._update_charts()

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

    # ── charts ─────────────────────────────────────────────────────────────────

    def _update_charts(self) -> None:
        if not _HAS_MPL or not self._mpl_canvas:
            return

        from .chartsmaze.models import RRGQuadrant

        # Gather unique Leading + Improving industries from all sectors.
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

        self._mpl_fig.clear()

        periods = [
            ("1W", lambda i: i.rank_1w, lambda i: i.performance_1w),
            ("1M", lambda i: i.rank_1m, lambda i: i.performance_1m),
            ("3M", lambda i: i.rank_3m, lambda i: i.performance_3m),
        ]

        for col, (period, get_rank, get_perf) in enumerate(periods, start=1):
            ax = self._mpl_fig.add_subplot(1, 3, col)
            ax.set_facecolor(SURFACE)
            for spine in ax.spines.values():
                spine.set_edgecolor(BORDER)

            valid = [
                (i, get_rank(i), get_perf(i))
                for i in pool
                if get_rank(i) is not None and get_perf(i) is not None
            ]
            valid.sort(key=lambda t: t[1])  # sort by rank ascending (best last, worst first)

            if not valid:
                ax.set_title(f"{period}  —  no data", color=FG2)
                continue

            xs     = list(range(len(valid)))
            ranks  = [t[1] for t in valid]
            perfs  = [t[2] for t in valid]
            colors = [
                GREEN if t[0].quadrant == RRGQuadrant.LEADING else YELLOW
                for t in valid
            ]
            labels = [t[0].name[:14] for t in valid]

            # Left Y: rank (inverted so rank 1 = top)
            ax.plot(xs, ranks, color=ACCENT, linewidth=1.6, marker="o",
                    markersize=3, zorder=3, label="Rank")
            ax.invert_yaxis()
            ax.set_ylabel("Rank  (↑ = better)", color=ACCENT, fontsize=7.5)
            ax.tick_params(axis="y", colors=ACCENT)
            ax.grid(axis="y", color=BORDER, linewidth=0.5)

            # Right Y: performance %
            ax2 = ax.twinx()
            ax2.set_facecolor(SURFACE)
            for spine in ax2.spines.values():
                spine.set_edgecolor(BORDER)

            ax2.plot(xs, perfs, color=FG2, linewidth=1.0,
                     linestyle="--", zorder=2, alpha=0.7)
            ax2.scatter(xs, perfs, color=colors, s=28, zorder=4)
            ax2.axhline(0, color=BORDER, linewidth=0.8, zorder=1)
            ax2.set_ylabel("Performance %", color=FG2, fontsize=7.5)
            ax2.tick_params(axis="y", colors=FG2)

            # X axis: industry names
            ax.set_xticks(xs)
            ax.set_xticklabels(labels, rotation=55, ha="right",
                               color=FG2, fontsize=6.5)
            ax.tick_params(axis="x", colors=FG2)
            ax.set_xlim(-0.5, len(xs) - 0.5)

            leading_n  = sum(1 for t in valid if t[0].quadrant == RRGQuadrant.LEADING)
            improving_n = len(valid) - leading_n
            ax.set_title(
                f"{period}  Rank vs Perf  "
                f"({leading_n} Leading  {improving_n} Improving)",
                color=FG, fontsize=8.5,
            )

        self._mpl_fig.subplots_adjust(
            left=0.055, right=0.97, top=0.86, bottom=0.36, wspace=0.5,
        )
        self._mpl_canvas.draw()


# ── entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    app = ChartsMazeGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
