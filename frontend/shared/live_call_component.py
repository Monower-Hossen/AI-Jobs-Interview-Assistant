from __future__ import annotations

from typing import Any

import streamlit as st
from services import live_call_service as calls
from services.live_interview_coordination_service import leave_call, send_signal

_HTML = """
<div data-live-call-root class="live-call-root">
  <div class="live-call-head">
    <span data-status class="live-call-status" data-tone="warn">Ready to join</span>
    <span class="live-call-label">Real browser WebRTC · direct media</span>
  </div>
  <div class="live-call-controls">
    <button data-action="join" type="button">Join call</button>
    <button data-action="leave" type="button" disabled>Leave</button>
    <button data-action="camera" type="button" disabled>Turn camera off</button>
    <button data-action="mic" type="button" disabled>Mute microphone</button>
  </div>
  <div class="live-call-grid">
    <div class="live-call-tile">
      <span>Local preview</span>
      <video data-local autoplay muted playsinline></video>
    </div>
    <div class="live-call-tile">
      <span>Remote participant</span>
      <video data-remote autoplay playsinline></video>
    </div>
  </div>
  <div data-message class="live-call-message">Recording is not enabled for live calls.</div>
</div>
"""

_CSS = """
.live-call-root {
  display: flex;
  flex-direction: column;
  gap: 0.65rem;
  padding: 0.85rem;
  border: 1px solid var(--st-border-color, rgba(128, 128, 128, 0.35));
  border-radius: 0.75rem;
  background: var(--st-secondary-background-color, rgba(128, 128, 128, 0.08));
  color: var(--st-text-color, inherit);
}
.live-call-head { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; justify-content: space-between; }
.live-call-status { font-weight: 650; padding: 0.2rem 0.55rem; border-radius: 999px; font-size: 0.85rem; background: rgba(255, 180, 0, 0.18); }
.live-call-status[data-tone="ok"] { background: rgba(40, 190, 100, 0.2); }
.live-call-status[data-tone="bad"] { background: rgba(230, 70, 70, 0.22); }
.live-call-label { font-size: 0.8rem; opacity: 0.8; }
.live-call-controls { display: flex; flex-wrap: wrap; gap: 0.5rem; }
.live-call-controls button {
  border: 1px solid var(--st-primary-color, #377eb8);
  border-radius: 0.45rem;
  background: var(--st-primary-color, #377eb8);
  color: var(--st-text-color, white);
  padding: 0.35rem 0.7rem;
  cursor: pointer;
}
.live-call-controls button:disabled { opacity: 0.45; cursor: not-allowed; }
.live-call-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.6rem; }
.live-call-tile { display: flex; flex-direction: column; gap: 0.3rem; font-size: 0.8rem; }
.live-call-tile video { width: 100%; aspect-ratio: 4 / 3; background: #000; border-radius: 0.5rem; object-fit: cover; }
.live-call-message { font-size: 0.8rem; opacity: 0.75; }
"""

