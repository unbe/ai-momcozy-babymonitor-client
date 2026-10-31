"""Night vision detection and the timelapse automation that follows it.

In Auto mode (DP 108 == "0") the camera flips its IR-cut filter on its own and the DP never
changes, so night vision is detected from the video itself: infrared frames are grayscale
(R ~ G ~ B) while daytime frames are colourful.

Everything here is free of I/O so the automation can be unit tested with plain timestamps.
"""

from __future__ import annotations

import enum
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

COLOR_DIFF_THRESHOLD = 8.0
ACTIVATE_AFTER_SEC = 0.0
DEACTIVATE_AFTER_SEC = 120.0
THUMBNAIL_SIZE = 16


def thumbnail_color_diff(rgb: bytes) -> float:
    """Mean |R-G| + |G-B| + |B-R| over packed RGB24 pixels (0 for pure grayscale)."""
    pixels = len(rgb) // 3
    if pixels == 0:
        return 999.0
    total = sum(
        abs(rgb[i] - rgb[i + 1]) + abs(rgb[i + 1] - rgb[i + 2]) + abs(rgb[i + 2] - rgb[i])
        for i in range(0, pixels * 3, 3)
    )
    return total / pixels


def analyze_night_vision_frame(
    frame_bytes: bytes, threshold: float = COLOR_DIFF_THRESHOLD
) -> Tuple[bool, float]:
    """Analyze whether a JPEG frame is infrared monochrome (Night Vision active)."""
    if not frame_bytes or not isinstance(frame_bytes, (bytes, bytearray)):
        return False, 999.0
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
        img = img.resize((THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.Resampling.BOX)
        raw = img.tobytes()
        diff = thumbnail_color_diff(raw)
        return (diff < threshold), diff
    except Exception as exc:
        logger.debug("analyze_night_vision_frame error: %s", exc)
        return False, 999.0


def detect_night_vision_from_jpeg(
    frame_bytes: bytes, threshold: float = COLOR_DIFF_THRESHOLD
) -> bool:
    """Detect whether a JPEG frame is infrared monochrome (Night Vision active)."""
    is_nv, _ = analyze_night_vision_frame(frame_bytes, threshold)
    return is_nv


def analyze_hevc_keyframe(
    chunk: bytes, threshold: float = COLOR_DIFF_THRESHOLD
) -> Optional[Tuple[bool, float]]:
    """Decide from one Annex B keyframe (VPS/SPS/PPS + IDR) whether night vision is active.

    Only the keyframe is decoded and it is scaled straight to a 16x16 thumbnail, so this
    costs a fraction of decoding a whole GOP and re-encoding it as JPEG.
    Returns (is_night_vision, mean_color_diff), or None if the keyframe cannot be decoded.
    """
    try:
        import av

        codec = av.CodecContext.create("hevc", "r")
        last_frame = None
        for packet in list(codec.parse(chunk)) + list(codec.parse(b"")):
            for frame in codec.decode(packet):
                last_frame = frame
        for frame in codec.decode():
            last_frame = frame
        if last_frame is None:
            return None
        thumb = last_frame.reformat(width=THUMBNAIL_SIZE, height=THUMBNAIL_SIZE, format="rgb24")
        diff = thumbnail_color_diff(thumb.to_ndarray().tobytes())
    except Exception as exc:
        logger.debug("analyze_hevc_keyframe error: %s", exc)
        return None
    return diff < threshold, diff


class Action(enum.Enum):
    """What the caller must do after NightVisionAutomation.step()."""

    NONE = "none"
    MARK_AUTO = "mark_auto"  # relabel the running (manual) recording as an auto session
    START_AUTO = "start_auto"
    STOP_AUTO = "stop_auto"


class SyncState(str, enum.Enum):
    DISABLED = "disabled"
    IDLE = "idle"
    ACTIVATING = "activating"  # dark, waiting out ACTIVATE_AFTER_SEC
    RECORDING = "recording"
    STOPPING = "stopping"  # light again, waiting out DEACTIVATE_AFTER_SEC


class NightVisionAutomation:
    """Debounces night vision observations into start/stop decisions for the timelapse.

    - Dark (and not recording, not suppressed): start an auto session immediately (when activate_delay <= 0).
    - Light for deactivate_delay while recording an auto/night session: stop it.
      Manual daytime recordings are never stopped by daylight.
    - A manual stop suppresses auto-start until the next daylight observation.
    """

    def __init__(
        self,
        activate_delay: float = ACTIVATE_AFTER_SEC,
        deactivate_delay: float = DEACTIVATE_AFTER_SEC,
    ) -> None:
        self.activate_delay = activate_delay
        self.deactivate_delay = deactivate_delay
        self.is_dark = False
        self.last_diff: Optional[float] = None
        self._dark_since: Optional[float] = None
        self._light_since: Optional[float] = None
        self._night_session = False  # darkness was observed during the current recording
        self._suppressed = False

    @property
    def activating(self) -> bool:
        """True while a darkness countdown is running (the stream must stay up for it)."""
        return self._dark_since is not None and self.activate_delay > 0

    def observe(self, is_dark: bool, diff: float) -> None:
        """Record a fresh camera observation (and log it, including kick-in/out transitions)."""
        logger.info(
            "Night vision check result: %s (color saturation diff: %.1f, threshold: %.1f)",
            "ACTIVE (infrared monochrome)" if is_dark else "INACTIVE (daylight color)",
            diff,
            COLOR_DIFF_THRESHOLD,
        )
        if is_dark != self.is_dark:
            if is_dark:
                logger.info(
                    "Automatic Night Vision kick-in detected (camera frame transitioned to infrared monochrome, diff=%.1f).",
                    diff,
                )
            else:
                logger.info(
                    "Automatic Night Vision kick-out detected (camera frame transitioned to daylight color, diff=%.1f).",
                    diff,
                )
        self.is_dark = is_dark
        self.last_diff = diff

    def step(self, now: float, is_dark: bool, recording: bool, session_auto: bool) -> Action:
        """Advance the debounce timers with the latest darkness reading."""
        if is_dark:
            self._light_since = None
            if recording:
                if not self._night_session:
                    logger.info("Night Vision active detected during ongoing recording session (session marked as auto).")
                self._night_session = True
                return Action.MARK_AUTO
            if self._suppressed:
                return Action.NONE
            if self.activate_delay <= 0:
                self._dark_since = now
                logger.info("Night Vision active detected on camera. Auto-starting timelapse...")
                self._night_session = True
                return Action.START_AUTO
            if self._dark_since is None:
                self._dark_since = now
                logger.info(
                    "Night Vision active detected on camera. Starting %.0fs activation timer...", self.activate_delay
                )
                return Action.NONE
            elapsed = now - self._dark_since
            if elapsed >= self.activate_delay:
                logger.info("Night Vision active for >= %.0fs (%.1fs). Auto-starting timelapse...", self.activate_delay, elapsed)
                self._night_session = True
                return Action.START_AUTO
            return Action.NONE

        self._dark_since = None
        self._suppressed = False
        if not (recording and (session_auto or self._night_session)):
            self._light_since = None
            return Action.NONE
        if self._light_since is None:
            self._light_since = now
            logger.info(
                "Night Vision inactive detected on camera. Starting %.0fs deactivation timer...", self.deactivate_delay
            )
            return Action.NONE
        elapsed = now - self._light_since
        if elapsed >= self.deactivate_delay:
            logger.info("Night Vision inactive for >= %.0fs (%.1fs). Auto-stopping timelapse...", self.deactivate_delay, elapsed)
            self._night_session = False
            self._light_since = None
            return Action.STOP_AUTO
        return Action.NONE

    def sync_state(self, now: float, recording: bool, session_auto: bool) -> Tuple[SyncState, Optional[float]]:
        """Current state and seconds left on the running countdown, if any."""
        if recording:
            if self._light_since is not None and (session_auto or self._night_session):
                return SyncState.STOPPING, max(0.0, self.deactivate_delay - (now - self._light_since))
            return SyncState.RECORDING, None
        if self.activate_delay > 0 and self._dark_since is not None and not self._suppressed:
            return SyncState.ACTIVATING, max(0.0, self.activate_delay - (now - self._dark_since))
        return SyncState.IDLE, None

    def reset_timers(self) -> None:
        self._dark_since = None
        self._light_since = None

    def on_manual_start(self) -> None:
        self._suppressed = False

    def on_manual_stop(self) -> None:
        """User stopped recording: don't auto-restart until the next daylight reading."""
        self.reset_timers()
        self._night_session = False
        self._suppressed = True

    def clear(self) -> None:
        """Forget all session state (footage erased)."""
        self.reset_timers()
        self._night_session = False
        self._suppressed = False
