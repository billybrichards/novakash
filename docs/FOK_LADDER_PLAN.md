# FOK Ladder Plan

**Status:** TODO — designed, not yet implemented
**Config:** `ORDER_PRICING_MODE=fokladder`
**Priority:** High — could capture 31 unfilled wins (96.9% WR) from current session

## The Problem

Current bestask mode: single GTC at bestAsk + 2¢, one retry. 
- Fill rate: ~30% (32 unfilled, 9 filled today)
- Unfilled trades would have won 96.9% (31/32)
- Filled trades only won 33.3% (3/9) — adverse selection
- Gamma price is fetched ONCE and goes stale within seconds

## The Solution: FOK Ladder

Rapid FOK attempts with fresh Gamma on each step, 2-second intervals.

### Flow

```
T-70: FULL EVALUATION
  ├─ Gamma API fetch (~200ms)
  ├─ VPIN (in-memory, ~0ms)
  ├─ Delta calculation (~0ms)
  ├─ TimesFM fetch (~1,400ms) ← only here, not on ladder
  ├─ TWAP evaluation (~0ms)
  ├─ CG veto check (~0ms)
  └─ DECISION: TRADE DOWN (or SKIP → abort)

T-67: LADDER STEP 1 (fast re-eval ~600ms total)
  ├─ Fetch fresh Gamma (~200ms)
  ├─ Check: delta still DOWN? VPIN still above gate?
  ├─ Check: floor ($0.30) ≤ bestAsk ≤ cap ($0.83)
  ├─ FOK at bestAsk + 1¢ (~200ms)
  └─ Result: FILLED → done! / MISS → continue

T-65: LADDER STEP 2
  ├─ Fetch fresh Gamma
  ├─ Fast re-eval (delta, VPIN, floor, cap)
  ├─ FOK at new bestAsk + 1¢
  └─ ...

T-63: LADDER STEP 3 ... (repeat)
T-61: LADDER STEP 4 ...
T-59: LADDER STEP 5 ...
T-57: LADDER STEP 6 ...
T-55: LADDER STEP 7 ...
T-53: LADDER STEP 8 ...
T-51: LADDER STEP 9 ...
T-49: LADDER STEP 10 ...

T-47: FALLBACK — place GTD at last Gamma + 2¢
  └─ Sits on book until window close (47 seconds)

T-0: Window closes, GTD auto-expires
```

### Fast Re-eval at Each Step

Each ladder step re-checks (all ~0ms except gamma):
- **Gamma fetch** (~200ms) — fresh market price
- **Delta direction** — has BTC flipped? If delta was -0.05% (DOWN) but now +0.03% (UP) → ABORT
- **VPIN** — still above 0.45 gate? If dropped below → ABORT  
- **Floor check** — bestAsk ≥ $0.30? If below → SKIP this step
- **Cap check** — bestAsk ≤ $0.83? If above → SKIP this step

**Does NOT re-run:** TimesFM (1.4s), TWAP (would be same data), CG veto (cached), full signal generation

### Abort Conditions

Stop the ladder immediately if:
1. Delta flips direction (was DOWN, now UP)
2. VPIN drops below gate (0.45)
3. Gamma bestAsk drops below floor ($0.30) — market turned against us
4. Already filled on a previous step
5. T-15 reached (need time for GTD fallback)

### Timing Budget

```
Per step: 200ms gamma + 200ms FOK + 100ms overhead = ~500ms
Interval: 2 seconds (leaves 1.5s buffer)
10 steps × 2s = 20 seconds (T-67 to T-47)
GTD fallback at T-47: 47 seconds on book
Total window coverage: 67 seconds of active pursuit
```

### Config

```env
ORDER_PRICING_MODE=fokladder    # Enable FOK ladder
FOK_LADDER_STEPS=10             # Max FOK attempts
FOK_LADDER_INTERVAL_MS=2000     # 2 seconds between steps
FOK_LADDER_BUMP=0.01            # +1¢ above bestAsk per FOK
FOK_PRICE_CAP=0.83              # Hard cap
PRICE_FLOOR=0.30                # Hard floor
```

### Three Modes (switchable via env)

| Mode | Config | Behaviour |
|------|--------|-----------|
| `bestask` | Current default | Single GTC at bestAsk + 2¢ |
| `cap` | Legacy | Single GTC at $0.73 cap |
| `fokladder` | New | Rapid FOK with fresh gamma, GTD fallback |

### Signal Component Modularity

Each evaluator should be independently toggleable:

```env
TIMESFM_ENABLED=true       # TimesFM direction forecast
TWAP_ENABLED=true           # TWAP trend analysis
CG_VETO_ENABLED=true        # CoinGlass smart money veto
TWAP_OVERRIDE_ENABLED=true  # Allow TWAP to flip delta direction
```

This allows testing combinations:
- Disable TWAP override → stop flipping UP signals to DOWN
- Disable TimesFM → faster eval, test if WR changes
- Disable CG veto → more trades, test if veto adds value

### Expected Impact

Current: 9 filled / 41 total = 22% fill rate, 33% WR on fills
Expected: ~25-30 filled / 41 = 60-70% fill rate
Key question: will the extra fills be from the 97% bucket or 33% bucket?

### Risks

1. **More adverse selection fills** — if the market moves against us and we chase, we fill at bad prices
2. **Gamma API rate limiting** — 10 fetches per window = ~120/hour. Should be fine.
3. **FOK spam** — 10 FOK orders per window. Polymarket may flag rapid submissions.
4. **Stale abort** — if we abort mid-ladder, we might have a pending FOK that fills after abort decision

### Polymarket Oracle Investigation (BLOCKING)

Before implementing, we MUST understand why "BTC went DOWN but oracle said UP":
- What price source does the oracle use? (Binance? Chainlink? Pyth?)
- What exact timestamp does it snapshot?
- Is it open→close or something else (TWAP, specific second)?

This affects whether better fill rates actually improve WR or just give us more "correct direction but oracle disagreed" losses.

## Montreal Rules

> ⚠️ ALL FOK ladder operations (gamma fetches, FOK submissions, fill checks) 
> run on Montreal engine. No Polymarket API calls from VPS or Railway.
