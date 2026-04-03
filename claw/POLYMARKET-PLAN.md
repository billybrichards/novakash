# Polymarket Automation System - Implementation Plan

**Date:** 2026-04-02  
**Agent:** Novakash2  
**Status:** Planning Phase

---

## 🎯 Executive Summary

Build a Playwright-based automation system to:
1. Authenticate to Polymarket via Gmail 2FA code retrieval
2. Monitor and display portfolio balance
3. Track order history
4. Execute redemption actions
5. Provide dashboard for monitoring

---

## 🏗️ Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Polymarket Automation                    │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ Gmail Client│  │Playwright    │  │  Data Store       │  │
│  │ (IMAP/SMTP) │  │  Browser     │  │  (Portfolio/Order)│  │
│  └──────┬──────┘  └──────┬───────┘  └────────┬──────────┘  │
│         │                │                    │             │
│         └────────────────┴────────────────────┘             │
│                           │                                 │
│                  ┌────────▼────────┐                        │
│                  │  Dashboard API  │                        │
│                  │  (REST/Socket)  │                        │
│                  └─────────────────┘                        │
└─────────────────────────────────────────────────────────────┘
```

---

## 📋 Feature Breakdown

### Phase 1: Authentication System (Priority: Critical)

#### 1.1 Gmail Integration for 2FA Codes
- **Technology:** `himalaya` CLI or `imaplib` (Python)
- **Workflow:**
  1. Connect to Gmail IMAP: `imap.gmail.com`
  2. Authenticate with app-specific password (NOT main password)
  3. Search inbox for "Polymarket" emails
  4. Extract 6-digit verification code
  5. Return code to Playwright flow

- **Security Requirements:**
  - Use Gmail App Passwords (2FA required on Gmail account)
  - NEVER store main Gmail password
  - App password stored in `.env` (gitignored)
  - Consider OAuth2 for production

#### 1.2 Playwright Login Flow
```
1. Navigate to polymarket.com
2. Click "Connect Wallet" or "Sign In"
3. Detect 2FA prompt
4. Call Gmail client to fetch code
5. Enter code automatically
6. Verify successful login via URL/element detection
```

---

### Phase 2: Portfolio Balance Tracking (Priority: High)

#### 2.1 Balance Detection Strategy
- **Target Elements:** Identify portfolio balance selectors on Polymarket UI
- **Extraction Methods:**
  - Text content parsing from balance display elements
  - Network request interception (API calls to Polymarket backend)
  - Cookie/localStorage inspection for session data

#### 2.2 Balance Monitoring
- **Real-time:** WebSocket/SSE if available
- **Polling:** Every 5 minutes (configurable)
- **Alerts:** Threshold-based notifications

---

### Phase 3: Order History (Priority: High)

#### 3.1 Order History Navigation
- Navigate to "My Orders" or "Activity" section
- Handle pagination (if > 10 orders)
- Extract order details:
  - Market name
  - Position (Yes/No)
  - Amount
  - Price
  - Status (Open/Filled/Cancelled)
  - Timestamp

#### 3.2 Data Storage
```json
{
  "order_id": "string",
  "market": "string",
  "position": "YES|NO",
  "amount": "number",
  "price": "number",
  "status": "OPEN|FILLED|CANCELLED",
  "timestamp": "ISO8601"
}
```

---

### Phase 4: Redemption System (Priority: Medium)

#### 4.1 Redemption Flow
```
1. Navigate to settled markets
2. Identify redeemable positions
3. Click "Redeem" button
4. Confirm transaction
5. Verify completion
```

#### 4.2 Safety Measures
- Confirmation dialogs handling
- Transaction verification
- Error recovery (retry logic)

---

## 🛠️ Technical Stack

| Component | Technology | Rationale |
|-----------|------------|-----------|
| Browser Automation | Playwright (TypeScript/Python) | Reliable, fast, good selector support |
| Gmail Access | `himalaya` CLI or `imaplib` | Simple IMAP access |
| Data Storage | SQLite or JSON files | Simple, portable |
| Dashboard | FastAPI/Express + React | Real-time updates |
| Scheduling | cron or `node-cron` | Periodic checks |
| Logging | Winston/Pino | Structured logs |

---

## 📁 Project Structure

```
polymarket-automation/
├── src/
│   ├── auth/
│   │   ├── gmail-client.ts      # Gmail 2FA code retrieval
│   │   ├── polymarket-login.ts  # Playwright login flow
│   │   └── session-manager.ts   # Session persistence
│   ├── portfolio/
│   │   ├── balance-checker.ts   # Balance extraction
│   │   ├── order-history.ts     # Order tracking
│   │   └── redemption.ts        # Redeem actions
│   ├── dashboard/
│   │   ├── api.ts               # REST API
│   │   └── websocket.ts         # Real-time updates
│   └── utils/
│       ├── selectors.ts         # Page selectors
│       └── logger.ts            # Logging
├── tests/
│   ├── auth.test.ts
│   ├── portfolio.test.ts
│   └── e2e/
│       └── login-flow.test.ts
├── config/
│   ├── selectors.json           # Page selectors (easy updates)
│   └── env.example
├── docs/
│   └── PLAN.md                  # This document
├── .env
├── package.json
└── playwright.config.ts
```

---

## 🔒 Security Considerations

### ⚠️ Critical Security Notes

1. **Gmail Access:**
   - Use Gmail App Passwords (generate at: myaccount.google.com/apppasswords)
   - Enable 2FA on Gmail account first
   - Store app password in `.env` (never commit)
   - Consider using a dedicated email account

2. **Polymarket Session:**
   - Store session cookies securely (encrypted if possible)
   - Implement session expiry detection
   - Auto-logout on suspicious activity

3. **Financial Safety:**
   - Implement confirmation steps for all transactions
   - Add dry-run mode (preview actions before executing)
   - Log all actions with timestamps
   - Set spending limits

---

## 🧪 Testing Strategy

### Unit Tests
- Gmail client (mock IMAP)
- Code extraction logic
- Selector validation

### Integration Tests
- Full login flow (with test credentials)
- Balance extraction
- Order history parsing

### E2E Tests
```typescript
// tests/e2e/login-flow.test.ts
test('should login via Gmail 2FA', async () => {
  const page = await launchPolymarket();
  await navigateToLogin(page);
  const code = await gmailClient.getPolymarketCode();
  await enter2FACode(page, code);
  await expect(page).toHaveURL('/dashboard');
});

