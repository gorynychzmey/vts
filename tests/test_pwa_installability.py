"""The PWA must satisfy what a browser checks before offering to install it.

Reported 2026-09-02 from Android: Chrome installs the app, but Edge adds a
SHORTCUT — no entry in the app list, though the icon opens it standalone.
Chrome dropped the offline requirement in 2021; Edge still enforces it, and a
service worker with no navigation handler fails that check.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "vts" / "static"


def test_service_worker_answers_navigations():
    """Without this the app installs as a shortcut in Edge."""
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert 'req.mode === "navigate"' in sw, (
        "the service worker has no navigation handler, so a browser that still "
        "requires offline support installs a shortcut instead of an app"
    )
    # Network first: index.html is served no-store because it carries
    # per-request state, so a cache-first shell would show stale content.
    handler = sw[sw.index('req.mode === "navigate"'):]
    assert "fetch(req)" in handler[:400], "navigations must go to the network first"


def test_offline_response_is_self_contained():
    """It must not depend on an asset: the app caches none."""
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "OFFLINE_HTML" in sw
    assert "caches.match" not in sw.split("OFFLINE_HTML")[0][-300:], (
        "the offline page must not be fetched from a cache the app never fills"
    )


def test_manifest_has_what_installability_requires():
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest.get("name") and manifest.get("short_name")
    assert manifest.get("start_url")
    assert manifest.get("display") in {"standalone", "fullscreen", "minimal-ui"}
    sizes = {icon.get("sizes") for icon in manifest.get("icons", [])}
    # 192 and 512 are the pair every engine asks for.
    assert "192x192" in sizes and "512x512" in sizes, sizes
    purposes = {icon.get("purpose") for icon in manifest.get("icons", [])}
    assert "maskable" in purposes, "Android needs a maskable icon for the launcher"


def test_share_target_accepts_the_media_the_pipeline_handles():
    """A container missing here means the share sheet greys the app out."""
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    accept = manifest["share_target"]["params"]["files"][0]["accept"]
    assert "audio/*" in accept and "video/*" in accept
    # Android often reports a specific type rather than a wildcard, so the
    # common containers are listed explicitly as well.
    for ext in (".mp4", ".m4a", ".mp3", ".wav"):
        assert ext in accept, f"{ext} missing from the share target"


def test_share_receiver_stages_the_file_rather_than_the_hidden_input():
    """The visible list is built from stagedFiles (multi-file upload).

    Writing fileInput.files directly put the shared file where nothing reads
    it: the form switched to File mode and the list stayed empty, which is what
    the Android share looked like.
    """
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    receiver = app[app.index("async function applyPendingSharedFileIfAny"):]
    receiver = receiver[:receiver.index("\n}\n")]
    assert "addStagedFiles" in receiver, (
        "the shared file never reaches stagedFiles, so it cannot appear in the list"
    )
    assert not re.search(r"fileInput\.files\s*=", receiver), (
        "assigning fileInput.files bypasses the staged list"
    )
