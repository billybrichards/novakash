"""Strategy Registry -- loads YAML configs, builds gate pipelines, evaluates.

Config-first strategy system. Each strategy defined in YAML with optional
custom Python hooks. No inheritance chain.

Audit: CA-07.
"""

from __future__ import annotations

import importlib.util
import os
import time
from dataclasses import asdict, dataclass, field, replace as _dc_replace
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
from strategies.exit.conviction_fade import ConvictionFadeDetector, FadeInstruction
from strategies.runtime_override import apply_runtime_overrides

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
    # v4_down_only v2.3.0 — ensemble-era guards (#198)
    from strategies.gates.conviction import ConvictionGate
    from strategies.gates.vpin_gate import VPINGate
    from strategies.gates.regime_v4 import RegimeV4Gate
    # 2026-05-01 — N-tick confirmation gate for declarative-gate strategies
    # (parity with v8/v9 inline check_confirmation logic).
    from strategies.gates.consecutive_pass_ticks import ConsecutivePassTicksGate
    # 2026-05-02 — data-backed strategy gates (see
    # docs/data-backed-strategy-plan-2026-05-02/02-key-findings.md).
    from strategies.gates.multi_model_consensus import MultiModelConsensusGate
    from strategies.gates.model_disagreement_veto import ModelDisagreementVetoGate
    from strategies.gates.confidence_band_skip import ConfidenceBandSkipGate
    # 2026-05-04 — close strict-gate-list safety gap surfaced by the
    # 10:55 UTC regime flip (chainlink/tiingo direction disagreement +
    # CLOB asks outside historical PnL band). Mirrors the v9_ensemble
    # ``skip_on_oracle_disagree`` and ``fill_band_*`` checks but as
    # standalone gate-list-style gates.
    from strategies.gates.oracle_direction import OracleDirectionGate
    from strategies.gates.fill_band import FillBandGate

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
            # v4_down_only v2.3.0
            "conviction_gate": ConvictionGate,
            "vpin_gate": VPINGate,
            "regime_v4": RegimeV4Gate,
            # 2026-05-01 — N-tick confirmation
            "consecutive_pass_ticks": ConsecutivePassTicksGate,
            # 2026-05-02 — data-backed strategy gates
            "multi_model_consensus": MultiModelConsensusGate,
            "model_disagreement_veto": ModelDisagreementVetoGate,
            "confidence_band_skip": ConfidenceBandSkipGate,
            # 2026-05-04 — strict-gate-list safety gates
            "oracle_direction": OracleDirectionGate,
            "fill_band": FillBandGate,
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
        position_monitor: Any = None,
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
        # Post-fill exit monitoring (2026-04-24). PositionMonitor tracks
        # open positions and triggers exits when signals flip. Stays None
        # when not wired (tests, legacy composition paths).
        self._position_monitor = position_monitor
        # Conviction fade detector (2026-04-29). Tracks LGB dist decay
        # from entry and flags positions where model confidence is fading.
        # Always created (lightweight); activation controlled by per-strategy
        # gate_param ``conviction_fade_enabled``.
        self._conviction_fade_detector = ConvictionFadeDetector()
        # Engine paper_mode flag — when True, TRADE decisions are logged
        # but never sent to execute_trade_uc. Prevents paper-mode executions
        # from inserting window_states rows that block LIVE trades after
        # mode switch. Updated via set_paper_mode() from runtime.py.
        self._paper_mode = True  # safe default: paper until told otherwise
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

    def set_paper_mode(self, paper: bool) -> None:
        """Update engine paper_mode. Called from runtime on mode switch."""
        self._paper_mode = paper

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

    def _effective_configs(self) -> dict[str, "StrategyConfig"]:
        """Return ``strategy_id -> StrategyConfig`` with runtime overrides
        applied (mode + gate_params).

        The raw ``self._configs`` reflects YAML-on-disk only. At runtime the
        DB ``strategy_runtime_overrides`` table can flip a strategy's mode
        (e.g. v10 GHOST→LIVE without restart, audit #291). Anything that
        cares about *effective* state — TG window summary, per-strategy skip
        cards, position-monitor gating — must apply the override or it will
        silently miss strategies that were promoted/demoted at runtime.

        Specifically: bug surfaced 2026-04-27 where v10_lgb_only was set
        LIVE via override but its rows never appeared in the window summary
        TG card because the build use case only saw the YAML mode (GHOST).
        """
        effective: dict[str, StrategyConfig] = {}
        for name, cfg in self._configs.items():
            eff_mode, eff_params = apply_runtime_overrides(
                name, cfg.mode, cfg.gate_params
            )
            if eff_mode != cfg.mode or eff_params != cfg.gate_params:
                effective[name] = _dc_replace(
                    cfg, mode=eff_mode, gate_params=eff_params
                )
            else:
                effective[name] = cfg
        return effective

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
            # Apply DB runtime override (audit #291): flips mode/gate_params
            # without engine restart. No-op when cache is empty or no override
            # is set for this strategy. Full logic in strategies.runtime_override.
            _eff_mode, _eff_params = apply_runtime_overrides(
                name, config.mode, config.gate_params
            )
            if _eff_mode != config.mode or _eff_params != config.gate_params:
                config = _dc_replace(config, mode=_eff_mode, gate_params=_eff_params)
            if config.mode == "DISABLED":
                continue
            if config.timescale != window_tf:
                continue
            # Multi-asset guard: skip strategies whose configured asset
            # doesn't match this window's asset. Without this, a BTC window
            # would evaluate v7_15m_sniper_eth and produce mislabelled data.
            window_asset = getattr(window, "asset", "BTC")
            if getattr(config, "asset", "BTC") != window_asset:
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

                        # Regime field fix (2026-04-21): shadow_decisions were
                        # landing in DB with ``regime: None`` because the
                        # decision-write path never captured surface.v4_regime.
                        # Inject it into metadata on EVERY path (TRADE / SKIP /
                        # ERROR) so Strategy Lab queries are analysable and
                        # per-regime WR splits work out of the box.
                        meta_to_write = dict(decision.metadata or {})
                        # Top-level ``regime`` is what the Strategy Lab reads.
                        # Fall back to vpin regime when v4_regime absent —
                        # matches the resolution policy inside _evaluate_one_inner.
                        resolved_regime = getattr(surface, "v4_regime", None) or getattr(
                            surface, "regime", None
                        )
                        # Only overwrite when absent; strategy hooks may have
                        # already set a richer value and we don't want to
                        # clobber that.
                        if meta_to_write.get("regime") is None:
                            meta_to_write["regime"] = resolved_regime
                        # Mirror under the legacy ``v4_regime`` key that v4_fusion
                        # / v5_ensemble / v6_sniper already emit — keeps consumers
                        # that read either key happy.
                        if meta_to_write.get("v4_regime") is None:
                            meta_to_write["v4_regime"] = getattr(
                                surface, "v4_regime", None
                            )

                        # Classifier shadow logging (note #226, phase 1a).
                        # Stamp pc / pl on EVERY strategy_decisions row,
                        # even for strategies that don't consume pc today
                        # (v8_champion_lgb_only is LIVE and ignores it).
                        # This is the audit trail for the 48h burn-in —
                        # no other place in the pipeline records pc on a
                        # per-evaluation, per-strategy basis keyed by
                        # asset + window_ts + eval_offset.
                        # Only fill when absent so strategies that already
                        # surfaced richer values (e.g. v9_ensemble's own
                        # pc/pl snapshot + disagreement) keep their version.
                        p_classifier = getattr(surface, "probability_classifier", None)
                        p_lgb = getattr(surface, "probability_lgb", None)
                        if (
                            "probability_classifier" not in meta_to_write
                            and p_classifier is not None
                        ):
                            meta_to_write["probability_classifier"] = p_classifier
                        if (
                            "probability_lgb" not in meta_to_write
                            and p_lgb is not None
                        ):
                            meta_to_write["probability_lgb"] = p_lgb
                        # ── Audit #307: ret_since_open, CoinGlass, v4_regime ───
                        # ret_since_open: return since window open at decision time
                        if "ret_since_open" not in meta_to_write:
                            _cur = getattr(surface, "current_price", None)
                            _opn = getattr(surface, "open_price", None)
                            if _cur and _opn and _opn > 0:
                                meta_to_write["ret_since_open"] = round(
                                    (_cur - _opn) / _opn, 6
                                )

                        # CoinGlass 6 fields (from surface at score time)
                        for _cg_key in (
                            "cg_oi_usd",
                            "cg_funding_rate",
                            "cg_taker_buy_vol",
                            "cg_taker_sell_vol",
                            "cg_liq_total",
                            "cg_liq_long",
                            "cg_liq_short",
                        ):
                            if _cg_key not in meta_to_write:
                                _cg_val = getattr(surface, _cg_key, None)
                                if _cg_val is not None:
                                    meta_to_write[_cg_key] = _cg_val

                        # Fetch telemetry for burn-in p95 latency checks.
                        # Stamped only when absent — strategy hooks don't
                        # know this field; it's owned by the data_surface
                        # layer which did the HTTP call.
                        try:
                            telemetry = self._data_surface.get_fetch_telemetry(
                                getattr(window, "asset", "BTC")
                            )
                        except Exception:
                            telemetry = {}
                        for tk, tv in telemetry.items():
                            if tk not in meta_to_write:
                                meta_to_write[tk] = tv

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
                            metadata_json=json.dumps(meta_to_write),
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
                    and not self._paper_mode
                ):
                    # ── Defense-in-depth: reject past-close evals before
                    # we even hit ExecuteTradeUseCase. Forensics 2026-04-26
                    # window 1777232100: a LIVE TRADE decision was being
                    # dispatched against a window 5+ min past close. The
                    # DEFINITIVE check still lives in execute_trade.Step 0
                    # (timing_recheck) but doing a quick gate here means
                    # we never even build a stake / take a lease for an
                    # unambiguously-dead window. Cheap arithmetic, never
                    # blocks a fresh window.
                    try:
                        import time as _t_now
                        _w_dur = (
                            900 if window_tf == "15m"
                            else 3600 if window_tf == "1h"
                            else 14400 if window_tf == "4h"
                            else 300
                        )
                        _w_close = int(window_ts) + int(_w_dur)
                        _w_offset = _w_close - int(_t_now.time())
                        if _w_offset <= 0:
                            log.warning(
                                "registry.dispatch_past_close_blocked",
                                strategy=name,
                                window_ts=int(window_ts),
                                close_ts=_w_close,
                                current_offset=_w_offset,
                                timeframe=window_tf,
                            )
                            # Surface as a normal SKIP-equivalent: no
                            # execute, no lease, no CLOB. Registry's
                            # logging continues with the existing flow.
                            continue
                    except Exception as _stale_exc:
                        log.warning(
                            "registry.dispatch_stale_check_error",
                            error=str(_stale_exc)[:200],
                        )
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
                            # Register fill with PositionMonitor for exit
                            # monitoring (2026-04-24). Only LIVE fills with
                            # exit_monitor_enabled in metadata.
                            if self._position_monitor is not None:
                                _exit_enabled = (decision.metadata or {}).get(
                                    "exit_monitor_enabled", False
                                )
                                _hedge_enabled = (config.gate_params or {}).get(
                                    "hedge_exit_enabled", False
                                )
                                if (_exit_enabled or _hedge_enabled) and result.fill_price:
                                    _confirmed = getattr(result, "size_matched", 0.0) or 0.0
                                    _dir = (decision.direction or "UP").upper()
                                    _opp = (
                                        getattr(window_market, "down_token_id", "") or ""
                                    ) if (_dir == "UP" and window_market is not None) else (
                                        (getattr(window_market, "up_token_id", "") or "")
                                        if window_market is not None else ""
                                    )
                                    self._position_monitor.on_fill(
                                        strategy_id=name,
                                        window_ts=window_ts,
                                        direction=decision.direction or "UP",
                                        fill_price=result.fill_price,
                                        fill_size=result.fill_size or 0,
                                        order_id=result.order_id or "",
                                        token_id=result.token_id or "",
                                        confirmed_size=_confirmed,
                                        opposite_token_id=_opp,
                                    )
                                    # Register conviction fade tracking (2026-04-29).
                                    # Capture LGB dist at entry time for fade detection.
                                    _fade_enabled = (config.gate_params or {}).get(
                                        "conviction_fade_enabled", False
                                    )
                                    if _fade_enabled and surface is not None:
                                        _lgb_p = getattr(surface, "probability_lgb", None)
                                        if _lgb_p is not None:
                                            try:
                                                _entry_dist = abs(float(_lgb_p) - 0.5)
                                            except (TypeError, ValueError):
                                                _entry_dist = 0.0
                                            self._conviction_fade_detector.register(
                                                strategy_id=name,
                                                window_ts=window_ts,
                                                entry_dist=_entry_dist,
                                            )
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
        # ── Post-fill exit evaluation (2026-04-24) ───────────────────────
        # For each open monitored position, evaluate whether signals have
        # flipped and an exit should be triggered. Runs on every eval tick.
        if self._position_monitor is not None:
            for pos_key, pos in list(
                self._position_monitor.get_open_positions().items()
            ):
                try:
                    _p_cfg = self._configs.get(pos.strategy_id)
                    if _p_cfg is None:
                        continue
                    # Apply DB runtime overrides (same as per-strategy eval
                    # loop at L478). Without this, gate_params set via the
                    # strategy_runtime_overrides table (e.g.
                    # exit_monitor_enabled, hedge_exit_enabled) are invisible
                    # to the post-fill exit evaluators.
                    _eff_mode_exit, _eff_params_exit = apply_runtime_overrides(
                        pos.strategy_id, _p_cfg.mode, _p_cfg.gate_params
                    )
                    if _eff_params_exit != _p_cfg.gate_params:
                        _p_cfg = _dc_replace(
                            _p_cfg, gate_params=_eff_params_exit
                        )
                    _exit_params = {}
                    # Read exit params from the strategy's gate_params
                    _gp = _p_cfg.gate_params
                    _exit_params = {
                        "exit_monitor_enabled": _gp.get(
                            "exit_monitor_enabled", True
                        ),
                        "exit_eval_start_offset": _gp.get(
                            "exit_eval_start_offset", 48
                        ),
                        "exit_eval_end_offset": _gp.get(
                            "exit_eval_end_offset", 30
                        ),
                        "exit_mark_min_pct": _gp.get(
                            "exit_mark_min_pct", 0.3145
                        ),
                        "exit_mark_ticks": _gp.get(
                            "exit_mark_ticks", 6
                        ),
                        # PR #402: multi-tier ladder + signal-flip detector
                        "exit_tiers": _gp.get("exit_tiers"),
                        "flip_enabled": _gp.get("flip_enabled", False),
                        "flip_p_threshold": _gp.get(
                            "flip_p_threshold", 0.85
                        ),
                        "flip_dist_threshold": _gp.get(
                            "flip_dist_threshold", 0.20
                        ),
                        "flip_consecutive_ticks": _gp.get(
                            "flip_consecutive_ticks", 3
                        ),
                        "flip_min_offset": _gp.get(
                            "flip_min_offset", 60
                        ),
                        "flip_max_offset": _gp.get(
                            "flip_max_offset", 240
                        ),
                        "stale_mark_max_age_seconds": _gp.get(
                            "stale_mark_max_age_seconds", 5.0
                        ),
                    }
                    exit_reason = self._position_monitor.evaluate_exit(
                        strategy_id=pos.strategy_id,
                        window_ts=pos.window_ts,
                        surface=surface,
                        **_exit_params,
                    )
                    if exit_reason:
                        _shadow = _gp.get("exit_shadow_mode", True)
                        _max_retries = _gp.get("exit_max_retries", 1)
                        _retry_timeout = _gp.get(
                            "exit_retry_timeout_seconds", 5
                        )
                        import asyncio as _aio

                        _aio.create_task(
                            self._position_monitor.execute_exit(
                                strategy_id=pos.strategy_id,
                                window_ts=pos.window_ts,
                                reason=exit_reason,
                                exit_shadow_mode=_shadow,
                                exit_max_retries=_max_retries,
                                exit_retry_timeout_seconds=_retry_timeout,
                            )
                        )

                    # ── Hedge-exit eval (PR #X, 2026-04-27) ───────────
                    # Independent of mark-to-market exit. Buy-opposite-and-
                    # hold pattern. Off by default; user opts in per
                    # strategy via hedge_exit_enabled.
                    _hedge_params = {
                        "hedge_exit_enabled": _gp.get(
                            "hedge_exit_enabled", False
                        ),
                        "hedge_lgb_p_opposite_min": _gp.get(
                            "hedge_lgb_p_opposite_min", 0.85
                        ),
                        "hedge_lgb_dist_min": _gp.get(
                            "hedge_lgb_dist_min", 0.20
                        ),
                        "hedge_chainlink_delta_opposite": _gp.get(
                            "hedge_chainlink_delta_opposite", True
                        ),
                        "hedge_tiingo_delta_opposite": _gp.get(
                            "hedge_tiingo_delta_opposite", True
                        ),
                        "hedge_consensus_consecutive_ticks": _gp.get(
                            "hedge_consensus_consecutive_ticks", 5
                        ),
                        "hedge_active_offset_min": _gp.get(
                            "hedge_active_offset_min", 90
                        ),
                        "hedge_active_offset_max": _gp.get(
                            "hedge_active_offset_max", 200
                        ),
                        "hedge_max_opposite_ask": _gp.get(
                            "hedge_max_opposite_ask", 0.45
                        ),
                        "hedge_min_guaranteed_profit_usd": _gp.get(
                            "hedge_min_guaranteed_profit_usd", 0.50
                        ),
                        "stale_mark_max_age_seconds": _gp.get(
                            "stale_mark_max_age_seconds", 5.0
                        ),
                    }
                    hedge_instruction = self._position_monitor.evaluate_hedge_exit(
                        strategy_id=pos.strategy_id,
                        window_ts=pos.window_ts,
                        surface=surface,
                        **_hedge_params,
                    )
                    if hedge_instruction:
                        _h_shadow = _gp.get("hedge_shadow_mode", True)
                        _h_max_retries = _gp.get("hedge_max_retries", 1)
                        _h_buy_timeout = _gp.get(
                            "hedge_buy_timeout_seconds", 5
                        )
                        import asyncio as _aio2

                        _aio2.create_task(
                            self._position_monitor.execute_hedge_exit(
                                strategy_id=pos.strategy_id,
                                window_ts=pos.window_ts,
                                instruction=hedge_instruction,
                                hedge_shadow_mode=_h_shadow,
                                hedge_max_retries=_h_max_retries,
                                hedge_buy_timeout_seconds=_h_buy_timeout,
                            )
                        )

                    # ── Conviction fade eval (2026-04-29) ─────────────
                    # Independent of mark-to-market and hedge exits. Detects
                    # model confidence decay (LGB dist shrinks from entry).
                    # Default shadow_mode=True — log only, don't execute.
                    _fade_params = {
                        "conviction_fade_enabled": _gp.get(
                            "conviction_fade_enabled", False
                        ),
                        "conviction_fade_threshold_pct": _gp.get(
                            "conviction_fade_threshold_pct", 0.40
                        ),
                        "conviction_fade_absolute_floor": _gp.get(
                            "conviction_fade_absolute_floor", 0.08
                        ),
                        "conviction_fade_min_ticks": _gp.get(
                            "conviction_fade_min_ticks", 3
                        ),
                        "conviction_fade_active_offset_min": _gp.get(
                            "conviction_fade_active_offset_min", 60
                        ),
                        "conviction_fade_active_offset_max": _gp.get(
                            "conviction_fade_active_offset_max", 200
                        ),
                        "stale_mark_max_age_seconds": _gp.get(
                            "stale_mark_max_age_seconds", 5.0
                        ),
                    }
                    fade_instruction = self._conviction_fade_detector.evaluate(
                        strategy_id=pos.strategy_id,
                        window_ts=pos.window_ts,
                        surface=surface,
                        **_fade_params,
                    )
                    if fade_instruction:
                        _f_shadow = _gp.get("conviction_fade_shadow_mode", True)
                        if _f_shadow:
                            log.info(
                                "conviction_fade.shadow_exit",
                                strategy=fade_instruction.strategy_id,
                                window_ts=fade_instruction.window_ts,
                                entry_dist=f"{fade_instruction.entry_dist:.4f}",
                                current_dist=f"{fade_instruction.current_dist:.4f}",
                                fade_pct=f"{fade_instruction.fade_pct:.4f}",
                                trigger_reason=fade_instruction.trigger_reason,
                                consecutive_ticks=fade_instruction.consecutive_ticks,
                                action="shadow_exit",
                            )
                            # Write to exit_shadow_log (conviction fade shadow)
                            import asyncio as _aio_fade

                            _aio_fade.create_task(
                                self._position_monitor._write_exit_shadow_log(
                                    strategy_id=fade_instruction.strategy_id,
                                    window_ts=fade_instruction.window_ts,
                                    direction=pos.direction,
                                    detector_type="fade",
                                    entry_dist=fade_instruction.entry_dist,
                                    current_dist=fade_instruction.current_dist,
                                    fade_pct=fade_instruction.fade_pct,
                                    consecutive_ticks=fade_instruction.consecutive_ticks,
                                    triggered=True,
                                    shadow_mode=True,
                                    reason=f"conviction_fade: {fade_instruction.trigger_reason}",
                                )
                            )
                        else:
                            # Real exit — use the same execute_exit path as
                            # mark-to-market. Reason string carries fade context.
                            _f_max_retries = _gp.get("exit_max_retries", 1)
                            _f_retry_timeout = _gp.get(
                                "exit_retry_timeout_seconds", 5
                            )
                            _exit_reason = (
                                f"conviction_fade: entry_dist={fade_instruction.entry_dist:.4f} "
                                f"current_dist={fade_instruction.current_dist:.4f} "
                                f"fade_pct={fade_instruction.fade_pct:.4f} "
                                f"trigger={fade_instruction.trigger_reason}"
                            )
                            import asyncio as _aio3

                            _aio3.create_task(
                                self._position_monitor.execute_exit(
                                    strategy_id=pos.strategy_id,
                                    window_ts=pos.window_ts,
                                    reason=_exit_reason,
                                    exit_shadow_mode=False,
                                    exit_max_retries=_f_max_retries,
                                    exit_retry_timeout_seconds=_f_retry_timeout,
                                )
                            )

                except Exception as _exc:
                    log.warning(
                        "registry.exit_eval_error",
                        position=pos_key,
                        error=str(_exc)[:200],
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
            #
            # NOTE: must read EFFECTIVE mode (post runtime override), not
            # raw YAML mode — a strategy promoted GHOST→LIVE at runtime
            # otherwise silently never emits skip cards. Bug 2026-04-27
            # missed v10 entirely after re-LIVE flip.
            _eff_configs_skip = self._effective_configs()
            for dec in decisions:
                config = _eff_configs_skip.get(dec.strategy_id)
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
        ("source_disagreement", "SKIPPED_CONSENSUS"),
        ("risk", "SKIPPED_RISK_GATED"),
        ("kill_switch", "SKIPPED_RISK_GATED"),
        ("daily_loss", "SKIPPED_RISK_GATED"),
        ("exposure", "SKIPPED_RISK_GATED"),
        ("trade_not_advised", "SKIPPED_RISK_GATED"),
        ("entry_price", "SKIPPED_PRICE_BAND"),
        ("price_floor", "SKIPPED_PRICE_BAND"),
        ("dynamic_cap", "SKIPPED_PRICE_BAND"),
        ("spread", "SKIPPED_PRICE_BAND"),
        ("direction", "SKIPPED_DIRECTION"),
        ("timing", "SKIPPED_TIMING"),
        ("health_unsafe", "SKIPPED_HEALTH"),
        ("health_degraded", "SKIPPED_HEALTH"),
        ("regime", "SKIPPED_REGIME"),
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
                    # Sibling strategy claimed the window first (try_claim_trade
                    # lost the race). NOT a real cooldown — bucketing this as
                    # SKIPPED_COOLDOWN previously made TG cards say "cooldown"
                    # for v9 even when post_loss_cooldown_min=0, confusing the
                    # operator. SKIPPED_DEDUP renders as "sibling already
                    # traded" — true and actionable.
                    outcome = "SKIPPED_DEDUP"
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
                fields = {}
            if fields and any(v is not None for v in fields.values()):
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

        # 2026-04-19: also upsert v5_ensemble probability surface into
        # window_snapshots so historical counterfactual WR (p_lgb alone
        # vs p_classifier alone vs ensemble p_up) is queryable in SQL.
        # Fire-and-forget, mirrors the v3/v4 upsert above. Safe when
        # ensemble is disabled — extractor returns NULLs, DB UPDATE is
        # a no-op (COALESCE preserves existing values).
        if self._db is not None and hasattr(
            self._db, "update_window_ensemble_fields"
        ):
            try:
                from strategies.five_min_vpin import _ensemble_surface_fields

                ens_fields = _ensemble_surface_fields(surface)
            except Exception:
                ens_fields = {}
            if ens_fields and any(v is not None for v in ens_fields.values()):
                ens_task = asyncio.create_task(
                    self._db.update_window_ensemble_fields(
                        window_ts=surface.window_ts,
                        asset=surface.asset,
                        timeframe=surface.timescale,
                        eval_offset=surface.eval_offset,
                        ensemble_fields=ens_fields,
                    )
                )
                ens_task.add_done_callback(
                    self._log_async_write_error(
                        "registry.ensemble_fields_write_error"
                    )
                )

                # Audit-task #332 — also stamp probability_lgb_v12 on the
                # parallel signal_evaluations writer target. Column was added
                # by migration but no engine path ever wrote it (writer
                # regression #6, parallel to PRs #438/#441). UPDATE-only:
                # no-op when the row has not yet been written by
                # ``_write_signal_evaluation``; the next eval tick fills it.
                v12 = ens_fields.get("probability_lgb_v12")
                if v12 is not None and hasattr(
                    self._db, "update_signal_evaluations_lgb_v12"
                ):
                    se_task = asyncio.create_task(
                        self._db.update_signal_evaluations_lgb_v12(
                            window_ts=surface.window_ts,
                            asset=surface.asset,
                            timeframe=surface.timescale,
                            eval_offset=surface.eval_offset,
                            probability_lgb_v12=v12,
                        )
                    )
                    se_task.add_done_callback(
                        self._log_async_write_error(
                            "registry.signal_eval_lgb_v12_write_error"
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
            # Pass *effective* configs so a strategy promoted GHOST→LIVE
            # via the DB override (v10_lgb_only after 2026-04-27 re-LIVE)
            # is bucketed correctly in the summary instead of being hidden
            # under the GHOST collapse rule. The raw YAML configs would
            # erase v10's lines from the card.
            summary_ctx = self._build_summary_uc.execute(
                window_ts=window_ts,
                eval_offset=eval_offset,
                timescale=inferred_tf,
                open_price=getattr(surface, "open_price", None),
                current_price=getattr(surface, "current_price", None),
                sources_agree=sources_agree,
                decisions=decisions,
                configs=self._effective_configs(),
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
                    # Override strategy_id/version from the YAML config name,
                    # not the hardcoded _STRATEGY_ID in the hook. Shared hooks
                    # (e.g. v8_champion.py used by cedar variants) would
                    # otherwise tag every decision as "v8_champion".
                    # StrategyDecision is frozen — use dataclasses.replace.
                    if hook_result.strategy_id != name:
                        from dataclasses import replace as _dc_replace
                        hook_result = _dc_replace(
                            hook_result,
                            strategy_id=name,
                            strategy_version=config.version,
                        )
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
