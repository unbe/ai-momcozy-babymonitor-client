"""Command-Line Interface for Momcozy BM04 Baby Monitor."""

from __future__ import annotations

import os

# Disable aiohttp sendfile to prevent Python asyncio sslproto _set_app_protocol crash on client disconnect over HTTPS
os.environ.setdefault("AIOHTTP_NOSENDFILE", "1")

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from .cloud import DEFAULT_CONFIG_PATH, MomcozyCloudApi, normalize_config_path
from .constants import LULLABY_TRACKS, NightVisionMode, PtzDirection, Sensitivity
from .device import MomcozyMonitor
from .stream import VideoStreamer
from .web import WebServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("momcozy-cli")


def _get_configured_device(args: argparse.Namespace) -> MomcozyMonitor:
    """Resolve device credentials from CLI arguments or saved config file."""
    raw_cfg = args.config if hasattr(args, "config") and args.config else DEFAULT_CONFIG_PATH
    config_path = normalize_config_path(raw_cfg)
    saved = {}
    if config_path.exists():
        try:
            saved = json.loads(config_path.read_text())
        except Exception:
            pass

    dev_id = getattr(args, "device_id", None) or os.environ.get("MOMCOZY_DEVICE_ID") or saved.get("device_id")
    local_key = getattr(args, "local_key", None) or os.environ.get("MOMCOZY_LOCAL_KEY") or saved.get("local_key")
    ip = getattr(args, "ip", None) or os.environ.get("MOMCOZY_IP") or saved.get("ip")

    # If dev_id is an app client UUID (36 chars with hyphens), auto-resolve to real camera ID
    if (not dev_id) or (dev_id and len(dev_id) == 36 and "-" in dev_id):
        api = MomcozyCloudApi.load_session(config_path)
        if api and api.token:
            try:
                res = api.query_devices()
                dev_list = res.get("deviceList") if isinstance(res, dict) else []
                if dev_list:
                    first = dev_list[0]
                    ext_id = (first.get("metadata") or {}).get("extDeviceId") or first.get("sn", "").replace("BM04-", "")
                    if ext_id:
                        dev_id = ext_id
                        saved["client_install_id"] = saved.get("device_id")
                        saved["device_id"] = dev_id
                        saved["camera_name"] = first.get("deviceName", "BM04")
                        if first.get("mac"):
                            saved["mac"] = first.get("mac")
                        config_path.write_text(json.dumps(saved, indent=2))
            except Exception as e:
                logger.debug("Could not auto-resolve device ID from cloud: %s", e)

    # Persist IP if explicitly passed via CLI
    if getattr(args, "ip", None) and config_path.exists():
        saved["ip"] = args.ip
        try:
            config_path.write_text(json.dumps(saved, indent=2))
        except Exception:
            pass

    # Log local IP resolution source
    if getattr(args, "ip", None):
        logger.info("Local IP determined from CLI flag (--ip %s)", ip)
    elif os.environ.get("MOMCOZY_IP"):
        logger.info("Local IP determined from MOMCOZY_IP environment variable: %s", ip)
    elif saved.get("ip"):
        logger.info("Local IP determined from config file %s: %s", config_path, ip)
    else:
        logger.info(
            "Local IP determination: No local IP specified in CLI, MOMCOZY_IP env, or config (%s).",
            config_path,
        )

    # Attempt local network auto-discovery if IP is missing
    if not ip and dev_id:
        logger.info("Scanning local network for camera %s via UDP broadcast...", dev_id)
        try:
            import tinytuya
            devices = tinytuya.deviceScan(False, 6)
            for dev_ip, dev_info in devices.items():
                if dev_info.get("gwId") == dev_id or dev_info.get("id") == dev_id:
                    ip = dev_ip
                    logger.info("Discovered camera %s on local network at %s", dev_id, ip)
                    saved["ip"] = ip
                    if config_path.exists():
                        try:
                            config_path.write_text(json.dumps(saved, indent=2))
                            logger.info("Saved discovered IP %s to %s", ip, config_path)
                        except Exception:
                            pass
                    break
        except Exception as scan_err:
            logger.debug("Local network scan encountered error: %s", scan_err)

        if not ip:
            logger.warning(
                "Camera %s was NOT discovered on local network via UDP broadcast.\n"
                "  • Reason: UDP broadcasts (ports 6666/6667) do not cross different subnets/VLANs or Docker bridge networks.\n"
                "  • How to enable local control: Provide your camera's LAN IP address directly using:\n"
                "      - Environment variable: MOMCOZY_IP=<CAMERA_IP>\n"
                "      - Or in %s: {\"ip\": \"<CAMERA_IP>\"}\n"
                "      - Or CLI flag: --ip <CAMERA_IP>\n"
                "  • Device control will fall back to Tuya Cloud Gateway.",
                dev_id,
                config_path,
            )

    # Auto-resolve local_key from Tuya Cloud if missing
    if not local_key and config_path.exists():
        try:
            api = MomcozyCloudApi.load_session(config_path)
            if api and api.tuya_creds and api.tuya_creds.token:
                logger.info("Retrieving Tuya encryption key for device %s from Tuya Cloud...", dev_id)
                synced = api.sync_devices_and_keys(config_path, raise_on_error=True)
                try:
                    saved = json.loads(config_path.read_text())
                except Exception:
                    pass
                if dev_id and dev_id in synced and synced[dev_id].get("local_key"):
                    local_key = synced[dev_id]["local_key"]
                    logger.info("Retrieved and saved Tuya local encryption key for %s", dev_id)
                else:
                    found_key = next((item["local_key"] for item in synced.values() if item.get("local_key")), None)
                    if found_key:
                        local_key = found_key
                        logger.info("Retrieved and saved Tuya local encryption key")
                    else:
                        logger.warning(
                            "Tuya Cloud did not return an encryption key for device %s. "
                            "If your camera requires a local key, pass --local-key <KEY>.",
                            dev_id,
                        )
            elif api and (not api.tuya_creds or not api.tuya_creds.token):
                logger.warning(
                    "No Tuya cloud credentials found in session (%s). "
                    "Run './momcozy.py login' to link your Tuya credentials.",
                    config_path,
                )
        except Exception as e:
            logger.warning("Failed to auto-resolve Tuya local key from cloud: %s", e)

    if not dev_id:
        print(
            "Error: Device ID required. Run 'momcozy login' first or pass --device-id and --local-key.",
            file=sys.stderr,
        )
        sys.exit(1)

    tuya_cfg = saved.get("tuya") if isinstance(saved.get("tuya"), dict) else {}
    tuya_reg = tuya_cfg.get("region")
    tuya_uid = tuya_cfg.get("uid")
    tuya_token = tuya_cfg.get("token")
    account_reg = saved.get("region")
    stream_reg = "eu" if (str(tuya_reg) == "33" or account_reg == "EU") else "us"

    cc = saved.get("country_code", "1")
    phone_code_map = {"CH": "41", "DE": "49", "US": "1", "CA": "1", "GB": "44", "UK": "44", "FR": "33"}
    stream_cc = phone_code_map.get(str(cc).upper(), str(cc))

    import time as _time

    _dps_cache: dict = {"ts": 0.0, "dps": saved.get("dps") if isinstance(saved.get("dps"), dict) else {}, "session": None, "client": None}

    def _fetch_cloud_dps() -> dict:
        now = _time.time()
        if _dps_cache["dps"] and (now - _dps_cache["ts"] < 15.0):
            return _dps_cache["dps"]
        if not config_path.exists():
            return _dps_cache["dps"]
        api_inst = MomcozyCloudApi.load_session(config_path)
        if not api_inst or not api_inst.tuya_creds or not api_inst.tuya_creds.token:
            return _dps_cache["dps"]
        t_client = _dps_cache["client"] or api_inst.get_tuya_client()
        _dps_cache["client"] = t_client
        t_session = _dps_cache["session"]
        if t_session is None:
            t_session = t_client.login_with_uid(
                country_code=api_inst.tuya_creds.region,
                uid=api_inst.tuya_creds.uid,
                token=api_inst.tuya_creds.token,
            )
            _dps_cache["session"] = t_session
        try:
            info = t_client.get_device_info(t_session, dev_id, home_id=api_inst.tuya_home_id)
        except Exception as exc:
            logger.debug("Tuya session expired during get_device_info (%s), refreshing session...", exc)
            info = None

        if info is None:
            try:
                t_session = t_client.login_with_uid(
                    country_code=api_inst.tuya_creds.region,
                    uid=api_inst.tuya_creds.uid,
                    token=api_inst.tuya_creds.token,
                )
                _dps_cache["session"] = t_session
                info = t_client.get_device_info(t_session, dev_id, home_id=api_inst.tuya_home_id)
            except Exception as exc:
                logger.debug("Tuya session refresh failed: %s", exc)
                info = None

        if isinstance(info, dict) and info.get("dps"):
            _dps_cache["dps"] = info["dps"]
            _dps_cache["ts"] = now
        return _dps_cache["dps"]

    def _publish_cloud_dps(dps_dict: dict) -> bool:
        if not config_path.exists():
            return False
        api_inst = MomcozyCloudApi.load_session(config_path)
        if not api_inst or not api_inst.tuya_creds or not api_inst.tuya_creds.token:
            return False
        t_client = _dps_cache["client"] or api_inst.get_tuya_client()
        _dps_cache["client"] = t_client
        t_session = _dps_cache["session"]
        if t_session is None:
            t_session = t_client.login_with_uid(
                country_code=api_inst.tuya_creds.region,
                uid=api_inst.tuya_creds.uid,
                token=api_inst.tuya_creds.token,
            )
            _dps_cache["session"] = t_session
        try:
            return t_client.publish_dps(t_session, dev_id, dps_dict)
        except Exception as exc:
            logger.debug("Cloud publish_dps failed, refreshing session: %s", exc)
            t_session = t_client.login_with_uid(
                country_code=api_inst.tuya_creds.region,
                uid=api_inst.tuya_creds.uid,
                token=api_inst.tuya_creds.token,
            )
            _dps_cache["session"] = t_session
            return t_client.publish_dps(t_session, dev_id, dps_dict)

    def _on_ip_discovered(new_ip: str) -> None:
        if config_path.exists():
            try:
                data = json.loads(config_path.read_text())
                data["ip"] = new_ip
                config_path.write_text(json.dumps(data, indent=2))
                logger.info("Saved newly discovered IP %s to %s", new_ip, config_path)
            except Exception as e:
                logger.debug("Failed saving newly discovered IP to config: %s", e)

    return MomcozyMonitor(
        device_id=dev_id,
        local_key=local_key or "",
        ip=ip,
        stream_account_email=saved.get("email"),
        stream_account_password=os.environ.get("MOMCOZY_PASSWORD"),
        stream_country_code=stream_cc,
        stream_region=stream_reg,
        stream_tuya_uid=tuya_uid,
        stream_tuya_token=tuya_token,
        stream_tuya_region=str(tuya_reg) if tuya_reg else None,
        cloud_dps_provider=_fetch_cloud_dps if (tuya_uid and tuya_token) else None,
        cloud_dp_publisher=_publish_cloud_dps if (tuya_uid and tuya_token) else None,
        initial_dps=saved.get("dps") if isinstance(saved.get("dps"), dict) else None,
        on_ip_discovered=_on_ip_discovered,
    )


