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


def filter_delivery_refs(delivery: Any) -> list[dict]:
    """Normalise a preset's `delivery` list to {deliver_to[, variant]} entries.

    Presets reference targets by NAME only — the target row holds the adapter
    config and the (encrypted) secrets. Anything else in an entry is dropped, so
    a hand-edited preset can never smuggle secrets in through this path.
    """
    out: list[dict] = []
    for item in delivery or []:
        if not isinstance(item, dict):
            continue
        name = item.get("deliver_to")
        if not name:
            continue
        entry: dict = {"deliver_to": str(name)}
        variant = item.get("variant")
        if variant:
            entry["variant"] = str(variant)
        out.append(entry)
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
        "diarize": bool(o.get("diarize", False)),
        "prompts": filter_prompt_refs(o.get("prompts", []), valid_user_prompt_ids),
        # NOTE: this dict is an allowlist — a field absent here is silently
        # dropped from every preset-driven submit. Adding `delivery` to the
        # preset schema without adding it here would look like it worked and
        # deliver nothing.
        "delivery": filter_delivery_refs(o.get("delivery", [])),
    }
