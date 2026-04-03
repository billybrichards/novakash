"""
Create v4.1 trading config in the database.
Deactivates old configs and creates a fresh one with correct v4.1 values.
Run via: railway run -- python scripts/create_v41_config.py
"""
import asyncio
import json
import os

async def main():
    import asyncpg
    
    db_url = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        return
    
    conn = await asyncpg.connect(db_url)
    
    # 1. Show current configs
    print("=== CURRENT CONFIGS ===")
    rows = await conn.fetch("SELECT id, name, mode, is_active, updated_at FROM trading_configs ORDER BY updated_at DESC")
    for r in rows:
        print(f"  ID={r['id']} | {r['name']} | mode={r['mode']} | active={r['is_active']} | updated={r['updated_at']}")
    
    # 2. Deactivate ALL existing paper configs
    deactivated = await conn.execute("UPDATE trading_configs SET is_active = FALSE WHERE mode = 'paper' AND is_active = TRUE")
    print(f"\nDeactivated old configs: {deactivated}")
    
    # 3. Create v4.1 config with correct values
    v41_config = {
        # VPIN
        "vpin_bucket_size_usd": 500000,
        "vpin_lookback_buckets": 50,
        "vpin_informed_threshold": 0.55,
        "vpin_cascade_threshold": 0.70,
        
        # 5-Min Strategy
        "five_min_vpin_gate": 0.45,
        "five_min_min_delta_pct": 0.08,
        "five_min_cascade_min_delta_pct": 0.03,
        
        # Risk
        "starting_bankroll": 500.0,
        "bet_fraction": 0.05,
        "max_position_usd": 500.0,
        "max_drawdown_pct": 0.45,
        "daily_loss_limit": 50.0,
        
        # Fees
        "polymarket_fee_mult": 0.072,
        "opinion_fee_mult": 0.04,
        "preferred_venue": "opinion",
        
        # Arb
        "arb_min_spread": 0.001,
        "arb_max_position": 50.0,
        "arb_max_execution_ms": 500,
        "enable_arb_strategy": True,
        
        # Cascade
        "cascade_cooldown_seconds": 300,
        "cascade_min_liq_usd": 100000,
        "enable_cascade_strategy": True,
    }
    
    await conn.execute("""
        INSERT INTO trading_configs (name, mode, is_active, config, created_at, updated_at)
        VALUES ($1, $2, $3, $4, NOW(), NOW())
    """, "Paper Config v4.1 — Regime-Aware", "paper", True, json.dumps(v41_config))
    
    print(f"\n✅ Created 'Paper Config v4.1 — Regime-Aware' (active)")
    
    # 4. Verify
    print("\n=== CONFIGS AFTER UPDATE ===")
    rows = await conn.fetch("SELECT id, name, mode, is_active, updated_at FROM trading_configs ORDER BY updated_at DESC")
    for r in rows:
        status = "🟢 ACTIVE" if r['is_active'] else "⚪ inactive"
        print(f"  {status} | {r['name']} | mode={r['mode']}")
    
    # 5. Verify the new config values
    row = await conn.fetchrow("SELECT config FROM trading_configs WHERE is_active = TRUE AND mode = 'paper' ORDER BY updated_at DESC LIMIT 1")
    if row:
        cfg = json.loads(row['config']) if isinstance(row['config'], str) else row['config']
        print("\n=== v4.1 CONFIG VALUES ===")
        for k, v in sorted(cfg.items()):
            print(f"  {k}: {v}")
    
    await conn.close()
    print("\n✅ Done. DB config now matches v4.1 spec.")

asyncio.run(main())
