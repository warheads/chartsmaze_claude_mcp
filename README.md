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

### 1. Create and activate a virtual environment (recommended)

Using a virtual environment avoids the PATH issue where pip installs scripts
into a user-level folder that Windows can't find.

**Windows (Command Prompt)**

```bat
cd C:\Users\Admin\Documents\GitHub\chartsmaze_claude_mcp
python -m venv .venv
.venv\Scripts\activate
```

**Windows (PowerShell)**

```powershell
cd C:\Users\Admin\Documents\GitHub\chartsmaze_claude_mcp
python -m venv .venv
.venv\Scripts\Activate.ps1
```

> If PowerShell blocks the script, run once as admin:
> `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

**macOS / Linux**

```bash
cd ~/path/to/chartsmaze_claude_mcp
python3 -m venv .venv
source .venv/bin/activate
```

---

### 2. Install the package and Playwright

With the venv active:

```bash
pip install -e .
playwright install chromium
```

---

### 3. Configure environment variables

```bash
cp .env.example .env   # macOS/Linux
copy .env.example .env  # Windows
```

Edit `.env` and fill in your credentials:

| Variable | Required | Where to find it |
|---|---|---|
| `TRADINGVIEW_SESSION` | **Yes** (for watchlist) | Browser DevTools → Application → Cookies → `tradingview.com` → `sessionid` |
| `TRADINGVIEW_SESSION_SIGN` | No (recommended) | Same location → `sessionid_sign` |
| `CHARTSMAZE_SESSION` | No | Same approach on `chartsmaze.com` → `session` (paid features only) |

**How to copy cookies from your browser:**

1. Log in to tradingview.com (and optionally chartsmaze.com) in Chrome/Edge.
2. Press **F12** → **Application** tab → **Cookies** in the left panel.
3. Click the site URL, then find and copy the cookie value.
4. Paste into `.env`.

---

### 4. Run

#### CLI (standalone — no MCP client needed)

```bash
# Full automated workflow
chartsmaze run

# With filters
chartsmaze run --top-n 5 --min-eps 10 --min-revenue 15 --min-volume 100000

# Individual commands
chartsmaze sectors
chartsmaze industries Technology
chartsmaze screen Technology "Capital Goods" --min-volume 100000
chartsmaze stock RELIANCE
chartsmaze discover https://chartsmaze.com
```

#### MCP server (for Claude Desktop / any MCP client)

```bash
chartsmaze-mcp
# or, if the entry point is still not on PATH:
python -m chartsmaze_mcp.server
```

---

## Troubleshooting: `'chartsmaze' is not recognized`

This error means Windows can't find the entry-point script. It happens when
pip installs into the **user-level** Scripts folder instead of your Python's
Scripts folder, and that folder is not on `PATH`.

**Quickest fix — use `python -m` instead:**

```bat
python -m chartsmaze_mcp.cli run
python -m chartsmaze_mcp.server
```

**Permanent fix — use a virtual environment (see step 1 above).**

**Alternative — add the user Scripts folder to PATH:**

1. Run in a command prompt to find the folder:
   ```bat
   python -c "import site; print(site.getusersitepackages())"
   ```
   Replace `site-packages` at the end with `Scripts`.
   Example result: `C:\Users\Admin\AppData\Roaming\Python\Python314\Scripts`

2. Add that path to your user `PATH`:
   - Search Windows for **"Edit the system environment variables"**
   - Click **Environment Variables** → select `Path` under **User variables** → **Edit**
   - Click **New**, paste the Scripts path, click **OK**
   - Open a **new** Command Prompt and retry.

---

## Claude Desktop config

Add to `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "chartsmaze": {
      "command": "C:\\Users\\Admin\\Documents\\GitHub\\chartsmaze_claude_mcp\\.venv\\Scripts\\chartsmaze-mcp.exe",
      "env": {
        "TRADINGVIEW_SESSION": "<your-session-id>",
        "TRADINGVIEW_SESSION_SIGN": "<your-session-sign>"
      }
    }
  }
}
```

> Use the full path to the venv entry point to avoid PATH issues entirely.
> On macOS/Linux replace with the equivalent `.venv/bin/chartsmaze-mcp` path.

Alternatively, if `chartsmaze-mcp` is on your PATH:

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

## CLI reference

```
chartsmaze run [options]
  --top-n N          Top N sectors to focus on (default 3)
  --min-eps PCT      Min EPS growth % (default 0)
  --min-revenue PCT  Min revenue growth % (default 0)
  --min-volume VOL   Min 20-day volume MA (default 50000)
  --watchlist NAME   TradingView watchlist name (default "ChartsMaze Picks")
  --exchange EX      Exchange prefix for TradingView (default NSE)

chartsmaze sectors
chartsmaze industries SECTOR
chartsmaze screen SECTOR [SECTOR ...] [--min-eps] [--min-revenue] [--min-volume] [--include-circuit]
chartsmaze stock TICKER
chartsmaze discover URL
```

---

## How ChartsMaze scraping works

ChartsMaze is a React/Next.js SPA. The client uses **Playwright** to:

1. Load the page in a headless Chromium browser.
2. **Intercept all XHR/fetch JSON responses** — the cleanest source of data.
3. Fall back to parsing `window.__NEXT_DATA__` (Next.js server-rendered props).
4. Last resort: scrape visible table/card DOM elements.

If a page returns no data, run `chartsmaze discover https://chartsmaze.com/<page>` to
see exactly which endpoints the page hits — you can then update the parser keys
in `src/chartsmaze_mcp/chartsmaze/client.py` to match.
