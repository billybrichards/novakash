"""Strategy Registry -- loads YAML configs, builds gate pipelines, evaluates.

Config-first strategy system. Each strategy defined in YAML with optional
custom Python hooks. No inheritance chain.

Audit: CA-07.
"""

from __future__ import annotations

import importlib.util
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import structlog
import yaml

from alerts.haiku_summarizer import HaikuSummarizer
from domain.decision_metadata import DecisionMetadata
from domain.value_objects import GateCheckTrace, StrategyDecision, WindowEvaluationTrace
from strategies import gate_params as _gate_params
from strategies.data_surface import DataSurfaceManager, FullDataSurface
from strategies.gates.base import Gate, GateResult

log = structlog.get_logger(__name__)

# Gate type -> class mapping
_GATE_REGISTRY: dict[str, type] = {}


def _register_gates() -> None:
    """Populate _GATE_REGISTRY from the gates package."""
    if _GATE_REGISTRY:
        return
    from strategies.gates.timing import TimingGate
    from strategies.gates.direction import DirectionGate
    from strategies.gates.confidence import ConfidenceGate
    from strategies.gates.session_hours import SessionHoursGate
    from strategies.gates.clob_sizing import CLOBSizingGate
    from strategies.gates.source_agreement import SourceAgreementGate
    from strategies.gates.delta_magnitude import DeltaMagnitudeGate
    from strategies.gates.taker_flow import TakerFlowGate
    from strategies.gates.cg_confirmation import CGConfirmationGate
    from strategies.gates.spread import SpreadGate
    from strategies.gates.dynamic_cap import DynamicCapGate
    from strategies.gates.regime import RegimeGate
    from strategies.gates.macro_direction import MacroDirectionGate
    from strategies.gates.trade_advised import TradeAdvisedGate
    from strategies.gates.entry_price_floor import EntryPriceFloorGate

    _GATE_REGISTRY.update(
        {
            "timing": TimingGate,
            "direction": DirectionGate,
            "confidence": ConfidenceGate,
            "session_hours": SessionHoursGate,
            "clob_sizing": CLOBSizingGate,
            "source_agreement": SourceAgreementGate,
            "delta_magnitude": DeltaMagnitudeGate,
            "taker_flow": TakerFlowGate,
            "cg_confirmation": CGConfirmationGate,
            "spread": SpreadGate,
            "dynamic_cap": DynamicCapGate,
            "regime": RegimeGate,
            "macro_direction": MacroDirectionGate,
            "trade_advised": TradeAdvisedGate,
            "entry_price_floor": EntryPriceFloorGate,
        }
    )


@dataclass
class StrategyConfig:
    """Parsed strategy YAML configuration."""

    name: str
    version: str
    mode: str  # LIVE | GHOST | DISABLED
    asset: str
    timescale: str
    gates: list[dict]
    sizing: dict
    hooks_file: Optional[str] = None
    pre_gate_hook: Optional[str] = None
    post_gate_hook: Optional[str] = None
    # Per-strategy tuning knobs for hook code. Consumed via
    # strategies.gate_params.get_*() inside the hook; YAML value wins over
    # the legacy env fallback. Empty dict = pure env-only behaviour
    # (pre-migration default — v4/v5 hooks still work unchanged).
    gate_params: dict = field(default_factory=dict)


@dataclass
class SizingResult:
    """Position sizing output from the registry."""

    fraction: float = 0.025
    max_collateral_pct: float = 0.10
    entry_cap: Optional[float] = None
    size_modifier: float = 1.0
    label: str = "default"


