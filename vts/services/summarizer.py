from __future__ import annotations

import asyncio
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx


@lru_cache(maxsize=4)
def _load_tokenizer(path: str) -> "tokenizers.Tokenizer":  # type: ignore[name-defined]
    import tokenizers  # noqa: PLC0415

    return tokenizers.Tokenizer.from_file(path)


def _tokenize_local(path: str, text: str) -> list[int]:
    enc = _load_tokenizer(path).encode(text)
    return enc.ids


def _detokenize_local(path: str, token_ids: list[int]) -> str:
    return _load_tokenizer(path).decode(token_ids)


def _llama_server_base(url: str) -> str:
    url = url.rstrip("/")
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


def _is_loading_model_response(response: httpx.Response) -> bool:
    if response.status_code not in (503, 529):
        return False
    detail = _response_error_detail(response).lower()
    return "loading model" in detail or "model is loading" in detail


async def _post_with_loading_retry(
    *,
    client: httpx.AsyncClient,
    endpoint: str,
    payload: dict[str, Any],
    loading_wait_seconds: float,
) -> httpx.Response:
    response = await client.post(endpoint, json=payload)
    elapsed = 0.0
    attempt = 0
    while _is_loading_model_response(response) and elapsed < loading_wait_seconds:
        delay = min(0.5 * (2**attempt), 5.0)
        wait_for = min(delay, loading_wait_seconds - elapsed)
        if wait_for <= 0:
            break
        await asyncio.sleep(wait_for)
        elapsed += wait_for
        response = await client.post(endpoint, json=payload)
        attempt += 1
    return response


def _loading_wait_seconds(timeout_seconds: int, *, cap_seconds: float = 120.0) -> float:
    return max(5.0, min(float(timeout_seconds) * 0.6, cap_seconds))


def _is_transient_http_error(exc: Exception) -> bool:
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError))


async def _post_with_transient_retry(
    *,
    client: httpx.AsyncClient,
    endpoint: str,
    payload: dict[str, Any],
    loading_wait_seconds: float,
    max_attempts: int,
) -> httpx.Response:
    attempts = max(1, max_attempts)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await _post_with_loading_retry(
                client=client,
                endpoint=endpoint,
                payload=payload,
                loading_wait_seconds=loading_wait_seconds,
            )
        except Exception as exc:
            if not _is_transient_http_error(exc):
                raise
            last_exc = exc
            if attempt >= attempts:
                break
            await asyncio.sleep(min(0.5 * (2 ** (attempt - 1)), 5.0))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("llama request failed without response and without captured exception")


