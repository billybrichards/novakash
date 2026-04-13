# Strategy Engine v2 — Config-First Registry + Fresh Data Surface

**Date:** 2026-04-13  
**Status:** Design  
**Branch:** TBD (from develop)

---

## 1. Problem Statement

The current strategy system has five problems:

1. **Timing override hack** — V4FusionStrategy (parent) hard-skips at `timing=early` (>T-180). Child strategies like v4_down_only must string-match `"timing=early" in decision.skip_reason` to reverse the parent's decision. Same hack for confidence threshold overrides. Every new strategy inherits this mess.

2. **Stale data at decision time** — V4 snapshot fetched via new HTTP session per eval tick (100-5000ms blocking). Tiingo/Chainlink/CLOB read from DB instead of in-memory caches. V3 composites not wired into StrategyContext at all. CoinGlass 10s stale.

3. **Adding strategies requires Python inheritance** — New strategies must subclass V4FusionStrategy, override `evaluate()`, and fight the parent's gates. No way to define a strategy from config.

4. **v4_up_asian is broken** — 0 trades from 19,490 decisions because thresholds are too tight. Fixing requires code changes, testing, deployment. Should be a YAML config tweak.

5. **No multi-timescale/asset path** — Everything is hardcoded to BTC 5m. No way to run the same gate pipeline on 15m BTC or ETH without duplicating code.

## 2. Design Goals

- **Every strategy gets the full data surface at every eval tick** — v2 probability, v3 multi-horizon (9 timescales), v4 macro/consensus/CLOB/regime/HMM, TimesFM quantiles, CoinGlass, all price deltas — all fresh, all in-memory
- **New strategies defined in YAML** — direction, timing, confidence, gates, sizing. No Python needed for simple filter strategies
- **Custom logic in separate .py files** — referenced by config, not inherited from parent classes
- **Documentation .md per strategy** — explains rationale, data analysis, performance expectations
- **Eliminates timing override problem** — each strategy owns its gates independently
- **Scales to new timescales** (15m BTC first) **and assets** (ETH, SOL, XRP)
- **Backward compatible** — existing production v4_down_only continues working during migration

## 3. Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                    Data Surface Layer                     │
│  Background pre-fetch loop (2s):                         │
│  ├─ V4 snapshot (persistent HTTP session → cache)        │
│  ├─ Tiingo/Chainlink/CLOB (in-memory from feeds)        │
│  ├─ CoinGlass (in-memory snapshot)                       │
│  ├─ VPIN + regime (in-memory from calculator)            │
│  ├─ Binance price (WS push)                             │
│  └─ V3 composites (from V4 snapshot or direct)           │
│                                                          │
│  Output: FullDataSurface (frozen dataclass, ~200 fields) │
│  Refreshed every 2s, read by all strategies              │
└──────────────────────────┬───────────────────────────────┘
                           │
                    FullDataSurface
                           │
┌──────────────────────────┴───────────────────────────────┐
│                   Strategy Registry                       │
│  Loads YAML configs at startup:                          │
│  ├─ v4_down_only.yaml → GatePipeline + hooks            │
│  ├─ v4_up_basic.yaml  → GatePipeline                    │
│  ├─ v4_up_asian.yaml  → GatePipeline                    │
│  ├─ v4_fusion.yaml    → GatePipeline + hooks (.py)      │
│  └─ v10_gate.yaml     → GatePipeline + hooks (.py)      │
│                                                          │
│  Each strategy: independently assembled gate pipeline    │
│  No inheritance. No parent decisions to override.        │
└──────────────────────────┬───────────────────────────────┘
                           │
              Per-strategy StrategyDecision
                           │
