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
                    logger.info("Successfully logged in to Momcozy Cloud as %s", self.email)
                    # Automatically fetch linked Tuya user
                    try:
                        self.get_tuya_credentials()
                    except Exception as exc:
                        logger.warning("Could not auto-fetch Tuya credentials: %s", exc)
                    return res
                last_error = data.get("message", "Authentication failed")
            except Exception as e:
                last_error = str(e)

        raise RuntimeError(f"Momcozy login failed: {last_error}")

    def query_devices(self) -> Any:
        """List devices bound to the account (/api/app/device/queryDevices).

        NOTE: response shape not yet verified against a live account.
        """
        if not self.token:
            raise RuntimeError("Must be logged in")
        resp = requests.get(f"{self.base_url}/api/app/device/queryDevices",
                            headers=self._headers(need_auth=True), timeout=15)
        data = resp.json()
        if data.get("code") != "200":
            raise RuntimeError(f"queryDevices failed: {data.get('message')}")
        return data.get("result")

    def get_tuya_credentials(self) -> TuyaCredentials:
        """Fetch Tuya cloud credentials linked to this Momcozy account."""
        if not self.token:
            raise RuntimeError("Must be logged in to fetch Tuya credentials")
        url = f"{self.base_url}/api/app/third/tuya/user"
        resp = requests.get(url, headers=self._headers(need_auth=True), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "200":
            raise RuntimeError(f"Failed to get Tuya user credentials: {data.get('message')}")
        result = data.get("result", {})
        self.tuya_creds = TuyaCredentials(
            region=result.get("region", "NA"),
            uid=result.get("uid", ""),
            token=result.get("token", ""),
        )
        return self.tuya_creds

    def save_session(self, path: Path | str = DEFAULT_CONFIG_PATH) -> None:
        """Save credentials to disk."""
        path = Path(path)
        payload = {
            "email": self.email,
            "token": self.token,
            "refresh_token": self.refresh_token,
            "user_uid": self.user_uid,
            "device_id": self.device_id,
            "country_code": self.country_code,
            "country_name": self.country_name,
            "region": self.region,
            "tuya": {
                "region": self.tuya_creds.region if self.tuya_creds else "NA",
                "uid": self.tuya_creds.uid if self.tuya_creds else "",
                "token": self.tuya_creds.token if self.tuya_creds else "",
            } if self.tuya_creds else None,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))
        os.chmod(path, 0o600)
        logger.info("Saved Momcozy session to %s", path)

    @classmethod
    def load_session(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> Optional[MomcozyCloudApi]:
        """Load session from disk if present."""
        path = Path(path)
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
            tuya_data = data.get("tuya")
            if tuya_data:
                client.tuya_creds = TuyaCredentials(
                    region=tuya_data.get("region", "NA"),
                    uid=tuya_data.get("uid", ""),
                    token=tuya_data.get("token", ""),
                )
            return client
        except Exception as e:
            logger.warning("Failed to load session from %s: %s", path, e)
            return None
