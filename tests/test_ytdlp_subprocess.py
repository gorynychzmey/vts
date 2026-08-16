"""The yt-dlp download runs in a child process (vts-xkx4).

These exercise the real subprocess and the real JSON protocol — no stubbing of
_run_download_subprocess — because the whole point of the split is behaviour at
the process boundary: progress must stream while the download runs, the error
*text* must survive so classify_failure_code() still works, and the title must
come back through a hop that cannot carry yt-dlp's info_dict wholesale.

yt-dlp itself is replaced by a fake module injected into the child's import
path, so nothing here touches the network.
"""

from __future__ import annotations

import logging
import textwrap
import threading
import time
from pathlib import Path

import pytest

from vts.core.failures import classify_failure_code
from vts.services.downloader import _run_download, kill_active_downloads


FAKE_YTDLP = """
import sys


class YoutubeDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        url = urls[0]
        logger = self.options.get("logger")
        if logger:
            logger.info("starting " + url)
        for hook in self.options.get("progress_hooks", []):
            hook({
                "status": "downloading",
                "downloaded_bytes": 512,
                "total_bytes": 1024,
                "filename": "/tmp/video.source.mp4",
                "info_dict": {"title": "Fake Title", "_filename": "/tmp/video.source.mp4"},
            })
        if "BOOM" in url:
            raise RuntimeError(SENTINEL_ERROR)
        for hook in self.options.get("progress_hooks", []):
            hook({
                "status": "finished",
                "downloaded_bytes": 1024,
                "total_bytes": 1024,
                "filename": "/tmp/video.source.mp4",
                "info_dict": {"title": "Fake Title", "_filename": "/tmp/video.source.mp4"},
            })
"""


@pytest.fixture
def fake_ytdlp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Put a fake yt_dlp ahead of the real one on the child's PYTHONPATH."""

    def _make(sentinel_error: str) -> Path:
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir(exist_ok=True)
        source = f"SENTINEL_ERROR = {sentinel_error!r}\n" + textwrap.dedent(FAKE_YTDLP)
        (stub_dir / "yt_dlp.py").write_text(source)
        repo_root = Path(__file__).resolve().parents[1]
        monkeypatch.setenv("PYTHONPATH", f"{stub_dir}:{repo_root}")
        return stub_dir

    return _make


def _run(url: str, tmp_path: Path) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    def progress_cb(phase: str, payload: dict) -> None:
        events.append((phase, payload))

    _run_download(
        url=url,
        outtmpl=str(tmp_path / "video.source.%(ext)s"),
        ydl_opts={"quiet": True},
        phase="video",
        progress_cb=progress_cb,
        logger=logging.getLogger("test-ytdlp"),
    )
    return events


def test_progress_and_title_cross_the_process_boundary(
    tmp_path: Path, fake_ytdlp_path
) -> None:
    fake_ytdlp_path("unused")
    events = _run("https://example.com/video", tmp_path)

    phases = {phase for phase, _ in events}
    assert phases == {"video"}

    # Mid-download progress survived the hop and was computed from the raw
    # byte counts, not shipped pre-computed.
    downloading = [payload for _, payload in events if payload.get("progress") == 0.5]
    assert downloading, f"no half-way progress event in {events}"
    assert downloading[0]["downloaded_bytes"] == 512
    assert downloading[0]["total_bytes"] == 1024

    # The title is what DownloadStep persists as the task name; it can only
    # arrive via the reconstructed info_dict.
    assert any(payload.get("media_title") == "Fake Title" for _, payload in events)
    assert any(payload.get("media_filename") == "video.source.mp4" for _, payload in events)

    # Terminal event still reports completion.
    assert events[-1][1]["progress"] == 1.0


def test_child_error_text_survives_for_failure_classification(
    tmp_path: Path, fake_ytdlp_path
) -> None:
    """The retry loop branches on classify_failure_code(str(exc)).

    If the boundary lost the message and surfaced only an exit code, a live
    stream that has not started would be retried against every player client
    instead of failing fast — so assert the symptom, not just the string.
    """
    fake_ytdlp_path("ERROR: This live event will begin in a few moments.")

    with pytest.raises(RuntimeError) as excinfo:
        _run("https://example.com/BOOM", tmp_path)

    assert classify_failure_code(str(excinfo.value)) == "download_live_not_started"


def test_cancelling_the_task_does_not_leave_the_download_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled task must stop downloading, not just stop waiting.

    asyncio.to_thread cancellation abandons only the await — the worker thread
    runs on. That was invisible while yt-dlp was a thread; as a process it is
    an orphan that keeps using bandwidth and writing into the media dir until
    it finishes. kill_active_downloads() is what actually stops it, so assert
    the process is gone rather than that cancel() returned.
    """
    stub_dir = tmp_path / "slow"
    stub_dir.mkdir()
    (stub_dir / "yt_dlp.py").write_text(
        textwrap.dedent(
            """
            import time


            class YoutubeDL:
                def __init__(self, options):
                    self.options = options

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def download(self, urls):
                    time.sleep(120)
            """
        )
    )
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", f"{stub_dir}:{repo_root}")

    thread = threading.Thread(target=lambda: _swallow(lambda: _run(
        "https://example.com/slow", tmp_path)), daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not _live_children() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _live_children(), "download child never started"

    stopped = kill_active_downloads()
    assert stopped >= 1

    thread.join(timeout=15)
    assert not thread.is_alive(), "worker thread stayed blocked after the child was killed"
    assert not _live_children(), "download child survived cancellation"


def _swallow(fn) -> None:
    try:
        fn()
    except Exception:
        pass


def _live_children() -> list:
    from vts.services import downloader

    with downloader._CHILDREN_LOCK:
        return [p for p in downloader._CHILDREN if p.poll() is None]


def test_runner_failure_without_protocol_error_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that dies before emitting an error line must not look like success."""
    stub_dir = tmp_path / "broken"
    stub_dir.mkdir()
    # Importing yt_dlp raises, so the child exits non-zero via the protocol's
    # error path only if it got that far; either way this must not pass silently.
    (stub_dir / "yt_dlp.py").write_text("raise ImportError('no yt-dlp here')\n")
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", f"{stub_dir}:{repo_root}")

    with pytest.raises(RuntimeError):
        _run("https://example.com/video", tmp_path)

