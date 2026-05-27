"""Unit tests for the OBS Studio uploader script.

The script imports `obspython` at top level (provided by OBS at runtime).
We stub it before import so the module is loadable in a plain Python
environment.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


class _FakeObsData:
    """Stands in for OBS' obs_data_t — backed by a Python dict.

    The script accesses values via obs_data_get_string / get_bool /
    has_user_value. Our fake mirrors that interface.
    """
    def __init__(self, raw: dict | None = None) -> None:
        self._d: dict = dict(raw or {})

    def set_string(self, key: str, value: str) -> None:
        self._d[key] = value

    def set_bool(self, key: str, value: bool) -> None:
        self._d[key] = bool(value)

    def get_string(self, key: str) -> str:
        v = self._d.get(key, "")
        return v if isinstance(v, str) else ""

    def get_bool(self, key: str) -> bool:
        return bool(self._d.get(key, False))

    def has_user_value(self, key: str) -> bool:
        return key in self._d


@pytest.fixture(scope="module")
def obs_module():
    stub = types.ModuleType("obspython")
    stub.OBS_FRONTEND_EVENT_RECORDING_STOPPED = 1
    stub.OBS_TEXT_DEFAULT = 0
    stub.OBS_TEXT_PASSWORD = 1
    stub.obs_frontend_add_event_callback = lambda _cb: None
    stub.obs_frontend_get_last_recording = lambda: ""
    stub.obs_data_get_string = lambda settings, key: settings.get_string(key)
    stub.obs_data_get_bool = lambda settings, key: settings.get_bool(key)
    stub.obs_data_has_user_value = lambda settings, key: settings.has_user_value(key)
    stub.obs_data_set_default_string = lambda *_args, **_kwargs: None
    stub.obs_data_set_default_bool = lambda *_args, **_kwargs: None
    stub.obs_properties_create = lambda: None
    stub.obs_properties_add_text = lambda *_args, **_kwargs: None
    stub.obs_properties_add_bool = lambda *_args, **_kwargs: None
    sys.modules["obspython"] = stub

    path = Path(__file__).resolve().parent.parent / "scripts" / "obs" / "obs_to_vts.py"
    spec = importlib.util.spec_from_file_location("obs_to_vts", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_env_bool_defaults(obs_module, monkeypatch):
    monkeypatch.delenv("X_TEST", raising=False)
    assert obs_module._env_bool("X_TEST", default=True) is True
    assert obs_module._env_bool("X_TEST", default=False) is False


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("off", False),
    ("", True),  # empty falls back to default
])
def test_env_bool_parses(obs_module, monkeypatch, raw: str, expected: bool):
    monkeypatch.setenv("X_TEST", raw)
    assert obs_module._env_bool("X_TEST", default=True) is expected


def _empty_settings():
    return _FakeObsData()


def test_read_config_env_fallback_when_ui_empty(obs_module, monkeypatch):
    monkeypatch.setenv("VTS_BASE_URL", "https://vts.example.com/")
    monkeypatch.setenv("VTS_API_TOKEN", "vts_abc")
    monkeypatch.setenv("VTS_LANGUAGE", "ru")
    cfg = obs_module._read_config(_empty_settings())
    assert cfg["base_url"] == "https://vts.example.com"  # trailing slash stripped
    assert cfg["token"] == "vts_abc"
    assert cfg["language"] == "ru"


def test_read_config_ui_overrides_env(obs_module, monkeypatch):
    monkeypatch.setenv("VTS_BASE_URL", "https://wrong.example.com")
    monkeypatch.setenv("VTS_API_TOKEN", "vts_wrong")
    s = _FakeObsData({
        "vts_base_url": "https://right.example.com/",
        "vts_api_token": "vts_right",
    })
    cfg = obs_module._read_config(s)
    assert cfg["base_url"] == "https://right.example.com"
    assert cfg["token"] == "vts_right"


def test_read_config_ui_empty_falls_back_per_field(obs_module, monkeypatch):
    """Mixed sources: UI sets base, env supplies token."""
    monkeypatch.setenv("VTS_API_TOKEN", "vts_from_env")
    s = _FakeObsData({"vts_base_url": "https://ui.example.com"})
    cfg = obs_module._read_config(s)
    assert cfg["base_url"] == "https://ui.example.com"
    assert cfg["token"] == "vts_from_env"


def test_read_config_bool_ui_value_wins_over_env(obs_module, monkeypatch):
    """UI explicitly setting summary=False must beat env VTS_SUMMARY=true."""
    monkeypatch.setenv("VTS_SUMMARY", "true")
    s = _FakeObsData({"vts_summary": False})
    cfg = obs_module._read_config(s)
    assert cfg["summary"] is False


def test_read_config_bool_missing_ui_falls_back_to_env(obs_module, monkeypatch):
    monkeypatch.setenv("VTS_TRANSCRIPT", "false")
    s = _FakeObsData()  # no UI value for transcript
    cfg = obs_module._read_config(s)
    assert cfg["transcript"] is False


def test_read_config_defaults_when_nothing_set(obs_module, monkeypatch):
    for k in ("VTS_BASE_URL", "VTS_API_TOKEN", "VTS_TRANSCRIPT",
              "VTS_SUMMARY", "VTS_LANGUAGE", "VTS_AUDIO_ONLY"):
        monkeypatch.delenv(k, raising=False)
    cfg = obs_module._read_config(_empty_settings())
    assert cfg == {
        "base_url": "",
        "token": "",
        "transcript": True,
        "summary": True,
        "language": "",
        "audio_only": False,
    }


def test_multipart_body_contains_all_fields(obs_module, tmp_path: Path):
    f = tmp_path / "recording.mkv"
    f.write_bytes(b"FAKE_VIDEO_BYTES")
    body, ctype = obs_module._build_multipart_body(
        f, {"transcript": "true", "summary": "false", "language": "ru"}
    )
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=", 1)[1]
    text = body.decode("latin-1")  # bytes-safe view
    assert f"--{boundary}" in text
    assert f"--{boundary}--" in text  # closing boundary
    assert 'name="transcript"' in text
    assert 'name="summary"' in text
    assert 'name="language"' in text
    assert 'name="file"; filename="recording.mkv"' in text
    assert "FAKE_VIDEO_BYTES" in text


def test_multipart_body_skips_no_fields(obs_module, tmp_path: Path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    body, ctype = obs_module._build_multipart_body(f, {})
    boundary = ctype.split("boundary=", 1)[1]
    # Even with no form fields, file part + closing boundary must be present.
    assert f"--{boundary}--".encode() in body
    assert b'name="file"' in body
