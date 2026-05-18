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

import asyncio
import logging
import re
import sys
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

# Matches any TradingView watchlist/symbols_list collection endpoint, e.g.:
#   https://in.tradingview.com/api/v1/symbols_list/all/?source=web
#   https://in.tradingview.com/api/v1/watchlists/
#   https://in.tradingview.com/api/v1/watchlists/?source=web
# Captures group 1 = base URL up to and including the collection segment.
_LIST_URL_RE = re.compile(
    r"(https://[a-z]+\.tradingview\.com(?:/api/v\d+)?"
    r"/(?:symbols?_lists?|watchlists?)/)",
    re.IGNORECASE,
)

# Sub-bucket suffixes that appear on the symbols_list READ endpoints but
# should be stripped when deriving the CRUD base URL.
_LIST_BUCKETS = {"all", "custom", "colored"}


def _crud_base(list_url: str) -> str:
    """
    Derive the plain CRUD base from a discovered list URL, e.g.:
      https://in.tradingview.com/api/v1/symbols_list/all/  →  …/symbols_list/
      https://in.tradingview.com/api/v1/watchlists/        →  …/watchlists/
    """
    url = list_url.split("?")[0].rstrip("/")
    last = url.rsplit("/", 1)[-1]
    if last in _LIST_BUCKETS:
        url = url.rsplit("/", 1)[0]
    return url + "/"


