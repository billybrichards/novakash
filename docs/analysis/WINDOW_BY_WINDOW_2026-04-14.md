# Window-by-Window Analysis: Last 24 Hours

**Generated:** 2026-04-14 16:35 UTC
**Window range:** 2026-04-13 20:35 to 2026-04-14 16:30 UTC
**Total windows:** 220
**Data sources:** Railway PostgreSQL (strategy_decisions), Binance 1m klines (BTC delta), Polymarket CLOB API (trades + redeems = ground truth)

**Important:** Polymarket resolves via Chainlink oracle, NOT Binance. The BTC Actual column shows Binance direction, but the Outcome column is the Polymarket oracle truth (redeems). These sometimes disagree on tiny moves.

## Legend

- **TRADE DOWN [L]** = Strategy decided to trade DOWN, LIVE mode
- **TRADE DOWN [G]** = Strategy decided to trade DOWN, GHOST mode (paper)
- **SKIP(reason)** = Abbreviated skip reason (see key below)
- **Outcome** = Polymarket oracle ground truth: WIN (redeem>0), LOSS (redeem=0), PENDING (no redeem yet)

### Skip Reason Key

| Abbrev | Meaning |
|---|---|
| src(0/2) | 0 of 2 price sources available |
| src(1/2) | Only 1 of 2 sources agree |
| T-N | Eval at T-N seconds, outside sweet spot |
| dir | Direction filter (UP-only strat skipping DOWN) |
| conf(N) | Confidence distance N, below minimum |
| no_conf | No confidence distance available |
| consensus | Consensus not safe |
| spread | CLOB spread too wide |
| delta(N) | Delta magnitude too small |
| taker | Taker flow mismatch |
| not_advised | Trade not advised |
| poly(p=N) | Polymarket probability gate |
| cg_conf | CoinGlass confirmation failed |
| clob_size | CLOB sizing issue |
| early | Too early in window |

## Window Table

