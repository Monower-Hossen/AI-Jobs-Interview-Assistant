from __future__ import annotations

from typing import Any

import streamlit as st
from services import live_call_service as calls
from services.live_interview_coordination_service import (
    leave_call,
    release_media_session,
    send_signal,
)

_HTML = """
<div data-live-call-root class="live-call-root">
  <div class="live-call-topbar">
    <div class="live-call-heading">
      <span class="live-call-eyebrow">Secure live interview</span>
      <strong data-room-title>Live interview</strong>
    </div>
    <span data-status class="live-call-status" data-tone="warn">Ready to join</span>
  </div>
  <div class="live-call-stage">
    <div class="live-call-tile live-call-remote">
      <div class="live-call-tile-head">
        <span data-remote-label>Remote participant</span>
        <span class="live-call-badges">
          <span data-remote-camera class="live-call-badge" data-media-on="false">Camera off</span>
          <span data-remote-mic class="live-call-badge" data-media-on="false">Mic muted</span>
        </span>
      </div>
      <video data-remote autoplay playsinline></video>
      <audio data-remote-audio autoplay hidden></audio>
      <div data-remote-empty class="live-call-empty">Waiting for the other participant…</div>
    </div>
    <div class="live-call-tile live-call-local">
      <div class="live-call-tile-head">
        <span data-local-label>You</span>
        <span class="live-call-badges">
          <span data-local-camera class="live-call-badge" data-media-on="true">Camera on</span>
          <span data-local-mic class="live-call-badge" data-media-on="true">Mic on</span>
        </span>
      </div>
      <video data-local autoplay muted playsinline></video>
      <div data-local-empty class="live-call-empty">Camera preview is off</div>
    </div>
  </div>
  <div class="live-call-toolbar">
    <div class="live-call-actions">
      <button data-action="join" type="button">Join call</button>
      <button data-action="leave" type="button" disabled>Leave</button>
      <button data-action="camera" type="button" disabled>Turn camera off</button>
      <button data-action="mic" type="button" disabled>Mute microphone</button>
      <button data-action="sound" type="button" hidden>Enable sound</button>
    </div>
    <label class="live-call-device">
      <span>Camera</span>
      <select data-camera aria-label="Camera device" disabled>
        <option value="">Default camera</option>
      </select>
    </label>
  </div>
  <div data-message class="live-call-message">Live calls are not recorded.</div>
  <div data-diagnostics class="live-call-message" aria-live="polite"></div>
</div>
"""

_CSS = """
.live-call-root {
  --call-bg: #080d18;
  --call-panel: #101827;
  --call-panel-2: #0b1220;
  --call-border: #25304a;
  --call-text: #e8eef8;
  --call-muted: #91a0b8;
  --call-accent: #4f8cff;
  --call-good: #35d399;
  --call-warn: #f4b740;
  --call-bad: #ff6b6b;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  padding: 0.9rem;
  border: 1px solid var(--call-border);
  border-radius: 1rem;
  background: linear-gradient(145deg, var(--call-panel), var(--call-bg));
  color: var(--call-text);
  box-shadow: 0 18px 44px rgba(0, 0, 0, 0.28);
  font-family: var(--st-font, inherit);
}
.live-call-root [hidden] { display: none !important; }
.live-call-topbar { display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: center; justify-content: space-between; }
.live-call-heading { display: flex; flex-direction: column; gap: 0.15rem; min-width: 0; }
.live-call-eyebrow { color: var(--call-muted); font-size: 0.7rem; letter-spacing: 0.11em; text-transform: uppercase; }
.live-call-heading strong { font-size: 1rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.live-call-status { border: 1px solid rgba(244, 183, 64, 0.45); border-radius: 999px; background: rgba(244, 183, 64, 0.14); color: var(--call-warn); font-size: 0.78rem; font-weight: 700; padding: 0.28rem 0.7rem; white-space: nowrap; }
.live-call-status[data-tone="ok"] { border-color: rgba(53, 211, 153, 0.45); background: rgba(53, 211, 153, 0.14); color: var(--call-good); }
.live-call-status[data-tone="bad"] { border-color: rgba(255, 107, 107, 0.45); background: rgba(255, 107, 107, 0.14); color: var(--call-bad); }
.live-call-status[data-tone="info"] { border-color: rgba(79, 140, 255, 0.45); background: rgba(79, 140, 255, 0.14); color: #9dbcff; }
.live-call-stage { display: grid; grid-template-columns: minmax(0, 1fr) 240px; gap: 0.7rem; align-items: stretch; }
.live-call-tile { position: relative; display: flex; flex-direction: column; gap: 0.35rem; overflow: hidden; border: 1px solid var(--call-border); border-radius: 0.9rem; background: var(--call-panel-2); }
.live-call-tile-head { display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center; justify-content: space-between; padding: 0.45rem 0.55rem 0; font-size: 0.78rem; }
.live-call-tile-head > span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.live-call-badges { display: flex; flex-wrap: wrap; gap: 0.3rem; }
.live-call-badge { border: 1px solid rgba(145, 160, 184, 0.35); border-radius: 999px; color: var(--call-muted); font-size: 0.68rem; padding: 0.1rem 0.4rem; white-space: nowrap; }
.live-call-badge[data-media-on="true"] { border-color: rgba(53, 211, 153, 0.4); color: var(--call-good); }
.live-call-tile video { display: block; width: 100%; flex: 1; min-height: 0; aspect-ratio: 16 / 9; background: #030711; object-fit: cover; }
.live-call-remote video { min-height: 340px; }
.live-call-local video { min-height: 190px; }
.live-call-empty { position: absolute; inset: 2.1rem 0 0; display: grid; place-items: center; padding: 1rem; color: var(--call-muted); font-size: 0.82rem; text-align: center; pointer-events: none; }
.live-call-toolbar { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; justify-content: space-between; }
.live-call-actions { display: flex; flex-wrap: wrap; gap: 0.45rem; }
.live-call-actions button { border: 1px solid var(--call-accent); border-radius: 0.55rem; background: var(--call-accent); color: #ffffff; cursor: pointer; font-weight: 650; padding: 0.42rem 0.75rem; }
.live-call-actions button[data-action="leave"] { border-color: var(--call-bad); background: rgba(255, 107, 107, 0.14); color: var(--call-bad); }
.live-call-actions button[data-action="sound"] { border-color: rgba(244, 183, 64, 0.6); background: rgba(244, 183, 64, 0.14); color: var(--call-warn); }
.live-call-actions button:disabled { cursor: not-allowed; opacity: 0.42; }
.live-call-device { display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center; color: var(--call-muted); font-size: 0.75rem; }
.live-call-device select { max-width: 220px; border: 1px solid var(--call-border); border-radius: 0.45rem; background: var(--call-panel-2); color: var(--call-text); padding: 0.3rem 0.4rem; }
.live-call-device select:disabled { opacity: 0.5; }
.live-call-message { color: var(--call-muted); font-size: 0.75rem; }
@media (max-width: 760px) {
  .live-call-stage { grid-template-columns: 1fr; }
  .live-call-remote video { min-height: 260px; }
  .live-call-local video { min-height: 180px; }
  .live-call-toolbar { align-items: stretch; flex-direction: column; }
  .live-call-actions { width: 100%; }
  .live-call-actions button { flex: 1 1 auto; }
}
"""

