"""Unit tests for the Polymarket EIP-712 (L1) signer."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.polymarket import signer
from app.polymarket.lifecycle import OrderRequest


# A throwaway funded-looking key (NOT a real key — random bytes).
TEST_PRIVATE_KEY = (
    "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
)
TEST_FUNDER = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"  # arbitrary check-summable
TEST_API_KEY = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", TEST_PRIVATE_KEY)
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", TEST_FUNDER)
    monkeypatch.delenv("POLYMARKET_SIGNATURE_TYPE", raising=False)
    monkeypatch.delenv("POLYMARKET_FEE_RATE_BPS", raising=False)
    monkeypatch.delenv("POLYMARKET_CHAIN_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_EXCHANGE_ADDRESS", raising=False)
    yield


def test_have_signing_creds_toggles_with_env(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    assert signer.have_signing_creds() is False
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", TEST_PRIVATE_KEY)
    assert signer.have_signing_creds() is True


def test_build_signed_order_buy_envelope_shape(env):
    env_settings = type("S", (), {"polymarket_wallet_address": TEST_FUNDER})()
    out = signer.build_signed_order(
        action="buy",
        token_id="71321045679252212594626385532706912750332728571942134274129960732224366387",
        size_shares=10,
        price_decimal=0.42,
        time_in_force="GTC",
        api_key=TEST_API_KEY,
        settings=env_settings,
        salt=12345,
        nonce=0,
    )
    assert set(out.keys()) == {"order", "owner", "orderType"}
    assert out["owner"] == TEST_API_KEY
    assert out["orderType"] == "GTC"
    o = out["order"]
    # All uint256 fields go on the wire as strings.
    for key in ("salt", "tokenId", "makerAmount", "takerAmount",
                "expiration", "nonce", "feeRateBps"):
        assert isinstance(o[key], str), key
    assert o["side"] == "BUY"
    assert o["taker"] == "0x0000000000000000000000000000000000000000"
    # Amounts: 0.42 * 10 * 1e6 = 4_200_000 USDC base units in;
    # 10 * 1e6 = 10_000_000 outcome tokens out.
    assert o["makerAmount"] == "4200000"
    assert o["takerAmount"] == "10000000"
    assert o["signatureType"] == 0
    assert o["signature"].startswith("0x") and len(o["signature"]) == 132


def test_build_signed_order_sell_swaps_legs(env):
    out = signer.build_signed_order(
        action="sell",
        token_id="999",
        size_shares=20,
        price_decimal=0.30,
        time_in_force="IOC",
        api_key=TEST_API_KEY,
        settings=type("S", (), {"polymarket_wallet_address": TEST_FUNDER})(),
        salt=1,
    )
    o = out["order"]
    assert o["side"] == "SELL"
    # SELL: maker gives outcome tokens, taker pays USDC.
    assert o["makerAmount"] == str(20 * 1_000_000)
    assert o["takerAmount"] == str(int(0.30 * 20 * 1_000_000))
    # IOC maps to Polymarket's FAK orderType.
    assert out["orderType"] == "FAK"


def test_build_signed_order_signature_changes_with_amount(env):
    settings = type("S", (), {"polymarket_wallet_address": TEST_FUNDER})()
    a = signer.build_signed_order(
        action="buy", token_id="1", size_shares=10, price_decimal=0.5,
        time_in_force="GTC", api_key=TEST_API_KEY, settings=settings, salt=1,
    )
    b = signer.build_signed_order(
        action="buy", token_id="1", size_shares=11, price_decimal=0.5,
        time_in_force="GTC", api_key=TEST_API_KEY, settings=settings, salt=1,
    )
    assert a["order"]["signature"] != b["order"]["signature"]


def test_build_signed_order_proxy_signature_type_uses_funder_as_maker(env, monkeypatch):
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "1")
    out = signer.build_signed_order(
        action="buy", token_id="1", size_shares=1, price_decimal=0.5,
        time_in_force="GTC", api_key=TEST_API_KEY,
        settings=type("S", (), {"polymarket_wallet_address": TEST_FUNDER})(),
        salt=1,
    )
    o = out["order"]
    assert o["signatureType"] == 1
    # maker is the funder (proxy/safe), signer is the EOA derived from the key.
    assert o["maker"].lower() == TEST_FUNDER.lower()
    assert o["signer"] != o["maker"]


def test_build_signed_order_rejects_invalid_inputs(env):
    settings = type("S", (), {"polymarket_wallet_address": TEST_FUNDER})()
    with pytest.raises(signer.PolymarketSignerError):
        signer.build_signed_order(
            action="buy", token_id="1", size_shares=0, price_decimal=0.5,
            time_in_force="GTC", api_key=TEST_API_KEY, settings=settings,
        )
    with pytest.raises(signer.PolymarketSignerError):
        signer.build_signed_order(
            action="buy", token_id="1", size_shares=10, price_decimal=1.5,
            time_in_force="GTC", api_key=TEST_API_KEY, settings=settings,
        )
    with pytest.raises(signer.PolymarketSignerError):
        signer.build_signed_order(
            action="weird", token_id="1", size_shares=10, price_decimal=0.5,
            time_in_force="GTC", api_key=TEST_API_KEY, settings=settings,
        )
    with pytest.raises(signer.PolymarketSignerError):
        signer.build_signed_order(
            action="buy", token_id="", size_shares=10, price_decimal=0.5,
            time_in_force="GTC", api_key=TEST_API_KEY, settings=settings,
        )


def test_resolve_signer_settings_requires_private_key(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    with pytest.raises(signer.PolymarketSignerError):
        signer._resolve_signer_settings(
            type("S", (), {"polymarket_wallet_address": TEST_FUNDER})()
        )


# --- Integration with OrderRequest --------------------------------------------


class _Settings:
    polymarket_wallet_address = TEST_FUNDER
    polymarket_api_key = TEST_API_KEY


def test_order_request_unsigned_skeleton_when_no_key(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    req = OrderRequest(
        market_id="cond_x", token_id="42", side="yes", action="buy",
        count=5, limit_price_cents=33, type="limit", time_in_force="GTC",
    )
    payload = req.to_polymarket_payload(settings=_Settings())
    assert payload["tokenID"] == "42"
    assert payload["price"] == 0.33
    assert payload["size"] == 5
    assert payload["side"] == "BUY"
    assert "signature" not in payload


def test_order_request_signs_when_key_present(env):
    req = OrderRequest(
        market_id="cond_x", token_id="42", side="yes", action="buy",
        count=5, limit_price_cents=33, type="limit", time_in_force="GTC",
    )
    payload = req.to_polymarket_payload(settings=_Settings())
    assert "order" in payload and payload["owner"] == TEST_API_KEY
    assert payload["order"]["signature"].startswith("0x")
    assert payload["order"]["makerAmount"] == str(int(0.33 * 5 * 1_000_000))