class StrategyRegistry:
    """Loads strategy configs, builds pipelines, evaluates all strategies.

    Each strategy has:
    - A YAML config defining its gate pipeline
    - Optional Python hooks for custom logic
    - A documentation .md file (not loaded, for humans)

    When execute_trade_uc is provided, LIVE strategies with action=TRADE
    will be executed automatically after evaluation.
    """

    def __init__(
        self,
        config_dir: str,
        data_surface: DataSurfaceManager,
        execute_trade_uc: Any = None,
        alerter: Any = None,
        decision_repo: Any = None,
        trace_repo: Any = None,
        db: Any = None,
    ):
        self._config_dir = Path(config_dir)
        self._data_surface = data_surface
        self._execute_uc = execute_trade_uc
        self._alerter = alerter
        self._decision_repo = decision_repo  # PgStrategyDecisionRepository
        self._trace_repo = trace_repo
        # v4.4.0: optional db handle — lets registry upsert v3/v4 surface
        # fields onto window_snapshots rows so analysts can query the
        # denormalised columns without JSONB extraction from
        # window_evaluation_traces.surface_json. Stays None when not wired
        # (tests, legacy composition paths) — writer is a no-op then.
        self._db = db
        self._configs: dict[str, StrategyConfig] = {}
        self._pipelines: dict[str, list[Gate]] = {}
        self._hooks: dict[str, dict[str, Callable]] = {}
        # Track last window_ts to send summary once per window at final offset
        self._last_summary_window: int = 0
        # In-memory dedup: strategy_id -> last window_ts that was executed
        # Prevents double-execution when WindowStateRepository is unavailable
        self._executed_windows: dict[str, int] = {}
        # PR 4: per-(strategy, window_ts, outcome) TG card emission dedup.
        # When a trade retries every 2s for a 5m window, we'd otherwise
        # flood TG with 30+ identical FAILED_EXECUTION cards. Cap at
        # FAILED_EXECUTION_CARD_CAP per (strategy, window, outcome) tuple;
        # extra attempts still log to DB via gate_check_traces, they just
        # don't spam TG. LRU-bounded so long-running engines don't leak.
        self._attempt_card_counts: dict[tuple[str, int, str], int] = {}
        self._attempt_card_cap: int = 2
        # Haiku summarizer for human-readable Telegram messages
        self._haiku = HaikuSummarizer()
        # Clean-arch: BuildWindowSummaryUseCase produces a
        # WindowSummaryContext VO from the current decisions + prior
        # trades; the adapter formatter renders it to Telegram text.
        # Keeps grouping logic pure and testable (PR C).
        from use_cases.build_window_summary import BuildWindowSummaryUseCase

        self._build_summary_uc = BuildWindowSummaryUseCase()

    def load_all(self) -> None:
        """Scan config_dir for *.yaml, build pipelines, load hooks."""
        _register_gates()

        # Keep the raw YAML source text per-strategy so seed_registry_to_db()
        # can persist it verbatim without re-reading files — preserves comments
        # and formatting in the audit-trail TEXT column.
        self._raw_yaml: dict[str, str] = {}

        for yaml_file in sorted(self._config_dir.glob("*.yaml")):
            try:
                config = self._parse_yaml(yaml_file)
                gates = self._build_pipeline(config)
                hooks = self._load_hooks(config) if config.hooks_file else {}
                self._configs[config.name] = config
                self._pipelines[config.name] = gates
                self._hooks[config.name] = hooks
                try:
                    self._raw_yaml[config.name] = yaml_file.read_text()
                except Exception:
                    # Non-fatal: DB seed falls back to re-serialising the
                    # parsed StrategyConfig if raw text is unavailable.
                    pass
                log.info(
                    "registry.loaded",
                    strategy=config.name,
                    version=config.version,
                    mode=config.mode,
                    gates=len(gates),
                    hooks=list(hooks.keys()),
                )
            except Exception as exc:
                log.error(
                    "registry.load_error",
                    file=str(yaml_file),
                    error=str(exc)[:200],
                )

    async def seed_registry_to_db(self) -> None:
        """Upsert every loaded strategy into the `strategy_configs` table.

        Runs after ``load_all()``. Idempotent — the ``(strategy_id, version)``
        composite PK means re-seeding the same shipping version is a no-op
        beyond bumping ``updated_at``. Bumping the YAML ``version`` inserts
        a new row without touching the old one, giving us free history.

        This is the Phase-2 (Option C.1) companion to the filesystem rsync
        fix (Option A, PR #253). Hub reads the resulting rows via
        ``/api/strategies``; the filesystem resolver stays as a fallback
        so a fresh cluster with an engine that has not yet booted still
        serves a usable (stale) registry rather than 500-ing.

        Governance: this write path is engine-only. The hub has SELECT-only
        access to the table. Keeps the "engine owns strategy catalog"
        invariant clean and avoids creating an accidental auto-promotion
        surface in the UI (see feedback_no_auto_model_promotion.md).

        No-op when ``self._db`` is None (tests, legacy composition paths)
        or when the pool is unavailable.
        """
        import json

        pool = None
        if self._db is not None:
            pool = getattr(self._db, "_pool", None) or getattr(self._db, "pool", None)
        if pool is None:
            log.debug("registry.seed_skipped", reason="no_db_pool")
            return

        if not self._configs:
            log.debug("registry.seed_skipped", reason="no_configs_loaded")
            return

        upserted = 0
        try:
            async with pool.acquire() as conn:
                for name, cfg in self._configs.items():
                    raw_yaml = self._raw_yaml.get(name)
                    if raw_yaml is None:
                        # Fallback: re-serialise from the parsed config.
                        raw_yaml = yaml.safe_dump(
                            {
                                "name": cfg.name,
                                "version": cfg.version,
                                "mode": cfg.mode,
                                "asset": cfg.asset,
                                "timescale": cfg.timescale,
                                "gates": cfg.gates,
                                "sizing": cfg.sizing,
                                "hooks_file": cfg.hooks_file,
                                "pre_gate_hook": cfg.pre_gate_hook,
                                "post_gate_hook": cfg.post_gate_hook,
                            },
                            sort_keys=False,
                        )
                    await conn.execute(
                        """
                        INSERT INTO strategy_configs (
                            strategy_id, version, mode, asset, timescale,
                            config_yaml, gates_json, sizing_json, hooks_file
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9)
                        ON CONFLICT (strategy_id, version) DO UPDATE SET
                            mode        = EXCLUDED.mode,
                            asset       = EXCLUDED.asset,
                            timescale   = EXCLUDED.timescale,
                            config_yaml = EXCLUDED.config_yaml,
                            gates_json  = EXCLUDED.gates_json,
                            sizing_json = EXCLUDED.sizing_json,
                            hooks_file  = EXCLUDED.hooks_file,
                            updated_at  = NOW()
                        """,
                        cfg.name,
                        cfg.version,
                        cfg.mode,
                        cfg.asset,
                        cfg.timescale,
                        raw_yaml,
                        json.dumps(cfg.gates) if cfg.gates is not None else None,
                        json.dumps(cfg.sizing) if cfg.sizing else None,
                        cfg.hooks_file,
                    )
                    upserted += 1
            log.info("registry.seeded_to_db", count=upserted)
        except Exception as exc:
            # Seed failure must not block engine startup — hub falls back
            # to the filesystem resolver. Log loudly; operator debug via
            # the warning log line.
            log.warning("registry.seed_error", error=str(exc)[:300])

    def _parse_yaml(self, path: Path) -> StrategyConfig:
        """Parse a YAML strategy config file."""
        with open(path) as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError(f"Empty or invalid YAML: {path}")
        for required in ("name", "version"):
            if not data.get(required):
                raise ValueError(f"Missing required field '{required}' in {path}")

        raw_gate_params = data.get("gate_params") or {}
        if not isinstance(raw_gate_params, dict):
            raise ValueError(
                f"'gate_params' in {path} must be a mapping, got "
                f"{type(raw_gate_params).__name__}"
            )
        return StrategyConfig(
            name=data["name"],
            version=data["version"],
            mode=data.get("mode", "GHOST"),
            asset=data.get("asset", "BTC"),
            timescale=data.get("timescale", "5m"),
            gates=data.get("gates", []),
            sizing=data.get("sizing", {"type": "fixed_kelly", "fraction": 0.025}),
            hooks_file=data.get("hooks_file"),
            pre_gate_hook=data.get("pre_gate_hook"),
            post_gate_hook=data.get("post_gate_hook"),
            gate_params=dict(raw_gate_params),
        )

    def _build_pipeline(self, config: StrategyConfig) -> list[Gate]:
        """Build a gate pipeline from the config's gate list."""
        pipeline = []
        for gate_def in config.gates:
            gate_type = gate_def["type"]
            params = gate_def.get("params", {})

            gate_cls = _GATE_REGISTRY.get(gate_type)
            if gate_cls is None:
                raise ValueError(
                    f"Unknown gate type '{gate_type}' in strategy '{config.name}'"
                )

            gate = gate_cls(**params)
            pipeline.append(gate)

        return pipeline

    def _load_hooks(self, config: StrategyConfig) -> dict[str, Callable]:
        """Load Python hooks from the strategy's .py file.

        Path is sandboxed to config_dir — no directory traversal allowed.
        """
        hooks_path = (self._config_dir / config.hooks_file).resolve()
        config_root = self._config_dir.resolve()
        if not hooks_path.is_relative_to(config_root):
            raise ValueError(
                f"hooks_file '{config.hooks_file}' escapes config dir "
                f"(resolved to {hooks_path}, must be under {config_root})"
            )
        if not hooks_path.exists():
            log.warning("registry.hooks_missing", file=str(hooks_path))
            return {}

        spec = importlib.util.spec_from_file_location(
            f"strategy_hooks.{config.name}", hooks_path
        )
        if spec is None or spec.loader is None:
            return {}

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        hooks: dict[str, Callable] = {}
        # Collect all callable attributes as potential hooks
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if callable(attr) and not attr_name.startswith("_"):
                hooks[attr_name] = attr

        return hooks

    @property
    def strategy_names(self) -> list[str]:
        """Return all registered strategy names."""
        return list(self._configs.keys())

    @property
    def configs(self) -> dict[str, StrategyConfig]:
        """Return all strategy configs."""
        return dict(self._configs)

    async def evaluate_all(
        self,
        window: Any,
        state: Any,
        *,
        window_market: Any = None,
        current_btc_price: float = 0.0,
        open_price: float = 0.0,
    ) -> list[StrategyDecision]:
        """Evaluate all enabled strategies on the current data surface.

        When execute_trade_uc is wired and a LIVE strategy returns TRADE,
        the trade is executed automatically. GHOST strategies are logged
        but never executed.

        Args:
            window: Window object with asset, window_ts, eval_offset
            state: Market state (for surface construction)
            window_market: WindowMarket with token IDs (for execution)
            current_btc_price: Live BTC price (for execution)
            open_price: Window open price (for execution)
        """
        eval_offset = getattr(window, "eval_offset", None)
        window_ts = getattr(window, "window_ts", 0)
        window_tf = getattr(window, "timeframe", "5m")
        surface = self._data_surface.get_surface(window, eval_offset)

        if self._trace_repo is not None:
            try:
                self._write_window_trace(surface)
            except Exception as exc:
                log.warning("registry.window_trace_error", error=str(exc)[:200])

        decisions = []
        for name, config in self._configs.items():
            if config.mode == "DISABLED":
                continue
            if config.timescale != window_tf:
                continue
            try:
                decision = self._evaluate_one(name, config, surface)
                decisions.append(decision)

                # Write decision to strategy_decisions table (fire-and-forget)
                # Writes EVERY decision (TRADE + SKIP + ERROR) so Command Center
                # and Strategy Lab have full per-strategy per-offset history.
                if self._decision_repo is not None:
                    try:
                        import asyncio, json, time
                        from domain.value_objects import StrategyDecisionRecord

                        record = StrategyDecisionRecord(
                            strategy_id=name,
                            strategy_version=config.version,
                            asset=getattr(window, "asset", "BTC"),
                            window_ts=window_ts,
                            timeframe=getattr(window, "timeframe", "5m")
                            if hasattr(window, "timeframe")
                            else "5m",
                            eval_offset=eval_offset,
                            mode=config.mode,
                            action=decision.action,
                            direction=decision.direction,
                            confidence=decision.confidence,
                            confidence_score=decision.confidence_score,
                            entry_cap=decision.entry_cap,
                            collateral_pct=decision.collateral_pct,
                            entry_reason=decision.entry_reason,
                            skip_reason=decision.skip_reason,
                            metadata_json=json.dumps(decision.metadata or {}),
                            evaluated_at=time.time(),
                        )

                        def _on_write_error(task, _n=name):
                            if not task.cancelled() and task.exception():
                                log.warning(
                                    "registry.decision_write_error",
                                    strategy=_n,
                                    error=str(task.exception())[:200],
                                )

                        t = asyncio.create_task(
                            self._decision_repo.write_decision(record)
                        )
                        t.add_done_callback(_on_write_error)
                    except Exception as _e:
                        log.warning(
                            "registry.decision_record_error", error=str(_e)[:200]
                        )

                if self._trace_repo is not None:
                    try:
                        self._write_gate_traces(
                            config=config,
                            surface=surface,
                            decision=decision,
                        )
                    except Exception as _e:
                        log.warning(
                            "registry.gate_trace_error",
                            strategy=name,
                            error=str(_e)[:200],
                        )

                # Execute LIVE trades when use case is wired
                # In-memory dedup: only execute once per window per strategy
                _already_executed = self._executed_windows.get(name) == window_ts
                if (
                    config.mode == "LIVE"
                    and decision.action == "TRADE"
                    and self._execute_uc is not None
                    and window_market is not None
                    and not _already_executed
                ):
                    try:
                        result = await self._execute_uc.execute(
                            decision=decision,
                            window_market=window_market,
                            current_btc_price=current_btc_price,
                            open_price=open_price,
                        )
                        # Only dedup successful executions. A failed/no-fill
                        # attempt should be allowed to retry at a later eval
                        # offset within the same window.
                        if result.success:
                            self._executed_windows[name] = window_ts
                        log.info(
                            "registry.executed",
                            strategy=name,
                            success=result.success,
                            order_id=result.order_id,
                            fill_price=result.fill_price,
                            mode=result.execution_mode,
                        )
                        # Per-strategy trade-attempt card (T-0 surface).
                        # FILLED / FAILED_EXECUTION branch — we attempted,
                        # so operator always sees the outcome. Skip-path
                        # cards are emitted further down at final eval.
                        await self._fire_trade_attempt_card(
                            strategy=name,
                            window_ts=window_ts,
                            decision=decision,
                            execution_result=result,
                            timeframe=getattr(window, "timeframe", "5m"),
                        )
                    except Exception as exec_exc:
                        log.error(
                            "registry.execute_error",
                            strategy=name,
                            error=str(exec_exc)[:200],
                        )
                        # Execute path raised — fire FAILED_EXECUTION card
                        # so the silent exception doesn't go unnoticed.
                        await self._fire_trade_attempt_card(
                            strategy=name,
                            window_ts=window_ts,
                            decision=decision,
                            execution_result=None,
                            exec_error=str(exec_exc)[:200],
                            timeframe=getattr(window, "timeframe", "5m"),
                        )
                elif config.mode == "GHOST" and decision.action == "TRADE":
                    log.info(
                        "registry.ghost_decision",
                        strategy=name,
                        action=decision.action,
                        direction=decision.direction,
                    )

            except Exception as exc:
                log.warning(
                    "registry.evaluate_error",
                    strategy=name,
                    error=str(exc)[:200],
                )
                decisions.append(
                    StrategyDecision.error(
                        reason=f"registry_error: {str(exc)[:200]}",
                        strategy_id=name,
                        strategy_version=config.version,
                    )
                )
        # Send per-window summary at final eval offset
        # 5m windows: T-60 (eval_offset <= 62)
        # 15m windows: T-270 (eval_offset <= 280, first eval in trade window)
        window_ts = getattr(window, "window_ts", 0)
        eval_offset_val = getattr(window, "eval_offset", None)
        window_tf = getattr(window, "timeframe", "5m")
        summary_threshold = 280 if window_tf == "15m" else 62
        if (
            decisions
            and eval_offset_val is not None
            and eval_offset_val <= summary_threshold
            and window_ts != self._last_summary_window
            and self._alerter is not None
        ):
            self._last_summary_window = window_ts
            try:
                import asyncio

                asyncio.create_task(
                    self._send_window_summary(
                        window_ts, eval_offset_val, decisions, surface
                    )
                )
            except Exception:
                pass

            # Per-strategy skip cards at final eval offset — one per LIVE
            # strategy that didn't already attempt execution. The
            # FILLED / FAILED_EXECUTION path above handles trade attempts;
            # here we emit SKIPPED_* cards so the operator sees why each
            # LIVE strategy passed on this window.
            for dec in decisions:
                config = self._configs.get(dec.strategy_id)
                if config is None or config.mode != "LIVE":
                    continue
                if dec.action == "TRADE":
                    # Already emitted FILLED / FAILED_EXECUTION above.
                    continue
                try:
                    await self._fire_trade_attempt_card(
                        strategy=dec.strategy_id,
                        window_ts=window_ts,
                        decision=dec,
                        execution_result=None,
                        timeframe=window_tf,
                    )
                except Exception as exc:
                    log.debug(
                        "registry.skip_card_error",
                        strategy=dec.strategy_id,
                        error=str(exc)[:200],
                    )

        return decisions

    # ── Skip-reason → attempt-card outcome classifier ─────────────────
    # Maps raw ``skip_reason`` strings produced by gates/hooks into the
    # stable outcome enum used by TelegramAlerter.send_trade_attempt_result.
    # Anything not matched falls through to "SKIPPED_NO_EDGE" so the card
    # still fires (never drop silently).
    _SKIP_OUTCOME_PATTERNS: tuple[tuple[str, str], ...] = (
        ("cooldown", "SKIPPED_COOLDOWN"),
        ("consensus", "SKIPPED_CONSENSUS"),
        ("sources_agree", "SKIPPED_CONSENSUS"),
        ("risk", "SKIPPED_RISK_GATED"),
        ("kill_switch", "SKIPPED_RISK_GATED"),
        ("daily_loss", "SKIPPED_RISK_GATED"),
        ("exposure", "SKIPPED_RISK_GATED"),
        ("entry_price", "SKIPPED_PRICE_BAND"),
        ("price_floor", "SKIPPED_PRICE_BAND"),
        ("dynamic_cap", "SKIPPED_PRICE_BAND"),
        ("spread", "SKIPPED_PRICE_BAND"),
    )

    @classmethod
    def _classify_skip_outcome(cls, skip_reason: Optional[str]) -> str:
        """Map a decision's skip_reason to an attempt-card outcome label."""
        if not skip_reason:
            return "SKIPPED_NO_EDGE"
        lower = skip_reason.lower()
        for needle, outcome in cls._SKIP_OUTCOME_PATTERNS:
            if needle in lower:
                return outcome
        return "SKIPPED_NO_EDGE"

    async def _fire_trade_attempt_card(
        self,
        *,
        strategy: str,
        window_ts: int,
        decision: StrategyDecision,
        execution_result: Any,
        timeframe: str,
        exec_error: Optional[str] = None,
    ) -> None:
        """Emit a per-strategy trade-attempt card via the alerter.

        Safe no-op when ``self._alerter`` lacks ``send_trade_attempt_result``
        (legacy composition, some tests). Exceptions are logged and
        swallowed — a telemetry failure must never break evaluation.
        """
        if self._alerter is None:
            return
        send = getattr(self._alerter, "send_trade_attempt_result", None)
        if send is None:
            return

        # Classify outcome.
        # PR 4: distinguish execute-trade "already_traded" (window-level
        # dedup kicked in because a sibling strategy won try_claim_trade
        # first) from a real FAILED_EXECUTION. They used to collapse into
        # the same FAILED_EXECUTION card, making sibling-losers look like
        # genuine failures. ExecuteTradeUseCase._failed("already_traded")
        # sets result.failure_reason = "already_traded"; that's our signal.
        if execution_result is not None:
            if getattr(execution_result, "success", False):
                outcome = "FILLED"
            else:
                failure_reason = getattr(execution_result, "failure_reason", "") or ""
                if failure_reason == "already_traded":
                    outcome = "SKIPPED_COOLDOWN"
                else:
                    outcome = "FAILED_EXECUTION"
        elif exec_error is not None:
            outcome = "FAILED_EXECUTION"
        else:
            outcome = self._classify_skip_outcome(decision.skip_reason)

        # PR 4: per-(strategy, window, outcome) TG card cap. Retries at
        # later eval offsets still log full context to DB via
        # gate_check_traces, but we cap TG at _attempt_card_cap per tuple
        # so operator isn't drowned. FILLED always emits (one-shot), and
        # skip-path cards from the final-eval loop are already 1-per-window
        # — the cap matters mainly for FAILED_EXECUTION / SKIPPED_COOLDOWN
        # retries within a window.
        tuple_key = (strategy, int(window_ts or 0), outcome)
        count = self._attempt_card_counts.get(tuple_key, 0) + 1
        self._attempt_card_counts[tuple_key] = count
        if outcome != "FILLED" and count > self._attempt_card_cap:
            log.debug(
                "registry.trade_attempt_card_capped",
                strategy=strategy,
                window_ts=window_ts,
                outcome=outcome,
                count=count,
            )
            return
        # Bound the dict — keep last 512 tuple keys (≈ 256 windows × 2
        # outcomes) so engines running for weeks don't grow unbounded.
        if len(self._attempt_card_counts) > 512:
            oldest = next(iter(self._attempt_card_counts))
            self._attempt_card_counts.pop(oldest, None)

        # Pull blocking gate from metadata if surfaced.
        meta = decision.metadata or {}
        blocking_gate = meta.get("blocking_gate") or meta.get("failed_gate")
        gate_reason = decision.skip_reason if outcome.startswith("SKIPPED_") else None

        side = decision.direction or "?"
        price: Optional[float] = None
        stake: Optional[float] = None
        order_id: Optional[str] = None
        if execution_result is not None:
            price = getattr(execution_result, "fill_price", None)
            stake = getattr(execution_result, "stake_usd", None)
            order_id = getattr(execution_result, "order_id", None)
        if price is None:
            price = decision.entry_cap

        try:
            await send(
                strategy=strategy,
                window_ts=int(window_ts or 0),
                side=side,
                outcome=outcome,
                stake_usd=float(stake) if stake is not None else None,
                price=float(price) if price is not None else None,
                edge_bps=None,
                blocking_gate=blocking_gate,
                gate_reason=gate_reason or (exec_error if outcome == "FAILED_EXECUTION" else None),
                order_id=str(order_id) if order_id else None,
                timeframe=timeframe,
            )
        except Exception as exc:
            log.bind(strategy=strategy, outcome=outcome).warning(
                "registry.trade_attempt_card_failed",
                error=str(exc)[:200],
            )

    def _write_window_trace(self, surface: FullDataSurface) -> None:
        import asyncio

        trace = WindowEvaluationTrace(
            asset=surface.asset,
            window_ts=surface.window_ts,
            timeframe=surface.timescale,
            eval_offset=surface.eval_offset,
            surface_data=self._surface_trace_data(surface),
            assembled_at=surface.assembled_at,
        )
        task = asyncio.create_task(
            self._trace_repo.write_window_evaluation_trace(trace)
        )
        task.add_done_callback(
            self._log_async_write_error("registry.window_trace_write_error")
        )

        # v4.4.0: also upsert denormalised v3/v4 columns into window_snapshots
        # so SQL analysis doesn't need JSONB extraction. Fire-and-forget.
        if self._db is not None and hasattr(
            self._db, "update_window_surface_fields"
        ):
            try:
                from strategies.five_min_vpin import _v34_surface_fields

                fields = _v34_surface_fields(surface)
            except Exception:
                return
            if any(v is not None for v in fields.values()):
                surf_task = asyncio.create_task(
                    self._db.update_window_surface_fields(
                        window_ts=surface.window_ts,
                        asset=surface.asset,
                        timeframe=surface.timescale,
                        eval_offset=surface.eval_offset,
                        surface_fields=fields,
                    )
                )
                surf_task.add_done_callback(
                    self._log_async_write_error(
                        "registry.surface_fields_write_error"
                    )
                )

    def _write_gate_traces(
        self,
        *,
        config: StrategyConfig,
        surface: FullDataSurface,
        decision: StrategyDecision,
    ) -> None:
        import asyncio

        traces = self._build_gate_check_traces(
            config=config,
            surface=surface,
            decision=decision,
        )
        if not traces:
            return
        task = asyncio.create_task(self._trace_repo.write_gate_check_traces(traces))
        task.add_done_callback(
            self._log_async_write_error(
                "registry.gate_trace_write_error", strategy=decision.strategy_id
            )
        )

    def _log_async_write_error(self, event: str, **context: Any) -> Callable:
        def _cb(task) -> None:
            if not task.cancelled() and task.exception():
                log.warning(event, error=str(task.exception())[:200], **context)

        return _cb

    def _build_gate_check_traces(
        self,
        *,
        config: StrategyConfig,
        surface: FullDataSurface,
        decision: StrategyDecision,
    ) -> list[GateCheckTrace]:
        gate_results = (decision.metadata or {}).get("gate_results") or []
        traces: list[GateCheckTrace] = []
        evaluated_at = time.time()

        if gate_results:
            for idx, result in enumerate(gate_results):
                gate_name = str(result.get("gate") or f"gate_{idx}")
                traces.append(
                    GateCheckTrace(
                        asset=surface.asset,
                        window_ts=surface.window_ts,
                        timeframe=surface.timescale,
                        eval_offset=surface.eval_offset,
                        strategy_id=decision.strategy_id,
                        gate_order=idx,
                        gate_name=gate_name,
                        passed=bool(result.get("passed")),
                        mode=config.mode,
                        action=decision.action,
                        direction=decision.direction,
                        reason=str(result.get("reason") or ""),
                        skip_reason=decision.skip_reason,
                        observed_data=self._gate_observed_data(
                            gate_name, surface, decision
                        ),
                        config_data=self._gate_config_data(config, idx, gate_name),
                        evaluated_at=evaluated_at,
                    )
                )
            return traces

        traces.append(
            GateCheckTrace(
                asset=surface.asset,
                window_ts=surface.window_ts,
                timeframe=surface.timescale,
                eval_offset=surface.eval_offset,
                strategy_id=decision.strategy_id,
                gate_order=0,
                gate_name="custom_logic",
                passed=decision.action == "TRADE",
                mode=config.mode,
                action=decision.action,
                direction=decision.direction,
                reason=decision.skip_reason or decision.entry_reason,
                skip_reason=decision.skip_reason,
                observed_data=self._gate_observed_data(
                    "custom_logic", surface, decision
                ),
                config_data={
                    "hook": config.pre_gate_hook or config.post_gate_hook or "custom"
                },
                evaluated_at=evaluated_at,
            )
        )
        return traces

    def _gate_config_data(
        self,
        config: StrategyConfig,
        gate_index: int,
        gate_name: str,
    ) -> dict:
        if gate_index < len(config.gates):
            gate_def = config.gates[gate_index]
            return {
                "type": gate_def.get("type"),
                "params": gate_def.get("params", {}),
            }
        return {"type": gate_name, "params": {}}

    def _surface_trace_data(self, surface: FullDataSurface) -> dict:
        return asdict(surface)

    def _gate_observed_data(
        self,
        gate_name: str,
        surface: FullDataSurface,
        decision: StrategyDecision,
    ) -> dict:
        buy_ratio = None
        if (
            surface.cg_taker_buy_vol is not None
            and surface.cg_taker_sell_vol is not None
        ):
            total = (surface.cg_taker_buy_vol or 0.0) + (
                surface.cg_taker_sell_vol or 0.0
            )
            if total > 0:
                buy_ratio = (surface.cg_taker_buy_vol or 0.0) / total

        observed = {
            "eval_offset": surface.eval_offset,
            "delta_pct": surface.delta_pct,
            "abs_delta_pct": abs(surface.delta_pct or 0.0),
            "vpin": surface.vpin,
            "regime": surface.regime,
            "v2_probability_up": surface.v2_probability_up,
            "poly_direction": surface.poly_direction,
            "poly_confidence_distance": surface.poly_confidence_distance,
            "poly_timing": surface.poly_timing,
            "poly_trade_advised": surface.poly_trade_advised,
            "entry_cap": decision.entry_cap,
            "cg_buy_ratio": buy_ratio,
            "clob_up_ask": surface.clob_up_ask,
            "clob_down_ask": surface.clob_down_ask,
            "clob_implied_up": surface.clob_implied_up,
        }

        if gate_name == "direction":
            observed["actual_direction"] = decision.direction or surface.poly_direction
        elif gate_name == "spread":
            if surface.clob_up_ask is not None and surface.clob_down_ask is not None:
                observed["combined_ask"] = surface.clob_up_ask + surface.clob_down_ask
        elif gate_name == "taker_flow":
            observed["cg_taker_buy_vol"] = surface.cg_taker_buy_vol
            observed["cg_taker_sell_vol"] = surface.cg_taker_sell_vol
        elif gate_name == "confidence":
            observed["confidence_score"] = decision.confidence_score
        return observed

    async def _send_window_summary(
        self,
        window_ts: int,
        eval_offset: int,
        decisions: list[StrategyDecision],
        surface: FullDataSurface,
    ) -> None:
        """Send Haiku-powered window summary to Telegram.

        Builds a context dict from the data surface and strategy decisions,
        then calls HaikuSummarizer for a human-readable AI summary.
        Falls back to template if Haiku API is unavailable.
        """
        try:
            from datetime import datetime, timezone

            window_time = datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime(
                "%H:%M"
            )

            # Compute model direction and confidence distance
            p_up = surface.v2_probability_up
            model_direction = None
            dist = None
            if p_up is not None:
                model_direction = "UP" if p_up > 0.5 else "DOWN"
                dist = abs(p_up - 0.5)

            # Delta as percentage string
            delta_pct = None
            if surface.delta_pct is not None:
                delta_pct = f"{surface.delta_pct * 100:+.2f}"

            # Source deltas
            chainlink_delta = (
                f"{surface.delta_chainlink * 100:+.2f}"
                if surface.delta_chainlink is not None
                else "N/A"
            )
            tiingo_delta = (
                f"{surface.delta_tiingo * 100:+.2f}"
                if surface.delta_tiingo is not None
                else "N/A"
            )

            # Source agreement (CL + Ti only -- matches the strategy gate
            # definition; Binance shown separately as a cross-check).
            sources_agree = self._check_source_agreement(surface)
            binance_cross_check = self._format_binance_cross_check(surface)

            # Prior-offset TRADE decisions for this window. Used by the
            # use case to populate "Already traded this window" and kill
            # the "inactive then LOSS" contradiction. Best-effort — if the
            # repo lookup errors we just skip the earlier-trade surfacing.
            prior: list = []
            if self._decision_repo is not None:
                try:
                    prior = await self._decision_repo.get_decisions_for_window(
                        asset=getattr(surface, "asset", "BTC"),
                        window_ts=window_ts,
                    )
                except Exception as exc:
                    log.debug(
                        "registry.summary_prior_lookup_error",
                        error=str(exc)[:120],
                    )

            # Build structured summary via the use case (pure).
            # Infer timescale from decisions (15m strategies prefix v15m_).
            inferred_tf = (
                "15m"
                if decisions and decisions[0].strategy_id.startswith("v15m")
                else "5m"
            )
            summary_ctx = self._build_summary_uc.execute(
                window_ts=window_ts,
                eval_offset=eval_offset,
                timescale=inferred_tf,
                open_price=getattr(surface, "open_price", None),
                current_price=getattr(surface, "current_price", None),
                sources_agree=sources_agree,
                decisions=decisions,
                configs=self._configs,
                prior_decisions=prior,
            )

            # Render via adapter formatter (deterministic; also becomes
            # the anchor the Haiku path enriches).
            from adapters.alert.window_summary_formatter import (
                format_window_summary,
            )

            rendered_summary = format_window_summary(summary_ctx)
            decision_lines = rendered_summary.split("\n")

            # Haiku context text — concise per-strategy decision list
            # derived from the VO so both the structured and narrative
            # paths agree on what happened.
            decisions_text_parts: list[str] = []
            for r in summary_ctx.eligible:
                decisions_text_parts.append(
                    f"{r.strategy_id} ({r.mode}): TRADING — {r.text}"
                )
            for r in summary_ctx.blocked_signal:
                decisions_text_parts.append(
                    f"{r.strategy_id} ({r.mode}): SKIPPED — {r.text}"
                )
            for r in summary_ctx.blocked_exec_timing:
                decisions_text_parts.append(
                    f"{r.strategy_id} ({r.mode}): EXEC TIMING — {r.text}"
                )
            for r in summary_ctx.off_window:
                decisions_text_parts.append(
                    f"{r.strategy_id} ({r.mode}): OFF-WINDOW — {r.text}"
                )
            for r in summary_ctx.already_traded:
                decisions_text_parts.append(
                    f"{r.strategy_id} ({r.mode}): already {r.text}"
                )
            if summary_ctx.ghost_shadow:
                decisions_text_parts.append(
                    "ghost shadow: "
                    + ", ".join(r.strategy_id for r in summary_ctx.ghost_shadow)
                )

            context = {
                "window_time": window_time,
                "window_ts": window_ts,
                "timescale": summary_ctx.timescale,
                "delta_pct": delta_pct,
                "vpin": surface.vpin,
                "regime": surface.regime,
                "p_up": p_up,
                "model_direction": model_direction,
                "dist": dist,
                "chainlink_delta": chainlink_delta,
                "tiingo_delta": tiingo_delta,
                "binance_cross_check": binance_cross_check,
                "sources_agree": sources_agree,
                # Full surface for Haiku context
                "clob_up_ask": getattr(surface, "clob_up_ask", None),
                "clob_dn_ask": getattr(surface, "clob_dn_ask", None),
                "clob_mid": getattr(surface, "clob_mid", None),
                "trade_advised": getattr(surface, "trade_advised", None),
                "v4_consensus": getattr(surface, "v4_consensus_direction", None),
                "v2_direction": getattr(surface, "v2_direction", None),
                "open_price": getattr(surface, "open_price", None),
                "macro_bias": getattr(surface, "macro_bias", None),
                "decisions_text": "\n".join(decisions_text_parts),
                "decision_lines": decision_lines,
            }

            msg = await self._haiku.summarize_evaluation(context)

            if hasattr(self._alerter, "send_raw_message"):
                await self._alerter.send_raw_message(msg)
            elif hasattr(self._alerter, "send_system_alert"):
                await self._alerter.send_system_alert(msg)

            # Task 7.5: loud STRATEGY MISSED WINDOW alert for any LIVE
            # strategy that was never evaluated in-window. Isolated try
            # so a failure here can't break the summary send above.
            try:
                await self._dispatch_missed_window_alerts(summary_ctx)
            except Exception as exc:
                log.warning(
                    "registry.missed_window_dispatch_error",
                    error=str(exc)[:200],
                )
        except Exception as exc:
            log.warning("registry.summary_alert_error", error=str(exc)[:200])

    async def _dispatch_missed_window_alerts(
        self,
        summary_ctx: "WindowSummaryContext",
    ) -> None:
        """Fire :meth:`TelegramAlerter.send_strategy_missed_window` once per
        LIVE strategy that landed in ``window_expired`` with the explicit
        "never evaluated in-window" body — i.e. zero in-window evaluations.

        Diagnostic context (sibling-eval rate, first-eval offset) is
        computed here from the same VO so the alert can answer
        "engine paused vs strategy-specific config bug" in 5 seconds.

        PAPER strategies are intentionally excluded — they may legitimately
        have eval gaps and must not trigger this loud alert.
        """
        alerter = self._alerter
        if alerter is None or not hasattr(alerter, "send_strategy_missed_window"):
            return

        # Strategies the operator wants alerted on.
        targets = [
            line for line in summary_ctx.window_expired
            if line.mode == "LIVE" and "never evaluated in-window" in (line.text or "")
        ]
        if not targets:
            return

        # Per-bucket distinct LIVE strategy_ids (excluding ghost_shadow and
        # window_expired by design — siblings_evaluated is "did we see any
        # in-window decision?", not "did anything happen at all?").
        evaluated_buckets = (
            summary_ctx.eligible,
            summary_ctx.already_traded,
            summary_ctx.blocked_signal,
            summary_ctx.blocked_exec_timing,
            summary_ctx.off_window,
        )
        evaluated_live_ids: set[str] = {
            line.strategy_id
            for bucket in evaluated_buckets
            for line in bucket
            if line.mode == "LIVE"
        }
        # Total LIVE strategies = evaluated set ∪ window_expired (LIVE only).
        # ghost_shadow stays excluded — siblings_total counts LIVE peers.
        all_live_ids: set[str] = set(evaluated_live_ids) | {
            line.strategy_id
            for line in summary_ctx.window_expired
            if line.mode == "LIVE"
        }

        for line in targets:
            sid = line.strategy_id
            cfg = self._configs.get(sid)
            bounds = self._build_summary_uc._timing_bounds(cfg)
            bounds_str = (
                f"T-{bounds[1]}..T-{bounds[0]}"
                if bounds is not None
                else "unknown"
            )
            siblings_evaluated = len(evaluated_live_ids - {sid})
            siblings_total = len(all_live_ids - {sid})
            try:
                await alerter.send_strategy_missed_window(
                    strategy_id=sid,
                    mode=line.mode,
                    window_ts=summary_ctx.window_ts,
                    bounds_str=bounds_str,
                    siblings_evaluated=siblings_evaluated,
                    siblings_total=siblings_total,
                    first_eval_offset=summary_ctx.eval_offset,
                )
            except Exception as exc:
                log.warning(
                    "registry.missed_window_alert_send_error",
                    strategy_id=sid,
                    error=str(exc)[:200],
                )

    @staticmethod
    def _check_source_agreement(surface: FullDataSurface) -> str:
        """Consensus over the sources the trade gates actually care about.

        Only Chainlink + Tiingo participate -- this matches the gate
        definition in ``engine/strategies/configs/v4_fusion.py``
        (``_sources_agree_surface``) and the documented invariant on
        ``BtcPriceBlock``: Binance aggTrade feeds VPIN but is NOT a
        direction-consensus input.

        Previously this used a 3-way vote including Binance, which
        produced misleading ``NO (mixed)`` output while the gate saw
        ``YES`` -- because the rendered Chainlink/Tiingo deltas in the
        same message both agreed, the Binance sign that flipped the
        verdict was invisible. See the April 18 2026 postmortem.

        Binance cross-check is surfaced separately via
        ``_format_binance_cross_check`` so operators still see the
        third-source sign without it corrupting consensus.
        """
        cl = surface.delta_chainlink
        ti = surface.delta_tiingo
        missing: list[str] = []
        if cl is None:
            missing.append("chainlink")
        if ti is None:
            missing.append("tiingo")
        if missing:
            return f"unknown ({'+'.join(missing)} missing)"
        cl_dir = "UP" if cl > 0 else "DOWN"
        ti_dir = "UP" if ti > 0 else "DOWN"
        if cl_dir == ti_dir:
            return f"YES ({cl_dir})"
        return f"NO (CL={cl_dir}, Ti={ti_dir})"

    @staticmethod
    def _format_binance_cross_check(surface: FullDataSurface) -> str | None:
        """Return a human-readable Binance cross-check string, or None.

        Binance agreement/disagreement with the CL+Ti consensus is
        diagnostic only -- it is NOT part of the consensus used for
        gating. Surfacing it lets operators spot CEX-vs-oracle
        divergence (e.g. Chainlink lag during fast moves) without it
        silently flipping the rendered "sources agree" verdict.
        """
        bi = surface.delta_binance
        if bi is None:
            return None
        sign = "UP" if bi > 0 else "DOWN"
        return f"{bi * 100:+.2f}% ({sign})"

    def wire_execute_uc(self, uc: "ExecuteTradeUseCase") -> None:
        """Inject the ExecuteTradeUseCase after orchestrator startup."""
        self._execute_uc = uc

    def _summarize_window_history(
        self,
        history: list[Any],
        current_decision: StrategyDecision,
    ) -> str:
        if not history:
            return ""

        from collections import Counter
        import re

        trade_offsets = sorted(
            [
                record.eval_offset
                for record in history
                if record.action == "TRADE" and record.eval_offset is not None
            ],
            reverse=True,
        )
        if trade_offsets:
            return f"earlier this window: TRADE at T-{trade_offsets[0]}"

        def _normalize_reason(reason: str) -> str:
            cleaned = (reason or "unknown").strip()
            cleaned = re.sub(r"T-\d+", "T-*", cleaned)
            return cleaned[:70]

        skip_reasons = [
            _normalize_reason(record.skip_reason)
            for record in history
            if record.action == "SKIP" and record.skip_reason
        ]
        if skip_reasons:
            top_reason, top_count = Counter(skip_reasons).most_common(1)[0]
            return (
                f"dominant skip: {top_reason} ({top_count}/{len(skip_reasons)} evals)"
            )

        if current_decision.action == "ERROR":
            return f"{len(history)} evals logged"

        return "no earlier window pattern"

    def _evaluate_one(
        self,
        name: str,
        config: StrategyConfig,
        surface: FullDataSurface,
    ) -> StrategyDecision:
        """Run one strategy's gate pipeline on the surface.

        Per-strategy gate_params (YAML) are bound to the module-level
        contextvar in ``strategies.gate_params`` before any hook runs
        and reset on exit. Hooks read tuning knobs via
        ``gate_params.get_bool/get_float/...`` so each strategy sees its
        own overrides even when sharing hook code.
        """
        token = _gate_params.set_active(config.gate_params)
        try:
            return self._evaluate_one_inner(name, config, surface)
        finally:
            _gate_params.reset_active(token)

    def _evaluate_one_inner(
        self,
        name: str,
        config: StrategyConfig,
        surface: FullDataSurface,
    ) -> StrategyDecision:

        # Pre-gate hook (e.g., v4_fusion custom evaluation)
        if config.pre_gate_hook:
            hook_fn = self._hooks.get(name, {}).get(config.pre_gate_hook)
            if hook_fn:
                hook_result = hook_fn(surface)
                if hook_result is not None:
                    if hook_result.action == "SKIP":
                        return hook_result  # Hook skipped — honour immediately
                    # Hook returned TRADE — run YAML gates as post-filters
                    for gate in self._pipelines[name]:
                        gate_result = gate.evaluate(surface)
                        if not gate_result.passed:
                            # Preserve hook-computed direction/confidence so the
                            # Signal Explorer can render "would have traded X
                            # but filtered by gate Y". DecisionMetadata layers
                            # a ``post_hook_gate_failed`` marker into extras
                            # while keeping the hook's existing metadata as
                            # the base — matches pre-VO semantics 1:1.
                            hook_meta = DecisionMetadata.from_dict(
                                hook_result.metadata
                            ).with_extras(post_hook_gate_failed=gate_result.gate_name)
                            return StrategyDecision.skip(
                                reason=(
                                    f"post_hook_gate {gate_result.gate_name}: "
                                    f"{gate_result.reason}"
                                ),
                                strategy_id=name,
                                strategy_version=config.version,
                                direction=hook_result.direction,
                                confidence=hook_result.confidence,
                                confidence_score=hook_result.confidence_score,
                                entry_cap=hook_result.entry_cap,
                                collateral_pct=hook_result.collateral_pct,
                                metadata=hook_meta,
                            )
                    return hook_result  # All post-hook gates passed

        # Run gate pipeline
        gate_results: list[GateResult] = []
        for gate in self._pipelines[name]:
            result = gate.evaluate(surface)
            gate_results.append(result)
            if not result.passed:
                # gate_results trace lives under extras — it's strategy-
                # scoped debug info, not part of the shared decision shape.
                return StrategyDecision.skip(
                    reason=f"{result.gate_name}: {result.reason}",
                    strategy_id=name,
                    strategy_version=config.version,
                    metadata=DecisionMetadata(
                        extras={
                            "gate_results": [
                                {
                                    "gate": r.gate_name,
                                    "passed": r.passed,
                                    "reason": r.reason,
                                }
                                for r in gate_results
                            ]
                        }
                    ),
                )

        # All gates passed -- determine direction + sizing
        direction = self._determine_direction(config, surface)
        sizing = self._calculate_sizing(config, surface, gate_results)

        # Post-gate hook (e.g., v10 confidence classification)
        if config.post_gate_hook:
            hook_fn = self._hooks.get(name, {}).get(config.post_gate_hook)
            if hook_fn:
                sizing = hook_fn(surface, sizing)

        # Build confidence from surface
        confidence = surface.v4_conviction
        confidence_score = surface.v4_conviction_score
        if surface.poly_confidence_distance is not None:
            confidence_score = surface.poly_confidence_distance * 2.0

        # Resolve window_ts with `is None` check — window_ts=0 is a legit
        # epoch-origin value (tests) that falsy-short-circuit would corrupt.
        _raw_window_ts = getattr(surface, "window_ts", None)
        resolved_window_ts = (
            _raw_window_ts
            if _raw_window_ts is not None
            else getattr(surface, "eval_window_ts", None)
        )
        # Regime resolution: v4_regime (HMM classifier) preferred. Falls back
        # to vol-regime `surface.regime` (CALM / NORMAL / TRANSITION / CASCADE)
        # because data_surface only populates v4_regime when the TimesFM
        # /v4/snapshot endpoint delivers the `regime` key.
        resolved_regime = surface.v4_regime or getattr(surface, "regime", None)

        trade_metadata = DecisionMetadata(
            regime=resolved_regime,
            conviction=surface.v4_conviction,
            window_ts=resolved_window_ts,
            extras={
                "gate_results": [
                    {"gate": r.gate_name, "passed": r.passed, "reason": r.reason}
                    for r in gate_results
                ],
                "sizing": {
                    "fraction": sizing.fraction,
                    "modifier": sizing.size_modifier,
                    "label": sizing.label,
                    "entry_cap": sizing.entry_cap,
                },
                "poly_direction": surface.poly_direction,
                "poly_confidence_distance": surface.poly_confidence_distance,
                "v2_probability_up": surface.v2_probability_up,
            },
        )

        # Defensive: if all gates passed but we still couldn't derive a
        # direction, convert to SKIP rather than emit a TRADE with
        # direction=None. Pre-VO code accepted None and bounced further
        # downstream; the factory's explicit validation catches it here,
        # which is where the bug belongs.
        if direction not in ("UP", "DOWN"):
            return StrategyDecision.skip(
                reason="no_direction_after_gates_passed",
                strategy_id=name,
                strategy_version=config.version,
                metadata=trade_metadata,
            )

        return StrategyDecision.trade(
            direction=direction,
            strategy_id=name,
            strategy_version=config.version,
            entry_reason=f"{name}_T{surface.eval_offset}_{direction}_{sizing.label}",
            metadata=trade_metadata,
            confidence=confidence,
            confidence_score=confidence_score,
            entry_cap=sizing.entry_cap,
            collateral_pct=sizing.max_collateral_pct * sizing.size_modifier,
        )

    def _determine_direction(
        self,
        config: StrategyConfig,
        surface: FullDataSurface,
    ) -> Optional[str]:
        """Determine trade direction from config + surface.

        Priority: config fixed direction > poly_direction > v2_probability_up.
        """
        # Check if direction gate fixed it
        for gate_def in config.gates:
            if gate_def["type"] == "direction":
                d = gate_def.get("params", {}).get("direction", "ANY")
                if d != "ANY":
                    return d

        # From polymarket outcome
        if surface.poly_direction:
            return surface.poly_direction

        # From v2 probability
        if surface.v2_probability_up is not None:
            return "UP" if surface.v2_probability_up > 0.5 else "DOWN"

        return None

    def _calculate_sizing(
        self,
        config: StrategyConfig,
        surface: FullDataSurface,
        gate_results: list[GateResult],
    ) -> SizingResult:
        """Calculate position sizing from config + gate data."""
        sizing_cfg = config.sizing
        result = SizingResult(
            fraction=sizing_cfg.get("fraction", 0.025),
            max_collateral_pct=sizing_cfg.get("max_collateral_pct", 0.10),
        )

        # Check gate results for sizing data
        for gr in gate_results:
            if "size_modifier" in gr.data:
                result.size_modifier = gr.data["size_modifier"]
                result.label = gr.data.get("label", "gate_sized")
            if "entry_cap" in gr.data:
                result.entry_cap = gr.data["entry_cap"]

        # Custom sizing hook
        if sizing_cfg.get("type") == "custom" and sizing_cfg.get("custom_hook"):
            hook_fn = self._hooks.get(config.name, {}).get(sizing_cfg["custom_hook"])
            if hook_fn:
                custom = hook_fn(surface, result)
                if isinstance(custom, SizingResult):
                    result = custom

        # Use V4 recommended collateral if available
        if result.max_collateral_pct == 0.10 and surface.v4_recommended_collateral_pct:
            result.max_collateral_pct = surface.v4_recommended_collateral_pct

        return result
