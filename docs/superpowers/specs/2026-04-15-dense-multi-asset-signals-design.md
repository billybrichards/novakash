# Dense Multi-Asset Signal Collection — Design Spec

**Date:** 2026-04-15
**Author:** Claude (brainstorm w/ Billy)
**Supersedes:** Task #165 (Phase 4 of Note #35 — "engine writes 15m signal_evaluations at 5m cadence")
**Related:** Notes #35, #23; Tasks #160, #163, #164, #165

---

## 1. Intent

Shadow-only dense collection of `signal_evaluations` rows across the **full window** at **2-second cadence** for **BTC, ETH, SOL, XRP** on **5m and 15m** Polymarket up/down windows.

Zero impact on live trading. Pure training-data fuel for ML retrains.

### 1.1 Why

- Current 15m `signal_evaluations` = 814 rows/week (task #160 — too sparse to train).
- Current 5m offsets: `T-240 → T-60` only (middle of window). Missing `T-298 → T-242` (pre-close signals) and `T-58 → T-2` (deadline-proximity signals).
- Multi-asset (ETH/SOL/XRP) adds 3× more training substrate with zero extra trading risk.
- ML eval (task #153) needs post-CoinGlass-fix data for ETH/SOL/XRP retrains.

### 1.2 Non-goals

- **No** trade execution at new offsets. Existing strategy runners (V4, v4_fusion, v15m_*) keep their exact current entry offsets.
- **No** promotion of any model or strategy. This is data collection only. (Per feedback `feedback_no_auto_model_promotion.md`.)
- **No** touching strategy gates or registry. Registry-driven paths untouched.

---

## 2. Scope

### 2.1 Asset × Timeframe matrix

| | 5m (300s) | 15m (900s) |
|---|---|---|
| BTC | ✅ dense | ✅ dense |
| ETH | ✅ dense | ✅ dense |
| SOL | ✅ dense | ✅ dense |
| XRP | ✅ dense | ✅ dense |

Polymarket slugs (confirmed, feed already supports): `{asset}-updown-{5m|15m}-{window_ts}` via `https://gamma-api.polymarket.com/events?slug=...`.

### 2.2 Offset coverage

| Timeframe | Current offsets | New offsets | Count |
|---|---|---|---|
| 5m | T-240 → T-60 (every 2s) | **T-298 → T-2 (every 2s)** | 149 |
| 15m | T-600 → T-60 (every 2s) | **T-898 → T-2 (every 2s)** | 449 |

### 2.3 Volume projection

Per-asset-per-window rows:
- 5m: 149 rows × 12 windows/hr = 1,788/hr
- 15m: 449 rows × 4 windows/hr = 1,796/hr
- **Per asset total:** 3,584 rows/hr = ~86k/day

4 assets: **~14k rows/hr = 344k/day = ~2.4M/week**

DB write rate: 14k/3600 ≈ **3.9 writes/sec**. PG comfortable.
Gamma API rate: 8 streams × (12+4) new-windows/hr = **~128 req/hr** (0.036/sec). Trivial.

---

## 3. Architecture (Clean Architecture)

Current `engine/` structure:
```
engine/
├── domain/            # entities.py, ports.py, value_objects.py (flat)
├── use_cases/         # evaluate_window.py, evaluate_strategies.py, ...
├── adapters/          # polymarket/, persistence/, strategies/, ...
├── infrastructure/    # composition.py (DI), runtime.py
├── data/feeds/        # infra: Polymarket5MinFeed, ChainlinkFeed, TiingoFeed
├── signals/           # window_evaluator.py (domain-flavored signal math)
```

Gaps detected in current code:
- `WindowInfo` lives in `engine/data/feeds/polymarket_5min.py:42-62` (infra layer) but is a pure domain concept.
- `engine/use_cases/evaluate_window.py:151-196` performs inline `aiohttp` calls to Tiingo — application layer importing infra client. CA anti-pattern ("Business Logic in Routes" variant).

### 3.1 Layer map

| Layer | New / Change | File |
|---|---|---|
| **Domain** | Extend `value_objects.py` with `Asset`, `Timeframe`, `EvalOffset` frozen VOs | `engine/domain/value_objects.py` |
| **Domain** | Move `WindowInfo` → domain entity `Window` | `engine/domain/entities.py` |
| **Application (ports)** | New: `IPriceGateway`, `IMarketDiscovery` | `engine/domain/ports.py` |
| **Application (UC)** | New: `CollectDenseSignalsUseCase` | `engine/use_cases/collect_dense_signals.py` |
| **Application (UC)** | Refactor: `EvaluateWindowUseCase` accepts `IPriceGateway`, adds `skip_trade: bool` param | `engine/use_cases/evaluate_window.py` |
| **Infrastructure** | New: `GammaMarketDiscovery` (impl `IMarketDiscovery`) | `engine/adapters/polymarket/gamma_discovery.py` |
| **Infrastructure** | New: `CompositePriceGateway` (impl `IPriceGateway`) | `engine/adapters/market_feed/composite_price_gateway.py` |
| **Infrastructure** | Trim: `Polymarket5MinFeed` delegates market discovery to new adapter | `engine/data/feeds/polymarket_5min.py` |
| **Presentation** | Env flags + composition wiring | `engine/infrastructure/composition.py`, `.env.example` |

### 3.2 Dependency direction

```
Presentation (composition.py)
    ↓
Infrastructure (GammaMarketDiscovery, CompositePriceGateway)
    ↓ implements
Application ports (IPriceGateway, IMarketDiscovery)
    ↑ used by
Application use case (CollectDenseSignalsUseCase)
    ↑ uses
Domain (Asset, Timeframe, EvalOffset, Window)
```

Domain has ZERO external deps. Application depends only on domain + ports. Infra depends on application ports + external libs (httpx, aiohttp). Presentation wires all.

---

## 4. Domain changes

### 4.1 Value Objects (`engine/domain/value_objects.py`)

```python
SUPPORTED_ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"})
SUPPORTED_DURATIONS = frozenset({300, 900})

@dataclass(frozen=True)
class Asset:
    symbol: str
    def __post_init__(self):
        normalized = self.symbol.upper().strip()
        object.__setattr__(self, "symbol", normalized)
        if normalized not in SUPPORTED_ASSETS:
            raise ValueError(f"unsupported asset {self.symbol!r}")

@dataclass(frozen=True)
class Timeframe:
    duration_secs: int
    def __post_init__(self):
        if self.duration_secs not in SUPPORTED_DURATIONS:
            raise ValueError(f"unsupported timeframe {self.duration_secs}s")
    @property
    def label(self) -> str:
        return "15m" if self.duration_secs == 900 else "5m"

@dataclass(frozen=True)
class EvalOffset:
    seconds_before_close: int
    def __post_init__(self):
        if not 2 <= self.seconds_before_close <= 898:
            raise ValueError(f"offset {self.seconds_before_close}s out of range")
```

### 4.2 Entity (`engine/domain/entities.py`)

Move `WindowInfo` from `engine/data/feeds/polymarket_5min.py:42-62`. Rename → `Window`. Strip any infra coupling (there is none; it's already a pure dataclass). Keep `WindowState` enum alongside it.

`Polymarket5MinFeed` re-imports `Window` from domain (no breaking change to callsites — they use the feed's `get_current_window()` accessor which keeps same return shape).

---

## 5. Application ports (`engine/domain/ports.py` additions)

Two new domain DTOs (plain frozen dataclasses in `engine/domain/value_objects.py`):

```python
@dataclass(frozen=True)
class PriceCandle:
    """(open, close) pair for a given window. open_ts = window_ts."""
    open_price: float
    close_price: float
    source: str  # "tiingo_rest" | "tiingo_db" | "chainlink"

@dataclass(frozen=True)
class WindowMarket:
    """Polymarket market metadata for a given window."""
    up_token_id: str
    down_token_id: Optional[str]
    up_price: Optional[float]
    down_price: Optional[float]
    price_source: str  # "gamma_api" | "stale_gamma" | "synthetic"
```

Ports:

```python
class IPriceGateway(ABC):
    """Per-asset current price. Routes to Chainlink / Tiingo / Binance under the hood."""
    @abstractmethod
    async def get_current_price(self, asset: Asset) -> Optional[float]: ...
    @abstractmethod
    async def get_window_candle(
        self, asset: Asset, window_ts: int, tf: Timeframe
    ) -> Optional[PriceCandle]:
        """For delta-vs-open computations."""

class IMarketDiscovery(ABC):
    """Polymarket window → token ids + prices."""
    @abstractmethod
    async def find_window_market(
        self, asset: Asset, tf: Timeframe, window_ts: int
    ) -> Optional[WindowMarket]: ...
```

---

## 6. New use case (`engine/use_cases/collect_dense_signals.py`)

```python
class CollectDenseSignalsUseCase:
    """
    Shadow-only dense signal_evaluations writer.

    Every 2s, for each configured (asset, timeframe) pair:
      1. Compute current window_ts (floor of now / duration).
      2. If new window: call discovery → resolve token ids.
      3. Compute current eval_offset = duration - (now - window_ts).
      4. If offset ∈ [2, duration-2] and not already written: invoke
         EvaluateWindowUseCase(window, skip_trade=True).
      5. The use case writes the signal_evaluations row via the existing
         ISignalEvaluationRepo.

    Never calls order manager. Never fires strategies. Pure write path.
    """
    def __init__(
        self,
        assets: list[Asset],
        timeframes: list[Timeframe],
        price_gw: IPriceGateway,
        discovery: IMarketDiscovery,
        evaluate_window_uc: EvaluateWindowUseCase,
        clock: IClock,
    ): ...

    async def tick(self) -> None: ...
```

### 6.1 `EvaluateWindowUseCase` refactor (`engine/use_cases/evaluate_window.py`)

- Add `skip_trade: bool = False` param on `execute()`. When True, the use case computes the signal + writes `signal_evaluations` but skips any trade-path branching.
- Inline Tiingo `aiohttp` calls at [evaluate_window.py:151-196](engine/use_cases/evaluate_window.py:151) move to `IPriceGateway.get_window_candle()`. Use case calls `await self._price_gw.get_window_candle(...)` instead.
- Inline per-asset branching at [evaluate_window.py:131-138](engine/use_cases/evaluate_window.py:131) (BTC special-case) moves to `IPriceGateway.get_current_price(asset)` — gateway routes BTC → Binance spot, others → Chainlink/Tiingo internally.

Existing callers (`EvaluateStrategiesUseCase`, strategy runners) pass `skip_trade=False` — zero behavior change.

---

## 7. Infrastructure adapters

### 7.1 `GammaMarketDiscovery` (`engine/adapters/polymarket/gamma_discovery.py`)

Extracts logic from `Polymarket5MinFeed._fetch_market_data` ([polymarket_5min.py:415-485](engine/data/feeds/polymarket_5min.py:415)).

```python
class GammaMarketDiscovery(IMarketDiscovery):
    def __init__(self, http_client: httpx.AsyncClient): ...
    async def find_window_market(self, asset, tf, window_ts) -> Optional[WindowMarket]:
        slug = f"{asset.symbol.lower()}-updown-{tf.label}-{window_ts}"
        r = await self._http.get("https://gamma-api.polymarket.com/events", params={"slug": slug})
        # parse response → WindowMarket
```

`Polymarket5MinFeed` keeps its own instance for backward compat (no behavior change for existing flows) OR takes a `IMarketDiscovery` via constructor injection.

### 7.2 `CompositePriceGateway` (`engine/adapters/market_feed/composite_price_gateway.py`)

```python
class CompositePriceGateway(IPriceGateway):
    def __init__(
        self,
        chainlink_feed: ChainlinkFeed,
        tiingo_feed: TiingoFeed,
        binance_spot_feed: BinanceWebSocketFeed,
        db: DBClient,  # for db_tick fallback
        tiingo_api_key: str,
    ): ...

    async def get_current_price(self, asset: Asset) -> Optional[float]:
        if asset.symbol == "BTC":
            return self._binance_spot.latest_price  # fastest
        # ETH/SOL/XRP: Chainlink primary
        if p := self._chainlink.latest_prices.get(asset.symbol):
            return p
        # fallback: latest Tiingo tick from DB
        return await self._db.get_latest_tiingo_price(asset.symbol)

    async def get_window_candle(self, asset, window_ts, tf):
        # Current inline aiohttp call from evaluate_window.py, moved here.
        ...
```

---

## 8. Composition wiring (`engine/infrastructure/composition.py`)

```python
# ── Dense Multi-Asset Signal Collection (task #165 superset) ────
dense_enabled = os.environ.get("DENSE_SIGNALS_ENABLED", "false").lower() == "true"
if dense_enabled:
    dense_assets_raw = os.environ.get("DENSE_SIGNALS_ASSETS", "BTC,ETH,SOL,XRP")
    dense_tfs_raw = os.environ.get("DENSE_SIGNALS_TIMEFRAMES", "5m,15m")
    dense_assets = [Asset(s.strip()) for s in dense_assets_raw.split(",")]
    dense_tfs = [Timeframe(900 if t.strip() == "15m" else 300) for t in dense_tfs_raw.split(",")]

    self._price_gateway = CompositePriceGateway(
        chainlink_feed=self._chainlink_feed,
        tiingo_feed=self._tiingo_feed,
        binance_spot_feed=self._binance_spot_feed,
        db=self._db_client,
        tiingo_api_key=settings.tiingo_api_key,
    )
    self._market_discovery = GammaMarketDiscovery(httpx.AsyncClient(timeout=10.0))

    self._dense_signals_uc = CollectDenseSignalsUseCase(
        assets=dense_assets,
        timeframes=dense_tfs,
        price_gw=self._price_gateway,
        discovery=self._market_discovery,
        evaluate_window_uc=self._evaluate_window_uc,
        clock=self._clock,
    )
    # Orchestrator registers dense_signals_uc.tick() every 2s
    self._orchestrator.add_periodic(self._dense_signals_uc.tick, interval_secs=2.0)
    log.info("dense_signals.enabled", assets=[a.symbol for a in dense_assets],
             timeframes=[tf.label for tf in dense_tfs])
```

### 8.1 Env vars (`.env.example`)

```bash
# ── Dense multi-asset signal collection (shadow-only, task #165) ──
DENSE_SIGNALS_ENABLED=false           # set true on Montreal when ready
DENSE_SIGNALS_ASSETS=BTC,ETH,SOL,XRP
DENSE_SIGNALS_TIMEFRAMES=5m,15m
```

---

## 9. Rollout (5 PRs, incremental)

| PR | Content | Behavior change? |
|---|---|---|
| **PR-1** | Domain VOs (`Asset`, `Timeframe`, `EvalOffset`) + move `Window` to `entities.py` | None — additive |
| **PR-2** | Introduce `IPriceGateway`, `IMarketDiscovery` ports + migrate Tiingo/Gamma HTTP from `evaluate_window.py` into new adapters; inject gateway into existing UC | None — refactor, existing callers unchanged |
| **PR-3** | `CollectDenseSignalsUseCase` + `skip_trade` flag on `EvaluateWindowUseCase` + composition wiring; `DENSE_SIGNALS_ENABLED=false` by default | None — gated off |
| **PR-4** | Montreal deploy, flip `DENSE_SIGNALS_ENABLED=true` for **BTC 5m only**; 24h verify: row rate, DB pressure, Gamma 429s, disk IO | **Shadow rows only** |
| **PR-5** | Expand `DENSE_SIGNALS_ASSETS=BTC,ETH,SOL,XRP` + both timeframes. Full production. | **Shadow rows only** |

Each PR has CI green + the verification-before-completion skill. PR-4 requires 24h observability soak before PR-5 flip.

### 9.1 Deployment constraint

Per memory `feedback_five_min_enabled.md`: Montreal must have `FIVE_MIN_ENABLED=true` set (independent of this flag — dense collection piggybacks the existing feed orchestration).

Per memory `reference_montreal_deploy.md`: deploy via rsync, verify with grep on Montreal-side files.

Per memory `feedback_no_direct_develop.md`: each PR goes through develop via PR, not direct push.

---

## 10. Testing

### 10.1 Domain (no mocks)

- `engine/tests/unit/domain/test_value_objects.py` — Asset/Timeframe/EvalOffset validation boundaries.
- `engine/tests/unit/domain/test_window_entity.py` — state machine transitions.

### 10.2 Application (with mocks — `InMemory*` fakes)

- `engine/tests/unit/use_cases/test_collect_dense_signals.py`:
  - emits one row per (asset, tf) per tick at correct offset
  - skips when offset outside [2, duration-2]
  - de-dupes: does not write twice for same (asset, ts, offset)
  - never invokes order_manager (assert absent from mock)

- Update `engine/tests/unit/use_cases/test_evaluate_window.py`:
  - `skip_trade=True` path writes row, does not call trade path
  - injected `IPriceGateway` used instead of inline HTTP

### 10.3 Infra (smoke)

- `engine/tests/integration/test_gamma_discovery.py` — hit real gamma-api with fixed-past window, assert parse.
- `engine/tests/integration/test_composite_price_gateway.py` — real Chainlink + Tiingo feeds, assert all 4 assets return non-None.

### 10.4 Shadow verification (post-deploy PR-4)

24h Montreal soak; audit-task accept criteria:
- [ ] `signal_evaluations` row rate ≥ 3.5/sec sustained
- [ ] Gamma 4xx/5xx rate < 0.5%
- [ ] No `order_manager.place_order` calls from dense UC (log audit)
- [ ] DB disk growth < 100 MB/day (compressed)
- [ ] Existing strategy trades unaffected (compare trade counts ±10%)

---

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Binance WS BTC-only → VPIN/liq-surge blind for ETH/SOL/XRP | Accept. Row still has price+delta+chainlink+tiingo features. Future PR can add per-asset Binance streams if VPIN value proven. |
| Gamma API rate-limit on new-window bursts | 128 req/hr well under any public API budget. Still: add exponential backoff on 429 in `GammaMarketDiscovery`. |
| DB write amplification triggering WAL bloat | PR-4 soak measures this. Row is narrow (~200 bytes). 2.4M/week × 200B = 480 MB/week. Acceptable. |
| `skip_trade` path accidentally triggers trade (leak) | Test asserts order_manager mock has zero calls. Runtime log audit in PR-4. |
| Chainlink stale for ETH/SOL/XRP | `CompositePriceGateway` has Tiingo DB fallback. Both feeds already multi-asset per Phase 2 of main CLAUDE.md. |
| Memory pressure from tracking 8 concurrent windows | Per-stream state is ~1 KB. Negligible. |

---

## 12. Success criteria

After PR-5 + 1 week in production:

1. `signal_evaluations` count per (asset, tf) ≥ 50k rows/week → sufficient for 15m retrains (task #160 acceptance).
2. ETH/SOL/XRP retrains (task #153) run on post-CoinGlass-fix dense data.
3. Zero trade-path regressions (trade count week-over-week within normal variance).
4. Phase 2 (note #35) synthetic-15m work becomes optional — dense path supersedes it as data source.

---

## 13. Open questions

- **None blocking.** Billy confirmed: shadow-only (implicit from ML-training framing), 4 assets (BTC/ETH/SOL/XRP), treat as superset of task #165.

---

## 14. Out of scope (deferred)

- Per-asset Binance WS streams (ETH/SOL/XRP VPIN). Own spec if signal-value proven.
- DOGE/BNB. Add later once 4-asset baseline stable.
- 1h / 4h timeframes (task #161). Separate feed path, not just offset expansion.
- Synthetic 15m windows from ticks_binance (Phase 2 of note #35). Orthogonal; still useful for backfill of regimes without Polymarket markets.
- Cross-timeframe nested features (Phase 3 of note #35). Trainer-side work; this spec feeds that.
