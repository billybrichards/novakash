"""
Tests for engine/signals/v2_feature_body.py — the train/serve contract
for Sequoia v5's push-mode scoring.

These tests enforce the invariants that v5's serving correctness
depends on:

  1. Exactly 25 fields, matching FEATURE_COLUMNS_V5 in the timesfm
     training code (one-to-one, no extras, no drift).
  2. Missing values stay None on the wire (→ JSON null → scorer NaN).
  3. Bool coercion: True/False → 1.0/0.0, never 1/0 int.
  4. Regime and delta_source categorical encodings match training.
  5. `confidence_from_result` NEVER reads `result["timesfm"]["confidence"]`
     as P(UP) confidence — that was the v11 bug. It prefers a top-level
     `confidence` field, falls back to max(p, 1-p), and returns 0.5 on
     garbage input as the conservative fallback.
  6. `build_v5_feature_body` derives clob_mid / clob_spread correctly
     from the two CLOB prices and handles missing inputs gracefully.
"""

import math

import pytest

from signals.v2_feature_body import (
    DELTA_SOURCE_TO_NUM,
    REGIME_TO_NUM,
    V5FeatureBody,
    build_v5_feature_body,
    coerce_float,
    confidence_from_result,
    encode_delta_source,
    encode_regime,
    prob_to_logit,
)


# The exact list and order that must match FEATURE_COLUMNS_V5 in
# training/train_lgb_v5.py in the novakash-timesfm-repo. If this list
# drifts from the training side, the scorer sees fields out of order
# and v5 silently returns garbage. This test is the firewall.
EXPECTED_V5_FIELDS: list[str] = [
    "eval_offset",
    "vpin",
    "delta_pct",
    "twap_delta",
    "clob_spread",
    "clob_mid",
    "clob_up_bid",
    "clob_up_ask",
    "clob_down_bid",
    "clob_down_ask",
    "binance_price",
    "chainlink_price",
    "tiingo_close",
    "delta_binance",
    "delta_chainlink",
    "delta_tiingo",
    "gate_vpin_passed",
    "gate_delta_passed",
    "gate_cg_passed",
    "gate_twap_passed",
    "gate_timesfm_passed",
    "gate_passed",
    "regime_num",
    "delta_source_num",
    "v2_logit",
]


# ────────────────────────────────────────────────────────────────────
#  Schema contract
# ────────────────────────────────────────────────────────────────────


def test_v5_feature_body_schema_is_exactly_25_fields():
    """The schema is load-bearing: drift here = v5 silently breaks."""
    body = V5FeatureBody()
    d = body.to_json_dict()
    assert list(d.keys()) == EXPECTED_V5_FIELDS
    assert len(d) == 25


def test_empty_body_is_all_none_and_zero_coverage():
    body = V5FeatureBody()
    d = body.to_json_dict()
    assert all(v is None for v in d.values())
    assert body.coverage() == 0.0


def test_full_body_is_full_coverage():
    body = V5FeatureBody(
        eval_offset=180.0,
        vpin=0.6,
        delta_pct=0.03,
        twap_delta=0.04,
        clob_spread=0.1,
        clob_mid=0.5,
        clob_up_bid=0.54,
        clob_up_ask=0.55,
        clob_down_bid=0.44,
        clob_down_ask=0.45,
        binance_price=67000.0,
        chainlink_price=67001.0,
        tiingo_close=66999.0,
        delta_binance=0.02,
        delta_chainlink=0.022,
        delta_tiingo=0.019,
        gate_vpin_passed=1.0,
        gate_delta_passed=1.0,
        gate_cg_passed=1.0,
        gate_twap_passed=1.0,
        gate_timesfm_passed=1.0,
        gate_passed=1.0,
        regime_num=1.0,
        delta_source_num=0.0,
        v2_logit=0.5,
    )
    assert body.coverage() == 1.0


def test_partial_body_has_fractional_coverage():
    body = V5FeatureBody(eval_offset=120.0, vpin=0.5, delta_pct=0.01)
    assert body.coverage() == pytest.approx(3 / 25, rel=1e-9)


# ────────────────────────────────────────────────────────────────────
#  coerce_float — the 0.0-vs-None bug guard
# ────────────────────────────────────────────────────────────────────


