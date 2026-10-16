"""Video streaming and recording utilities for Momcozy BM04.

Provides MJPEG live streaming, snapshot taking, and video recording to MP4.
"""

from __future__ import annotations

import asyncio
import io
import logging
import struct
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Set

import cv2
import numpy as np

from .tuya_cloud import (
    APP_SECRET,
    APP_VERSION,
    CERT_SHA256,
    CH_KEY,
    CLIENT_ID,
    COMPOSITE_KEY,
    DERIVED_KEY,
    PACKAGE_NAME,
    SDK_VERSION,
    TTID,
)

logger = logging.getLogger(__name__)

_SYNC_MARKER = b"\x03\x00\x01\x00\x00\x00\x00\x00"


class MomcozyHevcHandler:
    """Reassembles and decodes BM04 1080p HEVC video streams into JPEG frames."""

    def __init__(self, on_frame: Any) -> None:
        try:
            import av
        except ImportError as e:
            raise ImportError(
                "Live HEVC video streaming requires the 'av' package. "
                "Install it with: pip install av (or pip install -r requirements.txt)"
            ) from e

        self._on_frame = on_frame
        self._buffer = bytearray()
        self._fu_buffer = bytearray()
        self._codec = av.CodecContext.create("hevc", "r")
        self.frame_count = 0

    def feed_record(self, plaintext: bytes) -> None:
        self._buffer.extend(plaintext)
        annexb_chunk = bytearray()

        while True:
            sync_idx = self._buffer.find(_SYNC_MARKER)
            if sync_idx < 0:
                if len(self._buffer) > 7:
                    del self._buffer[:-7]
                break
            if sync_idx > 0:
                del self._buffer[:sync_idx]

            if len(self._buffer) < 28:
                break

            ext = struct.unpack("<I", self._buffer[16:20])[0]
            hdr_len = 36 if ext == 8 else 28
            if len(self._buffer) < hdr_len:
                break

            plen = struct.unpack("<I", self._buffer[hdr_len - 4 : hdr_len])[0]
            total_len = hdr_len + plen
            if len(self._buffer) < total_len:
                break

            pkt = bytes(self._buffer[:total_len])
            del self._buffer[:total_len]

            rtp_pkt = pkt[hdr_len:]
            if len(rtp_pkt) < 12:
                continue

            payload = rtp_pkt[12:]
            if not payload:
                continue

            nal_hdr = payload[0]
            nal_type = (nal_hdr >> 1) & 0x3F

            if nal_type == 49:  # FU (Fragmentation Unit)
                if len(payload) < 3:
                    continue
                fu_hdr = payload[2]
                s = bool(fu_hdr & 0x80)
                e = bool(fu_hdr & 0x40)
                ftype = fu_hdr & 0x3F
                if s:
                    self._fu_buffer = bytearray(b"\x00\x00\x00\x01")
                    b0 = (payload[0] & 0x81) | (ftype << 1)
                    b1 = payload[1]
                    self._fu_buffer.extend([b0, b1])
                    self._fu_buffer.extend(payload[3:])
                else:
                    self._fu_buffer.extend(payload[3:])
                if e:
                    annexb_chunk.extend(self._fu_buffer)
                    self._fu_buffer = bytearray()
            elif nal_type == 48:  # AP (Aggregation Packet)
                ap_off = 2
                while ap_off + 2 <= len(payload):
                    nlen = struct.unpack(">H", payload[ap_off : ap_off + 2])[0]
                    ap_off += 2
                    annexb_chunk.extend(b"\x00\x00\x00\x01")
                    annexb_chunk.extend(payload[ap_off : ap_off + nlen])
                    ap_off += nlen
            else:  # Single NALU (VPS, SPS, PPS, IDR, etc.)
                annexb_chunk.extend(b"\x00\x00\x00\x01")
                annexb_chunk.extend(payload)

        if annexb_chunk:
            for packet in self._codec.parse(bytes(annexb_chunk)):
                for frame in self._codec.decode(packet):
                    arr = frame.to_ndarray(format="bgr24")
                    _, enc = cv2.imencode(".jpg", arr, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    jpg_bytes = enc.tobytes()
                    self.frame_count += 1
                    self._on_frame(jpg_bytes)


def _patch_tuya_stream_session() -> None:
    """Monkey-patch tuya_ipc_p2p_sdk StreamSession to handle BM04 HEVC stream format."""
    from tuya_ipc_p2p_sdk.crypto import decrypt_record
    from tuya_ipc_p2p_sdk.stream_session import StreamSession

    if getattr(StreamSession, "_momcozy_patched", False):
        return

    orig_init = StreamSession.__init__

    def momcozy_session_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        self._momcozy_hevc = MomcozyHevcHandler(self._on_frame)

    def momcozy_on_media_record(self: Any, record: bytes) -> None:
        try:
            pt = decrypt_record(self._answer_key, record)
        except Exception:
            return
        if hasattr(self, "_momcozy_hevc"):
            self._momcozy_hevc.feed_record(pt)
            self._frames = self._momcozy_hevc.frame_count

    StreamSession.__init__ = momcozy_session_init
    StreamSession._on_media_record = momcozy_on_media_record
    StreamSession._momcozy_patched = True


def _configure_tuya_p2p_sdk() -> None:
    """Configure tuya_ipc_p2p_sdk module constants with Momcozy OEM credentials."""
    import tuya_ipc_p2p_sdk.const as sdk_const
    import tuya_ipc_p2p_sdk.gateway.client as sdk_gw_client
    import tuya_ipc_p2p_sdk.gateway.identity as sdk_gw_identity

    for mod in (sdk_const, sdk_gw_client, sdk_gw_identity):
        for attr, val in [
            ("PACKAGE_NAME", PACKAGE_NAME),
            ("CERT_SHA256", CERT_SHA256),
            ("DERIVED_KEY", DERIVED_KEY),
            ("APP_SECRET", APP_SECRET),
            ("COMPOSITE_KEY", COMPOSITE_KEY),
            ("CLIENT_ID", CLIENT_ID),
            ("CH_KEY", CH_KEY),
            ("APP_VERSION", APP_VERSION),
            ("SDK_VERSION", SDK_VERSION),
            ("TTID", TTID),
        ]:
            if hasattr(mod, attr):
                setattr(mod, attr, val)


def create_p2p_client(
    email: str = "",
    password: str = "",
    country_code: str = "1",
    region: str = "us",
    tuya_uid: Optional[str] = None,
    tuya_token: Optional[str] = None,
    tuya_region: Optional[str] = None,
) -> Any:
    """Create a TuyaIpcP2pClient configured with Momcozy OEM gateway credentials."""
    _configure_tuya_p2p_sdk()
    _patch_tuya_stream_session()
    from tuya_ipc_p2p_sdk import TuyaIpcP2pClient
    from tuya_ipc_p2p_sdk.const import BROKER_PORT, broker_host
    from tuya_ipc_p2p_sdk.crypto import md5_hex
    from tuya_ipc_p2p_sdk.exceptions import (
        TuyaIpcP2pAuthenticationError,
        TuyaIpcP2pGatewayError,
    )
    from tuya_ipc_p2p_sdk.gateway.client import GatewayClient, _encrypt_password
    from tuya_ipc_p2p_sdk.json_types import require_str
    from tuya_ipc_p2p_sdk.models import AccountSession, MqttIdentity

    class MomcozyGatewayClient(GatewayClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("composite_key", COMPOSITE_KEY)
            super().__init__(*args, **kwargs)

        def mqtt_identity(self, session: AccountSession) -> MqttIdentity:
            app_key = CLIENT_ID
            tail = md5_hex(md5_hex(app_key) + session.ecode)[16:32]
            username = f"p1000018_v1_{app_key}_{CH_KEY}_mb_{session.sid}{tail}"
            password = md5_hex(md5_hex(COMPOSITE_KEY) + session.ecode)[8:24]
            salt = "sdkfasodifca"
            h = md5_hex(session.uid + salt)
            client_id = f"{PACKAGE_NAME}_mb_{session.device_fingerprint}_{h}_DEFAULT"
            return MqttIdentity(
                host=broker_host(self._region),
                port=BROKER_PORT,
                client_id=client_id,
                username=username,
                password=password,
            )

        async def async_login(self, email_arg: str, password_arg: str, cc_arg: str) -> AccountSession:
            if tuya_uid and tuya_token:
                uid_cc = str(tuya_region or cc_arg)
                try:
                    token_obj = await self._async_call_object(
                        "smartlife.m.user.username.token.get",
                        "2.0",
                        {"countryCode": uid_cc, "username": tuya_uid, "isUid": True},
                    )
                    encrypted_pw = _encrypt_password(
                        require_str(token_obj, "publicKey"),
                        require_str(token_obj, "exponent"),
                        md5_hex(tuya_token),
                    )
                    login_post = {
                        "countryCode": uid_cc,
                        "uid": tuya_uid,
                        "passwd": encrypted_pw,
                        "token": require_str(token_obj, "token"),
                        "ifencrypt": 1,
                        "createGroup": True,
                        "options": '{"group": 1}',
                    }
                    try:
                        account = await self._async_call_object(
                            "smartlife.m.user.uid.password.login.reg",
                            "1.0",
                            login_post,
                        )
                    except TuyaIpcP2pGatewayError:
                        login_post_fallback = dict(login_post)
                        login_post_fallback.pop("createGroup", None)
                        account = await self._async_call_object(
                            "smartlife.m.user.uid.password.login",
                            "1.0",
                            login_post_fallback,
                        )
                except TuyaIpcP2pGatewayError as exc:
                    raise TuyaIpcP2pAuthenticationError(f"Failed to log in with Tuya UID: {exc}") from exc

                return AccountSession(
                    sid=require_str(account, "sid"),
                    ecode=require_str(account, "ecode"),
                    uid=require_str(account, "uid"),
                    device_fingerprint=self._device_fingerprint,
                )

            return await super().async_login(email_arg, password_arg, cc_arg)

    client = TuyaIpcP2pClient(
        email=email or (tuya_uid or ""),
        password=password or (tuya_token or ""),
        country_code=country_code,
        region=region,
    )
    client._gateway = MomcozyGatewayClient(region=region)
    return client


class VideoStreamer:
    """Manages the video stream from Momcozy BM04."""

    def __init__(
        self,
        device_id: str,
        local_key: str,
        email: str = "",
        password: str = "",
        country_code: str = "1",
        region: str = "us",
        tuya_uid: Optional[str] = None,
        tuya_token: Optional[str] = None,
        tuya_region: Optional[str] = None,
    ) -> None:
        self.device_id = device_id
        self.local_key = local_key
        self.email = email
        self.password = password
        self.country_code = country_code
        self.region = region
        self.tuya_uid = tuya_uid
        self.tuya_token = tuya_token
        self.tuya_region = tuya_region

        self._running = False
        self._connected = False
        self._last_error: Optional[str] = None
        self._latest_frame: Optional[bytes] = None
        self._listeners: Set[asyncio.Queue[bytes]] = set()
        self._stream_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def latest_frame(self) -> Optional[bytes]:
        return self._latest_frame

    async def start(self) -> None:
        """Start background streaming task."""
        async with self._lock:
            if self._running:
                return
            self._running = True
            self._stream_task = asyncio.create_task(self._stream_loop())
            logger.info("Video streamer started for device %s", self.device_id)

    async def stop(self) -> None:
        """Stop background streaming task."""
        async with self._lock:
            self._running = False
            self._connected = False
            if self._stream_task:
                self._stream_task.cancel()
                try:
                    await self._stream_task
                except asyncio.CancelledError:
                    pass
                self._stream_task = None
            logger.info("Video streamer stopped")

    async def _stream_loop(self) -> None:
        """Internal loop fetching frames from the camera P2P stream."""
        backoff = 2
        while self._running:
            try:
                logger.info("Connecting to P2P video stream for %s...", self.device_id)
                async with create_p2p_client(
                    email=self.email,
                    password=self.password,
                    country_code=self.country_code,
                    region=self.region,
                    tuya_uid=self.tuya_uid,
                    tuya_token=self.tuya_token,
                    tuya_region=self.tuya_region,
                ) as client:
                    stream = client.create_camera_stream(self.device_id, self.local_key)
                    await stream.async_start()
                    backoff = 2
                    try:
                        async for jpeg_frame in stream.async_frames():
                            if not self._running:
                                break
                            self._connected = True
                            self._last_error = None
                            self._latest_frame = jpeg_frame
                            for q in list(self._listeners):
                                if q.full():
                                    try:
                                        q.get_nowait()
                                    except asyncio.QueueEmpty:
                                        pass
                                try:
                                    q.put_nowait(jpeg_frame)
                                except asyncio.QueueFull:
                                    pass
                    finally:
                        self._connected = False
                        await stream.async_stop()
            except asyncio.CancelledError:
                self._connected = False
                break
            except Exception as e:
                self._connected = False
                self._last_error = str(e)
                logger.error("P2P stream error: %s. Reconnecting in %ss...", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 1.5)

    async def subscribe(self) -> AsyncIterator[bytes]:
        """Subscribe to live frames."""
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=10)
        if self._latest_frame:
            queue.put_nowait(self._latest_frame)
        self._listeners.add(queue)
        try:
            while self._running:
                frame = await queue.get()
                yield frame
        finally:
            self._listeners.discard(queue)

    async def get_snapshot(self, timeout_seconds: float = 10.0) -> bytes:
        """Capture a single frame as JPEG."""
        if not self._running:
            await self.start()

        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            if self._latest_frame:
                return self._latest_frame
            if self._last_error and time.time() - start_time > 3.0:
                raise RuntimeError(f"Camera stream error: {self._last_error}")
            await asyncio.sleep(0.1)

        err_suffix = f": {self._last_error}" if self._last_error else ""
        raise TimeoutError(f"Timed out waiting for video frame from camera{err_suffix}")

    async def record_clip(
        self,
        output_file: str | Path,
        duration_seconds: int = 10,
        fps: int = 15,
    ) -> Path:
        """Record video clip from live stream and save as MP4."""
        out_path = Path(output_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        first_frame = await self.get_snapshot()
        img = cv2.imdecode(np.frombuffer(first_frame, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("Failed to decode video frame")

        height, width, _ = img.shape
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(out_path), fourcc, fps, (width, height))

        start_time = time.time()
        frames_recorded = 0

        try:
            async for frame_bytes in self.subscribe():
                frame_img = cv2.imdecode(
                    np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
                )
                if frame_img is not None:
                    writer.write(frame_img)
                    frames_recorded += 1
                if time.time() - start_time >= duration_seconds:
                    break
        finally:
            writer.release()

        logger.info(
            "Recorded %d frames (%d seconds) to %s",
            frames_recorded,
            duration_seconds,
            out_path,
        )
        return out_path
