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

from .constants import LULLABY_TRACKS, NightVisionMode, PtzDirection
from .device import MomcozyMonitor
from .stream import VideoStreamer

logger = logging.getLogger(__name__)

HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Momcozy BM04 Baby Monitor</title>
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
    .video-ctrl-btn {
      position: absolute;
      bottom: 12px;
      right: 12px;
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
      z-index: 10;
      line-height: 1;
    }
    .video-ctrl-btn:hover {
      background: rgba(0,0,0,0.85);
      border-color: rgba(255,255,255,0.4);
    }
    .video-ctrl-btn:active {
      transform: scale(0.95);
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
      <button class="video-ctrl-btn" id="fs-btn" onclick="toggleFullscreen()" title="Toggle Fullscreen">
        <svg id="fs-enter-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>
        <svg id="fs-exit-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="display:none;"><path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/></svg>
        <span id="fs-btn-text">Fullscreen</span>
      </button>
      <img id="stream-img" src="/stream" alt="Camera Stream">
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

    // Stream auto-reconnection and error overlays
    const streamImg = document.getElementById('stream-img');
    let streamRetryTimer = null;

    if (streamImg) {
      streamImg.addEventListener('dblclick', (e) => {
        e.preventDefault();
        toggleFullscreen();
      });
    }

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
      streamImg.onerror = function() {
        setStreamError('Live Stream Disconnected', 'Video feed connection lost. Reconnecting in 3s...');
        if (!streamRetryTimer) {
          streamRetryTimer = setTimeout(() => {
            streamRetryTimer = null;
            if (streamImg) streamImg.src = '/stream?t=' + Date.now();
          }, 3000);
        }
      };
      streamImg.onload = function() {
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

      // Disconnect stream when page is unloaded or hidden to conserve bandwidth and trigger server idle timeout
      window.addEventListener('beforeunload', () => {
        if (streamImg) streamImg.src = '';
      });
      window.addEventListener('pagehide', () => {
        if (streamImg) streamImg.src = '';
      });
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') {
          if (streamImg) streamImg.src = '';
        } else if (document.visibilityState === 'visible') {
          if (streamImg && !streamImg.src.includes('/stream')) {
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

    setInterval(updateStatus, 3000);
    updateStatus();
  </script>
</body>
</html>
"""


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
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/stream", self.handle_stream)
        self.app.router.add_get("/snapshot", self.handle_snapshot)
        self.app.router.add_get("/api/status", self.handle_api_status)
        self.app.router.add_post("/api/control", self.handle_api_control)

    async def handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=HTML_DASHBOARD, content_type="text/html")

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        """Serve live video as multipart/x-mixed-replace MJPEG stream."""
        if not self.streamer:
            return web.Response(
                status=503,
                text="Live video stream is not configured: Missing Tuya credentials.",
                content_type="text/plain",
            )

        if not self.streamer.is_running:
            try:
                await self.streamer.start()
            except Exception as e:
                logger.warning("Could not start live video streamer: %s", e)
                return web.Response(
                    status=502,
                    text=f"Could not start video stream: {e}",
                    content_type="text/plain",
                )

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
                await response.drain()
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception as e:
            logger.debug("Video stream subscription error: %s", e)

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

        raw_it = getattr(self.streamer, "idle_timeout", 0.0) if self.streamer else 0.0
        stream_idle_timeout = float(raw_it) if isinstance(raw_it, (int, float)) else 0.0

        raw_le = getattr(self.streamer, "last_error", None) if self.streamer else None
        stream_error = str(raw_le) if isinstance(raw_le, str) else None

        stream_info = {
            "stream_configured": self.streamer is not None,
            "stream_running": bool(getattr(self.streamer, "is_running", False)) if self.streamer else False,
            "stream_connected": bool(getattr(self.streamer, "is_connected", False)) if self.streamer else False,
            "stream_listeners": stream_listeners,
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
                **stream_info,
            })

    async def handle_api_control(self, request: web.Request) -> web.Response:
        """Handle control actions from web UI."""
        data = {}
        try:
            data = await request.json()
            action = data.get("action")

            if action == "ptz_step":
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

        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        logger.info("Momcozy Web Dashboard running at http://%s:%d", self.host, self.port)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            if self.streamer:
                await self.streamer.stop()
