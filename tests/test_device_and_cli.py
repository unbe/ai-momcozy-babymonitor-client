"""Unit tests for device control, status parsing, CLI, ConfigStore, and hwaccel."""

from concurrent.futures import ThreadPoolExecutor
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from momcozy.cli import _get_configured_device, build_parser
from momcozy.config import ConfigStore, normalize_config_path
from momcozy.constants import (
    DP,
    LULLABY_TRACKS,
    LullabyMode,
    PRODUCT_ID_BM04,
    PtzDirection,
)
from momcozy.device import BM04Status, MomcozyMonitor, celsius_to_fahrenheit
from momcozy.hwaccel import detect_vaapi_device
from momcozy.tuya_cloud import TuyaSession


class TestMomcozyConstants(unittest.TestCase):
    def test_dp_values(self):
        self.assertEqual(int(DP.BASIC_INDICATOR), 101)
        self.assertEqual(int(DP.BASIC_FLIP), 103)
        self.assertEqual(int(DP.BASIC_PRIVATE), 105)
        self.assertEqual(int(DP.BASIC_NIGHTVISION), 108)
        self.assertEqual(int(DP.PTZ_STOP), 116)
        self.assertEqual(int(DP.PTZ_CONTROL), 119)
        self.assertEqual(int(DP.SENSOR_TEMPERATURE), 142)
        self.assertEqual(int(DP.TEMP_ALERT_SWITCH), 236)
        self.assertEqual(int(DP.TEMP_MAX_CVALUE), 240)
        self.assertEqual(int(DP.TEMP_MIN_CVALUE), 241)
        self.assertEqual(int(DP.IPC_LULLABY_CONTROL), 233)
        self.assertEqual(int(DP.IPC_LULLABY_LIST), 234)
        self.assertEqual(int(DP.IPC_LULLABY_VOLUME), 235)

    def test_product_id(self):
        self.assertEqual(PRODUCT_ID_BM04, "mzohju3minbjogwz")

    def test_ptz_directions_and_lullaby_modes(self):
        self.assertEqual(PtzDirection.UP.value, "0")
        self.assertEqual(PtzDirection.RIGHT.value, "2")
        self.assertEqual(PtzDirection.DOWN.value, "4")
        self.assertEqual(PtzDirection.LEFT.value, "6")
        self.assertEqual(LullabyMode.SINGLE_REPEAT.value, "0")
        self.assertEqual(LullabyMode.ALL_REPEAT.value, "1")
        self.assertEqual(LullabyMode.PLAY_ONCE.value, "2")


class TestTemperatureAndStatus(unittest.TestCase):
    def test_celsius_to_fahrenheit(self):
        self.assertAlmostEqual(celsius_to_fahrenheit(0.0), 32.0, places=1)
        self.assertAlmostEqual(celsius_to_fahrenheit(20.0), 68.0, places=1)
        self.assertAlmostEqual(celsius_to_fahrenheit(100.0), 212.0, places=1)
        self.assertAlmostEqual(celsius_to_fahrenheit(21.5), 70.7, places=1)

    def test_status_from_raw_dps(self):
        raw_dps = {
            "142": 225,
            "236": True,
            "241": 180,
            "240": 280,
            "105": False,
            "108": "0",
            "101": True,
            "134": True,
            "167": False,
            "233": "1",
            "234": "0",
            "235": 65,
            "160": 80,
        }

        status = BM04Status(
            temperature_c=float(raw_dps["142"]) / 10.0,
            temperature_f=celsius_to_fahrenheit(float(raw_dps["142"]) / 10.0),
            temp_alert_enabled=raw_dps["236"],
            temp_min_c=float(raw_dps["241"]) / 10.0,
            temp_max_c=float(raw_dps["240"]) / 10.0,
            privacy_mode=raw_dps["105"],
            night_vision=raw_dps["108"],
            indicator_light=raw_dps["101"],
            motion_switch=raw_dps["134"],
            cry_detection_switch=raw_dps["167"],
            lullaby_playing=(raw_dps["233"] == "1"),
            lullaby_track=raw_dps["234"],
            lullaby_track_name=LULLABY_TRACKS.get(raw_dps["234"]),
            lullaby_volume=raw_dps["235"],
            master_volume=raw_dps["160"],
            raw_dps=raw_dps,
        )

        self.assertEqual(status.temperature_c, 22.5)
        self.assertEqual(status.temperature_f, 72.5)
        self.assertTrue(status.temp_alert_enabled)
        self.assertEqual(status.temp_min_c, 18.0)
        self.assertEqual(status.temp_max_c, 28.0)
        self.assertTrue(status.lullaby_playing)
        self.assertEqual(status.lullaby_track_name, "Brahms' Lullaby")
        self.assertEqual(status.lullaby_volume, 65)

        summary = status.summary()
        self.assertIn("22.5°C", summary)
        self.assertIn("Brahms' Lullaby", summary)

    def test_dp_185_detection_event_parsing(self):
        raw_payload = {
            "v": "3.0",
            "bucket": "ty-eu-storage30-pic",
            "cmd": "ipc_bang",
            "type": "image",
            "files": [["/path/detect/1791828432.jpeg", "key123"]],
        }
        b64_str = base64.b64encode(json.dumps(raw_payload).encode()).decode()

        monitor = MomcozyMonitor(device_id="dev1", local_key="key1", probe_lan=False)
        status = monitor._parse_dps({"185": b64_str})

        self.assertEqual(status.latest_event_cmd, "ipc_bang")
        self.assertEqual(status.latest_event_name, "Sound / Cry Alert")
        self.assertEqual(status.latest_event_bucket, "ty-eu-storage30-pic")
        self.assertEqual(status.latest_event_file, "/path/detect/1791828432.jpeg")
        self.assertEqual(status.latest_event_time, 1791828432)
        self.assertIn("2026", status.latest_event_time_str)


