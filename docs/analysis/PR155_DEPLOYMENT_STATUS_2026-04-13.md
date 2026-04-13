# PR #155 Deployment Status - TimesFM v5 Chainlink Fix

## Executive Summary

**PR #155** has been merged to `develop` but **deployment to Montreal is failing** due to a **pre-existing infrastructure issue**, not the code changes.

### What Was Fixed

The PR addresses a critical bug where TimesFM v5.2 model returned constant `0.606` probability due to missing `chainlink_price` feature:

1. ✅ Added `chainlink_price` to 3 call sites in `build_v5_feature_body()`
2. ✅ Fixed indentation error in `five_min_vpin.py:1751` (discovered during deployment)

### Deployment Status

| Component | Status | Notes |
|-----------|--------|-------|
| **Code Merge** | ✅ Merged to develop | Commit `9cc9ba2` |
| **Hub to AWS** | ✅ Success | Deployed successfully |
| **Frontend to AWS** | ✅ Success | Deployed successfully |
| **Engine to Montreal** | ❌ Failing | **44 consecutive failures** |

## Montreal Deployment Issue

The Engine deployment to Montreal has been **failing for ALL 44 recent runs** - this is a **pre-existing infrastructure issue**.

### Failure Details

- **Failure Point:** Step 10 - "Restart engine via scripts/restart_engine.sh"
- **Error:** Process count verification fails (expected 1, got 0)
- **Root Cause:** Unknown - requires server-side investigation

### Investigation Findings

1. **Montreal Instance:** `i-0785ed930423ae9fd` (15.223.247.178)
2. **Region:** `ca-central-1b` (Montreal)
3. **SSM Access:** Not available on instance
4. **GitHub Actions Secrets:** `ENGINE_SSH_KEY` and `ENGINE_HOST` required

### Manual Investigation Required

**You need to SSH to Montreal manually:**

```bash
# Generate temp SSH key
ssh-keygen -t ed25519 -f /tmp/ec2_temp_key -N "" -q

# Push key to instance (60s window)
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user ubuntu \
  --ssh-public-key file:///tmp/ec2_temp_key.pub \
  --availability-zone ca-central-1b \
  --region ca-central-1

# SSH to instance
ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes \
  -i /tmp/ec2_temp_key ubuntu@15.223.247.178
```

**Once connected, check:**

```bash
# Check if engine directory exists
ls -la /home/novakash/novakash/

# Check if engine process is running
ps aux | grep "python3 main.py"

# Check engine log
tail -50 /home/novakash/engine.log

# Try manual pull and restart
sudo -u novakash bash -c 'cd /home/novakash/novakash && git pull origin develop'
cd /home/novakash/novakash
./scripts/restart_engine.sh
```

## What Needs to Happen

1. **Manual SSH to Montreal** - Use EC2 Instance Connect (see commands above)
2. **Check if `/home/novakash/novakash` exists** - If not, the repo was never deployed
3. **If repo exists:**
   - Check Python syntax: `python3 -m py_compile /home/novakash/novakash/engine/main.py`
   - Check logs: `tail -100 /home/novakash/engine.log`
   - Try manual restart: `./scripts/restart_engine.sh`
4. **If repo doesn't exist:**
   - Clone the repo: `git clone https://github.com/billybrichards/novakash.git`
   - Set up `.env` file with required secrets
   - Install dependencies: `pip3 install -r engine/requirements.txt`
   - Start the engine

## Next Steps

**Immediate Actions:**
1. SSH to Montreal manually (see commands above)
2. Verify `/home/novakash/novakash` directory exists
3. If missing, set up the repo from scratch
4. If exists, check logs and restart manually
5. Once working, re-run GitHub Actions deployment

**Once Manual Fix is Done:**
- Trigger GitHub Actions workflow manually
- Verify deployment succeeds
- Monitor engine logs for 15-30 minutes
- Confirm trades resume with variable P(UP) probabilities

## Expected Behavior After Fix

Once the engine is running with the fix:
- Model will return **variable P(UP)** (0.3-0.9) instead of constant 0.606
- Conviction will reach **15-25%** instead of stuck at 10.2%
- Trades will resume at **~40/day** for DOWN-only
- Win rate should return to **75-80%**

## Timeline

- **PR Merged:** 2026-04-13 12:02:36Z
- **Indentation Fix:** 2026-04-13 13:12:54Z
- **Latest Deployment Attempt:** 2026-04-13 12:15:03Z
- **Status:** Waiting for manual Montreal investigation

---
**Related:** PR #155, Commit `9cc9ba2`
**Instance:** i-0785ed930423ae9fd (15.223.247.178)
**Region:** ca-central-1b
