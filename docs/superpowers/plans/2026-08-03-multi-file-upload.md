# Multi-file Upload Implementation Plan (vts-vm0)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user upload several media files in one go and have them processed as a single recording — one transcript, one summary, and for video sets one playable combined video.

**Architecture:** N files are uploaded under one chunked-upload session, ordered server-side, and concatenated inside the existing `extract_audio` pipeline step into the single `audio_16k.wav` the rest of the DAG already consumes. Video sets additionally get a stream-copied `video.mkv` for the player. No schema migration: file-boundary metadata lives in `Task.options`.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy (async), Pydantic v2, pytest + pytest-asyncio, ffmpeg/ffprobe, vanilla JS frontend, Playwright for browser scenarios.

**Spec:** `docs/superpowers/specs/2026-08-03-multi-file-upload-design.md`

## Global Constraints

- Run tests with the venv interpreter: `.venv/bin/python -m pytest` (this repo uses `requirements.txt`, not `uv`).
- Bump `__version__` in `vts/__init__.py` before committing (patch bump per task is fine).
- `Task.options` is a JSON column: **always reassign**, never mutate in place (`options = dict(task.options or {}); options[...] = ...; task.options = options`).
- Set limits: sum of all files ≤ `settings.max_upload_bytes` (2 GiB, unchanged); file count ≤ new `settings.upload_max_files` (default 10). Both enforced at `init`, before any bytes transfer.
- Mixed video+audio sets are rejected. Video suffixes: `.mp4 .mkv .webm .avi .mov .wmv .flv .ts .m4v`. Audio suffixes: `.mp3 .m4a .aac .ogg .opus .flac .wav .wma`.
- Video concat is stream-copy only. Parts must match on: video codec, width, height, frame rate; audio codec, sample rate, channel count. Mismatch → reject naming the parameter.
- Ordering precedence: container `creation_time` → browser `lastModified` → natural filename sort.
- Any file that fails to probe or normalise fails the **whole** task, naming the file.
- New browser scenarios must be verified to FAIL against pre-change code (repo convention), then pass.

---

### Task 1: Media probe helper — duration, creation_time, stream parameters

**Files:**
- Modify: `vts/services/media.py` (append after `probe_duration`, which ends at line 57)
- Test: `tests/test_media_probe.py` (create)

**Interfaces:**
- Consumes: nothing (leaf task).
- Produces:
  - `@dataclass(frozen=True) MediaProbe` with fields: `duration_sec: float`, `creation_time: str | None`, `has_video: bool`, `video_codec: str | None`, `width: int | None`, `height: int | None`, `frame_rate: str | None`, `audio_codec: str | None`, `sample_rate: int | None`, `channels: int | None`
  - `probe_media(path: Path) -> MediaProbe` — raises `RuntimeError` if ffprobe fails
  - `MediaProbe.video_signature() -> tuple` and `MediaProbe.audio_signature() -> tuple` for compatibility comparison

- [ ] **Step 1: Write the failing test**

```python
"""ffprobe-backed media inspection (vts-vm0).

Ordering and video-compatibility both need facts about the file that only
ffprobe can supply, so they share one probe rather than shelling out twice.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vts.services.media import MediaProbe, probe_media


def _make(path: Path, *, video: bool, seconds: int = 1, size: str = "320x240",
          rate: int = 25, sample_rate: int = 44100, creation: str | None = None) -> Path:
    cmd = ["ffmpeg", "-y"]
    if video:
        cmd += ["-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={seconds}"]
    cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate={sample_rate}"]
    # Each container takes its own codecs: WebM permits only VP8/VP9 with
    # Vorbis/Opus, and wav is PCM. Muxing h264+aac into webm produces NO FILE
    # at all — which is how an early measurement mistook "could not build the
    # file" for "the container cannot store creation_time".
    name = str(path)
    if video:
        if name.endswith(".webm"):
            cmd += ["-c:v", "libvpx-vp9"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "ultrafast"]
    if name.endswith(".webm"):
        cmd += ["-c:a", "libvorbis"]
    elif name.endswith(".wav"):
        cmd += ["-c:a", "pcm_s16le"]
    else:
        cmd += ["-c:a", "aac"]
    cmd += ["-ar", str(sample_rate), "-shortest"]
    if creation:
        cmd += ["-metadata", f"creation_time={creation}"]
    cmd += [str(path)]
    subprocess.run(cmd, capture_output=True, check=True)
    return path


def test_probe_reads_duration_and_streams_for_video(tmp_path):
    f = _make(tmp_path / "v.mp4", video=True, seconds=2, size="640x480", rate=25)
    probe = probe_media(f)
    assert probe.has_video is True
    assert probe.width == 640 and probe.height == 480
    assert probe.video_codec == "h264"
    assert probe.audio_codec == "aac"
    assert probe.sample_rate == 44100
    assert 1.8 < probe.duration_sec < 2.3


def test_probe_reads_audio_only_file(tmp_path):
    f = _make(tmp_path / "a.m4a", video=False, seconds=1)
    probe = probe_media(f)
    assert probe.has_video is False
    assert probe.width is None and probe.height is None
    assert probe.audio_codec == "aac"


def test_probe_reads_creation_time_when_present(tmp_path):
    f = _make(tmp_path / "c.mp4", video=True, creation="2026-08-01T10:00:00.000000Z")
    assert probe_media(f).creation_time == "2026-08-01T10:00:00.000000Z"


def test_probe_returns_none_creation_time_when_absent(tmp_path):
    """A container that genuinely cannot store the tag must probe as None.

    wav is used because it really does drop creation_time: the assertion holds
    even though the tag is explicitly SET on the ffmpeg command line, so the
    test cannot pass merely because nobody asked for the tag.
    """
    f = _make(tmp_path / "c.wav", video=False, creation="2026-08-01T10:00:00.000000Z")
    assert probe_media(f).creation_time is None


def test_probe_reads_creation_time_from_webm(tmp_path):
    """webm DOES carry creation_time. Pinned because an early measurement
    claimed otherwise: that probe used h264+aac, which WebM cannot mux, so no
    file was produced and the empty output was misread as unsupported."""
    f = _make(tmp_path / "c.webm", video=True, creation="2026-08-01T10:00:00.000000Z")
    assert probe_media(f).creation_time == "2026-08-01T10:00:00.000000Z"


def test_signatures_match_for_identical_parameters(tmp_path):
    a = _make(tmp_path / "a.mp4", video=True, size="640x480", rate=25)
    b = _make(tmp_path / "b.mp4", video=True, size="640x480", rate=25)
    pa, pb = probe_media(a), probe_media(b)
    assert pa.video_signature() == pb.video_signature()
    assert pa.audio_signature() == pb.audio_signature()


def test_video_signature_differs_on_resolution(tmp_path):
    a = _make(tmp_path / "a.mp4", video=True, size="640x480")
    b = _make(tmp_path / "b.mp4", video=True, size="1280x720")
    assert probe_media(a).video_signature() != probe_media(b).video_signature()


def test_audio_signature_differs_on_sample_rate(tmp_path):
    """This is the case ffmpeg does NOT report as an error — concat -c copy
    silently produces a wrong-duration file (measured: 4.38s for 2+2s)."""
    a = _make(tmp_path / "a.mp4", video=True, sample_rate=44100)
    b = _make(tmp_path / "b.mp4", video=True, sample_rate=48000)
    assert probe_media(a).audio_signature() != probe_media(b).audio_signature()


def test_probe_raises_on_unreadable_file(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a media file")
    with pytest.raises(RuntimeError, match="ffprobe"):
        probe_media(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_media_probe.py -v`
Expected: FAIL — `ImportError: cannot import name 'MediaProbe' from 'vts.services.media'`

- [ ] **Step 3: Write minimal implementation**

Append to `vts/services/media.py` (after `probe_duration`):

```python
@dataclass(frozen=True)
class MediaProbe:
    """What one ffprobe call tells us about an uploaded file (vts-vm0).

    Ordering wants `creation_time`; video concat compatibility wants the stream
    parameters. One probe serves both.
    """

    duration_sec: float
    creation_time: str | None
    has_video: bool
    video_codec: str | None
    width: int | None
    height: int | None
    frame_rate: str | None
    audio_codec: str | None
    sample_rate: int | None
    channels: int | None

    def video_signature(self) -> tuple:
        return (self.video_codec, self.width, self.height, self.frame_rate)

    def audio_signature(self) -> tuple:
        return (self.audio_codec, self.sample_rate, self.channels)


def probe_media(path: Path) -> MediaProbe:
    """Inspect `path` with a single ffprobe call.

    Raises RuntimeError when ffprobe cannot read the file — the caller turns
    that into a task failure naming the file (vts-vm0).
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:format_tags=creation_time"
                         ":stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(f"ffprobe failed for {path.name}{': ' + tail if tail else ''}")
    payload = json.loads(proc.stdout or "{}")
    fmt = payload.get("format", {}) or {}
    streams = payload.get("streams", []) or []

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    raw_duration = fmt.get("duration")
    try:
        duration = float(raw_duration) if raw_duration is not None else 0.0
    except (TypeError, ValueError):
        duration = 0.0

    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return MediaProbe(
        duration_sec=duration,
        creation_time=(fmt.get("tags") or {}).get("creation_time"),
        has_video=video is not None,
        video_codec=(video or {}).get("codec_name"),
        width=_int((video or {}).get("width")),
        height=_int((video or {}).get("height")),
        frame_rate=(video or {}).get("r_frame_rate"),
        audio_codec=(audio or {}).get("codec_name"),
        sample_rate=_int((audio or {}).get("sample_rate")),
        channels=_int((audio or {}).get("channels")),
    )
```

Add to the imports at the top of `vts/services/media.py` if not already present:

```python
from dataclasses import dataclass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_media_probe.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_media_probe.py vts/services/media.py vts/__init__.py
git commit -m "feat(media): probe_media — duration, creation_time, stream signatures (vts-vm0)"
```

---

### Task 2: Ordering resolution

**Files:**
- Create: `vts/services/upload_order.py`
- Test: `tests/test_upload_order.py`

**Interfaces:**
- Consumes: `MediaProbe` from Task 1 (only `.creation_time` is read; tests construct it directly).
- Produces:
  - `natural_key(name: str) -> list` — digit runs compared numerically
  - `resolve_order(entries: list[dict]) -> tuple[list[dict], str]` — returns `(ordered_entries, order_source)` where `order_source` is `"creation_time" | "last_modified" | "filename"`. Each entry is a dict with at least `{"filename": str, "last_modified": int | None, "creation_time": str | None}`; entries are returned unchanged apart from order.

