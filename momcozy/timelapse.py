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
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .config import ConfigStore
from .hwaccel import detect_vaapi_device

if TYPE_CHECKING:
    from .stream import VideoStreamer

logger = logging.getLogger(__name__)


DEFAULT_MOTION_MERGE_GAP_SEC = 30.0
_GENERIC_MOTION_LABELS = {"", "Motion Detected", "Motion Alert"}


def format_duration(seconds: float) -> str:
    """Format duration in seconds to a human-readable string (e.g., '4h 12m' or '25m')."""
    total_sec = max(0, int(seconds))
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {total_sec % 60:02d}s"


def format_event_duration(seconds: float) -> str:
    """Format a motion range duration concisely (e.g., '18s', '1m 25s', '1h 05m')."""
    total_sec = max(0, int(round(seconds)))
    if total_sec < 60:
        return f"{total_sec}s"
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {total_sec % 60:02d}s"


def merge_motion_ranges(
    events: List[Dict[str, Any]],
    merge_gap_sec: float = DEFAULT_MOTION_MERGE_GAP_SEC,
) -> List[Dict[str, Any]]:
    """Normalize motion/detection events into [time, end_time] ranges and merge nearby ranges.

    Ranges of the same event type whose gap (next.time - cur.end_time) is <= merge_gap_sec
    are merged into a single continuous range. Specific labels (e.g., 'Baby Motion',
    'Human Detected') take precedence over generic labels ('Motion Detected', 'Motion Alert').
    """
    if not events:
        return []

    gap = max(0.0, float(merge_gap_sec))
    by_type: Dict[str, List[Dict[str, Any]]] = {}

    for ev in events:
        if not isinstance(ev, dict):
            continue
        raw_start = ev.get("time", ev.get("start_time"))
        if raw_start is None:
            continue
        start = round(float(raw_start), 1)
        raw_end = ev.get("end_time", start)
        end = round(max(start, float(raw_end if raw_end is not None else start)), 1)
        ev_type = str(ev.get("type", "motion"))
        label = str(ev.get("label", "Motion Detected"))
        item: Dict[str, Any] = {
            "time": start,
            "end_time": end,
            "type": ev_type,
            "label": label,
        }
        if ev.get("source") == "scan":
            item["source"] = "scan"
        by_type.setdefault(ev_type, []).append(item)

    merged_all: List[Dict[str, Any]] = []
    for ev_type, group in by_type.items():
        group.sort(key=lambda x: (x["time"], x["end_time"]))
        merged_group: List[Dict[str, Any]] = []
        for item in group:
            if not merged_group:
                merged_group.append(dict(item))
                continue
            cur = merged_group[-1]
            if item["time"] - cur["end_time"] <= gap:
                cur["end_time"] = round(max(cur["end_time"], item["end_time"]), 1)
                if cur["label"] in _GENERIC_MOTION_LABELS and item["label"] not in _GENERIC_MOTION_LABELS:
                    cur["label"] = item["label"]
                elif not cur["label"] and item["label"]:
                    cur["label"] = item["label"]
                # If a hardware/manual event merges with a scan range, retain non-scan provenance
                if cur.get("source") == "scan" and item.get("source") != "scan":
                    cur.pop("source", None)
            else:
                merged_group.append(dict(item))
        merged_all.extend(merged_group)

    merged_all.sort(key=lambda x: (x["time"], x["end_time"]))
    return merged_all


def format_video_duration(seconds: float) -> str:
    """Format video playback seconds to 'M:SS'."""
    total_sec = max(0, round(seconds))
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


