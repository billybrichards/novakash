# Montreal Engine Connection & Monitoring Guide

**Last Updated:** 2026-04-13

## Instance Details

| Field | Value |
|-------|-------|
| **Name** | novakash-montreal-vnc |
| **Instance ID** | i-0785ed930423ae9fd |
| **Public IP** | 15.223.247.178 |
| **Private IP** | 172.31.10.59 |
| **Region** | ca-central-1b (Montreal) |
| **User** | novakash |
| **Engine Process** | `python3 main.py` (nohup background) |
| **Log File** | `/home/novakash/engine.log` |
| **Code Directory** | `/home/novakash/novakash/engine/` |

## Connection Methods

### Method 1: EC2 Instance Connect (Recommended for diagnostics)

**Step-by-step:**

```bash
# 1. Generate temporary key
rm -f /tmp/montreal_key /tmp/montreal_key.pub
ssh-keygen -t ed25519 -f /tmp/montreal_key -N "" -C "montreal-diag-$(date +%s)"

# 2. Send public key to instance (60-second TTL)
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd \
  --availability-zone ca-central-1b \
  --instance-os-user novakash \
  --ssh-public-key file:///tmp/montreal_key.pub

# 3. SSH with 60-second TTL key
aws ec2-instance-connect ssh \
  --instance-id i-0785ed930423ae9fd \
  --region ca-central-1 \
  --os-user novakash
```

**Run single commands without interactive SSH:**

```bash
echo "your command here" | aws ec2-instance-connect ssh \
  --instance-id i-0785ed930423ae9fd \
  --region ca-central-1 \
  --os-user novakash
```

### Method 2: Direct SSH (If you have ENGINE_SSH_KEY)

```bash
# Decode GitHub Actions secret
echo "$ENGINE_SSH_KEY" | base64 -d > /tmp/deploy_key
chmod 600 /tmp/deploy_key

# SSH directly
ssh -i /tmp/deploy_key -o "StrictHostKeyChecking=no" novakash@15.223.247.178
```

## Monitoring Commands

### Check Engine Process Status
```bash
echo "pgrep -fa 'python3 main.py'" | aws ec2-instance-connect ssh \
  --instance-id i-0785ed930423ae9fd --region ca-central-1 --os-user novakash
```

### Check Recent Trade Decisions
```bash
echo "tail -100 /home/novakash/engine.log | grep -E '(TRADE|SKIP|eval_offset)'" | \
  aws ec2-instance-connect ssh \
  --instance-id i-0785ed930423ae9fd --region ca-central-1 --os-user novakash
```

### Check Specific Strategy Decisions
```bash
echo "tail -100 /home/novakash/engine.log | grep -E '(v4_down_only|v4_up_asian)'" | \
  aws ec2-instance-connect ssh \
  --instance-id i-0785ed930423ae9fd --region ca-central-1 --os-user novakash
```

### View Live Log Tail
```bash
echo "tail -f /home/novakash/engine.log" | \
  aws ec2-instance-connect ssh \
  --instance-id i-0785ed930423ae9fd --region ca-central-1 --os-user novakash
```

### Check Environment Variables
```bash
echo "cat /home/novakash/novakash/engine/.env | grep -E '(PAPER_MODE|V10_6_ENABLED)'" | \
  aws ec2-instance-connect ssh \
  --instance-id i-0785ed930423ae9fd --region ca-central-1 --os-user novakash
```

## Restart Engine

```bash
echo "pkill -9 -f 'python3 main.py' && sleep 3 && cd /home/novakash/novakash/engine && nohup python3 main.py >> /home/novakash/engine.log 2>&1 &" | \
  aws ec2-instance-connect ssh \
  --instance-id i-0785ed930423ae9fd --region ca-central-1 --os-user novakash
```

## Known Issues

### Issue 1: V10.6 Gate Blocking v4 Strategies

**Symptom:** All trades skipped with "too early (T-XXX > T-120)"

**Root Cause:**
- Server `.env` has `V10_6_MAX_EVAL_OFFSET=120` (instead of default 180)
- v4_down_only intended trade window: **T-90 to T-150**
- V10.6 gate blocks evaluation at T-120, preventing v4 from trading at T-150