class TestMomcozyMonitorControl(unittest.TestCase):
    def test_monitor_ptz_step_and_cloud_fallback(self):
        published_dps = []

        def mock_cloud_pub(dps):
            published_dps.append(dps)
            return True

        monitor = MomcozyMonitor(
            device_id="dev123",
            local_key="key123",
            ip=None,
            cloud_dp_publisher=mock_cloud_pub,
            probe_lan=False,
        )

        monitor.ptz_step("2", duration_seconds=0.01)
        self.assertEqual(len(published_dps), 2)
        self.assertEqual(published_dps[0], {"119": "2"})
        self.assertEqual(published_dps[1], {"116": True})

    def test_local_lan_connection_auto_rediscovery(self):
        discovered = []

        def mock_conn(addr, timeout=1.5):
            host, _ = addr
            if host == "10.11.2.12":
                raise ConnectionRefusedError("[Errno 61] Connection refused")
            mock_sock = MagicMock()
            mock_sock.__enter__.return_value = mock_sock
            mock_sock.__exit__.return_value = False
            return mock_sock

        fake_scan = {
            "10.11.2.186": {
                "ip": "10.11.2.186",
                "gwId": "bfb3260f33c5cdc22arfhh",
            }
        }

        with patch("socket.create_connection", side_effect=mock_conn), \
             patch("tinytuya.deviceScan", return_value=fake_scan):
            monitor = MomcozyMonitor(
                device_id="bfb3260f33c5cdc22arfhh",
                local_key="key1",
                ip="10.11.2.12",
                on_ip_discovered=lambda ip: discovered.append(ip),
            )
            self.assertEqual(monitor.ip, "10.11.2.186")
            self.assertEqual(discovered, ["10.11.2.186"])
            self.assertEqual(monitor.control_mode, "local_lan")
            self.assertFalse(monitor._lan_tcp_unavailable)

    def test_probe_lan_false_defers_blocking_scan(self):
        with patch("socket.create_connection") as mock_conn, \
             patch("tinytuya.deviceScan") as mock_scan:
            monitor = MomcozyMonitor(
                device_id="bfb3260f33c5cdc22arfhh",
                local_key="key1",
                ip="10.11.2.12",
                probe_lan=False,
            )
            mock_conn.assert_not_called()
            mock_scan.assert_not_called()
            self.assertTrue(monitor._lan_tcp_unavailable)
            self.assertIn("pending", monitor.control_diagnostic.lower())


