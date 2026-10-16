"""HLS (HTTP Live Streaming) transcoder for Momcozy BM04.

Transcodes live camera frames into HLS MPEG-TS segments and playlist (.m3u8)
using FFmpeg. Supports Google Chromecast, Apple TV, iOS Safari, and smart displays.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
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
    """Live HLS transcoder feeding raw HEVC chunks from VideoStreamer into an FFmpeg HLS pipeline.

    Supports Intel VA-API hardware acceleration (Intel Quick Sync Video) on Intel GPUs
    (e.g., Haswell Gen 7.5 Pentium G3220 using i965 driver) with graceful fallback to
    software libx264 encoding.
    """

    def __init__(
        self,
        streamer: VideoStreamer,
        output_dir: Optional[Path | str] = None,
        hls_time: int = 2,
        hls_list_size: int = 5,
        idle_timeout: float = 60.0,
        hwaccel: Optional[str] = None,
        vaapi_device: Optional[str] = None,
    ) -> None:
        self.streamer = streamer
        self.output_dir = Path(output_dir or "/tmp/momcozy_hls")
        self.hls_time = hls_time
        self.hls_list_size = hls_list_size
        self.idle_timeout = idle_timeout
        self.hwaccel = (hwaccel or os.getenv("MOMCOZY_HWACCEL", "auto")).lower()
        self.vaapi_device = vaapi_device or os.getenv("MOMCOZY_VAAPI_DEVICE")

        self._running = False
        self._process: Optional[subprocess.Popen] = None
        self._queue: queue.Queue[Optional[bytes]] = queue.Queue(maxsize=30)
        self._writer_thread: Optional[threading.Thread] = None
        self._feed_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._last_access = time.monotonic()
        self._lock = asyncio.Lock()
        self._using_vaapi = False

    @property
    def is_available(self) -> bool:
        """Return True if ffmpeg binary is present on the system."""
        return shutil.which("ffmpeg") is not None

    @property
    def is_running(self) -> bool:
        """Return True if FFmpeg transcoder is currently active and healthy."""
        return self._running and self._process is not None and self._process.poll() is None

    @property
    def using_vaapi(self) -> bool:
        """Return True if the active FFmpeg pipeline is using VA-API hardware acceleration."""
        return self._using_vaapi

    def touch(self) -> None:
        """Update last access time to reset idle timeout."""
        self._last_access = time.monotonic()

    def _detect_vaapi_device(self) -> Optional[str]:
        """Detect available VA-API device path, or None if unavailable or disabled."""
        if self.hwaccel in ("none", "cpu", "disabled", "false", "0"):
            return None

        # Check explicit device configuration
        if self.vaapi_device:
            if Path(self.vaapi_device).exists() or self.hwaccel in ("vaapi", "force"):
                return self.vaapi_device

        # Standard Linux DRM render nodes
        for dev in ("/dev/dri/renderD128", "/dev/dri/card0"):
            if Path(dev).exists():
                return dev

        if self.hwaccel in ("vaapi", "force"):
            return "/dev/dri/renderD128"

        return None

    def _build_cmd(
        self,
        playlist_path: Path,
        segment_pattern: str,
        use_vaapi: bool = False,
        vaapi_device: Optional[str] = None,
    ) -> list[str]:
        """Build FFmpeg command line arguments for either VA-API or software encoding."""
        cmd = ["ffmpeg", "-y"]
        if use_vaapi and vaapi_device:
            cmd.extend([
                "-init_hw_device", f"vaapi=hw:{vaapi_device}",
                "-filter_hw_device", "hw",
            ])

        cmd.extend([
            "-f", "hevc",
            "-framerate", "15",
            "-i", "pipe:0",
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=mono:sample_rate=8000",
            "-shortest",
        ])

        if use_vaapi and vaapi_device:
            # Software HEVC decode -> upload to VA-API surface -> hardware H.264 encode (Intel QSV)
            cmd.extend([
                "-vf", "format=nv12,hwupload",
                "-c:v", "h264_vaapi",
                "-g", "30",
                "-bf", "0",
            ])
        else:
            # Software libx264 encode
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-pix_fmt", "yuv420p",
                "-g", "30",
                "-sc_threshold", "0",
            ])

        cmd.extend([
            "-c:a", "aac",
            "-b:a", "32k",
            "-f", "hls",
            "-hls_time", str(self.hls_time),
            "-hls_list_size", str(self.hls_list_size),
            "-hls_flags", "delete_segments+split_by_time+omit_endlist",
            "-hls_segment_filename", segment_pattern,
            str(playlist_path),
        ])
        return cmd

    async def _start_ffmpeg_process(self, playlist_path: Path, segment_pattern: str) -> bool:
        """Spawn FFmpeg process, attempting VA-API hardware acceleration first with software fallback."""
        vaapi_dev = self._detect_vaapi_device()
        env = os.environ.copy()
        if "LIBVA_DRIVER_NAME" not in env:
            # Intel Pentium G3220 (Haswell Gen 7.5) uses i965 driver
            env["LIBVA_DRIVER_NAME"] = "i965"

        self._using_vaapi = False
        self._process = None

        if vaapi_dev:
            logger.info("Starting FFmpeg HLS pipeline with Intel VA-API hardware acceleration (%s)...", vaapi_dev)
            cmd = self._build_cmd(playlist_path, segment_pattern, use_vaapi=True, vaapi_device=vaapi_dev)
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                await asyncio.sleep(0.08)
                if proc.poll() is not None:
                    err_msg = ""
                    if proc.stderr:
                        try:
                            err_msg = proc.stderr.read().decode("utf-8", errors="replace").strip()
                        except Exception:
                            pass
                    logger.warning(
                        "VA-API hardware acceleration failed to start (exit %s): %s. Falling back to software encoding.",
                        proc.returncode,
                        err_msg[-300:] if err_msg else "unknown error",
                    )
                else:
                    self._process = proc
                    self._using_vaapi = True
            except Exception as exc:
                logger.warning("Failed to spawn FFmpeg with VA-API: %s. Falling back to software encoding.", exc)

        if self._process is None or self._process.poll() is not None:
            logger.info("Starting FFmpeg HLS pipeline with software encoding (libx264)...")
            cmd = self._build_cmd(playlist_path, segment_pattern, use_vaapi=False)
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                self._using_vaapi = False
            except Exception as exc:
                logger.error("Failed to spawn FFmpeg process: %s", exc)
                return False

        return True

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

            ok = await self._start_ffmpeg_process(playlist_path, segment_pattern)
            if not ok:
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
        """Worker thread pushing raw HEVC chunks into FFmpeg stdin pipe."""
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
        """Read raw HEVC chunks from VideoStreamer and enqueue for FFmpeg."""
        try:
            async for chunk, is_key in self.streamer.subscribe_hevc():
                if not self._running:
                    break
                try:
                    if self._queue.full():
                        try:
                            self._queue.get_nowait()
                        except queue.Empty:
                            pass
                    self._queue.put_nowait(chunk)
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
        self._using_vaapi = False

        if self._feed_task and not self._feed_task.done():
            self._feed_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._feed_task
        self._feed_task = None

        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
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
