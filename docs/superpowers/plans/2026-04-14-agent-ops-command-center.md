# Agent Ops Command Center — Implementation Plan

See docs/HANDOVER_2026-04-14_SESSION2.md for the full architecture description.

Plan details will be written in a fresh session after the infra split is complete.

Key decisions:
- Use Claude Agent SDK (pip install claude-agent-sdk)
- Custom MCP tools for DB query
- Agents can Read/Edit/Bash + open PRs via gh CLI
- Hub endpoint spawns agents, saves reports to agent_tasks table
- Frontend /ops page with button grid + report cards

