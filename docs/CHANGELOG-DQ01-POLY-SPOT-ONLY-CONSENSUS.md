# DQ-01 — Polymarket engine spot-only consensus vote

**Status:** SHIPPED default-OFF — operator flips `V11_POLY_SPOT_ONLY_CONSENSUS=true` to activate.

**Target:** `engine/signals/gates.py::SourceAgreementGate`

**Safety contract:** Zero behaviour change on merge. Activation requires operator to set env var on the Montreal host and restart the engine.

## Motivation — the contamination caught in v11.1 aggregate data

`docs/CHANGELOG-v11.1-SOURCE-AGREEMENT-2-3-MAJORITY.md` documented a
deliberate decision: move SourceAgreementGate from unanimous
Chainlink + Tiingo agreement (pass rate ~57%) to a 2/3 majority vote
across CL + TI + Binance (pass rate ~98%). The engineering justification
was "Binance's systematic DOWN bias will be neutralised in aggregate
because CL + TI unanimity already captures most valid signals — Binance
only tips the vote on windows where CL and TI disagree."

The evidence table in that changelog shows the bias explicitly:

| Source | UP Signals | DOWN Signals | Bias |
| ------ | ---------- | ------------ | ---- |
| Chainlink | 52.7% | 47.3% | Balanced |
| Tiingo | 42.5% | 57.5% | Slight DOWN |
| Binance | 16.9% | 83.1% | **Strong DOWN** |

And the most common disagreement pattern was CL=UP, TI=DOWN, BIN=DOWN
(19.6% of all evaluations). Under the 2/3 rule, this pattern passes
as DOWN — **the biased source sides with the lean-DOWN spot source
against the balanced spot source and wins the majority.**

That is exactly the failure mode the user flagged on 2026-04-11:

> we are making some really terrible trade decisions ... we noted a
> down after 2 consecutive previous up markets and other indicators
> in my view felt obvious it was either going up or down when we
> voted up or down respectively

The 19.6% contamination rate is high enough to move aggregate PnL and
low enough to be invisible on any single trade (you'd attribute the
loss to the market, not the vote topology). The DQ-01 flag gives us
a way to cleanly A/B whether dropping Binance from the vote moves
live PnL.

## What the flag changes

`V11_POLY_SPOT_ONLY_CONSENSUS` is read once at `SourceAgreementGate`
construction time (same ergonomic as `V10_6_ENABLED` — operator
restart required to pick up flag changes).

**Flag OFF (default, behaviour = pre-DQ-01 v11.1 path):**

- 2/3 majority vote across CL + TI + Binance
- `result.data` carries `cl_dir`, `ti_dir`, `bin_dir`, `direction`,
  `up_votes`, `down_votes`
- Reason string format: `"2/3 UP (CL=UP TI=UP BIN=DOWN)"`

**Flag ON:**

- Spot-only consensus: Chainlink and Tiingo only
- Binance is completely ignored by the vote (still consumed by
  VPIN, taker-flow, liquidations, and every other downstream gate)
- CL and TI must agree on direction. If they disagree, the gate
  fails with a `spot disagree` reason and the window is skipped.
- `result.data` carries `mode: "spot_only"`, `cl_dir`, `ti_dir`,
  `direction`; crucially `bin_dir` is NOT present, which is the
  runtime proof that Binance was never read
- Reason string format: `"spot-only UP (CL=UP TI=UP)"` on pass,
  `"spot disagree: CL=UP TI=DOWN (spot-only mode)"` on fail

**Expected operator telemetry after flipping the flag:**

- Pass rate drops from ~98% (2/3) toward ~57% (unanimous CL + TI)
- `signal_evaluations.gate_failed = 'source_agreement'` rows gain
  a new reason string `'spot disagree'` — operator should see this
  in the UI-01 gate heartbeat's aggregate stats breakdown
- Total trade frequency drops ~40% absolute
- Structured log events emit `gate.source_agreement.spot_disagree`
  with `mode=spot_only`, `cl_dir`, `ti_dir`

## Rollback

1. Set `V11_POLY_SPOT_ONLY_CONSENSUS=false` (or unset it entirely) on
   `/home/novakash/novakash/engine/.env`
2. Restart the engine: `sudo systemctl restart novakash-engine` (or
   `pkill -f engine.main && cd /home/novakash/novakash/engine &&
    ./restart_engine.sh`)
3. Verify in logs: look for `gate.source_agreement.spot_disagree`
   events disappearing and `2/3` reason strings reappearing
4. Alternatively, pure code rollback is a revert of this single
   commit — the diff is contained to one gate class and one test file

## Relationship to prior versions

| Version | Vote topology | Pass rate | Trade freq | Source of truth |
| ------- | ------------- | --------- | ---------- | --------------- |
| v11.0 | Unanimous CL + TI | 56.9% | ~20% | CHANGELOG-v11.0 |
| v11.1 | 2/3 CL + TI + BIN | 98.2% | ~73% | CHANGELOG-v11.1 |
| **DQ-01 (flag ON)** | **Spot-only CL + TI (unanimous)** | **~57% est.** | **~40% est.** | this doc |

DQ-01 is functionally equivalent to reverting to v11.0 on the
consensus vote, but keeps the v11.1 evidence column names, telemetry
columns, and downstream gate plumbing intact. It is a minimal
surgical change that lets operators flip between the two voting
topologies in live trading without a code change.

## Files touched

- `engine/signals/gates.py` — `SourceAgreementGate` gains `__init__`
  that reads `V11_POLY_SPOT_ONLY_CONSENSUS` and a branch in
  `evaluate` that implements the spot-only path
- `engine/tests/test_source_agreement_spot_only.py` — NEW 16-case
  test suite covering default-off preservation, enabled-mode votes,
  spot-only disagree fail, fail-closed on missing CL/TI, BIN-None
  tolerance, and flag value parsing (case-insensitivity)
- `docs/CHANGELOG-DQ01-POLY-SPOT-ONLY-CONSENSUS.md` — this file

## Verification

- `pytest engine/tests/test_source_agreement_spot_only.py
   engine/tests/test_eval_offset_bounds_gate.py` → **23/23 pass**
- AST parse on `engine/signals/gates.py` → valid
- `git diff origin/develop..HEAD -- margin_engine/ hub/ frontend/` → empty
  (scope strict — engine only)

## Rationale for default OFF

Same three reasons as DS-01 and DQ-07:

1. **Zero-deploy-risk merges.** A merge that flips live behaviour
   mid-window has no safe rollback path. A merge that ships a
   dormant flag has an instant rollback (don't flip the flag).
2. **A/B evidence before commitment.** Operators can run the flag
   ON for one day and compare PnL / pass rate / gate_failed
   distribution against the flag-OFF baseline directly, without
   going through a revert cycle.
3. **Precedent pattern.** DS-01 (V10_6_ENABLED), DQ-07
   (MARGIN_V4_MAX_MARK_DIVERGENCE_BPS), macro-advisory gates — all
   ship default-OFF. Operators expect new safety changes to arrive
   this way.
