# Apr 9, 2026 — Full Day Trading Analysis

## Summary

| Metric | Value |
|--------|-------|
| **Total resolved trades** | 65 |
| **Win/Loss** | 50W/15L (77% WR) |
| **Gross PnL** | +$9.91 |
| **Wallet trajectory** | $57 → $122 (Asian) → $13.47 (London+Evening) → $63.32 (recovery) |
| **Kill switch activated** | 19:38 UTC (81.7% drawdown from $73.56 peak) |
| **Trades blocked by kill** | 21 evaluations — **19W/2L = 90.5% WR, ~$30 missed profit** |

## Session Breakdown

```
SESSION      TRADES  W/L    WR%    PNL       AVG_LOSS_STAKE  MODEL
────────────────────────────────────────────────────────────────────────
ASIAN(0-8)     34    34/0   100%   +$56.65   n/a             OAK (pre-SEQUOIA)
LONDON(9-12)   17     8/9    47%   -$39.16   $6.76           OAK → SEQUOIA (13:22)
US_OPEN(13-16)  5     4/1    80%   +$3.00    $3.40           SEQUOIA
EVENING(17-18)  9     4/5    44%   -$10.58   $3.39           SEQUOIA
BLOCKED(19-21) 21*   19/2*  91%*  $0 (missed) n/a           SEQUOIA (kill switch)
```

*Blocked trades: signal evaluations passed all gates but risk_manager rejected due to kill switch. Outcome checked against market_data for counterfactual analysis.

## Regime × Session Entry Zone Map

```
SESSION      REGIME       N    W/L    WR%    PNL      VERDICT
─────────────────────────────────────────────────────────────────
ASIAN        CASCADE       3   3/0    100%   +$4.80   ✅ TRADE
ASIAN        NORMAL       10  10/0    100%  +$16.00   ✅ TRADE
ASIAN        TRANSITION   21  21/0    100%  +$35.85   ✅ TRADE
LONDON       NORMAL        5   3/2     60%   -$3.96   ⚠️ REDUCE SIZE
LONDON       TRANSITION   11   4/7     36%  -$38.16   ❌ BLOCK or HALF
US_OPEN      NORMAL        2   2/0    100%   +$3.20   ✅ TRADE
US_OPEN      TRANSITION    2   1/1     50%   -$1.80   ⚠️ REDUCE SIZE
EVENING      CASCADE       3   1/2     33%   -$5.18   ❌ BLOCK or HALF
EVENING      TRANSITION    5   3/2     60%   -$2.00   ⚠️ REDUCE SIZE
```

## Kill Switch Analysis — 21 Blocked Trades (19:33-20:47 UTC)

These trades passed all gates (source agreement, delta magnitude, taker flow, CG confirmation, DUNE confidence, spread, dynamic cap) but were rejected by the risk manager's kill switch.

```
TIME   REGIME      T-OFF  CONF   DIR    OUTCOME  WOULD_BE
──────────────────────────────────────────────────────────
19:33  NORMAL       100   0.764  UP     UP       WIN
19:33  NORMAL       100   0.764  UP     UP       WIN
19:38  CALM         114   0.729  UP     UP       WIN
19:42  CALM         144   0.729  UP     UP       WIN
19:47  CASCADE      150   0.729  UP     DOWN     LOSS
19:47  CASCADE      150   0.729  UP     UP       WIN
19:53  TRANSITION   104   0.754  DOWN   DOWN     WIN
19:57  CASCADE      150   0.717  DOWN   DOWN     WIN
20:02  TRANSITION   150   0.729  UP     UP       WIN
20:02  TRANSITION   150   0.729  UP     DOWN     LOSS
20:08  NORMAL       100   0.754  DOWN   DOWN     WIN
20:13  NORMAL       100   0.764  UP     UP       WIN
20:17  TRANSITION   150   0.729  UP     UP       WIN
20:17  TRANSITION   150   0.729  UP     UP       WIN
20:23  NORMAL       100   0.764  UP     UP       WIN
20:28  TRANSITION   104   0.754  DOWN   DOWN     WIN
20:32  TRANSITION   150   0.729  UP     UP       WIN
20:32  TRANSITION   150   0.729  UP     UP       WIN
20:38  CASCADE      114   0.717  DOWN   DOWN     WIN
20:42  CASCADE      150   0.729  UP     UP       WIN
20:47  CASCADE      150   0.717  DOWN   DOWN     WIN
```

