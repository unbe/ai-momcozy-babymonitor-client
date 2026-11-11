"""Unit tests for TimelapseRecorder, NightVisionAutomation, and YUV stride safety."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import av
import numpy as np
from PIL import Image

from momcozy.device import BM04Status
from momcozy.nightvision import (
    Action,
    NightVisionAutomation,
    SyncState,
    analyze_hevc_keyframe,
    detect_night_vision_from_jpeg,
)
from momcozy.timelapse import (
    TimelapseRecorder,
    extract_hevc_keyframe_luma,
    format_duration,
    format_event_duration,
    format_video_duration,
    luma_fingerprint_diff,
    merge_motion_ranges,
)
from momcozy.web import WebServer


def _encode_single_hevc_frame(width: int, height: int, bgr: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    container = av.open(buf, mode="w", format="hevc")
    stream = container.add_stream("hevc", rate=15)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :] = bgr
    frame = av.VideoFrame.from_ndarray(img, format="bgr24")
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return buf.getvalue()


class TestPlanarYuvStrideAndSmallFrames(unittest.TestCase):
    def test_extract_luma_and_analyze_nv_respect_plane_line_size_padding(self):
        # Simulate padded YUV planes where line_size > width (e.g., 64-byte SIMD alignment)
        class FakePlane:
            def __init__(self, width: int, height: int, line_size: int, fill_val: int, pad_val: int):
                self.line_size = line_size
                buf = bytearray(line_size * height)
                for y in range(height):
                    row_start = y * line_size
                    buf[row_start : row_start + width] = bytes([fill_val]) * width
                    buf[row_start + width : row_start + line_size] = bytes([pad_val]) * (line_size - width)
                self._buf = bytes(buf)

            def __buffer__(self, flags: int) -> memoryview:
                return memoryview(self._buf)

        class FakeFrame:
            def __init__(self, w: int, h: int, y_plane: FakePlane, u_plane: FakePlane, v_plane: FakePlane):
                self.width = w
                self.height = h
                self.planes = [y_plane, u_plane, v_plane]

        # Active pixels: Y=40, U=128, V=128 (pure monochrome IR!).
        # Stride padding bytes are poisoned with 255 (high chroma!), which previously skewed sampling!
        w, h = 66, 34
        y_plane = FakePlane(w, h, line_size=128, fill_val=40, pad_val=250)
        u_plane = FakePlane(w // 2, h // 2, line_size=64, fill_val=128, pad_val=255)
        v_plane = FakePlane(w // 2, h // 2, line_size=64, fill_val=128, pad_val=0)
        fake_frame = FakeFrame(w, h, y_plane, u_plane, v_plane)

        mock_ctx = MagicMock()
        mock_ctx.parse.return_value = [MagicMock()]
        mock_ctx.decode.return_value = [fake_frame]

        with patch("av.CodecContext.create", return_value=mock_ctx):
            fp = extract_hevc_keyframe_luma(b"dummy_hevc")
            self.assertEqual(fp, bytes([40]) * 256)

            verdict = analyze_hevc_keyframe(b"dummy_hevc")
            self.assertIsNotNone(verdict)
            is_nv, max_chroma = verdict
            self.assertTrue(is_nv)
            self.assertEqual(max_chroma, 0.0)

    def test_tiny_hevc_keyframes_do_not_raise_index_error(self):
        # 16x16 real encoded HEVC frame (smaller than old 16*step assumptions)
        tiny_mono = _encode_single_hevc_frame(16, 16, (80, 80, 80))
        fp = extract_hevc_keyframe_luma(tiny_mono)
        self.assertIsNotNone(fp)
        self.assertEqual(len(fp), 256)

        verdict = analyze_hevc_keyframe(tiny_mono)
        self.assertIsNotNone(verdict)
        self.assertTrue(verdict[0])


class TestTimelapseRecorderCore(unittest.IsolatedAsyncioTestCase):
    async def test_timelapse_recorder_circular_ring_buffer(self):
        with tempfile.TemporaryDirectory() as td:
            mock_streamer = MagicMock()
            mock_streamer.is_running = True

            recorder = TimelapseRecorder(
                streamer=mock_streamer,
                storage_dir=td,
                total_slots=3,
                slot_duration_sec=10.0,
                sample_interval_sec=0.01,
                fps=30,
            )

            st = recorder.get_status()
            self.assertFalse(st["is_recording"])
            self.assertEqual(st["total_slots"], 3)
            self.assertEqual(st["filled_slots"], 0)
            self.assertFalse(st["has_content"])
            self.assertEqual(recorder.slot_path(0).name, "slot_00.hevc")
            self.assertEqual(recorder.slot_path(2).name, "slot_02.hevc")

            dummy_key_1 = b"\x00\x00\x00\x01\x40\x01" + b"A" * 100
            dummy_key_2 = b"\x00\x00\x00\x01\x40\x01" + b"B" * 100
            dummy_delta = b"\x00\x00\x00\x01\x02\x01" + b"C" * 50

            async def fake_hevc_stream():
                yield dummy_key_1, True
                yield dummy_delta, False
                yield dummy_key_2, True
                try:
                    while True:
                        await asyncio.sleep(10)
                except asyncio.CancelledError:
                    pass

            mock_streamer.subscribe_hevc = fake_hevc_stream
            await recorder.start()
            self.assertTrue(recorder.is_recording)
            await asyncio.sleep(0.1)
            await recorder.stop()
            self.assertFalse(recorder.is_recording)

            st = recorder.get_status()
            self.assertTrue(st["has_content"])
            self.assertGreater(st["total_frames"], 0)

            recorder._rotate_slot()
            self.assertEqual(recorder._current_slot, 1)
            recorder._active_file.write(dummy_key_1)
            recorder._rotate_slot()
            self.assertEqual(recorder._current_slot, 2)
            recorder._active_file.write(dummy_key_2)
            recorder._rotate_slot()
            self.assertEqual(recorder._current_slot, 0)
            recorder._active_file.write(b"NEW_DATA")
            recorder._close_active_slot()
            self.assertEqual(recorder.slot_path(0).read_bytes(), b"NEW_DATA")

            recorder.clear()
            self.assertEqual(recorder._current_slot, 0)
            self.assertEqual(len(recorder._slots_meta), 0)
            self.assertFalse(recorder.slot_path(0).exists())

            self.assertEqual(format_duration(3600), "1h 00m")
            self.assertEqual(format_duration(45), "0m 45s")
            self.assertEqual(format_video_duration(720), "12:00")
            self.assertEqual(format_video_duration(65), "1:05")

    async def test_timelapse_export_mp4_stream_copy(self):
        with tempfile.TemporaryDirectory() as td:
            storage_dir = Path(td)
            mock_streamer = MagicMock()
            mock_streamer.is_running = True

            recorder = TimelapseRecorder(
                streamer=mock_streamer,
                storage_dir=storage_dir,
                total_slots=4,
                fps=30,
            )

            slot0 = recorder.slot_path(0)
            slot1 = recorder.slot_path(1)
            for s in (slot0, slot1):
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=30",
                        "-c:v", "libx265", "-f", "hevc", str(s),
                    ],
                    check=True,
                    capture_output=True,
                )

            recorder._slots_meta["0"] = {"mtime": 1000.0, "frames": 30, "bytes": slot0.stat().st_size}
            recorder._slots_meta["1"] = {"mtime": 2000.0, "frames": 30, "bytes": slot1.stat().st_size}

            ordered = recorder.get_ordered_slots()
            self.assertEqual([x[0] for x in ordered], [0, 1])

            mp4_path = await recorder.export_mp4(codec="hevc")
            self.assertIsNotNone(mp4_path)
            self.assertTrue(mp4_path.exists())
            self.assertGreater(mp4_path.stat().st_size, 1000)

            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4_path)],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertGreater(float(probe.stdout.strip()), 0.5)
            self.assertEqual(mp4_path, await recorder.export_mp4(codec="hevc"))

            mp4_h264 = await recorder.export_mp4(codec="h264")
            self.assertIsNotNone(mp4_h264)
            self.assertTrue(mp4_h264.exists())

    async def test_timelapse_export_framerate_bounds_dedup_and_range_stability(self):
        with tempfile.TemporaryDirectory() as td:
            storage_dir = Path(td)
            mock_streamer = MagicMock()
            mock_streamer.is_running = True
            recorder = TimelapseRecorder(streamer=mock_streamer, storage_dir=storage_dir, total_slots=2)

            slot0 = recorder.slot_path(0)
            slot0.write_bytes(b"\x00\x00\x00\x01\x40\x01" + b"\x00" * 100)
            recorder._slots_meta["0"] = {"mtime": 1000.0, "frames": 250, "duration": 1.0, "bytes": slot0.stat().st_size}

            captured_cmd = None

            def mock_run(cmd, *args, **kwargs):
                nonlocal captured_cmd
                captured_cmd = cmd
                Path(cmd[-1]).write_bytes(b"fake_mp4_bytes")
                m = MagicMock()
                m.returncode = 0
                return m

            with patch("subprocess.run", side_effect=mock_run):
                mp4 = await recorder.export_mp4(codec="hevc", ratio=60)
                self.assertIsNotNone(mp4)
                self.assertNotIn("-bsf:v", captured_cmd)
                r_idx = captured_cmd.index("-r")
                fps_arg = float(captured_cmd[r_idx + 1])
                self.assertLessEqual(fps_arg, 60.0)
                self.assertGreaterEqual(fps_arg, 5.0)

            call_count = 0

            def mock_generate(slots, out, codec, fps):
                nonlocal call_count
                call_count += 1
                time.sleep(0.04)
                out.write_bytes(b"dummy_mp4_data")
                return True

            with patch.object(recorder, "_generate_mp4", side_effect=mock_generate):
                res1, res2 = await asyncio.gather(
                    recorder.export_mp4(codec="h264", ratio=60),
                    recorder.export_mp4(codec="h264", ratio=60),
                )
                self.assertEqual(res1, res2)
                self.assertEqual(call_count, 1)

                recorder._running = True
                recorder._record_task = MagicMock()
                recorder._record_task.done.return_value = False
                slot0.write_bytes(slot0.read_bytes() + b"\x00" * 20)
                await recorder.export_mp4(codec="h264", ratio=60)
                self.assertEqual(call_count, 1)

                recorder._exports_cache["h264_r60"]["created_at"] = time.time() - 100.0
                await recorder.export_mp4(codec="h264", ratio=60, allow_cache_only=True)
                self.assertEqual(call_count, 1)
                await recorder.export_mp4(codec="h264", ratio=60, allow_cache_only=False)
                self.assertEqual(call_count, 2)

                # Matching session_id preserves cached export mid-stream even if TTL expired
                recorder._exports_cache["h264_r60"]["created_at"] = time.time() - 100.0
                recorder._exports_cache["h264_r60"]["session_id"] = "sess_42"
                await recorder.export_mp4(codec="h264", ratio=60, allow_cache_only=False, session_id="sess_42")
                self.assertEqual(call_count, 2)

                # Different session_id regenerates export
                await recorder.export_mp4(codec="h264", ratio=60, allow_cache_only=False, session_id="sess_99")
                self.assertEqual(call_count, 3)

    def test_timelapse_motion_events_timeline_and_concurrent_thread_safety(self):
        with tempfile.TemporaryDirectory() as td:
            storage_dir = Path(td)
            recorder = TimelapseRecorder(
                streamer=MagicMock(),
                storage_dir=storage_dir,
                total_slots=4,
                slot_duration_sec=600.0,
                speed_ratio=60,
            )

            s0 = recorder.slot_path(0)
            s0.write_bytes(b"\x00\x00\x00\x01\x40\x01" + b"\x00" * 50)
            recorder._slots_meta["0"] = {"mtime": 10000.0, "duration": 600.0, "frames": 300, "bytes": 56}

            self.assertTrue(recorder.record_event(event_type="motion", label="Motion Alert", timestamp=9700.0))
            self.assertFalse(recorder.record_event(event_type="motion", label="Motion Alert", timestamp=9705.0))
            self.assertFalse(recorder.record_event(event_type="motion", label="Motion Alert", timestamp=9730.0))
            self.assertTrue(
                recorder.record_event(event_type="motion", label="Baby Motion", timestamp=9850.0, end_timestamp=9880.0)
            )

            st = recorder.get_status()
            self.assertEqual(st["motion_events_count"], 2)
            self.assertEqual(st["events"][0]["percent"], 50.0)
            self.assertEqual(st["events"][0]["end_percent"], 55.0)

            # Concurrent record_event + get_status across 8 threads must not corrupt meta.json or drop events
            def record_worker(i: int) -> None:
                recorder.record_event(
                    event_type=f"type_{i}",
                    label=f"Event {i}",
                    timestamp=9500.0 + i * 5.0,
                )
                recorder.get_status()

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(record_worker, range(20)))

            meta_on_disk = json.loads(recorder.meta_file.read_text())
            self.assertEqual(len(meta_on_disk["events"]), 22)

    def test_circular_wrapped_buffer_active_slot_ordering_and_events(self):
        """Ensure full ring buffer (e.g. 72 slots) with active recording in the middle
        places active slot at the end of ordered slots and never drops motion events."""
        with tempfile.TemporaryDirectory() as td:
            storage_dir = Path(td)
            recorder = TimelapseRecorder(
                streamer=MagicMock(),
                storage_dir=storage_dir,
                total_slots=72,
                slot_duration_sec=600.0,
                speed_ratio=60,
            )
            now = time.time()
            # Slots 35..71 have mtimes from previous 12h cycle
            for i in range(35, 72):
                recorder.slot_path(i).write_bytes(b"x" * 1000)
                recorder._slots_meta[str(i)] = {"mtime": now - (72 - (i - 35)) * 600.0, "duration": 600.0, "frames": 100}
            # Slots 0..34 have mtimes from current cycle
            for i in range(0, 35):
                recorder.slot_path(i).write_bytes(b"x" * 1000)
                recorder._slots_meta[str(i)] = {"mtime": now - (35 - i) * 600.0, "duration": 600.0, "frames": 100}

            # Slot 35 is currently recording
            recorder._current_slot = 35
            recorder._running = True
            recorder._record_task = MagicMock()
            recorder._record_task.done.return_value = False
            recorder._slot_start_monotonic = time.monotonic() - 60.0
            recorder._slot_start_wall = now - 60.0

            ordered = recorder.get_ordered_slots()
            self.assertEqual(len(ordered), 72)
            # Oldest slot should be 36, newest active slot should be 35 at the end
            self.assertEqual(ordered[0][0], 36)
            self.assertEqual(ordered[-1][0], 35)

            # Events recorded during the 12-hour buffer must NOT be filtered out
            recorder._events = [
                {"time": now - 15000.0, "end_time": now - 14900.0, "type": "motion", "label": "Motion Alert 1"},
                {"time": now - 5000.0, "end_time": now - 4900.0, "type": "motion", "label": "Motion Alert 2"},
            ]
            st = recorder.get_status()
            self.assertEqual(len(st["events"]), 2)
            self.assertEqual(st["motion_events_count"], 2)
            self.assertEqual(st["events"][0]["label"], "Motion Alert 1")
            self.assertEqual(st["events"][1]["label"], "Motion Alert 2")

    async def test_timelapse_playback_motion_detection_and_sustained_ranges(self):
        fp_a = b"\x10" * 256
        fp_b = b"\x10" * 256
        fp_c = b"\x30" * 256
        self.assertEqual(luma_fingerprint_diff(fp_a, fp_b), 0.0)
        self.assertEqual(luma_fingerprint_diff(fp_a, fp_c), 32.0)
        self.assertEqual(luma_fingerprint_diff(None, fp_a), 999.0)

        fp_spike = bytearray(b"\x10" * 256)
        fp_spike[1 * 16 + 3] = 0x10 + 200
        self.assertEqual(luma_fingerprint_diff(fp_a, bytes(fp_spike)), 0.0)

        self.assertEqual(merge_motion_ranges([]), [])
        self.assertEqual(format_event_duration(85.0), "1m 25s")

        with tempfile.TemporaryDirectory() as td:
            recorder = TimelapseRecorder(
                streamer=MagicMock(),
                storage_dir=Path(td),
                total_slots=4,
                slot_duration_sec=300.0,
                speed_ratio=60,
                motion_merge_gap_sec=30.0,
            )

            vps_marker = b"\x00\x00\x00\x01\x40\x01"
            s0 = recorder.slot_path(0)
            p0 = b"".join(vps_marker + bytes([0, i]) + b"\x00" * 40 for i in range(150))
            s0.write_bytes(p0)
            recorder._slots_meta["0"] = {"mtime": 1300.0, "duration": 300.0, "frames": 150, "bytes": len(p0)}

            s1 = recorder.slot_path(1)
            p1 = b"".join(vps_marker + bytes([1, i]) + b"\x00" * 40 for i in range(150))
            s1.write_bytes(p1)
            recorder._slots_meta["1"] = {"mtime": 5200.0, "duration": 300.0, "frames": 150, "bytes": len(p1)}

            def mock_luma(chunk):
                slot_id, frame_idx = chunk[6], chunk[7]
                if slot_id == 0:
                    if frame_idx < 10:
                        return b"\x10" * 256
                    if frame_idx <= 20:
                        return bytes([0x10 + (frame_idx - 9) * 5]) * 256
                    if frame_idx <= 25:
                        return bytes([0x10 + 11 * 5]) * 256
                    if frame_idx <= 35:
                        return bytes([0x10 + 11 * 5 + (frame_idx - 25) * 5]) * 256
                    if frame_idx < 110:
                        return bytes([0x10 + 21 * 5]) * 256
                    if frame_idx <= 115:
                        return bytes([0x10 + 21 * 5 + (frame_idx - 109) * 6]) * 256
                    return bytes([0x10 + 27 * 6]) * 256
                return b"\xd0" * 256

            with patch("momcozy.timelapse.extract_hevc_keyframe_luma", side_effect=mock_luma):
                ranges = await recorder.scan_motion_events(force=True)

            self.assertEqual(len(ranges), 2)
            self.assertGreater(ranges[0]["end_time"] - ranges[0]["time"], 45.0)

    async def test_motion_scan_optimizations_clustering_gating_and_stage_breakdown(self):
        """Verify optimizations #1, #2, #3, #5, #6, and #9 in detect_motion_events."""
        with tempfile.TemporaryDirectory() as td:
            storage_dir = Path(td)
            recorder = TimelapseRecorder(
                streamer=MagicMock(),
                storage_dir=storage_dir,
                sample_interval_sec=1.0,
                fps=10,
                speed_ratio=60,
                motion_merge_gap_sec=30.0,
            )

            # 180 frames: coarse_step=30 => 6 coarse windows:
            # [0, 30]: quiet
            # [30, 60]: active onset
            # [60, 90]: active interior
            # [90, 120]: active interior
            # [120, 150]: active offset
            # [150, 180]: quiet
            vps_marker = b"\x00\x00\x00\x01\x40\x01"
            s0 = recorder.slot_path(0)
            payload = b"".join(vps_marker + bytes([0, i]) + b"\x00" * 40 for i in range(180))
            s0.write_bytes(payload)
            recorder._slots_meta["0"] = {"mtime": 1000.0, "duration": 180.0, "frames": 180, "bytes": len(payload)}

            decoded_frames = []
            codecs_received = []

            def mock_luma(chunk, codec=None):
                frame_idx = chunk[7]
                decoded_frames.append(frame_idx)
                codecs_received.append(codec)
                # Sustained motion active from frame 45 to 135 with dynamic changes
                if 45 <= frame_idx <= 135:
                    return bytes([0x20 + (frame_idx * 3) % 200]) * 256
                return b"\x10" * 256

            with patch("momcozy.timelapse.extract_hevc_keyframe_luma", side_effect=mock_luma):
                with self.assertLogs("momcozy.timelapse", level="INFO") as log_ctx:
                    events = recorder.detect_motion_events(coarse_step=30, min_window=2)

            self.assertEqual(len(events), 1)
            # Item #6: Codec context was created and passed to extract_hevc_keyframe_luma
            self.assertTrue(any(c is not None for c in codecs_received))

            # Item #1: Interior windows [60, 90] and [90, 120] should NOT have had interior decodes
            # (e.g. frame 75, 105)
            self.assertNotIn(75, decoded_frames)
            self.assertNotIn(105, decoded_frames)

            # Item #2: Outward probe should NOT enter quiet windows [0, 30] and [150, 180]
            self.assertNotIn(15, decoded_frames)
            self.assertNotIn(165, decoded_frames)

            # Item #9: Stage breakdown logged
            found_stage_log = any(
                "Motion scan complete: indexed 180 frames (decoded" in m and "coarse" in m and "search" in m and "probe" in m
                for m in log_ctx.output
            )
            self.assertTrue(found_stage_log, f"Log messages: {log_ctx.output}")

    async def test_slot_level_motion_scan_caching_and_invalidation(self):
        """Verify per-slot motion scan caching, cache hits, persistence, and invalidation."""
        with tempfile.TemporaryDirectory() as td:
            storage_dir = Path(td)
            recorder = TimelapseRecorder(
                streamer=MagicMock(),
                storage_dir=storage_dir,
                sample_interval_sec=2.0,
                fps=10,
                speed_ratio=60,
                motion_merge_gap_sec=30.0,
            )

            vps_marker = b"\x00\x00\x00\x01\x40\x01"
            # Slot 0: 60 frames with motion between 20 and 40
            s0 = recorder.slot_path(0)
            p0 = b"".join(vps_marker + bytes([0, i]) + b"\x00" * 40 for i in range(60))
            s0.write_bytes(p0)
            recorder._slots_meta["0"] = {"mtime": 1000.0, "duration": 120.0, "frames": 60, "bytes": len(p0)}

            # Slot 1: 60 frames with no motion
            s1 = recorder.slot_path(1)
            p1 = b"".join(vps_marker + bytes([1, i]) + b"\x00" * 40 for i in range(60))
            s1.write_bytes(p1)
            recorder._slots_meta["1"] = {"mtime": 2000.0, "duration": 120.0, "frames": 60, "bytes": len(p1)}
            recorder._save_meta()

            decoded_chunks = []

            def mock_luma(chunk, codec=None):
                slot_id, frame_idx = chunk[6], chunk[7]
                decoded_chunks.append((slot_id, frame_idx))
                if slot_id == 0 and 20 <= frame_idx <= 40:
                    return bytes([0x20 + (frame_idx * 5) % 150]) * 256
                return b"\x10" * 256

            with patch("momcozy.timelapse.extract_hevc_keyframe_luma", side_effect=mock_luma):
                with self.assertLogs("momcozy.timelapse", level="INFO") as log_ctx1:
                    ev1 = recorder.detect_motion_events(coarse_step=15, min_window=2)

            self.assertEqual(len(ev1), 1)
            self.assertGreater(len(decoded_chunks), 0)
            # Metadata now stores scan_intervals for both slots
            self.assertIn("scan_intervals", recorder._slots_meta["0"])
            self.assertIn("scan_intervals", recorder._slots_meta["1"])
            self.assertEqual(len(recorder._slots_meta["1"]["scan_intervals"]), 0)  # quiet slot
            self.assertGreater(len(recorder._slots_meta["0"]["scan_intervals"]), 0)

            # Second scan without changes: 100% cache hit, 0 new decodes
            decoded_chunks.clear()
            with patch("momcozy.timelapse.extract_hevc_keyframe_luma", side_effect=mock_luma):
                # Reset scan mtime check to trigger detect_motion_events
                recorder._last_motion_scan_mtime = 0.0
                with self.assertLogs("momcozy.timelapse", level="INFO") as log_ctx2:
                    ev2 = recorder.detect_motion_events(coarse_step=15, min_window=2)

            self.assertEqual(len(ev2), 1)
            self.assertEqual(len(decoded_chunks), 0)  # Zero keyframes decoded!
            self.assertTrue(any("2 slots cached" in m for m in log_ctx2.output))

            # Test persistence across reload: meta.json preserves scan_intervals
            recorder2 = TimelapseRecorder(
                streamer=MagicMock(),
                storage_dir=storage_dir,
                sample_interval_sec=2.0,
                fps=10,
                speed_ratio=60,
                motion_merge_gap_sec=30.0,
            )
            self.assertIn("scan_intervals", recorder2._slots_meta["0"])
            decoded_chunks.clear()
            with patch("momcozy.timelapse.extract_hevc_keyframe_luma", side_effect=mock_luma):
                ev_reloaded = recorder2.detect_motion_events(coarse_step=15, min_window=2)
            self.assertEqual(len(ev_reloaded), 1)
            self.assertEqual(len(decoded_chunks), 0)

            # Invalidate slot 1 by rewriting with motion
            p1_mod = b"".join(vps_marker + bytes([1, i]) + b"\x00" * 40 for i in range(70))
            s1.write_bytes(p1_mod)
            # Update meta for slot 1
            recorder._slots_meta["1"]["bytes"] = len(p1_mod)
            recorder._slots_meta["1"]["frames"] = 70
            recorder._slots_meta["1"]["mtime"] = 2500.0
            recorder._last_motion_scan_mtime = 0.0

            decoded_chunks.clear()
            with patch("momcozy.timelapse.extract_hevc_keyframe_luma", side_effect=mock_luma):
                with self.assertLogs("momcozy.timelapse", level="INFO") as log_ctx3:
                    ev3 = recorder.detect_motion_events(coarse_step=15, min_window=2)

            self.assertEqual(len(ev3), 1)
            # Slot 0 hit cache (not decoded), only modified slot 1 was decoded!
            self.assertTrue(all(slot_id == 1 for slot_id, _ in decoded_chunks))
            self.assertTrue(any("1 slots cached" in m for m in log_ctx3.output))

            # Test force=True: all slots re-scanned even if valid cache exists
            decoded_chunks.clear()
            recorder._last_motion_scan_mtime = 0.0
            with patch("momcozy.timelapse.extract_hevc_keyframe_luma", side_effect=mock_luma):
                recorder.detect_motion_events(coarse_step=15, min_window=2, force=True)
            self.assertTrue(any(slot_id == 0 for slot_id, _ in decoded_chunks))
            self.assertTrue(any(slot_id == 1 for slot_id, _ in decoded_chunks))

    async def test_premux_slots_on_rotation_and_closed_slot_caching(self):
        """Verify pre-muxing slots on rotation, fast concat, and closed-slot export caching."""
        with tempfile.TemporaryDirectory() as td:
            storage_dir = Path(td)
            mock_streamer = MagicMock()
            mock_streamer.is_running = True
            recorder = TimelapseRecorder(
                streamer=mock_streamer,
                storage_dir=storage_dir,
                total_slots=4,
                slot_duration_sec=600.0,
                speed_ratio=60,
            )

            # Generate real valid HEVC bitstream for slot 0
            s0 = recorder.slot_path(0)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=15",
                    "-c:v", "libx265", "-f", "hevc", str(s0),
                ],
                check=True,
                capture_output=True,
            )
            recorder._slots_meta["0"] = {"mtime": 1000.0, "frames": 15, "duration": 1.0, "bytes": s0.stat().st_size}

            # 1. Test _sync_premux_slot
            mp4_0 = recorder.slot_mp4_path(0)
            self.assertFalse(mp4_0.exists())
            self.assertTrue(recorder._sync_premux_slot(0))
            self.assertTrue(mp4_0.exists())
            self.assertGreater(mp4_0.stat().st_size, 100)

            # Re-running _sync_premux_slot hits mtime cache without running ffmpeg
            with patch("subprocess.run") as mock_subproc:
                self.assertTrue(recorder._sync_premux_slot(0))
                mock_subproc.assert_not_called()

            # 2. Test _rotate_slot clears stale mp4 for overwritten slot and pre-muxes closed slot
            # Pre-create a stale mp4 for slot 1
            mp4_1 = recorder.slot_mp4_path(1)
            mp4_1.write_bytes(b"stale_mp4")
            self.assertTrue(mp4_1.exists())

            recorder._current_slot = 0
            recorder._rotate_slot()
            self.assertEqual(recorder._current_slot, 1)
            self.assertFalse(mp4_1.exists())  # stale slot 1 mp4 deleted
            await asyncio.sleep(0.1)  # wait for bg rotate prep to finish

            # 3. Test closed-slot vs active slot partitioning during active recording
            recorder._running = True
            recorder._record_task = MagicMock()
            recorder._record_task.done.return_value = False
            s1 = recorder.slot_path(1)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=15",
                    "-c:v", "libx265", "-f", "hevc", str(s1),
                ],
                check=True,
                capture_output=True,
            )
            recorder._slots_meta["1"] = {"mtime": 2000.0, "frames": 15, "duration": 1.0, "bytes": s1.stat().st_size}

            # Normal playback request (include_active=False): uses closed slots (slot 0)
            recorder._exports_cache.clear()
            recorder._in_flight_exports.clear()
            with patch.object(recorder, "_generate_mp4", wraps=recorder._generate_mp4) as spy_gen:
                mp4_export = await recorder.export_mp4(codec="hevc", ratio=60, include_active=False)
                self.assertIsNotNone(mp4_export)
                self.assertTrue(mp4_export.exists())
                spy_gen.assert_called_once()
                slots_passed = spy_gen.call_args[0][0]
                self.assertEqual([s[0] for s in slots_passed], [0])  # Slot 1 excluded

                # Immediate follow-up call: returns cached export without re-generating!
                spy_gen.reset_mock()
                mp4_export2 = await recorder.export_mp4(codec="hevc", ratio=60, include_active=False)
                self.assertEqual(mp4_export, mp4_export2)
                spy_gen.assert_not_called()

                # Explicit download request (include_active=True): includes active slot 1
                spy_gen.reset_mock()
                mp4_download = await recorder.export_mp4(codec="hevc", ratio=60, include_active=True)
                self.assertIsNotNone(mp4_download)
                spy_gen.assert_called_once()
                slots_download = spy_gen.call_args[0][0]
                self.assertEqual([s[0] for s in slots_download], [0, 1])  # Slot 1 included

            # 4. Test _generate_mp4 selects pre-muxed mp4 for concat
            captured_list = None
            orig_run = subprocess.run

            def capture_ffmpeg(cmd, *args, **kwargs):
                nonlocal captured_list
                if "-f" in cmd and "concat" in cmd:
                    list_idx = cmd.index("-i") + 1
                    captured_list = Path(cmd[list_idx]).read_text()
                return orig_run(cmd, *args, **kwargs)

            with patch("subprocess.run", side_effect=capture_ffmpeg):
                recorder._exports_cache.clear()
                await recorder.export_mp4(codec="hevc", ratio=60, include_active=True)
                self.assertIsNotNone(captured_list)
                self.assertIn("slot_00.mp4", captured_list)

            # 5. Test clear removes both .hevc and .mp4 files
            recorder.clear()
            self.assertFalse(s0.exists())
            self.assertFalse(mp4_0.exists())


