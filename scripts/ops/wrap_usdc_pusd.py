"""
One-shot USDC -> pUSD on-ramp via Polymarket's CollateralOnramp.

Background
==========
Polymarket migrated CTF Exchange V1 -> V2 on 2026-04-28. V2 settles in
**pUSD** (`0xC011a7E1...`), not USDC directly. Existing wallets that hold
USDC must wrap it once via the CollateralOnramp contract before the V2
matcher accepts orders backed by their balance.

Flow (executed by the user's Polymarket Magic-link proxy):
  1. proxy.exec USDC.approve(CollateralOnramp, MAX) — one-time
  2. proxy.exec CollateralOnramp.wrap(USDC, proxy, amount) — pulls USDC,
     mints pUSD 1:1 to the proxy
  3. (Optional) proxy.exec pUSD.approve(V2_CTF_EXCHANGE, MAX)
     and pUSD.approve(V2_NEGRISK_EXCHANGE, MAX) so the V2 matcher can
     settle fills. Skipped if `--skip-allowances` is passed; SDK's
     `update_balance_allowance` will set them lazily.

Pattern is the same `factory.proxy([(1, target, 0, calldata)])` envelope
already proven for redemption (see `tasks/lessons.md` and the redeem
helper). Each tx costs ~0.005 MATIC.

Usage
=====
On Montreal (only; Polymarket reads from Mac are forbidden per
`feedback_no_local_polymarket.md`, but Polygon RPC reads/writes are fine):

    cd /home/novakash/novakash
    set -a && source engine/.env && set +a
    python3 scripts/ops/wrap_usdc_pusd.py --amount-usdc 290 --execute

Pass `--amount-usdc` in human dollars (USDC has 6 decimals; the script
converts). Default is 0 = "wrap whatever the proxy currently holds".

Add `--dry-run` (default) to preview the calls without sending. Add
`--execute` to actually submit. Add `--skip-allowances` to only do the
USDC->pUSD wrap and let the SDK handle pUSD allowances later.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

from eth_account import Account
from eth_utils import keccak
from web3 import Web3


# --- Polygon mainnet contract addresses ----------------------------------

USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
PUSD = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
ONRAMP = Web3.to_checksum_address("0x93070a847efEf7F70739046A929D47a521F5B8ee")
PROXY_FACTORY = Web3.to_checksum_address(
    "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
)
V2_CTF_EXCHANGE = Web3.to_checksum_address(
    "0xE111180000d2663C0091e4f400237545B87B996B"
)
V2_NEGRISK_EXCHANGE = Web3.to_checksum_address(
    "0xe2222d279d744050d28e00520010520000310F59"
)

UINT256_MAX = (1 << 256) - 1


# --- Encoding helpers ----------------------------------------------------

def _selector(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def _addr_word(a: str) -> bytes:
    return bytes(12) + bytes.fromhex(a[2:].lower())


def _u256(n: int) -> bytes:
    return n.to_bytes(32, "big")


def encode_approve(spender: str, amount: int) -> bytes:
    """ERC20: approve(spender, amount)."""
    return _selector("approve(address,uint256)") + _addr_word(spender) + _u256(amount)


def encode_wrap(token_in: str, recipient: str, amount: int) -> bytes:
    """CollateralOnramp.wrap(address tokenIn, address to, uint256 amount).

    Pulls `amount` of `tokenIn` from msg.sender via transferFrom, mints
    pUSD 1:1 to `to`. Selector `0x62355638` confirmed via 4byte and
    on-chain simulation (`TransferFromFailed()` revert when allowance
    is missing — same selector pattern as Solady).
    """
    return (
        _selector("wrap(address,address,uint256)")
        + _addr_word(token_in)
        + _addr_word(recipient)
        + _u256(amount)
    )


def encode_proxy_calls(calls: list[tuple[int, str, int, bytes]]) -> bytes:
    """factory.proxy((uint8 op, address to, uint256 value, bytes data)[]).

    Each tuple is one operation the proxy will execute. ``op`` = 1 = CALL.

    Defers to ``eth_abi.encode`` because the array element is a *dynamic*
    tuple (it contains the dynamic ``bytes data`` field), so Solidity's
    layout requires an offset-per-element header before the structs
    themselves -- not the head/tail-inline layout used for arrays of
    static-only tuples. Hand-rolling the offset table is fiddly and we
    already depend on web3.py.
    """
    from eth_abi import encode as abi_encode

    sel = _selector("proxy((uint8,address,uint256,bytes)[])")
    encoded = abi_encode(
        ["(uint8,address,uint256,bytes)[]"],
        [[(op, Web3.to_checksum_address(to), value, data) for op, to, value, data in calls]],
    )
    return sel + encoded


# --- ERC20 read helpers --------------------------------------------------

ERC20_BAL_OF = "balanceOf(address)"
ERC20_ALLOWANCE = "allowance(address,address)"


def erc20_balance_of(w3: Web3, token: str, owner: str) -> int:
    data = _selector(ERC20_BAL_OF) + _addr_word(owner)
    out = w3.eth.call({"to": token, "data": data})
    return int.from_bytes(out, "big")


def erc20_allowance(w3: Web3, token: str, owner: str, spender: str) -> int:
    data = _selector(ERC20_ALLOWANCE) + _addr_word(owner) + _addr_word(spender)
    out = w3.eth.call({"to": token, "data": data})
    return int.from_bytes(out, "big")


# --- Cross-RPC sanity ----------------------------------------------------

PUBLIC_RPCS = (
    os.environ.get("POLYGON_RPC_URL"),
    "https://polygon.gateway.tenderly.co",
    "https://1rpc.io/matic",
)


def cross_check_usdc_balance(proxy: str) -> int:
    """Read USDC balance from multiple RPCs. Return the consensus value or
    raise if they disagree. Mirrors `wallet_truth.py` defensive pattern.
    """
    seen: dict[int, int] = {}
    for rpc in PUBLIC_RPCS:
        if not rpc:
            continue
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
            bal = erc20_balance_of(w3, USDC, proxy)
            seen[bal] = seen.get(bal, 0) + 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] {rpc}: {exc}")
    if not seen:
        raise RuntimeError("no RPC returned a USDC balance")
    # Need ≥2 agreeing nodes
    for bal, n in seen.items():
        if n >= 2:
            return bal
    raise RuntimeError(
        f"RPC balances disagree (no 2-of-3 consensus): {seen}. "
        "Re-run after ~30s; transient cache lag."
    )


# --- Build + submit ------------------------------------------------------

def build_calls(
    proxy: str,
    amount: int,
    do_allowances: bool,
    *,
    current_usdc_allowance: int,
) -> list[tuple[int, str, int, bytes]]:
    """Return the list of (op, to, value, data) tuples for proxy.exec.

    Skips USDC.approve if existing allowance already covers the amount.
    Always wraps. Optionally approves pUSD to V2 exchanges.
    """
    calls: list[tuple[int, str, int, bytes]] = []

    if current_usdc_allowance < amount:
        calls.append((1, USDC, 0, encode_approve(ONRAMP, UINT256_MAX)))

    calls.append((1, ONRAMP, 0, encode_wrap(USDC, proxy, amount)))

    if do_allowances:
        calls.append((1, PUSD, 0, encode_approve(V2_CTF_EXCHANGE, UINT256_MAX)))
        calls.append((1, PUSD, 0, encode_approve(V2_NEGRISK_EXCHANGE, UINT256_MAX)))

    return calls


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--amount-usdc",
        type=float,
        default=0.0,
        help="Human-dollar amount of USDC to wrap. 0 = whole proxy balance.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit the tx. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--skip-allowances",
        action="store_true",
        help=(
            "Skip the pUSD->V2_exchange approvals. Use when you'd rather "
            "let the SDK's update_balance_allowance handle them lazily."
        ),
    )
    parser.add_argument(
        "--rpc",
        default=os.environ.get("POLYGON_RPC_URL")
        or "https://polygon.gateway.tenderly.co",
        help="Polygon RPC for tx submission. Reads still cross-check.",
    )
    args = parser.parse_args()

    # ---- creds ----
    pk = os.environ.get("POLY_PRIVATE_KEY")
    funder = os.environ.get("POLY_FUNDER_ADDRESS")
    if not pk or not funder:
        print("ERROR: POLY_PRIVATE_KEY and POLY_FUNDER_ADDRESS must be set in env.")
        return 2
    acct = Account.from_key(pk)
    proxy = Web3.to_checksum_address(funder)
    print(f"EOA:   {acct.address}")
    print(f"Proxy: {proxy}")

    # ---- web3 ----
    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        print(f"ERROR: cannot connect to RPC {args.rpc}")
        return 2

    # ---- balances + allowances (cross-checked) ----
    print("\n=== state before ===")
    usdc = cross_check_usdc_balance(proxy)
    print(f"  USDC.balanceOf(proxy):              {usdc / 1e6:>10.6f}")
    pusd = erc20_balance_of(w3, PUSD, proxy)
    print(f"  pUSD.balanceOf(proxy):              {pusd / 1e6:>10.6f}")
    usdc_allow = erc20_allowance(w3, USDC, proxy, ONRAMP)
    print(f"  USDC.allowance(proxy -> ONRAMP):    {('MAX' if usdc_allow >= UINT256_MAX // 2 else f'{usdc_allow / 1e6:.6f}'):>10}")
    pusd_allow_ctf = erc20_allowance(w3, PUSD, proxy, V2_CTF_EXCHANGE)
    pusd_allow_nr = erc20_allowance(w3, PUSD, proxy, V2_NEGRISK_EXCHANGE)
    print(f"  pUSD.allowance(proxy -> V2_CTF):    {('MAX' if pusd_allow_ctf >= UINT256_MAX // 2 else f'{pusd_allow_ctf / 1e6:.6f}'):>10}")
    print(f"  pUSD.allowance(proxy -> V2_NEGRISK):{('MAX' if pusd_allow_nr >= UINT256_MAX // 2 else f'{pusd_allow_nr / 1e6:.6f}'):>10}")

    # ---- pick amount ----
    if args.amount_usdc > 0:
        amount = int(round(args.amount_usdc * 1_000_000))
    else:
        amount = usdc
    if amount <= 0:
        print("\nNothing to wrap (proxy USDC balance is zero).")
        return 1
    if amount > usdc:
        print(f"\nERROR: requested {amount / 1e6} USDC but proxy holds only {usdc / 1e6}.")
        return 2
    print(f"\nWrap target: {amount / 1e6} USDC -> pUSD")

    # ---- build calls ----
    do_pusd = not args.skip_allowances
    calls = build_calls(
        proxy=proxy,
        amount=amount,
        do_allowances=do_pusd,
        current_usdc_allowance=usdc_allow,
    )
    print("\n=== planned proxy operations ===")
    for i, (op, to, val, data) in enumerate(calls, 1):
        sel = data[:4].hex()
        # decode for readability
        if sel == _selector("approve(address,uint256)").hex():
            spender = "0x" + data[16:36].hex()
            amt = int.from_bytes(data[36:68], "big")
            amt_str = "MAX" if amt >= UINT256_MAX // 2 else f"{amt / 1e6:.6f}"
            label = f"{('USDC' if to == USDC else 'pUSD' if to == PUSD else to)}.approve({spender}, {amt_str})"
        elif sel == _selector("wrap(address,address,uint256)").hex():
            tin = "0x" + data[16:36].hex()
            tto = "0x" + data[48:68].hex()
            amt = int.from_bytes(data[68:100], "big")
            label = f"ONRAMP.wrap({'USDC' if Web3.to_checksum_address(tin) == USDC else tin}, {'PROXY' if Web3.to_checksum_address(tto) == proxy else tto}, {amt / 1e6:.6f})"
        else:
            label = f"<raw 0x{sel}>"
        print(f"  [{i}] op={op} to={to} value={val} -- {label}")

    if not args.execute:
        print("\n[dry-run] not submitting. Re-run with --execute to send.")
        return 0

    # ---- build outer tx + submit ----
    outer = encode_proxy_calls(calls)
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    gas_price = w3.eth.gas_price
    tx = {
        "from": acct.address,
        "to": PROXY_FACTORY,
        "value": 0,
        "data": "0x" + outer.hex(),
        "gas": 800_000,  # 4 calls bundled — generous; refund returns surplus
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
        print(f"  tx REVERTED in block {receipt.blockNumber}.")
        return 1
    print(f"  ✅ confirmed in block {receipt.blockNumber}, gas used {receipt.gasUsed}")

    # ---- post-state ----
    print("\n=== state after ===")
    usdc2 = cross_check_usdc_balance(proxy)
    pusd2 = erc20_balance_of(w3, PUSD, proxy)
    print(f"  USDC.balanceOf(proxy):  {usdc2 / 1e6:.6f}  (Δ {(usdc2 - usdc) / 1e6:+.6f})")
    print(f"  pUSD.balanceOf(proxy):  {pusd2 / 1e6:.6f}  (Δ {(pusd2 - pusd) / 1e6:+.6f})")
    if do_pusd:
        a1 = erc20_allowance(w3, PUSD, proxy, V2_CTF_EXCHANGE)
        a2 = erc20_allowance(w3, PUSD, proxy, V2_NEGRISK_EXCHANGE)
        print(f"  pUSD allowance V2_CTF:    {'MAX' if a1 >= UINT256_MAX // 2 else a1 / 1e6}")
        print(f"  pUSD allowance V2_NEGRISK:{'MAX' if a2 >= UINT256_MAX // 2 else a2 / 1e6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
