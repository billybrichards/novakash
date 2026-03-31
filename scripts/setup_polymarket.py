#!/usr/bin/env python3
"""
Derive Polymarket CLOB API credentials from a wallet private key.

Usage:
    python scripts/setup_polymarket.py

Requires POLY_PRIVATE_KEY in .env or as environment variable.
Outputs the API key, secret, and passphrase to add to your .env file.

NOTE: You must complete one manual trade on polymarket.com first
to initialise your proxy wallet before the API works.
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()


def main():
    private_key = os.getenv("POLY_PRIVATE_KEY")
    if not private_key:
        print("ERROR: POLY_PRIVATE_KEY not set in .env or environment")
        sys.exit(1)

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        # Polymarket CLOB endpoint
        host = "https://clob.polymarket.com"
        chain_id = 137  # Polygon mainnet

        client = ClobClient(host, key=private_key, chain_id=chain_id)

        # Derive API credentials
        print("Deriving API credentials from wallet...")
        creds: ApiCreds = client.derive_api_key()

        print("\n✅ Success! Add these to your .env file:\n")
        print(f"POLY_API_KEY={creds.api_key}")
        print(f"POLY_API_SECRET={creds.api_secret}")
        print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")

        # Get funder address
        print(f"\nFunder address (check polymarket.com/settings):")
        print(f"POLY_FUNDER_ADDRESS=<your proxy wallet address>")

    except ImportError:
        print("ERROR: py-clob-client not installed. Run: pip install py-clob-client==0.34.5")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