┌──────────────────────────┴───────────────────────────────┐
│                   Execution Layer                         │
│  LIVE strategy decision → order placement                │
│  GHOST strategy decisions → logged to strategy_decisions │
└──────────────────────────────────────────────────────────┘
```

## 4. Component Design

### 4.1 Data Surface Layer

**Purpose:** Assemble the complete data surface once, keep it fresh in memory, serve it to all strategies at every eval tick with zero blocking I/O.

**File:** `engine/strategies/data_surface.py`

```python
@dataclass(frozen=True)
class FullDataSurface:
    """Complete data surface available to all gates and strategies.
    
    Assembled every 2s by background loop. Read-only at decision time.
    ~200 fields covering v2/v3/v4/market/CLOB/CoinGlass/macro.
    """
    # Identity
    asset: str
    timescale: str
    window_ts: int
    eval_offset: int | None
    assembled_at: float          # Unix epoch — staleness check
    
    # Price (Binance WS, <100ms fresh)
    current_price: float
    open_price: float
    
    # Deltas (in-memory from feeds, <2s fresh)
    delta_binance: float | None
    delta_tiingo: float | None
    delta_chainlink: float | None
    delta_pct: float             # Primary (source-selected)
    delta_source: str
    
    # VPIN + Regime (in-memory, <1s fresh)
    vpin: float
    regime: str                  # CALM | NORMAL | TRANSITION | CASCADE
    
    # TWAP
    twap_delta: float | None
    
    # V2 Predictions (from V4 snapshot cache, <5s fresh)
    v2_probability_up: float | None
    v2_probability_raw: float | None
    v2_quantiles_p10: float | None
    v2_quantiles_p50: float | None
    v2_quantiles_p90: float | None
    
    # V3 Multi-Horizon Composites (9 timescales)
    v3_5m_composite: float | None
    v3_15m_composite: float | None
    v3_1h_composite: float | None
    v3_4h_composite: float | None
    v3_24h_composite: float | None
    v3_48h_composite: float | None
    v3_72h_composite: float | None
    v3_1w_composite: float | None
    v3_2w_composite: float | None
    
    # V3 Sub-Signals
    v3_sub_elm: float | None
    v3_sub_cascade: float | None
    v3_sub_taker: float | None
    v3_sub_oi: float | None
    v3_sub_funding: float | None
    v3_sub_vpin: float | None
    v3_sub_momentum: float | None
    
    # V4 Regime / HMM
    v4_regime: str | None        # calm_trend | volatile_trend | chop | risk_off
    v4_regime_confidence: float | None
    v4_regime_persistence: float | None
    
    # V4 Macro
    v4_macro_bias: str | None    # BULL | BEAR | NEUTRAL
    v4_macro_direction_gate: str | None  # ALLOW_ALL | LONG_ONLY | SHORT_ONLY
    v4_macro_size_modifier: float | None
    
    # V4 Consensus
    v4_consensus_safe_to_trade: bool | None
    v4_consensus_agreement_score: float | None
    v4_consensus_max_divergence_bps: float | None
    
    # V4 Conviction
    v4_conviction: str | None    # NONE | LOW | MEDIUM | HIGH
    v4_conviction_score: float | None
    
    # V4 Polymarket Outcome (from timesfm-repo)
    poly_direction: str | None   # UP | DOWN
    poly_trade_advised: bool | None
    poly_confidence: float | None
    poly_confidence_distance: float | None
    poly_timing: str | None      # early | optimal | late_window | expired
    poly_max_entry_price: float | None
    poly_reason: str | None
    
    # V4 Recommended Action
    v4_recommended_side: str | None
    v4_recommended_collateral_pct: float | None
    
    # V4 Sub-Signals
    v4_sub_signals: dict | None
    
    # V4 Quantiles
    v4_quantiles: dict | None
    
    # CLOB (in-memory from CLOBFeed, <2s fresh)
    clob_up_bid: float | None
    clob_up_ask: float | None
    clob_down_bid: float | None
    clob_down_ask: float | None
    clob_implied_up: float | None
    
    # Gamma (refreshed per window)
    gamma_up_price: float | None
    gamma_down_price: float | None
    
    # CoinGlass (in-memory snapshot, <10s fresh)
    cg_oi_usd: float | None
    cg_funding_rate: float | None
    cg_taker_buy_vol: float | None
    cg_taker_sell_vol: float | None
    cg_liq_total: float | None
    cg_liq_long: float | None
    cg_liq_short: float | None
    cg_long_short_ratio: float | None
    
    # TimesFM Quantiles (from V4 snapshot, <5s fresh)
    timesfm_expected_move_bps: float | None
    timesfm_vol_forecast_bps: float | None
    
    # Window metadata
    hour_utc: int | None
    seconds_to_close: int | None
