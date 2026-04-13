# ML Upgrade Plan — Shadow Mode Only, No Promotion Without Approval

> **HARD GATE:** No model promotion, no threshold changes, no strategy parameter
> changes go live without Billy's personal evaluation of performance data.
> Everything runs in shadow/ghost mode until explicitly promoted.

**Goal:** Systematically improve ML training pipeline and model quality while
accumulating evaluation data. All upgrades are observable (shadow mode, ghost
strategies, A/B dashboards) but zero changes to live trading until approved.

---

## Phase 1: Data Foundation (this week)

### 1a. Shadow-label all windows ✅ (PR #164)
- Every 2-min tick: stamp `actual_direction` on all resolved `window_snapshots`
- 10x training corpus (was: only traded windows labeled)
- **Ships to prod**: Safe — pure DB writes, no execution path change

### 1b. Merge PR #164 and verify
- [ ] Merge into develop
- [ ] Watch Montreal logs: `reconcile_uc.complete windows_labeled=N`
- [ ] Verify: `SELECT COUNT(*) FROM window_snapshots WHERE actual_direction IS NOT NULL`
- [ ] After 24h: count total labeled windows (expect ~288/day × history)

### 1c. Context extension 2048→8192 (timesfm repo)
- **Repo:** `novakash-timesfm-repo` (separate PR)
- **Change:** `main.py:103` → `max_context=8192`, `price_feed.py` → `BUFFER_SIZE=8192`
- **Impact:** 137 min context instead of 34 min. +200ms latency (700→900ms)
- **Safety:** Zero output format change. v2 scorer consumes same fields.
- **Evaluation:** Compare prediction accuracy (pre/post) on labeled windows
- **GATE:** Run 48h shadow, collect accuracy metrics, Billy reviews before keeping

### 1d. Quantile-derived features (timesfm repo)
- **Repo:** `novakash-timesfm-repo`
- **Change:** `v2_scorer.py` — add 4 new features from existing quantile data:
  - `tfm_tail_risk = (P50 - P10) / P50`
  - `tfm_skew = (P90 + P10 - 2×P50) / spread`
  - `tfm_interval_width = (P75 - P25) / P50`
  - `tfm_quantile_ratio = P75 / P25`
- **Impact:** 5 → 9 TimesFM features in LightGBM input
- **Safety:** Additive features only. Existing models ignore new columns. LightGBM retrain needed to USE them.
- **GATE:** Features recorded but not consumed until next retrain cycle (Phase 2)

---

## Phase 2: Training Pipeline Validation (week 2)

### 2a. Run training pipeline end-to-end (dry run)
- [ ] `cd novakash-timesfm-repo && python training/build_dataset.py --dry-run`
- [ ] Check: how many labeled rows per Δ bucket (30/60/90/120/180/240)?
- [ ] Check: which features have >50% null rate?
- [ ] Check: which joins are broken?
- **No model trained yet** — just validating the pipeline produces clean data

### 2b. Gate importance analysis
- [ ] Run against Railway DB:
  ```sql
  SELECT gate_failed, COUNT(*) as blocked,
         COUNT(*) FILTER (WHERE actual_direction IS NOT NULL) as labeled,
         ROUND(AVG(CASE WHEN actual_direction = direction THEN 1.0 ELSE 0.0 END)::numeric, 3) as would_have_won_pct
  FROM gate_audit ga
  JOIN window_snapshots ws ON ga.window_ts = ws.window_ts AND ga.asset = ws.asset
  WHERE ga.decision = 'SKIP'
  GROUP BY gate_failed ORDER BY blocked DESC
  ```
- [ ] Document: which gates block the most trades? Which block trades that would have been correct?
- **GATE:** Results presented to Billy for threshold tuning decisions

### 2c. Feature selection analysis
- [ ] Train logistic regression (baseline) on labeled windows
- [ ] Train LightGBM with all 43+ features
- [ ] Rank features by gain
- [ ] Identify bottom 50% (noise candidates)
- **GATE:** Feature selection decisions reviewed by Billy before any model uses them

### 2d. Per-Δ-bucket accuracy analysis
- [ ] For each bucket (30/60/90/120/180/240s):
  - Accuracy, base rate, skill = accuracy - base_rate
  - Row count per bucket
  - Null rate per feature
- [ ] Identify sweet spot buckets (T-60 to T-90 from window analysis)
- [ ] Identify noise buckets (T-30 if too volatile, T-240 if too stale)
- **Output:** Report with per-bucket metrics for Billy to review