- [ ] **Step 1: Write the failing test**

```python
"""Concat order for a multi-file upload (vts-vm0).

Order is decided server-side with no user step, so the fallback chain has to be
right: container creation_time, then the browser's lastModified, then natural
filename sort. Measured container coverage is in the spec — ogg/opus/wav and
avi/wmv/ts carry no creation_time at all, and opus is what Telegram and
WhatsApp voice messages use, so the filename rule is load-bearing.
"""
from __future__ import annotations

from vts.services.upload_order import natural_key, resolve_order


def _e(filename, creation_time=None, last_modified=None):
    return {"filename": filename, "creation_time": creation_time, "last_modified": last_modified}


def test_creation_time_wins_when_all_present():
    entries = [
        _e("b.mp4", creation_time="2026-08-01T10:05:00.000000Z", last_modified=999),
        _e("a.mp4", creation_time="2026-08-01T10:00:00.000000Z", last_modified=1),
    ]
    ordered, source = resolve_order(entries)
    assert [e["filename"] for e in ordered] == ["a.mp4", "b.mp4"]
    assert source == "creation_time"


def test_falls_back_to_last_modified_when_any_creation_time_missing():
    entries = [
        _e("b.mp4", creation_time="2026-08-01T10:05:00.000000Z", last_modified=2000),
        _e("a.opus", creation_time=None, last_modified=1000),
    ]
    ordered, source = resolve_order(entries)
    assert [e["filename"] for e in ordered] == ["a.opus", "b.mp4"]
    assert source == "last_modified"


def test_falls_back_to_natural_filename_when_no_dates():
    entries = [_e("rec_10.opus"), _e("rec_9.opus"), _e("rec_1.opus")]
    ordered, source = resolve_order(entries)
    assert [e["filename"] for e in ordered] == ["rec_1.opus", "rec_9.opus", "rec_10.opus"]
    assert source == "filename"


def test_natural_sort_beats_lexicographic():
    names = ["part10.wav", "part2.wav", "part1.wav"]
    assert sorted(names, key=natural_key) == ["part1.wav", "part2.wav", "part10.wav"]
    assert sorted(names) == ["part1.wav", "part10.wav", "part2.wav"]


def test_telegram_and_whatsapp_names_order_correctly():
    tg = [_e("audio_2026-08-01_10-15-03.ogg"), _e("audio_2026-08-01_09-58-11.ogg")]
    assert [e["filename"] for e in resolve_order(tg)[0]] == [
        "audio_2026-08-01_09-58-11.ogg", "audio_2026-08-01_10-15-03.ogg",
    ]
    wa = [_e("PTT-20260801-WA0011.opus"), _e("PTT-20260801-WA0002.opus")]
    assert [e["filename"] for e in resolve_order(wa)[0]] == [
        "PTT-20260801-WA0002.opus", "PTT-20260801-WA0011.opus",
    ]


def test_identical_last_modified_falls_through_to_filename():
    """Downloading a set in one go gives every file the same mtime — that is no
    signal at all, so it must not be treated as an order."""
    entries = [_e("rec_10.opus", last_modified=5000), _e("rec_9.opus", last_modified=5000)]
    ordered, source = resolve_order(entries)
    assert [e["filename"] for e in ordered] == ["rec_9.opus", "rec_10.opus"]
    assert source == "filename"


def test_single_file_is_returned_unchanged():
    entries = [_e("only.mp4")]
    ordered, source = resolve_order(entries)
    assert [e["filename"] for e in ordered] == ["only.mp4"]
    assert source == "filename"


def test_unparseable_creation_time_falls_back():
    entries = [_e("b.mp4", creation_time="not-a-date"), _e("a.mp4", creation_time="also-bad")]
    ordered, source = resolve_order(entries)
    assert [e["filename"] for e in ordered] == ["a.mp4", "b.mp4"]
    assert source == "filename"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_upload_order.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vts.services.upload_order'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Decide the order in which uploaded files are concatenated (vts-vm0).

The order files are joined in is the order they are heard in, and it is decided
server-side with no user step — so the fallback chain matters. Preference:

  1. container `creation_time` — the true recording time, survives copying
  2. the browser's `lastModified` — present for every file, but for a file
     downloaded from a messenger it is the DOWNLOAD time
  3. natural filename sort — digit runs compared numerically

Step 3 is load-bearing rather than a formality: ogg/opus/wav and
avi/wmv/ts carry no `creation_time` at all (measured — see the spec), and opus
is exactly what Telegram and WhatsApp voice messages use.
"""
from __future__ import annotations

import re
from datetime import datetime

_DIGITS = re.compile(r"(\d+)")


def natural_key(name: str) -> list:
    """Sort key where digit runs compare numerically: rec_9 before rec_10."""
    return [int(part) if part.isdigit() else part.lower() for part in _DIGITS.split(name)]


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def resolve_order(entries: list[dict]) -> tuple[list[dict], str]:
    """Return (ordered entries, which rule decided the order).

    A signal is used only when it is present AND discriminating for every file:
    a set downloaded in one go shares one mtime, which is no order at all, and
    silently trusting it would scramble the recording.
    """
    items = list(entries)

    stamps = [_parse(e.get("creation_time")) for e in items]
    if all(s is not None for s in stamps) and len({s for s in stamps} ) == len(items):
        return [e for _, e in sorted(zip(stamps, items), key=lambda pair: pair[0])], "creation_time"

    mtimes = [e.get("last_modified") for e in items]
    if all(m is not None for m in mtimes) and len(set(mtimes)) == len(items):
        return [e for _, e in sorted(zip(mtimes, items), key=lambda pair: pair[0])], "last_modified"

    return sorted(items, key=lambda e: natural_key(e.get("filename", ""))), "filename"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_upload_order.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_upload_order.py vts/services/upload_order.py vts/__init__.py
git commit -m "feat(uploads): resolve concat order — creation_time, mtime, natural name (vts-vm0)"
```

---

### Task 3: Set validation — kind split, mixed rejection, video compatibility

**Files:**
- Create: `vts/services/upload_set.py`
- Test: `tests/test_upload_set.py`

**Interfaces:**
- Consumes: `MediaProbe` from Task 1.
- Produces:
  - `VIDEO_SUFFIXES: frozenset[str]`, `AUDIO_SUFFIXES: frozenset[str]`
  - `class UploadSetError(ValueError)` — message is user-facing
  - `classify_suffixes(filenames: list[str]) -> str` — returns `"video"` or `"audio"`; raises `UploadSetError` on a mixed or unsupported set
  - `verify_probes(kind: str, probes: list[tuple[str, MediaProbe]]) -> None` — raises `UploadSetError` when probed content contradicts the suffix grouping, or when video parts are incompatible

- [ ] **Step 1: Write the failing test**

