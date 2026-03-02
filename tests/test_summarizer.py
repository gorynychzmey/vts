import asyncio

import httpx
import pytest

from vts.services.summarizer import llama_chat_completion, llama_tokenize


def _response(
    *,
    status_code: int,
    url: str,
    payload: dict[str, object],
    method: str = "POST",
) -> httpx.Response:
    request = httpx.Request(method, url)
    return httpx.Response(status_code, json=payload, request=request)


def test_llama_chat_completion_retries_without_response_format(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = "http://llama.local/v1/chat/completions"
    post_calls: list[dict[str, object]] = []

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            post_calls.append({"url": url, "json": json})
            if "response_format" in json:
                return _response(
                    status_code=400,
                    url=endpoint,
                    payload={"error": {"message": "Unsupported parameter: response_format"}},
                )
            return _response(
                status_code=200,
                url=endpoint,
                payload={"choices": [{"message": {"content": '{"status":"ready"}'}}]},
            )

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={"error": "not found"}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        llama_chat_completion(
            llama_url="http://llama.local/v1",
            model="Qwen2.5-7B-Instruct-Q4",
            system_prompt='Return compact JSON: {"status":"ready"}.',
            user_prompt="Warm up model for upcoming summarization.",
        )
    )

    assert raw == '{"status":"ready"}'
    assert len(post_calls) >= 2
    assert any(
        isinstance(call["json"], dict) and "response_format" in call["json"]
        for call in post_calls
    )
    assert any(
        isinstance(call["json"], dict) and "response_format" not in call["json"]
        for call in post_calls
    )


def test_llama_chat_completion_uses_model_from_models_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_endpoint = "http://llama.local/v1/chat/completions"
    models_endpoint = "http://llama.local/v1/models"
    get_responses = [
        _response(
            status_code=200,
            url=models_endpoint,
            payload={
                "data": [
                    {"id": "server-model-id"},
                ]
            },
            method="GET",
        )
    ]
    post_calls: list[dict[str, object]] = []
    get_calls: list[str] = []

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            post_calls.append({"url": url, "json": json})
            model_value = str(json.get("model", ""))
            if model_value == "server-model-id":
                return _response(
                    status_code=200,
                    url=chat_endpoint,
                    payload={"choices": [{"message": {"content": '{"status":"ready"}'}}]},
                )
            if not model_value:
                return _response(
                    status_code=400,
                    url=chat_endpoint,
                    payload={"error": {"message": "Model is required"}},
                )
            return _response(
                status_code=400,
                url=chat_endpoint,
                payload={"error": {"message": f"Unknown model: {model_value}"}},
            )

        async def get(self, url: str) -> httpx.Response:
            get_calls.append(url)
            return get_responses.pop(0)

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        llama_chat_completion(
            llama_url="http://llama.local/v1",
            model="unknown-model",
            system_prompt='Return compact JSON: {"status":"ready"}.',
            user_prompt="Warm up model for upcoming summarization.",
        )
    )

    assert raw == '{"status":"ready"}'
    assert get_calls == [models_endpoint]
    assert any(
        isinstance(call["json"], dict) and call["json"].get("model") == "server-model-id"
        for call in post_calls
    )


def test_llama_chat_completion_retries_with_gguf_model_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_endpoint = "http://llama.local/v1/chat/completions"
    post_responses = [
        _response(
            status_code=400,
            url=chat_endpoint,
            payload={"error": {"message": "model 'Qwen2.5-7B-Instruct-Q4_K_M' not found"}},
        ),
        _response(
            status_code=200,
            url=chat_endpoint,
            payload={"choices": [{"message": {"content": '{"status":"ready"}'}}]},
        ),
    ]
    post_calls: list[dict[str, object]] = []

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            post_calls.append({"url": url, "json": json})
            return post_responses.pop(0)

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={"error": "not found"}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        llama_chat_completion(
            llama_url="http://llama.local/v1",
            model="Qwen2.5-7B-Instruct-Q4_K_M",
            system_prompt='Return compact JSON: {"status":"ready"}.',
            user_prompt="Warm up model for upcoming summarization.",
        )
    )

    assert raw == '{"status":"ready"}'
    assert len(post_calls) == 2
    second_payload = post_calls[1]["json"]
    assert isinstance(second_payload, dict)
    assert second_payload.get("model") == "Qwen2.5-7B-Instruct-Q4_K_M.gguf"


