# 15m Ghost Decision Analysis

**Source:** Direct DB query against Hub Postgres (`hopper.proxy.rlwy.net:35772`), executed inside `hub` container at 2026-05-01 17:25 UTC.
**Why direct DB:** `/api/v58/strategy-decisions` returns `name 'resolved' is not defined` (audit task #329).

## 14d ghost WR — cross-join on `market_data.outcome`

```
strategy             asset  decisions   resolved   correct    WR
------------------- ------ ----------- ---------- --------- -------
v7_15m_sniper        BTC          17        17        17    100.00%   ← tiny n
v15m_up_basic        BTC        3316      3316      2564     77.32%   ⚡⚡
v15m_up_asian        BTC        1979      1979      1458     73.67%   ⚡
v15m_gate            BTC       22197     22197     14696     66.21%
v7_15m_sniper_sol    SOL         694       694       442     63.69%
v7_15m_sniper_xrp    XRP         658       658       417     63.37%
v15m_down_only       BTC       49946     49946     28937     57.94%
v15m_fusion_v5_9     BTC        2119      2119      1212     57.20%
v15m_fusion          BTC        6996      6996      3872     55.35%
v7_15m_sniper_eth    ETH         743       743       327     44.01%   ❌ actively wrong
```

**Natural up-rate baseline (BTC 15m, last 14d):** 50.61% UP / 49.39% DOWN. So strategy edges are computed against ~50%.

## Edge over directional baseline

| Strategy | n | WR | UP-bet % | Baseline | Edge |
|---|---|---|---|---|---|
| v15m_up_basic | 3316 | 77.32% | 100% UP | 50.61% | **+26.71pp** |
| v15m_up_asian | 1979 | 73.67% | 100% UP | 50.61% | **+23.06pp** |
| v15m_gate | 22197 | 66.21% | 21% UP | mixed | **+16.82pp** |
| v15m_down_only | 49946 | 57.94% | 0% UP (100% DOWN) | 49.39% | +8.55pp |
| v15m_fusion_v5_9 | 2119 | 57.20% | 8.92% UP | mixed | +7.81pp |
| v15m_fusion | 6996 | 55.35% | 7.89% UP | mixed | +5.96pp |

The high-WR strategies have real positive edge over the natural baseline. Not regime exposure.

## Per-window dedup (truth)

The 14d WR numbers above are inflated by multi-eval — each window is evaluated every few seconds. Per-window deduped:

| Strategy | Unique windows | Correct | WR |
|---|---|---|---|
| **v15m_up_basic** | **132** | **96** | **72.73%** ← best |
| v15m_up_asian | 44 | 28 | 63.64% |
| v15m_gate | 756 | 443 | 58.60% |

## Per-day breakdown — when did v15m_up_basic actually fire?

```
2026-04-18: n= 398  WR=46.48%   ← first day, mediocre
2026-04-19: n= 736  WR=79.35%
2026-04-20: n= 254  WR=96.46%
2026-04-21: n=1249  WR=80.78%
2026-04-22: n= 679  WR=79.68%
2026-04-23: ZERO trades
2026-04-24: ZERO
2026-04-25: ZERO
... (continues zero through 2026-05-01)
```

**5 active days, then 9 days dormant.** The strategy is regime-conditional — it only fires when `poly_direction=UP` (top skip last 24h: `direction: direction=DOWN != UP` n=1637). Since 2026-04-23 the underlying signal direction has been DOWN-leaning, so the strategy stays out.

## Hour-of-day breakdown for v15m_up_basic (deduped, 14d)

```
hour  n    WR        bar
 0    6    100%      ████████████████████   ⚡
 1    2     50%      ██████████
 2    6     67%      █████████████
 3    6     67%      █████████████
 4    3    100%      ████████████████████   ⚡
 5    3    100%      ████████████████████   ⚡
 6    9     67%      █████████████
 7    8     63%      ████████████
 8    8     88%      █████████████████      ⚡
 9    5     80%      ████████████████       ⚡
10    6    100%      ████████████████████   ⚡
11    6     67%      █████████████
12    5    100%      ████████████████████   ⚡
13    6     67%      █████████████
14    8     63%      ████████████
15    2    100%      ████████████████████   ⚡
16    8     25%      █████                  ❌  US AM session
17    7     57%      ███████████
18    6     50%      ██████████             ⚠️  
19    5    100%      ████████████████████
20    5     80%      ████████████████
21    4     50%      ██████████             ⚠️
22    2    100%      ████████████████████
23    6     83%      ████████████████       ⚡
```

**Hour 16 is catastrophic (25% WR, n=8)** — that's NYC market open + crypto desk activity. **Hours 17, 18, 21 are sub-60%** — US session noise. Everything else is 67-100%.

## Cross-strategy hour-of-day pattern

The "bad hours" are consistent across all v15m strategies:

| Strategy | Bad hours (WR<55%) |
|---|---|
| v15m_fusion (deduped) | 6, 8, 12, 20, 21 |
| v15m_gate | 18, 19, 21 |
| v15m_up_basic | **16, 18 (and 1, but n=2)** |
| v15m_down_only | 17, 18, 19, 20, 21, 22, 23, 0, 1, 4, 8, 10 — half the day |

**Common bad hours across strategies: 16-21 UTC (US session, news/macro releases).** This is the single most impactful filter.

## Agreement gate — the strongest signal we found

When you union all 5 strategies (v15m_fusion, v15m_up_basic, v15m_gate, v15m_up_asian, v15m_fusion_v5_9) and look at windows where multiple strategies agree on direction:

| Agreement count | Windows | Correct | WR |
|---|---|---|---|
| **≥5 strategies agree** | **7** | **7** | **100%** |
| ≥4 strategies agree | 31 | 26 | **84%** |
| ≥3 strategies agree | 215 | 123 | 57% |
| ≥2 strategies agree | 260 | 157 | 60% |
| ≥1 strategy votes | 392 | 196 | 50% |

**Bayesian posterior in action**: when ≥4 of 5 independent strategies agree on direction, win probability jumps from baseline (~50%) to **84%**. That's a +34pp lift from the consensus alone.

This is the basis for the proposed `v15m_consensus` meta-strategy.

## Cross-corroboration: v15m_fusion vs v15m_up_basic

When both strategies decided on the same window:
- `ub=UP, f=UP` (agreement): 54 windows, **44 right (UP correct: 44/54 = 81%)**
- `ub=UP, f=DOWN` (disagreement): 50 windows, UP was right 38 times (76%), DOWN was right 12 times (24%)

When they agree on UP, win rate is 81%. When v15m_up_basic says UP and v15m_fusion says DOWN, v15m_up_basic is right 76% of the time. **The UP signal from v15m_up_basic is more reliable than v15m_fusion's DOWN signal in the disagreement zone.**

## PnL simulation (BTC 15m, 14d, $5/trade, 0.65 entry, 7.2% fee)

| Scenario | Windows | Wins | Losses | WR | Sim PnL |
|---|---|---|---|---|---|
| v15m_up_basic unfiltered | 132 | 96 | 36 | 72.7% | **+$59.85** |
| v15m_up_basic + block UTC 16-21 | 97 | 76 | 21 | **78.4%** | **+$84.88** |

**Hour filter cuts trade count by 26% but increases PnL by 42%.** The blocked 35 windows lost $25 net.

## Recent 24h state (today's diagnostic)

```
v15m_up_basic decisions in last 24h:
   SKIP: 2,430
   TRADE: 0

Top skip reasons:
   1,637  direction: direction=DOWN != UP        ← regime-conditional gate
       9  timing: T-102 outside [180, 540]       ← ten variations of same skip
       9  timing: T-100 outside [180, 540]
   ...
```

Strategy is doing exactly what its config says. The gate killing it is the directional filter — the underlying `poly_direction` (from v9 LGB blend) has been DOWN-leaning today.

## ETH 15m: actively wrong — disable

`v7_15m_sniper_eth` shows **44% WR over 743 ghost decisions** — actively predicting wrong. Confirmed via note #232 + GPU box log: ETH 15m feature cache returns `NoSuchKey` for `v2/eth/current_15m_binance.json`. The strategy is firing on broken/OOD signal — should be disabled until audit #267 ships.

## SOL/XRP 15m: marginal but breakeven

```
v7_15m_sniper_sol  694 trades  WR=63.69%   barely above breakeven
v7_15m_sniper_xrp  658 trades  WR=63.37%   barely above breakeven
```

Both at +13pp edge over baseline (assuming ~50% baseline). With Polymarket fees the breakeven WR is ~62.5% at typical entry prices. So these are at-or-just-above breakeven. **Not worth promoting LIVE without per-asset feature cache (audit #267).**

## What to ship

See [`recommendation.md`](./recommendation.md).
