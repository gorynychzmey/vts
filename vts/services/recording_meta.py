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
