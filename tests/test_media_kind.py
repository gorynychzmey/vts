from __future__ import annotations

from pathlib import Path

import pytest

from vts.services.media_kind import media_content_type, media_kind


@pytest.mark.parametrize(
    "name,expected",
    [
        ("video.mp4", "video"),
        ("clip.MKV", "video"),
        ("recording.webm", "video"),
        ("movie.mov", "video"),
        ("stream.ts", "video"),
        ("song.mp3", "audio"),
        ("podcast.m4a", "audio"),
        ("voice.wav", "audio"),
        ("track.flac", "audio"),
        ("audio.original.opus", "audio"),
        ("audio.original.OGG", "audio"),
        # audio_only extraction from a video source still lands on
        # an audio container — extension is what we trust.
        ("audio.original.mp3", "audio"),
    ],
)
def test_media_kind_dispatches_by_suffix(name: str, expected: str) -> None:
    assert media_kind(Path(name)) == expected


def test_media_kind_unknown_defaults_to_video() -> None:
    # Upload validation rejects unknown suffixes before storage, so this
    # branch is unreachable in practice — but defaulting to <video> is the
    # safer choice because the browser falls back to "no playable source"
    # rather than silently misclassifying as audio.
    assert media_kind(Path("mystery.xyz")) == "video"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("video.mp4", "video/mp4"),
        ("clip.mkv", "video/x-matroska"),
        ("recording.webm", "video/webm"),
        ("song.mp3", "audio/mpeg"),
        ("voice.wav", "audio/wav"),
        ("track.flac", "audio/flac"),
        ("podcast.m4a", "audio/mp4"),
        ("audio.opus", "audio/ogg"),
    ],
)
def test_media_content_type_known_suffixes(name: str, expected: str) -> None:
    assert media_content_type(Path(name)) == expected


def test_media_content_type_unknown_falls_back() -> None:
    assert media_content_type(Path("mystery.xyz")) == "application/octet-stream"


def test_media_content_type_is_case_insensitive() -> None:
    assert media_content_type(Path("VIDEO.MP4")) == "video/mp4"
