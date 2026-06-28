from __future__ import annotations
from dataclasses import dataclass

VALID_SOURCES = {"system", "user"}

@dataclass(frozen=True)
class SystemPresetDef:
    key: str
    i18n_name_key: str
    display_name: str
    options: dict

SYSTEM_PRESETS: list[SystemPresetDef] = [
    SystemPresetDef(
        key="default",
        i18n_name_key="preset.system.default",
        display_name="Default",
        options={
            "language": None, "audio_only": False, "transcript": True,
            "prompts": [{"source": "system", "id": "summary"}],
        },
    ),
]

def list_system_presets() -> list[SystemPresetDef]:
    return list(SYSTEM_PRESETS)

def system_preset_keys() -> set[str]:
    return {p.key for p in SYSTEM_PRESETS}

def default_system_preset() -> SystemPresetDef:
    return SYSTEM_PRESETS[0]

def parse_preset_ref(value: dict | str) -> tuple[str, str]:
    if isinstance(value, str):
        source, _, ref_id = value.partition(":")
    elif isinstance(value, dict):
        source = str(value.get("source", ""))
        ref_id = str(value.get("id", ""))
    else:
        raise ValueError(f"invalid preset ref: {value!r}")
    if source not in VALID_SOURCES:
        raise ValueError(f"invalid preset source: {source!r}")
    if not ref_id:
        raise ValueError("preset ref id must not be empty")
    return source, ref_id

def preset_ref_to_dict(source: str, id: str) -> dict:
    return {"source": source, "id": id}
