from __future__ import annotations


def is_email_allowed(
    email: str,
    *,
    allowed_emails: list[str],
    allowed_domains: list[str],
) -> bool:
    """Return True iff `email` is permitted by either list.

    Either list may be empty. If both are empty no email is accepted —
    enabling OAuth without configuring an allow-list is a fail-safe deny.
    Matching is case-insensitive; whitespace is stripped.
    """
    if not email:
        return False
    normalized = email.strip().lower()
    if not normalized or "@" not in normalized:
        return False
    if normalized in {e.strip().lower() for e in allowed_emails}:
        return True
    domain = normalized.split("@", 1)[1]
    if domain in {d.strip().lower() for d in allowed_domains}:
        return True
    return False
