"""
Engine-side assembler for the Sequoia v5 feature body.

Sequoia v5 ships a training refactor (one wide LightGBM model over
`eval_offset`, sourced from `signal_evaluations`) but its serving path
was never wired up: the Montreal scorer's pull-mode feature assembly
still produces v4 feature names, so every v5 inference received 25
NaNs and returned the all-missing leaf — the constant 0.60614485 we
caught in production on 2026-04-10.

The permanent fix is push-mode: the engine (which already computes all
25 of v5's features at decision time) sends them in a POST body, the
scorer uses the body directly, and there is no pull-mode feature
assembly to drift against training. This module owns the engine's half
of that contract.

## Train/serve parity rules

Every field here mirrors `FEATURE_COLUMNS_V5` in
`training/train_lgb_v5.py`. If a field is renamed, removed, or
re-encoded on the training side, it MUST be updated here in the same
PR — or the scorer receives garbage. The scorer's parity check is only
useful as a factual gate if both sides source their field list from a
shared spec; until that spec exists, this dataclass is the contract on
the engine side.

## Missing-value semantics

LightGBM handles missing values natively by sending samples down a
deterministic "default" branch at each split. That default is chosen
at training time from whichever side of the split had more missing
values, so as long as the engine sends `None` (→ JSON `null` → Python
`None` on the scorer → `np.nan` in the row vector) for any feature it
cannot supply, the model sees the exact same distribution it was
trained on. DO NOT send `0.0` as a "missing" default — that's a real
value and creates the NaN-propagation bug documented at
`app/v2_scorer.py:178` in the timesfm-service repo.

## Categorical encodings

`regime` and `delta_source` are encoded as floats at training time:

    regime:
        NORMAL     → 0.0
        CASCADE    → 1.0
        TRENDING   → 2.0
        CALM       → 3.0
        LOW_VOL    → 4.0
        TRANSITION → 5.0

    delta_source:
        binance   → 0.0
        chainlink → 1.0
        tiingo    → 2.0

These maps must match `training/novakash_dataset.py:412-420` exactly.
Anything not in the map becomes `None` (→ NaN), not a garbage value.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import Any, Optional


# ────────────────────────────────────────────────────────────────────
#  Categorical encoding — must mirror training/novakash_dataset.py
# ────────────────────────────────────────────────────────────────────

REGIME_TO_NUM: dict[str, float] = {
    "NORMAL": 0.0,
    "CASCADE": 1.0,
    "TRENDING": 2.0,
    "CALM": 3.0,
    "LOW_VOL": 4.0,
    "TRANSITION": 5.0,
}

DELTA_SOURCE_TO_NUM: dict[str, float] = {
    "binance": 0.0,
    "chainlink": 1.0,
    "tiingo": 2.0,
}


# ────────────────────────────────────────────────────────────────────
#  Coercion helpers
# ────────────────────────────────────────────────────────────────────


def coerce_float(value: Any) -> Optional[float]:
    """
    Coerce any engine-side value to `Optional[float]` for the JSON payload.

    Rules:
      - None                       → None
      - float NaN / inf            → None   (JSON can't represent NaN; scorer gets null → NaN)
      - Python bool / numpy.bool_  → 1.0 / 0.0 (gate booleans)
      - Python int / float         → float(value)
      - numpy int / float scalars  → float(value)   (NumPy int64 is NOT a Python int subclass)
      - str                        → None   (defensive; v5 has no string features)
      - anything else              → None

    Returning `None` for missing (rather than `0.0`) is load-bearing: the
    scorer converts `None` back to `np.nan`, which is the only value
    LightGBM was trained to treat as "missing". `0.0` is a real signal
    and creates a train/serve skew for any feature where 0.0 is plausible
    (funding rates, delta percentages, gate booleans, etc.).

    NumPy handling note: `numpy.int64` is NOT a subclass of Python `int`,
    and `numpy.bool_` is NOT a subclass of Python `bool`. Without the
    `float()` conversion fallback below, any numpy-typed value silently
    became None, which would drop features whenever the engine's
    upstream math happened to produce numpy scalars. The fallback
    `float(value)` path catches those and any other numeric-like object
    that implements `__float__`.
    """
    if value is None:
        return None
    # bool must be checked before int/float because Python's `bool` is
    # a subclass of `int`. NumPy's `numpy.bool_` is not a subclass of
    # either, so we ALSO explicitly check for it via its class name to
    # avoid importing numpy just for isinstance (engine/signals/ is a
    # lean module that shouldn't drag in numpy as a hard dep).
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if type(value).__name__ == "bool_":  # numpy.bool_
        return 1.0 if bool(value) else 0.0
    if isinstance(value, (int, float)):
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    # Fallback for anything that supports __float__ (numpy scalars,
    # Decimal, SupportsFloat protocol, etc.). Defensive try/except
    # because passing a weird object should degrade to None, not crash
    # the scoring path. Strings will reach this branch too — float("foo")
    # raises ValueError, which we catch and return None, so the earlier
    # "str → None" rule still holds as a concrete behaviour.
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def encode_regime(regime: Optional[str]) -> Optional[float]:
    """Map `regime` string to its training-side float encoding, or None if unknown."""
    if regime is None:
        return None
    return REGIME_TO_NUM.get(regime.upper() if isinstance(regime, str) else regime)


def encode_delta_source(source: Optional[str]) -> Optional[float]:
    """Map `delta_source` string to its training-side float encoding, or None if unknown."""
    if source is None:
        return None
    if not isinstance(source, str):
        return None
    return DELTA_SOURCE_TO_NUM.get(source.lower())


def compute_session_bucket(window_ts: Optional[int]) -> Optional[float]:
    """Return the training-side `session_bucket` integer for a window timestamp.

    Mirrors the SQL CASE expression in `training/queries.py:222-228` of
    novakash-timesfm-repo (Sequoia v4 non-linear hour encoding):

        00-03 UTC → 0   (Late Asian)
        04-08 UTC → 1   (Early Asian)
        09-11 UTC → 2   (London)
        12-16 UTC → 3   (US)
        17-23 UTC → 4   (Evening)

    NB: This 5-bucket integer encoding is DIFFERENT from
    `engine/services/cell_bucketing.py::session_label`, which is a
    7-bucket string used for cell-based pause logic. Don't conflate them
    — the v9.x booster was trained on the 5-bucket integer above.

    Returns None for an unparseable / out-of-range timestamp, matching
    the same-shape NaN policy as every other Optional[float] field.
    """
    if window_ts is None:
        return None
    try:
        hour = _dt.datetime.fromtimestamp(int(window_ts), _dt.timezone.utc).hour
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    if hour <= 3:
        return 0.0
    if hour <= 8:
        return 1.0
    if hour <= 11:
        return 2.0
    if hour <= 16:
        return 3.0
    return 4.0


def prob_to_logit(p: Optional[float]) -> Optional[float]:
    """
    Convert a calibrated probability to logit (log-odds).

    Used to produce `v2_logit` from the previous tick's `probability_up`.
    Returns None for None, out-of-range, or boundary values (p ≤ 0 or p ≥ 1)
    where logit is undefined / infinite. v5 is trained on finite logits and
    treats missing as NaN via LightGBM's default-direction mechanism.
    """
    if p is None:
        return None
    try:
        f = float(p)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or not (0.0 < f < 1.0):
        return None
    return math.log(f / (1.0 - f))


# ────────────────────────────────────────────────────────────────────
#  V5 feature body — the full 25-feature contract
# ────────────────────────────────────────────────────────────────────


@dataclass
class V5FeatureBody:
    """
    One-to-one mirror of `FEATURE_COLUMNS_V5` in
    `training/train_lgb_v5.py` (novakash-timesfm-repo).

    Every field is `Optional[float]` so the engine can populate only
    what it has; the scorer receives JSON null for missing values and
    converts them back to NaN on entry to the row vector. Fields the
    engine cannot supply (e.g. `clob_*` when the Polymarket CLOB poller
    is cold) MUST stay None rather than get defaulted to 0.0.

    Adding a field here is a breaking change against the scorer — bump
    both sides in the same PR, and prefer adding to the end of the
    dataclass so existing tests don't have to be re-ordered.
    """

    # ── Continuous consumption context ───────────────────────────────
    eval_offset: Optional[float] = None
    vpin: Optional[float] = None
    delta_pct: Optional[float] = None
    twap_delta: Optional[float] = None

    # ── Polymarket CLOB snapshot ────────────────────────────────────
    clob_spread: Optional[float] = None
    clob_mid: Optional[float] = None
    clob_up_bid: Optional[float] = None
    clob_up_ask: Optional[float] = None
    clob_down_bid: Optional[float] = None
    clob_down_ask: Optional[float] = None

    # ── Multi-source prices ─────────────────────────────────────────
    binance_price: Optional[float] = None
    chainlink_price: Optional[float] = None
    tiingo_close: Optional[float] = None
    delta_binance: Optional[float] = None
    delta_chainlink: Optional[float] = None
    delta_tiingo: Optional[float] = None

    # ── Engine-side gate booleans (True/False/None → 1.0/0.0/NaN) ───
    gate_vpin_passed: Optional[float] = None
    gate_delta_passed: Optional[float] = None
    gate_cg_passed: Optional[float] = None
    gate_twap_passed: Optional[float] = None
    gate_timesfm_passed: Optional[float] = None
    gate_passed: Optional[float] = None

    # ── Categorical-as-numeric ──────────────────────────────────────
    regime_num: Optional[float] = None
    delta_source_num: Optional[float] = None

    # ── v2 as a prior (logit of previous scorer output) ─────────────
    v2_logit: Optional[float] = None

    # ── Polymarket canonical reference price (sister to PR #464) ────
    # When present, this is `eventMetadata.priceToBeat` from Polymarket
    # Gamma — the canonical reference that Polymarket uses to resolve
    # the window. Set when `WindowInfo.open_price_source ==
    # "polymarket_priceToBeat"`. Stays None if Gamma metadata had not
    # published yet at evaluation time and the engine fell back to
    # chainlink_polygon / binance.
    #
    # Coordinated rollout: the engine ALWAYS populates this when the
    # canonical value is available (from PR #464). The downstream
    # consumer (timesfm-service) gates on its own
    # OPEN_PRICE_USE_PRICE_TO_BEAT env flag (default false) — when off,
    # this field is pure telemetry and timesfm continues computing
    # delta_* against chainlink_polygon (current behaviour). When on,
    # timesfm recomputes delta_* against priceToBeat. Flipping the
    # flag is the cutover.
    polymarket_price_to_beat: Optional[float] = None

    # ── Audit #224/#233 Tier 1 enrichments (added 2026-05-07) ───────
    # Train/serve parity for the 12 features the v9.x boosters expect
    # but the engine push contract previously omitted (they defaulted
    # to NaN at scoring time per `app/v2_scorer.py:879-889`). All are
    # Optional[float] = None so any caller that doesn't supply them
    # still produces a valid body — the scorer falls back to its
    # NaN-default behaviour, identical to today.
    #
    # Wired here (engine has the source data on develop):
    #   gamma_up_price, gamma_down_price          — Polymarket Gamma API token prices
    #   gamma_implied_up                          — up / (up + down)
    #   gamma_market_vig                          — 1.0 - (up + down)
    #   clob_imbalance                            — clob_up_ask - clob_down_ask
    #   session_bucket                            — 5-bucket integer from window UTC hour
    #   source_delta_divergence                   — abs(tiingo_vs_binance - chainlink_vs_binance)
    #
    # Pass-through only (engine does not yet maintain a 60s VPIN
    # rolling buffer — these stay None until that buffer lands):
    #   vpin_mean_60s, vpin_std_60s,
    #   vpin_min_60s, vpin_max_60s, vpin_range_60s
    gamma_up_price: Optional[float] = None
    gamma_down_price: Optional[float] = None
    gamma_implied_up: Optional[float] = None
    gamma_market_vig: Optional[float] = None
    clob_imbalance: Optional[float] = None
    session_bucket: Optional[float] = None
    vpin_mean_60s: Optional[float] = None
    vpin_std_60s: Optional[float] = None
    vpin_min_60s: Optional[float] = None
    vpin_max_60s: Optional[float] = None
    vpin_range_60s: Optional[float] = None
    source_delta_divergence: Optional[float] = None

    # ── v9.3 BTC / v9.5 ETH+XRP feature coverage (added 2026-05-23) ──
    # Adds 11 schema slots the boosters were trained on but the engine
    # push contract previously omitted. Mirrors the canonical NaN-default
    # block in `app/v2_scorer.py:890-910` (timesfm-service) and the
    # loader stubs at `app/v5_feature_loader.py:480-527`.
    #
    # **Computed engine-side** (this PR populates these from existing
    # ticks_* tables — see `engine/signals/feature_emitters.py`):
    #   gamma_spread                              — abs(gamma_up - gamma_down)
    #   clob_pre_imbalance                        — pre-event CLOB book imbalance
    #   clob_pre_vig                              — pre-event CLOB market vig
    #   clob_up_pre_stdev                         — stddev of UP token price pre-event
    #   clob_dn_pre_stdev                         — stddev of DOWN token price pre-event
    #   clob_up_pre_n                             — count of CLOB ticks pre-event
    #   nested_5m_oi_delta_cumulative             — Σ oi_delta_pct within current 5m window
    #
    # **Stubs (no engine data source yet — stays None → NaN at scoring)**:
    #   binance_depth_imbalance_inner / _1pct / _5pct  — depth-imbalance ratios
    #       at three book bands. `data.binance.vision` provides this at
    #       training time only; the engine's BinanceWebSocketFeed
    #       currently has on_book=None (no depth subscription) and no
    #       ticks_binance_book table exists. Documented as known gap.
    #   binance_spread_pct                        — (best_ask − best_bid) / mid * 100
    #       Same training-time-only source as depth_imbalance. Stub.
    #
    # NB on naming parity: training-side feature lists use exactly these
    # snake_case names. `clob_dn_pre_stdev` (not `clob_down_pre_stdev`)
    # is the canonical spelling per `app/v2_scorer.py:906` and the v9.5
    # ETH+XRP loader stubs at `app/v5_feature_loader.py:524`.
    gamma_spread: Optional[float] = None
    binance_depth_imbalance_inner: Optional[float] = None
    binance_depth_imbalance_1pct: Optional[float] = None
    binance_depth_imbalance_5pct: Optional[float] = None
    binance_spread_pct: Optional[float] = None
    clob_pre_imbalance: Optional[float] = None
    clob_pre_vig: Optional[float] = None
    clob_up_pre_stdev: Optional[float] = None
    clob_dn_pre_stdev: Optional[float] = None
    clob_up_pre_n: Optional[float] = None
    nested_5m_oi_delta_cumulative: Optional[float] = None

    def to_json_dict(self) -> dict[str, Optional[float]]:
        """
        Serialise to a dict suitable for JSON encoding.

        Keys are exactly `FEATURE_COLUMNS_V5` in the training-side order.
        Values are `float | None`. `None` becomes JSON `null`, which the
        scorer maps back to `np.nan`.

        All 25 keys are ALWAYS present in the output, even if their
        values are None. This gives the scorer a stable schema to
        validate against (a factual parity check on the scorer side can
        assert that `set(body.keys()) == set(FEATURE_COLUMNS_V5)`).
        """
        return {
            "eval_offset": self.eval_offset,
            "vpin": self.vpin,
            "delta_pct": self.delta_pct,
            "twap_delta": self.twap_delta,
            "clob_spread": self.clob_spread,
            "clob_mid": self.clob_mid,
            "clob_up_bid": self.clob_up_bid,
            "clob_up_ask": self.clob_up_ask,
            "clob_down_bid": self.clob_down_bid,
            "clob_down_ask": self.clob_down_ask,
            "binance_price": self.binance_price,
            "chainlink_price": self.chainlink_price,
            "tiingo_close": self.tiingo_close,
            "delta_binance": self.delta_binance,
            "delta_chainlink": self.delta_chainlink,
            "delta_tiingo": self.delta_tiingo,
            "gate_vpin_passed": self.gate_vpin_passed,
            "gate_delta_passed": self.gate_delta_passed,
            "gate_cg_passed": self.gate_cg_passed,
            "gate_twap_passed": self.gate_twap_passed,
            "gate_timesfm_passed": self.gate_timesfm_passed,
            "gate_passed": self.gate_passed,
            "regime_num": self.regime_num,
            "delta_source_num": self.delta_source_num,
            "v2_logit": self.v2_logit,
            "polymarket_price_to_beat": self.polymarket_price_to_beat,
            # Audit #224/#233 Tier 1 (added 2026-05-07).
            "gamma_up_price": self.gamma_up_price,
            "gamma_down_price": self.gamma_down_price,
            "gamma_implied_up": self.gamma_implied_up,
            "gamma_market_vig": self.gamma_market_vig,
            "clob_imbalance": self.clob_imbalance,
            "session_bucket": self.session_bucket,
            "vpin_mean_60s": self.vpin_mean_60s,
            "vpin_std_60s": self.vpin_std_60s,
            "vpin_min_60s": self.vpin_min_60s,
            "vpin_max_60s": self.vpin_max_60s,
            "vpin_range_60s": self.vpin_range_60s,
            "source_delta_divergence": self.source_delta_divergence,
            # v9.3 BTC / v9.5 ETH+XRP coverage (added 2026-05-23).
            "gamma_spread": self.gamma_spread,
            "binance_depth_imbalance_inner": self.binance_depth_imbalance_inner,
            "binance_depth_imbalance_1pct": self.binance_depth_imbalance_1pct,
            "binance_depth_imbalance_5pct": self.binance_depth_imbalance_5pct,
            "binance_spread_pct": self.binance_spread_pct,
            "clob_pre_imbalance": self.clob_pre_imbalance,
            "clob_pre_vig": self.clob_pre_vig,
            "clob_up_pre_stdev": self.clob_up_pre_stdev,
            "clob_dn_pre_stdev": self.clob_dn_pre_stdev,
            "clob_up_pre_n": self.clob_up_pre_n,
            "nested_5m_oi_delta_cumulative": self.nested_5m_oi_delta_cumulative,
        }

    def coverage(self) -> float:
        """
        Fraction of fields that are not None.

        Useful for logging and for a defense-in-depth sanity check at
        call sites: if coverage drops below some threshold on a
        decision-path call, something has broken in feature collection
        and we should surface it rather than silently pushing an
        almost-empty body.
        """
        values = self.to_json_dict().values()
        total = len(values)
        populated = sum(1 for v in values if v is not None)
        return populated / total if total else 0.0


# ────────────────────────────────────────────────────────────────────
#  Single-source-of-truth feature body builder
# ────────────────────────────────────────────────────────────────────


def build_v5_feature_body(
    *,
    eval_offset: Optional[float] = None,
    vpin: Optional[float] = None,
    delta_pct: Optional[float] = None,
    twap_delta: Optional[float] = None,
    clob_up_price: Optional[float] = None,     # Polymarket token price (UP)
    clob_down_price: Optional[float] = None,   # Polymarket token price (DOWN)
    clob_up_bid: Optional[float] = None,
    clob_up_ask: Optional[float] = None,
    clob_down_bid: Optional[float] = None,
    clob_down_ask: Optional[float] = None,
    binance_price: Optional[float] = None,
    chainlink_price: Optional[float] = None,
    tiingo_close: Optional[float] = None,
    delta_binance: Optional[float] = None,
    delta_chainlink: Optional[float] = None,
    delta_tiingo: Optional[float] = None,
    gate_vpin_passed: Optional[bool] = None,
    gate_delta_passed: Optional[bool] = None,
    gate_cg_passed: Optional[bool] = None,
    gate_twap_passed: Optional[bool] = None,
    gate_timesfm_passed: Optional[bool] = None,
    gate_passed: Optional[bool] = None,
    regime: Optional[str] = None,             # raw string, encoded internally
    delta_source: Optional[str] = None,       # raw string, encoded internally
    prev_v2_probability_up: Optional[float] = None,  # for v2_logit
    polymarket_price_to_beat: Optional[float] = None,  # Polymarket canonical reference (PR #464 sister)
    # ── Audit #224/#233 Tier 1 inputs (added 2026-05-07) ────────────
    # gamma_*_price are the Polymarket Gamma API token implied prices
    # (engine attrs `_gamma_up_price` / `_gamma_down_price`). When BOTH
    # are supplied, gamma_implied_up + gamma_market_vig are derived in
    # the same shapes the training pipeline computes them.
    gamma_up_price: Optional[float] = None,
    gamma_down_price: Optional[float] = None,
    # window_ts (epoch seconds, UTC) is used to derive session_bucket.
    # Pass `window.window_ts` from the call site. None → session_bucket
    # stays None (NaN at scoring), which is the safe fallback.
    window_ts: Optional[int] = None,
    # vpin rolling-window stats. Engine does not yet maintain a 60s
    # vpin buffer; these stay None (→ NaN at scoring) until that lands.
    # Wiring kwargs ahead of time so a future buffer doesn't need to
    # bump the V5FeatureBody schema again.
    vpin_mean_60s: Optional[float] = None,
    vpin_std_60s: Optional[float] = None,
    vpin_min_60s: Optional[float] = None,
    vpin_max_60s: Optional[float] = None,
    # ── v9.3 BTC / v9.5 ETH+XRP inputs (added 2026-05-23) ───────────
    # CLOB pre-event aggregates (5 features) — derived in the engine
    # via `feature_emitters.compute_clob_pre_aggregates` over the
    # ticks_clob / clob_book_snapshots window pre-window-open. Pass the
    # dict-spread output of that helper, OR None if it failed / DB
    # unavailable / pre-window not yet observed.
    clob_pre_imbalance: Optional[float] = None,
    clob_pre_vig: Optional[float] = None,
    clob_up_pre_stdev: Optional[float] = None,
    clob_dn_pre_stdev: Optional[float] = None,
    clob_up_pre_n: Optional[float] = None,
    # nested_5m_oi_delta_cumulative — Σ oi_delta_pct from ticks_coinglass
    # within the current 5m window (computed by
    # `feature_emitters.compute_oi_delta_cumulative`). None when
    # CoinGlass is offline for the asset or no ticks yet in the window.
    nested_5m_oi_delta_cumulative: Optional[float] = None,
    # Binance depth / spread features — stubs only on develop. The
    # BinanceWebSocketFeed currently has on_book=None (no depth20
    # subscription) and no ticks_binance_book table exists, so these
    # stay None → NaN at scoring. Wiring kwargs ahead of time so a
    # future depth pipeline doesn't need to bump the V5FeatureBody
    # schema again. Documented in PR body.
    binance_depth_imbalance_inner: Optional[float] = None,
    binance_depth_imbalance_1pct: Optional[float] = None,
    binance_depth_imbalance_5pct: Optional[float] = None,
    binance_spread_pct: Optional[float] = None,
) -> V5FeatureBody:
    """
    Single source of truth for building a V5FeatureBody from engine state.

    Every decision-path call site (`five_min_vpin.py:1478`,
    `gates.py:374`, and any future caller) MUST build its feature body
    through this function — never by constructing `V5FeatureBody(...)`
    directly — so that train/serve parity is enforced in one place.

    Only pass the arguments you have. Fields you don't pass default to
    None, which the scorer interprets as `np.nan` via the missing-value
    path in LightGBM. DO NOT pass `0.0` as a "missing" default — that's
    a real signal and creates a silent train/serve skew for any feature
    where 0.0 is a plausible value (funding rates, delta percentages,
    gate booleans, etc.).

    `clob_mid` and `clob_spread` are computed here from
    `clob_up_price + clob_down_price` so every call site expresses them
    the same way. If future CLOB features ever expose actual
    bid/ask/mid/spread separately, add them as new args rather than
    re-deriving.

    `gate_*` booleans are coerced to 1.0 / 0.0 / None via `coerce_float`
    which handles bool subtyping correctly (Python `bool` is a subclass
    of `int`, so raw casting loses the semantic distinction between
    True/1 and False/0 at the JSON boundary).

    Regime and delta_source strings are encoded via the training-side
    maps; unknown strings become None (→ NaN), never a garbage numeric.

    The `prev_v2_probability_up` kwarg is the PRIOR scorer output for
    this asset/window that will be converted to its logit and sent as
    `v2_logit`. First tick in a window has no prior: pass None and the
    field stays None. Out-of-range priors (p ≤ 0 or p ≥ 1) also become
    None because logit is undefined there.

    The `polymarket_price_to_beat` kwarg is the Polymarket-published
    canonical reference price for the window's resolution
    (`eventMetadata.priceToBeat` from Gamma). Pair with PR #464 which
    captures it on `WindowInfo.open_price` when
    `open_price_source == "polymarket_priceToBeat"`. Stays None when
    Gamma metadata had not published yet at evaluation time and the
    engine fell back to chainlink_polygon. The scorer-side
    `OPEN_PRICE_USE_PRICE_TO_BEAT` flag (default false) decides whether
    the consumer recomputes its delta_* family against this value or
    keeps using `chainlink_price`.
    """
    _clob_mid: Optional[float] = None
    _clob_spread: Optional[float] = None
    if clob_up_price is not None and clob_down_price is not None:
        try:
            up_f = float(clob_up_price)
            dn_f = float(clob_down_price)
            _clob_mid = (up_f + dn_f) / 2.0
            _clob_spread = abs(up_f - dn_f)
        except (TypeError, ValueError):
            _clob_mid = None
            _clob_spread = None

    # ── Audit #224/#233 derivations ─────────────────────────────────
    # All formulas mirror `training/build_dataset.py` and
    # `training/queries.py` exactly so a v9.x booster trained on these
    # features sees the same shape live as it did during training.

    # Gamma implied probability + market vig — `build_dataset.py:323-326`.
    # gamma_implied_up = gamma_up / (gamma_up + gamma_down)
    # gamma_market_vig = 1.0 - (gamma_up + gamma_down)
    # (vig is non-negative for "tight" books, near 0 when market sums to 1.)
    _gamma_implied_up: Optional[float] = None
    _gamma_market_vig: Optional[float] = None
    _gamma_up_f = coerce_float(gamma_up_price)
    _gamma_down_f = coerce_float(gamma_down_price)
    if _gamma_up_f is not None and _gamma_down_f is not None:
        _gamma_total = _gamma_up_f + _gamma_down_f
        if _gamma_total > 0.0:
            _gamma_implied_up = _gamma_up_f / _gamma_total
        _gamma_market_vig = 1.0 - _gamma_total

    # CLOB imbalance — `build_dataset.py:291`.
    # clob_imbalance = clob_up_ask - clob_down_ask
    _clob_imbalance: Optional[float] = None
    _up_ask_f = coerce_float(clob_up_ask)
    _dn_ask_f = coerce_float(clob_down_ask)
    if _up_ask_f is not None and _dn_ask_f is not None:
        _clob_imbalance = _up_ask_f - _dn_ask_f

    # session_bucket — `queries.py:222-228` (5-bucket integer encoding).
    _session_bucket = compute_session_bucket(window_ts)

    # Source delta divergence — `build_dataset.py:268,272,329-332`.
    # tiingo_vs_binance     = (tiingo - binance) / binance
    # chainlink_vs_binance  = (chainlink - binance) / binance
    # source_delta_divergence = abs(tiingo_vs_binance - chainlink_vs_binance)
    # Requires all three prices; if any is missing or binance is 0,
    # divergence stays None → NaN at scoring time.
    _source_delta_divergence: Optional[float] = None
    _bin_f = coerce_float(binance_price)
    _tii_f = coerce_float(tiingo_close)
    _chain_f = coerce_float(chainlink_price)
    if _bin_f is not None and _bin_f != 0.0 and _tii_f is not None and _chain_f is not None:
        _t_vs_b = (_tii_f - _bin_f) / _bin_f
        _c_vs_b = (_chain_f - _bin_f) / _bin_f
        _source_delta_divergence = abs(_t_vs_b - _c_vs_b)

    # vpin_range_60s — `build_dataset.py:307`.
    # vpin_range_60s = vpin_max_60s - vpin_min_60s
    # Engine has no 60s vpin buffer on develop; both inputs typically
    # arrive None and this stays None.
    _vpin_range_60s: Optional[float] = None
    _vmax_f = coerce_float(vpin_max_60s)
    _vmin_f = coerce_float(vpin_min_60s)
    if _vmax_f is not None and _vmin_f is not None:
        _vpin_range_60s = _vmax_f - _vmin_f

    # gamma_spread — `build_dataset.py:325` (v9.5).
    # gamma_spread = abs(gamma_up_price - gamma_down_price)
    # Captures whether the Polymarket book is symmetric (~|0.5-0.5|=0)
    # or skewed (e.g. |0.7-0.3|=0.4). Different signal from gamma_market_vig
    # which captures sum-deviation from 1.0. Both inputs already coerced
    # above as `_gamma_up_f` / `_gamma_down_f`.
    _gamma_spread: Optional[float] = None
    if _gamma_up_f is not None and _gamma_down_f is not None:
        _gamma_spread = abs(_gamma_up_f - _gamma_down_f)

    return V5FeatureBody(
        eval_offset=coerce_float(eval_offset),
        vpin=coerce_float(vpin),
        delta_pct=coerce_float(delta_pct),
        twap_delta=coerce_float(twap_delta),
        clob_spread=coerce_float(_clob_spread),
        clob_mid=coerce_float(_clob_mid),
        clob_up_bid=coerce_float(clob_up_bid),
        clob_up_ask=coerce_float(clob_up_ask),
        clob_down_bid=coerce_float(clob_down_bid),
        clob_down_ask=coerce_float(clob_down_ask),
        binance_price=coerce_float(binance_price),
        chainlink_price=coerce_float(chainlink_price),
        tiingo_close=coerce_float(tiingo_close),
        delta_binance=coerce_float(delta_binance),
        delta_chainlink=coerce_float(delta_chainlink),
        delta_tiingo=coerce_float(delta_tiingo),
        gate_vpin_passed=coerce_float(gate_vpin_passed),
        gate_delta_passed=coerce_float(gate_delta_passed),
        gate_cg_passed=coerce_float(gate_cg_passed),
        gate_twap_passed=coerce_float(gate_twap_passed),
        gate_timesfm_passed=coerce_float(gate_timesfm_passed),
        gate_passed=coerce_float(gate_passed),
        regime_num=encode_regime(regime),
        delta_source_num=encode_delta_source(delta_source),
        v2_logit=prob_to_logit(prev_v2_probability_up),
        polymarket_price_to_beat=coerce_float(polymarket_price_to_beat),
        # Audit #224/#233 Tier 1 fields (added 2026-05-07).
        gamma_up_price=_gamma_up_f,
        gamma_down_price=_gamma_down_f,
        gamma_implied_up=coerce_float(_gamma_implied_up),
        gamma_market_vig=coerce_float(_gamma_market_vig),
        clob_imbalance=coerce_float(_clob_imbalance),
        session_bucket=_session_bucket,
        vpin_mean_60s=coerce_float(vpin_mean_60s),
        vpin_std_60s=coerce_float(vpin_std_60s),
        vpin_min_60s=coerce_float(vpin_min_60s),
        vpin_max_60s=coerce_float(vpin_max_60s),
        vpin_range_60s=coerce_float(_vpin_range_60s),
        source_delta_divergence=coerce_float(_source_delta_divergence),
        # v9.3 BTC / v9.5 ETH+XRP feature coverage (added 2026-05-23).
        gamma_spread=coerce_float(_gamma_spread),
        binance_depth_imbalance_inner=coerce_float(binance_depth_imbalance_inner),
        binance_depth_imbalance_1pct=coerce_float(binance_depth_imbalance_1pct),
        binance_depth_imbalance_5pct=coerce_float(binance_depth_imbalance_5pct),
        binance_spread_pct=coerce_float(binance_spread_pct),
        clob_pre_imbalance=coerce_float(clob_pre_imbalance),
        clob_pre_vig=coerce_float(clob_pre_vig),
        clob_up_pre_stdev=coerce_float(clob_up_pre_stdev),
        clob_dn_pre_stdev=coerce_float(clob_dn_pre_stdev),
        clob_up_pre_n=coerce_float(clob_up_pre_n),
        nested_5m_oi_delta_cumulative=coerce_float(nested_5m_oi_delta_cumulative),
    )


# ────────────────────────────────────────────────────────────────────
#  Confidence extraction — the v11 bug fix
# ────────────────────────────────────────────────────────────────────


def confidence_from_result(result: dict) -> float:
    """
    Extract confidence in the LightGBM P(UP) signal from a v2 scorer response.

    Why this function exists:
    ─────────────────────────
    Pre-v11 code read `result["timesfm"]["confidence"]` as if it were
    "confidence in the v2 direction call". It is NOT. That field is the
    v1 TimesFM forecaster's confidence in ITS OWN quantile forecast —
    a totally independent model scoring a different thing (predicted
    close price quantiles vs a 5m window direction). Using it as P(UP)
    confidence caused the dynamic-threshold code in v11 to widen gates
    based on a metric with no relationship to the actual model output.
    When v5 shipped and pinned P(UP) at a constant 0.606, the v11 gate
    also started trusting the (still-unrelated) TimesFM confidence and
    waved trades through.

    Correct precedence:
      1. If the scorer provides a TOP-LEVEL `confidence` field, use it.
         The scorer is responsible for computing a confidence that
         actually corresponds to the returned `probability_up`. When
         the timesfm-service fix ships this field, the engine will
         start reading it automatically.
      2. Otherwise, derive from `probability_up` locally as the standard
         "distance from indifference" score: `max(p, 1-p)`. This is
         bounded in [0.5, 1.0], it's strictly monotonic in |p - 0.5|,
         and it's what a calibrated probability ACTUALLY represents.
      3. If `probability_up` is also missing / invalid, return 0.5
         (indifferent). This is the most conservative fallback: it
         maxes out the gate thresholds, which is the right failure
         mode — no trade rather than a trade on bad data.

    NEVER read `result["timesfm"]["confidence"]` here. That field stays
    in the response for observability (it's still a valid v1 diagnostic)
    but it is not what we gate on.
    """
    if not isinstance(result, dict):
        return 0.5

    # 1. Prefer scorer-provided confidence if present and in range.
    top_conf = result.get("confidence")
    if isinstance(top_conf, (int, float)) and not isinstance(top_conf, bool):
        f = float(top_conf)
        if not math.isnan(f) and 0.0 <= f <= 1.0:
            return f

    # 2. Derive from probability_up.
    p_raw = result.get("probability_up")
    if isinstance(p_raw, (int, float)) and not isinstance(p_raw, bool):
        p = float(p_raw)
        if not math.isnan(p) and 0.0 <= p <= 1.0:
            return max(p, 1.0 - p)

    # 3. Conservative fallback.
    return 0.5
