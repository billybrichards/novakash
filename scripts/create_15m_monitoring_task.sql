-- Create audit task for 15m strategy paper mode monitoring
-- Run this after deploying with FIFTEEN_MIN_ENABLED=true

INSERT INTO audit_tasks_dev (
  task_key,
  task_type,
  source,
  title,
  status,
  priority,
  category,
  severity,
  payload,
  metadata,
  dedupe_key,
  created_by
) VALUES (
  'monitor-15m-paper',
  'monitoring',
  'system',
  '15m Strategy Paper Mode Monitoring',
  'OPEN',
  90,
  'deployment',
  'HIGH',
  '{
    "deployment_env": "paper",
    "strategies": ["v15m_down_only", "v15m_up_asian", "v15m_up_basic", "v15m_fusion", "v15m_gate"],
    "monitoring_duration_hours": 168,
    "required_windows": 50,
    "success_criteria": {
      "all_strategies_evaluate": true,
      "clob_data_present": true,
      "v4_snapshot_15m_populated": true,
      "decisions_persist_to_db": true,
      "telegram_alerts_sent": true,
      "no_strategy_crashes": true,
      "reconciliation_works": true,
      "frontend_monitor_loads": true
    },
    "verification_queries": {
      "strategy_decisions": "SELECT strategy_id, COUNT(*) FROM strategy_decisions WHERE timeframe=''15m'' GROUP BY strategy_id",
      "clob_ticks": "SELECT COUNT(*) FROM ticks_clob WHERE timeframe=''15m''",
      "window_snapshots": "SELECT COUNT(*) FROM window_snapshots WHERE timeframe=''15m''",
      "clob_book_snapshots": "SELECT COUNT(*) FROM clob_book_snapshots WHERE timeframe=''15m''"
    },
    "alert_checks": [
      "15m window CLOSING alerts in Telegram",
      "Strategy decision summaries for 15m windows",
      "No error alerts for 15m evaluation",
      "CLOB data availability confirmed in alerts"
    ]
  }',
  '{
    "deployment_date": "2026-04-14",
    "timeframe": "15m",
    "monitor_url": "/polymarket/15min",
    "telegram_channel": "#btc-trader-alerts",
    "expected_windows_per_day": 96,
    "data_retention_days": 30,
    "related_pr": "https://github.com/billybrichards/novakash/pull/172"
  }',
  'monitor-15m-paper-2026-04-14',
  'system'
) ON CONFLICT (dedupe_key) DO UPDATE SET
  updated_at = NOW(),
  status = EXCLUDED.status,
  priority = EXCLUDED.priority,
  payload = EXCLUDED.payload,
  metadata = EXCLUDED.metadata
RETURNING id, task_key, title, status, priority;
