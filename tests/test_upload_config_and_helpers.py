import os
import pytest


def test_upload_settings_defaults(monkeypatch):
    monkeypatch.setenv("VTS_DATABASE_URL", "postgresql+asyncpg://x/y")
    import vts.core.config as c
    c.get_settings.cache_clear()
    s = c.get_settings()
    assert s.upload_chunked_threshold_bytes == 52_428_800
    assert s.upload_chunk_bytes == 8_388_608
    assert s.max_upload_bytes == 2_147_483_648
    assert s.upload_session_ttl_seconds == 86_400
    c.get_settings.cache_clear()


def test_allowed_suffixes_module_level():
    from vts.api.main import _ALLOWED_UPLOAD_SUFFIXES
    assert ".mp4" in _ALLOWED_UPLOAD_SUFFIXES
    assert ".m4a" in _ALLOWED_UPLOAD_SUFFIXES


def test_normalize_prompts_json_default_and_parse():
    from vts.api.main import _normalize_prompts_json
    assert _normalize_prompts_json(None) == [{"source": "system", "id": "summary"}]
    out = _normalize_prompts_json('[{"source":"system","id":"summary"}]')
    assert out == [{"source": "system", "id": "summary"}]


def test_normalize_prompts_json_rejects_bad():
    from fastapi import HTTPException
    from vts.api.main import _normalize_prompts_json
    with pytest.raises(HTTPException):
        _normalize_prompts_json("{not json")
    with pytest.raises(HTTPException):
        _normalize_prompts_json('{"not":"a list"}')
