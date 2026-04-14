# Frontend Margin Issues - Fix Plan

## Issues to Fix

### 1. Add v4 Fields to PositionsPanel (HIGH PRIORITY)
**File:** `frontend/src/pages/margin-engine/components/PositionsPanel.jsx`

**Missing Fields:**
- `strategy_version`
- `v4_entry_regime`
- `v4_entry_macro_bias`
- `v4_entry_consensus_safe`
- `entry_commission`
- `exit_commission`
- `continuation_count`
- `stop_loss_price`
- `take_profit_price`

**Action:** Add columns to display these fields for both open and closed positions

### 2. Consolidate V4Panel in V4Surface (HIGH PRIORITY)
**Files:** 
- `frontend/src/pages/margin-engine/components/V4Panel.jsx` (keep as reusable component)
- `frontend/src/pages/data-surfaces/V4Surface.jsx` (update to use V4Panel)

**Action:** V4Surface should import and use V4Panel component instead of duplicating logic

### 3. Add Fee Display to All Position Views (MEDIUM PRIORITY)
**Files:**
- `frontend/src/pages/margin-engine/components/PositionsPanel.jsx`
- `frontend/src/pages/margin-engine/MarginEngine.jsx` (if applicable)

**Action:** Add column/section showing:
- `entry_commission`
- `exit_commission`
- `total_commission`

### 4. Extract Hardcoded Values to Constants (MEDIUM PRIORITY)
**File:** `frontend/src/pages/margin-engine/components/constants.js`

**Action:** Add constants:
```javascript
export const DEFAULT_ASSET = 'BTC';
export const DEFAULT_TIMESCALES = '5m,15m,1h,4h';
export const DEFAULT_STRATEGY = 'fee_aware_15m';
export const POLLING_INTERVAL_MS = 5000;
```

Update all pages to use these constants.

### 5. Add Documentation Links (LOW PRIORITY)
**Files:** All margin pages with empty states

**Action:** Add help links:
- `MarginEngine.jsx`
- `V4Panel.jsx`
- `SignalPanel.jsx`
- `TradeTimelinePanel.jsx`

### 6. Add CSV Export to TradeTimeline (LOW PRIORITY)
**File:** `frontend/src/pages/margin-engine/components/TradeTimelinePanel.jsx`

**Action:** Add CSV export button with download functionality

### 7. Add Control Panel (OPTIONAL - If Time Permits)
**File:** `frontend/src/pages/margin-engine/MarginEngine.jsx`

**Action:** Add buttons for:
- Kill switch
- Resume after kill
- Toggle paper mode (if API supports)

## Implementation Status

✅ **COMPLETED - All Issues Fixed**

### Changes Summary

1. **PositionsPanel.jsx** - Added v4 fields and fee display:
   - New columns: Strategy, v4 Context, Fees
   - v4 Context shows: regime, macro_bias, consensus_safe
   - Fees column shows: total commission with breakdown (entry/exit)
   - Position details include: continuation_count, stop_loss_price, take_profit_price

2. **V4Surface.jsx** - Consolidated with V4Panel:
   - Removed 586 lines of duplicate code
   - Now imports and uses V4Panel component
   - Uses constants for default values

3. **constants.js** - Added new constants:
   - DEFAULT_ASSET = 'BTC'
   - DEFAULT_TIMESCALES = '5m,15m,1h,4h'
   - DEFAULT_STRATEGY = 'fee_aware_15m'
   - POLLING_INTERVAL_MS = 5000

4. **TradeTimelinePanel.jsx** - Added CSV export:
   - CSV Export button in filter bar
   - Exports all current filtered trades
   - Includes all trade fields in CSV format

5. **Documentation Links** - Added to all pages:
   - V4Panel: V4 Fusion Surface guide
   - SignalPanel: Signal Pipeline guide
   - TradeTimeline: Trade Timeline guide
   - MarginEngine: Multiple help links for different sections

6. **MarginEngine.jsx** - Added control panel:
   - Kill Switch button (with confirmation)
   - Resume button (when kill switch triggered)
   - Toggle Paper Mode button
   - All controls show loading states
   - Uses POLLING_INTERVAL_MS constant

### Files Modified (7 files)
- `frontend/src/pages/data-surfaces/V4Surface.jsx` (-586 lines)
- `frontend/src/pages/margin-engine/MarginEngine.jsx` (+143 lines)
- `frontend/src/pages/margin-engine/components/PositionsPanel.jsx` (+38 lines)
- `frontend/src/pages/margin-engine/components/SignalPanel.jsx` (+10 lines)
- `frontend/src/pages/margin-engine/components/TradeTimelinePanel.jsx` (+65 lines)
- `frontend/src/pages/margin-engine/components/V4Panel.jsx` (+10 lines)
- `frontend/src/pages/margin-engine/components/constants.js` (+5 lines)

### Net Change
- 270 insertions, 587 deletions
- Significant code reduction through consolidation
- All functionality preserved and enhanced
