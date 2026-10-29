"""12-Hour Circular Disk-Backed Timelapse Recorder for Momcozy BM04.

Records a 1:60 timelapse spanning up to 12 hours (12 minutes of video)
using a fixed circular slot ring buffer on disk with zero transcoding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .stream import VideoStreamer

logger = logging.getLogger(__name__)


def format_duration(seconds: float) -> str:
    """Format duration in seconds to a human-readable string (e.g., '4h 12m' or '25m')."""
    total_sec = max(0, int(seconds))
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {total_sec % 60:02d}s"


def format_video_duration(seconds: float) -> str:
    """Format video playback seconds to 'M:SS'."""
    total_sec = max(0, int(seconds))
    mins = total_sec // 60
    secs = total_sec % 60
    return f"{mins}:{secs:02d}"


def format_bytes(bytes_count: int) -> str:
    """Format bytes into MB/GB representation."""
    if bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f} KB"
    if bytes_count < 1024 * 1024 * 1024:
        return f"{bytes_count / (1024 * 1024):.1f} MB"
    return f"{bytes_count / (1024 * 1024 * 1024):.2f} GB"


class TimelapseRecorder:
    """Fixed-slot circular ring buffer timelapse recorder.

    Records camera HEVC keyframes directly to disk without transcoding (0% CPU).
    The ring buffer holds a fixed number of slots (e.g. 72 slots of 10 minutes = 12 hours).
    Older slots are directly overwritten at slot boundaries, avoiding any periodic cleanup tasks.
    """

    def __init__(
        self,
        streamer: VideoStreamer,
        storage_dir: Optional[Path | str] = None,
        total_slots: int = 72,
        slot_duration_sec: float = 600.0,
        sample_interval_sec: float = 2.0,
        fps: int = 15,
    ) -> None:
        self.streamer = streamer
        self.storage_dir = Path(storage_dir or os.getenv("MOMCOZY_TIMELAPSE_DIR", "timelapse"))
        self.total_slots = int(total_slots)
        self.slot_duration_sec = float(slot_duration_sec)
        self.sample_interval_sec = float(sample_interval_sec)
        self.fps = int(fps)

        self._running = False
        self._record_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # Slot state
        self._current_slot = 0
        self._frames_in_slot = 0
        self._bytes_in_slot = 0
        self._slot_start_monotonic = 0.0
        self._slot_start_wall = 0.0
        self._last_sample_monotonic = 0.0
        self._active_file: Optional[Any] = None

        # Metadata tracking: slot_idx -> {"mtime": float, "frames": int, "bytes": int}
        self._slots_meta: Dict[str, Dict[str, Any]] = {}

        # Cache for exported MP4
        self._cached_mp4_path: Optional[Path] = None
        self._cached_mp4_mtime: float = 0.0
        self._cached_mp4_codec: str = ""

        self._load_meta()

    @property
    def is_recording(self) -> bool:
        return self._running and self._record_task is not None and not self._record_task.done()

    @property
    def meta_file(self) -> Path:
        return self.storage_dir / "meta.json"

    def slot_path(self, slot_idx: int) -> Path:
        return self.storage_dir / f"slot_{slot_idx:02d}.hevc"

    def _load_meta(self) -> None:
        """Load metadata from meta.json or reconstruct from existing slot files."""
        if not self.storage_dir.exists():
            return

        meta_loaded = False
        if self.meta_file.exists():
            try:
                data = json.loads(self.meta_file.read_text(encoding="utf-8"))
                self._current_slot = int(data.get("current_slot", 0)) % self.total_slots
                self._slots_meta = data.get("slots", {})
                meta_loaded = True
            except Exception as exc:
                logger.warning("Error reading timelapse meta.json: %s", exc)

        if not meta_loaded:
            # Reconstruct from slot files mtimes
            self._slots_meta = {}
            newest_slot = 0
            newest_mtime = 0.0
            for i in range(self.total_slots):
                p = self.slot_path(i)
                if p.exists() and p.stat().st_size > 0:
                    st = p.stat()
                    # Approximate frame count (30KB per frame average)
                    approx_frames = max(1, st.st_size // 35000)
                    self._slots_meta[str(i)] = {
                        "mtime": st.st_mtime,
                        "frames": approx_frames,
                        "bytes": st.st_size,
                    }
                    if st.st_mtime > newest_mtime:
                        newest_mtime = st.st_mtime
                        newest_slot = i

            if newest_mtime > 0:
                self._current_slot = newest_slot

    def _save_meta(self) -> None:
        """Persist metadata to meta.json."""
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            meta = {
                "total_slots": self.total_slots,
                "slot_duration_sec": self.slot_duration_sec,
                "sample_interval_sec": self.sample_interval_sec,
                "fps": self.fps,
                "current_slot": self._current_slot,
                "slots": self._slots_meta,
            }
            tmp_meta = self.storage_dir / "meta.json.tmp"
            tmp_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            tmp_meta.replace(self.meta_file)
        except Exception as exc:
            logger.debug("Failed saving timelapse meta.json: %s", exc)

    async def start(self) -> None:
        """Start recording the circular timelapse."""
        async with self._lock:
            if self._running:
                return
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            self._running = True

            # If streamer is not running, start it
            if not self.streamer.is_running:
                logger.info("Timelapse starting camera stream...")
                await self.streamer.start()

            self._record_task = asyncio.create_task(self._record_loop())
            logger.info(
                "Timelapse recording started (storage=%s, slots=%d, slot_duration=%.0fs, rate=1:60)",
                self.storage_dir,
                self.total_slots,
                self.slot_duration_sec,
            )

    async def stop(self) -> None:
        """Stop recording the timelapse."""
        async with self._lock:
            if not self._running:
                return
            self._running = False
            if self._record_task:
                self._record_task.cancel()
                try:
                    await self._record_task
                except asyncio.CancelledError:
                    pass
                self._record_task = None

            self._close_active_slot()
            self._save_meta()
            logger.info("Timelapse recording stopped.")

    def _close_active_slot(self) -> None:
        """Close active file handle and update slot metadata."""
        if self._active_file:
            try:
                self._active_file.flush()
                self._active_file.close()
            except Exception:
                pass
            self._active_file = None

        if self._frames_in_slot > 0:
            p = self.slot_path(self._current_slot)
            sz = p.stat().st_size if p.exists() else self._bytes_in_slot
            elapsed = 0.0
            if self._slot_start_monotonic > 0:
                elapsed = min(self.slot_duration_sec, time.monotonic() - self._slot_start_monotonic)
            self._slots_meta[str(self._current_slot)] = {
                "mtime": time.time(),
                "duration": round(elapsed, 1),
                "frames": self._frames_in_slot,
                "bytes": sz,
            }

    def _rotate_slot(self) -> None:
        """Advance to next circular slot, truncating any existing old slot file."""
        self._close_active_slot()
        self._save_meta()

        self._current_slot = (self._current_slot + 1) % self.total_slots
        self._frames_in_slot = 0
        self._bytes_in_slot = 0
        self._slot_start_monotonic = time.monotonic()
        self._slot_start_wall = time.time()

        # Direct overwrite: open in "wb" mode to truncate previous 12-hour-old file
        slot_p = self.slot_path(self._current_slot)
        slot_p.parent.mkdir(parents=True, exist_ok=True)
        self._active_file = open(slot_p, "wb")
        logger.info(
            "Timelapse rotated to slot %02d/%02d (%s)",
            self._current_slot,
            self.total_slots,
            slot_p.name,
        )

    async def _record_loop(self) -> None:
        """Continuous background loop collecting HEVC keyframes."""
        backoff = 1.0

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        # Open initial slot
        slot_p = self.slot_path(self._current_slot)
        slot_p.parent.mkdir(parents=True, exist_ok=True)
        # If continuing a recently modified slot, append, else truncate
        mode = "ab" if slot_p.exists() and str(self._current_slot) in self._slots_meta else "wb"
        self._active_file = open(slot_p, mode)
        self._slot_start_monotonic = time.monotonic()
        self._slot_start_wall = time.time()
        self._last_sample_monotonic = 0.0

        target_frames_per_slot = int(self.slot_duration_sec / self.sample_interval_sec)

        while self._running:
            try:
                async for chunk, is_key in self.streamer.subscribe_hevc():
                    if not self._running:
                        break

                    # Only capture complete keyframes (VPS/SPS/PPS + IDR)
                    if not is_key:
                        continue

                    now = time.monotonic()
                    # Sample rate control: ensure at least sample_interval_sec between frames
                    if (now - self._last_sample_monotonic) < (self.sample_interval_sec - 0.1):
                        continue

                    self._last_sample_monotonic = now

                    # Write frame directly to active slot file
                    if self._active_file is None:
                        self._active_file = open(self.slot_path(self._current_slot), "ab")

                    self._active_file.write(chunk)
                    self._frames_in_slot += 1
                    self._bytes_in_slot += len(chunk)

                    # Periodically flush every 15 frames
                    if self._frames_in_slot % 15 == 0:
                        self._active_file.flush()

                    # Check slot rotation condition
                    slot_elapsed = now - self._slot_start_monotonic
                    if self._frames_in_slot >= target_frames_per_slot or slot_elapsed >= self.slot_duration_sec:
                        self._rotate_slot()

                if not self._running:
                    break
                await asyncio.sleep(0.5)
                backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Timelapse HEVC subscriber exception: %s. Retrying in %.1fs...", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(10.0, backoff * 1.5)

        self._close_active_slot()

    def get_ordered_slots(self) -> List[Tuple[int, Path, Dict[str, Any]]]:
        """Return all non-empty slot files in chronological order (oldest to newest)."""
        valid_slots = []
        for i in range(self.total_slots):
            p = self.slot_path(i)
            if p.exists():
                try:
                    sz = p.stat().st_size
                    if sz > 0:
                        meta = self._slots_meta.get(str(i), {})
                        mtime = meta.get("mtime") or p.stat().st_mtime
                        valid_slots.append((i, p, mtime, meta))
                except OSError:
                    pass

        # Sort strictly by mtime: oldest first, newest last
        valid_slots.sort(key=lambda item: item[2])
        return [(idx, path, meta) for idx, path, _, meta in valid_slots]

    def get_status(self) -> Dict[str, Any]:
        """Return current status of the timelapse ring buffer."""
        ordered = self.get_ordered_slots()
        total_frames = 0
        total_bytes = 0
        total_real_time_sec = 0.0

        for idx, path, meta in ordered:
            if idx == self._current_slot and self.is_recording:
                active_elapsed = max(0.0, time.monotonic() - self._slot_start_monotonic)
                total_real_time_sec += min(self.slot_duration_sec, active_elapsed)
                total_frames += self._frames_in_slot
                total_bytes += self._bytes_in_slot
            else:
                dur = meta.get("duration")
                if dur is None:
                    frames = meta.get("frames", 0)
                    dur = min(self.slot_duration_sec, frames * 4.0 if frames > 0 else 0.0)
                total_real_time_sec += float(dur)
                total_frames += meta.get("frames", 0)
                total_bytes += meta.get("bytes", path.stat().st_size if path.exists() else 0)

        max_duration = self.total_slots * self.slot_duration_sec
        total_real_time_sec = min(total_real_time_sec, max_duration)
        video_sec = total_real_time_sec / 60.0

        return {
            "is_recording": self.is_recording,
            "total_slots": self.total_slots,
            "filled_slots": len(ordered),
            "current_slot": self._current_slot,
            "total_frames": total_frames,
            "buffered_seconds": round(total_real_time_sec, 1),
            "buffered_formatted": format_duration(total_real_time_sec),
            "video_duration_seconds": round(video_sec, 1),
            "video_duration_formatted": format_video_duration(video_sec),
            "speed_ratio": "1:60",
            "disk_bytes": total_bytes,
            "disk_formatted": format_bytes(total_bytes),
            "has_content": total_frames > 0,
        }

    async def export_mp4(self, codec: str = "hevc") -> Optional[Path]:
        """Concatenate ordered slots and remux to MP4 using stream-copy (zero transcoding).

        If codec=='h264' is explicitly requested, transcodes using libx264 as fallback.
        """
        ordered = self.get_ordered_slots()
        if not ordered:
            return None

        # Flush active slot so latest frames are on disk
        if self._active_file:
            try:
                self._active_file.flush()
            except Exception:
                pass

        # Determine latest mtime among active slots
        latest_mtime = max(p.stat().st_mtime for _, p, _ in ordered if p.exists())

        out_name = f"timelapse_cache_{codec}.mp4"
        out_path = self.storage_dir / out_name

        # Check if cache is fresh
        if (
            self._cached_mp4_path == out_path
            and out_path.exists()
            and self._cached_mp4_mtime >= latest_mtime
            and self._cached_mp4_codec == codec
        ):
            return out_path

        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, self._generate_mp4, ordered, out_path, codec)
        if success and out_path.exists():
            self._cached_mp4_path = out_path
            self._cached_mp4_mtime = time.time()
            self._cached_mp4_codec = codec
            return out_path

        return None

    def _generate_mp4(
        self,
        ordered_slots: List[Tuple[int, Path, Dict[str, Any]]],
        output_file: Path,
        codec: str,
    ) -> bool:
        """Run FFmpeg to concatenate slots into an MP4 container."""
        if shutil.which("ffmpeg") is None:
            logger.error("FFmpeg not found. Cannot export timelapse MP4.")
            return False

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f_list:
            list_path = f_list.name
            try:
                for _, path, _ in ordered_slots:
                    if path.exists() and path.stat().st_size > 0:
                        # Write safe FFmpeg concat line
                        clean_path = str(path.resolve()).replace("'", "'\\''")
                        f_list.write(f"file '{clean_path}'\n")
                f_list.flush()

                tmp_out = output_file.with_suffix(".tmp.mp4")

                cmd = [
                    "ffmpeg",
                    "-y",
                    "-r",
                    str(self.fps),
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    list_path,
                ]

                if codec.lower() == "h264":
                    cmd.extend([
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-tune",
                        "zerolatency",
                        "-pix_fmt",
                        "yuv420p",
                    ])
                else:
                    # Stream copy HEVC: 0% CPU, ultrafast remux
                    cmd.extend([
                        "-c:v",
                        "copy",
                        "-tag:v",
                        "hvc1",
                    ])

                cmd.extend([
                    "-movflags",
                    "+faststart",
                    str(tmp_out),
                ])

                logger.info("Generating timelapse MP4 (%s) with cmd: %s", codec, " ".join(cmd[:12]))
                proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                if proc.returncode != 0:
                    logger.error("FFmpeg timelapse export failed: %s", proc.stderr[-300:] if proc.stderr else "unknown error")
                    if tmp_out.exists():
                        tmp_out.unlink()
                    return False

                tmp_out.replace(output_file)
                logger.info(
                    "Timelapse MP4 created successfully: %s (%s, %d bytes)",
                    output_file.name,
                    codec,
                    output_file.stat().st_size,
                )
                return True
            except Exception as exc:
                logger.error("Error building timelapse MP4: %s", exc)
                return False
            finally:
                if os.path.exists(list_path):
                    try:
                        os.unlink(list_path)
                    except Exception:
                        pass