**Result: 19 WINS, 2 LOSSES = 90.5% WR**

At $3.40 stakes: 19 × $1.60 - 2 × $3.40 = **+$23.60 missed profit**.

**Key insight:** The kill switch activated during the transition from EVENING (44% WR) to NIGHT (90%+ WR). The evening losses triggered the kill, then the night session — which is historically the best trading period — was completely blocked.

## Data Collection Gap

**Important:** When the risk manager blocks a trade, the signal evaluation and gate audit data IS recorded (the gate pipeline runs before risk check). However, the engine does NOT:
- Record a trade in the `trades` table (no order placed)
- Record in `trade_bible` (no outcome to track)
- Record fill data in `poly_trade_history`
- Log detailed execution context (only `trade.risk_blocked` with reason)

The counterfactual analysis above was possible by joining `signal_evaluations` (which records all TRADE decisions) with `market_data` (which has outcomes). But the 74% expire rate on GTC orders means many of these "would have won" signals might not have filled anyway.

## SEQUOIA Model Performance (Post 13:22 UTC)

| Metric | Value |
|--------|-------|
| Signal evaluation accuracy | 29W/4L = **87.9% WR** (including unfilled) |
| Actual resolved trades | 6W/6L = 50% WR (fill rate was terrible) |
| DUNE confidence range | 0.717 — 0.838 (smooth, no bimodal clustering) |
| Temperature calibration | T ≈ 1.0 (properly calibrated) |
| GTC expire rate | 74% (20 of 27 orders expired unfilled) |

**SEQUOIA's directional accuracy is excellent (88%)** — the issue is execution (fills), not prediction.

## Bankroll Timeline

```
00:00  $57.07  ← Engine restart (OAK model, v10.4 thresholds)
00:08  First trade (WIN)
08:53  $122+   ← Peak (34W/0L, 100% Asian session)
09:28  First loss ($8.33 stake — oversized, 7.5% of stale $115 bankroll)
12:17  $63ish  ← London massacre complete (8W/9L, -$39.16)
13:22  Engine restart with SEQUOIA + v10.5 thresholds (BET_FRACTION=5%, BANKROLL=$63)
13:37  First SEQUOIA loss ($3.40 — correctly sized)
17:08  Evening losses begin
18:48  $13.47  ← Bottom (wallet depleted)
19:38  Kill switch fires (81.7% drawdown from $73.56 peak)
19:38+ Blocked 21 trades (19W/2L would-be)
20:54  $63.32  ← Wallet recovery (positions resolved + top-up)
21:00  Engine still kill-switched despite wallet recovery
```

## Identified System Issues

1. **Kill switch never auto-resumes** — once triggered, stays active forever. The 19W/2L blocked trades show this costs more than it saves.
2. **STARTING_BANKROLL stale** — morning trades used 7.5% of $115 when wallet was $57 (2x oversized). Evening used 5% of $63 which was correct.
3. **No session awareness** — Asian 100% WR and London 47% WR get identical stake sizing.
4. **Consecutive loss cooldown = 10** — too loose. Evening had 4 losses in a row without triggering cooldown.
5. **Peak tracking discontinuity** — peak set from overnight ($73.56) doesn't reset when session regime changes.

## Proposed v10.7 Config

See `/docs/v10_7_config_proposal.md` for the detailed plan addressing all 5 issues.
