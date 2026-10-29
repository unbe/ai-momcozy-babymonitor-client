"""Unit tests for Momcozy BM04 Python Client."""

import unittest

from momcozy.constants import (
    DP,
    LULLABY_TRACKS,
    NightVisionMode,
    PtzDirection,
    PRODUCT_ID_BM04,
)
from momcozy.cloud import MomcozyCloudApi, hash_password
from momcozy.device import BM04Status, MomcozyMonitor, celsius_to_fahrenheit
from momcozy.cli import build_parser


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

    def test_ptz_directions(self):
        self.assertEqual(PtzDirection.UP.value, "0")
        self.assertEqual(PtzDirection.RIGHT.value, "2")
        self.assertEqual(PtzDirection.DOWN.value, "4")
        self.assertEqual(PtzDirection.LEFT.value, "6")


class TestMomcozyPasswordHash(unittest.TestCase):
    def test_argon2_hashing(self):
        # Hash should produce a stable base64 encoded string of 32 bytes (44 chars with '=')
        h1 = hash_password("Secret123")
        h2 = hash_password("Secret123")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 44)
        self.assertTrue(h1.endswith("="))

        # Different passwords produce different hashes
        h3 = hash_password("OtherPassword")
        self.assertNotEqual(h1, h3)


class TestTemperatureConversion(unittest.TestCase):
    def test_celsius_to_fahrenheit(self):
        self.assertAlmostEqual(celsius_to_fahrenheit(0.0), 32.0, places=1)
        self.assertAlmostEqual(celsius_to_fahrenheit(20.0), 68.0, places=1)
        self.assertAlmostEqual(celsius_to_fahrenheit(100.0), 212.0, places=1)
        self.assertAlmostEqual(celsius_to_fahrenheit(21.5), 70.7, places=1)


class TestStatusParsing(unittest.TestCase):
    def test_status_from_raw_dps(self):
        raw_dps = {
            "142": 225,  # 22.5 deg C
            "236": True, # Temp alert on
            "241": 180, # Min temp 18.0 C
            "240": 280, # Max temp 28.0 C
            "105": False, # Privacy off
            "108": "0",  # Night vision auto
            "101": True, # Status LED on
            "134": True, # Motion switch on
            "167": False, # Cry detection off
            "233": "1",  # Lullaby playing
            "234": "0",  # Brahms' Lullaby
            "235": 65,   # Lullaby volume
            "160": 80,   # Master volume
        }

        # Simulated status construction
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


class TestCliParser(unittest.TestCase):
    def test_parser_subcommands(self):
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

        args = parser.parse_args(["share", "add", "family@example.com", "--country", "CH"])
        self.assertEqual(args.subcommand, "share")
        self.assertEqual(args.action, "add")
        self.assertEqual(args.account, "family@example.com")
        self.assertEqual(args.country, "CH")

        args = parser.parse_args(["share", "add", "family@example.com", "--country", "CH", "--home-id", "98765432"])
        self.assertEqual(args.subcommand, "share")
        self.assertEqual(args.action, "add")
        self.assertEqual(args.account, "family@example.com")
        self.assertEqual(args.country, "CH")
        self.assertEqual(args.home_id, 98765432)

        args = parser.parse_args(["share", "remove", "mem123"])
        self.assertEqual(args.subcommand, "share")
        self.assertEqual(args.action, "remove")
        self.assertEqual(args.member_code, "mem123")

        args = parser.parse_args(["share", "search", "partner@example.com"])
        self.assertEqual(args.subcommand, "share")
        self.assertEqual(args.action, "search")
        self.assertEqual(args.account, "partner@example.com")


class TestMomcozyDeviceShare(unittest.TestCase):
    def setUp(self):
        self.api = MomcozyCloudApi(region="EU", device_id="test_dev_123")
        self.api.token = "mock_token"

    def test_search_tuya_user(self):
        from unittest.mock import patch, MagicMock

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": "200",
            "result": {
                "memberCode": "MEM999",
                "uid": "UID999",
                "region": "33"
            }
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.request", return_value=mock_resp) as mock_req:
            res = self.api.search_tuya_user("friend@example.com", country_code="CH")
            self.assertEqual(res["memberCode"], "MEM999")
            self.assertEqual(res["region"], "33")
            mock_req.assert_called_once()
            args, kwargs = mock_req.call_args
            self.assertEqual(args[0], "GET")
            self.assertIn("/api/app/third/tuya/user/search", args[1])
            self.assertEqual(kwargs["params"]["email"], "friend@example.com")

    def test_add_device_share_flow(self):
        from unittest.mock import patch, MagicMock

        def mock_req(method, url, *args, **kwargs):
            mock = MagicMock()
            mock.raise_for_status = MagicMock()
            if "/search" in url:
                mock.json.return_value = {"code": "200", "result": {"memberCode": "MEM1", "uid": "U1", "region": "EU"}}
            elif "/share/notice" in url:
                mock.json.return_value = {"code": "200", "result": {}}
            elif "/share" in url:
                mock.json.return_value = {"code": "200", "result": {}}
            return mock

        with patch("requests.request", side_effect=mock_req), \
             patch.object(self.api, "add_tuya_device_share", return_value={"home_id": 12345, "success": True}) as mock_tuya_share:
            res = self.api.add_device_share("partner@example.com", tuya_dev_id="test_camera")
            self.assertEqual(res["account"], "partner@example.com")
            self.assertEqual(res["member_code"], "MEM1")
            self.assertEqual(res["device_id"], "test_camera")
            self.assertEqual(res["tuya_share"], {"home_id": 12345, "success": True})
            mock_tuya_share.assert_called_once_with(
                tuya_dev_id="test_camera",
                target_member_code="MEM1",
                target_region="EU",
                home_id=None,
            )