```python
"""Validation for a multi-file upload set (vts-vm0).

Two separate gates: the suffix split (cheap, runs at init before any bytes
move) and the probe check (authoritative, runs at finalize once bytes exist).
"""
from __future__ import annotations

import pytest

from vts.services.media import MediaProbe
from vts.services.upload_set import (
    UploadSetError,
    classify_suffixes,
    verify_probes,
)


def _probe(*, has_video=True, w=640, h=480, rate="25/1", vcodec="h264",
           acodec="aac", sr=44100, ch=1, duration=1.0):
    return MediaProbe(
        duration_sec=duration, creation_time=None, has_video=has_video,
        video_codec=vcodec if has_video else None,
        width=w if has_video else None, height=h if has_video else None,
        frame_rate=rate if has_video else None,
        audio_codec=acodec, sample_rate=sr, channels=ch,
    )


def test_all_video_classifies_as_video():
    assert classify_suffixes(["a.mp4", "b.mkv", "c.mov"]) == "video"


def test_all_audio_classifies_as_audio():
    assert classify_suffixes(["a.mp3", "b.opus", "c.wav"]) == "audio"


def test_mixed_set_is_rejected_naming_the_files():
    with pytest.raises(UploadSetError) as excinfo:
        classify_suffixes(["talk.mp4", "note.mp3"])
    message = str(excinfo.value)
    assert "talk.mp4" in message and "note.mp3" in message


def test_unsupported_suffix_is_rejected():
    with pytest.raises(UploadSetError, match="doc.pdf"):
        classify_suffixes(["a.mp4", "doc.pdf"])


def test_compatible_video_set_passes():
    verify_probes("video", [("a.mp4", _probe()), ("b.mp4", _probe())])


def test_video_set_rejected_on_resolution_mismatch():
    with pytest.raises(UploadSetError) as excinfo:
        verify_probes("video", [("a.mp4", _probe(w=640, h=480)), ("b.mp4", _probe(w=1280, h=720))])
    assert "b.mp4" in str(excinfo.value)


def test_video_set_rejected_on_frame_rate_mismatch():
    with pytest.raises(UploadSetError):
        verify_probes("video", [("a.mp4", _probe(rate="25/1")), ("b.mp4", _probe(rate="30/1"))])


def test_video_set_rejected_on_audio_sample_rate_mismatch():
    """ffmpeg reports NO error for this case — concat -c copy just produces a
    wrong-duration file (measured 4.38s for 2+2s), so we must catch it here."""
    with pytest.raises(UploadSetError):
        verify_probes("video", [("a.mp4", _probe(sr=44100)), ("b.mp4", _probe(sr=48000))])


def test_audio_set_ignores_video_parameters():
    """Audio parts are re-encoded to 16k mono anyway, so differing sample rates
    are fine — only video sets need matching parameters."""
    verify_probes("audio", [
        ("a.mp3", _probe(has_video=False, sr=44100)),
        ("b.wav", _probe(has_video=False, sr=48000)),
    ])


def test_video_suffix_without_video_stream_is_rejected():
    """An extension does not prove content: a .mkv may carry audio only."""
    with pytest.raises(UploadSetError, match="b.mkv"):
        verify_probes("video", [("a.mp4", _probe()), ("b.mkv", _probe(has_video=False))])


def test_audio_set_with_a_video_stream_is_rejected():
    with pytest.raises(UploadSetError, match="b.m4a"):
        verify_probes("audio", [("a.mp3", _probe(has_video=False)), ("b.m4a", _probe(has_video=True))])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_upload_set.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vts.services.upload_set'`

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_upload_set.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_upload_set.py vts/services/upload_set.py vts/__init__.py
git commit -m "feat(uploads): validate set kind and video concat compatibility (vts-vm0)"
```

---

### Task 4: Concatenation helpers

**Files:**
- Modify: `vts/services/media.py`
- Test: `tests/test_media_concat.py` (create)

**Interfaces:**
- Consumes: `run_ffmpeg` (existing, `vts/services/media.py:1`), `extract_audio_16k_mono` (existing, line 60).
- Produces:
  - `concat_to_audio_16k_mono(inputs: list[Path], output_wav: Path, log_path: Path, work_dir: Path) -> list[float]` — normalises each input to 16 kHz mono PCM, concatenates in the given order, returns per-input durations in seconds
  - `concat_video_stream_copy(inputs: list[Path], output: Path, log_path: Path, work_dir: Path) -> None` — stream-copy concat; assumes compatibility was already verified

- [ ] **Step 1: Write the failing test**

```python
"""Concatenating uploaded parts into the single artefacts the pipeline uses.

Durations are the assertion that matters: stream-copy concat of mismatched
inputs produces a WRONG DURATION without ffmpeg raising an error, so a test
that only checked the exit code would pass on a broken file (vts-vm0).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from vts.services.media import (
    concat_to_audio_16k_mono,
    concat_video_stream_copy,
    probe_duration,
)


def _audio(path: Path, seconds: int, freq: int, sample_rate: int, channels: int) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate={sample_rate}",
         "-ac", str(channels), str(path)],
        capture_output=True, check=True,
    )
    return path


def _video(path: Path, seconds: int, freq: int) -> Path:
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=44100",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ar", "44100",
         "-shortest", str(path)],
        capture_output=True, check=True,
    )
    return path


def test_audio_concat_sums_durations_across_formats(tmp_path):
    """Heterogeneous inputs join losslessly once normalised — 2+3+1 = 6."""
    a = _audio(tmp_path / "a.mp3", 2, 440, 44100, 2)
    b = _audio(tmp_path / "b.wav", 3, 880, 48000, 1)
    c = _audio(tmp_path / "c.ogg", 1, 660, 22050, 2)
    out = tmp_path / "audio_16k.wav"

    durations = concat_to_audio_16k_mono([a, b, c], out, tmp_path / "log.txt", tmp_path)

    assert out.exists()
    assert abs(probe_duration(out) - 6.0) < 0.2
    assert len(durations) == 3
    assert abs(durations[0] - 2.0) < 0.2
    assert abs(sum(durations) - 6.0) < 0.2


def test_audio_concat_output_is_16k_mono(tmp_path):
    a = _audio(tmp_path / "a.mp3", 1, 440, 44100, 2)
    out = tmp_path / "audio_16k.wav"
    concat_to_audio_16k_mono([a], out, tmp_path / "log.txt", tmp_path)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels",
         "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    assert "16000" in probe.stdout and probe.stdout.strip().endswith("1")


def test_video_concat_sums_durations(tmp_path):
    a = _video(tmp_path / "a.mp4", 2, 440)
    b = _video(tmp_path / "b.mp4", 2, 880)
    out = tmp_path / "video.mkv"

    concat_video_stream_copy([a, b], out, tmp_path / "log.txt", tmp_path)

    assert out.exists()
    assert abs(probe_duration(out) - 4.0) < 0.3


def test_video_concat_preserves_the_video_stream(tmp_path):
    a = _video(tmp_path / "a.mp4", 1, 440)
    out = tmp_path / "video.mkv"
    concat_video_stream_copy([a], out, tmp_path / "log.txt", tmp_path)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    assert "video" in probe.stdout


def test_audio_concat_respects_input_order(tmp_path):
    """Order in equals order out — the whole ordering feature depends on it."""
    short = _audio(tmp_path / "short.wav", 1, 440, 16000, 1)
    long = _audio(tmp_path / "long.wav", 3, 880, 16000, 1)
    out = tmp_path / "audio_16k.wav"
    durations = concat_to_audio_16k_mono([long, short], out, tmp_path / "log.txt", tmp_path)
    assert abs(durations[0] - 3.0) < 0.2
    assert abs(durations[1] - 1.0) < 0.2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_media_concat.py -v`
Expected: FAIL — `ImportError: cannot import name 'concat_to_audio_16k_mono'`

- [ ] **Step 3: Write minimal implementation**

Append to `vts/services/media.py`:

```python
def _write_concat_list(inputs: list[Path], list_path: Path) -> None:
    """ffmpeg's concat demuxer input list. Single quotes are escaped per its
    own quoting rules, so a filename containing one cannot break the list."""
    lines = []
    for item in inputs:
        escaped = str(item.resolve()).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concat_to_audio_16k_mono(
    inputs: list[Path], output_wav: Path, log_path: Path, work_dir: Path
) -> list[float]:
    """Normalise each input to 16 kHz mono PCM, then join in the given order.

    Returns each input's duration, in the same order, so the caller can record
    file-boundary offsets. Normalising first is what makes heterogeneous
    sources safe to concatenate: verified, mp3 44.1k stereo + wav 48k mono +
    ogg 22k stereo of 2+3+1s produce exactly 6.000s (vts-vm0).
    """
    if not inputs:
        raise RuntimeError("No input files to concatenate")

    work_dir.mkdir(parents=True, exist_ok=True)
    normalized: list[Path] = []
    durations: list[float] = []
    for index, item in enumerate(inputs):
        target = work_dir / f"norm_{index:03d}.wav"
        if not target.exists():
            extract_audio_16k_mono(item, target, log_path)
        normalized.append(target)
        durations.append(probe_duration(target))

    if len(normalized) == 1:
        shutil.copyfile(normalized[0], output_wav)
        return durations

    list_path = work_dir / "concat_audio.txt"
    _write_concat_list(normalized, list_path)
    run_ffmpeg(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
         "-c", "copy", str(output_wav)],
        log_path,
    )
    return durations


def concat_video_stream_copy(
    inputs: list[Path], output: Path, log_path: Path, work_dir: Path
) -> None:
    """Join video parts without re-encoding.

    Callers MUST have verified parameter compatibility first
    (vts.services.upload_set.verify_probes): stream-copying mismatched inputs
    silently produces a wrong-duration file, and for differing audio sample
    rates ffmpeg does not even report an error (vts-vm0).
    """
    if not inputs:
        raise RuntimeError("No input files to concatenate")

    work_dir.mkdir(parents=True, exist_ok=True)
    if len(inputs) == 1:
        run_ffmpeg(["ffmpeg", "-y", "-i", str(inputs[0]), "-c", "copy", str(output)], log_path)
        return

    list_path = work_dir / "concat_video.txt"
    _write_concat_list(inputs, list_path)
    run_ffmpeg(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
         "-c", "copy", str(output)],
        log_path,
    )
```

Ensure `import shutil` is present at the top of `vts/services/media.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_media_concat.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_media_concat.py vts/services/media.py vts/__init__.py
git commit -m "feat(media): concat helpers for audio and stream-copy video (vts-vm0)"
```

---

### Task 5: UploadSession stores N parts

**Files:**
- Modify: `vts/services/upload_session.py`
- Test: `tests/test_upload_session_multi.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `part_name(index: int, suffix: str) -> str` — `"audio.original.000.mp4"` style
  - `UploadSession.part_path_for(artifacts_root, username, upload_id, index, suffix) -> Path`
  - `UploadSession.init_multi(artifacts_root, username, *, user_id, upload_id, files, kind, options, display_name, created_at) -> Path` where `files` is a list of `{"filename": str, "suffix": str, "total_size": int, "last_modified": int | None}`; writes meta with a `"files"` list, each entry gaining `"received": 0` and `"index": int`
  - `UploadSession.append_chunk_at(part_path, meta_path, data, index) -> int`
  - `UploadSession.finalize_multi(artifacts_root, username, upload_id, meta) -> list[Path]` — renames every `.part` to its final name, deletes the sidecar, returns final paths in index order

The existing single-file `init`/`part_path`/`append_chunk`/`finalize` stay untouched so the single-shot path keeps working.

- [ ] **Step 1: Write the failing test**

```python
"""Multi-part chunked upload storage (vts-vm0).

