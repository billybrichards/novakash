# Polymarket Dashboard Integration — Playwright Automation

**Date:** 2026-04-02  
**Status:** Implementation Plan  
**Reference:** Archetapp's Polymarket Bot (auto-claim Playwright implementation)

---

## 🎯 Goal

Add a **Playwright-based automation layer** to Novakash that provides:
1. **Browser preview tab** - Live Polymarket dashboard access
2. **Balance monitoring** - Real-time portfolio balance checks
3. **Order history** - Trade history export/download
4. **Auto-redeem** - Click "Redeem" on settled positions
5. **Process status** - Monitor automation tasks in UI

---

## 🏗️ Architecture (Simple Integration)

```
┌─────────────────────────────────────────────────────┐
│              Novakash Frontend (React)              │
│         http://localhost:3000 (Railway)             │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌─────────────────┐  │
│  │ Dashboard│  │ Playwright│  │   Settings      │  │
│  │ Tab      │  │ Preview   │  │   Tab           │  │
│  └──────────┘  └──────────┘  └─────────────────┘  │
└─────────────────────┬──────────────────────────────┘
                      │
┌─────────────────────┴──────────────────────────────┐
│              Playwright Service (Python)           │
│              Runs in background process            │
│                                                    │
│  ┌────────────┐  ┌────────────┐  ┌─────────────┐  │
│  │ login()    │  │ getBalance │  │ redeem()    │  │
│  │ getOrders  │  │ getHistory │  │ status()    │  │
│  └────────────┘  └────────────┘  └─────────────┘  │
│                    (Headless Chrome)               │
└─────────────────────────────────────────────────────┘
                      │
              ┌───────▼───────┐
              │ Polymarket UI │
              │ https://...   │
              └───────────────┘
```

---

## 📋 Key Functions (From Archetapp Reference)

Based on Archetapp's `auto_claim.py` implementation:

| Function | Purpose | Priority |
|----------|---------|----------|
| `login_via_gmail()` | Auto-login via Gmail 2FA code retrieval | High |
| `get_portfolio_balance()` | Extract balance from UI | High |
| `get_order_history()` | Navigate to Activity page, scrape table | High |
| `get_redeemable_positions()` | Find settled markets with "Redeem" button | High |
| `redeem_position()` | Click redeem, confirm, verify | High |
| `get_process_status()` | Return current automation state | Medium |
| `screenshot_preview()` | Capture browser tab for dashboard preview | Medium |

---

## 🔌 API Endpoints (Add to Novakash Hub)

```python
# hub/api/playwright.py

@router.get("/playwright/status")
async def get_status() -> dict:
    """Return current Playwright automation status"""
    return {"logged_in": True, "last_check": "2026-04-02T21:00:00Z"}

@router.get("/playwright/balance")
async def get_balance() -> dict:
    """Get current portfolio balance via Playwright"""
    return {"usdc": 129.60, "positions_value": 45.20}

@router.get("/playwright/orders")
async def get_orders() -> list:
    """Get order history from Polymarket UI"""
    return [{"market": "BTC 5-min", "position": "Up", "amount": 32.00}]

@router.get("/playwright/redeemable")
async def get_redeemable() -> list:
    """Get list of positions ready for redemption"""
    return [{"market": "NBA Mavericks", "payout": 15.50}]

@router.post("/playwright/redeem")
async def redeem_position(market_slug: str) -> dict:
    """Redeem a specific position"""
    return {"success": True, "redeemed": 15.50}

@router.get("/playwright/screenshot")
async def get_screenshot() -> Response:
    """Return current browser screenshot for preview tab"""
    return Response(content=screenshot_bytes, media_type="image/png")
```

---

## 📁 File Structure (Add to Novakash)

