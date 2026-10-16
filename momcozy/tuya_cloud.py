"""Tuya Smart Home Cloud Gateway Client for Momcozy Device Sharing.

Implements the Tuya mobile gateway protocol (AES-128-GCM et=3 encrypted body,
HMAC-SHA256 signature, RSA password encryption) required for registering
device share invites on Tuya Cloud Home SDK.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

# Constants reverse-engineered from Momcozy BM04 APK (com.lute.momcozy) & libthing_security.so
PACKAGE_NAME = "com.lute.momcozy"
CERT_SHA256 = (
    "86:48:51:9D:96:A7:15:C0:3F:27:A1:EF:8A:AC:9E:E2"
    ":B8:2E:83:76:3F:97:E8:8F:2E:45:28:3D:2E:A5:93:F2"
)
DERIVED_KEY = "ker9483upg35v53uqaryum5j3wyhnv3x"
APP_SECRET = "jnmtjkaqcvervwfxeu75kvtnrksgwutp"
COMPOSITE_KEY = f"{PACKAGE_NAME}_{CERT_SHA256}_{DERIVED_KEY}_{APP_SECRET}"

CLIENT_ID = "4t7ucfdpqkpys9fwy5yv"
CH_KEY = "39641be9"
APP_VERSION = "2.9.2"
SDK_VERSION = "7.5.0"
LANGUAGE = "en_US"
TTID = "android"

DEFAULT_DEVICE_FINGERPRINT = "a" * 44

_NONCE_LENGTH = 12
_TAG_LENGTH = 16
_BODY_KEY_LENGTH = 16
_MD5_HEX_LENGTH = 32

_SIGN_WHITELIST: frozenset[str] = frozenset(
    {
        "a",
        "v",
        "lat",
        "lon",
        "lang",
        "deviceId",
        "appVersion",
        "ttid",
        "isH5",
        "h5Token",
        "os",
        "clientId",
        "postData",
        "time",
        "requestId",
        "et",
        "n4h5",
        "sid",
        "chKey",
        "sp",
    }
)

TUYA_GATEWAY_URLS = {
    "eu": "https://a1.tuyaeu.com/api.json",
    "33": "https://a1.tuyaeu.com/api.json",
    "us": "https://a1-us.iotbing.com/api.json",
    "na": "https://a1-us.iotbing.com/api.json",
    "1": "https://a1-us.iotbing.com/api.json",
    "cn": "https://a1.tuyacn.com/api.json",
    "86": "https://a1.tuyacn.com/api.json",
    "in": "https://a1-in.iotbing.com/api.json",
    "91": "https://a1-in.iotbing.com/api.json",
}


def md5_hex(value: str) -> str:
    """Return MD5 hash as hex string."""
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def swap_sign_string(value: str) -> str:
    """Reorder 32-character MD5 hex as s[8:16] + s[0:8] + s[24:32] + s[16:24]."""
    if len(value) != _MD5_HEX_LENGTH:
        return value
    return value[8:16] + value[0:8] + value[24:32] + value[16:24]


def post_data_sign_field(encrypted_post_data: str) -> str:
    """Compute the replacement string for postData in the Tuya signature."""
    return swap_sign_string(md5_hex(encrypted_post_data))


def build_sign_string(params: Dict[str, str]) -> str:
    """Sort whitelisted non-empty parameters and join with '||'."""
    return "||".join(
        f"{key}={params[key]}" for key in sorted(params) if params[key] and key in _SIGN_WHITELIST
    )


def sign_params(sign_string: str, key: str = COMPOSITE_KEY) -> str:
    """Calculate HMAC-SHA256 signature."""
    return hmac.new(key.encode("utf-8"), sign_string.encode("utf-8"), hashlib.sha256).hexdigest()


def pre_login_body_key(request_id: str, key: str = COMPOSITE_KEY) -> str:
    """Derive the body key before session login."""
    return hmac.new(request_id.encode("utf-8"), key.encode("utf-8"), hashlib.sha256).hexdigest()[:_BODY_KEY_LENGTH]


def session_body_key(request_id: str, ecode: str, key: str = COMPOSITE_KEY) -> str:
    """Derive the body key for an active session with ecode."""
    msg = f"{key}_{ecode}".encode("utf-8")
    return hmac.new(request_id.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:_BODY_KEY_LENGTH]


def encrypt_post_data(key: str, post_bytes: bytes) -> str:
    """Encrypt request body using AES-128-GCM and return base64 string."""
    nonce = secrets.token_bytes(_NONCE_LENGTH)
    sealed = AESGCM(key.encode("utf-8")).encrypt(nonce, post_bytes, None)
    return base64.b64encode(nonce + sealed).decode("ascii")


def decrypt_post_data(key: str, encrypted_b64: str) -> bytes:
    """Decrypt base64-encoded AES-128-GCM response body."""
    raw = base64.b64decode(encrypted_b64)
    if len(raw) < _NONCE_LENGTH + _TAG_LENGTH:
        raise ValueError("Encrypted payload shorter than nonce and tag")
    nonce = raw[:_NONCE_LENGTH]
    ciphertext = raw[_NONCE_LENGTH:]
    return AESGCM(key.encode("utf-8")).decrypt(nonce, ciphertext, None)


def encrypt_password_rsa(modulus_decimal: str, exponent_decimal: str, password_md5: str) -> str:
    """Encrypt MD5 password hex using RSA PKCS#1 v1.5 with Tuya public key."""
    public_key = rsa.RSAPublicNumbers(int(exponent_decimal), int(modulus_decimal)).public_key()
    return public_key.encrypt(password_md5.encode("ascii"), padding.PKCS1v15()).hex()