```

**Background refresh loop:**

```python
class DataSurfaceManager:
    """Keeps FullDataSurface fresh in memory. No blocking I/O at decision time."""
    
    def __init__(self, v4_port, feeds, vpin_calc, cg_feeds, twap):
        self._v4_session: aiohttp.ClientSession  # PERSISTENT session
        self._cached_v4: V4Snapshot | None = None
        self._cached_surface: FullDataSurface | None = None
        self._refresh_interval = 2.0  # seconds
    
    async def start(self):
        """Start background refresh loop."""
        asyncio.create_task(self._refresh_loop())
    
    async def _refresh_loop(self):
        """Fetch V4 snapshot every 2s using persistent HTTP session."""
        while True:
            self._cached_v4 = await self._fetch_v4()
            await asyncio.sleep(self._refresh_interval)
    
    def get_surface(self, window, eval_offset) -> FullDataSurface:
        """Build surface from cached data. ZERO I/O. <1ms."""
        # All reads from in-memory caches
        return FullDataSurface(
            asset=window.asset,
            # ... populate from cached feeds, no DB queries
        )
```

**Data freshness improvement summary:**

| Data | Before | After |
|---|---|---|
| V4 snapshot | 100-5000ms blocking HTTP per eval | <1ms (read from 2s cache) |
| Tiingo delta | DB query per eval | <1ms (in-memory from feed) |
| Chainlink delta | DB query per eval | <1ms (in-memory from feed) |
| CLOB bid/ask | DB query per eval | <1ms (in-memory from feed) |
| V3 composites | Missing (None) | Available from V4 snapshot |
| CoinGlass | 10s stale | 10s stale (unchanged, acceptable) |
| Total _build_context latency | 200-5000ms | <5ms |

### 4.2 Gate Library

**Purpose:** Reusable, composable, pure Python gate functions. Each gate takes `FullDataSurface` and gate-specific params, returns `GateResult`.

**Location:** `engine/strategies/gates/`

**Base interface:**

```python
# engine/strategies/gates/base.py
@dataclass(frozen=True)
class GateResult:
    passed: bool
    gate_name: str
    reason: str
    data: dict = field(default_factory=dict)

class Gate(ABC):
    """Pure Python gate. No I/O. No external deps."""
    
    @property
    @abstractmethod
    def name(self) -> str: ...
    
    @abstractmethod
    def evaluate(self, surface: FullDataSurface) -> GateResult: ...
