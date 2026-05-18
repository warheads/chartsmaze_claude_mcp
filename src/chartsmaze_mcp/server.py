"""
ChartsMaze MCP Server

Automates the trading research workflow:
  1. Identify leading sectors/industries via ChartsMaze Sector Analysis + RRG
  2. Screen stocks in those sectors: EPS growth, revenue growth, volume 20d MA ≥ 50K, no circuit
  3. Push qualifying tickers to a TradingView watchlist for manual chart review

Environment variables (put in .env or export before running):
  CHARTSMAZE_SESSION       — optional; session cookie for auth-gated scanner features
  TRADINGVIEW_SESSION      — required for watchlist tools
  TRADINGVIEW_SESSION_SIGN — optional; avoids TradingView 2FA re-prompts
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .chartsmaze.client import ChartsMazeClient
from .chartsmaze.models import RRGQuadrant, SectorData
from .tradingview.client import TradingViewClient

load_dotenv()
logging.basicConfig(level=logging.WARNING)

mcp = FastMCP(
    "ChartsMaze Workflow",
    dependencies=["playwright", "httpx", "beautifulsoup4", "python-dotenv"],
)


# =========================================================================== helpers

def _cm_client() -> ChartsMazeClient:
    return ChartsMazeClient(session_cookie=os.environ.get("CHARTSMAZE_SESSION"))


def _tv_client() -> Optional[TradingViewClient]:
    session = os.environ.get("TRADINGVIEW_SESSION")
    if not session:
        return None
    return TradingViewClient(
        session_id=session,
        session_sign=os.environ.get("TRADINGVIEW_SESSION_SIGN"),
    )


def _rank_sectors(sectors: list[SectorData], top_n: int) -> list[str]:
    """Sort sectors by RRG score (Leading quadrant + RS strength + 1-day perf)."""
    ranked = sorted(sectors, key=lambda s: s.rrg_score(), reverse=True)
    return [s.name for s in ranked[:top_n]]


def _fmt(obj: object) -> str:
    return json.dumps(obj, indent=2, default=str)


# =========================================================================== tools

@mcp.tool()
async def analyze_sectors() -> str:
    """
    Fetch all sectors from ChartsMaze with their 1-day performance and RRG
    quadrant (Leading / Improving / Weakening / Lagging).

    Returns a JSON array sorted by RRG score — Leading + high RS sectors first.
    Use this as **Step 1** of the workflow to decide which sectors to focus on.
    """
    async with _cm_client() as cm:
        sectors = await cm.get_sector_analysis()

    if not sectors:
        return _fmt({
            "error": "No sector data retrieved.",
            "hint": (
                "ChartsMaze is a React SPA. If data is consistently empty, "
                "ensure Playwright browsers are installed (`playwright install chromium`) "
                "and the site is reachable."
            ),
        })

    ranked = sorted(sectors, key=lambda s: s.rrg_score(), reverse=True)
    return _fmt([s.model_dump() for s in ranked])


@mcp.tool()
async def analyze_industries(
    sector: Annotated[
        str,
        Field(description="Sector name exactly as returned by analyze_sectors, e.g. 'Technology'"),
    ]
) -> str:
    """
    Fetch industries within *sector* from ChartsMaze.

    Returns a JSON array sorted by 1-day performance (best first).
    Use this as **Step 1b** to narrow down which industries inside a leading
    sector are actually driving the move.
    """
    async with _cm_client() as cm:
        industries = await cm.get_industry_analysis(sector)

    if not industries:
        return _fmt({"error": f"No industry data for sector '{sector}'."})

    ranked = sorted(industries, key=lambda i: i.performance_1d or 0.0, reverse=True)
    return _fmt([i.model_dump() for i in ranked])


@mcp.tool()
async def get_rrg_leaders() -> str:
    """
    Return sectors/industries currently in the **Leading** quadrant of
    ChartsMaze's Relative Rotation Graph (high RS-Ratio AND rising momentum).

    These are the strongest candidates for long setups.
    """
    async with _cm_client() as cm:
        leaders = await cm.get_rrg_leaders()

    if not leaders:
        return _fmt({
            "note": "No RRG data found. The RRG chart may require user interaction or a paid plan.",
            "fallback": "Use analyze_sectors() — sectors tagged quadrant='Leading' are RRG leaders.",
        })

    return _fmt(leaders)


@mcp.tool()
async def screen_stocks(
    sectors: Annotated[
        list[str],
        Field(description="Sector names to scan, e.g. ['Technology', 'Capital Goods']"),
    ],
    min_eps_growth_pct: Annotated[
        float,
        Field(description="Minimum quarterly EPS growth %. 0 = no filter."),
    ] = 0.0,
    min_revenue_growth_pct: Annotated[
        float,
        Field(description="Minimum quarterly revenue/sales growth %. 0 = no filter."),
    ] = 0.0,
    min_volume_20d_ma: Annotated[
        int,
        Field(description="Minimum 20-day average volume for liquidity. Default 50 000."),
    ] = 50_000,
    exclude_circuit: Annotated[
        bool,
        Field(description="Exclude stocks in upper/lower price circuit. Default True."),
    ] = True,
) -> str:
    """
    Run ChartsMaze's custom scanner for *sectors* and apply:
      • 20-day volume MA ≥ *min_volume_20d_ma* (liquidity gate)
      • Circuit filter  — drop stocks locked in upper/lower circuit
      • EPS growth      ≥ *min_eps_growth_pct* %
      • Revenue growth  ≥ *min_revenue_growth_pct* %

    Returns a JSON array of qualifying stocks with fundamentals and volume data.
    """
    async with _cm_client() as cm:
        stocks = await cm.screen_stocks(
            sectors=sectors,
            min_eps_growth_pct=min_eps_growth_pct,
            min_revenue_growth_pct=min_revenue_growth_pct,
            min_volume_20d_ma=min_volume_20d_ma,
            exclude_circuit=exclude_circuit,
        )

    return _fmt({
        "filters": {
            "sectors": sectors,
            "min_eps_growth_pct": min_eps_growth_pct,
            "min_revenue_growth_pct": min_revenue_growth_pct,
            "min_volume_20d_ma": min_volume_20d_ma,
            "exclude_circuit": exclude_circuit,
        },
        "count": len(stocks),
        "stocks": [s.model_dump() for s in stocks],
    })


@mcp.tool()
async def get_stock_info(
    ticker: Annotated[str, Field(description="NSE/BSE ticker symbol, e.g. 'RELIANCE'")]
) -> str:
    """
    Fetch detailed information for a single stock from ChartsMaze's stock-info page.

    Returns price, volume, EPS/revenue growth, sector, and circuit status.
    """
    async with _cm_client() as cm:
        stock = await cm.get_stock_info(ticker.upper())

    if not stock:
        return _fmt({"error": f"No data found for ticker '{ticker}'."})

    return _fmt(stock.model_dump())


@mcp.tool()
async def add_to_tradingview_watchlist(
    symbols: Annotated[
        list[str],
        Field(description="Ticker symbols WITHOUT exchange prefix, e.g. ['RELIANCE', 'TCS']"),
    ],
    watchlist_name: Annotated[
        str,
        Field(description="TradingView watchlist name. Created if it does not exist."),
    ],
    exchange: Annotated[
        str,
        Field(description="Exchange prefix appended to each symbol. Default 'NSE'."),
    ] = "NSE",
    replace: Annotated[
        bool,
        Field(description="If True, replace existing symbols. If False (default), append."),
    ] = False,
) -> str:
    """
    Add *symbols* to a TradingView watchlist.

    Requires **TRADINGVIEW_SESSION** environment variable (sessionid cookie).
    The watchlist is created automatically if it does not exist.
    """
    tv = _tv_client()
    if tv is None:
        return _fmt({
            "error": "TRADINGVIEW_SESSION not set.",
            "instructions": (
                "1. Log in to tradingview.com in your browser.\n"
                "2. Open DevTools → Application → Cookies → tradingview.com.\n"
                "3. Copy 'sessionid' value → set TRADINGVIEW_SESSION=<value>.\n"
                "4. Optionally copy 'sessionid_sign' → TRADINGVIEW_SESSION_SIGN."
            ),
        })

    tv_symbols = [f"{exchange}:{s.upper()}" for s in symbols]
    async with tv:
        result = await tv.add_to_watchlist(
            symbols=tv_symbols,
            watchlist_name=watchlist_name,
            replace=replace,
        )

    return _fmt(result)


@mcp.tool()
async def run_full_workflow(
    top_n_sectors: Annotated[
        int,
        Field(description="How many top sectors to focus on. Default 3."),
    ] = 3,
    min_eps_growth_pct: Annotated[
        float,
        Field(description="Minimum EPS growth % filter applied to the stock scan."),
    ] = 0.0,
    min_revenue_growth_pct: Annotated[
        float,
        Field(description="Minimum revenue growth % filter applied to the stock scan."),
    ] = 0.0,
    watchlist_name: Annotated[
        str,
        Field(description="TradingView watchlist to push results into."),
    ] = "ChartsMaze Picks",
    exchange: Annotated[
        str,
        Field(description="Exchange prefix for TradingView symbols."),
    ] = "NSE",
) -> str:
    """
    **Full automated workflow — run this to get your shortlist in one shot.**

    Steps executed:
      1. Fetch all sectors from ChartsMaze, rank by RRG score (Leading quadrant first).
      2. Pick the top *top_n_sectors* sectors.
      3. For each sector fetch top 3 industries (ranked by 1-day performance).
      4. Run the custom scanner for those sectors with:
           • volume 20d MA ≥ 50 000
           • EPS growth ≥ *min_eps_growth_pct* %
           • Revenue growth ≥ *min_revenue_growth_pct* %
           • Circuit exclusion ON
      5. Push qualifying tickers to the TradingView watchlist *watchlist_name*.

    Returns a full summary: top sectors, top industries, screened stocks, and
    the TradingView update result.
    """
    summary: dict = {}

    async with _cm_client() as cm:
        # ---- step 1: sector analysis
        all_sectors = await cm.get_sector_analysis()
        if not all_sectors:
            return _fmt({
                "error": "Could not retrieve sector data from ChartsMaze.",
                "hint": "Run analyze_sectors() alone to debug connectivity.",
            })

        top_sectors = _rank_sectors(all_sectors, top_n=top_n_sectors)
        summary["top_sectors"] = top_sectors
        summary["sector_details"] = [
            s.model_dump()
            for s in sorted(all_sectors, key=lambda x: x.rrg_score(), reverse=True)[:top_n_sectors]
        ]

        # ---- step 2: top industries per sector
        top_industries: list[str] = []
        industries_by_sector: dict[str, list[str]] = {}
        for sector in top_sectors:
            inds = await cm.get_industry_analysis(sector)
            inds_sorted = sorted(inds, key=lambda i: i.performance_1d or 0.0, reverse=True)
            top_3 = [i.name for i in inds_sorted[:3]]
            industries_by_sector[sector] = top_3
            top_industries.extend(top_3)
        summary["top_industries"] = industries_by_sector

        # ---- step 3: screen stocks
        stocks = await cm.screen_stocks(
            sectors=top_sectors,
            min_eps_growth_pct=min_eps_growth_pct,
            min_revenue_growth_pct=min_revenue_growth_pct,
            min_volume_20d_ma=50_000,
            exclude_circuit=True,
        )

    summary["screened_stocks"] = {
        "count": len(stocks),
        "filters": {
            "min_volume_20d_ma": 50_000,
            "exclude_circuit": True,
            "min_eps_growth_pct": min_eps_growth_pct,
            "min_revenue_growth_pct": min_revenue_growth_pct,
        },
        "stocks": [s.model_dump() for s in stocks],
    }

    tickers = [s.ticker for s in stocks]

    # ---- step 4: push to TradingView
    tv = _tv_client()
    if tv and tickers:
        tv_symbols = [f"{exchange}:{t}" for t in tickers]
        async with tv:
            tv_result = await tv.add_to_watchlist(
                symbols=tv_symbols,
                watchlist_name=watchlist_name,
            )
        summary["tradingview"] = tv_result
    elif not tickers:
        summary["tradingview"] = {
            "status": "skipped",
            "reason": "No stocks passed all filters.",
        }
    else:
        summary["tradingview"] = {
            "status": "skipped",
            "reason": "TRADINGVIEW_SESSION not set — set it to enable auto-watchlist.",
            "symbols_ready_to_add": [f"{exchange}:{t}" for t in tickers],
        }

    return _fmt(summary)


@mcp.tool()
async def discover_api_calls(
    url: Annotated[
        str,
        Field(description="Full ChartsMaze URL to probe, e.g. 'https://chartsmaze.com/custom-scanner'"),
    ]
) -> str:
    """
    Load *url* in a headless browser, capture every JSON API call made,
    and return the endpoint URLs with top-level response keys.

    Use this to understand ChartsMaze's internal API structure so you can
    refine the scraping logic or call endpoints directly.
    """
    async with _cm_client() as cm:
        calls = await cm.discover_api_calls(url)

    return _fmt({
        "url_probed": url,
        "api_calls_captured": len(calls),
        "calls": calls,
    })


# =========================================================================== entry point

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
