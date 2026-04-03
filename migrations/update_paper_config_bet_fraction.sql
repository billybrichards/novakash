-- Update Paper Config v1 to use BET_FRACTION=0.20
-- Run this once to fix the bet_fraction override issue

UPDATE trading_configs
SET config = jsonb_set(
    config,
    '{bet_fraction}',
    '"0.20"'
)
WHERE mode = 'paper'
  AND is_active = TRUE
  AND name = 'Paper Config v1';

-- Verify the update
SELECT id, name, mode, config->'bet_fraction' as bet_fraction
FROM trading_configs
WHERE mode = 'paper' AND is_active = TRUE;
