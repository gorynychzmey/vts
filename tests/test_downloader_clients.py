from vts.services.downloader import (
    _build_youtube_client_candidates,
    _is_youtube_url,
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