class TestTimelapseNightVisionSync(unittest.IsolatedAsyncioTestCase):
    async def test_recorder_sync_nv_state_and_session_append_vs_restart(self):
        with tempfile.TemporaryDirectory() as td:
            storage_dir = Path(td)
            mock_streamer = MagicMock()
            mock_streamer.is_running = True

            async def dummy_hevc():
                if False:
                    yield b"", True

            mock_streamer.subscribe_hevc = dummy_hevc

            mock_config = storage_dir / "mock_momcozy.json"
            mock_config.write_text(json.dumps({"timelapse_sync_night_vision": True}))
            rec = TimelapseRecorder(streamer=mock_streamer, storage_dir=storage_dir, config_path=mock_config)
            self.assertTrue(rec.sync_night_vision)

            rec.set_sync_night_vision(False)
            self.assertFalse(json.loads(mock_config.read_text())["timelapse_sync_night_vision"])
            rec.set_sync_night_vision(True)
            self.assertTrue(json.loads(mock_config.read_text())["timelapse_sync_night_vision"])

            with patch.dict(os.environ, {"MOMCOZY_TIMELAPSE_SYNC_NV": "1"}):
                rec_env = TimelapseRecorder(streamer=mock_streamer, storage_dir=storage_dir, config_path=mock_config)
                self.assertTrue(rec_env.sync_night_vision)

            await rec.start(fresh=True, session_type="manual")
            s0 = rec.slot_path(0)
            s0.write_bytes(b"\x00\x00\x00\x01\x40\x01" + b"\x00" * 20)
            rec._slots_meta["0"] = {"mtime": time.time(), "frames": 10, "bytes": 26}
            rec._save_meta()
            await rec.stop()
            self.assertEqual(rec.last_session_type, "manual")

            await rec.start(fresh=(rec.last_session_type != "auto"), session_type="auto")
            s0.write_bytes(b"\x00\x00\x00\x01\x40\x01" + b"\x00" * 30)
            rec._slots_meta["0"] = {"mtime": time.time(), "frames": 15, "bytes": 36}
            rec._save_meta()
            await rec.stop()
            self.assertEqual(rec.last_session_type, "auto")

            await rec.start(fresh=(rec.last_session_type != "auto"), session_type="auto")
            self.assertEqual(s0.stat().st_size, 36)
            await rec.stop()

    def test_detect_night_vision_from_jpeg_and_webserver_active_check(self):
        def make_jpeg(r: int, g: int, b: int) -> bytes:
            im = Image.new("RGB", (32, 32), color=(r, g, b))
            bio = io.BytesIO()
            im.save(bio, format="JPEG")
            return bio.getvalue()

        color_day = make_jpeg(220, 100, 50)
        mono_night = make_jpeg(120, 120, 120)
        mono_dark = make_jpeg(20, 20, 20)
        self.assertFalse(detect_night_vision_from_jpeg(color_day))
        self.assertTrue(detect_night_vision_from_jpeg(mono_night))
        self.assertTrue(detect_night_vision_from_jpeg(mono_dark))
        self.assertFalse(detect_night_vision_from_jpeg(b""))
        self.assertFalse(detect_night_vision_from_jpeg(b"corrupted_jpeg_data"))

        server = WebServer(monitor=MagicMock(), streamer=None)
        self.assertFalse(server.is_night_vision_active(None))
        self.assertFalse(server.is_night_vision_active(BM04Status(night_vision="0"), frame_bytes=color_day))
        self.assertTrue(server.is_night_vision_active(BM04Status(night_vision="0"), frame_bytes=mono_night))
        self.assertFalse(server.is_night_vision_active(BM04Status(night_vision="1"), frame_bytes=mono_night))
        self.assertFalse(server.is_night_vision_active(BM04Status(night_vision="2"), frame_bytes=mono_night))


