"""The web manifest must keep the app installable, and shareable-to.

Android only offers an app in the system "Share" sheet once the PWA is
installed, and it only installs when the manifest holds a valid, complete set
of fields. Both are silent when broken: a malformed manifest does not error
anywhere the user can see — the install option simply never appears, and with
it the share target.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

MANIFEST = Path("vts/static/manifest.webmanifest")


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_is_valid_json() -> None:
    """A syntax error here costs installability with no other symptom."""
    json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_declares_an_explicit_id(manifest: dict) -> None:
    """Without `id`, the identity is derived from `start_url`.

    That derivation is what makes a later `start_url` change silently register
    as a DIFFERENT app, leaving the installed copy orphaned. Pinning it to "/"
    keeps the identity the browsers already computed, so nothing re-installs.
    """
    assert manifest["id"] == "/", "changing id orphans every installed copy"


def test_install_requirements_are_present(manifest: dict) -> None:
    for field in ("name", "short_name", "start_url", "scope", "display"):
        assert manifest.get(field), f"{field} is required for the install prompt"
    assert manifest["display"] in {"standalone", "fullscreen", "minimal-ui"}

    sizes = {icon.get("sizes") for icon in manifest.get("icons", [])}
    # Android wants both, and a maskable one to avoid a letterboxed launcher icon.
    assert "192x192" in sizes and "512x512" in sizes
    purposes = {icon.get("purpose") for icon in manifest.get("icons", [])}
    assert "maskable" in purposes


def test_share_target_accepts_a_link_and_a_media_file(manifest: dict) -> None:
    """The whole point of installing it on a phone: share a video into vts."""
    share = manifest["share_target"]
    assert share["action"] == "/share"
    # A GET share target cannot carry files; the file half needs POST + multipart.
    assert share["method"].upper() == "POST"
    assert share["enctype"] == "multipart/form-data"

    params = share["params"]
    assert params.get("url") and params.get("text"), "sharing a link must work"

    accept = params["files"][0]["accept"]
    assert "video/*" in accept and "audio/*" in accept
    # Android share sheets often pass a concrete extension rather than a MIME
    # type, so the common containers are listed explicitly as well.
    for ext in (".mp4", ".m4a", ".mp3"):
        assert ext in accept, f"{ext} missing from the share target's accept list"


def test_icons_referenced_by_the_manifest_exist() -> None:
    """A 404 icon fails the install check as surely as a missing field."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for icon in manifest["icons"]:
        src = icon["src"]
        assert src.startswith("/static/"), src
        path = Path("vts") / src.lstrip("/")
        assert path.is_file(), f"{src} is declared but not shipped"