**Server Config (2026-04-13):**
```
PAPER_MODE=true
V10_6_ENABLED=true
V10_6_MIN_EVAL_OFFSET=90
V10_6_MAX_EVAL_OFFSET=120  # <-- This blocks v4!
```

**Solution Options:**
1. **Quick fix:** Change `V10_6_MAX_EVAL_OFFSET=150` in `.env` on server
2. **Disable V10.6:** Set `V10_6_ENABLED=false` in `.env`
3. **Remove gate:** Remove EvalOffsetBoundsGate from pipeline in `five_min_vpin.py`

**To Fix (Option 1 - Recommended):**
```bash
# SSH to server
# Edit .env
vim /home/novakash/novakash/engine/.env
# Change: V10_6_MAX_EVAL_OFFSET=150
# Restart engine
pkill -9 -f 'python3 main.py' && sleep 3 && cd /home/novakash/novakash/engine && nohup python3 main.py >> /home/novakash/engine.log 2>&1 &
```

**Current Status:** V10.6 gate active with max=120, blocking v4_down_only trades

### Issue 2: GitHub Actions Deployments Failing

**Symptom:** "Permission denied (publickey)" in deploy-engine.yml

**Root Cause:**
- `ENGINE_SSH_KEY` GitHub Actions secret doesn't match authorized_keys on server
- Server has different SSH key at `/home/novakash/.ssh/github_deploy`

**Workaround:** Use EC2 Instance Connect with temporary keys for manual diagnostics

## Expected eval_offset Behavior

The engine evaluates windows every ~2 seconds with countdown timer:

| eval_offset | Time to Close | Status | Expected Action |
|-------------|---------------|--------|-----------------|
| T-240 to T-120 | 4:00 - 2:00 | Too Early | Skip (V10.6: >T-120) |
| T-120 to T-90 | 2:00 - 1:30 | **Trade Window** | v4_down_only can trade |
| T-90 to T-60 | 1:30 - 1:00 | Too Late | Skip (V10.6: <T-90) |
| T-60 | 1:00 | Final | No v4 trade opportunity |

**Note:** With `V10_6_MAX_EVAL_OFFSET=150`, the trade window would be T-150 to T-90 (2:30 - 1:30)

## Quick Diagnostic Checklist

When diagnosing why no trades are being placed:

1. ✅ **Check eval_offset range:** `tail -50 engine.log | grep eval_offset`
2. ✅ **Check V10.6 gate status:** `grep V10_6_ENABLED .env`
3. ✅ **Check strategy mode:** `grep 'mode=.*LIVE' engine.log | tail -5`
4. ✅ **Check signal conditions:** `grep -E '(v4_down_only|v4upasian)' engine.log | tail -10`
5. ✅ **Check for errors:** `grep ERROR engine.log | tail -10`

## Files of Interest

| File | Purpose |
|------|---------|
| `/home/novakash/engine.log` | Main log file (stdout/stderr) |
| `/home/novakash/novakash/engine/.env` | Environment configuration |
| `/home/novakash/novakash/engine/strategies/five_min_vpin.py` | Main strategy code |
| `/home/novakash/novakash/scripts/restart_engine.sh` | Restart script |

## Troubleshooting Flow

```
No trades?
  ├─ Check eval_offset
  │   ├─ T-150 to T-90? → Strategy should be trading
  │   └─ Outside window? → Normal, waiting for next window
  ├─ Check V10_6_ENABLED
  │   ├─ true? → V10.6 gate blocking (max=120, min=90)
  │   └─ false? → Gate disabled, check other conditions
  ├─ Check signal conditions
  │   ├─ v4_down_only timing? → eval_offset check
  │   └─ v4_up_asian polymarket? → pup/dist thresholds
  └─ Check PAPER_MODE
      ├─ true? → LIVE mode trades, GHOST mode simulates
      └─ false? → Should see LIVE trades
```

---

**Last Diagnostic:** 2026-04-13 14:26 UTC  
**Current Issue:** V10.6 gate blocking at T-120, v4 needs T-150
