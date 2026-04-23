"""Direct on-chain MATIC-route redemption for Polymarket CTF wins.

Pattern: reference_onchain_redeem.md (proven 2026-04-16, 33+ redeems
through 2026-04-23 totalling $190+). Raw EOA -> factory.proxy([(1, CTF,
0, redeemPositions_calldata)]). No relayer quota consumed. ~$0.005 MATIC
gas per redeem.

Usage (on Montreal — requires engine/.env with POLY_PRIVATE_KEY):
    cd /home/novakash/novakash
    set -a && source engine/.env && set +a
    python3 scripts/ops/onchain_redeem.py

Edit CONDITION_IDS below before running. Get them from check_pending.py.

Known gotchas:
  - Sweeper race: Polymarket auto-sweeper or engine redeemer may beat us.
    Tx succeeds with $0 payout. ~10-15% rate. Harmless.
  - Nonce: uses local counter to avoid `nonce too low` on back-to-back.
  - POA middleware required for Polygon (extraData 902 bytes).
  - Cross-RPC stale-balance: never trust single RPC for post-tx reads.
"""
import os, json, time, sys
from urllib.request import Request, urlopen
from eth_abi import encode as abi_encode
from eth_utils import keccak
from eth_account import Account
from web3 import Web3

# ---- constants ----
EOA_PK        = os.environ["POLY_PRIVATE_KEY"]
PROXY         = Web3.to_checksum_address("0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10")
FACTORY       = Web3.to_checksum_address("0xaB45c5A4B0c941a2F231C04C3f49182e1A254052")
CTF           = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
USDC_E        = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
RPC_PRIMARY   = os.environ["POLYGON_RPC_URL"]
RPC_FALLBACK  = "https://polygon.drpc.org"
RPC_THIRD     = "https://1rpc.io/matic"

CONDITION_IDS = [
    # Paste conditionIds from check_pending.py output here.
    # Example:
    # "0xabc123...",
    # "0xdef456...",
]

# ---- raw JSON-RPC helper (so we can query any endpoint) ----
def rpc(url, method, params):
    body = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req = Request(url, data=body, headers={"Content-Type":"application/json",
                                           "User-Agent":"Mozilla/5.0"})
    return json.loads(urlopen(req, timeout=20).read())

def call_payout_denom(url, cid_hex):
    sel = keccak(b"payoutDenominator(bytes32)")[:4]
    data = "0x" + (sel + abi_encode(["bytes32"], [bytes.fromhex(cid_hex[2:])])).hex()
    r = rpc(url, "eth_call", [{"to": CTF, "data": data}, "latest"])
    if "result" in r:
        return int(r["result"], 16)
    raise RuntimeError(f"rpc err: {r.get('error')}")

# ---- 1. two-RPC consensus on resolution ----
print("=" * 70)
print("STEP 1: verify payoutDenominator across 2 RPCs")
print("=" * 70)
resolved = []
for cid in CONDITION_IDS:
    try:
        p = call_payout_denom(RPC_PRIMARY, cid)
    except Exception as e:
        p = f"ERR:{e}"
    try:
        f = call_payout_denom(RPC_FALLBACK, cid)
    except Exception as e:
        f = f"ERR:{e}"
    try:
        t = call_payout_denom(RPC_THIRD, cid)
    except Exception as e:
        t = f"ERR:{e}"
    nums = [x for x in (p, f, t) if isinstance(x, int)]
    ok = len(nums) >= 2 and all(x > 0 for x in nums)
    print(f"  {cid[:14]}..  primary={p}  drpc={f}  1rpc={t}  -> {'RESOLVED' if ok else 'SKIP'}")
    if ok:
        resolved.append(cid)

if not resolved:
    print("No resolved wins. Abort.")
    sys.exit(0)

print(f"\n{len(resolved)}/{len(CONDITION_IDS)} resolved. Proceeding.")

# ---- 2. build & submit tx per cid ----
w3 = Web3(Web3.HTTPProvider(RPC_PRIMARY, request_kwargs={"timeout": 30}))
try:
    from web3.middleware import ExtraDataToPOAMiddleware
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
except ImportError:
    from web3.middleware import geth_poa_middleware
    w3.middleware_onion.inject(geth_poa_middleware, layer=0)