The existing single-file session names its staging file `audio.original<suffix>`
with no index, so N files in one session would overwrite each other. Parts are
indexed instead, and the index is the resolved concat order.
"""
from __future__ import annotations

import json
import uuid

from vts.services.upload_session import UploadSession, part_name


def _files():
    return [
        {"filename": "a.mp4", "suffix": ".mp4", "total_size": 10, "last_modified": 1000},
        {"filename": "b.mp4", "suffix": ".mp4", "total_size": 20, "last_modified": 2000},
    ]


def test_part_name_is_indexed():
    assert part_name(0, ".mp4") == "audio.original.000.mp4"
    assert part_name(12, ".opus") == "audio.original.012.opus"


def test_init_multi_creates_one_part_per_file(tmp_path):
    upload_id = uuid.uuid4()
    UploadSession.init_multi(
        tmp_path, "tester", user_id="u1", upload_id=upload_id, files=_files(),
        kind="video", options={"transcript": True}, display_name=None,
        created_at="2026-08-03T00:00:00+00:00",
    )
    meta = UploadSession.load(tmp_path, "tester", upload_id)
    assert meta["kind"] == "video"
    assert [f["filename"] for f in meta["files"]] == ["a.mp4", "b.mp4"]
    assert [f["index"] for f in meta["files"]] == [0, 1]
    for index in (0, 1):
        assert UploadSession.part_path_for(tmp_path, "tester", upload_id, index, ".mp4").exists()


def test_parts_do_not_collide(tmp_path):
    upload_id = uuid.uuid4()
    UploadSession.init_multi(
        tmp_path, "tester", user_id="u1", upload_id=upload_id, files=_files(),
        kind="video", options={}, display_name=None, created_at="2026-08-03T00:00:00+00:00",
    )
    p0 = UploadSession.part_path_for(tmp_path, "tester", upload_id, 0, ".mp4")
    p1 = UploadSession.part_path_for(tmp_path, "tester", upload_id, 1, ".mp4")
    assert p0 != p1


def test_append_chunk_at_tracks_per_file_progress(tmp_path):
    upload_id = uuid.uuid4()
    UploadSession.init_multi(
        tmp_path, "tester", user_id="u1", upload_id=upload_id, files=_files(),
        kind="video", options={}, display_name=None, created_at="2026-08-03T00:00:00+00:00",
    )
    meta_path = UploadSession.meta_path(tmp_path, "tester", upload_id)
    part = UploadSession.part_path_for(tmp_path, "tester", upload_id, 1, ".mp4")

    assert UploadSession.append_chunk_at(part, meta_path, b"12345", 1) == 5

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["files"][1]["received"] == 5
    assert meta["files"][0]["received"] == 0


def test_finalize_multi_renames_all_parts_in_order(tmp_path):
    upload_id = uuid.uuid4()
    UploadSession.init_multi(
        tmp_path, "tester", user_id="u1", upload_id=upload_id, files=_files(),
        kind="video", options={}, display_name=None, created_at="2026-08-03T00:00:00+00:00",
    )
    meta_path = UploadSession.meta_path(tmp_path, "tester", upload_id)
    for index, payload in ((0, b"aaa"), (1, b"bbbb")):
        part = UploadSession.part_path_for(tmp_path, "tester", upload_id, index, ".mp4")
        UploadSession.append_chunk_at(part, meta_path, payload, index)

    meta = UploadSession.load(tmp_path, "tester", upload_id)
    finals = UploadSession.finalize_multi(tmp_path, "tester", upload_id, meta)

    assert [p.name for p in finals] == ["audio.original.000.mp4", "audio.original.001.mp4"]
    assert all(p.exists() for p in finals)
    assert not any(p.with_suffix(p.suffix + ".part").exists() for p in finals)
    assert not meta_path.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_upload_session_multi.py -v`
Expected: FAIL — `ImportError: cannot import name 'part_name'`

- [ ] **Step 3: Write minimal implementation**

Add to `vts/services/upload_session.py`:

```python
def part_name(index: int, suffix: str) -> str:
    """Indexed staging name, e.g. audio.original.000.mp4.

    The single-file session used a fixed `audio.original<suffix>`, so N files in
    one session would overwrite each other. The index IS the concat order, so
    globbing the finals in name order gives the right sequence (vts-vm0).
    """
    return f"audio.original.{index:03d}{suffix}"
```

Add these methods to `UploadSession`:

```python
    @classmethod
    def part_path_for(
        cls, artifacts_root: Path, username: str, upload_id: uuid.UUID,
        index: int, suffix: str,
    ) -> Path:
        return cls._dir(artifacts_root, username, upload_id) / "media" / f"{part_name(index, suffix)}.part"

    @classmethod
    def init_multi(
        cls,
        artifacts_root: Path,
        username: str,
        *,
        user_id: str,
        upload_id: uuid.UUID,
        files: list[dict],
        kind: str,
        options: dict,
        display_name: str | None,
        created_at: str,
    ) -> Path:
        """Stage N parts under one session. `files` is already in concat order."""
        d = cls._dir(artifacts_root, username, upload_id)
        media = d / "media"
        media.mkdir(parents=True, exist_ok=True)

        entries = []
        for index, item in enumerate(files):
            (media / f"{part_name(index, item['suffix'])}.part").touch(exist_ok=True)
            entries.append({
                "index": index,
                "filename": item["filename"],
                "suffix": item["suffix"],
                "total_size": item["total_size"],
                "last_modified": item.get("last_modified"),
                "received": 0,
            })

        meta = {
            "upload_id": str(upload_id),
            "user_id": user_id,
            "username": username,
            "kind": kind,
            "files": entries,
            "total_size": sum(e["total_size"] for e in entries),
            "options": options,
            "display_name": display_name,
            "created_at": created_at,
        }
        cls.meta_path(artifacts_root, username, upload_id).write_text(
            json.dumps(meta, ensure_ascii=True, indent=2), encoding="utf-8"
        )
        return d

    @staticmethod
    def append_chunk_at(part_path: Path, meta_path: Path, data: bytes, index: int) -> int:
        with open(part_path, "ab") as f:
            f.write(data)
        new_size = part_path.stat().st_size
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            for entry in meta.get("files", []):
                if entry.get("index") == index:
                    entry["received"] = new_size
                    break
            meta_path.write_text(json.dumps(meta, ensure_ascii=True, indent=2), encoding="utf-8")
        except (ValueError, OSError):
            pass
        return new_size

    @classmethod
    def finalize_multi(
        cls, artifacts_root: Path, username: str, upload_id: uuid.UUID, meta: dict
    ) -> list[Path]:
        """Rename every staging part to its final name, in index order."""
        media = cls._dir(artifacts_root, username, upload_id) / "media"
        finals: list[Path] = []
        for entry in sorted(meta.get("files", []), key=lambda e: e["index"]):
            final = media / part_name(entry["index"], entry["suffix"])
            part = media / f"{final.name}.part"
            part.rename(final)  # same dir/volume -> atomic
            finals.append(final)
        try:
            cls.meta_path(artifacts_root, username, upload_id).unlink()
        except OSError:
            pass
        return finals
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_upload_session_multi.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_upload_session_multi.py vts/services/upload_session.py vts/__init__.py
git commit -m "feat(uploads): indexed multi-part staging in UploadSession (vts-vm0)"
```

---

### Task 6: API — init, patch, offset, finalize for sets

**Files:**
- Modify: `vts/api/schemas.py` (near `UploadInitRequest`, line 420)
- Modify: `vts/api/main.py` (`uploads_init` 2424, `uploads_offset` 2474, `uploads_patch` 2485, `uploads_finalize` 2507)
- Modify: `vts/core/config.py` (add `upload_max_files` near `max_upload_bytes`, line 329)
- Test: `tests/test_uploads_multi_api.py` (create)

**Interfaces:**
- Consumes: `classify_suffixes`, `UploadSetError` (Task 3); `UploadSession.init_multi`, `part_path_for`, `append_chunk_at` (Task 5).
- Produces:
  - `UploadFileSpec` Pydantic model: `filename: str`, `total_size: int`, `last_modified: int | None = None`
  - `UploadInitRequest.files: list[UploadFileSpec] | None = None` (existing single-file fields retained)
  - `UploadInitOut.files: list[dict] | None = None` — `[{"index": int, "filename": str}]` so the client knows which index to PATCH
  - `PATCH /api/uploads/{id}?offset=N&index=K`
  - `settings.upload_max_files: int = 10`

- [ ] **Step 1: Write the failing test**

```python
"""Multi-file upload API (vts-vm0)."""
from __future__ import annotations

import pytest

_HEADERS = {"X-Forwarded-User": "tester"}


def _files(*specs):
    return [{"filename": n, "total_size": s, "last_modified": m} for n, s, m in specs]


@pytest.mark.asyncio
async def test_init_accepts_a_video_set(client):
    r = await client.post("/api/uploads/init", json={
        "filename": "unused.mp4", "total_size": 0,
        "files": _files(("a.mp4", 10, 1000), ("b.mp4", 20, 2000)),
    }, headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["upload_id"]
    assert [f["filename"] for f in body["files"]] == ["a.mp4", "b.mp4"]
    assert [f["index"] for f in body["files"]] == [0, 1]


@pytest.mark.asyncio
async def test_init_rejects_mixed_video_and_audio(client):
    r = await client.post("/api/uploads/init", json={
        "filename": "unused.mp4", "total_size": 0,
        "files": _files(("talk.mp4", 10, 1), ("note.mp3", 10, 2)),
    }, headers=_HEADERS)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "talk.mp4" in detail and "note.mp3" in detail


@pytest.mark.asyncio
async def test_init_rejects_too_many_files(client, monkeypatch):
    from vts.core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("VTS_UPLOAD_MAX_FILES", "2")
    get_settings.cache_clear()
    r = await client.post("/api/uploads/init", json={
        "filename": "unused.mp4", "total_size": 0,
        "files": _files(("a.mp4", 1, 1), ("b.mp4", 1, 2), ("c.mp4", 1, 3)),
    }, headers=_HEADERS)
    get_settings.cache_clear()
    assert r.status_code == 422
    assert "at most 2" in r.json()["detail"]


@pytest.mark.asyncio
async def test_init_rejects_set_over_total_size(client):
    from vts.core.config import get_settings
    limit = get_settings().max_upload_bytes
    r = await client.post("/api/uploads/init", json={
        "filename": "unused.mp4", "total_size": 0,
        "files": _files(("a.mp4", limit, 1), ("b.mp4", limit, 2)),
    }, headers=_HEADERS)
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_patch_writes_to_the_indexed_part(client):
    init = await client.post("/api/uploads/init", json={
        "filename": "unused.mp4", "total_size": 0,
        "files": _files(("a.mp4", 3, 1000), ("b.mp4", 4, 2000)),
    }, headers=_HEADERS)
    upload_id = init.json()["upload_id"]

    r = await client.patch(
        f"/api/uploads/{upload_id}?offset=0&index=1", content=b"bbbb", headers=_HEADERS
    )
    assert r.status_code == 200, r.text
    assert r.json()["received"] == 4

    # Index 0 is untouched.
    off = await client.get(f"/api/uploads/{upload_id}/offset?index=0", headers=_HEADERS)
    assert off.json()["received"] == 0


@pytest.mark.asyncio
async def test_single_file_init_still_works(client):
    """The existing single-file path must be untouched."""
    r = await client.post("/api/uploads/init", json={
        "filename": "solo.mp4", "total_size": 100,
    }, headers=_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["upload_id"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_uploads_multi_api.py -v`
Expected: FAIL — the `files` field is ignored, so `body["files"]` raises `KeyError`

- [ ] **Step 3: Write minimal implementation**

In `vts/core/config.py`, after `max_upload_bytes` (line 329):

```python
    # Cap on how many files one upload set may contain (vts-vm0). The total
    # size limit is max_upload_bytes above, shared across the whole set.
    upload_max_files: int = 10
```

In `vts/api/schemas.py`, before `UploadInitRequest`:

```python
class UploadFileSpec(BaseModel):
    filename: str
    total_size: int
    # File.lastModified from the browser, epoch ms. Used only as the second
    # ordering signal, after the container's own creation_time (vts-vm0).
    last_modified: int | None = None
```

Add to `UploadInitRequest`:

```python
    # When present, this is a multi-file set and `filename`/`total_size` above
    # are ignored. Absent means the existing single-file flow (vts-vm0).
    files: list[UploadFileSpec] | None = None
```

Add to `UploadInitOut`:

```python
    files: list[dict] | None = None
```

In `vts/api/main.py`, inside `uploads_init`, immediately after the signature and before the existing `suffix = ...` line:

```python
        if payload.files:
            if len(payload.files) > settings.upload_max_files:
                raise HTTPException(
                    status_code=422,
                    detail=f"A set may contain at most {settings.upload_max_files} files",
                )
            if any(f.total_size <= 0 for f in payload.files):
                raise HTTPException(status_code=422, detail="total_size must be positive")
            combined = sum(f.total_size for f in payload.files)
            if combined > settings.max_upload_bytes:
                raise HTTPException(status_code=413, detail="Set exceeds maximum upload size")
            try:
                kind = classify_suffixes([f.filename for f in payload.files])
            except UploadSetError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            normalized_prompts = _normalize_prompts_json(payload.prompts)
            if normalized_prompts and not payload.transcript:
                raise HTTPException(status_code=422, detail="prompts require transcript")
            if payload.diarize and not payload.transcript:
                raise HTTPException(status_code=422, detail="diarize requires transcript")

            upload_id = uuid.uuid4()
            options = {
                "language": payload.language or None,
                "audio_only": False,
                "transcript": payload.transcript,
                "diarize": payload.diarize,
                "prompts": normalized_prompts,
            }
            # Order is resolved at finalize, once creation_time can be probed
            # from the actual bytes. Index here is just selection order.
            spec_files = [
                {
                    "filename": f.filename,
                    "suffix": Path(f.filename).suffix.lower(),
                    "total_size": f.total_size,
                    "last_modified": f.last_modified,
                }
                for f in payload.files
            ]
            UploadSession.init_multi(
                settings.artifacts_root, user.username,
                user_id=user.id, upload_id=upload_id, files=spec_files, kind=kind,
                options=options, display_name=normalize_display_name(payload.display_name),
                created_at=datetime.now(tz=timezone.utc).isoformat(),
            )
            return UploadInitOut(
                upload_id=str(upload_id),
                chunk_size=settings.upload_chunk_bytes,
                files=[{"index": i, "filename": f["filename"]} for i, f in enumerate(spec_files)],
            )
```

Replace the body of `uploads_offset` with:

```python
        uid, meta = _load_owned_session(settings, user, upload_id)
        if meta.get("files"):
            entry = _entry_for_index(meta, index)
            part = UploadSession.part_path_for(
                settings.artifacts_root, user.username, uid, entry["index"], entry["suffix"]
            )
            return UploadOffsetOut(
                received=UploadSession.received_bytes(part), total_size=entry["total_size"]
            )
        part = UploadSession.part_path(settings.artifacts_root, user.username, uid, meta["suffix"])
        return UploadOffsetOut(received=UploadSession.received_bytes(part), total_size=meta["total_size"])
```

and add `index: int = 0` to its parameters.

Add this helper next to `_load_owned_session`:

```python
    def _entry_for_index(meta: dict, index: int) -> dict:
        for entry in meta.get("files", []):
            if entry.get("index") == index:
                return entry
        raise HTTPException(status_code=404, detail=f"No file at index {index}")
```

Replace the body of `uploads_patch` with:

```python
        uid, meta = _load_owned_session(settings, user, upload_id)
        if meta.get("files"):
            entry = _entry_for_index(meta, index)
            part = UploadSession.part_path_for(
                settings.artifacts_root, user.username, uid, entry["index"], entry["suffix"]
            )
            declared = entry["total_size"]
        else:
            part = UploadSession.part_path(settings.artifacts_root, user.username, uid, meta["suffix"])
            declared = meta["total_size"]

        current = UploadSession.received_bytes(part)
        if offset != current:
            raise HTTPException(status_code=409, detail=f"Offset mismatch; expected {current}")
        data = await request.body()
        if current + len(data) > declared:
            raise HTTPException(status_code=413, detail="Chunk exceeds declared total_size")
        meta_path = UploadSession.meta_path(settings.artifacts_root, user.username, uid)
        if meta.get("files"):
            new_size = await asyncio.to_thread(
                UploadSession.append_chunk_at, part, meta_path, data, index
            )
        else:
            new_size = await asyncio.to_thread(
                UploadSession.append_chunk, part, meta_path, data, declared
            )
        return JSONResponse({"received": new_size})
```

and add `index: int = 0` to its parameters.

Add the imports at the top of `vts/api/main.py`:

```python
from vts.services.upload_set import UploadSetError, classify_suffixes
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_uploads_multi_api.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the whole suite to confirm the single-file path is intact**

Run: `.venv/bin/python -m pytest tests/test_uploads_api.py -q`
Expected: PASS, no regressions

- [ ] **Step 6: Commit**

```bash
git add tests/test_uploads_multi_api.py vts/api/main.py vts/api/schemas.py vts/core/config.py vts/__init__.py
git commit -m "feat(api): accept multi-file upload sets at init/patch/offset (vts-vm0)"
```

---

### Task 7: Finalize — probe, order, validate, create the task

**Files:**
- Modify: `vts/api/main.py` (`uploads_finalize`, line 2507)
- Test: `tests/test_uploads_multi_finalize.py` (create)

**Interfaces:**
- Consumes: `probe_media` (Task 1), `resolve_order` (Task 2), `verify_probes`/`UploadSetError` (Task 3), `UploadSession.finalize_multi` (Task 5).
- Produces: `Task.options` keys `source_files` (list of `{name, offset_sec, duration_sec}` — offsets filled in Task 8) and `source_files_order` (str). `Task.source_url` is `file://<first filename>`.

- [ ] **Step 1: Write the failing test**

```python
"""Finalizing a multi-file set (vts-vm0).

