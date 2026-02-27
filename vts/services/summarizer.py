from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx


def _llama_server_base(llama_url: str) -> str:
    url = llama_url.rstrip("/")
    if url.endswith("/v1"):
        return url[: -len("/v1")]
    return url


async def llama_tokenize(
    *,
    llama_url: str,
    model: str,
    text: str,
    timeout_seconds: int = 120,
) -> list[int]:
    endpoint = _llama_server_base(llama_url) + "/tokenize"
    payload: dict[str, Any] = {"content": text}
    if model:
        payload["model"] = model
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(endpoint, json=payload)
    response.raise_for_status()
    data = response.json()
    tokens = data.get("tokens")
    if not isinstance(tokens, list):
        raise RuntimeError("Invalid llama.cpp tokenize response format")
    return [int(token) for token in tokens]


async def llama_detokenize(
    *,
    llama_url: str,
    model: str,
    tokens: list[int],
    timeout_seconds: int = 120,
) -> str:
    endpoint = _llama_server_base(llama_url) + "/detokenize"
    payload: dict[str, Any] = {"tokens": tokens}
    if model:
        payload["model"] = model
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(endpoint, json=payload)
    response.raise_for_status()
    data = response.json()
    content = data.get("content")
    if not isinstance(content, str):
        raise RuntimeError("Invalid llama.cpp detokenize response format")
    return content


async def chunk_text(
    *,
    text: str,
    llama_url: str,
    model: str,
    window_tokens: int = 2000,
    overlap_ratio: float = 0.15,
) -> list[str]:
    if not text.strip():
        return []
    tokens = await llama_tokenize(llama_url=llama_url, model=model, text=text)
    if not tokens:
        return []

    overlap = max(int(window_tokens * overlap_ratio), 1)
    step = max(window_tokens - overlap, 1)
    chunks: list[str] = []
    cursor = 0
    while cursor < len(tokens):
        part = tokens[cursor : cursor + window_tokens]
        chunk = await llama_detokenize(llama_url=llama_url, model=model, tokens=part)
        if chunk.strip():
            chunks.append(chunk)
        if cursor + window_tokens >= len(tokens):
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
    max_tokens: int | None = None,
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
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
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