def _origin_from_url(url: str) -> str:
    """https://in.tradingview.com/... → https://in.tradingview.com"""
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
        # Populated by _discover_list_url():
        self._list_url: Optional[str] = None   # URL for GET-all (may end with /all/)
        self._crud_url: Optional[str] = None   # Base for create / item ops

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

    async def discover_api_calls(
        self, url: str, interactive: bool = False
    ) -> list[dict]:
        """
        Load *url* in a browser (with your session cookie) and return every
        XHR/fetch call made.

        *interactive=True*: opens a **visible** browser window and waits for
        you to press Enter before capturing stops.  Use this to capture write
        operations (e.g. editing a watchlist) that don't happen on page load.
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
            if interactive:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                print(
                    "\n[tv-discover] Browser is open and capturing all API calls.\n"
                    "  → Go to your watchlist, add or remove ONE symbol, then\n"
                    "    come back here and press Enter to stop capture.",
                    file=sys.stderr,
                )
                # run input() in a thread so the asyncio loop keeps processing events
                await asyncio.get_event_loop().run_in_executor(None, input)
            else:
                await page.goto(url, wait_until="networkidle", timeout=45_000)
        except Exception as exc:
            raise RuntimeError(f"Could not load {url}: {exc}") from exc
        finally:
            await page.close()

        return calls

    async def _discover_list_url(self) -> str:
        if self._list_url:
            return self._list_url

        # Try the homepage first, then the watchlists page which triggers the
        # watchlists API call directly.
        all_calls: list[dict] = []
        for page_path in ("/", "/watchlists/"):
            calls = await self.discover_api_calls(_TV_BASE + page_path)
            all_calls.extend(calls)
            matched = [
                c["url"].split("?")[0]
                for c in calls
                if c["method"] == "GET" and _LIST_URL_RE.search(c["url"])
            ]
            # Prefer watchlists/ over symbols_list/ when both are present.
            watchlist_hits = [u for u in matched if "watchlist" in u.lower()]
            if watchlist_hits:
                matched = watchlist_hits
            if matched:
                break
        else:
            matched = []

        if not matched:
            captured = [f"  {c['method']} {c['url']}" for c in all_calls]
            hint = "\n".join(captured[:30]) if captured else "  (none)"
            raise RuntimeError(
                "Could not discover TradingView watchlist API endpoint.\n\n"
                "Captured API calls:\n" + hint + "\n\n"
                "Possible causes:\n"
                "  • TRADINGVIEW_SESSION is expired — re-copy from DevTools →\n"
                "    Application → Cookies → tradingview.com → sessionid.\n"
                "  • Watchlist URL pattern changed — run:\n"
                "    chartsmaze tv-discover https://www.tradingview.com\n"
                "    and look for the watchlists GET call, then open an issue."
            )

        best = max(set(matched), key=matched.count)
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

    async def _browser_write(self, candidates: list[tuple[str, str, dict]]) -> dict:
        """
        Try each (method, url, body) candidate by executing fetch() from inside
        the live TradingView page so that session cookies and CSRF tokens are
        attached automatically.  Returns the JSON response of the first 2xx.
        Skips candidates that return 404 or 405; surfaces any other error.
        """
        origin = _origin_from_url(self._list_url or _TV_BASE)
        assert self._ctx is not None
        page = await self._ctx.new_page()
        try:
            await page.goto(origin + "/", wait_until="domcontentloaded", timeout=30_000)
            result = await page.evaluate(
                """async (candidates) => {
                    const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
                    for (const [method, url, body] of candidates) {
                        let r;
                        try {
                            r = await fetch(url, {
                                method,
                                credentials: 'include',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'X-CSRFToken': csrf,
                                },
                                body: JSON.stringify(body),
                            });
                        } catch (e) { continue; }
                        const text = await r.text();
                        let data;
                        try { data = JSON.parse(text); } catch { data = {_raw: text}; }
                        if (r.ok) return {ok: true, status: r.status, method, url, data};
                        if (r.status !== 404 && r.status !== 405)
                            return {ok: false, status: r.status, method, url, data};
                    }
                    return null;
                }""",
                candidates,
            )
        finally:
            await page.close()

        if result is None:
            tried = ", ".join(f"{m} {u}" for m, u, _ in candidates)
            raise RuntimeError(
                f"No write endpoint accepted the request (tried: {tried}).\n"
                "Run 'chartsmaze tv-discover https://www.tradingview.com' while\n"
                "manually editing a TradingView watchlist to find the real endpoint."
            )
        if not result["ok"]:
            status = result["status"]
            if status in (401, 403):
                raise PermissionError(
                    f"TradingView auth failed (HTTP {status}). "
                    "Your TRADINGVIEW_SESSION cookie may be expired."
                )
            raise RuntimeError(
                f"TradingView returned HTTP {status} for {result['method']} {result['url']}:\n"
                f"{result['data']}"
            )
        logger.debug("write succeeded: %s %s", result["method"], result["url"])
        return result["data"]

    async def create_watchlist(self, name: str, symbols: list[str]) -> dict:
        """Create a new watchlist."""
        await self._discover_list_url()
        origin  = _origin_from_url(self._list_url or _TV_BASE)
        payload = {"name": name, "symbols": symbols}
        return await self._browser_write([
            ("POST", self._crud_url,                        payload),
            ("POST", f"{origin}/api/v1/watchlists/",        payload),
            ("POST", f"{origin}/api/v1/symbols_list/",      payload),
        ])

    async def update_watchlist(self, watchlist_id: str, name: str, symbols: list[str]) -> dict:
        """Replace a watchlist's name and symbol list."""
        await self._discover_list_url()
        base    = self._crud_url
        origin  = _origin_from_url(self._list_url or _TV_BASE)
        payload = {"name": name, "symbols": symbols}
        # Build candidates: use discovered crud_url first, then explicit
        # watchlists/ and symbols_list/ paths derived from the known origin.
        wl_base   = f"{origin}/api/v1/watchlists/"
        sl_base   = f"{origin}/api/v1/symbols_list/"
        item_urls = {f"{base}{watchlist_id}/", f"{wl_base}{watchlist_id}/", f"{sl_base}{watchlist_id}/"}
        candidates = [
            (method, url, payload)
            for url in [
                f"{base}{watchlist_id}/",
                f"{wl_base}{watchlist_id}/",
                f"{sl_base}{watchlist_id}/",
            ]
            for method in ("PATCH", "PUT")
        ] + [
            ("POST", base,    {**payload, "id": watchlist_id}),
            ("POST", wl_base, {**payload, "id": watchlist_id}),
            ("POST", sl_base, {**payload, "id": watchlist_id}),
        ]
        # Deduplicate while preserving order.
        seen: set[tuple] = set()
        unique = []
        for c in candidates:
            key = (c[0], c[1])
            if key not in seen:
                seen.add(key)
                unique.append(c)
        return await self._browser_write(unique)

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
        # The list response already includes the symbol array — no separate GET needed.
        current: list[str] = existing.get("symbols", [])

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