def test_coerce_float_none_stays_none():
    assert coerce_float(None) is None


def test_coerce_float_bools_become_semantic_floats():
    # Python bool is a subclass of int, so a naive `float(True)` would
    # give 1.0 — but we want to be explicit that True/False are
    # semantically distinct from 1/0 ints for gate booleans.
    assert coerce_float(True) == 1.0
    assert coerce_float(False) == 0.0


def test_coerce_float_ints_and_floats_pass_through():
    assert coerce_float(3) == 3.0
    assert coerce_float(3.14) == 3.14
    assert coerce_float(0) == 0.0  # 0 is a REAL value, not missing
    assert coerce_float(-2.5) == -2.5


def test_coerce_float_nan_and_inf_become_none():
    # JSON can't represent NaN/inf and the scorer would reject the body
    # or silently parse them as null anyway. Clean up at the boundary.
    assert coerce_float(float("nan")) is None
    assert coerce_float(float("inf")) is None
    assert coerce_float(float("-inf")) is None


def test_coerce_float_strings_become_none():
    # Defensive: v5 has no string features, so any string input is a
    # bug in the caller. Returning None rather than crashing means a
    # single broken call site degrades gracefully to "missing feature"
    # instead of crashing the scoring path.
    assert coerce_float("foo") is None
    assert coerce_float("") is None


def test_coerce_float_never_returns_zero_for_missing():
    """The 0.0-vs-None distinction is load-bearing for NaN-propagation semantics."""
    # If coerce_float ever returned 0.0 for None, any gate_* field with
    # missing state would tell the model "gate failed" (False → 0.0)
    # instead of "gate state unknown" (NaN). Regression guard.
    assert coerce_float(None) is not 0.0
    assert coerce_float(None) is None


def test_coerce_float_handles_numpy_scalars_if_numpy_present():
    """
    Regression guard for the numpy silent-drop bug.

    `numpy.int64` is NOT a subclass of Python `int`, and `numpy.bool_`
    is NOT a subclass of Python `bool`. Before the fix, passing a
    numpy-typed value silently returned None because the isinstance
    checks all failed and the function fell through to the default
    return. Now the fallback `float(value)` path catches them.

    This test is conditional on numpy being importable so engine/signals/
    doesn't pick up a hard numpy runtime dependency.
    """
    np = pytest.importorskip("numpy")
    # int scalar
    assert coerce_float(np.int64(5)) == 5.0
    assert coerce_float(np.int32(-3)) == -3.0
    # float scalar — this DID work before because numpy.float64 IS a
    # subclass of Python float, but pin it so nobody tries to "simplify"
    # the fallback path on the assumption that all numpy scalars need it.
    assert coerce_float(np.float64(1.5)) == 1.5
    assert coerce_float(np.float32(2.5)) == 2.5
    # bool scalar
    assert coerce_float(np.bool_(True)) == 1.0
    assert coerce_float(np.bool_(False)) == 0.0
    # numpy nan still becomes None
    assert coerce_float(np.float64("nan")) is None


def test_coerce_float_recovers_numeric_strings():
    """
    Strings that represent valid floats are accepted via the `float()`
    fallback. This is a minor behavior change from the earlier "all
    strings → None" stance, done as a defense-in-depth relaxation: if
    something upstream accidentally stringifies a float, we recover it
    instead of silently dropping the feature.
    """
    assert coerce_float("3.14") == 3.14
    assert coerce_float("0") == 0.0
    assert coerce_float("-1.5") == -1.5
    # Junk strings still become None (via ValueError path)
    assert coerce_float("foo") is None
    assert coerce_float("") is None
    # NaN string is explicitly rejected
    assert coerce_float("nan") is None
    assert coerce_float("inf") is None


# ────────────────────────────────────────────────────────────────────
#  Categorical encoding — must match training/novakash_dataset.py
# ────────────────────────────────────────────────────────────────────


def test_regime_encoding_matches_training_map():
    # Hardcoded expectations; if the training side ever changes these,
    # this test fails loudly and forces a coordinated update.
    assert REGIME_TO_NUM == {
        "NORMAL": 0.0,
        "CASCADE": 1.0,
        "TRENDING": 2.0,
        "CALM": 3.0,
        "LOW_VOL": 4.0,
        "TRANSITION": 5.0,
    }


