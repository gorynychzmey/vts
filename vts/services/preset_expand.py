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


def expand_preset_options(options: dict, valid_user_prompt_ids: set[str]) -> dict[str, Any]:
    o = options or {}
    return {
        "language": o.get("language"),
        "audio_only": bool(o.get("audio_only", False)),
        "transcript": bool(o.get("transcript", True)),
        "prompts": filter_prompt_refs(o.get("prompts", []), valid_user_prompt_ids),
    }