class TestTuyaCloudGateway(unittest.TestCase):
    def test_md5_and_swap_sign_string(self):
        from momcozy.tuya_cloud import md5_hex, swap_sign_string, post_data_sign_field

        raw = "test_string"
        h = md5_hex(raw)
        self.assertEqual(len(h), 32)
        swapped = swap_sign_string(h)
        self.assertEqual(len(swapped), 32)
        self.assertEqual(swapped, h[8:16] + h[0:8] + h[24:32] + h[16:24])
        self.assertEqual(post_data_sign_field(raw), swapped)

    def test_build_sign_string(self):
        from momcozy.tuya_cloud import build_sign_string

        params = {
            "a": "smartlife.m.sharing.device.account.add",
            "v": "3.0",
            "time": "1700000000",
            "ignore_this": "value",
            "empty_param": "",
        }
        sign_str = build_sign_string(params)
        self.assertEqual(sign_str, "a=smartlife.m.sharing.device.account.add||time=1700000000||v=3.0")

    def test_sign_params(self):
        from momcozy.tuya_cloud import sign_params

        sig = sign_params("a=test||v=1.0", key="my_secret_key")
        self.assertIsInstance(sig, str)
        self.assertEqual(len(sig), 64)

    def test_body_keys(self):
        from momcozy.tuya_cloud import pre_login_body_key, session_body_key

        k1 = pre_login_body_key("req123", key="comp_key")
        self.assertEqual(len(k1), 16)

        k2 = session_body_key("req123", "ecode456", key="comp_key")
        self.assertEqual(len(k2), 16)
        self.assertNotEqual(k1, k2)

    def test_encrypt_decrypt_post_data(self):
        from momcozy.tuya_cloud import encrypt_post_data, decrypt_post_data

        key = "0123456789abcdef"
        msg = b'{"hello": "world", "number": 42}'
        encrypted = encrypt_post_data(key, msg)
        self.assertIsInstance(encrypted, str)
        decrypted = decrypt_post_data(key, encrypted)
        self.assertEqual(decrypted, msg)

    def test_encrypt_password_rsa(self):
        from cryptography.hazmat.primitives.asymmetric import rsa, padding
        from momcozy.tuya_cloud import encrypt_password_rsa

        # Generate a test RSA private/public key
        priv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_numbers = priv_key.public_key().public_numbers()

        test_pw_md5 = "e10adc3949ba59abbe56e057f20f883e"
        encrypted_hex = encrypt_password_rsa(
            modulus_decimal=str(pub_numbers.n),
            exponent_decimal=str(pub_numbers.e),
            password_md5=test_pw_md5,
        )

        decrypted = priv_key.decrypt(bytes.fromhex(encrypted_hex), padding.PKCS1v15()).decode("ascii")
        self.assertEqual(decrypted, test_pw_md5)

    def test_login_with_uid(self):
        from unittest.mock import patch, MagicMock
        from momcozy.tuya_cloud import TuyaGatewayClient

        client = TuyaGatewayClient(region="eu")

        def mock_call_api(api, version, post_data, session=None, extra_params=None):
            if api == "smartlife.m.user.username.token.get":
                return {
                    "publicKey": "123456789",
                    "exponent": "65537",
                    "token": "rsa_token_abc",
                }
            elif "smartlife.m.user.uid.password.login" in api:
                return {
                    "sid": "session_id_12345",
                    "ecode": "session_ecode_67890",
                    "uid": "test_uid",
                }
            raise ValueError(f"Unexpected API: {api}")

        with patch.object(client, "call_api", side_effect=mock_call_api), \
             patch("momcozy.tuya_cloud.encrypt_password_rsa", return_value="encrypted_hex"):
            session = client.login_with_uid(country_code="33", uid="test_uid", token="cloud_token")
            self.assertEqual(session.sid, "session_id_12345")
            self.assertEqual(session.ecode, "session_ecode_67890")
            self.assertEqual(session.uid, "test_uid")

    def test_get_home_id(self):
        from unittest.mock import patch
        from momcozy.tuya_cloud import TuyaGatewayClient, TuyaSession

        client = TuyaGatewayClient(region="eu")
        session = TuyaSession(sid="sid1", ecode="eco1", uid="uid1")

        with patch.object(client, "call_api", return_value=[{"gid": 998877, "name": "My Home"}]):
            home_id = client.get_home_id(session)
            self.assertEqual(home_id, 998877)

    def test_share_device_to_user(self):
        from unittest.mock import patch
        from momcozy.tuya_cloud import TuyaGatewayClient, TuyaSession

        client = TuyaGatewayClient(region="eu")
        session = TuyaSession(sid="sid1", ecode="eco1", uid="uid1")

        with patch.object(client, "call_api", return_value={"relationId": 54321}) as mock_api:
            res = client.share_device_to_user(
                session=session,
                home_id=998877,
                tuya_dev_id="dev_camera_123",
                target_region="33",
                target_member_code="mem_code_abc",
            )
            self.assertEqual(res["home_id"], 998877)
            self.assertEqual(res["relationId"], 54321)
            mock_api.assert_called_once()
            call_args = mock_api.call_args
            self.assertEqual(call_args[0][0], "smartlife.m.sharing.device.account.add")
            self.assertEqual(call_args[0][1], "3.0")
            self.assertEqual(call_args[0][2]["groupId"], 998877)
            self.assertEqual(call_args[0][2]["devIds"], ["dev_camera_123"])
            self.assertEqual(call_args[0][2]["userAccount"], "mem_code_abc")
            self.assertEqual(call_args[0][2]["countryCode"], "33")

    def test_get_home_devices(self):
        from unittest.mock import patch
        from momcozy.tuya_cloud import TuyaGatewayClient, TuyaSession

        client = TuyaGatewayClient(region="eu")
        session = TuyaSession(sid="sid1", ecode="eco1", uid="uid1")

        mock_devs = [
            {"devId": "bfb3260f33c5cdc22arfhh", "name": "BM04", "localKey": "0123456789abcdef"}
        ]
        with patch.object(client, "call_api", return_value={"devices": mock_devs}):
            devs = client.get_home_devices(session, 998877)
            self.assertEqual(len(devs), 1)
            self.assertEqual(devs[0]["localKey"], "0123456789abcdef")

    def test_get_shared_devices(self):
        from unittest.mock import patch
        from momcozy.tuya_cloud import TuyaGatewayClient, TuyaSession

        client = TuyaGatewayClient(region="eu")
        session = TuyaSession(sid="sid1", ecode="eco1", uid="uid1")

        mock_shared = [
            {"devId": "bfb3260f33c5cdc22arfhh", "name": "Shared BM04", "localKey": "fedcba9876543210"}
        ]
        with patch.object(client, "call_api", return_value=mock_shared) as mock_api:
            devs = client.get_shared_devices(session)
            self.assertEqual(len(devs), 1)
            self.assertEqual(devs[0]["localKey"], "fedcba9876543210")
            self.assertEqual(mock_api.call_args[0][0], "thing.m.my.shared.device.list")

    def test_get_all_devices_combines_home_and_shared(self):
        from unittest.mock import patch
        from momcozy.tuya_cloud import TuyaGatewayClient, TuyaSession

        client = TuyaGatewayClient(region="eu")
        session = TuyaSession(sid="sid1", ecode="eco1", uid="uid1")

        home_devs = [{"devId": "dev1", "name": "Home Cam", "localKey": "key1"}]
        shared_devs = [{"devId": "dev2", "name": "Shared Cam", "localKey": "key2"}]

        with patch.object(client, "get_home_devices", return_value=home_devs), \
             patch.object(client, "get_shared_devices", return_value=shared_devs), \
             patch.object(client, "get_home_id", return_value=123):
            all_devs = client.get_all_devices(session)
            self.assertEqual(len(all_devs), 2)
            self.assertEqual({d["devId"] for d in all_devs}, {"dev1", "dev2"})

    def test_sync_devices_and_keys(self):
        import tempfile
        import json
        from pathlib import Path
        from unittest.mock import patch
        from momcozy.cloud import MomcozyCloudApi, TuyaCredentials

        api = MomcozyCloudApi()
        api.token = "fake_token"
        api.tuya_creds = TuyaCredentials(region="33", uid="uid123", token="tuya_token")

        momcozy_devs = {
            "deviceList": [
                {
                    "sn": "BM04-bfb3260f33c5cdc22arfhh",
                    "deviceName": "Baby Room",
                    "mac": "50:7B:91:11:22:33",
                    "metadata": {"extDeviceId": "bfb3260f33c5cdc22arfhh"},
                }
            ]
        }
        tuya_devs = [
            {
                "devId": "bfb3260f33c5cdc22arfhh",
                "name": "Baby Room",
                "localKey": "abcdef0123456789",
                "ip": "10.11.2.12",
            }
        ]

        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json") as tmp:
            tmp.write(json.dumps({"device_id": "bfb3260f33c5cdc22arfhh"}))
            tmp_path = Path(tmp.name)

        try:
            with patch.object(api, "query_devices", return_value=momcozy_devs), \
                 patch.object(api, "get_tuya_devices", return_value=tuya_devs):
                synced = api.sync_devices_and_keys(tmp_path)
                self.assertIn("bfb3260f33c5cdc22arfhh", synced)
                self.assertEqual(synced["bfb3260f33c5cdc22arfhh"]["local_key"], "abcdef0123456789")

                # Verify updated JSON config
                saved = json.loads(tmp_path.read_text())
                self.assertEqual(saved["local_key"], "abcdef0123456789")
                self.assertEqual(saved["camera_name"], "Baby Room")
                self.assertEqual(saved["mac"], "50:7B:91:11:22:33")
                self.assertEqual(saved["ip"], "10.11.2.12")
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def test_get_all_devices_propagates_error_when_all_fail(self):
        from unittest.mock import patch
        from momcozy.tuya_cloud import TuyaGatewayClient, TuyaSession

        client = TuyaGatewayClient(region="eu")
        session = TuyaSession(sid="sid1", ecode="eco1", uid="uid1")

        with patch.object(client, "get_home_id", side_effect=RuntimeError("Home service down")), \
             patch.object(client, "get_shared_devices", side_effect=RuntimeError("Shared service down")):
            with self.assertRaises(RuntimeError) as ctx:
                client.get_all_devices(session)
            self.assertIn("Failed to fetch Tuya devices", str(ctx.exception))
            self.assertIn("Home service down", str(ctx.exception))
            self.assertIn("Shared service down", str(ctx.exception))

    def test_sync_devices_and_keys_propagates_cloud_errors(self):
        from unittest.mock import patch
        from momcozy.cloud import MomcozyCloudApi

        api = MomcozyCloudApi()
        api.token = "fake_token"

        with patch.object(api, "query_devices", side_effect=RuntimeError("Momcozy 500 error")), \
             patch.object(api, "get_tuya_devices", side_effect=RuntimeError("Tuya gateway timeout")):
            with self.assertRaises(RuntimeError) as ctx:
                api.sync_devices_and_keys(raise_on_error=True)
            self.assertIn("Momcozy 500 error", str(ctx.exception))
            self.assertIn("Tuya gateway timeout", str(ctx.exception))