def test_llama_chat_completion_failure_contains_body_message(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_endpoint = "http://llama.local/v1/chat/completions"

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            if "response_format" in json:
                message = "Unsupported parameter: response_format"
            elif "max_completion_tokens" in json:
                message = "Unsupported parameter: max_completion_tokens"
            elif "model" not in json:
                message = "model name is missing from the request"
            else:
                message = f"model '{json['model']}' not found"
            return _response(
                status_code=400,
                url=chat_endpoint,
                payload={"error": {"message": message}},
            )

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={"error": "not found"}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            llama_chat_completion(
                llama_url="http://llama.local/v1",
                model="Qwen2.5-7B-Instruct-Q4",
                system_prompt='Return compact JSON: {"status":"ready"}.',
                user_prompt="Warm up model for upcoming summarization.",
                max_tokens=32,
            )
        )
    message = str(excinfo.value)
    assert "Unsupported parameter: response_format" in message
    assert "Unsupported parameter: max_completion_tokens" in message
    assert "model name is missing from the request" in message


def test_llama_chat_completion_retries_on_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = "http://llama.local/v1/chat/completions"
    post_calls: list[dict[str, object]] = []

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            post_calls.append({"url": url, "json": json})
            if len(post_calls) == 1:
                raise httpx.ReadTimeout("simulated timeout", request=httpx.Request("POST", endpoint))
            return _response(
                status_code=200,
                url=endpoint,
                payload={"choices": [{"message": {"content": '{"status":"ready"}'}}]},
            )

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={"error": "not found"}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        llama_chat_completion(
            llama_url="http://llama.local/v1",
            model="Qwen2.5-7B-Instruct-Q4",
            system_prompt='Return compact JSON: {"status":"ready"}.',
            user_prompt="Warm up model for upcoming summarization.",
            request_attempts=2,
        )
    )

    assert raw == '{"status":"ready"}'
    assert len(post_calls) == 2


