# Playwright Auto-Redemption & Account Dashboard

**Date:** 2026-04-02
**Status:** Approved Design
**Replaces:** On-chain PositionRedeemer (`engine/execution/redeemer.py`)

---

## Goal

Replace the on-chain Gnosis Safe redeemer with a Playwright-based browser automation service that:
1. Auto-redeems settled Polymarket positions via the UI
2. Provides live browser preview of the Polymarket account
3. Scrapes account balance, positions, and order history
4. Exposes all data via Hub API + WebSocket for a new frontend dashboard tab

## Architecture

Engine-embedded service. The `PlaywrightService` runs inside the engine process, managed by the orchestrator — same lifecycle pattern as every other engine service.

```
Orchestrator
├── PlaywrightService (new — replaces PositionRedeemer)
│   ├── Chromium browser (headless, persistent context)
│   ├── Cookie store (~/.polymarket_cookies.json)
│   └── IMAP client (Gmail verification codes)
├── PolymarketClient (unchanged)
├── OrderManager (unchanged)
├── RiskManager (unchanged)
└── ... other services unchanged
```

## Constraints

- **No local testing.** Playwright code is deployed and tested on Railway only.
- **No on-chain redeemer.** Once Playwright is live, the old `PositionRedeemer` and `_redeem_loop()` are removed from the orchestrator.
- **Graceful degradation.** If the browser crashes or login fails, the engine continues trading. Playwright retries on the next loop iteration.

---

## 1. PlaywrightService (`engine/playwright/`)

### Files

```
engine/playwright/
├── __init__.py
├── service.py          # PlaywrightService class (lifecycle, browser mgmt)
├── login.py            # Gmail OAuth login flow + IMAP code fetch
├── account.py          # Balance, positions, order history scraping
└── redeemer.py         # Find redeemable positions, click Redeem
```

### Class: PlaywrightService

**Constructor args:**
- `gmail_address: str` — `bbrichards123@gmail.com`
- `gmail_app_password: str` — IMAP app password
- `cookie_path: str` — path to cookie JSON file
- `headless: bool = True`

**Lifecycle:**
- `async start()` — Launch Chromium with persistent context, restore cookies, attempt login if needed
- `async stop()` — Save cookies, close browser context and Playwright instance

**Public API:**

| Method | Returns | Description |
|--------|---------|-------------|
| `is_logged_in()` | `bool` | Check if current session is authenticated |
| `login()` | `bool` | Full login flow (navigate → email → IMAP code → submit) |
| `get_portfolio_balance()` | `dict` | `{usdc: float, positions_value: float, total: float}` |
| `get_positions()` | `list[dict]` | All positions: market, outcome, shares, value, status |
| `get_redeemable()` | `list[dict]` | Settled positions with Redeem button available |
| `redeem_all()` | `dict` | `{redeemed: int, failed: int, total_value: float, details: list}` |
| `get_order_history(limit=50)` | `list[dict]` | Recent orders from activity page |
| `screenshot()` | `bytes` | PNG screenshot of current browser page |

### Login Flow

1. Navigate to `https://polymarket.com`
2. Check for logged-in state (profile icon / wallet indicator present)
3. If not logged in:
   a. Click "Log In" button
   b. Click "Continue with Email" or Google OAuth option
   c. Enter `bbrichards123@gmail.com`
   d. Polymarket sends verification code to Gmail
   e. Connect to `imap.gmail.com:993` with app password `oxkfkhchcoljzxkr`
   f. Search for latest Polymarket verification email (subject filter)
   g. Extract 6-digit code from email body
   h. Enter code in Polymarket verification input
   i. Wait for redirect to logged-in state
4. Save cookies to `cookie_path`

### Cookie Persistence

- On `start()`: load cookies from JSON, add to browser context
- On `stop()`: dump all cookies to JSON
- Cookie file location: `engine/data/.polymarket_cookies.json`
- File is gitignored

### Error Handling

- All public methods wrapped in try/except that logs errors via structlog
- Browser crash → `_browser_alive = False` → next loop call attempts relaunch
- Login failure → log warning, return False, retry on next loop
- Individual redeem failure → log + continue to next position, report in summary
- IMAP failure → log error, login fails gracefully

---

## 2. Orchestrator Changes

### Removals

- Remove `self._redeemer` (PositionRedeemer instance)
- Remove `_redeem_loop()` method
- Remove redeemer import and initialization
- Remove redeemer `connect()` and `start()` calls

### Additions

- Add `self._playwright` (PlaywrightService instance)
- Initialize in `__init__()` with Gmail creds from settings
- Call `await self._playwright.start()` in orchestrator `start()` — after DB, before strategies
- Call `await self._playwright.stop()` in orchestrator `stop()`

### New Async Loops

| Loop | Interval | Behavior |
|------|----------|----------|
| `_playwright_redeem_loop()` | 300s (5 min) | Call `redeem_all()`, persist results to DB, send Telegram alert on success |
| `_playwright_balance_loop()` | 60s | Call `get_portfolio_balance()`, update system state in DB, broadcast via WebSocket |
| `_playwright_screenshot_loop()` | 30s | Call `screenshot()`, cache in memory, broadcast base64 via WebSocket event type `playwright_screenshot` |

All loops follow the existing pattern:
```python
async def _playwright_redeem_loop(self):
    while not self._shutdown_event.is_set():
        try:
            if self._playwright and self._playwright._browser_alive:
                result = await self._playwright.redeem_all()
                if result["redeemed"] > 0:
                    await self._alerter.send(...)
                    await self._db.insert_redeem_event(result)
        except Exception as e:
            log.error("playwright.redeem_loop.error", error=str(e))
        await asyncio.sleep(300)
```

