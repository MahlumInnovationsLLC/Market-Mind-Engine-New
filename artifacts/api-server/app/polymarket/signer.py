"""EIP-712 (L1) signer for Polymarket CLOB orders.

Polymarket's ``POST /order`` requires the L2 HMAC headers *and* an
EIP-712-signed Order envelope inside the body. The signature is over
the ``CTFExchange.Order`` struct using the Polymarket CTF Exchange
domain. This module wraps ``eth-account`` to produce that signature
from a wallet private key and assembles the on-wire JSON shape the
CLOB expects.

Signature types (mirror Polymarket's ``SignatureType`` enum):
  0 = EOA            — signer signs directly; ``maker == signer == funder``
  1 = POLY_PROXY     — signer is the EOA controlling a Polymarket proxy
                       wallet whose address is ``funder`` (the ``maker``)
  2 = POLY_GNOSIS_SAFE — signer is an owner of a Polymarket-deployed Safe
                         whose address is ``funder``

Defaults to EOA. Overrides via env let the caller switch to proxy/safe
without code changes.

Amount conventions
------------------
Polymarket uses USDC.e (6 decimals) for collateral and conditional
outcome tokens (also 6 decimals). For a BUY of ``size`` shares at
``price`` (decimal probability):

  makerAmount = round(price * size * 1e6)   # USDC paid in
  takerAmount = round(size * 1e6)           # outcome tokens received

For a SELL the legs swap. We always quantise to integer base units
(no fractional micro-USDC on the wire).
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any, Literal

from eth_account import Account
from eth_account.messages import encode_typed_data

log = logging.getLogger(__name__)


class PolymarketSignerError(RuntimeError):
    pass


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Polymarket CTFExchange (Polygon mainnet). Override with
# POLYMARKET_EXCHANGE_ADDRESS for the negRisk exchange or a testnet.
DEFAULT_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
DEFAULT_CHAIN_ID = 137  # Polygon mainnet
DEFAULT_DOMAIN_NAME = "Polymarket CTF Exchange"
DEFAULT_DOMAIN_VERSION = "1"

# Token base units. USDC and CTF outcome tokens are both 6-decimal on
# Polygon, so the same scalar applies to both legs.
BASE_UNIT = 10 ** 6

SIDE_BUY = 0
SIDE_SELL = 1

SignatureType = Literal[0, 1, 2]


def _checksum(addr: str) -> str:
    """Return the EIP-55 checksum form of an address. Tolerates already-checksummed input."""
    if not addr:
        raise PolymarketSignerError("address is required")
    try:
        return Account._utils.address.to_checksum_address(addr)  # type: ignore[attr-defined]
    except Exception:
        # eth-utils path (always available because eth-account depends on it)
        from eth_utils import to_checksum_address

        return to_checksum_address(addr)


def _amounts(action: str, size_shares: int, price_decimal: float) -> tuple[int, int]:
    """Return (maker_amount, taker_amount) in 6-decimal base units."""
    if size_shares <= 0:
        raise PolymarketSignerError("size must be > 0")
    if not (0 < price_decimal < 1):
        raise PolymarketSignerError(f"price must be in (0,1), got {price_decimal}")
    usdc = int(round(price_decimal * size_shares * BASE_UNIT))
    tokens = int(size_shares * BASE_UNIT)
    if usdc <= 0:
        raise PolymarketSignerError("computed USDC leg is zero — price/size too small")
    if action == "buy":
        return usdc, tokens
    if action == "sell":
        return tokens, usdc
    raise PolymarketSignerError(f"unknown action {action!r}")


def _resolve_signer_settings(settings: Any) -> dict[str, Any]:
    """Pull the L1 signing knobs from env (with sensible defaults).

    Kept independent of the dataclass so adding new envs doesn't
    require widening ``Settings`` for every signing-only field.
    """
    private_key = (os.environ.get("POLYMARKET_PRIVATE_KEY") or "").strip()
    if not private_key:
        raise PolymarketSignerError(
            "POLYMARKET_PRIVATE_KEY is not set — cannot sign live Polymarket orders"
        )
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    funder = (
        os.environ.get("POLYMARKET_FUNDER_ADDRESS")
        or getattr(settings, "polymarket_wallet_address", None)
        or ""
    )
    if not funder:
        raise PolymarketSignerError(
            "POLYMARKET_FUNDER_ADDRESS or POLYMARKET_WALLET_ADDRESS is required"
        )

    sig_type_raw = os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0").strip()
    try:
        sig_type = int(sig_type_raw)
    except ValueError as e:
        raise PolymarketSignerError(
            f"POLYMARKET_SIGNATURE_TYPE must be 0/1/2 (got {sig_type_raw!r})"
        ) from e
    if sig_type not in (0, 1, 2):
        raise PolymarketSignerError(
            f"POLYMARKET_SIGNATURE_TYPE must be 0, 1 or 2 (got {sig_type})"
        )

    chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID") or DEFAULT_CHAIN_ID)
    exchange = os.environ.get("POLYMARKET_EXCHANGE_ADDRESS") or DEFAULT_CTF_EXCHANGE
    fee_rate_bps = int(os.environ.get("POLYMARKET_FEE_RATE_BPS") or "0")
    neg_risk = (os.environ.get("POLYMARKET_NEG_RISK") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
        "on",
    )

    return {
        "private_key": private_key,
        "funder": _checksum(funder),
        "signature_type": sig_type,
        "chain_id": chain_id,
        "exchange": _checksum(exchange),
        "fee_rate_bps": fee_rate_bps,
        "neg_risk": neg_risk,
    }


def _eip712_payload(
    *,
    chain_id: int,
    verifying_contract: str,
    order: dict[str, Any],
) -> dict[str, Any]:
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "taker", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "expiration", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "feeRateBps", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
            ],
        },
        "primaryType": "Order",
        "domain": {
            "name": DEFAULT_DOMAIN_NAME,
            "version": DEFAULT_DOMAIN_VERSION,
            "chainId": chain_id,
            "verifyingContract": verifying_contract,
        },
        "message": order,
    }


def build_signed_order(
    *,
    action: str,                # "buy" | "sell"
    token_id: str,
    size_shares: int,
    price_decimal: float,
    time_in_force: str,         # "GTC" | "IOC" | "FOK"
    api_key: str,
    settings: Any,
    expiration: int | None = None,
    salt: int | None = None,
    nonce: int = 0,
) -> dict[str, Any]:
    """Build and sign a Polymarket CLOB order envelope.

    Returns the JSON body shape ``POST /order`` expects:
      {"order": {…signed Order…}, "owner": <api_key>, "orderType": <GTC|FOK|FAK>}
    """
    cfg = _resolve_signer_settings(settings)
    if not token_id:
        raise PolymarketSignerError("token_id is required")
    if not api_key:
        raise PolymarketSignerError("api_key (L2 owner) is required")

    acct = Account.from_key(cfg["private_key"])
    signer_addr = _checksum(acct.address)

    # For EOA signing maker == signer; for POLY_PROXY / POLY_GNOSIS_SAFE
    # the signer is the EOA, the maker is the proxy/safe (the funder).
    if cfg["signature_type"] == 0:
        maker = signer_addr
    else:
        maker = cfg["funder"]

    side_int = SIDE_BUY if action == "buy" else SIDE_SELL
    maker_amount, taker_amount = _amounts(action, size_shares, price_decimal)

    if salt is None:
        # 64-bit random salt; well within uint256 and matches py-clob-client.
        salt = secrets.randbits(64)
    if expiration is None:
        # 0 = no expiration for GTC; for IOC/FOK an expiration is harmless
        # because the matcher rejects the rest of the order. Default 0.
        expiration = 0

    order_msg = {
        "salt": int(salt),
        "maker": maker,
        "signer": signer_addr,
        "taker": ZERO_ADDRESS,
        "tokenId": int(token_id),
        "makerAmount": int(maker_amount),
        "takerAmount": int(taker_amount),
        "expiration": int(expiration),
        "nonce": int(nonce),
        "feeRateBps": int(cfg["fee_rate_bps"]),
        "side": int(side_int),
        "signatureType": int(cfg["signature_type"]),
    }

    typed = _eip712_payload(
        chain_id=cfg["chain_id"],
        verifying_contract=cfg["exchange"],
        order=order_msg,
    )
    encoded = encode_typed_data(full_message=typed)
    signed = acct.sign_message(encoded)
    signature_hex = signed.signature.hex()
    if not signature_hex.startswith("0x"):
        signature_hex = "0x" + signature_hex

    # On the wire all numeric uint256 fields are sent as strings (the CLOB
    # parses them as bigints). ``side`` becomes a string label, but the
    # signed payload encodes it as the uint8.
    order_body = {
        "salt": str(order_msg["salt"]),
        "maker": maker,
        "signer": signer_addr,
        "taker": ZERO_ADDRESS,
        "tokenId": str(order_msg["tokenId"]),
        "makerAmount": str(order_msg["makerAmount"]),
        "takerAmount": str(order_msg["takerAmount"]),
        "side": "BUY" if side_int == SIDE_BUY else "SELL",
        "expiration": str(order_msg["expiration"]),
        "nonce": str(order_msg["nonce"]),
        "feeRateBps": str(order_msg["feeRateBps"]),
        "signatureType": int(cfg["signature_type"]),
        "signature": signature_hex,
    }

    tif = (time_in_force or "GTC").upper()
    if tif == "IOC":
        order_type = "FAK"   # Fill-And-Kill is Polymarket's IOC
    elif tif == "FOK":
        order_type = "FOK"
    else:
        order_type = "GTC"

    return {
        "order": order_body,
        "owner": api_key,
        "orderType": order_type,
    }


def have_signing_creds() -> bool:
    """True iff a private key is configured. Funder/wallet address is
    pulled from settings at call time; this is just the cheap precheck
    used to decide whether ``OrderRequest`` should attempt signing."""
    return bool((os.environ.get("POLYMARKET_PRIVATE_KEY") or "").strip())


# Convenience self-test ----------------------------------------------------

def _selftest_smoke() -> dict[str, Any]:
    """Sign a synthetic order with a throwaway key — used by the live
    smoke test. Returns the envelope so the caller can sanity-check the
    fields without ever hitting the network."""
    acct = Account.create()
    os.environ.setdefault("POLYMARKET_FUNDER_ADDRESS", acct.address)
    os.environ["POLYMARKET_PRIVATE_KEY"] = acct.key.hex()
    return build_signed_order(
        action="buy",
        token_id="123456789",
        size_shares=10,
        price_decimal=0.42,
        time_in_force="GTC",
        api_key="00000000-0000-0000-0000-000000000000",
        settings=None,
        salt=int(time.time()),
    )
