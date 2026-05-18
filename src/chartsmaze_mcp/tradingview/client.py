"""
TradingView watchlist client.

Uses Playwright to discover the live watchlist API endpoints by intercepting
the requests TradingView makes when it loads.  Once discovered, the actual
CRUD calls are made directly with httpx (no browser needed).

Authentication: set TRADINGVIEW_SESSION (and optionally TRADINGVIEW_SESSION_SIGN)
from the cookies in your TradingView browser session.

  devtools → Application → Cookies → tradingview.com
    • sessionid        → TRADINGVIEW_SESSION
    • sessionid_sign   → TRADINGVIEW_SESSION_SIGN  (optional but avoids 2FA prompts)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import httpx
from playwright.async_api import Browser, BrowserContext, async_playwright

logger = logging.getLogger(__name__)

_TV_BASE   = "https://www.tradingview.com"
_TV_ORIGIN = _TV_BASE
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Matches the base symbols_list path on any TradingView subdomain, e.g.:
#   https://in.tradingview.com/api/v1/symbols_list/all/?source=web
#   https://www.tradingview.com/api/v2/symbols_lists/custom/
# Captures group 1 = everything up to and including "symbols_list(s)/",
# and group 2 = the optional sub-bucket (all | custom | colored | …)
_SYMBOLS_LIST_RE = re.compile(
    r"(https://[a-z]+\.tradingview\.com(?:/api/v\d+)?/symbols?_lists?/)"
    r"([a-z]*/)?",
    re.IGNORECASE,
)


def _crud_base(list_url: str) -> str:
    """
    Strip the sub-bucket suffix so we get the plain CRUD base, e.g.:
      https://in.tradingview.com/api/v1/symbols_list/all/  →
      https://in.tradingview.com/api/v1/symbols_list/
    """
    m = _SYMBOLS_LIST_RE.match(list_url)
    if m:
        return m.group(1)
    # Fallback: strip last path segment
    url = list_url.split("?")[0].rstrip("/")
    return url.rsplit("/", 1)[0] + "/"


def _origin_from_url(url: str) -> str:
    """https://in.tradingview.com/... → https://in.tradingview.com"""
    m = re.match(r"(https://[^/]+)", url)
    return m.group(1) if m else _TV_BASE


