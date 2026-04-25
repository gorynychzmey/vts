# LLM backends

vts talks to a single OpenAI-compatible HTTP endpoint for summarization. The exact URL is
set by `services.llm.url` in `config.yaml` (or `VTS_LLM_URL`). However vts uses a few
endpoints **beyond** the OpenAI standard, which means not every backend works
out of the box.

## Endpoints vts uses

| Endpoint | Used for | OpenAI standard? |
|---|---|---|
| `POST /v1/chat/completions` | Generating each summary stage | Yes |
| `GET /v1/models` | Resolving the configured model name | Yes |
| `GET /props` | Reading the model's `n_ctx` so token budgets fit the loaded context | **No** — llama.cpp specific |
| `POST /tokenize` | Counting tokens precisely before each request | **No** — llama.cpp specific |
| `POST /detokenize` | Splitting and rejoining text on token boundaries | **No** — llama.cpp specific |

## Compatibility matrix

| Backend | `/chat/completions` | `/props` | `/tokenize` + `/detokenize` | Works as-is? |
|---|---|---|---|---|
| **llama.cpp server** | yes | yes | yes | ✅ Yes — native target |
| **Ollama** | yes (via `/v1/`) | no | no | ⚠️ Needs local tokenizer + manual `n_ctx` |
| **vLLM** | yes | no | no | ⚠️ Same as Ollama |
| **OpenAI / Anthropic** | yes | no | no | ⚠️ Same; also paid |
| **LiteLLM proxy** | yes | depends on backend | depends on backend | Depends — useful as a router |

## Option A: llama.cpp (recommended)

The default and best-tested path. One container, one `.gguf` file, no extra
configuration needed.

```bash
mkdir -p models
# Download a model, e.g. Qwen2.5-7B-Instruct (Q4_K_M ≈ 4.6GB):
#   https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF
# Place it at ./models/Qwen2.5-7B-Instruct-Q4_K_M.gguf

docker compose --profile llm-llamacpp --profile asr-whisper up -d
```

`config.yaml`:

```yaml
services:
  llm:
    url: http://llama:8000/v1
    model: Qwen2.5-7B-Instruct-Q4_K_M
```

## Option B: Ollama

Use this if you already run Ollama on the host or prefer its model management.

vts will work, but with two caveats:

1. **Token counting falls back to a local tokenizer file.** Without it, vts calls
   `/tokenize` on every request and Ollama returns 404, breaking the run. Mount a
   HuggingFace `tokenizer.json` for the model and point vts at it:

   ```yaml
   services:
     llm:
       url: http://ollama:11434/v1
       model: qwen2.5:7b-instruct
   llm_tokenizer_path: /opt/vts/tokenizers/qwen2.5/tokenizer.json
   ```

2. **`n_ctx` is not auto-detected.** vts normally reads it from `/props`. Without
   that, set it explicitly in `config.yaml`:

   ```yaml
   summary:
     n_ctx: 32768  # match what your Ollama model was launched with
   ```

After starting the stack, pull the model:

```bash
docker compose exec ollama ollama pull qwen2.5:7b-instruct
```

## Option C: LiteLLM in front of Ollama / OpenAI / etc.

If you want to point vts at hosted models (OpenAI, Anthropic, Mistral, …) or
mix-and-match, run [LiteLLM](https://github.com/BerriAI/litellm) as a proxy.
LiteLLM exposes an OpenAI-compatible API and can route to any backend. The
`/props`, `/tokenize`, `/detokenize` caveats from Option B still apply unless
your underlying backend implements them — in practice you will set
`llm_tokenizer_path` and a static `summary.n_ctx`.

LiteLLM is not bundled in the default `compose.yaml`. A minimal addition:

```yaml
  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    command: ["--config", "/app/litellm_config.yaml", "--port", "4000"]
    volumes:
      - ./litellm_config.yaml:/app/litellm_config.yaml:ro
    ports:
      - "4000:4000"
```

Then point vts at it:

```yaml
services:
  llm:
    url: http://litellm:4000/v1
    api_key: sk-anything  # litellm will accept any non-empty bearer
    model: my-routed-model
```

## Why these llama.cpp endpoints matter

vts uses **adaptive token budgeting**: every summarization stage computes how much
output to ask for as a fraction of the input token count, clamped to fit the
remaining context window. This requires:

- An exact `n_ctx` — to know the budget ceiling.
- Exact tokenization — to measure inputs without overshooting.

When these are unavailable (Ollama / OpenAI / vLLM without a side-channel),
vts falls back to a local HuggingFace tokenizer and a configured `n_ctx`. The
output quality is the same; the difference is operational — you have to keep
those two settings in sync with the actual deployed model yourself.
