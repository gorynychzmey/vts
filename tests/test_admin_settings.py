from vts.core.config import Settings


def test_is_admin_case_insensitive() -> None:
    settings = Settings(admin_emails=["Admin@Example.com"])
    assert settings.is_admin("admin@example.com")
    assert settings.is_admin("ADMIN@EXAMPLE.COM")
    assert not settings.is_admin("user@example.com")

