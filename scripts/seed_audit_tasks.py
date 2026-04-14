#!/usr/bin/env python3
"""
seed_audit_tasks.py — Seed audit_tasks_dev from the static TASKS array in
frontend/src/pages/AuditChecklist.jsx.

Reads the JSX file, extracts every task's id/category/severity/status/title via
regex, then POSTs each one to the Hub /api/audit-tasks endpoint with
dedupe_key=task.id so re-runs are idempotent (ON CONFLICT DO UPDATE keeps the
row but preserves any status changes made since the last seed).

Usage:
  python scripts/seed_audit_tasks.py

Environment (falls back to defaults for local dev):
  HUB_URL      — e.g. http://16.54.141.121:8091  (default: http://localhost:8000)
  HUB_USERNAME — Hub login username               (default: billy)
  HUB_PASSWORD — Hub login password               (required — no default)

On Montreal / CI you can pass credentials inline:
  HUB_URL=http://16.54.141.121:8091 HUB_USERNAME=billy HUB_PASSWORD=xxx \
      python scripts/seed_audit_tasks.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

HUB_URL = os.environ.get("HUB_URL", "http://localhost:8000").rstrip("/")
HUB_USERNAME = os.environ.get("HUB_USERNAME", "billy")
HUB_PASSWORD = os.environ.get("HUB_PASSWORD", "")

REPO_ROOT = Path(__file__).parent.parent
JSX_FILE = REPO_ROOT / "frontend" / "src" / "pages" / "AuditChecklist.jsx"

# Map static severity labels → numeric priority for the DB schema
SEVERITY_PRIORITY = {
    "CRITICAL": 10,
    "HIGH": 7,
    "MEDIUM": 5,
    "LOW": 3,
    "INFO": 1,
}

# Category IDs that belong to CATEGORIES, not TASKS — exclude from seed
_CATEGORY_IDS = {
    "data-quality",
    "production-errors",
    "decision-surface",
    "v4-adoption",
    "clean-architect",
    "frontend",
    "ci-cd",
    "signal-optimization",
    "config-migration",
    "ml-training-data",
    "btc-15m-expansion",
}

# ── Extraction ─────────────────────────────────────────────────────────────────


def extract_tasks(jsx_path: Path) -> list[dict]:
    """Parse the TASKS array from AuditChecklist.jsx and return a list of dicts."""
    content = jsx_path.read_text()

    # Pattern 1 — most common field order: id, category, severity, status, title
    pattern1 = (
        r"id: '([^']+)',\s*\n\s*"
        r"category: '([^']+)',\s*\n\s*"
        r"severity: '([^']+)',\s*\n\s*"
        r"status: '([^']+)',\s*\n\s*"
        r"title: '([^']+)'"
    )
    # Pattern 2 — id, category, title, severity, status (ML / 15M tasks)
    pattern2 = (
        r"id: '([^']+)',\s*\n\s*"
        r"category: '([^']+)',\s*\n\s*"
        r"title: '([^']+)',\s*\n\s*"
        r"severity: '([^']+)',\s*\n\s*"
        r"status: '([^']+)'"
    )

    tasks: dict[str, dict] = {}

    for m in re.finditer(pattern1, content):
        tid, cat, sev, stat, title = m.groups()
        if tid not in _CATEGORY_IDS:
            tasks[tid] = {
                "id": tid,
                "category": cat,
                "severity": sev,
                "status": stat,
                "title": title,
            }

    for m in re.finditer(pattern2, content):
        tid, cat, title, sev, stat = m.groups()
        if tid not in _CATEGORY_IDS and tid not in tasks:
            tasks[tid] = {
                "id": tid,
                "category": cat,
                "severity": sev,
                "status": stat,
                "title": title,
            }

    # Preserve original TASKS array order
    order: list[str] = []
    for m in re.finditer(r"id: '([^']+)'", content):
        tid = m.group(1)
        if tid in tasks and tid not in order:
            order.append(tid)

    return [tasks[tid] for tid in order if tid in tasks]


# ── Auth ───────────────────────────────────────────────────────────────────────


def login(session: requests.Session) -> str:
    """POST /auth/login and return the access token."""
    if not HUB_PASSWORD:
        print("ERROR: HUB_PASSWORD environment variable is not set.", file=sys.stderr)
        print("       export HUB_PASSWORD=<your-hub-password>", file=sys.stderr)
        sys.exit(1)

    resp = session.post(
        f"{HUB_URL}/auth/login",
        json={"username": HUB_USERNAME, "password": HUB_PASSWORD},
        timeout=15,
    )
    if not resp.ok:
        print(f"ERROR: Login failed {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)

    token = resp.json().get("access_token")
    if not token:
        print(f"ERROR: No access_token in login response: {resp.json()}", file=sys.stderr)
        sys.exit(1)

    return token


# ── Seed ───────────────────────────────────────────────────────────────────────


def seed(tasks: list[dict], token: str, session: requests.Session) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    created = skipped = errors = 0

    for t in tasks:
        # Map static severity → DB severity + priority
        sev = t["severity"]
        priority = SEVERITY_PRIORITY.get(sev, 0)

        # Normalise status: DONE→DONE, OPEN→OPEN, INFO→INFO, BLOCKED→BLOCKED,
        # IN_PROGRESS→IN_PROGRESS — the DB stores uppercase strings
        status = t["status"].upper()

        payload = {
            "task_key": t["id"],
            "task_type": "audit_checklist",
            "source": "audit_checklist_jsx",
            "title": t["title"],
            "status": status,
            "severity": sev,
            "category": t["category"],
            "priority": priority,
            "dedupe_key": t["id"],
            "payload": {},
            "metadata": {"jsx_id": t["id"]},
        }

        try:
            resp = session.post(
                f"{HUB_URL}/api/audit-tasks",
                json=payload,
                headers=headers,
                timeout=15,
            )
            if resp.ok:
                row = resp.json().get("task", {})
                action = "created" if row.get("attempt_count", 0) == 0 else "upserted"
                print(f"  [{action}] {t['id']}: {t['title'][:60]}")
                created += 1
            else:
                print(
                    f"  [ERROR {resp.status_code}] {t['id']}: {resp.text[:120]}",
                    file=sys.stderr,
                )
                errors += 1
        except requests.RequestException as exc:
            print(f"  [EXCEPTION] {t['id']}: {exc}", file=sys.stderr)
            errors += 1

    print()
    print(f"Done. {created} upserted, {skipped} skipped, {errors} errors.")


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    print(f"Reading tasks from: {JSX_FILE}")
    if not JSX_FILE.exists():
        print(f"ERROR: {JSX_FILE} not found", file=sys.stderr)
        sys.exit(1)

    tasks = extract_tasks(JSX_FILE)
    print(f"Extracted {len(tasks)} tasks from TASKS array")
    print()

    print(f"Connecting to Hub: {HUB_URL}")
    session = requests.Session()
    token = login(session)
    print(f"Logged in as {HUB_USERNAME}")
    print()

    print("Seeding tasks (idempotent — safe to re-run)...")
    seed(tasks, token, session)


if __name__ == "__main__":
    main()