---

## Phase 3: Model Retraining (week 3, only if Phase 2 approved)

### 3a. Retrain v2 LightGBM with new features
- **Prerequisite:** 500+ labeled rows per Δ bucket
- **New features:** 4 quantile-derived + any selected from Phase 2c
- **Procedure:**
  1. `training/build_dataset.py` → Parquet with new feature columns
  2. `training/train_lgb.py` → Walk-forward split, one model per Δ bucket
  3. `training/calibration.py` → Temperature-scaled isotonic
  4. **DO NOT run `--promote`**
- **Output:** Model artifacts in `/training/output/` (local only, NOT in S3)
- **Evaluation metrics per bucket:**
  - Accuracy vs base rate
  - Brier score
  - Calibration plot (predicted P vs actual frequency)
  - Sharpe ratio (simulated PnL)
  - Comparison table: old model vs new model on same test set
- **GATE:** Billy reviews comparison table. Only `--promote` after explicit approval.

### 3b. Shadow deploy new model (if approved by Billy)
- Upload to S3 as `shadow/` prefix (not `current.json`)
- Run dual inference: production model + shadow model on every window
- Log both outputs to `ticks_v2_probability` with `model_version` distinguisher
- Dashboard: side-by-side accuracy tracking
- **Duration:** 7 days minimum
- **GATE:** Billy reviews shadow performance dashboard after 7 days

### 3c. Promote (only on Billy's explicit approval)
- `scripts/aws_bootstrap.py --promote --version=<sha>`
- Updates `current.json` pointer
- Telegram alert: "Model promoted: v2.X.Y → v2.X.Z"
- Monitor live accuracy for 24h

---

## Phase 4: Fine-tuning Pipeline (week 4+, only if Phase 3 shows value)

### 4a. Export labeled dataset for TimesFM fine-tuning
- Export from Railway: all windows with `actual_direction IS NOT NULL`
- Format: time series chunks aligned to window boundaries
- Walk-forward split (same as v2)

### 4b. Implement fine-tuning loop
- HuggingFace `timesfm` library fine-tuning API
- Hyperparameter sweep: learning rate, epochs, context length
- Validation: accuracy on holdout, comparison to zero-shot
- **GATE:** Accuracy comparison table reviewed by Billy

### 4c. Shadow deploy fine-tuned TimesFM
- Run fine-tuned model in parallel with base model
- Dual output columns in `ticks_timesfm`
- 7-day shadow period
- **GATE:** Billy reviews shadow performance dashboard

### 4d. Promote fine-tuned model (only on Billy's approval)
- Swap model weights on Montreal EC2
- Monitor latency (fine-tuned may be slower)
- Telegram alert on promotion

---

## Phase 5: Directional Asymmetry (parallel with Phase 3-4)

### 5a. DOWN-only classifier
- Train LightGBM on DOWN-labeled windows only
- Separate feature set (may need fewer features)
- Shadow-evaluate alongside existing strategies (GHOST mode in strategy registry)
- **GATE:** Performance review by Billy

### 5b. UP session-specific classifier
- Train on Asian session (23:00-02:59 UTC) UP windows only
- The 81-99% WR finding from Apr 12 window analysis
- Shadow-evaluate as GHOST strategy
- **GATE:** Performance review by Billy

---

## Evaluation Dashboard Requirements

For Billy to make promotion decisions, the dashboard needs:

1. **Per-model accuracy over time** — rolling 7-day accuracy for each model version
2. **Base rate comparison** — is the model actually beating random?
3. **Per-Δ-bucket breakdown** — which time offsets perform best?
4. **Calibration plot** — does P=0.7 actually mean 70% chance of UP?
5. **Simulated PnL** — if we traded every prediction, what's the equity curve?
6. **A/B comparison** — production model vs shadow model, same windows

This can be built as a new frontend page (`/ml-evaluation`) reading from
`ticks_v2_probability` + `window_snapshots` with `model_version` filtering.

---

## Key Principle

**Observe first, act second.** Every ML change follows this pattern:
1. Record new data (shadow mode, no behavior change)
2. Analyze recorded data (offline, present to Billy)
3. Billy decides whether to proceed
4. Shadow deploy candidate (dual inference, no trading change)
5. Billy evaluates shadow performance (minimum 7 days)
6. Billy explicitly promotes (or rejects)

No automated promotions. No "looks good, ship it." Every model change
goes through Billy's hands.
