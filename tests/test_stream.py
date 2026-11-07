"""Unit tests for video/audio P2P streaming, HEVC parsing, MP3 encoding, and SDK patching."""

import asyncio
import hashlib
import hmac
import inspect
import io
import struct
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import av
import numpy as np
from tuya_ipc_p2p_sdk.models import AccountSession
import tuya_ipc_p2p_sdk.crypto as sdk_crypto
import tuya_ipc_p2p_sdk.stream_session as sdk_stream_session
import tuya_ipc_p2p_sdk.transport.kcp_segment as sdk_kcp
import tuya_ipc_p2p_sdk.transport.relay_framing as relay_framing

from momcozy.stream import (
    ALAW_LUT,
    MomcozyAudioHandler,
    MomcozyHevcHandler,
    StreamingMp3Encoder,
    VideoStreamer,
    _AUDIO_SYNC_MARKER,
    _SYNC_MARKER,
    _alaw_to_linear,
    _patch_tuya_stream_session,
    create_p2p_client,
    decode_hevc_gop_to_jpeg,
    fast_build_segment,
    fast_hmac_sha1,
    fast_parse_segment,
    fast_unwrap_media_frame,
    is_hevc_keyframe,
    make_wav_header,
    parse_hevc_nalus,
)
from momcozy.tuya_cloud import CLIENT_ID, PACKAGE_NAME


class TestVideoStreamerLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_video_streamer_error_tracking(self):
        streamer = VideoStreamer("dev123", "localkey")
        self.assertFalse(streamer.is_running)
        self.assertFalse(streamer.is_connected)
        self.assertIsNone(streamer.last_error)

        streamer._last_error = "Connection refused"
        self.assertEqual(streamer.last_error, "Connection refused")

    async def test_video_streamer_idle_timeout(self):
        streamer = VideoStreamer("dev123", "localkey", idle_timeout=0.05)
        self.assertEqual(streamer.idle_timeout, 0.05)
        self.assertEqual(streamer.listener_count, 0)

        async def mock_loop():
            try:
                while streamer.is_running:
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass

        streamer._stream_loop = mock_loop
        await streamer.start()
        self.assertTrue(streamer.is_running)
        self.assertIsNotNone(streamer._idle_task)

        await asyncio.sleep(0.08)
        self.assertFalse(streamer.is_running)
        self.assertIsNone(streamer._idle_task)

    async def test_video_streamer_idle_timeout_cancelled_by_listener(self):
        streamer = VideoStreamer("dev123", "localkey", idle_timeout=0.08)
        streamer._running = True
        streamer._connected = True
        streamer._latest_frame = b"fake_jpeg"

        streamer._schedule_idle_stop()
        self.assertIsNotNone(streamer._idle_task)

        sub_iter = streamer.subscribe()
        frame = await sub_iter.__anext__()
        self.assertEqual(frame, b"fake_jpeg")
        self.assertEqual(streamer.listener_count, 1)
        self.assertIsNone(streamer._idle_task)

        await sub_iter.aclose()
        self.assertEqual(streamer.listener_count, 0)
        self.assertIsNotNone(streamer._idle_task)
        await streamer.stop()

    async def test_audio_listener_idle_management(self):
        streamer = VideoStreamer("dev123", "localkey", idle_timeout=0.08)
        streamer._running = True
        streamer._connected = True

        streamer._schedule_idle_stop()
        self.assertIsNotNone(streamer._idle_task)

        sub_iter = streamer.subscribe_audio(format="wav")
        header = await sub_iter.__anext__()
        self.assertEqual(len(header), 44)
        self.assertEqual(streamer.audio_listener_count, 1)
        self.assertEqual(streamer.total_listener_count, 1)
        self.assertIsNone(streamer._idle_task)

        await sub_iter.aclose()
        self.assertEqual(streamer.audio_listener_count, 0)
        self.assertEqual(streamer.total_listener_count, 0)
        self.assertIsNotNone(streamer._idle_task)
        await streamer.stop()

    async def test_streamer_idle_stop_suppressed_when_keepalive_enabled(self):
        streamer = VideoStreamer(device_id="dev1", local_key="key1", idle_timeout=10.0)
        streamer._schedule_idle_stop()
        self.assertIsNotNone(streamer._idle_task)
        streamer._cancel_idle_stop()
        self.assertIsNone(streamer._idle_task)

        streamer.keepalive_predicate = lambda: True
        self.assertTrue(streamer.keepalive)
        streamer._schedule_idle_stop()
        self.assertIsNone(streamer._idle_task)

    async def test_video_streamer_subscribe_hevc(self):
        streamer = VideoStreamer("dev123", "localkey")
        self.assertEqual(streamer.hevc_listener_count, 0)

        streamer._latest_hevc_keyframe = b"\x00\x00\x00\x01\x26\x01KEY"
        streamer._connected = True
        streamer._running = True

        gen = streamer.subscribe_hevc()
        chunk, is_key = await gen.__anext__()
        self.assertEqual(chunk, b"\x00\x00\x00\x01\x26\x01KEY")
        self.assertTrue(is_key)
        self.assertEqual(streamer.hevc_listener_count, 1)
        await gen.aclose()
        self.assertEqual(streamer.hevc_listener_count, 0)

        streamer._latest_hevc_keyframe = None
        gen_kf = streamer.subscribe_hevc(keyframes_only=True)
        t = asyncio.create_task(gen_kf.__anext__())
        streamer._broadcast_hevc(b"\x00\x00\x00\x01\x01DELTA", is_key=False)
        await asyncio.sleep(0.01)
        self.assertFalse(t.done())
        streamer._broadcast_hevc(b"\x00\x00\x00\x01\x26KEY2", is_key=True)
        res_chunk, res_is_key = await t
        self.assertEqual(res_chunk, b"\x00\x00\x00\x01\x26KEY2")
        self.assertTrue(res_is_key)
        await gen_kf.aclose()

    async def test_video_streamer_detection_snapshot_capture(self):
        streamer = VideoStreamer("dev123", "localkey")
        streamer._latest_frame = b"CURRENT_JPEG_FRAME"

        captured = streamer.capture_detection_snapshot(trigger="Test Trigger")
        self.assertEqual(captured, b"CURRENT_JPEG_FRAME")
        self.assertEqual(streamer.latest_detection_snapshot, b"CURRENT_JPEG_FRAME")
        self.assertEqual(streamer.latest_detection_trigger, "Test Trigger")
        self.assertIsNotNone(streamer.latest_detection_time)

        snap = await streamer.get_detection_snapshot()
        self.assertEqual(snap, b"CURRENT_JPEG_FRAME")

    async def test_get_snapshot_fetches_fresh_frame_when_cached_frame_stale(self):
        streamer = VideoStreamer("dev123", "localkey")
        streamer._running = True
        streamer._latest_frame = b"STALE_BASELINE_FRAME"
        streamer._latest_frame_time = time.monotonic() - 60.0

        async def push_fresh_frame():
            await asyncio.sleep(0.05)
            streamer._latest_frame = b"NEW_LIVE_CAMERA_FRAME"
            streamer._latest_frame_time = time.monotonic()

        push_task = asyncio.create_task(push_fresh_frame())
        fresh_frame = await streamer.get_snapshot(timeout_seconds=2.0)
        await push_task
        self.assertEqual(fresh_frame, b"NEW_LIVE_CAMERA_FRAME")

    async def test_get_snapshot_with_rolling_gop_buffer_and_single_flight_dedup(self):
        streamer = VideoStreamer("dev123", "localkey")
        self.assertEqual(streamer.gop_buffer_size, 0)

        buf = io.BytesIO()
        container = av.open(buf, mode="w", format="hevc")
        stream = container.add_stream("hevc", rate=15)
        stream.width = 160
        stream.height = 120
        stream.pix_fmt = "yuv420p"
        img = np.zeros((120, 160, 3), dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(img, format="bgr24")
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()

        streamer._running = True
        raw_hevc = buf.getvalue()
        streamer._gop_chunks = [raw_hevc]
        streamer._gop_version = 1
        streamer._broadcast_hevc(raw_hevc, is_key=True)
        self.assertEqual(streamer.gop_buffer_size, 1)

        decode_calls = 0
        orig_decode = decode_hevc_gop_to_jpeg

        def slow_decode(chunks):
            nonlocal decode_calls
            decode_calls += 1
            time.sleep(0.04)
            return orig_decode(chunks)

        with patch("momcozy.stream.decode_hevc_gop_to_jpeg", side_effect=slow_decode):
            results = await asyncio.gather(
                streamer.get_snapshot(max_age=0.0),
                streamer.get_snapshot(max_age=0.0),
                streamer.get_snapshot(max_age=0.0),
            )

        # All 3 concurrent callers share a single GOP decode!
        self.assertEqual(decode_calls, 1)
        for snap in results:
            self.assertTrue(snap.startswith(b"\xff\xd8"))
        self.assertEqual(streamer.latest_frame, results[0])

        await streamer.stop()
        self.assertEqual(streamer.gop_buffer_size, 0)
        self.assertIsNone(streamer.latest_frame)

    async def test_get_clean_frame_returns_fresh_keyframe(self):
        streamer = VideoStreamer(device_id="dev1", local_key="key1")
        streamer._running = False

        async def fake_start():
            streamer._running = True
            streamer._latest_frame = b"\xff\xd8\xff\xe0" + b"X" * 200
            streamer._latest_frame_time = time.monotonic()

        streamer.start = fake_start
        frame = await streamer.get_clean_frame(timeout_seconds=2.0)
        self.assertIsNotNone(frame)
        self.assertTrue(frame.startswith(b"\xff\xd8"))


class TestAudioAndHevcCodecs(unittest.TestCase):
    def test_alaw_lut_and_wav_header(self):
        for b in range(256):
            self.assertEqual(_alaw_to_linear(b), int(ALAW_LUT[b]))
        self.assertEqual(_alaw_to_linear(0x55), -8)
        self.assertEqual(_alaw_to_linear(0xD5), 8)

        hdr = make_wav_header(sample_rate=8000, channels=1, bits_per_sample=16)
        self.assertEqual(len(hdr), 44)
        self.assertTrue(hdr.startswith(b"RIFF"))
        self.assertEqual(struct.unpack("<I", hdr[24:28])[0], 8000)
        self.assertEqual(struct.unpack("<H", hdr[22:24])[0], 1)

    def test_momcozy_audio_handler(self):
        received_pcm = []
        handler = MomcozyAudioHandler(lambda pcm: received_pcm.append(pcm))

        header = bytearray(_AUDIO_SYNC_MARKER)
        header.extend(b"\x00" * 8)
        header.extend(struct.pack("<I", 8))
        header.extend(b"\x00" * 12)
        header.extend(struct.pack("<I", 12 + 10))
        rtp_hdr = b"\x80\x08\x00\x01\x00\x00\x00\x00\x00\x00\x00\x01"
        payload = b"\x55" * 10

        handler.feed_record(bytes(header + rtp_hdr + payload))
        self.assertEqual(len(received_pcm), 1)
        self.assertEqual(received_pcm[0], struct.pack("<h", -8) * 10)

    def test_streaming_mp3_encoder_bounded_memory_sink(self):
        encoder = StreamingMp3Encoder(sample_rate=8000, bit_rate=32000)
        pcm_chunk = b"\x00" * 1600
        total_bytes = 0
        for _ in range(50):
            chunk = encoder.encode(pcm_chunk)
            total_bytes += len(chunk)
            # Sink buffer must be drained completely on every encode() call (O(1) memory)
            self.assertEqual(len(encoder._sink._buf), 0)
        total_bytes += len(encoder.flush())
        self.assertEqual(len(encoder._sink._buf), 0)
        self.assertGreater(total_bytes, 0)

    def test_momcozy_hevc_handler_and_av_requirement(self):
        frames = []
        handler = MomcozyHevcHandler(lambda f: frames.append(f))

        header = bytearray(_SYNC_MARKER)
        header.extend(b"\x00" * 8)
        header.extend(struct.pack("<I", 8))
        header.extend(b"\x00" * 12)
        header.extend(struct.pack("<I", 13))
        rtp_hdr = b"\x80\x5f\x00\x01\x00\x00\x00\x00\x00\x00\x00\x0a"
        nal_payload = b"\x40"

        handler.feed_record(bytes(header + rtp_hdr + nal_payload))
        self.assertEqual(len(handler._buffer), 0)

        with patch.dict(sys.modules, {"av": None}):
            with self.assertRaises(ImportError) as ctx:
                MomcozyHevcHandler(lambda f: None)
            self.assertIn("requires the 'av' package", str(ctx.exception))

    def test_hevc_nalu_parsing_and_au_parameter_sets(self):
        vps = b"\x00\x00\x00\x01\x40\x01\x0c\x01"
        sps = b"\x00\x00\x00\x01\x42\x01\x01\x01"
        pps = b"\x00\x00\x00\x01\x44\x01\xc0"
        idr = b"\x00\x00\x00\x01\x26\x01\xaf\xfe"
        p_slice = b"\x00\x00\x00\x01\x02\x01\x12\x34"

        combined = vps + sps + pps + idr
        nalus = parse_hevc_nalus(combined)
        self.assertEqual([n[0] for n in nalus], [32, 33, 34, 19])
        self.assertTrue(is_hevc_keyframe(combined))
        self.assertTrue(is_hevc_keyframe(idr))
        self.assertTrue(is_hevc_keyframe(vps))
        self.assertFalse(is_hevc_keyframe(p_slice))

        handler = MomcozyHevcHandler(lambda f: None)
        raw_slice = b"\x26\x01\x02\x03"
        au, is_key = handler._build_au(raw_slice, is_key_candidate=True)
        self.assertFalse(is_key)
        self.assertEqual(au, raw_slice)

        handler._latest_vps = vps
        handler._latest_sps = sps
        handler._latest_pps = pps
        au, is_key = handler._build_au(raw_slice, is_key_candidate=True)
        self.assertTrue(is_key)
        self.assertTrue(au.startswith(vps))
        self.assertIn(sps, au)
        self.assertIn(pps, au)
        self.assertTrue(au.endswith(raw_slice))

    def test_is_transcoding_needed_and_handler_bypass(self):
        streamer = VideoStreamer("dev123", "localkey")
        self.assertFalse(streamer.is_transcoding_needed())

        q_hevc = asyncio.Queue()
        streamer._hevc_listeners.add(q_hevc)
        self.assertFalse(streamer.is_transcoding_needed())

        q_jpeg = asyncio.Queue()
        streamer._listeners.add(q_jpeg)
        self.assertTrue(streamer.is_transcoding_needed())

        streamer._listeners.discard(q_jpeg)
        self.assertFalse(streamer.is_transcoding_needed())

        self.assertEqual(streamer.stream_format, "Idle")
        streamer._connected = True
        self.assertEqual(streamer.stream_format, "HEVC (Pass-through)")
        streamer._listeners.add(q_jpeg)
        self.assertEqual(streamer.stream_format, "MJPEG (Transcoded)")
        streamer._listeners.discard(q_jpeg)
        streamer._hevc_listeners.discard(q_hevc)
        self.assertEqual(streamer.stream_format, "HEVC (Native)")
        streamer._connected = False

        streamer._snapshot_requested = True
        self.assertTrue(streamer.is_transcoding_needed())
        streamer._snapshot_requested = False

        mock_frame_cb = MagicMock()
        mock_hevc_cb = MagicMock()
        handler = MomcozyHevcHandler(
            on_frame=mock_frame_cb,
            on_hevc=mock_hevc_cb,
            is_transcoding_needed=lambda: False,
        )
        handler._codec = MagicMock()
        payload = b"\x00" * 12 + b"\x26\x01\x0c\x01"
        record = _SYNC_MARKER + b"\x00" * 8 + struct.pack("<I", 0) + struct.pack("<I", 0) + struct.pack("<I", len(payload)) + payload
        handler.feed_record(record)
        self.assertTrue(mock_hevc_cb.called)
        self.assertFalse(handler._codec.parse.called)
        self.assertFalse(mock_frame_cb.called)

    def test_decode_hevc_gop_to_jpeg(self):
        self.assertIsNone(decode_hevc_gop_to_jpeg([]))

        buf = io.BytesIO()
        container = av.open(buf, mode="w", format="hevc")
        stream = container.add_stream("hevc", rate=15)
        stream.width = 160
        stream.height = 120
        stream.pix_fmt = "yuv420p"
        for i in range(3):
            img = np.zeros((120, 160, 3), dtype=np.uint8)
            img[:, :] = (i * 40, 50, 100)
            frame = av.VideoFrame.from_ndarray(img, format="bgr24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()

        jpg_bytes = decode_hevc_gop_to_jpeg([buf.getvalue()])
        self.assertIsNotNone(jpg_bytes)
        self.assertTrue(jpg_bytes.startswith(b"\xff\xd8"))
        self.assertTrue(jpg_bytes.endswith(b"\xff\xd9"))

    def test_streamer_initial_timeout_configuration(self):
        source = inspect.getsource(VideoStreamer._stream_loop)
        self.assertIn("initial_timeout = 20.0", source)
        self.assertIn("Do not poison _last_error on initial cold-start reconnect", source)


class TestStreamOptimizationsAndPatching(unittest.TestCase):
    def test_fast_hmac_and_kcp_helpers(self):
        for key in [b"secret", b"k" * 16, b"k" * 64, b"long_key_" * 10]:
            for msg in [b"", b"short", b"x" * 1400]:
                self.assertEqual(fast_hmac_sha1(key, msg), hmac.new(key, msg, hashlib.sha1).digest())

        raw = fast_build_segment(101, 81, 2, 128, 55555, 12, 11, b"hello fast kcp")
        expected_raw = sdk_kcp.build_segment(101, 81, 2, 128, 55555, 12, 11, b"hello fast kcp")
        self.assertEqual(raw, expected_raw)
        seg = fast_parse_segment(raw)
        self.assertEqual(seg.conversation, 101)
        self.assertEqual(seg.data, b"hello fast kcp")

        key = b"relay_media_key_16b"
        segment = fast_build_segment(1, 81, 0, 128, 100, 1, 0, b"media payload")
        frame = relay_framing.media_frame(key, segment)
        self.assertEqual(fast_unwrap_media_frame(key, frame[4:]), segment)

    def test_patch_tuya_stream_session_is_idempotent_and_preserves_fast_hmac(self):
        _patch_tuya_stream_session()
        first_init = sdk_stream_session.StreamSession.__init__
        first_media = sdk_stream_session.StreamSession._on_media_record
        first_dial = sdk_stream_session.StreamSession._async_dial_relay

        # Repeated calls via create_p2p_client must NOT double-wrap StreamSession methods
        # and must NOT clobber sdk_crypto.hmac_sha1!
        create_p2p_client(tuya_uid="u1", tuya_token="t1", tuya_region="33")
        create_p2p_client(tuya_uid="u1", tuya_token="t1", tuya_region="33")

        self.assertIs(sdk_stream_session.StreamSession.__init__, first_init)
        self.assertIs(sdk_stream_session.StreamSession._on_media_record, first_media)
        self.assertIs(sdk_stream_session.StreamSession._async_dial_relay, first_dial)
        self.assertIs(sdk_crypto.hmac_sha1, fast_hmac_sha1)

    def test_hevc_handler_delta_frame_filtering_and_full_rate(self):
        delivered = []
        full_rate_needed = False
        handler = MomcozyHevcHandler(
            on_frame=None,
            on_hevc=lambda chunk, is_key: delivered.append((chunk, is_key)),
            is_transcoding_needed=lambda: False,
            is_full_rate_needed=lambda: full_rate_needed,
        )

        def make_record(payload_bytes):
            header = bytearray(28)
            header[0:8] = b"\x03\x00\x01\x00\x00\x00\x00\x00"
            struct.pack_into("<I", header, 16, 0)
            struct.pack_into("<I", header, 24, 12 + len(payload_bytes))
            return bytes(header) + bytes(12) + payload_bytes

        handler.feed_record(make_record(bytes([(32 << 1), 0x01, 0x00, 0x01])))
        handler.feed_record(make_record(bytes([(33 << 1), 0x01, 0x00, 0x02])))
        handler.feed_record(make_record(bytes([(34 << 1), 0x01, 0x00, 0x03])))
        handler.feed_record(make_record(bytes([(19 << 1), 0x01, 0xAA, 0xBB])))
        self.assertEqual(len(delivered), 1)
        self.assertTrue(delivered[-1][1])

        delta_nal = bytes([(1 << 1), 0x01, 0x11, 0x22])
        fu_start = bytes([(49 << 1), 0x01, 0x81, 0x33])
        fu_end = bytes([(49 << 1), 0x01, 0x41, 0x44])
        handler.feed_record(make_record(delta_nal))
        handler.feed_record(make_record(fu_start))
        handler.feed_record(make_record(fu_end))
        self.assertEqual(len(delivered), 1)

        full_rate_needed = True
        handler.feed_record(make_record(delta_nal))
        handler.feed_record(make_record(fu_start))
        handler.feed_record(make_record(fu_end))
        self.assertEqual(len(delivered), 3)
        self.assertFalse(delivered[-1][1])

        streamer = VideoStreamer(device_id="test_dev", local_key="key123")
        self.assertFalse(streamer.is_full_rate_needed())
        q_kf = asyncio.Queue()
        streamer._hevc_keyframe_listeners.add(q_kf)
        self.assertFalse(streamer.is_full_rate_needed())
        q_full = asyncio.Queue()
        streamer._hevc_listeners.add(q_full)
        self.assertTrue(streamer.is_full_rate_needed())

    def test_p2p_client_mqtt_identity_and_session_reuse(self):
        client = create_p2p_client(tuya_uid="test_uid", tuya_token="test_tok", tuya_region="33")
        mock_session = MagicMock()
        mock_session.ecode = "ecode123"
        mock_session.sid = "sid456"
        mock_session.uid = "uid789"
        mock_session.device_fingerprint = "fp000"
        identity = client._gateway.mqtt_identity(mock_session)
        self.assertIn(CLIENT_ID, identity.username)
        self.assertIn(PACKAGE_NAME, identity.client_id)

        cached = AccountSession(sid="cached_sid", ecode="cached_ecode", uid="cached_uid", device_fingerprint="fp1")
        client2 = create_p2p_client(tuya_uid="uid1", tuya_token="tok1", session=cached)
        client2._gateway.async_login = AsyncMock()
        ret = asyncio.run(client2.async_account())
        self.assertEqual(ret.sid, "cached_sid")
        client2._gateway.async_login.assert_not_called()

    def test_create_p2p_client_session_updated_callback_and_streamer_persistence(self):
        updates = []
        client = create_p2p_client(
            tuya_uid="uid1",
            tuya_token="tok1",
            on_session_updated=lambda s: updates.append(s),
        )
        rsa_n = (
            "16525679346432988803281856461803423983212623182635955743086474842159735066127111"
            "73069078532627718894510679396682216740963945894740181941653466581829808717211039"
            "18278367187267249387469306294083054327209045854461486623500042046249911664454588"
            "355119414358044668319815236208994331675895149218003259947699886240389"
        )
        client._gateway._async_call_object = AsyncMock(
            side_effect=[
                {"publicKey": rsa_n, "exponent": "65537", "token": "rsa_tok"},
                {"sid": "new_sid", "ecode": "new_ecode", "uid": "uid1"},
            ]
        )
        ret = asyncio.run(client.async_login())
        self.assertEqual(ret.sid, "new_sid")
        self.assertEqual(len(updates), 1)

        initial_session = AccountSession(sid="init_sid", ecode="init_ecode", uid="u1", device_fingerprint="fp")
        updated_sessions = []
        streamer = VideoStreamer(
            device_id="dev1",
            local_key="key1",
            session=initial_session,
            on_session_updated=lambda s: updated_sessions.append(s),
        )
        refreshed = AccountSession(sid="refr_sid", ecode="refr_ecode", uid="u1", device_fingerprint="fp")
        streamer._on_session_refreshed(refreshed)
        self.assertEqual(streamer._account_session.sid, "refr_sid")
        self.assertEqual(updated_sessions[0].sid, "refr_sid")

        manual = AccountSession(sid="man_sid", ecode="man_ecode", uid="u1", device_fingerprint="fp")
        streamer.update_session(manual)
        self.assertEqual(streamer._account_session.sid, "man_sid")


if __name__ == "__main__":
    unittest.main()
