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


if __name__ == "__main__":
    unittest.main()