### Settings Additions

New fields in `engine/config/settings.py`:
- `gmail_address: str = ""` — env: `GMAIL_ADDRESS`
- `gmail_app_password: str = ""` — env: `GMAIL_APP_PASSWORD`
- `playwright_enabled: bool = False` — env: `PLAYWRIGHT_ENABLED`

---

## 3. Hub API Endpoints

### New File: `hub/api/playwright.py`

Mounted at `/api/playwright` with tag `playwright`.

| Endpoint | Method | Source | Returns |
|----------|--------|--------|---------|
| `/status` | GET | DB (system_state) | `{logged_in, browser_alive, last_redeem_at, last_balance_at}` |
| `/balance` | GET | DB (cached by engine) | `{usdc, positions_value, total}` |
| `/positions` | GET | DB (cached by engine) | `[{market, outcome, shares, value, status}]` |
| `/redeemable` | GET | DB (cached by engine) | `[{market, outcome, value}]` |
| `/redeem` | POST | Sets `redeem_requested=true` in `playwright_state` table | `{triggered: true}` (engine checks this flag each loop iteration and runs immediate sweep) |
| `/history` | GET | DB (cached by engine) | `[{market, side, amount, price, date, status}]` |
| `/screenshot` | GET | DB/memory (cached PNG) | PNG image response (`image/png`) |

**Note:** Most endpoints read from DB where the engine caches scraped data. The POST `/redeem` sets a flag that the engine's redeem loop picks up immediately (sleep interrupted).

### Auth

All endpoints require `get_current_user` dependency (JWT), same as every other route.

---

## 4. WebSocket Events

New event types broadcast by the engine via the existing ConnectionManager:

| Event Type | Interval | Payload |
|------------|----------|---------|
| `playwright_status` | 60s | `{logged_in, browser_alive}` |
| `playwright_balance` | 60s | `{usdc, positions_value, total}` |
| `playwright_screenshot` | 30s | `{image_base64: "..."}` |
| `playwright_redeem` | On event | `{redeemed: int, total_value: float, details: [...]}` |

---

## 5. Frontend — PlaywrightDashboard Page

### Route

`/playwright` — added to sidebar nav in `Layout.jsx`

### Sections

**1. Live Browser Preview**
- Auto-updating screenshot from WebSocket `playwright_screenshot` events
- Manual "Refresh" button
- Status indicator (green = logged in, red = disconnected)
- Full-width card with aspect ratio matching Polymarket layout

**2. Account Overview**
- Balance card: USDC cash, positions value, total
- Positions table: market name, outcome (YES/NO), shares, current value, status badge
- Status badges: `Active` (cyan), `Settled` (green), `Redeemable` (purple pulse)
- "Redeem All" button (purple, calls POST `/api/playwright/redeem`)

**3. Activity History**
- Order history table: date, market, side, amount, price, status
- Paginated, sortable by date
- Same table styling as Trades page

### Styling

Follows existing patterns:
- CSS variables (`--card`, `--border`, `--accent-purple`, etc.)
- Card layout with `bg-[var(--card)] border border-[var(--border)] rounded-lg`
- Tables with `text-white/40` headers
- Status badges with colored backgrounds

---

## 6. Database Changes

### New Table: `playwright_state`

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PK | Auto-increment |
| `logged_in` | BOOLEAN | Current login status |
| `browser_alive` | BOOLEAN | Browser process alive |
| `usdc_balance` | FLOAT | Cached USDC balance |
| `positions_value` | FLOAT | Cached positions value |
| `positions_json` | JSONB | Cached positions list |
| `redeemable_json` | JSONB | Cached redeemable list |
| `history_json` | JSONB | Cached order history |
| `screenshot_png` | BYTEA | Latest screenshot |
| `updated_at` | TIMESTAMP | Last update time |

| `redeem_requested` | BOOLEAN | Hub sets to true via POST /redeem, engine resets after sweep |

Single row, upserted on each loop iteration. Hub API reads from this table.

### New Table: `redeem_events`

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PK | Auto-increment |
| `redeemed_count` | INT | Positions redeemed |
| `failed_count` | INT | Positions that failed |
| `total_value` | FLOAT | Total USDC redeemed |
| `details_json` | JSONB | Per-position details |
| `created_at` | TIMESTAMP | Event time |

---

## 7. Docker / Railway Changes

### engine/Dockerfile additions

```dockerfile
# Install Playwright + Chromium
RUN pip install playwright
RUN playwright install chromium --with-deps
```

### New Environment Variables (Railway)

| Variable | Value | Service |
|----------|-------|---------|
| `GMAIL_ADDRESS` | `bbrichards123@gmail.com` | engine |
| `GMAIL_APP_PASSWORD` | `oxkfkhchcoljzxkr` | engine |
| `PLAYWRIGHT_ENABLED` | `true` | engine |

### engine/requirements.txt additions

```
playwright>=1.40.0
```

---

## 8. Removed Code

- `engine/execution/redeemer.py` — deleted (on-chain redeemer)
- Orchestrator: remove `_redeemer` init, `_redeem_loop()`, redeemer imports
- Orchestrator: remove redeemer `connect()` / `start()` / `stop()` calls
- Keep `engine/execution/` directory — other files (order_manager, polymarket_client, etc.) remain

---

## Non-Goals

- No interactive browser control from frontend (view-only via screenshots)
- No multi-account support
- No local testing of Playwright
- No Playwright-based trading (execution stays via py-clob-client)
