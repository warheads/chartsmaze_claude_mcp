"""
TradingView watchlist client using the unofficial internal API.

Authentication: set TRADINGVIEW_SESSION (and optionally TRADINGVIEW_SESSION_SIGN)
from the cookies in your TradingView browser session.

  devtools → Application → Cookies → tradingview.com
    • sessionid        → TRADINGVIEW_SESSION
    • sessionid_sign   → TRADINGVIEW_SESSION_SIGN  (optional but avoids 2FA prompts)
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TV_API = "https://api.tradingview.com"
_TV_ORIGIN = "https://www.tradingview.com"


class TradingViewClient:
    def __init__(self, session_id: str, session_sign: Optional[str] = None):
        self._session_id = session_id
        self._session_sign = session_sign
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> "TradingViewClient":
        cookie = f"sessionid={self._session_id}"
        if self._session_sign:
            cookie += f"; sessionid_sign={self._session_sign}"
        self._client = httpx.AsyncClient(
            headers={
                "Cookie": cookie,
                "Origin": _TV_ORIGIN,
                "Referer": _TV_ORIGIN + "/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------ public API

    async def list_watchlists(self) -> list[dict]:
        """Return all watchlists for the authenticated user."""
        resp = await self._client.get(f"{_TV_API}/market-lists/v3/lists/")
        resp.raise_for_status()
        data = resp.json()
        # API may return a list directly or {"lists": [...]}
        if isinstance(data, list):
            return data
        return data.get("lists", data.get("data", []))

    async def get_watchlist(self, watchlist_id: str) -> dict:
        """Return a single watchlist by ID (includes its symbol list)."""
        resp = await self._client.get(
            f"{_TV_API}/market-lists/v3/list/{watchlist_id}/",
            params={"populate_data": "false"},
        )
        resp.raise_for_status()
        return resp.json()

    async def create_watchlist(self, name: str, symbols: list[str]) -> dict:
        """Create a new watchlist."""
        resp = await self._client.post(
            f"{_TV_API}/market-lists/v3/list/",
            json={"name": name, "symbols": symbols},
        )
        resp.raise_for_status()
        return resp.json()

    async def update_watchlist(self, watchlist_id: str, name: str, symbols: list[str]) -> dict:
        """Replace a watchlist's name and symbol list."""
        resp = await self._client.put(
            f"{_TV_API}/market-lists/v3/list/{watchlist_id}/",
            json={"name": name, "symbols": symbols},
        )
        resp.raise_for_status()
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
        all_wls = await self.list_watchlists()
        existing = next((w for w in all_wls if w.get("name") == watchlist_name), None)

        if existing is None:
            await self.create_watchlist(watchlist_name, symbols)
            return {
                "status": "created",
                "watchlist_name": watchlist_name,
                "symbols_added": symbols,
                "total_symbols": len(symbols),
            }

        wl_id = existing.get("id") or existing.get("uuid")
        # fetch full details to get current symbols
        full = await self.get_watchlist(wl_id)
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
            "status": "updated",
            "watchlist_name": watchlist_name,
            "watchlist_id": wl_id,
            "symbols_added": added,
            "total_symbols": len(merged),
        }