class TestWebServer(unittest.IsolatedAsyncioTestCase):
    async def test_handle_api_status_success(self):
        from unittest.mock import MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer
        from momcozy.device import BM04Status

        mock_monitor = MagicMock()
        mock_monitor.get_status.return_value = BM04Status(temperature_c=22.5, temperature_f=72.5)

        server = WebServer(monitor=mock_monitor)
        req = make_mocked_request("GET", "/api/status")
        resp = await server.handle_api_status(req)

        self.assertEqual(resp.status, 200)
        import json
        body = json.loads(resp.text)
        self.assertTrue(body["online"])
        self.assertEqual(body["temperature_c"], 22.5)

    async def test_handle_api_status_error_resilience(self):
        from unittest.mock import MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        mock_monitor = MagicMock()
        mock_monitor.get_status.side_effect = RuntimeError("Failed to communicate with device at 10.11.2.12: Check device key")

        server = WebServer(monitor=mock_monitor)
        req = make_mocked_request("GET", "/api/status")
        resp = await server.handle_api_status(req)

        self.assertEqual(resp.status, 200)
        import json
        body = json.loads(resp.text)
        self.assertFalse(body["online"])
        self.assertIn("Check device key", body["error"])

    async def test_handle_api_control_error_resilience(self):
        from unittest.mock import MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        mock_monitor = MagicMock()
        mock_monitor.ptz_move.side_effect = RuntimeError("Socket timeout")

        server = WebServer(monitor=mock_monitor)
        req = make_mocked_request("POST", "/api/control")
        req._read_bytes = b'{"action": "ptz_move", "direction": "0"}'

        resp = await server.handle_api_control(req)
        self.assertEqual(resp.status, 502)
        import json
        body = json.loads(resp.text)
        self.assertEqual(body["status"], "error")
        self.assertIn("Socket timeout", body["error"])

    async def test_handle_api_control_ptz_step(self):
        from unittest.mock import MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        mock_monitor = MagicMock()
        server = WebServer(monitor=mock_monitor)
        self.assertEqual(server.port, 8421)

        req = make_mocked_request("POST", "/api/control")
        req._read_bytes = b'{"action": "ptz_step", "direction": "2", "duration": 0.4}'

        resp = await server.handle_api_control(req)
        self.assertEqual(resp.status, 200)
        import json
        body = json.loads(resp.text)
        self.assertEqual(body["status"], "ok")
        mock_monitor.ptz_step.assert_called_once_with("2", 0.4)

        # Omitted duration should default to 0.175s
        mock_monitor.reset_mock()
        req2 = make_mocked_request("POST", "/api/control")
        req2._read_bytes = b'{"action": "ptz_step", "direction": "0"}'
        resp2 = await server.handle_api_control(req2)
        self.assertEqual(resp2.status, 200)
        mock_monitor.ptz_step.assert_called_once_with("0", 0.175)

    async def test_handle_api_status_includes_stream_info(self):
        from unittest.mock import MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer
        from momcozy.device import BM04Status

        mock_monitor = MagicMock()
        mock_monitor.control_mode = "local_lan"
        mock_monitor.control_transport = "Local LAN (192.168.1.50:6668)"
        mock_monitor.get_status.return_value = BM04Status(temperature_c=21.0, temperature_f=69.8)

        mock_streamer = MagicMock()
        mock_streamer.is_running = True
        mock_streamer.is_connected = False
        mock_streamer.last_error = "Signaling error: 135"
        mock_streamer.stream_mode = "cloud_relay"
        mock_streamer.transport_description = "Cloud Relay (Tuya P2P)"

        server = WebServer(monitor=mock_monitor, streamer=mock_streamer)
        req = make_mocked_request("GET", "/api/status")
        resp = await server.handle_api_status(req)

        self.assertEqual(resp.status, 200)
        import json
        body = json.loads(resp.text)
        self.assertTrue(body["stream_configured"])
        self.assertTrue(body["stream_running"])
        self.assertFalse(body["stream_connected"])
        self.assertEqual(body["stream_error"], "Signaling error: 135")
        self.assertEqual(body["stream_mode"], "cloud_relay")
        self.assertEqual(body["stream_transport"], "Cloud Relay (Tuya P2P)")
        self.assertEqual(body["stream_format"], "None")
        self.assertFalse(body["transcoding"])
        self.assertEqual(body["control_mode"], "local_lan")
        self.assertEqual(body["control_transport"], "Local LAN (192.168.1.50:6668)")

    async def test_handle_favicon_returns_baby_svg(self):
        from unittest.mock import MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        mock_monitor = MagicMock()
        server = WebServer(monitor=mock_monitor, streamer=None)
        req = make_mocked_request("GET", "/favicon.ico")
        resp = await server.handle_favicon(req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "image/svg+xml")
        self.assertIn("<svg", resp.text)
        self.assertIn("Pacifier", resp.text)

    async def test_handle_stream_not_configured(self):
        from unittest.mock import MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        mock_monitor = MagicMock()
        server = WebServer(monitor=mock_monitor, streamer=None)
        req = make_mocked_request("GET", "/stream")
        resp = await server.handle_stream(req)

        self.assertEqual(resp.status, 503)
        self.assertIn("not configured", resp.text)

    async def test_handle_stream_start_failure(self):
        from unittest.mock import AsyncMock, MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        mock_monitor = MagicMock()
        mock_streamer = MagicMock()
        mock_streamer.is_running = False
        mock_streamer.start = AsyncMock(side_effect=RuntimeError("Broker auth failed"))

        server = WebServer(monitor=mock_monitor, streamer=mock_streamer)
        req = make_mocked_request("GET", "/stream")
        resp = await server.handle_stream(req)

        self.assertEqual(resp.status, 502)
        self.assertIn("Broker auth failed", resp.text)

    async def test_video_streamer_error_tracking(self):
        from momcozy.stream import VideoStreamer
        streamer = VideoStreamer("dev123", "localkey")
        self.assertFalse(streamer.is_running)
        self.assertFalse(streamer.is_connected)
        self.assertIsNone(streamer.last_error)

        # Simulate exception caught in streamer
        streamer._last_error = "Connection refused"
        self.assertEqual(streamer.last_error, "Connection refused")

    async def test_video_streamer_idle_timeout(self):
        import asyncio
        from momcozy.stream import VideoStreamer

        streamer = VideoStreamer("dev123", "localkey", idle_timeout=0.05)
        self.assertEqual(streamer.idle_timeout, 0.05)
        self.assertEqual(streamer.listener_count, 0)

        # Start streamer with mock stream loop
        async def mock_loop():
            try:
                while streamer.is_running:
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass

        streamer._stream_loop = mock_loop
        await streamer.start()
        self.assertTrue(streamer.is_running)
        self.assertIsNotNone(streamer._idle_task)

        # Wait for the 0.05s idle timeout to fire
        await asyncio.sleep(0.08)
        self.assertFalse(streamer.is_running)
        self.assertIsNone(streamer._idle_task)

    async def test_video_streamer_idle_timeout_cancelled_by_listener(self):
        import asyncio
        from momcozy.stream import VideoStreamer

        streamer = VideoStreamer("dev123", "localkey", idle_timeout=0.08)
        streamer._running = True
        streamer._connected = True
        streamer._latest_frame = b"fake_jpeg"

        # When a listener subscribes, idle stop is cancelled
        streamer._schedule_idle_stop()
        self.assertIsNotNone(streamer._idle_task)

        # Subscribing and receiving initial frame cancels idle stop
        sub_iter = streamer.subscribe()
        frame = await sub_iter.__anext__()
        self.assertEqual(frame, b"fake_jpeg")
        self.assertEqual(streamer.listener_count, 1)
        self.assertIsNone(streamer._idle_task)

        # Cleaning up listener reschedules idle stop
        await sub_iter.aclose()
        self.assertEqual(streamer.listener_count, 0)
        self.assertIsNotNone(streamer._idle_task)
        await streamer.stop()

    def test_serve_idle_timeout_cli_arg(self):
        from momcozy.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["serve", "--idle-timeout", "600"])
        self.assertEqual(args.idle_timeout, 600.0)

        default_args = parser.parse_args(["serve"])
        self.assertEqual(default_args.idle_timeout, 300.0)

    def test_tuya_gateway_publish_dps(self):
        from unittest.mock import MagicMock
        from momcozy.tuya_cloud import TuyaGatewayClient, TuyaSession

        client = TuyaGatewayClient(region="eu")
        client.call_api = MagicMock(return_value=True)
        session = TuyaSession(sid="sid1", ecode="ec1", uid="uid1")

        ok = client.publish_dps(session, "dev123", {"119": "0"})
        self.assertTrue(ok)
        client.call_api.assert_called_once()
        api_name, ver, payload = client.call_api.call_args[0][:3]
        self.assertEqual(api_name, "smartlife.m.device.dp.publish")
        self.assertEqual(ver, "1.0")
        self.assertEqual(payload["devId"], "dev123")
        self.assertIn('"119"', payload["dps"])

    def test_monitor_ptz_step_and_cloud_fallback(self):
        from unittest.mock import MagicMock
        from momcozy.device import MomcozyMonitor

        published_dps = []
        def mock_cloud_pub(dps):
            published_dps.append(dps)
            return True

        monitor = MomcozyMonitor(
            device_id="dev123",
            local_key="key123",
            ip=None, # No local IP -> triggers cloud fallback
            cloud_dp_publisher=mock_cloud_pub,
        )

        monitor.ptz_step("2", duration_seconds=0.01)
        # Should have published DP 119 ("2") and then DP 116 (True)
        self.assertEqual(len(published_dps), 2)
        self.assertEqual(published_dps[0], {"119": "2"})
        self.assertEqual(published_dps[1], {"116": True})

    def test_momcozy_hevc_handler(self):
        import struct
        from momcozy.stream import MomcozyHevcHandler, _SYNC_MARKER

        frames = []
        handler = MomcozyHevcHandler(lambda f: frames.append(f))

        # Build mock record with 36-byte header and mock RTP packet (12 bytes RTP + 1 byte NAL)
        # ext=8, plen=13
        header = bytearray(_SYNC_MARKER) # 8 bytes
        header.extend(b"\x00" * 8) # 8 bytes (offset 8..15)
        header.extend(struct.pack("<I", 8)) # offset 16..19: ext=8
        header.extend(b"\x00" * 12) # offset 20..31: 4 res + 8 codec/dims
        header.extend(struct.pack("<I", 13)) # offset 32..35: plen=13

        # RTP header (12 bytes)
        rtp_hdr = b"\x80\x5f\x00\x01\x00\x00\x00\x00\x00\x00\x00\x0a"
        # NAL unit: Single NALU (e.g. VPS = 32 << 1 = 64 = 0x40)
        nal_payload = b"\x40"

        record = bytes(header + rtp_hdr + nal_payload)
        handler.feed_record(record)
        # Should parse RTP packet without error
        self.assertEqual(len(handler._buffer), 0)

    def test_momcozy_hevc_handler_requires_av(self):
        import sys
        from unittest.mock import patch
        from momcozy.stream import MomcozyHevcHandler

        with patch.dict(sys.modules, {"av": None}):
            with self.assertRaises(ImportError) as ctx:
                MomcozyHevcHandler(lambda f: None)
            self.assertIn("requires the 'av' package", str(ctx.exception))

    def test_create_p2p_client_mqtt_identity(self):
        from momcozy.stream import create_p2p_client
        from momcozy.tuya_cloud import CLIENT_ID, PACKAGE_NAME
        from unittest.mock import MagicMock

        client = create_p2p_client(tuya_uid="test_uid", tuya_token="test_tok", tuya_region="33")
        mock_session = MagicMock()
        mock_session.ecode = "ecode123"
        mock_session.sid = "sid456"
        mock_session.uid = "uid789"
        mock_session.device_fingerprint = "fp000"

        identity = client._gateway.mqtt_identity(mock_session)
        self.assertIn(CLIENT_ID, identity.username)
        self.assertIn(PACKAGE_NAME, identity.client_id)


    def test_normalize_config_path(self):
        from pathlib import Path
        from momcozy.cloud import normalize_config_path
        import tempfile

        # Standard file path
        f = Path("/tmp/custom_config.json")
        self.assertEqual(normalize_config_path(f), f)

        # Directory path should append .momcozy.json
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            self.assertEqual(normalize_config_path(tmp_path), tmp_path / ".momcozy.json")

            # If existing file is inside
            existing = tmp_path / "momcozy.json"
            existing.write_text("{}")
            self.assertEqual(normalize_config_path(tmp_path), existing)

    def test_tuya_get_device_info_session_invalid(self):
        from unittest.mock import MagicMock
        from momcozy.tuya_cloud import TuyaGatewayClient, TuyaSession

        client = TuyaGatewayClient()
        session = TuyaSession(sid="sid_invalid", ecode="ecode", uid="uid")
        client.call_api = MagicMock(side_effect=RuntimeError("Tuya API smartlife.m.device.get failed [USER_SESSION_INVALID]: USER_SESSION_INVALID"))

        with self.assertRaises(RuntimeError) as ctx:
            client.get_device_info(session, "dev123")
        self.assertIn("USER_SESSION_INVALID", str(ctx.exception))

    def test_alaw_lut_conversion(self):
        from momcozy.stream import _alaw_to_linear, ALAW_LUT

        # Verify all 256 byte mappings match precomputed LUT
        for b in range(256):
            self.assertEqual(_alaw_to_linear(b), int(ALAW_LUT[b]))

        # In G.711 A-law with even-bit inversion (0x55), 0x55 and 0xD5 are center quant levels
        self.assertEqual(_alaw_to_linear(0x55), -8)
        self.assertEqual(_alaw_to_linear(0xD5), 8)

    def test_make_wav_header(self):
        import struct
        from momcozy.stream import make_wav_header

        hdr = make_wav_header(sample_rate=8000, channels=1, bits_per_sample=16)
        self.assertEqual(len(hdr), 44)
        self.assertTrue(hdr.startswith(b"RIFF"))
        self.assertIn(b"WAVEfmt ", hdr)
        self.assertIn(b"data", hdr)

        # Check sample rate at offset 24
        rate = struct.unpack("<I", hdr[24:28])[0]
        self.assertEqual(rate, 8000)

        # Check channels at offset 22
        channels = struct.unpack("<H", hdr[22:24])[0]
        self.assertEqual(channels, 1)

    def test_momcozy_audio_handler(self):
        import struct
        from momcozy.stream import MomcozyAudioHandler, _AUDIO_SYNC_MARKER

        received_pcm = []
        handler = MomcozyAudioHandler(lambda pcm: received_pcm.append(pcm))

        # Build mock audio record: 36-byte header, 12-byte RTP (PT 8 = PCMA), 10 bytes payload
        header = bytearray(_AUDIO_SYNC_MARKER)  # 8 bytes
        header.extend(b"\x00" * 8)              # 8 bytes
        header.extend(struct.pack("<I", 8))      # ext=8
        header.extend(b"\x00" * 12)             # 12 bytes
        header.extend(struct.pack("<I", 12 + 10))  # plen = 22 (12 bytes RTP + 10 bytes payload)

        rtp_hdr = b"\x80\x08\x00\x01\x00\x00\x00\x00\x00\x00\x00\x01"  # PT=8 (PCMA)
        payload = b"\x55" * 10  # 10 bytes of silence

        record = bytes(header + rtp_hdr + payload)
        handler.feed_record(record)

        self.assertEqual(len(received_pcm), 1)
        # 10 bytes of A-law -> 20 bytes of 16-bit linear PCM
        self.assertEqual(len(received_pcm[0]), 20)
        self.assertEqual(received_pcm[0], struct.pack("<h", -8) * 10)

    def test_streaming_mp3_encoder(self):
        from momcozy.stream import StreamingMp3Encoder

        encoder = StreamingMp3Encoder(sample_rate=8000, bit_rate=32000)
        pcm_chunk = b"\x00" * 1600  # 800 samples of 16-bit PCM = 0.1s
        out1 = encoder.encode(pcm_chunk)
        out2 = encoder.flush()
        total_mp3 = out1 + out2
        self.assertGreater(len(total_mp3), 0)

    async def test_handle_audio_not_configured(self):
        from unittest.mock import MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        mock_monitor = MagicMock()
        server = WebServer(monitor=mock_monitor, streamer=None)
        req = make_mocked_request("GET", "/audio")
        resp = await server.handle_audio(req)

        self.assertEqual(resp.status, 503)
        self.assertIn("not configured", resp.text)

    async def test_handle_audio_streaming(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        mock_monitor = MagicMock()
        mock_streamer = MagicMock()
        mock_streamer.is_running = True

        async def mock_sub_audio(format="mp3"):
            yield b"MOCK_AUDIO_DATA"

        mock_streamer.subscribe_audio = mock_sub_audio

        server = WebServer(monitor=mock_monitor, streamer=mock_streamer)

        # Test MP3 format
        req_mp3 = make_mocked_request("GET", "/audio?format=mp3")
        resp_mp3 = await server.handle_audio(req_mp3)
        self.assertEqual(resp_mp3.status, 200)
        self.assertEqual(resp_mp3.content_type, "audio/mpeg")

        # Test WAV format
        req_wav = make_mocked_request("GET", "/audio?format=wav")
        resp_wav = await server.handle_audio(req_wav)
        self.assertEqual(resp_wav.status, 200)
        self.assertEqual(resp_wav.content_type, "audio/wav")

    async def test_audio_listener_idle_management(self):
        import asyncio
        from momcozy.stream import VideoStreamer

        streamer = VideoStreamer("dev123", "localkey", idle_timeout=0.08)
        streamer._running = True
        streamer._connected = True

        # Initially no listeners -> idle stop scheduled
        streamer._schedule_idle_stop()
        self.assertIsNotNone(streamer._idle_task)

        # Subscribing to audio cancels idle stop
        sub_iter = streamer.subscribe_audio(format="wav")
        header = await sub_iter.__anext__()
        self.assertEqual(len(header), 44)
        self.assertEqual(streamer.audio_listener_count, 1)
        self.assertEqual(streamer.total_listener_count, 1)
        self.assertIsNone(streamer._idle_task)

        # Closing audio subscription reschedules idle stop
        await sub_iter.aclose()
        self.assertEqual(streamer.audio_listener_count, 0)
        self.assertEqual(streamer.total_listener_count, 0)
        self.assertIsNotNone(streamer._idle_task)
        await streamer.stop()

    def test_dp_185_detection_event_parsing(self):
        import base64
        import json
        from momcozy.device import MomcozyMonitor

        raw_payload = {
            "v": "3.0",
            "bucket": "ty-eu-storage30-pic",
            "cmd": "ipc_bang",
            "type": "image",
            "files": [["/path/detect/1791828432.jpeg", "key123"]],
        }
        b64_str = base64.b64encode(json.dumps(raw_payload).encode()).decode()

        monitor = MomcozyMonitor(device_id="dev1", local_key="key1")
        status = monitor._parse_dps({"185": b64_str})

        self.assertEqual(status.latest_event_cmd, "ipc_bang")
        self.assertEqual(status.latest_event_name, "Sound / Cry Alert")
        self.assertEqual(status.latest_event_bucket, "ty-eu-storage30-pic")
        self.assertEqual(status.latest_event_file, "/path/detect/1791828432.jpeg")
        self.assertEqual(status.latest_event_time, 1791828432)
        self.assertIn("2026", status.latest_event_time_str)

    def test_local_lan_connection_auto_rediscovery(self):
        from unittest.mock import patch, MagicMock
        from momcozy.device import MomcozyMonitor

        discovered = []

        # Mock socket.create_connection so 10.11.2.12 fails, but 10.11.2.186 succeeds
        def mock_conn(addr, timeout=1.5):
            host, port = addr
            if host == "10.11.2.12":
                raise ConnectionRefusedError("[Errno 61] Connection refused")
            mock_sock = MagicMock()
            mock_sock.__enter__.return_value = mock_sock
            mock_sock.__exit__.return_value = False
            return mock_sock

        # Mock tinytuya.deviceScan to return the new IP 10.11.2.186
        fake_scan = {
            "10.11.2.186": {
                "ip": "10.11.2.186",
                "gwId": "bfb3260f33c5cdc22arfhh",
            }
        }

        with patch("socket.create_connection", side_effect=mock_conn):
            with patch("tinytuya.deviceScan", return_value=fake_scan):
                monitor = MomcozyMonitor(
                    device_id="bfb3260f33c5cdc22arfhh",
                    local_key="key1",
                    ip="10.11.2.12",  # Stale IP
                    on_ip_discovered=lambda ip: discovered.append(ip),
                )
                self.assertEqual(monitor.ip, "10.11.2.186")
                self.assertEqual(discovered, ["10.11.2.186"])
                self.assertEqual(monitor.control_mode, "local_lan")
                self.assertFalse(monitor._lan_tcp_unavailable)

    async def test_handle_detection_snapshot_endpoint(self):
        from unittest.mock import AsyncMock, MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        mock_monitor = MagicMock()
        mock_streamer = MagicMock()
        mock_streamer.get_detection_snapshot = AsyncMock(return_value=b"FAKE_JPEG_BYTES")

        server = WebServer(monitor=mock_monitor, streamer=mock_streamer)

        req = make_mocked_request("GET", "/detection_snapshot.jpg")
        resp = await server.handle_detection_snapshot(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "image/jpeg")
        self.assertEqual(resp.body, b"FAKE_JPEG_BYTES")

    async def test_video_streamer_detection_snapshot_capture(self):
        from momcozy.stream import VideoStreamer

        streamer = VideoStreamer("dev123", "localkey")
        streamer._latest_frame = b"CURRENT_JPEG_FRAME"

        # Capture snapshot
        captured = streamer.capture_detection_snapshot(trigger="Test Trigger")
        self.assertEqual(captured, b"CURRENT_JPEG_FRAME")
        self.assertEqual(streamer.latest_detection_snapshot, b"CURRENT_JPEG_FRAME")
        self.assertEqual(streamer.latest_detection_trigger, "Test Trigger")
        self.assertIsNotNone(streamer.latest_detection_time)

        # get_detection_snapshot returns cached frame
        snap = await streamer.get_detection_snapshot()
        self.assertEqual(snap, b"CURRENT_JPEG_FRAME")

    async def test_get_snapshot_fetches_fresh_frame_when_cached_frame_stale(self):
        import asyncio
        import time
        from momcozy.stream import VideoStreamer

        streamer = VideoStreamer("dev123", "localkey")
        streamer._running = True
        streamer._latest_frame = b"STALE_BASELINE_FRAME"
        streamer._latest_frame_time = time.monotonic() - 60.0  # 60s ago

        async def push_fresh_frame():
            await asyncio.sleep(0.05)
            streamer._latest_frame = b"NEW_LIVE_CAMERA_FRAME"
            streamer._latest_frame_time = time.monotonic()

        push_task = asyncio.create_task(push_fresh_frame())
        fresh_frame = await streamer.get_snapshot(timeout_seconds=2.0)
        await push_task

        # Ensure get_snapshot waited for and returned the fresh frame, NOT the stale baseline
        self.assertEqual(fresh_frame, b"NEW_LIVE_CAMERA_FRAME")

    async def test_handle_snapshot_endpoint(self):
        from unittest.mock import AsyncMock, MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        mock_monitor = MagicMock()
        mock_streamer = MagicMock()
        mock_streamer.get_snapshot = AsyncMock(return_value=b"LIVE_SNAPSHOT_BYTES")

        server = WebServer(monitor=mock_monitor, streamer=mock_streamer)

        req = make_mocked_request("GET", "/snapshot")
        resp = await server.handle_snapshot(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "image/jpeg")
        self.assertEqual(resp.body, b"LIVE_SNAPSHOT_BYTES")
        self.assertIn("Cache-Control", resp.headers)
        self.assertIn("no-store", resp.headers["Cache-Control"])

    async def test_hls_transcoder_properties_and_security(self):
        import tempfile
        from unittest.mock import MagicMock
        from pathlib import Path
        from momcozy.hls import HlsTranscoder

        mock_streamer = MagicMock()
        with tempfile.TemporaryDirectory() as tmp_dir:
            hls = HlsTranscoder(streamer=mock_streamer, output_dir=tmp_dir)
            self.assertTrue(hls.is_available)
            self.assertFalse(hls.is_running)

            # Security check: path traversal attempts must be rejected
            self.assertIsNone(hls.get_segment("../../../etc/passwd"))
            self.assertIsNone(hls.get_segment("invalid_name.mp4"))

            # Valid segment retrieval
            seg_path = Path(tmp_dir) / "segment_000.ts"
            seg_path.write_bytes(b"TS_CHUNK_DATA")
            self.assertEqual(hls.get_segment("segment_000.ts"), b"TS_CHUNK_DATA")

            # Playlist retrieval when running
            hls._running = True
            hls._process = MagicMock()
            hls._process.poll.return_value = None
            pl_path = Path(tmp_dir) / "stream.m3u8"
            pl_path.write_text("#EXTM3U\nsegment_000.ts\n")
            playlist = await hls.get_playlist(timeout_seconds=0.5)
            self.assertIsNotNone(playlist)
            self.assertIn("segment_000.ts", playlist)

    async def test_hls_transcoder_subscribes_hevc_and_avoids_mjpeg_transcoding(self):
        import asyncio
        import tempfile
        from unittest.mock import MagicMock, patch
        from momcozy.hls import HlsTranscoder

        mock_streamer = MagicMock()
        mock_streamer.is_running = True

        async def fake_hevc():
            yield b"hevc_keyframe", True

        mock_streamer.subscribe_hevc = fake_hevc
        mock_streamer.subscribe = MagicMock()

        with tempfile.TemporaryDirectory() as tmp_dir:
            hls = HlsTranscoder(streamer=mock_streamer, output_dir=tmp_dir)
            with patch("subprocess.Popen") as mock_popen:
                mock_proc = MagicMock()
                mock_proc.stdin = MagicMock()
                mock_proc.poll.return_value = None
                mock_popen.return_value = mock_proc

                started = await hls.start()
                self.assertTrue(started)

                # Verify ffmpeg arguments: -f hevc -framerate 15 -i pipe:0
                mock_popen.assert_called_once()
                cmd = mock_popen.call_args[0][0]
                self.assertIn("-f", cmd)
                idx_f = cmd.index("-f")
                self.assertEqual(cmd[idx_f + 1], "hevc")
                self.assertIn("-framerate", cmd)
                self.assertIn("pipe:0", cmd)

                # Wait briefly for feed task
                await asyncio.sleep(0.05)
                # Ensure subscribe (MJPEG) was NEVER called
                mock_streamer.subscribe.assert_not_called()

                await hls.stop()

    async def test_hls_transcoder_uses_vaapi_when_device_present(self):
        import asyncio
        import tempfile
        from unittest.mock import MagicMock, patch
        from momcozy.hls import HlsTranscoder

        mock_streamer = MagicMock()
        mock_streamer.is_running = True

        async def fake_hevc():
            yield b"hevc_keyframe", True

        mock_streamer.subscribe_hevc = fake_hevc

        with tempfile.TemporaryDirectory() as tmp_dir:
            hls = HlsTranscoder(streamer=mock_streamer, output_dir=tmp_dir, hwaccel="vaapi", vaapi_device="/dev/dri/renderD128")
            with patch("subprocess.Popen") as mock_popen:
                mock_proc = MagicMock()
                mock_proc.stdin = MagicMock()
                mock_proc.poll.return_value = None  # Process stays alive
                mock_popen.return_value = mock_proc

                started = await hls.start()
                self.assertTrue(started)
                self.assertTrue(hls.using_vaapi)

                # Verify VA-API hardware encoder arguments
                mock_popen.assert_called_once()
                cmd = mock_popen.call_args[0][0]
                self.assertIn("-init_hw_device", cmd)
                idx_hw = cmd.index("-init_hw_device")
                self.assertEqual(cmd[idx_hw + 1], "vaapi=hw:/dev/dri/renderD128")
                self.assertIn("-c:v", cmd)
                idx_cv = cmd.index("-c:v")
                self.assertEqual(cmd[idx_cv + 1], "h264_vaapi")
                self.assertIn("-vf", cmd)
                idx_vf = cmd.index("-vf")
                self.assertEqual(cmd[idx_vf + 1], "format=nv12,hwupload")

                await hls.stop()
                self.assertFalse(hls.using_vaapi)

    async def test_hls_transcoder_vaapi_fallback_to_software(self):
        import asyncio
        import tempfile
        from unittest.mock import MagicMock, patch
        from momcozy.hls import HlsTranscoder

        mock_streamer = MagicMock()
        mock_streamer.is_running = True

        async def fake_hevc():
            yield b"hevc_keyframe", True

        mock_streamer.subscribe_hevc = fake_hevc

        with tempfile.TemporaryDirectory() as tmp_dir:
            hls = HlsTranscoder(streamer=mock_streamer, output_dir=tmp_dir, hwaccel="vaapi", vaapi_device="/dev/dri/renderD128")
            with patch("subprocess.Popen") as mock_popen:
                vaapi_fail_proc = MagicMock()
                vaapi_fail_proc.poll.return_value = 1  # Fails immediately
                vaapi_fail_proc.returncode = 1
                vaapi_fail_proc.stderr = MagicMock()
                vaapi_fail_proc.stderr.read.return_value = b"vaInitialize failed: driver error"

                software_proc = MagicMock()
                software_proc.stdin = MagicMock()
                software_proc.poll.return_value = None  # Stays alive

                mock_popen.side_effect = [vaapi_fail_proc, software_proc]

                started = await hls.start()
                self.assertTrue(started)
                self.assertFalse(hls.using_vaapi)

                # Popen should have been called twice: 1st for VA-API, 2nd for software fallback
                self.assertEqual(mock_popen.call_count, 2)
                second_cmd = mock_popen.call_args_list[1][0][0]
                self.assertIn("-c:v", second_cmd)
                idx_cv = second_cmd.index("-c:v")
                self.assertEqual(second_cmd[idx_cv + 1], "libx264")

                await hls.stop()

    async def test_handle_hls_endpoints(self):
        from unittest.mock import AsyncMock, MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        mock_monitor = MagicMock()
        mock_streamer = MagicMock()
        server = WebServer(monitor=mock_monitor, streamer=mock_streamer)

        # Mock hls
        server.hls = MagicMock()
        server.hls.is_available = True
        server.hls.get_playlist = AsyncMock(return_value="#EXTM3U\n#EXTINF:2.0,\nsegment_000.ts\n")
        server.hls.get_segment = MagicMock(return_value=b"TS_SEGMENT_BYTES")

        # Test playlist endpoint
        req_pl = make_mocked_request("GET", "/hls/stream.m3u8")
        resp_pl = await server.handle_hls_playlist(req_pl)
        self.assertEqual(resp_pl.status, 200)
        self.assertEqual(resp_pl.content_type.lower(), "application/vnd.apple.mpegurl")
        self.assertIn("segment_000.ts", resp_pl.text)
        self.assertEqual(resp_pl.headers.get("Access-Control-Allow-Origin"), "*")

        # Test segment endpoint
        req_seg = make_mocked_request("GET", "/hls/segment_000.ts", match_info={"segment": "segment_000.ts"})
        resp_seg = await server.handle_hls_segment(req_seg)
        self.assertEqual(resp_seg.status, 200)
        self.assertEqual(resp_seg.content_type.lower(), "video/mp2t")
        self.assertEqual(resp_seg.body, b"TS_SEGMENT_BYTES")
        self.assertEqual(resp_seg.headers.get("Access-Control-Allow-Origin"), "*")

    def test_create_ssl_context_self_signed(self):
        import ssl
        from momcozy.web import create_ssl_context

        ctx = create_ssl_context()
        self.assertIsInstance(ctx, ssl.SSLContext)

    async def test_webserver_http_port_and_index_injection(self):
        import ssl
        from unittest.mock import MagicMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        mock_monitor = MagicMock()
        # Test SSL enabled assigns default 8420
        srv_ssl = WebServer(monitor=mock_monitor, port=8421, ssl_context=MagicMock(spec=ssl.SSLContext))
        self.assertEqual(srv_ssl.http_port, 8420)

        # Test index injection contains http_port
        req = make_mocked_request("GET", "/")
        resp = await srv_ssl.handle_index(req)
        self.assertEqual(resp.status, 200)
        self.assertIn("window.serverHttpPort = 8420;", resp.text)

    def test_hevc_nalu_parsing_and_keyframe_detection(self):
        from momcozy.stream import parse_hevc_nalus, is_hevc_keyframe

        # VPS NALU (type 32: (0x40 >> 1) == 32)
        vps = b"\x00\x00\x00\x01\x40\x01\x0c\x01"
        # SPS NALU (type 33: (0x42 >> 1) == 33)
        sps = b"\x00\x00\x00\x01\x42\x01\x01\x01"
        # PPS NALU (type 34: (0x44 >> 1) == 34)
        pps = b"\x00\x00\x00\x01\x44\x01\xc0"
        # IDR NALU (type 19: (0x26 >> 1) == 19)
        idr = b"\x00\x00\x00\x01\x26\x01\xaf\xfe"
        # Non-IRAP P-slice (type 1: (0x02 >> 1) == 1)
        p_slice = b"\x00\x00\x00\x01\x02\x01\x12\x34"

        combined = vps + sps + pps + idr
        nalus = parse_hevc_nalus(combined)
        self.assertEqual(len(nalus), 4)
        self.assertEqual(nalus[0][0], 32)
        self.assertEqual(nalus[1][0], 33)
        self.assertEqual(nalus[2][0], 34)
        self.assertEqual(nalus[3][0], 19)

        self.assertTrue(is_hevc_keyframe(combined))
        self.assertTrue(is_hevc_keyframe(idr))
        self.assertTrue(is_hevc_keyframe(vps))
        self.assertFalse(is_hevc_keyframe(p_slice))

    async def test_video_streamer_subscribe_hevc(self):
        from momcozy.stream import VideoStreamer
        streamer = VideoStreamer("dev123", "localkey")
        self.assertEqual(streamer.hevc_listener_count, 0)

        # Mock latest keyframe
        streamer._latest_hevc_keyframe = b"\x00\x00\x00\x01\x26\x01KEY"
        streamer._connected = True
        streamer._running = True

        gen = streamer.subscribe_hevc()
        chunk, is_key = await gen.__anext__()
        self.assertEqual(chunk, b"\x00\x00\x00\x01\x26\x01KEY")
        self.assertTrue(is_key)
        self.assertEqual(streamer.hevc_listener_count, 1)
        await gen.aclose()
        self.assertEqual(streamer.hevc_listener_count, 0)

    async def test_web_server_dashboard_webcodecs_and_fallback(self):
        from momcozy.web import HTML_DASHBOARD
        self.assertIn('id="stream-canvas"', HTML_DASHBOARD)
        self.assertIn('id="stream-img"', HTML_DASHBOARD)
        self.assertIn("checkHevcWebCodecsSupport", HTML_DASHBOARD)
        self.assertIn("fallbackToMjpeg", HTML_DASHBOARD)
        self.assertIn("startMjpegStream", HTML_DASHBOARD)
        self.assertIn("startWebCodecsStream", HTML_DASHBOARD)
        self.assertIn("initPlayer", HTML_DASHBOARD)

    async def test_web_server_ws_stream_unconfigured(self):
        from unittest.mock import MagicMock, AsyncMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        server = WebServer(monitor=MagicMock(), streamer=None)
        req = make_mocked_request("GET", "/ws/stream")
        with unittest.mock.patch("aiohttp.web.WebSocketResponse") as mock_ws_cls:
            mock_ws = MagicMock()
            mock_ws.prepare = AsyncMock()
            mock_ws.close = AsyncMock()
            mock_ws_cls.return_value = mock_ws

            resp = await server.handle_ws_stream(req)
            self.assertEqual(resp, mock_ws)
            mock_ws.close.assert_awaited_once_with(code=4004, message=b"Streamer not configured")

    async def test_handle_ws_stream_streaming_and_reader(self):
        from unittest.mock import MagicMock, AsyncMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer

        mock_streamer = MagicMock()
        mock_streamer.is_running = True

        async def fake_hevc():
            yield b"keyframe_bytes", True
            yield b"delta_bytes", False

        mock_streamer.subscribe_hevc = fake_hevc

        server = WebServer(monitor=MagicMock(), streamer=mock_streamer)
        req = make_mocked_request("GET", "/ws/stream")

        with unittest.mock.patch("aiohttp.web.WebSocketResponse") as mock_ws_cls:
            mock_ws = MagicMock()
            mock_ws.prepare = AsyncMock()
            mock_ws.close = AsyncMock()
            mock_ws.send_bytes = AsyncMock()
            mock_ws.closed = False

            async def fake_incoming():
                # simulate reader receiving nothing and ending
                if False:
                    yield None

            mock_ws.__aiter__ = lambda self: fake_incoming()
            mock_ws_cls.return_value = mock_ws

            resp = await server.handle_ws_stream(req)
            self.assertEqual(resp, mock_ws)
            # Verify sent frames with 1-byte header: \x01 for key, \x00 for delta
            self.assertEqual(mock_ws.send_bytes.call_count, 2)
            mock_ws.send_bytes.assert_any_call(b"\x01keyframe_bytes")
            mock_ws.send_bytes.assert_any_call(b"\x00delta_bytes")

    def test_is_transcoding_needed_and_handler_bypass(self):
        import asyncio
        from unittest.mock import MagicMock
        from momcozy.stream import VideoStreamer, MomcozyHevcHandler

        streamer = VideoStreamer("dev123", "localkey")
        # No listeners: False
        self.assertFalse(streamer.is_transcoding_needed())

        # Only HEVC passthrough listener: False
        q_hevc = asyncio.Queue()
        streamer._hevc_listeners.add(q_hevc)
        self.assertFalse(streamer.is_transcoding_needed())

        # Active JPEG listener: True
        q_jpeg = asyncio.Queue()
        streamer._listeners.add(q_jpeg)
        self.assertTrue(streamer.is_transcoding_needed())

        # Disconnect JPEG listener: False even though HEVC listener is still connected
        streamer._listeners.discard(q_jpeg)
        self.assertFalse(streamer.is_transcoding_needed())

        # Test stream_format property
        self.assertEqual(streamer.stream_format, "Idle")
        streamer._connected = True
        self.assertEqual(streamer.stream_format, "HEVC (Pass-through)")
        streamer._listeners.add(q_jpeg)
        self.assertEqual(streamer.stream_format, "MJPEG (Transcoded)")
        streamer._listeners.discard(q_jpeg)
        streamer._hevc_listeners.discard(q_hevc)
        self.assertEqual(streamer.stream_format, "HEVC (Native)")
        streamer._connected = False

        # Snapshot requested: True
        streamer._snapshot_requested = True
        self.assertTrue(streamer.is_transcoding_needed())
        streamer._snapshot_requested = False

        # Test MomcozyHevcHandler bypasses codec when is_transcoding_needed returns False
        mock_frame_cb = MagicMock()
        mock_hevc_cb = MagicMock()
        handler = MomcozyHevcHandler(
            on_frame=mock_frame_cb,
            on_hevc=mock_hevc_cb,
            is_transcoding_needed=lambda: False,
        )
        handler._codec = MagicMock()

        # Properly formatted Tuya packet with IDR slice (type 19) to complete the Access Unit
        from momcozy.stream import _SYNC_MARKER
        import struct
        payload = b"\x00" * 12 + b"\x26\x01\x0c\x01"
        record = _SYNC_MARKER + b"\x00" * 8 + struct.pack("<I", 0) + struct.pack("<I", 0) + struct.pack("<I", len(payload)) + payload
        handler.feed_record(record)

        # HEVC callback should have been called with complete Access Unit
        self.assertTrue(mock_hevc_cb.called)
        # But codec.parse should NOT have been called because transcoding was not needed!
        self.assertFalse(handler._codec.parse.called)
        self.assertFalse(mock_frame_cb.called)

    def test_client_auto_promotion_and_retry_script(self):
        from momcozy.web import HTML_DASHBOARD

        # Verify client script includes fatal decoder error tracking
        self.assertIn("hasFatalDecoderError = false", HTML_DASHBOARD)
        # Verify client waits for camera stream instead of falling back after arbitrary attempts
        self.assertIn("Waiting for camera stream", HTML_DASHBOARD)
        # Verify decoder error recovery attempts before fatal fallback
        self.assertIn("Recovering from decoder error", HTML_DASHBOARD)
        # Verify retryStream resets fallback if supported
        self.assertIn("if (supportedHevcCodec)", HTML_DASHBOARD)
        # Verify auto-promotion in updateStatus
        self.assertIn("Auto-promoting from MJPEG to WebCodecs HEVC", HTML_DASHBOARD)

    def test_hevc_handler_build_au_parameter_sets(self):
        from momcozy.stream import MomcozyHevcHandler
        handler = MomcozyHevcHandler(lambda f: None)

        # Slice candidate without SPS/PPS should not be marked as keyframe
        raw_slice = b"\x26\x01\x02\x03"
        au, is_key = handler._build_au(raw_slice, is_key_candidate=True)
        self.assertFalse(is_key)
        self.assertEqual(au, raw_slice)

        # Feed VPS, SPS, PPS
        vps = b"\x00\x00\x00\x01\x40\x01\x0c\x01"
        sps = b"\x00\x00\x00\x01\x42\x01\x01\x01"
        pps = b"\x00\x00\x00\x01\x44\x01\xc0"
        handler._latest_vps = vps
        handler._latest_sps = sps
        handler._latest_pps = pps

        # Now slice candidate should have VPS+SPS+PPS prepended and be marked as keyframe
        au, is_key = handler._build_au(raw_slice, is_key_candidate=True)
        self.assertTrue(is_key)
        self.assertTrue(au.startswith(vps))
        self.assertIn(sps, au)
        self.assertIn(pps, au)
        self.assertTrue(au.endswith(raw_slice))

    def test_streamer_initial_timeout_configuration(self):
        import inspect
        from momcozy.stream import VideoStreamer

        source = inspect.getsource(VideoStreamer._stream_loop)
        self.assertIn("initial_timeout = 20.0", source)
        self.assertIn("Do not poison _last_error on initial cold-start reconnect", source)

    def test_decode_hevc_gop_to_jpeg(self):
        import io
        import av
        import numpy as np
        from momcozy.stream import decode_hevc_gop_to_jpeg

        # Empty chunks
        self.assertIsNone(decode_hevc_gop_to_jpeg([]))

        # Synthesize 3 HEVC frames (1 IDR + 2 delta)
        buf = io.BytesIO()
        container = av.open(buf, mode="w", format="hevc")
        stream = container.add_stream("hevc", rate=15)
        stream.width = 160
        stream.height = 120
        stream.pix_fmt = "yuv420p"

        for i in range(3):
            img = np.zeros((120, 160, 3), dtype=np.uint8)
            img[:, :] = (i * 40, 50, 100)
            frame = av.VideoFrame.from_ndarray(img, format="bgr24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()

        hevc_data = buf.getvalue()
        self.assertGreater(len(hevc_data), 0)

        # Decode GOP chunks to JPEG
        jpg_bytes = decode_hevc_gop_to_jpeg([hevc_data])
        self.assertIsNotNone(jpg_bytes)
        self.assertTrue(jpg_bytes.startswith(b"\xff\xd8"))  # JPEG header
        self.assertTrue(jpg_bytes.endswith(b"\xff\xd9"))    # JPEG footer

    async def test_get_snapshot_with_rolling_gop_buffer(self):
        import io
        import av
        import numpy as np
        from momcozy.stream import VideoStreamer

        streamer = VideoStreamer("dev123", "localkey")
        self.assertEqual(streamer.gop_buffer_size, 0)

        # Synthesize HEVC GOP
        buf = io.BytesIO()
        container = av.open(buf, mode="w", format="hevc")
        stream = container.add_stream("hevc", rate=15)
        stream.width = 160
        stream.height = 120
        stream.pix_fmt = "yuv420p"
        img = np.zeros((120, 160, 3), dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(img, format="bgr24")
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()

        # Seed rolling GOP buffer
        streamer._running = True
        streamer._gop_chunks = [buf.getvalue()]
        self.assertEqual(streamer.gop_buffer_size, 1)

        # get_snapshot should burst-decode the buffered GOP and return immediately
        snap = await streamer.get_snapshot(max_age=0.0)
        self.assertIsNotNone(snap)
        self.assertTrue(snap.startswith(b"\xff\xd8"))
        self.assertEqual(streamer.latest_frame, snap)

        # Verify stop clears the GOP buffer
        await streamer.stop()
        self.assertEqual(streamer.gop_buffer_size, 0)
        self.assertIsNone(streamer.latest_frame)

    async def test_timelapse_recorder_circular_ring_buffer(self):
        import asyncio
        import tempfile
        import time
        from pathlib import Path
        from unittest.mock import MagicMock, AsyncMock
        from momcozy.timelapse import TimelapseRecorder, format_duration, format_video_duration

        with tempfile.TemporaryDirectory() as td:
            mock_streamer = MagicMock()
            mock_streamer.is_running = True

            # Create 3-slot recorder with very short duration for testing (1s interval, 2 frames per slot)
            recorder = TimelapseRecorder(
                streamer=mock_streamer,
                storage_dir=td,
                total_slots=3,
                slot_duration_sec=10.0,
                sample_interval_sec=0.01,
                fps=30,
            )

            # Check initial status
            st = recorder.get_status()
            self.assertFalse(st["is_recording"])
            self.assertEqual(st["total_slots"], 3)
            self.assertEqual(st["filled_slots"], 0)
            self.assertFalse(st["has_content"])

            # Test slot paths
            self.assertEqual(recorder.slot_path(0).name, "slot_00.hevc")
            self.assertEqual(recorder.slot_path(2).name, "slot_02.hevc")

            # Simulate incoming HEVC chunks
            dummy_key_1 = b"\x00\x00\x00\x01\x40\x01" + b"A" * 100
            dummy_key_2 = b"\x00\x00\x00\x01\x40\x01" + b"B" * 100
            dummy_delta = b"\x00\x00\x00\x01\x02\x01" + b"C" * 50

            async def fake_hevc_stream():
                # Yield keyframe, delta (should be ignored), keyframe
                yield dummy_key_1, True
                yield dummy_delta, False
                yield dummy_key_2, True
                try:
                    while True:
                        await asyncio.sleep(10)
                except asyncio.CancelledError:
                    pass

            mock_streamer.subscribe_hevc = fake_hevc_stream

            # Start recording
            await recorder.start()
            self.assertTrue(recorder.is_recording)

            # Give it a moment to process the frames
            await asyncio.sleep(0.1)

            await recorder.stop()
            self.assertFalse(recorder.is_recording)

            st = recorder.get_status()
            self.assertTrue(st["has_content"])
            self.assertGreater(st["total_frames"], 0)

            # Test slot rotation and direct overwrite
            # Slot 0 should exist with content
            slot0 = recorder.slot_path(0)
            self.assertTrue(slot0.exists())
            self.assertGreater(slot0.stat().st_size, 0)

            # Simulate rotating through all slots and verifying overwrite
            recorder._rotate_slot()
            self.assertEqual(recorder._current_slot, 1)
            recorder._active_file.write(dummy_key_1)
            recorder._rotate_slot()
            self.assertEqual(recorder._current_slot, 2)
            recorder._active_file.write(dummy_key_2)
            recorder._rotate_slot()
            # Should have wrapped around to 0
            self.assertEqual(recorder._current_slot, 0)
            # Writing to slot 0 truncates and overwrites
            recorder._active_file.write(b"NEW_DATA")
            recorder._close_active_slot()

            # Verify slot 0 content was overwritten with NEW_DATA
            self.assertEqual(recorder.slot_path(0).read_bytes(), b"NEW_DATA")

            # Test starting fresh clears previous recordings
            self.assertTrue(recorder.slot_path(0).exists())
            recorder.clear()
            self.assertEqual(recorder._current_slot, 0)
            self.assertEqual(recorder._frames_in_slot, 0)
            self.assertEqual(len(recorder._slots_meta), 0)
            self.assertFalse(recorder.slot_path(0).exists())

            # Check format utilities
            self.assertEqual(format_duration(3600), "1h 00m")
            self.assertEqual(format_duration(45), "0m 45s")
            self.assertEqual(format_video_duration(720), "12:00")
            self.assertEqual(format_video_duration(65), "1:05")

    async def test_timelapse_web_endpoints(self):
        import asyncio
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, AsyncMock
        from aiohttp.test_utils import make_mocked_request
        from momcozy.web import WebServer
        from momcozy.timelapse import TimelapseRecorder

        with tempfile.TemporaryDirectory() as td:
            mock_streamer = MagicMock()
            mock_streamer.is_running = True

            server = WebServer(monitor=MagicMock(), streamer=mock_streamer)
            server.timelapse = TimelapseRecorder(streamer=mock_streamer, storage_dir=td, total_slots=3)

            # Test GET /api/timelapse/status
            req = make_mocked_request("GET", "/api/timelapse/status")
            resp = await server.handle_timelapse_status(req)
            self.assertEqual(resp.status, 200)

            # Test POST /api/timelapse/start
            req = make_mocked_request("POST", "/api/timelapse/start")
            with unittest.mock.patch.object(server.timelapse, "start", AsyncMock()) as mock_start:
                resp = await server.handle_timelapse_start(req)
                self.assertEqual(resp.status, 200)
                mock_start.assert_called_once()

            # Test POST /api/timelapse/stop
            req = make_mocked_request("POST", "/api/timelapse/stop")
            with unittest.mock.patch.object(server.timelapse, "stop", AsyncMock()) as mock_stop:
                resp = await server.handle_timelapse_stop(req)
                self.assertEqual(resp.status, 200)
                mock_stop.assert_called_once()

            # Test GET /api/timelapse/video.mp4 when empty (should 404)
            req = make_mocked_request("GET", "/api/timelapse/video.mp4")
            resp = await server.handle_timelapse_video(req)
            self.assertEqual(resp.status, 404)

            # Test GET /api/timelapse/video.mp4 when video exists
            dummy_mp4 = Path(td) / "dummy.mp4"
            dummy_mp4.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100)

            with unittest.mock.patch.object(server.timelapse, "export_mp4", AsyncMock(return_value=dummy_mp4)):
                req = make_mocked_request("GET", "/api/timelapse/video.mp4")
                resp = await server.handle_timelapse_video(req)
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Accept-Ranges"), "bytes")
                self.assertIn("inline", resp.headers.get("Content-Disposition", ""))

                # With download query param
                req_dl = make_mocked_request("GET", "/api/timelapse/video.mp4?download=1")
                resp_dl = await server.handle_timelapse_video(req_dl)
                self.assertEqual(resp_dl.status, 200)
                self.assertIn("attachment", resp_dl.headers.get("Content-Disposition", ""))

            # Test GET /timelapse standalone player page
            req_page = make_mocked_request("GET", "/timelapse")
            resp_page = await server.handle_timelapse_page(req_page)
            self.assertEqual(resp_page.status, 200)
            self.assertEqual(resp_page.content_type, "text/html")
            self.assertIn("Timelapse", resp_page.text)
            self.assertIn("1:60", resp_page.text)
            self.assertIn("canPlayHevc", resp_page.text)
            # Verify no codec dropdown is present
            self.assertNotIn("<select", resp_page.text.lower())

            # Test status endpoint includes timelapse info
            req_status = make_mocked_request("GET", "/api/status")
            with unittest.mock.patch.object(server.monitor, "get_status", MagicMock()):
                resp_status = await server.handle_api_status(req_status)
                self.assertEqual(resp_status.status, 200)
                import json
                data = json.loads(resp_status.text)
                self.assertIn("timelapse", data)
                self.assertEqual(data["timelapse"]["total_slots"], 3)

    async def test_timelapse_export_mp4_stream_copy(self):
        import subprocess
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock
        from momcozy.timelapse import TimelapseRecorder

        with tempfile.TemporaryDirectory() as td:
            storage_dir = Path(td)
            mock_streamer = MagicMock()
            mock_streamer.is_running = True

            recorder = TimelapseRecorder(
                streamer=mock_streamer,
                storage_dir=storage_dir,
                total_slots=4,
                fps=30,
            )

            # Generate 2 valid synthetic HEVC chunks using ffmpeg
            slot0 = recorder.slot_path(0)
            slot1 = recorder.slot_path(1)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=30",
                    "-c:v", "libx265", "-f", "hevc", str(slot0),
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=30",
                    "-c:v", "libx265", "-f", "hevc", str(slot1),
                ],
                check=True,
                capture_output=True,
            )

            recorder._slots_meta["0"] = {"mtime": 1000.0, "frames": 30, "bytes": slot0.stat().st_size}
            recorder._slots_meta["1"] = {"mtime": 2000.0, "frames": 30, "bytes": slot1.stat().st_size}

            ordered = recorder.get_ordered_slots()
            self.assertEqual(len(ordered), 2)
            self.assertEqual(ordered[0][0], 0)
            self.assertEqual(ordered[1][0], 1)

            # Export MP4 via stream copy (native HEVC, 0% CPU transcoding)
            mp4_path = await recorder.export_mp4(codec="hevc")
            self.assertIsNotNone(mp4_path)
            self.assertTrue(mp4_path.exists())
            self.assertGreater(mp4_path.stat().st_size, 1000)

            # Verify that calling export_mp4 again returns the cached file
            mp4_path_2 = await recorder.export_mp4(codec="hevc")
            self.assertEqual(mp4_path, mp4_path_2)

            # Test export with h264 fallback
            mp4_h264 = await recorder.export_mp4(codec="h264")
            self.assertIsNotNone(mp4_h264)
            self.assertTrue(mp4_h264.exists())
            self.assertGreater(mp4_h264.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()


