"""How the thinking switch reaches the LLM backend.

Ollama's OpenAI-compatible /v1 honours only the standard `reasoning_effort`
field: measured on Ollama 0.32 with a Qwen3 model, `reasoning_effort: "none"`
returned the answer with no reasoning in 1.8 s, while `think: false` (Ollama's
native-API field) and `thinking: {"type": "disabled"}` were ignored and the
model reasoned for 30 s. The `thinking` object stays for LiteLLM routes.
"""
from vts.services.summarizer import _build_chat_payload


def _payload(thinking):
    return _build_chat_payload(
        model="m",
        system_prompt="s",
        user_prompt="u",
        max_tokens=32,
        include_response_format=False,
        temperature=0.2,
        thinking=thinking,
    )


def test_thinking_false_sends_reasoning_effort_none():
    payload = _payload(False)
    assert payload["reasoning_effort"] == "none"
    assert payload["thinking"] == {"type": "disabled"}


def test_thinking_true_sends_no_reasoning_effort():
    payload = _payload(True)
    assert "reasoning_effort" not in payload
    assert payload["thinking"] == {"type": "enabled"}


def test_thinking_unset_sends_neither_field():
    payload = _payload(None)
    assert "reasoning_effort" not in payload
    assert "thinking" not in payload
