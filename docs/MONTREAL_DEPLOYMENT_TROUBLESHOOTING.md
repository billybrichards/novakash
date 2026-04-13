# Montreal Engine Deployment Troubleshooting Guide

## Issue
**Deployment Status:** Failing for 44+ consecutive runs  
**Failure Point:** Step 10 - "Restart engine via scripts/restart_engine.sh"  
**Root Cause:** Unknown - requires server-side investigation

## Access Required
- **Server:** Montreal EC2 instance
- **Host:** 15.223.247.178 (Public IP), Instance ID: i-0785ed930423ae9fd
- **Region:** ca-central-1 (Montreal)
- **User:** `novakash` (engine process user, NOT ubuntu)
- **SSH Key:** `ENGINE_SSH_KEY` GitHub Actions secret (ed25519 key stored at /home/novakash/.ssh/github_deploy)

### SSH Connection Methods

**Method 1: GitHub Actions Deploy Key (Recommended for CI/CD)**
```bash
# Decode the ENGINE_SSH_KEY secret and use it
echo "$ENGINE_SSH_KEY" | base64 -d > /tmp/deploy_key
chmod 600 /tmp/deploy_key
ssh -i /tmp/deploy_key -o "StrictHostKeyChecking=no" novakash@15.223.247.178
```

**Method 2: EC2 Instance Connect (Manual Access)**
```bash
# Note: Defaults to ec2-user, not novakash
aws ec2-instance-connect ssh --instance-id i-0785ed930423ae9fd --region ca-central-1
# Then switch to novakash: sudo -u novakash -i
```

**Method 3: SSH Key Pair (If you have the private key)**
```bash
# If you have the novakash-montreal.pem key
ssh -i ~/.ssh/novakash-montreal.pem novakash@15.223.247.178
```

**Current Status (2026-04-13):**
- Local keys (~/.ssh/novakash-local-rsa.pem, ~/.ssh/novakash-margin-engine.pem) do NOT work
- AWS key pair 'novakash-montreal' exists but private key not available locally
- EC2 Instance Connect defaults to ec2-user, requires sudo to switch to novakash
- GitHub Actions workflow configured with StrictHostKeyChecking=no

## Manual Investigation Steps

### 1. SSH into Montreal Server
```bash
ssh -i <path-to-deploy-key> ubuntu@3.98.114.0
```

### 2. Check if Engine Can Start Manually
```bash
# Switch to novakash user
sudo -u novakash -i

# Navigate to engine directory
cd /home/novakash/novakash/engine

# Check Python version
python3 --version

# Check if main.py has syntax errors
python3 -m py_compile main.py

# Try starting the engine (will block, use Ctrl+C to stop)
python3 main.py
```

### 3. Check Engine Logs
```bash
# View recent log entries
sudo tail -100 /home/novakash/engine.log

# View rotated logs
ls -la /home/novakash/engine-*.log

# Check for specific errors
sudo grep -i "error" /home/novakash/engine.log | tail -50
```

### 4. Check Process Status
```bash
# Check if any python3 main.py processes are running
pgrep -fa "python3 main.py"

# Check system resources
free -h
df -h
uptime
```

### 5. Check Environment
```bash
# Check Python environment
which python3
python3 -c "import sys; print(sys.executable)"

# Check installed packages
cd /home/novakash/novakash/engine
python3 -m pip list 2>/dev/null | head -30

# Check .env file
cat /home/novakash/novakash/engine/.env
```

### 6. Check Directory Permissions
```bash
# Check ownership
ls -la /home/novakash/novakash/

# Fix if needed
sudo chown -R novakash:novakash /home/novakash/novakash/
sudo chmod -R 755 /home/novakash/novakash/
```

### 7. Test Restart Script
```bash
cd /home/novakash/novakash/scripts
sudo chmod +x restart_engine.sh
./restart_engine.sh
```

## Common Issues & Fixes

### Issue: Python Module Not Found
```bash
# Install dependencies
cd /home/novakash/novakash/engine
python3 -m pip install -r requirements.txt
```

### Issue: Permission Denied
```bash
# Fix ownership
sudo chown -R novakash:novakash /home/novakash/novakash/
```

### Issue: Port Already in Use
```bash
# Check for conflicting processes
lsof -i :<port>
ps aux | grep python3
```

### Issue: Database Connection Failed
```bash
# Check .env DATABASE_URL
cat /home/novakash/novakash/engine/.env | grep DATABASE_URL

# Test connection
python3 -c "import asyncpg; print('DB connection OK')"
```

## After Investigation

1. **Document findings** in this file
2. **Fix the issue** on the server
3. **Test manually** - ensure engine starts
4. **Re-run deployment** - trigger GitHub Actions workflow manually
5. **Verify** - check that process stays running

## Quick Status Check Command

```bash
ssh -i <key> ubuntu@3.98.114.0 'echo "=== Process Check ===" && pgrep -fa "python3 main.py" && echo "=== Recent Log ===" && sudo tail -50 /home/novakash/engine.log'
```

## Files to Review

- `/home/novakash/engine.log` - Main engine log
- `/home/novakash/novakash/engine/.env` - Environment configuration
- `/home/novakash/novakash/scripts/restart_engine.sh` - Restart script
- `.github/workflows/deploy-engine.yml` - Deployment workflow

## Contact
If issues persist, check:
1. AWS EC2 console for instance health
2. CloudWatch logs if configured
3. Network connectivity to PostgreSQL, Binance WS, etc.

---
**Last Updated:** 2026-04-13  
**Related PR:** #155 (fix/timesfm-v5-chainlink-feature)
