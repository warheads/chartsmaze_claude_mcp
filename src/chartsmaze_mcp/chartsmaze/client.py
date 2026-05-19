"""
ChartsMaze data loader — direct CSV approach.

ChartsMaze serves its data as gzip-compressed CSVs from its own CDN
(under /static/media/).  The filenames include content-hash suffixes that
rotate whenever the data is updated, so we:

  1. Load the scanner page with Playwright once to capture all .gz URLs.
  2. Download those files via httpx (no auth needed — they're public).
  3. Parse, join, and filter the CSV data entirely in Python.

Data files used:
  rs_filter.gz       — 88-column stock sheet: sector, industry, 20d vol MA,
                       circuit limit, RS rating, price, exchange, …
  fundamental.gz     — 68-column earnings sheet: YoY EPS %, YoY Sales %, P/E, …
  industry.gz        — 12-column industry sheet: 1D/1W/1M/3M performance, rank
  rrg_daily.gz       — Daily RS-Ratio,RS-Momentum time series for 1 300+ indices
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import io
import logging
import re
from typing import Any, Optional

import httpx
from playwright.async_api import Browser, BrowserContext, async_playwright

from .models import IndustryData, QuarterlyData, RRGQuadrant, SectorData, StockData

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Patterns to match against the URL path for each data role.
_FILE_PATTERNS: dict[str, str] = {
    "rs_filter":   "RS%20filter",
    "fundamental": "fundamental%20analysis",
    "industry":    "Industry%20Analysis",
    "rrg_daily":   "RRG_Index_and_Industry_D",
    "rrg_weekly":  "RRG_Index_and_Industry_W",
}

# Column aliases used in RS filter CSV.
_COL_TICKER        = "Stock Name"
_COL_COMPANY       = "Company Name"
_COL_SECTOR        = "Sector"
_COL_INDUSTRY      = "Basic Industry"
_COL_PRICE         = "Stock Price"
_COL_CHANGE_1D     = "1 Day Returns(%)"
_COL_VOL_20D_MA    = "20 Days MA Volume"
_COL_CIRCUIT_LIMIT = "Circuit Limit"
_COL_MARKET_CAP    = "Market Cap"
_COL_RS_RATING     = "RS Rating"
_COL_EXCHANGE      = "Exchange"

# Column aliases used in fundamental CSV.
_COL_EPS_YOY   = "YoY % EPS Latest"
_COL_SALES_YOY = "YoY % Sales Latest"
_COL_PE        = "P/E"

# Extended columns from rs_filter.gz — try multiple name variants defensively.
# The actual column name is detected at runtime via _pick_col().
_COL_RETURNS_1M_VARIANTS   = ["1 Month Returns(%)", "1M Returns(%)", "1 Month Return(%)"]
_COL_RETURNS_3M_VARIANTS   = ["3 Month Returns(%)", "3M Returns(%)", "3 Month Return(%)"]
_COL_52W_HIGH_VARIANTS     = ["% from 52W High", "52W High%", "Stock % from 52W High",
                               "% From 52W High", "Pct From 52W High"]

# Quarterly parser constants.
_MONTH_IDX: dict[str, int] = {
    m: i for i, m in enumerate(
        ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    )
}
_QTR_RE = re.compile(
    r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[- ]?(\d{2,4})\b',
    re.IGNORECASE,
)


class ChartsMazeClient:
    BASE = "https://chartsmaze.com"

    def __init__(self, session_cookie: Optional[str] = None):
        self._session_cookie = session_cookie
        # Caches populated during a single context.
        self._file_urls: dict[str, str] = {}
        self._parsed:    dict[str, list[dict]] = {}
        self._pw   = None
        self._browser: Optional[Browser]        = None
        self._ctx:     Optional[BrowserContext] = None

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> "ChartsMazeClient":
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=True)
        except Exception as exc:
            await self._pw.stop()
            raise RuntimeError(
                "Chromium not found. Run: python -m playwright install chromium"
            ) from exc
        self._ctx = await self._browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ------------------------------------------------------------------ public API

    async def get_sector_analysis(self) -> list[SectorData]:
        """
        Return sectors with multi-factor scoring data:
          • RRG quadrant + RS-Ratio / RS-Momentum (from rrg_daily.gz)
          • 1D/1M/3M performance aggregated from constituent industries
          • Average industry trend score (rank improvement + perf consistency)
        """
        await self._ensure_data()
        industry_rows = self._parsed.get("industry", [])
        rs_rows       = self._parsed.get("rs_filter", [])
        rrg_rows      = self._parsed.get("rrg_daily", [])

        industry_to_sector: dict[str, str] = {}
        for r in rs_rows:
            ind = r.get(_COL_INDUSTRY, "").strip()
            sec = r.get(_COL_SECTOR, "").strip()
            if ind and sec:
                industry_to_sector[ind] = sec

        rrg_latest = _parse_rrg_latest(rrg_rows, industry_to_sector)

        # Build per-sector aggregates from industry rows.
        sector_p1d:    dict[str, list[float]] = {}
        sector_p1m:    dict[str, list[float]] = {}
        sector_p3m:    dict[str, list[float]] = {}
        sector_trends: dict[str, list[float]] = {}

        for row in industry_rows:
            ind = row.get("Basic Industry", "").strip()
            sec = industry_to_sector.get(ind, "")
            if not sec:
                continue
            rrg = rrg_latest.get(ind, {})
            ind_obj = IndustryData(
                name=ind,
                sector=sec,
                performance_1d=_flt(row.get("Industry 1D Performance(%)")),
                performance_1w=_flt(row.get("Industry 1W Performance(%)")),
                performance_1m=_flt(row.get("Industry 1M Performance(%)")),
                performance_3m=_flt(row.get("Industry 3M Performance(%)")),
                rank_1w=_int(row.get("Industry 1W Rank")),
                rank_1m=_int(row.get("Industry 1M Rank")),
                rank_3m=_int(row.get("Industry 3M Rank")),
                stock_count=_int(row.get("Number of Stocks")),
                market_cap=_flt(row.get("Group Market Cap")),
                from_52w_high_pct=_flt(
                    row.get("Industry % from 52W High")
                    or row.get("% from 52W High")
                    or row.get("Industry 52W High%")
                ),
                quadrant=rrg.get("quadrant"),
                rs_ratio=rrg.get("rs_ratio"),
                rs_momentum=rrg.get("rs_momentum"),
            )
            sector_trends.setdefault(sec, []).append(ind_obj.trend_score())
            if ind_obj.performance_1d is not None:
                sector_p1d.setdefault(sec, []).append(ind_obj.performance_1d)
            if ind_obj.performance_1m is not None:
                sector_p1m.setdefault(sec, []).append(ind_obj.performance_1m)
            if ind_obj.performance_3m is not None:
                sector_p3m.setdefault(sec, []).append(ind_obj.performance_3m)

        sectors: list[SectorData] = []
        for sec in sector_trends:
            p1d = sector_p1d.get(sec, [])
            p1m = sector_p1m.get(sec, [])
            p3m = sector_p3m.get(sec, [])
            trends = sector_trends[sec]
            rrg = rrg_latest.get(sec, {})
            sectors.append(SectorData(
                name=sec,
                performance_1d=sum(p1d) / len(p1d) if p1d else None,
                performance_1m=sum(p1m) / len(p1m) if p1m else None,
                performance_3m=sum(p3m) / len(p3m) if p3m else None,
                quadrant=rrg.get("quadrant"),
                rs_ratio=rrg.get("rs_ratio"),
                rs_momentum=rrg.get("rs_momentum"),
                industry_avg_trend=sum(trends) / len(trends),
            ))

        return sectors

    async def get_industry_analysis(self, sector: str) -> list[IndustryData]:
        """Return industries filtered to *sector*, sorted by multi-factor trend score."""
        await self._ensure_data()
        industry_rows = self._parsed.get("industry", [])
        rs_rows       = self._parsed.get("rs_filter", [])
        rrg_rows      = self._parsed.get("rrg_daily", [])

        industry_to_sector: dict[str, str] = {
            r[_COL_INDUSTRY]: r[_COL_SECTOR]
            for r in rs_rows
            if r.get(_COL_INDUSTRY) and r.get(_COL_SECTOR)
        }
        rrg_latest = _parse_rrg_latest(rrg_rows, industry_to_sector)

        industries: list[IndustryData] = []
        for row in industry_rows:
            ind     = row.get("Basic Industry", "").strip()
            ind_sec = industry_to_sector.get(ind, "")
            if sector and ind_sec and sector.lower() not in ind_sec.lower():
                continue
            rrg = rrg_latest.get(ind, {})
            industries.append(IndustryData(
                name=ind,
                sector=ind_sec,
                performance_1d=_flt(row.get("Industry 1D Performance(%)")),
                performance_1w=_flt(row.get("Industry 1W Performance(%)")),
                performance_1m=_flt(row.get("Industry 1M Performance(%)")),
                performance_3m=_flt(row.get("Industry 3M Performance(%)")),
                rank_1w=_int(row.get("Industry 1W Rank")),
                rank_1m=_int(row.get("Industry 1M Rank")),
                rank_3m=_int(row.get("Industry 3M Rank")),
                stock_count=_int(row.get("Number of Stocks")),
                market_cap=_flt(row.get("Group Market Cap")),
                from_52w_high_pct=_flt(
                    row.get("Industry % from 52W High")
                    or row.get("% from 52W High")
                    or row.get("Industry 52W High%")
                ),
                quadrant=rrg.get("quadrant"),
                rs_ratio=rrg.get("rs_ratio"),
                rs_momentum=rrg.get("rs_momentum"),
            ))

        return sorted(industries, key=lambda i: i.trend_score(), reverse=True)

    async def get_rrg_leaders(self) -> list[dict]:
        """Return sectors/industries currently in the Leading RRG quadrant."""
        await self._ensure_data()
        rrg_rows = self._parsed.get("rrg_daily", [])
        rs_rows  = self._parsed.get("rs_filter", [])
        industry_to_sector = {
            r[_COL_INDUSTRY]: r[_COL_SECTOR]
            for r in rs_rows
            if r.get(_COL_INDUSTRY) and r.get(_COL_SECTOR)
        }
        rrg_latest = _parse_rrg_latest(rrg_rows, industry_to_sector)
        return [
            {"name": name, **data}
            for name, data in rrg_latest.items()
            if data.get("quadrant") == RRGQuadrant.LEADING
        ]

    async def screen_stocks(
        self,
        sectors: list[str],
        min_eps_growth_pct: float = 0.0,
        min_revenue_growth_pct: float = 0.0,
        min_volume_20d_ma: int = 50_000,
        exclude_circuit: bool = True,
    ) -> list[StockData]:
        """
        Filter all stocks from the RS filter + fundamental data.

        Filters applied:
          • Sector membership (if *sectors* is non-empty)
          • 20-day volume MA >= *min_volume_20d_ma*
          • Circuit limit > 5 % (proxy for illiquid/circuit-prone stocks)
          • YoY EPS growth >= *min_eps_growth_pct*
          • YoY Sales growth >= *min_revenue_growth_pct*
        """
        await self._ensure_data()
        rs_rows   = self._parsed.get("rs_filter", [])
        fund_rows = self._parsed.get("fundamental", [])

        # Build ticker → fundamental row map.
        fund_map: dict[str, dict] = {r[_COL_TICKER]: r for r in fund_rows if r.get(_COL_TICKER)}

        stocks: list[StockData] = []
        for r in rs_rows:
            ticker = r.get(_COL_TICKER, "").strip()
            if not ticker:
                continue

            sec = r.get(_COL_SECTOR, "").strip()
            if sectors and not any(s.lower() in sec.lower() for s in sectors):
                continue

            vol_20d = _flt(r.get(_COL_VOL_20D_MA))
            if vol_20d is not None and vol_20d < min_volume_20d_ma:
                continue

            if exclude_circuit:
                circuit_pct = _flt(r.get(_COL_CIRCUIT_LIMIT))
                if circuit_pct is not None and circuit_pct <= 5:
                    continue

            f = fund_map.get(ticker, {})
            eps_yoy   = _flt(f.get(_COL_EPS_YOY))
            sales_yoy = _flt(f.get(_COL_SALES_YOY))

            if min_eps_growth_pct > 0 and (eps_yoy is None or eps_yoy < min_eps_growth_pct):
                continue
            if min_revenue_growth_pct > 0 and (sales_yoy is None or sales_yoy < min_revenue_growth_pct):
                continue

            stocks.append(StockData(
                ticker=ticker,
                name=r.get(_COL_COMPANY) or ticker,
                sector=sec,
                industry=r.get(_COL_INDUSTRY, "").strip() or None,
                exchange=r.get(_COL_EXCHANGE, "NSE").strip().upper() or "NSE",
                price=_flt(r.get(_COL_PRICE)),
                change_pct=_flt(r.get(_COL_CHANGE_1D)),
                volume_20d_ma=int(vol_20d) if vol_20d is not None else None,
                circuit_status=None,
                eps_growth_pct=eps_yoy,
                revenue_growth_pct=sales_yoy,
                market_cap=_flt(r.get(_COL_MARKET_CAP)),
                pe_ratio=_flt(f.get(_COL_PE)) if f else None,
                rs_rating=_flt(r.get(_COL_RS_RATING)),
                returns_1m=_pick_col(r, _COL_RETURNS_1M_VARIANTS),
                returns_3m=_pick_col(r, _COL_RETURNS_3M_VARIANTS),
                from_52w_high_pct=_pick_col(r, _COL_52W_HIGH_VARIANTS),
            ))

        return sorted(stocks, key=lambda s: s.eps_growth_pct or 0.0, reverse=True)

    async def get_stock_info(self, ticker: str) -> Optional[StockData]:
        """Return all available data for a single ticker."""
        await self._ensure_data()
        rs_rows   = self._parsed.get("rs_filter", [])
        fund_rows = self._parsed.get("fundamental", [])
        fund_map  = {r[_COL_TICKER]: r for r in fund_rows if r.get(_COL_TICKER)}

        target = ticker.upper()
        for r in rs_rows:
            if r.get(_COL_TICKER, "").upper() != target:
                continue
            f  = fund_map.get(target, {})
            vol_20d = _flt(r.get(_COL_VOL_20D_MA))
            return StockData(
                ticker=target,
                name=r.get(_COL_COMPANY) or target,
                sector=r.get(_COL_SECTOR, "").strip() or None,
                industry=r.get(_COL_INDUSTRY, "").strip() or None,
                exchange=r.get(_COL_EXCHANGE, "NSE").strip().upper() or "NSE",
                price=_flt(r.get(_COL_PRICE)),
                change_pct=_flt(r.get(_COL_CHANGE_1D)),
                volume_20d_ma=int(vol_20d) if vol_20d is not None else None,
                circuit_status=None,
                eps_growth_pct=_flt(f.get(_COL_EPS_YOY)),
                revenue_growth_pct=_flt(f.get(_COL_SALES_YOY)),
                market_cap=_flt(r.get(_COL_MARKET_CAP)),
                pe_ratio=_flt(f.get(_COL_PE)) if f else None,
                rs_rating=_flt(r.get(_COL_RS_RATING)),
                returns_1m=_pick_col(r, _COL_RETURNS_1M_VARIANTS),
                returns_3m=_pick_col(r, _COL_RETURNS_3M_VARIANTS),
                from_52w_high_pct=_pick_col(r, _COL_52W_HIGH_VARIANTS),
            )
        return None

    async def get_stocks_grouped_by_industry(self) -> dict[str, list[StockData]]:
        """Return every stock keyed by Basic Industry, with extended fields."""
        await self._ensure_data()
        rs_rows   = self._parsed.get("rs_filter", [])
        fund_rows = self._parsed.get("fundamental", [])
        fund_map: dict[str, dict] = {r[_COL_TICKER]: r for r in fund_rows if r.get(_COL_TICKER)}

        result: dict[str, list[StockData]] = {}
        for r in rs_rows:
            ticker = r.get(_COL_TICKER, "").strip()
            if not ticker:
                continue
            sec = r.get(_COL_SECTOR, "").strip()
            ind = r.get(_COL_INDUSTRY, "").strip()
            f   = fund_map.get(ticker, {})
            vol_20d = _flt(r.get(_COL_VOL_20D_MA))
            result.setdefault(ind or "—", []).append(StockData(
                ticker=ticker,
                name=r.get(_COL_COMPANY) or ticker,
                sector=sec or None,
                industry=ind or None,
                exchange=r.get(_COL_EXCHANGE, "NSE").strip().upper() or "NSE",
                price=_flt(r.get(_COL_PRICE)),
                change_pct=_flt(r.get(_COL_CHANGE_1D)),
                volume_20d_ma=int(vol_20d) if vol_20d is not None else None,
                eps_growth_pct=_flt(f.get(_COL_EPS_YOY)),
                revenue_growth_pct=_flt(f.get(_COL_SALES_YOY)),
                market_cap=_flt(r.get(_COL_MARKET_CAP)),
                pe_ratio=_flt(f.get(_COL_PE)) if f else None,
                rs_rating=_flt(r.get(_COL_RS_RATING)),
                returns_1m=_pick_col(r, _COL_RETURNS_1M_VARIANTS),
                returns_3m=_pick_col(r, _COL_RETURNS_3M_VARIANTS),
                from_52w_high_pct=_pick_col(r, _COL_52W_HIGH_VARIANTS),
            ))
        for lst in result.values():
            lst.sort(key=lambda s: s.rs_rating or 0.0, reverse=True)
        return result

    async def get_all_quarterly_data(self) -> dict[str, list[QuarterlyData]]:
        """Return {ticker: [QuarterlyData, ...]} for all tickers in fundamental.gz."""
        await self._ensure_data()
        return {
            r[_COL_TICKER]: _extract_quarterly(r)
            for r in self._parsed.get("fundamental", [])
            if r.get(_COL_TICKER)
        }

    async def discover_api_calls(self, url: str) -> list[dict]:
        """Load *url* and return the .gz data-file URLs the page requests."""
        gz_urls = await self._discover_gz_urls(url)
        return [{"url": u} for u in gz_urls]

    # ------------------------------------------------------------------ internals

    async def _ensure_data(self) -> None:
        """Discover file URLs and download+parse them once per client context."""
        if self._parsed:
            return
        # Load both pages in parallel — industry-analytics may expose additional
        # industry gz columns that custom-scanner does not.
        results = await asyncio.gather(
            self._discover_gz_urls(f"{self.BASE}/custom-scanner"),
            self._discover_gz_urls(f"{self.BASE}/industry-analytics"),
            return_exceptions=True,
        )
        url_set: set[str] = set()
        for r in results:
            if isinstance(r, list):
                url_set.update(r)
        urls = list(url_set)
        if not urls:
            logger.warning("No .gz URLs discovered; data will be empty.")
            return
        async with httpx.AsyncClient(
            headers={"User-Agent": _UA, "Referer": self.BASE + "/"},
            verify=False,
            timeout=60.0,
        ) as http:
            for role, pattern in _FILE_PATTERNS.items():
                url = next((u for u in urls if pattern in u), None)
                if not url:
                    logger.debug("File not found for role %s", role)
                    continue
                try:
                    resp = await http.get(url)
                    resp.raise_for_status()
                    body = gzip.decompress(resp.content)
                    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8"))))
                    self._parsed[role] = rows
                    logger.debug("Loaded %s: %d rows from %s", role, len(rows), url)
                except Exception as exc:
                    logger.warning("Failed to load %s from %s: %s", role, url, exc)

    async def _discover_gz_urls(self, page_url: str) -> list[str]:
        """Navigate to *page_url* and collect all .gz file URLs."""
        assert self._ctx is not None
        page = await self._ctx.new_page()
        gz_urls: list[str] = []

        async def on_request(req: Any) -> None:
            u = req.url
            if ".gz" in u and "chartsmaze" in u:
                gz_urls.append(u)

        page.on("request", on_request)
        try:
            await page.goto(page_url, wait_until="networkidle", timeout=45_000)
        finally:
            await page.close()

        return gz_urls


# ============================================================ helpers

def _extract_quarterly(row: dict) -> list[QuarterlyData]:
    """
    Parse quarterly EPS / Sales / OPM from a fundamental row.

    Handles two common ChartsMaze naming conventions:
      • Date-tagged:  "EPS Dec25", "QoQ% EPS Sep25", "OPM Jun25 (%)"
      • Latest-N:     "EPS Latest", "EPS Latest-1", "Quarter Latest", …
    """
    qtrs: dict[str, dict] = {}

    # ── Date-tagged columns ───────────────────────────────────────────────
    for col, raw in row.items():
        m = _QTR_RE.search(col)
        if not m:
            continue
        val = _flt(raw)
        if val is None:
            continue
        mon = m.group(1).capitalize()
        yr  = m.group(2)[-2:]
        key = f"{mon} {yr}"
        q   = qtrs.setdefault(key, {})
        cl  = col.lower()
        if "opm" in cl or "operating" in cl:
            q.setdefault("opm", val)
        elif ("qoq" in cl or "q-o-q" in cl) and (
                "sales" in cl or "revenue" in cl or "turnover" in cl):
            q.setdefault("qoq_sales", val)
        elif ("yoy" in cl or "y-o-y" in cl) and (
                "sales" in cl or "revenue" in cl or "turnover" in cl):
            q.setdefault("yoy_sales", val)
        elif "sales" in cl or "revenue" in cl or "turnover" in cl:
            q.setdefault("sales", val)
        elif ("qoq" in cl or "q-o-q" in cl) and "eps" in cl:
            q.setdefault("qoq_eps", val)
        elif ("yoy" in cl or "y-o-y" in cl) and "eps" in cl:
            q.setdefault("yoy_eps", val)
        elif "eps" in cl:
            q.setdefault("eps", val)

    # ── Latest-N columns (fallback when no date tags found) ───────────────
    if not qtrs:
        latest_re = re.compile(r'Latest(?:-(\d+))?$', re.I)
        quarter_labels: dict[int, str] = {}
        for col, raw in row.items():
            lm = latest_re.search(col)
            if not lm:
                continue
            n = int(lm.group(1) or 0)
            cl = col.lower()
            if "quarter" in cl:
                quarter_labels[n] = str(raw).strip()
                continue
            val = _flt(raw)
            if val is None:
                continue
            key = f"Latest-{n}"
            q   = qtrs.setdefault(key, {})
            if "opm" in cl or "operating" in cl:
                q.setdefault("opm", val)
            elif ("qoq" in cl or "q-o-q" in cl) and (
                    "sales" in cl or "revenue" in cl or "turnover" in cl):
                q.setdefault("qoq_sales", val)
            elif ("yoy" in cl or "y-o-y" in cl) and (
                    "sales" in cl or "revenue" in cl or "turnover" in cl):
                q.setdefault("yoy_sales", val)
            elif "sales" in cl or "revenue" in cl or "turnover" in cl:
                q.setdefault("sales", val)
            elif ("qoq" in cl or "q-o-q" in cl) and "eps" in cl:
                q.setdefault("qoq_eps", val)
            elif ("yoy" in cl or "y-o-y" in cl) and "eps" in cl:
                q.setdefault("yoy_eps", val)
            elif "eps" in cl:
                q.setdefault("eps", val)
        # Replace generic keys with real quarter labels if available
        relabelled: dict[str, dict] = {}
        for key, q in qtrs.items():
            lm2 = re.search(r'\d+$', key)
            n2  = int(lm2.group()) if lm2 else 0
            label = quarter_labels.get(n2, key)
            relabelled[label] = q
        qtrs = relabelled

    # Sort most-recent first using month index, fall back to string sort
    def _qkey(k: str) -> tuple:
        parts = k.split()
        if len(parts) == 2 and parts[0] in _MONTH_IDX:
            return (int(parts[1]), _MONTH_IDX[parts[0]])
        return (0, 0)

    return [
        QuarterlyData(quarter=k, **v)
        for k in sorted(qtrs, key=_qkey, reverse=True)
    ][:4]


def _flt(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return None


def _int(v: Any) -> Optional[int]:
    f = _flt(v)
    return int(f) if f is not None else None


def _pick_col(row: dict, variants: list[str]) -> Optional[float]:
    """Return the first non-None float found among *variants* of a column name."""
    for name in variants:
        v = _flt(row.get(name))
        if v is not None:
            return v
    return None



# Maps "Nifty X" index name prefixes → sector names used in RS filter.gz
_NIFTY_TO_SECTOR: dict[str, str] = {
    "Nifty Auto":                       "Auto",
    "Nifty Consumer Durables":          "Consumer Durables",
    "Nifty Pharma":                     "Healthcare",
    "Nifty Healthcare Index":           "Healthcare",
    "Nifty Bank":                       "Financial Services",
    "Nifty Financial Services":         "Financial Services",
    "Nifty Private Bank":               "Financial Services",
    "Nifty FMCG":                       "FMCG",
    "Nifty IT":                         "Information Technology",
    "Nifty Metal":                      "Metals & Mining",
    "Nifty Realty":                     "Realty",
    "Nifty Energy":                     "Oil, Gas & Consumable fuels",
    "Nifty Oil & Gas":                  "Oil, Gas & Consumable fuels",
    "Nifty Media":                      "Media Entertainment & Publication",
    "Nifty Infrastructure":             "Construction",
    "Nifty PSU Bank":                   "Financial Services",
    "Nifty Capital Markets":            "Financial Services",
    "Nifty India Consumption":          "Consumer Services",
    "Nifty Services Sector":            "Services",
    "Nifty Commodities":                "Metals & Mining",
}


def _parse_rrg_latest(
    rrg_rows: list[dict],
    industry_to_sector: dict[str, str],
) -> dict[str, dict]:
    """
    Parse the RRG CSV and return a map of ``name → {rs_ratio, rs_momentum, quadrant}``.

    Row names come in two forms:
      • ``"Nifty Consumer Durables:Nifty 500"``  → sector-level index
      • ``"Electrical - Power Equipment MCW:Nifty 500"``  → industry-level (MCW/EW weighted)

    We prefer MCW (market-cap weighted) rows over EW rows when both exist.
    We take the latest (rightmost) non-empty date column.
    """
    if not rrg_rows:
        return {}

    date_cols: list[str] = [k for k in rrg_rows[0].keys() if k != "Index Name:Benchmark"]

    raw: dict[str, dict] = {}  # canonical_name → entry
    for row in rrg_rows:
        full_name = row.get("Index Name:Benchmark", "").strip()
        if not full_name:
            continue
        short = full_name.split(":")[0].strip()

        # Strip MCW/EW suffix to get the canonical industry name.
        is_mcw = short.endswith(" MCW")
        is_ew  = short.endswith(" EW")
        canonical = short[:-4].strip() if (is_mcw or is_ew) else short

        # Skip EW if MCW is (or will be) present — MCW is more representative.
        if is_ew and canonical in raw:
            continue

        rs_ratio = rs_momentum = None
        for col in reversed(date_cols):
            val = row.get(col, "").strip()
            if not val:
                continue
            parts = val.split(",")
            if len(parts) == 2:
                rs_ratio    = _flt(parts[0])
                rs_momentum = _flt(parts[1])
                break

        if rs_ratio is None or rs_momentum is None:
            continue

        raw[canonical] = {
            "rs_ratio":    rs_ratio,
            "rs_momentum": rs_momentum,
            "quadrant":    _quadrant(rs_ratio, rs_momentum),
        }

    # Build final result: key by industry name, sector name, and Nifty→sector mapping.
    result: dict[str, dict] = {}
    for canonical, entry in raw.items():
        result[canonical] = entry
        # Map Nifty index → sector.
        sec = _NIFTY_TO_SECTOR.get(canonical)
        if sec and sec not in result:
            result[sec] = entry
        # Map industry → sector.
        sec2 = industry_to_sector.get(canonical)
        if sec2 and sec2 not in result:
            result[sec2] = entry

    return result


def _quadrant(rs_ratio: float, rs_momentum: float) -> RRGQuadrant:
    if rs_ratio > 100 and rs_momentum > 100:
        return RRGQuadrant.LEADING
    if rs_ratio > 100:
        return RRGQuadrant.WEAKENING
    if rs_momentum > 100:
        return RRGQuadrant.IMPROVING
    return RRGQuadrant.LAGGING
