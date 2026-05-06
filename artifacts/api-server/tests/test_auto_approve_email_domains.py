"""Unit coverage for the test-only operator-allow-list bypass.

`OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS` lets the e2e test harness sign
in as a freshly minted Clerk testing user (whose email lives in a
known throwaway domain) without an operator hand-approving each one.
The env var is empty in production, so the bypass is a no-op there.

These tests exercise the helpers + `check_approval` directly so the
contract is locked down independently of the `runTest` Playwright
harness.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import auth as auth_module
from app.auth import (
    AuthedUser,
    _email_in_auto_approve_domain,
    _auto_approve_email_domains,
    check_approval,
)


def _user(email: str | None) -> AuthedUser:
    claims: dict[str, Any] = {}
    if email is not None:
        claims["email"] = email
    return AuthedUser(user_id="user_test_1", session_id="sess_1", claims=claims)


def test_domains_parsed_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS",
        "  example.com , E2E.test ,@throwaway.dev,",
    )
    assert _auto_approve_email_domains() == {
        "example.com",
        "e2e.test",
        "throwaway.dev",
    }


def test_domains_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS", raising=False)
    assert _auto_approve_email_domains() == set()


def test_email_match_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS", "Example.COM")
    assert _email_in_auto_approve_domain("Tester+ABC@Example.com") is True
    assert _email_in_auto_approve_domain("tester@other.com") is False
    assert _email_in_auto_approve_domain(None) is False
    assert _email_in_auto_approve_domain("") is False


def test_no_op_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS", raising=False)
    assert _email_in_auto_approve_domain("any@example.com") is False


def test_check_approval_short_circuits_on_auto_approve_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bypass must approve even when:
      * no `app_users` row exists for the user, AND
      * the email is NOT in `OPERATOR_ALLOWED_EMAILS`.

    Also, it must short-circuit BEFORE the DB lookup so a stale
    `approved=False` row can't override the test bypass.
    """
    monkeypatch.setenv("OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS", "example.com")
    monkeypatch.delenv("OPERATOR_ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("OPERATOR_ADMIN_EMAILS", raising=False)

    # Force the DB lookup to return a not-approved row to prove the
    # bypass beats persisted state.
    async def _stub_get_user_row(_uid: str) -> dict[str, Any]:
        return {"approved": False, "is_admin": False, "email": "tester@example.com"}

    from app import admin as admin_module

    monkeypatch.setattr(admin_module, "get_user_row", _stub_get_user_row)

    approved, email = asyncio.run(
        check_approval(_user("tester@example.com"))
    )
    assert approved is True
    assert email == "tester@example.com"


def test_check_approval_overrides_stale_row_via_row_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the JWT lacks an email claim but the persisted ``app_users``
    row was previously upserted with a resolved email in the bypass
    domain, the bypass must override the row's ``approved=False`` so
    the operator-approval gate does not block the test harness on a
    stale row from a prior run.
    """
    monkeypatch.setenv("OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS", "example.com")
    monkeypatch.delenv("OPERATOR_ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("OPERATOR_ADMIN_EMAILS", raising=False)

    from app import admin as admin_module

    async def _stale_row(_uid: str) -> dict[str, Any]:
        return {
            "approved": False,
            "is_admin": False,
            "email": "ticket-stale@example.com",
        }

    monkeypatch.setattr(admin_module, "get_user_row", _stale_row)

    approved, email = asyncio.run(
        check_approval(_user(None))  # JWT has no email claim
    )
    assert approved is True
    assert email == "ticket-stale@example.com"


def test_check_approval_respects_stale_row_when_domain_not_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The override must NOT fire when the row's email is outside the
    bypass domain — production deployments leave the env var unset, so
    this guards against the bypass leaking into normal allow-list
    semantics.
    """
    monkeypatch.setenv("OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS", "example.com")
    monkeypatch.delenv("OPERATOR_ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("OPERATOR_ADMIN_EMAILS", raising=False)

    from app import admin as admin_module

    async def _row(_uid: str) -> dict[str, Any]:
        return {
            "approved": False,
            "is_admin": False,
            "email": "real@somewhere-else.com",
        }

    monkeypatch.setattr(admin_module, "get_user_row", _row)

    approved, email = asyncio.run(check_approval(_user(None)))
    assert approved is False
    assert email == "real@somewhere-else.com"


def test_check_approval_falls_through_when_domain_not_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS", "e2e.test")
    monkeypatch.delenv("OPERATOR_ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("OPERATOR_ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)

    from app import admin as admin_module

    async def _no_row(_uid: str) -> None:
        return None

    monkeypatch.setattr(admin_module, "get_user_row", _no_row)

    approved, _ = asyncio.run(
        check_approval(_user("not-on-list@somewhere-else.com"))
    )
    assert approved is False


def test_check_approval_resolves_email_via_clerk_then_auto_approves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the JWT has no email claim, we must still match the bypass
    once `_fetch_clerk_user` resolves the primary email.
    """
    monkeypatch.setenv("OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS", "example.com")
    monkeypatch.delenv("OPERATOR_ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("OPERATOR_ADMIN_EMAILS", raising=False)

    from app import admin as admin_module

    async def _no_row(_uid: str) -> None:
        return None

    monkeypatch.setattr(admin_module, "get_user_row", _no_row)

    async def _fake_clerk_user(_uid: str) -> dict[str, Any]:
        return {
            "primary_email_address_id": "idem_1",
            "email_addresses": [
                {"id": "idem_1", "email_address": "ticket-abc@Example.com"}
            ],
        }

    monkeypatch.setattr(auth_module, "_fetch_clerk_user", _fake_clerk_user)

    approved, email = asyncio.run(
        check_approval(_user(None))
    )
    assert approved is True
    assert email == "ticket-abc@example.com"
