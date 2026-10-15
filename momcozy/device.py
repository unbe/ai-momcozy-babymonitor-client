"""Momcozy BM04 Baby Monitor Client.

Controls the BM04 monitor via local network (LAN) and/or P2P cloud streaming.
Provides high-level methods to read sensors (temperature, motion, crying, noise),
adjust settings (night vision, PTZ, volume, alarms), and stream live video.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Optional, Tuple

import tinytuya

from .constants import (
    DP,
    LULLABY_TRACKS,
    LullabyMode,
    NightVisionMode,
    PtzDirection,
    Sensitivity,
)

logger = logging.getLogger(__name__)


def celsius_to_fahrenheit(celsius: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return round((celsius * 9.0 / 5.0) + 32.0, 1)


@dataclass
class BM04Status:
    """Snapshot of all monitored values and settings of Momcozy BM04."""

    temperature_c: Optional[float] = None
    temperature_f: Optional[float] = None
    temp_alert_enabled: bool = False
    temp_min_c: Optional[float] = None
    temp_max_c: Optional[float] = None

    privacy_mode: bool = False
    night_vision: str = "0"  # 0=auto, 1=off, 2=on
    indicator_light: bool = True
    image_flipped: bool = False

    motion_detected: bool = False
    motion_switch: bool = False
    motion_sensitivity: str = "1"
    motion_tracking: bool = False

    sound_switch: bool = False
    sound_sensitivity: str = "1"
    cry_detection_switch: bool = False

    lullaby_playing: bool = False
    lullaby_track: str = "0"
    lullaby_track_name: str = "Brahms' Lullaby"
    lullaby_volume: int = 50
    lullaby_mode: str = "1"  # 0=single, 1=repeat all

    master_volume: int = 50
    sd_card_status: int = 1  # 1=normal, 4=no card
    sd_storage: str = ""

    raw_dps: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        """Format a human-readable status table."""
        temp_str = (
            f"{self.temperature_c:.1f}°C ({self.temperature_f:.1f}°F)"
            if self.temperature_c is not None
            else "N/A"
        )
        nv_map = {"0": "Auto", "1": "Off", "2": "On"}
        sens_map = {"0": "Low", "1": "Medium", "2": "High"}
        lullaby_str = (
            f"Playing '{self.lullaby_track_name}' (Vol: {self.lullaby_volume}%)"
            if self.lullaby_playing
            else "Stopped"
        )

        return (
            f"--- Momcozy BM04 Baby Monitor Status ---\n"
            f"  • Temperature:        {temp_str}\n"
            f"  • Temp Alerts:        {'Enabled' if self.temp_alert_enabled else 'Disabled'} "
            f"(Range: {self.temp_min_c}°C - {self.temp_max_c}°C)\n"
            f"  • Privacy / Sleep:    {'ON (Screen/Camera Off)' if self.privacy_mode else 'OFF (Active)'}\n"
            f"  • Night Vision:       {nv_map.get(self.night_vision, self.night_vision)}\n"
            f"  • Status LED:         {'ON' if self.indicator_light else 'OFF'}\n"
            f"  • Motion Detection:   {'ON' if self.motion_switch else 'OFF'} "
            f"(Sensitivity: {sens_map.get(self.motion_sensitivity, self.motion_sensitivity)})\n"
            f"  • Motion Tracking:    {'ON' if self.motion_tracking else 'OFF'}\n"
            f"  • Sound Detection:    {'ON' if self.sound_switch else 'OFF'}\n"
            f"  • Cry Detection:      {'ON' if self.cry_detection_switch else 'OFF'}\n"
            f"  • Lullaby:            {lullaby_str}\n"
            f"  • Master Volume:      {self.master_volume}%\n"
            f"  • SD Card Status:     {'Normal' if self.sd_card_status == 1 else f'Status code {self.sd_card_status}'}\n"
        )


class MomcozyMonitor:
    """Client for controlling a Momcozy BM04 device.

    Can connect locally using device IP, device ID, and local key (fast, offline,
    no cloud lag) or manage cloud streaming.
    """

    def __init__(
        self,
        device_id: str,
        local_key: str,
        ip: Optional[str] = None,
        version: float = 3.3,
        stream_account_email: Optional[str] = None,
        stream_account_password: Optional[str] = None,
        stream_country_code: str = "1",
        stream_region: str = "us",
    ) -> None:
        self.device_id = device_id
        self.local_key = local_key
        self.ip = ip
        self.version = version

        # Video stream account details
        self.stream_email = stream_account_email
        self.stream_password = stream_account_password
        self.stream_country_code = stream_country_code
        self.stream_region = stream_region

        self._device: Optional[tinytuya.Device] = None
        self._init_local_device()

    def _init_local_device(self) -> None:
        if self.ip:
            self._device = tinytuya.Device(
                dev_id=self.device_id,
                address=self.ip,
                local_key=self.local_key,
                version=self.version,
            )
            self._device.set_socketPersistent(True)
            self._device.set_socketTimeout(5)

    def set_ip(self, ip: str) -> None:
        """Update monitor IP address."""
        self.ip = ip
        self._init_local_device()

    def discover_local_ip(self) -> Optional[str]:
        """Scan the local network for this device IP."""
        logger.info("Scanning local subnet for device %s...", self.device_id)
        devices = tinytuya.deviceScan(False, 5)
        for dev_ip, dev_info in devices.items():
            if dev_info.get("gwId") == self.device_id or dev_info.get("id") == self.device_id:
                logger.info("Found BM04 at %s", dev_ip)
                self.set_ip(dev_ip)
                return dev_ip
        logger.warning("Device %s not found in local scan", self.device_id)
        return None

    def _ensure_device(self) -> tinytuya.Device:
        if not self._device:
            if not self.ip:
                self.discover_local_ip()
            if not self._device:
                raise RuntimeError(
                    f"Device IP for {self.device_id} was not auto-discovered on the local subnet via UDP broadcast.\n"
                    f"Please provide the monitor's local Wi-Fi IP address (e.g., from your router or DHCP leases):\n"
                    f"    ./momcozy.py status --ip <CAMERA_IP>\n"
                    f"(Once provided with --ip, it will be automatically saved to ~/.momcozy.json)"
                )
        return self._device

    def get_status(self) -> BM04Status:
        """Query all DataPoints from the monitor and return a structured status."""
        dev = self._ensure_device()
        data = dev.status()
        dps = data.get("dps", {})
        if not dps:
            # Retry once with version 3.4/3.5 fallback if needed
            for v in [3.4, 3.5, 3.3]:
                dev.set_version(v)
                data = dev.status()
                dps = data.get("dps", {})
                if dps:
                    self.version = v
                    break

        status = BM04Status(raw_dps=dps)

        # Temperature sensor (tenths of degree Celsius)
        temp_raw = dps.get(str(int(DP.SENSOR_TEMPERATURE)))
        if temp_raw is not None:
            try:
                celsius = float(temp_raw) / 10.0
                status.temperature_c = round(celsius, 1)
                status.temperature_f = celsius_to_fahrenheit(celsius)
            except (ValueError, TypeError):
                pass

        # Temperature alert settings
        status.temp_alert_enabled = bool(dps.get(str(int(DP.TEMP_ALERT_SWITCH)), False))
        min_c = dps.get(str(int(DP.TEMP_MIN_CVALUE)))
        max_c = dps.get(str(int(DP.TEMP_MAX_CVALUE)))
        if min_c is not None:
            status.temp_min_c = float(min_c) / 10.0 if min_c > 100 else float(min_c)
        if max_c is not None:
            status.temp_max_c = float(max_c) / 10.0 if max_c > 100 else float(max_c)

        # Basic controls
        status.privacy_mode = bool(dps.get(str(int(DP.BASIC_PRIVATE)), False))
        status.night_vision = str(dps.get(str(int(DP.BASIC_NIGHTVISION)), "0"))
        status.indicator_light = bool(dps.get(str(int(DP.BASIC_INDICATOR)), True))
        status.image_flipped = bool(dps.get(str(int(DP.BASIC_FLIP)), False))

        # Detections
        status.motion_switch = bool(dps.get(str(int(DP.MOTION_SWITCH)), False))
        status.motion_tracking = bool(dps.get(str(int(DP.MOTION_TRACKING)), False))
        status.motion_sensitivity = str(dps.get(str(int(DP.MOTION_SENSITIVITY)), "1"))
        status.sound_switch = bool(dps.get(str(int(DP.DECIBEL_SWITCH)), False))
        status.sound_sensitivity = str(dps.get(str(int(DP.DECIBEL_SENSITIVITY)), "1"))
        status.cry_detection_switch = bool(dps.get(str(int(DP.CRY_DETECTION_SWITCH)), False))

        # Lullaby
        lullaby_ctrl = str(dps.get(str(int(DP.IPC_LULLABY_CONTROL)), "0"))
        status.lullaby_playing = (lullaby_ctrl == "1")
        status.lullaby_track = str(dps.get(str(int(DP.IPC_LULLABY_LIST)), "0"))
        status.lullaby_track_name = LULLABY_TRACKS.get(status.lullaby_track, f"Track {status.lullaby_track}")
        status.lullaby_volume = int(dps.get(str(int(DP.IPC_LULLABY_VOLUME)), 50))
        status.lullaby_mode = str(dps.get(str(int(DP.IPC_LULLABY_MODE)), "1"))

        # Volume & Storage
        status.master_volume = int(dps.get(str(int(DP.BASIC_DEVICE_VOLUME)), 50))
        status.sd_card_status = int(dps.get(str(int(DP.SD_STATUS)), 1))
        status.sd_storage = str(dps.get(str(int(DP.SD_STORAGE)), ""))

        return status

    def get_temperature(self) -> Tuple[Optional[float], Optional[float]]:
        """Return (celsius, fahrenheit) tuple of current room temperature."""
        status = self.get_status()
        return status.temperature_c, status.temperature_f

    def set_dp(self, dp_id: int | DP, value: Any) -> Dict[str, Any]:
        """Send a single DataPoint command to the camera."""
        dev = self._ensure_device()
        dp_key = int(dp_id)
        logger.debug("Setting DP %s = %r", dp_key, value)
        return dev.set_value(dp_key, value)

    def set_dps(self, dps_dict: Dict[int | DP | str, Any]) -> Dict[str, Any]:
        """Send multiple DataPoints in a single packet."""
        dev = self._ensure_device()
        formatted = {str(int(k)): v for k, v in dps_dict.items()}
        return dev.set_multiple_values(formatted)

    # --- Sensor & Alert Controls ---

    def set_temperature_alert(
        self,
        enabled: bool,
        min_c: Optional[float] = None,
        max_c: Optional[float] = None,
    ) -> None:
        """Configure temperature alarms (Celsius thresholds)."""
        payload: Dict[int, Any] = {DP.TEMP_ALERT_SWITCH: enabled}
        if min_c is not None:
            payload[DP.TEMP_MIN_CVALUE] = int(round(min_c * 10))
        if max_c is not None:
            payload[DP.TEMP_MAX_CVALUE] = int(round(max_c * 10))
        self.set_dps(payload)

    def set_motion_detection(
        self,
        enabled: bool,
        sensitivity: Optional[Sensitivity | str] = None,
        tracking: Optional[bool] = None,
    ) -> None:
        """Configure motion detection."""
        payload: Dict[int, Any] = {DP.MOTION_SWITCH: enabled}
        if sensitivity is not None:
            val = sensitivity.value if isinstance(sensitivity, Sensitivity) else str(sensitivity)
            payload[DP.MOTION_SENSITIVITY] = val
        if tracking is not None:
            payload[DP.MOTION_TRACKING] = tracking
        self.set_dps(payload)

    def set_sound_detection(
        self,
        enabled: bool,
        sensitivity: Optional[Sensitivity | str] = None,
    ) -> None:
        """Configure sound / noise detection."""
        payload: Dict[int, Any] = {DP.DECIBEL_SWITCH: enabled}
        if sensitivity is not None:
            val = sensitivity.value if isinstance(sensitivity, Sensitivity) else str(sensitivity)
            payload[DP.DECIBEL_SENSITIVITY] = val
        self.set_dps(payload)

    def set_cry_detection(self, enabled: bool) -> None:
        """Turn baby cry detection on or off."""
        self.set_dp(DP.CRY_DETECTION_SWITCH, enabled)

    # --- Camera Controls (PTZ, Night Vision, Privacy) ---

    def ptz_move(self, direction: PtzDirection | str) -> None:
        """Start pan/tilt rotation (direction: '0'=up, '2'=right, '4'=down, '6'=left)."""
        val = direction.value if isinstance(direction, PtzDirection) else str(direction)
        self.set_dp(DP.PTZ_CONTROL, val)

    def ptz_stop(self) -> None:
        """Stop camera movement."""
        self.set_dp(DP.PTZ_STOP, True)

    def ptz_step(self, direction: PtzDirection | str, duration_seconds: float = 0.5) -> None:
        """Rotate camera in a direction for a short duration and then stop."""
        self.ptz_move(direction)
        time.sleep(duration_seconds)
        self.ptz_stop()

    def set_night_vision(self, mode: NightVisionMode | str) -> None:
        """Set Infrared Night Vision (0=auto, 1=off, 2=on)."""
        val = mode.value if isinstance(mode, NightVisionMode) else str(mode)
        self.set_dp(DP.BASIC_NIGHTVISION, val)

    def set_privacy_mode(self, enabled: bool) -> None:
        """Turn Privacy / Sleep mode on (screen and video stream turn off)."""
        self.set_dp(DP.BASIC_PRIVATE, enabled)

    def set_indicator_light(self, enabled: bool) -> None:
        """Toggle the camera front LED status light."""
        self.set_dp(DP.BASIC_INDICATOR, enabled)

    def set_flip(self, enabled: bool) -> None:
        """Flip video 180 degrees (e.g., if mounted upside down on a crib)."""
        self.set_dp(DP.BASIC_FLIP, enabled)

    def set_volume(self, volume_percent: int) -> None:
        """Set master speaker volume (1-100)."""
        vol = max(1, min(100, volume_percent))
        self.set_dp(DP.BASIC_DEVICE_VOLUME, vol)

    # --- Lullaby Controls ---

    def play_lullaby(
        self,
        track_id: int | str = 0,
        volume: Optional[int] = None,
        repeat_all: bool = True,
    ) -> None:
        """Play soothing sounds / lullabies on monitor speaker."""
        track_str = str(track_id)
        payload: Dict[int, Any] = {
            DP.IPC_LULLABY_LIST: track_str,
            DP.IPC_LULLABY_CONTROL: "1",
            DP.IPC_LULLABY_MODE: "1" if repeat_all else "0",
        }
        if volume is not None:
            payload[DP.IPC_LULLABY_VOLUME] = max(0, min(100, volume))
        self.set_dps(payload)

    def stop_lullaby(self) -> None:
        """Stop playing lullaby."""
        self.set_dps({
            DP.IPC_LULLABY_CONTROL: "0",
            DP.IPC_LULLABY_MODE: "1",
        })

    def set_lullaby_volume(self, volume: int) -> None:
        """Change lullaby playback volume (0-100)."""
        self.set_dp(DP.IPC_LULLABY_VOLUME, max(0, min(100, volume)))

    # --- Live Video Streaming (P2P Engine) ---

    async def async_stream_frames(self) -> AsyncIterator[bytes]:
        """Yield live JPEG video frames over native Tuya P2P connection."""
        from tuya_ipc_p2p_sdk import TuyaIpcP2pClient

        if not self.stream_email or not self.stream_password:
            raise ValueError(
                "Streaming requires stream_account_email and stream_account_password."
            )

        async with TuyaIpcP2pClient(
            username=self.stream_email,
            password=self.stream_password,
            country_code=self.stream_country_code,
            region=self.stream_region,
        ) as client:
            stream = client.create_camera_stream(self.device_id, self.local_key)
            await stream.async_start()
            try:
                async for frame in stream.async_frames():
                    yield frame
            finally:
                await stream.async_stop()
