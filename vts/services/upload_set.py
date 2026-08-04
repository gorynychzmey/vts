"""Validate a multi-file upload set (vts-vm0).

A set must be all-video or all-audio: mixing them has no coherent combined
artefact. Video parts are joined by stream copy, which silently corrupts when
parameters differ — measured, 640x480 + 1280x720 yields 4.82s for 2+2s with
broken DTS, and mismatched audio sample rates yield 4.38s with NO ffmpeg error
at all. So compatibility is checked from probed parameters up front rather than
by trusting ffmpeg's exit code.
"""
from __future__ import annotations

from pathlib import Path

from vts.services.media import MediaProbe

VIDEO_SUFFIXES: frozenset[str] = frozenset(
    {".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m4v"}
)
AUDIO_SUFFIXES: frozenset[str] = frozenset(
    {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma"}
)


class UploadSetError(ValueError):
    """The set cannot be processed as one recording. Message is user-facing."""


def classify_suffixes(filenames: list[str]) -> str:
    """Return "video" or "audio" for the set, or raise UploadSetError.

    Cheap enough to run at init, before any bytes are transferred.
    """
    video, audio, unsupported = [], [], []
    for name in filenames:
        suffix = Path(name).suffix.lower()
        if suffix in VIDEO_SUFFIXES:
            video.append(name)
        elif suffix in AUDIO_SUFFIXES:
            audio.append(name)
        else:
            unsupported.append(name)

    if unsupported:
        raise UploadSetError(f"Unsupported file type: {', '.join(unsupported)}")
    if video and audio:
        raise UploadSetError(
            "A set must be either all video or all audio, not both. "
            f"Video: {', '.join(video)}. Audio: {', '.join(audio)}."
        )
    return "video" if video else "audio"


def verify_probes(kind: str, probes: list[tuple[str, MediaProbe]]) -> None:
    """Check probed content against the suffix grouping, and video parts
    against each other. Raises UploadSetError naming the offending file."""
    for name, probe in probes:
        if kind == "video" and not probe.has_video:
            raise UploadSetError(f"{name} has no video stream, but the set is a video set.")
        if kind == "audio" and probe.has_video:
            raise UploadSetError(f"{name} contains video, but the set is an audio set.")

    if kind != "video" or len(probes) < 2:
        return

    first_name, first = probes[0]
    for name, probe in probes[1:]:
        if probe.video_signature() != first.video_signature():
            raise UploadSetError(
                f"{name} does not match {first_name}: video is "
                f"{probe.video_codec} {probe.width}x{probe.height} @ {probe.frame_rate} "
                f"vs {first.video_codec} {first.width}x{first.height} @ {first.frame_rate}. "
                "Video parts must share codec, resolution and frame rate."
            )
        if probe.audio_signature() != first.audio_signature():
            raise UploadSetError(
                f"{name} does not match {first_name}: audio is "
                f"{probe.audio_codec} {probe.sample_rate} Hz {probe.channels}ch "
                f"vs {first.audio_codec} {first.sample_rate} Hz {first.channels}ch. "
                "Video parts must share audio codec, sample rate and channel count."
            )