class TestNightVisionAutomationStateMachine(unittest.TestCase):
    def test_darkness_activation_and_daylight_deactivation(self):
        nv = NightVisionAutomation()
        now = 1000.0
        self.assertEqual(nv.step(now, is_dark=False, recording=False, session_auto=False), Action.NONE)
        self.assertEqual(nv.step(now, is_dark=True, recording=False, session_auto=False), Action.START_AUTO)

        nv_delayed = NightVisionAutomation(activate_delay=60.0)
        self.assertEqual(nv_delayed.step(now, is_dark=True, recording=False, session_auto=False), Action.NONE)
        state, rem = nv_delayed.sync_state(now + 30.0, recording=False, session_auto=False)
        self.assertEqual(state, SyncState.ACTIVATING)
        self.assertAlmostEqual(rem, 30.0, places=1)
        self.assertEqual(nv_delayed.step(now + 60.1, is_dark=True, recording=False, session_auto=False), Action.START_AUTO)

        nv2 = NightVisionAutomation()
        self.assertEqual(nv2.step(now, is_dark=True, recording=True, session_auto=True), Action.MARK_AUTO)
        self.assertEqual(nv2.step(now + 10.0, is_dark=False, recording=True, session_auto=True), Action.NONE)
        self.assertTrue(nv2.is_stopping)
        self.assertEqual(nv2.step(now + 130.1, is_dark=False, recording=True, session_auto=True), Action.STOP_AUTO)

    def test_manual_daytime_and_manual_stop_suppression(self):
        nv = NightVisionAutomation()
        now = 1000.0
        self.assertEqual(nv.step(now, is_dark=False, recording=True, session_auto=False), Action.NONE)
        self.assertEqual(nv.step(now + 300.0, is_dark=False, recording=True, session_auto=False), Action.NONE)

        nv.on_manual_stop()
        self.assertEqual(nv.step(now, is_dark=True, recording=False, session_auto=False), Action.NONE)
        self.assertEqual(nv.step(now + 50.0, is_dark=False, recording=False, session_auto=False), Action.NONE)
        self.assertEqual(nv.step(now + 60.0, is_dark=True, recording=False, session_auto=False), Action.START_AUTO)

    def test_webserver_adaptive_nv_check_interval(self):
        mock_streamer = MagicMock()
        mock_streamer.is_running = False
        server = WebServer(monitor=MagicMock(), streamer=mock_streamer)
        self.assertEqual(server._nv_check_interval(), 300.0)
        mock_streamer.is_running = True
        self.assertEqual(server._nv_check_interval(), 30.0)

        server.timelapse = MagicMock()
        server.timelapse.is_recording = True
        server.nv._light_since = None
        self.assertEqual(server._nv_check_interval(), 60.0)
        server.nv._light_since = 1000.0
        self.assertEqual(server._nv_check_interval(), 15.0)

        server.timelapse.sync_night_vision = True
        server._last_nv_check = time.monotonic()
        server.timelapse.is_recording = True
        self.assertAlmostEqual(server._monitor_tick_delay(), 4.0, places=1)
        server.timelapse.is_recording = False
        mock_streamer.is_running = False
        self.assertAlmostEqual(server._monitor_tick_delay(), 30.0, places=1)


if __name__ == "__main__":
    unittest.main()