def extract_hevc_keyframe_luma(chunk: bytes, grid_size: int = 16) -> Optional[bytes]:
    """Extract a fast grid_size x grid_size grayscale luminance fingerprint from an HEVC keyframe.

    Decodes a single keyframe using PyAV with thread_count=1 (sub-10ms).
    Directly subsamples the Y plane without color conversions or software rescaling.
    """
    try:
        import av

        codec = av.CodecContext.create("hevc", "r")
        codec.thread_count = 1
        last_frame = None
        for packet in list(codec.parse(chunk)) + list(codec.parse(b"")):
            for frame in codec.decode(packet):
                last_frame = frame
        for frame in codec.decode():
            last_frame = frame
        if not last_frame or not last_frame.planes:
            return None

        plane = last_frame.planes[0]
        y = memoryview(plane)
        w, h = last_frame.width, last_frame.height
        if w <= 0 or h <= 0:
            return None
        stride = getattr(plane, "line_size", w) or w
        samples = bytearray(grid_size * grid_size)
        idx = 0
        for r in range(grid_size):
            py = min(h - 1, ((2 * r + 1) * h) // (2 * grid_size))
            row_offset = py * stride
            for c in range(grid_size):
                px = min(w - 1, ((2 * c + 1) * w) // (2 * grid_size))
                samples[idx] = y[row_offset + px]
                idx += 1
        return bytes(samples)
    except Exception as exc:
        logger.debug("extract_hevc_keyframe_luma error: %s", exc)
        return None


def luma_fingerprint_diff(
    fp1: Optional[bytes],
    fp2: Optional[bytes],
    grid_size: int = 16,
    block_size: int = 4,
    step: int = 2,
    drop_outliers: int = 1,
) -> float:
    """Compute maximum localized block difference between two luma fingerprints (0.0 to 255.0).

    Uses a sliding block_size x block_size window across the grid_size x grid_size fingerprint.
    Trims the highest outlier pixel in each block (drop_outliers=1) to eliminate false positives
    from single-pixel fluctuations (such as on-screen camera clock digits, OSD text changes,
    or sensor compression noise).
    Computing the maximum difference across localized blocks ensures that localized movements
    (such as a baby occupying ~1/10th of a room) are detected reliably.
    """
    if not fp1 or not fp2 or len(fp1) != len(fp2):
        return 999.0
    if len(fp1) != grid_size * grid_size:
        return sum(abs(a - b) for a, b in zip(fp1, fp2)) / len(fp1)

    max_block_diff = 0.0
    for br in range(0, grid_size - block_size + 1, step):
        for bc in range(0, grid_size - block_size + 1, step):
            deltas = []
            for r in range(block_size):
                row = (br + r) * grid_size
                for c in range(block_size):
                    deltas.append(abs(fp1[row + bc + c] - fp2[row + bc + c]))
            if drop_outliers > 0 and len(deltas) > drop_outliers:
                deltas.sort()
                trimmed = deltas[:-drop_outliers]
                b_diff = sum(trimmed) / len(trimmed)
            else:
                b_diff = sum(deltas) / len(deltas)
            if b_diff > max_block_diff:
                max_block_diff = b_diff

    return max_block_diff


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
        config_path: Optional[Path | str] = None,
        motion_merge_gap_sec: float = DEFAULT_MOTION_MERGE_GAP_SEC,
    ) -> None:
        self.streamer = streamer
        self.storage_dir = Path(storage_dir or os.getenv("MOMCOZY_TIMELAPSE_DIR", "timelapse"))
        self.config_path = Path(config_path) if config_path else None
        self.total_slots = int(total_slots)
        self.slot_duration_sec = float(slot_duration_sec)
        self.sample_interval_sec = float(sample_interval_sec)
        self.fps = int(fps)
        self.speed_ratio = int(speed_ratio) if int(speed_ratio) in (15, 30, 60, 90) else 60
        self.motion_merge_gap_sec = float(motion_merge_gap_sec)

        self._running = False
        self._record_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._meta_lock = threading.RLock()

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
        persisted_sync = self._load_persisted_sync_setting()
        self._persisted_sync_setting: Optional[bool] = persisted_sync
        self.sync_night_vision: bool = bool(persisted_sync) if persisted_sync is not None else False
        self.last_session_type: str = "manual"  # "manual" | "auto"
        self.current_session_type: Optional[str] = None

        # Lease on the camera stream, held while recording
        self._stream_lease: Optional[Any] = None

        # Cache for exported MP4: cache_key -> {"path": Path, "created_at": float, "mtime": float}
        self._exports_cache: Dict[str, Dict[str, Any]] = {}
        # In-flight export tasks deduplication: cache_key -> asyncio.Task
        self._in_flight_exports: Dict[str, asyncio.Task] = {}

        # Motion detection on-demand scan cache
        self._last_motion_scan_mtime: float = 0.0
        self._last_meta_file_mtime: float = 0.0

        self._load_meta()

    def _detect_vaapi_device(self) -> Optional[str]:
        """Detect available VA-API device path for hardware acceleration."""
        return detect_vaapi_device()

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

    def _get_config_path(self) -> Path:
        """Resolve persistent configuration file path (.momcozy.json)."""
        if self.config_path is not None:
            return self.config_path
        default_dir = Path(os.getenv("MOMCOZY_TIMELAPSE_DIR", "timelapse"))
        try:
            if self.storage_dir == default_dir or self.storage_dir.resolve() == default_dir.resolve():
                from .cloud import DEFAULT_CONFIG_PATH
                return DEFAULT_CONFIG_PATH
        except Exception:
            pass
        return self.storage_dir / ".momcozy.json"

    def _load_persisted_sync_setting(self) -> Optional[bool]:
        """Load persisted night vision sync preference from env or config file."""
        env_val = os.environ.get("MOMCOZY_TIMELAPSE_SYNC_NV") or os.environ.get("MOMCOZY_SYNC_NIGHT_VISION")
        if env_val is not None:
            return env_val.strip().lower() in ("1", "true", "yes", "on", "enable", "enabled")
        cfg = ConfigStore.read(self._get_config_path())
        if "timelapse_sync_night_vision" in cfg:
            return bool(cfg["timelapse_sync_night_vision"])
        return None

    def _persist_sync_setting(self, enabled: bool) -> None:
        """Persist night vision sync preference to config file."""
        cfg_path = self._get_config_path()
        try:
            def _apply(cfg: Dict[str, Any]) -> bool:
                if cfg.get("timelapse_sync_night_vision") == bool(enabled):
                    return False
                cfg["timelapse_sync_night_vision"] = bool(enabled)
                return True

            ConfigStore.mutate(cfg_path, _apply, create=True)
        except Exception as exc:
            logger.warning("Could not persist timelapse_sync_night_vision to config %s: %s", cfg_path, exc)

    def _load_meta(self) -> None:
        """Load metadata from meta.json or reconstruct from existing slot files."""
        with self._meta_lock:
            if not self.storage_dir.exists():
                return

            meta_loaded = False
            if self.meta_file.exists():
                try:
                    self._last_meta_file_mtime = self.meta_file.stat().st_mtime
                    data = json.loads(self.meta_file.read_text(encoding="utf-8"))
                    self._current_slot = int(data.get("current_slot", 0)) % self.total_slots
                    self._slots_meta = data.get("slots", {})
                    self._events = merge_motion_ranges(
                        data.get("events", []),
                        merge_gap_sec=self.motion_merge_gap_sec,
                    )
                    self._last_motion_scan_mtime = 0.0
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

            # Reconcile with any slot files present on disk (fills in missing slots or estimates 0-frame entries)
            newest_slot = self._current_slot
            newest_mtime = 0.0
            for i in range(self.total_slots):
                p = self.slot_path(i)
                if p.exists() and p.stat().st_size > 0:
                    s_key = str(i)
                    st = p.stat()
                    slot_info = self._slots_meta.get(s_key)
                    if not slot_info or not slot_info.get("frames"):
                        approx_frames = max(1, st.st_size // 35000)
                        self._slots_meta[s_key] = {
                            "mtime": slot_info.get("mtime", st.st_mtime) if slot_info else st.st_mtime,
                            "frames": approx_frames,
                            "bytes": st.st_size,
                            "duration": slot_info.get("duration", min(self.slot_duration_sec, approx_frames * 4.0)) if slot_info else min(self.slot_duration_sec, approx_frames * 4.0),
                        }
                    slot_mtime = float(self._slots_meta[s_key].get("mtime", st.st_mtime))
                    if slot_mtime > newest_mtime:
                        newest_mtime = slot_mtime
                        newest_slot = i

            if newest_mtime > 0:
                cur_mtime = float(self._slots_meta.get(str(self._current_slot), {}).get("mtime", 0.0))
                if not meta_loaded or newest_mtime > cur_mtime:
                    self._current_slot = newest_slot

    def _save_meta(self) -> None:
        """Persist metadata to meta.json atomically under _meta_lock."""
        with self._meta_lock:
            tmp_path: Optional[Path] = None
            try:
                self.storage_dir.mkdir(parents=True, exist_ok=True)
                meta = {
                    "total_slots": self.total_slots,
                    "slot_duration_sec": self.slot_duration_sec,
                    "sample_interval_sec": self.sample_interval_sec,
                    "fps": self.fps,
                    "speed_ratio": self.speed_ratio,
                    "current_slot": self._current_slot,
                    "slots": dict(self._slots_meta),
                    "events": list(self._events),
                    "last_session_type": self.last_session_type,
                }
                with tempfile.NamedTemporaryFile(
                    "w",
                    dir=self.storage_dir,
                    prefix="meta.json.",
                    suffix=".tmp",
                    encoding="utf-8",
                    delete=False,
                ) as tmp_f:
                    tmp_path = Path(tmp_f.name)
                    json.dump(meta, tmp_f, indent=2)
                    tmp_f.flush()
                tmp_path.replace(self.meta_file)
                self._last_meta_file_mtime = self.meta_file.stat().st_mtime
            except Exception as exc:
                logger.debug("Failed saving timelapse meta.json: %s", exc)
                if tmp_path is not None and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass

    def set_sync_night_vision(self, enabled: bool) -> bool:
        """Enable or disable automatic timelapse synchronization with Night Vision."""
        self.sync_night_vision = bool(enabled)
        self._persisted_sync_setting = bool(enabled)
        self._persist_sync_setting(enabled)
        logger.info("Timelapse Night Vision sync set to: %s", self.sync_night_vision)
        return self.sync_night_vision

    def clear(self) -> None:
        """Clear all previous timelapse slot recordings and metadata."""
        with self._meta_lock:
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
            self._last_motion_scan_mtime = 0.0
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

    def _retention_cutoff(self, latest_ts: float) -> float:
        """Compute earliest wall-clock timestamp still covered by the circular slot buffer."""
        max_history_sec = self.total_slots * self.slot_duration_sec
        cutoff = latest_ts - max_history_sec
        ordered = self.get_ordered_slots()
        if ordered:
            idx0, path0, meta0 = ordered[0]
            dur0 = float(meta0.get("duration") or self.slot_duration_sec)
            end0 = float(meta0.get("mtime") or (path0.stat().st_mtime if path0.exists() else latest_ts))
            cutoff = min(cutoff, end0 - dur0 - 5.0)
        return cutoff

    def record_event(
        self,
        event_type: str = "motion",
        label: str = "Motion Alert",
        timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        merge_gap_sec: Optional[float] = None,
    ) -> bool:
        """Record a motion or detection range [timestamp, end_timestamp].

        Nearby or overlapping ranges of the same type within merge_gap_sec are merged
        into a single continuous range. Returns True if a new distinct event range was
        created, or False if merged into an existing nearby range.
        Prunes ranges older than the circular buffer's retention window.
        """
        start_ts = round(float(timestamp if timestamp is not None else time.time()), 1)
        end_ts = round(max(start_ts, float(end_timestamp) if end_timestamp is not None else start_ts), 1)
        gap = self.motion_merge_gap_sec if merge_gap_sec is None else max(0.0, float(merge_gap_sec))

        with self._meta_lock:
            merges_with_existing = False
            with self._meta_lock:
                events_snapshot = list(self._events)

            for ev in events_snapshot:
                if ev.get("type") != event_type:
                    continue
                ev_start = float(ev.get("time", 0.0))
                ev_end = max(ev_start, float(ev.get("end_time", ev_start)))
                if start_ts <= ev_end + gap and end_ts >= ev_start - gap:
                    merges_with_existing = True
                    break

            candidate = {
                "time": start_ts,
                "end_time": end_ts,
                "type": event_type,
                "label": label,
            }
            merged = merge_motion_ranges(self._events + [candidate], merge_gap_sec=gap)

            # Keep ranges within the circular ring buffer's historical retention window
            latest_ts = max([float(ev.get("end_time", ev.get("time", 0.0))) for ev in merged] + [end_ts])
            cutoff = self._retention_cutoff(latest_ts)
            pruned = [ev for ev in merged if float(ev.get("end_time", ev.get("time", 0.0))) >= cutoff]

            if pruned != self._events:
                self._events = pruned
                self._save_meta()
                if not merges_with_existing:
                    logger.info(
                        "Recorded timelapse %s range: '%s' [%.1f - %.1f]",
                        event_type,
                        label,
                        start_ts,
                        end_ts,
                    )
                else:
                    logger.debug(
                        "Extended timelapse %s range with '%s' [%.1f - %.1f]",
                        event_type,
                        label,
                        start_ts,
                        end_ts,
                    )

            return not merges_with_existing

    @property
    def has_content(self) -> bool:
        """Check if any recorded timelapse slots exist with content."""
        ordered = self.get_ordered_slots()
        return bool(ordered and any(p.exists() and p.stat().st_size > 0 for _, p, _ in ordered))

    def detect_motion_events(
        self,
        threshold: float = 3.5,
        coarse_step: int = 30,
        min_window: int = 1,
        merge_gap_sec: Optional[float] = None,
        debounce_sec: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Run adaptive binary search motion detection across recorded timelapse slots on playback.

        Operates on-demand during playback (zero CPU overhead during recording).
        Leverages state-difference invariance: if Frame(A) ≈ Frame(B), no motion occurred
        in interval [A, B] and the entire block is skipped in O(1).
        Intervals with diff >= threshold are recursively subdivided down to min_window to
        identify active motion intervals [start_time, end_time], and nearby intervals within
        merge_gap_sec are merged into cohesive motion ranges.
        Uses localized block luma differences so movements occupying ~1/10th of the frame
        are detected reliably without dilution from a static background.
        """
        ordered = self.get_ordered_slots()
        if not ordered:
            return self._events

        latest_mtime = max(p.stat().st_mtime for _, p, _ in ordered if p.exists())
        if self._last_motion_scan_mtime >= latest_mtime:
            return self._events

        effective_gap = float(
            merge_gap_sec
            if merge_gap_sec is not None
            else (debounce_sec if debounce_sec is not None else self.motion_merge_gap_sec)
        )

        vps_marker = b"\x00\x00\x00\x01\x40\x01"
        indexed_frames: List[Tuple[Path, int, int, float]] = []

        for slot_idx, path, meta in ordered:
            if not path.exists() or path.stat().st_size < 100:
                continue
            file_size = path.stat().st_size
            duration = float(meta.get("duration", self.slot_duration_sec))
            slot_end = float(meta.get("mtime", path.stat().st_mtime))
            slot_start = slot_end - duration

            try:
                with open(path, "rb") as f:
                    import mmap

                    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                    offsets = []
                    pos = 0
                    while True:
                        pos = mm.find(vps_marker, pos)
                        if pos == -1:
                            break
                        offsets.append(pos)
                        pos += 6
                    mm.close()

                num_kf = len(offsets)
                if num_kf == 0:
                    continue

                for i, off in enumerate(offsets):
                    length = (offsets[i + 1] - off) if (i + 1 < num_kf) else (file_size - off)
                    frame_ts = slot_start + (i / max(1, num_kf - 1)) * duration
                    indexed_frames.append((path, off, length, frame_ts))
            except Exception as exc:
                logger.debug("Failed indexing keyframes in %s: %s", path, exc)

        total_frames = len(indexed_frames)
        if total_frames < 2:
            return self._events

        fp_cache: Dict[int, Optional[bytes]] = {}

        def get_fp(idx: int) -> Optional[bytes]:
            if idx not in fp_cache:
                f_path, f_off, f_len, _ = indexed_frames[idx]
                try:
                    with open(f_path, "rb") as f:
                        f.seek(f_off)
                        chunk = f.read(f_len)
                    fp_cache[idx] = extract_hevc_keyframe_luma(chunk)
                except Exception:
                    fp_cache[idx] = None
            return fp_cache[idx]

        raw_intervals: List[Tuple[float, float]] = []
        eff_min_window = max(1, int(min_window))

        def subdivide(left: int, right: int, out_indices: List[Tuple[int, int]]) -> None:
            if right - left <= eff_min_window:
                out_indices.append((left, right))
                return
            mid = (left + right) // 2
            fp_l = get_fp(left)
            fp_m = get_fp(mid)
            fp_r = get_fp(right)
            diff_lm = luma_fingerprint_diff(fp_l, fp_m) if (fp_l is not None and fp_m is not None) else -1.0
            diff_mr = luma_fingerprint_diff(fp_m, fp_r) if (fp_m is not None and fp_r is not None) else -1.0
            left_active = diff_lm >= threshold
            right_active = diff_mr >= threshold
            if left_active:
                subdivide(left, mid, out_indices)
            if right_active:
                subdivide(mid, right, out_indices)
            if not left_active and not right_active:
                # Gradual drift across mid where diff(left, right) >= threshold but each half < threshold
                if diff_lm >= diff_mr and diff_lm >= 0.0:
                    subdivide(left, mid, out_indices)
                elif diff_mr >= 0.0:
                    subdivide(mid, right, out_indices)

        # Partition frames into contiguous recording segments so paused recording gaps
        # never fabricate artificial motion ranges across unrecorded wall-clock intervals.
        max_frame_gap_sec = max(30.0, self.sample_interval_sec * 15.0)
        segments: List[Tuple[int, int]] = []
        seg_start = 0
        for idx in range(1, total_frames):
            if indexed_frames[idx][3] - indexed_frames[idx - 1][3] > max_frame_gap_sec:
                if idx - 1 > seg_start:
                    segments.append((seg_start, idx - 1))
                seg_start = idx
        if total_frames - 1 > seg_start:
            segments.append((seg_start, total_frames - 1))

        for s_begin, s_end in segments:
            seg_leaves: List[Tuple[int, int]] = []
            for start in range(s_begin, s_end, coarse_step):
                end = min(s_end, start + coarse_step)
                fp_start = get_fp(start)
                fp_end = get_fp(end)
                if fp_start is None or fp_end is None:
                    continue
                if luma_fingerprint_diff(fp_start, fp_end) >= threshold:
                    subdivide(start, end, seg_leaves)

            if not seg_leaves:
                continue

            seg_leaves.sort()
            merged_idx: List[List[int]] = []
            for l_idx, r_idx in seg_leaves:
                if not merged_idx or (indexed_frames[l_idx][3] - indexed_frames[merged_idx[-1][1]][3]) > effective_gap:
                    merged_idx.append([l_idx, r_idx])
                else:
                    merged_idx[-1][1] = max(merged_idx[-1][1], r_idx)

            # Locally probe (1, 2, 4, 8 frames) around detected range boundaries within effective_gap
            # so sustained motion that partially returns near a coarse anchor is captured to its full extent.
            for s_idx, e_idx in merged_idx:
                cur_s = s_idx
                while cur_s > s_begin:
                    found_prev = False
                    for step in (1, 2, 4, 8):
                        cand = cur_s - step
                        if cand < s_begin or (indexed_frames[cur_s][3] - indexed_frames[cand][3]) > effective_gap:
                            break
                        fp_cand, fp_cur = get_fp(cand), get_fp(cur_s)
                        if fp_cand is not None and fp_cur is not None and luma_fingerprint_diff(fp_cand, fp_cur) >= threshold:
                            best = cand
                            for k in range(cand, cur_s):
                                fp_a, fp_b = get_fp(k), get_fp(k + 1)
                                if fp_a is not None and fp_b is not None and luma_fingerprint_diff(fp_a, fp_b) >= threshold:
                                    best = k
                                    break
                            cur_s = best
                            found_prev = True
                            break
                    if not found_prev:
                        break

                cur_e = e_idx
                while cur_e < s_end:
                    found_next = False
                    for step in (1, 2, 4, 8):
                        cand = cur_e + step
                        if cand > s_end or (indexed_frames[cand][3] - indexed_frames[cur_e][3]) > effective_gap:
                            break
                        fp_cur, fp_cand = get_fp(cur_e), get_fp(cand)
                        if fp_cur is not None and fp_cand is not None and luma_fingerprint_diff(fp_cur, fp_cand) >= threshold:
                            best = cand
                            for k in range(cand, cur_e, -1):
                                fp_a, fp_b = get_fp(k - 1), get_fp(k)
                                if fp_a is not None and fp_b is not None and luma_fingerprint_diff(fp_a, fp_b) >= threshold:
                                    best = k
                                    break
                            cur_e = best
                            found_next = True
                            break
                    if not found_next:
                        break

                t_start = indexed_frames[cur_s][3]
                t_end = indexed_frames[cur_e][3]
                raw_intervals.append((t_start, max(t_start, t_end)))

        scanned_ranges = merge_motion_ranges(
            [
                {
                    "time": round(t_s, 1),
                    "end_time": round(t_e, 1),
                    "type": "motion",
                    "label": "Motion Detected",
                    "source": "scan",
                }
                for t_s, t_e in raw_intervals
            ],
            merge_gap_sec=effective_gap,
        )

        # Retain non-scan (hardware/manual) events within the retention window and merge with fresh scan ranges
        latest_frame_ts = indexed_frames[-1][3]
        with self._meta_lock:
            cutoff = min(self._retention_cutoff(latest_frame_ts), indexed_frames[0][3] - 5.0)
            retained_external = [
                ev
                for ev in self._events
                if ev.get("source") != "scan" and float(ev.get("end_time", ev.get("time", 0.0))) >= cutoff
            ]
            merged = merge_motion_ranges(
                retained_external + scanned_ranges,
                merge_gap_sec=effective_gap,
            )

            self._events = merged
            self._last_motion_scan_mtime = latest_mtime
            self._save_meta()
            result_events = list(self._events)
        logger.info(
            "Motion scan complete: indexed %d frames (decoded %d), detected %d motion ranges",
            total_frames,
            len(fp_cache),
            len(scanned_ranges),
        )
        return result_events

    async def scan_motion_events(self, force: bool = False) -> List[Dict[str, Any]]:
        """Asynchronously run detect_motion_events in executor thread."""
        loop = asyncio.get_running_loop()
        if force:
            self._last_motion_scan_mtime = 0.0
        return await loop.run_in_executor(None, self.detect_motion_events)

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
            self._exports_cache.clear()
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
            with self._meta_lock:
                self._slots_meta[str(self._current_slot)] = {
                    "mtime": time.time(),
                    "duration": round(elapsed, 1),
                    "frames": self._frames_in_slot,
                    "bytes": sz,
                }

    def _rotate_slot(self) -> None:
        """Advance to next circular slot, truncating any existing old slot file."""
        self._close_active_slot()

        self._current_slot = (self._current_slot + 1) % self.total_slots
        self._frames_in_slot = 0
        self._bytes_in_slot = 0
        self._slot_start_monotonic = time.monotonic()
        self._slot_start_wall = time.time()
        self._save_meta()

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
                    stream_iter = sub_hevc(leased=False, keyframes_only=True)
                except TypeError:
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

    def _sync_meta_if_changed(self) -> None:
        """Reload meta.json or disk slots if modified externally while server is running."""
        if self.is_recording:
            return
        if not self.storage_dir.exists():
            return
        try:
            current_meta_mtime = self.meta_file.stat().st_mtime if self.meta_file.exists() else 0.0
            if current_meta_mtime != self._last_meta_file_mtime:
                self._load_meta()
                return
            disk_slots = set()
            for i in range(self.total_slots):
                p = self.slot_path(i)
                if p.exists() and p.stat().st_size > 0:
                    disk_slots.add(str(i))
            if disk_slots != set(self._slots_meta.keys()):
                self._load_meta()
        except OSError:
            pass

    def get_ordered_slots(self) -> List[Tuple[int, Path, Dict[str, Any]]]:
        """Return all non-empty slot files in chronological order (oldest to newest)."""
        with self._meta_lock:
            self._sync_meta_if_changed()
            slots_snapshot = {k: dict(v) for k, v in self._slots_meta.items()}
        valid_slots = []
        for i in range(self.total_slots):
            p = self.slot_path(i)
            if p.exists():
                try:
                    sz = p.stat().st_size
                    if sz > 0:
                        meta = slots_snapshot.get(str(i), {})
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
                frames = meta.get("frames", 0)
                if not frames and path.exists() and path.stat().st_size > 0:
                    frames = max(1, path.stat().st_size // 35000)
                if dur is None or dur <= 0:
                    dur = min(self.slot_duration_sec, frames * 4.0 if frames > 0 else 0.0)
                total_real_time_sec += float(dur)
                total_frames += frames
                total_bytes += meta.get("bytes", path.stat().st_size if path.exists() else 0)

        max_duration = self.total_slots * self.slot_duration_sec
        total_real_time_sec = min(total_real_time_sec, max_duration)
        video_sec = total_real_time_sec / self.speed_ratio if self.speed_ratio > 0 else 0.0

        # Calculate motion range timeline positions mapped across concatenated recorded slots
        events_list: List[Dict[str, Any]] = []
        if ordered and total_real_time_sec > 0:
            slot_spans: List[Tuple[float, float, float]] = []
            for idx, path, meta in ordered:
                if idx == self._current_slot and self.is_recording:
                    dur = min(self.slot_duration_sec, max(0.0, time.monotonic() - self._slot_start_monotonic))
                    end_t = time.time()
                else:
                    frames = meta.get("frames", 0)
                    dur = float(
                        meta.get("duration")
                        or min(self.slot_duration_sec, (frames * 4.0 if frames > 0 else self.slot_duration_sec))
                    )
                    end_t = float(meta.get("mtime") or (path.stat().st_mtime if path.exists() else time.time()))
                slot_spans.append((end_t - dur, end_t, dur))

            def _project_ts(ts: float) -> float:
                cum_dur = 0.0
                for s_start, s_end, s_dur in slot_spans:
                    if ts <= s_end:
                        offset = max(0.0, min(ts - s_start, s_dur))
                        return min(total_real_time_sec, cum_dur + offset)
                    cum_dur += s_dur
                return min(cum_dur, total_real_time_sec)

            earliest_slot_start = slot_spans[0][0]
            latest_slot_end = slot_spans[-1][1]

            with self._meta_lock:
                events_snapshot = list(self._events)

            for ev in events_snapshot:
                start_time = float(ev.get("time", ev.get("start_time", 0.0)))
                end_time = max(start_time, float(ev.get("end_time", start_time)))
                if end_time < earliest_slot_start - 5.0 or start_time > latest_slot_end + 30.0:
                    continue

                start_elapsed = _project_ts(start_time)
                end_elapsed = max(start_elapsed, _project_ts(end_time))

                if start_elapsed <= total_real_time_sec + 30.0:
                    percent = min(100.0, max(0.0, round((start_elapsed / total_real_time_sec) * 100.0, 2)))
                    end_percent = min(100.0, max(percent, round((end_elapsed / total_real_time_sec) * 100.0, 2)))
                    ev_video_sec = round(start_elapsed / self.speed_ratio, 1) if self.speed_ratio > 0 else 0.0
                    end_video_sec = (
                        round(max(ev_video_sec, end_elapsed / self.speed_ratio), 1)
                        if self.speed_ratio > 0
                        else 0.0
                    )
                    duration_sec = round(max(0.0, end_time - start_time), 1)
                    start_time_str = time.strftime("%I:%M:%S %p", time.localtime(start_time))
                    end_time_str = time.strftime("%I:%M:%S %p", time.localtime(end_time))
                    time_str = f"{start_time_str} – {end_time_str}" if duration_sec >= 1.0 else start_time_str
                    events_list.append({
                        "time": start_time,
                        "start_time": start_time,
                        "end_time": end_time,
                        "duration_sec": duration_sec,
                        "duration_formatted": format_event_duration(duration_sec),
                        "time_str": time_str,
                        "start_time_str": start_time_str,
                        "end_time_str": end_time_str,
                        "type": ev.get("type", "motion"),
                        "label": ev.get("label", "Motion Alert"),
                        "elapsed_sec": round(start_elapsed, 1),
                        "end_elapsed_sec": round(end_elapsed, 1),
                        "video_sec": ev_video_sec,
                        "end_video_sec": end_video_sec,
                        "video_formatted": format_video_duration(ev_video_sec),
                        "end_video_formatted": format_video_duration(end_video_sec),
                        "percent": percent,
                        "end_percent": end_percent,
                    })
            events_list.sort(key=lambda x: (x["elapsed_sec"], x["end_elapsed_sec"]))

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
            "has_content": self.has_content,
            "events": events_list,
            "motion_events_count": len(events_list) if events_list else len(self._events),
            "sync_night_vision": self.sync_night_vision,
            "last_session_type": self.last_session_type,
            "current_session_type": self.current_session_type,
        }

    async def export_mp4(
        self,
        codec: str = "hevc",
        ratio: Optional[int] = None,
        allow_cache_only: bool = False,
    ) -> Optional[Path]:
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
            if allow_cache_only:
                if (not self.is_recording and created_at >= latest_mtime) or (self.is_recording and (time.time() - created_at) < 300.0):
                    return out_path
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
        target_video_sec = max(0.5, real_time_sec / effective_ratio)
        export_fps = min(60.0, max(5.0, round(total_frames / target_video_sec, 2)))

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
