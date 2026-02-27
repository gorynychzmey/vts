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


def _response_error_detail(response: httpx.Response, *, max_len: int = 800) -> str:
    detail: str | None = None
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = None

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                detail = message.strip()
            elif isinstance(error.get("type"), str):
                detail = str(error["type"]).strip()
        elif isinstance(error, str) and error.strip():
            detail = error.strip()
        elif isinstance(payload.get("message"), str):
            detail = str(payload["message"]).strip()

    if not detail:
        text = response.text.strip()
        if text:
            detail = text
    if not detail:
        detail = "<empty response body>"
    compact = " ".join(detail.split())
    if len(compact) > max_len:
        return compact[: max_len - 3] + "..."
    return compact


def _raise_with_response_details(response: httpx.Response, *, context: str) -> None:
    request_url = "<unknown>"
    if response.request is not None:
        request_url = str(response.request.url)
    detail = _response_error_detail(response)
    raise RuntimeError(
        f"{context} failed with HTTP {response.status_code} for {request_url}: {detail}"
    )


def _build_chat_payload(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int | None,
    include_response_format: bool,
    max_tokens_key: str = "max_tokens",
    include_model: bool = True,
    model_override: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
    }
    if include_response_format:
        payload["response_format"] = {"type": "json_object"}
    selected_model = model_override if model_override is not None else model
    if include_model and selected_model.strip():
        payload["model"] = selected_model
    if max_tokens is not None:
        payload[max_tokens_key] = max_tokens
    return payload


async def _list_chat_models(
    *,
    client: httpx.AsyncClient,
    llama_url: str,
) -> list[str]:
    endpoint = llama_url.rstrip("/") + "/models"
    try:
        response = await client.get(endpoint)
    except httpx.HTTPError:
        return []
    if not response.is_success:
        return []
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    result: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if isinstance(model_id, str) and model_id.strip():
            result.append(model_id.strip())
    return result


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
        if response.status_code == 400 and model:
            retry_response = await client.post(endpoint, json={"content": text})
            if retry_response.is_success:
                response = retry_response
            else:
                available_models = await _list_chat_models(client=client, llama_url=llama_url)
                if available_models and model not in available_models:
                    server_model = available_models[0]
                    model_retry = await client.post(
                        endpoint,
                        json={"content": text, "model": server_model},
                    )
                    if model_retry.is_success:
                        response = model_retry
                    else:
                        _raise_with_response_details(
                            model_retry,
                            context=f"llama tokenize (retry with model={server_model})",
                        )
                else:
                    _raise_with_response_details(
                        retry_response,
                        context="llama tokenize (retry without model)",
                    )
    if not response.is_success:
        _raise_with_response_details(response, context="llama tokenize")
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
        if response.status_code == 400 and model:
            retry_response = await client.post(endpoint, json={"tokens": tokens})
            if retry_response.is_success:
                response = retry_response
            else:
                available_models = await _list_chat_models(client=client, llama_url=llama_url)
                if available_models and model not in available_models:
                    server_model = available_models[0]
                    model_retry = await client.post(
                        endpoint,
                        json={"tokens": tokens, "model": server_model},
                    )
                    if model_retry.is_success:
                        response = model_retry
                    else:
                        _raise_with_response_details(
                            model_retry,
                            context=f"llama detokenize (retry with model={server_model})",
                        )
                else:
                    _raise_with_response_details(
                        retry_response,
                        context="llama detokenize (retry without model)",
                    )
    if not response.is_success:
        _raise_with_response_details(response, context="llama detokenize")
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
    queue: list[tuple[str, dict[str, Any]]] = []
    seen_payloads: set[str] = set()

    def enqueue(label: str, payload: dict[str, Any]) -> None:
        key = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if key in seen_payloads:
            return
        seen_payloads.add(key)
        queue.append((label, payload))

    enqueue(
        "default",
        _build_chat_payload(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            include_response_format=True,
        ),
    )
    enqueue(
        "without_response_format",
        _build_chat_payload(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            include_response_format=False,
        ),
    )
    if max_tokens is not None:
        enqueue(
            "without_response_format_max_completion_tokens",
            _build_chat_payload(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                include_response_format=False,
                max_tokens_key="max_completion_tokens",
            ),
        )
    enqueue(
        "without_response_format_without_model",
        _build_chat_payload(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            include_response_format=False,
            include_model=False,
        ),
    )
    if max_tokens is not None:
        enqueue(
            "without_response_format_without_model_max_completion_tokens",
            _build_chat_payload(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                include_response_format=False,
                max_tokens_key="max_completion_tokens",
                include_model=False,
            ),
        )

    failures: list[str] = []
    discovered_model_fallback = False
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        while queue:
            label, payload = queue.pop(0)
            response = await client.post(endpoint, json=payload)
            if response.is_success:
                data = response.json()
                break
            failures.append(
                f"{label}: HTTP {response.status_code} ({_response_error_detail(response)})"
            )
            if response.status_code != 400:
                _raise_with_response_details(response, context="llama chat completion")
            if not discovered_model_fallback and model.strip():
                discovered_model_fallback = True
                available_models = await _list_chat_models(client=client, llama_url=llama_url)
                if available_models and model not in available_models:
                    server_model = available_models[0]
                    enqueue(
                        f"server_model:{server_model}",
                        _build_chat_payload(
                            model=model,
                            model_override=server_model,
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            max_tokens=max_tokens,
                            include_response_format=False,
                        ),
                    )
                    if max_tokens is not None:
                        enqueue(
                            f"server_model:{server_model}:max_completion_tokens",
                            _build_chat_payload(
                                model=model,
                                model_override=server_model,
                                system_prompt=system_prompt,
                                user_prompt=user_prompt,
                                max_tokens=max_tokens,
                                include_response_format=False,
                                max_tokens_key="max_completion_tokens",
                            ),
                        )
        else:
            attempts = "; ".join(failures) if failures else "no attempts executed"
            raise RuntimeError(
                f"llama chat completion failed after retries for {endpoint}: {attempts}"
            )

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
