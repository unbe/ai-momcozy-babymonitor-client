"""Video streaming and recording utilities for Momcozy BM04.

Provides MJPEG live streaming, snapshot taking, and video recording to MP4.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from pathlib import Path
from typing import AsyncIterator, Optional, Set

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class VideoStreamer:
    """Manages the video stream from Momcozy BM04."""

    def __init__(
        self,
        device_id: str,
        local_key: str,
        email: str,
        password: str,
        country_code: str = "1",
        region: str = "us",
    ) -> None:
        self.device_id = device_id
        self.local_key = local_key
        self.email = email
        self.password = password
        self.country_code = country_code
        self.region = region

        self._running = False
        self._latest_frame: Optional[bytes] = None
        self._listeners: Set[asyncio.Queue[bytes]] = set()
        self._stream_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def latest_frame(self) -> Optional[bytes]:
        return self._latest_frame

    async def start(self) -> None:
        """Start background streaming task."""
        async with self._lock:
            if self._running:
                return
            self._running = True
            self._stream_task = asyncio.create_task(self._stream_loop())
            logger.info("Video streamer started for device %s", self.device_id)

    async def stop(self) -> None:
        """Stop background streaming task."""
        async with self._lock:
            self._running = False
            if self._stream_task:
                self._stream_task.cancel()
                try:
                    await self._stream_task
                except asyncio.CancelledError:
                    pass
                self._stream_task = None
            logger.info("Video streamer stopped")

    async def _stream_loop(self) -> None:
        """Internal loop fetching frames from the camera P2P stream."""
        from tuya_ipc_p2p_sdk import TuyaIpcP2pClient

        backoff = 2
        while self._running:
            try:
                logger.info("Connecting to P2P video stream for %s...", self.device_id)
                async with TuyaIpcP2pClient(
                    username=self.email,
                    password=self.password,
                    country_code=self.country_code,
                    region=self.region,
                ) as client:
                    stream = client.create_camera_stream(self.device_id, self.local_key)
                    await stream.async_start()
                    backoff = 2
                    try:
                        async for jpeg_frame in stream.async_frames():
                            if not self._running:
                                break
                            self._latest_frame = jpeg_frame
                            for q in list(self._listeners):
                                if q.full():
                                    try:
                                        q.get_nowait()
                                    except asyncio.QueueEmpty:
                                        pass
                                try:
                                    q.put_nowait(jpeg_frame)
                                except asyncio.QueueFull:
                                    pass
                    finally:
                        await stream.async_stop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("P2P stream error: %s. Reconnecting in %ss...", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 1.5)

    async def subscribe(self) -> AsyncIterator[bytes]:
        """Subscribe to live frames."""
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=10)
        if self._latest_frame:
            queue.put_nowait(self._latest_frame)
        self._listeners.add(queue)
        try:
            while self._running:
                frame = await queue.get()
                yield frame
        finally:
            self._listeners.discard(queue)

    async def get_snapshot(self, timeout_seconds: float = 10.0) -> bytes:
        """Capture a single frame as JPEG."""
        if not self._running:
            await self.start()

        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            if self._latest_frame:
                return self._latest_frame
            await asyncio.sleep(0.1)

        raise TimeoutError("Timed out waiting for video frame from camera")

    async def record_clip(
        self,
        output_file: str | Path,
        duration_seconds: int = 10,
        fps: int = 15,
    ) -> Path:
        """Record video clip from live stream and save as MP4."""
        out_path = Path(output_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        first_frame = await self.get_snapshot()
        img = cv2.imdecode(np.frombuffer(first_frame, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("Failed to decode video frame")

        height, width, _ = img.shape
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(out_path), fourcc, fps, (width, height))

        start_time = time.time()
        frames_recorded = 0

        try:
            async for frame_bytes in self.subscribe():
                frame_img = cv2.imdecode(
                    np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
                )
                if frame_img is not None:
                    writer.write(frame_img)
                    frames_recorded += 1
                if time.time() - start_time >= duration_seconds:
                    break
        finally:
            writer.release()

        logger.info(
            "Recorded %d frames (%d seconds) to %s",
            frames_recorded,
            duration_seconds,
            out_path,
        )
        return out_path
