# GitHub Actions CI/CD Setup for Montreal Engine Deployment

## Status
✅ **Engine manually deployed and running** (as of 2026-04-13 12:31 UTC)  
✅ **CI/CD workflow updated** with PR checks  
⚠️ **GitHub Actions secrets required** - manual configuration needed

## Required GitHub Actions Secrets

Go to: `https://github.com/billybrichards/novakash/settings/secrets/actions`

### 1. ENGINE_SSH_KEY (Private Key)

The deploy key we generated: `/tmp/github_deploy_key`

**Public key already added to Montreal:**
```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGmBcMGxir2ohFTaVyNZThDAmhAs+LWWYzL54JAQf/en github-actions-deploy
```

**Get the private key:**
```bash
# If you still have it:
cat /tmp/github_deploy_key

# Or regenerate:
ssh-keygen -t ed25519 -f /tmp/github_deploy_key -N "" -C "github-actions-deploy"
cat /tmp/github_deploy_key
```

**Add to GitHub:**
- Name: `ENGINE_SSH_KEY`
- Value: Paste the FULL private key (including `-----BEGIN OPENSSH PRIVATE KEY-----` and `-----END OPENSSH PRIVATE KEY-----`)

### 2. ENGINE_HOST

- Name: `ENGINE_HOST`
- Value: `15.223.247.178`

### 3. Other Required Secrets

The workflow also needs these (may already exist):

| Secret | Value | Notes |
|--------|-------|-------|
| `DATABASE_URL` | `postgresql://...` | From Montreal's `.env` |
| `COINGLASS_API_KEY` | `abd0524e5...` | From Montreal's `.env` |
| `BINANCE_API_KEY` | `...` | From Montreal's `.env` |
| `BINANCE_API_SECRET` | `...` | From Montreal's `.env` |
| `POLY_API_KEY` | `dab14494-...` | From Montreal's `.env` |
| `POLY_API_SECRET` | `EABOaoY...` | From Montreal's `.env` |
| `POLY_API_PASSPHRASE` | `ade6063...` | From Montreal's `.env` |
| `POLY_PRIVATE_KEY` | `0xa180...` | From Montreal's `.env` |
| `POLY_FUNDER_ADDRESS` | `0x181D2...` | From Montreal's `.env` |
| `POLY_SIGNATURE_TYPE` | `1` | From Montreal's `.env` |
| `TELEGRAM_BOT_TOKEN` | `8696560188:...` | From Montreal's `.env` |
| `TELEGRAM_CHAT_ID` | `1000045351` | From Montreal's `.env` |

## How to Get Montreal .env Values

```bash
# SSH to Montreal (using EC2 Instance Connect)
ssh-keygen -t ed25519 -f /tmp/ec2_temp_key -N "" -q
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user ubuntu \
  --ssh-public-key file:///tmp/ec2_temp_key.pub \
  --availability-zone ca-central-1b \
  --region ca-central-1
ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes \
  -i /tmp/ec2_temp_key ubuntu@15.223.247.178

# View .env
sudo cat /home/novakash/novakash/engine/.env
```

## Workflow Behavior

### On Pull Request
1. ✅ Python syntax check runs
2. ✅ PR check passes
3. ❌ **No deployment** (waits for merge)

### On Push to develop
1. ✅ Python syntax check runs
2. ✅ Code deployed to Montreal
3. ✅ Engine restarted
4. ✅ Health probe verifies process running
5. ✅ Log scan checks for errors

## Verify Setup

After adding secrets, trigger a manual deploy:
1. Go to: `https://github.com/billybrichards/novakash/actions/workflows/deploy-engine.yml`
2. Click "Run workflow"
3. Select "develop" branch
4. Click "Run workflow"

## Current Status

- **Instance:** `i-0785ed930423ae9fd` (15.223.247.178)
- **Region:** `ca-central-1b` (Montreal)
- **Engine Process:** ✅ Running
- **Latest Code:** ✅ Pulled (commit `9cc9ba2` + fix)
- **Deploy Key:** ✅ Added to `/home/novakash/.ssh/authorized_keys`

## Troubleshooting

### Deployment fails with "Permission denied (publickey)"
- Check `ENGINE_SSH_KEY` secret is correct
- Verify public key is in `/home/novakash/.ssh/authorized_keys` on Montreal

### Deployment fails with "host key verification failed"
- Workflow has hardcoded host key for 15.223.247.178
- Should work automatically

### Engine doesn't start after deploy
- Check `/home/novakash/engine.log` on Montreal
- Verify `.env` file has all required secrets
- Check Python syntax: `sudo -u novakash bash -c 'cd /home/novakash/novakash/engine && python3 -m py_compile main.py'`

---
**Last Updated:** 2026-04-13 12:35 UTC
**Deployed by:** Manual SSH via EC2 Instance Connect
**Next Step:** Add GitHub Actions secrets to enable auto-deploy