```

**Gate catalog:**

| Gate | File | Config Params | What It Does |
|---|---|---|---|
| `TimingGate` | `timing.py` | `min_offset`, `max_offset` | Checks eval_offset is in window |
| `DirectionGate` | `direction.py` | `direction: UP\|DOWN\|ANY` | Filters by prediction direction |
| `ConfidenceGate` | `confidence.py` | `min_dist`, `max_dist` (optional) | Checks distance from 0.5 |
| `SessionHoursGate` | `session_hours.py` | `hours_utc: [23,0,1,2]` | Filters by hour of day |
| `CLOBSizingGate` | `clob_sizing.py` | `schedule: [...]`, `null_modifier` | Sets position size from CLOB data |
| `SourceAgreementGate` | `source_agreement.py` | `min_sources`, `spot_only` | Price sources agree on direction |
| `DeltaMagnitudeGate` | `delta_magnitude.py` | `min_threshold` | Delta large enough to trade |
| `TakerFlowGate` | `taker_flow.py` | (none) | Taker buy/sell flow alignment |
| `CGConfirmationGate` | `cg_confirmation.py` | `oi_threshold`, `liq_threshold` | CoinGlass OI + liquidations confirm |
| `SpreadGate` | `spread.py` | `max_spread_bps` | CLOB spread reasonable |
| `DynamicCapGate` | `dynamic_cap.py` | `default_cap` | Set entry cap from confidence |
| `RegimeGate` | `regime.py` | `allowed: [calm_trend, volatile_trend]` | Filter by HMM regime |
| `MacroDirectionGate` | `macro_direction.py` | (none) | Macro bias alignment |
| `V3AlignmentGate` | `v3_alignment.py` | `min_timescales`, `min_agreement` | Cross-timescale v3 alignment |
| `TradeAdvisedGate` | `trade_advised.py` | (none) | V4 polymarket `trade_advised=true` |

**Key design decision:** Gates read from `FullDataSurface` directly — NOT from a V4FusionStrategy parent's decision. The `poly_timing` field is just data in the surface, not a hard gate. Each strategy config decides whether and how to use it.

### 4.3 Strategy Config System

**Purpose:** Define strategies in YAML. Engine loads configs, builds gate pipelines, registers strategies.

**Location:** `engine/strategies/configs/`

**Each strategy has up to 3 files:**
- `{name}.yaml` — **Required.** Gates, sizing, mode, asset, timescale
- `{name}.md` — **Required.** Documentation: rationale, data analysis, expected performance
- `{name}.py` — **Optional.** Custom hooks for complex logic

**YAML schema:**

```yaml
# Strategy config schema
name: string                    # Unique ID (e.g., "v4_down_only")
version: string                 # Semantic version
mode: LIVE | GHOST | DISABLED   # Execution mode
asset: string                   # BTC (future: ETH, SOL, XRP)
timescale: string               # 5m (future: 15m, 1h, 4h)

# Gate pipeline — executed in order, short-circuits on first failure
gates:
  - type: string                # Gate name from gate library
    params: dict                # Gate-specific parameters

# Position sizing
sizing:
  type: fixed_kelly | clob_dynamic | custom
  fraction: float               # Base Kelly fraction
  max_collateral_pct: float     # Cap
  custom_hook: string           # Function name in .py file (if type=custom)

# Optional: custom hooks file
hooks_file: string              # e.g., "v4_down_only.py"

# Optional: pre-gate hook (runs before gate pipeline)
pre_gate_hook: string           # Function name — can transform surface or early-exit

# Optional: post-gate hook (runs after all gates pass)
post_gate_hook: string          # Function name — can adjust sizing, add metadata
```

**Example configs:**

```yaml
# v4_down_only.yaml
name: v4_down_only
version: "2.0.0"
mode: LIVE
asset: BTC
timescale: 5m

gates:
  - type: timing
    params: { min_offset: 90, max_offset: 150 }
  - type: direction
    params: { direction: DOWN }
  - type: confidence
    params: { min_dist: 0.10 }
  - type: trade_advised
    params: {}

sizing:
  type: custom
  fraction: 0.025
  max_collateral_pct: 0.10
  custom_hook: clob_sizing

hooks_file: v4_down_only.py
```

```yaml
# v4_up_basic.yaml
name: v4_up_basic
version: "1.0.0"
mode: GHOST
asset: BTC
timescale: 5m

gates:
  - type: timing
    params: { min_offset: 60, max_offset: 180 }
  - type: direction
    params: { direction: UP }
  - type: confidence
    params: { min_dist: 0.10 }

sizing:
  type: fixed_kelly
  fraction: 0.025
  max_collateral_pct: 0.05
```

```yaml
# v4_up_asian.yaml
name: v4_up_asian
version: "2.0.0"
mode: GHOST
asset: BTC
timescale: 5m

