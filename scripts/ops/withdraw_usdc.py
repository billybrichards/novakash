"""
Withdraw USDC from Polymarket proxy wallet to an external address.

Background
==========
USDC.e sits in the Polymarket proxy wallet (``0x181D...``). This script
transfers it to a destination address (e.g. your MetaMask) via the same
``factory.proxy([(1, USDC, 0, transfer_calldata)])`` envelope used by
``onchain_redeem.py`` and ``wrap_usdc_pusd.py``.

The proxy can only be called by the EOA signer (``POLY_PRIVATE_KEY``).
No one else can move funds.

Safety
======
- **Dry-run by default** — shows what WOULD happen without sending.
- **Cross-RPC balance check** — 2-of-3 consensus before any tx.
- **Amount validation** — rejects if requested > available balance.
- **Destination validation** — rejects zero address and proxy-to-self.
- **Only calls USDC.transfer** — cannot trade, bridge, or approve.
- **Montreal only** — never run from OpenClaw VPS.

Usage
=====
On Montreal:

    cd /home/novakash/novakash
    set -a && source engine/.env && set +a

    # Dry run (preview only):
    python3 scripts/ops/withdraw_usdc.py \\
        --to 0xYourMetaMaskAddress \\
        --amount-usdc 10.00

    # Execute for real:
    python3 scripts/ops/withdraw_usdc.py \\
        --to 0xYourMetaMaskAddress \\
        --amount-usdc 10.00 \\
        --execute

    # Withdraw ALL USDC in proxy:
    python3 scripts/ops/withdraw_usdc.py \\
        --to 0xYourMetaMaskAddress \\
        --all \\
        --execute
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from eth_account import Account
from eth_utils import keccak
from web3 import Web3


# --- Polygon mainnet contract addresses ----------------------------------

USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
PROXY_FACTORY = Web3.to_checksum_address(
    "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
)

ZERO_ADDRESS = "0x" + "0" * 40


# --- Encoding helpers (same as wrap_usdc_pusd.py) ------------------------

def _selector(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def _addr_word(a: str) -> bytes:
    return bytes(12) + bytes.fromhex(a[2:].lower())


def _u256(n: int) -> bytes:
    return n.to_bytes(32, "big")


def encode_transfer(to: str, amount: int) -> bytes:
    """ERC20: transfer(address to, uint256 amount)."""
    return _selector("transfer(address,uint256)") + _addr_word(to) + _u256(amount)


def encode_proxy_calls(calls: list[tuple[int, str, int, bytes]]) -> bytes:
    """factory.proxy((uint8 op, address to, uint256 value, bytes data)[])."""
    from eth_abi import encode as abi_encode

    sel = _selector("proxy((uint8,address,uint256,bytes)[])")
    encoded = abi_encode(
        ["(uint8,address,uint256,bytes)[]"],
        [[(op, Web3.to_checksum_address(to), value, data) for op, to, value, data in calls]],
    )
    return sel + encoded


# --- ERC20 read helpers --------------------------------------------------

def erc20_balance_of(w3: Web3, token: str, owner: str) -> int:
    data = _selector("balanceOf(address)") + _addr_word(owner)
    out = w3.eth.call({"to": token, "data": data})
    return int.from_bytes(out, "big")


# --- Cross-RPC sanity (same as wrap_usdc_pusd.py) ------------------------

PUBLIC_RPCS = (
    os.environ.get("POLYGON_RPC_URL"),
    "https://polygon.gateway.tenderly.co",
    "https://1rpc.io/matic",
)


def cross_check_usdc_balance(proxy: str) -> int:
    """Read USDC balance from multiple RPCs. Return consensus value."""
    seen: dict[int, int] = {}
    for rpc in PUBLIC_RPCS:
        if not rpc:
            continue
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
            bal = erc20_balance_of(w3, USDC, proxy)
            seen[bal] = seen.get(bal, 0) + 1
        except Exception as exc:
            print(f"  [warn] {rpc}: {exc}")
    if not seen:
        raise RuntimeError("no RPC returned a USDC balance")
    for bal, n in seen.items():
        if n >= 2:
            return bal
    raise RuntimeError(
        f"RPC balances disagree (no 2-of-3 consensus): {seen}. "
        "Re-run after ~30s; transient cache lag."
    )


# --- Main ----------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Withdraw USDC from Polymarket proxy to an external wallet."
    )
    parser.add_argument(
        "--to",
        required=True,
        help="Destination address (e.g. your MetaMask on Polygon).",
    )
    parser.add_argument(
        "--amount-usdc",
        type=float,
        default=0.0,
        help="Amount in human dollars (e.g. 10.00). Use --all for full balance.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Withdraw entire USDC balance from proxy.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit the tx. Without this, runs dry-run only.",
    )
    parser.add_argument(
        "--rpc",
        default=os.environ.get("POLYGON_RPC_URL")
        or "https://polygon.gateway.tenderly.co",
        help="Polygon RPC for tx submission.",
    )
    args = parser.parse_args()

    # ---- validate destination ----
    try:
        dest = Web3.to_checksum_address(args.to)
    except Exception:
        print(f"ERROR: invalid destination address: {args.to}")
        return 2
    if dest == ZERO_ADDRESS:
        print("ERROR: cannot send to zero address.")
        return 2

    # ---- creds ----
    pk = os.environ.get("POLY_PRIVATE_KEY")
    funder = os.environ.get("POLY_FUNDER_ADDRESS")
    if not pk or not funder:
        print("ERROR: POLY_PRIVATE_KEY and POLY_FUNDER_ADDRESS must be set in env.")
        return 2
    acct = Account.from_key(pk)
    proxy = Web3.to_checksum_address(funder)

    if dest.lower() == proxy.lower():
        print("ERROR: destination is the proxy itself — nothing to do.")
        return 2

    print(f"EOA (signer):   {acct.address}")
    print(f"Proxy (from):   {proxy}")
    print(f"Destination:    {dest}")

    # ---- web3 ----
    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        print(f"ERROR: cannot connect to RPC {args.rpc}")
        return 2

    # ---- balances (cross-checked) ----
    print("\n=== current balances ===")
    usdc_proxy = cross_check_usdc_balance(proxy)
    print(f"  USDC in proxy:       ${usdc_proxy / 1e6:.6f}")
    try:
        usdc_dest = erc20_balance_of(w3, USDC, dest)
        print(f"  USDC at destination: ${usdc_dest / 1e6:.6f}")
    except Exception:
        usdc_dest = None
        print(f"  USDC at destination: (could not read)")

    # ---- pick amount ----
    if args.all:
        amount = usdc_proxy
    elif args.amount_usdc > 0:
        amount = int(round(args.amount_usdc * 1_000_000))
    else:
        print("\nERROR: specify --amount-usdc or --all")
        return 2

    if amount <= 0:
        print("\nNothing to withdraw (proxy USDC balance is zero).")
        return 1
    if amount > usdc_proxy:
        print(f"\nERROR: requested ${amount / 1e6:.2f} but proxy holds only ${usdc_proxy / 1e6:.2f}.")
        return 2

    human_amount = amount / 1e6
    print(f"\n{'=' * 60}")
    print(f"  WITHDRAW: ${human_amount:.6f} USDC")
    print(f"  FROM:     {proxy} (Polymarket proxy)")
    print(f"  TO:       {dest}")
    print(f"{'=' * 60}")

    # ---- build proxy call ----
    transfer_data = encode_transfer(dest, amount)
    calls = [(1, USDC, 0, transfer_data)]

    print(f"\n  proxy.exec: USDC.transfer({dest}, ${human_amount:.6f})")

    if not args.execute:
        print("\n[dry-run] Not submitting. Re-run with --execute to send.")
        return 0

    # ---- confirm ----
    print(f"\n⚠️  This will move ${human_amount:.2f} USDC to {dest}.")
    print("    Press Enter to confirm, or Ctrl+C to abort.")
    try:
        input("    > ")
    except KeyboardInterrupt:
        print("\n\nAborted.")
        return 1

    # ---- build outer tx + submit ----
    outer = encode_proxy_calls(calls)
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    gas_price = w3.eth.gas_price
    tx = {
        "from": acct.address,
        "to": PROXY_FACTORY,
        "value": 0,
        "data": "0x" + outer.hex(),
        "gas": 200_000,
        "maxFeePerGas": gas_price * 2,
        "maxPriorityFeePerGas": min(gas_price, 30 * 10**9),
        "nonce": nonce,
        "chainId": 137,
        "type": 2,
    }

    print(f"\nSubmitting tx (nonce={nonce}, gas_price={gas_price / 1e9:.2f} gwei)...")
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    txh = w3.eth.send_raw_transaction(raw)
    print(f"  tx: 0x{txh.hex()}")
    print(f"  https://polygonscan.com/tx/0x{txh.hex()}")

    # ---- wait + verify ----
    print("\nWaiting up to 90s for receipt...")
    deadline = time.time() + 90
    receipt = None
    while time.time() < deadline:
        try:
            receipt = w3.eth.get_transaction_receipt(txh)
            if receipt:
                break
        except Exception:
            pass
        time.sleep(3)
    if not receipt:
        print("  no receipt within timeout; check Polygonscan for status.")
        return 1
    if receipt.status != 1:
        print(f"  ❌ tx REVERTED in block {receipt.blockNumber}.")
        return 1
    print(f"  ✅ confirmed in block {receipt.blockNumber}, gas used {receipt.gasUsed}")

    # ---- post-state ----
    print("\n=== balances after ===")
    usdc_proxy_after = cross_check_usdc_balance(proxy)
    print(f"  USDC in proxy:       ${usdc_proxy_after / 1e6:.6f}  (Δ ${(usdc_proxy_after - usdc_proxy) / 1e6:+.6f})")
    try:
        usdc_dest_after = erc20_balance_of(w3, USDC, dest)
        delta_dest = (usdc_dest_after - usdc_dest) / 1e6 if usdc_dest is not None else usdc_dest_after / 1e6
        print(f"  USDC at destination: ${usdc_dest_after / 1e6:.6f}  (Δ ${delta_dest:+.6f})")
    except Exception:
        print(f"  USDC at destination: (could not read)")

    print(f"\n✅ Withdrawal complete: ${human_amount:.2f} USDC → {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
