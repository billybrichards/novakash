"""Activate config #25 directly in the DB. Run via railway run."""
import asyncio, os

async def main():
    import asyncpg
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(db_url)
    
    # Deactivate all paper configs
    await conn.execute("UPDATE trading_configs SET is_active = FALSE WHERE mode = 'paper'")
    
    # Activate v4.1
    await conn.execute("UPDATE trading_configs SET is_active = TRUE, updated_at = NOW() WHERE id = 25")
    
    # Update system_state
    await conn.execute("UPDATE system_state SET active_paper_config_id = 25, updated_at = NOW() WHERE id = 1")
    
    # Verify
    row = await conn.fetchrow("SELECT id, name, is_active FROM trading_configs WHERE id = 25")
    print(f"Config #{row['id']}: {row['name']} | active={row['is_active']}")
    
    await conn.close()
    print("✅ v4.1 activated")

asyncio.run(main())
