# V58Monitor.jsx Audit — 2026-04-06

## Overview

- **File:** `frontend/src/pages/V58Monitor.jsx`
- **Size:** 3,113 lines / ~122KB
- **Purpose:** Primary trading monitor dashboard for the v7 BTC 5-minute strategy
- **Status:** FUNCTIONAL but oversized — needs decomposition

## Architecture

The file contains **1 exported component** (`V58Monitor`) and **16 internal components**:

### Component Map (by line range)

| Lines | Component | Purpose | Issues |
|-------|-----------|---------|--------|
| 20-33 | `T` (theme tokens) | Color/font constants | None — clean |
| 35-42 | Font injection | Loads IBM Plex Mono via DOM manipulation | Side effect outside React lifecycle |
| 44-91 | `windowStatus`, `directionColor`, `confidenceBar` | Helper functions | None — clean |
| 94-112 | `StatCard` | Reusable stat display | Duplicates `components/StatCard.jsx` |
| 115-132 | `SectionHeader` | Section title styling | Could be shared |
| 134-239 | `PriceChart` | Lightweight-charts candlestick | Well-implemented, proper cleanup |
| 241-383 | `WindowTimeline` | Horizontal scrollable window pills | Complex but functional |
| 385-639 | `SignalSourcesPanel` | Per-window signal breakdown (TimesFM, TWAP, Gamma, Point) | Large (254 lines) but necessary |
| 641-736 | `AgreementTracker` | TimesFM vs v5.7c agreement donut + stats | Uses canvas donut chart |
| 738-850 | `TradeLog` | Filtered trade list from windows | Good, uses `useMemo` |
| 852-909 | `ProgressRing` + `AccuracyCard` | SVG ring gauge | Clean, reusable |
| 911-1088 | `AccuracyScoreboard` | Multi-ring accuracy display with gate analysis | Large but well-structured |
| 1090-1114 | `CheckBadge` + `PnlBadge` | Tiny display helpers | Clean |
| 1116-1306 | `OutcomeHistoryTable` | 10-column outcome table with interactive rows | Large (190 lines), works |
| 1312-1541 | `WhatIfAnalysis` | "What if we bet $4" scenario panel | ~230 lines, heavy inline styles |
| 1543-1792 | `SignalSourceCards` | Per-signal detail cards for selected window | ~250 lines, heavy inline styles |
| 1794-2117 | `TradeButtons` | Manual paper/live trade placement with live Gamma prices | **Has own API calls + state** |
| 2123-2275 | `MyTradesPanel` | Manual trades table with P&L | Works well |
| 2276-3113 | `V58Monitor` (main) | Orchestrator: state, fetch, WS, layout | 837 lines, does too much |

## Issues Found

### 1. HARDCODED STAKE: `$4.00` (lines 1904, 2037, 2798, 3084)

**Severity:** MEDIUM  
The manual trade stake is hardcoded as `$4.00` in both the TradeButtons UI and the section header. Should be configurable or pulled from trading config.

```jsx
// Line 1904
<div style={{ fontSize: 18, fontWeight: 700, color: T.warning }}>$4.00</div>
// Line 1926
+{((activeBet.win_pnl / 4) * 100).toFixed(0)}% return  // hardcoded divisor
```

### 2. DUPLICATE `StatCard` (lines 94-112)

**Severity:** LOW  
Defines its own `StatCard` component that duplicates `components/StatCard.jsx`. The V58 version is simpler (no format prop) but serves the same purpose.

### 3. DUPLICATE `CountdownTimer` USAGE (lines 2662, 2828)

**Severity:** LOW  
`CountdownTimer` is rendered twice in the layout — once in the Signal Sources + Countdown section and again in the Trade Buttons section. This is intentional (trade context) but noted in code comment at line 2816.

### 4. `TradeButtons` MISSING `api` IN useEffect DEPS (line 1820)

**Severity:** MEDIUM  
```jsx
useEffect(() => {
  // ... uses api('GET', '/v58/live-prices...')
  return () => { clearInterval(interval); clearInterval(ageInterval); };
}, [latestWindow?.window_ts]); // missing 'api' dependency
```

### 5. SILENT ERROR SWALLOWING (lines 2310, 2396, 2398, 1814)

**Severity:** MEDIUM  
Multiple `catch {}` / `catch (_) {}` blocks with no logging:
- Line 1814: `TradeButtons` price fetch — `catch {}`
- Line 2310: Manual trades fetch — `catch (_) {}`
- Line 2396: WebSocket message parse — `catch (_) {}`
- Line 2398: WebSocket connection — `catch (_) {}`

The main `fetchAll` at line 2355 does log errors (`console.error`), but individual Promise.allSettled results don't log failures.

### 6. WIN/LOSS DETERMINED BY `delta_pct` PROXY (lines 49-59)

