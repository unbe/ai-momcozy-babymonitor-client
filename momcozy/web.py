"""Web Dashboard and MJPEG Streaming Server for Momcozy BM04.

Provides an intuitive web interface inspired by the Momcozy app, with live video,
real-time sensor telemetry (temperature, alerts), and full remote control.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from aiohttp import web

from .chromecast import ChromecastManager, get_local_ip
from .constants import LULLABY_TRACKS, NightVisionMode, PtzDirection
from .device import MomcozyMonitor
from .hls import HlsTranscoder
from .stream import VideoStreamer

logger = logging.getLogger(__name__)

HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Momcozy BM04 Baby Monitor</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.ico">
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
    .video-card img {
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
    .video-ctrl-btn.casting {
      background: rgba(66, 153, 225, 0.85);
      border-color: rgba(66, 153, 225, 1);
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
    .video-card.is-fullscreen img {
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
        <button class="video-ctrl-btn" id="cast-btn" onclick="scrollToCastCard()" title="Cast to Chromecast / TV">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 16.1A5 5 0 0 1 5.9 20M2 12.05A9 9 0 0 1 9.95 20M2 8V6a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-6"/><line x1="2" y1="20" x2="2.01" y2="20"/></svg>
          <span id="cast-btn-text">Cast</span>
        </button>
        <button class="video-ctrl-btn" id="fs-btn" onclick="toggleFullscreen()" title="Toggle Fullscreen">
          <svg id="fs-enter-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>
          <svg id="fs-exit-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="display:none;"><path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/></svg>
          <span id="fs-btn-text">Fullscreen</span>
        </button>
      </div>
      <img id="stream-img" src="/stream" alt="Camera Stream">
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

      <!-- Chromecast Controls -->
      <div class="card" id="chromecast-card">
        <h2>📺 Chromecast / TV <span class="badge" id="cast-status-badge">Idle</span></h2>
        <div style="font-size: 0.82rem; color: var(--text-muted); margin-bottom: 12px; line-height: 1.4;">
          Stream live video to your TV or Google Nest Hub, or cast audio to your Google Home / Nest speakers.
        </div>
        <div class="control-row">
          <span>Target Device</span>
          <select id="cast-device-select" style="max-width: 175px;">
            <option value="">Searching for devices...</option>
          </select>
        </div>
        <div class="control-row" style="margin-top: 6px;">
          <span>Custom Host / IP</span>
          <input type="text" id="cast-custom-ip" placeholder="e.g. 192.168.1.50" style="padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.82rem; width: 165px;">
        </div>
        <div style="display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap;">
          <button class="btn primary" style="flex: 1; font-size: 0.82rem;" onclick="startChromecast('video')">📺 Cast Video</button>
          <button class="btn" style="flex: 1; font-size: 0.82rem;" onclick="startChromecast('audio')">🔊 Cast Audio</button>
          <button class="btn" style="font-size: 0.82rem; color: #E53E3E;" onclick="stopChromecast()">⏹️ Stop</button>
        </div>
        <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 12px; font-size: 0.75rem; color: var(--text-muted);">
          <span id="cast-active-desc">Ready to cast</span>
          <button class="btn" style="padding: 3px 8px; font-size: 0.72rem;" onclick="refreshChromecastDevices()">🔄 Scan Devices</button>
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

    // Stream auto-reconnection, double-click fullscreen, and error overlays
    const streamImg = document.getElementById('stream-img');
    let streamRetryTimer = null;
    let streamRetryCount = 0;

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

    function retryStream() {
      const errOverlay = document.getElementById('video-error');
      if (errOverlay) errOverlay.classList.add('hidden');
      const streamBadge = document.getElementById('stream-badge');
      const liveDot = document.getElementById('live-dot');
      if (streamBadge) streamBadge.innerText = 'RECONNECTING...';
      if (liveDot) {
        liveDot.style.background = '#ED8936';
        liveDot.style.animation = 'pulse 1s infinite';
      }
      if (streamImg) {
        streamImg.src = '/stream?t=' + Date.now();
      }
      showToast('Reconnecting to video stream...', 'info', 2500);
      updateStatus();
    }

    if (streamImg) {
      streamImg.addEventListener('dblclick', (e) => {
        e.preventDefault();
        toggleFullscreen();
      });

      streamImg.onerror = function() {
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
        streamRetryCount = 0;
        const errOverlay = document.getElementById('video-error');
        if (errOverlay) errOverlay.classList.add('hidden');
        const streamBadge = document.getElementById('stream-badge');
        const liveDot = document.getElementById('live-dot');
        if (streamBadge) streamBadge.innerText = 'LIVE STREAM';
        if (liveDot) {
          liveDot.style.background = '#48BB78';
          liveDot.style.animation = 'pulse 1.5s infinite';
        }
      };

      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
          // Re-trigger live stream if device woke up with a stale connection
          if (streamImg && (!streamImg.complete || streamImg.naturalWidth === 0)) {
            streamImg.src = '/stream?t=' + Date.now();
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
        } else if (d.stream_error) {
          setStreamError('Video Stream Error', d.stream_error);
        } else if (d.stream_connected) {
          const errOverlay = document.getElementById('video-error');
          if (errOverlay) errOverlay.classList.add('hidden');
          const streamBadge = document.getElementById('stream-badge');
          const liveDot = document.getElementById('live-dot');
          if (streamBadge) streamBadge.innerText = 'LIVE STREAM';
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

    // Google Cast framework initialization for supported browsers (Chrome, Edge, Android)
    window['__onGCastApiAvailable'] = function(isAvailable) {
      if (isAvailable && window.cast && window.cast.framework) {
        try {
          cast.framework.CastContext.getInstance().setOptions({
            receiverApplicationId: chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
            autoJoinPolicy: chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED
          });
          const castContext = cast.framework.CastContext.getInstance();
          castContext.addEventListener(
            cast.framework.CastContextEventType.SESSION_STATE_CHANGED,
            function(event) {
              const session = castContext.getCurrentSession();
              const badge = document.getElementById('cast-status-badge');
              const castBtn = document.getElementById('cast-btn');
              if (session) {
                if (badge) {
                  badge.textContent = 'Browser Cast';
                  badge.className = 'badge active';
                }
                if (castBtn) castBtn.classList.add('casting');
                const streamUrl = window.location.origin + '/hls/stream.m3u8';
                const mediaInfo = new chrome.cast.media.MediaInfo(streamUrl, 'application/x-mpegURL');
                mediaInfo.streamType = chrome.cast.media.StreamType.LIVE;
                mediaInfo.metadata = new chrome.cast.media.GenericMediaMetadata();
                mediaInfo.metadata.title = 'Momcozy Baby Monitor Live';
                const request = new chrome.cast.media.LoadRequest(mediaInfo);
                session.loadMedia(request);
              } else {
                if (badge && !activeServerCast) {
                  badge.textContent = 'Idle';
                  badge.className = 'badge';
                }
                if (castBtn && !activeServerCast) castBtn.classList.remove('casting');
              }
            }
          );
        } catch(e) {
          console.debug('Google Cast SDK setup notice:', e);
        }
      }
    };

    function scrollToCastCard() {
      const card = document.getElementById('chromecast-card');
      if (card) {
        card.scrollIntoView({ behavior: 'smooth' });
        card.style.borderColor = 'var(--primary)';
        card.style.boxShadow = '0 0 0 3px rgba(255, 126, 152, 0.3)';
        setTimeout(() => {
          card.style.borderColor = 'var(--border)';
          card.style.boxShadow = 'none';
        }, 1500);
      }
      if (window.cast && window.cast.framework) {
        try {
          cast.framework.CastContext.getInstance().requestSession();
        } catch(e) {}
      }
    }

    let activeServerCast = null;
    let knownCastDevices = [];
    async function refreshChromecastDevices() {
      try {
        const res = await fetch('/api/chromecast/devices');
        const data = await res.json();
        const sel = document.getElementById('cast-device-select');
        if (!sel) return;
        sel.innerHTML = '';
        knownCastDevices = data.devices || [];
        if (knownCastDevices.length === 0) {
          const opt = document.createElement('option');
          opt.value = '';
          opt.textContent = data.available ? 'No devices found (enter IP below)' : 'Discovery idle (enter IP below)';
          sel.appendChild(opt);
        } else {
          knownCastDevices.forEach(d => {
            const opt = document.createElement('option');
            opt.value = d.name;
            opt.textContent = `${d.name} (${d.model_name}${d.host ? ' - ' + d.host : ''})`;
            sel.appendChild(opt);
          });
        }
      } catch(e) {
        console.debug('Failed loading Chromecast devices:', e);
      }
    }

    async function startChromecast(mediaType) {
      const sel = document.getElementById('cast-device-select');
      const customIp = document.getElementById('cast-custom-ip').value.trim();
      const target = customIp || (sel ? sel.value : '');

      if (!target) {
        showToast('Please select a Chromecast or enter an IP address', 'warn');
        return;
      }

      showToast(`Connecting to ${target} (${mediaType})...`, 'info', 3000);
      try {
        const res = await fetch('/api/chromecast/cast', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({target, type: mediaType})
        });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          activeServerCast = target;
          showToast(`Now casting ${mediaType} to ${data.device || target}!`, 'success', 4000);
          const badge = document.getElementById('cast-status-badge');
          if (badge) {
            badge.textContent = `Casting ${mediaType}`;
            badge.className = 'badge active';
          }
          const desc = document.getElementById('cast-active-desc');
          if (desc) desc.textContent = `Streaming ${mediaType} to ${data.device || target}`;
          const castBtn = document.getElementById('cast-btn');
          if (castBtn) castBtn.classList.add('casting');
        } else {
          showToast(`Cast failed: ${data.error || 'Unknown error'}`, 'error', 5000);
        }
      } catch(e) {
        showToast(`Cast error: ${e.message}`, 'error', 5000);
      }
    }

    async function stopChromecast() {
      const sel = document.getElementById('cast-device-select');
      const customIp = document.getElementById('cast-custom-ip').value.trim();
      const target = customIp || (sel ? sel.value : '');
      if (!target) {
        showToast('Please select a device to stop', 'warn');
        return;
      }
      try {
        const res = await fetch('/api/chromecast/stop', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({target})
        });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          activeServerCast = null;
          showToast(`Stopped casting on ${target}`, 'info', 3000);
          const badge = document.getElementById('cast-status-badge');
          if (badge) {
            badge.textContent = 'Idle';
            badge.className = 'badge';
          }
          const desc = document.getElementById('cast-active-desc');
          if (desc) desc.textContent = 'Ready to cast';
          const castBtn = document.getElementById('cast-btn');
          if (castBtn) castBtn.classList.remove('casting');
        } else {
          showToast(`Stop failed: ${data.error || 'Unknown error'}`, 'error');
        }
      } catch(e) {
        showToast(`Stop error: ${e.message}`, 'error');
      }
    }

    setInterval(updateStatus, 3000);
    updateStatus();
    refreshChromecastDevices();
    setInterval(refreshChromecastDevices, 10000);
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


class WebServer:
    """Embedded aiohttp server hosting the BM04 web dashboard and MJPEG stream."""

    def __init__(
        self,
        monitor: MomcozyMonitor,
        streamer: Optional[VideoStreamer] = None,
        host: str = "0.0.0.0",
        port: int = 8421,
    ) -> None:
        self.monitor = monitor
        self.streamer = streamer
        self.host = host
        self.port = port
        self.hls: Optional[HlsTranscoder] = HlsTranscoder(streamer=self.streamer) if self.streamer else None
        self.chromecast = ChromecastManager(default_port=self.port)
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/favicon.ico", self.handle_favicon)
        self.app.router.add_get("/stream", self.handle_stream)
        self.app.router.add_get("/audio", self.handle_audio)
        self.app.router.add_get("/audio.mp3", self.handle_audio)
        self.app.router.add_get("/hls/stream.m3u8", self.handle_hls_playlist)
        self.app.router.add_get("/hls/{segment}", self.handle_hls_segment)
        self.app.router.add_get("/snapshot", self.handle_snapshot)
        self.app.router.add_get("/detection_snapshot.jpg", self.handle_detection_snapshot)
        self.app.router.add_get("/api/detection_snapshot", self.handle_detection_snapshot)
        self.app.router.add_get("/api/status", self.handle_api_status)
        self.app.router.add_post("/api/control", self.handle_api_control)
        self.app.router.add_get("/api/chromecast/devices", self.handle_api_chromecast_devices)
        self.app.router.add_post("/api/chromecast/cast", self.handle_api_chromecast_cast)
        self.app.router.add_post("/api/chromecast/stop", self.handle_api_chromecast_stop)
        self.app.router.add_get("/api/chromecast/status", self.handle_api_chromecast_status)

    async def handle_favicon(self, request: web.Request) -> web.Response:
        return web.Response(
            text=BABY_FAVICON_SVG,
            content_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    async def handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=HTML_DASHBOARD, content_type="text/html")

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
            return web.Response(body=frame, content_type="image/jpeg")
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
        playlist = await self.hls.get_playlist(timeout_seconds=6.0)
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

    async def handle_api_chromecast_devices(self, request: web.Request) -> web.Response:
        """List discovered Chromecast devices."""
        devices = self.chromecast.get_devices()
        return web.json_response({
            "status": "ok",
            "available": self.chromecast.is_available,
            "devices": devices,
            "count": len(devices),
        }, headers={"Access-Control-Allow-Origin": "*"})

    async def handle_api_chromecast_cast(self, request: web.Request) -> web.Response:
        """Command a Chromecast to cast video (HLS) or audio (MP3)."""
        data = {}
        try:
            data = await request.json()
        except Exception:
            pass

        target = str(data.get("target") or data.get("device") or "").strip()
        media_type = str(data.get("type") or data.get("media") or "video").lower()
        if media_type not in ("video", "audio"):
            media_type = "video"

        if not target:
            return web.json_response(
                {"status": "error", "error": "Target device name or IP is required"},
                status=400,
                headers={"Access-Control-Allow-Origin": "*"},
            )

        # Resolve host IP that the Chromecast can reach
        host_arg = str(data.get("host_ip") or "").strip()
        if not host_arg:
            req_host = request.host.split(":")[0]
            if req_host and req_host not in ("localhost", "127.0.0.1", "0.0.0.0"):
                host_arg = req_host
            else:
                host_arg = get_local_ip()

        if media_type == "audio":
            media_url = f"http://{host_arg}:{self.port}/audio.mp3"
            content_type = "audio/mp3"
        else:
            media_url = f"http://{host_arg}:{self.port}/hls/stream.m3u8"
            content_type = "application/x-mpegURL"
            # Pre-warm HLS pipeline
            if self.hls:
                self.hls.touch()
                if not self.hls.is_running:
                    asyncio.create_task(self.hls.start())

        thumb_url = f"http://{host_arg}:{self.port}/detection_snapshot.jpg"

        try:
            res = await asyncio.to_thread(
                self.chromecast.cast_media,
                target,
                media_url,
                content_type=content_type,
                title="Momcozy BM04 Baby Monitor",
                thumb_url=thumb_url,
                media_type=media_type,
            )
            return web.json_response(res, headers={"Access-Control-Allow-Origin": "*"})
        except Exception as exc:
            logger.warning("Chromecast cast error for target '%s': %s", target, exc)
            return web.json_response(
                {"status": "error", "error": str(exc)},
                status=502,
                headers={"Access-Control-Allow-Origin": "*"},
            )

    async def handle_api_chromecast_stop(self, request: web.Request) -> web.Response:
        """Stop Chromecast playback."""
        data = {}
        try:
            data = await request.json()
        except Exception:
            pass
        target = str(data.get("target") or data.get("device") or "").strip()
        if not target:
            return web.json_response(
                {"status": "error", "error": "Target device name or IP is required"},
                status=400,
                headers={"Access-Control-Allow-Origin": "*"},
            )
        try:
            res = await asyncio.to_thread(self.chromecast.stop_cast, target)
            return web.json_response(res, headers={"Access-Control-Allow-Origin": "*"})
        except Exception as exc:
            return web.json_response(
                {"status": "error", "error": str(exc)},
                status=502,
                headers={"Access-Control-Allow-Origin": "*"},
            )

    async def handle_api_chromecast_status(self, request: web.Request) -> web.Response:
        """Return active cast status."""
        return web.json_response({
            "status": "ok",
            "available": self.chromecast.is_available,
            "active_casts": self.chromecast._active_casts,
            "devices": self.chromecast.get_devices(),
        }, headers={"Access-Control-Allow-Origin": "*"})

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

        stream_info = {
            "stream_configured": self.streamer is not None,
            "stream_running": bool(getattr(self.streamer, "is_running", False)) if self.streamer else False,
            "stream_connected": bool(getattr(self.streamer, "is_connected", False)) if self.streamer else False,
            "stream_listeners": stream_listeners,
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
            "control_mode": control_mode,
            "control_transport": control_transport,
            "control_diagnostic": control_diagnostic,
        }
        try:
            status = await asyncio.to_thread(self.monitor.get_status)

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
                    frame = self.streamer.capture_detection_snapshot(trigger="Manual UI Capture")
                    return web.json_response({"status": "ok", "captured": frame is not None})
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

            return web.json_response({"status": "ok"})
        except Exception as e:
            action_name = data.get("action", "unknown") if isinstance(data, dict) else "unknown"
            logger.warning("Device action '%s' failed: %s", action_name, e)
            return web.json_response({"status": "error", "error": str(e)}, status=502)

    async def run(self) -> None:
        """Run the server until interrupted."""
        if self.streamer and not self.streamer.is_running:
            try:
                await self.streamer.start()
            except Exception as e:
                logger.warning("Could not auto-start live video streamer: %s", e)

        # Start background Chromecast device discovery
        self.chromecast.start_discovery()

        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        logger.info("Momcozy Web Dashboard running at http://%s:%d", self.host, self.port)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            if self.hls:
                await self.hls.stop()
            self.chromecast.stop_all()
            if self.streamer:
                await self.streamer.stop()
            await runner.cleanup()
