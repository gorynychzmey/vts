from __future__ import annotations
from typing import Any


def filter_prompt_refs(prompts: list[dict], valid_user_ids: set[str]) -> list[dict]:
    out: list[dict] = []
    for r in prompts or []:
        if not isinstance(r, dict):
            continue
        src = r.get("source")
        if src == "system":
            out.append({"source": "system", "id": str(r.get("id"))})
        elif src == "user" and str(r.get("id")) in valid_user_ids:
            out.append({"source": "user", "id": str(r.get("id"))})
    return out


def resolve_preset(source: str, id: str, system_presets: list, user_preset_options: dict | None) -> dict | None:
    """Return the raw options dict for a preset ref, or None if not found.

    system_presets: list of SystemPresetDef; user_preset_options: the user
    preset's .options if source=='user' (caller fetched it), else None."""
    if source == "system":
        for p in system_presets:
            if p.key == id:
                return dict(p.options)
        return None
    return dict(user_preset_options) if user_preset_options is not None else None


def expand_preset_options(options: dict, valid_user_prompt_ids: set[str]) -> dict[str, Any]:
    o = options or {}
    return {
        "language": o.get("language"),
        "audio_only": bool(o.get("audio_only", False)),
        "transcript": bool(o.get("transcript", True)),
        "prompts": filter_prompt_refs(o.get("prompts", []), valid_user_prompt_ids),
    }
