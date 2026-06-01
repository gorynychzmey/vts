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
              "VTS_SUMMARY", "VTS_LANGUAGE", "VTS_AUDIO_ONLY", "VTS_NOTIFY",
              "VTS_DISPLAY_NAME_TEMPLATE"):
        monkeypatch.delenv(k, raising=False)
    cfg = obs_module._read_config(_empty_settings())
    assert cfg == {
        "base_url": "",
        "token": "",
        "transcript": True,
        "summary": True,
        "language": "",
        "audio_only": False,
        "notify": True,
        "display_name_template": "",
    }


def test_read_config_notify_can_be_disabled_via_env(obs_module, monkeypatch):
    monkeypatch.setenv("VTS_NOTIFY", "false")
    cfg = obs_module._read_config(_empty_settings())
    assert cfg["notify"] is False


def test_read_config_notify_ui_wins_over_env(obs_module, monkeypatch):
    monkeypatch.setenv("VTS_NOTIFY", "true")
    s = _FakeObsData({"vts_notify": False})
    cfg = obs_module._read_config(s)
    assert cfg["notify"] is False


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


# ---------------------------------------------------------------- display name

def test_render_display_name_empty_template_yields_empty(obs_module, tmp_path: Path):
    f = tmp_path / "2026-06-01 22-57-08.mp4"
    # Empty template → "" → caller sends no display_name, VTS uses file:// label.
    assert obs_module._render_display_name("", f) == ""
    assert obs_module._render_display_name("   ", f) == ""


def test_render_display_name_static_template(obs_module, tmp_path: Path):
    f = tmp_path / "rec.mkv"
    assert obs_module._render_display_name("Weekly standup", f) == "Weekly standup"


def test_render_display_name_filename_placeholder(obs_module, tmp_path: Path):
    f = tmp_path / "2026-06-01 22-57-08.mp4"
    # {filename} expands to the stem (no extension); result is trimmed.
    assert obs_module._render_display_name("OBS: {filename}", f) == "OBS: 2026-06-01 22-57-08"


def test_render_display_name_unknown_placeholder_left_intact(obs_module, tmp_path: Path):
    f = tmp_path / "rec.mp4"
    # A typo'd placeholder must not crash the upload — template is used as-is.
    assert obs_module._render_display_name("Call {nope}", f) == "Call {nope}"


def test_read_config_display_name_template_from_env(obs_module, monkeypatch):
    monkeypatch.setenv("VTS_DISPLAY_NAME_TEMPLATE", "OBS: {filename}")
    cfg = obs_module._read_config(_empty_settings())
    assert cfg["display_name_template"] == "OBS: {filename}"


def test_multipart_omits_display_name_when_template_empty(obs_module, tmp_path: Path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    rendered = obs_module._render_display_name("", f)
    assert rendered == ""
    # The caller only adds the field when rendered is truthy; assert the
    # rendered value is what gates it (no display_name part for empty template).
    body, _ = obs_module._build_multipart_body(f, {"transcript": "true"})
    assert b'name="display_name"' not in body


# ---------------------------------------------------------------- notify

def test_notify_command_linux_with_notify_send(obs_module, monkeypatch):
    monkeypatch.setattr(obs_module.sys, "platform", "linux")
    monkeypatch.setattr(obs_module.shutil, "which",
                        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None)
    cmd = obs_module._notify_command("Title", "Body")
    assert cmd is not None
    assert cmd[0] == "notify-send"
    assert "Title" in cmd and "Body" in cmd


def test_notify_command_linux_without_notify_send(obs_module, monkeypatch):
    monkeypatch.setattr(obs_module.sys, "platform", "linux")
    monkeypatch.setattr(obs_module.shutil, "which", lambda _name: None)
    assert obs_module._notify_command("Title", "Body") is None


def test_notify_command_macos_uses_osascript(obs_module, monkeypatch):
    monkeypatch.setattr(obs_module.sys, "platform", "darwin")
    cmd = obs_module._notify_command("VTS", "Done")
    assert cmd is not None
    assert cmd[0] == "osascript"
    # Both title and body must end up inside the AppleScript expression.
    joined = " ".join(cmd)
    assert "Done" in joined and "VTS" in joined


def test_notify_command_windows_uses_powershell(obs_module, monkeypatch):
    monkeypatch.setattr(obs_module.sys, "platform", "win32")
    monkeypatch.setattr(obs_module.shutil, "which",
                        lambda name: "powershell.exe" if name == "powershell.exe" else None)
    cmd = obs_module._notify_command("Title", "Body")
    assert cmd is not None
    assert cmd[0] == "powershell.exe"
    # The PS script should mention both strings.
    ps = cmd[-1]
    assert "Title" in ps and "Body" in ps
    # Belt-and-braces against PS console flashing: -WindowStyle Hidden.
    # (The real fix is CREATE_NO_WINDOW; this is the second line of defence.)
    assert "Hidden" in cmd


def test_notify_passes_create_no_window_flag_only_on_windows(obs_module, monkeypatch):
    """The flash-suppressing CREATE_NO_WINDOW flag must NOT leak into the
    subprocess call on non-Windows platforms — `creationflags=` is a
    Windows-only kwarg and crashes everywhere else."""
    captured: dict[str, object] = {}

    def _capture_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return None

    monkeypatch.setattr(obs_module.subprocess, "run", _capture_run)

    # Linux: no creationflags expected.
    monkeypatch.setattr(obs_module.sys, "platform", "linux")
    monkeypatch.setattr(obs_module.shutil, "which",
                        lambda _name: "/usr/bin/notify-send")
    obs_module._notify("T", "B", enabled=True)
    assert "creationflags" not in captured["kwargs"]

    # Windows: flag is set and matches the documented constant.
    captured.clear()
    monkeypatch.setattr(obs_module.sys, "platform", "win32")
    monkeypatch.setattr(obs_module.shutil, "which",
                        lambda _name: "powershell.exe")
    obs_module._notify("T", "B", enabled=True)
    assert captured["kwargs"].get("creationflags") == 0x08000000


def test_notify_command_unknown_platform_returns_none(obs_module, monkeypatch):
    monkeypatch.setattr(obs_module.sys, "platform", "freebsd14")
    assert obs_module._notify_command("Title", "Body") is None


def test_notify_disabled_does_not_run_subprocess(obs_module, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(obs_module.subprocess, "run",
                        lambda *a, **kw: calls.append(list(a[0])) or None)
    obs_module._notify("T", "B", enabled=False)
    assert calls == []


def test_notify_swallows_subprocess_errors(obs_module, monkeypatch):
    """A broken notifier (timeout, missing binary, etc.) must NOT interrupt
    the upload thread — _notify swallows every OSError / SubprocessError."""
    monkeypatch.setattr(obs_module.sys, "platform", "linux")
    monkeypatch.setattr(obs_module.shutil, "which",
                        lambda _name: "/usr/bin/notify-send")

    def _boom(*_a, **_kw):
        raise OSError("simulated broken pipe")
    monkeypatch.setattr(obs_module.subprocess, "run", _boom)
    # Must not raise.
    obs_module._notify("T", "B", enabled=True)


def test_notify_no_op_when_no_notifier_available(obs_module, monkeypatch):
    """Linux without notify-send: enabled=True but no command → no subprocess."""
    monkeypatch.setattr(obs_module.sys, "platform", "linux")
    monkeypatch.setattr(obs_module.shutil, "which", lambda _name: None)
    calls: list[object] = []
    monkeypatch.setattr(obs_module.subprocess, "run",
                        lambda *a, **kw: calls.append(a) or None)
    obs_module._notify("T", "B", enabled=True)
    assert calls == []
