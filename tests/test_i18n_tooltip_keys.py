from __future__ import annotations

import json
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "vts" / "static"

# Keys assigned to element.title via an intermediate variable in app.js,
# which the line-based extractor below cannot see (e.g. app.js ~1678:
# `const label = expanded ? t("action.collapse") : t("action.expand")`,
# and the push-toggle label near app.js ~3725).
EXTRA_TITLE_KEYS = {"action.collapse", "action.disable_notifications"}

# Static aria-label= fallbacks in index.html are deliberately NOT locked to
# en.js here: data-i18n-aria-label overwrites them at runtime, and short
# accessible names are preferable for screen readers even where the visual
# tooltip is a longer result-explaining sentence.


def _load_locale(name: str) -> dict[str, str]:
    src = (STATIC / "i18n" / f"{name}.js").read_text(encoding="utf-8")
    match = re.search(r"window\.__VTS_I18N\.\w+ = (\{.*\});", src, re.S)
    assert match, f"cannot locate the dictionary literal in {name}.js"
    return json.loads(match.group(1))


LOCALES = {name: _load_locale(name) for name in ("ru", "en", "de")}


def _tooltip_keys_from_index() -> set[str]:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return set(re.findall(r'data-i18n-title="([^"]+)"', html))


def _tooltip_keys_from_app_js() -> set[str]:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    keys: set[str] = set()
    for line in js.splitlines():
        if re.search(r'\.title\s*=|setAttribute\("title"', line):
            keys.update(re.findall(r't\("([a-z0-9_.]+)"\)', line))
    return keys


def test_every_tooltip_key_exists_in_all_locales() -> None:
    keys = _tooltip_keys_from_index() | _tooltip_keys_from_app_js() | EXTRA_TITLE_KEYS
    assert len(keys) > 20, f"extractor found only {len(keys)} keys — regressed?"
    missing = {
        locale: sorted(key for key in keys if key not in dictionary)
        for locale, dictionary in LOCALES.items()
    }
    missing = {locale: keys_ for locale, keys_ in missing.items() if keys_}
    assert not missing, f"tooltip keys missing from locale dictionaries: {missing}"


def test_static_title_fallbacks_match_en() -> None:
    """Elements carrying both a static title= fallback and data-i18n-title
    must keep the fallback equal to the en.js value, so the pre-i18n first
    paint shows the same wording English users get after i18n applies."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    en = LOCALES["en"]
    mismatches: list[tuple[str, str, str]] = []
    for tag in re.finditer(r"<[a-zA-Z][^>]*>", html, re.S):
        text = tag.group(0)
        title_match = re.search(r'(?<!-)\btitle="([^"]*)"', text)
        key_match = re.search(r'data-i18n-title="([^"]+)"', text)
        if not (title_match and key_match):
            continue
        expected = en.get(key_match.group(1))
        if expected is not None and title_match.group(1) != expected:
            mismatches.append((key_match.group(1), title_match.group(1), expected))
    assert not mismatches, (
        "static title= fallbacks out of sync with en.js "
        f"(key, fallback, en): {mismatches}"
    )
