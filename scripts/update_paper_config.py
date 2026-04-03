#!/usr/bin/env python3
"""
Update Paper Config v1 to use BET_FRACTION=0.20
"""
import asyncio
import json
import os

async def main():
    try:
        import asyncpg
    except ImportError:
        print("asyncpg not available in Railway shell")
        return

    # Get DATABASE_URL from env
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        # Try reading from .env
        with open('.env') as f:
            for line in f:
                if line.startswith('DATABASE_URL='):
                    dsn = line.split('=', 1)[1].strip()
                    break
    
    if not dsn:
        print("DATABASE_URL not found")
        return
    
    # Strip SQLAlchemy prefix if present
    if dsn.startswith("postgresql+asyncpg://"):
        dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    
    print(f"Connecting to DB...")
    conn = await asyncpg.connect(dsn)
    
    try:
        # Get current config
        row = await conn.fetchrow(
            "SELECT id, name, config FROM trading_configs WHERE mode = 'paper' AND is_active = TRUE"
        )
        
        if not row:
            print("No active paper config found")
            return
        
        config_id = row['id']
        config_name = row['name']
        config = row['config'] if isinstance(row['config'], dict) else json.loads(row['config'] or '{}')
        
        print(f"Current config: {config_name} (id={config_id})")
        print(f"Current bet_fraction: {config.get('bet_fraction', 'NOT SET')}")
        
        # Update bet_fraction
        config['bet_fraction'] = '0.20'
        
        # Update DB
        await conn.execute(
            "UPDATE trading_configs SET config = $1 WHERE id = $2",
            json.dumps(config),
            config_id
        )
        
        print(f"✓ Updated {config_name} bet_fraction to 0.20")
        
        # Verify
        row = await conn.fetchrow(
            "SELECT config FROM trading_configs WHERE id = $1",
            config_id
        )
        updated_config = row['config'] if isinstance(row['config'], dict) else json.loads(row['config'] or '{}')
        print(f"✓ Verified: bet_fraction = {updated_config.get('bet_fraction')}")
        
    finally:
        await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
