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
        speed_ratio: int = 60,
    ) -> None:
        self.streamer = streamer
        self.storage_dir = Path(storage_dir or os.getenv("MOMCOZY_TIMELAPSE_DIR", "timelapse"))
        self.total_slots = int(total_slots)
        self.slot_duration_sec = float(slot_duration_sec)
        self.sample_interval_sec = float(sample_interval_sec)
        self.fps = int(fps)
        self.speed_ratio = int(speed_ratio) if int(speed_ratio) in (15, 30, 60, 90) else 60

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

        # Recorded motion/detection events list: [{"time": float, "type": str, "label": str}]
        self._events: List[Dict[str, Any]] = []

        # Night vision sync automation state
        self.sync_night_vision: bool = False
        self.last_session_type: str = "manual"  # "manual" | "auto"
        self.current_session_type: Optional[str] = None

        # Lease on the camera stream, held while recording
        self._stream_lease: Optional[Any] = None

        # Cache for exported MP4: cache_key -> {"path": Path, "created_at": float, "mtime": float}
        self._exports_cache: Dict[str, Dict[str, Any]] = {}
        # In-flight export tasks deduplication: cache_key -> asyncio.Task
        self._in_flight_exports: Dict[str, asyncio.Task] = {}

        self._load_meta()

    def _detect_vaapi_device(self) -> Optional[str]:
        """Detect available VA-API device path for hardware acceleration."""
        hwaccel = os.getenv("MOMCOZY_HWACCEL", "auto").lower()
        if hwaccel in ("none", "cpu", "disabled", "false", "0"):
            return None

        vaapi_dev = os.getenv("MOMCOZY_VAAPI_DEVICE")
        if vaapi_dev:
            if Path(vaapi_dev).exists() or hwaccel in ("vaapi", "force"):
                return vaapi_dev

        for dev in ("/dev/dri/renderD128", "/dev/dri/card0"):
            if Path(dev).exists():
                return dev

        if hwaccel in ("vaapi", "force"):
            return "/dev/dri/renderD128"

        return None

    def set_speed_ratio(self, ratio: int | str) -> int:
        """Set the speed ratio (15, 30, 60, 90)."""
        if isinstance(ratio, str):
            clean = ratio.strip().replace("1:", "")
            r = int(clean)
        else:
            r = int(ratio)
        if r not in (15, 30, 60, 90):
            raise ValueError(f"Invalid speed ratio: {ratio}. Options are 1:15, 1:30, 1:60, 1:90.")
        if self.speed_ratio != r:
            self.speed_ratio = r
            self._exports_cache.clear()
            self._save_meta()
        return self.speed_ratio

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
                self._events = data.get("events", [])
                self.sync_night_vision = bool(data.get("sync_night_vision", False))
                self.last_session_type = str(data.get("last_session_type", "manual"))
                if "speed_ratio" in data:
                    try:
                        r = int(data["speed_ratio"])
                        if r in (15, 30, 60, 90):
                            self.speed_ratio = r
                    except Exception:
                        pass
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
                "speed_ratio": self.speed_ratio,
                "current_slot": self._current_slot,
                "slots": self._slots_meta,
                "events": self._events,
                "sync_night_vision": self.sync_night_vision,
                "last_session_type": self.last_session_type,
            }
            tmp_meta = self.storage_dir / "meta.json.tmp"
            tmp_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            tmp_meta.replace(self.meta_file)
        except Exception as exc:
            logger.debug("Failed saving timelapse meta.json: %s", exc)

    def set_sync_night_vision(self, enabled: bool) -> bool:
        """Enable or disable automatic timelapse synchronization with Night Vision."""
        self.sync_night_vision = bool(enabled)
        self._save_meta()
        logger.info("Timelapse Night Vision sync set to: %s", self.sync_night_vision)
        return self.sync_night_vision

    def clear(self) -> None:
        """Clear all previous timelapse slot recordings and metadata."""
        if self._active_file:
            try:
                self._active_file.close()
            except Exception:
                pass
            self._active_file = None

        self._current_slot = 0
        self._frames_in_slot = 0
        self._bytes_in_slot = 0
        self._slot_start_monotonic = time.monotonic()
        self._slot_start_wall = time.time()
        self._slots_meta = {}
        self._events = []
        self._exports_cache.clear()
        for task in list(self._in_flight_exports.values()):
            if not task.done():
                task.cancel()
        self._in_flight_exports.clear()

        if self.storage_dir.exists():
            for p in self.storage_dir.glob("slot_*.hevc"):
                try:
                    p.unlink()
                except OSError:
                    pass
            for p in self.storage_dir.glob("timelapse_cache_*.mp4*"):
                try:
                    p.unlink()
                except OSError:
                    pass
        self._save_meta()

    def record_event(
        self,
        event_type: str = "motion",
        label: str = "Motion Alert",
        timestamp: Optional[float] = None,
    ) -> bool:
        """Record a motion or detection event with a timestamp.

        Debounces events within 15 seconds to avoid duplicates.
        Prunes events older than the circular buffer's retention window.
        """
        ts = float(timestamp if timestamp is not None else time.time())
        # Debounce: if an event of the same type occurred within the last 15s, skip
        for ev in reversed(self._events):
            if ev.get("type") == event_type and abs(float(ev.get("time", 0.0)) - ts) < 15.0:
                return False

        self._events.append({
            "time": round(ts, 1),
            "type": event_type,
            "label": label,
        })
        # Keep events within the circular ring buffer's historical retention window
        max_history_sec = self.total_slots * self.slot_duration_sec
        latest_ts = max([float(ev.get("time", 0.0)) for ev in self._events] + [ts])
        cutoff = latest_ts - max_history_sec
        self._events = [ev for ev in self._events if float(ev.get("time", 0.0)) >= cutoff]
        self._save_meta()
        logger.info("Recorded timelapse %s event: '%s' at timestamp %.1f", event_type, label, ts)
        return True

    async def start(self, fresh: bool = False, session_type: str = "manual") -> None:
        """Start recording the circular timelapse.

        If fresh is True, starts a brand new recording session, wiping any old slots and cache.
        If fresh is False (default), resumes/appends into the existing circular buffer.
        """
        async with self._lock:
            if self._running:
                return
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            if fresh:
                self.clear()
            self._running = True
            self.current_session_type = session_type
            self.last_session_type = session_type
            self._save_meta()

            # Hold the camera stream open for the whole recording.
            self._stream_lease = self.streamer.acquire()
            if not self.streamer.is_running:
                logger.info("Timelapse starting camera stream...")
                await self.streamer.start()

            self._record_task = asyncio.create_task(self._record_loop())
            logger.info(
                "Timelapse recording started (storage=%s, slots=%d, slot_duration=%.0fs, rate=1:%d, fresh=%s, session_type=%s)",
                self.storage_dir,
                self.total_slots,
                self.slot_duration_sec,
                self.speed_ratio,
                fresh,
                session_type,
            )

    async def stop(self, linger: Optional[float] = None) -> None:
        """Stop recording the timelapse.

        `linger` is how long the camera stream may stay up afterwards if nothing else uses
        it (default: the streamer's idle timeout; 0 disconnects immediately).
        """
        async with self._lock:
            if not self._running:
                return
            self._running = False
            if self.current_session_type:
                self.last_session_type = self.current_session_type
                self.current_session_type = None
            if self._record_task:
                self._record_task.cancel()
                try:
                    await self._record_task
                except asyncio.CancelledError:
                    pass
                self._record_task = None

            self._close_active_slot()
            self._save_meta()
            if self._stream_lease is not None:
                self.streamer.release(self._stream_lease, linger)
                self._stream_lease = None
            logger.info("Timelapse recording stopped (session=%s).", self.last_session_type)

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
        target_frames_per_slot = int(self.slot_duration_sec / self.sample_interval_sec)

        # Open initial slot
        slot_p = self.slot_path(self._current_slot)
        slot_p.parent.mkdir(parents=True, exist_ok=True)
        # If continuing a recently modified slot, append, else truncate
        mode = "ab" if slot_p.exists() and str(self._current_slot) in self._slots_meta else "wb"
        if mode == "ab":
            meta = self._slots_meta.get(str(self._current_slot), {})
            self._frames_in_slot = meta.get("frames", 0)
            self._bytes_in_slot = meta.get("bytes", slot_p.stat().st_size if slot_p.exists() else 0)
            prev_dur = float(meta.get("duration", 0.0))
            self._slot_start_monotonic = time.monotonic() - prev_dur
        else:
            self._frames_in_slot = 0
            self._bytes_in_slot = 0
            self._slot_start_monotonic = time.monotonic()

        self._active_file = open(slot_p, mode)
        self._slot_start_wall = time.time()
        self._last_sample_monotonic = 0.0

        if self._frames_in_slot >= target_frames_per_slot:
            self._rotate_slot()

        while self._running:
            try:
                if not self.streamer.is_running:
                    logger.warning("Camera stream stopped during timelapse recording. Restarting it...")
                    await self.streamer.start()
                # Our own lease keeps the stream up, so the subscription need not hold one.
                sub_hevc = getattr(self.streamer, "subscribe_hevc", None)
                if sub_hevc is None:
                    break
                try:
                    stream_iter = sub_hevc(leased=False)
                except TypeError:
                    stream_iter = sub_hevc()
                async for chunk, is_key in stream_iter:
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
        video_sec = total_real_time_sec / self.speed_ratio if self.speed_ratio > 0 else 0.0

        # Calculate event timeline positions relative to the start of recorded video
        events_list: List[Dict[str, Any]] = []
        if ordered and total_real_time_sec > 0:
            first_slot_meta = ordered[0][2]
            first_slot_path = ordered[0][1]
            first_slot_mtime = float(first_slot_meta.get("mtime") or (first_slot_path.stat().st_mtime if first_slot_path.exists() else time.time()))
            first_slot_dur = float(first_slot_meta.get("duration") or min(self.slot_duration_sec, total_real_time_sec))
            start_wall_time = first_slot_mtime - first_slot_dur

            for ev in self._events:
                ev_time = float(ev.get("time", 0.0))
                elapsed_sec = max(0.0, ev_time - start_wall_time)
                if elapsed_sec <= total_real_time_sec + 30.0:  # within current buffer window
                    percent = min(100.0, max(0.0, round((elapsed_sec / total_real_time_sec) * 100.0, 1)))
                    ev_video_sec = round(elapsed_sec / self.speed_ratio, 1) if self.speed_ratio > 0 else 0.0
                    events_list.append({
                        "time": ev_time,
                        "time_str": time.strftime("%I:%M:%S %p", time.localtime(ev_time)),
                        "type": ev.get("type", "motion"),
                        "label": ev.get("label", "Motion Alert"),
                        "elapsed_sec": round(elapsed_sec, 1),
                        "video_sec": ev_video_sec,
                        "video_formatted": format_video_duration(ev_video_sec),
                        "percent": percent,
                    })
            events_list.sort(key=lambda x: x["elapsed_sec"])

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
            "speed_ratio": f"1:{self.speed_ratio}",
            "speed_ratio_val": self.speed_ratio,
            "speed_ratio_options": ["1:15", "1:30", "1:60", "1:90"],
            "disk_bytes": total_bytes,
            "disk_formatted": format_bytes(total_bytes),
            "has_content": total_frames > 0,
            "events": events_list,
            "motion_events_count": len(events_list),
            "sync_night_vision": self.sync_night_vision,
            "last_session_type": self.last_session_type,
            "current_session_type": self.current_session_type,
        }

    async def export_mp4(self, codec: str = "hevc", ratio: Optional[int] = None) -> Optional[Path]:
        """Concatenate ordered slots and remux to MP4 using stream-copy (zero transcoding).

        If codec=='h264' is explicitly requested, attempts Intel VA-API hardware acceleration
        with fallback to libx264. Multiple concurrent requests for the same export share the
        same in-flight task to prevent thundering-herd duplicate transcodes.
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

        effective_ratio = ratio if (isinstance(ratio, int) and ratio in (15, 30, 60, 90)) else self.speed_ratio
        norm_codec = "h264" if str(codec).lower().strip() == "h264" else "hevc"
        cache_key = f"{norm_codec}_r{effective_ratio}"

        # Determine latest mtime among active slots
        latest_mtime = max(p.stat().st_mtime for _, p, _ in ordered if p.exists())

        # Check if cache is fresh
        cache_entry = self._exports_cache.get(cache_key)
        if cache_entry and cache_entry.get("path") and cache_entry["path"].exists():
            out_path = cache_entry["path"]
            created_at = float(cache_entry.get("created_at", 0.0))
            if not self.is_recording:
                # Slot recordings are static; cache is valid if built after or at latest slot mtime
                if created_at >= latest_mtime:
                    return out_path
            else:
                # While recording is active, slot files are written every 2-4 seconds.
                # Use TTL (20s for HEVC, 60s for H.264) to prevent re-encoding on every keyframe
                ttl = 20.0 if norm_codec == "hevc" else 60.0
                if (time.time() - created_at) < ttl:
                    return out_path

        # If an export for this cache_key is already in flight, await it to avoid thundering-herd FFmpeg processes
        if cache_key in self._in_flight_exports:
            task = self._in_flight_exports[cache_key]
            if not task.done():
                logger.info("Timelapse export for %s is already in flight, awaiting existing task...", cache_key)
                try:
                    return await asyncio.shield(task)
                except Exception as exc:
                    logger.warning("Existing timelapse export failed: %s", exc)
                    return None

        # Compute dynamic fps so video duration matches real_time / effective_ratio
        st = self.get_status()
        real_time_sec = max(1.0, float(st.get("buffered_seconds", 0.0)))
        total_frames = max(1, int(st.get("total_frames", 1)))
        target_video_sec = max(0.1, real_time_sec / effective_ratio)
        export_fps = max(1.0, round(total_frames / target_video_sec, 2))

        out_name = f"timelapse_cache_{norm_codec}_r{effective_ratio}.mp4"
        out_path = self.storage_dir / out_name

        async def _do_export() -> Optional[Path]:
            try:
                loop = asyncio.get_running_loop()
                success = await loop.run_in_executor(
                    None,
                    self._generate_mp4,
                    ordered,
                    out_path,
                    norm_codec,
                    export_fps,
                )
                if success and out_path.exists():
                    self._exports_cache[cache_key] = {
                        "path": out_path,
                        "created_at": time.time(),
                        "mtime": latest_mtime,
                    }
                    return out_path
                return None
            finally:
                self._in_flight_exports.pop(cache_key, None)

        task = asyncio.create_task(_do_export())
        self._in_flight_exports[cache_key] = task
        return await task

    def _generate_mp4(
        self,
        ordered_slots: List[Tuple[int, Path, Dict[str, Any]]],
        output_file: Path,
        codec: str,
        fps: float = 15.0,
    ) -> bool:
        """Run FFmpeg to concatenate slots into an MP4 container.

        Uses native stream-copy for HEVC (0% CPU, instant).
        For H.264, uses Intel VA-API hardware acceleration if available,
        falling back cleanly to multi-threaded libx264.
        """
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
                env = os.environ.copy()
                if "LIBVA_DRIVER_NAME" not in env:
                    # Intel Haswell Gen 7.5 uses i965 driver
                    env["LIBVA_DRIVER_NAME"] = "i965"

                if codec.lower() == "h264":
                    vaapi_dev = self._detect_vaapi_device()
                    if vaapi_dev:
                        logger.info("Attempting VA-API hardware accelerated H.264 export via %s...", vaapi_dev)
                        va_cmd = [
                            "ffmpeg",
                            "-y",
                            "-init_hw_device", f"vaapi=hw:{vaapi_dev}",
                            "-filter_hw_device", "hw",
                            "-r", str(fps),
                            "-f", "concat",
                            "-safe", "0",
                            "-i", list_path,
                            "-vf", "format=nv12,hwupload",
                            "-c:v", "h264_vaapi",
                            "-b:v", "3000k",
                            "-maxrate", "5000k",
                            "-bufsize", "6000k",
                            "-movflags", "+faststart",
                            str(tmp_out),
                        ]
                        proc = subprocess.run(va_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env)
                        if proc.returncode == 0 and tmp_out.exists():
                            tmp_out.replace(output_file)
                            logger.info(
                                "Timelapse MP4 created successfully via VA-API: %s (h264, %d bytes)",
                                output_file.name,
                                output_file.stat().st_size,
                            )
                            return True
                        logger.warning(
                            "VA-API H.264 export failed (code %s): %s. Falling back to libx264 software encoding.",
                            proc.returncode,
                            proc.stderr[-300:] if proc.stderr else "unknown error",
                        )
                        if tmp_out.exists():
                            tmp_out.unlink()

                    # Software libx264 fallback with multithreading
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-threads", "0",
                        "-r", str(fps),
                        "-f", "concat",
                        "-safe", "0",
                        "-i", list_path,
                        "-c:v", "libx264",
                        "-preset", "ultrafast",
                        "-tune", "zerolatency",
                        "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart",
                        str(tmp_out),
                    ]
                else:
                    # Stream copy HEVC: 0% CPU, ultrafast remux
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-r", str(fps),
                        "-f", "concat",
                        "-safe", "0",
                        "-i", list_path,
                        "-c:v", "copy",
                        "-tag:v", "hvc1",
                        "-movflags", "+faststart",
                        str(tmp_out),
                    ]

                logger.info("Generating timelapse MP4 (%s) with cmd: %s", codec, " ".join(cmd[:12]))
                proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env)
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