gates:
  - type: timing
    params: { min_offset: 90, max_offset: 150 }
  - type: direction
    params: { direction: UP }
  - type: confidence
    params: { min_dist: 0.10, max_dist: 0.20 }
  - type: session_hours
    params: { hours_utc: [23, 0, 1, 2] }

sizing:
  type: fixed_kelly
  fraction: 0.025
  max_collateral_pct: 0.05
```

```yaml
# v4_fusion.yaml
name: v4_fusion
version: "4.1.0"
mode: GHOST
asset: BTC
timescale: 5m

# V4 fusion uses custom evaluation — the polymarket_v2 path has
# complex timing/CLOB-divergence logic that doesn't reduce to gates
gates: []  # Gates handled by custom hook

hooks_file: v4_fusion.py
pre_gate_hook: evaluate_polymarket_v2

sizing:
  type: fixed_kelly
  fraction: 0.025
```

```yaml
# v10_gate.yaml
name: v10_gate
version: "10.6.0"
mode: GHOST
asset: BTC
timescale: 5m

# V10 uses the full 8-gate pipeline
gates:
  - type: timing
    params: { min_offset: 5, max_offset: 300 }
  - type: source_agreement
    params: { min_sources: 2, spot_only: false }
  - type: delta_magnitude
    params: { min_threshold: 0.0005 }
  - type: taker_flow
    params: {}
  - type: cg_confirmation
    params: { oi_threshold: 0.01, liq_threshold: 1000000 }
  - type: confidence
    params: { min_dist: 0.12 }
  - type: spread
    params: { max_spread_bps: 100 }
  - type: dynamic_cap
    params: { default_cap: 0.65 }

hooks_file: v10_gate.py
post_gate_hook: classify_confidence

sizing:
  type: fixed_kelly
  fraction: 0.025
```

### 4.4 Strategy Registry

**Purpose:** Load YAML configs, build gate pipelines, register strategies, evaluate at each tick.

**File:** `engine/strategies/registry.py`

```python
class StrategyRegistry:
    """Loads strategy configs, builds pipelines, evaluates all strategies."""
    
    def __init__(self, config_dir: str, data_surface: DataSurfaceManager):
        self._configs: dict[str, StrategyConfig] = {}
        self._pipelines: dict[str, list[Gate]] = {}
        self._hooks: dict[str, module] = {}
        self._data_surface = data_surface
    
    def load_all(self):
        """Scan config_dir for *.yaml, build pipelines."""
        for yaml_file in Path(config_dir).glob("*.yaml"):
            config = self._parse_yaml(yaml_file)
            gates = self._build_pipeline(config)
            hooks = self._load_hooks(config) if config.hooks_file else None
            self._register(config, gates, hooks)
    
    async def evaluate_all(self, window, state) -> list[StrategyDecision]:
        """Evaluate all enabled strategies on the current data surface."""
        surface = self._data_surface.get_surface(window, window.eval_offset)
        
        decisions = []
        for name, config in self._configs.items():
            if config.mode == "DISABLED":
                continue
            decision = await self._evaluate_one(name, config, surface)
            decisions.append(decision)
        return decisions
    
    def _evaluate_one(self, name, config, surface) -> StrategyDecision:
        """Run one strategy's gate pipeline on the surface."""
        # Pre-gate hook (e.g., v4_fusion custom evaluation)
        if config.pre_gate_hook:
            hook_fn = self._hooks[name].get(config.pre_gate_hook)
            result = hook_fn(surface)
            if result is not None:
                return result  # Hook handled it (TRADE or SKIP)
        
        # Run gate pipeline
        for gate in self._pipelines[name]:
            result = gate.evaluate(surface)
            if not result.passed:
                return StrategyDecision(
                    action="SKIP",
                    skip_reason=f"{gate.name}: {result.reason}",
                    strategy_id=name,
                    ...
                )
        
        # All gates passed — determine direction + sizing
        # Direction priority: config fixed > poly_direction > v2_probability_up
        direction = self._determine_direction(config, surface)
        sizing = self._calculate_sizing(config, surface)
        
        # Post-gate hook (e.g., v10 confidence classification)
        if config.post_gate_hook:
            hook_fn = self._hooks[name].get(config.post_gate_hook)
            sizing = hook_fn(surface, sizing)
        
        return StrategyDecision(
            action="TRADE",
            direction=direction,
            strategy_id=name,
            ...
        )