```
novakash/
├── engine/playwright/
│   ├── __init__.py
│   ├── browser.py          # Playwright browser instance
│   ├── login.py            # Gmail 2FA login flow
│   ├── portfolio.py        # Balance, orders, history extraction
│   └── redeemer.py         # Auto-redeem functionality
├── hub/api/playwright.py   # API routes (above)
└── frontend/src/pages/PlaywrightDashboard.jsx  # New tab
```

---

## 🎨 Frontend Tab (Add to Novakash Dashboard)

```jsx
// frontend/src/pages/PlaywrightDashboard.jsx

import { useEffect, useState } from 'react';
import { useWebSocket } from '../hooks/useWebSocket';

export default function PlaywrightDashboard() {
  const [status, setStatus] = useState(null);
  const [balance, setBalance] = useState(null);
  const [screenshot, setScreenshot] = useState(null);

  useEffect(() => {
    // Poll status every 30s
    const interval = setInterval(async () => {
      const res = await fetch('/api/playwright/status');
      const data = await res.json();
      setStatus(data);
    }, 30000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="playwright-dashboard">
      <h2>Polymarket Automation</h2>
      
      <div className="status-card">
        <h3>Automation Status</h3>
        <p>Logged in: {status?.logged_in ? '✅' : '❌'}</p>
        <p>Last check: {status?.last_check}</p>
      </div>

      <div className="balance-card">
        <h3>Portfolio Balance</h3>
        <p>USDC: ${balance?.usdc}</p>
        <p>Positions: ${balance?.positions_value}</p>
      </div>

      <div className="preview-tab">
        <h3>Browser Preview</h3>
        <img src={screenshot} alt="Polymarket Dashboard" />
        <button onClick={() => fetchScreenshot()}>Refresh Preview</button>
      </div>

      <div className="redeem-section">
        <h3>Redeemable Positions</h3>
        {/* List redeemable positions with redeem buttons */}
      </div>
    </div>
  );
}
```

---

## 🛠️ Implementation Steps

### Phase 1: Playwright Service (3-4 days)
1. Setup Playwright browser instance (headless Chrome)
2. Implement Gmail 2FA login flow
3. Add balance extraction from Polymarket UI
4. Add order history scraping
5. Add auto-redeem functionality

### Phase 2: API Integration (2 days)
1. Add Playwright API routes to Novakash hub
2. Connect to existing PostgreSQL database
3. Add WebSocket for real-time updates

### Phase 3: Frontend Tab (2 days)
1. Create PlaywrightDashboard.jsx component
2. Add status/balance/screenshot display
3. Add redeem buttons and process monitoring
4. Style to match existing Novakash UI

### Phase 4: Testing (2 days)
1. Test login flow with Gmail 2FA
2. Test balance extraction accuracy
3. Test auto-redeem on settled positions
4. Test screenshot preview functionality

**Total: ~9-10 days**

---

## 🔒 Security Notes

1. **Gmail App Password:** Store in `.env`, never commit
2. **Session Cookies:** Encrypt storage, detect expiry
3. **Confirmation:** Require user confirmation before redemptions
4. **Dry-run Mode:** Preview actions before executing

---

## 📊 Reference Implementation

**Archetapp's Bot:** https://gist.github.com/Archetapp/7680adabc48f812a561ca79d73cbac69

Key file: `auto_claim.py`
- Uses Playwright for browser automation
- Auto-claims winning positions
- Runs as background process
- Can be adapted for Novakash integration

---

## 🚀 Next Steps

1. **Create `engine/playwright/` directory**
2. **Implement `browser.py`** - Playwright instance management
3. **Implement `login.py`** - Gmail 2FA login flow
4. **Add API routes** to `hub/api/playwright.py`
5. **Create frontend tab** `PlaywrightDashboard.jsx`
6. **Deploy to Railway** with Chrome dependencies

---

**Questions for Billy:**
1. Do you want the browser preview tab to show a live interactive browser, or just periodic screenshots?
2. Should auto-redeem be automatic or require manual confirmation for each position?
3. Any specific Polymarket markets you want to prioritize for testing?