def _build_chat_payload(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int | None,
    include_response_format: bool,
    temperature: float,
    top_p: float | None = None,
    min_p: float | None = None,
    repeat_penalty: float | None = None,
    cache_prompt: bool = False,
    max_tokens_key: str = "max_tokens",
    include_model: bool = True,
    model_override: str | None = None,
    thinking: bool | None = None,
    num_ctx: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    if thinking is not None:
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if top_p is not None:
        payload["top_p"] = top_p
    if min_p is not None:
        payload["min_p"] = min_p
    if repeat_penalty is not None:
        payload["repeat_penalty"] = repeat_penalty
    if cache_prompt:
        payload["cache_prompt"] = True
    if include_response_format:
        payload["response_format"] = {"type": "json_object"}
    selected_model = model_override if model_override is not None else model
    if include_model and selected_model.strip():
        payload["model"] = selected_model
    if max_tokens is not None:
        payload[max_tokens_key] = max_tokens
    if num_ctx is not None:
        payload["num_ctx"] = num_ctx
    return payload


def _model_name_variants(model: str) -> list[str]:
    value = model.strip()
    if not value:
        return []
    candidates: list[str] = []

    def add(item: str) -> None:
        normalized = item.strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    add(value)
    if value.endswith(".gguf"):
        add(value[: -len(".gguf")])
    else:
        add(f"{value}.gguf")
    if "/" in value:
        basename = value.rsplit("/", 1)[-1]
        add(basename)
        if basename.endswith(".gguf"):
            add(basename[: -len(".gguf")])
        else:
            add(f"{basename}.gguf")
    return candidates


class LLMClient:
    def __init__(self, *, url: str, api_key: str | None = None) -> None:
        self.url = url
        self._headers: dict[str, str] = (
            {"Authorization": f"Bearer {api_key}"} if api_key else {}
        )

    def _client(self, timeout_seconds: int) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout_seconds, headers=self._headers)

    async def _list_models(self, *, client: httpx.AsyncClient) -> list[str]:
        endpoint = self.url.rstrip("/") + "/models"
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

    async def tokenize(
        self,
        *,
        model: str,
        text: str,
        timeout_seconds: int = 120,
        tokenizer_path: str | None = None,
    ) -> list[int]:
        if tokenizer_path:
            return _tokenize_local(tokenizer_path, text)
        endpoint = _llama_server_base(self.url) + "/tokenize"
        payload: dict[str, Any] = {"content": text}
        if model:
            payload["model"] = model
        loading_wait_seconds = _loading_wait_seconds(timeout_seconds, cap_seconds=90.0)
        async with self._client(timeout_seconds) as client:
            response = await _post_with_loading_retry(
                client=client,
                endpoint=endpoint,
                payload=payload,
                loading_wait_seconds=loading_wait_seconds,
            )
            if response.status_code == 400 and model:
                retry_response = await _post_with_loading_retry(
                    client=client,
                    endpoint=endpoint,
                    payload={"content": text},
                    loading_wait_seconds=loading_wait_seconds,
                )
                if retry_response.is_success:
                    response = retry_response
                else:
                    recovered = False
                    for variant in _model_name_variants(model)[1:]:
                        model_retry = await _post_with_loading_retry(
                            client=client,
                            endpoint=endpoint,
                            payload={"content": text, "model": variant},
                            loading_wait_seconds=loading_wait_seconds,
                        )
                        if model_retry.is_success:
                            response = model_retry
                            recovered = True
                            break
                    if not recovered:
                        available_models = await self._list_models(client=client)
                        if available_models and model not in available_models:
                            server_model = available_models[0]
                            model_retry = await _post_with_loading_retry(
                                client=client,
                                endpoint=endpoint,
                                payload={"content": text, "model": server_model},
                                loading_wait_seconds=loading_wait_seconds,
                            )
                            if model_retry.is_success:
                                response = model_retry
                                recovered = True
                            else:
                                _raise_with_response_details(
                                    model_retry,
                                    context=f"llama tokenize (retry with model={server_model})",
                                )
                    if not recovered:
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

    async def detokenize(
        self,
        *,
        model: str,
        tokens: list[int],
        timeout_seconds: int = 120,
        tokenizer_path: str | None = None,
    ) -> str:
        if tokenizer_path:
            return _detokenize_local(tokenizer_path, tokens)
        endpoint = _llama_server_base(self.url) + "/detokenize"
        payload: dict[str, Any] = {"tokens": tokens}
        if model:
            payload["model"] = model
        loading_wait_seconds = _loading_wait_seconds(timeout_seconds, cap_seconds=90.0)
        async with self._client(timeout_seconds) as client:
            response = await _post_with_loading_retry(
                client=client,
                endpoint=endpoint,
                payload=payload,
                loading_wait_seconds=loading_wait_seconds,
            )
            if response.status_code == 400 and model:
                retry_response = await _post_with_loading_retry(
                    client=client,
                    endpoint=endpoint,
                    payload={"tokens": tokens},
                    loading_wait_seconds=loading_wait_seconds,
                )
                if retry_response.is_success:
                    response = retry_response
                else:
                    recovered = False
                    for variant in _model_name_variants(model)[1:]:
                        model_retry = await _post_with_loading_retry(
                            client=client,
                            endpoint=endpoint,
                            payload={"tokens": tokens, "model": variant},
                            loading_wait_seconds=loading_wait_seconds,
                        )
                        if model_retry.is_success:
                            response = model_retry
                            recovered = True
                            break
                    if not recovered:
                        available_models = await self._list_models(client=client)
                        if available_models and model not in available_models:
                            server_model = available_models[0]
                            model_retry = await _post_with_loading_retry(
                                client=client,
                                endpoint=endpoint,
                                payload={"tokens": tokens, "model": server_model},
                                loading_wait_seconds=loading_wait_seconds,
                            )
                            if model_retry.is_success:
                                response = model_retry
                                recovered = True
                            else:
                                _raise_with_response_details(
                                    model_retry,
                                    context=f"llama detokenize (retry with model={server_model})",
                                )
                    if not recovered:
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
        self,
        *,
        text: str,
        model: str,
        window_tokens: int = 2000,
        overlap_ratio: float = 0.15,
        tokenizer_path: str | None = None,
    ) -> list[str]:
        if not text.strip():
            return []
        tokens = await self.tokenize(model=model, text=text, tokenizer_path=tokenizer_path)
        if not tokens:
            return []

        overlap = max(int(window_tokens * overlap_ratio), 1)
        step = max(window_tokens - overlap, 1)
        chunks: list[str] = []
        cursor = 0
        while cursor < len(tokens):
            part = tokens[cursor : cursor + window_tokens]
            chunk = await self.detokenize(model=model, tokens=part, tokenizer_path=tokenizer_path)
            if chunk.strip():
                chunks.append(chunk)
            if cursor + window_tokens >= len(tokens):
                break
            cursor += step
        return chunks

    async def chat_completion(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: int = 600,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        top_p: float | None = None,
        min_p: float | None = None,
        repeat_penalty: float | None = None,
        cache_prompt: bool = False,
        request_attempts: int = 3,
        use_json_format: bool = True,
        thinking: bool | None = None,
        num_ctx: int | None = None,
    ) -> str:
        endpoint = self.url.rstrip("/") + "/chat/completions"
        loading_wait_seconds = _loading_wait_seconds(timeout_seconds, cap_seconds=120.0)
        queue: list[tuple[str, dict[str, Any]]] = []
        seen_payloads: set[str] = set()

        def enqueue(label: str, payload: dict[str, Any]) -> None:
            key = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            if key in seen_payloads:
                return
            seen_payloads.add(key)
            queue.append((label, payload))

        common = dict(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            min_p=min_p,
            repeat_penalty=repeat_penalty,
            cache_prompt=cache_prompt,
            num_ctx=num_ctx,
        )

        enqueue("default", _build_chat_payload(**common, include_response_format=use_json_format, thinking=thinking))
        model_variants = _model_name_variants(model)
        for variant in model_variants[1:]:
            enqueue(f"default_model_variant:{variant}", _build_chat_payload(**common, model_override=variant, include_response_format=use_json_format))
            enqueue(f"without_response_format_model_variant:{variant}", _build_chat_payload(**common, model_override=variant, include_response_format=False))
            if max_tokens is not None:
                enqueue(f"without_response_format_model_variant:{variant}:max_completion_tokens", _build_chat_payload(**common, model_override=variant, include_response_format=False, max_tokens_key="max_completion_tokens"))
        enqueue("without_response_format", _build_chat_payload(**common, include_response_format=False, thinking=thinking))
        if max_tokens is not None:
            enqueue("without_response_format_max_completion_tokens", _build_chat_payload(**common, include_response_format=False, max_tokens_key="max_completion_tokens", thinking=thinking))
        enqueue("without_response_format_without_model", _build_chat_payload(**common, include_response_format=False, thinking=thinking, include_model=False))
        if max_tokens is not None:
            enqueue("without_response_format_without_model_max_completion_tokens", _build_chat_payload(**common, include_response_format=False, max_tokens_key="max_completion_tokens", thinking=thinking, include_model=False))

        failures: list[str] = []
        discovered_model_fallback = False
        async with self._client(timeout_seconds) as client:
            while queue:
                label, payload = queue.pop(0)
                try:
                    response = await _post_with_transient_retry(
                        client=client,
                        endpoint=endpoint,
                        payload=payload,
                        loading_wait_seconds=loading_wait_seconds,
                        max_attempts=request_attempts,
                    )
                except Exception as exc:
                    if _is_transient_http_error(exc):
                        failures.append(f"{label}: {exc.__class__.__name__} ({str(exc).strip() or 'no details'})")
                        if isinstance(exc, httpx.TimeoutException):
                            attempts = "; ".join(failures)
                            raise RuntimeError(
                                f"llama chat completion failed after retries for {endpoint}: {attempts}"
                            ) from exc
                        continue
                    raise
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
                    available_models = await self._list_models(client=client)
                    if available_models and model not in available_models:
                        server_model = available_models[0]
                        enqueue(f"server_model:{server_model}", _build_chat_payload(**common, model_override=server_model, include_response_format=False))
                        if max_tokens is not None:
                            enqueue(f"server_model:{server_model}:max_completion_tokens", _build_chat_payload(**common, model_override=server_model, include_response_format=False, max_tokens_key="max_completion_tokens"))
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

    async def count_tokens(
        self,
        *,
        text: str,
        model: str,
        timeout_seconds: int = 120,
        tokenizer_path: str | None = None,
    ) -> int:
        tokens = await self.tokenize(
            model=model,
            text=text,
            timeout_seconds=timeout_seconds,
            tokenizer_path=tokenizer_path,
        )
        return len(tokens)


def load_prompt(prompts_dir: Path, filename: str, fallback: str) -> str:
    path = prompts_dir / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return fallback


def tokens_to_words(tokens: int) -> int:
    """Approximate word count from a token count (≈0.75 words per token)."""
    return max(1, round(tokens * 0.75))


def inject_budget_vars(
    prompt: str,
    *,
    input_tokens: int | None = None,
    target_tokens: int | None = None,
    target_ratio: float | None = None,
) -> str:
    """Replace budget placeholders in *prompt*.

    Supported placeholders: ``${INPUT_WORDS}``, ``${TARGET_WORDS}``,
    ``${TARGET_RATIO}``.
    """
    if input_tokens is not None:
        prompt = prompt.replace("${INPUT_WORDS}", str(tokens_to_words(input_tokens)))
    if target_tokens is not None:
        prompt = prompt.replace("${TARGET_WORDS}", str(tokens_to_words(target_tokens)))
    if target_ratio is not None:
        prompt = prompt.replace("${TARGET_RATIO}", str(round(target_ratio * 100)))
    return prompt


def parse_json_response(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw, "summary": raw}
    if isinstance(payload, dict):
        return payload
    return {"raw": raw, "summary": str(payload)}
