# GPU Box Monitor

Live signal-health snapshot of `3.96.151.28` (the GPU classifier box that's the takeover target for primary).

**Report URL (local):** `file:///Users/billyrichards/Code/novakash/docs/gpu-monitor/index.html` — open in any browser.

## What's in here

| File | Purpose |
|---|---|
| `index.html` | The visual report. Self-contained — Tailwind via CDN, no other deps. |
| `soak_2026-05-02.jsonl` | 90 raw samples (15 sweeps × BTC/ETH/XRP × 5m/15m), one JSON record per line |
| `cls_5m_metrics.json` | Training metrics for the 5m classifier head (`cls_traj_14f_iso`) |
| `cls_15m_metrics.json` | Training metrics for the cross-asset 15m classifier (`15m_head_v1`) |
| `lora_5m_history.json` | 5m LoRA training history (`lora_btc_5m_2h_2026-04-25T07-07`) |
| `lora_15m_ft.json` | 15m LoRA fine-tune metrics (`15m_v1/15m_v1`) |
| `v91_provenance.md` | v9.1 LGB lineage + honest-validation tables (+5.92pp lift) |

## Findings (2026-05-02 18:34 UTC soak)

### Healthy ✅

- **BTC 5m** — all 6 signals (pup, lgb, v9.1, v10, v12, classifier) flowing with non-trivial variance. v3 stuck-guard occasionally trips on v9 saturation; self-recovers. Same behavior as primary box.
- **BTC 15m** — pup, lgb (v9), classifier flowing. v9.1/v10/v12 are 5m-only by design (LGB stacks have no 15m boosters yet) — `None` is correct, not a bug.
- **ETH 15m + XRP 15m** — cross-asset 15m classifier flowing (status `no_model` means no LGB exists for these — by design). Currently in top-20% conviction zone (p > 0.62). `v_eth_15m_classifier_top20` and `v_xrp_15m_classifier_top20` would fire UP bets right now once they're enabled.

### Noted ⚠

- **ETH 5m + XRP 5m v9 saturating at 1.0** — the v9 LGB is BTC-trained and being asked to score ETH/XRP features it has no representation for. Output is unreliable. The new ETH/XRP strategies in PR #474 explicitly use the classifier ONLY for these assets — they don't read v9/v10/v12.

## How to regenerate

The HTML is built deterministically from the artifacts in this folder. To refresh after a new soak:

```bash
# 1. Run the soak on GPU box (collects 5 min of samples × 6 streams)
ssh ubuntu@3.96.151.28 'bash -c "<paste soak_full.py>"'

# 2. scp soak.jsonl + model artifacts to /tmp/gpu_report/
scp ubuntu@3.96.151.28:/tmp/soak_full.jsonl /tmp/gpu_report/soak.jsonl
aws s3 cp s3://bbrnovakash-models-do-not-delete/path1_classifier/2026-04-25T15-34/cls_traj_14f_iso/training_metrics.json /tmp/gpu_report/cls_5m_metrics.json
# ... etc — full list in scripts/build_gpu_report.py header

# 3. Rebuild
python3 /Users/billyrichards/Code/novakash/scripts/build_gpu_report.py
```

The build script (`scripts/build_gpu_report.py`) is the single source of truth for what goes into the report. ~430 lines, no external deps beyond Python stdlib.

## Next refresh trigger points

Refresh this report when:
- New model gets deployed to the GPU box (LoRA or classifier swap)
- After SCORER_ASSETS or other env changes restart the container
- Weekly health snapshot during ghost soak of PR #474 strategies
- Before flipping engine `TIMESFM_URL` primary→GPU (sanity check)
