"""
Trading Config API — Manage paper and live trading configurations.

Paper and Live are INDEPENDENT modes — both can run simultaneously.
- Paper: always safe to toggle on/off
- Live: requires an approved config + password confirmation to enable
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from db.database import get_session


class _DBShim:
    """Wraps SQLAlchemy AsyncSession to provide asyncpg-style fetch/fetchrow/execute."""

    def __init__(self, session: AsyncSession):
        self._session = session

    def _prepare(self, query: str, args: tuple) -> tuple:
        """Convert $1, $2... positional params to :p1, :p2... named params."""
        import re
        if not args:
            return query, {}
        params = {}
        for i, val in enumerate(args, 1):
            pname = f"p{i}"
            query = query.replace(f"${i}", f":{pname}", 1)
            params[pname] = val
        return query, params

    async def fetch(self, query: str, *args):
        q, params = self._prepare(query, args)
        result = await self._session.execute(text(q), params)
        return [dict(r) for r in result.mappings().all()]

    async def fetchrow(self, query: str, *args):
        q, params = self._prepare(query, args)
        result = await self._session.execute(text(q), params)
        row = result.mappings().first()
        return dict(row) if row else None

    async def execute(self, query: str, *args):
        q, params = self._prepare(query, args)
        await self._session.execute(text(q), params)
        await self._session.commit()

    async def fetchval(self, query: str, *args):
        q, params = self._prepare(query, args)
        result = await self._session.execute(text(q), params)
        row = result.first()
        return row[0] if row else None


async def get_db():
    async for session in get_session():
        yield _DBShim(session)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/trading-config", tags=["trading-config"])


# ─── Default config template ─────────────────────────────────────────────────

CONFIG_DEFAULTS: list[dict] = [
    # ── Risk Management ───────────────────────────────────────────────────────
    {
        "key": "starting_bankroll",
        "label": "Starting Bankroll",
        "description": "Initial paper/live balance in USD. This is the base from which all position sizes are calculated.",
        "type": "number",
        "default": 100.0,  # Increased for paper mode to see more activity
        "min": 5.0,
        "max": 100000.0,
        "step": 5.0,
        "unit": "USD",
        "category": "risk",
        "impact": "Determines absolute position sizing. Higher = larger bets in USD but same risk percentage.",
        "widget": "slider",
    },
    {
        "key": "bet_fraction",
        "label": "Bet Fraction (Kelly %)",
        "description": "Fraction of bankroll to stake per trade. 0.05 = 5% Kelly. Higher = more aggressive compounding but larger drawdowns.",
        "type": "number",
        "default": 0.10,  # Increased for paper mode (was 0.05)
        "min": 0.01,
        "max": 0.20,
        "step": 0.005,
        "unit": "%",
        "category": "risk",
        "impact": "At 5% with $25 bankroll → $1.25 per trade. At 10% → $2.50 per trade.",
        "widget": "slider",
    },
    {
        "key": "max_position_usd",
        "label": "Max Position Size",
        "description": "Hard cap on any single position in USD, regardless of Kelly fraction. Protects against runaway compounding.",
        "type": "number",
        "default": 500.0,
        "min": 10.0,
        "max": 50000.0,
        "step": 10.0,
        "unit": "USD",
        "category": "risk",
        "impact": "Absolute ceiling per trade. Bankroll growth won't push trades above this.",
        "widget": "slider",
    },
    {
        "key": "max_drawdown_pct",
        "label": "Max Drawdown Kill-Switch",
        "description": "If equity drops this % below peak, the engine halts all trading and waits for manual reset. Protects against catastrophic loss.",
        "type": "number",
        "default": 0.10,
        "min": 0.05,
        "max": 0.50,
        "step": 0.01,
        "unit": "%",
        "category": "risk",
        "impact": "At 10%: $25 bankroll kills at $22.50 equity. A hard stop — engine won't trade until reset.",
        "widget": "slider",
    },
    {
        "key": "daily_loss_limit",
        "label": "Daily Loss Limit",
        "description": "Maximum USD loss allowed in a single trading day before the engine pauses until midnight UTC reset.",
        "type": "number",
        "default": 50.0,
        "min": 1.0,
        "max": 5000.0,
        "step": 1.0,
        "unit": "USD",
        "category": "risk",
        "impact": "Prevents 'tilt' scenarios. At $50 limit with $1.25 trades → ~40 losses before halt.",
        "widget": "number",
    },
    # ── VPIN Signals ──────────────────────────────────────────────────────────
    {
        "key": "vpin_informed_threshold",
        "label": "VPIN Informed Trader Threshold",
        "description": "When VPIN crosses this level, informed trading is detected. The engine enters signal-ready state for directional bets.",
        "type": "number",
        "default": 0.45,  # Lowered for paper mode to generate more signals
        "min": 0.30,
        "max": 0.90,
        "step": 0.01,
        "unit": "ratio",
        "category": "vpin",
        "impact": "Lower = more signals, more noise. Higher = fewer signals, higher quality. Sweet spot: 0.50–0.65.",
        "widget": "slider",
    },
    {
        "key": "vpin_cascade_threshold",
        "label": "VPIN Cascade Threshold",
        "description": "Higher VPIN level that signals a potential cascade liquidation event. Must be above the informed threshold.",
        "type": "number",
        "default": 0.55,  # Lowered for paper mode to generate more cascade signals
        "min": 0.40,
        "max": 0.95,
        "step": 0.01,
        "unit": "ratio",
        "category": "vpin",
        "impact": "Triggers cascade strategy. At 0.70: rare but high-conviction signals. Expect 2–5 per day in volatile markets.",
        "widget": "slider",
    },
    {
        "key": "vpin_bucket_size_usd",
        "label": "VPIN Bucket Size",
        "description": "Volume per bucket in USD. Larger buckets = smoother VPIN signal but slower response to regime changes.",
        "type": "number",
        "default": 50000,
        "min": 10000,
        "max": 200000,
        "step": 5000,
        "unit": "USD",
        "category": "vpin",
        "impact": "Smaller = noisier but faster. Larger = cleaner but lags by minutes in low-volume periods.",
        "widget": "slider",
    },
    {
        "key": "vpin_lookback_buckets",
        "label": "VPIN Lookback Window",
        "description": "Number of buckets used to calculate rolling VPIN. More buckets = more stable but slower to adapt.",
        "type": "number",
        "default": 50,
        "min": 10,
        "max": 100,
        "step": 5,
        "unit": "buckets",
        "category": "vpin",
        "impact": "At 50 buckets × $50K = $2.5M total lookback volume. ~1 hour of moderate BTC volume.",
        "widget": "slider",
    },
    # ── Arb Strategy ─────────────────────────────────────────────────────────
    {
        "key": "arb_min_spread",
        "label": "Arb Minimum Spread",
        "description": "Minimum price spread between venues to trigger an arbitrage trade. Must exceed combined fees to be profitable.",
        "type": "number",
        "default": 0.005,  # Lowered for paper mode (was 0.015) - more arb trades
        "min": 0.005,
        "max": 0.050,
        "step": 0.001,
        "unit": "ratio",
        "category": "arb",
        "impact": "At 1.5% spread with 1.8% total fees → marginal. Set to fees + 0.5% for buffer. Polymarket + Opinion: need ~2.5%.",
        "widget": "slider",
    },
    {
        "key": "arb_max_position",
        "label": "Arb Max Position",
        "description": "Maximum USD to commit per arbitrage leg. Arb trades are two-sided — total exposure is 2× this value.",
        "type": "number",
        "default": 100.0,
        "min": 10.0,
        "max": 5000.0,
        "step": 10.0,
        "unit": "USD",
        "category": "arb",
        "impact": "Controls max arb book. At $100, total exposure per arb = $200 (both legs).",
        "widget": "slider",
    },
    {
        "key": "arb_max_execution_ms",
        "label": "Arb Execution Timeout",
        "description": "Maximum milliseconds to execute both legs of an arb. If the second leg takes longer, the trade is abandoned.",
        "type": "number",
        "default": 500,
        "min": 100,
        "max": 2000,
        "step": 50,
        "unit": "ms",
        "category": "arb",
        "impact": "500ms is generous. In fast-moving markets, spread closes in <100ms. Lower = safer, fewer fills.",
        "widget": "slider",
    },
    {
        "key": "enable_arb_strategy",
        "label": "Enable Arbitrage Strategy",
        "description": "Master switch for the cross-venue arbitrage strategy. When off, no arb trades execute regardless of signals.",
        "type": "boolean",
        "default": True,
        "category": "arb",
        "impact": "Disabling saves fees on marginal arbs during low-spread periods.",
        "widget": "toggle",
    },
    # ── Cascade Strategy ──────────────────────────────────────────────────────
    {
        "key": "cascade_cooldown_seconds",
        "label": "Cascade Cooldown",
        "description": "Minimum seconds between cascade bets. Prevents doubling into a continuing liquidation cascade.",
        "type": "number",
        "default": 900,
        "min": 60,
        "max": 3600,
        "step": 60,
        "unit": "seconds",
        "category": "cascade",
        "impact": "900s = 15min cooldown. In a 1-hour cascade, max 4 bets. Longer = safer, fewer trades.",
        "widget": "slider",
    },
    {
        "key": "cascade_min_liq_usd",
        "label": "Cascade Min Liquidation Volume",
        "description": "Minimum USD in liquidations detected before triggering a cascade bet. Filters out noise.",
        "type": "number",
        "default": 5000000,
        "min": 100000,
        "max": 100000000,
        "step": 500000,
        "unit": "USD",
        "category": "cascade",
        "impact": "$5M threshold catches major liquidation events (~2–5 per week). Lower = more signals but more false positives.",
        "widget": "slider",
    },
    {
        "key": "enable_cascade_strategy",
        "label": "Enable Cascade Strategy",
        "description": "Master switch for the liquidation cascade detection strategy. When off, no cascade trades execute.",
        "type": "boolean",
        "default": True,
        "category": "cascade",
        "impact": "Safe to disable during trending markets where cascades are more frequent and less predictive.",
        "widget": "toggle",
    },
    # ── Fees & Venues ─────────────────────────────────────────────────────────
    {
        "key": "polymarket_fee_mult",
        "label": "Polymarket Fee Multiplier",
        "description": "Effective fee rate on Polymarket. 0.072 = 7.2% round-trip (maker + taker + spread). Read-only — set by the platform.",
        "type": "number",
        "default": 0.072,
        "min": 0.072,
        "max": 0.072,
        "step": 0.001,
        "unit": "%",
        "category": "fees",
        "impact": "7.2% total cost per round-trip on Polymarket. A $10 trade costs $0.72 in fees.",
        "widget": "readonly",
    },
    {
        "key": "opinion_fee_mult",
        "label": "Opinion Market Fee Multiplier",
        "description": "Effective fee rate on Opinion Markets. 0.04 = 4% round-trip. Significantly cheaper than Polymarket.",
        "type": "number",
        "default": 0.04,
        "min": 0.04,
        "max": 0.04,
        "step": 0.001,
        "unit": "%",
        "category": "fees",
        "impact": "4% total cost per round-trip on Opinion. A $10 trade costs $0.40. 1.8× cheaper than Polymarket.",
        "widget": "readonly",
    },
    {
        "key": "preferred_venue",
        "label": "Preferred Venue",
        "description": "Default venue when spread is equal on both platforms. Opinion is preferred by default due to lower fees.",
        "type": "string",
        "default": "opinion",
        "options": ["opinion", "polymarket"],
        "category": "fees",
        "impact": "At identical spreads, routing to Opinion saves 3.2% per round-trip vs Polymarket.",
        "widget": "venue_select",
    },
]

DEFAULT_CONFIG_VALUES: dict[str, Any] = {
    item["key"]: item["default"] for item in CONFIG_DEFAULTS
}


# ─── Pydantic models ──────────────────────────────────────────────────────────

class CreateConfigRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=lambda: DEFAULT_CONFIG_VALUES.copy())
    mode: str = Field(default="paper", pattern="^(paper|live)$")


class UpdateConfigRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = None
    config: Optional[dict[str, Any]] = None


class ApproveConfigRequest(BaseModel):
    password: str


class ToggleModeRequest(BaseModel):
    enabled: bool
    mode: str = Field(..., pattern="^(paper|live)$")
    confirmation: Optional[str] = None  # Required "CONFIRM" string for live enable


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _verify_password(plain: str) -> bool:
    """Check against the TRADING_APPROVAL_PASSWORD env var (hashed or plain)."""
    expected = os.environ.get("TRADING_APPROVAL_PASSWORD", "")
    if not expected:
        # No password set — approval disabled in dev
        return True
    # Support both plain and sha256-hashed stored passwords
    if expected.startswith("sha256:"):
        hashed = hashlib.sha256(plain.encode()).hexdigest()
        return f"sha256:{hashed}" == expected
    return plain == expected


def _row_to_dict(row) -> dict:
    if row is None:
        return None
    return dict(row)


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/defaults")
async def get_defaults():
    """Return the full config variable schema with defaults, ranges, and descriptions."""
    return {
        "defaults": CONFIG_DEFAULTS,
        "categories": {
            "risk": {"label": "Risk Management", "icon": "🛡️", "description": "Position sizing, drawdown protection, and daily limits"},
            "vpin": {"label": "VPIN Signals", "icon": "📡", "description": "Volume-synchronised probability of informed trading parameters"},
            "arb": {"label": "Arbitrage Strategy", "icon": "⚡", "description": "Cross-venue arbitrage detection and execution settings"},
            "cascade": {"label": "Cascade Strategy", "icon": "🌊", "description": "Liquidation cascade detection and bet timing"},
            "fees": {"label": "Fees & Venues", "icon": "💸", "description": "Fee multipliers and venue routing preferences"},
        },
    }


@router.get("/list")
async def list_configs(mode: Optional[str] = None, db=Depends(get_db)):
    """List all trading configs, optionally filtered by mode (paper|live)."""
    if mode:
        rows = await db.fetch(
            """
            SELECT id, name, version, description, mode, is_active, is_approved,
                   approved_at, approved_by, parent_id, created_at, updated_at
            FROM trading_configs
            WHERE mode = $1
            ORDER BY updated_at DESC
            """,
            mode,
        )
    else:
        rows = await db.fetch(
            """
            SELECT id, name, version, description, mode, is_active, is_approved,
                   approved_at, approved_by, parent_id, created_at, updated_at
            FROM trading_configs
            ORDER BY updated_at DESC
            """
        )
    return {"configs": [dict(r) for r in rows]}


@router.get("/active/{mode}")
async def get_active_config(mode: str, db=Depends(get_db)):
    """Get the currently active config for a given mode (paper|live)."""
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")

    row = await db.fetchrow(
        """
        SELECT * FROM trading_configs
        WHERE mode = $1 AND is_active = TRUE
        LIMIT 1
        """,
        mode,
    )
    if not row:
        return {"config": None}
    return {"config": dict(row)}


@router.get("/live-status")
async def get_live_status(db=Depends(get_db)):
    """
    Returns current status of both paper and live engines.
    {
      paper_enabled, live_enabled,
      active_paper_config, active_live_config,
      live_has_approved_config, can_go_live
    }
    """
    state_row = await db.fetchrow("SELECT * FROM system_state WHERE id = 1")
    state = dict(state_row) if state_row else {}

    paper_enabled = state.get("paper_enabled", True)
    live_enabled = state.get("live_enabled", False)
    paper_config_id = state.get("active_paper_config_id")
    live_config_id = state.get("active_live_config_id")

    # Check live config approval
    live_config = None
    if live_config_id:
        row = await db.fetchrow(
            "SELECT * FROM trading_configs WHERE id = $1", live_config_id
        )
        if row:
            live_config = dict(row)

    paper_config = None
    if paper_config_id:
        row = await db.fetchrow(
            "SELECT * FROM trading_configs WHERE id = $1", paper_config_id
        )
        if row:
            paper_config = dict(row)

    has_approved_config = live_config is not None and live_config.get("is_approved", False)

    # Check API keys (look for them in setup/env)
    api_keys_configured = bool(
        os.environ.get("POLYMARKET_API_KEY") or os.environ.get("OPINION_API_KEY")
    )

    can_go_live = has_approved_config and api_keys_configured

    return {
        "paper_enabled": paper_enabled,
        "live_enabled": live_enabled,
        "active_paper_config": paper_config,
        "active_live_config": live_config,
        "live_has_approved_config": has_approved_config,
        "api_keys_configured": api_keys_configured,
        "can_go_live": can_go_live,
    }


@router.post("/toggle-mode")
async def toggle_mode(req: ToggleModeRequest, db=Depends(get_db)):
    """
    Enable or disable paper or live trading independently.
    Both modes can be on simultaneously.
    Enabling live requires confirmation='CONFIRM' string.
    """
    if req.mode == "live" and req.enabled:
        # Require explicit confirmation string
        if req.confirmation != "CONFIRM":
            raise HTTPException(
                status_code=400,
                detail="Must pass confirmation='CONFIRM' to enable live trading",
            )
        # Verify there's an approved live config
        approved = await db.fetchrow(
            """
            SELECT id FROM trading_configs
            WHERE mode = 'live' AND is_approved = TRUE AND is_active = TRUE
            LIMIT 1
            """
        )
        if not approved:
            raise HTTPException(
                status_code=400,
                detail="No approved live config is active. Approve a config first.",
            )

    col = "paper_enabled" if req.mode == "paper" else "live_enabled"
    await db.execute(
        f"""
        UPDATE system_state
        SET {col} = $1, updated_at = NOW()
        WHERE id = 1
        """,
        req.enabled,
    )

    log.info("trading.mode.toggled", mode=req.mode, enabled=req.enabled)
    return {"ok": True, "mode": req.mode, "enabled": req.enabled}


@router.get("/{config_id}")
async def get_config(config_id: int, db=Depends(get_db)):
    """Get a single config with full details including version history."""
    row = await db.fetchrow("SELECT * FROM trading_configs WHERE id = $1", config_id)
    if not row:
        raise HTTPException(status_code=404, detail="Config not found")

    result = dict(row)

    # Fetch version chain (walk up parent_id)
    history = []
    parent_id = result.get("parent_id")
    while parent_id:
        parent_row = await db.fetchrow(
            "SELECT id, name, version, created_at, updated_at FROM trading_configs WHERE id = $1",
            parent_id,
        )
        if not parent_row:
            break
        history.append(dict(parent_row))
        parent_id = dict(parent_row).get("parent_id")

    result["version_history"] = history
    return {"config": result}


@router.post("")
async def create_config(req: CreateConfigRequest, db=Depends(get_db)):
    """Create a new trading config."""
    # Merge with defaults to ensure all keys present
    merged = DEFAULT_CONFIG_VALUES.copy()
    merged.update(req.config)

    import json

    row = await db.fetchrow(
        """
        INSERT INTO trading_configs (name, description, config, mode, version)
        VALUES ($1, $2, $3, $4, 1)
        RETURNING *
        """,
        req.name,
        req.description,
        json.dumps(merged),
        req.mode,
    )

    log.info("trading_config.created", name=req.name, mode=req.mode)
    return {"config": dict(row)}


@router.put("/{config_id}")
async def update_config(config_id: int, req: UpdateConfigRequest, db=Depends(get_db)):
    """
    Update a config. Bumps version and saves current as parent.
    Live configs lose approval on update (must re-approve).
    """
    import json

    existing = await db.fetchrow(
        "SELECT * FROM trading_configs WHERE id = $1", config_id
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Config not found")

    existing = dict(existing)
    new_version = existing["version"] + 1

    # Build new config
    new_config = existing["config"] if existing["config"] else {}
    if req.config:
        new_config.update(req.config)

    # Insert new version (old id becomes parent)
    new_row = await db.fetchrow(
        """
        INSERT INTO trading_configs
            (name, description, config, mode, version, is_active, is_approved, parent_id)
        VALUES ($1, $2, $3, $4, $5, $6, FALSE, $7)
        RETURNING *
        """,
        req.name or existing["name"],
        req.description if req.description is not None else existing["description"],
        json.dumps(new_config),
        existing["mode"],
        new_version,
        existing["is_active"],
        config_id,  # old becomes parent
    )

    # Deactivate old version
    await db.execute(
        "UPDATE trading_configs SET is_active = FALSE WHERE id = $1", config_id
    )

    log.info("trading_config.updated", old_id=config_id, new_id=new_row["id"], version=new_version)
    return {"config": dict(new_row)}


@router.post("/{config_id}/clone")
async def clone_config(config_id: int, db=Depends(get_db)):
    """
    Clone a config — useful for promoting a paper config to live.
    The clone gets mode='live', is_approved=False, version=1.
    """
    import json

    existing = await db.fetchrow(
        "SELECT * FROM trading_configs WHERE id = $1", config_id
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Config not found")

    existing = dict(existing)
    new_mode = "live" if existing["mode"] == "paper" else "paper"
    new_name = f"{existing['name']} ({new_mode.upper()})"

    row = await db.fetchrow(
        """
        INSERT INTO trading_configs (name, description, config, mode, version, parent_id)
        VALUES ($1, $2, $3, $4, 1, $5)
        RETURNING *
        """,
        new_name,
        existing["description"],
        json.dumps(existing["config"]),
        new_mode,
        config_id,
    )

    log.info("trading_config.cloned", source_id=config_id, new_id=row["id"], new_mode=new_mode)
    return {"config": dict(row)}


@router.post("/{config_id}/activate")
async def activate_config(config_id: int, db=Depends(get_db)):
    """
    Set this config as the active config for its mode.
    Deactivates any other active config for the same mode.
    Also updates system_state.active_{mode}_config_id.
    """
    row = await db.fetchrow(
        "SELECT * FROM trading_configs WHERE id = $1", config_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Config not found")

    config = dict(row)
    mode = config["mode"]

    if mode == "live" and not config["is_approved"]:
        raise HTTPException(
            status_code=400,
            detail="Live configs must be approved before activation",
        )

    async with db.transaction():
        # Deactivate other configs of same mode
        await db.execute(
            "UPDATE trading_configs SET is_active = FALSE WHERE mode = $1 AND id != $2",
            mode,
            config_id,
        )
        # Activate this one
        await db.execute(
            "UPDATE trading_configs SET is_active = TRUE, updated_at = NOW() WHERE id = $1",
            config_id,
        )
        # Update system_state
        col = "active_paper_config_id" if mode == "paper" else "active_live_config_id"
        await db.execute(
            f"UPDATE system_state SET {col} = $1, updated_at = NOW() WHERE id = 1",
            config_id,
        )

    log.info("trading_config.activated", config_id=config_id, mode=mode)
    return {"ok": True, "config_id": config_id, "mode": mode}


@router.post("/{config_id}/approve")
async def approve_config(config_id: int, req: ApproveConfigRequest, db=Depends(get_db)):
    """
    Approve a config for live trading. Requires password verification.
    Only live-mode configs can be approved.
    """
    row = await db.fetchrow(
        "SELECT * FROM trading_configs WHERE id = $1", config_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Config not found")

    config = dict(row)
    if config["mode"] != "live":
        raise HTTPException(
            status_code=400,
            detail="Only live-mode configs can be approved",
        )

    if not _verify_password(req.password):
        raise HTTPException(
            status_code=403,
            detail="Invalid approval password",
        )

    await db.execute(
        """
        UPDATE trading_configs
        SET is_approved = TRUE,
            approved_at = NOW(),
            approved_by = 'admin',
            updated_at = NOW()
        WHERE id = $1
        """,
        config_id,
    )

    log.info("trading_config.approved", config_id=config_id)
    return {"ok": True, "config_id": config_id, "approved": True}


@router.delete("/{config_id}")
async def delete_config(config_id: int, db=Depends(get_db)):
    """Soft-delete: deactivate and unapprove config."""
    row = await db.fetchrow(
        "SELECT id, is_active FROM trading_configs WHERE id = $1", config_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Config not found")

    await db.execute(
        """
        UPDATE trading_configs
        SET is_active = FALSE, is_approved = FALSE, updated_at = NOW()
        WHERE id = $1
        """,
        config_id,
    )

    log.info("trading_config.deleted", config_id=config_id)
    return {"ok": True, "config_id": config_id}
