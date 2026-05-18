"""
TradingView watchlist client.

API endpoints discovered empirically via tv-discover --interactive:
  List all:   GET  {origin}/api/v1/symbols_list/all/?source=web
  List custom:GET  {origin}/api/v1/symbols_list/custom/?source=web
  Get item:   GET  {origin}/api/v1/symbols_list/custom/{id}?source=web
  Create:     POST {origin}/api/v1/symbols_list/custom/?source=web
                   body: {"name": "...", "symbols": ["NSE:RELIANCE", ...]}
  Append:     POST {origin}/api/v1/symbols_list/custom/{id}/append/?source=web
                   body: {"symbols": ["NSE:TCS", ...]}

The regional subdomain (www vs in vs ...) is discovered automatically by
loading tradingview.com and observing which host handles the /api/v1/ calls.

Authentication: set TRADINGVIEW_SESSION (and optionally TRADINGVIEW_SESSION_SIGN)
from the cookies in your TradingView browser session.

  devtools → Application → Cookies → tradingview.com
    • sessionid        → TRADINGVIEW_SESSION
    • sessionid_sign   → TRADINGVIEW_SESSION_SIGN
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from typing import Any, Optional

import httpx
from playwright.async_api import Browser, BrowserContext, async_playwright

logger = logging.getLogger(__name__)

_TV_BASE = "https://www.tradingview.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _origin_from_url(url: str) -> str:
    m = re.match(r"(https://[^/]+)", url)
    return m.group(1) if m else _TV_BASE


class TradingViewClient:
    def __init__(
        self,
        session_id: str,
        session_sign: Optional[str] = None,
        headless: bool = True,
    ):
        self._session_id   = session_id
        self._session_sign = session_sign
        self._headless     = headless
        self._pw           = None
        self._browser: Optional[Browser]          = None
        self._ctx:     Optional[BrowserContext]   = None
        self._http:    Optional[httpx.AsyncClient] = None
        self._origin:  Optional[str]              = None  # e.g. https://in.tradingview.com

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> "TradingViewClient":
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=self._headless)
        except Exception as exc:
            await self._pw.stop()
            raise RuntimeError(
                "Chromium not found. Run: python -m playwright install chromium"
            ) from exc
        self._ctx = await self._browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 900},
        )
        await self._ctx.add_cookies([
            {
                "name":   "sessionid",
                "value":  self._session_id,
                "domain": ".tradingview.com",
                "path":   "/",
            },
            *([{
                "name":   "sessionid_sign",
                "value":  self._session_sign,
                "domain": ".tradingview.com",
                "path":   "/",
            }] if self._session_sign else []),
        ])
        self._http = self._make_http_client(_TV_BASE)
        return self

    def _make_http_client(self, origin: str) -> httpx.AsyncClient:
        cookie = f"sessionid={self._session_id}"
        if self._session_sign:
            cookie += f"; sessionid_sign={self._session_sign}"
        return httpx.AsyncClient(
            headers={
                "Cookie":       cookie,
                "Origin":       origin,
                "Referer":      origin + "/",
                "User-Agent":   _UA,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def __aexit__(self, *_: Any) -> None:
        if self._http:
            await self._http.aclose()
        if self._ctx:
            await self._ctx.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ------------------------------------------------------------------ discovery

    async def discover_api_calls(
        self, url: str, interactive: bool = False
    ) -> list[dict]:
        """
        Load *url* in a browser (with your session cookie) and return every
        XHR/fetch call made.

        *interactive=True*: opens a visible browser window and waits for you
        to press Enter before capture stops — use this to observe write operations.
        """
        assert self._ctx is not None
        calls: list[dict] = []
        page = await self._ctx.new_page()

        async def on_response(resp: Any) -> None:
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            try:
                body = req.post_data
            except Exception:
                body = None
            calls.append({
                "method":        req.method,
                "url":           req.url,
                "request_body":  body,
                "status":        resp.status,
                "content_type":  resp.headers.get("content-type", ""),
            })

        page.on("response", on_response)
        try:
            if interactive:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                print(
                    "\n[tv-discover] Browser is open and capturing all API calls.\n"
                    "  → Go to your watchlist, add or remove ONE symbol, then\n"
                    "    come back here and press Enter to stop capture.",
                    file=sys.stderr,
                )
                await asyncio.get_event_loop().run_in_executor(None, input)
            else:
                await page.goto(url, wait_until="networkidle", timeout=45_000)
        except Exception as exc:
            raise RuntimeError(f"Could not load {url}: {exc}") from exc
        finally:
            await page.close()

        return calls

    async def _ensure_origin(self) -> str:
        """
        Discover the regional TradingView subdomain (e.g. in.tradingview.com)
        by loading the homepage and reading which host answers /api/v1/ calls.
        """
        if self._origin:
            return self._origin

        calls = await self.discover_api_calls(_TV_BASE + "/")
        for c in calls:
            m = re.match(r"(https://[a-z]+\.tradingview\.com)/api/", c["url"])
            if m:
                self._origin = m.group(1)
                break

        if not self._origin:
            self._origin = _TV_BASE
            logger.warning("Could not detect regional subdomain; using %s", _TV_BASE)

        if self._origin != _TV_BASE and self._http:
            await self._http.aclose()
            self._http = self._make_http_client(self._origin)

        logger.debug("origin=%s", self._origin)
        return self._origin

    # ------------------------------------------------------------------ helpers

    def _url(self, path: str) -> str:
        return f"{self._origin or _TV_BASE}{path}"

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._http is not None
        try:
            resp = await self._http.request(method, url, **kwargs)
        except httpx.ConnectError as exc:
            raise RuntimeError(
                f"Cannot reach TradingView ({url}). Check your internet connection."
            ) from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"Request to TradingView timed out: {url}") from exc

        if resp.status_code in (401, 403):
            raise PermissionError(
                f"TradingView auth failed (HTTP {resp.status_code}). "
                "Your TRADINGVIEW_SESSION cookie may be expired — re-copy from "
                "DevTools → Application → Cookies → tradingview.com → sessionid."
            )
        if not resp.is_success:
            try:
                body = resp.text[:500]
            except Exception:
                body = "(unreadable)"
            raise RuntimeError(
                f"TradingView {method} {url} → HTTP {resp.status_code}\n"
                f"Response body: {body}"
            )
        return resp

    # ------------------------------------------------------------------ public API

    async def list_watchlists(self) -> list[dict]:
        """Return all user watchlists."""
        await self._ensure_origin()
        resp = await self._request(
            "GET", self._url("/api/v1/symbols_list/all/"), params={"source": "web"}
        )
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("lists", data.get("data", []))

    async def create_watchlist(self, name: str, symbols: list[str]) -> dict:
        """Create a new watchlist with *symbols* pre-loaded."""
        await self._ensure_origin()
        resp = await self._request(
            "POST",
            self._url("/api/v1/symbols_list/custom/"),
            params={"source": "web"},
            json={"name": name, "symbols": symbols},
        )
        return resp.json()

    async def append_to_watchlist(self, watchlist_id: str, symbols: list[str]) -> dict:
        """Append *symbols* to an existing watchlist (duplicates ignored server-side)."""
        await self._ensure_origin()
        resp = await self._request(
            "POST",
            self._url(f"/api/v1/symbols_list/custom/{watchlist_id}/append/"),
            params={"source": "web"},
            json=symbols,  # bare array — API rejects {"symbols": [...]}
        )
        return resp.json()

    async def add_to_watchlist(
        self,
        symbols: list[str],
        watchlist_name: str,
        replace: bool = False,
    ) -> dict:
        """
        Add *symbols* to the named watchlist, creating it if absent.

        *symbols* must include the exchange prefix, e.g. ``["NSE:RELIANCE", "NSE:TCS"]``.

        *replace=False* (default): POST to /append/ — only genuinely new symbols
        are sent, existing ones are untouched.

        *replace=True*: not supported via the observed API; raises NotImplementedError.
        """
        if replace:
            raise NotImplementedError(
                "replace=True is not yet supported. "
                "TradingView's observed write API only exposes an /append/ endpoint. "
                "To start fresh, delete the watchlist manually in the TradingView UI "
                "and re-run — the workflow will recreate it."
            )

        all_wls  = await self.list_watchlists()
        existing = next((w for w in all_wls if w.get("name") == watchlist_name), None)

        if existing is None:
            await self.create_watchlist(watchlist_name, symbols)
            return {
                "status":         "created",
                "watchlist_name": watchlist_name,
                "symbols_added":  symbols,
                "total_symbols":  len(symbols),
            }

        wl_id   = existing.get("id") or existing.get("uuid")
        current: set[str] = set(existing.get("symbols", []))
        new_symbols = [s for s in symbols if s not in current]

        if new_symbols:
            await self.append_to_watchlist(wl_id, new_symbols)

        return {
            "status":         "updated",
            "watchlist_name": watchlist_name,
            "watchlist_id":   wl_id,
            "symbols_added":  new_symbols,
            "total_symbols":  len(current) + len(new_symbols),
        }
