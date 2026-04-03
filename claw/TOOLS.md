# Agent Tooling Reference

## Browser Access

```bash
# Browser tool (preferred)
browser(action="start")
browser(action="navigate", url="...")
browser(action="screenshot")
browser(action="snapshot")

# VNC Desktop
# Display: :1 (1920x1080), VNC port: 5901
# Websockify: port 16007
# Password: openclaw
```

## Gmail Access

- **Email:** bbrichards123@gmail.com
- **App Password:** oxkfkhchcoljzxkr
- **IMAP:** imap.gmail.com:993 (TLS)
- **SMTP:** smtp.gmail.com:587 (STARTTLS)

```bash
# himalaya CLI
himalaya envelopes list
himalaya message read <ID>
himalaya envelopes list --query "subject:polymarket"
```

## Mission Control

```bash
TOKEN=$(cat /root/.mc_auth_token)

# Log activity
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://localhost:8000/api/v1/bot-activity/ \
  -d '{"agent_id":"novakash2","action_type":"<type>","metadata":{...}}'

# Board ID: 453ce251-1cb0-464e-bef1-a8e83fca0e1e
```

## Railway CLI

```bash
cd /root/.openclaw/workspace-novakash/novakash

railway variable list          # Show all env vars
railway variable set KEY=VALUE # Set env var
railway logs --tail 50         # View logs
railway restart --yes          # Restart service
railway up -d -m "message"     # Deploy
railway deployment list        # List deployments
```

## Lossless Claw (Context Management)

```bash
lcm_grep "search term"     # Search compacted history
lcm_describe <node_id>     # Inspect summary node
lcm_expand <node_id>       # Expand to full detail
```

## Frontend Dev

```bash
cd novakash/frontend
npm install
npm run dev                 # localhost:3000
npm run build               # Production build
```

## Design Skills

```bash
npx skills add shadcn/ui
npx skills add emilkowalski/skill
npx skills add raphaelsalaja/userinterface-wiki
```
