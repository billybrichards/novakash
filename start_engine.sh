#!/bin/bash
cd /home/novakash/novakash/engine
set -a
source .env
set +a
echo "Starting Novakash Engine from Montreal (CA)..."
echo "Mode: PAPER_MODE=$PAPER_MODE"
echo "Assets: $FIVE_MIN_ASSETS"
echo "Delta: $FIVE_MIN_MIN_DELTA_PCT"
python3 -m engine.main