def test_llama_tokenize_retries_without_model(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = "http://llama.local/tokenize"
    post_responses = [
        _response(
            status_code=400,
            url=endpoint,
            payload={"error": {"message": "Unknown model"}},
        ),
        _response(
            status_code=200,
            url=endpoint,
            payload={"tokens": [1, 2, 3]},
        ),
    ]
    post_calls: list[dict[str, object]] = []

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            post_calls.append({"url": url, "json": json})
            return post_responses.pop(0)

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    tokens = asyncio.run(
        llama_tokenize(
            llama_url="http://llama.local/v1",
            model="Qwen2.5-7B-Instruct-Q4",
            text="hello",
        )
    )

    assert tokens == [1, 2, 3]
    assert len(post_calls) == 2
    first_payload = post_calls[0]["json"]
    second_payload = post_calls[1]["json"]
    assert isinstance(first_payload, dict)
    assert isinstance(second_payload, dict)
    assert first_payload.get("model") == "Qwen2.5-7B-Instruct-Q4"
    assert "model" not in second_payload


def test_llama_tokenize_retries_with_server_model(monkeypatch: pytest.MonkeyPatch) -> None:
    tokenize_endpoint = "http://llama.local/tokenize"
    models_endpoint = "http://llama.local/v1/models"
    post_responses = [
        _response(
            status_code=400,
            url=tokenize_endpoint,
            payload={"error": {"message": "Unknown model"}},
        ),
        _response(
            status_code=400,
            url=tokenize_endpoint,
            payload={"error": {"message": "Model is required"}},
        ),
        _response(
            status_code=400,
            url=tokenize_endpoint,
            payload={"error": {"message": "Unknown model with .gguf"}},
        ),
        _response(
            status_code=200,
            url=tokenize_endpoint,
            payload={"tokens": [42]},
        ),
    ]
    get_responses = [
        _response(
            status_code=200,
            url=models_endpoint,
            payload={"data": [{"id": "Qwen2.5-7B-Instruct-Q4_K_M"}]},
            method="GET",
        )
    ]
    post_calls: list[dict[str, object]] = []
    get_calls: list[str] = []

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            post_calls.append({"url": url, "json": json})
            return post_responses.pop(0)

        async def get(self, url: str) -> httpx.Response:
            get_calls.append(url)
            return get_responses.pop(0)

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    tokens = asyncio.run(
        llama_tokenize(
            llama_url="http://llama.local/v1",
            model="model-name-without-match",
            text="hello",
        )
    )

    assert tokens == [42]
    assert get_calls == [models_endpoint]
    assert any(
        isinstance(call["json"], dict) and call["json"].get("model") == "Qwen2.5-7B-Instruct-Q4_K_M"
        for call in post_calls
    )


def test_llama_tokenize_retries_when_model_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = "http://llama.local/tokenize"
    post_responses = [
        _response(
            status_code=503,
            url=endpoint,
            payload={"error": {"message": "Loading model"}},
        ),
        _response(
            status_code=503,
            url=endpoint,
            payload={"error": {"message": "Loading model"}},
        ),
        _response(
            status_code=200,
            url=endpoint,
            payload={"tokens": [7, 8, 9]},
        ),
    ]
    post_calls: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            post_calls.append({"url": url, "json": json})
            return post_responses.pop(0)

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)
    monkeypatch.setattr("vts.services.summarizer.asyncio.sleep", _fake_sleep)

    tokens = asyncio.run(
        llama_tokenize(
            llama_url="http://llama.local/v1",
            model="Qwen2.5-7B-Instruct-Q4_K_M",
            text="hello",
        )
    )

    assert tokens == [7, 8, 9]
    assert len(post_calls) == 3
    assert len(sleep_calls) == 2


def test_llama_chat_completion_stops_variants_on_persistent_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """On persistent ReadTimeout, the variant loop should break immediately and not try more variants."""
    endpoint = "http://llama.local/v1/chat/completions"
    post_calls: list[dict[str, object]] = []

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            post_calls.append({"url": url, "json": json})
            raise httpx.ReadTimeout("simulated timeout", request=httpx.Request("POST", endpoint))

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={"error": "not found"}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    with pytest.raises(RuntimeError, match="ReadTimeout"):
        asyncio.run(
            llama_chat_completion(
                llama_url="http://llama.local/v1",
                model="Qwen2.5-7B-Instruct-Q4",
                system_prompt="Summarize.",
                user_prompt="Very long text.",
                request_attempts=1,
            )
        )

    # Only one POST call should be made — the variant loop breaks on timeout, not iterate all variants
    assert len(post_calls) == 1


def test_llama_chat_completion_no_response_format_when_use_json_format_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = "http://llama.local/v1/chat/completions"
    post_calls: list[dict[str, object]] = []

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            post_calls.append({"url": url, "json": json})
            return _response(
                status_code=200,
                url=endpoint,
                payload={"choices": [{"message": {"content": "## Topics\n- done"}}]},
            )

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={"error": "not found"}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        llama_chat_completion(
            llama_url="http://llama.local/v1",
            model="Qwen2.5-7B-Instruct-Q4",
            system_prompt="Extract knowledge as markdown.",
            user_prompt="Segment text here.",
            use_json_format=False,
        )
    )

    assert raw == "## Topics\n- done"
    assert all(
        "response_format" not in call["json"]
        for call in post_calls
        if isinstance(call["json"], dict)
    )
