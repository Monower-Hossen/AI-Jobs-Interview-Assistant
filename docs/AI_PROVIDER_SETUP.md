# AI provider setup

Google Gemini is the primary and default AI provider. All AI features route through the centralized
provider manager, including interview generation, answer evaluation, feedback, recorded and live
video workflows, resume analysis, dashboard AI, reports, and RAG question answering.

Whisper/STT, TTS, embeddings, and the vector database are unchanged. Gemini replaces Mistral only
for LLM operations.

## Install dependencies

```bash
venv\Scripts\python.exe -m pip install -r requirements.txt
```

The Gemini integration uses the official `google-genai` SDK. The installed SDK version is verified
by `venv\Scripts\python.exe -m pip check`.

## Configure Gemini

Copy `.env.example` to `.env` and set the required credential:

```env
AI_PROVIDER_MODE=auto
AI_PRIMARY_PROVIDER=gemini
AI_FALLBACK_ENABLED=true
LLM_PROVIDER=auto
PROVIDER_PRIORITY=gemini,groq,ollama

GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-3.5-flash
GEMINI_API_VERSION=v1beta
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
GEMINI_FALLBACK_MODELS=gemini-3.5-flash
```

`GEMINI_API_KEY` is the preferred variable. `GOOGLE_API_KEY` is accepted for backward compatibility,
but `GEMINI_API_KEY` takes precedence in the application. Keys are read from `.env` or process
environment and are never displayed in logs or the UI. The Google SDK may log a warning when both
variables are present; the application passes `GEMINI_API_KEY` explicitly to the SDK client.

If no provider is explicitly selected, the chain starts with Gemini. Mistral has been removed from
the provider registry, the fallback priority, and the provider selection UI. A stale
`AI_PRIMARY_PROVIDER=mistral` value is treated as `gemini`, and a stale `mistral` entry in
`PROVIDER_PRIORITY` is ignored.

## Select the Gemini model

Set `GEMINI_MODEL` to a model available to your Google AI key. The verified default in this project
is `gemini-3.5-flash`. `GEMINI_FALLBACK_MODELS` is used only when Gemini returns a model-not-found
error; it is not a fallback to another provider.

The SDK client uses the scheme/host from `GEMINI_BASE_URL` plus `GEMINI_API_VERSION`. Requests are
sent through `v1beta`; the `/openai` path in `GEMINI_BASE_URL` is not used by the SDK adapter.

## Verify provider connectivity

In the app:

1. Sign in as an administrator.
2. Open `Settings -> Providers`.
3. Select `Check provider health`.
4. Confirm `gemini` reports connected with the configured model.

From the command line, the same check is available without printing credentials:

```powershell
.\venv\Scripts\python.exe -c "from services.llm_service import test_provider_connection; result = test_provider_connection('gemini'); print({key: result.get(key) for key in ('healthy', 'model', 'error', 'latency_ms')})"
```

A minimal SDK request can also be made with the private `.env` credential:

```powershell
.\venv\Scripts\python.exe -c "import os; from dotenv import load_dotenv; from google import genai; from google.genai import types; load_dotenv('.env'); key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'); client = genai.Client(api_key=key, http_options=types.HttpOptions(base_url='https://generativelanguage.googleapis.com', api_version='v1beta')); response = client.models.generate_content(model=os.getenv('GEMINI_MODEL', 'gemini-3.5-flash'), contents='Reply with exactly OK', config=types.GenerateContentConfig(max_output_tokens=32)); print('MODEL_RESPONDED=', bool(response.text and response.text.strip()))"
```

Never paste an API key into a command, notebook, source file, or ticket.

## Provider modes

- `auto`: Gemini is first. Other configured providers may be used if Gemini fails, but Mistral is
  never used.
- `manual` (also `strict`, `fixed`, `single`): only `AI_PRIMARY_PROVIDER` is used; fallback is off.

```env
AI_PROVIDER_MODE=manual
AI_PRIMARY_PROVIDER=gemini
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
headers, and temporarily skips a failing provider after repeated failures. Gemini authentication
and model errors are wrapped in Gemini-specific messages.

## Common errors

- `Gemini API key not provided`: set `GEMINI_API_KEY` in `.env` or the provider settings, then
  reload the app.
- `Gemini authentication failed` or HTTP `401`/`403`: the key is invalid, disabled, or missing the
  Generative Language API. Create or enable a key in Google AI Studio.
- `404 model not found`: `GEMINI_MODEL` is not available through `v1beta` for this key. Set it to a
  model listed by your Google AI account.
- HTTP `429` or quota messages: the key is rate-limited or out of quota. Wait, reduce request volume,
  or check the Google AI quota dashboard.
- `google-genai SDK is not installed`: reinstall requirements with the project virtual environment.

## Health and UI

- Settings -> Providers shows the active provider, chain, and on-demand health checks.
- Dashboard -> AI provider status shows configured providers and circuit state.
- Health checks run small live requests; use them sparingly.

## Ollama

Start Ollama locally and pull the configured model:

```bash
ollama pull qwen3.5:4b
ollama serve
```

The app normalizes `http://localhost:11434` to the OpenAI-compatible `/v1` endpoint.
