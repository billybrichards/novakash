"""Telegram renderer — implements ``AlertRendererPort``.

Phase D of the TG narrative refactor. Takes frozen domain payloads and
emits Telegram-markdown strings with:

  * Lifecycle tier emoji + title + timestamps + identifiers
  * Canonical ━ divider (DIVIDER constant, no more drift)
  * BTC price block (one per alert, unifies 5+ scattered numbers)
  * Health badge (OK / DEGRADED / UNSAFE)
  * Cumulative tally footer
  * Four-quadrant outcome labels
  * Shadow report grouped by timeframe

Dispatch is by ``isinstance`` on the payload type. Unknown payloads raise
``TypeError`` — renderers don't silently swallow unknowns.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from domain.alert_values import (
    AlertFooter,
    AlertHeader,
    AlertTier,
    BtcPriceBlock,
    CumulativeTally,
    HealthBadge,
    HealthStatus,
    LifecyclePhase,
    MatchedTradeRow,
    OrphanDrift,
    OutcomeQuadrant,
    ReconcilePayload,
    RelayerCooldownPayload,
    ResolvedAlertPayload,
    ShadowReportPayload,
    ShadowRow,
    StrategyEligibility,
    TradeAlertPayload,
    WalletDeltaKind,
    WalletDeltaPayload,
    WindowOpenPayload,
    WindowSignalPayload,
)
from use_cases.ports import AlertRendererPort

DIVIDER = "━" * 24
SUB_DIVIDER = "─" * 24

_TIER_EMOJI: dict[LifecyclePhase, str] = {
    LifecyclePhase.MARKET: "🪟",
    LifecyclePhase.STATE: "⏱",
    LifecyclePhase.DECISION: "🎯",
    LifecyclePhase.EXECUTION: "⚡",
    LifecyclePhase.RESOLVE: "🏁",
    LifecyclePhase.OPS: "🗂",
}

_HEALTH_EMOJI: dict[HealthStatus, str] = {
    HealthStatus.OK: "✓",
    HealthStatus.DEGRADED: "⚠",
    HealthStatus.UNSAFE: "✗",
}

_OUTCOME_LABEL: dict[OutcomeQuadrant, str] = {
    OutcomeQuadrant.CORRECT_WIN: "✅ CORRECT + WIN",
    OutcomeQuadrant.CORRECT_LOSS: "😬 CORRECT + LOSS (crossed)",
    OutcomeQuadrant.WRONG_WIN: "🎲 WRONG + WIN (crossed)",
    OutcomeQuadrant.WRONG_LOSS: "❌ WRONG + LOSS",
}

_WALLET_KIND_EMOJI: dict[WalletDeltaKind, str] = {
    WalletDeltaKind.MANUAL_WITHDRAWAL: "🏦",
    WalletDeltaKind.TRADING_FLOW: "🔄",
    WalletDeltaKind.REDEMPTION: "💰",
    WalletDeltaKind.UNEXPECTED: "🚨",
    WalletDeltaKind.DRIFT: "🚨",
}


def _fmt_ts(ts_unix: int) -> str:
    dt = datetime.fromtimestamp(ts_unix, tz=timezone.utc)
    return dt.strftime("%H:%M:%S UTC")


def _fmt_t_offset(secs: Optional[int]) -> str:
    if secs is None:
        return ""
    sign = "+" if secs < 0 else "-"
    return f"T{sign}{abs(secs)}"


def _fmt_decimal(d: Optional[Decimal], places: int = 2) -> str:
    if d is None:
        return "-"
    return f"{d:.{places}f}"


def _fmt_pnl(d: Optional[Decimal]) -> str:
    if d is None:
        return "-"
    return f"+${d:.2f}" if d >= 0 else f"-${abs(d):.2f}"


class TelegramRenderer(AlertRendererPort):
    """Render domain payloads → Telegram-markdown strings."""

    def render(self, payload: object) -> str:
        if isinstance(payload, TradeAlertPayload):
            return self._render_trade(payload)
        if isinstance(payload, WindowSignalPayload):
            return self._render_window_signal(payload)
        if isinstance(payload, WindowOpenPayload):
            return self._render_window_open(payload)
        if isinstance(payload, ReconcilePayload):
            return self._render_reconcile(payload)
        if isinstance(payload, ShadowReportPayload):
            return self._render_shadow_report(payload)
        if isinstance(payload, ResolvedAlertPayload):
            return self._render_resolved(payload)
        if isinstance(payload, WalletDeltaPayload):
            return self._render_wallet_delta(payload)
        if isinstance(payload, RelayerCooldownPayload):
            return self._render_relayer_cooldown(payload)
        raise TypeError(
            f"TelegramRenderer has no render case for {type(payload).__name__}"
        )

    # ------------------------------------------------------------------
    # Primitive blocks
    # ------------------------------------------------------------------

    def _render_header(self, h: AlertHeader, tier: Optional[AlertTier] = None) -> str:
        emoji = _TIER_EMOJI.get(h.phase, "•")
        t_off = f"  ({_fmt_t_offset(h.t_offset_secs)})" if h.t_offset_secs is not None else ""
        replay = "  🕯 REPLAY" if h.is_replay else ""
        tier_tag = f"  [{tier.value}]" if tier and tier is AlertTier.TACTICAL else ""
        event_str = _fmt_ts(h.event_ts_unix)
        lines = [f"{emoji} *{h.title}*{tier_tag}{replay}",
                 f"_{event_str}_{t_off}"]
        return "\n".join(lines)

    def _render_footer(self, f: AlertFooter) -> str:
        parts = [f"emit {_fmt_ts(f.emit_ts_unix)}"]
        if f.wallet_usdc is not None:
            parts.append(f"wallet ${_fmt_decimal(f.wallet_usdc)}")
        if f.pending_redeem_usdc is not None and f.pending_redeem_usdc > 0:
            parts.append(f"pending +${_fmt_decimal(f.pending_redeem_usdc)}")
        if f.paper_mode:
            parts.append("📄 PAPER")
        else:
            parts.append("🔴 LIVE")
        if f.window_id:
            parts.append(f"win=`{f.window_id}`")
        if f.order_id:
            parts.append(f"ord=`{f.order_id[:10]}`")
        return "  •  ".join(parts)

    def _render_btc(self, b: BtcPriceBlock) -> str:
        pct = ""
        if b.chainlink_delta_pct is not None:
            sign = "+" if b.chainlink_delta_pct >= 0 else ""
            pct = f"  ({sign}{b.chainlink_delta_pct:.2f}% CL"
            if b.tiingo_delta_pct is not None:
                tsign = "+" if b.tiingo_delta_pct >= 0 else ""
                pct += f" / {tsign}{b.tiingo_delta_pct:.2f}% TI"
            pct += ")"
        agree = ""
        if b.sources_agree is True:
            agree = "  sources:✓"
        elif b.sources_agree is False:
            agree = "  sources:mixed"
        close = ""
        if b.close_price_usd is not None:
            close = f"  close ${b.close_price_usd:,.2f}"
        return (
            f"BTC ${b.now_price_usd:,.2f}"
            f"  open ${b.window_open_usd:,.2f}{close}{pct}{agree}"
        )

    def _render_ensemble_extras(self, extras: Optional[dict]) -> Optional[str]:
        """One-line summary of v5_ensemble's signal-source + ensemble blend.

        Reads opaque dict the use case forwards from
        ``StrategyDecision.metadata``. Returns None unless ``signal_source``
        is set, so non-ensemble strategies render unchanged.

        Format examples (one line each):
          ensemble: source=ensemble  used=0.812  lgb=0.773  path1=0.851  mode=blend
          ensemble: source=lgb_only  used=0.300  lgb=0.300  path1=n/a  mode=blend
          ensemble: source=ensemble  used=0.620  lgb=0.620  path1=n/a  mode=fallback_lgb_only
        """
        if not extras or not extras.get("signal_source"):
            return None

        def _fmt(p):
            try:
                return f"{float(p):.3f}"
            except (TypeError, ValueError):
                return "n/a"

        cfg = extras.get("ensemble_config") or {}
        mode = cfg.get("mode") if isinstance(cfg, dict) else None
        # Isotonic calibration (novakash-timesfm #107). Shown as a terse
        # ``iso=v3`` suffix when the forecaster applied calibration; omitted
        # entirely when None so pre-PR-107 forecaster builds render clean.
        iso_version = extras.get("isotonic_version")
        iso_suffix = f"  iso={iso_version}" if iso_version else ""
        return (
            f"🧬 ensemble: source={extras['signal_source']}"
            f"  used={_fmt(extras.get('probability_used'))}"
            f"  lgb={_fmt(extras.get('probability_lgb'))}"
            f"  path1={_fmt(extras.get('probability_classifier'))}"
            f"  mode={mode or 'n/a'}"
            f"{iso_suffix}"
        )

    def _render_health(self, h: HealthBadge) -> str:
        emoji = _HEALTH_EMOJI[h.status]
        tag = f"{emoji} {h.status.value}"
        if h.reasons:
            tag += "  [" + ", ".join(h.reasons) + "]"
        return f"health: {tag}"

    def _render_tally(self, t: Optional[CumulativeTally], label: str) -> Optional[str]:
        if t is None:
            return None
        wr = f"{t.win_rate * 100:.0f}%" if t.win_rate is not None else "—"
        return f"{label}: {t.wins}W/{t.losses}L ({wr}, {_fmt_pnl(t.pnl_usdc)})"

    # ------------------------------------------------------------------
    # Payload renderers
    # ------------------------------------------------------------------

    def _render_trade(self, p: TradeAlertPayload) -> str:
        dir_arrow = "↑" if p.direction == "UP" else "↓"
        gates = " ".join(
            f"{'✅' if g.get('passed') else '❌'}{g.get('name')}"
            for g in p.gate_results
        )
        order_line = ""
        if p.order_status == "FILLED":
            order_line = (
                f"✅ FILLED  stake=${_fmt_decimal(p.stake_usdc)}"
            )
            if p.fill_price_cents is not None:
                order_line += f" → filled=${_fmt_decimal(p.cost_usdc)}"
                if p.fill_size_shares is not None:
                    order_line += f" ({p.fill_size_shares:.2f}sh @ ${p.fill_price_cents:.3f})"
        elif p.order_status == "RESTING":
            order_line = f"🟡 RESTING  stake=${_fmt_decimal(p.stake_usdc)}"
        else:
            order_line = f"❌ FAILED  stake=${_fmt_decimal(p.stake_usdc)}"
        tallies = [
            self._render_tally(p.today_tally, "today"),
            self._render_tally(p.last_hour_tally, "1h"),
        ]
        tally_line = "  •  ".join(t for t in tallies if t)
        lines = [
            self._render_header(p.header, p.tier),
            DIVIDER,
            f"{dir_arrow} {p.direction}  |  conf={p.confidence_label} ({p.confidence_score:.2f})  |  mode={p.mode}",
            f"gates: {gates}" if gates else "gates: (none)",
        ]
        ensemble_line = self._render_ensemble_extras(p.extras)
        if ensemble_line:
            lines.append(ensemble_line)
        lines.extend([
            SUB_DIVIDER,
            order_line,
            self._render_btc(p.btc),
            self._render_health(p.health),
        ])
        if tally_line:
            lines.append(tally_line)
        lines.append(DIVIDER)
        lines.append(self._render_footer(p.footer))
        return "\n".join(lines)

    def _render_window_signal(self, p: WindowSignalPayload) -> str:
        model = ""
        if p.p_up is not None:
            model = f"P(UP)={p.p_up:.2f}"
            if p.p_up_distance is not None:
                model += f", dist={p.p_up_distance:.2f}"
        vpin = f"VPIN {p.vpin:.2f}" if p.vpin is not None else ""
        meta_line = "  |  ".join(x for x in (model, vpin) if x)

        strat_lines: list[str] = []
        live = [s for s in p.strategies if s.mode == "LIVE"]
        ghost = [s for s in p.strategies if s.mode == "GHOST"]
        disabled = [s for s in p.strategies if s.mode == "DISABLED"]
        if live:
            strat_lines.append("*LIVE:*")
            for s in live:
                strat_lines.append(f"  {self._render_strategy(s)}")
        if ghost:
            strat_lines.append("*GHOST (shadow):*")
            for s in ghost:
                strat_lines.append(f"  {self._render_strategy(s)}")
        if disabled:
            strat_lines.append(f"_disabled: {', '.join(s.strategy_id for s in disabled)}_")

        lines = [
            self._render_header(p.header, p.tier),
            DIVIDER,
            self._render_btc(p.btc),
        ]
        if meta_line:
            lines.append(meta_line)
        lines.append(self._render_health(p.health))
        if strat_lines:
            lines.append(SUB_DIVIDER)
            lines.extend(strat_lines)
        lines.append(DIVIDER)
        lines.append(self._render_footer(p.footer))
        return "\n".join(lines)

    def _render_strategy(self, s: StrategyEligibility) -> str:
        tag = f"{s.strategy_id}"
        if s.action == "TRADE":
            arrow = "↑" if s.direction == "UP" else "↓"
            conf = s.confidence or "?"
            score = f" ({s.confidence_score:.2f})" if s.confidence_score is not None else ""
            return f"{tag}: {arrow} TRADE {s.direction}  conf={conf}{score}"
        if s.action == "ALREADY_TRADED":
            off = _fmt_t_offset(s.already_traded_at_offset)
            return f"{tag}: traded at {off}"
        return f"{tag}: SKIP  {s.skip_reason or '—'}"

    def _render_window_open(self, p: WindowOpenPayload) -> str:
        gamma = ""
        if p.gamma_up_cents is not None and p.gamma_down_cents is not None:
            gamma = (
                f"Gamma: ↑${p.gamma_up_cents:.3f}  ↓${p.gamma_down_cents:.3f}"
            )
            if p.gamma_tilt:
                gamma += f"  ({p.gamma_tilt})"
        lines = [
            self._render_header(p.header, p.tier),
            DIVIDER,
            self._render_btc(p.btc),
        ]
        if gamma:
            lines.append(gamma)
        lines.append(DIVIDER)
        lines.append(self._render_footer(p.footer))
        return "\n".join(lines)

    def _render_reconcile(self, p: ReconcilePayload) -> str:
        lines = [
            self._render_header(p.header, p.tier),
            DIVIDER,
        ]
        # Group matched by (timeframe, strategy_id) — K.7 in plan.
        if p.matched:
            groups = self._group_matched(p.matched)
            lines.append("*LIVE matched:*")
            for (tf, sid), rows in groups.items():
                wins = sum(1 for r in rows if r.outcome == "WIN")
                losses = sum(1 for r in rows if r.outcome == "LOSS")
                net = sum((r.pnl_usdc for r in rows), Decimal("0"))
                lines.append(
                    f"  {tf} {sid}:  {wins}W / {losses}L ({_fmt_pnl(net)})"
                )
                for r in rows[:10]:
                    lines.append(f"    {self._render_matched_row(r)}")
        if p.paper_matched:
            groups = self._group_matched(p.paper_matched)
            lines.append("*PAPER matched:*")
            for (tf, sid), rows in groups.items():
                wins = sum(1 for r in rows if r.outcome == "WIN")
                losses = sum(1 for r in rows if r.outcome == "LOSS")
                lines.append(f"  {tf} {sid}:  {wins}W / {losses}L")
        if p.orphan_drift is not None and p.orphan_drift.changed:
            lines.append(SUB_DIVIDER)
            lines.append(self._render_orphan_drift(p.orphan_drift))
        elif p.orphan_drift is not None:
            lines.append(
                f"_orphans: {p.orphan_drift.current_count} (unchanged)_"
            )
        if p.cumulative is not None:
            cum = self._render_tally(p.cumulative, "today")
            if cum:
                lines.append(cum)
        lines.append(DIVIDER)
        lines.append(self._render_footer(p.footer))
        return "\n".join(lines)

    @staticmethod
    def _group_matched(
        rows: tuple[MatchedTradeRow, ...],
    ) -> dict[tuple[str, str], list[MatchedTradeRow]]:
        out: dict[tuple[str, str], list[MatchedTradeRow]] = {}
        for r in rows:
            out.setdefault((r.timeframe, r.strategy_id), []).append(r)
        return out

    @staticmethod
    def _render_matched_row(r: MatchedTradeRow) -> str:
        emoji = "✅" if r.outcome == "WIN" else "❌"
        ord_tag = f"  ord=`{r.order_id[:10]}`" if r.order_id else ""
        return (
            f"{emoji} {r.outcome}  {r.direction}  @ ${r.entry_price_cents:.3f}  "
            f"{_fmt_pnl(r.pnl_usdc)}  cost ${_fmt_decimal(r.cost_usdc)}{ord_tag}"
        )

    @staticmethod
    def _render_orphan_drift(d: OrphanDrift) -> str:
        sign = "+" if d.delta >= 0 else ""
        line = (
            f"🗂 ORPHAN DRIFT  {d.prior_count} → {d.current_count} ({sign}{d.delta})"
        )
        if d.new_condition_ids:
            sample = ", ".join(cid[:10] + "…" for cid in d.new_condition_ids[:3])
            if len(d.new_condition_ids) > 3:
                sample += f", +{len(d.new_condition_ids) - 3} more"
            line += f"\n  new: {sample}"
        if d.auto_redeemed_wins or d.worthless_tokens:
            line += (
                f"\n  {d.auto_redeemed_wins} auto-redeemed, "
                f"{d.worthless_tokens} worthless"
            )
        return line

    def _render_shadow_report(self, p: ShadowReportPayload) -> str:
        move_pct = (
            (p.actual_close_usd - p.actual_open_usd) / p.actual_open_usd * 100.0
        )
        lines = [
            self._render_header(p.header, p.tier),
            DIVIDER,
            (
                f"actual: {p.actual_direction}  "
                f"${p.actual_open_usd:,.2f} → ${p.actual_close_usd:,.2f}  "
                f"({move_pct:+.2f}%, Chainlink)"
            ),
            SUB_DIVIDER,
        ]
        for r in p.rows:
            lines.append(self._render_shadow_row(r))
        if p.live_pnl_today_usdc is not None or p.ghost_pnl_today_usdc is not None:
            lines.append(SUB_DIVIDER)
            today_bits = []
            if p.live_pnl_today_usdc is not None:
                today_bits.append(f"today LIVE: {_fmt_pnl(p.live_pnl_today_usdc)}")
            if p.ghost_pnl_today_usdc is not None:
                today_bits.append(f"today GHOST: {_fmt_pnl(p.ghost_pnl_today_usdc)}")
            if (
                p.live_pnl_today_usdc is not None
                and p.ghost_pnl_today_usdc is not None
            ):
                edge = p.ghost_pnl_today_usdc - p.live_pnl_today_usdc
                today_bits.append(f"edge: {_fmt_pnl(edge)}")
            lines.append("  •  ".join(today_bits))
        lines.append(DIVIDER)
        lines.append(self._render_footer(p.footer))
        return "\n".join(lines)

    @staticmethod
    def _render_shadow_row(r: ShadowRow) -> str:
        tag = f"{r.strategy_id:<16} {r.mode:<5}"
        if r.action == "SKIP":
            return f"{tag} ⏭ skip  {r.skip_reason or '—'}"
        outcome_label = (
            _OUTCOME_LABEL[r.outcome] if r.outcome is not None else "(no outcome)"
        )
        price = f"@ ${r.entry_price_cents:.3f}" if r.entry_price_cents else ""
        pnl = _fmt_pnl(r.hypothetical_pnl_usdc)
        return f"{tag} {outcome_label}  {r.direction} {price}  {pnl}"

    def _render_resolved(self, p: ResolvedAlertPayload) -> str:
        label = _OUTCOME_LABEL[p.outcome_quadrant]
        lines = [
            self._render_header(p.header, p.tier),
            DIVIDER,
            f"predicted: {p.predicted_direction}",
            (
                f"actual:    {p.actual_direction}  "
                f"(${p.btc.window_open_usd:,.2f} → ${p.btc.now_price_usd:,.2f})"
            ),
            f"result:    {label}  {_fmt_pnl(p.pnl_usdc)}",
            f"entry:     ${p.entry_price_cents:.3f}  stake=${_fmt_decimal(p.stake_usdc)}  mode={p.mode}",
        ]
        tallies = [
            self._render_tally(p.today_tally, "today"),
            self._render_tally(p.session_tally, "session"),
        ]
        tally_line = "  •  ".join(t for t in tallies if t)
        if tally_line:
            lines.append(SUB_DIVIDER)
            lines.append(tally_line)
        lines.append(DIVIDER)
        lines.append(self._render_footer(p.footer))
        return "\n".join(lines)

    def _render_wallet_delta(self, p: WalletDeltaPayload) -> str:
        emoji = _WALLET_KIND_EMOJI[p.delta.kind]
        kind_label = p.delta.kind.value.replace("_", " ")
        dest = f"→ `{p.delta.dest_addr[:10]}…`" if p.delta.dest_addr else "→ (no tx)"
        owner_note = (
            f"  (your {p.owner_eoa_matched[:10]}…)" if p.owner_eoa_matched else ""
        )
        lines = [
            self._render_header(p.header, p.tier),
            DIVIDER,
            f"{emoji} {kind_label}  {_fmt_pnl(p.delta.amount_usdc)}  {dest}{owner_note}",
            (
                f"wallet: ${_fmt_decimal(p.delta.prior_balance_usdc)}  →  "
                f"${_fmt_decimal(p.delta.new_balance_usdc)}"
            ),
        ]
        if p.delta.tx_hash:
            lines.append(f"tx: `{p.delta.tx_hash[:16]}…`")
        if p.today_realized_pnl_usdc is not None:
            lines.append(
                f"today trade P&L: {_fmt_pnl(p.today_realized_pnl_usdc)}"
            )
        if p.delta.kind is WalletDeltaKind.UNEXPECTED:
            lines.append("*ACTION REQUIRED:* verify or rotate POLY_PRIVATE_KEY")
        if p.delta.kind is WalletDeltaKind.DRIFT:
            lines.append("*ACTION REQUIRED:* wallet balance changed with no matching tx")
        lines.append(DIVIDER)
        lines.append(self._render_footer(p.footer))
        return "\n".join(lines)

    def _render_relayer_cooldown(self, p: RelayerCooldownPayload) -> str:
        if p.resumed:
            head = "✅ RELAYER RESUMED"
        else:
            head = f"🚫 RELAYER COOLDOWN  ({p.quota_left}/{p.quota_total} quota left)"
        lines = [
            self._render_header(p.header, p.tier),
            DIVIDER,
            head,
        ]
        if not p.resumed and p.cooldown_reset_unix:
            reset = _fmt_ts(p.cooldown_reset_unix)
            lines.append(f"reset at: {reset}")
        if p.reason:
            lines.append(f"reason: `{p.reason[:80]}`")
        lines.append(DIVIDER)
        lines.append(self._render_footer(p.footer))
        return "\n".join(lines)
