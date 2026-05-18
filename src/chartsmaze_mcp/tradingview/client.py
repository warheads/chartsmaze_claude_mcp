"""
TradingView watchlist client.

Uses Playwright to discover the live watchlist API endpoints by intercepting
the requests TradingView makes when it loads your watchlists.  Once discovered
the actual CRUD calls are made directly with httpx (no browser needed).

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

# Regex that matches any TradingView URL that looks like a watchlist list endpoint,
# e.g. https://www.tradingview.com/api/v1/watchlists/
#       https://www.tradingview.com/market-lists/v2/lists/
# Capture group 1 = everything up to and including the trailing slash.
_LIST_URL_RE = re.compile(
    r"(https://(?:www\.)?tradingview\.com"
    r"/(?:[a-z0-9_-]+/)*"           # path segments
    r"(?:watchlists?|market-lists?|lists?)"
    r"/(?:v\d+/)?(?:lists?|watchlists?)/"
    r")",
    re.IGNORECASE,
)


class TradingViewClient:
    def __init__(self, session_id: str, session_sign: Optional[str] = None):
        self._session_id   = session_id
        self._session_sign = session_sign
        self._pw           = None
        self._browser: Optional[Browser]        = None
        self._ctx:     Optional[BrowserContext] = None
        self._http:    Optional[httpx.AsyncClient] = None
        # Discovered at runtime: URL that returns the list of watchlists.
        self._list_url: Optional[str] = None

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
            {"name": "sessionid", "value": self._session_id, "domain": ".tradingview.com", "path": "/"},
            *([{"name": "sessionid_sign", "value": self._session_sign, "domain": ".tradingview.com", "path": "/"}]
              if self._session_sign else []),
        ])
        cookie_header = f"sessionid={self._session_id}"
        if self._session_sign:
            cookie_header += f"; sessionid_sign={self._session_sign}"
        self._http = httpx.AsyncClient(
            headers={
                "Cookie":       cookie_header,
                "Origin":       _TV_ORIGIN,
                "Referer":      _TV_ORIGIN + "/",
                "User-Agent":   _UA,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        return self

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
        """
        Load tradingview.com in a headless browser and intercept the GET request
        that returns all watchlists.  Returns the full URL (with trailing slash).
        """
        if self._list_url:
            return self._list_url

        calls = await self.discover_api_calls(_TV_BASE + "/")

        # Filter to GET calls whose URL matches the watchlist pattern.
        matched = [
            c["url"] for c in calls
            if c["method"] == "GET" and _LIST_URL_RE.search(c["url"])
        ]

        if not matched:
            # Emit all captured URLs to help the user debug.
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
                "    and look for the 'lists' GET call, then open an issue."
            )

        # Normalise: strip query string, ensure trailing slash.
        url = matched[0].split("?")[0].rstrip("/") + "/"
        # Prefer the URL called most often (dedup redirects).
        url = max(set(matched), key=matched.count).split("?")[0].rstrip("/") + "/"
        self._list_url = url
        logger.debug("Discovered watchlist list URL: %s", self._list_url)
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

    def _item_url(self, list_url: str, item_id: str) -> str:
        """Derive the single-item URL from the list URL, e.g. …/lists/ → …/list/{id}/"""
        # list_url ends with /lists/ or /watchlists/ — swap for /list/{id}/
        base = re.sub(r"(?:lists?|watchlists?)/$", "", list_url, flags=re.IGNORECASE)
        segment = "watchlist" if "watchlist" in list_url.lower() else "list"
        return f"{base}{segment}/{item_id}/"

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
        list_url = await self._discover_list_url()
        url      = self._item_url(list_url, watchlist_id)
        resp = await self._request("GET", url, params={"populate_data": "false"})
        return resp.json()

    async def create_watchlist(self, name: str, symbols: list[str]) -> dict:
        """Create a new watchlist."""
        list_url = await self._discover_list_url()
        resp = await self._request(
            "POST", list_url, json={"name": name, "symbols": symbols}
        )
        return resp.json()

    async def update_watchlist(self, watchlist_id: str, name: str, symbols: list[str]) -> dict:
        """Replace a watchlist's name and symbol list."""
        list_url = await self._discover_list_url()
        url      = self._item_url(list_url, watchlist_id)
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
                "status":        "created",
                "watchlist_name": watchlist_name,
                "symbols_added": symbols,
                "total_symbols": len(symbols),
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
            "status":        "updated",
            "watchlist_name": watchlist_name,
            "watchlist_id":  wl_id,
            "symbols_added": added,
            "total_symbols": len(merged),
        }