Finalize is where the bytes first exist, so it is where ordering is resolved by
probing and where video compatibility is enforced.
"""
from __future__ import annotations

import subprocess
import uuid

import pytest

_HEADERS = {"X-Forwarded-User": "tester"}


def _make_audio(path, seconds, freq, creation=None):
    cmd = ["ffmpeg", "-y", "-f", "lavfi",
           "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=16000", "-ac", "1"]
    if creation:
        cmd += ["-metadata", f"creation_time={creation}"]
    cmd += [str(path)]
    subprocess.run(cmd, capture_output=True, check=True)
    return path.read_bytes()


async def _upload_set(client, files):
    """files: [(filename, bytes)] -> upload_id"""
    init = await client.post("/api/uploads/init", json={
        "filename": "unused" + files[0][0][-4:], "total_size": 0,
        "files": [{"filename": n, "total_size": len(b), "last_modified": None} for n, b in files],
    }, headers=_HEADERS)
    assert init.status_code == 200, init.text
    upload_id = init.json()["upload_id"]
    for index, (_, payload) in enumerate(files):
        r = await client.patch(
            f"/api/uploads/{upload_id}?offset=0&index={index}", content=payload, headers=_HEADERS
        )
        assert r.status_code == 200, r.text
    return upload_id


@pytest.mark.asyncio
async def test_finalize_creates_one_task_with_source_files(client, tmp_path):
    a = _make_audio(tmp_path / "a.m4a", 1, 440, creation="2026-08-01T10:00:00.000000Z")
    b = _make_audio(tmp_path / "b.m4a", 1, 880, creation="2026-08-01T10:05:00.000000Z")
    upload_id = await _upload_set(client, [("b.m4a", b), ("a.m4a", a)])

    r = await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()

    options = body["options"]
    assert options["source_files_order"] == "creation_time"
    # Selected b-then-a, but a was recorded first — order must be corrected.
    assert [f["name"] for f in options["source_files"]] == ["a.m4a", "b.m4a"]
    assert body["source_url"].startswith("file://")


@pytest.mark.asyncio
async def test_finalize_rejects_incompatible_video_set(client, tmp_path):
    def _video(path, size):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration=1",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=44100",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)],
            capture_output=True, check=True,
        )
        return path.read_bytes()

    small = _video(tmp_path / "s.mp4", "320x240")
    large = _video(tmp_path / "l.mp4", "640x480")
    upload_id = await _upload_set(client, [("s.mp4", small), ("l.mp4", large)])

    r = await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)
    assert r.status_code == 422
    assert "l.mp4" in r.json()["detail"]


@pytest.mark.asyncio
async def test_finalize_rejects_incomplete_set(client, tmp_path):
    a = _make_audio(tmp_path / "a.m4a", 1, 440)
    init = await client.post("/api/uploads/init", json={
        "filename": "unused.m4a", "total_size": 0,
        "files": [
            {"filename": "a.m4a", "total_size": len(a), "last_modified": None},
            {"filename": "b.m4a", "total_size": 999, "last_modified": None},
        ],
    }, headers=_HEADERS)
    upload_id = init.json()["upload_id"]
    await client.patch(f"/api/uploads/{upload_id}?offset=0&index=0", content=a, headers=_HEADERS)

    r = await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)
    assert r.status_code == 409
    assert "b.m4a" in r.json()["detail"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_uploads_multi_finalize.py -v`
Expected: FAIL — finalize does not handle `meta["files"]`, so it raises `KeyError: 'suffix'`

- [ ] **Step 3: Write minimal implementation**

In `vts/api/main.py`, at the start of `uploads_finalize` after `uid, meta = _load_owned_session(...)`:

```python
        if meta.get("files"):
            media_dir = Path(settings.artifacts_root) / _user_hash_dir(user.username) / str(uid) / "media"
            # Every part must be complete before anything else is worth doing.
            for entry in meta["files"]:
                part = UploadSession.part_path_for(
                    settings.artifacts_root, user.username, uid, entry["index"], entry["suffix"]
                )
                if UploadSession.received_bytes(part) != entry["total_size"]:
                    raise HTTPException(
                        status_code=409, detail=f"Upload incomplete: {entry['filename']}"
                    )

            finals = await asyncio.to_thread(
                UploadSession.finalize_multi, settings.artifacts_root, user.username, uid, meta
            )

            try:
                probes = await asyncio.to_thread(
                    lambda: [(e["filename"], probe_media(p)) for e, p in zip(meta["files"], finals)]
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            try:
                verify_probes(meta["kind"], probes)
            except UploadSetError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            entries = [
                {
                    "filename": entry["filename"],
                    "creation_time": probe.creation_time,
                    "last_modified": entry.get("last_modified"),
                    "index": entry["index"],
                    "duration_sec": probe.duration_sec,
                }
                for entry, (_, probe) in zip(meta["files"], probes)
            ]
            ordered, order_source = resolve_order(entries)

            # Rename to concat order: extract_audio globs the finals in name
            # order, so the index in the name IS the order (vts-vm0).
            renamed: list[Path] = []
            for position, item in enumerate(ordered):
                current = media_dir / part_name(item["index"], Path(item["filename"]).suffix.lower())
                target = media_dir / f"ordered.{position:03d}{Path(item['filename']).suffix.lower()}"
                current.rename(target)
                renamed.append(target)
            for position, path in enumerate(renamed):
                path.rename(media_dir / part_name(position, path.suffix))

            options = dict(meta["options"])
            options["source_files"] = [
                {"name": item["filename"], "offset_sec": 0.0, "duration_sec": item["duration_sec"]}
                for item in ordered
            ]
            options["source_files_order"] = order_source
            options["source_files_kind"] = meta["kind"]

            repo = Repo(session)
            artifact = task_dir(settings.artifacts_root, user.username, uid)
            task = await repo.create_task(
                user_id=uuid.UUID(user.id),
                source_url=f"file://{ordered[0]['filename']}",
                options=options,
                artifact_dir=str(artifact),
                task_id=uid,
                source_title=meta.get("display_name"),
            )
            await session.commit()
            return await _enqueue_uploaded_task(task, repo, redis, settings)
```

Add imports at the top of `vts/api/main.py`:

```python
from vts.services.media import probe_media
from vts.services.upload_order import resolve_order
from vts.services.upload_session import part_name
from vts.services.upload_set import verify_probes
```

Add this helper near `_find_media_file` (the media dir must match `task_dir`'s layout):

```python
def _user_hash_dir(username: str) -> str:
    from vts.services.storage import user_hash
    return user_hash(username)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_uploads_multi_finalize.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_uploads_multi_finalize.py vts/api/main.py vts/__init__.py
git commit -m "feat(api): finalize a set — probe, order, validate, create task (vts-vm0)"
```

---

### Task 8: Pipeline — concatenate in `extract_audio`, build `video.mkv`

**Files:**
- Modify: `vts/pipeline/steps/media.py` (`ExtractAudioStep.run`, lines 168-190)
- Test: `tests/test_extract_audio_multi.py` (create)

**Interfaces:**
- Consumes: `concat_to_audio_16k_mono`, `concat_video_stream_copy` (Task 4); `options["source_files"]`, `options["source_files_kind"]` (Task 7).
- Produces: `media/audio_16k.wav` (combined) and, for video sets, `media/video.mkv`; `options["source_files"]` entries gain real `offset_sec` values.

- [ ] **Step 1: Write the failing test**

```python
"""extract_audio joins a multi-file set (vts-vm0).

