"""DecisionMetadata — a typed Value Object for a strategy's reasoning trace.

Replaces the free-form ``StrategyDecision.metadata: dict`` that let three
different decision builders diverge on key conventions (``regime`` vs
``v4_regime``, ``conviction`` vs ``v4_conviction``, etc.). See the
"Three Builders" investigation notes + Clean Architecture discussion
(2026-04-17 session handover) for why this lands.

Design shape (Option 2 from the architecture review):

  1. **Shared typed fields** — things every strategy MUST provide. Canonical
     names enforced by the type system. No two strategies can diverge here
     because the field name is the contract.

  2. **Free-form ``extras`` mapping** — strategy-specific reasoning fields.
     Each strategy owns its own extras namespace. Cross-strategy divergence
     is impossible because no two strategies write to the same extras
     namespace — each strategy's extras are isolated.

The ``FullDataSurface`` (not this VO) is the persisted snapshot of input
signals — already captured to ``window_evaluation_traces.surface_json`` on
every eval. This VO is *reasoning trace* — "what the strategy selected as
justification for its decision". Keeping those roles distinct is why the
VO doesn't balloon to surface-size (60+ fields of v2/v3/v4/CoinGlass data).

Persistence contract:
  * ``to_dict()`` produces the flat shape the DB / Trade Recorder consumes.
    Shared fields + extras merge into one level — consumers that read
    ``metadata["clob_delta"]`` today continue to work.
  * ``from_dict(d)`` parses legacy rows, including the ``v4_regime`` /
    ``v4_conviction`` fallback so historical ``strategy_decisions`` rows
    read cleanly.

This VO is Domain layer: standard library only. No SQLAlchemy, no
Pydantic, no framework imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

# Legacy keys we translate at parse time. The v4_fusion tier-0 rewrite
# (v4.5.0) wrote these prefixed names; newer code writes the canonical
# forms. The fallback preserves read-compatibility with existing DB rows
# without needing a migration.
_LEGACY_KEY_MAP: dict[str, str] = {
    "v4_regime": "regime",
    "v4_conviction": "conviction",
}


@dataclass(frozen=True)
class DecisionMetadata:
    """Strategy's reasoning trace for one decision.

    Immutable (``frozen=True``). Construct via the factories
    (``from_surface``, ``from_dict``) or the keyword-only constructor —
    see the factory docstrings for the preferred paths.

    Shared fields — every strategy populates these (or explicitly None):

    :param regime: Regime label active at evaluation time. Accepts either
        short-form (``CALM`` / ``CASCADE`` / ``NORMAL`` / ``TRANSITION``)
        or sister-repo long-form (``calm_trend`` / ``volatile_trend`` /
        ``chop`` / ``risk_off``). String-untyped by design — two regime
        vocabularies exist and this VO should not force one over the
        other at the domain layer.
    :param conviction: Conviction label. Two vocabularies exist too:
        ``NONE``/``LOW``/``MEDIUM``/``HIGH`` (V4Snapshot) and
        ``LOW``/``MODERATE``/``HIGH``/``DECISIVE``
        (StrategyDecision.confidence). Normalise at the consumer boundary
        if needed.
    :param window_ts: Window timestamp in epoch-seconds the decision was
        evaluated for. ``Optional[int]`` — ``None`` means "no window"
        (rare; maintenance paths), ``0`` is semantically distinct from
        ``None`` and NOT treated as missing.
    :param dedup_key: Correlation id used to suppress duplicate trades
        across retries. Optional; only set when dedup matters for the
        strategy.

    Strategy-specific:

    :param extras: Mapping of strategy-scoped reasoning fields. Each
        strategy owns its extras namespace; cross-strategy key collisions
        are expected and safe because no consumer reads
        ``metadata["chainlink_delta"]`` for a strategy that doesn't
        set it. Read through ``.extras.get(...)`` at the call site.
    """

    regime: Optional[str] = None
    conviction: Optional[str] = None
    window_ts: Optional[int] = None
    dedup_key: Optional[str] = None
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Defensive: window_ts=0 is valid (epoch origin, but fine as a
        # semantic value); negative ts is not. Using `is None` + `<` rather
        # than truthiness specifically dodges the falsy-short-circuit bug
        # that bit configs/v4_fusion.py earlier.
        if self.window_ts is not None and self.window_ts < 0:
            raise ValueError(
                f"DecisionMetadata.window_ts must be >= 0, got {self.window_ts}"
            )
        # Extras must be a mapping — reject sequences/sets/None-assigned-
        # to-non-default-types that would silently break downstream.
        if not isinstance(self.extras, Mapping):
            raise TypeError(
                f"DecisionMetadata.extras must be a Mapping, got "
                f"{type(self.extras).__name__}"
            )

    @classmethod
    def empty(cls) -> "DecisionMetadata":
        """Sentinel — returns a metadata with all fields unset.

        Useful for SKIP decisions from paths that haven't built reasoning
        yet (ERROR branches, timing-gate-only rejections).
        """
        return cls()

    @classmethod
    def from_dict(cls, d: Optional[Mapping[str, Any]]) -> "DecisionMetadata":
        """Parse a legacy metadata dict.

        Applies the ``v4_regime`` → ``regime`` and ``v4_conviction`` →
        ``conviction`` fallbacks so historical rows written by the
        pre-VO v4_fusion builder read correctly. Unknown keys land in
        ``extras`` verbatim.

        :param d: The raw dict from DB JSONB or a legacy builder. ``None``
            yields ``DecisionMetadata.empty()``.
        """
        if not d:
            return cls.empty()

        # Reserved keys — the shared typed fields we pull out by name.
        # Legacy keys in _LEGACY_KEY_MAP also considered reserved so they
        # don't leak into extras.
        reserved = {"regime", "conviction", "window_ts", "dedup_key"} | set(
            _LEGACY_KEY_MAP.keys()
        )

        regime = d.get("regime")
        if regime is None:
            regime = d.get("v4_regime")

        conviction = d.get("conviction")
        if conviction is None:
            conviction = d.get("v4_conviction")

        # Explicit `is None` check — preserves window_ts=0 instead of
        # collapsing it via `or` falsy short-circuit.
        raw_ts = d.get("window_ts")
        window_ts = int(raw_ts) if raw_ts is not None else None

        dedup_key = d.get("dedup_key")

        extras = {k: v for k, v in d.items() if k not in reserved}

        return cls(
            regime=regime,
            conviction=conviction,
            window_ts=window_ts,
            dedup_key=dedup_key,
            extras=extras,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the flat dict shape consumers expect.

        Shared fields + extras merge into one level. Keys that are ``None``
        are OMITTED (not serialised as ``None``) so the JSONB rows stay
        terse and diffs against legacy rows are minimal.

        If ``regime`` is set, the canonical ``regime`` key is used — never
        the legacy ``v4_regime``. On the write side the legacy prefix
        disappears once all builders migrate to this VO.
        """
        out: dict[str, Any] = {}
        if self.regime is not None:
            out["regime"] = self.regime
        if self.conviction is not None:
            out["conviction"] = self.conviction
        if self.window_ts is not None:
            out["window_ts"] = self.window_ts
        if self.dedup_key is not None:
            out["dedup_key"] = self.dedup_key
        # Extras flatten to top level — this matches the current on-disk
        # shape so trade_recorder and other downstream consumers that
        # read metadata.get("clob_delta") continue to work unchanged.
        out.update(dict(self.extras))
        return out

    def with_extras(self, **kwargs: Any) -> "DecisionMetadata":
        """Return a copy with additional extras merged in.

        Convenience for builders that want to stage shared fields first,
        then layer strategy-specifics without mutating the (frozen) VO.
        Replaces values on key collision.
        """
        merged = dict(self.extras)
        merged.update(kwargs)
        return DecisionMetadata(
            regime=self.regime,
            conviction=self.conviction,
            window_ts=self.window_ts,
            dedup_key=self.dedup_key,
            extras=merged,
        )
