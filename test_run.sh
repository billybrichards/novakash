#!/bin/bash
cd /root/.openclaw/workspace-novakash/novakash
python3 scripts/analyze_15min_simple.py
echo "Status: $?"
