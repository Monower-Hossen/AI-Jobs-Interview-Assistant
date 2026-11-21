# AI fallback architecture

## Design goal

All AI features route through one provider manager. Feature modules call the existing
`get_llm_service()` façade, so resume analysis, job matching, interview generation, answer
evaluation, reports, roadmaps, behavioral assessment, technical assessment, and RAG Q&A share the
same routing policy.

## Request flow

```text
Service / UI
    -> get_llm_service()
    -> LLMService.generate() or generate_structured()
    -> ProviderManager
    -> provider chain (Groq -> Gemini -> Ollama -> Mistral)
    -> LLMResponse / validated Pydantic model
```

`ProviderManager` owns:

- provider registration
- chain construction from environment settings
- request routing and fallback
- retry and timeout policy
- rate-limit cooldowns
- circuit-breaker state
- health checks
- non-secret status reporting

## Provider adapters

- `GroqProvider`, `MistralProvider`, `OllamaProvider`: OpenAI-compatible HTTP transport via httpx.
- `GeminiProvider`: official `google-genai` SDK using `client.models.generate_content`.
- `LLMProvider` is the common interface: `generate`, `generate_structured`, and `get_model_name`.

The Gemini adapter maps `max_tokens` to `max_output_tokens` and `json_mode` to
`response_mime_type="application/json"`.

## Routing policy

`AI_PROVIDER_MODE=auto` builds a chain from `AI_PRIMARY_PROVIDER` (or the legacy alias
`LLM_PROVIDER`) followed by the deduplicated entries in `PROVIDER_PRIORITY`. The effective fallback
flag is `AI_FALLBACK_ENABLED` combined with the mode: `manual`, `strict`, `fixed`, and `single`
disable fallback even if the raw flag is true.

Ollama is registered but skipped unless `ENABLE_OLLAMA=true` or it is explicitly selected.

## Failure handling

1. The active provider receives the request.
2. Transport errors are retried with bounded exponential backoff.
3. Rate-limit responses set a per-provider cooldown.
4. Repeated failures open a circuit for `PROVIDER_CIRCUIT_BREAKER_COOLDOWN` seconds.
5. The manager routes the unchanged prompt and options to the next eligible provider.
6. If no provider succeeds, a single aggregated `RuntimeError` is raised.

Errors are sanitized before logging. API keys are redacted and never included in logs or UI.

## Prompt and RAG preservation

The manager does not rewrite prompts. Structured calls still append the exact JSON-only suffix and
retry prefixes from the previous implementation, and local JSON extraction/Pydantic validation is
unchanged.

RAG context formatting remains in `backend/services/knowledge_base.py`:

- context blocks use `[Source: name]` plus document text
- blocks are joined with `\n\n---\n\n`
- `MAX_CONTEXT_CHARS` and `MAX_RAG_CHUNKS` still bound the context
- interview generation truncates knowledge context to its existing 4000-character limit

Switching LLM providers does not touch the embedding model or vector index.

## Health and observability

- `check_provider_health()` runs a small live request against each configured provider.
- `get_provider_status()` returns circuit and routing state without making network calls.
- Logs use stable event names such as `provider.request`, `provider.failure`, and
  `provider.circuit_open`.

## Reset behavior

Changing provider settings calls `reset_llm_service()`, which clears the LLM singleton, provider
manager, request pacing state, rate-limit cooldowns, and circuit-breaker state.
