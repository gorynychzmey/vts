import asyncio
import json
from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import httpx
import pytest

from vts.services.summarizer import (
    LLMClient,
    _load_tokenizer,
    _tokenize_local,
    _detokenize_local,
)


def _response(
    *,
    status_code: int,
    url: str,
    payload: dict[str, object],
    method: str = "POST",
) -> httpx.Response:
    request = httpx.Request(method, url)
    return httpx.Response(status_code, json=payload, request=request)


def _client(url: str = "http://llama.local/v1") -> LLMClient:
    return LLMClient(url=url)


def _sse_frames(content: str) -> bytes:
    """One completion delivered as a single SSE content frame."""
    frame = 'data: {"choices":[{"delta":{"content":%s}}]}' % json.dumps(content)
    return (frame + "\ndata: [DONE]\n").encode()


class _StubStream:
    """Stands in for the context manager returned by `httpx.AsyncClient.stream`.

    `chat_completion` streams now, so a stub that only answers `post` never
    gets called. This mirrors the surface the real code touches: status,
    `is_success`, `aiter_lines` for the body and `aread` for an error body.
    Non-2xx responses carry their JSON payload through `aread` so the
    fallback-queue tests can still assert on upstream error messages.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        content: str | None = None,
        error_message: str | None = None,
        raise_on_iter: BaseException | None = None,
    ) -> None:
        self.status_code = status_code
        self._content = content
        self._error_message = error_message
        self._raise_on_iter = raise_on_iter

    async def __aenter__(self) -> "_StubStream":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    async def aiter_lines(self) -> "AsyncIterator[str]":
        if self._raise_on_iter is not None:
            raise self._raise_on_iter
        body = _sse_frames(self._content if self._content is not None else "")
        for line in body.decode().splitlines():
            yield line

    async def aread(self) -> bytes:
        if self._error_message is not None:
            return json.dumps({"error": {"message": self._error_message}}).encode()
        return b""


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

        def stream(self, method: str, url: str, json: dict[str, object]) -> _StubStream:
            post_calls.append({"url": url, "json": json})
            if "response_format" in json:
                return _StubStream(
                    status_code=400,
                    error_message="Unsupported parameter: response_format",
                )
            return _StubStream(content='{"status":"ready"}')

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={"error": "not found"}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        _client().chat_completion(
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

        def stream(self, method: str, url: str, json: dict[str, object]) -> _StubStream:
            post_calls.append({"url": url, "json": json})
            model_value = str(json.get("model", ""))
            if model_value == "server-model-id":
                return _StubStream(content='{"status":"ready"}')
            if not model_value:
                return _StubStream(status_code=400, error_message="Model is required")
            return _StubStream(
                status_code=400, error_message=f"Unknown model: {model_value}"
            )

        async def get(self, url: str) -> httpx.Response:
            get_calls.append(url)
            return get_responses.pop(0)

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        _client().chat_completion(
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
    post_responses = [
        _StubStream(
            status_code=400,
            error_message="model 'Qwen2.5-7B-Instruct-Q4_K_M' not found",
        ),
        _StubStream(content='{"status":"ready"}'),
    ]
    post_calls: list[dict[str, object]] = []

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def stream(self, method: str, url: str, json: dict[str, object]) -> _StubStream:
            post_calls.append({"url": url, "json": json})
            return post_responses.pop(0)

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={"error": "not found"}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        _client().chat_completion(
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

        def stream(self, method: str, url: str, json: dict[str, object]) -> _StubStream:
            if "response_format" in json:
                message = "Unsupported parameter: response_format"
            elif "max_completion_tokens" in json:
                message = "Unsupported parameter: max_completion_tokens"
            elif "model" not in json:
                message = "model name is missing from the request"
            else:
                message = f"model '{json['model']}' not found"
            return _StubStream(status_code=400, error_message=message)

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={"error": "not found"}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            _client().chat_completion(
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


def test_llama_chat_completion_recovers_from_a_transient_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient network error before any output must not fail the call.

    Streaming removed the per-payload retry loop (`request_attempts` only
    governed the blocking POST), so recovery now comes from advancing to the
    next payload variant instead. The guarantee under test is unchanged: a
    connection that drops before the first chunk gets another chance.
    """
    endpoint = "http://llama.local/v1/chat/completions"
    post_calls: list[dict[str, object]] = []

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def stream(self, method: str, url: str, json: dict[str, object]) -> _StubStream:
            post_calls.append({"url": url, "json": json})
            if len(post_calls) == 1:
                return _StubStream(
                    raise_on_iter=httpx.ReadTimeout(
                        "simulated timeout", request=httpx.Request("POST", endpoint)
                    )
                )
            return _StubStream(content='{"status":"ready"}')

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={"error": "not found"}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        _client().chat_completion(
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
        _client().tokenize(
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
        _client().tokenize(
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
        _client().tokenize(
            model="Qwen2.5-7B-Instruct-Q4_K_M",
            text="hello",
        )
    )

    assert tokens == [7, 8, 9]
    assert len(post_calls) == 3
    assert len(sleep_calls) == 2


def test_llama_chat_completion_reports_a_persistent_transport_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that times out on every variant fails, naming the cause.

    This test previously asserted exactly one attempt: the blocking-POST code
    special-cased `httpx.TimeoutException` and abandoned the queue at once, so
    a hung backend could not burn every variant on a long timeout. Streaming
    removes that special case — a transport error before the first chunk is
    now indistinguishable from any other pre-generation failure, and the queue
    advances. That is acceptable because the socket read timeout bounds each
    attempt, and because the expensive case this feature exists for — a stall
    *after* generation started — raises StreamTimeout and is never retried
    (see test_chat_completion_does_not_retry_a_stream_timeout).

    What must still hold, and is asserted here: the call terminates, the error
    names ReadTimeout, and the queue is finite rather than retried forever.
    """
    endpoint = "http://llama.local/v1/chat/completions"
    post_calls: list[dict[str, object]] = []

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def stream(self, method: str, url: str, json: dict[str, object]) -> _StubStream:
            post_calls.append({"url": url, "json": json})
            return _StubStream(
                raise_on_iter=httpx.ReadTimeout(
                    "simulated timeout", request=httpx.Request("POST", endpoint)
                )
            )

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={"error": "not found"}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    with pytest.raises(RuntimeError, match="ReadTimeout"):
        asyncio.run(
            _client().chat_completion(
                model="Qwen2.5-7B-Instruct-Q4",
                system_prompt="Summarize.",
                user_prompt="Very long text.",
                request_attempts=1,
            )
        )

    # The queue is walked at most once: each payload is tried a single time,
    # so no per-payload retry multiplies the load on an already-hung backend.
    assert post_calls, "the backend was never called"
    payload_keys = [
        json.dumps(call["json"], sort_keys=True, default=str) for call in post_calls
    ]
    assert len(payload_keys) == len(set(payload_keys)), "a payload was retried"


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

        def stream(self, method: str, url: str, json: dict[str, object]) -> _StubStream:
            post_calls.append({"url": url, "json": json})
            return _StubStream(content="## Topics\n- done")

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={"error": "not found"}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        _client().chat_completion(
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


def _make_stub_tokenizer(encode_ids: list[int], decode_text: str) -> MagicMock:
    enc = MagicMock()
    enc.ids = encode_ids
    tok = MagicMock()
    tok.encode.return_value = enc
    tok.decode.return_value = decode_text
    return tok


def test_tokenize_local_uses_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _make_stub_tokenizer([10, 20, 30], "hello")
    monkeypatch.setattr("vts.services.summarizer._load_tokenizer", lambda path: stub)
    result = _tokenize_local("/fake/tokenizer.json", "hello world")
    stub.encode.assert_called_once_with("hello world")
    assert result == [10, 20, 30]


def test_detokenize_local_uses_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _make_stub_tokenizer([], "hello world")
    monkeypatch.setattr("vts.services.summarizer._load_tokenizer", lambda path: stub)
    result = _detokenize_local("/fake/tokenizer.json", [10, 20, 30])
    stub.decode.assert_called_once_with([10, 20, 30])
    assert result == "hello world"


def test_llama_tokenize_uses_local_when_tokenizer_path_set(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _make_stub_tokenizer([1, 2, 3], "")
    monkeypatch.setattr("vts.services.summarizer._load_tokenizer", lambda path: stub)
    tokens = asyncio.run(
        _client().tokenize(
            model="any-model",
            text="test input",
            tokenizer_path="/fake/tokenizer.json",
        )
    )
    assert tokens == [1, 2, 3]
    stub.encode.assert_called_once_with("test input")


def test_llama_tokenize_skips_http_when_tokenizer_path_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """No HTTP calls should be made when tokenizer_path is provided."""
    stub = _make_stub_tokenizer([7], "")
    monkeypatch.setattr("vts.services.summarizer._load_tokenizer", lambda path: stub)

    http_called = []

    class StubAsyncClient:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, *a: object) -> bool:
            return False

        async def post(self, url: str, json: object) -> None:
            http_called.append(url)

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)
    asyncio.run(
        _client().tokenize(
            model="any-model",
            text="hello",
            tokenizer_path="/fake/tokenizer.json",
        )
    )
    assert http_called == []


def test_count_tokens_local(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _make_stub_tokenizer([1, 2, 3, 4, 5], "")
    monkeypatch.setattr("vts.services.summarizer._load_tokenizer", lambda path: stub)
    n = asyncio.run(
        _client().count_tokens(
            text="some text",
            model="any-model",
            tokenizer_path="/fake/tokenizer.json",
        )
    )
    assert n == 5


def test_chunk_text_local(monkeypatch: pytest.MonkeyPatch) -> None:
    # 5 tokens total, window=3, overlap_ratio=0 → 2 chunks
    all_tokens = [10, 20, 30, 40, 50]
    decode_map = {
        (10, 20, 30): "chunk one",
        (30, 40, 50): "chunk two",
    }

    enc = MagicMock()
    enc.ids = all_tokens
    tok = MagicMock()
    tok.encode.return_value = enc
    tok.decode.side_effect = lambda ids: decode_map[tuple(ids)]

    monkeypatch.setattr("vts.services.summarizer._load_tokenizer", lambda path: tok)

    chunks = asyncio.run(
        _client().chunk_text(
            text="some long text",
            model="any-model",
            window_tokens=3,
            overlap_ratio=0.0,
            tokenizer_path="/fake/tokenizer.json",
        )
    )
    assert chunks == ["chunk one", "chunk two"]


def test_derive_stream_ceiling_clamps_to_floor_and_cap() -> None:
    from vts.services.summarizer import derive_stream_ceiling

    kw = dict(
        min_tokens_per_second=3.0,
        slack=1.5,
        floor_seconds=300,
        cap_seconds=3600,
    )
    # Segment window: the middle of the range, neither bound binds.
    assert derive_stream_ceiling(1255, **kw) == pytest.approx(627.5)
    # Short user prompt: raised to the floor, so a cold start is not cut short.
    assert derive_stream_ceiling(300, **kw) == 300
    # Final summary: clamped down to the cap.
    assert derive_stream_ceiling(15000, **kw) == 3600
    # No budget given (max_tokens=None) -> the cap is the only sane answer.
    assert derive_stream_ceiling(None, **kw) == 3600


def test_parse_sse_content_extracts_only_content_deltas() -> None:
    from vts.services.summarizer import parse_sse_content

    assert parse_sse_content(
        'data: {"choices":[{"delta":{"content":"Hello"}}]}'
    ) == "Hello"
    # A role-only opening chunk carries no text.
    assert parse_sse_content('data: {"choices":[{"delta":{"role":"assistant"}}]}') is None
    # Terminator, keep-alive comment, blank line, and non-SSE noise.
    assert parse_sse_content("data: [DONE]") is None
    assert parse_sse_content(": keep-alive") is None
    assert parse_sse_content("") is None
    assert parse_sse_content("event: ping") is None
    # Malformed JSON must not raise — a truncated frame is not fatal.
    assert parse_sse_content('data: {"choices":[{"delta"') is None
    # Empty-string content is not progress and must not reset the idle timer.
    assert parse_sse_content('data: {"choices":[{"delta":{"content":""}}]}') is None


class _FakeClock:
    """Monotonic clock the test advances by hand, so no test ever sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _lines(*items: str) -> "AsyncIterator[str]":
    for item in items:
        yield item


def test_read_sse_stream_accumulates_content() -> None:
    from vts.services.summarizer import read_sse_stream

    clock = _FakeClock()
    out = asyncio.run(
        read_sse_stream(
            _lines(
                'data: {"choices":[{"delta":{"role":"assistant"}}]}',
                'data: {"choices":[{"delta":{"content":"Hel"}}]}',
                'data: {"choices":[{"delta":{"content":"lo"}}]}',
                "data: [DONE]",
            ),
            first_chunk_timeout=300,
            idle_timeout=120,
            ceiling=600,
            clock=clock,
        )
    )
    assert out == "Hello"


def test_read_sse_stream_fails_on_idle_gap() -> None:
    from vts.services.summarizer import StreamTimeout, read_sse_stream

    clock = _FakeClock()

    async def gappy() -> "AsyncIterator[str]":
        yield 'data: {"choices":[{"delta":{"content":"a"}}]}'
        clock.now += 200.0  # silence longer than idle_timeout
        yield 'data: {"choices":[{"delta":{"content":"b"}}]}'

    with pytest.raises(StreamTimeout) as excinfo:
        asyncio.run(
            read_sse_stream(
                gappy(),
                first_chunk_timeout=300,
                idle_timeout=120,
                ceiling=6000,
                clock=clock,
            )
        )
    assert excinfo.value.reason == "idle"
    assert excinfo.value.chunks == 1


def test_read_sse_stream_fails_when_first_chunk_never_arrives() -> None:
    """Keep-alive comments must not look like progress.

    A backend stuck loading the model but still emitting SSE comments (or a
    reverse proxy sending its own keep-alives) is indistinguishable from a
    dead connection unless non-content lines are barred from resetting the
    idle clock. The three-yield shape and the `elapsed` assertion below both
    exist to catch a regression where a non-content line resets `last_seen`:
    under that bug the per-line check at the third yield (200s since the
    second) would no longer see the full 400s gap and would not fire there,
    so the raise would only happen once the generator is exhausted — later,
    and for a different elapsed value than asserted here.
    """
    from vts.services.summarizer import StreamTimeout, read_sse_stream

    clock = _FakeClock()

    async def slow_start() -> "AsyncIterator[str]":
        yield ": keep-alive"
        clock.now += 200.0  # under first_chunk_timeout
        yield ": keep-alive"  # a last_seen reset here would mask the stall
        clock.now += 200.0  # 400 total > 300, but only 200 since the reset
        yield ": keep-alive"
        # Only reached if the bug above swallows the timeout at the previous
        # line: proves the raise fires there, not later during exhaustion.
        raise AssertionError("must have raised StreamTimeout before this line")

    with pytest.raises(StreamTimeout) as excinfo:
        asyncio.run(
            read_sse_stream(
                slow_start(),
                first_chunk_timeout=300,
                idle_timeout=120,
                ceiling=6000,
                clock=clock,
            )
        )
    assert excinfo.value.reason == "first_chunk"
    assert excinfo.value.chunks == 0
    assert excinfo.value.elapsed == 400.0


def test_read_sse_stream_fails_when_stream_ends_without_any_chunk() -> None:
    """An immediately-empty stream must never be reported as success.

    HTTP 200 with an empty body, an upstream that cuts the connection right
    away, or a proxy returning nothing all look the same here: the generator
    ends having yielded no content. That must raise, not return "" — an empty
    summarization silently stored as a successful result is worse than a
    loud failure.
    """
    from vts.services.summarizer import StreamTimeout, read_sse_stream

    clock = _FakeClock()

    with pytest.raises(StreamTimeout) as excinfo:
        asyncio.run(
            read_sse_stream(
                _lines("data: [DONE]"),
                first_chunk_timeout=300,
                idle_timeout=120,
                ceiling=6000,
                clock=clock,
            )
        )
    assert excinfo.value.reason == "first_chunk"
    assert excinfo.value.chunks == 0


def test_read_sse_stream_fails_when_ceiling_exceeded() -> None:
    from vts.services.summarizer import StreamTimeout, read_sse_stream

    clock = _FakeClock()

    async def steady() -> "AsyncIterator[str]":
        for _ in range(100):
            clock.now += 10.0  # each gap is under idle_timeout
            yield 'data: {"choices":[{"delta":{"content":"x"}}]}'

    with pytest.raises(StreamTimeout) as excinfo:
        asyncio.run(
            read_sse_stream(
                steady(),
                first_chunk_timeout=300,
                idle_timeout=120,
                ceiling=200,
                clock=clock,
            )
        )
    assert excinfo.value.reason == "ceiling"


def test_read_sse_stream_allows_slow_but_steady_generation() -> None:
    """The regression this whole feature exists for.

    Under the old total-duration timeout a 14-minute window was discarded
    seconds before it landed. Steady chunks must succeed no matter how long
    the whole thing takes, as long as each gap is short.
    """
    from vts.services.summarizer import read_sse_stream

    clock = _FakeClock()

    async def slow() -> "AsyncIterator[str]":
        for _ in range(60):
            clock.now += 30.0  # 30 min total, every gap well under idle_timeout
            yield 'data: {"choices":[{"delta":{"content":"y"}}]}'

    out = asyncio.run(
        read_sse_stream(
            slow(),
            first_chunk_timeout=300,
            idle_timeout=120,
            ceiling=100_000,
            clock=clock,
        )
    )
    assert out == "y" * 60


def test_stream_settings_map_from_nested_yaml() -> None:
    from vts.core.config import _normalize_yaml_overrides

    got = _normalize_yaml_overrides(
        {"services": {"llm": {"stream_idle_timeout_seconds": 90, "min_tokens_per_second": 5}}}
    )
    assert got["llm_stream_idle_timeout_seconds"] == 90
    assert got["llm_min_tokens_per_second"] == 5


def _sse_body(*contents: str) -> bytes:
    frames = [
        'data: {"choices":[{"delta":{"content":%s}}]}' % json.dumps(c) for c in contents
    ]
    frames.append("data: [DONE]")
    return ("\n".join(frames) + "\n").encode()


def test_chat_completion_streams_and_joins(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = "http://llama.local/v1/chat/completions"
    sent: list[dict[str, object]] = []

    class StubStream:
        def __init__(self, body: bytes, status: int = 200) -> None:
            self._body = body
            self.status_code = status

        async def __aenter__(self) -> "StubStream":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        @property
        def is_success(self) -> bool:
            return 200 <= self.status_code < 300

        async def aiter_lines(self) -> "AsyncIterator[str]":
            for line in self._body.decode().splitlines():
                yield line

        async def aread(self) -> bytes:
            return self._body

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        def stream(self, method: str, url: str, json: dict[str, object]):
            sent.append(json)
            return StubStream(_sse_body("Hel", "lo"))

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        _client().chat_completion(
            model="Qwen2.5-7B-Instruct-Q4",
            system_prompt="sys",
            user_prompt="user",
        )
    )
    assert raw == "Hello"
    assert sent[0]["stream"] is True


def test_chat_completion_falls_back_on_error_before_first_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 still walks the payload queue — that behaviour is load-bearing."""
    labels: list[bool] = []

    class StubStream:
        def __init__(self, has_format: bool) -> None:
            self._has_format = has_format
            self.status_code = 400 if has_format else 200

        async def __aenter__(self) -> "StubStream":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        @property
        def is_success(self) -> bool:
            return self.status_code == 200

        async def aiter_lines(self) -> "AsyncIterator[str]":
            for line in _sse_body("ok").decode().splitlines():
                yield line

        async def aread(self) -> bytes:
            return b'{"error":{"message":"Unsupported parameter: response_format"}}'

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        def stream(self, method: str, url: str, json: dict[str, object]):
            has_format = "response_format" in json
            labels.append(has_format)
            return StubStream(has_format)

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        _client().chat_completion(
            model="Qwen2.5-7B-Instruct-Q4",
            system_prompt="sys",
            user_prompt="user",
        )
    )
    assert raw == "ok"
    assert labels[0] is True and labels[-1] is False  # tried with, then without


def test_chat_completion_does_not_retry_a_stream_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vts-94wf: an identical prompt cannot succeed on attempt two."""
    attempts = 0

    class StubStream:
        def __init__(self) -> None:
            self.status_code = 200

        async def __aenter__(self) -> "StubStream":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        @property
        def is_success(self) -> bool:
            return True

        async def aiter_lines(self) -> "AsyncIterator[str]":
            raise StreamTimeout("idle", chunks=3, elapsed=130.0)
            yield ""  # pragma: no cover - unreachable, satisfies the generator

        async def aread(self) -> bytes:
            return b""

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        def stream(self, method: str, url: str, json: dict[str, object]):
            nonlocal attempts
            attempts += 1
            return StubStream()

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={}, method="GET")

    from vts.services.summarizer import StreamTimeout

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    with pytest.raises(RuntimeError):
        asyncio.run(
            _client().chat_completion(
                model="Qwen2.5-7B-Instruct-Q4",
                system_prompt="sys",
                user_prompt="user",
            )
        )
    assert attempts == 1, "a stream timeout must not be retried"


def _captured_stream_timeout(
    monkeypatch: pytest.MonkeyPatch, **kwargs: object
) -> httpx.Timeout:
    """Run one streamed completion and return the client's configured timeout."""
    seen: list[object] = []

    class StubAsyncClient:
        def __init__(self, *args: object, **client_kwargs: object) -> None:
            seen.append(client_kwargs.get("timeout"))

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        def stream(self, method: str, url: str, json: dict[str, object]) -> _StubStream:
            return _StubStream(content="ok")

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    asyncio.run(
        _client().chat_completion(
            model="Qwen2.5-7B-Instruct-Q4",
            system_prompt="sys",
            user_prompt="user",
            **kwargs,
        )
    )

    assert len(seen) == 1
    timeout = seen[0]
    assert isinstance(timeout, httpx.Timeout)
    return timeout


def test_chat_completion_sets_a_socket_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without one, a silent upstream hangs forever.

    The reader's three limits are only evaluated when a line arrives, so an
    upstream that accepts the connection and then sends nothing never trips
    any of them. Only the transport can break that deadlock.
    """
    timeout = _captured_stream_timeout(
        monkeypatch, timeout_seconds=60, stream_first_chunk_timeout=300.0
    )
    assert timeout.read is not None
    # Never below the first-chunk allowance: a 60s transport budget would
    # abort a cold model load our own limit still permits (prod load ~75s).
    assert timeout.read >= 300.0


def test_chat_completion_read_timeout_covers_a_raised_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idle limit is operator-tunable, so it must be in the floor too.

    Raising `llm_stream_idle_timeout_seconds` above the first-chunk timeout is
    reasonable for a slow CPU model. If the read timeout ignored it, the
    transport would cut the connection before our own limit fired — a bare
    ReadTimeout that also walks the whole fallback queue, instead of a clean
    StreamTimeout("idle") reporting how many chunks had arrived.
    """
    timeout = _captured_stream_timeout(
        monkeypatch,
        timeout_seconds=60,
        stream_first_chunk_timeout=300.0,
        stream_idle_timeout=600.0,
    )
    assert timeout.read is not None
    assert timeout.read >= 600.0


def test_chat_completion_read_timeout_does_not_inherit_the_request_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read spans one gap between bytes, not the whole request.

    Inheriting `timeout_seconds` multiplies it by the number of payload
    variants on a hung backend: a 1200s warmup across 8 variants would allow
    160 minutes of hanging. The read timeout covers only what it exists for —
    model load and inter-chunk pauses.
    """
    timeout = _captured_stream_timeout(
        monkeypatch,
        timeout_seconds=1200,
        stream_first_chunk_timeout=300.0,
        stream_idle_timeout=120.0,
    )
    assert timeout.read == 300.0
    # The whole-request budget still governs connect/write/pool.
    assert timeout.connect == 1200.0


def test_stream_kwargs_helper_reads_settings() -> None:
    from vts.pipeline.steps.summarization import stream_kwargs

    class _S:
        llm_stream_idle_timeout_seconds = 90
        llm_stream_first_chunk_timeout_seconds = 200
        llm_min_tokens_per_second = 5.0
        llm_ceiling_slack_multiplier = 2.0
        llm_ceiling_floor_seconds = 100
        llm_ceiling_cap_seconds = 900

    assert stream_kwargs(_S()) == {
        "stream_idle_timeout": 90.0,
        "stream_first_chunk_timeout": 200.0,
        "min_tokens_per_second": 5.0,
        "ceiling_slack": 2.0,
        "ceiling_floor_seconds": 100,
        "ceiling_cap_seconds": 900,
    }
