#!/usr/bin/env python3
"""
Paper trading entry point.

Starts the trading engine in paper mode, regardless of .env setting.
All trades are simulated — no real orders are placed.

Usage:
    python scripts/paper_trade.py
"""

import asyncio
import os
import sys

# Force paper mode
os.environ["PAPER_MODE"] = "true"

# Add engine to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

from main import main  # noqa: E402


if __name__ == "__main__":
    print("🔸 Starting in PAPER TRADE mode")
    print("   No real orders will be placed.")
    print("   Press Ctrl+C to stop.\n")
    asyncio.run(main())
