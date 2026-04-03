# Agents — OpenClaw Configuration

Three agents interact with this project. All run on OpenClaw.

---

## 1. Novakash (Bot 1)

- **Telegram:** @Novakash_bot
- **Workspace:** `/root/.openclaw/workspace-novakash/`
- **Model:** qwen35-122b-abliterated (via ollama-pro6000)
- **Role:** Primary development agent. Built the engine, hub, frontend. Handles Railway deploys, config changes, bug fixes.
- **Session Key:** `agent:novakash:telegram:direct:1000045351`

### Key Configs
- CLAUDE.md with plan-first methodology
- Subagent strategy for parallel work
- Self-improvement loop (tasks/lessons.md)

---

## 2. Novakash2 (Bot 2)

- **Telegram:** @novakashbot2_bot
- **Workspace:** `/root/.openclaw/workspace-novakash2/`
- **Model:** Claude Opus 4.6 (default) / qwen35-122b (session override)
- **Role:** Design-first frontend agent. UI/UX, animations, design systems.
- **Session Key:** `agent:novakash2:telegram:direct:1000045351`

### Key Configs
- SOUL.md — design philosophy (animations, spacing, tokens)
- AGENTS.md — mandatory planning protocol, design standards
- IDENTITY.md — specialisations (shadcn/ui, emilkowalski, userinterface-wiki)
- TOOLS.md — browser access, VNC, Gmail credentials, Mission Control
- HEARTBEAT.md — periodic checks

### Design Standards
- Animations: 200-350ms enter (ease-out-quart), 150-200ms exit (ease-in)
- Scale: 0.95→1, never 0→1
- Spacing: 4/8/12/16/24/32/48/64px systematic scale
- Components: shadcn/ui foundation, CSS vars for theming
- Accessibility: WCAG AA 4.5:1 contrast, prefers-reduced-motion
- Never hardcode colours or spacing

---

## 3. Novakash_bot (Telegram Alerts)

- **Token:** 8696560188:AAH_s1bpcRe__DCHV7sgQNBmPbbHzH-JdGI
- **Chat ID:** 1000045351
- **Role:** Sends Telegram alerts for trades, signals, cascades, kill switch events
- **Config:** Set via TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID on Railway
- **Code:** `engine/alerts/telegram.py`

---

## Owner

- **Name:** Billy Richards
- **Telegram:** @brb1480 (ID: 1000045351)
- **Email:** bbrichards123@gmail.com
- **Preferences:** Direct, no waffle. Show design decisions. Flag issues proactively. Ask before pushing to production.

---

## Mission Control

- **Board ID:** 453ce251-1cb0-464e-bef1-a8e83fca0e1e
- **Auth:** `cat /root/.mc_auth_token`
- **API:** http://localhost:8000/api/v1
