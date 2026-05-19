"""
Diagnostic script — shows every quarterly-relevant column + value for a given ticker.

Usage:
    python debug_quarters.py VIMTALABS
    python debug_quarters.py ITC
"""
from __future__ import annotations
import asyncio
import re
import sys
from dotenv import load_dotenv

load_dotenv()

_QTR_RE = re.compile(
    r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[- ]?(\d{2,4})\b',
    re.IGNORECASE,
)
_LATEST_RE = re.compile(r'Latest(?:-(\d+))?', re.I)


async def main(ticker: str) -> None:
    from src.chartsmaze_mcp.chartsmaze.client import ChartsMazeClient

    async with ChartsMazeClient() as client:
        await client._ensure_data()
        fund_rows = client._parsed.get("fundamental", [])
        row = next((r for r in fund_rows if r.get("Stock Name", "").upper() == ticker.upper()), None)

        if row is None:
            print(f"[!] Ticker '{ticker}' not found in fundamental.gz")
            print("    Available tickers:", [r.get("Stock Name","") for r in fund_rows[:20]])
            return

        print(f"\n=== {ticker} — ALL COLUMNS ({len(row)} total) ===\n")

        date_tagged = {}
        latest_cols = {}
        for col, val in row.items():
            if _QTR_RE.search(col):
                date_tagged[col] = val
            elif _LATEST_RE.search(col):
                latest_cols[col] = val

        print(f"--- Date-tagged columns ({len(date_tagged)}) ---")
        for col, val in sorted(date_tagged.items()):
            print(f"  {col!r:50s} = {val!r}")

        print(f"\n--- Latest-N columns ({len(latest_cols)}) ---")
        for col, val in sorted(latest_cols.items()):
            print(f"  {col!r:50s} = {val!r}")

        print("\n--- Result from _extract_quarterly ---")
        from src.chartsmaze_mcp.chartsmaze.client import _extract_quarterly
        qtrs = _extract_quarterly(row)
        if not qtrs:
            print("  (empty — no quarterly data extracted)")
        for q in qtrs:
            print(f"  {q}")


if __name__ == "__main__":
    ticker = sys.argv[1] if len(sys.argv) > 1 else "ITC"
    asyncio.run(main(ticker))
