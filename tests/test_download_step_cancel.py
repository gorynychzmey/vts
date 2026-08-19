"""Cancelling a task must stop the download, not just stop waiting for it (vts-jb2u).

kill_active_downloads() already existed and already worked — but nothing in the
production code ever called it, so the behaviour it provides was unreachable:
the processor only tests for cancellation BETWEEN steps, and a download is one
of the two steps long enough for that to matter (diarization, which solved the
same problem in vts-hv7, is the other).

So these drive the real DownloadStep against a real child process and assert
the process is gone — not that a helper returned a number. A test that called
kill_active_downloads() itself would pass just as well with the wiring absent,
which is exactly the bug being fixed here.
"""

from __future__ import annotations

import asyncio
import logging
import textwrap
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from vts.pipeline.steps.base import StepState
from vts.pipeline.steps.media import DownloadCancelled, DownloadStep


# Sleeps rather than downloading, so the child is still alive when the cancel
# lands. Progress is emitted first: the cancel check rides the progress
# callback, which is the only code of ours that runs during a download.
SLOW_YTDLP = """
import time


class YoutubeDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        for hook in self.options.get("progress_hooks", []):
            hook({
                "status": "downloading",
                "downloaded_bytes": 1,
                "total_bytes": 1000,
                "filename": "/tmp/video.source.mp4",
                "info_dict": {"title": "Slow", "_filename": "/tmp/video.source.mp4"},
            })
        for _ in range(240):
            time.sleep(0.5)
            for hook in self.options.get("progress_hooks", []):
                hook({
                    "status": "downloading",
                    "downloaded_bytes": 2,
                    "total_bytes": 1000,
                    "filename": "/tmp/video.source.mp4",
                    "info_dict": {},
                })
"""


class _FakeBus:
    """Cancellation flag is a plain bool; publish_event just records."""

    def __init__(self, cancel: bool = False, pause: bool = False) -> None:
        self.events: list[dict] = []
        self._cancel = cancel
        self._pause = pause

    async def publish_event(self, **kwargs) -> None:
        self.events.append(kwargs)

    async def is_cancel_requested(self, task_id) -> bool:
        return self._cancel

    async def is_pause_requested(self, task_id) -> bool:
        return self._pause


def _dirs(tmp_path: Path) -> dict[str, Path]:
    names = ("media", "outputs", "segments", "logs")
    for name in names:
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return {name: tmp_path / name for name in names}


def _ctx(bus: _FakeBus, source_url: str = "https://example.com/slow") -> SimpleNamespace:
    """The slice of PipelineContext DownloadStep.run actually touches."""
    settings = SimpleNamespace(
        ytdlp_cookies_file=None,
        ytdlp_cookies_from_browser=None,
        ytdlp_youtube_player_client=None,
        ytdlp_youtube_po_token=None,
        ytdlp_verbose=False,
    )

    def task_flag(options: dict, key: str, *, default: bool) -> bool:
        return bool(options.get(key, default))

    async def task_url(task_id) -> str:
        return source_url

    async def get_user_preferred_ytdlp_client(user_uuid) -> str | None:
        return None

    async def set_user_preferred_ytdlp_client(user_uuid, client) -> None:
        return None

    async def save_task_source_title(task_id, title) -> None:
        return None

    return SimpleNamespace(
        bus=bus,
        settings=settings,
        task_flag=task_flag,
        task_url=task_url,
        get_user_preferred_ytdlp_client=get_user_preferred_ytdlp_client,
        set_user_preferred_ytdlp_client=set_user_preferred_ytdlp_client,
        save_task_source_title=save_task_source_title,
    )


def _state(tmp_path: Path, dirs: dict[str, Path]) -> StepState:
    return StepState(
        task_id=uuid.uuid4(),
        user_id=str(uuid.uuid4()),
        dirs=dirs,
        logger=logging.getLogger("test-download-cancel"),
        task_options={"source_url": "https://example.com/slow", "audio_only": False},
    )


def _live_children() -> list:
    from vts.services import downloader

    with downloader._CHILDREN_LOCK:
        return [p for p in downloader._CHILDREN if p.poll() is None]