test('should extract portfolio balance', async () => {
  const balance = await portfolioChecker.getBalance(page);
  expect(balance).toMatch(/\d+\.\d+ USDC/);
});
```

---

## 📊 Dashboard Design

### API Endpoints

```
GET  /api/portfolio/balance     # Current balance
GET  /api/portfolio/orders      # Order history
POST /api/portfolio/redeem      # Trigger redemption
GET  /api/portfolio/redeemable  # List redeemable positions
WS   /ws/portfolio              # Real-time updates
```

### Dashboard UI Components
- Balance card (current holdings)
- Order history table
- Active positions card
- Redeem button (when available)
- Activity log
- Connection status indicator

---

## 📅 Implementation Timeline

| Phase | Description | Estimated Duration |
|-------|-------------|-------------------|
| 1 | Gmail client + Auth flow | 2-3 days |
| 2 | Portfolio balance tracking | 2 days |
| 3 | Order history extraction | 2 days |
| 4 | Redemption system | 2-3 days |
| 5 | Dashboard + API | 3-4 days |
| 6 | Testing + Documentation | 2 days |
| **Total** | | **~2-3 weeks** |

---

## 🚀 Initial Testing Plan

### Test 1: Gmail Code Retrieval
```bash
# Test Gmail connection
$ npm run test:gmail

# Expected: Returns 6-digit code from latest Polymarket email
```

### Test 2: Login Flow
```bash
# Test full login
$ npm run test:login

# Expected: Successfully logged in to Polymarket
```

### Test 3: Balance Extraction
```bash
# Test balance detection
$ npm run test:balance

# Expected: Returns current portfolio balance
```

---

## 🎯 Success Criteria

- [ ] Can login to Polymarket via Gmail 2FA automation
- [ ] Accurately extracts portfolio balance
- [ ] Can retrieve full order history
- [ ] Can identify and execute redemptions
- [ ] Dashboard displays real-time data
- [ ] All actions logged for audit trail

---

## ⚠️ Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Gmail API changes | High | Use standard IMAP (stable) |
| Polymarket UI changes | High | Configurable selectors |
| Session expiry | Medium | Auto-relogin detection |
| Terms of Service | Critical | Review ToS before deployment |
| Financial loss | Critical | Confirmation steps + dry-run mode |

---

## 📝 Next Steps

1. **Setup Environment**
   - Install Node.js 20+
   - Install Playwright: `npm init playwright@latest`
   - Setup Gmail app password
   - Clone repo structure

2. **Implement Gmail Client**
   - Test IMAP connection
   - Test code extraction

3. **Build Login Flow**
   - Map Polymarket login selectors
   - Implement 2FA automation

4. **Test Balance Extraction**
   - Identify balance selectors
   - Implement extraction logic

5. **Create Dashboard**
   - Build API endpoints
   - Create simple UI

---

## 🔧 Configuration

### .env.example
```env
# Gmail Configuration
GMAIL_USER=bbrichards123@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx

# Polymarket Configuration
POLYMARKET_URL=https://polymarket.com
SESSION_STORAGE=./storage/session.json

# Dashboard Configuration
PORT=3001
```

---

## 📞 Questions for Billy

1. Do you have a Gmail app password already set up, or should I guide you through creating one?
2. Are there any specific markets/positions you want to prioritize for redemption testing?
3. Do you want the dashboard as a separate web app, or integrated into an existing project?
4. What's your preferred balance check frequency (real-time, every 5min, 15min, hourly)?

---

**Document Version:** 1.0  
**Last Updated:** 2026-04-02  
**Status:** Ready for Implementation Review