Everything after this step reads only audio_16k.wav, so concatenating here
leaves the rest of the pipeline untouched. A video set additionally needs
video.mkv, because the player resolves media via _find_media_file.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from vts.services.media import probe_duration
from vts.services.storage import ensure_task_dirs


def _audio(path: Path, seconds: int, freq: int) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=16000",
         "-ac", "1", str(path)],
        capture_output=True, check=True,
    )
    return path


def _video(path: Path, seconds: int, freq: int) -> Path:
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=44100",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)],
        capture_output=True, check=True,
    )
    return path


class _Ctx:
    class settings:
        timezone = "UTC"

    def task_flag(self, options, key, *, default):
        return default


class _State:
    def __init__(self, dirs, options):
        self.dirs = dirs
        self.task_options = options
        self.task_id = uuid.uuid4()
        self.user_id = "u1"
        import logging
        self.logger = logging.getLogger("test.extract")


@pytest.mark.asyncio
async def test_audio_set_is_concatenated(tmp_path):
    from vts.pipeline.steps.media import ExtractAudioStep

    dirs = ensure_task_dirs(tmp_path)
    _audio(dirs["media"] / "audio.original.000.wav", 2, 440)
    _audio(dirs["media"] / "audio.original.001.wav", 3, 880)

    options = {
        "source_files_kind": "audio",
        "source_files": [
            {"name": "a.wav", "offset_sec": 0.0, "duration_sec": 2.0},
            {"name": "b.wav", "offset_sec": 0.0, "duration_sec": 3.0},
        ],
    }
    st = _State(dirs, options)
    await ExtractAudioStep().run(_Ctx(), st)

    out = dirs["media"] / "audio_16k.wav"
    assert out.exists()
    assert abs(probe_duration(out) - 5.0) < 0.3


@pytest.mark.asyncio
async def test_offsets_are_recorded(tmp_path):
    from vts.pipeline.steps.media import ExtractAudioStep

    dirs = ensure_task_dirs(tmp_path)
    _audio(dirs["media"] / "audio.original.000.wav", 2, 440)
    _audio(dirs["media"] / "audio.original.001.wav", 3, 880)

    options = {
        "source_files_kind": "audio",
        "source_files": [
            {"name": "a.wav", "offset_sec": 0.0, "duration_sec": 0.0},
            {"name": "b.wav", "offset_sec": 0.0, "duration_sec": 0.0},
        ],
    }
    st = _State(dirs, options)
    await ExtractAudioStep().run(_Ctx(), st)

    files = st.task_options["source_files"]
    assert abs(files[0]["offset_sec"] - 0.0) < 0.01
    assert abs(files[1]["offset_sec"] - 2.0) < 0.3


@pytest.mark.asyncio
async def test_video_set_also_builds_video_mkv(tmp_path):
    from vts.pipeline.steps.media import ExtractAudioStep

    dirs = ensure_task_dirs(tmp_path)
    _video(dirs["media"] / "audio.original.000.mp4", 1, 440)
    _video(dirs["media"] / "audio.original.001.mp4", 1, 880)

    options = {
        "source_files_kind": "video",
        "source_files": [
            {"name": "a.mp4", "offset_sec": 0.0, "duration_sec": 1.0},
            {"name": "b.mp4", "offset_sec": 0.0, "duration_sec": 1.0},
        ],
    }
    st = _State(dirs, options)
    await ExtractAudioStep().run(_Ctx(), st)

    assert (dirs["media"] / "audio_16k.wav").exists()
    video = dirs["media"] / "video.mkv"
    assert video.exists(), "a video set must produce a combined video for the player"
    assert abs(probe_duration(video) - 2.0) < 0.4


@pytest.mark.asyncio
async def test_single_file_task_is_unaffected(tmp_path):
    """No source_files in options means the original single-file behaviour."""
    from vts.pipeline.steps.media import ExtractAudioStep

    dirs = ensure_task_dirs(tmp_path)
    _audio(dirs["media"] / "audio.original.wav", 2, 440)

    st = _State(dirs, {})
    await ExtractAudioStep().run(_Ctx(), st)

    out = dirs["media"] / "audio_16k.wav"
    assert out.exists()
    assert abs(probe_duration(out) - 2.0) < 0.3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_extract_audio_multi.py -v`
Expected: FAIL — the step globs `audio.original.*` and takes only the first match, so the combined duration is 2.0 not 5.0

- [ ] **Step 3: Write minimal implementation**

Replace the body of `ExtractAudioStep.run` in `vts/pipeline/steps/media.py`:

```python
    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        output = st.dirs["media"] / "audio_16k.wav"
        trimmed = st.dirs["media"] / "audio_16k_trimmed.wav"
        # After trim step we remove audio_16k.wav, so resume from later stages
        # must treat the trimmed WAV as a valid completion marker too.
        if trimmed.exists():
            return True
        if output.exists():
            return True

        log_path = st.dirs["logs"] / "ffmpeg.log"
        source_files = (st.task_options or {}).get("source_files") or []

        if source_files:
            # A multi-file set (vts-vm0). Parts are named audio.original.NNN.*
            # where NNN is the concat order, so sorting by name IS the order.
            parts = sorted(
                p for p in st.dirs["media"].glob("audio.original.*")
                if not p.name.endswith(".probe.json") and not p.name.endswith(".part")
            )
            if len(parts) != len(source_files):
                raise RuntimeError(
                    f"Expected {len(source_files)} uploaded parts, found {len(parts)}"
                )

            work = st.dirs["media"] / "concat_work"
            durations = await asyncio.to_thread(
                concat_to_audio_16k_mono, parts, output, log_path, work
            )

            # Record real boundaries now that the durations are known.
            offset = 0.0
            updated = []
            for entry, duration in zip(source_files, durations):
                item = dict(entry)
                item["offset_sec"] = round(offset, 3)
                item["duration_sec"] = round(duration, 3)
                updated.append(item)
                offset += duration
            # JSON column: reassign, never mutate in place.
            options = dict(st.task_options or {})
            options["source_files"] = updated
            st.task_options = options

            if (st.task_options or {}).get("source_files_kind") == "video":
                # The player resolves media via _find_media_file, which prefers
                # video.mkv — without this a video set would play one part.
                await asyncio.to_thread(
                    concat_video_stream_copy, parts, st.dirs["media"] / "video.mkv", log_path, work
                )

            shutil.rmtree(work, ignore_errors=True)
            return True

        audio_file = next(st.dirs["media"].glob("audio.original.*"), None)
        if not audio_file:
            raise RuntimeError("Missing downloaded audio file")
        await asyncio.to_thread(
            extract_audio_16k_mono,
            audio_file,
            output,
            log_path,
        )
        return True
```

Add to the imports at the top of `vts/pipeline/steps/media.py`:

```python
import shutil

from vts.services.media import concat_to_audio_16k_mono, concat_video_stream_copy
```

(The existing `from vts.services.media import (...)` block at line 9 can be extended instead.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_extract_audio_multi.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Persist the updated options**

The step mutates `st.task_options` in memory; the offsets must reach the DB. Add to `vts/pipeline/context.py`, next to `persist_detected_language` (line 205):

```python
    async def persist_task_options(self, task_id: uuid.UUID, options: dict) -> None:
        """Write back options the pipeline computed (e.g. source_files offsets).

        JSON column: the caller passes a fresh dict and this reassigns it —
        mutating in place would not be seen by SQLAlchemy (vts-vm0).
        """
        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                return
            task.options = dict(options)
            await session.commit()
```

Call it at the end of the `if source_files:` branch, before `return True`:

```python
            await ctx.persist_task_options(st.task_id, options)
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, no regressions

- [ ] **Step 7: Commit**

```bash
git add tests/test_extract_audio_multi.py vts/pipeline/steps/media.py vts/pipeline/context.py vts/__init__.py
git commit -m "feat(pipeline): concatenate a multi-file set in extract_audio (vts-vm0)"
```

---

### Task 9: Frontend — multiple selection, aggregate progress

**Files:**
- Modify: `vts/static/index.html` (line 199, the `#file-input` element)
- Modify: `vts/static/app.js` (`uploadFileChunked` ~2311, the submit handler ~2845)
- Test: `tests/ui/scenarios/multi-file-upload.mjs` (create)