@pytest.fixture
def slow_ytdlp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_dir = tmp_path / "slow-stub"
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "yt_dlp.py").write_text(textwrap.dedent(SLOW_YTDLP))
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", f"{stub_dir}:{repo_root}")


@pytest.mark.asyncio
async def test_cancel_during_download_kills_the_child(tmp_path: Path, slow_ytdlp) -> None:
    """The step itself must stop the download when the task is cancelled.

    Asserted on the child process, because that is the thing that keeps using
    bandwidth and writing into the media dir. `stopped >= 1` from the helper
    would be satisfied by a test that called the helper directly; a dead
    process can only happen if the step actually reached for it.
    """
    dirs = _dirs(tmp_path)
    bus = _FakeBus(cancel=True)
    st = _state(tmp_path, dirs)

    with pytest.raises(DownloadCancelled):
        await DownloadStep().run(_ctx(bus), st)

    # The kill is synchronous inside the callback, so by the time the exception
    # surfaces the child is already reaped.
    deadline = time.monotonic() + 10
    while _live_children() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _live_children(), "the download child survived the cancel"


@pytest.mark.asyncio
async def test_download_runs_to_completion_when_not_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel check must not fire on its own.

    A guard that raised whenever the bus was merely consulted would pass the
    test above and break every real download, so pin the negative case too.
    """
    stub_dir = tmp_path / "quick-stub"
    stub_dir.mkdir()
    (stub_dir / "yt_dlp.py").write_text(
        textwrap.dedent(
            """
            import os
            import subprocess


            class YoutubeDL:
                def __init__(self, options):
                    self.options = options

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def download(self, urls):
                    target = self.options["outtmpl"].replace("%(ext)s", "m4a")
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    # A REAL (tiny) audio file: the step probes what it
                    # downloaded with ffprobe, so placeholder bytes fail well
                    # past the guard this test is about.
                    subprocess.run(
                        ["ffmpeg", "-y", "-f", "lavfi", "-i",
                         "sine=frequency=440:duration=1", "-c:a", "aac", target],
                        check=True, capture_output=True,
                    )
                    for hook in self.options.get("progress_hooks", []):
                        hook({
                            "status": "finished",
                            "downloaded_bytes": 11,
                            "total_bytes": 11,
                            "filename": target,
                            "info_dict": {"title": "Done", "_filename": target},
                        })
            """
        )
    )
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", f"{stub_dir}:{repo_root}")

    dirs = _dirs(tmp_path)
    bus = _FakeBus(cancel=False)
    st = _state(tmp_path, dirs)
    # audio_only: the video path muxes with real ffmpeg, which cannot read the
    # placeholder bytes this stub writes. The cancel guard is upstream of that
    # split, so either path exercises it.
    st.task_options["audio_only"] = True

    await DownloadStep().run(_ctx(bus), st)

    assert not _live_children(), "a completed download left a child behind"
    assert bus.events, "no progress was published during a normal download"


@pytest.mark.asyncio
async def test_pause_during_download_kills_the_child(tmp_path: Path, slow_ytdlp) -> None:
    """A pause must stop the download too — and stay a pause.

    Pause now interrupts via atask.cancel(), but yt-dlp runs under
    asyncio.to_thread: cancelling abandons the await and leaves the child
    downloading for a task nobody is waiting on. Only killing it actually
    stops it, which is why the step has to notice the pause itself.

    Asserted on the real child process for the same reason as the cancel case
    above — that is the thing still using bandwidth. TaskPaused rather than
    DownloadCancelled because the latter makes the processor exit without
    writing a status, which would leave the row stuck in `running`.
    """
    from vts.pipeline.processor import TaskPaused

    dirs = _dirs(tmp_path)
    bus = _FakeBus(pause=True)
    st = _state(tmp_path, dirs)

    with pytest.raises(TaskPaused):
        await DownloadStep().run(_ctx(bus), st)

    deadline = time.monotonic() + 10
    while _live_children() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _live_children(), "the download child survived the pause"