_JS = """
const liveCallStates = new WeakMap();

export default function (component) {
  const { data, parentElement, setStateValue } = component;
  const root = parentElement.querySelector("[data-live-call-root]");
  if (!root) {
    return () => {};
  }

  let cfg = data || {};
  let state = liveCallStates.get(parentElement);
  if (!state) {
    state = {
      pc: null,
      localStream: null,
      remoteStream: null,
      started: false,
      joining: false,
      cameraEnabled: true,
      micEnabled: true,
      processedIds: new Set(),
      pendingSignals: [],
      pendingCandidates: [],
      reconnectAttempts: 0,
      reconnecting: false,
      attached: false,
      lastAck: -1,
      outbox: [],
      config: cfg
    };
    liveCallStates.set(parentElement, state);
  }
  cfg = data || {};
  state.config = cfg;

  const statusEl = root.querySelector("[data-status]");
  const joinButton = root.querySelector("[data-action='join']");
  const leaveButton = root.querySelector("[data-action='leave']");
  const cameraButton = root.querySelector("[data-action='camera']");
  const micButton = root.querySelector("[data-action='mic']");
  const localVideo = root.querySelector("[data-local]");
  const remoteVideo = root.querySelector("[data-remote]");
  const messageEl = root.querySelector("[data-message]");
  if (!statusEl || !joinButton || !leaveButton || !cameraButton || !micButton || !localVideo || !remoteVideo) {
    return () => {};
  }

  const ack = Number(cfg.outboxAck || 0);
  if (state.lastAck < 0) {
    state.lastAck = ack;
  } else if (ack !== state.lastAck) {
    state.lastAck = ack;
    state.outbox = [];
  }

  function setStatus(text, tone) {
    statusEl.textContent = text;
    statusEl.dataset.tone = tone;
  }

  function updateButtons() {
    const enabled = Boolean(cfg.enabled);
    joinButton.disabled = !enabled || state.started;
    leaveButton.disabled = !state.started;
    cameraButton.disabled = !state.started;
    micButton.disabled = !state.started;
    cameraButton.textContent = state.cameraEnabled ? "Turn camera off" : "Turn camera on";
    micButton.textContent = state.micEnabled ? "Mute microphone" : "Unmute microphone";
  }

  function beginJoin() {
    if (state.joining || state.started) {
      return false;
    }
    state.joining = true;
    return true;
  }

  function emitSignal(kind, payload) {
    state.outbox = [...state.outbox, { kind: kind, payload: payload }];
    setStateValue("outbox", state.outbox);
  }

  function markProcessed(id) {
    const numericId = Number(id);
    if (!Number.isFinite(numericId) || state.processedIds.has(numericId)) {
      return false;
    }
    state.processedIds.add(numericId);
    if (state.processedIds.size > 300) {
      const oldest = state.processedIds.values().next().value;
      state.processedIds.delete(oldest);
    }
    return true;
  }

  function candidateInit(payload) {
    const init = { candidate: String(payload.candidate || "") };
    if (typeof payload.sdp_mid === "string") {
      init.sdpMid = payload.sdp_mid;
    }
    if (Number.isInteger(payload.sdp_mline_index)) {
      init.sdpMLineIndex = payload.sdp_mline_index;
    }
    return init;
  }

  async function applyPendingCandidates() {
    const pc = state.pc;
    if (!pc || !pc.remoteDescription) {
      return;
    }
    const pending = state.pendingCandidates.splice(0);
    for (const init of pending) {
      try {
        await pc.addIceCandidate(new RTCIceCandidate(init));
      } catch (error) {
        setStatus("A remote ICE candidate could not be applied.", "bad");
      }
    }
  }

  function ensurePeerConnection() {
    if (state.pc && state.pc.signalingState !== "closed") {
      return state.pc;
    }
    const pc = new RTCPeerConnection({
      iceServers: Array.isArray(cfg.iceServers) ? cfg.iceServers : [],
      iceTransportPolicy: cfg.iceTransportPolicy || "all"
    });
    state.pc = pc;

    pc.onicecandidate = (event) => {
      if (event.candidate && event.candidate.candidate) {
        const payload = { candidate: String(event.candidate.candidate) };
        if (typeof event.candidate.sdpMid === "string") {
          payload.sdp_mid = event.candidate.sdpMid;
        }
        if (Number.isInteger(event.candidate.sdpMLineIndex)) {
          payload.sdp_mline_index = event.candidate.sdpMLineIndex;
        }
        emitSignal("ice_candidate", payload);
      } else {
        emitSignal("ice_gathering_complete", {});
      }
    };

    pc.ontrack = (event) => {
      if (!state.remoteStream) {
        state.remoteStream = new MediaStream();
      }
      const incoming = event.streams && event.streams[0] ? event.streams[0] : null;
      const track = incoming
        ? incoming.getTracks().find((item) => item.id === event.track.id) || event.track
        : event.track;
      if (!state.remoteStream.getTracks().some((item) => item.id === track.id)) {
        state.remoteStream.addTrack(track);
      }
      remoteVideo.srcObject = state.remoteStream;
      remoteVideo.play().catch(() => {});
    };

    pc.onconnectionstatechange = () => {
      const value = pc.connectionState;
      if (value === "connected") {
        setStatus("Connection: connected", "ok");
      } else if (value === "failed" || value === "disconnected" || value === "closed") {
        setStatus("Connection: " + value, "bad");
        if (value === "failed") {
          scheduleReconnect(false);
        }
      } else {
        setStatus("Connection: " + value, "warn");
      }
    };

    pc.oniceconnectionstatechange = () => {
      const value = pc.iceConnectionState;
      if (value === "failed") {
        setStatus("ICE connection: failed", "bad");
        scheduleReconnect(false);
      } else if (value === "connected" || value === "completed") {
        setStatus("ICE connection: " + value, "ok");
      }
    };
    return pc;
  }

  async function createOffer(restart) {
    const pc = ensurePeerConnection();
    if (pc.signalingState !== "stable") {
      return;
    }
    const offer = restart ? await pc.createOffer({ iceRestart: true }) : await pc.createOffer();
    await pc.setLocalDescription(offer);
    emitSignal("offer", { type: "offer", sdp: pc.localDescription.sdp });
  }

  function scheduleReconnect(force) {
    const maxAttempts = Math.max(0, Number(cfg.maxReconnectAttempts || 0));
    if (state.reconnecting || state.reconnectAttempts >= maxAttempts) {
      if (state.reconnectAttempts >= maxAttempts) {
        setStatus("Connection failed after the retry limit.", "bad");
      }
      return;
    }
    state.reconnecting = true;
    state.reconnectAttempts += 1;
    const attempt = state.reconnectAttempts;
    setStatus("Reconnecting " + attempt + "/" + maxAttempts + "...", "warn");
    const base = Math.max(0.5, Number(cfg.reconnectBaseDelaySeconds || 2));
    const delay = Math.min(30, base * Math.pow(2, attempt - 1)) * 1000;
    setTimeout(async () => {
      try {
        if (cfg.initiator || force) {
          await createOffer(true);
        } else {
          emitSignal("hangup", { reason: "ice-restart-request" });
        }
      } catch (error) {
        setStatus("Reconnect could not be started.", "bad");
      }
      state.reconnecting = false;
    }, delay);
  }

  function attachLocalTracks() {
    if (!state.localStream || state.attached || !state.pc) {
      return;
    }
    state.localStream.getTracks().forEach((track) => state.pc.addTrack(track, state.localStream));
    state.attached = true;
  }

  async function startLocalMedia(sendOffer, attachTracks) {
    if (state.started) {
      return false;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus("Browser camera and microphone capture is unavailable.", "bad");
      return false;
    }
    const video = cfg.video || {};
    const audio = cfg.audio || {};
    const stream = await navigator.mediaDevices.getUserMedia({
      video: {
        width: { ideal: Number(video.width || 640) },
        height: { ideal: Number(video.height || 480) },
        frameRate: { ideal: Number(video.frameRate || 30) }
      },
      audio: {
        echoCancellation: Boolean(audio.echoCancellation),
        noiseSuppression: Boolean(audio.noiseSuppression),
        autoGainControl: Boolean(audio.autoGainControl)
      }
    });
    state.localStream = stream;
    localVideo.srcObject = stream;
    if (attachTracks) {
      attachLocalTracks();
    }
    localVideo.play().catch(() => {});
    state.started = true;
    state.cameraEnabled = true;
    state.micEnabled = true;
    setStatus("Local media active; waiting for peer connection.", "warn");
    updateButtons();
    if (sendOffer && cfg.initiator) {
      await createOffer(false);
    }
    const queued = state.pendingSignals.splice(0);
    for (const signal of queued) {
      await handleSignal(signal);
    }
    return true;
  }

  async function handleSignal(signal) {
    const payload = signal.payload || {};
    if (signal.signal_type === "offer") {
      if (cfg.initiator) {
        return;
      }
      if (!state.started) {
        state.pendingSignals.push(signal);
        return;
      }
      const pc = ensurePeerConnection();
      await pc.setRemoteDescription({ type: "offer", sdp: String(payload.sdp || "") });
      attachLocalTracks();
      await applyPendingCandidates();
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      emitSignal("answer", { type: "answer", sdp: pc.localDescription.sdp });
    } else if (signal.signal_type === "answer") {
      if (!cfg.initiator) {
        return;
      }
      const pc = ensurePeerConnection();
      await pc.setRemoteDescription({ type: "answer", sdp: String(payload.sdp || "") });
      await applyPendingCandidates();
    } else if (signal.signal_type === "ice_candidate") {
      const init = candidateInit(payload);
      const pc = ensurePeerConnection();
      if (pc.remoteDescription) {
        try {
          await pc.addIceCandidate(new RTCIceCandidate(init));
        } catch (error) {
          setStatus("A remote ICE candidate could not be applied.", "bad");
        }
      } else {
        state.pendingCandidates.push(init);
      }
    } else if (signal.signal_type === "hangup") {
      const reason = String(payload.reason || "");
      if (reason === "ice-restart-request") {
        if (cfg.initiator) {
          scheduleReconnect(true);
        }
        return;
      }
      setStatus("The remote participant ended the call.", "warn");
      cleanup(false);
      updateButtons();
    }
  }

  function cleanup(emitHangup) {
    if (emitHangup && state.started) {
      emitSignal("hangup", { reason: "user-left" });
    }
    if (state.pc) {
      state.pc.onicecandidate = null;
      state.pc.ontrack = null;
      state.pc.onconnectionstatechange = null;
      state.pc.oniceconnectionstatechange = null;
      try {
        state.pc.close();
      } catch (error) {
        setStatus("The peer connection could not be closed cleanly.", "bad");
      }
      state.pc = null;
    }
    if (state.localStream) {
      state.localStream.getTracks().forEach((track) => track.stop());
      state.localStream = null;
    }
    if (state.remoteStream) {
      state.remoteStream.getTracks().forEach((track) => track.stop());
      state.remoteStream = null;
    }
    localVideo.srcObject = null;
    remoteVideo.srcObject = null;
    state.started = false;
    state.attached = false;
    state.cameraEnabled = true;
    state.micEnabled = true;
    state.pendingCandidates = [];
    state.pendingSignals = [];
    state.reconnecting = false;
  }

  if (!root.dataset.bound) {
    root.dataset.bound = "1";
    joinButton.addEventListener("click", async () => {
      if (!beginJoin()) {
        return;
      }
      try {
        await startLocalMedia(true, true);
      } catch (error) {
        cleanup(false);
        setStatus("Camera or microphone access was denied or failed.", "bad");
        updateButtons();
      } finally {
        state.joining = false;
      }
    });
    leaveButton.addEventListener("click", () => {
      emitSignal("leave", {});
      cleanup(false);
      setStatus("You left the call.", "warn");
      updateButtons();
    });
    cameraButton.addEventListener("click", () => {
      state.cameraEnabled = !state.cameraEnabled;
      if (state.localStream) {
        state.localStream.getVideoTracks().forEach((track) => {
          track.enabled = state.cameraEnabled;
        });
      }
      updateButtons();
    });
    micButton.addEventListener("click", () => {
      state.micEnabled = !state.micEnabled;
      if (state.localStream) {
        state.localStream.getAudioTracks().forEach((track) => {
          track.enabled = state.micEnabled;
        });
      }
      updateButtons();
    });
  }

  if (!cfg.enabled && state.started) {
    cleanup(false);
    setStatus("Call is no longer available.", "warn");
  }
  if (!state.started) {
    setStatus(cfg.enabled ? "Ready to join" : "Call unavailable", cfg.enabled ? "warn" : "bad");
  }
  updateButtons();

  const incoming = Array.isArray(cfg.incoming) ? cfg.incoming : [];
  for (const signal of incoming) {
    if (!signal || !markProcessed(signal.id)) {
      continue;
    }
    if (!state.started) {
      state.pendingSignals.push(signal);
    } else {
      handleSignal(signal);
    }
  }

  if (messageEl) {
    messageEl.textContent = "Recording is not enabled for live calls. Connection status comes from the browser peer connection.";
  }

  return () => {
    if (state.pc) {
      try {
        state.pc.close();
      } catch (error) {
        setStatus("The peer connection could not be closed cleanly.", "bad");
      }
      state.pc = null;
    }
    if (state.localStream) {
      state.localStream.getTracks().forEach((track) => track.stop());
      state.localStream = null;
    }
    if (state.remoteStream) {
      state.remoteStream.getTracks().forEach((track) => track.stop());
      state.remoteStream = null;
    }
  };
}
"""

