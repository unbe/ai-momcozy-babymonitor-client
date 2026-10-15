"""Web Dashboard and MJPEG Streaming Server for Momcozy BM04.

Provides an intuitive web interface inspired by the Momcozy app, with live video,
real-time sensor telemetry (temperature, alerts), and full remote control.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
      top: 12px;
      left: 12px;
      background: rgba(0,0,0,0.6);
      color: #fff;
      padding: 6px 12px;
      border-radius: 20px;
      font-size: 0.85rem;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .live-dot {
      width: 8px;
      height: 8px;
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
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>🍼 Momcozy BM04 Monitor</h1>
      <span id="conn-badge" class="badge active">Connected</span>
    </header>

    <div class="video-card">
      <div class="video-overlay"><div class="live-dot"></div> LIVE STREAM</div>
      <img id="stream-img" src="/stream" alt="Camera Stream" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=\\'http://www.w3.org/2000/svg\\' width=\\'400\\' height=\\'225\\'><rect width=\\'100%\\' height=\\'100%\\' fill=\\'%23111\\'/><text x=\\'50%\\' y=\\'50%\\' fill=\\'%23888\\' font-size=\\'16\\' text-anchor=\\'middle\\'>Connecting to camera stream...</text></svg>'">
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
          <button class="btn" onmousedown="ptz('0')" onmouseup="ptzStop()" ontouchstart="ptz('0')" ontouchend="ptzStop()">▲</button>
          <div></div>
          <button class="btn" onmousedown="ptz('6')" onmouseup="ptzStop()" ontouchstart="ptz('6')" ontouchend="ptzStop()">◀</button>
          <button class="btn" onclick="ptzStop()">⏹</button>
          <button class="btn" onmousedown="ptz('2')" onmouseup="ptzStop()" ontouchstart="ptz('2')" ontouchend="ptzStop()">▶</button>
          <div></div>
          <button class="btn" onmousedown="ptz('4')" onmouseup="ptzStop()" ontouchstart="ptz('4')" ontouchend="ptzStop()">▼</button>
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
    async function apiPost(action, data={}) {
      try {
        await fetch('/api/control', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({action, ...data})
        });
      } catch(e) { console.error(e); }
    }

    function ptz(dir) { apiPost('ptz_move', {direction: dir}); }
    function ptzStop() { apiPost('ptz_stop'); }
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

    async function updateStatus() {
      try {
        const res = await fetch('/api/status');
        if (!res.ok) return;
        const d = await res.json();
        if (d.temperature_c !== null) {
          document.getElementById('temp-val').innerHTML = `${d.temperature_c.toFixed(1)}°C <span>(${d.temperature_f.toFixed(1)}°F)</span>`;
        }
        document.getElementById('motion-stat').innerText = d.motion_switch ? "Active" : "Off";
        document.getElementById('cry-stat').innerText = d.cry_detection_switch ? "Active" : "Off";
        document.getElementById('priv-stat').innerText = d.privacy_mode ? "ON" : "OFF";
        const nvMap = {"0": "Auto", "1": "Off", "2": "On"};
        document.getElementById('nv-stat').innerText = nvMap[d.night_vision] || d.night_vision;
      } catch(e) {}
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
        port: int = 8080,
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
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"},
        )
        await response.prepare(request)

        if not self.streamer:
            # Fallback test pattern if camera P2P streamer is not active
            while True:
                await asyncio.sleep(1)
                break
            return response

        if not self.streamer.is_running:
            await self.streamer.start()

        try:
            async for frame in self.streamer.subscribe():
                header = (
                    f"--frame\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                ).encode("ascii")
                await response.write(header + frame + b"\r\n")
        except (asyncio.CancelledError, ConnectionResetError):
            pass

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
        status = await asyncio.to_thread(self.monitor.get_status)
        return web.json_response({
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
        })

    async def handle_api_control(self, request: web.Request) -> web.Response:
        """Handle control actions from web UI."""
        data = await request.json()
        action = data.get("action")

        if action == "ptz_move":
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

    async def run(self) -> None:
        """Run the server until interrupted."""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        logger.info("Momcozy Web Dashboard running at http://%s:%d", self.host, self.port)
        while True:
            await asyncio.sleep(3600)
