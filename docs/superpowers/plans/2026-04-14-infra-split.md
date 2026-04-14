# Infrastructure Split — TimesFM + Hub Separation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the shared 3.98.114.0 box (TimesFM + Hub + data-collector + macro-observer) into two dedicated instances to stop TimesFM OOM from killing the Hub and allow agent ops workloads.

**Architecture:** TimesFM gets a dedicated c6a.xlarge (8GB) or c6a.2xlarge (16GB) box. Hub + data-collector + macro-observer move to a new t3.medium (4GB). Engine stays on 15.223.247.178. CI/CD workflows updated to deploy to new IPs.

**Tech Stack:** AWS EC2, Docker Compose, GitHub Actions, Caddy

---

## Current State

```
3.98.114.0 (c6a.xlarge — 4 CPU / 8GB RAM) — SHARED, keeps OOMing
├── timesfm-api (ML model, 1.3-5GB RAM)
├── hub (FastAPI, ~100MB)  
├── data-collector (~40MB)
└── macro-observer (~70MB)

15.223.247.178 (t3.medium — 2 CPU / 4GB RAM)
└── engine (Python trading engine)

Railway PostgreSQL (shared by all)
```

## Target State

```
NEW_TIMESFM_IP (c6a.2xlarge — 8 CPU / 16GB RAM)
└── timesfm-api (ML model, up to 12GB with swap)

NEW_HUB_IP (t3.medium — 2 CPU / 4GB RAM)
├── hub (FastAPI)
├── data-collector
└── macro-observer

15.223.247.178 (t3.medium — unchanged)
└── engine
```

## File Map

| File | Change |
|------|--------|
| `.github/workflows/deploy-hub.yml` | Update HOST to new Hub IP |
| `.github/workflows/deploy-engine.yml` | Update TIMESFM_URL to new TimesFM IP |
| `engine/.env` on Montreal | Update TIMESFM_URL |
| GitHub Secrets | Add HUB_HOST_NEW, TIMESFM_HOST_NEW, SSH keys |

## Tasks

### Task 1: Provision new EC2 instances

- [ ] Launch TimesFM instance: c6a.2xlarge, ca-central-1d, Ubuntu 22.04, 30GB gp3
- [ ] Launch Hub instance: t3.medium, ca-central-1d, Ubuntu 22.04, 20GB gp3
- [ ] Configure security groups: allow 8080 (TimesFM), 8091 (Hub), 22 (SSH)
- [ ] Tag instances: novakash-timesfm-v2, novakash-hub-v2
- [ ] Allocate Elastic IPs for both (prevents IP change on restart)
- [ ] Generate SSH key pairs, add to GitHub Secrets

### Task 2: Set up TimesFM box

- [ ] SSH to new TimesFM box, install Docker
- [ ] Clone timesfm-service repo
- [ ] Copy docker-compose.yml with memory limits (12GB limit, 14GB swap limit)
- [ ] Add 4GB swap file
- [ ] Copy .env from old box
- [ ] `docker compose up -d`
- [ ] Verify /v4/health and /v2/health
- [ ] Verify from Montreal engine box: `curl http://NEW_TIMESFM_IP:8080/v4/health`

### Task 3: Set up Hub box

- [ ] SSH to new Hub box, install Docker
- [ ] Clone novakash repo (hub/ directory)
- [ ] Copy docker-compose.yml for hub + data-collector + macro-observer
- [ ] Copy .env with DATABASE_URL, all API keys
- [ ] `docker compose up -d`
- [ ] Verify /api/docs endpoint
- [ ] Test from browser

### Task 4: Update CI/CD

- [ ] Update `deploy-hub.yml`: HUB_HOST secret → new IP
- [ ] Update `deploy-engine.yml`: TIMESFM_URL → new IP in .env template
- [ ] Update GitHub Secrets: HUB_HOST, HUB_URL, TIMESFM_HOST
- [ ] Update Montreal engine .env: TIMESFM_URL → new IP
- [ ] Restart engine on Montreal to pick up new TIMESFM_URL

### Task 5: Verify and decommission

- [ ] Run full system health check: all feeds, all strategies, TimesFM, Hub
- [ ] Verify at least one paper trade goes through end-to-end
- [ ] Stop containers on old 3.98.114.0 box
- [ ] Terminate old instance (after 24h observation)

---

## Critical Details

- **Elastic IPs prevent IP changes on restart** — no more TIMESFM_URL drift
- **Engine (Montreal) connects to TimesFM via public IP** — ensure security group allows 15.223.247.178
- **Hub connects to Railway PostgreSQL** — same DATABASE_URL, no change needed
- **TimesFM 16GB box**: set docker memory limit to 12GB, leave 4GB for OS + swap
- **data-collector and macro-observer** connect to TimesFM — update their TIMESFM_URL env vars
