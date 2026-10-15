"""Momcozy Cloud API Client.

Handles authentication with Momcozy Cloud API using Argon2id password hashing,
retrieving account information, and querying linked Tuya devices & encryption keys.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from argon2.low_level import Type, hash_secret_raw

from .constants import (
    API_BASE_URL,
    APP_ID,
    APP_VERSION,
    ARGON2_FIXED_SALT_B64,
    ARGON2_HASH_LEN_BYTES,
    ARGON2_MEMORY_COST_KB,
    ARGON2_PARALLELISM,
    ARGON2_TIME_COST,
    CLIENT_TYPE,
    REGION_API_URLS,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".momcozy.json"


def hash_password(plain_password: str) -> str:
    """Hash password with Argon2id matching the official Momcozy Android app."""
    salt = base64.b64decode(ARGON2_FIXED_SALT_B64)
    raw_hash = hash_secret_raw(
        secret=plain_password.encode("utf-8"),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST_KB,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN_BYTES,
        type=Type.ID,
    )
    return base64.b64encode(raw_hash).decode("utf-8")


@dataclass
class TuyaCredentials:
    """Tuya cloud authentication credentials returned by Momcozy."""

    region: str
    uid: str
    token: str


@dataclass
class MomcozyDeviceRecord:
    """Device discovered from Momcozy / Tuya account."""

    device_id: str
    name: str
    product_id: str
    local_key: str = ""
    ip: str = ""
    category: str = "sp"  # smart camera
    is_online: bool = True
    dps: Dict[str, Any] = field(default_factory=dict)


class MomcozyCloudApi:
    """Client for Momcozy cloud service."""

    def __init__(
        self,
        base_url: str = API_BASE_URL,
        device_id: Optional[str] = None,
        country_code: str = "US",
        country_name: str = "United States",
        region: Optional[str] = None,
    ) -> None:
        if region:
            base_url = REGION_API_URLS[region.upper()]
        self.region = region.upper() if region else None
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id or str(uuid.uuid4())
        self.country_code = country_code
        self.country_name = country_name
        self.token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.user_uid: Optional[str] = None
        self.email: Optional[str] = None
        self.tuya_creds: Optional[TuyaCredentials] = None
        self.tuya_home_id: Optional[int] = None
        self._tuya_client: Optional[Any] = None

    def _headers(self, need_auth: bool = True) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Client": CLIENT_TYPE,
            "X-COZY-APPID": APP_ID,
            "Version": APP_VERSION,
            "Language": "en",
            "CountryCode": self.country_code,
            "DeviceId": self.device_id,
            "DeviceType": "1",
            "TimeZone": "UTC",
            "ZoneId": "UTC",
        }
        if need_auth and self.token:
            headers["authorization"] = f"Bearer {self.token}"
        return headers

    def init_auth(self) -> str:
        """Call login init endpoint to receive session ID."""
        url = f"{self.base_url}/api/app/user/login/init"
        payload = {
            "platForm": "BREAST_PUMP",
            "countryCode": self.country_code,
            "countryName": self.country_name,
        }
        resp = requests.post(url, json=payload, headers=self._headers(need_auth=False), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "200":
            raise RuntimeError(f"Init auth failed: {data.get('message', 'Unknown error')}")
        result = data.get("result", {})
        return result.get("sid", "")

    def login(self, email: str, password: str) -> Dict[str, Any]:
        """Authenticate with email and password.

        If no region was given, try every regional gateway (EU, NA, AP) until one
        accepts the account; the account's home region is then remembered.
        """
        if self.region:
            return self._login_once(email, password)
        errors = []
        for region in ("EU", "NA", "AP"):
            self.region = region
            self.base_url = REGION_API_URLS[region]
            try:
                return self._login_once(email, password)
            except RuntimeError as exc:
                errors.append(f"{region}: {exc}")
        self.region = None
        raise RuntimeError("Login failed in all regions - " + "; ".join(errors))

    def _login_once(self, email: str, password: str) -> Dict[str, Any]:
        hashed_pw = hash_password(password)
        sid = self.init_auth()

        # Try v3 first, fallback to v1
        endpoints = ["/api/app/user/login/v3", "/api/app/user/login"]
        last_error = None

        for endpoint in endpoints:
            url = f"{self.base_url}{endpoint}"
            payload = {
                "type": 1,  # Email login
                "email": email,
                "password": hashed_pw,
                "platForm": "BREAST_PUMP",
                "remember": 1,
            }
            try:
                headers = self._headers(need_auth=False)
                if sid:
                    headers["sid"] = sid
                resp = requests.post(url, json=payload, headers=headers, timeout=15)
                data = resp.json()
                if data.get("code") == "200":
                    res = data.get("result", {})
                    self.token = res.get("token")
                    self.refresh_token = res.get("refreshToken")
                    self.user_uid = res.get("uid")
                    self.email = res.get("email", email)
                    # Automatically fetch linked Tuya user and user profile info
                    try:
                        self.get_tuya_credentials()
                    except Exception as exc:
                        logger.warning("Could not auto-fetch Tuya credentials: %s", exc)
                    try:
                        info = self.get_user_info()
                        if info.get("countryCode"):
                            self.country_code = info["countryCode"]
                        if info.get("countryName"):
                            self.country_name = info["countryName"]
                        reg = (info.get("userMemberInfo") or {}).get("regionCode")
                        if reg and not self.region:
                            self.region = reg
                    except Exception as exc:
                        logger.warning("Could not auto-fetch user profile info: %s", exc)
                    return res
                last_error = data.get("message", "Authentication failed")
            except Exception as e:
                last_error = str(e)

        raise RuntimeError(f"Momcozy login failed: {last_error}")

    def _api_request(self, method: str, url: str, need_auth: bool = True, **kwargs) -> Dict[str, Any]:
        """Perform HTTP request to Momcozy Cloud."""
        headers = self._headers(need_auth=need_auth)
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))
        resp = requests.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        data = resp.json()
        code = str(data.get("code", ""))

        if code in ("10004030", "10001002", "10001003"):
            raise RuntimeError("Login session has expired or was signed out. Please run './momcozy.py login' to re-authenticate.")

        return data

    def query_devices(self) -> Any:
        """List devices bound to the account (/api/app/device/queryDevices)."""
        if not self.token:
            raise RuntimeError("Must be logged in")
        data = self._api_request("GET", f"{self.base_url}/api/app/device/queryDevices", need_auth=True, timeout=15)
        if data.get("code") != "200":
            raise RuntimeError(f"queryDevices failed: {data.get('message')}")
        return data.get("result")

    def get_tuya_credentials(self) -> TuyaCredentials:
        """Fetch Tuya cloud credentials linked to this Momcozy account."""
        if not self.token:
            raise RuntimeError("Must be logged in to fetch Tuya credentials")
        url = f"{self.base_url}/api/app/third/tuya/user"
        data = self._api_request("GET", url, need_auth=True, timeout=15)
        if data.get("code") != "200":
            raise RuntimeError(f"Failed to get Tuya user credentials: {data.get('message')}")
        result = data.get("result", {})
        self.tuya_creds = TuyaCredentials(
            region=result.get("region", "NA"),
            uid=result.get("uid", ""),
            token=result.get("token", ""),
        )
        return self.tuya_creds

    def get_user_info(self) -> Dict[str, Any]:
        """Fetch user profile information (/api/app/user/getUserInfo/v2)."""
        if not self.token:
            raise RuntimeError("Must be logged in to fetch user info")
        url = f"{self.base_url}/api/app/user/getUserInfo/v2"
        data = self._api_request("GET", url, need_auth=True, timeout=15)
        if data.get("code") != "200":
            raise RuntimeError(f"Failed to get user info: {data.get('message')}")
        return data.get("result", {})

    def search_tuya_user(self, account: str, country_code: Optional[str] = None, account_type: Optional[int] = None) -> Dict[str, Any]:
        """Search for a target Momcozy/Tuya user by email or phone to prepare for device sharing.

        Calls /api/app/third/tuya/user/search.
        Returns dict containing 'memberCode', 'uid', and 'region'.
        """
        if not self.token:
            raise RuntimeError("Must be logged in to search users")
        url = f"{self.base_url}/api/app/third/tuya/user/search"
        if account_type is None:
            account_type = 2 if account.replace("+", "").isdigit() else 1
        params: Dict[str, Any] = {"type": account_type}
        if account_type == 1:
            params["email"] = account
        else:
            params["phone"] = account
        data = self._api_request("GET", url, need_auth=True, params=params, timeout=15)
        if data.get("code") != "200":
            raise RuntimeError(f"User search failed: {data.get('message', 'Unknown error')}")
        return data.get("result", {})

    def share_device(self, tuya_dev_id: str, member_code: str) -> Dict[str, Any]:
        """Grant device access to a target memberCode on Tuya/Momcozy cloud.

        Calls POST /api/app/third/tuya/device/{tuyaDevId}/share/{memberCode}.
        """
        if not self.token:
            raise RuntimeError("Must be logged in to share device")
        url = f"{self.base_url}/api/app/third/tuya/device/{tuya_dev_id}/share/{member_code}"
        data = self._api_request("POST", url, need_auth=True, timeout=15)
        if data.get("code") != "200":
            raise RuntimeError(f"Device share failed: {data.get('message', 'Unknown error')}")
        return data.get("result", {})

    def share_device_notify(self, tuya_dev_id: str, member_code: str) -> Dict[str, Any]:
        """Send a notification to the target user about the shared device.

        Calls POST /api/app/third/tuya/device/share/notice.
        """
        if not self.token:
            raise RuntimeError("Must be logged in to send share notification")
        url = f"{self.base_url}/api/app/third/tuya/device/share/notice"
        payload = {
            "tuyaDevId": tuya_dev_id,
            "memberCode": member_code,
        }
        data = self._api_request("POST", url, need_auth=True, json=payload, timeout=15)
        if data.get("code") != "200":
            raise RuntimeError(f"Share notification failed: {data.get('message', 'Unknown error')}")
        return data.get("result", {})

    def query_shared_emails(self, member_codes: list[str]) -> list[Dict[str, Any]]:
        """Query email / account details for a list of shared member codes.

        Calls POST /api/app/third/tuya/user/queryEmail.
        """
        if not self.token:
            raise RuntimeError("Must be logged in to query shared user info")
        url = f"{self.base_url}/api/app/third/tuya/user/queryEmail"
        payload = {"memberCodeList": member_codes}
        data = self._api_request("POST", url, need_auth=True, json=payload, timeout=15)
        if data.get("code") != "200":
            raise RuntimeError(f"Query shared emails failed: {data.get('message', 'Unknown error')}")
        res = data.get("result", {})
        return res.get("tuyaUserInfoBeanList", [])

    def delete_device_share(self, tuya_dev_id: str, member_code: str) -> Dict[str, Any]:
        """Revoke a user's shared access to the device.

        Calls DELETE /api/app/third/tuya/device/{tuyaDevId}/share/{memberCode}.
        """
        if not self.token:
            raise RuntimeError("Must be logged in to revoke device share")
        url = f"{self.base_url}/api/app/third/tuya/device/{tuya_dev_id}/share/{member_code}"
        data = self._api_request("DELETE", url, need_auth=True, timeout=15)
        if data.get("code") != "200":
            raise RuntimeError(f"Revoke device share failed: {data.get('message', 'Unknown error')}")
        return data.get("result", {})

    def get_tuya_client(self) -> Any:
        """Get or initialize a TuyaGatewayClient configured for this account's region."""
        if self._tuya_client is not None:
            return self._tuya_client
        from .tuya_cloud import TuyaGatewayClient

        # Map Tuya numeric region code ("33" -> "eu", "1" -> "us", "86" -> "cn") or account region
        reg = "eu"
        if self.tuya_creds and self.tuya_creds.region:
            r = str(self.tuya_creds.region).lower()
            if r in ("33", "eu"):
                reg = "eu"
            elif r in ("86", "cn"):
                reg = "cn"
            elif r in ("1", "us", "na"):
                reg = "us"
        elif self.region == "EU":
            reg = "eu"
        elif self.region in ("US", "NA"):
            reg = "us"

        self._tuya_client = TuyaGatewayClient(region=reg)
        return self._tuya_client

    def add_tuya_device_share(
        self,
        tuya_dev_id: str,
        target_member_code: str,
        target_region: str,
        home_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create device share invite on Tuya Cloud Home SDK gateway.

        This calls Tuya's thing.m.sharing.device.account.add (v 3.0) API.
        Without this step, the destination user cannot accept the device share
        in Tuya / Momcozy apps because no Tuya share relationship exists.
        """
        if not self.tuya_creds or not self.tuya_creds.token:
            self.get_tuya_credentials()

        if not self.tuya_creds or not self.tuya_creds.token:
            raise RuntimeError("No Tuya credentials available for Tuya Home sharing")

        tuya_client = self.get_tuya_client()
        session = tuya_client.login_with_uid(
            country_code=self.tuya_creds.region,
            uid=self.tuya_creds.uid,
            token=self.tuya_creds.token,
        )

        active_home_id = home_id or self.tuya_home_id
        if not active_home_id:
            active_home_id = tuya_client.get_home_id(session)
            self.tuya_home_id = active_home_id
            # Cache the discovered home ID in session if path known
            if hasattr(self, "_config_path") and self._config_path:
                try:
                    self.save_session(self._config_path)
                except Exception:
                    pass

        return tuya_client.share_device_to_user(
            session=session,
            home_id=active_home_id,
            tuya_dev_id=tuya_dev_id,
            target_region=target_region,
            target_member_code=target_member_code,
        )

    def add_device_share(
        self,
        account: str,
        tuya_dev_id: Optional[str] = None,
        country_code: Optional[str] = None,
        home_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """High-level helper to share a device with a user by email/account.

        Executes the full sharing flow matching the official Momcozy Android app:
          1. Searches user by account on Momcozy Cloud -> gets memberCode, region, uid.
          2. Creates Tuya Home device share invite via Tuya Gateway -> binds device permissions.
          3. Grants device share on Momcozy Cloud -> registers Momcozy metadata.
          4. Sends push notification to recipient -> confirms share.
        """
        dev_id = tuya_dev_id or self.device_id
        if not dev_id:
            raise ValueError("tuya_dev_id or configured device_id required")

        user_info = self.search_tuya_user(account, country_code=country_code)
        member_code = user_info.get("memberCode")
        if not member_code:
            raise RuntimeError(f"No memberCode returned for user {account}: {user_info}")

        target_region = user_info.get("region") or "33"
        logger.info("Found target user %s (memberCode=%s, region=%s)", account, member_code, target_region)

        # Step 2: Create Tuya Home device share invite in Tuya Cloud
        tuya_share_res: Dict[str, Any] = {}
        try:
            tuya_share_res = self.add_tuya_device_share(
                tuya_dev_id=dev_id,
                target_member_code=member_code,
                target_region=target_region,
                home_id=home_id,
            )
            logger.info("Tuya Home Cloud device share registered successfully: %s", tuya_share_res)
        except Exception as exc:
            logger.warning("Tuya Home Cloud device share warning: %s", exc)

        # Step 3: Register device share metadata on Momcozy Cloud
        self.share_device(dev_id, member_code)

        # Step 4: Notify recipient
        try:
            self.share_device_notify(dev_id, member_code)
        except Exception as exc:
            logger.warning("Share notice notification warning: %s", exc)

        return {
            "account": account,
            "member_code": member_code,
            "device_id": dev_id,
            "region": target_region,
            "uid": user_info.get("uid"),
            "tuya_share": tuya_share_res,
        }

    def save_session(self, path: Path | str = DEFAULT_CONFIG_PATH) -> None:
        """Save credentials to disk."""
        path = Path(path).expanduser()
        tuya_payload = None
        if self.tuya_creds:
            tuya_payload = {
                "region": self.tuya_creds.region,
                "uid": self.tuya_creds.uid,
                "token": self.tuya_creds.token,
            }
            if self.tuya_home_id:
                tuya_payload["home_id"] = self.tuya_home_id

        payload = {
            "email": self.email,
            "token": self.token,
            "refresh_token": self.refresh_token,
            "user_uid": self.user_uid,
            "device_id": self.device_id,
            "country_code": self.country_code,
            "country_name": self.country_name,
            "region": self.region,
            "tuya": tuya_payload,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))
        os.chmod(path, 0o600)
        logger.info("Saved Momcozy session to %s", path)

    @classmethod
    def load_session(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> Optional[MomcozyCloudApi]:
        """Load session from disk if present."""
        path = Path(path).expanduser()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            client = cls(
                device_id=data.get("device_id"),
                country_code=data.get("country_code", "US"),
                country_name=data.get("country_name", "United States"),
                region=data.get("region"),
            )
            client.email = data.get("email")
            client.token = data.get("token")
            client.refresh_token = data.get("refresh_token")
            client.user_uid = data.get("user_uid")
            client._config_path = str(path)
            tuya_data = data.get("tuya")
            if tuya_data:
                client.tuya_creds = TuyaCredentials(
                    region=tuya_data.get("region", "NA"),
                    uid=tuya_data.get("uid", ""),
                    token=tuya_data.get("token", ""),
                )
                client.tuya_home_id = tuya_data.get("home_id")
            return client
        except Exception as e:
            logger.warning("Failed to load session from %s: %s", path, e)
            return None
