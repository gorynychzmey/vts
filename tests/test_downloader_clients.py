import logging

import pytest

from vts.services.downloader import (
    _build_youtube_client_candidates,
    _extract_download_progress,
    _is_youtube_url,
    _run_download_with_client_resolution,
    _with_youtube_player_client,
)


def test_is_youtube_url() -> None:
    assert _is_youtube_url("https://youtube.com/watch?v=abc")
    assert _is_youtube_url("https://www.youtube.com/live/abc")
    assert _is_youtube_url("https://youtu.be/abc")
    assert not _is_youtube_url("https://example.com/video")


def test_build_youtube_client_candidates_prefers_saved_client() -> None:
    candidates = _build_youtube_client_candidates(
        preferred_client="ios",
        configured_client=None,
    )
    assert candidates[0] == "ios"
    assert "android_vr" in candidates
    assert len(candidates) == len(set(candidates))


def test_build_youtube_client_candidates_configured_override() -> None:
    candidates = _build_youtube_client_candidates(
        preferred_client="ios",
        configured_client="web",
    )
    assert candidates == ["web"]


def test_with_youtube_player_client_preserves_other_extractor_args() -> None:
    options = {
        "extractor_args": {
            "youtube": {
                "po_token": ["web+token"],
            }
        }
    }
    updated = _with_youtube_player_client(options, "android_vr")
    assert updated["extractor_args"]["youtube"]["po_token"] == ["web+token"]
    assert updated["extractor_args"]["youtube"]["player_client"] == ["android_vr"]


def test_run_download_with_client_resolution_skips_fallback_for_live_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _fake_run_download(**kwargs: object) -> None:
        options = kwargs.get("ydl_opts")
        extractor_args = options.get("extractor_args", {}) if isinstance(options, dict) else {}
        youtube_args = extractor_args.get("youtube", {}) if isinstance(extractor_args, dict) else {}
        player_client = youtube_args.get("player_client", [""])[0]
        calls.append(str(player_client))
        raise RuntimeError("ERROR: [youtube] abc: This live event will begin in a few moments.")

    monkeypatch.setattr("vts.services.downloader._run_download", _fake_run_download)

    with pytest.raises(RuntimeError, match="live event will begin"):
        _run_download_with_client_resolution(
            url="https://youtube.com/watch?v=abc",
            outtmpl="/tmp/out.%(ext)s",
            ydl_opts={},
            phase="video",
            progress_cb=lambda phase, payload: None,
            logger=logging.getLogger("test_downloader_live_not_started"),
            preferred_youtube_client="ios",
            configured_youtube_client=None,
        )

    assert calls == ["ios"]


def test_extract_download_progress_uses_percent_fallback() -> None:
    progress, downloaded, total = _extract_download_progress(
        {
            "downloaded_bytes": 0,
            "total_bytes": None,
            "total_bytes_estimate": None,
            "_percent_str": " 10.6% ",
        }
    )
    assert progress == pytest.approx(0.106)
    assert downloaded == 0
    assert total == 0
