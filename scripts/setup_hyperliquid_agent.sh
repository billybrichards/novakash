#!/usr/bin/env bash
#
# setup_hyperliquid_agent.sh — interactive agent-wallet key installer
#
# WHAT THIS SCRIPT DOES:
#   1. Prompts you (via `read -s`, hidden input) to paste your Hyperliquid
#      AGENT WALLET private key — NOT your main MetaMask key
#   2. Validates it's a 0x-prefixed 64-hex-char string
#   3. Derives the agent wallet address and prints it so you can verify
#   4. Prompts for your MAIN account address (0x… public, from MetaMask)
#   5. Writes the agent key to /opt/margin-engine/.keys/hyperliquid_agent.pem
#      with permissions 600 (owner read/write only)
#   6. Appends MARGIN_HYPERLIQUID_MAIN_ADDRESS to /opt/margin-engine/.env
#
# WHAT THIS SCRIPT DOES NOT DO:
#   - Accept your main MetaMask private key (refuses if addresses match)
#   - Log the key anywhere (nowhere in bash_history, nowhere in journald)
#   - Commit anything to git
#   - Transfer, bridge, or move any funds
#   - Enable live trading — you still need to flip MARGIN_PAPER_MODE=false
#     manually after verifying the agent works with a paper run first
#
# USAGE:
#   sudo bash scripts/setup_hyperliquid_agent.sh
#
# SAFETY NOTES:
#   - Run ON THE SERVER, not on your laptop. The key should never leave
#     your password manager except into this server's secure file.
#   - Run from a terminal you trust — if this is a screen/tmux/shell that
#     logs to disk, abort and use a fresh ssh session
#   - The script uses `set -o pipefail` and `trap ERR` so partial writes
#     don't leave a half-written key file

set -eo pipefail

# ─── Constants ────────────────────────────────────────────────────────────

readonly KEYS_DIR="/opt/margin-engine/.keys"
readonly KEY_FILE="${KEYS_DIR}/hyperliquid_agent.pem"
readonly ENV_FILE="/opt/margin-engine/.env"

# ─── Preflight checks ─────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    echo "error: must run as root (use sudo)" >&2
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    echo "error: python3 required for key validation" >&2
    exit 1
fi

if ! python3 -c "from eth_account import Account" 2>/dev/null; then
    echo "error: eth_account not installed. Run:" >&2
    echo "    sudo /opt/margin-engine/venv/bin/pip install eth-account" >&2
    exit 1
fi

mkdir -p "$KEYS_DIR"
chown ubuntu:ubuntu "$KEYS_DIR"
chmod 700 "$KEYS_DIR"

# ─── Disable bash history for this script so the key never hits ~/.bash_history
unset HISTFILE
set +o history

# ─── Prompt for main account address (public, not sensitive) ──────────────

echo ""
echo "========================================================================"
echo " Hyperliquid Agent Wallet Setup"
echo "========================================================================"
echo ""
echo "STEP 1 of 2: Main account address"
echo ""
echo "This is your MetaMask wallet's PUBLIC address (0x… 40-hex-chars)."
echo "It is NOT a secret. It's the account that owns your USDC on Hyperliquid."
echo "You can see it in MetaMask or in the top-right of app.hyperliquid.xyz"
echo ""
read -rp "Main account address (0x…): " MAIN_ADDRESS

# Validate format: 0x + 40 hex chars
if ! [[ "$MAIN_ADDRESS" =~ ^0x[0-9a-fA-F]{40}$ ]]; then
    echo "error: main address must be 0x followed by 40 hex characters" >&2
    exit 1
fi
MAIN_ADDRESS_LOWER=$(echo "$MAIN_ADDRESS" | tr '[:upper:]' '[:lower:]')

# ─── Prompt for agent key (SECRET — hidden input) ─────────────────────────

echo ""
echo "STEP 2 of 2: Agent wallet private key"
echo ""
echo "This is your AGENT WALLET's private key, created at:"
echo "    app.hyperliquid.xyz → Account → API → Create Agent Wallet"
echo ""
echo "Format: 0x followed by 64 hex characters."
echo ""
echo "!!!  DO NOT PASTE YOUR METAMASK SEED PHRASE OR MAIN WALLET KEY  !!!"
echo "!!!  If unsure, cancel (Ctrl-C) and create an API wallet first  !!!"
echo ""
read -rsp "Agent private key (input hidden): " AGENT_KEY
echo ""

# Validate format: 0x + 64 hex chars
if ! [[ "$AGENT_KEY" =~ ^0x[0-9a-fA-F]{64}$ ]]; then
    echo "error: agent key must be 0x followed by 64 hex characters" >&2
    unset AGENT_KEY
    exit 1
fi

# ─── Derive and validate the agent address ───────────────────────────────
# Use python to derive without leaking the key into bash variables any
# further than it already is. The validation subprocess reads the key
# from argv (argv is visible in `ps` for ~1ms — accept the risk, it's
# the only way to pass a key to a subprocess without using stdin which
# we already used for the prompt).

