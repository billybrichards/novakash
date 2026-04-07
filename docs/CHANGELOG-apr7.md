# CHANGELOG — April 7, 2026

## Session Results

```
Starting wallet:  $130.82 USDC
Ending wallet:    $164.16 USDC
PROFIT:           +$33.34 (+25.5%)

Resolved: 40 trades (28W/12L — 70.0% WR)
Expired:  47 orders (unfilled, zero cost)
Open:     0
```

## Timeline

### Phase 1: v8.0→v8.1 Deployment (20:21–01:00 UTC, Apr 6-7)
- 15+ bugs found and fixed in execution path
- FOK ladder, GTC fallback, multi-offset eval deployed
- v2.2 gate extended to ALL offsets

### Phase 2: Pre-Fix Trading (00:00–09:50 UTC)
- **23W/9L (71.9% WR)**
- ALL fills at ~$0.73 due to ORDER_PRICING_MODE=cap + V81_CAP_T240=0.73 in .env
- Avg win: +$2.90, Avg loss: -$8.50
- v8_standard trades (no v2.2 gate) accounted for 7/9 losses

### Phase 3: Pricing Fix (09:50 UTC)
Root cause found: hidden `.env` on Montreal overriding dynamic caps.
- `V81_CAP_T240=0.73` → fixed to `0.55`
- `ORDER_PRICING_MODE=cap` → removed (dead code now)
- FOK decimal precision fixed (was 100% failure rate)
- Fill price calc fixed (was stake/shares, now uses limit price)
- RFQ cap fixed (was hardcoded, now uses dynamic cap)

### Phase 4: Post-Fix Trading (09:50–12:45 UTC)
- **5W/3L (62.5% WR)** — lower WR but much better economics
- Avg win: +$5.85 (2x improvement from $2.90)
- Fills at $0.65 instead of $0.73
- Both T-70 losses were NORMAL regime (weakest signals)

### Phase 5: Chrome Kill (12:40 UTC)
- Discovered Google Chrome running since Apr 4 with Polymarket open on VNC
- Chrome's Polymarket session was creating additional positions on the same wallet
- Killed Chrome — wallet jumped from $79 to $164 as positions resolved
- Now only our engine trades on the wallet

## Commits (develop branch)

| Commit | Description |
|--------|-------------|
| `6f232d2` | FOK decimal precision + GTC uses dynamic cap |
| `78110de` | gate_audit type mismatch fix |
| `2ce445e` | trade_placed flag in actual execute path |
| `54a477d` | Cap bands: T-70=$0.73, T-240=$0.55 |
| `d10bbdb` | 8x-pricing-execution docs |
| `089c56e` | Post-fix analysis report |
| `1496d9e` | RFQ dynamic cap + GTC fill notification + session reload |
| `bfd50ac` | Result notification uses DB not Polymarket aggregate |
| `1a3308a` | Fill price calc (limit price, not stake/shares) |
| `01215ec` | Notification accuracy (real caps, no Gamma) |
| `bb2c9d7` | SITREP W/L from DB (survives restarts) |
| `3896fca` | Notification TODOs doc |

## Dynamic Cap Schedule (LIVE)

```
Offset          Cap     Applied Since
T-240..T-180    $0.55   09:50 UTC
T-170..T-120    $0.60   09:50 UTC
T-110..T-80     $0.65   09:50 UTC  
T-70..T-60      $0.73   09:50 UTC
```

## Known Issues

1. **DB pnl_usd unreliable** — pre-fix values used wrong fill price calc. Wallet is ground truth.
2. **NORMAL regime at T-70** — both post-fix losses were VPIN 0.49/0.54. Consider requiring TRANSITION+ at late offsets.
3. **Gamma ↑$0.500 ↓$0.500** still shows in window header — cosmetic, not used for pricing.
4. **STARTING_BANKROLL in .env** needs updating to $164.16 for accurate drawdown tracking.

## Files on Develop

- `docs/CHANGELOG-apr7.md` — this file
- `docs/8x-pricing-execution.md` — execution audit + resolution
- `docs/analyses/2026-04-07-overnight-session.md` — overnight analysis
- `docs/analyses/2026-04-07-pricing-fix-report.md` — post-fix report
- `docs/LIVE_DATA_RULES.md` — data analysis rules
- `docs/TODO-notifications.md` — notification fix tracker
