from __future__ import annotations

import pytest

from vts.mcp.allowlist import is_email_allowed


def test_empty_both_lists_rejects_everyone() -> None:
    assert is_email_allowed("alice@example.com", allowed_emails=[], allowed_domains=[]) is False


def test_email_in_allowed_emails_passes() -> None:
    assert is_email_allowed(
        "alice@example.com",
        allowed_emails=["alice@example.com"],
        allowed_domains=[],
    ) is True


def test_domain_in_allowed_domains_passes() -> None:
    assert is_email_allowed(
        "anyone@vostrikov.de",
        allowed_emails=[],
        allowed_domains=["vostrikov.de"],
    ) is True


def test_email_match_is_case_insensitive() -> None:
    assert is_email_allowed(
        "Alice@Example.COM",
        allowed_emails=["alice@example.com"],
        allowed_domains=[],
    ) is True


def test_domain_match_is_case_insensitive() -> None:
    assert is_email_allowed(
        "x@Vostrikov.DE",
        allowed_emails=[],
        allowed_domains=["vostrikov.de"],
    ) is True


def test_either_match_is_enough_or_logic() -> None:
    assert is_email_allowed(
        "guest@vostrikov.de",
        allowed_emails=["alice@example.com"],
        allowed_domains=["vostrikov.de"],
    ) is True


def test_no_match_rejects() -> None:
    assert is_email_allowed(
        "stranger@somewhere.com",
        allowed_emails=["alice@example.com"],
        allowed_domains=["vostrikov.de"],
    ) is False


def test_email_without_at_sign_rejects() -> None:
    assert is_email_allowed("not-an-email", allowed_emails=[], allowed_domains=["x"]) is False


def test_blank_email_rejects() -> None:
    assert is_email_allowed("", allowed_emails=["a@b"], allowed_domains=["x"]) is False
    assert is_email_allowed("   ", allowed_emails=["a@b"], allowed_domains=["x"]) is False
