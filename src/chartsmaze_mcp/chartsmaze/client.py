"""
ChartsMaze scraper client.

Strategy (applied in order per page):
  1. Intercept XHR/fetch JSON responses — cleanest when it works.
  2. Parse window.__NEXT_DATA__ embedded in the HTML — reliable for Next.js SSR.
  3. DOM-scrape the rendered table/card layout — fallback.

Because ChartsMaze is a React SPA, Playwright is used to execute JavaScript
before any extraction is attempted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Response,
    async_playwright,
)

from .models import IndustryData, RRGQuadrant, SectorData, StockData

logger = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_RRG_QUADRANT_VALUES = {q.value for q in RRGQuadrant}


class ChartsMazeClient:
    BASE = "https://chartsmaze.com"

    def __init__(self, session_cookie: Optional[str] = None):
        self._session_cookie = session_cookie
        self._pw = None
        self._browser: Optional[Browser] = None
        self._ctx: Optional[BrowserContext] = None

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> "ChartsMazeClient":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._ctx = await self._browser.new_context(
            user_agent=_BROWSER_UA,
            viewport={"width": 1280, "height": 900},
        )
        if self._session_cookie:
            await self._ctx.add_cookies(
                [
                    {
                        "name": "session",
                        "value": self._session_cookie,
                        "domain": "chartsmaze.com",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                    }
                ]
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
        """Return all sectors with performance + RRG quadrant data."""
        probe_urls = [
            f"{self.BASE}/",
            f"{self.BASE}/market-breadth",
        ]
        for url in probe_urls:
            nd, calls = await self._load(url)
            for c in calls:
                sectors = _parse_sectors(c["data"], c["url"])
                if sectors:
                    return sectors
            if nd:
                sectors = _sectors_from_next_data(nd)
                if sectors:
                    return sectors
        return []

    async def get_industry_analysis(self, sector: str) -> list[IndustryData]:
        """Return industries (optionally filtered to a sector)."""
        nd, calls = await self._load(f"{self.BASE}/")
        for c in calls:
            industries = _parse_industries(c["data"], c["url"], sector)
            if industries:
                return industries
        if nd:
            return _industries_from_next_data(nd, sector)
        return []

    async def get_rrg_leaders(self) -> list[dict]:
        """Return only the sectors/industries in the RRG Leading quadrant."""
        nd, calls = await self._load(
            f"{self.BASE}/",
            wait_for="canvas, .rrg-chart, [class*='rrg'], [class*='rotation']",
        )
        all_rrg: list[dict] = []
        for c in calls:
            pts = _parse_rrg(c["data"], c["url"])
            if pts:
                all_rrg.extend(pts)
        if not all_rrg and nd:
            all_rrg = _rrg_from_next_data(nd)
        return [d for d in all_rrg if d.get("quadrant") == RRGQuadrant.LEADING.value]

    async def screen_stocks(
        self,
        sectors: list[str],
        min_eps_growth_pct: float = 0.0,
        min_revenue_growth_pct: float = 0.0,
        min_volume_20d_ma: int = 50_000,
        exclude_circuit: bool = True,
    ) -> list[StockData]:
        """
        Run the custom scanner, apply filters, return qualifying stocks.

        The scanner page is browser-automated: sector/volume filters are set
        via the UI before capturing the resulting API call.
        """
        page: Page = await self._ctx.new_page()
        captured: list[dict] = []

        async def _on_response(r: Response) -> None:
            if "application/json" in r.headers.get("content-type", ""):
                try:
                    captured.append({"url": r.url, "data": await r.json()})
                except Exception:
                    pass

        page.on("response", _on_response)

        try:
            await page.goto(f"{self.BASE}/custom-scanner", wait_until="networkidle", timeout=40_000)

            # --- set sector filter ---
            for sector in sectors:
                await _try_set_filter(page, sector, selectors=[
                    '[data-filter="sector"]',
                    'select[name="sector"]',
                    '[placeholder*="sector" i]',
                    '[aria-label*="sector" i]',
                ])

            # --- set min volume filter ---
            await _try_fill_input(page, str(min_volume_20d_ma), selectors=[
                '[data-filter="volume"]',
                'input[name*="volume" i]',
                '[placeholder*="volume" i]',
                '[placeholder*="vol" i]',
            ])

            # --- set EPS growth filter ---
            if min_eps_growth_pct > 0:
                await _try_fill_input(page, str(min_eps_growth_pct), selectors=[
                    '[data-filter="eps"]',
                    'input[name*="eps" i]',
                    '[placeholder*="eps" i]',
                ])

            # --- set revenue growth filter ---
            if min_revenue_growth_pct > 0:
                await _try_fill_input(page, str(min_revenue_growth_pct), selectors=[
                    '[data-filter="revenue"]',
                    'input[name*="revenue" i]',
                    '[placeholder*="revenue" i]',
                    '[placeholder*="sales" i]',
                ])

            # submit / trigger search
            await _try_click(page, selectors=[
                'button[type="submit"]',
                'button:has-text("Scan")',
                'button:has-text("Search")',
                'button:has-text("Filter")',
                'button:has-text("Apply")',
            ])

            await page.wait_for_load_state("networkidle", timeout=20_000)
            await asyncio.sleep(1.5)

            # extract from intercepted API calls
            all_stocks: list[StockData] = []
            for c in captured:
                stocks = _parse_stocks(c["data"], c["url"])
                if stocks:
                    all_stocks.extend(stocks)
                    break

            # fallback: DOM scrape
            if not all_stocks:
                all_stocks = await _scrape_table(page)

        finally:
            await page.close()

        return _apply_filters(
            all_stocks,
            sectors=sectors,
            min_eps_growth_pct=min_eps_growth_pct,
            min_revenue_growth_pct=min_revenue_growth_pct,
            min_volume_20d_ma=min_volume_20d_ma,
            exclude_circuit=exclude_circuit,
        )

    async def get_stock_info(self, ticker: str) -> Optional[StockData]:
        """Fetch the stock-info page for a single ticker."""
        nd, calls = await self._load(f"{self.BASE}/stock-info/{ticker}")
        for c in calls:
            s = _parse_single_stock(c["data"], ticker)
            if s:
                return s
        if nd:
            return _stock_from_next_data(nd, ticker)
        return None

    async def discover_api_calls(self, url: str) -> list[dict]:
        """
        Load *url* and return every JSON API call observed.
        Useful for understanding ChartsMaze's internal API structure.
        """
        _, calls = await self._load(url)
        return [{"url": c["url"], "keys": list(c["data"].keys()) if isinstance(c["data"], dict) else type(c["data"]).__name__} for c in calls]

    # ------------------------------------------------------------------ internals

    async def _load(
        self,
        url: str,
        wait_for: Optional[str] = None,
        timeout: int = 35_000,
    ) -> tuple[Any, list[dict]]:
        """Load *url*, return (next_data, captured_json_responses)."""
        page: Page = await self._ctx.new_page()
        captured: list[dict] = []

        async def _on_response(r: Response) -> None:
            if "application/json" in r.headers.get("content-type", ""):
                try:
                    captured.append({"url": r.url, "data": await r.json()})
                except Exception:
                    pass

        page.on("response", _on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            await page.wait_for_load_state("networkidle", timeout=timeout)
            if wait_for:
                try:
                    await page.wait_for_selector(wait_for, timeout=8_000)
                except Exception:
                    pass

            next_data: Any = await page.evaluate(
                """() => {
                    const el = document.getElementById('__NEXT_DATA__');
                    if (!el) return null;
                    try { return JSON.parse(el.textContent); } catch { return null; }
                }"""
            )
        finally:
            await page.close()

        return next_data, captured


# ============================================================ parsing helpers

def _flt(d: dict, keys: list[str]) -> Optional[float]:
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(str(v).replace(",", "").replace("%", "").strip())
            except (ValueError, TypeError):
                pass
    return None


def _int(d: dict, keys: list[str]) -> Optional[int]:
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return int(float(str(v).replace(",", "").strip()))
            except (ValueError, TypeError):
                pass
    return None


def _items(data: Any, *candidate_keys: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in candidate_keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _quadrant_from_rs(rs_ratio: Optional[float], rs_momentum: Optional[float]) -> Optional[RRGQuadrant]:
    if rs_ratio is None or rs_momentum is None:
        return None
    if rs_ratio > 100 and rs_momentum > 100:
        return RRGQuadrant.LEADING
    if rs_ratio > 100:
        return RRGQuadrant.WEAKENING
    if rs_momentum <= 100 and rs_ratio <= 100:
        return RRGQuadrant.LAGGING
    return RRGQuadrant.IMPROVING


def _parse_sectors(data: Any, url: str) -> list[SectorData]:
    rows = _items(data, "sectors", "sectorData", "data", "result", "rows")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = r.get("sector") or r.get("sectorName") or r.get("name") or r.get("title")
        if not name:
            continue
        perf_1d = _flt(r, ["change_pct", "change", "perf_1d", "1d", "dayChange", "performance"])
        perf_5d = _flt(r, ["perf_5d", "5d", "weekChange"])
        perf_1m = _flt(r, ["perf_1m", "1m", "monthChange"])
        rs_ratio = _flt(r, ["rs_ratio", "rsRatio", "RS_Ratio", "x"])
        rs_momentum = _flt(r, ["rs_momentum", "rsMomentum", "RS_Momentum", "y"])
        raw_q = r.get("quadrant") or r.get("rrg_quadrant")
        quadrant = (
            RRGQuadrant(raw_q)
            if raw_q in _RRG_QUADRANT_VALUES
            else _quadrant_from_rs(rs_ratio, rs_momentum)
        )
        out.append(SectorData(
            name=str(name),
            performance_1d=perf_1d,
            performance_5d=perf_5d,
            performance_1m=perf_1m,
            quadrant=quadrant,
            rs_ratio=rs_ratio,
            rs_momentum=rs_momentum,
            stock_count=_int(r, ["count", "stockCount", "stocks"]),
        ))
    return out


def _parse_industries(data: Any, url: str, sector: str) -> list[IndustryData]:
    rows = _items(data, "industries", "industryData", "data", "result")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        item_sector = r.get("sector") or r.get("sectorName") or ""
        if sector and item_sector and sector.lower() not in item_sector.lower():
            continue
        name = r.get("industry") or r.get("industryName") or r.get("name")
        if not name:
            continue
        rs_ratio = _flt(r, ["rs_ratio", "rsRatio", "x"])
        rs_momentum = _flt(r, ["rs_momentum", "rsMomentum", "y"])
        raw_q = r.get("quadrant") or r.get("rrg_quadrant")
        quadrant = (
            RRGQuadrant(raw_q)
            if raw_q in _RRG_QUADRANT_VALUES
            else _quadrant_from_rs(rs_ratio, rs_momentum)
        )
        out.append(IndustryData(
            name=str(name),
            sector=item_sector or sector,
            performance_1d=_flt(r, ["change_pct", "change", "perf_1d", "1d"]),
            performance_5d=_flt(r, ["perf_5d", "5d"]),
            quadrant=quadrant,
            rs_ratio=rs_ratio,
            rs_momentum=rs_momentum,
        ))
    return out


def _parse_rrg(data: Any, url: str) -> list[dict]:
    rows = _items(data, "rrg", "rrgData", "data", "result", "sectors", "industries")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = r.get("name") or r.get("sector") or r.get("industry") or r.get("symbol")
        if not name:
            continue
        rs_ratio = _flt(r, ["rs_ratio", "rsRatio", "RS_Ratio", "x"])
        rs_momentum = _flt(r, ["rs_momentum", "rsMomentum", "RS_Momentum", "y"])
        raw_q = r.get("quadrant") or r.get("rrg_quadrant")
        quadrant = (
            raw_q
            if raw_q in _RRG_QUADRANT_VALUES
            else (_quadrant_from_rs(rs_ratio, rs_momentum).value if _quadrant_from_rs(rs_ratio, rs_momentum) else None)
        )
        out.append({"name": str(name), "rs_ratio": rs_ratio, "rs_momentum": rs_momentum, "quadrant": quadrant})
    return out


def _parse_stocks(data: Any, url: str) -> list[StockData]:
    rows = _items(data, "stocks", "data", "result", "rows", "screenerData", "scanResult")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ticker = (
            r.get("symbol") or r.get("ticker") or r.get("Symbol")
            or r.get("scrip") or r.get("NSE_Symbol") or r.get("nse_symbol")
        )
        if not ticker:
            continue
        circuit = _circuit_status(r)
        out.append(StockData(
            ticker=str(ticker).strip(),
            name=r.get("company") or r.get("companyName") or r.get("name"),
            sector=r.get("sector") or r.get("Sector"),
            industry=r.get("industry") or r.get("Industry"),
            price=_flt(r, ["price", "ltp", "lastPrice", "close", "CMP", "Last"]),
            change_pct=_flt(r, ["change_pct", "changePct", "pChange", "change", "Change"]),
            volume=_int(r, ["volume", "vol", "totalVolume", "Volume"]),
            volume_20d_ma=_int(r, ["volume_20d_ma", "avg_volume_20d", "avgVolume20", "vol20dma", "avgVol20d"]),
            circuit_status=circuit,
            eps_growth_pct=_flt(r, ["eps_growth", "epsGrowth", "eps_growth_pct", "EPSGrowth"]),
            revenue_growth_pct=_flt(r, ["revenue_growth", "revenueGrowth", "sales_growth", "SalesGrowth"]),
            market_cap=_flt(r, ["market_cap", "marketCap", "mktCap"]),
            pe_ratio=_flt(r, ["pe_ratio", "pe", "PE"]),
        ))
    return out


def _parse_single_stock(data: Any, ticker: str) -> Optional[StockData]:
    if not isinstance(data, dict):
        return None
    sym = data.get("symbol") or data.get("ticker") or data.get("scrip")
    if not sym and not any(k in data for k in ("price", "ltp", "lastPrice", "volume", "close")):
        return None
    circuit = _circuit_status(data)
    return StockData(
        ticker=str(sym or ticker),
        name=data.get("company") or data.get("companyName") or data.get("name"),
        sector=data.get("sector"),
        industry=data.get("industry"),
        price=_flt(data, ["price", "ltp", "lastPrice", "close", "CMP"]),
        change_pct=_flt(data, ["change_pct", "changePct", "pChange"]),
        volume=_int(data, ["volume", "vol", "totalVolume"]),
        volume_20d_ma=_int(data, ["volume_20d_ma", "avg_volume_20d", "avgVolume20"]),
        circuit_status=circuit,
        eps_growth_pct=_flt(data, ["eps_growth", "epsGrowth"]),
        revenue_growth_pct=_flt(data, ["revenue_growth", "revenueGrowth", "salesGrowth"]),
        market_cap=_flt(data, ["market_cap", "marketCap", "mktCap"]),
        pe_ratio=_flt(data, ["pe_ratio", "pe", "PE"]),
    )


def _circuit_status(r: dict) -> Optional[str]:
    raw = r.get("circuit") or r.get("circuit_status") or r.get("circuitStatus") or r.get("Circuit")
    if raw is None:
        return None
    s = str(raw).lower()
    if "upper" in s or s in ("uc", "u"):
        return "Upper"
    if "lower" in s or s in ("lc", "l"):
        return "Lower"
    if raw in (True, 1, "true", "yes", "1"):
        return "Upper"
    return None


# ---- __NEXT_DATA__ extraction

def _page_props(nd: dict) -> dict:
    return nd.get("props", {}).get("pageProps", {})


def _sectors_from_next_data(nd: dict) -> list[SectorData]:
    props = _page_props(nd)
    for k in ("sectors", "sectorData", "marketData", "data"):
        if k in props:
            r = _parse_sectors(props[k], "next_data")
            if r:
                return r
    return []


def _industries_from_next_data(nd: dict, sector: str) -> list[IndustryData]:
    props = _page_props(nd)
    for k in ("industries", "industryData", "data"):
        if k in props:
            r = _parse_industries(props[k], "next_data", sector)
            if r:
                return r
    return []


def _rrg_from_next_data(nd: dict) -> list[dict]:
    props = _page_props(nd)
    for k in ("rrg", "rrgData", "rotationData", "data"):
        if k in props:
            r = _parse_rrg(props[k], "next_data")
            if r:
                return r
    return []


def _stock_from_next_data(nd: dict, ticker: str) -> Optional[StockData]:
    props = _page_props(nd)
    for k in ("stock", "stockData", "stockInfo", "data"):
        if k in props:
            s = _parse_single_stock(props[k], ticker)
            if s:
                return s
    return None


# ---- DOM fallback

async def _scrape_table(page: Page) -> list[StockData]:
    stocks: list[StockData] = []
    try:
        rows = await page.locator("table tbody tr").all()
        if not rows:
            rows = await page.locator("[class*='stock-row'], [class*='stockRow'], [class*='scan-row']").all()
        for row in rows:
            cells = await row.locator("td").all()
            texts = [await c.inner_text() for c in cells]
            if len(texts) < 2:
                continue
            ticker = texts[0].strip()
            if not ticker or len(ticker) > 20 or not ticker[0].isalpha():
                continue
            s = StockData(
                ticker=ticker,
                name=texts[1].strip() if len(texts) > 1 else None,
                price=_safe_flt(texts[2]) if len(texts) > 2 else None,
                change_pct=_safe_flt(texts[3]) if len(texts) > 3 else None,
                volume=_safe_int(texts[4]) if len(texts) > 4 else None,
                volume_20d_ma=_safe_int(texts[5]) if len(texts) > 5 else None,
            )
            stocks.append(s)
    except Exception as e:
        logger.warning("DOM table scrape failed: %s", e)
    return stocks


def _safe_flt(s: str) -> Optional[float]:
    try:
        return float(re.sub(r"[,%₹$]", "", s).strip())
    except (ValueError, TypeError):
        return None


def _safe_int(s: str) -> Optional[int]:
    try:
        return int(float(re.sub(r"[,%₹$K]", "", s).strip()) * (1000 if s.strip().upper().endswith("K") else 1))
    except (ValueError, TypeError):
        return None


# ---- filter logic

def _apply_filters(
    stocks: list[StockData],
    sectors: list[str],
    min_eps_growth_pct: float,
    min_revenue_growth_pct: float,
    min_volume_20d_ma: int,
    exclude_circuit: bool,
) -> list[StockData]:
    out = []
    for s in stocks:
        if s.volume_20d_ma is not None and s.volume_20d_ma < min_volume_20d_ma:
            continue
        if exclude_circuit and s.circuit_status in ("Upper", "Lower"):
            continue
        if min_eps_growth_pct > 0 and (s.eps_growth_pct is None or s.eps_growth_pct < min_eps_growth_pct):
            continue
        if min_revenue_growth_pct > 0 and (s.revenue_growth_pct is None or s.revenue_growth_pct < min_revenue_growth_pct):
            continue
        if sectors and s.sector and not any(sec.lower() in s.sector.lower() for sec in sectors):
            continue
        out.append(s)
    return out


# ---- UI interaction helpers

async def _try_set_filter(page: Page, value: str, selectors: list[str]) -> None:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() == 0:
                continue
            tag = await el.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                await el.select_option(label=value)
            else:
                await el.click()
                await page.keyboard.type(value)
                # choose first autocomplete option
                opt = page.locator(f"[role='option']:has-text('{value}'), li:has-text('{value}')").first
                if await opt.count() > 0:
                    await opt.click()
                else:
                    await page.keyboard.press("Enter")
            await asyncio.sleep(0.4)
            return
        except Exception as e:
            logger.debug("_try_set_filter(%s) failed: %s", sel, e)


async def _try_fill_input(page: Page, value: str, selectors: list[str]) -> None:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() == 0:
                continue
            await el.fill(value)
            await asyncio.sleep(0.3)
            return
        except Exception as e:
            logger.debug("_try_fill_input(%s) failed: %s", sel, e)


async def _try_click(page: Page, selectors: list[str]) -> None:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() == 0:
                continue
            await el.click()
            return
        except Exception as e:
            logger.debug("_try_click(%s) failed: %s", sel, e)
