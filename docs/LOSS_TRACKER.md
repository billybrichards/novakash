# Loss Tracker — v9/v10 Live Trading

**Source of truth:** `trade_bible` table + Telegram notifications + position_monitor logs

---

## Loss Log

### 2026-04-08 12:53 UTC — LOSS -$11.47

| Field | Value |
|-------|-------|
| **Time** | 12:53:15 UTC |
| **PnL** | -$11.47 |
| **Cost** | $11.47 |
| **Payout** | $0.00 (total loss) |
| **Source** | position_monitor (on-chain detection) |
| **DB Match** | NONE — orphaned position, no trade row matched |
| **Notification said** | `v2.2_confirmed_T70` — **WRONG** (fuzzy match pulled old v8 trade) |
| **Actual origin** | Likely from v10 dedup bug (12:12-12:22 UTC) when ~90 orders were placed on one window. Some may have filled on CLOB but engine marked them EXPIRED after 60s poll. |
| **Config** | Unknown — could be v10 or v9, position is orphaned |
| **Wallet before** | ~$114.02 |
| **Wallet after** | $103.55 |

**Root cause:** C1 audit issue — GTC fills after 60s polling create orphaned positions. The engine marks the trade as EXPIRED but the CLOB order is still alive until GTD expiry. If a market maker fills it between 60s and GTD expiry, the position exists on Polymarket but isn't tracked in the DB.

**Compounded by:** v10 dedup bug — 90+ orders placed on one window. Even though the engine marked most as EXPIRED, some may have filled on-chain.

---

### 2026-04-08 02:41 UTC — LOSS -$5.70

| Field | Value |
|-------|-------|
| **Config** | v9_EARLY_CASCADE_T230_FAK |
| **Entry** | $0.55 |
| **Direction** | NO (DOWN) |
| **Oracle** | UP (wrong) |
| **Regime** | CASCADE (VPIN 0.79) |
| **Note** | Early CASCADE disabled after this at 08:43 UTC |

---

### 2026-04-08 01:26 UTC — LOSS -$6.16

| Field | Value |
|-------|-------|
| **Config** | v9_EARLY_CASCADE_T220_FAK |
| **Entry** | $0.55 |
| **Direction** | YES (UP) |
| **Oracle** | DOWN |
| **Regime** | CASCADE (VPIN 0.71) |

---

### 2026-04-08 00:06 UTC — LOSS -$6.16

| Field | Value |
|-------|-------|
| **Config** | v9_EARLY_CASCADE_T230_FAK |
| **Entry** | $0.55 |
| **Direction** | YES (UP) |
| **Oracle** | DOWN |
| **Regime** | CASCADE (VPIN 0.66) |

---

### 2026-04-07 23:41 UTC — LOSS -$5.49

| Field | Value |
|-------|-------|
| **Config** | v9_EARLY_CASCADE_T220_FAK |
| **Entry** | $0.55 |
| **Direction** | YES (UP) |
| **Oracle** | DOWN |
| **Regime** | CASCADE (VPIN 0.73) |

---

## Pattern Analysis

| Category | Losses | Total PnL | Common Factor |
|----------|--------|-----------|---------------|
| v9 EARLY CASCADE | 4 | -$23.51 | All CASCADE regime, VPIN 0.65-0.79 |
| Orphaned positions | 1 | -$11.47 | No DB match, from dedup bug or late GTC fill |
| v9 GOLDEN | 0 (from trades table) | $0.00 | — |
| v10 DUNE | 0 (too new) | $0.00 | — |

**Key insight:** Every attributable loss was from EARLY CASCADE (now disabled). The orphaned position loss is a system bug, not a strategy failure.

---

## Fixes Applied

- [x] Early CASCADE disabled (V9_VPIN_EARLY=9.99) — 08:43 UTC
- [x] v10 dedup fix — prevents 90+ orders per window
- [x] Position monitor → trades linking — new token_id matching (fix deployed)
- [ ] C1: GTC fills after 60s poll — need to extend poll or mark PENDING_EXPIRY
- [ ] Fuzzy notification matching — still shows wrong entry_reason for orphaned positions

---

**Last updated:** 2026-04-08 13:00 UTC