```

### 4.5 Timing Override Elimination

**The root cause:** V4FusionStrategy reads `poly_timing` from the timesfm-repo response and hard-skips on `early`. Child strategies then hack around this with string matching.

**The fix:** In the config-first system, `poly_timing` is just a field on `FullDataSurface`. No strategy uses it as a hard gate unless it wants to. Each strategy defines its own `TimingGate(min_offset, max_offset)` based on eval_offset — which is the actual seconds-to-close, not the timesfm-repo's label.

```yaml
# v4_down_only: trades T-90 to T-150. No parent to override.
gates:
  - type: timing
    params: { min_offset: 90, max_offset: 150 }

# v4_up_basic: trades T-60 to T-180. Its own gate.
gates:
  - type: timing
    params: { min_offset: 60, max_offset: 180 }
```

The `poly_timing` field remains available on `FullDataSurface` for any strategy that WANTS to use it (e.g., a future strategy that only trades in `late_window` with CLOB divergence). But it's opt-in, not forced by inheritance.

**Same pattern for confidence:** V4FusionStrategy's 0.12 threshold is eliminated. Each strategy defines its own `ConfidenceGate(min_dist=X)`. v4_down_only uses 0.10, v10_gate uses 0.12, v4_up_basic uses 0.10.

### 4.6 Multi-Timescale / Multi-Asset Scaling

**Current state:** Everything hardcoded to BTC 5m.

**Design for scaling:**

```yaml
# Future: 15m BTC strategy
name: v4_down_15m
version: "1.0.0"
mode: GHOST
asset: BTC
timescale: 15m          # DataSurfaceManager fetches V4 snapshot for 15m

gates:
  - type: timing
    params: { min_offset: 180, max_offset: 600 }  # Wider window for 15m
  - type: direction
    params: { direction: DOWN }
  - type: confidence
    params: { min_dist: 0.10 }
