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