class TradingViewClient:
    def __init__(self, session_id: str, session_sign: Optional[str] = None):
        self._session_id   = session_id
        self._session_sign = session_sign
        self._pw           = None
        self._browser: Optional[Browser]          = None
        self._ctx:     Optional[BrowserContext]   = None
        self._http:    Optional[httpx.AsyncClient] = None
        # Populated by _discover_list_url():
        self._list_url: Optional[str] = None   # URL for GET-all (may end with /all/)
        self._crud_url: Optional[str] = None   # Base for create / item ops

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> "TradingViewClient":
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
        # httpx client is created with placeholder headers; Origin/Referer are
        # updated after endpoint discovery so they match the real subdomain.
        self._http = self._make_http_client(_TV_ORIGIN)
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

    async def discover_api_calls(self, url: str) -> list[dict]:
        """
        Load *url* in a headless browser (with your session cookie) and return
        every XHR/fetch request TradingView makes.  Useful for debugging which
        endpoints are live and finding the correct watchlist path.
        """
        assert self._ctx is not None
        calls: list[dict] = []
        page = await self._ctx.new_page()

        async def on_response(resp: Any) -> None:
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            calls.append({
                "method":       req.method,
                "url":          req.url,
                "status":       resp.status,
                "content_type": resp.headers.get("content-type", ""),
            })

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="networkidle", timeout=45_000)
        except Exception as exc:
            raise RuntimeError(f"Could not load {url}: {exc}") from exc
        finally:
            await page.close()

        return calls

    async def _discover_list_url(self) -> str:
        if self._list_url:
            return self._list_url

        calls = await self.discover_api_calls(_TV_BASE + "/")

        # Look for GET requests matching the symbols_list pattern on any subdomain.
        matched: list[str] = []
        for c in calls:
            if c["method"] != "GET":
                continue
            if _SYMBOLS_LIST_RE.search(c["url"]):
                matched.append(c["url"].split("?")[0])

        if not matched:
            captured = [f"  {c['method']} {c['url']}" for c in calls]
            hint = "\n".join(captured[:30]) if captured else "  (none)"
            raise RuntimeError(
                "Could not discover TradingView watchlist API endpoint.\n\n"
                "Captured API calls:\n" + hint + "\n\n"
                "Possible causes:\n"
                "  • TRADINGVIEW_SESSION is expired — re-copy from DevTools →\n"
                "    Application → Cookies → tradingview.com → sessionid.\n"
                "  • Watchlist URL pattern changed — run:\n"
                "    chartsmaze tv-discover https://www.tradingview.com\n"
                "    and look for the symbols_list GET call, then open an issue."
            )

        # Use the URL seen most often; prefer /all/ over /custom/ or /colored/.
        def _rank(u: str) -> int:
            return matched.count(u) * 10 + (5 if "all" in u else 1)

        best = max(set(matched), key=_rank)
        self._list_url = best.rstrip("/") + "/"
        self._crud_url = _crud_base(self._list_url)

        # Re-initialise httpx with the correct Origin/Referer for this subdomain.
        origin = _origin_from_url(self._list_url)
        if origin != _TV_ORIGIN and self._http:
            await self._http.aclose()
            self._http = self._make_http_client(origin)

        logger.debug("list_url=%s  crud_url=%s", self._list_url, self._crud_url)
        return self._list_url

    # ------------------------------------------------------------------ helpers

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._http is not None
        try:
            resp = await self._http.request(method, url, **kwargs)
        except httpx.ConnectError as exc:
            raise RuntimeError(
                f"Cannot reach TradingView ({url}). "
                "Check your internet connection."
            ) from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"Request to TradingView timed out: {url}") from exc

        if resp.status_code in (401, 403):
            raise PermissionError(
                f"TradingView authentication failed (HTTP {resp.status_code}). "
                "Your TRADINGVIEW_SESSION cookie may have expired — "
                "re-copy it from DevTools → Application → Cookies → "
                "tradingview.com → sessionid."
            )
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------ public API

    async def list_watchlists(self) -> list[dict]:
        """Return all watchlists for the authenticated user."""
        url  = await self._discover_list_url()
        resp = await self._request("GET", url)
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("lists", data.get("watchlists", data.get("data", [])))

    async def get_watchlist(self, watchlist_id: str) -> dict:
        """Return a single watchlist by ID (includes its symbol list)."""
        await self._discover_list_url()
        url  = f"{self._crud_url}{watchlist_id}/"
        resp = await self._request("GET", url, params={"populate_data": "false"})
        return resp.json()

    async def create_watchlist(self, name: str, symbols: list[str]) -> dict:
        """Create a new watchlist."""
        await self._discover_list_url()
        resp = await self._request(
            "POST", self._crud_url, json={"name": name, "symbols": symbols}
        )
        return resp.json()

    async def update_watchlist(self, watchlist_id: str, name: str, symbols: list[str]) -> dict:
        """Replace a watchlist's name and symbol list."""
        await self._discover_list_url()
        url  = f"{self._crud_url}{watchlist_id}/"
        resp = await self._request("PUT", url, json={"name": name, "symbols": symbols})
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

        When *replace=False* (default) existing symbols are kept and new ones
        are appended (deduplication preserves insertion order).
        """
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
        full    = await self.get_watchlist(wl_id)
        current: list[str] = full.get("symbols", [])

        if replace:
            merged = symbols
        else:
            seen: dict[str, None] = dict.fromkeys(current)
            for s in symbols:
                seen[s] = None
            merged = list(seen.keys())

        added = [s for s in symbols if s not in set(current)]
        await self.update_watchlist(wl_id, watchlist_name, merged)
        return {
            "status":         "updated",
            "watchlist_name": watchlist_name,
            "watchlist_id":   wl_id,
            "symbols_added":  added,
            "total_symbols":  len(merged),
        }
