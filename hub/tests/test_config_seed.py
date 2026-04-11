"""
CFG-02 — tests for hub/db/config_seed.py.

Verifies:
  1. The seed list contains every service named in the plan.
  2. validate_seed() rejects secrets and duplicates.
  3. seed_summary() per-service counts match expectations.
  4. seed_config_keys() executes the expected UPSERT for every row
     and is idempotent (a second pass produces the same SQL count).
  5. ON CONFLICT clause preserves operator-set current_value rows
     by NOT including current_value in the UPDATE SET list.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from db.config_seed import (
    ALL_SEED_KEYS,
    SECRET_PATTERN,
    seed_config_keys,
    seed_summary,
    validate_seed,
)


# ─── Inventory tests ──────────────────────────────────────────────────────────


def test_seed_list_is_non_empty():
    assert len(ALL_SEED_KEYS) > 0


def test_seed_list_contains_expected_services():
    """Every service from CONFIG_MIGRATION_PLAN.md §4 has at least one row."""
    services = {row[0] for row in ALL_SEED_KEYS}
    # hub and timesfm are intentionally empty in v1 — see plan §4.3 / §4.6
    expected = {"engine", "margin_engine", "data-collector", "macro-observer"}
    assert expected.issubset(services), (
        f"missing services: {expected - services}"
    )


def test_seed_per_service_counts_are_reasonable():
    """Per-service counts match the rough plan inventory.

    The plan §4.7 says ~88 engine, ~41 margin, 7 data, 6 macro = 142,
    but the literal tables in §4.1.x and §4.2.2 add up to more (the
    plan summary uses "~"). We accept the literal table reading as
    the source of truth.
    """
    counts = seed_summary()
    # Engine has the most keys — at least 80 per the plan minimum.
    assert counts.get("engine", 0) >= 80
    # margin_engine has at least 40.
    assert counts.get("margin_engine", 0) >= 40
    # data-collector has 7 hardcoded constants slated for promotion.
    assert counts.get("data-collector", 0) == 7
    # macro-observer has 6 inline reads.
    assert counts.get("macro-observer", 0) == 6


def test_total_count_meets_plan_target():
    """The plan target is 142+ keys; we exceed that with the literal table reading."""
    assert len(ALL_SEED_KEYS) >= 142


# ─── Validation tests ─────────────────────────────────────────────────────────


def test_validate_seed_passes_on_real_seed():
    """The shipped seed list passes its own validator."""
    validate_seed(ALL_SEED_KEYS)  # raises on failure


def test_validate_seed_rejects_secret_pattern_keys():
    """Per CONFIG_MIGRATION_PLAN.md §10.4, secret-like keys are banned."""
    bad_rows = [
        ("engine", "POLY_API_KEY", "string", "", "should not be here", "infra", False),
        ("engine", "POLY_API_SECRET", "string", "", "x", "infra", False),
        ("engine", "GMAIL_APP_PASSWORD", "string", "", "x", "infra", False),
        ("engine", "POLY_PRIVATE_KEY", "string", "", "x", "infra", False),
        ("engine", "POLY_API_PASSPHRASE", "string", "", "x", "infra", False),
        ("engine", "POLY_FUNDER_ADDRESS", "string", "", "x", "infra", False),
        ("engine", "OPINION_WALLET_KEY", "string", "", "x", "infra", False),
        ("engine", "TELEGRAM_BOT_TOKEN", "string", "", "x", "infra", False),
    ]
    for row in bad_rows:
        with pytest.raises(ValueError, match="refusing to seed secret-like key"):
            validate_seed([row])


def test_validate_seed_rejects_duplicate_pairs():
    """A duplicate (service, key) tuple is a hard error."""
    rows = [
        ("engine", "BET_FRACTION", "float", "0.025", "x", "sizing", False),
        ("engine", "BET_FRACTION", "float", "0.05", "y", "sizing", False),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        validate_seed(rows)


def test_secret_pattern_compiles_correctly():
    """SECRET_PATTERN matches the keys we want to ban and nothing else."""
    # Should match
    for key in (
        "POLY_API_KEY",
        "BINANCE_API_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "GMAIL_APP_PASSWORD",
        "POLY_PRIVATE_KEY",
        "POLY_API_PASSPHRASE",
        "POLY_FUNDER_ADDRESS",
        "OPINION_WALLET_KEY",
    ):
        assert SECRET_PATTERN.match(key), f"should match: {key}"
    # Should NOT match (these are real config keys, not secrets)
    for key in (
        "BET_FRACTION",
        "V10_6_ENABLED",
        "FIVE_MIN_ENABLED",
        "MAX_POSITION_USD",
        "TELEGRAM_ALERTS_PAPER",  # this is a tunable, not a secret
        "VPIN_BUCKET_SIZE_USD",
    ):
        assert not SECRET_PATTERN.match(key), f"should not match: {key}"


def test_no_seeded_key_matches_secret_pattern():
    """Walk every shipped seed row and assert none look like a secret."""
    for row in ALL_SEED_KEYS:
        service, key = row[0], row[1]
        assert not SECRET_PATTERN.match(key), (
            f"{service}.{key} matches SECRET_PATTERN — secrets stay in .env"
        )


def test_every_seed_row_has_seven_fields():
    """Each row is the expected 7-tuple shape."""
    for row in ALL_SEED_KEYS:
        assert len(row) == 7, f"row has wrong shape: {row}"
        service, key, vtype, default_value, description, category, restart_required = row
        assert isinstance(service, str) and service
        assert isinstance(key, str) and key
        assert vtype in ("bool", "int", "float", "string", "enum", "csv"), (
            f"unknown type {vtype!r} on {service}.{key}"
        )
        assert isinstance(restart_required, bool)


def test_v10_gate_keys_are_marked_restart_required():
    """Per plan §6.3, all V10_* gate keys must have restart_required=TRUE
    until CFG-07b lands the gate-class hot-reload refactor."""
    for row in ALL_SEED_KEYS:
        service, key, _, _, _, _, restart_required = row
        if service == "engine" and (key.startswith("V10_") or key == "V11_POLY_SPOT_ONLY_CONSENSUS"):
            assert restart_required is True, (
                f"{service}.{key} should be restart_required=TRUE per plan §6.3"
            )


# ─── seed_config_keys() tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_config_keys_executes_one_upsert_per_row():
    """seed_config_keys() runs exactly len(ALL_SEED_KEYS) execute calls."""
    session = MagicMock()
    session.execute = AsyncMock()

    counts = await seed_config_keys(session)

    assert session.execute.call_count == len(ALL_SEED_KEYS)
    assert sum(counts.values()) == len(ALL_SEED_KEYS)


@pytest.mark.asyncio
async def test_seed_config_keys_is_idempotent_on_resync():
    """Running seed_config_keys() twice produces 2x the execute count
    (the SQL is the same UPSERT each time, the DB does the dedup)."""
    session = MagicMock()
    session.execute = AsyncMock()

    await seed_config_keys(session)
    first_call_count = session.execute.call_count

    await seed_config_keys(session)
    assert session.execute.call_count == 2 * first_call_count


@pytest.mark.asyncio
async def test_seed_upsert_preserves_current_value_on_conflict():
    """The ON CONFLICT clause must NOT include current_value in the
    UPDATE SET list — operators set current_value, developers set
    description / type / category. Re-seeding preserves operator state."""
    session = MagicMock()
    session.execute = AsyncMock()

    await seed_config_keys(session)
    # Grab the SQL string from the first call
    first_call = session.execute.call_args_list[0]
    sql_arg = first_call.args[0]
    sql_text = getattr(sql_arg, "text", str(sql_arg))

    assert "ON CONFLICT (service, key) DO UPDATE SET" in sql_text
    # These developer-owned fields ARE updated
    assert "type = EXCLUDED.type" in sql_text
    assert "default_value = EXCLUDED.default_value" in sql_text
    assert "description = EXCLUDED.description" in sql_text
    assert "category = EXCLUDED.category" in sql_text
    # The operator-owned current_value field is NOT in the UPDATE list
    assert "current_value = EXCLUDED.current_value" not in sql_text
