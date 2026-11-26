# Resource admission and leases

## Design goal

Phase 3.3.4 replaces scattered heavy-task counters with one process-wide admission and lease
manager in `backend/services/resource_monitor.py`. Every admission decision is atomic, every
accepted operation receives a unique lease, and stale leases can be inspected or recovered without
touching another live session.

The admission layer keeps the Python + Streamlit deployment unchanged. `psutil` remains optional,
no model downloads are triggered, and all defaults stay CPU-first for the 8 GB target machine.

## Request flow

```text
Caller
    -> acquire_resource_lease(task, owner_id, worker=...)
    -> collect/cached ResourceSnapshot
    -> atomic admission under _STATE_LOCK
    -> ResourceLease
    -> heartbeat / worker probe
    -> release_resource_lease(...)
```

`acquire_heavy_task()` remains the context-manager wrapper for short operations. It delegates to the
same manager and releases on normal exit and exceptions. Long-lived recorders use the explicit lease
API because their worker owns the lifetime.

## Admission rules

A request is admitted only when all enabled checks pass:

- guardrails are enabled;
- task name is registered;
- the same owner does not already hold an active lease for that task;
- task-specific budget has a free slot;
- global active-lease limit has a free slot;
- RAM is known when guardrails are enabled;
- CPU usage is at or below the configured threshold;
- the task's memory tier has enough free RAM.

Rejections are counted by reason code: `unknown_task`, `invalid_owner`, `task_busy`,
`task_excluded`, `workload_limit`, `cpu_high`, `memory_unknown`, and `memory_low`. Owner identifiers
are hashed in diagnostics; raw owner values are never stored in snapshots or logs.

## Health thresholds

Resource data comes from `ResourceSnapshot` with a short in-process cache. The cache prevents
repeated subprocess or system calls during a Streamlit rerun. If monitoring raises, the manager
returns a conservative `memory_unknown` rejection instead of crashing the page.

Relevant environment settings:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `RESOURCE_GUARDRAILS_ENABLED` | `true` | Enables admission enforcement. |
| `RESOURCE_MONITOR_ENABLED` | `true` | Enables resource sampling. |
| `RESOURCE_MONITOR_CACHE_SECONDS` | phase 1 default | Snapshot cache lifetime. |
| `RESOURCE_LOW_AVAILABLE_MB` | phase 1 default | Heavy-work floor in low-memory mode. |
| `RESOURCE_CRITICAL_AVAILABLE_MB` | phase 1 default | Heavy local ML floor. |
| `EMBEDDING_BATCH_SIZE` | `4` | Local document embedding batch size, capped to 2 in low-memory mode. |
| `EMBEDDING_DIMENSION` | `384` | Expected vector dimension used before a model is loaded. |
| `RESOURCE_LIGHTWEIGHT_AVAILABLE_MB` | `256` | Floor for low-footprint tasks such as cloud LLM calls and audio analysis. |
| `RESOURCE_ALLOW_CONCURRENT_HEAVY_ML` | `false` | Preserves legacy ML exclusion behavior. |
| `RESOURCE_LOW_MEMORY_MODE` | `false` | Applies the restrictive profile. |
| `RESOURCE_LEASE_HEARTBEAT_SECONDS` | `300` | Default lease heartbeat TTL, clamped to 30-3600 seconds. |
| `RESOURCE_MAX_ACTIVE_LEASES` | `4` | Global process-wide lease cap, clamped to 1-8. |
| `RESOURCE_CPU_ADMISSION_THRESHOLD_PERCENT` | `85.0` | CPU admission ceiling, clamped to 10-100. |
| `LIVE_MEDIA_HEARTBEAT_SECONDS` | `15.0` | Browser media heartbeat interval, clamped to 5-60 seconds. |
| `LIVE_MEDIA_STALE_SECONDS` | `45.0` | Media-session idle timeout, clamped to at least twice the heartbeat. |

Task budgets are clamped to 1-8:

| Task | Default limit | Representative caller |
| --- | ---: | --- |
| `whisper_transcription` | 1 | `backend/services/speech_to_text.py` |
| `video_processing` | 1 | `backend/services/live_interview_media_service.py` |
| `embedding_generation` | 1 | `backend/rag/embeddings.py` |
| `live_recording` | 1 | reserved for recording lifecycle |
| `llm_inference` | 4 | reserved for future admission |
| `video_analysis` | 1 | reserved for future admission |
| `audio_analysis` | 2 | reserved for future admission |
| `webrtc_session` | 1 | `backend/services/live_call_media_service.py` |

Low-memory mode caps the global lease count at 2, task budgets at 1, CPU threshold at 70%,
embedding batch size at 2, and keeps the existing tiny Whisper model override.

Memory admission is tiered. `llm_inference`, `audio_analysis`, and browser-only `webrtc_session`
use the lightweight floor,
`whisper_transcription`/`video_processing`/`embedding_generation`/`video_analysis` use the critical
floor, and heavy local work uses the low-memory floor when low-memory mode is enabled. A memory-based
rejection is retried once with a freshly collected snapshot so a stale cache cannot block work after
RAM recovers. Rejections carry the measured RAM, the required floor, and whether in-app work is
active; local ML work is never admitted below its safety floor.

