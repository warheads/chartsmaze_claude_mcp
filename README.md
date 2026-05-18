# ChartsMaze MCP Server

Automates the **sector → industry → stock** trading research workflow on
[chartsmaze.com](https://chartsmaze.com) and pushes the final shortlist to a
TradingView watchlist.

---

## Workflow automated

```
ChartsMaze Sector Analysis + RRG
        ↓ top 3 leading sectors
ChartsMaze Industry Analysis
        ↓ top industries per sector
ChartsMaze Custom Scanner
  • 20-day volume MA ≥ 50 000  (liquidity)
  • EPS growth ≥ threshold
  • Revenue growth ≥ threshold
  • Exclude upper / lower circuit stocks
        ↓ filtered tickers
TradingView Watchlist  ← ready for manual chart review
```

---

## Setup

### 1. Install dependencies

```bash
pip install -e .
playwright install chromium
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Where to find it |
|---|---|---|
| `TRADINGVIEW_SESSION` | **Yes** (for watchlist) | DevTools → Application → Cookies → `tradingview.com` → `sessionid` |
| `TRADINGVIEW_SESSION_SIGN` | No (recommended) | Same location → `sessionid_sign` |
| `CHARTSMAZE_SESSION` | No | Same approach on `chartsmaze.com` → `session` (only needed for paid features) |

### 3. Run the server

```bash
chartsmaze-mcp
# or
python -m chartsmaze_mcp.server
```

---

## Tools

| Tool | Purpose |
|---|---|
| `run_full_workflow` | **One-shot** — runs all steps and pushes to TradingView |
| `analyze_sectors` | Sectors ranked by RRG score (Leading quadrant first) |
| `analyze_industries` | Top industries within a sector |
| `get_rrg_leaders` | Sectors/industries in the Leading quadrant |
| `screen_stocks` | Custom scanner with volume, circuit, EPS, revenue filters |
| `get_stock_info` | Detailed data for a single ticker |
| `add_to_tradingview_watchlist` | Push a symbol list to TradingView |
| `discover_api_calls` | Debug helper — shows what API calls a ChartsMaze page makes |

---

## Claude Desktop config

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "chartsmaze": {
      "command": "chartsmaze-mcp",
      "env": {
        "TRADINGVIEW_SESSION": "<your-session-id>",
        "TRADINGVIEW_SESSION_SIGN": "<your-session-sign>"
      }
    }
  }
}
```

---

## How ChartsMaze scraping works

ChartsMaze is a React/Next.js SPA. The client uses **Playwright** to:

1. Load the page in a headless Chromium browser.
2. **Intercept all XHR/fetch JSON responses** — the cleanest source of data.
3. Fall back to parsing `window.__NEXT_DATA__` (Next.js server-rendered props).
4. Last resort: scrape visible table/card DOM elements.

If a page returns no data, run `discover_api_calls("https://chartsmaze.com/<page>")` to
see exactly which endpoints the page hits — you can then update the parser keys
in `src/chartsmaze_mcp/chartsmaze/client.py` to match.
