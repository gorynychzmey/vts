from __future__ import annotations

from dataclasses import dataclass

VALID_SOURCES = {"system", "user"}


@dataclass(frozen=True)
class SystemPromptDef:
    key: str
    file: str
    i18n_name_key: str
    display_name: str


SYSTEM_PROMPTS: list[SystemPromptDef] = [
    SystemPromptDef("summary", "global_prompt.md", "prompt.system.summary", "Summary"),
]


def list_system_prompts() -> list[SystemPromptDef]:
    return list(SYSTEM_PROMPTS)


def system_prompt_keys() -> set[str]:
    return {p.key for p in SYSTEM_PROMPTS}


def parse_ref(value: dict | str) -> tuple[str, str]:
    if isinstance(value, str):
        source, _, ref_id = value.partition(":")
    elif isinstance(value, dict):
        source = str(value.get("source", ""))
        ref_id = str(value.get("id", ""))
    else:
        raise ValueError(f"invalid prompt ref: {value!r}")
    if source not in VALID_SOURCES:
        raise ValueError(f"invalid prompt source: {source!r}")
    if not ref_id:
        raise ValueError("prompt ref id must not be empty")
    return source, ref_id


def ref_to_dict(source: str, id: str) -> dict:
    return {"source": source, "id": id}


def ref_key(source: str, id: str) -> str:
    return f"{source}:{id}"
