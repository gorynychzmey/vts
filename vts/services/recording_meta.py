"""Normalising the metadata a Recording stores (vts-8w1r).

The pipeline stores whichever spelling of a language its backend produced: the
ASR sidecar returns a code ("ru"), while the cpp backend returns the full
English name ("russian"). Measured on production: 104 recordings say "russian"
and one says "ru" — the same language, listed twice.

That is a pre-existing property of Task.options, and rewriting the pipeline to
fix it is a different job with its own consequences. What the library must not
do is present the two as different languages, so the code is normalised on the
way into the recording. Unknown values pass through unchanged: showing what is
actually stored beats guessing.
"""
from __future__ import annotations

from typing import Any

# The languages whisper names in full. Only the ones this deployment can
# plausibly see are listed; anything else falls through untouched rather than
# being mapped by a guess.
_NAME_TO_CODE = {
    "russian": "ru",
    "english": "en",
    "german": "de",
    "ukrainian": "uk",
    "french": "fr",
    "spanish": "es",
    "italian": "it",
    "portuguese": "pt",
    "polish": "pl",
    "dutch": "nl",
    "turkish": "tr",
    "kazakh": "kk",
    "belarusian": "be",
    "czech": "cs",
    "chinese": "zh",
    "japanese": "ja",
    "korean": "ko",
    "arabic": "ar",
    "hebrew": "he",
    "hindi": "hi",
}


def language_code(value: Any) -> str | None:
    """A language as a code, from either spelling the pipeline may have stored.

    Returns None for an absent or blank value — a recording whose language was
    never determined states nothing rather than claiming a default.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return _NAME_TO_CODE.get(text, text)


def recording_display_name(title: Any, source_url: Any) -> str | None:
    """The name a recording is known by.

    Derived HERE, not in the browser: the name is a property of the recording,
    and it has to be the same string in the library, in the API, in an MCP
    client and in anything exported later. A view that computed it would be one
    of several answers.

    Measured on production: 55 of 122 recordings had no title, because their
    TASK had none — an upload is only titled if the user types something. The
    name was in `source_url` all along, and the task list already fell back to
    it while the library printed "untitled".

    An upload carries its filename in a `file://` pseudo-URL; that is what the
    user recognises. Otherwise the URL itself is a poor name but a true one,
    which beats no name at all.
    """
    explicit = str(title).strip() if isinstance(title, str) else ""
    if explicit:
        return explicit
    url = str(source_url).strip() if isinstance(source_url, str) else ""
    if not url:
        return None
    if url.startswith("file://"):
        from urllib.parse import unquote

        return unquote(url[len("file://"):]) or url
    return url
