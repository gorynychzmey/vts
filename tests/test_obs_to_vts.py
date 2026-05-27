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


@pytest.fixture(scope="module")
def obs_module():
    # Stub obspython with the constants the script references.
    stub = types.ModuleType("obspython")
    stub.OBS_FRONTEND_EVENT_RECORDING_STOPPED = 1  # arbitrary
    stub.obs_frontend_add_event_callback = lambda _cb: None
    stub.obs_frontend_get_last_recording = lambda: ""
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


def test_read_config_strips_trailing_slash(obs_module, monkeypatch):
    monkeypatch.setenv("VTS_BASE_URL", "https://vts.example.com/")
    monkeypatch.setenv("VTS_API_TOKEN", "vts_abc")
    cfg = obs_module._read_config()
    assert cfg["base_url"] == "https://vts.example.com"
    assert cfg["token"] == "vts_abc"


def test_read_config_defaults(obs_module, monkeypatch):
    for k in ("VTS_BASE_URL", "VTS_API_TOKEN", "VTS_TRANSCRIPT",
              "VTS_SUMMARY", "VTS_LANGUAGE", "VTS_AUDIO_ONLY"):
        monkeypatch.delenv(k, raising=False)
    cfg = obs_module._read_config()
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