acct = Account.from_key(EOA_PK)
EOA = acct.address
print(f"\nEOA signer: {EOA}")
print(f"EOA MATIC balance: {w3.from_wei(w3.eth.get_balance(EOA), 'ether'):.4f}")

sel_redeem = keccak(b"redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
sel_proxy  = keccak(b"proxy((uint8,address,uint256,bytes)[])")[:4]

def build_outer(cid_hex: str) -> bytes:
    inner = sel_redeem + abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [USDC_E, b"\x00" * 32, bytes.fromhex(cid_hex[2:]), [1, 2]],
    )
    outer = sel_proxy + abi_encode(
        ["(uint8,address,uint256,bytes)[]"],
        [[(1, CTF, 0, inner)]],
    )
    return outer

USDC_TRANSFER_TOPIC = "0x" + keccak(b"Transfer(address,address,uint256)").hex()
proxy_topic_padded = "0x" + "0" * 24 + PROXY.lower()[2:]

results = []
# Fetch nonce ONCE, then increment locally to avoid nonce-race on back-to-back txs.
nonce = w3.eth.get_transaction_count(EOA, "pending")
for idx, cid in enumerate(resolved):
    print(f"\n--- [{idx+1}/{len(resolved)}] redeeming {cid[:14]}.. (nonce={nonce}) ---")
    data = build_outer(cid)
    # Polygon gas: use feeHistory median-ish; safe upper bound
    base = w3.eth.get_block("latest").get("baseFeePerGas") or w3.to_wei(50, "gwei")
    max_priority = w3.to_wei(30, "gwei")
    max_fee = base * 2 + max_priority
    tx = {
        "from": EOA, "to": FACTORY, "value": 0,
        "data": "0x" + data.hex(),
        "gas": 450_000,
        "maxFeePerGas": max_fee, "maxPriorityFeePerGas": max_priority,
        "nonce": nonce, "chainId": 137,
        "type": 2,
    }
    signed = acct.sign_transaction(tx)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    try:
        tx_hash = w3.eth.send_raw_transaction(raw)
    except Exception as e:
        print(f"  SUBMIT FAILED: {e}")
        results.append((cid, None, 0.0, f"submit_fail: {e}"))
        # Don't increment nonce — tx was never accepted by the mempool.
        continue
    print(f"  tx: {tx_hash.hex()}")
    # Wait receipt
    try:
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    except Exception as e:
        print(f"  RECEIPT TIMEOUT: {e}")
        results.append((cid, tx_hash.hex(), 0.0, "receipt_timeout"))
        continue
    if rcpt["status"] != 1:
        print(f"  REVERTED. block={rcpt['blockNumber']}")
        results.append((cid, tx_hash.hex(), 0.0, "reverted"))
        continue
    # Decode USDC Transfer -> proxy
    payout = 0
    for log in rcpt["logs"]:
        if (log["address"].lower() == USDC_E.lower()
            and len(log["topics"]) >= 3
            and log["topics"][0].hex().lower().lstrip("0x") == USDC_TRANSFER_TOPIC.lstrip("0x")
            and log["topics"][2].hex().lower().endswith(PROXY.lower()[2:])):
            payout = int(log["data"].hex(), 16) / 1e6
            break
    print(f"  OK block={rcpt['blockNumber']}  payout=${payout:.2f}  gas_used={rcpt['gasUsed']}")
    results.append((cid, tx_hash.hex(), payout, "ok"))
    nonce += 1  # local increment — don't re-query pending nonce
    time.sleep(1)

# ---- 3. summary ----
print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
total = 0.0
for cid, tx, payout, status in results:
    tx_link = f"https://polygonscan.com/tx/{tx}" if tx else "-"
    print(f"  {cid[:14]}..  ${payout:5.2f}  {status}  {tx_link}")
    total += payout
print(f"\nTotal redeemed: ${total:.2f}")
