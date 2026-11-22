# AI provider setup

Groq is the primary and default AI provider. All AI features route through the centralized
provider manager, including interview generation, answer evaluation, feedback, recorded and live
video workflows, resume analysis, dashboard AI, reports, and RAG question answering.

Whisper/STT, TTS, embeddings, and the vector database are unchanged from the local-first design.

## Install dependencies

```bash
venv\Scripts\python.exe -m pip install -r requirements.txt
```

The Gemini integration uses the official `google-genai` SDK. The installed SDK version is verified
by `venv\Scripts\python.exe -m pip check`.

## Configure providers

Copy `.env.example` to `.env` and set the credentials you plan to use:

```env
AI_PROVIDER_MODE=auto
AI_PRIMARY_PROVIDER=groq
AI_FALLBACK_ENABLED=true
AI_PROVIDER_PRIORITY=groq,gemini,ollama
PROVIDER_PRIORITY=groq,gemini,ollama

GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=your_supported_groq_model

GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.5-flash

OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5-coder:3b
ENABLE_OLLAMA=false
```

`AI_PROVIDER_PRIORITY` is preferred. `PROVIDER_PRIORITY` remains supported for existing
installations; when both are present, the former wins. `GOOGLE_API_KEY` is accepted for Gemini as a
backward-compatible alias, but `GEMINI_API_KEY` takes precedence. Keys are read from `.env` or the
process environment and are never displayed in logs or the UI.

The default chain is `Groq -> Gemini -> Ollama`. Ollama is only used when `ENABLE_OLLAMA=true` or
when it is explicitly selected as the primary provider.

## Select models

Set `GROQ_MODEL` to a model available to your Groq key, and `GEMINI_MODEL` to a model available to
your Google AI key. The verified Gemini default in this project is `gemini-3.5-flash`.

`GROQ_FALLBACK_MODELS` and `GEMINI_FALLBACK_MODELS` are used only when the provider reports a
model-not-found error; they are not fallbacks to another provider.

## Verify provider connectivity

In the app:

1. Sign in as an administrator.
2. Open `Settings -> Providers`.
3. Select `Check provider health`.
4. Confirm `Groq` reports `Connected` with the configured model.

From the command line, the same check is available without printing credentials:

```powershell
.\venv\Scripts\python.exe -c "from services.llm_service import test_provider_connection; result = test_provider_connection('groq'); print({key: result.get(key) for key in ('healthy', 'status', 'model', 'model_available', 'error', 'latency_ms')})"
```

Health checks are lightweight metadata/list requests with timeouts. They never generate answer
tokens, and they report `Connected`, `Not configured`, `Authentication failed`,
`Connection failed`, `Service unavailable`, or `Disabled`.

Never paste an API key into a command, notebook, source file, or ticket.

## Provider modes

- `auto`: Groq is first. Other configured providers may be used if Groq fails.
- `manual` (also `strict`, `fixed`, `single`): only `AI_PRIMARY_PROVIDER` is used; fallback is off.

```env
AI_PROVIDER_MODE=manual
AI_PRIMARY_PROVIDER=groq
AI_FALLBACK_ENABLED=false
```

## Request policy

```env
AI_REQUEST_TIMEOUT=60
AI_MAX_RETRIES=2
PROVIDER_CIRCUIT_BREAKER_THRESHOLD=3
PROVIDER_CIRCUIT_BREAKER_COOLDOWN=60
```

The manager retries transient transport errors with bounded exponential backoff, honors retry-after
headers, applies per-provider cooldowns, opens circuit breakers after repeated failures, and routes
the unchanged prompt and options to the next eligible provider.
