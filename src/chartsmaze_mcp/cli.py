"""
Standalone CLI for running the ChartsMaze workflow without an MCP client.

Usage:
    chartsmaze run                  Full workflow (sectors → screen → TradingView)
    chartsmaze sectors              List sectors ranked by RRG score
    chartsmaze industries SECTOR    List industries in a sector
    chartsmaze screen SECTOR ...    Screen stocks with filters
    chartsmaze stock TICKER         Single stock info
    chartsmaze discover URL         Capture .gz data calls made by a ChartsMaze page
    chartsmaze tv-discover URL      Capture ALL XHR/fetch calls made by a TradingView page
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ------------------------------------------------------------------ commands

async def _cmd_sectors(_args: argparse.Namespace) -> None:
    from .chartsmaze.client import ChartsMazeClient
    from .chartsmaze.models import SectorData

    async with ChartsMazeClient(session_cookie=os.environ.get("CHARTSMAZE_SESSION")) as cm:
        sectors = await cm.get_sector_analysis()

    if not sectors:
        print("No sector data returned. Run 'chartsmaze discover https://chartsmaze.com' to debug.", file=sys.stderr)
        sys.exit(1)

    ranked = sorted(sectors, key=lambda s: s.rrg_score(), reverse=True)
    _print([s.model_dump() for s in ranked])


async def _cmd_industries(args: argparse.Namespace) -> None:
    from .chartsmaze.client import ChartsMazeClient

    async with ChartsMazeClient(session_cookie=os.environ.get("CHARTSMAZE_SESSION")) as cm:
        industries = await cm.get_industry_analysis(args.sector)

    ranked = sorted(industries, key=lambda i: i.performance_1d or 0.0, reverse=True)
    _print([i.model_dump() for i in ranked])


async def _cmd_screen(args: argparse.Namespace) -> None:
    from .chartsmaze.client import ChartsMazeClient

    async with ChartsMazeClient(session_cookie=os.environ.get("CHARTSMAZE_SESSION")) as cm:
        stocks = await cm.screen_stocks(
            sectors=args.sectors,
            min_eps_growth_pct=args.min_eps,
            min_revenue_growth_pct=args.min_revenue,
            min_volume_20d_ma=args.min_volume,
            exclude_circuit=not args.include_circuit,
        )

    _print({
        "count": len(stocks),
        "stocks": [s.model_dump() for s in stocks],
    })


async def _cmd_stock(args: argparse.Namespace) -> None:
    from .chartsmaze.client import ChartsMazeClient

    async with ChartsMazeClient(session_cookie=os.environ.get("CHARTSMAZE_SESSION")) as cm:
        stock = await cm.get_stock_info(args.ticker.upper())

    if not stock:
        print(f"No data found for '{args.ticker}'", file=sys.stderr)
        sys.exit(1)
    _print(stock.model_dump())


async def _cmd_discover(args: argparse.Namespace) -> None:
    from .chartsmaze.client import ChartsMazeClient

    async with ChartsMazeClient(session_cookie=os.environ.get("CHARTSMAZE_SESSION")) as cm:
        calls = await cm.discover_api_calls(args.url)

    _print({"url": args.url, "calls_captured": len(calls), "calls": calls})


async def _cmd_tv_discover(args: argparse.Namespace) -> None:
    from .tradingview.client import TradingViewClient

    tv_session = os.environ.get("TRADINGVIEW_SESSION")
    if not tv_session:
        print("ERROR: TRADINGVIEW_SESSION not set in .env", file=sys.stderr)
        sys.exit(1)

    interactive = getattr(args, "interactive", False)
    async with TradingViewClient(
        session_id=tv_session,
        session_sign=os.environ.get("TRADINGVIEW_SESSION_SIGN"),
        headless=not interactive,
    ) as tv:
        calls = await tv.discover_api_calls(args.url, interactive=interactive)

    # For interactive mode highlight write operations prominently.
    if interactive:
        writes = [c for c in calls if c["method"] in ("POST", "PUT", "PATCH", "DELETE")]
        print(f"\n[tv-discover] {len(calls)} total calls, {len(writes)} write (POST/PUT/PATCH/DELETE):\n", file=sys.stderr)
        for c in writes:
            body = c.get("request_body") or ""
            body_preview = (body[:120] + "…") if len(body) > 120 else body
            print(f"  {c['method']:6} {c['status']}  {c['url']}", file=sys.stderr)
            if body_preview:
                print(f"         body: {body_preview}", file=sys.stderr)
        print("", file=sys.stderr)

    _print({"url": args.url, "calls_captured": len(calls), "calls": calls})


async def _cmd_run(args: argparse.Namespace) -> None:
    from .chartsmaze.client import ChartsMazeClient
    from .chartsmaze.models import SectorData
    from .tradingview.client import TradingViewClient

    summary: dict = {}

    async with ChartsMazeClient(session_cookie=os.environ.get("CHARTSMAZE_SESSION")) as cm:
        print(f"[1/4] Fetching sector analysis...", file=sys.stderr)
        all_sectors = await cm.get_sector_analysis()

        if not all_sectors:
            print("ERROR: Could not retrieve sector data. Run 'chartsmaze discover https://chartsmaze.com' to debug.", file=sys.stderr)
            sys.exit(1)

        ranked = sorted(all_sectors, key=lambda s: s.rrg_score(), reverse=True)
        top_sectors = [s.name for s in ranked[: args.top_n]]
        summary["top_sectors"] = top_sectors
        summary["sector_details"] = [s.model_dump() for s in ranked[: args.top_n]]
        print(f"    Leading sectors: {', '.join(top_sectors)}", file=sys.stderr)

        print(f"[2/4] Fetching industry analysis...", file=sys.stderr)
        industries_by_sector: dict[str, list[str]] = {}
        for sector in top_sectors:
            inds = await cm.get_industry_analysis(sector)
            inds_sorted = sorted(inds, key=lambda i: i.performance_1d or 0.0, reverse=True)
            industries_by_sector[sector] = [i.name for i in inds_sorted[:3]]
        summary["top_industries"] = industries_by_sector

        print(f"[3/4] Screening stocks (vol 20d MA ≥ {args.min_volume:,}, no circuit)...", file=sys.stderr)
        stocks = await cm.screen_stocks(
            sectors=top_sectors,
            min_eps_growth_pct=args.min_eps,
            min_revenue_growth_pct=args.min_revenue,
            min_volume_20d_ma=args.min_volume,
            exclude_circuit=True,
        )
        tickers = [s.ticker for s in stocks]
        summary["screened_stocks"] = {"count": len(stocks), "stocks": [s.model_dump() for s in stocks]}
        print(f"    {len(stocks)} stocks passed filters", file=sys.stderr)

    tv_session = os.environ.get("TRADINGVIEW_SESSION")
    if tv_session and tickers:
        print(f"[4/4] Pushing {len(tickers)} tickers to TradingView watchlist '{args.watchlist}'...", file=sys.stderr)
        tv_symbols = [f"{args.exchange}:{t}" for t in tickers]
        async with TradingViewClient(
            session_id=tv_session,
            session_sign=os.environ.get("TRADINGVIEW_SESSION_SIGN"),
        ) as tv:
            tv_result = await tv.add_to_watchlist(
                symbols=tv_symbols,
                watchlist_name=args.watchlist,
            )
        summary["tradingview"] = tv_result
        print(f"    Done — {tv_result.get('symbols_added', [])} added", file=sys.stderr)
    elif not tv_session:
        summary["tradingview"] = {
            "status": "skipped",
            "reason": "TRADINGVIEW_SESSION not set",
            "symbols_ready": [f"{args.exchange}:{t}" for t in tickers],
        }
        print("[4/4] Skipped TradingView — set TRADINGVIEW_SESSION to enable.", file=sys.stderr)
    else:
        summary["tradingview"] = {"status": "skipped", "reason": "no stocks passed filters"}

    _print(summary)


# ------------------------------------------------------------------ argparse

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chartsmaze",
        description="ChartsMaze + TradingView trading workflow CLI",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # run
    r = sub.add_parser("run", help="Full workflow: sectors → industries → screen → TradingView")
    r.add_argument("--top-n", type=int, default=3, metavar="N", dest="top_n", help="Number of top sectors (default 3)")
    r.add_argument("--min-eps", type=float, default=0.0, metavar="PCT", dest="min_eps", help="Min EPS growth %% (default 0)")
    r.add_argument("--min-revenue", type=float, default=0.0, metavar="PCT", dest="min_revenue", help="Min revenue growth %% (default 0)")
    r.add_argument("--min-volume", type=int, default=50_000, metavar="VOL", dest="min_volume", help="Min 20d volume MA (default 50000)")
    r.add_argument("--watchlist", default="ChartsMaze Picks", help="TradingView watchlist name")
    r.add_argument("--exchange", default="NSE", help="Exchange prefix for TradingView (default NSE)")

    # sectors
    sub.add_parser("sectors", help="List sectors ranked by RRG score")

    # industries
    ind = sub.add_parser("industries", help="List industries in a sector")
    ind.add_argument("sector", help="Sector name, e.g. 'Technology'")

    # screen
    scr = sub.add_parser("screen", help="Screen stocks in sectors with filters")
    scr.add_argument("sectors", nargs="+", help="Sector names")
    scr.add_argument("--min-eps", type=float, default=0.0, dest="min_eps", metavar="PCT")
    scr.add_argument("--min-revenue", type=float, default=0.0, dest="min_revenue", metavar="PCT")
    scr.add_argument("--min-volume", type=int, default=50_000, dest="min_volume", metavar="VOL")
    scr.add_argument("--include-circuit", action="store_true", dest="include_circuit", help="Do NOT exclude circuit stocks")

    # stock
    stk = sub.add_parser("stock", help="Single stock info")
    stk.add_argument("ticker", help="NSE/BSE ticker, e.g. RELIANCE")

    # discover (ChartsMaze .gz files)
    disc = sub.add_parser("discover", help="Capture .gz data calls made by a ChartsMaze page")
    disc.add_argument("url", help="Full URL, e.g. https://chartsmaze.com/custom-scanner")

    # tv-discover (all TradingView XHR/fetch calls)
    tvd = sub.add_parser("tv-discover", help="Capture ALL XHR/fetch calls made by a TradingView page (requires TRADINGVIEW_SESSION)")
    tvd.add_argument("url", help="Full URL, e.g. https://www.tradingview.com")
    tvd.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Open a VISIBLE browser and wait for you to perform an action before stopping capture — use this to find write (POST/PUT/PATCH) endpoints",
    )

    return p


_DISPATCH = {
    "run":         _cmd_run,
    "sectors":     _cmd_sectors,
    "industries":  _cmd_industries,
    "screen":      _cmd_screen,
    "stock":       _cmd_stock,
    "discover":    _cmd_discover,
    "tv-discover": _cmd_tv_discover,
}


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(_DISPATCH[args.command](args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