_LIVE_CALL_COMPONENT = st.components.v2.component(
    "live_interview_rtc",
    html=_HTML,
    css=_CSS,
    js=_JS,
)


def _state_key(room_id: str) -> str:
    return f"_live_call_rtc_{room_id}"


def _error_key(room_id: str) -> str:
    return f"_live_call_rtc_error_{room_id}"


def _enqueue_outbox(
    *,
    room_id: str,
    role: str,
    recipient_role: str,
    user_id: int,
    session_token: str,
) -> None:
    key = _state_key(room_id)
    raw_state = st.session_state.get(key) or {}
    events = list(raw_state.get("outbox") or [])
    if not events:
        return
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "")
        payload = event.get("payload")
        if kind == "leave":
            try:
                leave_call(
                    room_id=room_id,
                    user_id=user_id,
                    session_token=session_token,
                )
            except calls.LiveCallError as exc:
                st.session_state[_error_key(room_id)] = str(exc)[:200]
            except Exception as exc:
                st.session_state[_error_key(room_id)] = f"Leave could not be completed: {type(exc).__name__}"
            continue
        if kind not in calls.SIGNAL_TYPES or not isinstance(payload, dict):
            continue
        try:
            send_signal(
                room_id=room_id,
                user_id=user_id,
                session_token=session_token,
                signal_type=kind,
                recipient_role=recipient_role,
                payload=payload,
            )
        except calls.LiveCallError as exc:
            st.session_state[_error_key(room_id)] = str(exc)[:200]
        except Exception as exc:
            st.session_state[_error_key(room_id)] = f"Signal could not be queued: {type(exc).__name__}"
    raw_state["outbox"] = []
    raw_state["outbox_ack"] = int(raw_state.get("outbox_ack") or 0) + 1


def render_live_call(
    *,
    room: dict[str, Any],
    incoming: list[dict[str, Any]],
    media_config: dict[str, Any],
    enabled: bool,
    user_id: int,
    session_token: str,
) -> None:
    room_id = str(room["room_id"])
    role = str(room.get("role") or "")
    recipient_role = (
        calls.ROLE_CANDIDATE if role == calls.ROLE_ADMIN else calls.ROLE_ADMIN
    )
    key = _state_key(room_id)
    raw_state = st.session_state.get(key) or {}
    outbox_ack = int(raw_state.get("outbox_ack") or 0)

    def on_outbox_change() -> None:
        _enqueue_outbox(
            room_id=room_id,
            role=role,
            recipient_role=recipient_role,
            user_id=user_id,
            session_token=session_token,
        )

    _LIVE_CALL_COMPONENT(
        key=key,
        data={
            **media_config,
            "enabled": bool(enabled),
            "outboxAck": outbox_ack,
            "incoming": incoming,
        },
        default={"outbox": [], "outbox_ack": 0},
        on_outbox_change=on_outbox_change,
        on_outbox_ack_change=lambda: None,
    )
    error = str(st.session_state.get(_error_key(room_id)) or "")
    if error:
        st.error(error, icon=":material/error:")
