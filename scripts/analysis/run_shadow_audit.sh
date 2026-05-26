#!/bin/bash
# Wrapper to run shadow_audit_realistic_fills.py with proper DB credentials
# Usage: bash scripts/analysis/run_shadow_audit.sh [YYYY-MM-DD]

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Load credentials
source "$PROJECT_ROOT/scripts/cross-compare/_lib.sh"

# Export for Python
export DB_HOST
export DB_PORT
export DB_USER
export DB_PASS
export DB_NAME

DATE="${1:-2026-05-25}"
echo "Running shadow audit for $DATE..."

PYTHONUNBUFFERED=1 python3 "$SCRIPT_DIR/shadow_audit_realistic_fills.py" --date "$DATE"