def cmd_login(args: argparse.Namespace) -> None:
    """Log in to Momcozy Cloud and save credentials."""
    email = args.email or input("Momcozy account email: ").strip()
    password = args.password or getpass.getpass("Password: ")
    country = args.country or "US"

    api = MomcozyCloudApi(country_code=country, region=args.region)
    print(f"Connecting to Momcozy Cloud ({api.base_url})...")
    try:
        api.login(email, password)
        config_path = normalize_config_path(args.config if args.config else DEFAULT_CONFIG_PATH)
        api.save_session(config_path)

        # Automatically discover user's baby monitor and sync Tuya keys
        try:
            synced = api.sync_devices_and_keys(config_path)
            if synced:
                for did, dinfo in synced.items():
                    key_tag = " [localKey retrieved]" if dinfo.get("local_key") else ""
                    print(f"  • Discovered Monitor: {dinfo.get('name', 'BM04')} (ID: {did}){key_tag}")
        except Exception as exc:
            logger.debug("Device discovery during login: %s", exc)

        print(f"✓ Login successful! Saved session to {config_path}")
        if api.tuya_creds:
            print(f"  • Tuya Region: {api.tuya_creds.region}")
            print(f"  • Tuya UID:    {api.tuya_creds.uid}")
    except Exception as e:
        print(f"Login failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_sync(args: argparse.Namespace) -> None:
    """Synchronize devices and local encryption keys from Momcozy & Tuya Cloud."""
    config_path = normalize_config_path(args.config if args.config else DEFAULT_CONFIG_PATH)
    api = MomcozyCloudApi.load_session(config_path)
    if not api or not api.token:
        print("Not logged in. Run './momcozy.py login' first.", file=sys.stderr)
        sys.exit(1)

    print("Connecting to Momcozy & Tuya Cloud to synchronize devices and local keys...")
    try:
        synced = api.sync_devices_and_keys(config_path, raise_on_error=True)
        if not synced:
            print("No devices found on your account.", file=sys.stderr)
            return

        print(f"\n✓ Successfully synchronized {len(synced)} device(s) (saved to {config_path}):\n")
        missing_keys = []
        for dev_id, info in synced.items():
            name = info.get("name", "BM04")
            key = info.get("local_key")
            mac = info.get("mac") or "Unknown"
            ip = info.get("ip") or "Discovered locally"
            print(f"  Device: {name} (ID: {dev_id})")
            print(f"    • Local Key:  {key if key else 'Not available on cloud'}")
            print(f"    • MAC:        {mac}")
            print(f"    • Network IP: {ip}")
            print()
            if not key:
                missing_keys.append(dev_id)

        if missing_keys:
            print(
                f"Warning: Cloud did not provide encryption keys for: {', '.join(missing_keys)}. "
                f"Pass --local-key if local encryption is required.",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"Sync failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_whoami(args: argparse.Namespace) -> None:
    """Show details about the currently logged-in Momcozy account."""
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    api = MomcozyCloudApi.load_session(config_path)
    if not api or not api.token:
        print("Not logged in. Run './momcozy.py login' first.", file=sys.stderr)
        sys.exit(1)

    print(f"Momcozy Account: {api.email}")
    print(f"  • User UID:     {api.user_uid}")
    print(f"  • Cloud Region: {api.region or 'Auto'} ({api.base_url})")
    print(f"  • Country:      {api.country_code} ({api.country_name})")

    try:
        info = api.get_user_info()
        name = f"{info.get('firstName', '')} {info.get('lastName', '')}".strip()
        if name:
            print(f"  • Name:         {name}")
        c_code = info.get("countryCode")
        c_name = info.get("countryName")
        if c_code or c_name:
            print(f"  • Profile:      {c_code} ({c_name})")
    except Exception as exc:
        logger.debug("Could not query live user profile: %s", exc)

    if api.tuya_creds:
        print(f"Tuya Cloud:")
        print(f"  • Tuya UID:     {api.tuya_creds.uid}")
        print(f"  • Tuya Region:  {api.tuya_creds.region} (Data Center Region)")
        if api.tuya_home_id:
            print(f"  • Tuya Home ID: {api.tuya_home_id}")


def cmd_devices(args: argparse.Namespace) -> None:
    """Dump the raw device list from the cloud (use to find device_id / local key)."""
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    api = MomcozyCloudApi.load_session(config_path)
    if not api or not api.token:
        print("Not logged in. Run 'momcozy login' first.", file=sys.stderr)
        sys.exit(1)
    try:
        print(json.dumps(api.query_devices(), indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_status(args: argparse.Namespace) -> None:
    """Print current monitor telemetry and settings."""
    monitor = _get_configured_device(args)
    if args.ip:
        monitor.set_ip(args.ip)

    try:
        status = monitor.get_status()
        if args.json:
            data = dict(status.__dict__)
            data["control_transport"] = monitor.control_transport
            data["control_mode"] = monitor.control_mode
            data["control_diagnostic"] = monitor.control_diagnostic
            data["stream_transport"] = "Cloud Relay (Tuya P2P)"
            print(json.dumps(data, indent=2, default=str))
        else:
            summary = status.summary()
            if "Stream Transport:" not in summary:
                summary += "  • Stream Transport:   Cloud Relay (Tuya P2P)\n"
            print(summary)
    except Exception as e:
        print(f"Error reading status: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_temperature(args: argparse.Namespace) -> None:
    """Quick command to print current room temperature."""
    monitor = _get_configured_device(args)
    c, f = monitor.get_temperature()
    if c is not None:
        if args.fahrenheit:
            print(f"{f:.1f}")
        else:
            print(f"{c:.1f}")
    else:
        print("N/A", file=sys.stderr)
        sys.exit(1)


def cmd_set(args: argparse.Namespace) -> None:
    """Change settings on the BM04 monitor."""
    monitor = _get_configured_device(args)

    if args.night_vision is not None:
        nv_map = {"auto": NightVisionMode.AUTO, "off": NightVisionMode.OFF, "on": NightVisionMode.ON}
        monitor.set_night_vision(nv_map[args.night_vision.lower()])
        print(f"✓ Night vision set to {args.night_vision}")

    if args.privacy is not None:
        enabled = args.privacy.lower() in ["on", "true", "1"]
        monitor.set_privacy_mode(enabled)
        print(f"✓ Privacy mode {'enabled' if enabled else 'disabled'}")

    if args.led is not None:
        enabled = args.led.lower() in ["on", "true", "1"]
        monitor.set_indicator_light(enabled)
        print(f"✓ Status LED {'enabled' if enabled else 'disabled'}")

    if args.flip is not None:
        enabled = args.flip.lower() in ["on", "true", "1"]
        monitor.set_flip(enabled)
        print(f"✓ 180° Video flip {'enabled' if enabled else 'disabled'}")

    if args.motion is not None:
        enabled = args.motion.lower() in ["on", "true", "1"]
        sens = args.motion_sensitivity
        monitor.set_motion_detection(enabled, sensitivity=sens)
        print(f"✓ Motion detection {'enabled' if enabled else 'disabled'}")

    if args.sound is not None:
        enabled = args.sound.lower() in ["on", "true", "1"]
        sens = args.sound_sensitivity
        monitor.set_sound_detection(enabled, sensitivity=sens)
        print(f"✓ Sound detection {'enabled' if enabled else 'disabled'}")

    if args.cry is not None:
        enabled = args.cry.lower() in ["on", "true", "1"]
        monitor.set_cry_detection(enabled)
        print(f"✓ Cry detection {'enabled' if enabled else 'disabled'}")

    if args.temp_alert is not None:
        enabled = args.temp_alert.lower() in ["on", "true", "1"]
        monitor.set_temperature_alert(enabled, min_c=args.temp_min, max_c=args.temp_max)
        print(f"✓ Temp alerts {'enabled' if enabled else 'disabled'}")

    if args.volume is not None:
        monitor.set_volume(args.volume)
        print(f"✓ Master volume set to {args.volume}%")


def cmd_ptz(args: argparse.Namespace) -> None:
    """Move camera pan/tilt."""
    monitor = _get_configured_device(args)
    dir_map = {
        "up": PtzDirection.UP,
        "down": PtzDirection.DOWN,
        "left": PtzDirection.LEFT,
        "right": PtzDirection.RIGHT,
    }
    action = args.action.lower()
    if action == "stop":
        monitor.ptz_stop()
        print("✓ Camera PTZ stopped")
    elif action in dir_map:
        dur = args.duration if args.duration is not None else 0.175
        if dur > 0:
            monitor.ptz_step(dir_map[action], duration_seconds=dur)
            print(f"✓ Moved {action} for {dur}s")
        else:
            monitor.ptz_move(dir_map[action])
            print(f"✓ Moving {action} (call 'ptz stop' to halt)")


def cmd_lullaby(args: argparse.Namespace) -> None:
    """Play or stop lullabies."""
    monitor = _get_configured_device(args)
    if args.action == "stop":
        monitor.stop_lullaby()
        print("✓ Lullaby stopped")
    elif args.action == "play":
        track_id = str(args.track)
        name = LULLABY_TRACKS.get(track_id, f"Track {track_id}")
        monitor.play_lullaby(track_id=track_id, volume=args.volume)
        print(f"✓ Playing '{name}' at volume {args.volume or 50}%")
    elif args.action == "list":
        print("Available Lullabies:")
        for tid, name in LULLABY_TRACKS.items():
            print(f"  [{tid}] {name}")


def cmd_snapshot(args: argparse.Namespace) -> None:
    """Take a still image snapshot."""
    monitor = _get_configured_device(args)
    streamer = VideoStreamer(
        device_id=monitor.device_id,
        local_key=monitor.local_key,
        email=monitor.stream_email or "",
        password=monitor.stream_password or "",
        country_code=monitor.stream_country_code,
        region=monitor.stream_region,
        tuya_uid=monitor.stream_tuya_uid,
        tuya_token=monitor.stream_tuya_token,
        tuya_region=monitor.stream_tuya_region,
    )
    print(f"Connecting to {monitor.device_id} to capture snapshot...")
    try:
        frame = asyncio.run(streamer.get_snapshot(timeout_seconds=args.timeout))
        out_path = Path(args.output)
        out_path.write_bytes(frame)
        print(f"✓ Snapshot saved to {out_path.resolve()} ({len(frame)} bytes)")
    except Exception as e:
        print(f"Error taking snapshot: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_record(args: argparse.Namespace) -> None:
    """Record an MP4 video clip."""
    monitor = _get_configured_device(args)
    streamer = VideoStreamer(
        device_id=monitor.device_id,
        local_key=monitor.local_key,
        email=monitor.stream_email or "",
        password=monitor.stream_password or "",
        country_code=monitor.stream_country_code,
        region=monitor.stream_region,
        tuya_uid=monitor.stream_tuya_uid,
        tuya_token=monitor.stream_tuya_token,
        tuya_region=monitor.stream_tuya_region,
    )
    print(f"Recording {args.duration}s clip from {monitor.device_id} to {args.output}...")
    try:
        saved_path = asyncio.run(
            streamer.record_clip(
                output_file=args.output,
                duration_seconds=args.duration,
                fps=args.fps,
            )
        )
        print(f"✓ Video recorded successfully to {saved_path.resolve()}")
    except Exception as e:
        print(f"Error recording video: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_serve(args: argparse.Namespace) -> None:
    """Run Web Dashboard and MJPEG stream server."""
    monitor = _get_configured_device(args)
    idle_timeout = float(
        getattr(args, "idle_timeout", None)
        if getattr(args, "idle_timeout", None) is not None
        else (os.environ.get("MOMCOZY_IDLE_TIMEOUT") or 300.0)
    )
    streamer = None
    if (monitor.stream_tuya_uid and monitor.stream_tuya_token) or (
        monitor.stream_email and monitor.stream_password
    ):
        streamer = VideoStreamer(
            device_id=monitor.device_id,
            local_key=monitor.local_key,
            email=monitor.stream_email or "",
            password=monitor.stream_password or "",
            country_code=monitor.stream_country_code,
            region=monitor.stream_region,
            tuya_uid=monitor.stream_tuya_uid,
            tuya_token=monitor.stream_tuya_token,
            tuya_region=monitor.stream_tuya_region,
            idle_timeout=idle_timeout,
        )

    ssl_context = None
    ssl_flag = getattr(args, "ssl", False) or bool(os.environ.get("MOMCOZY_SSL"))
    ssl_cert = getattr(args, "ssl_cert", None) or os.environ.get("MOMCOZY_SSL_CERT")
    ssl_key = getattr(args, "ssl_key", None) or os.environ.get("MOMCOZY_SSL_KEY")
    if ssl_flag or ssl_cert or ssl_key:
        from .web import create_ssl_context
        try:
            ssl_context = create_ssl_context(certfile=ssl_cert, keyfile=ssl_key)
        except Exception as e:
            print(f"Error configuring SSL: {e}", file=sys.stderr)
            sys.exit(1)

    http_port = getattr(args, "http_port", 8420)
    if not ssl_context:
        http_port = args.port
    elif os.environ.get("MOMCOZY_HTTP_PORT"):
        try:
            http_port = int(os.environ["MOMCOZY_HTTP_PORT"])
        except ValueError:
            pass

    timelapse_dir = getattr(args, "timelapse_dir", None) or os.environ.get("MOMCOZY_TIMELAPSE_DIR")
    server = WebServer(
        monitor=monitor,
        streamer=streamer,
        host=args.host,
        port=args.port,
        http_port=http_port,
        ssl_context=ssl_context,
        timelapse_dir=timelapse_dir,
    )
    if getattr(args, "sync_night_vision", None) is not None and server.timelapse:
        server._set_night_vision_sync(bool(args.sync_night_vision))
    proto = "https" if ssl_context else "http"
    display_host = "localhost" if args.host == "0.0.0.0" else args.host
    print(f"\n=======================================================")
    print(f" Momcozy BM04 Web Dashboard & Live Streaming Server")
    print(f" Dashboard:    {proto}://{display_host}:{args.port}")
    if ssl_context and http_port != args.port:
        print(f" Cast / HTTP:  http://{args.host}:{http_port}/hls/stream.m3u8")
    print(f" MJPEG Stream: {proto}://{args.host}:{args.port}/stream")
    print(f" Audio Stream: {proto}://{args.host}:{args.port}/audio")
    print(f" HLS Stream:   {proto}://{args.host}:{args.port}/hls/stream.m3u8")
    print(f" Snapshot:     {proto}://{args.host}:{args.port}/snapshot")
    print(f" Web Casting:  Supported (Google Cast Web Sender in browser)")
    print(f"=======================================================\n")
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\nServer stopped.")




def cmd_share(args: argparse.Namespace) -> None:
    config_target = normalize_config_path(args.config or DEFAULT_CONFIG_PATH)
    api = MomcozyCloudApi.load_session(config_target)
    if not api or not api.token:
        print("Error: You must run './momcozy.py login' first.", file=sys.stderr)
        sys.exit(1)

    dev_id = args.device_id
    if not dev_id:
        config_path = config_target
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text())
                dev_id = cfg.get("device_id")
            except Exception:
                pass
    if not dev_id:
        print("Error: No target device ID specified or found in config.", file=sys.stderr)
        sys.exit(1)

    if args.action == "add":
        print(f"Sharing device {dev_id} with {args.account}...")
        try:
            home_id = getattr(args, "home_id", None)
            res = api.add_device_share(args.account, tuya_dev_id=dev_id, country_code=args.country, home_id=home_id)
            print(f"✓ Successfully shared device with {args.account}!")
            print(f"  • Member Code: {res.get('member_code')}")
            print(f"  • Tuya UID:    {res.get('uid')}")
            print(f"  • Region:      {res.get('region')}")
            tuya_share = res.get("tuya_share") or {}
            if tuya_share:
                home_val = tuya_share.get("home_id")
                rel_val = tuya_share.get("relationId")
                detail = f"Home ID: {home_val}" if home_val else "active"
                if rel_val:
                    detail += f", Relation ID: {rel_val}"
                print(f"  • Tuya Share:  {detail}")
        except Exception as e:
            print(f"Error sharing device: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.action == "remove":
        print(f"Revoking share for member {args.member_code} on device {dev_id}...")
        try:
            api.delete_device_share(dev_id, args.member_code)
            print(f"✓ Successfully revoked share for {args.member_code}!")
        except Exception as e:
            print(f"Error revoking share: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.action == "search":
        print(f"Searching user {args.account}...")
        try:
            info = api.search_tuya_user(args.account, country_code=args.country)
            print("✓ Found user:")
            print(f"  • Member Code: {info.get('memberCode')}")
            print(f"  • Tuya UID:    {info.get('uid')}")
            print(f"  • Region:      {info.get('region')}")
        except Exception as e:
            print(f"Error searching user: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.action == "query":
        print(f"Querying emails for member codes: {args.member_codes}...")
        try:
            users = api.query_shared_emails(args.member_codes)
            if not users:
                print("No user details returned.")
            for u in users:
                print(f"  • Email: {u.get('email', 'N/A')}, Member: {u.get('memberCode')}, Phone: {u.get('phone', 'N/A')}")
        except Exception as e:
            print(f"Error querying users: {e}", file=sys.stderr)
            sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    description = """🍼 Momcozy BM04 Baby Monitor Client & Streaming Server

Control your Momcozy BM04/BM05 baby monitor locally over Wi-Fi or via cloud.
Features live video streaming (MJPEG/P2P), responsive PTZ pan/tilt controls,
real-time temperature and baby crying detection, and a modern Web UI."""

    epilog = """Examples:
  # 1. Authenticate and retrieve local encryption keys
  ./momcozy.py login
  ./momcozy.py sync

  # 2. View current room temperature & monitor telemetry
  ./momcozy.py status
  ./momcozy.py temperature -f

  # 3. Launch Web UI dashboard & live video stream (http://localhost:8421)
  ./momcozy.py serve

  # 4. Pan / tilt camera
  ./momcozy.py ptz left --duration 0.35
  ./momcozy.py ptz up --duration 0.5

  # 5. Capture snapshot or record video clip
  ./momcozy.py snapshot -o baby.jpg
  ./momcozy.py record -d 15 -o nursery.mp4

  # 6. Play soothing lullaby or white noise
  ./momcozy.py lullaby list
  ./momcozy.py lullaby play --track 4 --volume 60

  # 7. Configure camera features
  ./momcozy.py set --night-vision auto --led off --volume 70"""

    parser = argparse.ArgumentParser(
        prog="momcozy",
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="Path to credentials file (default: .momcozy.json next to script)")
    parser.add_argument("--device-id", help="Target device ID (overrides .momcozy.json)")
    parser.add_argument("--local-key", help="Device local 16-character AES encryption key (overrides config)")
    parser.add_argument("--ip", help="Device IP on local Wi-Fi (overrides config)")

    sub = parser.add_subparsers(
        dest="subcommand",
        title="Available Commands",
        metavar="COMMAND",
        required=True,
    )

    # login
    p_login = sub.add_parser(
        "login",
        help="Log in to Momcozy Cloud and link Tuya credentials",
        description="Authenticate with your Momcozy mobile account (email and password). Retrieves cloud authentication tokens and Tuya credentials needed for local device keys and video streaming.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_login.add_argument("--email", "-e", help="Momcozy account email address")
    p_login.add_argument("--password", "-p", help="Momcozy account password (prompted securely if omitted)")
    p_login.add_argument("--country", "-c", default="US", help="ISO2 country code (default: US, e.g. CH, DE, GB)")
    p_login.add_argument(
        "--region",
        "-r",
        choices=["NA", "EU", "AP", "CN"],
        default=None,
        help="Data center region (default: auto-detected from country; e.g. EU for Europe)",
    )
    p_login.set_defaults(func=cmd_login)

    # whoami
    p_whoami = sub.add_parser(
        "whoami",
        help="Show logged-in account, region, and profile info",
        description="Display current authenticated Momcozy account email, user ID, data center region, and linked Tuya Cloud credentials.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_whoami.set_defaults(func=cmd_whoami)

    # devices
    p_devices = sub.add_parser(
        "devices",
        help="List devices bound to your account (raw JSON)",
        description="Query Momcozy Cloud and dump the raw device list JSON, including hardware serial numbers, device names, and MAC addresses.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_devices.set_defaults(func=cmd_devices)

    # sync / keys
    p_sync = sub.add_parser(
        "sync",
        help="Synchronize device encryption keys & IP from cloud",
        description="Fetch camera local IP addresses and 16-character AES local encryption keys from Tuya Cloud, saving them to .momcozy.json for local LAN control.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_sync.set_defaults(func=cmd_sync)

    p_keys = sub.add_parser(
        "keys",
        help="Alias for 'sync' - fetch device encryption keys",
        description="Alias for 'sync'. Retrieves local encryption keys and device metadata from Tuya Cloud.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_keys.set_defaults(func=cmd_sync)

    # status
    p_status = sub.add_parser(
        "status",
        help="Show all monitor measurements, alerts, and settings",
        description="Display real-time environmental telemetry (room temperature, baby crying, motion, sound detection) and camera settings (night vision, privacy, volume).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_status.add_argument("--json", action="store_true", help="Output telemetry in raw JSON format (useful for scripts & Home Assistant)")
    p_status.set_defaults(func=cmd_status)

    # temperature
    p_temp = sub.add_parser(
        "temperature",
        help="Print current room temperature",
        description="Quickly read the baby's room temperature from the monitor's environmental sensor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_temp.add_argument("-f", "--fahrenheit", action="store_true", help="Output temperature in Fahrenheit (°F) instead of Celsius (°C)")
    p_temp.set_defaults(func=cmd_temperature)

    # set
    p_set = sub.add_parser(
        "set",
        help="Change monitor settings and thresholds",
        description="Configure camera features, alert toggles, sensitivity levels, and volume on the monitor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_set.add_argument("--night-vision", choices=["auto", "off", "on"], help="IR night vision mode ('auto': automatic IR-cut, 'off': always off, 'on': always on)")
    p_set.add_argument("--privacy", choices=["on", "off"], help="Privacy/sleep mode (turns off lens and disables streaming)")
    p_set.add_argument("--led", choices=["on", "off"], help="Front status LED light indicator")
    p_set.add_argument("--flip", choices=["on", "off"], help="Rotate video 180° (for upside-down crib or ceiling mounts)")
    p_set.add_argument("--motion", choices=["on", "off"], help="Motion detection alerts")
    p_set.add_argument("--motion-sensitivity", choices=["0", "1", "2"], help="Motion sensitivity ('0': Low, '1': Medium, '2': High)")
    p_set.add_argument("--sound", choices=["on", "off"], help="Sound & noise detection alerts")
    p_set.add_argument("--sound-sensitivity", choices=["0", "1", "2"], help="Sound sensitivity ('0': Low, '1': Medium, '2': High)")
    p_set.add_argument("--cry", choices=["on", "off"], help="Baby crying detection alerts")
    p_set.add_argument("--temp-alert", choices=["on", "off"], help="Temperature out-of-range alarm notification")
    p_set.add_argument("--temp-min", type=float, help="Minimum temperature threshold in Celsius (e.g. 18.0)")
    p_set.add_argument("--temp-max", type=float, help="Maximum temperature threshold in Celsius (e.g. 26.0)")
    p_set.add_argument("--volume", type=int, help="Camera speaker master volume (1-100%%)")
    p_set.set_defaults(func=cmd_set)

    # ptz
    p_ptz = sub.add_parser(
        "ptz",
        help="Pan and tilt camera motor",
        description="Move the camera's motorized pan and tilt head in four directions or halt movement.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_ptz.add_argument("action", choices=["up", "down", "left", "right", "stop"], help="PTZ action ('up', 'down', 'left', 'right', or 'stop')")
    p_ptz.add_argument("--duration", "-d", type=float, default=0.175, help="Step movement duration in seconds (default: 0.175; continuous if 0)")
    p_ptz.set_defaults(func=cmd_ptz)

    # lullaby
    p_lullaby = sub.add_parser(
        "lullaby",
        help="Play/stop soothing lullabies and sounds",
        description="Control lullabies and ambient sound tracks (White Noise, Heartbeat, Rainforest, Brahms' Lullaby) on the monitor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_lullaby.add_argument("action", choices=["play", "stop", "list"], help="Action: 'play' to start playback, 'stop' to halt, 'list' to view available tracks")
    p_lullaby.add_argument(
        "--track",
        "-t",
        default="0",
        help="Track ID to play (0: Brahms, 1: Hush Baby, 2: London Bridge, 3: Twinkle, 4: White Noise, 5: Heartbeat, 6: Rainforest, 7: Stream; default: 0)",
    )
    p_lullaby.add_argument("--volume", "-v", type=int, default=50, help="Lullaby playback volume percentage (0-100; default: 50)")
    p_lullaby.set_defaults(func=cmd_lullaby)

    # snapshot
    p_snap = sub.add_parser(
        "snapshot",
        help="Capture a still JPEG image from video stream",
        description="Capture a single 1080p JPEG still frame directly from the camera's live P2P video stream.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_snap.add_argument("-o", "--output", default="snapshot.jpg", help="Output file path for saved JPEG image (default: snapshot.jpg)")
    p_snap.add_argument("-t", "--timeout", type=float, default=10.0, help="Timeout in seconds waiting for video stream frame (default: 10.0)")
    p_snap.set_defaults(func=cmd_snapshot)

    # record
    p_rec = sub.add_parser(
        "record",
        help="Record an MP4 video clip from live stream",
        description="Record a live video clip from the camera and encode it to an MP4 video file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_rec.add_argument("-o", "--output", default="recording.mp4", help="Output file path for MP4 video (default: recording.mp4)")
    p_rec.add_argument("-d", "--duration", type=int, default=10, help="Recording duration in seconds (default: 10)")
    p_rec.add_argument("--fps", type=int, default=15, help="Video recording frame rate (default: 15)")
    p_rec.set_defaults(func=cmd_record)

    # serve
    p_serve = sub.add_parser(
        "serve",
        help="Start Web UI dashboard & MJPEG video server",
        description="Launch an embedded HTTP server hosting the interactive browser dashboard, MJPEG live video feed (/stream), and snapshot endpoint (/snapshot).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_serve.add_argument("--host", default="0.0.0.0", help="Network interface host IP to bind (default: 0.0.0.0, accessible on LAN)")
    p_serve.add_argument("--port", "-p", type=int, default=8421, help="TCP port to listen on (default: 8421)")
    p_serve.add_argument(
        "--idle-timeout",
        type=float,
        default=300.0,
        help="Inactivity timeout in seconds before disconnecting cloud video stream when no clients are watching (default: 300 = 5 min, 0 to disable)",
    )
    p_serve.add_argument("--ssl", action="store_true", help="Enable HTTPS (auto-generates a self-signed certificate if none provided)")
    p_serve.add_argument("--ssl-cert", help="Path to custom SSL certificate PEM file for HTTPS")
    p_serve.add_argument("--ssl-key", help="Path to custom SSL private key PEM file for HTTPS")
    p_serve.add_argument(
        "--http-port",
        type=int,
        default=8420,
        help="Plain HTTP port for Chromecast and LAN streaming when SSL is enabled (default: 8420)",
    )
    p_serve.add_argument(
        "--sync-night-vision",
        action="store_true",
        default=None,
        help="Enable automatic timelapse synchronization with camera Night Vision",
    )
    p_serve.add_argument(
        "--timelapse-dir",
        default=None,
        help="Directory to store circular timelapse ring buffer recordings (default: $MOMCOZY_TIMELAPSE_DIR or 'timelapse')",
    )
    p_serve.set_defaults(func=cmd_serve)

    # share
    p_share = sub.add_parser(
        "share",
        help="Share camera access with another Momcozy account",
        description="Manage camera sharing with family members and other Momcozy user accounts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    share_sub = p_share.add_subparsers(
        dest="action",
        title="Share Commands",
        metavar="ACTION",
        required=True,
    )

    # share add <account>
    p_sadd = share_sub.add_parser(
        "add",
        help="Share device with a user by email",
        description="Grant camera viewing and control access to another Momcozy account by email.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_sadd.add_argument("account", help="Recipient email or Momcozy account username")
    p_sadd.add_argument("--country", "-c", help="Recipient ISO2 country code (default: your account country)")
    p_sadd.add_argument("--home-id", type=int, help="Optional Tuya Home/Group ID (automatically resolved if omitted)")

    # share remove <member_code>
    p_srem = share_sub.add_parser(
        "remove",
        help="Revoke device access for a member code",
        description="Revoke device access for an existing shared family member using their member code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_srem.add_argument("member_code", help="Member code of the shared user to revoke")

    # share search <account>
    p_ssearch = share_sub.add_parser(
        "search",
        help="Search for a Momcozy user to get their member code",
        description="Query the Tuya/Momcozy user directory to look up a user's member code and account details by email.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_ssearch.add_argument("account", help="Email or account username to look up")
    p_ssearch.add_argument("--country", "-c", help="ISO2 country code of recipient (e.g. US, CH, DE)")

    # share query <member_code...>
    p_squery = share_sub.add_parser(
        "query",
        help="Look up email addresses for member codes",
        description="Resolve member codes back to their associated email addresses and phone numbers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_squery.add_argument("member_codes", nargs="+", help="One or more member codes to resolve")
    p_share.set_defaults(func=cmd_share)

    return parser


def main() -> None:
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
