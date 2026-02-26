from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx


def chunk_text(text: str, window_tokens: int = 2000, overlap_ratio: float = 0.15) -> list[str]:
    words = text.split()
    if not words:
        return []
    overlap = max(int(window_tokens * overlap_ratio), 1)
    step = max(window_tokens - overlap, 1)
    chunks: list[str] = []
    cursor = 0
    while cursor < len(words):
        part = words[cursor : cursor + window_tokens]
        chunks.append(" ".join(part))
        if cursor + window_tokens >= len(words):
            break
        cursor += step
    return chunks


def load_prompt(prompts_dir: Path, filename: str, fallback: str) -> str:
    path = prompts_dir / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return fallback


async def llama_chat_completion(
    *,
    llama_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout_seconds: int = 600,
) -> str:
    endpoint = llama_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
    }
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(endpoint, json=payload)
    response.raise_for_status()
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Invalid llama.cpp response format") from exc
    return str(content)


def parse_json_response(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw, "summary": raw}
    if isinstance(payload, dict):
        return payload
    return {"raw": raw, "summary": str(payload)}