class TestCliAndConfiguration(unittest.TestCase):
    def test_parser_subcommands_and_removed_aliases(self):
        parser = build_parser()
        args = parser.parse_args(["status", "--json"])
        self.assertEqual(args.subcommand, "status")
        self.assertTrue(args.json)

        args = parser.parse_args(["ptz", "up", "--duration", "1.5"])
        self.assertEqual(args.subcommand, "ptz")
        self.assertEqual(args.action, "up")
        self.assertEqual(args.duration, 1.5)

        args = parser.parse_args(["set", "--night-vision", "auto", "--privacy", "on"])
        self.assertEqual(args.night_vision, "auto")
        self.assertEqual(args.privacy, "on")

        args = parser.parse_args(["lullaby", "play", "--track", "3", "--volume", "70"])
        self.assertEqual(args.subcommand, "lullaby")
        self.assertEqual(args.action, "play")
        self.assertEqual(args.track, "3")
        self.assertEqual(args.volume, 70)

        args = parser.parse_args(["share", "add", "family@example.com", "--country", "CH", "--home-id", "98765432"])
        self.assertEqual(args.subcommand, "share")
        self.assertEqual(args.action, "add")
        self.assertEqual(args.account, "family@example.com")
        self.assertEqual(args.country, "CH")
        self.assertEqual(args.home_id, 98765432)

        args = parser.parse_args(["share", "remove", "mem123", "--relation-id", "777"])
        self.assertEqual(args.subcommand, "share")
        self.assertEqual(args.action, "remove")
        self.assertEqual(args.member_code, "mem123")
        self.assertEqual(args.relation_id, 777)

        args = parser.parse_args(["share", "search", "partner@example.com"])
        self.assertEqual(args.subcommand, "share")
        self.assertEqual(args.action, "search")
        self.assertEqual(args.account, "partner@example.com")

        args = parser.parse_args(["sync"])
        self.assertEqual(args.subcommand, "sync")

        # Legacy "keys" subcommand alias must be rejected
        with self.assertRaises(SystemExit):
            parser.parse_args(["keys"])

    def test_serve_cli_args(self):
        parser = build_parser()
        args = parser.parse_args(["serve", "--idle-timeout", "600", "--timelapse-dir", "/data/timelapse"])
        self.assertEqual(args.idle_timeout, 600.0)
        self.assertEqual(args.timelapse_dir, "/data/timelapse")

        default_args = parser.parse_args(["serve"])
        self.assertEqual(default_args.idle_timeout, 300.0)
        self.assertIsNone(default_args.timelapse_dir)

    def test_get_configured_device_populates_dps_cache_and_defers_lan_for_serve(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / ".momcozy.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "device_id": "dev_abc",
                        "local_key": "0123456789abcdef",
                        "ip": "10.0.0.55",
                        "dps": {"142": 235, "108": "0"},
                        "tuya": {"region": "33", "uid": "u1", "token": "t1"},
                        "tuya_session": {"sid": "s_init", "ecode": "e_init", "uid": "u1"},
                    }
                )
            )
            parser = build_parser()
            args = parser.parse_args(["--config", str(cfg_path), "serve"])

            with patch("socket.create_connection") as mock_conn, \
                 patch("tinytuya.deviceScan") as mock_scan:
                monitor = _get_configured_device(args)

            # Blocking LAN probes must NOT run synchronously during `serve` config load
            mock_conn.assert_not_called()
            mock_scan.assert_not_called()
            # Previously unreachable _dps_cache / initial_dps wiring now populates cached DPs
            self.assertEqual(monitor._cached_dps, {"142": 235, "108": "0"})
            self.assertIsNotNone(getattr(monitor, "session_manager", None))

            # Bidirectional session propagation via TuyaSessionManager listeners
            notified = []
            monitor.session_manager.add_listener(lambda s: notified.append(s.sid))
            monitor.session_manager.update_session(TuyaSession(sid="s_new", ecode="e_new", uid="u1"))
            self.assertEqual(notified, ["s_new"])
            self.assertEqual(monitor.tuya_session.sid, "s_new")
            saved = json.loads(cfg_path.read_text())
            self.assertEqual(saved["tuya_session"]["sid"], "s_new")


class TestConfigStoreAndHwaccel(unittest.TestCase):
    def test_normalize_config_path(self):
        f = Path("/tmp/custom_config.json")
        self.assertEqual(normalize_config_path(f), f)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            self.assertEqual(normalize_config_path(tmp_path), tmp_path / ".momcozy.json")

            existing = tmp_path / "momcozy.json"
            existing.write_text("{}")
            self.assertEqual(normalize_config_path(tmp_path), existing)

    def test_config_store_concurrent_updates_preserve_inode_and_keys(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".momcozy.json"
            cfg.write_text(json.dumps({"initial": True, "local_key": "keep_me"}))
            initial_inode = cfg.stat().st_ino

            def worker(idx: int) -> None:
                ConfigStore.update(cfg, {f"key_{idx}": idx})

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(worker, range(32)))

            # Inode must not change (Docker single-file bind mount safety)
            self.assertEqual(cfg.stat().st_ino, initial_inode)
            data = ConfigStore.read(cfg)
            self.assertTrue(data["initial"])
            self.assertEqual(data["local_key"], "keep_me")
            for idx in range(32):
                self.assertEqual(data[f"key_{idx}"], idx)

    def test_detect_vaapi_device(self):
        self.assertIsNone(detect_vaapi_device(hwaccel="none"))
        self.assertIsNone(detect_vaapi_device(hwaccel="off"))
        self.assertEqual(
            detect_vaapi_device(hwaccel="vaapi", vaapi_device="/dev/dri/renderD129"),
            "/dev/dri/renderD129",
        )
        with patch.dict(os.environ, {"MOMCOZY_HWACCEL": "disabled"}):
            self.assertIsNone(detect_vaapi_device(hwaccel="auto"))


if __name__ == "__main__":
    unittest.main()
