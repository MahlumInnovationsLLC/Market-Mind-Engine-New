"""Live-mode smoke test for the Polymarket L1 signer.

Builds a signed envelope locally and (optionally) POSTs it to the
configured Polymarket CLOB. Useful for verifying credentials before
flipping ``AXIOM_TRADING_MODE=live``.

Usage::

    # Dry run — sign only, dump the envelope, never hit the network.
    python -m scripts.polymarket_live_smoke --token-id 12345 --price 0.50 --size 5

    # Submit against staging (or whatever ``POLYMARKET_API_BASE_URL`` points to).
    python -m scripts.polymarket_live_smoke --token-id 12345 --price 0.50 --size 5 --submit

Required env (signing):
  POLYMARKET_PRIVATE_KEY       — hex EOA key (with or without 0x prefix)
  POLYMARKET_WALLET_ADDRESS    — funder/proxy wallet (or POLYMARKET_FUNDER_ADDRESS)

Required env if --submit:
  POLYMARKET_API_KEY / POLYMARKET_API_SECRET / POLYMARKET_API_PASSPHRASE
  POLYMARKET_API_BASE_URL      — defaults to https://clob.polymarket.com

Exit code is non-zero on any failure so this can run in CI.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Allow `python scripts/polymarket_live_smoke.py` from the api-server root.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Provide harmless defaults so config.get_settings() can import in CI.
os.environ.setdefault("SUPABASE_DB_PASSWORD", "smoke")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "smoke")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Polymarket signer + live smoke test")
    p.add_argument("--token-id", required=True, help="ERC1155 outcome token id")
    p.add_argument("--price", type=float, required=True, help="Decimal price 0..1")
    p.add_argument("--size", type=int, required=True, help="Contracts (>=1)")
    p.add_argument("--side", choices=("buy", "sell"), default="buy")
    p.add_argument("--tif", choices=("GTC", "IOC", "FOK"), default="GTC")
    p.add_argument("--submit", action="store_true",
                   help="Actually POST to the configured CLOB (otherwise dry-run)")
    return p.parse_args()


async def _submit(envelope: dict) -> dict:
    from app.polymarket import client as poly_client

    async with poly_client.PolymarketClient() as c:
        return await c.submit_order(envelope)


def main() -> int:
    args = _parse_args()
    from app.config import get_settings  # noqa: WPS433 — lazy import after env defaults
    from app.polymarket import signer

    s = get_settings()
    if not signer.have_signing_creds():
        print("FAIL: POLYMARKET_PRIVATE_KEY is not set", file=sys.stderr)
        return 2

    try:
        envelope = signer.build_signed_order(
            action=args.side,
            token_id=str(args.token_id),
            size_shares=int(args.size),
            price_decimal=float(args.price),
            time_in_force=args.tif,
            api_key=s.polymarket_api_key or "smoke-no-api-key",
            settings=s,
        )
    except signer.PolymarketSignerError as e:
        print(f"FAIL: signer rejected request: {e}", file=sys.stderr)
        return 3

    print(json.dumps(envelope, indent=2))

    if not args.submit:
        print("\nDry run complete — pass --submit to POST to the CLOB.",
              file=sys.stderr)
        return 0

    if not (s.polymarket_api_key and s.polymarket_api_secret
            and s.polymarket_api_passphrase and s.polymarket_wallet_address):
        print("FAIL: --submit requires POLYMARKET_API_{KEY,SECRET,PASSPHRASE} "
              "and POLYMARKET_WALLET_ADDRESS", file=sys.stderr)
        return 4

    try:
        ack = asyncio.run(_submit(envelope))
    except Exception as e:  # noqa: BLE001 — we want to print whatever surfaces
        print(f"FAIL: submit raised: {e}", file=sys.stderr)
        return 5
    print("\n--- ack ---")
    print(json.dumps(ack, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
