"""Web Dashboard and MJPEG Streaming Server for Momcozy BM04.

Provides an intuitive web interface inspired by the Momcozy app, with live video,
real-time sensor telemetry (temperature, alerts), and full remote control.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
import socket
import ssl
import subprocess
import time
from typing import Any, Awaitable, Callable, Dict, Optional
import urllib.parse

# Disable aiohttp sendfile to prevent Python asyncio sslproto _set_app_protocol crash on client disconnect over HTTPS
os.environ.setdefault("AIOHTTP_NOSENDFILE", "1")

import aiohttp.web_fileresponse

aiohttp.web_fileresponse.NOSENDFILE = True

from aiohttp import web

from .config import DEFAULT_CONFIG_PATH
from .device import MomcozyMonitor
from .hls import HlsTranscoder
from .nightvision import (
    Action,
    NightVisionAutomation,
    SyncState,
    analyze_hevc_keyframe,
    analyze_night_vision_frame,
)
from .stream import VideoStreamer
from .timelapse import TimelapseRecorder

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

HTML_DASHBOARD = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
HTML_TIMELAPSE_PLAYER = (STATIC_DIR / "timelapse.html").read_text(encoding="utf-8")
BABY_FAVICON_SVG = (STATIC_DIR / "favicon.svg").read_text(encoding="utf-8")

_STATIC_CONTENT_TYPES: Dict[str, str] = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}

_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


class SafeFileResponse(web.FileResponse):
    """FileResponse that streams chunks to prevent Python asyncio loop.sendfile SSL crashes."""

    async def _sendfile(
        self, request: Any, fobj: Any, offset: int, count: int
    ) -> Any:
        writer = await web.StreamResponse.prepare(self, request)
        assert writer is not None
        return await self._sendfile_fallback(writer, fobj, offset, count)


def is_trusted_request_origin(request: web.Request) -> bool:
    """Validate Sec-Fetch-Site and Origin headers to block browser CSRF attacks.

    Requests without browser Origin/Sec-Fetch-Site headers (e.g. CLI, curl, or local
    automation scripts) are permitted because no cookie/basic auth is used, whereas
    all modern browsers attach Origin and/or Sec-Fetch-Site on cross-site mutations.
    """
    sec_fetch_site = request.headers.get("Sec-Fetch-Site")
    if sec_fetch_site is not None:
        sfs = sec_fetch_site.strip().lower()
        if sfs and sfs not in ("same-origin", "same-site", "none"):
            return False

    origin = request.headers.get("Origin")
    if origin is not None:
        origin_raw = origin.strip()
        if not origin_raw or origin_raw.lower() == "null":
            return False
        parsed_origin = urllib.parse.urlparse(origin_raw)
        origin_host = (parsed_origin.hostname or "").lower()
        if not origin_host:
            return False

        raw_req_host = (request.host or "").strip()
        parsed_req = urllib.parse.urlparse(f"http://{raw_req_host}")
        req_host = (parsed_req.hostname or "").lower()
        if not req_host:
            return False

        if origin_host != req_host and not (
            origin_host in _LOCAL_HOSTNAMES and req_host in _LOCAL_HOSTNAMES
        ):
            return False

    return True


def create_ssl_context(certfile: Optional[str] = None, keyfile: Optional[str] = None) -> ssl.SSLContext:
    """Create an SSL context for HTTPS.

    If certfile and keyfile are omitted, automatically creates a self-signed
    certificate in the config directory (or ~/.momcozy_ssl) for seamless HTTPS.
    """
    if not certfile or not keyfile:
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
        keyfile = str(keyfile or key_path)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return ctx


class WebServer:
    """Embedded aiohttp server hosting the BM04 web dashboard and MJPEG stream."""

    _LAN_IP_TTL = 60.0
    _cached_lan_ip: Optional[str] = None
    _cached_lan_ip_time: float = float("-inf")

    def __init__(
        self,
        monitor: MomcozyMonitor,
        streamer: Optional[VideoStreamer] = None,
        host: str = "0.0.0.0",
        port: int = 8421,
        http_port: Optional[int] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        timelapse_dir: Optional[Path | str] = None,
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
            TimelapseRecorder(streamer=self.streamer, storage_dir=timelapse_dir) if self.streamer else None
        )
        if self.streamer:
            self.streamer.keepalive_predicate = lambda: bool(
                (self.timelapse and self.timelapse.is_recording)
                or self.nv.activating
            )
        self._cached_status: Optional[Any] = None
        self._cached_status_time: float = 0.0
        self.nv = NightVisionAutomation()
        self._nv_lease: Optional[Any] = None  # stream lease held during the darkness countdown
        self._last_nv_check: float = float("-inf")
        self._last_lan_check: float = 0.0
        self._lan_probe_task: Optional[asyncio.Task[Any]] = None
        self.app = web.Application(middlewares=[self._csrf_middleware])
        self._setup_routes()

    @staticmethod
    @web.middleware
    async def _csrf_middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        if request.method in _STATE_CHANGING_METHODS and not is_trusted_request_origin(request):
            logger.warning(
                "Blocked cross-origin %s %s (Origin=%r, Sec-Fetch-Site=%r, Host=%r)",
                request.method,
                request.path,
                request.headers.get("Origin"),
                request.headers.get("Sec-Fetch-Site"),
                request.host,
            )
            return web.json_response(
                {"status": "error", "error": "Cross-origin request blocked"},
                status=403,
            )
        return await handler(request)

    @property
    def _nv_active_start(self) -> Optional[float]:
        return self.nv._dark_since

    @property
    def _nv_inactive_start(self) -> Optional[float]:
        return self.nv._light_since

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/favicon.ico", self.handle_favicon)
        self.app.router.add_get("/static/{filename}", self.handle_static)
        self.app.router.add_get("/stream", self.handle_stream)
        self.app.router.add_get("/ws/stream", self.handle_ws_stream)
        self.app.router.add_get("/audio", self.handle_audio)
        self.app.router.add_get("/hls/stream.m3u8", self.handle_hls_playlist)
        self.app.router.add_get("/hls/{segment}", self.handle_hls_segment)
        self.app.router.add_get("/snapshot", self.handle_snapshot)
        self.app.router.add_get("/detection_snapshot.jpg", self.handle_detection_snapshot)
        self.app.router.add_get("/api/status", self.handle_api_status)
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_post("/api/control", self.handle_api_control)
        self.app.router.add_post("/api/timelapse/start", self.handle_timelapse_start)
        self.app.router.add_post("/api/timelapse/stop", self.handle_timelapse_stop)
        self.app.router.add_post("/api/timelapse/ratio", self.handle_timelapse_ratio)
        self.app.router.add_post("/api/timelapse/clear", self.handle_timelapse_clear)
        self.app.router.add_post("/api/timelapse/sync_night_vision", self.handle_timelapse_sync_night_vision)
        self.app.router.add_get("/api/timelapse/status", self.handle_timelapse_status)
        self.app.router.add_post("/api/timelapse/event", self.handle_timelapse_record_event)
        self.app.router.add_get("/api/timelapse/video.mp4", self.handle_timelapse_video)
        self.app.router.add_get("/timelapse", self.handle_timelapse_page)

    async def handle_static(self, request: web.Request) -> web.Response:
        """Serve packaged frontend CSS/JS/SVG assets from momcozy/static."""
        filename = request.match_info.get("filename", "")
        if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
            return web.Response(status=404, text="Not found")
        target = (STATIC_DIR / filename).resolve()
        if target.parent != STATIC_DIR or not target.is_file():
            return web.Response(status=404, text="Not found")
        content_type = _STATIC_CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        return web.Response(
            body=target.read_bytes(),
            headers={
                "Content-Type": content_type,
                "Cache-Control": "no-cache",
            },
        )

    async def handle_health(self, request: web.Request) -> web.Response:
        """Lightweight local health check that never issues external/cloud requests."""
        return web.json_response({
            "status": "ok",
            "timelapse_recording": bool(self.timelapse and self.timelapse.is_recording),
            "stream_running": bool(self.streamer and self.streamer.is_running),
        })

    async def handle_favicon(self, request: web.Request) -> web.Response:
        return web.Response(
            text=BABY_FAVICON_SVG,
            content_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @classmethod
    def get_local_lan_ip(cls) -> str:
        """Determine primary local LAN IP address with a 60s TTL cache."""
        now = time.monotonic()
        if cls._cached_lan_ip is not None and (now - cls._cached_lan_ip_time) < cls._LAN_IP_TTL:
            return cls._cached_lan_ip
        ip = "127.0.0.1"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
        except Exception:
            ip = "127.0.0.1"
        cls._cached_lan_ip = ip
        cls._cached_lan_ip_time = now
        return ip

    async def handle_index(self, request: web.Request) -> web.Response:
        lan_ip = self.get_local_lan_ip()
        html = HTML_DASHBOARD.replace(
            "window.serverLanIp = '';\n    window.serverPort = 8421;\n    window.serverHttpPort = 8420;",
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
        # If no snapshot is cached and stream is not connected or running, do not block for 5s
        if not self.streamer.latest_detection_snapshot and not self.streamer.latest_frame:
            if not self.streamer.is_connected:
                return web.Response(status=404, text="No detection snapshot available yet")
        try:
            frame = await self.streamer.get_detection_snapshot(timeout_seconds=3.0)
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

    @staticmethod
    def _is_auto_night_mode(status: Any) -> bool:
        """True when the camera is in Auto night mode (DP 108 == "0").

        Only Auto mode is automated; manually forced modes ("1"/"2") are ignored. In Auto mode
        the DP stays "0" even when the camera switches to infrared, so darkness is detected
        from the video (see momcozy.nightvision).
        """
        if not status:
            return False
        raw = getattr(status, "raw_dps", {}) or {}
        return str(raw.get("108", getattr(status, "night_vision", "0"))).strip() == "0"

    def is_night_vision_active(self, status: Optional[Any], frame_bytes: Optional[bytes] = None) -> bool:
        """Check whether automatic Night Vision is currently active."""
        if not self._is_auto_night_mode(status):
            return False
        if frame_bytes is not None:
            is_nv, _ = analyze_night_vision_frame(frame_bytes)
            return is_nv
        return self.nv.is_dark

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
        is_nv = self.nv.is_dark and (self._cached_status is None or self._is_auto_night_mode(self._cached_status))

        sync_state, timer_remaining = SyncState.DISABLED, None
        if self.timelapse.sync_night_vision:
            sync_state, timer_remaining = self.nv.sync_state(
                now_mono, self.timelapse.is_recording, self.timelapse.current_session_type == "auto"
            )

        status_dict["night_vision_active"] = is_nv
        status_dict["sync_state"] = sync_state.value
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
        data: Dict[str, Any] = {}
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
            else:
                return web.json_response(
                    {"status": "error", "error": f"Unknown action: {action}"},
                    status=400,
                )

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
            self.nv.on_manual_start()
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
            self.nv.on_manual_stop()
            await self.timelapse.stop()
            self._sync_countdown_lease()
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
            self.nv.clear()
            self._sync_countdown_lease()
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

        self._set_night_vision_sync(enabled)
        return web.json_response({
            "success": True,
            "sync_night_vision": self.timelapse.sync_night_vision,
            "timelapse": self._get_timelapse_status(),
        })

    async def handle_timelapse_status(self, request: web.Request) -> web.Response:
        """Get timelapse recorder status with on-demand playback motion scan."""
        if not self.timelapse:
            return web.json_response({})
        scan_param = request.query.get("scan")
        force_param = request.query.get("force")
        force_scan = force_param == "1" or scan_param == "force"
        should_scan = scan_param in ("1", "force") or force_scan or (not self.timelapse._events and self.timelapse.has_content)
        if should_scan:
            try:
                await self.timelapse.scan_motion_events(force=force_scan)
            except Exception as exc:
                logger.debug("Motion scan error: %s", exc)
        return web.json_response(self._get_timelapse_status())

    async def handle_timelapse_record_event(self, request: web.Request) -> web.Response:
        """Manually or externally record a motion/detection range or point event."""
        if not self.timelapse:
            return web.json_response({"success": False, "error": "Timelapse recorder not configured."}, status=503)
        try:
            data = await request.json()
        except Exception:
            data = {}
        ev_type = str(data.get("type", "motion"))
        label = str(data.get("label", "Motion Alert"))
        ts = data.get("timestamp", data.get("start_time"))
        timestamp = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None
        raw_end = data.get("end_timestamp", data.get("end_time"))
        end_timestamp = float(raw_end) if isinstance(raw_end, (int, float)) and not isinstance(raw_end, bool) else None
        raw_dur = data.get("duration")
        if end_timestamp is None and isinstance(raw_dur, (int, float)) and not isinstance(raw_dur, bool):
            base_ts = timestamp if timestamp is not None else time.time()
            end_timestamp = base_ts + max(0.0, float(raw_dur))
        recorded = self.timelapse.record_event(
            event_type=ev_type,
            label=label,
            timestamp=timestamp,
            end_timestamp=end_timestamp,
        )
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

        # For mid-stream scrub/seek requests (start byte > 0), do not regenerate MP4 mid-stream
        # to avoid replacing the file under the player's active demuxer.
        # Initial requests (Range: bytes=0- or no Range) must always allow fresh cache generation.
        session_id = request.query.get("t")
        range_header = request.headers.get("Range", "")
        is_midstream_seek = False
        if not is_download and range_header.startswith("bytes="):
            try:
                start_spec = range_header[6:].split("-")[0].strip()
                if start_spec and int(start_spec) > 0:
                    is_midstream_seek = True
            except (ValueError, IndexError):
                pass

        export_kwargs: Dict[str, Any] = {
            "codec": codec,
            "ratio": ratio,
            "allow_cache_only": is_midstream_seek,
        }
        if session_id is not None:
            export_kwargs["session_id"] = session_id
        if is_download:
            export_kwargs["include_active"] = True

        try:
            mp4_path = await self.timelapse.export_mp4(**export_kwargs)
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

    def _set_night_vision_sync(self, enabled: bool) -> None:
        """Enable/disable night vision sync, restarting the automation from a clean slate."""
        self.timelapse.set_sync_night_vision(enabled)
        self.nv.reset_timers()
        if enabled:
            self._last_nv_check = float("-inf")  # sample on the next tick
        self._sync_countdown_lease()

    def _nv_check_interval(self) -> float:
        """Seconds between night vision samples.

        Sampling is cheap while the stream is up anyway; with the stream down every sample
        means reconnecting to the camera, so it is rare.
        While recording in stable darkness, 60s checks save CPU; once daylight is detected
        and the deactivation countdown begins, checks speed up to 15s.
        """
        if self.nv.activating:
            return 15.0
        if self.timelapse and self.timelapse.is_recording:
            if getattr(self.nv, "is_stopping", False):
                return 15.0
            return 60.0
        if self.streamer and self.streamer.is_running:
            return 30.0
        return 300.0

    def _monitor_tick_delay(self) -> float:
        """Sleep until the next status poll, waking early when a night vision sample is due.

        While recording, poll every 4s to capture hardware motion events in real-time.
        When idle (not recording), sleep in 30s intervals (or until the next sample is due)
        to eliminate unnecessary wakeups and telemetry polling.
        """
        if not (self.timelapse and self.timelapse.sync_night_vision):
            return 4.0
        due_in = self._last_nv_check + self._nv_check_interval() - time.monotonic()
        if self.timelapse.is_recording:
            return min(4.0, max(0.5, due_in))
        return min(30.0, max(0.5, due_in))

    def _sync_countdown_lease(self) -> None:
        """Keep the camera connected while a darkness countdown is running."""
        if self.streamer is None or self.timelapse is None:
            return
        want = self.timelapse.sync_night_vision and self.nv.activating and not self.timelapse.is_recording
        if want and self._nv_lease is None:
            self._nv_lease = self.streamer.acquire(linger=0)
        elif not want and self._nv_lease is not None:
            self.streamer.release(self._nv_lease)
            self._nv_lease = None

    async def _sample_night_vision(self) -> None:
        """Fetch one keyframe and feed the verdict to the automation.

        The caller holds a stream lease; on failure the previous verdict is kept.
        """
        timeout = 5.0
        if not self.streamer.is_running:
            timeout = 25.0
            logger.info("Checking night vision state (restarting camera stream for a keyframe)...")
        try:
            keyframe = await self.streamer.get_keyframe(timeout_seconds=timeout, max_age=10.0)
        except Exception as err:
            logger.warning("Could not fetch keyframe for night vision check: %s", err)
            return
        verdict = await asyncio.to_thread(analyze_hevc_keyframe, keyframe)
        if verdict is None:
            logger.warning("Could not decode keyframe for night vision check.")
            return
        self.nv.observe(*verdict)

    async def _advance_night_vision(self, now: float, status: Any) -> None:
        """Feed the latest verdict to the debounce state machine and carry out its decision."""
        is_dark = self._is_auto_night_mode(status) and self.nv.is_dark
        action = self.nv.step(
            now,
            is_dark,
            recording=self.timelapse.is_recording,
            session_auto=self.timelapse.current_session_type == "auto",
        )
        if action is Action.MARK_AUTO:
            self.timelapse.current_session_type = "auto"
        elif action is Action.START_AUTO:
            # An auto session continues an earlier auto one; otherwise it replaces manual footage.
            fresh = self.timelapse.last_session_type != "auto"
            await self.timelapse.start(fresh=fresh, session_type="auto")
        elif action is Action.STOP_AUTO:
            # linger=0: the camera disconnects at once unless a viewer is still watching.
            await self.timelapse.stop(linger=0)
        self._sync_countdown_lease()

    async def _run_night_vision_cycle(self, status: Any, now: float) -> None:
        """One automation tick: sample the camera if a sample is due, then advance the state machine."""
        if (
            self.streamer is not None
            and self._is_auto_night_mode(status)
            and now - self._last_nv_check >= self._nv_check_interval()
        ):
            self._last_nv_check = now
            # linger=0: if sampling is the only reason the stream is up, it disconnects right after.
            with self.streamer.lease(linger=0):
                await self._sample_night_vision()
                await self._advance_night_vision(time.monotonic(), status)
        else:
            await self._advance_night_vision(now, status)

    async def _maybe_check_lan_health(self, now_mono: float) -> None:
        """Periodically verify Local LAN reachability and rediscover changed camera IPs."""
        if not self.monitor or not getattr(self.monitor, "local_key", None):
            return
        if not callable(getattr(self.monitor, "check_local_lan_health", None)):
            return
        if now_mono - self._last_lan_check < 60.0:
            return
        if self._lan_probe_task is not None and not self._lan_probe_task.done():
            return
        self._last_lan_check = now_mono
        try:
            await asyncio.to_thread(self.monitor.check_local_lan_health)
        except Exception as exc:
            logger.debug("Periodic LAN health check error: %s", exc)

    async def _background_monitor_loop(self) -> None:
        """Server-side background loop managing:
        1. Night Vision timelapse automation (see momcozy.nightvision).
        2. Hardware motion event logging while recording.
        3. Periodic Local LAN reachability & IP rediscovery.
        Runs entirely server-side without requiring any connected clients.
        """
        last_ev_time = 0.0
        while True:
            try:
                await asyncio.sleep(self._monitor_tick_delay())
                now_mono = time.monotonic()
                await self._maybe_check_lan_health(now_mono)
                if not self.monitor or not self.timelapse:
                    continue

                # If timelapse is neither recording nor configured to sync with Night Vision,
                # reset debounce timers and idle to minimize camera queries.
                if not self.timelapse.is_recording and not self.timelapse.sync_night_vision:
                    self.nv.reset_timers()
                    self._sync_countdown_lease()
                    continue
                # Query device telemetry
                try:
                    status = await asyncio.to_thread(self.monitor.get_status)
                    self._cached_status = status
                    self._cached_status_time = now_mono
                except Exception as poll_err:
                    logger.debug("Background monitor failed to poll camera status: %s", poll_err)
                    continue

                if self.timelapse.sync_night_vision:
                    await self._run_night_vision_cycle(status, now_mono)

                # Motion events while recording
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

    async def run(self) -> None:
        """Run the server until interrupted."""
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

        if self.streamer and not self.streamer.is_running:
            try:
                await self.streamer.start()
            except Exception as e:
                logger.warning("Could not auto-start live video streamer: %s", e)

        lan_probe_task: Optional[asyncio.Task[Any]] = None
        if (
            getattr(self.monitor, "local_key", None)
            and getattr(self.monitor, "_lan_tcp_unavailable", False)
            and callable(getattr(self.monitor, "test_local_lan_connection", None))
        ):
            self._last_lan_check = time.monotonic()
            lan_probe_task = asyncio.create_task(
                asyncio.to_thread(self.monitor.test_local_lan_connection, True)
            )
            self._lan_probe_task = lan_probe_task

        bg_task = asyncio.create_task(self._background_monitor_loop())
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            if lan_probe_task and not lan_probe_task.done():
                lan_probe_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await lan_probe_task
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
