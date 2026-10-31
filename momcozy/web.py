"""Web Dashboard and MJPEG Streaming Server for Momcozy BM04.

Provides an intuitive web interface inspired by the Momcozy app, with live video,
real-time sensor telemetry (temperature, alerts), and full remote control.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import socket
import ssl
import subprocess
import time
from typing import Any, Optional

# Disable aiohttp sendfile to prevent Python asyncio sslproto _set_app_protocol crash on client disconnect over HTTPS
os.environ.setdefault("AIOHTTP_NOSENDFILE", "1")

import aiohttp.web_fileresponse
aiohttp.web_fileresponse.NOSENDFILE = True

from aiohttp import web

from .constants import DP, LULLABY_TRACKS, NightVisionMode, PtzDirection
from .device import MomcozyMonitor
from .hls import HlsTranscoder
from .stream import VideoStreamer
from .timelapse import TimelapseRecorder

logger = logging.getLogger(__name__)


class SafeFileResponse(web.FileResponse):
    """FileResponse that streams chunks to prevent Python asyncio loop.sendfile SSL crashes."""

    async def _sendfile(
        self, request: Any, fobj: Any, offset: int, count: int
    ) -> Any:
        writer = await web.StreamResponse.prepare(self, request)
        assert writer is not None
        return await self._sendfile_fallback(writer, fobj, offset, count)

HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Momcozy BM04 Baby Monitor</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.ico">
  <script>
    if (location.hostname === '0.0.0.0') {
      location.hostname = 'localhost';
    }
    window.serverLanIp = '';
    window.serverPort = 8421;
    window.serverHttpPort = 8420;

    window.addEventListener('error', function(e) {
      console.error('[Window Error]', e.message, 'at', e.filename, ':', e.lineno, e.error);
    });
    window.addEventListener('unhandledrejection', function(e) {
      console.error('[Unhandled Promise Rejection]', e.reason);
    });

    window['__onGCastApiAvailable'] = function(isAvailable, reason) {
      console.log(`[Google Cast] __onGCastApiAvailable called: isAvailable=${isAvailable}, reason=${reason}`);
      if (isAvailable && window.cast && window.cast.framework) {
        try {
          const castContext = cast.framework.CastContext.getInstance();
          castContext.setOptions({
            receiverApplicationId: chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
            autoJoinPolicy: chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED
          });
          window.googleCastInitialized = true;
          console.log("[Google Cast] CastContext initialized successfully.");

          castContext.addEventListener(
            cast.framework.CastContextEventType.SESSION_STATE_CHANGED,
            function(event) {
              console.log("[Google Cast] Session state changed:", event.sessionState);
              const castBtn = document.getElementById('cast-btn');
              const castText = document.getElementById('cast-btn-text');

              switch (event.sessionState) {
                case cast.framework.SessionState.SESSION_STARTED:
                case cast.framework.SessionState.SESSION_RESUMED: {
                  if (castBtn) castBtn.classList.add('active');
                  if (castText) castText.innerText = 'Casting';
                  showToast('Connected to Cast device!', 'success', 3000);
                  const session = castContext.getCurrentSession();
                  if (session && typeof window.loadStreamToCastSession === 'function') {
                    window.loadStreamToCastSession(session);
                  }
                  break;
                }
                case cast.framework.SessionState.SESSION_STARTING: {
                  if (castBtn) castBtn.classList.remove('active');
                  if (castText) castText.innerText = 'Connecting...';
                  break;
                }
                case cast.framework.SessionState.SESSION_ENDING: {
                  if (castBtn) castBtn.classList.remove('active');
                  if (castText) castText.innerText = 'Disconnecting...';
                  break;
                }
                case cast.framework.SessionState.SESSION_ENDED:
                case cast.framework.SessionState.NO_SESSION: {
                  if (castBtn) castBtn.classList.remove('active');
                  if (castText) castText.innerText = 'Cast';
                  break;
                }
                case cast.framework.SessionState.SESSION_START_FAILED: {
                  if (castBtn) castBtn.classList.remove('active');
                  if (castText) castText.innerText = 'Cast';
                  showToast('Failed to connect to Cast device.', 'error', 4000);
                  break;
                }
              }
            }
          );
        } catch (e) {
          console.error("[Google Cast] Exception during CastContext initialization:", e);
        }
      } else {
        const isSecure = window.isSecureContext;
        console.warn(`[Google Cast] Cast API is not available (isAvailable=${isAvailable}). isSecureContext=${isSecure}, origin=${window.location.origin}`);
        if (!isSecure) {
          console.error("[Google Cast] Insecure Context: Chrome restricts Google Cast to HTTPS or localhost. On plain HTTP over LAN, Chrome disables the Cast Web Sender API.");
        }
      }
    };
  </script>
  <script src="https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1"></script>
  <style>
    :root {
      --primary: #FF7E98;
      --primary-dark: #E65A77;
      --bg: #F7F8FA;
      --card-bg: #FFFFFF;
      --text: #2D3748;
      --text-muted: #718096;
      --border: #E2E8F0;
      --accent-blue: #4299E1;
      --accent-green: #48BB78;
      --accent-orange: #ED8936;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 16px;
    }
    .container {
      max-width: 900px;
      margin: 0 auto;
      display: grid;
      grid-gap: 16px;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 12px 16px;
      background: var(--card-bg);
      border-radius: 12px;
      border: 1px solid var(--border);
    }
    header h1 {
      font-size: 1.25rem;
      font-weight: 700;
      color: var(--primary);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .badge {
      display: inline-block;
      padding: 4px 8px;
      font-size: 0.75rem;
      font-weight: 600;
      border-radius: 6px;
      background: #EBF8FF;
      color: var(--accent-blue);
    }
    .badge.active { background: #F0FFF4; color: var(--accent-green); }
    .badge.alert { background: #FFF5F5; color: #E53E3E; }

    /* Video Player */
    .video-card {
      background: #000;
      border-radius: 16px;
      overflow: hidden;
      position: relative;
      aspect-ratio: 16 / 9;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    }
    .video-card img,
    .video-card canvas {
      width: 100%;
      height: 100%;
      object-fit: contain;
    }
    .video-overlay {
      position: absolute;
      bottom: 12px;
      left: 12px;
      background: rgba(0,0,0,0.65);
      backdrop-filter: blur(4px);
      -webkit-backdrop-filter: blur(4px);
      color: #fff;
      padding: 4px 8px;
      border-radius: 12px;
      font-size: 0.72rem;
      font-weight: 600;
      letter-spacing: 0.3px;
      display: flex;
      align-items: center;
      gap: 5px;
      border: 1px solid rgba(255,255,255,0.15);
      z-index: 10;
      pointer-events: none;
    }
    .video-ctrls-group {
      position: absolute;
      bottom: 12px;
      right: 12px;
      display: flex;
      gap: 6px;
      z-index: 10;
    }
    .video-ctrl-btn {
      background: rgba(0,0,0,0.65);
      backdrop-filter: blur(4px);
      -webkit-backdrop-filter: blur(4px);
      color: #fff;
      border: 1px solid rgba(255,255,255,0.25);
      border-radius: 8px;
      padding: 5px 9px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 5px;
      font-size: 0.72rem;
      font-weight: 600;
      transition: background 0.15s, transform 0.15s;
      line-height: 1;
    }
    .video-ctrl-btn:hover {
      background: rgba(0,0,0,0.85);
      border-color: rgba(255,255,255,0.4);
    }
    .video-ctrl-btn:active {
      transform: scale(0.95);
    }
    .video-ctrl-btn.active {
      background: rgba(72, 187, 120, 0.75);
      border-color: rgba(72, 187, 120, 0.9);
      color: #fff;
    }
    /* Fullscreen styling for standard API and CSS fallback (iOS) */
    .video-card:fullscreen,
    .video-card:-webkit-full-screen {
      width: 100vw;
      height: 100vh;
      border-radius: 0;
      background: #000;
    }
    .video-card.is-fullscreen {
      position: fixed !important;
      top: 0 !important;
      left: 0 !important;
      width: 100vw !important;
      height: 100vh !important;
      z-index: 999999 !important;
      border-radius: 0 !important;
      margin: 0 !important;
      background: #000 !important;
      aspect-ratio: auto !important;
    }
    .video-card:fullscreen img,
    .video-card:-webkit-full-screen img,
    .video-card.is-fullscreen img,
    .video-card:fullscreen canvas,
    .video-card:-webkit-full-screen canvas,
    .video-card.is-fullscreen canvas {
      width: 100%;
      height: 100%;
      object-fit: contain;
    }
    .live-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #E53E3E;
      animation: pulse 1.5s infinite;
    }
    @keyframes pulse {
      0% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(1.2); }
      100% { opacity: 1; transform: scale(1); }
    }

    /* Telemetry grid */
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      grid-gap: 16px;
    }
    .card {
      background: var(--card-bg);
      border-radius: 12px;
      padding: 16px;
      border: 1px solid var(--border);
    }
    .card h2 {
      font-size: 1rem;
      font-weight: 600;
      margin-bottom: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .temp-display {
      font-size: 2.25rem;
      font-weight: 700;
      color: var(--text);
      display: flex;
      align-items: baseline;
      gap: 8px;
    }
    .temp-display span {
      font-size: 1.1rem;
      font-weight: 400;
      color: var(--text-muted);
    }

    /* Controls */
    .ptz-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      grid-gap: 8px;
      max-width: 200px;
      margin: 8px auto;
    }
    .btn {
      background: #EDF2F7;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 14px;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: all 0.2s;
      user-select: none;
      -webkit-user-select: none;
      touch-action: manipulation;
    }
    .btn:hover { background: #E2E8F0; }
    .btn:active { transform: scale(0.96); }
    .btn.primary { background: var(--primary); color: #fff; border-color: var(--primary-dark); }
    .btn.primary:hover { background: var(--primary-dark); }

    .control-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 8px 0;
      border-bottom: 1px solid #EDF2F7;
    }
    .control-row:last-child { border-bottom: none; }
    select, input[type="range"] {
      padding: 6px 10px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: #fff;
    }

    /* Video error overlay */
    .video-error-overlay {
      position: absolute;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(15, 23, 42, 0.88);
      backdrop-filter: blur(4px);
      color: #fff;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 24px;
      text-align: center;
      z-index: 10;
      gap: 10px;
      transition: opacity 0.3s ease;
    }
    .video-error-overlay.hidden { display: none; }

    /* Toast notifications */
    #toast-container {
      position: fixed;
      top: 20px;
      right: 20px;
      z-index: 9999;
      display: flex;
      flex-direction: column;
      gap: 10px;
      max-width: 420px;
      pointer-events: none;
    }
    .toast {
      pointer-events: auto;
      background: #FFFFFF;
      border-left: 5px solid #E53E3E;
      box-shadow: 0 4px 14px rgba(0,0,0,0.18);
      border-radius: 8px;
      padding: 12px 16px;
      display: flex;
      align-items: flex-start;
      gap: 10px;
      font-size: 0.88rem;
      color: #2D3748;
      animation: slideIn 0.3s ease-out;
      transition: opacity 0.3s ease-out, transform 0.3s ease-out;
    }
    .toast.error { border-left-color: #E53E3E; }
    .toast.warn { border-left-color: #ED8936; }
    .toast.info { border-left-color: #4299E1; }
    .toast.success { border-left-color: #48BB78; }
    @keyframes slideIn {
      from { transform: translateX(100%); opacity: 0; }
      to { transform: translateX(0); opacity: 1; }
    }

    /* Transport badges */
    .transport-pill {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 8px;
      font-size: 0.72rem;
      font-weight: 600;
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.2);
      color: #fff;
      margin-left: 6px;
      letter-spacing: 0.2px;
    }
    .transport-pill.cloud {
      background: rgba(66, 153, 225, 0.35);
      border: 1px solid rgba(66, 153, 225, 0.55);
      color: #EBF8FF;
    }
    .transport-pill.local {
      background: rgba(72, 187, 120, 0.35);
      border: 1px solid rgba(72, 187, 120, 0.55);
      color: #F0FFF4;
    }
  </style>
</head>
<body>
  <div id="toast-container"></div>
  <div class="container">
    <header>
      <div>
        <h1>🍼 Momcozy BM04 Monitor</h1>
        <div style="font-size: 0.76rem; color: var(--text-muted); margin-top: 3px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
          <span>Control: <strong id="hdr-control-transport" style="color:var(--text);">Checking...</strong> <span id="hdr-control-diag" style="color:var(--text-muted); font-size:0.7rem; display:none;"></span></span>
          <span>&bull;</span>
          <span>Stream: <strong id="hdr-stream-transport" style="color:var(--text);">Cloud Relay (Tuya P2P)</strong></span>
        </div>
      </div>
      <span id="conn-badge" class="badge active">Connected</span>
    </header>

    <div class="video-card">
      <div class="video-overlay" id="video-overlay">
        <div class="live-dot" id="live-dot"></div>
        <span id="stream-badge">LIVE STREAM</span>
      </div>
      <div class="video-ctrls-group">
        <button class="video-ctrl-btn" id="audio-btn" onclick="toggleAudio()" title="Live Audio">
          <svg id="audio-off-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>
          <svg id="audio-on-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="display:none;"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>
          <span id="audio-btn-text">Audio</span>
        </button>
        <button class="video-ctrl-btn" id="cast-btn" onclick="handleCastClick(event)" title="Cast to TV / Chromecast">
          <svg id="cast-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M2 16.1A5 5 0 0 1 5.9 20M2 12.05A9 9 0 0 1 9.95 20M2 8V6a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-6"/>
            <line x1="2" y1="20" x2="2.01" y2="20"/>
          </svg>
          <span id="cast-btn-text">Cast</span>
        </button>
        <button class="video-ctrl-btn" id="timelapse-ctrl-btn" onclick="openTimelapsePlayback()" title="Watch 12-Hour Timelapse">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
          <span id="timelapse-ctrl-text">Timelapse</span>
        </button>
        <button class="video-ctrl-btn" id="fs-btn" onclick="toggleFullscreen()" title="Toggle Fullscreen">
          <svg id="fs-enter-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>
          <svg id="fs-exit-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="display:none;"><path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/></svg>
          <span id="fs-btn-text">Fullscreen</span>
        </button>
      </div>
      <canvas id="stream-canvas" style="display:none;"></canvas>
      <img id="stream-img" alt="Camera Stream" style="display:none;">
      <audio id="live-audio" preload="none"></audio>
      <div id="video-error" class="video-error-overlay hidden">
        <div style="font-size: 2.2rem;">⚠️</div>
        <div id="video-error-title" style="font-weight: 700; font-size: 1.05rem;">Live Stream Offline</div>
        <div id="video-error-detail" style="font-size: 0.85rem; color: #E2E8F0; max-width: 90%; line-height: 1.4;"></div>
        <button class="btn primary" onclick="retryStream()" style="margin-top: 8px;">🔄 Retry Stream</button>
      </div>
    </div>

    <div class="grid">
      <!-- Sensor Telemetry -->
      <div class="card">
        <h2>🌡️ Environment Sensors <span class="badge" id="temp-badge">Normal</span></h2>
        <div class="temp-display" id="temp-val">--.-°C <span>(--.-°F)</span></div>
        <div style="margin-top: 12px;">
          <div class="control-row">
            <span>Baby Crying Alert</span>
            <span id="cry-stat" class="badge">No</span>
          </div>
          <div class="control-row">
            <span>Motion Detection</span>
            <span id="motion-stat" class="badge">Clear</span>
          </div>
          <div class="control-row">
            <span>Night Vision</span>
            <span id="nv-stat" class="badge">Auto</span>
          </div>
          <div class="control-row">
            <span>Privacy Mode</span>
            <span id="priv-stat" class="badge">Off</span>
          </div>
        </div>
      </div>

      <!-- PTZ Pan & Tilt -->
      <div class="card">
        <h2>🕹️ Camera Pan & Tilt</h2>
        <div class="ptz-grid">
          <div></div>
          <button class="btn" onclick="ptzStep('0')">▲</button>
          <div></div>
          <button class="btn" onclick="ptzStep('6')">◀</button>
          <button class="btn" onclick="ptzStop()">⏹</button>
          <button class="btn" onclick="ptzStep('2')">▶</button>
          <div></div>
          <button class="btn" onclick="ptzStep('4')">▼</button>
          <div></div>
        </div>
        <div style="text-align: center; margin-top: 8px;">
          <a href="/snapshot" target="_blank" class="btn" style="text-decoration:none; display:inline-flex;">📷 Snapshot</a>
        </div>
      </div>

      <!-- Lullaby Controls -->
      <div class="card">
        <h2>🎵 Soothing Lullaby</h2>
        <div class="control-row">
          <span>Track</span>
          <select id="lullaby-track" onchange="changeLullaby()">
            <option value="0">Brahms' Lullaby</option>
            <option value="1">Hush Little Baby</option>
            <option value="2">London Bridge</option>
            <option value="3">Twinkle Twinkle</option>
            <option value="4">White Noise</option>
            <option value="5">Heartbeat</option>
            <option value="6">Rainforest</option>
            <option value="7">Gentle Stream</option>
          </select>
        </div>
        <div class="control-row">
          <span>Volume</span>
          <input type="range" id="lullaby-vol" min="0" max="100" value="50" onchange="setLullabyVol(this.value)">
        </div>
        <div style="display:flex; gap:8px; margin-top:12px;">
          <button class="btn primary" style="flex:1;" onclick="toggleLullaby(true)">▶ Play</button>
          <button class="btn" style="flex:1;" onclick="toggleLullaby(false)">⏹ Stop</button>
        </div>
      </div>

      <!-- Settings -->
      <div class="card">
        <h2>⚙️ Monitor Settings</h2>
        <div class="control-row">
          <span>Night Vision</span>
          <select id="nv-select" onchange="setNightVision(this.value)">
            <option value="0">Auto</option>
            <option value="1">Always Off</option>
            <option value="2">Always On</option>
          </select>
        </div>
        <div class="control-row">
          <span>Privacy / Sleep Mode</span>
          <button class="btn" id="priv-btn" onclick="togglePrivacy()">Toggle</button>
        </div>
        <div class="control-row">
          <span>LED Indicator Light</span>
          <button class="btn" onclick="toggleLed()">Toggle LED</button>
        </div>
        <div class="control-row">
          <span>Flip 180° (Crib mount)</span>
          <button class="btn" onclick="toggleFlip()">Flip</button>
        </div>
      </div>

      <!-- Detection Snapshot Card -->
      <div class="card">
        <h2>📸 Detection Snapshot <span class="badge" id="detect-badge">Event Log</span></h2>
        <div style="position:relative; width:100%; aspect-ratio:16/9; background:#1A202C; border-radius:8px; overflow:hidden; margin:8px 0 12px 0; display:flex; align-items:center; justify-content:center; box-shadow: inset 0 2px 4px rgba(0,0,0,0.3);">
          <img id="detect-img" src="/detection_snapshot.jpg" alt="Latest Detection Snapshot" style="width:100%; height:100%; object-fit:contain; cursor:pointer;" onclick="viewFullDetectSnapshot()" title="Click to view full size">
          <div id="detect-empty" style="display:none; color:#A0AEC0; font-size:0.85rem; text-align:center; padding:12px;">No detection event recorded yet</div>
        </div>
        <div class="control-row">
          <span>Trigger Event</span>
          <span id="detect-trigger" class="badge" style="background:#EDF2F7; color:var(--text);">Sound / Cry Alert</span>
        </div>
        <div class="control-row">
          <span>Recorded At</span>
          <span id="detect-time" style="font-size:0.85rem; font-weight:600; color:var(--text);">Checking...</span>
        </div>
        <div class="control-row" id="detect-cloud-row" style="display:none;">
          <span>Camera Event File</span>
          <span id="detect-cloud-file" style="font-family:monospace; font-size:0.72rem; color:var(--text-muted); max-width:170px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"></span>
        </div>
        <div style="display:flex; gap:8px; margin-top:12px;">
          <button class="btn" style="flex:1; font-size:0.82rem;" onclick="viewFullDetectSnapshot()">🔍 Full Size</button>
          <button class="btn primary" style="flex:1; font-size:0.82rem;" onclick="captureLiveSnapshot()">📷 Capture Now</button>
        </div>
      </div>

      <!-- Timelapse Card -->
      <div class="card" id="timelapse-card">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
          <h2 style="margin:0; display:flex; align-items:center; gap:8px;">
            <span>⏱️ Timelapse</span>
            <select id="timelapse-ratio-select" onchange="changeTimelapseRatio(this.value)" style="background:rgba(66,153,225,0.12); color:var(--accent-blue); font-size:0.75rem; font-weight:600; padding:2px 6px; border-radius:6px; border:1px solid rgba(66,153,225,0.3); outline:none; cursor:pointer;" title="Select Timelapse Speed Ratio">
              <option value="15">1:15</option>
              <option value="30">1:30</option>
              <option value="60" selected>1:60</option>
              <option value="90">1:90</option>
            </select>
          </h2>
          <span class="badge" id="timelapse-badge">Stopped</span>
        </div>
        <div class="control-row">
          <span>Time Recorded</span>
          <span style="font-weight:600; font-size:0.85rem;">
            <span id="timelapse-buffered">0m 00s</span>
            <span style="color:var(--text-muted); font-weight:normal; font-size:0.8rem;"> (<span id="timelapse-vlen">0:00</span> video)</span>
          </span>
        </div>
        <div class="control-row">
          <span>Motion Events</span>
          <span id="timelapse-events-badge" class="badge" style="background:rgba(255,255,255,0.08); color:var(--text-muted); font-size:0.75rem;">0 events</span>
        </div>
        <div class="control-row">
          <span>Storage</span>
          <span id="timelapse-storage" style="font-size:0.85rem; font-weight:600;">0.0 MB</span>
        </div>
        <div style="display:flex; gap:8px; margin-top:12px;">
          <button class="btn primary" id="timelapse-toggle-btn" style="flex:1.2;" onclick="toggleTimelapseRecord()">⏺ Start Recording</button>
          <button class="btn" id="timelapse-watch-btn" style="flex:1;" onclick="openTimelapsePlayback()" disabled>▶ Watch</button>
          <button class="btn" id="timelapse-erase-btn" style="flex:0.8; background:rgba(239,68,68,0.12); color:#ef4444; border:1px solid rgba(239,68,68,0.3); font-size:0.85rem;" onclick="eraseTimelapse()" disabled title="Erase all recorded timelapse footage">🗑️ Erase</button>
        </div>
        <div style="margin-top:8px;">
          <button id="timelapse-sync-nv-btn" class="btn" style="width:100%; font-size:0.82rem; transition:all 0.2s;" onclick="toggleNightVisionSync()" title="Automatically start/resume timelapse on night vision (>=60s) and stop on daylight (>=120s)">🌙 Sync with Night Vision: OFF</button>
        </div>
      </div>

    </div>
  </div>

  <script>
    function showToast(message, type = 'error', duration = 5000) {
      const container = document.getElementById('toast-container');
      if (!container) return;
      const toast = document.createElement('div');
      toast.className = `toast ${type}`;
      const icon = type === 'error' ? '❌' : (type === 'warn' ? '⚠️' : (type === 'success' ? '✓' : 'ℹ️'));
      toast.innerHTML = `<span style="font-size:1.1rem; line-height:1;">${icon}</span><div style="flex:1;">${message}</div><button style="background:none;border:none;cursor:pointer;font-weight:bold;color:#718096;font-size:1rem;line-height:1;" onclick="this.parentElement.remove()">✕</button>`;
      container.appendChild(toast);
      setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(20px)';
        setTimeout(() => toast.remove(), 300);
      }, duration);
    }

    async function apiPost(action, data={}) {
      try {
        const res = await fetch('/api/control', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({action, ...data})
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok || json.status === 'error') {
          const errDetail = json.error || `Server returned error (${res.status})`;
          showToast(`Action failed: ${errDetail}`, 'error');
          return false;
        }
        return true;
      } catch(e) {
        console.error(e);
        showToast(`Request failed: ${e.message}`, 'error');
        return false;
      }
    }

    let lastDetectTime = null;
    function viewFullDetectSnapshot() {
      window.open('/detection_snapshot.jpg?t=' + Date.now(), '_blank');
    }

    async function captureLiveSnapshot() {
      showToast('Capturing snapshot from live camera...', 'info', 1500);
      const ok = await apiPost('capture_detection_snapshot');
      if (ok) {
        showToast('Detection snapshot captured!', 'success', 2000);
        const detectImg = document.getElementById('detect-img');
        if (detectImg) detectImg.src = '/detection_snapshot.jpg?t=' + Date.now();
        updateStatus();
      }
    }

    let ptzBusy = false;
    async function ptzStep(dir) {
      if (ptzBusy) return;
      ptzBusy = true;
      try {
        await apiPost('ptz_step', {direction: dir, duration: 0.175});
      } finally {
        setTimeout(() => { ptzBusy = false; }, 175);
      }
    }

    function ptz(dir) { apiPost('ptz_move', {direction: dir}); }
    function ptzStop() { apiPost('ptz_stop'); }

    // Fullscreen support with mobile iOS fallback
    function isFullscreen() {
      const card = document.querySelector('.video-card');
      return !!(
        document.fullscreenElement ||
        document.webkitFullscreenElement ||
        document.mozFullScreenElement ||
        document.msFullscreenElement ||
        (card && card.classList.contains('is-fullscreen'))
      );
    }

    function updateFullscreenIcons(active) {
      const enterIcon = document.getElementById('fs-enter-icon');
      const exitIcon = document.getElementById('fs-exit-icon');
      const btnText = document.getElementById('fs-btn-text');
      if (enterIcon) enterIcon.style.display = active ? 'none' : 'block';
      if (exitIcon) exitIcon.style.display = active ? 'block' : 'none';
      if (btnText) btnText.innerText = active ? 'Exit' : 'Fullscreen';
    }

    async function toggleFullscreen() {
      const card = document.querySelector('.video-card');
      if (!card) return;
      const active = isFullscreen();

      if (!active) {
        try {
          if (card.requestFullscreen) {
            await card.requestFullscreen();
          } else if (card.webkitRequestFullscreen) {
            await card.webkitRequestFullscreen();
          } else if (card.mozRequestFullScreen) {
            await card.mozRequestFullScreen();
          } else if (card.msRequestFullscreen) {
            await card.msRequestFullscreen();
          } else {
            card.classList.add('is-fullscreen');
            updateFullscreenIcons(true);
          }
        } catch (e) {
          console.warn('Native fullscreen request failed, falling back to CSS fullscreen:', e);
          card.classList.add('is-fullscreen');
          updateFullscreenIcons(true);
        }
      } else {
        if (card.classList.contains('is-fullscreen')) {
          card.classList.remove('is-fullscreen');
          updateFullscreenIcons(false);
        }
        try {
          if (document.exitFullscreen) {
            await document.exitFullscreen();
          } else if (document.webkitExitFullscreen) {
            await document.webkitExitFullscreen();
          } else if (document.mozCancelFullScreen) {
            await document.mozCancelFullScreen();
          } else if (document.msExitFullscreen) {
            await document.msExitFullscreen();
          }
        } catch (e) {
          console.warn('Exit fullscreen error:', e);
        }
      }
    }

    ['fullscreenchange', 'webkitfullscreenchange', 'mozfullscreenchange', 'MSFullscreenChange'].forEach(ev => {
      document.addEventListener(ev, () => {
        const active = isFullscreen();
        updateFullscreenIcons(active);
        const card = document.querySelector('.video-card');
        if (!active && card && card.classList.contains('is-fullscreen')) {
          card.classList.remove('is-fullscreen');
        }
      });
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        const card = document.querySelector('.video-card');
        if (card && card.classList.contains('is-fullscreen')) {
          card.classList.remove('is-fullscreen');
          updateFullscreenIcons(false);
        }
      }
    });

    // Live Audio streaming
    let audioActive = false;
    const audioElement = document.getElementById('live-audio');

    async function toggleAudio() {
      const audioBtn = document.getElementById('audio-btn');
      const offIcon = document.getElementById('audio-off-icon');
      const onIcon = document.getElementById('audio-on-icon');
      const audioText = document.getElementById('audio-btn-text');
      if (!audioElement) return;

      if (!audioActive) {
        audioElement.src = '/audio?t=' + Date.now();
        try {
          await audioElement.play();
          audioActive = true;
          if (audioBtn) audioBtn.classList.add('active');
          if (offIcon) offIcon.style.display = 'none';
          if (onIcon) onIcon.style.display = 'block';
          if (audioText) audioText.innerText = 'Listening';
          showToast('Live audio unmuted', 'info', 2000);
        } catch (e) {
          console.error('Audio play error:', e);
          showToast('Audio playback failed: ' + e.message, 'error');
        }
      } else {
        audioElement.pause();
        audioElement.removeAttribute('src');
        audioElement.load();
        audioActive = false;
        if (audioBtn) audioBtn.classList.remove('active');
        if (offIcon) offIcon.style.display = 'block';
        if (onIcon) onIcon.style.display = 'none';
        if (audioText) audioText.innerText = 'Audio';
        showToast('Live audio muted', 'info', 2000);
      }
    }

    if (audioElement) {
      audioElement.onerror = function() {
        if (audioActive) {
          console.warn('Audio stream error, stopping playback');
          audioElement.pause();
          audioActive = false;
          const audioBtn = document.getElementById('audio-btn');
          const offIcon = document.getElementById('audio-off-icon');
          const onIcon = document.getElementById('audio-on-icon');
          const audioText = document.getElementById('audio-btn-text');
          if (audioBtn) audioBtn.classList.remove('active');
          if (offIcon) offIcon.style.display = 'block';
          if (onIcon) onIcon.style.display = 'none';
          if (audioText) audioText.innerText = 'Audio';
          showToast('Live audio disconnected', 'warn', 3000);
        }
      };
    }

    // Stream player: WebCodecs (HEVC hardware pass-through) with automatic MJPEG fallback
    const streamImg = document.getElementById('stream-img');
    const streamCanvas = document.getElementById('stream-canvas');
    let streamMode = 'none'; // 'webcodecs' | 'mjpeg' | 'none'
    let streamRetryTimer = null;
    let streamRetryCount = 0;
    let decoderErrorCount = 0;
    let activeWs = null;
    let activeDecoder = null;
    let supportedHevcCodec = null;
    let isFallbackActive = false;
    let hasFatalDecoderError = false;

    function setStreamError(title, detail) {
      const streamBadge = document.getElementById('stream-badge');
      const liveDot = document.getElementById('live-dot');
      if (streamBadge) streamBadge.innerText = 'STREAM ERROR';
      if (liveDot) {
        liveDot.style.background = '#E53E3E';
        liveDot.style.animation = 'none';
      }
      const errOverlay = document.getElementById('video-error');
      if (errOverlay) {
        errOverlay.classList.remove('hidden');
        const titleEl = document.getElementById('video-error-title');
        const detailEl = document.getElementById('video-error-detail');
        if (titleEl) titleEl.innerText = title;
        if (detailEl) detailEl.innerText = detail;
      }
    }

    function cleanupDecoder() {
      if (activeDecoder) {
        try {
          activeDecoder.close();
        } catch (e) {}
        activeDecoder = null;
      }
    }

    function cleanupWebCodecs() {
      if (activeWs) {
        try {
          activeWs.onopen = null;
          activeWs.onmessage = null;
          activeWs.onerror = null;
          activeWs.onclose = null;
          activeWs.close();
        } catch (e) {}
        activeWs = null;
      }
      cleanupDecoder();
    }

    function fallbackToMjpeg(reason) {
      if (isFallbackActive) return;
      isFallbackActive = true;
      hasFatalDecoderError = true;
      console.warn('[Stream] Fallback to MJPEG triggered:', reason);
      if (reason && typeof showToast === 'function') {
        showToast('HEVC decoding unavailable: ' + reason + '. Switched to standard stream.', 'warn', 6000);
      }
      cleanupWebCodecs();
      startMjpegStream(reason);
    }

    function startMjpegStream(reason) {
      streamMode = 'mjpeg';
      if (streamCanvas) streamCanvas.style.display = 'none';
      if (streamImg) {
        streamImg.style.display = 'block';
        streamImg.src = '/stream?t=' + Date.now();
      }
      const streamBadge = document.getElementById('stream-badge');
      if (streamBadge) {
        streamBadge.innerText = 'LIVE STREAM (MJPEG)';
        streamBadge.title = reason ? `Standard MJPEG stream (Fallback: ${reason})` : 'Standard MJPEG stream';
      }
    }

    async function checkHevcWebCodecsSupport() {
      if (!window.isSecureContext) {
        console.warn('[WebCodecs] Insecure Context: Chrome disables VideoDecoder on plain HTTP unless accessed via localhost or 127.0.0.1.');
      }
      if (!('VideoDecoder' in window) || !('EncodedVideoChunk' in window)) {
        console.log('[WebCodecs] VideoDecoder or EncodedVideoChunk API not present in window. isSecureContext=' + window.isSecureContext);
        return null;
      }
      const codecs = [
        'hev1.1.6.L150.B0',
        'hvc1.1.6.L150.B0',
        'hev1.1.6.L120.B0',
        'hvc1.1.6.L120.B0',
        'hev1.1.6.L93.B0',
        'hvc1.1.6.L93.B0',
        'hev1.1.6.L153.B0',
        'hvc1.1.6.L153.B0',
        'hev1.1.6.L90.90'
      ];
      for (const c of codecs) {
        try {
          const res = await VideoDecoder.isConfigSupported({
            codec: c,
            codedWidth: 1920,
            codedHeight: 1080
          });
          console.log(`[WebCodecs] Codec support probe for ${c}: supported=${res && res.supported}`);
          if (res && res.supported) {
            return c;
          }
        } catch (e) {
          console.warn(`[WebCodecs] Probe ${c} threw:`, e);
        }
      }
      return null;
    }

    function startWebCodecsStream(codec) {
      cleanupWebCodecs();
      streamMode = 'webcodecs';
      isFallbackActive = false;

      let hasKeyframe = false;
      let frameCount = 0;
      let chunksFed = 0;
      let lastPts = 0;

      const streamBadgeInit = document.getElementById('stream-badge');
      if (streamBadgeInit) streamBadgeInit.innerText = 'CONNECTING...';

      function createDecoder() {
        cleanupDecoder();
        hasKeyframe = false;
        console.log(`[WebCodecs] Initializing VideoDecoder with codec: ${codec}`);
        try {
          activeDecoder = new VideoDecoder({
            output: (frame) => {
              if (streamMode !== 'webcodecs') {
                frame.close();
                return;
              }
              if (streamCanvas.width !== frame.displayWidth || streamCanvas.height !== frame.displayHeight) {
                streamCanvas.width = frame.displayWidth;
                streamCanvas.height = frame.displayHeight;
              }
              const ctx = streamCanvas.getContext('2d');
              ctx.drawImage(frame, 0, 0, streamCanvas.width, streamCanvas.height);
              frame.close();

              if (frameCount === 0) {
                streamRetryCount = 0;
                decoderErrorCount = 0;
                console.log(`[WebCodecs] First video frame rendered successfully (${streamCanvas.width}x${streamCanvas.height})`);
                if (streamCanvas) streamCanvas.style.display = 'block';
                if (streamImg) {
                  streamImg.style.display = 'none';
                  streamImg.src = '';
                }
                const streamBadge = document.getElementById('stream-badge');
                const liveDot = document.getElementById('live-dot');
                if (streamBadge) {
                  streamBadge.innerText = 'LIVE STREAM (HEVC)';
                  streamBadge.title = `Hardware-accelerated HEVC stream (${codec})`;
                }
                if (liveDot) {
                  liveDot.style.background = '#48BB78';
                  liveDot.style.animation = 'pulse 1.5s infinite';
                }
                const errOverlay = document.getElementById('video-error');
                if (errOverlay) errOverlay.classList.add('hidden');
              }
              frameCount++;
            },
            error: (err) => {
              const errName = err ? (err.name || 'Error') : 'UnknownError';
              const errMsg = err ? (err.message || String(err)) : 'Unknown decoder error';
              const diag = `codec=${codec}, state=${activeDecoder ? activeDecoder.state : 'null'}, qSize=${activeDecoder ? activeDecoder.decodeQueueSize : 'null'}, chunksFed=${chunksFed}, framesRendered=${frameCount}, hasKeyframe=${hasKeyframe}`;
              console.error(`[WebCodecs] Decoder error (${errName}): ${errMsg}. Details: [${diag}]`, err);
              decoderErrorCount++;
              if (decoderErrorCount < 3) {
                console.warn(`[WebCodecs] Recovering from decoder error (${decoderErrorCount}/3). Awaiting fresh keyframe...`);
                createDecoder();
              } else {
                fallbackToMjpeg(`Persistent decoder error (${errName}: ${errMsg})`);
              }
            }
          });

          activeDecoder.configure({
            codec: codec,
            codedWidth: 1920,
            codedHeight: 1080,
            optimizeForLatency: true
          });
          return true;
        } catch (err) {
          const errName = err ? (err.name || 'Error') : 'UnknownError';
          const errMsg = err ? (err.message || String(err)) : 'Configuration exception';
          console.error(`[WebCodecs] Failed configuring decoder (${errName}): ${errMsg}`, err);
          fallbackToMjpeg(`Decoder config failed: ${errMsg}`);
          return false;
        }
      }

      if (!createDecoder()) {
        return;
      }

      const wsProto = (location.protocol === 'https:') ? 'wss:' : 'ws:';
      const wsUrl = `${wsProto}//${location.host}/ws/stream`;
      console.log(`[WebCodecs] Connecting WebSocket to ${wsUrl}`);
      const ws = new WebSocket(wsUrl);
      ws.binaryType = 'arraybuffer';
      activeWs = ws;

      ws.onopen = () => {
        console.log('[WebCodecs] WebSocket connected to /ws/stream');
      };

      ws.onmessage = (event) => {
        if (streamMode !== 'webcodecs' || !activeDecoder || activeDecoder.state === 'closed') return;
        if (!(event.data instanceof ArrayBuffer)) return;
        const bytes = new Uint8Array(event.data);
        if (bytes.length <= 1) return;
        const isKey = (bytes[0] === 1);
        if (!hasKeyframe) {
          if (!isKey) return; // Wait for first keyframe
          hasKeyframe = true;
          console.log(`[WebCodecs] Received first keyframe Access Unit (${bytes.length - 1} bytes). Feeding decoder.`);
        }
        const chunkData = bytes.subarray(1);
        let pts = Math.round(performance.now() * 1000);
        if (pts <= lastPts) {
          pts = lastPts + 1000;
        }
        lastPts = pts;
        try {
          const chunk = new EncodedVideoChunk({
            type: isKey ? 'key' : 'delta',
            timestamp: pts,
            data: chunkData
          });
          chunksFed++;
          activeDecoder.decode(chunk);
        } catch (err) {
          const errName = err ? (err.name || 'Error') : 'Error';
          const errMsg = err ? (err.message || String(err)) : 'Decode enqueue failed';
          const diag = `codec=${codec}, state=${activeDecoder ? activeDecoder.state : 'null'}, chunksFed=${chunksFed}, isKey=${isKey}, size=${chunkData.byteLength}`;
          console.error(`[WebCodecs] Chunk decode exception (${errName}): ${errMsg}. Details: [${diag}]`, err);
          decoderErrorCount++;
          if (decoderErrorCount < 3) {
            console.warn(`[WebCodecs] Recovering from chunk decode exception (${decoderErrorCount}/3). Awaiting fresh keyframe...`);
            createDecoder();
          } else {
            fallbackToMjpeg(`Persistent chunk decode exception: ${errMsg}`);
          }
        }
      };

      ws.onerror = (e) => {
        console.warn('[WebCodecs] WebSocket error:', e);
      };

      ws.onclose = (e) => {
        console.log('[WebCodecs] WebSocket closed:', e.code, e.reason);
        if (streamMode === 'webcodecs' && !isFallbackActive && !hasFatalDecoderError) {
          streamRetryCount++;
          const delay = Math.min(3000, 1000 + streamRetryCount * 200);
          console.log(`[WebCodecs] Waiting for camera stream (attempt ${streamRetryCount} in ${delay}ms)...`);
          const streamBadge = document.getElementById('stream-badge');
          const liveDot = document.getElementById('live-dot');
          if (streamBadge) streamBadge.innerText = 'CONNECTING...';
          if (liveDot) {
            liveDot.style.background = '#ED8936';
            liveDot.style.animation = 'pulse 1s infinite';
          }
          if (!streamRetryTimer) {
            streamRetryTimer = setTimeout(() => {
              streamRetryTimer = null;
              retryStream();
            }, delay);
          }
        }
      };
    }

    async function initPlayer() {
      supportedHevcCodec = await checkHevcWebCodecsSupport();
      if (supportedHevcCodec) {
        console.log('[Stream] WebCodecs HEVC hardware decoding supported (' + supportedHevcCodec + '). Using WebCodecs.');
        startWebCodecsStream(supportedHevcCodec);
      } else {
        const reason = (!window.isSecureContext)
          ? 'Requires HTTPS or localhost'
          : 'WebCodecs HEVC not supported in this browser';
        console.log('[Stream] ' + reason + '. Using MJPEG fallback.');
        startMjpegStream(reason);
      }
    }

    function retryStream() {
      const errOverlay = document.getElementById('video-error');
      if (errOverlay) errOverlay.classList.add('hidden');
      const streamBadge = document.getElementById('stream-badge');
      const liveDot = document.getElementById('live-dot');
      if (streamBadge) streamBadge.innerText = 'CONNECTING...';
      if (liveDot) {
        liveDot.style.background = '#ED8936';
        liveDot.style.animation = 'pulse 1s infinite';
      }
      showToast('Connecting to camera stream...', 'info', 2500);

      decoderErrorCount = 0;
      hasFatalDecoderError = false;
      isFallbackActive = false;

      if (supportedHevcCodec) {
        startWebCodecsStream(supportedHevcCodec);
      } else {
        startMjpegStream();
      }
      updateStatus();
    }

    if (streamCanvas) {
      streamCanvas.addEventListener('dblclick', (e) => {
        e.preventDefault();
        toggleFullscreen();
      });
    }

    if (streamImg) {
      streamImg.addEventListener('dblclick', (e) => {
        e.preventDefault();
        toggleFullscreen();
      });

      streamImg.onerror = function() {
        if (streamMode !== 'mjpeg') return;
        streamRetryCount++;
        const delay = Math.min(10, 2 * streamRetryCount);
        setStreamError('Live Stream Reconnecting', `Video feed connection lost. Retrying in ${delay}s...`);
        if (!streamRetryTimer) {
          streamRetryTimer = setTimeout(() => {
            streamRetryTimer = null;
            if (streamImg) streamImg.src = '/stream?t=' + Date.now();
          }, delay * 1000);
        }
      };

      streamImg.onload = function() {
        if (streamMode !== 'mjpeg') return;
        streamRetryCount = 0;
        const errOverlay = document.getElementById('video-error');
        if (errOverlay) errOverlay.classList.add('hidden');
        const streamBadge = document.getElementById('stream-badge');
        const liveDot = document.getElementById('live-dot');
        if (streamBadge) {
          streamBadge.innerText = 'LIVE STREAM';
          streamBadge.title = 'Motion JPEG stream (Fallback)';
        }
        if (liveDot) {
          liveDot.style.background = '#48BB78';
          liveDot.style.animation = 'pulse 1.5s infinite';
        }
      };

      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
          if (streamMode === 'webcodecs') {
            if (!activeWs || activeWs.readyState !== WebSocket.OPEN) {
              retryStream();
            }
          } else if (streamMode === 'mjpeg') {
            if (streamImg && (!streamImg.complete || streamImg.naturalWidth === 0)) {
              streamImg.src = '/stream?t=' + Date.now();
            }
          }
          updateStatus();
        }
      });
    }

    function setNightVision(mode) { apiPost('night_vision', {mode}); }
    function togglePrivacy() { apiPost('toggle_privacy'); }
    function toggleLed() { apiPost('toggle_led'); }
    function toggleFlip() { apiPost('toggle_flip'); }
    function toggleLullaby(play) {
      const track = document.getElementById('lullaby-track').value;
      const vol = document.getElementById('lullaby-vol').value;
      apiPost('lullaby', {play, track, volume: parseInt(vol)});
    }
    function setLullabyVol(vol) { apiPost('lullaby_volume', {volume: parseInt(vol)}); }
    function changeLullaby() { toggleLullaby(true); }

    let offlineFailures = 0;
    async function updateStatus() {
      try {
        const res = await fetch('/api/status');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        offlineFailures = 0;
        const d = await res.json();
        if (d.server_lan_ip) window.serverLanIp = d.server_lan_ip;
        if (d.server_port) window.serverPort = d.server_port;
        if (d.server_http_port) window.serverHttpPort = d.server_http_port;
        const badge = document.getElementById('conn-badge');
        let banner = document.getElementById('status-error-banner');

        if (d.online === false) {
          if (badge) {
            badge.className = 'badge';
            badge.style.background = '#FED7D7';
            badge.style.color = '#C53030';
            badge.innerText = 'Offline / Communication Notice';
          }
          if (d.error && !banner) {
            banner = document.createElement('div');
            banner.id = 'status-error-banner';
            banner.style.cssText = 'background:#FFF5F5; border:1px solid #FEB2B2; color:#9B2C2C; padding:12px 16px; border-radius:8px; margin-bottom:16px; font-size:0.88rem; line-height:1.4;';
            const grid = document.querySelector('.grid');
            if (grid && grid.parentNode) {
              grid.parentNode.insertBefore(banner, grid);
            }
          }
          if (banner) {
            banner.innerHTML = `⚠️ <strong>Device Communication Notice:</strong> ${d.error}<br><span style="color:#742A2A; font-size:0.82rem;">Note: If the camera is at this IP but not responding, verify its local key or run <code>./momcozy.py login</code>.</span>`;
          }
        } else {
          if (badge) {
            badge.className = 'badge active';
            badge.style.background = '#C6F6D5';
            badge.style.color = '#22543D';
            badge.innerText = 'Connected';
          }
          if (banner) {
            banner.remove();
          }
        }

        // Live stream status inspection
        if (d.stream_configured === false) {
          setStreamError('Video Stream Not Configured', 'No Tuya cloud credentials found in .momcozy.json. Run "./momcozy.py login" to link your credentials.');
        } else if (d.stream_error && !d.stream_running) {
          setStreamError('Video Stream Error', d.stream_error);
        } else if (d.stream_connected) {
          const errOverlay = document.getElementById('video-error');
          if (errOverlay) errOverlay.classList.add('hidden');
          // Auto-promote to WebCodecs HEVC if stream is now connected and client is in temporary MJPEG mode without decoder errors
          if (supportedHevcCodec && !hasFatalDecoderError && streamMode === 'mjpeg') {
            console.log('[Stream] Camera live stream is now connected. Auto-promoting from MJPEG to WebCodecs HEVC.');
            isFallbackActive = false;
            streamRetryCount = 0;
            decoderErrorCount = 0;
            startWebCodecsStream(supportedHevcCodec);
          }
          const streamBadge = document.getElementById('stream-badge');
          const liveDot = document.getElementById('live-dot');
          if (streamBadge) {
            if (streamMode === 'webcodecs') {
              streamBadge.innerText = 'LIVE STREAM (HEVC)';
            } else if (streamMode === 'mjpeg') {
              streamBadge.innerText = 'LIVE STREAM (MJPEG)';
            } else {
              streamBadge.innerText = 'LIVE STREAM';
            }
          }
          if (liveDot) {
            liveDot.style.background = '#48BB78';
            liveDot.style.animation = 'pulse 1.5s infinite';
          }
        } else if (d.stream_running) {
          const streamBadge = document.getElementById('stream-badge');
          const liveDot = document.getElementById('live-dot');
          if (streamBadge) streamBadge.innerText = 'CONNECTING...';
          if (liveDot) {
            liveDot.style.background = '#ED8936';
            liveDot.style.animation = 'pulse 1s infinite';
          }
        }

        // Update transport badges
        if (d.control_transport) {
          const ctrlEl = document.getElementById('hdr-control-transport');
          if (ctrlEl) {
            ctrlEl.innerText = d.control_transport;
            if (d.control_diagnostic) {
              ctrlEl.title = d.control_diagnostic;
            }
          }
          const diagEl = document.getElementById('hdr-control-diag');
          if (diagEl) {
            if (d.control_mode !== 'local_lan' && d.control_diagnostic) {
              diagEl.innerText = `(${d.control_diagnostic})`;
              diagEl.title = d.control_diagnostic;
              diagEl.style.display = 'inline';
            } else {
              diagEl.style.display = 'none';
            }
          }
        }
        if (d.stream_transport) {
          const stEl = document.getElementById('hdr-stream-transport');
          if (stEl) stEl.innerText = d.stream_transport;
        }

        if (d.temperature_c !== null && d.temperature_c !== undefined) {
          document.getElementById('temp-val').innerHTML = `${d.temperature_c.toFixed(1)}°C <span>(${d.temperature_f.toFixed(1)}°F)</span>`;
        }
        document.getElementById('motion-stat').innerText = d.motion_switch ? "Active" : "Off";
        document.getElementById('cry-stat').innerText = d.cry_detection_switch ? "Active" : "Off";
        document.getElementById('priv-stat').innerText = d.privacy_mode ? "ON" : "OFF";
        const nvMap = {"0": "Auto", "1": "Off", "2": "On"};
        document.getElementById('nv-stat').innerText = nvMap[d.night_vision] || d.night_vision;

        // Detection snapshot telemetry
        const detectTrigger = d.detection_trigger || d.latest_event_name || 'Event Alert';
        const detectTime = d.detection_time_str || d.latest_event_time_str || '';
        const trigEl = document.getElementById('detect-trigger');
        if (trigEl) trigEl.innerText = detectTrigger;
        const timeEl = document.getElementById('detect-time');
        if (timeEl) timeEl.innerText = detectTime || 'No recent events';
        if (d.latest_event_file) {
          const rowEl = document.getElementById('detect-cloud-row');
          const fileEl = document.getElementById('detect-cloud-file');
          if (rowEl) rowEl.style.display = 'flex';
          if (fileEl) {
            fileEl.innerText = d.latest_event_file;
            fileEl.title = `${d.latest_event_bucket || 'cloud'}: ${d.latest_event_file}`;
          }
        }
        if (d.detection_time && d.detection_time !== lastDetectTime) {
          lastDetectTime = d.detection_time;
          const detectImg = document.getElementById('detect-img');
          if (detectImg) detectImg.src = '/detection_snapshot.jpg?t=' + Date.now();
        }
        if (d.timelapse) {
          updateTimelapseUI(d.timelapse);
        }
      } catch(e) {
        offlineFailures++;
        if (offlineFailures >= 2) {
          const badge = document.getElementById('conn-badge');
          if (badge) {
            badge.className = 'badge';
            badge.style.background = '#FED7D7';
            badge.style.color = '#C53030';
            badge.innerText = 'Server Unreachable';
          }
          let banner = document.getElementById('status-error-banner');
          if (!banner) {
            banner = document.createElement('div');
            banner.id = 'status-error-banner';
            banner.style.cssText = 'background:#FFF5F5; border:1px solid #FEB2B2; color:#9B2C2C; padding:12px 16px; border-radius:8px; margin-bottom:16px; font-size:0.88rem; line-height:1.4;';
            const grid = document.querySelector('.grid');
            if (grid && grid.parentNode) {
              grid.parentNode.insertBefore(banner, grid);
            }
          }
          if (banner) {
            banner.innerHTML = `⚠️ <strong>Dashboard Server Connection Notice:</strong> ${e.message}. Reconnecting...`;
          }
        }
      }
    }

    window.loadStreamToCastSession = function(session) {
      if (!session) {
        console.warn("[Google Cast] loadStreamToCastSession called without active session.");
        return;
      }
      const state = session.getSessionState ? session.getSessionState() : null;
      if (state && state !== cast.framework.SessionState.SESSION_STARTED && state !== cast.framework.SessionState.SESSION_RESUMED) {
        console.log("[Google Cast] Session not ready for media load (state=" + state + ").");
        return;
      }

      // Chromecast strictly rejects self-signed HTTPS certificates.
      // Therefore, always provide an HTTP stream URL for Chromecast over local LAN:
      const castProto = 'http:';
      const castPort = window.serverHttpPort ? (':' + window.serverHttpPort) : (window.location.protocol === 'http:' && window.location.port ? (':' + window.location.port) : ':8420');
      const streamUrl = `${castProto}//${window.location.hostname}${castPort}/hls/stream.m3u8`;
      console.log("[Google Cast] Loading stream to receiver:", streamUrl);

      const mediaInfo = new chrome.cast.media.MediaInfo(streamUrl, 'application/x-mpegurl');
      mediaInfo.streamType = chrome.cast.media.StreamType.LIVE;
      mediaInfo.metadata = new chrome.cast.media.GenericMediaMetadata();
      mediaInfo.metadata.metadataType = chrome.cast.media.MetadataType.GENERIC;
      mediaInfo.metadata.title = 'Momcozy BM04 Baby Monitor';
      mediaInfo.metadata.subtitle = 'Live Camera Feed';

      const request = new chrome.cast.media.LoadRequest(mediaInfo);
      request.autoplay = true;

      session.loadMedia(request).then(
        function() {
          console.log("[Google Cast] Stream loaded onto Cast receiver successfully.");
          showToast('Live stream casting to TV!', 'success', 3000);
        },
        function(err) {
          console.error("[Google Cast] Error loading cast media:", err);
          const errDetail = (err && (err.description || err.message || JSON.stringify(err))) || String(err);
          showToast('Could not load stream on Cast device: ' + errDetail, 'error', 5000);
        }
      );
    };

    async function handleCastClick(event) {
      if (event) {
        event.stopPropagation();
        event.preventDefault();
      }

      console.log("[Google Cast] Cast button clicked. isSecureContext:", window.isSecureContext, "googleCastInitialized:", !!window.googleCastInitialized);

      // Pre-warm HLS pipeline so stream chunks are ready when TV connects
      fetch('/hls/stream.m3u8').catch(() => {});

      // 1. If currently in a Cast session, clicking ends session
      if (window.googleCastInitialized && window.cast && window.cast.framework) {
        try {
          const castContext = cast.framework.CastContext.getInstance();
          const session = castContext.getCurrentSession();
          if (session) {
            console.log("[Google Cast] Ending active Cast session...");
            castContext.endCurrentSession(true);
            showToast('Cast session ended', 'info', 2000);
            return;
          }
        } catch(e) {
          console.error("[Google Cast] Error inspecting/ending session:", e);
        }
      }

      // 2. If Google Cast SDK is initialized, open native Cast device picker
      if (window.googleCastInitialized && window.cast && window.cast.framework) {
        try {
          const castContext = cast.framework.CastContext.getInstance();
          console.log("[Google Cast] Triggering native cast dialog via CastContext.requestSession()...");
          await castContext.requestSession();
          console.log("[Google Cast] requestSession completed successfully.");
          return;
        } catch (err) {
          if (err === 'cancel' || (err && typeof err === 'string' && err.includes('cancel'))) {
            console.log("[Google Cast] User dismissed cast dialog.");
            return;
          }
          console.error("[Google Cast] requestSession failed:", err);
          showToast('Cast request failed: ' + (err.description || err.message || err), 'error', 5000);
          return;
        }
      }

      // 3. Cast SDK not initialized: log why and show actionable user error
      console.error("[Google Cast] Cast framework is not available.");
      if (!window.isSecureContext) {
        const httpsPort = window.serverPort ? (':' + window.serverPort) : (window.location.port ? (':' + window.location.port) : '');
        const httpsUrl = `https://${window.location.hostname}${httpsPort}/`;
        console.error(`[Google Cast] INSECURE ORIGIN: Chrome strictly disables browser-side Cast on HTTP (${window.location.origin}).`);
        console.error(`[Google Cast] Switch to HTTPS dashboard at: ${httpsUrl}`);
        showToast(`Google Cast requires HTTPS. Open <a href="${httpsUrl}" style="color:#2B6CB0; text-decoration:underline; font-weight:bold;">${httpsUrl}</a> to cast.`, 'error', 12000);
      } else if (!window.cast) {
        console.error("[Google Cast] Google Cast Web SDK (cast_sender.js) did not load. Check network connection or script blockers.");
        showToast("Google Cast script could not be reached. Check network connection.", "error", 5000);
      } else {
        console.error("[Google Cast] CastContext could not be initialized or no devices were discovered yet.");
        showToast("Cast device discovery is still initializing. Ensure your Chromecast is on and on the same Wi-Fi.", "warn", 5000);
      }
    }

    function updateTimelapseUI(tl) {
      if (!tl) return;
      const badge = document.getElementById('timelapse-badge');
      const toggleBtn = document.getElementById('timelapse-toggle-btn');
      const watchBtn = document.getElementById('timelapse-watch-btn');
      const eraseBtn = document.getElementById('timelapse-erase-btn');
      const bufferedEl = document.getElementById('timelapse-buffered');
      const vlenEl = document.getElementById('timelapse-vlen');
      const storageEl = document.getElementById('timelapse-storage');
      const ratioSelect = document.getElementById('timelapse-ratio-select');
      const ctrlBtn = document.getElementById('timelapse-ctrl-btn');

      if (bufferedEl) bufferedEl.innerText = tl.buffered_formatted || '0m 00s';
      if (vlenEl) vlenEl.innerText = tl.video_duration_formatted || '0:00';
      if (storageEl) storageEl.innerText = tl.disk_formatted || '0.0 MB';
      if (ratioSelect && tl.speed_ratio_val) ratioSelect.value = String(tl.speed_ratio_val);

      const evBadge = document.getElementById('timelapse-events-badge');
      if (evBadge) {
        const evCount = tl.motion_events_count || 0;
        evBadge.innerText = `${evCount} event${evCount === 1 ? '' : 's'}`;
        if (evCount > 0) {
          evBadge.style.background = 'rgba(245, 158, 11, 0.2)';
          evBadge.style.color = '#f59e0b';
          evBadge.style.border = '1px solid rgba(245, 158, 11, 0.4)';
        } else {
          evBadge.style.background = 'rgba(255, 255, 255, 0.08)';
          evBadge.style.color = 'var(--text-muted)';
          evBadge.style.border = '1px solid transparent';
        }
      }

      if (tl.is_recording) {
        if (badge) {
          badge.className = 'badge active';
          if (tl.last_session_type === 'auto') {
            badge.style.background = '#EDE9FE';
            badge.style.color = '#7C3AED';
            badge.innerText = '🌙 Auto-Recording';
          } else {
            badge.style.background = '#FED7D7';
            badge.style.color = '#C53030';
            badge.innerText = 'Recording';
          }
        }
        if (toggleBtn) {
          toggleBtn.innerText = '⏹ Stop Recording';
          toggleBtn.className = 'btn';
        }
        if (ctrlBtn) ctrlBtn.classList.add('active');
        if (eraseBtn) eraseBtn.disabled = true;
      } else {
        if (badge) {
          badge.className = 'badge';
          if (tl.sync_state === 'activating') {
            badge.style.background = '#EDE9FE';
            badge.style.color = '#7C3AED';
            badge.innerText = `🌙 Activating (${Math.round(tl.sync_timer_remaining || 0)}s)`;
          } else {
            badge.style.background = '#EDF2F7';
            badge.style.color = 'var(--text-muted)';
            badge.innerText = tl.has_content ? 'Ready' : 'Stopped';
          }
        }
        if (toggleBtn) {
          if (tl.has_content) {
            toggleBtn.innerText = '▶ Resume Recording';
          } else {
            toggleBtn.innerText = '⏺ Start Recording';
          }
          toggleBtn.className = 'btn primary';
        }
        if (ctrlBtn) ctrlBtn.classList.remove('active');
        if (eraseBtn) eraseBtn.disabled = !tl.has_content;
      }

      const syncBtn = document.getElementById('timelapse-sync-nv-btn');
      if (syncBtn) {
        const syncOn = !!tl.sync_night_vision;
        if (syncOn) {
          let label = '🌙 Sync with Night Vision: ON';
          if (tl.sync_state === 'activating') {
            const sec = Math.round(tl.sync_timer_remaining || 0);
            label = `🌙 Night Vision Active (Starts in ${sec}s)`;
          } else if (tl.sync_state === 'stopping') {
            const sec = Math.round(tl.sync_timer_remaining || 0);
            label = `☀️ Day Mode Detected (Stops in ${sec}s)`;
          } else if (tl.is_recording && tl.last_session_type === 'auto') {
            label = '🌙 Sync: Auto-Recording (Active)';
          } else if (tl.night_vision_active) {
            label = '🌙 Sync with Night Vision: ON (Active)';
          }
          syncBtn.innerText = label;
          syncBtn.style.background = 'rgba(147, 51, 234, 0.2)';
          syncBtn.style.borderColor = 'rgba(147, 51, 234, 0.5)';
          syncBtn.style.color = '#c084fc';
          syncBtn.style.fontWeight = '600';
        } else {
          syncBtn.innerText = '🌙 Sync with Night Vision: OFF';
          syncBtn.style.background = '';
          syncBtn.style.borderColor = '';
          syncBtn.style.color = '';
          syncBtn.style.fontWeight = '';
        }
      }

      if (watchBtn) {
        watchBtn.disabled = !tl.has_content;
      }
    }

    async function changeTimelapseRatio(val) {
      try {
        const res = await fetch('/api/timelapse/ratio', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ratio: parseInt(val, 10) })
        });
        const d = await res.json();
        if (d.success) {
          updateTimelapseUI(d.timelapse);
        } else {
          showToast('Failed to change speed ratio: ' + (d.error || ''), 'error', 3500);
        }
      } catch (e) {
        showToast('Network error setting speed ratio: ' + e.message, 'error', 3500);
      }
    }

    async function toggleTimelapseRecord() {
      const toggleBtn = document.getElementById('timelapse-toggle-btn');
      const isCurrentlyRecording = toggleBtn && toggleBtn.innerText.includes('Stop');
      const isResume = toggleBtn && toggleBtn.innerText.includes('Resume');
      const endpoint = isCurrentlyRecording ? '/api/timelapse/stop' : '/api/timelapse/start';

      try {
        if (toggleBtn) toggleBtn.disabled = true;
        const res = await fetch(endpoint, { method: 'POST' });
        const d = await res.json();
        if (d.success) {
          let msg = 'Timelapse recording stopped.';
          if (!isCurrentlyRecording) {
            msg = isResume ? 'Timelapse recording resumed.' : 'New timelapse recording started!';
          }
          showToast(msg, 'success', 3500);
          updateTimelapseUI(d.timelapse);
        } else {
          showToast('Timelapse error: ' + (d.error || 'Failed to toggle recording'), 'error', 4000);
        }
      } catch (e) {
        showToast('Network error toggling timelapse: ' + e.message, 'error', 4000);
      } finally {
        if (toggleBtn) toggleBtn.disabled = false;
        updateStatus();
      }
    }

    async function eraseTimelapse() {
      if (!confirm("Are you sure you want to erase all recorded timelapse footage? This cannot be undone.")) {
        return;
      }
      const eraseBtn = document.getElementById('timelapse-erase-btn');
      try {
        if (eraseBtn) eraseBtn.disabled = true;
        const res = await fetch('/api/timelapse/clear', { method: 'POST' });
        const d = await res.json();
        if (d.success) {
          showToast('Timelapse footage erased.', 'info', 3000);
          updateTimelapseUI(d.timelapse);
        } else {
          showToast('Failed to erase timelapse: ' + (d.error || ''), 'error', 4000);
        }
      } catch (e) {
        showToast('Network error erasing timelapse: ' + e.message, 'error', 4000);
      } finally {
        updateStatus();
      }
    }

    async function toggleNightVisionSync() {
      const syncBtn = document.getElementById('timelapse-sync-nv-btn');
      try {
        if (syncBtn) syncBtn.disabled = true;
        const res = await fetch('/api/timelapse/sync_night_vision', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        const d = await res.json();
        if (d.success) {
          showToast(d.sync_night_vision ? '🌙 Night Vision timelapse sync ENABLED.' : '🌙 Night Vision timelapse sync DISABLED.', 'info', 4000);
          if (d.timelapse) updateTimelapseUI(d.timelapse);
        } else {
          showToast('Failed to toggle Night Vision sync: ' + (d.error || ''), 'error', 4000);
        }
      } catch (e) {
        showToast('Network error setting Night Vision sync: ' + e.message, 'error', 4000);
      } finally {
        if (syncBtn) syncBtn.disabled = false;
        updateStatus();
      }
    }

    function openTimelapsePlayback() {
      window.open('/timelapse', '_blank');
    }

    setInterval(updateStatus, 3000);
    updateStatus();
    initPlayer();
  </script>
</body>
</html>
"""


