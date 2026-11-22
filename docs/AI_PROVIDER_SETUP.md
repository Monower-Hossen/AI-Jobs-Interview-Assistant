# AI provider setup

The app uses a centralized provider manager with this default order:

1. Groq
2. Google Gemini through the official `google-genai` SDK
3. Ollama (optional local provider)
4. Mistral (optional)

## Install dependencies

```bash
venv\Scripts\python.exe -m pip install -r requirements.txt
```

The Gemini integration requires `google-genai`. If it is missing, Gemini is skipped until the
package is installed.

## Configure `.env`

Copy `.env.example` to `.env` and fill in only the providers you want to use. Never commit `.env`.

### Automatic mode

```env
AI_PROVIDER_MODE=auto
AI_PRIMARY_PROVIDER=groq
AI_FALLBACK_ENABLED=true
PROVIDER_PRIORITY=groq,gemini,ollama,mistral
GROQ_API_KEY=your-key
GEMINI_API_KEY=your-key
MISTRAL_API_KEY=your-key
ENABLE_OLLAMA=false
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3.5:4b
```

`AI_PRIMARY_PROVIDER` is tried first. If it fails, the manager routes the same prompt and options
to the next configured provider.

### Manual mode

```env
AI_PROVIDER_MODE=manual
AI_PRIMARY_PROVIDER=groq
AI_FALLBACK_ENABLED=false
```

Manual mode disables fallback and uses only the selected provider.

## Provider keys

- Groq: `GROQ_API_KEY`
- Gemini: `GEMINI_API_KEY` or `GOOGLE_API_KEY`
- Mistral: `MISTRAL_API_KEY`
- Ollama: no key required, but set `ENABLE_OLLAMA=true`

Keys are kept in environment variables or process memory. The app never displays stored keys.

### Gemini model and endpoint

The installed `google-genai` SDK defaults to the `v1` API, which can return `404 Not Found` for
models exposed through `v1beta`. This app explicitly selects `v1beta`. If the configured model still
returns `404 Not Found`, set a model available to your API key:

```env
GEMINI_MODEL=gemini-2.5-flash
GEMINI_FALLBACK_MODELS=gemini-2.5-flash
GEMINI_API_VERSION=v1beta
```

Model availability depends on the API key and endpoint; replace the example with an available model
when the provider returns `404 Not Found`.

`GEMINI_BASE_URL` is the OpenAI-compatible endpoint used by configuration helpers. The SDK client
uses its scheme/host plus the API version above; it does not send requests to the `/openai` path.
Model fallbacks are attempted only for model-not-found errors.

## Request policy

```env
AI_REQUEST_TIMEOUT=60
AI_MAX_RETRIES=2
PROVIDER_CIRCUIT_BREAKER_THRESHOLD=3
PROVIDER_CIRCUIT_BREAKER_COOLDOWN=60
```

The manager retries transient transport errors with exponential backoff, honors retry-after
headers where available, and temporarily skips failing providers after repeated failures.

## Health and UI

- Settings → Providers shows provider controls and an on-demand health check.
- Dashboard → AI provider status shows configured providers, the active chain, and circuit state.
- Health checks run small live requests; use them sparingly.

## Ollama

Start Ollama locally and pull the configured model:

```bash
ollama pull qwen3.5:4b
ollama serve
```

The app normalizes `http://localhost:11434` to the OpenAI-compatible `/v1` endpoint.