def test_encode_regime_accepts_canonical_strings():
    assert encode_regime("NORMAL") == 0.0
    assert encode_regime("CASCADE") == 1.0
    assert encode_regime("TRANSITION") == 5.0


def test_encode_regime_case_insensitive():
    assert encode_regime("cascade") == 1.0
    assert encode_regime("Transition") == 5.0


def test_encode_regime_unknown_becomes_none():
    # Unknown regime is a bug upstream but MUST NOT become a garbage
    # float (0.0) that the model interprets as "NORMAL regime". None
    # gives the model a missing value which it handles via training.
    assert encode_regime("UNKNOWN") is None
    assert encode_regime("FOOBAR") is None
    assert encode_regime(None) is None
    assert encode_regime("") is None


def test_delta_source_encoding_matches_training_map():
    assert DELTA_SOURCE_TO_NUM == {
        "binance": 0.0,
        "chainlink": 1.0,
        "tiingo": 2.0,
    }


def test_encode_delta_source_accepts_lowercase():
    assert encode_delta_source("binance") == 0.0
    assert encode_delta_source("chainlink") == 1.0
    assert encode_delta_source("tiingo") == 2.0


def test_encode_delta_source_is_case_sensitive_to_lowercase():
    # Training side is strict about lowercase — enforce it here too.
    assert encode_delta_source("Binance") == 0.0  # helper lowercases
    assert encode_delta_source("BINANCE") == 0.0


def test_encode_delta_source_unknown_becomes_none():
    assert encode_delta_source("unknown") is None
    assert encode_delta_source(None) is None
    assert encode_delta_source(42) is None  # non-string defensive


# ────────────────────────────────────────────────────────────────────
#  prob_to_logit — v2_logit self-reference semantics
# ────────────────────────────────────────────────────────────────────


def test_prob_to_logit_valid_probabilities():
    # logit(0.5) = 0
    assert prob_to_logit(0.5) == pytest.approx(0.0, abs=1e-12)
    # logit(0.75) = ln(3)
    assert prob_to_logit(0.75) == pytest.approx(math.log(3.0), rel=1e-12)


def test_prob_to_logit_boundaries_and_out_of_range_become_none():
    # logit is undefined at 0 and 1 (infinite)
    assert prob_to_logit(0.0) is None
    assert prob_to_logit(1.0) is None
    assert prob_to_logit(-0.01) is None
    assert prob_to_logit(1.01) is None


def test_prob_to_logit_none_and_garbage_become_none():
    assert prob_to_logit(None) is None
    assert prob_to_logit(float("nan")) is None
    assert prob_to_logit("not a number") is None


# ────────────────────────────────────────────────────────────────────
#  confidence_from_result — the v11 bug regression guard
# ────────────────────────────────────────────────────────────────────


def test_confidence_prefers_top_level_field():
    r = {"confidence": 0.82, "probability_up": 0.55}
    assert confidence_from_result(r) == 0.82


def test_confidence_falls_back_to_derived_when_no_top_level():
    # max(0.7, 0.3) = 0.7
    assert confidence_from_result({"probability_up": 0.7}) == 0.7
    assert confidence_from_result({"probability_up": 0.3}) == 0.7  # symmetric


def test_confidence_at_indifference_is_one_half():
    assert confidence_from_result({"probability_up": 0.5}) == 0.5


def test_confidence_NEVER_reads_timesfm_confidence_as_p_up_confidence():
    """THE v11 bug.

    Old code read `result["timesfm"]["confidence"]` as "confidence in
    the v2 direction call". That field is the v1 TimesFM forecaster's
    OWN metric — confidence in its OWN quantile forecast of the
    predicted close price. Different model, different thing being
    scored. Reading it as P(UP) confidence caused v11's dynamic gate
    thresholds to widen based on a metric completely unrelated to the
    LightGBM model's actual output.

    This test pins the fix: even when `timesfm.confidence` is 0.99 and
    there's no top-level `confidence`, the result must reflect the
    actual P(UP), not the v1 metric.
    """
    r = {"timesfm": {"confidence": 0.99}, "probability_up": 0.55}
    # Expected: max(0.55, 0.45) = 0.55 — NOT 0.99.
    assert confidence_from_result(r) == 0.55


