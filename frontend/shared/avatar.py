from __future__ import annotations

from html import escape

import streamlit as st
from services.ai_avatar_session import (
    CANCELLED,
    COMPLETED,
    ERROR,
    IDLE,
    LISTENING,
    PAUSED,
    PROCESSING,
    SETUP,
    SPEAKING,
    WAITING,
)

STATUS_META: dict[str, tuple[str, str]] = {
    IDLE: ("Ready", "idle"),
    SETUP: ("Preparing", "idle"),
    SPEAKING: ("Speaking", "speaking"),
    LISTENING: ("Listening", "listening"),
    PROCESSING: ("Thinking", "processing"),
    WAITING: ("Waiting for your response", "waiting"),
    PAUSED: ("Paused", "paused"),
    COMPLETED: ("Complete", "completed"),
    ERROR: ("Needs attention", "error"),
    CANCELLED: ("Stopped", "paused"),
}

_AVATAR_CSS = """
<style>
.ai-avatar-card {
  border: 1px solid rgba(120, 140, 160, 0.25);
  border-radius: 18px;
  padding: 18px;
  background: linear-gradient(135deg, rgba(30, 64, 92, 0.12), rgba(80, 40, 110, 0.08));
  display: flex;
  align-items: center;
  gap: 18px;
  margin-bottom: 12px;
}
.ai-avatar-face {
  width: 86px;
  height: 86px;
  border-radius: 50%;
  background: radial-gradient(circle at 35% 30%, #d8ecff 0%, #7fb2e8 55%, #4f6fd0 100%);
  border: 3px solid rgba(255, 255, 255, 0.65);
  box-shadow: 0 8px 26px rgba(35, 70, 120, 0.28);
  position: relative;
  flex: 0 0 auto;
}
.ai-avatar-eye {
  width: 9px;
  height: 12px;
  border-radius: 50%;
  background: #17324d;
  position: absolute;
  top: 31px;
}
.ai-avatar-eye.left { left: 24px; }
.ai-avatar-eye.right { right: 24px; }
.ai-avatar-mouth {
  width: 30px;
  height: 8px;
  border-radius: 0 0 18px 18px;
  background: #17324d;
  position: absolute;
  left: 25px;
  top: 55px;
  transform-origin: center top;
}
.ai-avatar-card[data-state="speaking"] .ai-avatar-mouth {
  animation: avatar-mouth 0.36s ease-in-out infinite alternate;
}
.ai-avatar-card[data-state="listening"] .ai-avatar-face {
  animation: avatar-pulse 1.1s ease-in-out infinite;
}
.ai-avatar-card[data-state="processing"] .ai-avatar-face {
  animation: avatar-think 1.4s ease-in-out infinite;
}
.ai-avatar-card[data-state="error"] .ai-avatar-face {
  filter: saturate(0.55);
}
.ai-avatar-copy { min-width: 0; }
.ai-avatar-role {
  font-size: 0.78rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  opacity: 0.7;
}
.ai-avatar-status {
  font-size: 1.05rem;
  font-weight: 700;
  margin: 3px 0 6px;
}
.ai-avatar-line {
  font-size: 0.92rem;
  opacity: 0.88;
  max-width: 620px;
}
.ai-avatar-bars {
  display: flex;
  gap: 4px;
  align-items: flex-end;
  height: 24px;
  margin-top: 8px;
}
.ai-avatar-bars span {
  width: 4px;
  height: 7px;
  border-radius: 3px;
  background: #5b8def;
}
.ai-avatar-card[data-state="speaking"] .ai-avatar-bars span {
  animation: avatar-bar 0.7s ease-in-out infinite alternate;
}
.ai-avatar-card[data-state="speaking"] .ai-avatar-bars span:nth-child(2) { animation-delay: 0.1s; }
.ai-avatar-card[data-state="speaking"] .ai-avatar-bars span:nth-child(3) { animation-delay: 0.2s; }
.ai-avatar-card[data-state="speaking"] .ai-avatar-bars span:nth-child(4) { animation-delay: 0.3s; }
@keyframes avatar-mouth {
  from { transform: scaleY(0.6); }
  to { transform: scaleY(2.2); }
}
@keyframes avatar-pulse {
  0%, 100% { transform: scale(1); box-shadow: 0 8px 26px rgba(35, 70, 120, 0.28); }
  50% { transform: scale(1.05); box-shadow: 0 0 0 8px rgba(91, 141, 239, 0.14); }
}
@keyframes avatar-think {
  0%, 100% { transform: rotate(-2deg); }
  50% { transform: rotate(2deg); }
}
@keyframes avatar-bar {
  from { height: 6px; opacity: 0.55; }
  to { height: 22px; opacity: 1; }
}
</style>
"""


def avatar_status_label(status: str) -> str:
    return STATUS_META.get(str(status), STATUS_META[IDLE])[0]


def render_ai_avatar(status: str, question_text: str, key: str = "avatar") -> None:
    label, tone = STATUS_META.get(str(status), STATUS_META[IDLE])
    safe_label = escape(label)
    safe_question = escape(str(question_text or "Listening for the next question.")[:220])
    safe_key = escape(str(key), quote=True)
    st.html(
        _AVATAR_CSS
        + f"""<div class="ai-avatar-card" data-state="{tone}" data-avatar-key="{safe_key}">
  <div class="ai-avatar-face" aria-hidden="true">
    <span class="ai-avatar-eye left"></span>
    <span class="ai-avatar-eye right"></span>
    <span class="ai-avatar-mouth"></span>
  </div>
  <div class="ai-avatar-copy">
    <div class="ai-avatar-role">AI interviewer</div>
    <div class="ai-avatar-status">{safe_label}</div>
    <div class="ai-avatar-line">{safe_question}</div>
    <div class="ai-avatar-bars" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
  </div>
</div>"""
    )
