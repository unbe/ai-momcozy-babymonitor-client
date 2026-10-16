"""HLS (HTTP Live Streaming) transcoder for Momcozy BM04.

Transcodes live camera frames into HLS MPEG-TS segments and playlist (.m3u8)
using FFmpeg. Supports Google Chromecast, Apple TV, iOS Safari, and smart displays.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .stream import VideoStreamer

logger = logging.getLogger(__name__)

_SEGMENT_RE = re.compile(r"^segment_\d+\.ts$")


class HlsTranscoder:
    """Live HLS transcoder feeding MJPEG frames from VideoStreamer into an FFmpeg HLS pipeline."""

    def __init__(
        self,
        streamer: VideoStreamer,
        output_dir: Optional[Path | str] = None,
        hls_time: int = 2,
        hls_list_size: int = 5,
        idle_timeout: float = 60.0,
    ) -> None:
        self.streamer = streamer
        self.output_dir = Path(output_dir or "/tmp/momcozy_hls")
        self.hls_time = hls_time
        self.hls_list_size = hls_list_size
        self.idle_timeout = idle_timeout

        self._running = False
        self._process: Optional[subprocess.Popen] = None
        self._queue: queue.Queue[Optional[bytes]] = queue.Queue(maxsize=30)
        self._writer_thread: Optional[threading.Thread] = None
        self._feed_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._last_access = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def is_available(self) -> bool:
        """Return True if ffmpeg binary is present on the system."""
        return shutil.which("ffmpeg") is not None

    @property
    def is_running(self) -> bool:
        """Return True if FFmpeg transcoder is currently active and healthy."""
        return self._running and self._process is not None and self._process.poll() is None

    def touch(self) -> None:
        """Update last access time to reset idle timeout."""
        self._last_access = time.monotonic()

    async def start(self) -> bool:
        """Start FFmpeg HLS transcoding process."""
        async with self._lock:
            if self.is_running:
                self.touch()
                return True

            if not self.is_available:
                logger.error("FFmpeg not found. Cannot start HLS transcoder.")
                return False

            if not self.streamer.is_running:
                logger.info("Starting VideoStreamer for HLS stream...")
                await self.streamer.start()

            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._cleanup_files()

            playlist_path = self.output_dir / "stream.m3u8"
            segment_pattern = str(self.output_dir / "segment_%03d.ts")

            cmd = [
                "ffmpeg",
                "-y",
                "-f", "image2pipe",
                "-vcodec", "mjpeg",
                "-r", "15",
                "-i", "pipe:0",
                "-f", "lavfi",
                "-i", "anullsrc=channel_layout=mono:sample_rate=8000",
                "-shortest",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-pix_fmt", "yuv420p",
                "-g", "30",
                "-sc_threshold", "0",
                "-c:a", "aac",
                "-b:a", "32k",
                "-f", "hls",
                "-hls_time", str(self.hls_time),
                "-hls_list_size", str(self.hls_list_size),
                "-hls_flags", "delete_segments+split_by_time+omit_endlist",
                "-hls_segment_filename", segment_pattern,
                str(playlist_path),
            ]

            logger.info("Starting FFmpeg HLS pipeline: %s", " ".join(cmd[:6]) + " ...")
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except Exception as exc:
                logger.error("Failed to spawn FFmpeg process: %s", exc)
                return False

            self._running = True
            self._last_access = time.monotonic()

            # Start background writer thread
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

            self._writer_thread = threading.Thread(target=self._writer_worker, daemon=True)
            self._writer_thread.start()

            # Start frame reader task
            loop = asyncio.get_running_loop()
            self._feed_task = loop.create_task(self._feed_frames_task())
            self._idle_task = loop.create_task(self._idle_monitor_task())

            return True

    def _writer_worker(self) -> None:
        """Worker thread pushing JPEGs into FFmpeg stdin pipe."""
        proc = self._process
        if not proc or not proc.stdin:
            return

        while self._running:
            try:
                chunk = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if chunk is None or not self._running:
                break

            try:
                proc.stdin.write(chunk)
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                logger.warning("FFmpeg stdin closed or broken pipe")
                break
            except Exception as e:
                logger.debug("Error writing frame to FFmpeg: %s", e)
                break

    async def _feed_frames_task(self) -> None:
        """Read frames from VideoStreamer and enqueue for FFmpeg."""
        try:
            async for frame in self.streamer.subscribe():
                if not self._running:
                    break
                try:
                    if self._queue.full():
                        try:
                            self._queue.get_nowait()
                        except queue.Empty:
                            pass
                    self._queue.put_nowait(frame)
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("HLS frame feed error: %s", exc)

    async def _idle_monitor_task(self) -> None:
        """Automatically stop FFmpeg process after idle timeout."""
        try:
            while self._running:
                await asyncio.sleep(5.0)
                if self.idle_timeout > 0 and time.monotonic() - self._last_access > self.idle_timeout:
                    logger.info("HLS stream idle for %.1fs. Stopping FFmpeg transcoder...", self.idle_timeout)
                    await self.stop()
                    break
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Stop FFmpeg process and clean up temporary files."""
        self._running = False

        if self._feed_task and not self._feed_task.done():
            self._feed_task.cancel()
            with asyncio.suppress(asyncio.CancelledError):
                await self._feed_task
        self._feed_task = None

        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            with asyncio.suppress(asyncio.CancelledError):
                await self._idle_task
        self._idle_task = None

        # Signal writer thread to exit
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass

        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=1.0)
        self._writer_thread = None

        proc = self._process
        self._process = None

        if proc:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            try:
                proc.terminate()
                await asyncio.to_thread(proc.wait, timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        self._cleanup_files()
        logger.info("HLS transcoder stopped and cleaned up.")

    def _cleanup_files(self) -> None:
        """Remove leftover .m3u8 and .ts files."""
        if not self.output_dir.exists():
            return
        for p in self.output_dir.glob("*.ts"):
            try:
                p.unlink()
            except OSError:
                pass
        for p in self.output_dir.glob("*.m3u8"):
            try:
                p.unlink()
            except OSError:
                pass

    async def get_playlist(self, timeout_seconds: float = 6.0) -> Optional[str]:
        """Return the current .m3u8 playlist, waiting for initial segments if starting up."""
        self.touch()
        if not self.is_running:
            ok = await self.start()
            if not ok:
                return None

        playlist_path = self.output_dir / "stream.m3u8"
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout_seconds:
            if playlist_path.exists():
                try:
                    content = playlist_path.read_text(encoding="utf-8")
                    if ".ts" in content:
                        return content
                except Exception:
                    pass
            await asyncio.sleep(0.2)

        if playlist_path.exists():
            try:
                return playlist_path.read_text(encoding="utf-8")
            except Exception:
                pass
        return None

    def get_segment(self, segment_name: str) -> Optional[bytes]:
        """Return bytes of an MPEG-TS segment file."""
        self.touch()
        if not _SEGMENT_RE.match(segment_name):
            return None
        segment_path = self.output_dir / segment_name
        if segment_path.exists():
            try:
                return segment_path.read_bytes()
            except Exception:
                return None
        return None