**Interfaces:**
- Consumes: the API from Tasks 6-7.
- Produces: `uploadFilesChunked(files, fields)` — uploads a whole set and returns the created task; progress is `sentTotal / sum(size)`.

- [ ] **Step 1: Write the failing browser scenario**

```javascript
// vts-vm0: several files upload as ONE task, with aggregate progress.
//
// The single-file flow sends one file and shows its percentage. A set must
// produce exactly one task card, and the ring must show progress across the
// whole set rather than restarting per file.
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, DEFAULT_API, launch } from "../harness.mjs";

export const name = "multi-file-upload";

const TASK_ID = "e1111111-1111-1111-1111-111111111111";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const FLAGS = {
  queued: { is_active:false, is_pending:true, is_finished:false, shows_progress:false,
            can_pause:true, can_resume:false, can_archive:false },
};

const TASK = {
  id: TASK_ID, source_url: "file://a.mp3", source_title: null, status: "queued",
  queue: null, queue_position: null, transcript_path: null, summary_path: null,
  options: {
    transcript: true, prompts: [],
    source_files: [
      { name: "a.mp3", offset_sec: 0, duration_sec: 10 },
      { name: "b.mp3", offset_sec: 10, duration_sec: 12 },
    ],
    source_files_order: "creation_time",
  },
  steps: [], capabilities: {}, created_at: "2026-08-03T10:00:00.000000+00:00",
  updated_at: "2026-08-03T10:00:00.000000+00:00", progress: {}, stats: {},
};

async function startServer() {
  const api = {
    ...DEFAULT_API,
    "/api/status-config": { status_flags: FLAGS, tasks_page_size: 10 },
    "/api/tasks": [],
    "/api/uploads/config": {
      chunked_threshold_bytes: 1, chunk_bytes: 8388608, max_upload_bytes: 2147483648,
    },
  };
  const patched = [];
  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url === "/api/events") {
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
      res.write(": connected\n\n");
      return;
    }
    if (url === "/api/uploads/init" && req.method === "POST") {
      let body = "";
      req.on("data", (c) => { body += c; });
      req.on("end", () => {
        const parsed = JSON.parse(body || "{}");
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({
          upload_id: TASK_ID, chunk_size: 8388608,
          files: (parsed.files || []).map((f, i) => ({ index: i, filename: f.filename })),
        }));
      });
      return;
    }
    if (url === `/api/uploads/${TASK_ID}` && req.method === "PATCH") {
      let size = 0;
      req.on("data", (c) => { size += c.length; });
      req.on("end", () => {
        patched.push({ url: req.url, size });
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ received: size }));
      });
      return;
    }
    if (url === `/api/uploads/${TASK_ID}/finalize` && req.method === "POST") {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(TASK));
      return;
    }
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (req.method !== "GET") { res.end(JSON.stringify({ status: "ok" })); return; }
      res.end(JSON.stringify(url in api ? api[url] : {}));
      return;
    }
    const f = url === "/" ? "/index.html" : url.replace("/static/", "/");
    const fp = path.join(STATIC_DIR, f);
    if (!fp.startsWith(STATIC_DIR) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
    let body = fs.readFileSync(fp).toString();
    if (f === "/index.html") body = body.replaceAll("__VTS_VERSION__", "verify");
    res.setHeader("Content-Type", CT[path.extname(fp)] || "text/plain");
    res.end(body);
  });
  await new Promise((r) => server.listen(0, r));
  return { server, baseUrl: `http://localhost:${server.address().port}`, patched: () => patched };
}

export async function run() {
  const failures = [];
  const { server, baseUrl, patched } = await startServer();
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      if (m.type() === "error" && !m.text().includes("EventSource")) {
        errors.push("console.error: " + m.text());
      }
    });
    await page.goto(baseUrl, { waitUntil: "load" });

    const acceptsMultiple = await page.evaluate(
      () => document.getElementById("file-input").multiple
    );
    if (!acceptsMultiple) {
      failures.push("#file-input does not accept multiple files");
      return failures;
    }

    await page.evaluate(() => {
      const radio = document.getElementById("source-type-file");
      radio.checked = true;
      radio.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await page.setInputFiles("#file-input", [
      { name: "a.mp3", mimeType: "audio/mpeg", buffer: Buffer.alloc(1000, 1) },
      { name: "b.mp3", mimeType: "audio/mpeg", buffer: Buffer.alloc(3000, 2) },
    ]);
    await page.click("#submit-btn", { force: true });
    await page.waitForTimeout(2500);

    const sent = patched();
    if (sent.length < 2) {
      failures.push(`expected a PATCH per file, saw ${sent.length}: ${JSON.stringify(sent)}`);
    }
    const indices = sent.map((p) => new URL("http://x" + p.url).searchParams.get("index"));
    if (!indices.includes("0") || !indices.includes("1")) {
      failures.push(`PATCHes did not target both indices: ${JSON.stringify(indices)}`);
    }

    const cards = await page.evaluate(() => document.querySelectorAll(".task").length);
    if (cards !== 1) failures.push(`expected exactly 1 task card for the set, got ${cards}`);

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}
```

- [ ] **Step 2: Run the scenario to verify it fails**

Run: `cd tests/ui && node -e "import('./scenarios/multi-file-upload.mjs').then(async m => { const f = await m.run(); console.log(f.length ? 'FAIL' : 'PASS'); f.forEach(x => console.log('  - ' + x)); })"`
Expected: FAIL — `#file-input does not accept multiple files`

- [ ] **Step 3: Write minimal implementation**

In `vts/static/index.html` line 199, add `multiple` to the file input:

```html
              multiple
```

In `vts/static/app.js`, add next to `uploadFileChunked`:

```javascript
async function uploadFilesChunked(files, fields) {
  const btn = document.getElementById("submit-btn");
  const icon = btn && btn.querySelector(".submit-icon");
  const ring = btn && btn.querySelector(".submit-progress");
  const fill = ring && ring.querySelector(".submit-progress-fill");
  const circumference = 56.55;
  // Aggregate: one bar for the whole set, not a bar that restarts per file.
  const grandTotal = files.reduce((sum, f) => sum + f.size, 0);
  let sentTotal = 0;
  const setProgress = (r) => { if (fill) fill.style.strokeDashoffset = circumference * (1 - r); };

  let uploadId = null;
  if (btn) btn.disabled = true;
  if (icon) icon.classList.add("hidden");
  if (ring) ring.classList.remove("hidden");
  setProgress(0);

  try {
    const init = await api("/api/uploads/init", {
      method: "POST",
      body: JSON.stringify({
        filename: files[0].name,
        total_size: files[0].size,
        files: files.map((f) => ({
          filename: f.name,
          total_size: f.size,
          last_modified: f.lastModified || null,
        })),
        language: fields.language || null,
        audio_only: fields.audio_only,
        transcript: fields.transcript,
        diarize: fields.diarize,
        prompts: fields.prompts,
        display_name: fields.display_name || null,
      }),
      headers: { "Content-Type": "application/json", "X-Forwarded-User": state.authUser },
    });
    uploadId = init.upload_id;
    state.taskPaging.ownIds.add(uploadId);
    const chunkSize = init.chunk_size || 8388608;

    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      let offset = 0;
      while (offset < file.size) {
        const slice = file.slice(offset, Math.min(offset + chunkSize, file.size));
        const buf = await slice.arrayBuffer();
        const resp = await api(`/api/uploads/${uploadId}?offset=${offset}&index=${index}`, {
          method: "PATCH",
          body: buf,
          headers: { "Content-Type": "application/octet-stream", "X-Forwarded-User": state.authUser },
        });
        const delta = resp.received - offset;
        offset = resp.received;
        sentTotal += delta > 0 ? delta : 0;
        setProgress(grandTotal ? sentTotal / grandTotal : 1);
      }
    }

    const task = await api(`/api/uploads/${uploadId}/finalize`, {
      method: "POST",
      headers: { "X-Forwarded-User": state.authUser },
    });
    setProgress(1);
    return task;
  } catch (err) {
    if (uploadId) state.taskPaging.ownIds.delete(uploadId);
    throw err;
  } finally {
    if (btn) btn.disabled = false;
    if (icon) icon.classList.remove("hidden");
    if (ring) ring.classList.add("hidden");
  }
}
```

In the submit handler, replace the file branch so a multi-selection routes to the new function:

```javascript
    if (isFile && fileInput) {
      const selected = Array.from(fileInput.files || []);
      if (!selected.length) {
        showTaskFormError(t("upload.file_unreadable"));
        return;
      }
      // Probe one byte of each: a stale reference fails here with a clear
      // message instead of mid-upload.
      for (const file of selected) {
        await file.slice(0, 1).arrayBuffer();
      }
      const fields = {
        language: form.language.value || "",
        audio_only: false,
        transcript: form.transcript.checked,
        diarize: form.diarize.checked,
        prompts: JSON.stringify(getSelectedPrompts()),
        display_name: "",
      };
      if (selected.length > 1) {
        created = await uploadFilesChunked(selected, fields);
      } else {
        const threshold = uploadConfig && Number.isFinite(uploadConfig.chunked_threshold_bytes)
          ? uploadConfig.chunked_threshold_bytes
          : Infinity;
        if (selected[0].size > threshold) {
          created = await uploadFileChunked(selected[0], fields);
        } else {
          const fd = new FormData();
          fd.append("file", selected[0]);
          if (fields.language) fd.append("language", fields.language);
          fd.append("audio_only", "false");
          fd.append("transcript", fields.transcript ? "true" : "false");
          fd.append("diarize", fields.diarize ? "true" : "false");
          fd.append("prompts", fields.prompts);
          created = await uploadFileWithProgress(fd);
        }
      }
    } else {
```

- [ ] **Step 4: Run the scenario to verify it passes**

Run: `cd tests/ui && node -e "import('./scenarios/multi-file-upload.mjs').then(async m => { const f = await m.run(); console.log(f.length ? 'FAIL' : 'PASS'); f.forEach(x => console.log('  - ' + x)); })"`
Expected: PASS

- [ ] **Step 5: Run the whole browser suite**

Run: `cd tests/ui && node run.mjs`
Expected: `UI VERIFY: PASSED` — in particular `chunked-upload`, `own-upload-not-flagged-new` and `upload-read-error` must still pass.

- [ ] **Step 6: Check syntax and commit**

```bash
node --check vts/static/app.js
git add tests/ui/scenarios/multi-file-upload.mjs vts/static/app.js vts/static/index.html vts/__init__.py
git commit -m "feat(ui): multi-file selection with aggregate upload progress (vts-vm0)"
```

---

### Task 10: Show the file list on the task card

**Files:**
- Modify: `vts/static/app.js` (about-dialog rendering, ~1107-1141)
- Modify: `vts/static/i18n/en.js`, `vts/static/i18n/ru.js`, `vts/static/i18n/de.js` (all three locales exist; a missing key renders as the raw key)
- Test: `tests/ui/scenarios/multi-file-about.mjs` (create)

**Interfaces:**
- Consumes: `options.source_files`, `options.source_files_order` (Task 7).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing browser scenario**

```javascript
// vts-vm0: a set's About dialog lists the parts and says how they were ordered.
// The order is decided server-side with no way to correct it, so showing which
// rule produced it is what makes a wrong order explicable.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "multi-file-about";

const TASK_ID = "e2222222-2222-2222-2222-222222222222";

const FLAGS = {
  completed: { is_active:false, is_pending:false, is_finished:true, shows_progress:true,
               can_pause:false, can_resume:false, can_archive:true },
};

const TASK = {
  id: TASK_ID, source_url: "file://part1.m4a", source_title: "Совещание",
  status: "completed", queue: null, queue_position: null,
  transcript_path: null, summary_path: null,
  options: {
    transcript: true, prompts: [],
    source_files: [
      { name: "part1.m4a", offset_sec: 0, duration_sec: 612.4 },
      { name: "part2.m4a", offset_sec: 612.4, duration_sec: 458.1 },
    ],
    source_files_order: "creation_time",
    source_files_kind: "audio",
  },
  steps: [], capabilities: {}, created_at: "2026-08-03T10:00:00.000000+00:00",
  updated_at: "2026-08-03T10:00:00.000000+00:00", progress: {}, stats: {},
};

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/status-config": { status_flags: FLAGS, tasks_page_size: 10 },
    "/api/tasks": [TASK],
    [`/api/tasks/${TASK_ID}`]: TASK,
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${TASK_ID}"]`, { timeout: 5000 });

    // The About dialog opens from the card's stats area (app.js:2016), not
    // from a dedicated button.
    await page.click(`[data-task-id="${TASK_ID}"] .task-stats`, { force: true });
    await page.waitForTimeout(400);

    const text = await page.evaluate(() => {
      const dialog = document.getElementById("task-about-dialog");
      return dialog ? dialog.textContent : "";
    });

    if (!text.includes("part1.m4a") || !text.includes("part2.m4a")) {
      failures.push(`About dialog does not list the set's files. Text: ${text.slice(0, 300)}`);
    }
    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}