Live Call uses `webrtc_session` because the browser owns `getUserMedia()` and `RTCPeerConnection`;
the server only authenticates signaling and holds bounded session state. Actual server-side video
work continues to use `video_processing` and its critical-memory floor.

## Lease lifecycle

Each lease carries a unique ID, task, owner ID, monotonic creation time, heartbeat time, expiry,
status, release status, and optional weak reference to a worker.

- `acquire_resource_lease()` reserves the slot atomically.
- `heartbeat_resource_lease()` extends expiry for the owning caller.
- `inspect_stale_leases()` reports expired leases without mutating state; `task=` restricts the scan.
- `recover_stale_leases()` releases only expired leases that are safe to release; `task=` restricts
  the scan to one workload.
- `release_resource_lease()` is idempotent and validates owner identity.
- `resource_lease()` provides exception-safe `with` semantics for short tasks.

A lease released as `rolled_back` records startup failure. A worker-aware release checks
`is_alive()` when `worker_stopped` is omitted, so a recorder cannot be released while its worker is
still running. If the worker probe itself fails, the lease is preserved as `unknown` instead of
being forcibly reclaimed.

## Stale recovery

Stale recovery runs automatically before admission checks and admin diagnostics, in addition to the
explicit `recover_stale_leases()` API. Live Call additionally reconciles idle media sessions before
each `webrtc_session` attachment, so a closed or abandoned invitation page cannot keep the bounded
slot reserved until the generic 300-second lease TTL expires. Recovery is deliberately conservative:

1. Expired leases with no worker, a stopped worker, or `worker_stopped=True` are recoverable.
2. Expired leases with an alive or unprobeable worker are preserved.
3. Dry runs never mutate state.
4. Recovery is idempotent; a second call returns zero recovered leases.
5. Releasing with the wrong owner ID is rejected.
6. Recovery never resets unrelated session, auth, or recorder state.
7. A media session is retained when any participant role has heartbeated within the media idle timeout.
8. Task-scoped recovery leaves leases for other workloads untouched.
9. A browser `pagehide` queues a `presence_end` signal that releases only the media lease, without
   marking the participant as left in the database. Missed browser cleanup still falls back to the
   media idle timeout.

This keeps one failed Streamlit rerun from freeing a recorder that another live session still owns,
and keeps a refresh or navigation from permanently locking the single WebRTC slot.

## Integrations

- Whisper conversion, model load, transcribe, and lazy segment consumption stay inside one lease.
- Embedding singleton construction stays inside one lease.
- Webcam recording acquires an owner-bound `video_processing` lease in `set_enabled()`, heartbeats
  during `enqueue()`, and releases after worker shutdown.
- Recorder startup failures roll the lease back.
- Admin settings exposes a read-only Resource Admission panel with refreshable diagnostics.

## Diagnostics

`get_resource_admission_diagnostics()` reports active lease counts, rejected admissions by reason,
expired and recovered counts, monitoring availability, resource pressure, monitor errors, a cached
resource snapshot, and redacted lease summaries. `media_slot_diagnostics()` narrows this to the
`webrtc_session` slot and reports active room IDs, pseudonymous owner IDs, lease IDs, heartbeat age,
and expiry ages. The Streamlit admin UI presents these as metrics and tables; no owner IDs, invitation
tokens, or credentials are exposed.

## Tests

`tests/test_phase334_resource_admission.py` covers atomic final-slot admission, concurrent unique
lease IDs, rejection codes, monitoring failures, cache behavior, settings clamps, heartbeat and
duplicate release, wrong-owner release, duplicate-owner rejection, stale dry runs, automatic and
explicit recovery, worker-aware release, session scoping, tiered memory admission, fresh-snapshot
retry, task-scoped recovery, typed memory errors, WebRTC classification, redacted diagnostics,
Whisper and embedding integration, recorder lease ownership, and Streamlit rerun safety.

`tests/test_invitation_acceptance_sync_regression.py` covers stale WebRTC slot recovery, active
session retention, valid-cleanup replacement, wrong-owner release protection, `presence_end`
outbox handling, cancellation/end lifecycle release, and redacted media diagnostics.
`tests/test_stage4_webrtc_media.py` covers browser `pagehide` cleanup and the bounded slot error
message shown to candidates.

Webcam/recording batches continue to use the low-memory overrides:

```powershell
$env:RESOURCE_LOW_AVAILABLE_MB="256"
$env:RESOURCE_CRITICAL_AVAILABLE_MB="128"
```

Do not apply those overrides to phase 1 guardrail tests.

## Limitations

- The manager is process-wide; separate Streamlit worker processes do not share lease state.
- Lease TTLs are safety nets, not proof of process death; alive workers are preserved.
- GPU VRAM is reported for diagnostics but does not grant extra admission capacity.
- LLM admission uses the lightweight memory tier; provider routing stays Groq-first, Gemini-second,
  and Ollama is skipped unless explicitly enabled.
- No new dependency is introduced; monitoring degrades conservatively when unavailable.