| Window (UTC) | BTC Actual | v4_fusion | v4_down_only | v4_up_basic | v4_up_asian | v10_gate | Outcome | P&L |
|---|---|---|---|---|---|---|---|---|
| 20:35 | DOWN -0.01% | SKIP(consensus) | SKIP(T-196) | SKIP(T-196) | SKIP(T-196) | SKIP(src(0/2)) | - | - |
| 20:50 | DOWN -0.06% | SKIP(consensus) | SKIP(T-156) | SKIP(dir) | SKIP(T-156) | SKIP(src(0/2)) | - | - |
| 20:55 | UP +0.05% | TRADE DOWN [G] | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(0/2)) | - | - |
| 21:00 | DOWN -0.04% | TRADE DOWN [G] | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(0/2)) | - | - |
| 21:05 | UP +0.23% | SKIP(early) | SKIP(T-152) | SKIP(dir) | SKIP(T-152) | SKIP(src(0/2)) | - | - |
| 21:15 | DOWN -0.11% | SKIP(early) | SKIP(T-152) | SKIP(dir) | SKIP(T-152) | SKIP(src(0/2)) | - | - |
| 21:25 | DOWN -0.02% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(0/2)) | - | - |
| 21:35 | UP +0.11% | SKIP(consensus) | SKIP(T-216) | SKIP(T-216) | SKIP(T-216) | SKIP(src(0/2)) | - | - |
| 21:40 | DOWN -0.10% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(0/2)) | - | - |
| 21:50 | DOWN -0.10% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(0/2)) | - | - |
| 21:55 | UP +0.02% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(0/2)) | - | - |
| 22:10 | UP +0.71% | TRADE DOWN [G] | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(src(0/2)) | - | - |
| 22:15 | UP +0.55% | SKIP(timing) | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(src(0/2)) | - | - |
| 22:20 | UP +0.03% | SKIP(timing) | SKIP(T-200) | SKIP(T-200) | SKIP(T-200) | SKIP(src(0/2)) | - | - |
| 22:35 | DOWN -0.14% | SKIP(consensus) | SKIP(T-230) | SKIP(T-230) | SKIP(T-230) | SKIP(src(1/2)) | - | - |
| 22:40 | UP +0.12% | SKIP(consensus) | SKIP(T-196) | SKIP(T-196) | SKIP(T-196) | SKIP(src(1/2)) | - | - |
| 22:55 | DOWN -0.18% | SKIP(consensus) | SKIP(T-232) | SKIP(T-232) | SKIP(T-232) | SKIP(src(1/2)) | - | - |
| 23:00 | DOWN -0.01% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(0/2)) | - | - |
| 23:05 | UP +0.10% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 23:10 | DOWN -0.06% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(0/2)) | - | - |
| 23:15 | UP +0.15% | TRADE DOWN [G] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 23:20 | UP +0.13% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 23:25 | UP +0.27% | SKIP(poly(p=0.399)) | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 23:30 | UP +0.05% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 23:35 | DOWN -0.19% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 23:40 | DOWN -0.17% | SKIP(early) | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(spread) | - | - |
| 23:45 | DOWN -0.09% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 23:50 | UP +0.08% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 23:55 | DOWN -0.15% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(no_conf) | - | - |
| **04/14** | | | | | | | | |
| 00:00 | DOWN -0.05% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(no_conf) | - | - |
| 00:05 | DOWN -0.17% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(no_conf) | - | - |
| 00:10 | UP +0.03% | TRADE DOWN [G] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 00:15 | UP +0.03% | TRADE DOWN [G] | SKIP(clob_size) | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 00:20 | DOWN -0.16% | TRADE DOWN [G] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(spread) | - | - |
| 00:25 | DOWN -0.02% | TRADE DOWN [G] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 00:30 | UP +0.03% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 00:35 | UP +0.19% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 00:40 | UP +0.05% | SKIP(poly(p=0.502)) | SKIP(dir) | SKIP(conf(0.002)) | SKIP(conf(0.002)) | SKIP(src(1/2)) | - | - |
| 00:45 | DOWN -0.33% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 00:50 | DOWN -0.17% | TRADE DOWN [G] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | TRADE DOWN [G] | - | - |
| 00:55 | DOWN -0.07% | TRADE DOWN [G] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | LOSS | -$3.25 |
| 01:00 | UP +0.04% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(cg_conf) | - | - |
| 01:05 | UP +0.13% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 01:10 | DOWN -0.02% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(delta) | LOSS | -$68.34 |
| 01:15 | DOWN -0.01% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 01:20 | DOWN -0.03% | SKIP(early) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 01:35 | UP +0.02% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 01:40 | UP +0.13% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 01:45 | UP +0.04% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 01:50 | UP +0.08% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 01:55 | DOWN -0.06% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 02:00 | UP +0.05% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 02:05 | UP +0.12% | SKIP(poly(p=0.460)) | SKIP(conf(0.040)) | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 02:10 | DOWN -0.04% | SKIP(poly(p=0.479)) | SKIP(conf(0.021)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 02:15 | UP +0.08% | TRADE DOWN [L] | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 02:20 | DOWN -0.05% | SKIP(timing) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 02:25 | DOWN -0.14% | SKIP(poly(p=0.419)) | SKIP(conf(0.081)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 02:30 | DOWN -0.13% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 02:35 | UP +0.16% | SKIP(poly(p=0.425)) | SKIP(conf(0.075)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 02:40 | UP +0.01% | SKIP(poly(p=0.536)) | SKIP(dir) | SKIP(conf(0.036)) | SKIP(conf(0.036)) | SKIP(src(1/2)) | - | - |
| 02:45 | DOWN -0.09% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(conf(0.003)) | SKIP(conf(0.003)) | SKIP(src(1/2)) | - | - |
| 02:50 | DOWN -0.16% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 02:55 | DOWN -0.03% | SKIP(poly(p=0.511)) | SKIP(dir) | SKIP(conf(0.011)) | SKIP(conf(0.011)) | SKIP(src(1/2)) | - | - |
| 03:00 | UP +0.00% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 03:05 | UP +0.10% | SKIP(timing) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 03:10 | DOWN -0.08% | TRADE DOWN [L] | SKIP(conf(0.008)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 03:15 | DOWN -0.02% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 03:20 | DOWN -0.11% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(spread) | - | - |
| 03:25 | UP +0.00% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 03:30 | UP +0.06% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | - | - |
| 03:35 | UP +0.04% | TRADE DOWN [L] | SKIP(conf(0.037)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 03:40 | UP +0.07% | TRADE DOWN [L] | SKIP(conf(0.011)) | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 03:45 | DOWN -0.02% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 03:50 | UP +0.01% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(conf(0.009)) | SKIP(conf(0.009)) | SKIP(src(1/2)) | - | - |
| 03:55 | DOWN -0.03% | SKIP(poly(p=0.479)) | SKIP(conf(0.021)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 04:00 | DOWN -0.00% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 04:05 | UP +0.00% | SKIP(poly(p=0.489)) | SKIP(conf(0.011)) | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 04:10 | DOWN -0.00% | TRADE DOWN [L] | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 04:15 | DOWN -0.01% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 04:20 | DOWN -0.02% | SKIP(poly(p=0.521)) | SKIP(dir) | SKIP(conf(0.021)) | SKIP(conf(0.021)) | SKIP(src(1/2)) | - | - |
| 04:25 | UP +0.04% | SKIP(poly(p=0.493)) | SKIP(conf(0.007)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 04:30 | DOWN -0.12% | SKIP(timing) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 04:35 | DOWN -0.00% | SKIP(poly(p=0.438)) | SKIP(conf(0.062)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 04:40 | DOWN -0.09% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 04:45 | UP +0.02% | SKIP(poly(p=0.443)) | SKIP(conf(0.057)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 04:50 | DOWN -0.03% | SKIP(poly(p=0.490)) | SKIP(conf(0.010)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 04:55 | DOWN -0.04% | SKIP(timing) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 05:00 | DOWN -0.09% | TRADE DOWN [L] | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 05:05 | UP +0.02% | SKIP(poly(p=0.567)) | SKIP(dir) | SKIP(conf(0.067)) | SKIP(conf(0.067)) | SKIP(src(1/2)) | - | - |
| 05:10 | UP +0.01% | SKIP(poly(p=0.581)) | SKIP(dir) | SKIP(conf(0.081)) | SKIP(conf(0.081)) | SKIP(src(1/2)) | - | - |
| 05:15 | UP +0.09% | SKIP(poly(p=0.383)) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 05:20 | DOWN -0.03% | TRADE DOWN [L] | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 05:25 | DOWN -0.02% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 05:30 | DOWN -0.05% | SKIP(poly(p=0.483)) | SKIP(conf(0.017)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 05:35 | DOWN -0.00% | SKIP(poly(p=0.526)) | SKIP(dir) | SKIP(conf(0.026)) | SKIP(conf(0.026)) | SKIP(src(1/2)) | - | - |
| 05:40 | UP +0.01% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 05:45 | DOWN -0.05% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 05:50 | DOWN -0.07% | SKIP(poly(p=0.385)) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 05:55 | UP +0.09% | SKIP(timing) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 06:00 | UP +0.10% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | - | - |
| 06:05 | UP +0.08% | TRADE DOWN [L] | SKIP(clob_size) | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 06:10 | DOWN -0.00% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(conf(0.010)) | SKIP(conf(0.010)) | SKIP(src(1/2)) | - | - |
| 06:15 | UP +0.11% | SKIP(poly(p=0.513)) | SKIP(dir) | SKIP(conf(0.013)) | SKIP(conf(0.013)) | SKIP(src(1/2)) | - | - |
| 06:20 | UP +0.04% | SKIP(poly(p=0.501)) | SKIP(dir) | SKIP(conf(0.001)) | SKIP(conf(0.001)) | SKIP(src(1/2)) | - | - |
| 06:25 | DOWN -0.09% | SKIP(timing) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 06:30 | UP +0.01% | SKIP(poly(p=0.516)) | SKIP(dir) | SKIP(conf(0.017)) | SKIP(conf(0.017)) | SKIP(src(1/2)) | - | - |
| 06:35 | UP +0.01% | TRADE DOWN [L] | SKIP(clob_size) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 06:40 | DOWN -0.05% | SKIP(timing) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 06:45 | UP +0.10% | SKIP(poly(p=0.517)) | SKIP(dir) | SKIP(conf(0.017)) | SKIP(conf(0.017)) | SKIP(src(1/2)) | - | - |
| 06:50 | UP +0.14% | SKIP(poly(p=0.535)) | SKIP(dir) | SKIP(conf(0.035)) | SKIP(conf(0.035)) | SKIP(conf(0.035)) | - | - |
| 06:55 | UP +0.02% | SKIP(poly(p=0.491)) | SKIP(conf(0.009)) | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 07:00 | UP +0.10% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(spread) | - | - |
| 07:05 | UP +0.20% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 07:10 | DOWN -0.25% | TRADE DOWN [L] | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(spread) | - | - |
| 07:15 | DOWN -0.13% | SKIP(timing) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 07:20 | DOWN -0.06% | SKIP(timing) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 07:25 | UP +0.04% | SKIP(poly(p=0.489)) | SKIP(conf(0.011)) | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 07:30 | DOWN -0.15% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 07:35 | UP +0.05% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 07:40 | DOWN -0.08% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 07:45 | DOWN -0.01% | TRADE DOWN [L] | SKIP(dir) | SKIP(conf(0.023)) | SKIP(conf(0.023)) | SKIP(src(1/2)) | - | - |
| 07:50 | UP +0.07% | SKIP(poly(p=0.532)) | SKIP(dir) | SKIP(conf(0.032)) | SKIP(conf(0.032)) | SKIP(src(1/2)) | - | - |
| 07:55 | UP +0.01% | SKIP(poly(p=0.524)) | SKIP(dir) | SKIP(conf(0.024)) | SKIP(conf(0.024)) | SKIP(src(1/2)) | - | - |
| 08:00 | UP +0.01% | TRADE DOWN [L] | SKIP(conf(0.086)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 08:05 | DOWN -0.03% | SKIP(poly(p=0.549)) | SKIP(dir) | SKIP(conf(0.049)) | SKIP(conf(0.049)) | SKIP(src(1/2)) | - | - |
| 08:10 | UP +0.02% | SKIP(poly(p=0.483)) | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 08:15 | UP +0.02% | SKIP(timing) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 08:20 | DOWN -0.01% | SKIP(poly(p=0.553)) | SKIP(dir) | SKIP(conf(0.053)) | SKIP(conf(0.053)) | SKIP(src(1/2)) | - | - |
| 08:25 | UP +0.27% | SKIP(poly(p=0.452)) | SKIP(conf(0.048)) | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 08:30 | UP +0.19% | SKIP(poly(p=0.489)) | SKIP(conf(0.011)) | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 08:40 | DOWN -0.18% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 08:45 | UP +0.10% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 08:50 | DOWN -0.03% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 08:55 | UP +0.05% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 09:00 | DOWN -0.13% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | WIN | +$4.45 |
| 09:05 | UP +0.04% | SKIP(early) | SKIP(dir) | SKIP(conf(0.005)) | SKIP(conf(0.005)) | SKIP(conf(0.005)) | - | - |
| 09:15 | DOWN -0.05% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 09:20 | DOWN -0.15% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 09:25 | DOWN -0.07% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 09:30 | DOWN -0.06% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 09:35 | DOWN -0.05% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 09:40 | DOWN -0.22% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 09:45 | DOWN -0.05% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 09:50 | UP +0.07% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(no_conf) | - | - |
| 09:55 | UP +0.03% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(no_conf) | - | - |
| 10:00 | DOWN -0.00% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 10:05 | UP +0.08% | TRADE DOWN [L] | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | - | - |
| 10:10 | UP +0.05% | SKIP(poly(p=0.521)) | SKIP(dir) | SKIP(conf(0.021)) | SKIP(conf(0.021)) | SKIP(src(1/2)) | - | - |
| 10:15 | FLAT 0.00% | SKIP(timing) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 10:20 | UP +0.01% | TRADE DOWN [L] | SKIP(clob_size) | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 10:25 | UP +0.04% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 10:30 | UP +0.04% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 10:35 | DOWN -0.21% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 10:40 | DOWN -0.07% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 10:45 | UP +0.05% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 10:50 | UP +0.06% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 10:55 | UP +0.01% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 11:00 | UP +0.05% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 11:05 | DOWN -0.01% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 11:10 | UP 0.00% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 11:15 | UP +0.08% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 11:20 | UP +0.01% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 11:25 | DOWN -0.08% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 11:30 | UP +0.02% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 11:35 | UP +0.04% | SKIP(poly(p=0.435)) | SKIP(conf(0.065)) | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 11:40 | DOWN -0.11% | TRADE DOWN [L] | SKIP(conf(0.012)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 11:45 | UP +0.13% | SKIP(poly(p=0.497)) | SKIP(conf(0.003)) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 11:50 | DOWN -0.07% | SKIP(timing) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(delta) | - | - |
| 11:55 | DOWN -0.01% | TRADE DOWN [L] | TRADE DOWN [L] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | LOSS | -$14.40 |
| 12:00 | UP +0.07% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 12:05 | DOWN -0.02% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 12:10 | DOWN -0.09% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 12:15 | UP +0.04% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 12:20 | DOWN -0.09% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 12:25 | UP +0.11% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 12:30 | UP +0.05% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(no_conf) | - | - |
| 12:35 | UP +0.04% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 12:40 | DOWN -0.01% | SKIP(consensus) | SKIP(T-236) | SKIP(T-236) | SKIP(T-236) | SKIP(src(1/2)) | - | - |
| 12:50 | UP +0.04% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 12:55 | UP +0.12% | SKIP(poly(p=0.572)) | SKIP(dir) | SKIP(conf(0.072)) | SKIP(conf(0.072)) | SKIP(conf(0.072)) | - | - |
| 13:00 | UP +0.18% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 13:05 | DOWN -0.05% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 13:10 | DOWN -0.01% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 13:15 | UP +0.07% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(taker) | LOSS | -$3.40 |
| 13:20 | UP +0.13% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 13:25 | DOWN -0.19% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 13:30 | UP +0.37% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 13:35 | UP +0.31% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 13:40 | UP +0.57% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(cg_conf) | - | - |
| 13:45 | DOWN -0.01% | SKIP(timing) | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 13:50 | UP +0.35% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(taker) | - | - |
| 13:55 | UP +0.14% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(cg_conf) | - | - |
| 14:00 | DOWN -0.13% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(cg_conf) | WIN | +$1.22 |
| 14:05 | UP +0.17% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(cg_conf) | - | - |
| 14:10 | DOWN -0.05% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | LOSS | -$18.75 |
| 14:15 | UP +0.06% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | WIN | +$5.26 |
| 14:20 | UP +0.12% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | - | - |
| 14:25 | UP +0.46% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(cg_conf) | - | - |
| 14:30 | DOWN -0.19% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(taker) | PENDING | +$0.00 |
| 14:35 | DOWN -0.49% | SKIP(timing) | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(spread) | - | - |
| 14:40 | DOWN -0.20% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 14:45 | DOWN -0.28% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 14:50 | UP +0.07% | TRADE DOWN [L] | SKIP(dir) | SKIP(conf(0.017)) | SKIP(conf(0.017)) | SKIP(src(1/2)) | - | - |
| 14:55 | DOWN -0.41% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(spread) | - | - |
| 15:00 | DOWN -0.22% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 15:05 | DOWN -0.39% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(spread) | - | - |
| 15:10 | UP +0.06% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 15:15 | UP +0.09% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 15:20 | UP +0.16% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(taker) | - | - |
| 15:25 | DOWN -0.03% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 15:30 | UP +0.44% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | - | - |
| 15:35 | UP +0.14% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | - | - |
| 15:40 | UP +0.17% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | - | - |
| 15:45 | UP +0.10% | SKIP(timing) | SKIP(not_advised) | SKIP(dir) | SKIP(dir) | SKIP(spread) | - | - |
| 15:50 | UP +0.18% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(cg_conf) | - | - |
| 15:55 | DOWN -0.10% | TRADE DOWN [L] | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(spread) | - | - |
| 16:00 | UP +0.19% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | - | - |
| 16:20 | DOWN -0.05% | SKIP(consensus) | SKIP(dir) | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |
| 16:25 | UP +0.03% | SKIP(poly(p=0.574)) | SKIP(dir) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | SKIP(conf(0.074)) | - | - |
| 16:30 | DOWN -0.02% | TRADE DOWN [L] | TRADE DOWN [G] | SKIP(dir) | SKIP(dir) | SKIP(src(1/2)) | - | - |

---

## Summary

- **Total windows evaluated:** 220
- **Windows with live Polymarket trades:** 9
- **Live trade results (oracle truth):** 3W / 5L = **38% WR**
- **Pending resolution:** 1
- **Total P&L (settled):** **$-97.20**

### Per-Strategy Performance (vs Binance BTC Direction)

Note: This uses Binance direction, which occasionally differs from Chainlink oracle. Useful for strategy-level accuracy but not for P&L truth.

| Strategy | Mode | Trades | Wins | Losses | Win Rate | SKIPs |
|---|---|---|---|---|---|---|
| v4_fusion | LIVE | 69 | 40 | 29 | **58%** | 141 |
| v4_fusion | GHOST | 10 | 5 | 5 | **50%** | - |
| v4_down_only | LIVE | 45 | 26 | 19 | **58%** | 159 |
| v4_down_only | GHOST | 16 | 11 | 5 | **69%** | - |
| v4_up_basic | LIVE | 0 | - | - | - | 220 |
| v4_up_asian | LIVE | 0 | - | - | - | 220 |
| v10_gate | LIVE | 0 | - | - | - | 219 |
| v10_gate | GHOST | 1 | 1 | 0 | **100%** | - |

### Hypothetical Analysis

- **v4_fusion all signals (LIVE+GHOST):** 45/79 = **57% WR**
- **v4_down_only all signals (LIVE+GHOST):** 37/61 = **61% WR**
- **Combined v4_fusion + v4_down_only:** 82/140 = **59% WR**
- **If we also traded v4_down_only GHOST signals:** +16 trades, 11W/5L = **69% WR**

### Market Context

- **BTC direction split (Binance):** 115 UP (52%) / 104 DOWN (47%)
- **Average |delta| per window:** 0.0974%
- **v4_up_basic/v4_up_asian:** All 220 windows SKIP (direction filter -- only trade UP, never passed gates)
- **v10_gate:** Only 1 GHOST trade in 220 windows (source agreement blocking -- needs 2 price sources)
- **All live Polymarket bets were DOWN** -- system is heavily DOWN-biased in this period

### Live Trade Detail (Polymarket CLOB)

| Window (UTC) | Bet | Cost (USDC) | Redeem (USDC) | P&L | Oracle Result |
|---|---|---|---|---|---|
| 04/14 00:55 | DOWN | $3.25 | $0.00 | -$3.25 | LOSS |
| 04/14 01:10 | DOWN | $68.34 | $0.00 | -$68.34 | LOSS |
| 04/14 09:00 | DOWN | $10.20 | $14.65 | +$4.45 | WIN |
| 04/14 11:55 | DOWN | $14.40 | $0.00 | -$14.40 | LOSS |
| 04/14 13:15 | DOWN | $3.40 | $0.00 | -$3.40 | LOSS |
| 04/14 14:00 | DOWN | $3.75 | $4.97 | +$1.22 | WIN |
| 04/14 14:10 | DOWN | $18.75 | $0.00 | -$18.75 | LOSS |
| 04/14 14:15 | DOWN | $6.80 | $12.06 | +$5.26 | WIN |
| 04/14 14:30 | DOWN | $6.60 | pending | +$0.00 | PENDING |