**Severity:** HIGH — relates to win rate inconsistency  
The `windowStatus()` function determines WIN/LOSS by checking if `delta_pct` aligns with `direction`:
```jsx
if (w.direction === 'UP' && w.delta_pct > 0) → WIN
if (w.direction === 'DOWN' && w.delta_pct < 0) → WIN
```
This is a **directional** win rate (did BTC move in our direction), NOT the Polymarket oracle resolution. This means the WindowTimeline pills may show different WIN/LOSS than actual trade outcomes.

### 7. FONT INJECTION VIA DOM (lines 36-42)

**Severity:** LOW  
Google Fonts loaded by injecting a `<link>` tag into `document.head` at module load time. This is a side effect that runs outside React lifecycle. Should be in `index.html` or a CSS import.

### 8. MASSIVE INLINE STYLES

**Severity:** LOW (cosmetic)  
Every component uses inline `style={{}}` objects. This is functional but:
- Creates new object references on every render
- No hover/focus pseudo-class support (uses `onMouseEnter/Leave` hacks)
- Makes the file much larger than necessary

This is a design choice, not a bug. But it's a major contributor to the 3,113 line count.

### 9. ENTRY CAP HARDCODED (line 2743)

**Severity:** LOW  
```jsx
<div>Entry Cap: <span style={{ fontWeight: 700, color: '#c084fc' }}>$0.70</span></div>
```
The v7.1 criteria display hardcodes the entry cap as `$0.70`. Should come from config or the window data.

### 10. STALE NAMING: "v5.8" REFERENCES (lines 2852, 4, 8)

**Severity:** LOW  
The file header and AgreementTracker section still reference "v5.8" but the strategy has evolved to v7/v7.1. The section header says "v5.8 AGREEMENT TRACKER" but the actual logic tracks TimesFM vs v5.7c agreement which is still relevant.

## API Endpoints Used

All verified to exist in `hub/api/v58_monitor.py`:

| Endpoint | Method | Line | Frequency |
|----------|--------|------|-----------|
| `/v58/windows?limit=50` | GET | 2318 | Every 15s |
| `/v58/stats?days=7` | GET | 2319 | Every 15s |
| `/v58/price-history?minutes=60` | GET | 2320 | Every 15s |
| `/v58/outcomes?limit=100` | GET | 2321 | Every 15s |
| `/v58/accuracy?limit=100` | GET | 2322 | Every 15s |
| `/v58/gate-analysis` | GET | 2323 | Every 15s |
| `/v58/manual-trades` | GET | 2307 | Every 30s |
| `/v58/live-prices` | GET | 1809 | Every 2s (TradeButtons only) |
| `/v58/manual-trade` | POST | 1844 | On user click |

**Performance note:** 6 parallel requests every 15 seconds + 1 every 30s + 1 every 2s = significant API load when this page is open. Uses `Promise.allSettled` which is correct.

## WebSocket Integration

Lines 2371-2408: Connects to `/ws/live` for real-time updates.
- Auto-reconnects on close (5s delay)
- Triggers `fetchAll()` on `signal`, `trade`, `window` message types
- Properly cleans up on unmount

## Decomposition Recommendation

Split into ~6 files:

```
frontend/src/pages/V58Monitor/
├── index.jsx              # Main component (state, fetch, layout) ~400 lines
├── PriceChart.jsx         # Lightweight-charts candlestick ~110 lines
├── WindowTimeline.jsx     # Horizontal window pills ~150 lines
├── SignalSources.jsx      # SignalSourcesPanel + SignalSourceCards ~500 lines
├── OutcomeHistory.jsx     # OutcomeHistoryTable + WhatIfAnalysis ~420 lines
├── TradeButtons.jsx       # Manual trading + MyTradesPanel ~450 lines
├── AccuracyScoreboard.jsx # ProgressRing + AccuracyCard + Scoreboard ~250 lines
├── AgreementTracker.jsx   # Donut chart + stats ~100 lines
└── shared.js              # Theme tokens, helpers (windowStatus, directionColor, etc.) ~100 lines
```

**Priority:** LOW — The file works correctly. Decomposition improves maintainability but isn't blocking anything.

## Summary

| Category | Count | Severity |
|----------|-------|----------|
| Hardcoded values ($4 stake, $0.70 cap) | 2 | MEDIUM |
| Missing useEffect deps | 1 | MEDIUM |
| Silent error swallowing | 4 locations | MEDIUM |
| Win/loss proxy (delta_pct vs oracle) | 1 | HIGH |
| Duplicate components (StatCard) | 1 | LOW |
| Stale naming (v5.8) | 2 | LOW |
| Inline style bloat | Everywhere | LOW |

**Verdict:** The V58Monitor is the most important page in the app — it's the primary trading interface. It's **functional and well-designed** from a UX perspective. The main actionable issues are:
1. The `windowStatus()` WIN/LOSS proxy using `delta_pct` instead of Polymarket oracle (HIGH — relates to win rate confusion)
2. Hardcoded $4 stake
3. Silent error swallowing in catch blocks