```

- [ ] **Step 2: Run the scenario to verify it fails**

Run: `cd tests/ui && node -e "import('./scenarios/multi-file-about.mjs').then(async m => { const f = await m.run(); console.log(f.length ? 'FAIL' : 'PASS'); f.forEach(x => console.log('  - ' + x)); })"`
Expected: FAIL — the dialog does not list the files

Note: the dialog is `#task-about-dialog` and is opened by clicking the card's `.task-stats` element (`openTaskAboutDialog`, `app.js:1199`, wired at `app.js:2016`) — there is no dedicated About button. If either selector has drifted, inspect a rendered card and use the real one; do not weaken the assertion.

- [ ] **Step 3: Write minimal implementation**

Add to `vts/static/i18n/en.js`:

```javascript
  "about.source_files": "Source files",
  "about.order_creation_time": "ordered by recording time",
  "about.order_last_modified": "ordered by file date",
  "about.order_filename": "ordered by file name",
```

Add to `vts/static/i18n/ru.js`:

```javascript
  "about.source_files": "Исходные файлы",
  "about.order_creation_time": "порядок по времени записи",
  "about.order_last_modified": "порядок по дате файла",
  "about.order_filename": "порядок по имени файла",
```

Add to `vts/static/i18n/de.js` (leaving it out makes the dialog show raw keys
for German users):

```javascript
  "about.source_files": "Quelldateien",
  "about.order_creation_time": "nach Aufnahmezeit sortiert",
  "about.order_last_modified": "nach Dateidatum sortiert",
  "about.order_filename": "nach Dateiname sortiert",
```

In `vts/static/app.js`, in the About-dialog renderer after the source-url block (~line 1141):

```javascript
  // A set was joined into one recording; list the parts and say which rule
  // decided their order, since the user cannot change it (vts-vm0).
  const sourceFiles = Array.isArray(task.options && task.options.source_files)
    ? task.options.source_files : [];
  const filesEl = q(".about-source-files");
  if (filesEl) {
    if (sourceFiles.length > 1) {
      const orderKey = `about.order_${task.options.source_files_order || "filename"}`;
      const lines = sourceFiles.map((f, i) => `${i + 1}. ${f.name}`);
      filesEl.textContent = `${t("about.source_files")} (${t(orderKey)}): ${lines.join("; ")}`;
      filesEl.classList.remove("hidden");
    } else {
      filesEl.textContent = "";
      filesEl.classList.add("hidden");
    }
  }
```

Add the element to the About dialog in `vts/static/index.html`, immediately after `.about-source-url` (line 741):

```html
          <div class="about-source-files hidden"></div>
```

**Important:** `app.js` has no `defer`, so any element referenced via `getElementById` at load time must appear before the `<script>` tag. This element is read via `q()` inside a render function, so placement inside the dialog is fine.

- [ ] **Step 4: Run the scenario to verify it passes**

Run: `cd tests/ui && node -e "import('./scenarios/multi-file-about.mjs').then(async m => { const f = await m.run(); console.log(f.length ? 'FAIL' : 'PASS'); f.forEach(x => console.log('  - ' + x)); })"`
Expected: PASS

- [ ] **Step 5: Run the whole browser suite and commit**

```bash
cd tests/ui && node run.mjs   # expect UI VERIFY: PASSED
cd ../.. && node --check vts/static/app.js
git add tests/ui/scenarios/multi-file-about.mjs vts/static/app.js vts/static/index.html vts/static/i18n/en.js vts/static/i18n/ru.js vts/static/i18n/de.js vts/__init__.py
git commit -m "feat(ui): list a set's source files and its order source (vts-vm0)"
```

---

### Task 11: Documentation and issue close-out

**Files:**
- Modify: `docs/ARCHITECTURE.md` (the *Design decisions* section, after the sequential-pipeline entry)
- Modify: `docs/API.md` (the uploads section)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Document the upload API change**

In `docs/API.md`, in the chunked-upload section, add:

```markdown
### Multi-file sets

`POST /api/uploads/init` accepts a `files` array instead of a single
`filename`/`total_size`:

```json
{"files": [{"filename": "part1.m4a", "total_size": 1048576, "last_modified": 1785000000000}]}
```

The response carries `files: [{index, filename}]`; chunks are then sent with
`PATCH /api/uploads/{id}?offset=N&index=K`. `finalize` returns one task.

A set must be all-video or all-audio (mixed sets are rejected at `init`), the
combined size must not exceed `max_upload_bytes`, and it may contain at most
`upload_max_files` files. Video parts must share codec, resolution, frame rate,
audio codec, sample rate and channel count — mismatched sets are rejected at
`finalize`, because stream-copy concat of mismatched inputs silently produces a
wrong-duration file.
```

- [ ] **Step 2: Document the design decision**

In `docs/ARCHITECTURE.md`, after the "pipeline stays strictly sequential" entry:

```markdown
**A multi-file upload is joined into one recording, not tracked as N files.**
`extract_audio` normalises every uploaded part to 16 kHz mono and concatenates
them into the single `audio_16k.wav` that the rest of the DAG already consumes,
so nothing downstream knows a set arrived. A video set additionally gets a
stream-copied `video.mkv`, because the player resolves media through
`_find_media_file`, which prefers it. File boundaries live in
`Task.options.source_files` as `{name, offset_sec, duration_sec}` — never as
markers inside the transcript text, which would reach the LLM and surface as
noise in the summary.

The alternative — a `files` table with per-file ASR — was rejected: it costs a
schema migration and a DAG rework, and it is worse for the result, because
diarizing each file separately cannot link the same speaker across files
whereas one continuous recording links them for free.

Concat order is resolved server-side with no user step: container
`creation_time`, then the browser's `lastModified`, then natural filename sort.
The last rule is load-bearing rather than a formality — ogg, opus, wav, avi,
wmv and ts carry no `creation_time` at all, and opus is what Telegram and
WhatsApp voice messages use.
```

- [ ] **Step 3: Run everything one last time**

```bash
.venv/bin/python -m pytest -q
cd tests/ui && node run.mjs && cd ../..
```
Expected: all Python tests pass; `UI VERIFY: PASSED`

- [ ] **Step 4: Commit and close the issue**

```bash
git add docs/API.md docs/ARCHITECTURE.md vts/__init__.py
git commit -m "docs: multi-file upload API and design decision (vts-vm0)"
git push
bd close vts-vm0
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| Chunked upload of N files under one session | 5, 6 |
| Server-side ordering, no user step | 2, 7 |
| Concatenation into `audio_16k.wav` | 4, 8 |
| Video set → combined `video.mkv` | 4, 8 |
| Mixed video+audio rejected at init | 3, 6 |
| Video compatibility checked from probes | 1, 3, 7 |
| Whole task fails on a bad file | 1, 7 |
| Boundary metadata in `options.source_files` | 7, 8 |
| No markers in transcript text | 8 (nothing writes them) |
| Limits: total size, file count | 6 |
| `multiple` on the file input | 9 |
| Aggregate upload progress | 9 |
| File list + order source on the card | 10 |
| New scenarios fail before the change | 9, 10 (explicit steps) |

**Placeholder scan:** none — every step carries the code or the exact command.

**Type consistency:** `MediaProbe` field names are used identically in Tasks 1, 3 and 7. `resolve_order` returns `(list, str)` in Tasks 2 and 7. `part_name(index, suffix)` is used in Tasks 5, 7 and 8. `source_files` entries are `{name, offset_sec, duration_sec}` in Tasks 7, 8 and 10 — note Task 7 writes `offset_sec: 0.0` as a placeholder and Task 8 fills the real values, which is stated in both.

**Known risk to watch during execution:** Task 7 renames parts twice (via a temporary `ordered.NNN.*` name) to avoid collisions when the resolved order permutes indices. If the implementer finds a simpler collision-free rename, that is fine as long as the final names are `audio.original.NNN.<ext>` in concat order.