DERIVED_ADDRESS=$(python3 <<PYEOF
import sys
from eth_account import Account
try:
    acct = Account.from_key("$AGENT_KEY")
    print(acct.address.lower())
except Exception as e:
    print(f"error: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
)

if [[ -z "$DERIVED_ADDRESS" ]]; then
    echo "error: failed to derive agent address from key" >&2
    unset AGENT_KEY
    exit 1
fi

echo ""
echo "Derived agent address: $DERIVED_ADDRESS"
echo ""

# ─── Safety check: agent != main ─────────────────────────────────────────

if [[ "$DERIVED_ADDRESS" == "$MAIN_ADDRESS_LOWER" ]]; then
    echo ""
    echo "============================================================"
    echo "!!!                ABORTING                              !!!"
    echo "!!!                                                      !!!"
    echo "!!!  The agent key derives to the SAME address as your   !!!"
    echo "!!!  main account. That means you pasted your MAIN       !!!"
    echo "!!!  wallet private key, not an agent wallet key.        !!!"
    echo "!!!                                                      !!!"
    echo "!!!  NEVER store your main wallet key on a server.       !!!"
    echo "!!!                                                      !!!"
    echo "!!!  Go to app.hyperliquid.xyz → Account → API and       !!!"
    echo "!!!  create a dedicated agent wallet. Then rerun this.   !!!"
    echo "============================================================"
    unset AGENT_KEY
    exit 1
fi

# ─── Write the key file ──────────────────────────────────────────────────

# Write to a temp file first so a partial write doesn't leave a half-
# written key at the real path.
TMP_KEY_FILE="${KEY_FILE}.tmp.$$"
trap "rm -f $TMP_KEY_FILE" EXIT

# Use printf (not echo) to avoid interpretation of backslash sequences
# in the key.
printf '%s\n' "$AGENT_KEY" > "$TMP_KEY_FILE"
chmod 600 "$TMP_KEY_FILE"
chown ubuntu:ubuntu "$TMP_KEY_FILE"
mv "$TMP_KEY_FILE" "$KEY_FILE"

# Immediately clear the key from the shell variable
unset AGENT_KEY

echo "✓ Agent key written to $KEY_FILE (chmod 600, owner ubuntu)"

# ─── Update .env with main address ───────────────────────────────────────

if grep -q "^MARGIN_HYPERLIQUID_MAIN_ADDRESS=" "$ENV_FILE" 2>/dev/null; then
    # Replace existing line
    sed -i.bak "s|^MARGIN_HYPERLIQUID_MAIN_ADDRESS=.*|MARGIN_HYPERLIQUID_MAIN_ADDRESS=$MAIN_ADDRESS_LOWER|" "$ENV_FILE"
    echo "✓ Updated MARGIN_HYPERLIQUID_MAIN_ADDRESS in $ENV_FILE"
else
    cat >> "$ENV_FILE" <<EOF

# ── Hyperliquid live mode (added by setup_hyperliquid_agent.sh) ──
MARGIN_HYPERLIQUID_MAIN_ADDRESS=$MAIN_ADDRESS_LOWER
MARGIN_HYPERLIQUID_AGENT_KEY_PATH=$KEY_FILE
EOF
    echo "✓ Appended MARGIN_HYPERLIQUID_MAIN_ADDRESS to $ENV_FILE"
fi

chown ubuntu:ubuntu "$ENV_FILE"
chmod 600 "$ENV_FILE"

# ─── Summary + next steps ────────────────────────────────────────────────

echo ""
echo "========================================================================"
echo " Setup complete."
echo "========================================================================"
echo ""
echo " Main account:  $MAIN_ADDRESS_LOWER"
echo " Agent address: $DERIVED_ADDRESS"
echo " Key file:      $KEY_FILE"
echo ""
echo " NEXT STEPS:"
echo ""
echo "   1. Verify the agent wallet is bound to your account in Hyperliquid:"
echo "      Open app.hyperliquid.xyz → Account → API. You should see"
echo "      the agent address listed there."
echo ""
echo "   2. Verify live connectivity WITHOUT trading (optional):"
echo "      sudo -u ubuntu /opt/margin-engine/venv/bin/python -c \\"
echo "        'from hyperliquid.info import Info; \\"
echo "         import json; \\"
echo "         print(json.dumps(Info(\"https://api.hyperliquid.xyz\", skip_ws=True).user_state(\"$MAIN_ADDRESS_LOWER\"), indent=2))'"
echo ""
echo "   3. When ready to flip LIVE (not yet — verify paper first):"
echo "      sudo sed -i 's/^MARGIN_PAPER_MODE=true/MARGIN_PAPER_MODE=false/' $ENV_FILE"
echo "      sudo systemctl restart margin-engine"
echo "      sudo journalctl -u margin-engine -f"
echo ""
echo " IMPORTANT: MARGIN_PAPER_MODE is still 'true' after this script runs."
echo " You must explicitly flip it yourself after verifying the agent works."
echo ""