HTML_TIMELAPSE_PLAYER = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Momcozy BM04 - 12-Hour Timelapse</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.ico">
  <style>
    :root {
      --primary: #FF7E98;
      --primary-dark: #E65A77;
      --bg: #0f172a;
      --card-bg: #1e293b;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --border: #334155;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 16px;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
    }
    .container {
      width: 100%;
      max-width: 960px;
      background: var(--card-bg);
      border-radius: 16px;
      box-shadow: 0 10px 40px rgba(0,0,0,0.5);
      border: 1px solid var(--border);
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 14px 20px;
      border-bottom: 1px solid var(--border);
    }
    .title-group {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .title-group h1 {
      font-size: 1.15rem;
      font-weight: 700;
    }
    .video-container {
      position: relative;
      background: #000;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 380px;
      max-height: 72vh;
    }
    video {
      width: 100%;
      max-height: 72vh;
      background: #000;
      display: block;
      outline: none;
    }
    .overlay-msg {
      position: absolute;
      color: #fff;
      font-size: 0.95rem;
      background: rgba(15,23,42,0.85);
      padding: 12px 20px;
      border-radius: 10px;
      border: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 10px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    }
    .spinner {
      width: 18px;
      height: 18px;
      border: 2px solid rgba(255,255,255,0.2);
      border-top-color: var(--primary);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
    .footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 12px 20px;
      background: rgba(15,23,42,0.6);
      border-top: 1px solid var(--border);
      flex-wrap: wrap;
      gap: 12px;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      font-size: 0.82rem;
      font-weight: 600;
      color: var(--text);
      background: #334155;
      border: 1px solid var(--border);
      border-radius: 8px;
      cursor: pointer;
      text-decoration: none;
      transition: all 0.15s ease;
    }
    .btn:hover {
      background: #475569;
    }
    .btn.primary {
      background: var(--primary);
      color: #fff;
      border-color: var(--primary);
    }
    .btn.primary:hover {
      background: var(--primary-dark);
    }
    .btn.active {
      background: var(--primary);
      color: #fff;
      border-color: var(--primary);
    }
    .speed-group {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.85rem;
      color: var(--text-muted);
    }
    .speed-btn {
      padding: 4px 10px;
    }
    .timeline-card {
      padding: 12px 20px 8px 20px;
      background: rgba(15,23,42,0.7);
      border-top: 1px solid var(--border);
    }
    .timeline-bar-wrapper {
      position: relative;
      padding: 8px 0;
      cursor: pointer;
    }
    .timeline-bar {
      position: relative;
      width: 100%;
      height: 8px;
      background: rgba(255, 255, 255, 0.12);
      border-radius: 4px;
      overflow: visible;
      transition: height 0.15s ease;
    }
    .timeline-bar-wrapper:hover .timeline-bar {
      height: 10px;
    }
    .timeline-progress {
      position: absolute;
      left: 0;
      top: 0;
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, var(--primary), #fb7185);
      border-radius: 4px;
      pointer-events: none;
    }
    .timeline-thumb {
      position: absolute;
      top: 50%;
      left: 0%;
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: #fff;
      box-shadow: 0 0 6px rgba(0,0,0,0.6);
      transform: translate(-50%, -50%);
      pointer-events: none;
      transition: transform 0.1s ease;
    }
    .timeline-bar-wrapper:hover .timeline-thumb {
      transform: translate(-50%, -50%) scale(1.25);
    }
    .timeline-marker {
      position: absolute;
      top: 50%;
      width: 6px;
      height: 16px;
      border-radius: 3px;
      background: #f59e0b;
      box-shadow: 0 0 8px rgba(245, 158, 11, 0.9);
      transform: translate(-50%, -50%);
      cursor: pointer;
      z-index: 10;
      transition: all 0.15s ease;
    }
    .timeline-marker:hover {
      width: 8px;
      height: 20px;
      background: #fbbf24;
      box-shadow: 0 0 12px rgba(251, 191, 36, 1);
    }
    .timeline-tooltip {
      position: absolute;
      top: -30px;
      left: 0;
      transform: translateX(-50%);
      background: rgba(15, 23, 42, 0.95);
      color: #f8fafc;
      padding: 4px 8px;
      border-radius: 6px;
      font-size: 0.72rem;
      font-weight: 600;
      white-space: nowrap;
      pointer-events: none;
      border: 1px solid rgba(255, 255, 255, 0.2);
      box-shadow: 0 4px 12px rgba(0,0,0,0.5);
      display: none;
      z-index: 50;
    }
    .timeline-meta-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 6px;
      font-size: 0.8rem;
      color: var(--text-muted);
      flex-wrap: wrap;
      gap: 10px;
    }
    .timeline-nav-group {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .nav-event-btn {
      padding: 3px 8px;
      font-size: 0.75rem;
      font-weight: 600;
      border-radius: 6px;
      background: #334155;
      border: 1px solid var(--border);
      color: var(--text);
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .nav-event-btn:hover {
      background: rgba(245, 158, 11, 0.25);
      border-color: rgba(245, 158, 11, 0.6);
      color: #fbbf24;
    }
    .event-counter-badge {
      background: rgba(245, 158, 11, 0.15);
      color: #fbbf24;
      border: 1px solid rgba(245, 158, 11, 0.35);
      font-size: 0.75rem;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 10px;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="title-group">
        <span style="font-size: 1.3rem;">⏱️</span>
        <div>
          <h1 style="display:flex; align-items:center; gap:8px;">
            <span>Timelapse</span>
            <span id="player-ratio-badge" style="background:rgba(66,153,225,0.15); color:#60a5fa; font-size:0.75rem; font-weight:600; padding:2px 8px; border-radius:6px; border:1px solid rgba(66,153,225,0.3);" title="Timelapse Recording Speed Ratio">1:60</span>
            <span id="player-nv-sync-badge" style="display:none; background:rgba(147,51,234,0.18); color:#c084fc; font-size:0.75rem; font-weight:600; padding:2px 8px; border-radius:6px; border:1px solid rgba(147,51,234,0.4);" title="Timelapse is synchronized with Night Vision">🌙 NV Sync</span>
          </h1>
          <div style="font-size: 0.75rem; color: var(--text-muted);">Momcozy BM04</div>
        </div>
      </div>
      <div>
        <a href="/" class="btn">← Back to Dashboard</a>
      </div>
    </div>
    <div class="video-container">
      <video id="timelapse-video" controls autoplay playsinline loop></video>
      <div id="loading" class="overlay-msg">
        <div class="spinner"></div>
        <span>Preparing timelapse playback...</span>
      </div>
      <div id="error-box" class="overlay-msg" style="display:none; color:#f87171;">
        <span>⚠️ Unable to play video in browser. Please download the file to view.</span>
      </div>
    </div>
    <div class="timeline-card">
      <div class="timeline-bar-wrapper" id="timeline-wrapper">
        <div class="timeline-bar" id="timeline-bar" onclick="seekFromTimeline(event)">
          <div class="timeline-progress" id="timeline-progress"></div>
          <div class="timeline-markers" id="timeline-markers"></div>
          <div class="timeline-thumb" id="timeline-thumb"></div>
        </div>
        <div id="timeline-tooltip" class="timeline-tooltip"></div>
      </div>
      <div class="timeline-meta-row">
        <div class="timeline-time-info">
          <span id="current-time-text">0:00</span> / <span id="duration-time-text">0:00</span>
        </div>
        <div class="timeline-nav-group">
          <button class="nav-event-btn" id="prev-event-btn" onclick="jumpToPrevEvent()" title="Jump to previous motion event (Key: P)">⏮️ Prev Motion</button>
          <span id="events-counter-badge" class="badge event-counter-badge">🏃 0 Motion Events</span>
          <button class="nav-event-btn" id="next-event-btn" onclick="jumpToNextEvent()" title="Jump to next motion event (Key: N)">Next Motion ⏭️</button>
        </div>
      </div>
    </div>
    <div class="footer">
      <div class="speed-group">
        <span>Speed:</span>
        <button class="btn speed-btn active" onclick="setSpeed(1, this)">1x</button>
        <button class="btn speed-btn" onclick="setSpeed(2, this)">2x</button>
        <button class="btn speed-btn" onclick="setSpeed(4, this)">4x</button>
        <button class="btn speed-btn" onclick="setSpeed(8, this)">8x</button>
      </div>
      <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
        <button class="btn" onclick="reloadVideo()">🔄 Refresh</button>
        <a id="dl-btn" href="/api/timelapse/video.mp4?codec=hevc&download=1" class="btn primary" download="momcozy_timelapse.mp4" title="Native fast HEVC stream-copy (instant download)">⬇ Download MP4</a>
        <a id="dl-h264-btn" href="/api/timelapse/video.mp4?codec=h264&download=1" class="btn" download="momcozy_timelapse_h264.mp4" title="H.264 version for legacy players" style="font-size:0.75rem;">⬇ Download H.264</a>
      </div>
    </div>
  </div>

  <script>
    const video = document.getElementById('timelapse-video');
    const loading = document.getElementById('loading');
    const errorBox = document.getElementById('error-box');
    const dlBtn = document.getElementById('dl-btn');

    function canPlayHevc() {
      const v = document.createElement('video');
      const types = [
        'video/mp4; codecs="hvc1.1.6.L150.B0"',
        'video/mp4; codecs="hev1.1.6.L150.B0"',
        'video/mp4; codecs="hvc1.1.6.L93.B0"',
        'video/mp4; codecs="hvc1"'
      ];
      for (const t of types) {
        const can = v.canPlayType(t);
        if (can === 'probably' || can === 'maybe') return true;
      }
      return false;
    }

    let activeCodec = canPlayHevc() ? 'hevc' : 'h264';
    let fallbackAttempted = false;
    let currentRatio = 60;

    function updateDownloadLinks() {
      if (dlBtn) {
        dlBtn.href = `/api/timelapse/video.mp4?codec=hevc&ratio=${currentRatio}&download=1`;
      }
      const dlH264 = document.getElementById('dl-h264-btn');
      if (dlH264) {
        dlH264.href = `/api/timelapse/video.mp4?codec=h264&ratio=${currentRatio}&download=1`;
      }
    }

    function loadPlayback(codec) {
      activeCodec = codec;
      loading.style.display = 'flex';
      errorBox.style.display = 'none';
      const url = `/api/timelapse/video.mp4?codec=${codec}&ratio=${currentRatio}&t=${Date.now()}`;
      video.src = url;
      updateDownloadLinks();
      video.load();
      video.play().catch(function() {});
    }

    video.onloadeddata = function() {
      loading.style.display = 'none';
    };

    video.onerror = function(e) {
      console.warn('[Timelapse Player] Video error with codec ' + activeCodec, e);
      if (activeCodec === 'hevc' && !fallbackAttempted) {
        console.info('[Timelapse Player] Retrying with H.264 fallback...');
        fallbackAttempted = true;
        loadPlayback('h264');
        return;
      }
      loading.style.display = 'none';
      errorBox.style.display = 'flex';
    };

    let motionEvents = [];

    function formatTimeSec(seconds) {
      const s = Math.max(0, Math.floor(seconds));
      const m = Math.floor(s / 60);
      const rem = s % 60;
      return `${m}:${rem < 10 ? '0' : ''}${rem}`;
    }

    function renderTimelineMarkers() {
      const container = document.getElementById('timeline-markers');
      if (!container) return;
      container.innerHTML = '';
      const counter = document.getElementById('events-counter-badge');
      if (counter) {
        counter.textContent = `🏃 ${motionEvents.length} Motion Event${motionEvents.length === 1 ? '' : 's'}`;
      }

      motionEvents.forEach((ev) => {
        const el = document.createElement('div');
        el.className = 'timeline-marker';
        el.style.left = `${ev.percent}%`;

        el.addEventListener('mouseenter', () => {
          const tt = document.getElementById('timeline-tooltip');
          if (tt) {
            tt.textContent = `🏃 ${ev.label} • ${ev.time_str} (${ev.video_formatted})`;
            tt.style.left = `${ev.percent}%`;
            tt.style.display = 'block';
          }
        });
        el.addEventListener('mouseleave', () => {
          const tt = document.getElementById('timeline-tooltip');
          if (tt) tt.style.display = 'none';
        });
        el.addEventListener('click', (e) => {
          e.stopPropagation();
          seekToSec(ev.video_sec);
        });
        container.appendChild(el);
      });
    }

    function seekToSec(sec) {
      if (video.duration) {
        video.currentTime = Math.min(video.duration, Math.max(0, sec));
        video.play().catch(() => {});
      }
    }

    function seekFromTimeline(e) {
      const bar = document.getElementById('timeline-bar');
      if (!bar || !video.duration) return;
      const rect = bar.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      video.currentTime = ratio * video.duration;
    }

    function jumpToNextEvent() {
      if (!motionEvents.length || !video.duration) return;
      const cur = video.currentTime;
      const next = motionEvents.find(ev => ev.video_sec > cur + 0.8);
      if (next) {
        seekToSec(next.video_sec);
      } else if (motionEvents.length) {
        seekToSec(motionEvents[0].video_sec);
      }
    }

    function jumpToPrevEvent() {
      if (!motionEvents.length || !video.duration) return;
      const cur = video.currentTime;
      const prev = [...motionEvents].reverse().find(ev => ev.video_sec < cur - 0.8);
      if (prev) {
        seekToSec(prev.video_sec);
      } else if (motionEvents.length) {
        seekToSec(motionEvents[motionEvents.length - 1].video_sec);
      }
    }

    window.addEventListener('keydown', (e) => {
      if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT')) return;
      if (e.key === 'n' || e.key === 'N') jumpToNextEvent();
      else if (e.key === 'p' || e.key === 'P') jumpToPrevEvent();
    });

    video.addEventListener('timeupdate', () => {
      const curEl = document.getElementById('current-time-text');
      const durEl = document.getElementById('duration-time-text');
      const progress = document.getElementById('timeline-progress');
      const thumb = document.getElementById('timeline-thumb');
      if (curEl) curEl.textContent = formatTimeSec(video.currentTime);
      if (durEl && video.duration) durEl.textContent = formatTimeSec(video.duration);
      if (video.duration) {
        const pct = Math.min(100, Math.max(0, (video.currentTime / video.duration) * 100));
        if (progress) progress.style.width = `${pct}%`;
        if (thumb) thumb.style.left = `${pct}%`;
      }
    });

    video.addEventListener('loadedmetadata', () => {
      const durEl = document.getElementById('duration-time-text');
      if (durEl && video.duration) durEl.textContent = formatTimeSec(video.duration);
    });

    function setSpeed(rate, btn) {
      video.playbackRate = rate;
      document.querySelectorAll('.speed-btn').forEach(function(b) {
        b.classList.remove('active');
      });
      if (btn) btn.classList.add('active');
    }

    function reloadVideo() {
      fallbackAttempted = false;
      loadPlayback(activeCodec);
    }

    async function initPlayer() {
      try {
        const res = await fetch('/api/timelapse/status');
        const st = await res.json();
        if (st.speed_ratio_val) {
          currentRatio = st.speed_ratio_val;
          const badge = document.getElementById('player-ratio-badge');
          if (badge) badge.textContent = `1:${currentRatio}`;
        }
        if (st.events) {
          motionEvents = st.events;
          renderTimelineMarkers();
        }
        if (st.sync_night_vision) {
          const nvBadge = document.getElementById('player-nv-sync-badge');
          if (nvBadge) nvBadge.style.display = 'inline-block';
        }
      } catch (e) {}
      loadPlayback(activeCodec);
    }

    initPlayer();
  </script>
</body>
</html>
"""


BABY_FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <circle cx="32" cy="32" r="30" fill="#E0F2FE" stroke="#38BDF8" stroke-width="2"/>
  <circle cx="32" cy="34" r="19" fill="#FED7AA"/>
  <!-- Hair tuft -->
  <path d="M32 15 C34 9 39 12 37 16 C35 16 33 16 32 15 Z" fill="#9A3412"/>
  <!-- Eyes -->
  <circle cx="26" cy="32" r="2.5" fill="#1E293B"/>
  <circle cx="38" cy="32" r="2.5" fill="#1E293B"/>
  <circle cx="25.2" cy="31.2" r="0.9" fill="#FFFFFF"/>
  <circle cx="37.2" cy="31.2" r="0.9" fill="#FFFFFF"/>
  <!-- Rosy Cheeks -->
  <ellipse cx="22" cy="37" rx="3.5" ry="2" fill="#FB7185" opacity="0.6"/>
  <ellipse cx="42" cy="37" rx="3.5" ry="2" fill="#FB7185" opacity="0.6"/>
  <!-- Pacifier -->
  <circle cx="32" cy="41" r="4.5" fill="#38BDF8"/>
  <circle cx="32" cy="41" r="2" fill="#FFFFFF"/>
  <path d="M29 44 C29 48 35 48 35 44" fill="none" stroke="#0284C7" stroke-width="1.8" stroke-linecap="round"/>
</svg>"""


def create_ssl_context(certfile: Optional[str] = None, keyfile: Optional[str] = None) -> ssl.SSLContext:
    """Create an SSL context for HTTPS.

    If certfile and keyfile are omitted, automatically creates a self-signed
    certificate in the config directory (or ~/.momcozy_ssl) for seamless HTTPS.
    """
    if not certfile or not keyfile:
        from .cloud import DEFAULT_CONFIG_PATH
        config_dir = DEFAULT_CONFIG_PATH.parent

        # Check candidate locations for existing certificates
        candidate_dirs = [
            config_dir / "ssl",
            config_dir / ".momcozy_ssl",
            Path.home() / ".momcozy_ssl",
        ]
        ssl_dir = None
        for d in candidate_dirs:
            try:
                if (d / "cert.pem").exists() and (d / "key.pem").exists():
                    ssl_dir = d
                    break
            except Exception:
                continue

        if ssl_dir is None:
            # Prefer config_dir/ssl if writable, fallback to home directory
            try:
                target_dir = config_dir / "ssl"
                target_dir.mkdir(parents=True, exist_ok=True)
                ssl_dir = target_dir
            except Exception:
                ssl_dir = Path.home() / ".momcozy_ssl"
                ssl_dir.mkdir(parents=True, exist_ok=True)

        cert_path = ssl_dir / "cert.pem"
        key_path = ssl_dir / "key.pem"

        if not cert_path.exists() or not key_path.exists():
            logger.info("Generating self-signed SSL certificate in %s for HTTPS...", ssl_dir)
            cmd = [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key_path), "-out", str(cert_path),
                "-days", "3650", "-subj", "/CN=momcozy",
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                key_path.chmod(0o600)
            except Exception:
                pass

        certfile = str(cert_path)
        keyfile = str(key_path)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return ctx


def detect_night_vision_from_jpeg(frame_bytes: bytes, threshold: float = 8.0) -> bool:
    """Detect whether a JPEG frame is infrared monochrome (Night Vision active).

    Infrared night vision frames are grayscale, where R, G, B channels are virtually identical.
    Daytime frames have high color saturation where |R-G| + |G-B| + |B-R| is high.
    Subsamples to a 16x16 thumbnail for ultra-fast, cheap computation (<1.5ms).
    """
    if not frame_bytes or not isinstance(frame_bytes, (bytes, bytearray)):
        return False
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
        img = img.resize((16, 16), Image.Resampling.BOX)
        raw = img.tobytes()
        n = len(raw) // 3
        if n == 0:
            return False
        total_diff = sum(
            abs(raw[i] - raw[i + 1]) + abs(raw[i + 1] - raw[i + 2]) + abs(raw[i + 2] - raw[i])
            for i in range(0, len(raw), 3)
        )
        mean_diff = total_diff / n
        return mean_diff < threshold
    except Exception as exc:
        logger.debug("detect_night_vision_from_jpeg error: %s", exc)
        return False


class WebServer:
    """Embedded aiohttp server hosting the BM04 web dashboard and MJPEG stream."""

    def __init__(
        self,
        monitor: MomcozyMonitor,
        streamer: Optional[VideoStreamer] = None,
        host: str = "0.0.0.0",
        port: int = 8421,
        http_port: Optional[int] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
    ) -> None:
        self.monitor = monitor
        self.streamer = streamer
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        if http_port is not None:
            self.http_port = http_port
        else:
            self.http_port = 8420 if self.ssl_context else self.port
        self.hls: Optional[HlsTranscoder] = HlsTranscoder(streamer=self.streamer) if self.streamer else None
        self.timelapse: Optional[TimelapseRecorder] = (
            TimelapseRecorder(streamer=self.streamer) if self.streamer else None
        )
        if self.streamer:
            self.streamer.keepalive_predicate = lambda: bool(
                (self.timelapse and self.timelapse.is_recording)
                or (self._nv_active_start is not None)
            )
        self._cached_status: Optional[Any] = None
        self._cached_status_time: float = 0.0
        self._nv_active_start: Optional[float] = None
        self._nv_inactive_start: Optional[float] = None
        self._last_detected_nv: bool = False
        self._last_nv_frame_check: float = 0.0
        self._last_nv_poll_time: float = 0.0
        self._nv_session_was_active: bool = False
        self._nv_manual_stopped: bool = False
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/favicon.ico", self.handle_favicon)
        self.app.router.add_get("/stream", self.handle_stream)
        self.app.router.add_get("/ws/stream", self.handle_ws_stream)
        self.app.router.add_get("/audio", self.handle_audio)
        self.app.router.add_get("/audio.mp3", self.handle_audio)
        self.app.router.add_get("/hls/stream.m3u8", self.handle_hls_playlist)
        self.app.router.add_get("/hls/{segment}", self.handle_hls_segment)
        self.app.router.add_get("/snapshot", self.handle_snapshot)
        self.app.router.add_get("/detection_snapshot.jpg", self.handle_detection_snapshot)
        self.app.router.add_get("/api/detection_snapshot", self.handle_detection_snapshot)
        self.app.router.add_get("/api/status", self.handle_api_status)
        self.app.router.add_post("/api/control", self.handle_api_control)
        self.app.router.add_post("/api/timelapse/start", self.handle_timelapse_start)
        self.app.router.add_post("/api/timelapse/stop", self.handle_timelapse_stop)
        self.app.router.add_post("/api/timelapse/ratio", self.handle_timelapse_ratio)
        self.app.router.add_post("/api/timelapse/clear", self.handle_timelapse_clear)
        self.app.router.add_post("/api/timelapse/erase", self.handle_timelapse_clear)
        self.app.router.add_post("/api/timelapse/sync_night_vision", self.handle_timelapse_sync_night_vision)
        self.app.router.add_get("/api/timelapse/status", self.handle_timelapse_status)
        self.app.router.add_post("/api/timelapse/event", self.handle_timelapse_record_event)
        self.app.router.add_get("/api/timelapse/video.mp4", self.handle_timelapse_video)
        self.app.router.add_get("/timelapse", self.handle_timelapse_page)

    async def handle_favicon(self, request: web.Request) -> web.Response:
        return web.Response(
            text=BABY_FAVICON_SVG,
            content_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @staticmethod
    def get_local_lan_ip() -> str:
        """Determine primary local LAN IP address so LAN devices (like Chromecast) can reach the server."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    async def handle_index(self, request: web.Request) -> web.Response:
        lan_ip = self.get_local_lan_ip()
        html = HTML_DASHBOARD.replace(
            "window.serverLanIp = '';",
            f"window.serverLanIp = '{lan_ip}'; window.serverPort = {self.port}; window.serverHttpPort = {self.http_port};",
            1,
        )
        return web.Response(text=html, content_type="text/html")

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        """Serve live video as multipart/x-mixed-replace MJPEG stream."""
        client_ip = request.remote or "unknown"
        if not self.streamer:
            logger.warning("Rejected /stream from %s: Live video streamer is not configured (missing Tuya credentials)", client_ip)
            return web.Response(
                status=503,
                text="Live video stream is not configured: Missing Tuya credentials.",
                content_type="text/plain",
            )

        if not self.streamer.is_running:
            logger.info("Incoming /stream request from %s: Starting live video streamer on demand...", client_ip)
            try:
                await self.streamer.start()
            except Exception as e:
                logger.error("Could not start live video streamer for %s: %s", client_ip, e)
                return web.Response(
                    status=502,
                    text=f"Could not start video stream: {e}",
                    content_type="text/plain",
                )

        logger.info("Serving /stream MJPEG response to client %s", client_ip)
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-cache, no-store, must-revalidate, pre-check=0, post-check=0, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                "Connection": "close",
            },
        )
        await response.prepare(request)

        last_sent = 0.0
        min_interval = 0.065  # Target smooth ~15 FPS to prevent browser buffer saturation
        frames_sent = 0

        try:
            async for frame in self.streamer.subscribe():
                now = time.monotonic()
                if now - last_sent < min_interval:
                    continue
                last_sent = now
                header = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode("ascii") + b"\r\n\r\n"
                )
                await response.write(header + frame + b"\r\n")
                frames_sent += 1
        except (asyncio.CancelledError, ConnectionResetError):
            logger.info("Client %s disconnected from /stream (sent %d frames)", client_ip, frames_sent)
        except Exception as e:
            logger.warning("Client %s /stream write error: %s (sent %d frames)", client_ip, e, frames_sent)

        return response

    async def handle_ws_stream(self, request: web.Request) -> web.WebSocketResponse:
        """Serve raw HEVC video frames over WebSocket for WebCodecs API playback."""
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        client_ip = request.remote or "unknown"

        if not self.streamer:
            logger.warning("Rejected /ws/stream from %s: Live video streamer is not configured", client_ip)
            await ws.close(code=4004, message=b"Streamer not configured")
            return ws

        if not self.streamer.is_running:
            logger.info("Incoming /ws/stream connection from %s: Starting live video streamer on demand...", client_ip)
            try:
                await self.streamer.start()
            except Exception as e:
                logger.error("Could not start live video streamer for %s: %s", client_ip, e)
                await ws.close(code=4002, message=str(e).encode("utf-8"))
                return ws

        logger.info("Client %s connected to /ws/stream (HEVC pass-through)", client_ip)
        frames_sent = 0

        async def send_frames() -> None:
            nonlocal frames_sent
            async for chunk, is_key in self.streamer.subscribe_hevc():
                if ws.closed:
                    break
                header = b"\x01" if is_key else b"\x00"
                await ws.send_bytes(header + chunk)
                frames_sent += 1

        async def ws_reader() -> None:
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass

        sender_task = asyncio.create_task(send_frames())
        reader_task = asyncio.create_task(ws_reader())

        done, pending = await asyncio.wait(
            [sender_task, reader_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        for task in done:
            if not task.cancelled() and task.exception():
                exc = task.exception()
                if not isinstance(exc, (ConnectionResetError, asyncio.CancelledError)):
                    logger.warning("Client %s /ws/stream error: %s (sent %d frames)", client_ip, exc, frames_sent)

        logger.info("Client %s disconnected from /ws/stream (sent %d frames)", client_ip, frames_sent)
        if not ws.closed:
            await ws.close()
        return ws

    async def handle_audio(self, request: web.Request) -> web.StreamResponse:
        """Serve live microphone audio stream (MP3 or WAV)."""
        client_ip = request.remote or "unknown"
        if not self.streamer:
            logger.warning("Rejected /audio from %s: Streamer is not configured", client_ip)
            return web.Response(
                status=503,
                text="Live audio stream is not configured: Missing Tuya credentials.",
                content_type="text/plain",
            )

        fmt = request.query.get("format", "mp3").lower()
        if fmt not in ("mp3", "wav"):
            fmt = "mp3"

        if not self.streamer.is_running:
            logger.info("Incoming /audio request from %s: Starting streamer on demand...", client_ip)
            try:
                await self.streamer.start()
            except Exception as e:
                logger.error("Could not start streamer for /audio from %s: %s", client_ip, e)
                return web.Response(
                    status=502,
                    text=f"Could not start audio stream: {e}",
                    content_type="text/plain",
                )

        content_type = "audio/mpeg" if fmt == "mp3" else "audio/wav"
        logger.info("Serving /audio stream (%s) to client %s", fmt, client_ip)
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": content_type,
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                "Connection": "close",
            },
        )
        await response.prepare(request)

        bytes_sent = 0
        try:
            async for chunk in self.streamer.subscribe_audio(format=fmt):
                await response.write(chunk)
                bytes_sent += len(chunk)
        except (asyncio.CancelledError, ConnectionResetError):
            logger.info("Client %s disconnected from /audio (sent %d bytes)", client_ip, bytes_sent)
        except Exception as e:
            logger.warning("Client %s /audio write error: %s (sent %d bytes)", client_ip, e, bytes_sent)

        return response

    async def handle_snapshot(self, request: web.Request) -> web.Response:
        """Return single snapshot JPEG."""
        if not self.streamer:
            return web.Response(status=503, text="Video streamer not configured")
        try:
            frame = await self.streamer.get_snapshot(timeout_seconds=5)
            return web.Response(
                body=frame,
                content_type="image/jpeg",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )
        except Exception as e:
            return web.Response(status=504, text=f"Snapshot error: {e}")

    async def handle_detection_snapshot(self, request: web.Request) -> web.Response:
        """Return the latest detection event snapshot JPEG."""
        if not self.streamer:
            return web.Response(status=503, text="Video streamer not configured")
        try:
            frame = await self.streamer.get_detection_snapshot(timeout_seconds=5)
            if not frame:
                return web.Response(status=404, text="No detection snapshot available yet")
            return web.Response(
                body=frame,
                content_type="image/jpeg",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )
        except Exception as e:
            return web.Response(status=504, text=f"Detection snapshot error: {e}")

    async def handle_hls_playlist(self, request: web.Request) -> web.Response:
        """Serve live HLS playlist (.m3u8) for Chromecast and smart displays."""
        if not self.hls:
            return web.Response(
                status=503,
                text="Live video streamer is not configured: Missing Tuya credentials.",
                content_type="text/plain",
                headers={"Access-Control-Allow-Origin": "*"},
            )
        if not self.hls.is_available:
            return web.Response(
                status=501,
                text="FFmpeg is not installed on this host. Install FFmpeg to enable HLS streaming.",
                content_type="text/plain",
                headers={"Access-Control-Allow-Origin": "*"},
            )
        playlist = await self.hls.get_playlist(timeout_seconds=10.0)
        if playlist is None:
            return web.Response(
                status=503,
                text="HLS stream is starting up or unavailable. Please retry in a moment.",
                content_type="text/plain",
                headers={"Access-Control-Allow-Origin": "*"},
            )
        return web.Response(
            text=playlist,
            content_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    async def handle_hls_segment(self, request: web.Request) -> web.Response:
        """Serve live MPEG-TS HLS chunk (.ts)."""
        if not self.hls:
            return web.Response(status=503, text="HLS not configured", headers={"Access-Control-Allow-Origin": "*"})
        segment_name = request.match_info.get("segment", "")
        chunk = self.hls.get_segment(segment_name)
        if chunk is None:
            return web.Response(status=404, text=f"Segment {segment_name} not found", headers={"Access-Control-Allow-Origin": "*"})
        return web.Response(
            body=chunk,
            content_type="video/MP2T",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=3600",
            },
        )

    def is_night_vision_active(self, status: Optional[Any], frame_bytes: Optional[bytes] = None) -> bool:
        """Check whether automatic Night Vision is currently engaged on the monitor.

        Only operates when camera is in Automatic night mode (DP 108 == '0').
        Does not react to manually forced night vision (DP 108 == '1' or '2').
        Detects automatic night vision kicking in/out via video frame analysis.
        """
        if not status:
            return False

        nv = getattr(status, "night_vision", "0")
        raw = getattr(status, "raw_dps", {}) or {}
        dp_108 = str(raw.get("108", nv)).strip()

        # Only work with Auto mode ("0"). If manually set to 1 or 2, ignore/inactive.
        if dp_108 != "0":
            return False

        # In Auto mode ("0"), detect physical infrared night vision via frame analysis:
        target_frame = frame_bytes
        if target_frame is None and self.streamer:
            try:
                frame_val = getattr(self.streamer, "latest_frame", None)
                if callable(frame_val):
                    frame_val = frame_val()
                target_frame = frame_val
            except Exception:
                target_frame = None

        if target_frame and isinstance(target_frame, (bytes, bytearray)):
            now_mono = time.monotonic()
            if (now_mono - self._last_nv_frame_check) >= 5.0 or frame_bytes is not None:
                new_nv = detect_night_vision_from_jpeg(target_frame)
                if new_nv != self._last_detected_nv:
                    if new_nv:
                        logger.info("Automatic Night Vision kick-in detected (camera frame transitioned to infrared monochrome).")
                    else:
                        logger.info("Automatic Night Vision kick-out detected (camera frame transitioned to daylight color).")
                self._last_detected_nv = new_nv
                self._last_nv_frame_check = now_mono
            return self._last_detected_nv

        return self._last_detected_nv

    def _get_timelapse_status(self) -> Dict[str, Any]:
        """Return enriched timelapse status including night vision sync state and countdowns."""
        if not self.timelapse:
            return {
                "is_recording": False,
                "has_content": False,
                "total_slots": 72,
                "filled_slots": 0,
                "current_slot": 0,
                "total_frames": 0,
                "buffered_seconds": 0.0,
                "buffered_formatted": "0m 00s",
                "video_duration_seconds": 0.0,
                "video_duration_formatted": "0:00",
                "speed_ratio": "1:60",
                "speed_ratio_val": 60,
                "speed_ratio_options": ["1:15", "1:30", "1:60", "1:90"],
                "disk_bytes": 0,
                "disk_formatted": "0.0 MB",
                "events": [],
                "motion_events_count": 0,
                "sync_night_vision": False,
                "last_session_type": "manual",
                "current_session_type": None,
                "night_vision_active": False,
                "sync_state": "disabled",
                "sync_timer_remaining": None,
            }

        status_dict = self.timelapse.get_status()
        now_mono = time.monotonic()
        is_nv = self.is_night_vision_active(self._cached_status) if self._cached_status else self._last_detected_nv

        sync_state = "disabled"
        timer_remaining: Optional[float] = None
        if self.timelapse.sync_night_vision:
            if self.timelapse.is_recording:
                if (
                    self._nv_inactive_start is not None
                    and (self.timelapse.current_session_type == "auto" or self._nv_session_was_active)
                ):
                    sync_state = "stopping"
                    timer_remaining = max(0.0, 120.0 - (now_mono - self._nv_inactive_start))
                else:
                    sync_state = "recording"
            else:
                if self._nv_active_start is not None and not self._nv_manual_stopped:
                    sync_state = "activating"
                    timer_remaining = max(0.0, 60.0 - (now_mono - self._nv_active_start))
                else:
                    sync_state = "idle"

        status_dict["night_vision_active"] = is_nv
        status_dict["sync_state"] = sync_state
        status_dict["sync_timer_remaining"] = round(timer_remaining, 1) if timer_remaining is not None else None
        return status_dict

    async def handle_api_status(self, request: web.Request) -> web.Response:
        """Return current status as JSON."""
        raw_sm = getattr(self.streamer, "stream_mode", "none") if self.streamer else "none"
        stream_mode = raw_sm if isinstance(raw_sm, str) else "none"

        raw_st = getattr(self.streamer, "transport_description", "Disabled") if self.streamer else "Disabled"
        stream_transport = raw_st if isinstance(raw_st, str) else "Disabled"

        raw_cm = getattr(self.monitor, "control_mode", "cloud")
        control_mode = raw_cm if isinstance(raw_cm, str) else "cloud"

        raw_ct = getattr(self.monitor, "control_transport", "Tuya Cloud Gateway")
        control_transport = raw_ct if isinstance(raw_ct, str) else "Tuya Cloud Gateway"

        raw_cd = getattr(self.monitor, "control_diagnostic", "")
        control_diagnostic = raw_cd if isinstance(raw_cd, str) else ""

        raw_lc = getattr(self.streamer, "listener_count", 0) if self.streamer else 0
        stream_listeners = int(raw_lc) if isinstance(raw_lc, (int, float)) else 0

        raw_hl = getattr(self.streamer, "hevc_listener_count", 0) if self.streamer else 0
        hevc_listeners = int(raw_hl) if isinstance(raw_hl, (int, float)) else 0

        raw_al = getattr(self.streamer, "audio_listener_count", 0) if self.streamer else 0
        audio_listeners = int(raw_al) if isinstance(raw_al, (int, float)) else 0

        raw_it = getattr(self.streamer, "idle_timeout", 0.0) if self.streamer else 0.0
        stream_idle_timeout = float(raw_it) if isinstance(raw_it, (int, float)) else 0.0

        raw_le = getattr(self.streamer, "last_error", None) if self.streamer else None
        stream_error = str(raw_le) if isinstance(raw_le, str) else None

        raw_dt = getattr(self.streamer, "latest_detection_time", None) if self.streamer else None
        detect_time = float(raw_dt) if isinstance(raw_dt, (int, float)) and not isinstance(raw_dt, bool) else None

        raw_trig = getattr(self.streamer, "latest_detection_trigger", "") if self.streamer else ""
        detect_trigger = str(raw_trig) if isinstance(raw_trig, str) else ""

        has_detect_snap = bool(
            self.streamer is not None and (
                isinstance(getattr(self.streamer, "latest_detection_snapshot", None), bytes)
                or isinstance(getattr(self.streamer, "latest_frame", None), bytes)
            )
        )

        raw_sf = getattr(self.streamer, "stream_format", "None") if self.streamer else "None"
        stream_format = raw_sf if isinstance(raw_sf, str) else "None"

        raw_tc = False
        if self.streamer and callable(getattr(self.streamer, "is_transcoding_needed", None)):
            try:
                tc_val = self.streamer.is_transcoding_needed()
                raw_tc = bool(tc_val) if isinstance(tc_val, (bool, int)) else False
            except Exception:
                raw_tc = False

        stream_info = {
            "stream_configured": self.streamer is not None,
            "stream_running": bool(getattr(self.streamer, "is_running", False)) if self.streamer else False,
            "stream_connected": bool(getattr(self.streamer, "is_connected", False)) if self.streamer else False,
            "stream_listeners": stream_listeners,
            "hevc_listeners": hevc_listeners,
            "total_video_listeners": stream_listeners + hevc_listeners,
            "audio_available": True,
            "audio_listeners": audio_listeners,
            "has_detection_snapshot": has_detect_snap,
            "detection_trigger": detect_trigger,
            "detection_time": detect_time,
            "detection_time_str": (
                time.strftime("%b %d, %Y %H:%M:%S", time.localtime(detect_time))
                if detect_time
                else ""
            ),
            "stream_idle_timeout": stream_idle_timeout,
            "stream_error": stream_error,
            "stream_mode": stream_mode,
            "stream_transport": stream_transport,
            "stream_format": stream_format,
            "transcoding": raw_tc,
            "control_mode": control_mode,
            "control_transport": control_transport,
            "control_diagnostic": control_diagnostic,
            "server_lan_ip": self.get_local_lan_ip(),
            "server_port": self.port,
            "server_http_port": self.http_port,
            "timelapse": self._get_timelapse_status(),
        }
        try:
            now = time.monotonic()
            if self._cached_status is not None and (now - self._cached_status_time) < 1.5:
                status = self._cached_status
            else:
                status = await asyncio.to_thread(self.monitor.get_status)
                self._cached_status = status
                self._cached_status_time = now

            raw_cmd = getattr(status, "latest_event_cmd", "")
            latest_event_cmd = str(raw_cmd) if isinstance(raw_cmd, str) else ""

            raw_name = getattr(status, "latest_event_name", "")
            latest_event_name = str(raw_name) if isinstance(raw_name, str) else ""

            raw_ev_time = getattr(status, "latest_event_time", None)
            latest_event_time = int(raw_ev_time) if isinstance(raw_ev_time, (int, float)) and not isinstance(raw_ev_time, bool) else None

            raw_time_str = getattr(status, "latest_event_time_str", "")
            latest_event_time_str = str(raw_time_str) if isinstance(raw_time_str, str) else ""

            raw_file = getattr(status, "latest_event_file", "")
            latest_event_file = str(raw_file) if isinstance(raw_file, str) else ""

            raw_bucket = getattr(status, "latest_event_bucket", "")
            latest_event_bucket = str(raw_bucket) if isinstance(raw_bucket, str) else ""

            if self.timelapse and self.timelapse.is_recording:
                if latest_event_cmd in ("ipc_motion", "ipc_human", "ipc_crying", "ipc_alarm") and latest_event_time:
                    self.timelapse.record_event(
                        event_type="motion",
                        label=latest_event_name or "Motion Alert",
                        timestamp=float(latest_event_time),
                    )
                elif getattr(status, "motion_detected", False):
                    self.timelapse.record_event(
                        event_type="motion",
                        label="Motion Detected",
                    )

            return web.json_response({
                "online": True,
                "temperature_c": status.temperature_c,
                "temperature_f": status.temperature_f,
                "temp_alert_enabled": status.temp_alert_enabled,
                "temp_min_c": status.temp_min_c,
                "temp_max_c": status.temp_max_c,
                "motion_switch": status.motion_switch,
                "motion_sensitivity": status.motion_sensitivity,
                "sound_switch": status.sound_switch,
                "cry_detection_switch": status.cry_detection_switch,
                "privacy_mode": status.privacy_mode,
                "night_vision": status.night_vision,
                "lullaby_playing": status.lullaby_playing,
                "lullaby_track": status.lullaby_track,
                "master_volume": status.master_volume,
                "sd_card_status": status.sd_card_status,
                "latest_event_cmd": latest_event_cmd,
                "latest_event_name": latest_event_name,
                "latest_event_time": latest_event_time,
                "latest_event_time_str": latest_event_time_str,
                "latest_event_file": latest_event_file,
                "latest_event_bucket": latest_event_bucket,
                **stream_info,
            })
        except Exception as e:
            logger.debug("Could not read device status: %s", e)
            return web.json_response({
                "online": False,
                "error": str(e),
                "temperature_c": None,
                "temperature_f": None,
                "temp_alert_enabled": False,
                "temp_min_c": None,
                "temp_max_c": None,
                "motion_switch": False,
                "motion_sensitivity": "1",
                "sound_switch": False,
                "cry_detection_switch": False,
                "privacy_mode": False,
                "night_vision": "0",
                "lullaby_playing": False,
                "lullaby_track": "0",
                "master_volume": 50,
                "sd_card_status": 1,
                "latest_event_cmd": "",
                "latest_event_name": "",
                "latest_event_time": None,
                "latest_event_time_str": "",
                "latest_event_file": "",
                "latest_event_bucket": "",
                **stream_info,
            })

    async def handle_api_control(self, request: web.Request) -> web.Response:
        """Handle control actions from web UI."""
        data = {}
        try:
            data = await request.json()
            action = data.get("action")

            if action == "capture_detection_snapshot":
                if self.streamer:
                    frame = await self.streamer.get_snapshot(timeout_seconds=5)
                    self.streamer.capture_detection_snapshot(trigger="Manual UI Capture", frame=frame)
                    return web.json_response({"status": "ok", "captured": True})
                return web.json_response({"status": "error", "error": "Streamer not available"}, status=503)
            elif action == "ptz_step":
                direction = data.get("direction", "0")
                duration = float(data.get("duration", 0.175))
                await asyncio.to_thread(self.monitor.ptz_step, direction, duration)
            elif action == "ptz_move":
                direction = data.get("direction", "0")
                await asyncio.to_thread(self.monitor.ptz_move, direction)
            elif action == "ptz_stop":
                await asyncio.to_thread(self.monitor.ptz_stop)
            elif action == "night_vision":
                mode = data.get("mode", "0")
                await asyncio.to_thread(self.monitor.set_night_vision, mode)
            elif action == "toggle_privacy":
                st = await asyncio.to_thread(self.monitor.get_status)
                await asyncio.to_thread(self.monitor.set_privacy_mode, not st.privacy_mode)
            elif action == "toggle_led":
                st = await asyncio.to_thread(self.monitor.get_status)
                await asyncio.to_thread(self.monitor.set_indicator_light, not st.indicator_light)
            elif action == "toggle_flip":
                st = await asyncio.to_thread(self.monitor.get_status)
                await asyncio.to_thread(self.monitor.set_flip, not st.image_flipped)
            elif action == "lullaby":
                play = data.get("play", True)
                if play:
                    track = data.get("track", "0")
                    vol = data.get("volume", 50)
                    await asyncio.to_thread(self.monitor.play_lullaby, track, vol)
                else:
                    await asyncio.to_thread(self.monitor.stop_lullaby)
            elif action == "lullaby_volume":
                vol = data.get("volume", 50)
                await asyncio.to_thread(self.monitor.set_lullaby_volume, vol)
            elif action == "timelapse_sync_night_vision":
                if not self.timelapse:
                    return web.json_response({"status": "error", "error": "Timelapse recorder not available"}, status=503)
                enabled = data.get("enabled")
                if enabled is None:
                    enabled = not self.timelapse.sync_night_vision
                self.timelapse.set_sync_night_vision(bool(enabled))
                self._nv_active_start = None
                self._nv_inactive_start = None
                return web.json_response({
                    "status": "ok",
                    "sync_night_vision": self.timelapse.sync_night_vision,
                    "timelapse": self._get_timelapse_status(),
                })
            elif action in ("timelapse_clear", "timelapse_erase"):
                if not self.timelapse:
                    return web.json_response({"status": "error", "error": "Timelapse recorder not available"}, status=503)
                if self.timelapse.is_recording:
                    await self.timelapse.stop()
                self._nv_manual_stopped = False
                self._nv_active_start = None
                self._nv_inactive_start = None
                self._nv_session_was_active = False
                self.timelapse.clear()
                return web.json_response({
                    "status": "ok",
                    "timelapse": self._get_timelapse_status(),
                })

            self._cached_status = None
            return web.json_response({"status": "ok"})
        except Exception as e:
            action_name = data.get("action", "unknown") if isinstance(data, dict) else "unknown"
            logger.warning("Device action '%s' failed: %s", action_name, e)
            return web.json_response({"status": "error", "error": str(e)}, status=502)

    async def handle_timelapse_start(self, request: web.Request) -> web.Response:
        """Start or resume recording the 12-hour circular timelapse (manual session)."""
        if not self.timelapse:
            return web.json_response(
                {"success": False, "error": "Timelapse recorder not configured (missing video streamer)."},
                status=503,
            )
        try:
            fresh = False
            if request.can_read_body:
                try:
                    data = await request.json()
                    ratio = data.get("ratio")
                    if ratio is not None:
                        self.timelapse.set_speed_ratio(ratio)
                    if "fresh" in data:
                        fresh = bool(data["fresh"])
                except Exception:
                    pass
            self._nv_manual_stopped = False
            await self.timelapse.start(fresh=fresh, session_type="manual")
            return web.json_response({"success": True, "timelapse": self._get_timelapse_status()})
        except Exception as exc:
            logger.error("Error starting timelapse: %s", exc)
            return web.json_response({"success": False, "error": str(exc)}, status=500)

    async def handle_timelapse_stop(self, request: web.Request) -> web.Response:
        """Stop recording the timelapse."""
        if not self.timelapse:
            return web.json_response(
                {"success": False, "error": "Timelapse recorder not configured."},
                status=503,
            )
        try:
            self._nv_manual_stopped = True
            self._nv_active_start = None
            self._nv_inactive_start = None
            self._nv_session_was_active = False
            await self.timelapse.stop()
            return web.json_response({"success": True, "timelapse": self._get_timelapse_status()})
        except Exception as exc:
            logger.error("Error stopping timelapse: %s", exc)
            return web.json_response({"success": False, "error": str(exc)}, status=500)

    async def handle_timelapse_clear(self, request: web.Request) -> web.Response:
        """Clear all previous timelapse slot recordings and metadata (wipe)."""
        if not self.timelapse:
            return web.json_response(
                {"success": False, "error": "Timelapse recorder not configured."},
                status=503,
            )
        try:
            if self.timelapse.is_recording:
                await self.timelapse.stop()
            self._nv_manual_stopped = False
            self._nv_active_start = None
            self._nv_inactive_start = None
            self._nv_session_was_active = False
            self.timelapse.clear()
            logger.info("Timelapse footage erased by user request.")
            return web.json_response({"success": True, "timelapse": self._get_timelapse_status()})
        except Exception as exc:
            logger.error("Error clearing timelapse: %s", exc)
            return web.json_response({"success": False, "error": str(exc)}, status=500)

    async def handle_timelapse_ratio(self, request: web.Request) -> web.Response:
        """Update timelapse speed ratio (15, 30, 60, 90)."""
        if not self.timelapse:
            return web.json_response({"success": False, "error": "Timelapse recorder not configured."}, status=503)
        try:
            data = await request.json()
            ratio = data.get("ratio")
            if ratio is None:
                return web.json_response({"success": False, "error": "Missing 'ratio' parameter."}, status=400)
            self.timelapse.set_speed_ratio(ratio)
            return web.json_response({"success": True, "timelapse": self._get_timelapse_status()})
        except ValueError as exc:
            return web.json_response({"success": False, "error": str(exc)}, status=400)
        except Exception as exc:
            logger.error("Error setting timelapse speed ratio: %s", exc)
            return web.json_response({"success": False, "error": str(exc)}, status=500)

    async def handle_timelapse_sync_night_vision(self, request: web.Request) -> web.Response:
        """Toggle or configure automatic timelapse synchronization with Night Vision."""
        if not self.timelapse:
            return web.json_response({"success": False, "error": "Timelapse recorder not configured."}, status=503)
        try:
            data = await request.json() if request.can_read_body else {}
        except Exception:
            data = {}
        if "enabled" in data and data["enabled"] is not None:
            enabled = bool(data["enabled"])
        else:
            enabled = not self.timelapse.sync_night_vision

        self.timelapse.set_sync_night_vision(enabled)
        self._nv_active_start = None
        self._nv_inactive_start = None
        if self.streamer:
            if enabled:
                self._last_nv_poll_time = 0.0
            else:
                if not self.timelapse.is_recording and getattr(self.streamer, "total_listener_count", 0) == 0:
                    if callable(getattr(self.streamer, "_schedule_idle_stop", None)):
                        self.streamer._schedule_idle_stop()
        return web.json_response({
            "success": True,
            "sync_night_vision": self.timelapse.sync_night_vision,
            "timelapse": self._get_timelapse_status(),
        })

    async def handle_timelapse_status(self, request: web.Request) -> web.Response:
        """Get timelapse recorder status."""
        return web.json_response(self._get_timelapse_status())

    async def handle_timelapse_record_event(self, request: web.Request) -> web.Response:
        """Manually or externally record a motion/detection event."""
        if not self.timelapse:
            return web.json_response({"success": False, "error": "Timelapse recorder not configured."}, status=503)
        try:
            data = await request.json()
        except Exception:
            data = {}
        ev_type = str(data.get("type", "motion"))
        label = str(data.get("label", "Motion Alert"))
        ts = data.get("timestamp")
        timestamp = float(ts) if isinstance(ts, (int, float)) else None
        recorded = self.timelapse.record_event(event_type=ev_type, label=label, timestamp=timestamp)
        return web.json_response({
            "success": True,
            "recorded": recorded,
            "events_count": len(self.timelapse._events),
        })

    async def handle_timelapse_video(self, request: web.Request) -> web.StreamResponse:
        """Serve exported timelapse MP4 with HTTP Range support for scrubbing."""
        if not self.timelapse:
            return web.Response(
                status=503,
                text="Timelapse recorder not configured.",
                content_type="text/plain",
            )
        codec = request.query.get("codec")
        is_download = request.query.get("download") == "1"
        if not codec:
            # Default to fast stream-copy HEVC for instant downloads & high performance
            codec = "hevc"
        else:
            codec = codec.lower().strip()
            if codec not in ("hevc", "h264"):
                codec = "hevc"
        ratio_param = request.query.get("ratio")
        ratio = int(ratio_param) if (ratio_param and ratio_param.isdigit()) else None
        try:
            mp4_path = await self.timelapse.export_mp4(codec=codec, ratio=ratio)
        except Exception as exc:
            logger.error("Error generating timelapse MP4: %s", exc)
            return web.Response(status=500, text=f"Error exporting timelapse: {exc}", content_type="text/plain")

        if not mp4_path or not mp4_path.exists():
            return web.Response(status=404, text="No timelapse video frames recorded yet.", content_type="text/plain")

        effective_ratio = ratio or self.timelapse.speed_ratio
        disposition = f'attachment; filename="momcozy_timelapse_{codec}_r{effective_ratio}.mp4"' if is_download else "inline"

        return SafeFileResponse(
            mp4_path,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
                "Content-Disposition": disposition,
            },
        )

    async def handle_timelapse_page(self, request: web.Request) -> web.Response:
        """Serve the standalone timelapse player page."""
        return web.Response(text=HTML_TIMELAPSE_PLAYER, content_type="text/html")

    async def _background_monitor_loop(self) -> None:
        """Server-side background loop managing:
        1. Night Vision timelapse automation (start on >=60s active, stop on >=120s inactive).
        2. Hardware motion event logging while recording.
        Runs entirely server-side without requiring any connected clients.
        """
        last_ev_time = 0.0
        while True:
            try:
                await asyncio.sleep(4.0)
                if not self.monitor or not self.timelapse:
                    continue

                # If timelapse is neither recording nor configured to sync with Night Vision,
                # reset debounce timers and idle to minimize camera queries.
                if not self.timelapse.is_recording and not self.timelapse.sync_night_vision:
                    self._nv_active_start = None
                    self._nv_inactive_start = None
                    continue

                now_mono = time.monotonic()
                # Query device telemetry
                try:
                    status = await asyncio.to_thread(self.monitor.get_status)
                    self._cached_status = status
                    self._cached_status_time = now_mono
                except Exception as poll_err:
                    logger.debug("Background monitor failed to poll camera status: %s", poll_err)
                    continue

                # 1. Night Vision Automation
                if self.timelapse.sync_night_vision:
                    nv_mode = getattr(status, "night_vision", "0")
                    raw = getattr(status, "raw_dps", {}) or {}
                    dp_108 = str(raw.get("108", nv_mode)).strip()

                    clean_frame: Optional[bytes] = None

                    if dp_108 == "0" and self.streamer:
                        streamer_running = getattr(self.streamer, "is_running", False)
                        if not streamer_running:
                            # Stream is stopped. Periodic check every 5 minutes (300s):
                            if (now_mono - self._last_nv_poll_time >= 300.0) or (self._last_nv_poll_time == 0.0):
                                self._last_nv_poll_time = now_mono
                                logger.info("Checking night vision state (restarting camera stream for clean frame)...")
                                try:
                                    if hasattr(self.streamer, "get_clean_frame"):
                                        res = self.streamer.get_clean_frame(timeout_seconds=25.0)
                                        clean_frame = await res if asyncio.iscoroutine(res) else res
                                    elif hasattr(self.streamer, "get_snapshot"):
                                        res = self.streamer.get_snapshot(timeout_seconds=25.0)
                                        clean_frame = await res if asyncio.iscoroutine(res) else res
                                except Exception as err:
                                    logger.warning("Could not fetch clean frame for night vision check: %s", err)
                                    clean_frame = None
                        else:
                            # Stream is already running (viewers, active recording, or countdown).
                            time_val = getattr(self.streamer, "latest_frame_time", 0.0)
                            latest_time = time_val() if callable(time_val) else float(time_val or 0.0)
                            if (now_mono - latest_time > 10.0) and (now_mono - self._last_nv_frame_check > 8.0):
                                try:
                                    if hasattr(self.streamer, "get_clean_frame"):
                                        res = self.streamer.get_clean_frame(timeout_seconds=5.0)
                                        clean_frame = await res if asyncio.iscoroutine(res) else res
                                    elif hasattr(self.streamer, "get_snapshot"):
                                        res = self.streamer.get_snapshot(timeout_seconds=5.0)
                                        clean_frame = await res if asyncio.iscoroutine(res) else res
                                except Exception as snap_err:
                                    logger.debug("Snapshot query for night vision check failed: %s", snap_err)

                    is_nv = self.is_night_vision_active(status, frame_bytes=clean_frame)
                    if is_nv:
                        # Night vision is active
                        self._nv_inactive_start = None
                        if self.timelapse.is_recording:
                            if not self._nv_session_was_active:
                                logger.info("Night Vision active detected during ongoing recording session (session marked as auto).")
                            self._nv_session_was_active = True
                            self.timelapse.current_session_type = "auto"
                        else:
                            if not self._nv_manual_stopped:
                                if self._nv_active_start is None:
                                    self._nv_active_start = now_mono
                                    logger.info("Night Vision active detected on camera. Starting 60s activation timer...")
                                else:
                                    active_sec = now_mono - self._nv_active_start
                                    if active_sec >= 60.0:
                                        # 1 minute continuous night vision reached
                                        # "If the previous timelapse for automatic, the next automatic one appends to that, not restart."
                                        fresh = (self.timelapse.last_session_type != "auto")
                                        logger.info(
                                            "Night Vision active for >= 60s (%.1fs). Auto-starting timelapse (fresh=%s, prev_session=%s)...",
                                            active_sec,
                                            fresh,
                                            self.timelapse.last_session_type,
                                        )
                                        await self.timelapse.start(fresh=fresh, session_type="auto")
                                        self._nv_session_was_active = True
                    else:
                        # Night vision is inactive
                        self._nv_active_start = None
                        self._nv_manual_stopped = False
                        if self.timelapse.is_recording:
                            # Only auto-stop if session was started as auto or night vision had been active
                            if self.timelapse.current_session_type == "auto" or self._nv_session_was_active:
                                if self._nv_inactive_start is None:
                                    self._nv_inactive_start = now_mono
                                    logger.info("Night Vision inactive detected on camera. Starting 120s deactivation timer...")
                                else:
                                    inactive_sec = now_mono - self._nv_inactive_start
                                    if inactive_sec >= 120.0:
                                        # 2 minutes continuous daylight/inactive reached
                                        logger.info(
                                            "Night Vision inactive for >= 120s (%.1fs). Auto-stopping timelapse...",
                                            inactive_sec,
                                        )
                                        await self.timelapse.stop()
                                        self._nv_session_was_active = False
                                        self._nv_inactive_start = None
                                        # When auto-stop completes, if no client viewers connected, stop camera stream
                                        if (
                                            self.streamer
                                            and getattr(self.streamer, "total_listener_count", 0) == 0
                                        ):
                                            logger.info("Timelapse auto-stopped and no viewers connected. Stopping camera stream.")
                                            res = self.streamer.stop()
                                            if asyncio.iscoroutine(res):
                                                await res
                                            self._last_nv_poll_time = now_mono
                            else:
                                self._nv_inactive_start = None
                        else:
                            # Not recording and night vision is inactive:
                            # If stream was restarted for periodic check and has 0 viewers, stop stream immediately
                            if (
                                clean_frame is not None
                                and self.streamer
                                and getattr(self.streamer, "is_running", False)
                                and getattr(self.streamer, "total_listener_count", 0) == 0
                                and self._nv_active_start is None
                            ):
                                logger.info("Night vision inactive (daylight) and no viewers connected. Stopping camera stream.")
                                res = self.streamer.stop()
                                if asyncio.iscoroutine(res):
                                    await res

                # 2. Motion Events Tracking (while recording)
                if self.timelapse.is_recording:
                    ev_cmd = getattr(status, "latest_event_cmd", "")
                    ev_time = getattr(status, "latest_event_time", None)
                    if ev_cmd in ("ipc_motion", "ipc_human", "ipc_crying", "ipc_alarm") and ev_time:
                        if float(ev_time) != last_ev_time:
                            last_ev_time = float(ev_time)
                            self.timelapse.record_event(
                                event_type="motion",
                                label=getattr(status, "latest_event_name", "Motion Alert") or "Motion Alert",
                                timestamp=float(ev_time),
                            )
                    elif getattr(status, "motion_detected", False):
                        self.timelapse.record_event(
                            event_type="motion",
                            label="Motion Detected",
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Error in server background monitor loop: %s", exc)

    async def _motion_monitor_loop(self) -> None:
        """Alias for backward compatibility."""
        await self._background_monitor_loop()

    async def run(self) -> None:
        """Run the server until interrupted."""
        if self.streamer and not self.streamer.is_running:
            try:
                await self.streamer.start()
            except Exception as e:
                logger.warning("Could not auto-start live video streamer: %s", e)

        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port, ssl_context=self.ssl_context)
        await site.start()
        proto = "https" if self.ssl_context else "http"
        logger.info("Momcozy Web Dashboard running at %s://%s:%d", proto, self.host, self.port)

        if self.ssl_context and self.http_port and self.http_port != self.port:
            try:
                site_http = web.TCPSite(runner, self.host, self.http_port)
                await site_http.start()
                logger.info("Chromecast & LAN HTTP media endpoint running at http://%s:%d", self.host, self.http_port)
            except Exception as e:
                logger.warning("Could not bind HTTP media port %d: %s. Chromecast may fail on self-signed HTTPS.", self.http_port, e)

        bg_task = asyncio.create_task(self._background_monitor_loop())
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            bg_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bg_task
            if self.timelapse and self.timelapse.is_recording:
                await self.timelapse.stop()
            if self.hls:
                await self.hls.stop()
            if self.streamer:
                await self.streamer.stop()
            await runner.cleanup()