_JS = r"""
const liveCallStates = new WeakMap();

export function formatLiveCallSupportMessage(diagnostic) {
  const origin = diagnostic && diagnostic.origin ? String(diagnostic.origin) : "unknown";
  const prefix = "Current origin: " + origin + ".";
  const secure = Boolean(diagnostic && diagnostic.secure);
  if (!secure) {
    return prefix + " Secure HTTPS context required. Camera and microphone APIs are unavailable on this HTTP origin. localhost works for local development; LAN IP access requires HTTPS.";
  }
  const parts = [prefix];
  const hasMediaDevices = Boolean(diagnostic && diagnostic.hasMediaDevices);
  const hasGetUserMedia = Boolean(diagnostic && diagnostic.hasGetUserMedia);
  if (!hasMediaDevices || !hasGetUserMedia) {
    parts.push("Camera and microphone APIs unavailable.");
    const embedded = Boolean(diagnostic && diagnostic.embedded);
    const iframeAllow = diagnostic && diagnostic.iframeAllow ? String(diagnostic.iframeAllow) : "";
    if (embedded) {
      if (!/camera/.test(iframeAllow) || !/microphone/.test(iframeAllow)) {
        parts.push("Camera/microphone blocked by iframe permissions. The parent page must allow camera and microphone.");
      } else {
        parts.push("The component is embedded; verify parent-frame Permissions Policy.");
      }
    }
    const policy = diagnostic && diagnostic.policy ? diagnostic.policy : {};
    if (policy.camera === "blocked") {
      parts.push("Camera blocked by Permissions Policy.");
    }
    if (policy.microphone === "blocked") {
      parts.push("Microphone blocked by Permissions Policy.");
    }
    parts.push("Browser does not support media capture or Permissions Policy blocks it.");
  }
  const permissions = diagnostic && diagnostic.permissions ? diagnostic.permissions : {};
  if (permissions.camera === "denied") {
    parts.push("Camera permission denied.");
  }
  if (permissions.microphone === "denied") {
    parts.push("Microphone permission denied.");
  }
  return parts.join(" ");
}

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
      peerRoomId: "",
      localStream: null,
      remoteVideoStream: null,
      remoteAudioStream: null,
      started: false,
      joining: false,
      acquiring: false,
      cameraEnabled: true,
      cameraIntent: true,
      micEnabled: true,
      cameraAvailable: false,
      micAvailable: false,
      remoteCameraEnabled: false,
      remoteMicEnabled: false,
      remoteCameraSignaled: null,
      remoteMicSignaled: null,
      remoteCameraTrackLive: false,
      remoteMicTrackLive: false,
      ontrackCount: 0,
      soundBlocked: false,
      mediaDiagnostics: null,
      diagnosticText: "",
      offerSent: false,
      offerReceived: false,
      answerSent: false,
      answerReceived: false,
      iceGatheringState: "",
      iceConnectionState: "",
      peerConnectionState: "",
      processedIds: new Set(),
      signalChain: Promise.resolve(),
      pendingSignals: [],
      pendingCandidates: [],
      reconnectAttempts: 0,
      reconnecting: false,
      attached: false,
      lastAck: -1,
      outbox: [],
      cameraDevices: [],
      selectedDeviceId: "",
      currentDeviceId: "",
      cameraHealth: "unknown",
      cameraFramesOk: false,
      cameraFrameCount: 0,
      cameraFrameStart: 0,
      cameraFrameCallback: 0,
      cameraHealthTimer: 0,
      localPreviewReady: false,
      recoveringVideo: false,
      videoRecoveryAttempts: 0,
      watchedTracks: new WeakSet(),
      config: cfg
    };
    liveCallStates.set(parentElement, state);
  }
  cfg = data || {};
  state.config = cfg;

  const statusEl = root.querySelector("[data-status]");
  const titleEl = root.querySelector("[data-room-title]");
  const localLabelEl = root.querySelector("[data-local-label]");
  const remoteLabelEl = root.querySelector("[data-remote-label]");
  const localCameraEl = root.querySelector("[data-local-camera]");
  const localMicEl = root.querySelector("[data-local-mic]");
  const remoteCameraEl = root.querySelector("[data-remote-camera]");
  const remoteMicEl = root.querySelector("[data-remote-mic]");
  const localEmptyEl = root.querySelector("[data-local-empty]");
  const remoteEmptyEl = root.querySelector("[data-remote-empty]");
  const cameraSelect = root.querySelector("[data-camera]");
  const joinButton = root.querySelector("[data-action='join']");
  const leaveButton = root.querySelector("[data-action='leave']");
  const cameraButton = root.querySelector("[data-action='camera']");
  const micButton = root.querySelector("[data-action='mic']");
  const soundButton = root.querySelector("[data-action='sound']");
  const localVideo = root.querySelector("[data-local]");
  const remoteVideo = root.querySelector("[data-remote]");
  const remoteAudio = root.querySelector("[data-remote-audio]");
  const messageEl = root.querySelector("[data-message]");
  const diagnosticsEl = root.querySelector("[data-diagnostics]");
  if (!statusEl || !joinButton || !leaveButton || !cameraButton || !micButton || !soundButton || !localVideo || !remoteVideo || !remoteAudio || !cameraSelect) {
    return () => {};
  }

  const ack = Number(cfg.outboxAck || 0);
  if (state.lastAck < 0 || ack < state.lastAck) {
    state.lastAck = ack;
  } else if (ack > state.lastAck) {
    const acknowledged = Math.min(ack - state.lastAck, state.outbox.length);
    state.outbox = state.outbox.slice(acknowledged);
    setStateValue("outbox", state.outbox);
    state.lastAck = ack;
  }

  let deviceChangeHandler = null;

  state.pageHideHandler = state.pageHideHandler || null;

  function summarizeTrackSettings(track) {
    if (!track || typeof track.getSettings !== "function") {
      return {};
    }
    try {
      const settings = track.getSettings() || {};
      const result = {};
      if (settings.deviceId !== undefined) result.deviceId = String(settings.deviceId);
      if (settings.width !== undefined) result.width = Number(settings.width) || 0;
      if (settings.height !== undefined) result.height = Number(settings.height) || 0;
      if (settings.frameRate !== undefined) result.frameRate = Number(settings.frameRate) || 0;
      if (settings.facingMode !== undefined) result.facingMode = String(settings.facingMode);
      return result;
    } catch (error) {
      return {};
    }
  }

  function summarizeTrack(track) {
    if (!track) {
      return null;
    }
    const settings = summarizeTrackSettings(track);
    return {
      id: String(track.id || ""),
      kind: String(track.kind || ""),
      enabled: Boolean(track.enabled),
      muted: Boolean(track.muted),
      readyState: String(track.readyState || ""),
      settings: settings,
      width: Number(settings.width || 0),
      height: Number(settings.height || 0),
      frameRate: Number(settings.frameRate || 0)
    };
  }

  function senderSummary(pc) {
    const senders = pc && typeof pc.getSenders === "function" ? pc.getSenders() : [];
    const receivers = pc && typeof pc.getReceivers === "function" ? pc.getReceivers() : [];
    return {
      present: Boolean(pc),
      connectionState: String((pc && pc.connectionState) || ""),
      iceConnectionState: String((pc && pc.iceConnectionState) || ""),
      signalingState: String((pc && pc.signalingState) || ""),
      activeSenders: senders.filter((sender) => Boolean(sender && sender.track)).length,
      activeReceivers: receivers.filter((receiver) => Boolean(receiver && receiver.track)).length,
      videoSender: (() => {
        const index = senders.findIndex((sender) => sender && sender.track && sender.track.kind === "video");
        return index >= 0 ? { index: index, track: summarizeTrack(senders[index].track) } : null;
      })(),
      senders: senders.map((sender) => summarizeTrack(sender && sender.track)).filter(Boolean),
      receivers: receivers.map((receiver) => summarizeTrack(receiver && receiver.track)).filter(Boolean)
    };
  }

  function logDiagnostic(event, extra) {
    if (cfg.debugDiagnostics === false || typeof console.debug !== "function") {
      return;
    }
    const details = Object.assign(
      {
        role: String(cfg.role || "unknown"),
        participantKey: String(cfg.participantKey || "unknown"),
        localRole: String(cfg.localRole || "unknown"),
        remoteRole: String(cfg.remoteRole || "unknown")
      },
      extra || {}
    );
    try {
      console.debug("[live-call] " + event, details);
    } catch (error) {
      return;
    }
  }

  function localTracks(kind) {
    if (!state.localStream || typeof state.localStream.getTracks !== "function") {
      return [];
    }
    return state.localStream.getTracks().filter((track) => String(track.kind) === kind);
  }

  function streamTracks(stream, kind) {
    if (!stream || typeof stream.getTracks !== "function") {
      return [];
    }
    return stream.getTracks().filter((track) => String(track.kind) === kind);
  }

  function isCameraTrackLive(track) {
    return Boolean(track && String(track.readyState) === "live");
  }

  function isCameraTrackUsable(track) {
    return isCameraTrackLive(track)
      && Boolean(track.enabled)
      && !Boolean(track.muted)
      && state.cameraFramesOk !== false;
  }

  function syncLocalMediaState() {
    const video = localTracks("video");
    const audio = localTracks("audio").filter((track) => String(track.readyState) === "live");
    const liveVideo = video.filter(isCameraTrackLive);
    state.cameraAvailable = liveVideo.length > 0;
    state.micAvailable = audio.length > 0;
    state.cameraEnabled = liveVideo.some(isCameraTrackUsable);
    state.micEnabled = audio.some((track) => Boolean(track.enabled));
    if (liveVideo.some((track) => Boolean(track.muted))) {
      state.cameraHealth = "muted";
    } else if (state.cameraAvailable && liveVideo.some((track) => Boolean(track.enabled)) && !state.cameraEnabled && state.cameraFramesOk === false) {
      state.cameraHealth = "no-frames";
    } else if (state.cameraEnabled && state.cameraFramesOk) {
      state.cameraHealth = "ok";
    }
  }

  function syncRemoteMediaState() {
    const video = streamTracks(state.remoteVideoStream, "video").find((track) => String(track.readyState) === "live");
    const audio = streamTracks(state.remoteAudioStream, "audio").find((track) => String(track.readyState) === "live");
    const cameraSignaled = state.remoteCameraSignaled === null ? true : Boolean(state.remoteCameraSignaled);
    const micSignaled = state.remoteMicSignaled === null ? true : Boolean(state.remoteMicSignaled);
    state.remoteCameraTrackLive = Boolean(video);
    state.remoteMicTrackLive = Boolean(audio);
    state.remoteCameraEnabled = Boolean(video && video.enabled && !video.muted && cameraSignaled);
    state.remoteMicEnabled = Boolean(audio && audio.enabled && !audio.muted && micSignaled);
  }

  function setStatus(text, tone) {
    statusEl.textContent = text;
    statusEl.dataset.tone = tone;
  }

  function safeErrorName(error) {
    const raw = error && error.name ? String(error.name) : "UnknownError";
    return raw.replace(/[^A-Za-z0-9_]/g, "").slice(0, 60) || "UnknownError";
  }

  function safeMessage(error, fallback) {
    const text = error && error.message ? String(error.message) : fallback;
    return text.replace(/[\r\n]+/g, " ").slice(0, 240) || fallback;
  }

  function safeStopTracks(stream) {
    if (!stream || typeof stream.getTracks !== "function") {
      return;
    }
    stream.getTracks().forEach((track) => {
      try {
        track.stop();
      } catch (error) {
        setStatus("A media track could not be stopped cleanly.", "bad");
      }
    });
  }

  function previewSummary() {
    const playsInline = Boolean(localVideo.playsInline)
      || (typeof localVideo.getAttribute === "function" && localVideo.getAttribute("playsinline") !== null);
    return {
      hasSrcObject: Boolean(localVideo.srcObject),
      srcTrackIds: localVideo.srcObject && typeof localVideo.srcObject.getTracks === "function"
        ? localVideo.srcObject.getTracks().map((track) => String(track.id || ""))
        : [],
      autoplay: Boolean(localVideo.autoplay),
      muted: Boolean(localVideo.muted),
      playsInline: playsInline,
      videoWidth: Number(localVideo.videoWidth || 0),
      videoHeight: Number(localVideo.videoHeight || 0),
      readyState: Number(localVideo.readyState || 0),
      paused: Boolean(localVideo.paused),
      previewReady: Boolean(state.localPreviewReady),
      health: String(state.cameraHealth || "unknown")
    };
  }

  function clearCameraHealthProbe() {
    if (state.cameraFrameCallback && typeof localVideo.cancelVideoFrameCallback === "function") {
      try {
        localVideo.cancelVideoFrameCallback(state.cameraFrameCallback);
      } catch (error) {
        state.cameraFrameCallback = 0;
      }
    }
    state.cameraFrameCallback = 0;
    if (state.cameraHealthTimer) {
      if (typeof clearTimeout === "function") {
        clearTimeout(state.cameraHealthTimer);
      } else if (typeof clearInterval === "function") {
        clearInterval(state.cameraHealthTimer);
      }
      state.cameraHealthTimer = 0;
    }
  }

  function markCameraFrames() {
    const first = state.cameraFrameCount === 0;
    state.cameraFrameCount += 1;
    state.cameraFrameStart = state.cameraFrameStart || Date.now();
    state.cameraFramesOk = true;
    state.cameraHealth = "ok";
    if (first) {
      logDiagnostic("local video frames detected", {
        elapsedMs: Date.now() - state.cameraFrameStart,
        tracks: localTracks("video").map(summarizeTrack),
        preview: previewSummary()
      });
    }
    syncLocalMediaState();
    if (first) {
      emitMediaState();
    }
    updateMediaIndicators();
    updateEmptyState();
    updateStageDiagnostics();
  }

  function startCameraHealthProbe() {
    clearCameraHealthProbe();
    state.cameraFrameCount = 0;
    state.cameraFrameStart = Date.now();
    state.cameraFramesOk = false;
    state.cameraHealth = "unknown";
    const timeoutMs = 2500;
    state.cameraHealthTimer = setTimeout(() => {
      if (!state.started || state.cameraFramesOk) {
        return;
      }
      state.cameraHealth = localTracks("video").some((track) => Boolean(track.muted)) ? "muted" : "no-frames";
      logDiagnostic("local video frames unavailable", {
        timeoutMs: timeoutMs,
        tracks: localTracks("video").map(summarizeTrack),
        preview: previewSummary(),
        peer: senderSummary(state.pc)
      });
      setStatus("Camera is not producing video frames. Try switching cameras.", "bad");
      emitMediaState();
      syncLocalMediaState();
      updateMediaIndicators();
      updateEmptyState();
      updateStageDiagnostics();
      recoverLocalVideo("no-frames");
    }, timeoutMs);
    if (typeof localVideo.requestVideoFrameCallback === "function") {
      const onFrame = () => {
        if (!state.started) {
          return;
        }
        markCameraFrames();
        if (typeof localVideo.requestVideoFrameCallback === "function") {
          state.cameraFrameCallback = localVideo.requestVideoFrameCallback(onFrame);
        }
      };
      state.cameraFrameCallback = localVideo.requestVideoFrameCallback(onFrame);
    } else if (typeof localVideo.getVideoPlaybackQuality === "function") {
      let lastCount = Number(localVideo.getVideoPlaybackQuality().totalVideoFrames || 0);
      if (lastCount > 0) {
        markCameraFrames();
      }
      state.cameraHealthTimer = setInterval(() => {
        if (!state.started) {
          return;
        }
        const current = Number(localVideo.getVideoPlaybackQuality().totalVideoFrames || 0);
        if (current > lastCount) {
          lastCount = current;
          markCameraFrames();
        }
      }, 250);
    } else if (Number(localVideo.videoWidth || 0) > 0 && Number(localVideo.videoHeight || 0) > 0) {
      markCameraFrames();
    }
  }

  function videoSender() {
    if (!state.pc || typeof state.pc.getSenders !== "function") {
      return null;
    }
    return state.pc.getSenders().find((sender) => sender && sender.track && sender.track.kind === "video") || null;
  }

  function watchLocalVideoTrack(track) {
    if (!track || state.watchedTracks.has(track)) {
      return;
    }
    state.watchedTracks.add(track);
    track.onmute = () => {
      state.cameraHealth = "muted";
      state.cameraFramesOk = false;
      logDiagnostic("local video track muted", { track: summarizeTrack(track), preview: previewSummary() });
      emitMediaState();
      syncLocalMediaState();
      updateMediaIndicators();
      updateEmptyState();
      updateStageDiagnostics();
      recoverLocalVideo("muted");
    };
    track.onunmute = () => {
      state.cameraHealth = "unknown";
      state.cameraFramesOk = false;
      logDiagnostic("local video track unmuted", { track: summarizeTrack(track), preview: previewSummary() });
      state.localPreviewReady = false;
      attachLocalPreview(state.localStream);
    };
    track.onended = () => {
      state.cameraHealth = "ended";
      state.cameraFramesOk = false;
      logDiagnostic("local video track ended", { track: summarizeTrack(track), preview: previewSummary() });
      emitMediaState();
      syncLocalMediaState();
      updateMediaIndicators();
      updateEmptyState();
      updateStageDiagnostics();
      recoverLocalVideo("ended");
    };
  }

  function watchRemoteTrack(track, kind) {
    if (!track || state.watchedTracks.has(track)) {
      return;
    }
    state.watchedTracks.add(track);
    track.onmute = () => {
      logDiagnostic("remote track muted", { kind: String(kind || ""), track: summarizeTrack(track) });
      syncRemoteMediaState();
      updateMediaIndicators();
      updateStageDiagnostics();
    };
    track.onunmute = () => {
      logDiagnostic("remote track unmuted", { kind: String(kind || ""), track: summarizeTrack(track) });
      syncRemoteMediaState();
      updateMediaIndicators();
      updateStageDiagnostics();
    };
    track.onended = () => {
      logDiagnostic("remote track ended", { kind: String(kind || ""), track: summarizeTrack(track) });
      syncRemoteMediaState();
      updateMediaIndicators();
      updateStageDiagnostics();
    };
  }

  function attachLocalPreview(stream) {
    if (!stream) {
      return;
    }
    const tracks = typeof stream.getTracks === "function" ? stream.getTracks() : [];
    const previewStream = typeof MediaStream === "function" ? new MediaStream(tracks) : stream;
    localVideo.autoplay = true;
    localVideo.muted = true;
    localVideo.playsInline = true;
    if (typeof localVideo.setAttribute === "function") {
      localVideo.setAttribute("autoplay", "");
      localVideo.setAttribute("muted", "");
      localVideo.setAttribute("playsinline", "");
    }
    clearCameraHealthProbe();
    localVideo.srcObject = null;
    localVideo.srcObject = previewStream;
    state.localPreviewReady = false;
    let playPromise = null;
    try {
      playPromise = localVideo.play();
    } catch (error) {
      playPromise = Promise.reject(error);
    }
    if (playPromise && typeof playPromise.then === "function") {
      playPromise.then(() => {
        state.localPreviewReady = true;
        logDiagnostic("local preview playing", { preview: previewSummary() });
        startCameraHealthProbe();
        updateStageDiagnostics();
      }).catch((error) => {
        state.localPreviewReady = false;
        state.cameraHealth = "play-failed";
        logDiagnostic("local preview play failed", {
          error: safeErrorName(error),
          preview: previewSummary()
        });
        setStatus("The camera preview could not start. Check browser autoplay settings.", "bad");
        updateStageDiagnostics();
      });
    } else {
      state.localPreviewReady = true;
      startCameraHealthProbe();
    }
  }

  async function replaceLocalVideoTrack(nextTrack, deviceId) {
    const previous = localTracks("video")[0] || null;
    const sender = videoSender();
    nextTrack.enabled = state.cameraIntent && String(nextTrack.readyState) === "live";
    if (sender) {
      try {
        await sender.replaceTrack(nextTrack);
      } catch (error) {
        try {
          nextTrack.stop();
        } catch (stopError) {
          state.cameraHealth = "ended";
        }
        logDiagnostic("video sender replace failed", {
          error: safeErrorName(error),
          track: summarizeTrack(nextTrack),
          peer: senderSummary(state.pc)
        });
        setStatus("Camera switched locally; PeerConnection update failed.", "warn");
        return false;
      }
    }
    if (previous && previous !== nextTrack) {
      state.localStream.removeTrack(previous);
      try {
        previous.stop();
      } catch (error) {
        state.cameraHealth = "ended";
      }
    }
    if (!state.localStream.getTracks().includes(nextTrack)) {
      state.localStream.addTrack(nextTrack);
    }
    watchLocalVideoTrack(nextTrack);
    state.selectedDeviceId = String(deviceId || state.selectedDeviceId || "");
    state.currentDeviceId = String(summarizeTrackSettings(nextTrack).deviceId || state.selectedDeviceId);
    state.cameraFramesOk = false;
    state.cameraHealth = "unknown";
    attachLocalPreview(state.localStream);
    syncLocalMediaState();
    emitMediaState();
    updateButtons();
    updateMediaIndicators();
    updateEmptyState();
    updateStageDiagnostics();
    logDiagnostic("local video sender", {
      track: summarizeTrack(nextTrack),
      sender: videoSender() ? summarizeTrack(videoSender().track) : null,
      peer: senderSummary(state.pc)
    });
    return true;
  }

  async function recoverLocalVideo(reason) {
    if (!state.started || state.recoveringVideo || !state.cameraIntent) {
      return;
    }
    if (state.videoRecoveryAttempts >= 2) {
      logDiagnostic("camera recovery limit reached", { reason: String(reason || "") });
      return;
    }
    state.recoveringVideo = true;
    state.videoRecoveryAttempts += 1;
    try {
      const candidates = [state.selectedDeviceId];
      state.cameraDevices.forEach((device) => candidates.push(String(device.deviceId || "")));
      const seen = new Set();
      for (const deviceId of candidates) {
        const selected = String(deviceId || "");
        if (seen.has(selected)) {
          continue;
        }
        seen.add(selected);
        let stream = null;
        try {
          stream = await navigator.mediaDevices.getUserMedia({
            video: videoConstraints(selected),
            audio: false
          });
        } catch (error) {
          logDiagnostic("camera recovery attempt failed", {
            reason: String(reason || ""),
            error: safeErrorName(error),
            device: selected
          });
          continue;
        }
        const track = stream.getVideoTracks()[0];
        if (!track) {
          safeStopTracks(stream);
          continue;
        }
        stream.getTracks().forEach((item) => {
          if (item !== track) {
            try {
              item.stop();
            } catch (error) {
              state.cameraHealth = "ended";
            }
          }
        });
        const replaced = await replaceLocalVideoTrack(track, selected);
        if (replaced) {
          logDiagnostic("camera recovered", {
            reason: String(reason || ""),
            attempts: state.videoRecoveryAttempts,
            track: summarizeTrack(track)
          });
          return;
        }
      }
      setStatus("Camera recovery failed. Select another camera and try again.", "bad");
    } finally {
      state.recoveringVideo = false;
    }
  }

  async function diagnoseMediaSupport() {
    const mediaDevices = navigator.mediaDevices;
    const embedded = window.self !== window.top;
    let iframeAllow = "not-embedded";
    if (embedded) {
      try {
        iframeAllow = window.frameElement && typeof window.frameElement.getAttribute === "function"
          ? (window.frameElement.getAttribute("allow") || "")
          : "unknown";
      } catch (error) {
        iframeAllow = "cross-origin";
      }
    }
    const permissions = {};
    if (navigator.permissions && typeof navigator.permissions.query === "function") {
      for (const name of ["camera", "microphone"]) {
        try {
          const result = await navigator.permissions.query({ name: name });
          permissions[name] = result.state;
        } catch (error) {
          permissions[name] = "unknown (" + safeErrorName(error) + ")";
        }
      }
    }
    const policy = {};
    const policyObject = document.permissionsPolicy || document.featurePolicy;
    if (policyObject && typeof policyObject.allowsFeature === "function") {
      for (const name of ["camera", "microphone"]) {
        try {
          policy[name] = policyObject.allowsFeature(name) ? "allowed" : "blocked";
        } catch (error) {
          policy[name] = "unknown (" + safeErrorName(error) + ")";
        }
      }
    }
    return {
      secure: Boolean(window.isSecureContext),
      origin: String(window.location && window.location.origin ? window.location.origin : ""),
      hasMediaDevices: Boolean(mediaDevices),
      hasGetUserMedia: Boolean(mediaDevices && typeof mediaDevices.getUserMedia === "function"),
      hasEnumerateDevices: Boolean(mediaDevices && typeof mediaDevices.enumerateDevices === "function"),
      embedded: embedded,
      iframeAllow: String(iframeAllow).slice(0, 200),
      permissions: permissions,
      policy: policy
    };
  }

  function mapMediaError(error, kind) {
    const name = safeErrorName(error);
    const isCamera = kind === "camera";
    const subject = isCamera ? "Camera" : kind === "microphone" ? "Microphone" : "Media";
    if (name === "NotAllowedError" || name === "PermissionDeniedError" || name === "SecurityError") {
      return subject + " permission denied or blocked by iframe permissions. (" + name + ")";
    }
    if (name === "NotFoundError" || name === "DevicesNotFoundError") {
      return (isCamera ? "Camera device not found." : "Microphone device unavailable.") + " (" + name + ")";
    }
    if (name === "NotReadableError" || name === "TrackStartError" || name === "AbortError") {
      return subject + " device is unavailable or in use by another application. (" + name + ")";
    }
    if (name === "OverconstrainedError" || name === "ConstraintNotSatisfiedError") {
      const constraint = error && error.constraint ? String(error.constraint).slice(0, 40) : "device";
      return (isCamera ? "Selected camera is unavailable" : "Selected microphone is unavailable") + " (" + name + ": " + constraint + ")";
    }
    if (name === "TypeError") {
      return "Browser does not support media capture with the requested constraints. (" + name + ")";
    }
    return subject + " capture failed. (" + name + ")";
  }

  function setBadge(element, enabled, onText, offText) {
    if (!element) {
      return;
    }
    element.textContent = enabled ? onText : offText;
    element.dataset.mediaOn = enabled ? "true" : "false";
  }

  function updateMediaIndicators() {
    syncLocalMediaState();
    syncRemoteMediaState();
    setBadge(localCameraEl, Boolean(state.cameraEnabled && state.cameraAvailable), "Camera on", "Camera off");
    setBadge(localMicEl, Boolean(state.micEnabled && state.micAvailable), "Mic on", "Mic muted");
    setBadge(remoteCameraEl, state.remoteCameraEnabled, "Camera on", "Camera off");
    setBadge(remoteMicEl, state.remoteMicEnabled, "Mic on", "Mic muted");
  }

  function updateEmptyState() {
    syncLocalMediaState();
    syncRemoteMediaState();
    const localActive = Boolean(state.localStream && state.localStream.getVideoTracks().length && state.cameraEnabled && state.cameraAvailable);
    const remoteActive = Boolean(state.remoteVideoStream && state.remoteVideoStream.getVideoTracks().length);
    if (localEmptyEl) {
      localEmptyEl.hidden = localActive;
      localEmptyEl.textContent = !state.cameraAvailable
        ? "Camera unavailable"
        : state.cameraHealth === "no-frames" || state.cameraHealth === "muted" || state.cameraHealth === "play-failed"
          ? "Camera is not producing video"
          : "Camera preview is off";
    }
    if (remoteEmptyEl) {
      remoteEmptyEl.hidden = remoteActive;
      remoteEmptyEl.textContent = remoteParticipantJoined()
        ? "Connecting to the other participant…"
        : "Waiting for the other participant to join…";
    }
  }

  function remoteParticipantJoined() {
    return state.remoteCameraSignaled !== null
      || streamTracks(state.remoteVideoStream, "video").some((track) => String(track.readyState) === "live")
      || streamTracks(state.remoteAudioStream, "audio").some((track) => String(track.readyState) === "live");
  }

  function updateStageDiagnostics() {
    if (!diagnosticsEl) {
      return;
    }
    const pc = state.pc;
    if (pc && pc.iceGatheringState) {
      state.iceGatheringState = String(pc.iceGatheringState);
    }
    if (pc && pc.iceConnectionState) {
      state.iceConnectionState = String(pc.iceConnectionState);
    }
    if (pc && pc.connectionState) {
      state.peerConnectionState = String(pc.connectionState);
    }
    const flag = (value) => value ? "yes" : "no";
    const status = String(cfg.callStatus || "");
    diagnosticsEl.textContent = [
      "Local capture: " + flag(Boolean(state.localStream && state.started)),
      "Local video frames: " + (state.cameraHealth === "ok" ? "yes" : state.cameraHealth === "unknown" ? "checking" : "no"),
      "Local preview: " + (state.localPreviewReady ? "ready" : "failed"),
      "Camera muted: " + flag(localTracks("video").some((track) => Boolean(track.muted))),
      "Invitation ready: " + flag(status === "accepted" || status === "in_progress"),
      "Remote joined: " + flag(remoteParticipantJoined()),
      "Offer sent: " + flag(state.offerSent),
      "Offer received: " + flag(state.offerReceived),
      "Answer sent: " + flag(state.answerSent),
      "Answer received: " + flag(state.answerReceived),
      "ICE gathering: " + (state.iceGatheringState || "idle"),
      "ICE connection: " + (state.iceConnectionState || "new"),
      "Peer connection: " + (state.peerConnectionState || "new"),
      "Remote video track: " + flag(streamTracks(state.remoteVideoStream, "video").length > 0),
      "Remote audio track: " + flag(streamTracks(state.remoteAudioStream, "audio").length > 0)
    ].join(" · ");
  }

  function updateButtons() {
    syncLocalMediaState();
    const enabled = Boolean(cfg.enabled);
    joinButton.disabled = !enabled || state.started || state.joining || state.acquiring;
    leaveButton.disabled = !state.started;
    cameraButton.disabled = !enabled || !state.started || !state.cameraAvailable;
    micButton.disabled = !enabled || !state.started || !state.micAvailable;
    cameraSelect.disabled = !state.started || state.joining || !state.cameraAvailable;
    soundButton.hidden = !state.soundBlocked;
    cameraButton.textContent = !state.cameraAvailable
      ? "Camera unavailable"
      : state.cameraIntent ? "Turn camera off" : "Turn camera on";
    micButton.textContent = !state.micAvailable
      ? "Microphone unavailable"
      : state.micEnabled ? "Mute microphone" : "Unmute microphone";
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
    logDiagnostic("signal queued", { kind: String(kind), queueSize: state.outbox.length });
  }

  function emitPresenceEnd() {
    if (!state.started && !state.acquiring) {
      return;
    }
    emitSignal("presence_end", {});
  }

  function emitMediaState() {
    if (!state.started) {
      return;
    }
    syncLocalMediaState();
    const payload = {
      camera: Boolean(state.cameraEnabled && state.cameraAvailable),
      mic: Boolean(state.micEnabled && state.micAvailable)
    };
    emitSignal("media_state", payload);
    logDiagnostic("media_state signaled", {
      payload: payload,
      health: String(state.cameraHealth || "unknown"),
      preview: previewSummary(),
      localTracks: {
        camera: localTracks("video").map(summarizeTrack),
        microphone: localTracks("audio").map(summarizeTrack)
      },
      videoSender: videoSender() ? summarizeTrack(videoSender().track) : null
    });
  }

  function wasProcessed(id) {
    const numericId = Number(id);
    return !Number.isFinite(numericId) || state.processedIds.has(numericId);
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

  function playRemote() {
    const jobs = [];
    if (remoteVideo.srcObject && typeof remoteVideo.play === "function") {
      jobs.push(remoteVideo.play());
    }
    if (remoteAudio.srcObject && typeof remoteAudio.play === "function") {
      jobs.push(remoteAudio.play());
    }
    if (!jobs.length) {
      return;
    }
    Promise.all(jobs).then(() => {
      state.soundBlocked = false;
      soundButton.hidden = true;
    }).catch((error) => {
      const name = safeErrorName(error);
      if (name === "NotAllowedError" || name === "SecurityError") {
        state.soundBlocked = true;
        soundButton.hidden = false;
        if (messageEl) {
          messageEl.textContent = "Remote audio is waiting for a click. Select Enable sound to allow playback.";
        }
      }
    });
  }

  function connectionTone(value) {
    if (value === "connected" || value === "completed") {
      return "ok";
    }
    if (value === "failed" || value === "closed") {
      return "bad";
    }
    return "warn";
  }

  function connectionLabel(value) {
    if (value === "connected" || value === "completed") {
      return "Connected";
    }
    if (value === "connecting" || value === "checking" || value === "new") {
      return "Connecting…";
    }
    if (value === "disconnected") {
      return "Reconnecting…";
    }
    if (value === "failed") {
      return "Connection failed";
    }
    if (value === "closed") {
      return "Call ended";
    }
    return "Connection: " + value;
  }

  function ensurePeerConnection() {
    const roomId = String(cfg.roomId || "");
    if (state.pc && state.peerRoomId && state.peerRoomId !== roomId) {
      try {
        state.pc.close();
      } catch (error) {
        setStatus("The previous peer connection could not be closed cleanly.", "bad");
      }
      state.pc = null;
      state.attached = false;
    }
    if (state.pc && state.pc.signalingState !== "closed") {
      state.peerRoomId = roomId;
      return state.pc;
    }
    if (state.pc) {
      state.pc = null;
      state.attached = false;
    }
    const pc = new RTCPeerConnection({
      iceServers: Array.isArray(cfg.iceServers) ? cfg.iceServers : [],
      iceTransportPolicy: cfg.iceTransportPolicy || "all"
    });
    state.pc = pc;
    state.peerRoomId = roomId;
    state.offerSent = false;
    state.offerReceived = false;
    state.answerSent = false;
    state.answerReceived = false;
    state.iceGatheringState = "";
    state.iceConnectionState = "";
    state.peerConnectionState = "";

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
        state.iceGatheringState = "complete";
        emitSignal("ice_gathering_complete", {});
      }
      updateStageDiagnostics();
    };

    pc.ontrack = (event) => {
      const incoming = event.streams && event.streams[0] ? event.streams[0] : null;
      const track = incoming
        ? incoming.getTracks().find((item) => item.id === event.track.id) || event.track
        : event.track;
      watchRemoteTrack(track, track.kind);
      if (track.kind === "video") {
        if (!state.remoteVideoStream) {
          state.remoteVideoStream = new MediaStream();
        }
        if (!state.remoteVideoStream.getTracks().some((item) => item.id === track.id)) {
          state.remoteVideoStream.addTrack(track);
        }
        remoteVideo.srcObject = state.remoteVideoStream;
      } else if (track.kind === "audio") {
        if (!state.remoteAudioStream) {
          state.remoteAudioStream = new MediaStream();
        }
        if (!state.remoteAudioStream.getTracks().some((item) => item.id === track.id)) {
          state.remoteAudioStream.addTrack(track);
        }
        remoteAudio.srcObject = state.remoteAudioStream;
      }
      state.ontrackCount += 1;
      logDiagnostic("ontrack", {
        eventCount: state.ontrackCount,
        track: summarizeTrack(track),
        remoteVideoTracks: streamTracks(state.remoteVideoStream, "video").map(summarizeTrack),
        remoteAudioTracks: streamTracks(state.remoteAudioStream, "audio").map(summarizeTrack),
        peer: senderSummary(pc)
      });
      playRemote();
      updateMediaIndicators();
      updateEmptyState();
      updateStageDiagnostics();
    };

    pc.onconnectionstatechange = () => {
      const value = pc.connectionState;
      logDiagnostic("connection state changed", { connectionState: String(value || ""), peer: senderSummary(pc) });
      setStatus(connectionLabel(value), connectionTone(value));
      updateStageDiagnostics();
      if (value === "failed") {
        scheduleReconnect(false);
      }
    };

    pc.oniceconnectionstatechange = () => {
      const value = pc.iceConnectionState;
      logDiagnostic("ICE state changed", { iceConnectionState: String(value || ""), peer: senderSummary(pc) });
      setStatus(connectionLabel(value), connectionTone(value));
      updateStageDiagnostics();
      if (value === "failed") {
        scheduleReconnect(false);
      }
    };

    pc.onsignalingstatechange = () => {
      logDiagnostic("signaling state changed", { signalingState: String(pc.signalingState || ""), peer: senderSummary(pc) });
    };
    return pc;
  }

  async function createOffer(restart) {
    const pc = ensurePeerConnection();
    if (pc.signalingState !== "stable") {
      return;
    }
    if (!restart && state.offerSent && pc.localDescription && pc.localDescription.type === "offer") {
      logDiagnostic("duplicate offer suppressed", { peer: senderSummary(pc) });
      return;
    }
    const offer = restart ? await pc.createOffer({ iceRestart: true }) : await pc.createOffer();
    await pc.setLocalDescription(offer);
    state.offerSent = true;
    logDiagnostic("offer created", { restart: Boolean(restart), peer: senderSummary(pc) });
    emitSignal("offer", { type: "offer", sdp: pc.localDescription.sdp });
    updateStageDiagnostics();
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
    setStatus("Reconnecting " + attempt + "/" + maxAttempts + "…", "warn");
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

  function videoConstraints(deviceId) {
    const video = cfg.video || {};
    const constraints = {
      width: { ideal: Number(video.width || 640) },
      height: { ideal: Number(video.height || 480) },
      frameRate: { ideal: Number(video.frameRate || 30) }
    };
    if (deviceId) {
      constraints.deviceId = { exact: String(deviceId) };
    }
    return constraints;
  }

  function audioConstraints() {
    const audio = cfg.audio || {};
    return {
      echoCancellation: Boolean(audio.echoCancellation),
      noiseSuppression: Boolean(audio.noiseSuppression),
      autoGainControl: Boolean(audio.autoGainControl)
    };
  }

  async function populateCameraDevices() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
      return;
    }
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const cameras = devices.filter((device) => device.kind === "videoinput");
      const previous = cameraSelect.value || state.selectedDeviceId || "";
      state.cameraDevices = cameras;
      cameraSelect.innerHTML = "";
      const defaultOption = root.ownerDocument.createElement("option");
      defaultOption.value = "";
      defaultOption.textContent = "Default camera";
      cameraSelect.appendChild(defaultOption);
      cameras.forEach((device, index) => {
        const option = root.ownerDocument.createElement("option");
        option.value = device.deviceId;
        option.textContent = device.label || "Camera " + (index + 1);
        cameraSelect.appendChild(option);
      });
      const desired = previous && cameras.some((device) => device.deviceId === previous) ? previous : "";
      cameraSelect.value = desired;
      state.selectedDeviceId = desired;
    } catch (error) {
      setStatus("Camera devices could not be listed.", "warn");
    }
  }

  async function switchCamera(deviceId) {
    if (!state.localStream || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return;
    }
    const selected = String(deviceId || "");
    let nextStream;
    try {
      nextStream = await navigator.mediaDevices.getUserMedia({
        video: videoConstraints(selected),
        audio: false
      });
    } catch (error) {
      setStatus("Camera switch failed. The previous camera is still active. " + mapMediaError(error, "camera"), "bad");
      await populateCameraDevices();
      return;
    }
    const nextTrack = nextStream.getVideoTracks()[0];
    if (!nextTrack) {
      nextStream.getTracks().forEach((track) => track.stop());
      setStatus("Camera switch failed. No video track was returned.", "bad");
      return;
    }
    nextStream.getTracks().forEach((track) => {
      if (track !== nextTrack) {
        try {
          track.stop();
        } catch (error) {
          state.cameraHealth = "ended";
        }
      }
    });
    const replaced = await replaceLocalVideoTrack(nextTrack, selected);
    if (!replaced) {
      await populateCameraDevices();
      return;
    }
    state.videoRecoveryAttempts = 0;
    logDiagnostic("camera switched", {
      sender: videoSender() ? summarizeTrack(videoSender().track) : null,
      track: summarizeTrack(nextTrack),
      peer: senderSummary(state.pc)
    });
  }

  function attachLocalTracks() {
    syncLocalMediaState();
    if (!state.localStream || state.attached) {
      return;
    }
    if (!state.pc) {
      ensurePeerConnection();
    }
    if (!state.pc) {
      return;
    }
    const tracks = state.localStream.getTracks().filter((track) => String(track.readyState) === "live");
    if (!tracks.length) {
      logDiagnostic("no local tracks to publish", { peer: senderSummary(state.pc) });
      return;
    }
    tracks.forEach((track) => {
      if (String(track.kind) === "video") {
        watchLocalVideoTrack(track);
      }
      state.pc.addTrack(track, state.localStream);
    });
    state.attached = true;
    logDiagnostic("local tracks published", {
      tracks: tracks.map(summarizeTrack),
      videoSender: videoSender() ? summarizeTrack(videoSender().track) : null,
      peer: senderSummary(state.pc)
    });
  }

  function mergeMediaStreams(videoStream, audioStream) {
    const merged = new MediaStream();
    if (videoStream) {
      videoStream.getVideoTracks().forEach((track) => merged.addTrack(track));
    }
    if (audioStream) {
      audioStream.getAudioTracks().forEach((track) => merged.addTrack(track));
    }
    return merged;
  }

  async function acquireLocalMedia() {
    const diagnostic = await diagnoseMediaSupport();
    state.mediaDiagnostics = diagnostic;
    const supportMessage = formatLiveCallSupportMessage(diagnostic);
    if (!diagnostic.secure || !diagnostic.hasGetUserMedia) {
      throw new Error(supportMessage || "Browser does not support media capture.");
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: videoConstraints(state.selectedDeviceId),
        audio: audioConstraints()
      });
      return { stream: stream, notes: [] };
    } catch (combinedError) {
      const notes = [];
      if (diagnostic.permissions.camera === "denied") {
        notes.push("Camera permission denied.");
      }
      if (diagnostic.permissions.microphone === "denied") {
        notes.push("Microphone permission denied.");
      }
      let videoStream = null;
      let audioStream = null;
      try {
        videoStream = await navigator.mediaDevices.getUserMedia({
          video: videoConstraints(state.selectedDeviceId),
          audio: false
        });
      } catch (error) {
        notes.push(mapMediaError(error, "camera"));
      }
      try {
        audioStream = await navigator.mediaDevices.getUserMedia({
          video: false,
          audio: audioConstraints()
        });
      } catch (error) {
        notes.push(mapMediaError(error, "microphone"));
      }
      if (!videoStream && !audioStream) {
        notes.push(mapMediaError(combinedError, "media"));
        throw new Error(notes.join(" "));
      }
      return { stream: mergeMediaStreams(videoStream, audioStream), notes: notes };
    }
  }

  async function startLocalMedia(sendOffer, attachTracks) {
    if (state.started || state.acquiring || state.localStream) {
      return false;
    }
    state.acquiring = true;
    try {
      const acquired = await acquireLocalMedia();
      const stream = acquired.stream;
      state.localStream = stream;
      state.cameraAvailable = stream.getVideoTracks().length > 0;
      state.micAvailable = stream.getAudioTracks().length > 0;
      state.cameraEnabled = state.cameraAvailable;
      state.cameraIntent = state.cameraAvailable;
      state.micEnabled = state.micAvailable;
      state.cameraFramesOk = false;
      state.cameraFrameCount = 0;
      state.cameraFrameStart = Date.now();
      state.cameraHealth = "unknown";
      state.videoRecoveryAttempts = 0;
      stream.getVideoTracks().forEach((track) => {
        track.enabled = state.cameraIntent;
        watchLocalVideoTrack(track);
      });
      stream.getAudioTracks().forEach((track) => {
        track.enabled = state.micEnabled;
      });
      syncLocalMediaState();
      logDiagnostic("local media acquired", {
        camera: localTracks("video").map(summarizeTrack),
        microphone: localTracks("audio").map(summarizeTrack)
      });
      state.started = true;
      attachLocalPreview(stream);
      if (attachTracks) {
        attachLocalTracks();
      }
      emitMediaState();
      updateButtons();
      updateMediaIndicators();
      updateEmptyState();
      await populateCameraDevices();
      const remoteJoined = remoteParticipantJoined();
      const remoteText = remoteJoined
        ? " Remote participant joined; connecting…"
        : " Waiting for the other participant to join…";
      if (state.cameraAvailable && state.micAvailable) {
        setStatus("Local camera and microphone active." + remoteText, "warn");
      } else if (state.cameraAvailable) {
        setStatus("Microphone unavailable; using camera only." + remoteText, "warn");
      } else if (state.micAvailable) {
        setStatus("Camera unavailable; using microphone only." + remoteText, "warn");
      } else {
        setStatus("No local media tracks were available.", "bad");
      }
      state.diagnosticText = acquired.notes.length
        ? "Media diagnostics: " + acquired.notes.join(" ")
        : remoteJoined
          ? "Media capture succeeded; remote participant joined."
          : "Media capture succeeded; waiting for the other participant to join.";
      if (messageEl) {
        messageEl.textContent = state.diagnosticText;
      }
      if (sendOffer && cfg.initiator) {
        await createOffer(false);
      }
      const queued = state.pendingSignals.splice(0);
      for (const signal of queued) {
        await handleSignal(signal);
      }
      updateStageDiagnostics();
      return true;
    } finally {
      state.acquiring = false;
    }
  }

  function senderRoleMatches(signal) {
    const senderRole = String(signal.sender_role || "");
    if (!senderRole) {
      return true;
    }
    const expected = String(cfg.role || "") === "admin" ? "candidate" : "admin";
    return senderRole === expected;
  }

  async function handleSignal(signal) {
    const payload = signal.payload || {};
    const senderRole = String(signal.sender_role || "");
    if (!senderRoleMatches(signal)) {
      logDiagnostic("signal rejected for role", { stage: String(signal.signal_type || ""), senderRole: senderRole });
      return;
    }
    if (signal.signal_type === "offer") {
      if (cfg.initiator) {
        return;
      }
      if (!state.started) {
        state.pendingSignals.push(signal);
        return;
      }
      const pc = ensurePeerConnection();
      if (state.offerReceived && pc.localDescription && pc.localDescription.type === "answer") {
        logDiagnostic("duplicate offer suppressed", { peer: senderSummary(pc) });
        return;
      }
      await pc.setRemoteDescription({ type: "offer", sdp: String(payload.sdp || "") });
      state.offerReceived = true;
      logDiagnostic("offer received", { peer: senderSummary(pc) });
      attachLocalTracks();
      await applyPendingCandidates();
      if (state.answerSent && pc.localDescription && pc.localDescription.type === "answer") {
        logDiagnostic("duplicate answer suppressed", { peer: senderSummary(pc) });
        return;
      }
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      state.answerSent = true;
      logDiagnostic("answer created", { peer: senderSummary(pc) });
      emitSignal("answer", { type: "answer", sdp: pc.localDescription.sdp });
      updateStageDiagnostics();
    } else if (signal.signal_type === "answer") {
      if (!cfg.initiator) {
        return;
      }
      const pc = ensurePeerConnection();
      if (state.answerReceived && pc.remoteDescription && pc.remoteDescription.type === "answer") {
        logDiagnostic("duplicate answer suppressed", { peer: senderSummary(pc) });
        return;
      }
      await pc.setRemoteDescription({ type: "answer", sdp: String(payload.sdp || "") });
      state.answerReceived = true;
      logDiagnostic("answer applied", { peer: senderSummary(pc) });
      await applyPendingCandidates();
      updateStageDiagnostics();
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
      updateStageDiagnostics();
    } else if (signal.signal_type === "ice_gathering_complete") {
      state.iceGatheringState = "complete";
      logDiagnostic("remote ICE gathering complete", { peer: senderSummary(state.pc) });
      updateStageDiagnostics();
      return;
    } else if (signal.signal_type === "media_state") {
      state.remoteCameraSignaled = Boolean(payload.camera);
      state.remoteMicSignaled = Boolean(payload.mic);
      logDiagnostic("remote media_state received", {
        signaled: { camera: state.remoteCameraSignaled, mic: state.remoteMicSignaled },
        remoteVideoTracks: streamTracks(state.remoteVideoStream, "video").map(summarizeTrack),
        remoteAudioTracks: streamTracks(state.remoteAudioStream, "audio").map(summarizeTrack)
      });
      updateMediaIndicators();
      updateEmptyState();
      updateStageDiagnostics();
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
    clearCameraHealthProbe();
    localVideo.srcObject = null;
    remoteVideo.srcObject = null;
    remoteAudio.srcObject = null;
    safeStopTracks(state.localStream);
    safeStopTracks(state.remoteVideoStream);
    safeStopTracks(state.remoteAudioStream);
    state.pc = null;
    state.peerRoomId = "";
    state.localStream = null;
    state.remoteVideoStream = null;
    state.remoteAudioStream = null;
    state.started = false;
    state.acquiring = false;
    state.attached = false;
    state.cameraEnabled = true;
    state.cameraIntent = true;
    state.micEnabled = true;
    state.cameraAvailable = false;
    state.micAvailable = false;
    state.remoteCameraEnabled = false;
    state.remoteMicEnabled = false;
    state.remoteCameraSignaled = null;
    state.remoteMicSignaled = null;
    state.remoteCameraTrackLive = false;
    state.remoteMicTrackLive = false;
    state.ontrackCount = 0;
    state.soundBlocked = false;
    state.mediaDiagnostics = null;
    state.diagnosticText = "";
    state.offerSent = false;
    state.offerReceived = false;
    state.answerSent = false;
    state.answerReceived = false;
    state.iceGatheringState = "";
    state.iceConnectionState = "";
    state.peerConnectionState = "";
    state.pendingCandidates = [];
    state.pendingSignals = [];
    state.reconnecting = false;
    state.reconnectAttempts = 0;
    state.cameraDevices = [];
    state.selectedDeviceId = "";
    state.currentDeviceId = "";
    state.cameraHealth = "unknown";
    state.cameraFramesOk = false;
    state.cameraFrameCount = 0;
    state.cameraFrameStart = 0;
    state.localPreviewReady = false;
    state.recoveringVideo = false;
    state.videoRecoveryAttempts = 0;
    state.watchedTracks = new WeakSet();
    cameraSelect.innerHTML = "";
    const defaultOption = root.ownerDocument.createElement("option");
    defaultOption.value = "";
    defaultOption.textContent = "Default camera";
    cameraSelect.appendChild(defaultOption);
    cameraSelect.value = "";
    if (messageEl) {
      messageEl.textContent = "Live calls are not recorded.";
    }
    updateMediaIndicators();
    updateEmptyState();
    updateStageDiagnostics();
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
        const detail = safeMessage(error, "Camera or microphone capture failed.");
        setStatus(detail, "bad");
        state.diagnosticText = detail;
        if (messageEl) {
          messageEl.textContent = detail;
        }
        updateButtons();
      } finally {
        state.joining = false;
        updateButtons();
      }
    });
    leaveButton.addEventListener("click", () => {
      emitSignal("leave", {});
      cleanup(false);
      setStatus("You left the call.", "warn");
      updateButtons();
    });
    cameraButton.addEventListener("click", () => {
      syncLocalMediaState();
      if (!state.cameraAvailable) {
        return;
      }
      const nextEnabled = !state.cameraIntent;
      state.cameraIntent = nextEnabled;
      if (state.localStream) {
        state.localStream.getVideoTracks().forEach((track) => {
          track.enabled = nextEnabled && String(track.readyState) === "live";
        });
      }
      syncLocalMediaState();
      emitMediaState();
      updateButtons();
      updateMediaIndicators();
      updateEmptyState();
      updateStageDiagnostics();
    });
    micButton.addEventListener("click", () => {
      syncLocalMediaState();
      if (!state.micAvailable) {
        return;
      }
      const nextEnabled = !state.micEnabled;
      if (state.localStream) {
        state.localStream.getAudioTracks().forEach((track) => {
          track.enabled = nextEnabled && String(track.readyState) === "live";
        });
      }
      syncLocalMediaState();
      emitMediaState();
      updateButtons();
      updateMediaIndicators();
      updateEmptyState();
    });
    soundButton.addEventListener("click", async () => {
      try {
        if (remoteVideo.srcObject && remoteVideo.paused) {
          await remoteVideo.play();
        }
        if (remoteAudio.srcObject && remoteAudio.paused) {
          await remoteAudio.play();
        }
        state.soundBlocked = false;
        soundButton.hidden = true;
      } catch (error) {
        setStatus("Sound could not be enabled. (" + safeErrorName(error) + ")", "bad");
      }
    });
    cameraSelect.addEventListener("change", (event) => {
      switchCamera(event.target.value);
    });
    if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
      deviceChangeHandler = () => {
        if (state.started) {
          populateCameraDevices();
        }
      };
      navigator.mediaDevices.addEventListener("devicechange", deviceChangeHandler);
    }
  }

  if (!state.pageHideHandler && typeof window !== "undefined" && window.addEventListener) {
    state.pageHideHandler = () => {
      emitPresenceEnd();
    };
    window.addEventListener("pagehide", state.pageHideHandler);
  }

  if (titleEl) {
    titleEl.textContent = String(cfg.roomTitle || "Live interview");
  }
  if (localLabelEl) {
    localLabelEl.textContent = String(cfg.localName || "You") + (cfg.localRole ? " · " + String(cfg.localRole) : "");
  }
  if (remoteLabelEl) {
    remoteLabelEl.textContent = String(cfg.remoteName || "Remote participant") + (cfg.remoteRole ? " · " + String(cfg.remoteRole) : "");
  }
  root.dataset.localRole = String(cfg.localRole || "unknown");
  root.dataset.remoteRole = String(cfg.remoteRole || "unknown");
  logDiagnostic("component rendered", {
    roomId: String(cfg.roomId || ""),
    enabled: Boolean(cfg.enabled),
    initiator: Boolean(cfg.initiator),
    peer: senderSummary(state.pc)
  });

  if (!cfg.enabled && state.started) {
    cleanup(false);
    setStatus("Call is no longer available.", "warn");
  }
  if (!state.started) {
    setStatus(cfg.enabled ? "Ready to join" : "Call unavailable", cfg.enabled ? "warn" : "bad");
  }
  updateButtons();
  updateMediaIndicators();
  updateEmptyState();

  const incoming = Array.isArray(cfg.incoming) ? cfg.incoming : [];
  for (const signal of incoming) {
    if (!signal || wasProcessed(signal.id)) {
      continue;
    }
    state.signalChain = state.signalChain.then(async () => {
      if (!state.started && signal.signal_type !== "media_state") {
        state.pendingSignals.push(signal);
      } else {
        await handleSignal(signal);
      }
      markProcessed(signal.id);
      updateStageDiagnostics();
    }).catch((error) => {
      setStatus("A signaling stage failed. Retrying on the next refresh.", "bad");
      logDiagnostic("signal stage failed", {
        stage: String(signal.signal_type || ""),
        error: safeErrorName(error)
      });
    });
  }

  if (messageEl) {
    messageEl.textContent = state.diagnosticText
      || "Peer-to-peer media is encrypted by WebRTC. Signaling is authenticated; live calls are not recorded.";
  }
  updateStageDiagnostics();

  return () => {
    if (deviceChangeHandler && navigator.mediaDevices && navigator.mediaDevices.removeEventListener) {
      navigator.mediaDevices.removeEventListener("devicechange", deviceChangeHandler);
    }
    if (state.pageHideHandler && typeof window !== "undefined" && window.removeEventListener) {
      window.removeEventListener("pagehide", state.pageHideHandler);
      state.pageHideHandler = null;
    }
    logDiagnostic("component unmounted", { peer: senderSummary(state.pc) });
    cleanup(false);
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


def _participant_labels(room: dict[str, Any], role: str) -> tuple[str, str, str, str]:
    if role == calls.ROLE_ADMIN:
        return (
            str(room.get("admin_name") or "Administrator"),
            "Administrator",
            str(room.get("candidate_name") or "Candidate"),
            "Candidate",
        )
    return (
        str(room.get("candidate_name") or "Candidate"),
        "Candidate",
        str(room.get("admin_name") or "Administrator"),
        "Administrator",
    )


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
    processed = 0
    for event in events:
        processed += 1
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "")
        payload = event.get("payload")
        if kind == "presence_end":
            try:
                release_media_session(
                    room_id=room_id,
                    user_id=user_id,
                    session_token=session_token,
                )
            except calls.LiveCallError:
                pass
            except Exception:
                pass
            continue
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
    raw_state["outbox_ack"] = int(raw_state.get("outbox_ack") or 0) + processed


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
    if role == calls.ROLE_ADMIN:
        recipient_role = calls.ROLE_CANDIDATE
    elif role == calls.ROLE_CANDIDATE:
        recipient_role = calls.ROLE_ADMIN
    else:
        st.error("This live call has an invalid participant role.", icon=":material/error:")
        return
    local_name, local_role, remote_name, remote_role = _participant_labels(room, role)
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
            "roomId": room_id,
            "callStatus": str(room.get("status") or ""),
            "enabled": bool(enabled),
            "outboxAck": outbox_ack,
            "incoming": incoming,
            "roomTitle": str(room.get("title") or "Live interview"),
            "localName": local_name,
            "localRole": local_role,
            "remoteName": remote_name,
            "remoteRole": remote_role,
        },
        default={"outbox": [], "outbox_ack": 0},
        on_outbox_change=on_outbox_change,
        on_outbox_ack_change=lambda: None,
    )
    error = str(st.session_state.get(_error_key(room_id)) or "")
    if error:
        st.error(error, icon=":material/error:")
