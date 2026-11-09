"""Unit tests for Momcozy Cloud API, Tuya Gateway Client, and TuyaSessionManager."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives.asymmetric import padding, rsa

from momcozy.cloud import MomcozyCloudApi, TuyaCredentials, hash_password
from momcozy.tuya_cloud import (
    TuyaGatewayClient,
    TuyaSession,
    TuyaSessionManager,
    build_sign_string,
    decrypt_post_data,
    encrypt_password_rsa,
    encrypt_post_data,
    md5_hex,
    post_data_sign_field,
    pre_login_body_key,
    resolve_tuya_region,
    session_body_key,
    sign_params,
    swap_sign_string,
)


class TestMomcozyPasswordHash(unittest.TestCase):
    def test_argon2_hashing(self):
        h1 = hash_password("Secret123")
        h2 = hash_password("Secret123")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 44)
        self.assertTrue(h1.endswith("="))

        h3 = hash_password("OtherPassword")
        self.assertNotEqual(h1, h3)


class TestMomcozyDeviceShareAndSession(unittest.TestCase):
    def setUp(self):
        self.api = MomcozyCloudApi(region="EU", device_id="test_dev_123")
        self.api.token = "mock_token"

    def test_search_tuya_user(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": "200",
            "result": {
                "memberCode": "MEM999",
                "uid": "UID999",
                "region": "33",
            },
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

    def test_remove_device_share_revokes_tuya_relation_and_momcozy_metadata(self):
        self.api.tuya_creds = TuyaCredentials(region="33", uid="owner_uid", token="owner_tok")
        self.api.tuya_session = {"sid": "s1", "ecode": "e1", "uid": "owner_uid"}

        mock_tclient = MagicMock()
        mock_tclient.query_shared_users.return_value = [
            {"relationId": 98765, "userName": "MEM_TARGET", "autoRemarkName": "partner@example.com"},
            {"relationId": 11111, "userName": "OTHER_USER"},
        ]
        mock_tclient.revoke_device_share.return_value = True

        with patch.object(self.api, "get_tuya_client", return_value=mock_tclient), \
             patch.object(self.api, "delete_device_share", return_value={"deleted": True}) as mock_del:
            res = self.api.remove_device_share(
                member_code="MEM_TARGET",
                tuya_dev_id="dev_cam_1",
            )
            mock_tclient.query_shared_users.assert_called_once()
            mock_tclient.revoke_device_share.assert_called_once()
            self.assertEqual(mock_tclient.revoke_device_share.call_args[0][1], "dev_cam_1")
            self.assertEqual(mock_tclient.revoke_device_share.call_args[0][2], 98765)
            mock_del.assert_called_once_with("dev_cam_1", "MEM_TARGET")
            self.assertEqual(res["revoked_relation_ids"], [98765])

    def test_save_session_preserves_existing_device_keys(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".momcozy.json"
            cfg.write_text(
                json.dumps(
                    {
                        "local_key": "existing_16b_key",
                        "camera_name": "Nursery Cam",
                        "mac": "AA:BB:CC:DD:EE:FF",
                        "client_install_id": "install-uuid-123",
                        "ip": "10.11.2.186",
                        "dps": {"142": 215},
                        "timelapse_sync_night_vision": True,
                    }
                )
            )
            api = MomcozyCloudApi(region="EU")
            api.email = "parent@example.com"
            api.token = "fresh_jwt"
            api.save_session(cfg)

            merged = json.loads(cfg.read_text())
            self.assertEqual(merged["email"], "parent@example.com")
            self.assertEqual(merged["token"], "fresh_jwt")
            self.assertEqual(merged["local_key"], "existing_16b_key")
            self.assertEqual(merged["camera_name"], "Nursery Cam")
            self.assertEqual(merged["mac"], "AA:BB:CC:DD:EE:FF")
            self.assertEqual(merged["client_install_id"], "install-uuid-123")
            self.assertEqual(merged["ip"], "10.11.2.186")
            self.assertEqual(merged["dps"], {"142": 215})
            self.assertTrue(merged["timelapse_sync_night_vision"])


class TestTuyaCloudGateway(unittest.TestCase):
    def test_resolve_tuya_region(self):
        self.assertEqual(resolve_tuya_region("33"), "eu")
        self.assertEqual(resolve_tuya_region("EU"), "eu")
        self.assertEqual(resolve_tuya_region("86"), "cn")
        self.assertEqual(resolve_tuya_region("CN"), "cn")
        self.assertEqual(resolve_tuya_region("91"), "in")
        self.assertEqual(resolve_tuya_region("1"), "us")
        self.assertEqual(resolve_tuya_region(None), "us")

    def test_md5_and_swap_sign_string(self):
        raw = "test_string"
        h = md5_hex(raw)
        self.assertEqual(len(h), 32)
        swapped = swap_sign_string(h)
        self.assertEqual(len(swapped), 32)
        self.assertEqual(swapped, h[8:16] + h[0:8] + h[24:32] + h[16:24])
        self.assertEqual(post_data_sign_field(raw), swapped)

    def test_build_sign_string_and_sign_params(self):
        params = {
            "a": "smartlife.m.sharing.device.account.add",
            "v": "3.0",
            "time": "1700000000",
            "ignore_this": "value",
            "empty_param": "",
        }
        sign_str = build_sign_string(params)
        self.assertEqual(sign_str, "a=smartlife.m.sharing.device.account.add||time=1700000000||v=3.0")
        sig = sign_params("a=test||v=1.0", key="my_secret_key")
        self.assertEqual(len(sig), 64)

    def test_body_keys_and_post_data_crypto(self):
        k1 = pre_login_body_key("req123", key="comp_key")
        k2 = session_body_key("req123", "ecode456", key="comp_key")
        self.assertEqual(len(k1), 16)
        self.assertEqual(len(k2), 16)
        self.assertNotEqual(k1, k2)

        key = "0123456789abcdef"
        msg = b'{"hello": "world", "number": 42}'
        encrypted = encrypt_post_data(key, msg)
        decrypted = decrypt_post_data(key, encrypted)
        self.assertEqual(decrypted, msg)

    def test_encrypt_password_rsa(self):
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
        client = TuyaGatewayClient(region="eu")

        def mock_call_api(api, version, post_data, session=None, extra_params=None):
            if api == "smartlife.m.user.username.token.get":
                return {
                    "publicKey": "123456789",
                    "exponent": "65537",
                    "token": "rsa_token_abc",
                }
            if "smartlife.m.user.uid.password.login" in api:
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

    def test_home_and_shared_device_queries(self):
        client = TuyaGatewayClient(region="eu")
        session = TuyaSession(sid="sid1", ecode="eco1", uid="uid1")

        with patch.object(client, "call_api", return_value=[{"gid": 998877, "name": "My Home"}]):
            self.assertEqual(client.get_home_id(session), 998877)

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
            self.assertEqual(mock_api.call_args[0][0], "smartlife.m.sharing.device.account.add")

        home_devs = [{"devId": "dev1", "name": "Home Cam", "localKey": "key1"}]
        shared_devs = [{"devId": "dev2", "name": "Shared Cam", "localKey": "key2"}]
        with patch.object(client, "get_home_devices", return_value=home_devs), \
             patch.object(client, "get_shared_devices", return_value=shared_devs), \
             patch.object(client, "get_home_id", return_value=123):
            all_devs = client.get_all_devices(session)
            self.assertEqual({d["devId"] for d in all_devs}, {"dev1", "dev2"})

    def test_get_all_devices_propagates_error_when_all_fail(self):
        client = TuyaGatewayClient(region="eu")
        session = TuyaSession(sid="sid1", ecode="eco1", uid="uid1")
        with patch.object(client, "get_home_id", side_effect=RuntimeError("Home service down")), \
             patch.object(client, "get_shared_devices", side_effect=RuntimeError("Shared service down")):
            with self.assertRaises(RuntimeError) as ctx:
                client.get_all_devices(session)
            self.assertIn("Home service down", str(ctx.exception))
            self.assertIn("Shared service down", str(ctx.exception))

    def test_tuya_gateway_publish_dps_and_session_invalid(self):
        client = TuyaGatewayClient(region="eu")
        client.call_api = MagicMock(return_value=True)
        session = TuyaSession(sid="sid1", ecode="ec1", uid="uid1")

        self.assertTrue(client.publish_dps(session, "dev123", {"119": "0"}))
        api_name, ver, payload = client.call_api.call_args[0][:3]
        self.assertEqual(api_name, "smartlife.m.device.dp.publish")
        self.assertEqual(ver, "1.0")
        self.assertEqual(payload["devId"], "dev123")

        client.call_api = MagicMock(side_effect=RuntimeError("USER_SESSION_INVALID"))
        with self.assertRaises(RuntimeError):
            client.get_device_info(session, "dev123")

    def test_sync_devices_and_keys(self):
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
                saved = json.loads(tmp_path.read_text())
                self.assertEqual(saved["local_key"], "abcdef0123456789")
                self.assertEqual(saved["camera_name"], "Baby Room")
                self.assertEqual(saved["mac"], "50:7B:91:11:22:33")
                self.assertEqual(saved["ip"], "10.11.2.12")
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def test_sync_devices_and_keys_propagates_cloud_errors(self):
        api = MomcozyCloudApi()
        api.token = "fake_token"
        with patch.object(api, "query_devices", side_effect=RuntimeError("Momcozy 500 error")), \
             patch.object(api, "get_tuya_devices", side_effect=RuntimeError("Tuya gateway timeout")):
            with self.assertRaises(RuntimeError) as ctx:
                api.sync_devices_and_keys(raise_on_error=True)
            self.assertIn("Momcozy 500 error", str(ctx.exception))
            self.assertIn("Tuya gateway timeout", str(ctx.exception))

    def test_tuya_session_serialization_and_cloud_persistence(self):
        sess = TuyaSession(sid="sid123", ecode="ecode456", uid="uid789", device_fingerprint="fp999")
        restored = TuyaSession.from_dict(sess.to_dict())
        self.assertIsNotNone(restored)
        self.assertEqual(restored.sid, "sid123")
        self.assertEqual(restored.ecode, "ecode456")
        self.assertIsNone(TuyaSession.from_dict({}))
        self.assertIsNone(TuyaSession.from_dict("invalid"))

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / ".momcozy.json"
            api = MomcozyCloudApi()
            api.email = "test@example.com"
            api.token = "token123"
            api.tuya_session = {"sid": "s1", "ecode": "e1", "uid": "u1", "device_fingerprint": "fp1"}
            api.save_session(cfg_path)

            loaded = MomcozyCloudApi.load_session(cfg_path)
            self.assertEqual(loaded.tuya_session["sid"], "s1")

            new_sess = TuyaSession(sid="s2", ecode="e2", uid="u2", device_fingerprint="fp2")
            loaded.update_tuya_session(new_sess, cfg_path)
            reloaded = MomcozyCloudApi.load_session(cfg_path)
            self.assertEqual(reloaded.tuya_session["sid"], "s2")

    def test_cloud_api_get_active_tuya_session_reuse(self):
        api = MomcozyCloudApi()
        api.tuya_creds = TuyaCredentials(region="33", uid="u1", token="t1")
        mock_tclient = MagicMock()

        api.tuya_session = {"sid": "cached_s", "ecode": "cached_e", "uid": "u1", "device_fingerprint": "fp"}
        sess = api.get_active_tuya_session(mock_tclient)
        self.assertEqual(sess.sid, "cached_s")
        mock_tclient.login_with_uid.assert_not_called()

        api.tuya_session = None
        mock_tclient.login_with_uid.return_value = TuyaSession(sid="fresh_s", ecode="fresh_e", uid="u1")
        fresh_sess = api.get_active_tuya_session(mock_tclient)
        self.assertEqual(fresh_sess.sid, "fresh_s")
        mock_tclient.login_with_uid.assert_called_once()


class TestTuyaSessionManager(unittest.TestCase):
    def test_session_manager_backoff_and_relogin_on_expiry(self):
        persisted = []
        listeners = []
        mgr = TuyaSessionManager(
            initial_session={"sid": "init_s", "ecode": "init_e", "uid": "u1"},
            default_uid="u1",
            persist_cb=lambda s: persisted.append(s.sid),
        )
        mgr.add_listener(lambda s: listeners.append(s.sid))

        mock_client = MagicMock()
        mock_client.login_with_uid.return_value = TuyaSession(sid="relogin_s", ecode="relogin_e", uid="u1")

        # 1. Uses cached session without hitting login_with_uid
        cached = mgr.get_or_login(mock_client, country_code="33", uid="u1", token="tok1")
        self.assertEqual(cached.sid, "init_s")
        mock_client.login_with_uid.assert_not_called()

        # 2. force_refresh=True re-authenticates, persists, and notifies listeners
        refreshed = mgr.get_or_login(
            mock_client, country_code="33", uid="u1", token="tok1", force_refresh=True
        )
        self.assertEqual(refreshed.sid, "relogin_s")
        self.assertEqual(persisted, ["relogin_s"])
        self.assertEqual(listeners, ["relogin_s"])

        # 3. Rapid consecutive re-logins within 10 minutes trigger sliding-window cooldowns
        mgr_throttle = TuyaSessionManager(default_uid="u1")
        th_client = MagicMock()
        th_client.login_with_uid.side_effect = [
            TuyaSession(sid="s1", ecode="e1", uid="u1"),
            TuyaSession(sid="s2", ecode="e2", uid="u1"),
            TuyaSession(sid="s3", ecode="e3", uid="u1"),
            TuyaSession(sid="s4", ecode="e4", uid="u1"),
        ]

        # 1st login -> backoff defaults to 15s
        res1 = mgr_throttle.get_or_login(th_client, "33", "u1", "tok", force_refresh=True)
        self.assertEqual(res1.sid, "s1")
        self.assertEqual(mgr_throttle.auth_backoff, 15.0)

        # 2nd login in window -> cooldown escalates to 60s
        mgr_throttle.last_auth_attempt = 0.0  # simulate cooldown elapsed
        res2 = mgr_throttle.get_or_login(th_client, "33", "u1", "tok", force_refresh=True)
        self.assertEqual(res2.sid, "s2")
        self.assertEqual(mgr_throttle.auth_backoff, 60.0)

        # 3rd login in window -> cooldown escalates to 300s (5 min)
        mgr_throttle.last_auth_attempt = 0.0
        res3 = mgr_throttle.get_or_login(th_client, "33", "u1", "tok", force_refresh=True)
        self.assertEqual(res3.sid, "s3")
        self.assertEqual(mgr_throttle.auth_backoff, 300.0)

        # 4th login in window -> cooldown escalates to 900s (15 min)
        mgr_throttle.last_auth_attempt = 0.0
        res4 = mgr_throttle.get_or_login(th_client, "33", "u1", "tok", force_refresh=True)
        self.assertEqual(res4.sid, "s4")
        self.assertEqual(mgr_throttle.auth_backoff, 900.0)

        # While within cooldown, further force_refresh calls return None without network hit
        res_throttled = mgr_throttle.get_or_login(th_client, "33", "u1", "tok", force_refresh=True)
        self.assertIsNone(res_throttled)
        self.assertEqual(th_client.login_with_uid.call_count, 4)

        # Adopting session with record_login=False does not increase login count
        mgr_throttle.update_session({"sid": "disk_s", "ecode": "disk_e", "uid": "u1"}, record_login=False)
        self.assertEqual(len(mgr_throttle._recent_logins), 4)

        # 4. Rate-limit error triggers exponential backoff so subsequent calls skip network login storm
        mgr_rl = TuyaSessionManager(default_uid="u1")
        rl_client = MagicMock()
        rl_client.login_with_uid.side_effect = RuntimeError("USER_SESSION_LIMIT")

        first_res = mgr_rl.get_or_login(rl_client, country_code="33", uid="u1", token="tok1")
        self.assertIsNone(first_res)
        self.assertEqual(rl_client.login_with_uid.call_count, 1)
        self.assertFalse(mgr_rl.can_attempt_auth())
        self.assertGreaterEqual(mgr_rl.auth_backoff, 1800.0)

        second_res = mgr_rl.get_or_login(rl_client, country_code="33", uid="u1", token="tok1")
        self.assertIsNone(second_res)
        # Did NOT hit login_with_uid again during backoff window
        self.assertEqual(rl_client.login_with_uid.call_count, 1)


if __name__ == "__main__":
    unittest.main()
