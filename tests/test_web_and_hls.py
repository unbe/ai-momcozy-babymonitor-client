"""Unit tests for WebServer, static frontend assets, CSRF protection, and HlsTranscoder."""

import asyncio
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import make_mocked_request

from momcozy.device import BM04Status
from momcozy.hls import HlsTranscoder
from momcozy.timelapse import TimelapseRecorder
from momcozy.web import (
    HTML_DASHBOARD,
    HTML_TIMELAPSE_PLAYER,
    STATIC_DIR,
    SafeFileResponse,
    WebServer,
    create_ssl_context,
)


class TestStaticFrontendAndXssSafety(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is required for JS syntax validation")
    def test_all_static_javascript_files_have_valid_syntax(self):
        js_files = sorted(STATIC_DIR.glob("*.js"))
        self.assertGreaterEqual(len(js_files), 2)
        for js_file in js_files:
            proc = subprocess.run(
                ["node", "--check", str(js_file)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"JavaScript syntax error in {js_file.name}:\n{proc.stderr}",
            )

    def test_static_javascript_avoids_innerhtml_xss_sinks(self):
        dashboard_js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
        timelapse_js = (STATIC_DIR / "timelapse.js").read_text(encoding="utf-8")
        self.assertNotIn(".innerHTML", dashboard_js)
        self.assertNotIn(".innerHTML", timelapse_js)

        # Verify WebCodecs, auto-promotion, and timelapse playback hooks are present
        self.assertIn("checkHevcWebCodecsSupport", dashboard_js)
        self.assertIn("fallbackToMjpeg", dashboard_js)
        self.assertIn("startMjpegStream", dashboard_js)
        self.assertIn("startWebCodecsStream", dashboard_js)
        self.assertIn("hasFatalDecoderError = false", dashboard_js)
        self.assertIn("Waiting for camera stream", dashboard_js)
        self.assertIn("Recovering from decoder error", dashboard_js)
        self.assertIn("Auto-promoting from MJPEG to WebCodecs HEVC", dashboard_js)
        self.assertIn("if (d.detection_time && d.detection_time !== lastDetectTime)", dashboard_js)
        self.assertIn("canPlayHevc", timelapse_js)
        self.assertIn("recoveryAttempts < 10", timelapse_js)

    def test_html_templates_reference_static_assets_and_updated_tooltip(self):
        self.assertIn('/static/dashboard.css', HTML_DASHBOARD)
        self.assertIn('/static/dashboard.js', HTML_DASHBOARD)
        self.assertIn('id="stream-canvas"', HTML_DASHBOARD)
        self.assertIn('id="stream-img"', HTML_DASHBOARD)
        self.assertNotIn("(>=60s)", HTML_DASHBOARD)
        self.assertIn("immediately on night vision and stop on daylight (>=120s)", HTML_DASHBOARD)

        self.assertIn('/static/timelapse.css', HTML_TIMELAPSE_PLAYER)
        self.assertIn('/static/timelapse.js', HTML_TIMELAPSE_PLAYER)


class TestWebServerAndCsrf(unittest.IsolatedAsyncioTestCase):
    async def test_csrf_middleware_blocks_cross_origin_mutations_without_requiring_auth(self):
        mock_monitor = MagicMock()
        server = WebServer(monitor=mock_monitor)
        handler = AsyncMock(return_value=MagicMock(status=200))

        # 1. Cross-origin Origin header -> 403
        req_evil = make_mocked_request(
            "POST",
            "/api/control",
            headers={"Host": "192.168.1.10:8421", "Origin": "https://evil.example"},
        )
        resp_evil = await server._csrf_middleware(req_evil, handler)
        self.assertEqual(resp_evil.status, 403)
        handler.assert_not_awaited()

        # 2. Sec-Fetch-Site: cross-site -> 403
        req_sfs = make_mocked_request(
            "POST",
            "/api/timelapse/clear",
            headers={"Host": "192.168.1.10:8421", "Sec-Fetch-Site": "cross-site"},
        )
        resp_sfs = await server._csrf_middleware(req_sfs, handler)
        self.assertEqual(resp_sfs.status, 403)

        # 3. Sandboxed iframe Origin: null -> 403
        req_null = make_mocked_request(
            "POST",
            "/api/control",
            headers={"Host": "localhost:8421", "Origin": "null"},
        )
        resp_null = await server._csrf_middleware(req_null, handler)
        self.assertEqual(resp_null.status, 403)

        # 4. Same-origin browser request -> allowed (200)
        req_same = make_mocked_request(
            "POST",
            "/api/control",
            headers={
                "Host": "192.168.1.10:8421",
                "Origin": "https://192.168.1.10:8421",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        resp_same = await server._csrf_middleware(req_same, handler)
        self.assertEqual(resp_same.status, 200)

        # 5. Localhost / 127.0.0.1 loopback equivalence -> allowed (200)
        req_loopback = make_mocked_request(
            "POST",
            "/api/control",
            headers={"Host": "127.0.0.1:8421", "Origin": "http://localhost:8421"},
        )
        resp_loopback = await server._csrf_middleware(req_loopback, handler)
        self.assertEqual(resp_loopback.status, 200)

        # 6. Non-browser client (curl / CLI) with no Origin / Sec-Fetch-Site -> allowed (no auth)
        req_cli = make_mocked_request("POST", "/api/control", headers={"Host": "192.168.1.10:8421"})
        resp_cli = await server._csrf_middleware(req_cli, handler)
        self.assertEqual(resp_cli.status, 200)

        # 7. GET requests with cross-site Origin (e.g. HLS / links) -> allowed
        req_get = make_mocked_request(
            "GET",
            "/hls/stream.m3u8",
            headers={"Host": "192.168.1.10:8420", "Origin": "https://www.gstatic.com"},
        )
        resp_get = await server._csrf_middleware(req_get, handler)
        self.assertEqual(resp_get.status, 200)

    async def test_removed_aliases_are_not_registered(self):
        server = WebServer(monitor=MagicMock(), streamer=MagicMock())
        registered_paths = {
            r.canonical for r in server.app.router.resources() if hasattr(r, "canonical")
        }
        self.assertNotIn("/audio.mp3", registered_paths)
        self.assertNotIn("/api/detection_snapshot", registered_paths)
        self.assertNotIn("/api/timelapse/erase", registered_paths)
        self.assertFalse(hasattr(server, "_motion_monitor_loop"))

        # Removed duplicate timelapse actions on /api/control must return 400
        for removed_action in ("timelapse_clear", "timelapse_erase", "timelapse_sync_night_vision"):
            req = make_mocked_request("POST", "/api/control")
            req._read_bytes = json.dumps({"action": removed_action}).encode("utf-8")
            resp = await server.handle_api_control(req)
            self.assertEqual(resp.status, 400)

    async def test_handle_static_serves_assets_and_blocks_traversal(self):
        server = WebServer(monitor=MagicMock())
        req_js = make_mocked_request("GET", "/static/dashboard.js", match_info={"filename": "dashboard.js"})
        resp_js = await server.handle_static(req_js)
        self.assertEqual(resp_js.status, 200)
        self.assertIn("javascript", resp_js.content_type)

        for bad_name in ("../web.py", ".hidden", "sub/file.js", "missing.js"):
            req_bad = make_mocked_request("GET", f"/static/{bad_name}", match_info={"filename": bad_name})
            resp_bad = await server.handle_static(req_bad)
            self.assertEqual(resp_bad.status, 404)

    def test_get_local_lan_ip_is_cached_with_ttl(self):
        WebServer._cached_lan_ip = None
        WebServer._cached_lan_ip_time = float("-inf")
        mock_sock = MagicMock()
        mock_sock.__enter__.return_value = mock_sock
        mock_sock.getsockname.return_value = ("192.168.50.10", 54321)

        with patch("socket.socket", return_value=mock_sock) as sock_cls:
            ip1 = WebServer.get_local_lan_ip()
            ip2 = WebServer.get_local_lan_ip()
            self.assertEqual(ip1, "192.168.50.10")
            self.assertEqual(ip2, "192.168.50.10")
            self.assertEqual(sock_cls.call_count, 1)

    async def test_handle_api_status_and_health_and_control(self):
        mock_monitor = MagicMock()
        mock_monitor.control_mode = "local_lan"
        mock_monitor.control_transport = "Local LAN (192.168.1.50:6668)"
        mock_monitor.get_status.return_value = BM04Status(temperature_c=22.5, temperature_f=72.5)

        mock_streamer = MagicMock()
        mock_streamer.is_running = True
        mock_streamer.is_connected = False
        mock_streamer.last_error = "Signaling error: 135"
        mock_streamer.stream_mode = "cloud_relay"
        mock_streamer.transport_description = "Cloud Relay (Tuya P2P)"

        server = WebServer(monitor=mock_monitor, streamer=mock_streamer)
        resp = await server.handle_api_status(make_mocked_request("GET", "/api/status"))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.text)
        self.assertTrue(body["online"])
        self.assertEqual(body["temperature_c"], 22.5)
        self.assertEqual(body["stream_error"], "Signaling error: 135")
        self.assertEqual(body["control_mode"], "local_lan")

        # Health endpoint never calls monitor.get_status
        mock_monitor.get_status.reset_mock()
        resp_health = await server.handle_health(make_mocked_request("GET", "/health"))
        self.assertEqual(resp_health.status, 200)
        mock_monitor.get_status.assert_not_called()

        # Error resilience in status
        server._cached_status = None
        mock_monitor.get_status.side_effect = RuntimeError("Check device key")
        resp_err = await server.handle_api_status(make_mocked_request("GET", "/api/status"))
        self.assertEqual(resp_err.status, 200)
        self.assertFalse(json.loads(resp_err.text)["online"])

        # Control PTZ step + error resilience
        req_ptz = make_mocked_request("POST", "/api/control")
        req_ptz._read_bytes = b'{"action": "ptz_step", "direction": "2", "duration": 0.4}'
        resp_ptz = await server.handle_api_control(req_ptz)
        self.assertEqual(resp_ptz.status, 200)
        mock_monitor.ptz_step.assert_called_once_with("2", 0.4)

        mock_monitor.ptz_move.side_effect = RuntimeError("Socket timeout")
        req_fail = make_mocked_request("POST", "/api/control")
        req_fail._read_bytes = b'{"action": "ptz_move", "direction": "0"}'
        resp_fail = await server.handle_api_control(req_fail)
        self.assertEqual(resp_fail.status, 502)

    async def test_favicon_stream_ws_audio_snapshot_and_ssl_endpoints(self):
        ctx = create_ssl_context()
        self.assertIsInstance(ctx, ssl.SSLContext)

        srv_ssl = WebServer(monitor=MagicMock(), port=8421, ssl_context=MagicMock(spec=ssl.SSLContext))
        self.assertEqual(srv_ssl.http_port, 8420)
        resp_idx = await srv_ssl.handle_index(make_mocked_request("GET", "/"))
        self.assertIn("window.serverHttpPort = 8420;", resp_idx.text)

        resp_fav = await srv_ssl.handle_favicon(make_mocked_request("GET", "/favicon.ico"))
        self.assertEqual(resp_fav.status, 200)
        self.assertEqual(resp_fav.content_type, "image/svg+xml")

        # Unconfigured streamer responses
        self.assertEqual((await srv_ssl.handle_stream(make_mocked_request("GET", "/stream"))).status, 503)
        self.assertEqual((await srv_ssl.handle_audio(make_mocked_request("GET", "/audio"))).status, 503)

        # Stream start failure -> 502
        fail_streamer = MagicMock()
        fail_streamer.is_running = False
        fail_streamer.start = AsyncMock(side_effect=RuntimeError("Broker auth failed"))
        srv_fail = WebServer(monitor=MagicMock(), streamer=fail_streamer)
        self.assertEqual((await srv_fail.handle_stream(make_mocked_request("GET", "/stream"))).status, 502)

        # WebSocket unconfigured & streaming
        with patch("aiohttp.web.WebSocketResponse") as mock_ws_cls:
            mock_ws = MagicMock()
            mock_ws.prepare = AsyncMock()
            mock_ws.close = AsyncMock()
            mock_ws_cls.return_value = mock_ws
            await srv_ssl.handle_ws_stream(make_mocked_request("GET", "/ws/stream"))
            mock_ws.close.assert_awaited_once_with(code=4004, message=b"Streamer not configured")

        ok_streamer = MagicMock()
        ok_streamer.is_running = True
        ok_streamer.is_connected = True
        ok_streamer.latest_detection_snapshot = None
        ok_streamer.latest_frame = None
        ok_streamer.get_snapshot = AsyncMock(return_value=b"LIVE_SNAPSHOT_BYTES")
        ok_streamer.get_detection_snapshot = AsyncMock(return_value=b"FAKE_JPEG_BYTES")

        async def fake_hevc():
            yield b"keyframe_bytes", True
            yield b"delta_bytes", False

        async def mock_sub_audio(format="mp3"):
            yield b"MOCK_AUDIO_DATA"

        ok_streamer.subscribe_hevc = fake_hevc
        ok_streamer.subscribe_audio = mock_sub_audio
        srv_ok = WebServer(monitor=MagicMock(), streamer=ok_streamer)

        with patch("aiohttp.web.WebSocketResponse") as mock_ws_cls:
            mock_ws = MagicMock()
            mock_ws.prepare = AsyncMock()
            mock_ws.close = AsyncMock()
            mock_ws.send_bytes = AsyncMock()
            mock_ws.closed = False

            async def fake_incoming():
                if False:
                    yield None

            mock_ws.__aiter__ = lambda self: fake_incoming()
            mock_ws_cls.return_value = mock_ws
            await srv_ok.handle_ws_stream(make_mocked_request("GET", "/ws/stream"))
            mock_ws.send_bytes.assert_any_call(b"\x01keyframe_bytes")
            mock_ws.send_bytes.assert_any_call(b"\x00delta_bytes")

        resp_mp3 = await srv_ok.handle_audio(make_mocked_request("GET", "/audio?format=mp3"))
        self.assertEqual(resp_mp3.content_type, "audio/mpeg")
        resp_wav = await srv_ok.handle_audio(make_mocked_request("GET", "/audio?format=wav"))
        self.assertEqual(resp_wav.content_type, "audio/wav")

        resp_snap = await srv_ok.handle_snapshot(make_mocked_request("GET", "/snapshot"))
        self.assertEqual(resp_snap.body, b"LIVE_SNAPSHOT_BYTES")

        resp_det = await srv_ok.handle_detection_snapshot(make_mocked_request("GET", "/detection_snapshot.jpg"))
        self.assertEqual(resp_det.body, b"FAKE_JPEG_BYTES")
        ok_streamer.is_connected = False
        self.assertEqual(
            (await srv_ok.handle_detection_snapshot(make_mocked_request("GET", "/detection_snapshot.jpg"))).status,
            404,
        )

    async def test_timelapse_and_hls_web_endpoints(self):
        with tempfile.TemporaryDirectory() as td:
            mock_streamer = MagicMock()
            mock_streamer.is_running = True
            server = WebServer(monitor=MagicMock(), streamer=mock_streamer, timelapse_dir=td)
            server.timelapse.total_slots = 3

            # HLS endpoints
            server.hls = MagicMock()
            server.hls.is_available = True
            server.hls.get_playlist = AsyncMock(return_value="#EXTM3U\nsegment_000.ts\n")
            server.hls.get_segment = MagicMock(return_value=b"TS_SEGMENT_BYTES")
            resp_pl = await server.handle_hls_playlist(make_mocked_request("GET", "/hls/stream.m3u8"))
            self.assertEqual(resp_pl.status, 200)
            resp_seg = await server.handle_hls_segment(
                make_mocked_request("GET", "/hls/segment_000.ts", match_info={"segment": "segment_000.ts"})
            )
            self.assertEqual(resp_seg.body, b"TS_SEGMENT_BYTES")

            # Timelapse start / stop / resume / clear / ratio / sync_night_vision / event / video
            await server.handle_timelapse_start(make_mocked_request("POST", "/api/timelapse/start"))
            slot0 = server.timelapse.slot_path(0)
            slot0.write_bytes(b"frame_sample_bytes_12345")
            await server.handle_timelapse_stop(make_mocked_request("POST", "/api/timelapse/stop"))
            self.assertTrue(slot0.exists())
            await server.handle_timelapse_start(make_mocked_request("POST", "/api/timelapse/start"))
            self.assertTrue(slot0.exists())
            await server.timelapse.stop()

            req_ratio = make_mocked_request("POST", "/api/timelapse/ratio")
            req_ratio._read_bytes = b'{"ratio": 30}'
            self.assertEqual((await server.handle_timelapse_ratio(req_ratio)).status, 200)

            req_bad_ratio = make_mocked_request("POST", "/api/timelapse/ratio")
            req_bad_ratio._read_bytes = b'{"ratio": 999}'
            self.assertEqual((await server.handle_timelapse_ratio(req_bad_ratio)).status, 400)

            req_sync = make_mocked_request("POST", "/api/timelapse/sync_night_vision")
            req_sync._read_bytes = b'{"enabled": true}'
            resp_sync = await server.handle_timelapse_sync_night_vision(req_sync)
            self.assertTrue(json.loads(resp_sync.text)["sync_night_vision"])

            req_ev = make_mocked_request("POST", "/api/timelapse/event")
            req_ev._read_bytes = b'{"type": "motion", "label": "Crib Visit", "timestamp": 5000.0, "duration": 45.0}'
            self.assertEqual((await server.handle_timelapse_record_event(req_ev)).status, 200)

            dummy_mp4 = Path(td) / "dummy.mp4"
            dummy_mp4.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100)
            with patch.object(server.timelapse, "export_mp4", AsyncMock(return_value=dummy_mp4)):
                resp_vid = await server.handle_timelapse_video(
                    make_mocked_request("GET", "/api/timelapse/video.mp4", headers={"Range": "bytes=0-"})
                )
                self.assertIsInstance(resp_vid, SafeFileResponse)
                server.timelapse.export_mp4.assert_called_with(codec="hevc", ratio=None, allow_cache_only=False)

                await server.handle_timelapse_video(
                    make_mocked_request("GET", "/api/timelapse/video.mp4", headers={"Range": "bytes=5000-"})
                )
                server.timelapse.export_mp4.assert_called_with(codec="hevc", ratio=None, allow_cache_only=True)

                await server.handle_timelapse_video(
                    make_mocked_request("GET", "/api/timelapse/video.mp4?t=1794260624019", headers={"Range": "bytes=0-"})
                )
                server.timelapse.export_mp4.assert_called_with(codec="hevc", ratio=None, allow_cache_only=False, session_id="1794260624019")

            resp_clear = await server.handle_timelapse_clear(make_mocked_request("POST", "/api/timelapse/clear"))
            self.assertEqual(resp_clear.status, 200)
            self.assertFalse(slot0.exists())

            resp_page = await server.handle_timelapse_page(make_mocked_request("GET", "/timelapse"))
            self.assertEqual(resp_page.status, 200)
            self.assertEqual(resp_page.headers.get("Cache-Control"), "no-cache")
            self.assertIn("player-ratio-badge", resp_page.text)
            self.assertIn("timeline-markers", resp_page.text)
            self.assertIn("seekFromTimeline(event)", resp_page.text)

            req_js = make_mocked_request("GET", "/static/timelapse.js", match_info={"filename": "timelapse.js"})
            resp_js = await server.handle_static(req_js)
            self.assertEqual(resp_js.status, 200)
            self.assertIn("function seekFromTimeline", resp_js.body.decode("utf-8"))
            self.assertIn("window.seekFromTimeline = seekFromTimeline", resp_js.body.decode("utf-8"))


class TestHlsTranscoder(unittest.IsolatedAsyncioTestCase):
    async def test_hls_default_mkdtemp_directory_and_cleanup_on_stop(self):
        mock_streamer = MagicMock()
        hls = HlsTranscoder(streamer=mock_streamer)
        self.assertTrue(hls.output_dir.exists())
        self.assertTrue(hls.output_dir.name.startswith("momcozy_hls_"))
        temp_path = hls.output_dir
        await hls.stop()
        self.assertFalse(temp_path.exists())

    async def test_hls_properties_security_vaapi_and_software_fallback(self):
        mock_streamer = MagicMock()
        mock_streamer.is_running = True
        lease_token = object()
        mock_streamer.acquire.return_value = lease_token

        async def fake_hevc(leased=True):
            yield b"hevc_keyframe", True

        mock_streamer.subscribe_hevc = fake_hevc
        mock_streamer.subscribe = MagicMock()

        with tempfile.TemporaryDirectory() as tmp_dir:
            hls = HlsTranscoder(
                streamer=mock_streamer,
                output_dir=tmp_dir,
                hwaccel="vaapi",
                vaapi_device="/dev/dri/renderD128",
            )
            self.assertIsNone(hls.get_segment("../../../etc/passwd"))
            self.assertIsNone(hls.get_segment("invalid_name.mp4"))

            seg_path = Path(tmp_dir) / "segment_000.ts"
            seg_path.write_bytes(b"TS_CHUNK_DATA")
            self.assertEqual(hls.get_segment("segment_000.ts"), b"TS_CHUNK_DATA")

            with patch("subprocess.Popen") as mock_popen:
                vaapi_fail_proc = MagicMock()
                vaapi_fail_proc.poll.return_value = 1
                vaapi_fail_proc.returncode = 1
                vaapi_fail_proc.stderr = MagicMock()
                vaapi_fail_proc.stderr.read.return_value = b"vaInitialize failed"

                software_proc = MagicMock()
                software_proc.stdin = MagicMock()
                software_proc.poll.return_value = None

                mock_popen.side_effect = [vaapi_fail_proc, software_proc]
                started = await hls.start()
                self.assertTrue(started)
                self.assertFalse(hls.using_vaapi)
                mock_streamer.acquire.assert_called_once()
                mock_streamer.subscribe.assert_not_called()

                # Verify LIBVA_DRIVER_NAME="i965" was NOT forced into FFmpeg's environment
                vaapi_env = mock_popen.call_args_list[0][1].get("env", {})
                if "LIBVA_DRIVER_NAME" not in __import__("os").environ:
                    self.assertNotIn("LIBVA_DRIVER_NAME", vaapi_env)

                await hls.stop()
                mock_streamer.release.assert_called_once_with(lease_token)

    async def test_hls_keyframe_resync_on_queue_overflow(self):
        import queue as std_queue

        mock_streamer = MagicMock()
        mock_streamer.is_running = True

        chunks = [
            (b"key_1", True),
            (b"delta_1", False),  # fills 2-slot queue
            (b"delta_overflow", False),  # overflows -> flushes stale chunks & sets need_keyframe
            (b"delta_after_drop", False),  # MUST be skipped until next keyframe!
            (b"key_2", True),  # resynchronizes stream!
        ]

        async def fake_hevc(leased=True):
            for c, is_k in chunks:
                yield c, is_k

        mock_streamer.subscribe_hevc = fake_hevc
        with tempfile.TemporaryDirectory() as tmp_dir:
            hls = HlsTranscoder(streamer=mock_streamer, output_dir=tmp_dir)
            hls._queue = std_queue.Queue(maxsize=2)
            hls._running = True
            await hls._feed_frames_task()

            queued = []
            while not hls._queue.empty():
                item = hls._queue.get_nowait()
                if item is not None:
                    queued.append(item)

            self.assertEqual(queued, [b"key_2"])


if __name__ == "__main__":
    unittest.main()
