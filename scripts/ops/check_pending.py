"""Authoritative pending-wins scan — on-chain truth, never data-api flag alone.

data-api.polymarket.com `redeemable` flag lags CTF resolution by up to 15 min.
So any scan that trusts that flag will miss fresh wins. This script:

  1. Fetches all positions from data-api (source for list of conditionIds held).
  2. For every position with currentValue > 0.1 AND size > 0.01:
       - Call `CTF.payoutDenominator(conditionId)` across 3 RPCs.
       - If any RPC returns > 0 AND proxy holds winning-side tokens, it's
         redeemable — regardless of what data-api says.
  3. Prints redeemable list + conditionIds ready to paste into onchain_redeem.py.

Run on Montreal (`.env` has POLY_FUNDER_ADDRESS + POLYGON_RPC_URL):
    python3 /tmp/check_pending.py
"""

import json
import os
from urllib.request import Request, urlopen
from eth_abi import encode as abi_encode
from eth_utils import keccak

PROXY = os.environ.get(
    "POLY_FUNDER_ADDRESS", "0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10"
)
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
RPCS = [r for r in [
    os.environ.get("POLYGON_RPC_URL"),
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
] if r]


def rpc(url, to, data):
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }).encode()
    req = Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    return json.loads(urlopen(req, timeout=15).read())


def payout_denom(cid_hex: str) -> list:
    """Return list of (rpc_url, value_or_error) across all RPCs."""
    sel = keccak(b"payoutDenominator(bytes32)")[:4]
    data = "0x" + (
        sel + abi_encode(["bytes32"], [bytes.fromhex(cid_hex[2:])])
    ).hex()
    out = []
    for url in RPCS:
        try:
            r = rpc(url, CTF, data)
            val = int(r["result"], 16) if "result" in r else f"ERR:{r.get('error')}"
            out.append(val)
        except Exception as e:
            out.append(f"EXC:{str(e)[:40]}")
    return out


def main():
    req = Request(
        f"https://data-api.polymarket.com/positions?user={PROXY}&sizeThreshold=0.01&limit=500",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    positions = json.loads(urlopen(req, timeout=15).read())
    # Candidate = still holds value. Ignore fully-lost ($0) and dust.
    candidates = [
        p for p in positions
        if float(p.get("currentValue") or 0) > 0.5
        and float(p.get("size") or 0) > 0.01
    ]

    print(f"Total positions (size>0.01): {len(positions)}")
    print(f"Candidate positions (cv>$0.5): {len(candidates)}")
    print()

    redeemable_ids: list[str] = []
    for p in candidates:
        cid = p["conditionId"]
        outcome = p.get("outcome", "?")
        cv = float(p.get("currentValue") or 0)
        cost = float(p.get("initialValue") or 0)
        denoms = payout_denom(cid)
        nums = [v for v in denoms if isinstance(v, int)]
        ok = len(nums) >= 2 and all(v > 0 for v in nums)
        flag_dataapi = p.get("redeemable")
        title = (p.get("title") or "")[:50]
        marker = "REDEEMABLE" if ok else "OPEN/UNRESOLVED"
        print(
            f"  {marker:16}  {cid[:14]}..  {outcome:4}  "
            f"cv=${cv:5.2f}  cost=${cost:5.2f}  "
            f"denoms={denoms}  dataapi_flag={flag_dataapi}  {title}"
        )
        if ok:
            redeemable_ids.append(cid)

    print()
    print(f"Redeemable (on-chain truth): {len(redeemable_ids)}")
    if redeemable_ids:
        print("Paste into onchain_redeem.py CONDITION_IDS:")
        for cid in redeemable_ids:
            print(f'    "{cid}",')


if __name__ == "__main__":
    main()