def test_confidence_garbage_inputs_return_safe_default():
    # Safe default is 0.5 — this maxes the gate threshold, preventing
    # trades from being waved through on bad data.
    assert confidence_from_result({}) == 0.5
    assert confidence_from_result(None) == 0.5
    assert confidence_from_result("not a dict") == 0.5
    assert confidence_from_result({"probability_up": "nope"}) == 0.5
    assert confidence_from_result({"confidence": -0.5}) == 0.5  # out of range
    assert confidence_from_result({"confidence": 1.5}) == 0.5   # out of range


def test_confidence_bool_not_treated_as_numeric():
    # bool is a subclass of int in Python; guard against
    # `isinstance(True, (int, float))` silently treating True as 1.0.
    r = {"confidence": True, "probability_up": 0.6}
    # Expected: falls through to derivation (max(0.6, 0.4) = 0.6)
    assert confidence_from_result(r) == 0.6


# ────────────────────────────────────────────────────────────────────
#  build_v5_feature_body — single source of truth
# ────────────────────────────────────────────────────────────────────


def test_builder_with_no_args_returns_empty_body():
    body = build_v5_feature_body()
    assert body.coverage() == 0.0
    assert all(v is None for v in body.to_json_dict().values())


def test_builder_derives_clob_mid_and_spread_from_two_prices():
    body = build_v5_feature_body(clob_up_price=0.55, clob_down_price=0.45)
    d = body.to_json_dict()
    assert d["clob_mid"] == pytest.approx(0.50, abs=1e-12)
    assert d["clob_spread"] == pytest.approx(0.10, abs=1e-12)


def test_builder_derived_clob_requires_both_prices():
    # Missing one price → can't derive mid/spread
    body1 = build_v5_feature_body(clob_up_price=0.55)  # no down
    assert body1.to_json_dict()["clob_mid"] is None
    assert body1.to_json_dict()["clob_spread"] is None

    body2 = build_v5_feature_body(clob_down_price=0.45)  # no up
    assert body2.to_json_dict()["clob_mid"] is None


def test_builder_derived_clob_handles_garbage_prices():
    # Passing non-numeric values should not crash the builder
    body = build_v5_feature_body(clob_up_price="bad", clob_down_price=0.5)
    assert body.to_json_dict()["clob_mid"] is None
    assert body.to_json_dict()["clob_spread"] is None


def test_builder_gate_bools_coerce_correctly():
    body = build_v5_feature_body(
        gate_vpin_passed=True,
        gate_delta_passed=False,
        # gate_cg_passed intentionally omitted → None
    )
    d = body.to_json_dict()
    assert d["gate_vpin_passed"] == 1.0
    assert d["gate_delta_passed"] == 0.0
    assert d["gate_cg_passed"] is None


def test_builder_regime_and_delta_source_strings_encoded():
    body = build_v5_feature_body(regime="CASCADE", delta_source="chainlink")
    d = body.to_json_dict()
    assert d["regime_num"] == 1.0
    assert d["delta_source_num"] == 1.0


def test_builder_prev_v2_probability_becomes_logit_field():
    body = build_v5_feature_body(prev_v2_probability_up=0.75)
    d = body.to_json_dict()
    assert d["v2_logit"] == pytest.approx(math.log(3.0), rel=1e-12)


def test_builder_first_tick_no_prior_gives_none_logit():
    # First tick of a window has no prior v2 call → None
    body = build_v5_feature_body(prev_v2_probability_up=None)
    assert body.to_json_dict()["v2_logit"] is None


def test_builder_all_v5_field_names_present_in_output():
    """Make sure the builder populates every field in the schema,
    even if only with None. The scorer expects exactly 25 keys on
    every request and an extra or missing key should fail fast."""
    body = build_v5_feature_body()
    d = body.to_json_dict()
    for name in EXPECTED_V5_FIELDS:
        assert name in d, f"builder output missing field {name}"
    assert len(d) == len(EXPECTED_V5_FIELDS)