@dataclass
class TuyaSession:
    """Active Tuya Cloud session credentials."""
    sid: str
    ecode: str
    uid: str
    device_fingerprint: str = DEFAULT_DEVICE_FINGERPRINT


class TuyaGatewayClient:
    """Synchronous Tuya Smart Home Gateway client for Momcozy OEM Tuya SDK."""

    def __init__(
        self,
        region: str = "eu",
        device_fingerprint: str = DEFAULT_DEVICE_FINGERPRINT,
        session: Optional[requests.Session] = None,
        composite_key: str = COMPOSITE_KEY,
        base_url: Optional[str] = None,
    ) -> None:
        self.region = region.lower()
        self.device_fingerprint = device_fingerprint
        self.composite_key = composite_key
        self.http_session = session or requests.Session()
        self.base_url = base_url or TUYA_GATEWAY_URLS.get(self.region, TUYA_GATEWAY_URLS["eu"])

    def call_api(
        self,
        api: str,
        version: str,
        post_data: Any,
        session: Optional[TuyaSession] = None,
        extra_params: Optional[Dict[str, str]] = None,
        timeout: int = 15,
    ) -> Any:
        """Issue a signed, AES-GCM encrypted et=3 request to Tuya Gateway and return result."""
        # ThingApiParams.checkAPIName() in Tuya Android SDK rewrites "thing." prefix to "smartlife."
        if api.startswith("thing."):
            api = "smartlife." + api[len("thing."):]

        request_id = str(uuid.uuid4())
        key = (
            session_body_key(request_id, session.ecode, self.composite_key)
            if session
            else pre_login_body_key(request_id, self.composite_key)
        )
        post_json = json.dumps(post_data, separators=(",", ":")).encode("utf-8")
        encrypted_body = encrypt_post_data(key, post_json)

        params: Dict[str, str] = {
            "a": api,
            "v": version,
            "clientId": CLIENT_ID,
            "time": str(int(time.time())),
            "requestId": request_id,
            "lang": LANGUAGE,
            "deviceId": self.device_fingerprint,
            "appVersion": APP_VERSION,
            "ttid": TTID,
            "os": "Android",
            "sdkVersion": SDK_VERSION,
            "chKey": CH_KEY,
            "et": "3",
            "postData": encrypted_body,
        }
        if session:
            params["sid"] = session.sid
        if extra_params:
            params.update(extra_params)

        signed_params = dict(params)
        signed_params["postData"] = post_data_sign_field(encrypted_body)
        params["sign"] = sign_params(build_sign_string(signed_params), self.composite_key)

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": f"TY/{APP_VERSION}",
        }

        resp = self.http_session.post(self.base_url, data=params, headers=headers, timeout=timeout)
        resp.raise_for_status()

        try:
            raw = resp.json()
        except Exception as exc:
            raise RuntimeError(f"Tuya gateway returned non-JSON response: {resp.text}") from exc

        result = raw.get("result")
        if isinstance(result, str) and result:
            try:
                decrypted_bytes = decrypt_post_data(key, result)
                inner = json.loads(decrypted_bytes.decode("utf-8"))
                raw.update(inner)
                result = inner.get("result")
            except Exception as exc:
                raise RuntimeError(f"Failed to decrypt Tuya response: {exc}") from exc

        if not raw.get("success", True) or raw.get("errorCode"):
            err_code = raw.get("errorCode", "UNKNOWN")
            err_msg = raw.get("errorMsg", "Request rejected by Tuya Gateway")
            raise RuntimeError(f"Tuya API {api} failed [{err_code}]: {err_msg}")

        return result

    def login_with_uid(self, country_code: str, uid: str, token: str) -> TuyaSession:
        """Authenticate with Tuya Cloud using UID and cloud token (from Momcozy API)."""
        logger.info("Authenticating Tuya UID %s (region=%s)...", uid, country_code)

        # 1. Fetch RSA public key for password encryption
        token_res = self.call_api(
            "smartlife.m.user.username.token.get",
            "2.0",
            {
                "countryCode": str(country_code),
                "username": uid,
                "isUid": True,
            },
        )
        if not isinstance(token_res, dict) or not token_res.get("publicKey") or not token_res.get("token"):
            raise RuntimeError(f"Invalid token response from Tuya username.token.get: {token_res}")

        pub_key = str(token_res["publicKey"])
        exponent = str(token_res.get("exponent", "65537"))
        rsa_token = str(token_res["token"])

        # 2. Encrypt MD5 of token using RSA
        pw_md5 = md5_hex(token)
        encrypted_pw = encrypt_password_rsa(pub_key, exponent, pw_md5)

        # 3. Submit UID password login
        login_post = {
            "countryCode": str(country_code),
            "uid": uid,
            "passwd": encrypted_pw,
            "token": rsa_token,
            "ifencrypt": 1,
            "createGroup": True,
            "options": '{"group": 1}',
        }

        try:
            account = self.call_api("smartlife.m.user.uid.password.login.reg", "1.0", login_post)
        except Exception:
            # Fallback to direct login endpoint
            login_post.pop("createGroup", None)
            account = self.call_api("smartlife.m.user.uid.password.login", "1.0", login_post)

        if not isinstance(account, dict) or not account.get("sid") or not account.get("ecode"):
            raise RuntimeError(f"Tuya UID login did not return session credentials: {account}")

        logger.info("Tuya UID authentication successful (sid=%s)", account["sid"][:8] + "...")
        return TuyaSession(
            sid=account["sid"],
            ecode=account["ecode"],
            uid=account.get("uid", uid),
            device_fingerprint=self.device_fingerprint,
        )

    def get_home_ids(self, session: TuyaSession) -> List[int]:
        """Query all Tuya Home/Family IDs for the user."""
        logger.debug("Querying Tuya home list...")
        homes = self.call_api("tuya.m.location.list", "2.1", {}, session=session)
        home_ids: List[int] = []
        if isinstance(homes, list):
            for h in homes:
                if isinstance(h, dict):
                    hid = h.get("gid") or h.get("homeId") or h.get("id")
                    if hid:
                        home_ids.append(int(hid))
        return home_ids

    def get_home_id(self, session: TuyaSession) -> int:
        """Query user's primary Tuya Home/Family ID."""
        home_ids = self.get_home_ids(session)
        if home_ids:
            logger.debug("Found existing Tuya home ID: %s", home_ids[0])
            return home_ids[0]

        # Create default home if none exists
        logger.info("No Tuya home found; creating default home...")
        new_home = self.call_api("tuya.m.location.new", "2.0", {"name": "Home"}, session=session)
        if isinstance(new_home, dict):
            home_id = new_home.get("gid") or new_home.get("homeId") or new_home.get("id")
            if home_id:
                return int(home_id)

        raise RuntimeError("Unable to determine Tuya home ID")

    def share_device_to_user(
        self,
        session: TuyaSession,
        home_id: int,
        tuya_dev_id: str,
        target_region: str,
        target_member_code: str,
    ) -> Dict[str, Any]:
        """Create device share relationship on Tuya Cloud Home SDK."""
        logger.info(
            "Creating Tuya device share on Tuya Cloud: dev=%s -> member=%s (homeId=%s, region=%s)",
            tuya_dev_id, target_member_code, home_id, target_region
        )
        post = {
            "countryCode": str(target_region),
            "userAccount": target_member_code,
            "groupId": home_id,
            "devIds": [tuya_dev_id],
        }
        res = self.call_api(
            "smartlife.m.sharing.device.account.add",
            "3.0",
            post,
            session=session,
            extra_params={"gid": str(home_id)},
        )
        if not isinstance(res, dict):
            res = {"result": res}
        res["home_id"] = home_id
        return res

    def query_shared_users(self, session: TuyaSession, tuya_dev_id: str) -> List[Dict[str, Any]]:
        """Query list of users who have access to this shared device."""
        res = self.call_api(
            "smartlife.m.sharing.device.member.list",
            "1.0",
            {"devId": tuya_dev_id},
            session=session,
        )
        if isinstance(res, list):
            return res
        return []

    def revoke_device_share(
        self, session: TuyaSession, tuya_dev_id: str, relation_id: int
    ) -> bool:
        """Remove a device share relationship by relationId."""
        logger.info("Revoking Tuya device share for dev=%s, relationId=%s", tuya_dev_id, relation_id)
        res = self.call_api(
            "smartlife.m.sharing.device.remove",
            "3.0",
            {"devId": tuya_dev_id, "relationId": relation_id},
            session=session,
        )
        return bool(res)

    def publish_dps(
        self,
        session: TuyaSession,
        dev_id: str,
        dps: Dict[str, Any],
    ) -> bool:
        """Publish DataPoints (dps) to device via Tuya Cloud Gateway."""
        formatted_dps = {str(k): v for k, v in dps.items()}
        res = self.call_api(
            "smartlife.m.device.dp.publish",
            "1.0",
            {
                "devId": dev_id,
                "gwId": dev_id,
                "dps": json.dumps(formatted_dps, separators=(",", ":")),
            },
            session=session,
        )
        return bool(res)

    @staticmethod
    def _normalize_device_dict(dev: Dict[str, Any], home_id: Optional[int] = None) -> Dict[str, Any]:
        """Ensure top-level 'dps' is populated from 'dataPointInfo.dps' if present."""
        if not dev.get("dps"):
            dp_info = dev.get("dataPointInfo")
            if isinstance(dp_info, dict) and dp_info.get("dps"):
                dev["dps"] = dp_info["dps"]
        if home_id and not dev.get("homeId"):
            dev["homeId"] = home_id
        return dev

    def get_device_info(
        self, session: TuyaSession, dev_id: str, home_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """Query a specific device's full DeviceRespBean (including localKey and dps)."""
        extra = {"gid": str(home_id)} if home_id else None
        errors: List[str] = []
        for api_name, ver, post in (
            ("smartlife.m.device.get", "4.1", {"devId": dev_id}),
            ("smartlife.m.device.get", "2.0", {"devId": dev_id}),
            ("tuya.m.device.get", "1.0", {"devId": dev_id}),
        ):
            try:
                res = self.call_api(api_name, ver, post, session=session, extra_params=extra)
                if isinstance(res, dict) and (
                    res.get("devId") or res.get("localKey") or res.get("dps") or res.get("dataPointInfo")
                ):
                    return self._normalize_device_dict(res, home_id=home_id)
            except Exception as exc:
                errors.append(f"{api_name} (v{ver}): {exc}")
                logger.debug("Tuya device info API %s (v%s) failed: %s", api_name, ver, exc)
        if any("USER_SESSION_INVALID" in err for err in errors):
            logger.debug("Tuya get_device_info session invalid for %s: %s", dev_id, "; ".join(errors))
            raise RuntimeError(f"USER_SESSION_INVALID: {'; '.join(errors)}")
        if errors:
            logger.warning("Tuya get_device_info for %s failed: %s", dev_id, "; ".join(errors))
        return None

    def get_shared_devices(self, session: TuyaSession) -> List[Dict[str, Any]]:
        """Query devices shared with the user from other accounts."""
        logger.debug("Querying Tuya shared devices...")
        res = self.call_api("thing.m.my.shared.device.list", "3.2", {}, session=session)
        devs: List[Dict[str, Any]] = []
        if isinstance(res, list):
            devs = [d for d in res if isinstance(d, dict)]
        elif isinstance(res, dict):
            raw_list = res.get("devices") or res.get("deviceList") or res.get("list")
            if isinstance(raw_list, list):
                devs = [d for d in raw_list if isinstance(d, dict)]
        return [self._normalize_device_dict(d) for d in devs]

    def get_home_devices(self, session: TuyaSession, home_id: int) -> List[Dict[str, Any]]:
        """Query all devices in Tuya Home including their local encryption keys (localKey)."""
        logger.debug("Querying Tuya home %s devices...", home_id)
        errors: List[str] = []
        for api_name, ver, post in (
            ("m.life.my.group.device.list", "2.2", {"gid": home_id}),
            ("tuya.m.my.group.device.list", "1.0", {}),
            ("m.life.app.smart.local.device.list", "1.1", {"homeId": home_id, "groupType": "homeGroup"}),
            ("smartlife.m.my.group.device.list", "2.2", {"gid": home_id}),
        ):
            try:
                res = self.call_api(
                    api_name,
                    ver,
                    post,
                    session=session,
                    extra_params={"gid": str(home_id)},
                )
                if isinstance(res, list):
                    return [self._normalize_device_dict(d, home_id=home_id) for d in res if isinstance(d, dict)]
                if isinstance(res, dict):
                    devs = res.get("devices") or res.get("deviceList")
                    if isinstance(devs, list):
                        return [self._normalize_device_dict(d, home_id=home_id) for d in devs if isinstance(d, dict)]
            except Exception as exc:
                errors.append(f"{api_name} (v{ver}): {exc}")
                logger.debug("Tuya home devices API %s failed: %s", api_name, exc)
        if errors:
            raise RuntimeError(
                f"Tuya home devices query for home {home_id} failed across endpoints: {'; '.join(errors)}"
            )
        return []

    def get_all_devices(
        self,
        session: TuyaSession,
        home_id: Optional[int] = None,
        known_device_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Query all accessible devices (owned home devices, shared devices, and known device IDs)."""
        all_devs: List[Dict[str, Any]] = []
        seen_ids = set()
        errors: List[str] = []

        # 1. Discover all home IDs
        home_ids: List[int] = []
        try:
            home_ids = self.get_home_ids(session)
        except Exception as exc:
            logger.debug("Could not resolve all Tuya home IDs via get_home_ids: %s", exc)

        if not home_ids:
            try:
                discovered_id = self.get_home_id(session)
                if discovered_id:
                    home_ids.append(discovered_id)
            except Exception as exc:
                logger.warning("Could not resolve Tuya home ID: %s", exc)
                errors.append(f"get_home_id: {exc}")

        if home_id and home_id not in home_ids:
            home_ids.insert(0, home_id)

        for hid in home_ids:
            try:
                for dev in self.get_home_devices(session, hid):
                    if not isinstance(dev, dict):
                        continue
                    dev_id = str(dev.get("devId") or dev.get("id") or dev.get("gwId") or "")
                    if dev_id and dev_id not in seen_ids:
                        seen_ids.add(dev_id)
                        all_devs.append(dev)
                    elif not dev_id:
                        all_devs.append(dev)
            except Exception as exc:
                logger.warning("Error fetching home %s devices: %s", hid, exc)
                errors.append(f"get_home_devices({hid}): {exc}")

        # 2. Fetch shared devices
        try:
            for dev in self.get_shared_devices(session):
                if not isinstance(dev, dict):
                    continue
                dev_id = str(dev.get("devId") or dev.get("id") or dev.get("gwId") or "")
                if dev_id and dev_id not in seen_ids:
                    seen_ids.add(dev_id)
                    all_devs.append(dev)
                elif not dev_id:
                    all_devs.append(dev)
        except Exception as exc:
            logger.warning("Error fetching shared devices: %s", exc)
            errors.append(f"get_shared_devices: {exc}")

        # 3. Direct lookup for known device IDs if not yet found or missing localKey
        if known_device_ids:
            primary_hid = home_id or (home_ids[0] if home_ids else None)
            for k_id in known_device_ids:
                existing = next(
                    (
                        d
                        for d in all_devs
                        if str(d.get("devId") or d.get("id") or d.get("gwId") or "") == k_id
                    ),
                    None,
                )
                if not existing or not (existing.get("localKey") or existing.get("devKey") or existing.get("key")):
                    info = self.get_device_info(session, k_id, home_id=primary_hid)
                    if info:
                        if existing:
                            existing.update(info)
                        else:
                            seen_ids.add(k_id)
                            all_devs.append(info)

        if not all_devs and errors:
            raise RuntimeError(f"Failed to fetch Tuya devices from cloud: {'; '.join(errors)}")

        return all_devs

