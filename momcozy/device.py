"""Momcozy BM04 Baby Monitor Client.

Controls the BM04 monitor via local network (LAN) and/or P2P cloud streaming.
Provides high-level methods to read sensors (temperature, motion, crying, noise),
adjust settings (night vision, PTZ, volume, alarms), and stream live video.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, Optional, Tuple

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
    control_transport: str = ""
    control_diagnostic: Optional[str] = None

    # Latest detection event (DP 185)
    latest_event_cmd: str = ""
    latest_event_name: str = ""
    latest_event_time: Optional[int] = None
    latest_event_time_str: str = ""
    latest_event_bucket: str = ""
    latest_event_file: str = ""
    latest_event_key: str = ""

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
        transport_line = f"  • Control Transport:  {self.control_transport}\n" if self.control_transport else ""
        diag_line = f"  • Control Diagnostic: {self.control_diagnostic}\n" if (self.control_diagnostic and "Local LAN" not in (self.control_transport or "")) else ""

        return (
            f"--- Momcozy BM04 Baby Monitor Status ---\n"
            f"{transport_line}"
            f"{diag_line}"
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
        stream_tuya_uid: Optional[str] = None,
        stream_tuya_token: Optional[str] = None,
        stream_tuya_region: Optional[str] = None,
        cloud_dps_provider: Optional[Any] = None,
        cloud_dp_publisher: Optional[Callable[[Dict[str, Any]], bool]] = None,
        initial_dps: Optional[Dict[str, Any]] = None,
        on_ip_discovered: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.device_id = device_id
        self.local_key = local_key
        self.ip = ip
        self.version = version
        self.on_ip_discovered = on_ip_discovered

        # Video stream account details
        self.stream_email = stream_account_email
        self.stream_password = stream_account_password
        self.stream_country_code = stream_country_code
        self.stream_region = stream_region
        self.stream_tuya_uid = stream_tuya_uid
        self.stream_tuya_token = stream_tuya_token
        self.stream_tuya_region = stream_tuya_region

        self.cloud_dps_provider = cloud_dps_provider
        self.cloud_dp_publisher = cloud_dp_publisher
        self._cached_dps: Dict[str, Any] = dict(initial_dps) if initial_dps else {}
        self._lan_tcp_unavailable: bool = False
        self._lan_status_unsupported: bool = False
        self._lock = threading.RLock()

        self._device: Optional[tinytuya.Device] = None
        self.control_diagnostic: str = ""
        self._init_local_device()

    def test_local_lan_connection(self, allow_rediscovery: bool = True) -> bool:
        """Test direct TCP connection to port 6668 on the camera IP."""
        if not self.ip:
            if allow_rediscovery and self.device_id:
                discovered_ip = self.discover_local_ip()
                if discovered_ip:
                    return self.test_local_lan_connection(allow_rediscovery=False)
            self.control_diagnostic = (
                f"No local camera IP configured for device {self.device_id}. "
                "Specify 'ip' in .momcozy.json or set MOMCOZY_IP environment variable."
            )
            return False
        logger.info("Checking local LAN connection to camera at %s:6668...", self.ip)
        try:
            with socket.create_connection((self.ip, 6668), timeout=1.5):
                self._lan_tcp_unavailable = False
                self.control_diagnostic = f"Local LAN TCP port 6668 reachable at {self.ip}"
                logger.info("✓ Local LAN control active: connected to camera at %s:6668", self.ip)
                return True
        except Exception as exc:
            if allow_rediscovery and self.device_id:
                old_ip = self.ip
                logger.info(
                    "✗ Camera not reachable at %s:6668 (%s). Scanning local network via UDP broadcast...",
                    old_ip,
                    exc,
                )
                discovered_ip = self.discover_local_ip()
                if discovered_ip and discovered_ip != old_ip:
                    logger.info("✓ Camera rediscovered at new IP %s. Retesting connection...", discovered_ip)
                    self.set_ip(discovered_ip, test_connection=False)
                    return self.test_local_lan_connection(allow_rediscovery=False)

            self._lan_tcp_unavailable = True
            self.control_diagnostic = (
                f"Local LAN TCP port 6668 unreachable on {self.ip} ({exc}). "
                "Device control will fall back to Tuya Cloud Gateway."
            )
            logger.warning(
                "✗ Local LAN connection to %s:6668 failed (%s). "
                "Device control will fall back to Tuya Cloud Gateway.",
                self.ip,
                exc,
            )
            return False

    def _init_local_device(self) -> None:
        if self.ip:
            self._device = tinytuya.Device(
                dev_id=self.device_id,
                address=self.ip,
                local_key=self.local_key,
                version=self.version,
            )
            self._device.set_socketPersistent(True)
            self._device.set_socketTimeout(1.0)
            self.test_local_lan_connection()
        else:
            self.control_diagnostic = (
                f"No local IP address configured for camera {self.device_id}. "
                "Specify 'ip' in .momcozy.json or set MOMCOZY_IP environment variable."
            )
            logger.info("Local LAN control: %s", self.control_diagnostic)

    @property
    def control_mode(self) -> str:
        """Control mode: 'local_lan' or 'cloud'."""
        return "local_lan" if (self.ip and not self._lan_tcp_unavailable) else "cloud"

    @property
    def control_transport(self) -> str:
        """Human-readable description of device control transport."""
        if self.ip and not self._lan_tcp_unavailable:
            return f"Local LAN ({self.ip}:6668)"
        if self.ip and self._lan_tcp_unavailable:
            return f"Tuya Cloud Gateway (Local LAN {self.ip}:6668 unreachable)"
        return "Tuya Cloud Gateway (No local IP configured)"

    def set_ip(self, ip: str, test_connection: bool = True) -> None:
        """Update monitor IP address."""
        self.ip = ip
        if self.ip:
            self._device = tinytuya.Device(
                dev_id=self.device_id,
                address=self.ip,
                local_key=self.local_key,
                version=self.version,
            )
            self._device.set_socketPersistent(True)
            self._device.set_socketTimeout(1.0)
            if test_connection:
                self.test_local_lan_connection(allow_rediscovery=False)

    def discover_local_ip(self, timeout: int = 6) -> Optional[str]:
        """Scan the local network for this device IP."""
        logger.info("Scanning local subnet for device %s via UDP broadcast...", self.device_id)
        try:
            # Tuya cameras broadcast UDP status beacons every 4-5 seconds.
            # Using 6 seconds ensures we comfortably catch at least one broadcast cycle.
            devices = tinytuya.deviceScan(False, timeout)
            for dev_ip, dev_info in devices.items():
                if dev_info.get("gwId") == self.device_id or dev_info.get("id") == self.device_id:
                    logger.info("✓ Discovered camera %s on local network at %s", self.device_id, dev_ip)
                    self.set_ip(dev_ip, test_connection=False)
                    if self.on_ip_discovered:
                        try:
                            self.on_ip_discovered(dev_ip)
                        except Exception as save_err:
                            logger.debug("on_ip_discovered callback error: %s", save_err)
                    return dev_ip
            logger.warning(
                "✗ Camera %s was not found in local UDP scan. "
                "(Note: UDP broadcast discovery cannot cross subnets/VLANs or Docker bridge networks unless host networking is enabled).",
                self.device_id,
            )
            self.control_diagnostic = f"Camera {self.device_id} not found via UDP broadcast on local network."
        except Exception as exc:
            logger.warning("Local scan error: %s", exc)
            self.control_diagnostic = f"Local UDP scan error: {exc}"
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
                    f"(Or set MOMCOZY_IP=<CAMERA_IP>)"
                )
        return self._device

    def _parse_dps(self, dps: Dict[str, Any]) -> BM04Status:
        """Parse a raw Tuya DataPoint dictionary into a structured BM04Status."""
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
        status.sound_switch = bool(
            dps.get(str(int(DP.DECIBEL_SWITCH)))
            if dps.get(str(int(DP.DECIBEL_SWITCH))) is not None
            else dps.get(str(int(DP.DECIBEL_SWITCH_ALT)), False)
        )
        status.sound_sensitivity = str(dps.get(str(int(DP.DECIBEL_SENSITIVITY)), "1"))
        status.cry_detection_switch = bool(dps.get(str(int(DP.CRY_DETECTION_SWITCH)), False))

        # Event Detection Snapshot metadata (DP 185)
        raw_185 = dps.get("185") or dps.get(str(int(DP.MOVEMENT_DETECT_PIC)))
        if raw_185:
            try:
                if isinstance(raw_185, str):
                    decoded_bytes = base64.b64decode(raw_185)
                    info_185 = json.loads(decoded_bytes.decode("utf-8"))
                elif isinstance(raw_185, dict):
                    info_185 = raw_185
                else:
                    info_185 = {}

                cmd = info_185.get("cmd", "")
                status.latest_event_cmd = cmd
                cmd_map = {
                    "ipc_bang": "Sound / Cry Alert",
                    "ipc_motion": "Motion Alert",
                    "ipc_human": "Human Presence",
                    "ipc_crying": "Baby Crying",
                    "ipc_alarm": "Alarm Event",
                }
                status.latest_event_name = cmd_map.get(cmd, cmd or "Camera Detection Alert")
                status.latest_event_bucket = info_185.get("bucket", "")

                files = info_185.get("files", [])
                if files and isinstance(files, list) and len(files) > 0:
                    first_file = files[0]
                    if isinstance(first_file, (list, tuple)) and len(first_file) >= 1:
                        status.latest_event_file = str(first_file[0])
                        if len(first_file) >= 2:
                            status.latest_event_key = str(first_file[1])
                        # Extract timestamp from filename like /.../detect/1791828432.jpeg
                        fname = status.latest_event_file.split("/")[-1]
                        num_part = fname.split(".")[0]
                        if num_part.isdigit():
                            ts = int(num_part)
                            if ts > 10_000_000_000:
                                ts = ts // 1000
                            status.latest_event_time = ts
                            dt = datetime.fromtimestamp(ts)
                            status.latest_event_time_str = dt.strftime("%b %d, %Y %H:%M:%S")
            except Exception as exc:
                logger.debug("Could not parse DP 185: %s", exc)

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
        status.control_transport = self.control_transport
        status.control_diagnostic = self.control_diagnostic

        return status

    def get_status(self) -> BM04Status:
        """Query all DataPoints from the monitor and return a structured status."""
        dps: Dict[str, Any] = {}
        data: Dict[str, Any] = {}

        if (
            (self._device is not None or not self.cloud_dps_provider)
            and not self._lan_tcp_unavailable
            and not self._lan_status_unsupported
            and self.ip
        ):
            try:
                with self._lock:
                    dev = self._ensure_device()
                    data = dev.status() or {}
                dps = data.get("dps", {}) if isinstance(data, dict) else {}
                if not dps:
                    # BM04 IPC camera does not respond to DP_QUERY over local TCP port 6668.
                    # Flag as unsupported so future status polls don't hang on socket timeouts.
                    self._lan_status_unsupported = True
            except Exception as exc:
                logger.debug("Local TCP status query failed: %s", exc)
                self._lan_status_unsupported = True
                data = {"Error": str(exc)}

        if not dps and self.cloud_dps_provider:
            try:
                cloud_dps = self.cloud_dps_provider()
                if isinstance(cloud_dps, dict) and cloud_dps:
                    dps = dict(cloud_dps)
            except Exception as exc:
                logger.debug("Cloud DPS provider fallback failed: %s", exc)

        if not dps and self._cached_dps:
            dps = dict(self._cached_dps)

        if not dps:
            err = (data.get("Error") or data.get("Err")) if isinstance(data, dict) else None
            err = err or "No response from device"
            raise RuntimeError(
                f"Failed to communicate with device at {self.ip} (device ID: {self.device_id}): {err}.\n"
                f"  • {self.ip} did not return any Momcozy/Tuya monitor telemetry on port 6668.\n"
                f"  • Verify that {self.ip} is the IP address of your BM04 camera on your Wi-Fi (Sigmastar MAC: 50:7B:91:xx:xx:xx).\n"
                f"  • If the device requires a local encryption key, pass --local-key."
            )

        self._cached_dps = dict(dps)
        return self._parse_dps(dps)

    def get_temperature(self) -> Tuple[Optional[float], Optional[float]]:
        """Return (celsius, fahrenheit) tuple of current room temperature."""
        status = self.get_status()
        return status.temperature_c, status.temperature_f

    def set_dp(self, dp_id: int | DP, value: Any) -> Dict[str, Any]:
        """Send a single DataPoint command to the camera."""
        dp_key = int(dp_id)
        logger.debug("Setting DP %s = %r", dp_key, value)
        last_error = None

        if self.ip and not self._lan_tcp_unavailable:
            try:
                with self._lock:
                    dev = self._ensure_device()
                    res = dev.set_value(dp_key, value)
                    self._cached_dps[str(dp_key)] = value
                    return res or {"dps": {str(dp_key): value}}
            except Exception as exc:
                logger.warning("Local LAN set_dp failed for DP %s: %s", dp_key, exc)
                last_error = exc

        if self.cloud_dp_publisher:
            try:
                ok = self.cloud_dp_publisher({str(dp_key): value})
                if ok:
                    self._cached_dps[str(dp_key)] = value
                    return {"dps": {str(dp_key): value}, "cloud": True}
            except Exception as exc:
                logger.warning("Cloud DP publisher failed for DP %s: %s", dp_key, exc)
                last_error = exc

        if last_error:
            raise RuntimeError(f"Failed to set DP {dp_key} on device {self.device_id}: {last_error}")
        return {"dps": {str(dp_key): value}}

    def set_dps(self, dps_dict: Dict[int | DP | str, Any]) -> Dict[str, Any]:
        """Send multiple DataPoints in a single packet."""
        formatted = {str(int(k)): v for k, v in dps_dict.items()}
        last_error = None

        if self.ip and not self._lan_tcp_unavailable:
            try:
                with self._lock:
                    dev = self._ensure_device()
                    res = dev.set_multiple_values(formatted)
                    self._cached_dps.update(formatted)
                    return res or {"dps": formatted}
            except Exception as exc:
                logger.warning("Local LAN set_dps failed: %s", exc)
                last_error = exc

        if self.cloud_dp_publisher:
            try:
                ok = self.cloud_dp_publisher(formatted)
                if ok:
                    self._cached_dps.update(formatted)
                    return {"dps": formatted, "cloud": True}
            except Exception as exc:
                logger.warning("Cloud DP publisher failed: %s", exc)
                last_error = exc

        if last_error:
            raise RuntimeError(f"Failed to set DPS on device {self.device_id}: {last_error}")
        return {"dps": formatted}

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

    def ptz_step(self, direction: PtzDirection | str, duration_seconds: float = 0.175) -> None:
        """Rotate camera in a direction for a short duration and then stop (default: 0.175s)."""
        with self._lock:
            val = direction.value if isinstance(direction, PtzDirection) else str(direction)
            self.set_dp(DP.PTZ_CONTROL, val)
            time.sleep(duration_seconds)
            self.set_dp(DP.PTZ_STOP, True)

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
        from .stream import create_p2p_client

        if not (self.stream_tuya_uid and self.stream_tuya_token) and not (
            self.stream_email and self.stream_password
        ):
            raise ValueError(
                "Streaming requires Tuya UID/token or stream_account_email and stream_account_password."
            )

        async with create_p2p_client(
            email=self.stream_email or "",
            password=self.stream_password or "",
            country_code=self.stream_country_code,
            region=self.stream_region,
            tuya_uid=self.stream_tuya_uid,
            tuya_token=self.stream_tuya_token,
            tuya_region=self.stream_tuya_region,
        ) as client:
            stream = client.create_camera_stream(self.device_id, self.local_key)
            await stream.async_start()
            try:
                async for frame in stream.async_frames():
                    yield frame
            finally:
                await stream.async_stop()
