"""Command-Line Interface for Momcozy BM04 Baby Monitor."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from .cloud import DEFAULT_CONFIG_PATH, MomcozyCloudApi
from .constants import LULLABY_TRACKS, NightVisionMode, PtzDirection, Sensitivity
from .device import MomcozyMonitor
from .stream import VideoStreamer
from .web import WebServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("momcozy-cli")


def _get_configured_device(args: argparse.Namespace) -> MomcozyMonitor:
    """Resolve device credentials from CLI arguments or saved config file."""
    config_path = Path(args.config) if hasattr(args, "config") and args.config else DEFAULT_CONFIG_PATH
    saved = {}
    if config_path.exists():
        try:
            saved = json.loads(config_path.read_text())
        except Exception:
            pass

    dev_id = getattr(args, "device_id", None) or saved.get("device_id")
    local_key = getattr(args, "local_key", None) or saved.get("local_key")
    ip = getattr(args, "ip", None) or saved.get("ip")

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

    if not dev_id:
        print(
            "Error: Device ID required. Run 'momcozy login' first or pass --device-id and --local-key.",
            file=sys.stderr,
        )
        sys.exit(1)

    tuya_reg = saved.get("tuya", {}).get("region") if saved.get("tuya") else None
    account_reg = saved.get("region")
    stream_reg = "eu" if (tuya_reg == "33" or account_reg == "EU") else "us"

    cc = saved.get("country_code", "1")
    phone_code_map = {"CH": "41", "DE": "49", "US": "1", "CA": "1", "GB": "44", "UK": "44", "FR": "33"}
    stream_cc = phone_code_map.get(str(cc).upper(), str(cc))

    return MomcozyMonitor(
        device_id=dev_id,
        local_key=local_key or "",
        ip=ip,
        stream_account_email=saved.get("email"),
        stream_account_password=saved.get("stream_password"),
        stream_country_code=stream_cc,
        stream_region=stream_reg,
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
        # Store stream password in config file to enable P2P video stream
        config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
        api.save_session(config_path)

        # Automatically discover user's baby monitor
        try:
            dev_info = api.query_devices()
            dev_list = dev_info.get("deviceList") if isinstance(dev_info, dict) else []
            if dev_list:
                first = dev_list[0]
                ext_id = (first.get("metadata") or {}).get("extDeviceId") or first.get("sn", "").replace("BM04-", "")
                dev_name = first.get("deviceName", "BM04")
                mac = first.get("mac", "")
                data["client_install_id"] = data.get("device_id")
                data["device_id"] = ext_id
                data["camera_name"] = dev_name
                if mac:
                    data["mac"] = mac
                print(f"  • Discovered Monitor: {dev_name} (ID: {ext_id}, MAC: {mac})")
        except Exception as exc:
            logger.debug("Device discovery during login: %s", exc)

        config_path.write_text(json.dumps(data, indent=2))

        print(f"✓ Login successful! Saved session to {config_path}")
        if api.tuya_creds:
            print(f"  • Tuya Region: {api.tuya_creds.region}")
            print(f"  • Tuya UID:    {api.tuya_creds.uid}")
    except Exception as e:
        print(f"Login failed: {e}", file=sys.stderr)
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
            print(json.dumps(status.__dict__, indent=2, default=str))
        else:
            print(status.summary())
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
        if args.duration and args.duration > 0:
            monitor.ptz_step(dir_map[action], duration_seconds=args.duration)
            print(f"✓ Panned {action} for {args.duration}s")
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
    streamer = None
    if monitor.stream_email and monitor.stream_password:
        streamer = VideoStreamer(
            device_id=monitor.device_id,
            local_key=monitor.local_key,
            email=monitor.stream_email,
            password=monitor.stream_password,
            country_code=monitor.stream_country_code,
            region=monitor.stream_region,
        )

    server = WebServer(
        monitor=monitor,
        streamer=streamer,
        host=args.host,
        port=args.port,
    )
    print(f"\n=======================================================")
    print(f" Momcozy BM04 Web Dashboard & MJPEG Streaming Server")
    print(f" Dashboard:  http://{args.host}:{args.port}")
    print(f" Live Stream: http://{args.host}:{args.port}/stream")
    print(f" Snapshot:   http://{args.host}:{args.port}/snapshot")
    print(f"=======================================================\n")
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\nServer stopped.")


def cmd_share(args: argparse.Namespace) -> None:
    """Handle device sharing subcommands."""
    api = MomcozyCloudApi.load_session(args.config or DEFAULT_CONFIG_PATH)
    if not api or not api.token:
        print("Error: You must run './momcozy.py login' first.", file=sys.stderr)
        sys.exit(1)

    dev_id = args.device_id
    if not dev_id:
        config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
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
            res = api.add_device_share(args.account, tuya_dev_id=dev_id, country_code=args.country)
            print(f"✓ Successfully shared device with {args.account}!")
            print(f"  • Member Code: {res.get('member_code')}")
            print(f"  • Tuya UID:    {res.get('uid')}")
            print(f"  • Region:      {res.get('region')}")
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
    parser = argparse.ArgumentParser(
        prog="momcozy",
        description="Momcozy BM04 Baby Monitor Python Client & Streaming Server",
    )
    parser.add_argument("--config", help="Path to config JSON file (~/.momcozy.json by default)")
    parser.add_argument("--device-id", help="Target device ID")
    parser.add_argument("--local-key", help="Device local encryption key")
    parser.add_argument("--ip", help="Device IP on local Wi-Fi")

    sub = parser.add_subparsers(dest="subcommand", required=True)

    # login
    p_login = sub.add_parser("login", help="Log in to Momcozy Cloud")
    p_login.add_argument("--email", "-e")
    p_login.add_argument("--password", "-p")
    p_login.add_argument("--country", "-c", default="US", help="ISO2 country code, e.g. CH")
    p_login.add_argument("--region", "-r", choices=["NA", "EU", "AP", "CN"], default=None,
                         help="Account region (default: auto-detect; Switzerland/Germany = EU)")
    p_login.set_defaults(func=cmd_login)

    # whoami
    p_whoami = sub.add_parser("whoami", help="Show logged-in account, region, and profile info")
    p_whoami.set_defaults(func=cmd_whoami)

    # devices
    p_devices = sub.add_parser("devices", help="List devices bound to your account (raw JSON)")
    p_devices.set_defaults(func=cmd_devices)

    # status
    p_status = sub.add_parser("status", help="Show all monitor measurements and settings")
    p_status.add_argument("--json", action="store_true", help="Output raw JSON")
    p_status.set_defaults(func=cmd_status)

    # temperature
    p_temp = sub.add_parser("temperature", help="Print current temperature")
    p_temp.add_argument("-f", "--fahrenheit", action="store_true", help="Output in Fahrenheit")
    p_temp.set_defaults(func=cmd_temperature)

    # set
    p_set = sub.add_parser("set", help="Change monitor settings")
    p_set.add_argument("--night-vision", choices=["auto", "off", "on"])
    p_set.add_argument("--privacy", choices=["on", "off"])
    p_set.add_argument("--led", choices=["on", "off"])
    p_set.add_argument("--flip", choices=["on", "off"])
    p_set.add_argument("--motion", choices=["on", "off"])
    p_set.add_argument("--motion-sensitivity", choices=["0", "1", "2"])
    p_set.add_argument("--sound", choices=["on", "off"])
    p_set.add_argument("--sound-sensitivity", choices=["0", "1", "2"])
    p_set.add_argument("--cry", choices=["on", "off"])
    p_set.add_argument("--temp-alert", choices=["on", "off"])
    p_set.add_argument("--temp-min", type=float, help="Min temp alert in Celsius")
    p_set.add_argument("--temp-max", type=float, help="Max temp alert in Celsius")
    p_set.add_argument("--volume", type=int, help="Master volume (1-100)")
    p_set.set_defaults(func=cmd_set)

    # ptz
    p_ptz = sub.add_parser("ptz", help="Pan, tilt, and zoom controls")
    p_ptz.add_argument("action", choices=["up", "down", "left", "right", "stop"])
    p_ptz.add_argument("--duration", "-d", type=float, default=0.5, help="Step duration in seconds")
    p_ptz.set_defaults(func=cmd_ptz)

    # lullaby
    p_lullaby = sub.add_parser("lullaby", help="Lullaby controls")
    p_lullaby.add_argument("action", choices=["play", "stop", "list"])
    p_lullaby.add_argument("--track", "-t", default="0", help="Track ID (0..7)")
    p_lullaby.add_argument("--volume", "-v", type=int, default=50)
    p_lullaby.set_defaults(func=cmd_lullaby)

    # snapshot
    p_snap = sub.add_parser("snapshot", help="Capture a still JPEG image from video stream")
    p_snap.add_argument("-o", "--output", default="snapshot.jpg")
    p_snap.add_argument("-t", "--timeout", type=float, default=10.0)
    p_snap.set_defaults(func=cmd_snapshot)

    # record
    p_rec = sub.add_parser("record", help="Record an MP4 video clip from live stream")
    p_rec.add_argument("-o", "--output", default="recording.mp4")
    p_rec.add_argument("-d", "--duration", type=int, default=10, help="Duration in seconds")
    p_rec.add_argument("--fps", type=int, default=15)
    p_rec.set_defaults(func=cmd_record)

    # serve
    p_serve = sub.add_parser("serve", help="Start Web UI and MJPEG live stream server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", "-p", type=int, default=8080)
    p_serve.set_defaults(func=cmd_serve)

    # share
    p_share = sub.add_parser("share", help="Share camera access with another Momcozy account")
    share_sub = p_share.add_subparsers(dest="action", required=True)

    # share add <account>
    p_sadd = share_sub.add_parser("add", help="Share device with a user by email")
    p_sadd.add_argument("account", help="Recipient email or Momcozy account")
    p_sadd.add_argument("--country", "-c", help="Recipient country code (default: account country)")

    # share remove <member_code>
    p_srem = share_sub.add_parser("remove", help="Revoke device access for a member code")
    p_srem.add_argument("member_code", help="Recipient member code to remove")

    # share search <account>
    p_ssearch = share_sub.add_parser("search", help="Search for a Momcozy user to get their member code")
    p_ssearch.add_argument("account", help="Email or account to look up")
    p_ssearch.add_argument("--country", "-c", help="Country code")

    # share query <member_code...>
    p_squery = share_sub.add_parser("query", help="Look up email addresses for member codes")
    p_squery.add_argument("member_codes", nargs="+", help="Member codes to look up")

    p_share.set_defaults(func=cmd_share)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