```

**What needs to change for 15m support:**
1. `DataSurfaceManager` fetches V4 snapshot for multiple timescales (already supported — timesfm assembler handles `timescales: [5m, 15m]`)
2. `Polymarket5MinFeed` equivalent for 15m windows (or parameterize the existing feed)
3. Gamma/CLOB market lookup for 15m binary options (if Polymarket offers them)

**What needs to change for multi-asset:**
1. `DataSurfaceManager` runs per-asset refresh loops
2. Strategy config specifies `asset: ETH`
3. Feed infrastructure already supports multi-asset (Chainlink, Tiingo, CoinGlass all poll 4 assets)

### 4.7 Data Freshness Fixes

**Tier 1 — Eliminate blocking I/O at decision time:**

| Fix | File | Change |
|---|---|---|
| Persistent HTTP session for V4 | `data_surface.py` | Single `aiohttp.ClientSession` reused across all fetches |
| Background V4 pre-fetch (2s) | `data_surface.py` | Loop fetches V4, caches in memory |
| Read Tiingo from feed memory | `data/feeds/tiingo_feed.py` | Add `self._latest_prices: dict` updated on each poll |
| Read Chainlink from feed memory | `data/feeds/chainlink_feed.py` | Add `self._latest_prices: dict` updated on each poll |
| Read CLOB from feed memory | `data/feeds/clob_feed.py` | Add `self._latest_prices: dict` updated on each poll |

**Tier 2 — Enable missing data surfaces:**

| Fix | Where | Change |
|---|---|---|
| Enable V3 | Montreal env | Set `V3_ENABLED=true` on timesfm service |
| Wire V3 into surface | `data_surface.py` | Extract v3 composites from V4 snapshot into `FullDataSurface` |
| Wire V3 sub-signals | `data_surface.py` | Extract 7 sub-signals from V4 snapshot |

**Tier 3 — Timesfm-repo optimizations (optional, lower priority):**

| Fix | File | Change |
|---|---|---|
| Parallelize assembler pre-reads | `v4_snapshot_assembler.py` | `asyncio.gather(macro, events, consensus)` |
| Reduce CLOB TTL from 5s to 2s | `cross_region_fetcher.py` | Change `_TTL_CLOB_S = 2` |
| Cache V5FeatureLoader results (1s TTL) | `v5_feature_loader.py` | In-memory per-asset cache |
| Dedicated BTC forecast loop | `main.py` | BTC at 1s, others at 4s round-robin |

### 4.8 File Structure

```
engine/strategies/
├── data_surface.py               # DataSurfaceManager — background pre-fetch + cache
├── registry.py                   # StrategyRegistry — loads YAML, builds pipelines
├── gates/
│   ├── __init__.py
│   ├── base.py                   # Gate ABC + GateResult
│   ├── timing.py                 # TimingGate
│   ├── direction.py              # DirectionGate
│   ├── confidence.py             # ConfidenceGate
│   ├── session_hours.py          # SessionHoursGate
│   ├── clob_sizing.py            # CLOBSizingGate
│   ├── source_agreement.py       # SourceAgreementGate
│   ├── delta_magnitude.py        # DeltaMagnitudeGate
│   ├── taker_flow.py             # TakerFlowGate
│   ├── cg_confirmation.py        # CGConfirmationGate
│   ├── spread.py                 # SpreadGate
│   ├── dynamic_cap.py            # DynamicCapGate
│   ├── regime.py                 # RegimeGate
│   ├── macro_direction.py        # MacroDirectionGate
│   ├── v3_alignment.py           # V3AlignmentGate
│   └── trade_advised.py          # TradeAdvisedGate
├── configs/
│   ├── v4_down_only.yaml
│   ├── v4_down_only.md
│   ├── v4_down_only.py           # Custom: CLOB sizing hook
│   ├── v4_up_basic.yaml
│   ├── v4_up_basic.md
│   ├── v4_up_asian.yaml
│   ├── v4_up_asian.md
│   ├── v4_fusion.yaml
│   ├── v4_fusion.md
│   ├── v4_fusion.py              # Custom: polymarket_v2 evaluation
│   ├── v10_gate.yaml
│   ├── v10_gate.md
│   └── v10_gate.py               # Custom: 8-gate pipeline + DUNE
│
│   # Future:
│   ├── v4_down_15m.yaml          # 15m BTC DOWN
│   └── v4_down_15m.md
└── __init__.py
```

### 4.9 Domain Layer Reconciliation

The worktree (`novakash-clean-arch`) domain layer has good pure-Python types that overlap with the new system:

| Worktree File | Disposition |
|---|---|
| `domain/value_objects/signal_types.py` (GateContext, GateResult) | **Replace** with `FullDataSurface` + `gates/base.py GateResult` |
| `domain/value_objects/strategy_types.py` (StrategyDecision) | **Keep** — canonical VO, used by registry |
| `domain/services/gate_pipeline.py` (8 gates) | **Replace** with `strategies/gates/` library |
| `domain/entities/strategy.py` (IStrategy) | **Replace** with YAML config + registry |
| `domain/entities/gate.py` (IGate) | **Replace** with `gates/base.py Gate` ABC |
| `domain/ports.py` (15 ports) | **Keep** — canonical ports for infra layer |
| `domain/value_objects.py` (root, stubs) | **Delete** — duplicate of package |
| `application/ports/` (7 files) | **Delete** — dead code, domain ports canonical |
| `domain/enums/` (5 files) | **Keep** — Action, Direction, Confidence, etc. |
| `domain/exceptions.py` | **Keep** |
| `domain/constants.py` | **Keep** |

### 4.10 Audit Checklist Updates

New items to add to `AuditChecklist.jsx`:

| ID | Category | Severity | Title | Status |
|---|---|---|---|---|
| CA-07 | clean-architect | CRITICAL | Strategy Engine v2 — config-first registry replaces inheritance chain | OPEN |
| CA-08 | clean-architect | HIGH | Data Surface Layer — 1Hz fresh in-memory cache eliminates blocking I/O | OPEN |
| CA-09 | clean-architect | HIGH | Domain layer reconciliation — delete duplicates, merge worktree types | OPEN |
| SIG-05 | signal-optimization | HIGH | v4_up_basic strategy — global UP, dist≥0.10, T-60-180, all hours | OPEN |
| SIG-06 | signal-optimization | MEDIUM | v4_up_asian fix — relax thresholds via config (dist 0.10-0.20) | OPEN |
| DATA-FRESH-01 | data-quality | HIGH | V3 enablement — set V3_ENABLED=true on timesfm service | OPEN |
| DATA-FRESH-02 | data-quality | MEDIUM | Feed in-memory caches — Tiingo/Chainlink/CLOB expose latest prices | OPEN |

Update existing:

| ID | Change |
|---|---|
| CA-01 | Update status note: "Phase 4+ superseded by Strategy Engine v2 (CA-07). Config-first registry eliminates need for further god-class extraction." |
| SIG-03b | Update: "Timing override hack eliminated by CA-07 — each strategy owns its TimingGate independently." |
| SIG-04 | Update: "CLOBSizingGate now part of reusable gate library in CA-07." |

### 4.11 Migration Strategy

**Phase 1: Build infrastructure (no production changes)**
- DataSurfaceManager with background pre-fetch
- Gate library (16 gates)
- StrategyRegistry with YAML loading
- All 5 strategy configs + .md + .py hooks

**Phase 2: Parallel run**
- Wire StrategyRegistry into orchestrator alongside existing EvaluateStrategiesUseCase
- Both old and new systems evaluate every window
- Log decision comparison: `OLD_v4_down_only=SKIP NEW_v4_down_only=SKIP ✓`
- Alert on any mismatch

**Phase 3: Cutover**
- After 1 week of 0 mismatches: switch LIVE to new system
- Keep old code available for rollback
- Feature flag: `ENGINE_USE_STRATEGY_REGISTRY=true`

**Phase 4: Cleanup**
- Remove old strategy adapter classes
- Remove V4FusionStrategy inheritance chain
- Remove timing override hacks
- Delete dead `application/ports/` directory

## 5. Success Criteria

- [ ] All 5 strategies produce identical decisions to current system (parallel run verification)
- [ ] Zero blocking I/O at decision time (<5ms _build_context)
- [ ] V3 composites (9 timescales) available in FullDataSurface
- [ ] New strategy addable with YAML config + .md doc in <30 minutes
- [ ] v4_up_basic generating 5-15 TRADE decisions per day in GHOST mode
- [ ] v4_up_asian generating >0 TRADE decisions (currently 0)
- [ ] No regressions in v4_down_only production behavior
- [ ] Audit checklist updated with new CA/SIG/DATA items

## 6. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Decision mismatch during parallel run | Medium | High | 1-week parallel run, alert on any diff |
| V4 snapshot cache serves stale data | Low | Medium | 2s refresh + staleness check on surface |
| YAML config parsing errors at startup | Low | High | Schema validation + startup health check |
| Custom .py hooks have import errors | Low | Medium | Load hooks at startup, fail fast |
| V3 enablement causes timesfm instability | Low | Medium | Monitor timesfm after V3_ENABLED=true |

## 7. Out of Scope

- DB-backed config editing (future — when CFG-01 lands)
- UI for strategy creation (future — needs config API)
- WebSocket push from timesfm-repo (future — Phase 5 `/v4/stream`)
- Backtesting framework integration
- Multi-account support (MULTI-ACCOUNT-01 is separate)
