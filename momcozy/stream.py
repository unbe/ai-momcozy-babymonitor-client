"""Video streaming and recording utilities for Momcozy BM04.

Provides MJPEG live streaming, snapshot taking, and video recording to MP4.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import struct
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator, Optional, Set

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
_AUDIO_SYNC_MARKER = b"\x05\x00\x01\x00\x00\x00\x00\x00"


def _alaw_to_linear(a: int) -> int:
    """Expand one ITU-T G.711 A-law byte to signed 16-bit linear PCM."""
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    else:
        t += 0x108
        if seg > 1:
            t <<= (seg - 1)
    return -t if (a & 0x80) == 0 else t


ALAW_LUT = np.array([_alaw_to_linear(i) for i in range(256)], dtype=np.int16)


class MomcozyAudioHandler:
    """Reassembles and decodes BM04 8000Hz G.711 A-law audio stream into 16-bit linear PCM."""

    def __init__(self, on_pcm: Any = None) -> None:
        self._on_pcm = on_pcm
        self.packet_count = 0

    def feed_record(self, plaintext: bytes) -> None:
        """Parse decrypted media record from conversation 2 and emit 16-bit PCM."""
        if len(plaintext) < 48 or plaintext[:4] != b"\x05\x00\x01\x00":
            return
        ext = struct.unpack("<I", plaintext[16:20])[0]
        hdr_len = 36 if ext == 8 else 28
        if len(plaintext) < hdr_len + 12:
            return
        rtp_pkt = plaintext[hdr_len:]
        payload = rtp_pkt[12:]
        if not payload:
            return
        self.packet_count += 1
        pcm_bytes = ALAW_LUT[np.frombuffer(payload, dtype=np.uint8)].tobytes()
        if self._on_pcm:
            self._on_pcm(pcm_bytes)


class StreamingMp3Encoder:
    """Encodes continuous 16-bit 8000Hz PCM stream into live MP3 (audio/mpeg) frames."""

    def __init__(self, sample_rate: int = 8000, bit_rate: int = 32000) -> None:
        try:
            import av
        except ImportError as e:
            raise ImportError("Audio streaming requires 'av'. Install with pip install av") from e

        self.sample_rate = sample_rate
        self._buf = io.BytesIO()
        self._read_pos = 0
        self._container = av.open(self._buf, mode="w", format="mp3")
        self._stream = self._container.add_stream("mp3", rate=sample_rate)
        self._stream.bit_rate = bit_rate

    def encode(self, pcm_bytes: bytes) -> bytes:
        if not pcm_bytes:
            return b""
        import av
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = self.sample_rate
        for packet in self._stream.encode(frame):
            self._container.mux(packet)
        curr = self._buf.tell()
        self._buf.seek(self._read_pos)
        out = self._buf.read()
        self._read_pos = curr
        self._buf.seek(curr)
        return out

    def flush(self) -> bytes:
        try:
            for packet in self._stream.encode():
                self._container.mux(packet)
            self._container.close()
        except Exception:
            pass
        curr = self._buf.tell()
        self._buf.seek(self._read_pos)
        out = self._buf.read()
        self._read_pos = curr
        return out


def make_wav_header(sample_rate: int = 8000, channels: int = 1, bits_per_sample: int = 16) -> bytes:
    """Generate 44-byte WAV header for streaming PCM (0x7FFFFFFF data size)."""
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        0x7FFFFFFF,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        0x7FFFFFFF,
    )



def parse_hevc_nalus(data: bytes) -> list[tuple[int, bytes]]:
    """Parse Annex B stream into a list of (nal_type, full_nalu_bytes)."""
    nalus: list[tuple[int, bytes]] = []
    indices: list[int] = []
    pos = 0
    data_len = len(data)
    while pos < data_len:
        idx = data.find(b"\x00\x00\x00\x01", pos)
        if idx == -1:
            idx = data.find(b"\x00\x00\x01", pos)
            if idx == -1:
                break
            indices.append(idx)
            pos = idx + 3
        else:
            indices.append(idx)
            pos = idx + 4

    for i in range(len(indices)):
        start = indices[i]
        end = indices[i + 1] if i + 1 < len(indices) else data_len
        nalu = data[start:end]
        if nalu.startswith(b"\x00\x00\x00\x01"):
            hdr_pos = 4
        elif nalu.startswith(b"\x00\x00\x01"):
            hdr_pos = 3
        else:
            continue
        if hdr_pos < len(nalu):
            nal_type = (nalu[hdr_pos] >> 1) & 0x3F
            nalus.append((nal_type, nalu))
    return nalus


def is_hevc_keyframe(data: bytes) -> bool:
    """Check if an Annex B HEVC byte chunk contains VPS, SPS, PPS, or IRAP keyframe slices."""
    for nal_type, _ in parse_hevc_nalus(data):
        if nal_type in (16, 17, 18, 19, 20, 21, 32, 33, 34):
            return True
    return False


def decode_hevc_gop_to_jpeg(gop_chunks: list[bytes], jpeg_quality: int = 80) -> Optional[bytes]:
    """Decode a sequence of HEVC Annex B chunks (starting with a keyframe) to a single JPEG image.

    Decodes all frames in the GOP sequentially to maintain the reference picture buffer,
    then converts and encodes only the final decoded frame as JPEG.
    """
    if not gop_chunks:
        return None
    try:
        import av
    except ImportError:
        return None

    try:
        codec = av.CodecContext.create("hevc", "r")
        last_frame = None
        for chunk in gop_chunks:
            for packet in codec.parse(chunk):
                for frame in codec.decode(packet):
                    last_frame = frame
        for packet in codec.parse(b""):
            for frame in codec.decode(packet):
                last_frame = frame
        for frame in codec.decode():
            last_frame = frame

        if last_frame is None:
            return None

        arr = last_frame.to_ndarray(format="bgr24")
        _, enc = cv2.imencode(".jpg", arr, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
        return enc.tobytes()
    except Exception as exc:
        logger.warning("Error burst-decoding HEVC GOP buffer to JPEG: %s", exc)
        return None


class MomcozyHevcHandler:
    """Reassembles BM04 1080p HEVC video streams, yielding raw chunks and decoded JPEG frames."""

    def __init__(
        self,
        on_frame: Any,
        on_hevc: Optional[Any] = None,
        is_transcoding_needed: Optional[Callable[[], bool]] = None,
        cached_vps: Optional[bytes] = None,
        cached_sps: Optional[bytes] = None,
        cached_pps: Optional[bytes] = None,
        on_params_updated: Optional[Callable[[Optional[bytes], Optional[bytes], Optional[bytes]], None]] = None,
    ) -> None:
        try:
            import av
        except ImportError as e:
            raise ImportError(
                "Live HEVC video streaming requires the 'av' package. "
                "Install it with: pip install av (or pip install -r requirements.txt)"
            ) from e

        self._on_frame = on_frame
        self._on_hevc = on_hevc
        self._is_transcoding_needed = is_transcoding_needed
        self._on_params_updated = on_params_updated
        self._transcoding_active = False
        self._buffer = bytearray()
        self._fu_buffer = bytearray()
        self._fu_is_key = False
        self._param_buffer = bytearray()
        self._latest_vps: Optional[bytes] = cached_vps
        self._latest_sps: Optional[bytes] = cached_sps
        self._latest_pps: Optional[bytes] = cached_pps
        self._codec = av.CodecContext.create("hevc", "r")
        self.frame_count = 0

    def _notify_params_updated(self) -> None:
        if self._on_params_updated and self._latest_sps and self._latest_pps:
            try:
                self._on_params_updated(self._latest_vps, self._latest_sps, self._latest_pps)
            except Exception:
                pass

    def _build_au(self, nalu_bytes: bytes, is_key_candidate: bool) -> tuple[bytes, bool]:
        if not is_key_candidate:
            return (nalu_bytes, False)
        prefix = bytearray(self._param_buffer)
        self._param_buffer = bytearray()
        if not prefix:
            if self._latest_vps:
                prefix.extend(self._latest_vps)
            if self._latest_sps:
                prefix.extend(self._latest_sps)
            if self._latest_pps:
                prefix.extend(self._latest_pps)
        if self._latest_sps and self._latest_pps:
            if self._latest_vps and self._latest_vps not in prefix:
                prefix = bytearray(self._latest_vps) + prefix
            if self._latest_sps not in prefix:
                prefix.extend(self._latest_sps)
            if self._latest_pps not in prefix:
                prefix.extend(self._latest_pps)
            return (bytes(prefix + nalu_bytes), True)
        return (nalu_bytes, False)

    def feed_record(self, plaintext: bytes) -> None:
        self._buffer.extend(plaintext)
        buf = self._buffer

        while True:
            sync_idx = buf.find(_SYNC_MARKER)
            if sync_idx < 0:
                if len(buf) > 7:
                    del buf[:-7]
                break
            if sync_idx > 0:
                del buf[:sync_idx]

            buf_len = len(buf)
            if buf_len < 28:
                break

            ext = struct.unpack_from("<I", buf, 16)[0]
            hdr_len = 36 if ext == 8 else 28
            if buf_len < hdr_len:
                break

            plen = struct.unpack_from("<I", buf, hdr_len - 4)[0]
            total_len = hdr_len + plen
            if buf_len < total_len:
                break

            if total_len < hdr_len + 12:
                del buf[:total_len]
                continue

            payload = buf[hdr_len + 12 : total_len]
            del buf[:total_len]

            if not payload:
                continue

            nal_hdr = payload[0]
            nal_type = (nal_hdr >> 1) & 0x3F

            completed_au: Optional[tuple[bytes, bool]] = None

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
                    self._fu_is_key = (16 <= ftype <= 21)
                else:
                    if self._fu_buffer:
                        self._fu_buffer.extend(payload[3:])

                if e and self._fu_buffer:
                    nalu_bytes = bytes(self._fu_buffer)
                    self._fu_buffer = bytearray()
                    completed_au = self._build_au(nalu_bytes, self._fu_is_key)

            elif nal_type == 48:  # AP (Aggregation Packet)
                ap_off = 2
                while ap_off + 2 <= len(payload):
                    nlen = struct.unpack(">H", payload[ap_off : ap_off + 2])[0]
                    ap_off += 2
                    single_nal = payload[ap_off : ap_off + nlen]
                    ap_off += nlen
                    if not single_nal:
                        continue
                    single_type = (single_nal[0] >> 1) & 0x3F
                    nalu_with_start = b"\x00\x00\x00\x01" + single_nal
                    if single_type == 32:
                        self._latest_vps = nalu_with_start
                        self._notify_params_updated()
                    elif single_type == 33:
                        self._latest_sps = nalu_with_start
                        self._notify_params_updated()
                    elif single_type == 34:
                        self._latest_pps = nalu_with_start
                        self._notify_params_updated()

                    if single_type >= 32:
                        self._param_buffer.extend(nalu_with_start)
                    else:
                        is_key = (16 <= single_type <= 21)
                        completed_au = self._build_au(nalu_with_start, is_key)

            else:  # Single NAL unit
                nalu_with_start = b"\x00\x00\x00\x01" + payload
                if nal_type == 32:
                    self._latest_vps = nalu_with_start
                    self._notify_params_updated()
                elif nal_type == 33:
                    self._latest_sps = nalu_with_start
                    self._notify_params_updated()
                elif nal_type == 34:
                    self._latest_pps = nalu_with_start
                    self._notify_params_updated()

                if nal_type >= 32:
                    self._param_buffer.extend(nalu_with_start)
                else:
                    is_key = (16 <= nal_type <= 21)
                    completed_au = self._build_au(nalu_with_start, is_key)

            if completed_au:
                au_bytes, is_key = completed_au
                if self._on_hevc:
                    try:
                        self._on_hevc(au_bytes, is_key)
                    except Exception as exc:
                        logger.debug("Error in on_hevc callback: %s", exc)

                needs_transcode = True
                if self._is_transcoding_needed is not None:
                    try:
                        needs_transcode = bool(self._is_transcoding_needed())
                    except Exception:
                        needs_transcode = True

                if needs_transcode:
                    if not self._transcoding_active:
                        if not is_key:
                            continue
                        self._transcoding_active = True

                    try:
                        for packet in self._codec.parse(au_bytes):
                            for frame in self._codec.decode(packet):
                                arr = frame.to_ndarray(format="bgr24")
                                _, enc = cv2.imencode(".jpg", arr, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                                jpg_bytes = enc.tobytes()
                                self.frame_count += 1
                                if self._on_frame:
                                    self._on_frame(jpg_bytes)
                    except Exception as exc:
                        logger.warning("Error decoding camera HEVC video packet: %s", exc)
                else:
                    self._transcoding_active = False


def _put_drop_oldest(queue: "asyncio.Queue[Any]", item: Any) -> None:
    """Put item on a bounded queue, discarding the oldest entry when the queue is full."""
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        pass


def _patch_tuya_stream_session() -> None:
    """Monkey-patch tuya_ipc_p2p_sdk to handle BM04 HEVC video and G.711 A-law audio."""
    from tuya_ipc_p2p_sdk.camera_stream import CameraStream
    from tuya_ipc_p2p_sdk.crypto import decrypt_record
    from tuya_ipc_p2p_sdk.stream_session import StreamSession
    from tuya_ipc_p2p_sdk.transport.kcp_segment import parse_segment
    # 1. Patch StreamSession to manage video and audio handlers
    orig_init = StreamSession.__init__

    def momcozy_session_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        self._momcozy_hevc = MomcozyHevcHandler(
            self._on_frame,
            getattr(self, "_on_hevc", None),
            getattr(self, "_is_transcoding_needed", None),
        )
        self._momcozy_audio = MomcozyAudioHandler(getattr(self, "_on_pcm", None))

    def momcozy_on_media_record(self: Any, record: bytes) -> None:
        try:
            pt = decrypt_record(self._answer_key, record)
        except Exception as exc:
            logger.debug("Decryption failed on incoming media packet: %s", exc)
            return
        if hasattr(self, "_momcozy_hevc"):
            try:
                self._momcozy_hevc.feed_record(pt)
                self._frames = self._momcozy_hevc.frame_count
            except Exception as exc:
                logger.warning("Error processing HEVC media packet: %s", exc)

    orig_dial_relay = StreamSession._async_dial_relay

    async def momcozy_dial_relay(self: Any, token: Any, media_key: bytes) -> Any:
        relay = await orig_dial_relay(self, token, media_key)
        relay._momcozy_session = self
        conv2 = relay._conversation(2)

        def on_audio_record(record: bytes) -> None:
            is_needed = getattr(self, "_is_audio_needed", None)
            if is_needed is not None:
                try:
                    if not is_needed():
                        return
                except Exception:
                    pass
            if hasattr(self, "_momcozy_audio") and self._answer_key:
                try:
                    pt = decrypt_record(self._answer_key, record)
                    self._momcozy_audio.feed_record(pt)
                except Exception as exc:
                    logger.debug("Failed decrypting audio record: %s", exc)

        conv2.set_message_handler(on_audio_record)
        return relay

    StreamSession.__init__ = momcozy_session_init
    StreamSession._on_media_record = momcozy_on_media_record
    StreamSession._async_dial_relay = momcozy_dial_relay

    # 3. Patch CameraStream to expose async_audio() and async_hevc()
    orig_cam_init = CameraStream.__init__

    def momcozy_cam_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_cam_init(self, *args, **kwargs)
        self._audio_subscribers: Set[asyncio.Queue[bytes]] = set()
        self._hevc_subscribers: Set[asyncio.Queue[tuple[bytes, bool]]] = set()
        self._is_transcoding_needed: Optional[Callable[[], bool]] = None
        self._is_audio_needed: Optional[Callable[[], bool]] = None

    def momcozy_on_audio(self: Any, pcm: bytes) -> None:
        for subscriber in list(self._audio_subscribers):
            _put_drop_oldest(subscriber, pcm)

    def momcozy_on_hevc(self: Any, chunk: bytes, is_key: bool) -> None:
        # The SDK watchdog only counts transcoded JPEG frames; pass-through HEVC must feed it too.
        self._last_frame_at = time.monotonic()
        for subscriber in list(self._hevc_subscribers):
            _put_drop_oldest(subscriber, (chunk, is_key))

    def momcozy_async_hevc(self: Any) -> Any:
        queue: asyncio.Queue[tuple[bytes, bool]] = asyncio.Queue(maxsize=30)
        self._hevc_subscribers.add(queue)

        async def _generator():
            try:
                while True:
                    yield await queue.get()
            finally:
                self._hevc_subscribers.discard(queue)

        return _generator()

    CameraStream.async_hevc = momcozy_async_hevc

    orig_run_one_session = CameraStream._async_run_one_session

    async def momcozy_run_one_session(self: Any) -> bool:
        config = await self._client.async_stream_config(self._device_id, self._local_key)
        identity = await self._client.async_mqtt_identity()
        uid = await self._client.async_uid()
        session = StreamSession(config, identity, uid, self._on_frame)
        session._on_pcm = self._on_audio
        session._on_hevc = self._on_hevc
        session._is_transcoding_needed = getattr(self, "_is_transcoding_needed", None)
        session._is_audio_needed = getattr(self, "_is_audio_needed", None)
        if hasattr(session, "_momcozy_hevc"):
            session._momcozy_hevc._on_hevc = self._on_hevc
            session._momcozy_hevc._is_transcoding_needed = getattr(self, "_is_transcoding_needed", None)
            cached_params = getattr(self, "_cached_params", None)
            if cached_params:
                vps, sps, pps = cached_params
                if vps and not session._momcozy_hevc._latest_vps:
                    session._momcozy_hevc._latest_vps = vps
                if sps and not session._momcozy_hevc._latest_sps:
                    session._momcozy_hevc._latest_sps = sps
                if pps and not session._momcozy_hevc._latest_pps:
                    session._momcozy_hevc._latest_pps = pps
            session._momcozy_hevc._on_params_updated = getattr(self, "_update_cached_params", None)
        if hasattr(session, "_momcozy_audio"):
            session._momcozy_audio._on_pcm = self._on_audio
        self._session = session
        try:
            await session.async_start()
            self._notify_state()
            reason = await session.async_wait_closed()
            return session.frame_count > 0
        finally:
            self._session = None
            self._last_frame_at = 0.0
            self._first_frame.clear()
            self._motion.reset()
            self._notify_state()
            await session.async_close()

    async def momcozy_async_audio(self: Any) -> AsyncIterator[bytes]:
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self._audio_subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._audio_subscribers.discard(queue)

    CameraStream.__init__ = momcozy_cam_init
    CameraStream._on_audio = momcozy_on_audio
    CameraStream._on_hevc = momcozy_on_hevc
    CameraStream._async_run_one_session = momcozy_run_one_session
    CameraStream.async_audio = momcozy_async_audio

    StreamSession._momcozy_patched = True


def _configure_tuya_p2p_sdk() -> None:
    """Configure tuya_ipc_p2p_sdk module constants with Momcozy OEM credentials."""
    import tuya_ipc_p2p_sdk.const as sdk_const
    import tuya_ipc_p2p_sdk.gateway.client as sdk_gw_client
    import tuya_ipc_p2p_sdk.gateway.identity as sdk_gw_identity

    # Ensure Tuya P2P SDK errors and state warnings are visible in server logs
    p2p_log = logging.getLogger("tuya_ipc_p2p_sdk")
    p2p_log.setLevel(logging.INFO)
    p2p_log.propagate = True

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

    import hmac
    import tuya_ipc_p2p_sdk.crypto as sdk_crypto
    sdk_crypto.hmac_sha1 = lambda key, message: hmac.digest(key, message, "sha1")


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


class _Lease:
    """Token for one user of the camera stream (see VideoStreamer.acquire)."""

    __slots__ = ("linger",)

    def __init__(self, linger: float) -> None:
        self.linger = linger


class VideoStreamer:
    """Manages the video stream from Momcozy BM04.

    Stream lifetime is lease based: every user of the stream (viewers, timelapse, the night
    vision sampler) holds a lease. When the last lease is released the stream stays up for
    that lease's `linger` seconds (never shorter than a linger already pending) and is then
    disconnected. `idle_timeout <= 0` disables automatic disconnects entirely.
    """

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
        idle_timeout: float = 300.0,
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
        self.idle_timeout = float(idle_timeout)

        self._running = False
        self._connected = False
        self._last_error: Optional[str] = None
        self._latest_frame: Optional[bytes] = None
        self._latest_frame_time: float = 0.0
        self._latest_detection_snapshot: Optional[bytes] = None
        self._latest_detection_time: Optional[float] = None
        self._latest_detection_trigger: str = ""
        self._listeners: Set[asyncio.Queue[bytes]] = set()
        self._audio_listeners: Set[asyncio.Queue[bytes]] = set()
        self._hevc_listeners: Set[asyncio.Queue[tuple[bytes, bool]]] = set()
        self._hevc_resync: Set[asyncio.Queue[tuple[bytes, bool]]] = set()
        self._cached_vps: Optional[bytes] = None
        self._cached_sps: Optional[bytes] = None
        self._cached_pps: Optional[bytes] = None
        self._latest_hevc_keyframe: Optional[bytes] = None
        self._keyframe_time: float = 0.0
        self._gop_chunks: list[bytes] = []
        self._snapshot_requests: Set[object] = set()
        self._stream_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self.keepalive_predicate: Optional[Callable[[], bool]] = None
        self._leases: Set[_Lease] = set()
        self._stop_deadline: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def listener_count(self) -> int:
        """Number of active client queues subscribing to the video stream."""
        return len(self._listeners)

    @property
    def audio_listener_count(self) -> int:
        """Number of active client queues subscribing to the audio stream."""
        return len(self._audio_listeners)

    @property
    def hevc_listener_count(self) -> int:
        """Number of active client queues subscribing to the raw HEVC pass-through stream."""
        return len(self._hevc_listeners)

    @property
    def total_listener_count(self) -> int:
        """Total active listeners across video, audio, and HEVC pass-through."""
        return len(self._listeners) + len(self._audio_listeners) + len(self._hevc_listeners)

    @property
    def gop_buffer_size(self) -> int:
        """Number of raw HEVC chunks in the current rolling GOP buffer."""
        return len(self._gop_chunks)

    @property
    def _snapshot_requested(self) -> bool:
        return len(self._snapshot_requests) > 0

    @_snapshot_requested.setter
    def _snapshot_requested(self, val: bool) -> None:
        if val:
            self._snapshot_requests.add(self)
        else:
            self._snapshot_requests.discard(self)

    @property
    def keepalive(self) -> bool:
        """Return True if background tasks or predicate require keeping the stream alive."""
        if hasattr(self, "keepalive_predicate") and self.keepalive_predicate is not None:
            try:
                return bool(self.keepalive_predicate())
            except Exception:
                return False
        return False

    def is_transcoding_needed(self) -> bool:
        """Return True only if an active listener or pending snapshot requires JPEG transcoding."""
        return len(self._listeners) > 0 or len(self._snapshot_requests) > 0

    def is_audio_needed(self) -> bool:
        """Return True only if an active listener is consuming audio."""
        return len(self._audio_listeners) > 0

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def latest_frame(self) -> Optional[bytes]:
        return self._latest_frame

    @property
    def latest_frame_time(self) -> float:
        return self._latest_frame_time

    @property
    def latest_detection_snapshot(self) -> Optional[bytes]:
        return self._latest_detection_snapshot

    @property
    def latest_detection_time(self) -> Optional[float]:
        return self._latest_detection_time

    @property
    def latest_detection_trigger(self) -> str:
        return self._latest_detection_trigger

    def capture_detection_snapshot(self, trigger: str = "Live Snapshot", frame: Optional[bytes] = None) -> Optional[bytes]:
        """Freeze and save current video frame as detection snapshot."""
        target = frame or self._latest_frame
        if target:
            self._latest_detection_snapshot = target
            self._latest_detection_time = time.time()
            self._latest_detection_trigger = trigger
            logger.info("Saved detection snapshot for %s (trigger: %s, %d bytes)", self.device_id, trigger, len(target))
            return target
        return None

    async def get_detection_snapshot(self, timeout_seconds: float = 5.0) -> bytes:
        """Return the latest detection snapshot, or capture a live frame if none stored yet."""
        if self._latest_detection_snapshot:
            return self._latest_detection_snapshot
        if self._latest_frame:
            self._latest_detection_snapshot = self._latest_frame
            self._latest_detection_time = time.time()
            self._latest_detection_trigger = "Live Capture"
            return self._latest_frame
        frame = await self.get_snapshot(timeout_seconds=timeout_seconds)
        self._latest_detection_snapshot = frame
        self._latest_detection_time = time.time()
        self._latest_detection_trigger = "Live Snapshot"
        return frame

    @property
    def stream_mode(self) -> str:
        """Stream transport mode: 'cloud_relay' or 'local'."""
        return "cloud_relay"

    @property
    def transport_description(self) -> str:
        """Human-readable description of video stream transport."""
        return "Cloud Relay (Tuya P2P)"

    @property
    def stream_format(self) -> str:
        """Format and transcoding status of the video stream."""
        if not self._connected:
            return "Idle"
        if self.is_transcoding_needed():
            return "MJPEG (Transcoded)"
        if self._hevc_listeners:
            return "HEVC (Pass-through)"
        return "HEVC (Native)"

    # ------------------------------------------------------------------
    # Stream lifetime (leases + idle stop)
    # ------------------------------------------------------------------

    def acquire(self, linger: Optional[float] = None) -> _Lease:
        """Register a user of the stream. `linger` defaults to the idle timeout."""
        lease = _Lease(self.idle_timeout if linger is None else float(linger))
        self._leases.add(lease)
        self._cancel_idle_stop()
        return lease

    def release(self, lease: _Lease, linger: Optional[float] = None) -> None:
        """Release a lease; the stream stops `linger` seconds after the last one is gone."""
        if lease not in self._leases:
            return
        self._leases.discard(lease)
        if not self._leases and self._running:
            self._schedule_idle_stop(lease.linger if linger is None else float(linger))

    @contextlib.contextmanager
    def lease(self, linger: Optional[float] = None) -> Iterator[_Lease]:
        """Hold a lease for the duration of a `with` block."""
        token = self.acquire(linger)
        try:
            yield token
        finally:
            self.release(token)

    @contextlib.contextmanager
    def _listening(self, listeners: Set[Any], queue: Any, kind: str, leased: bool = True) -> Iterator[None]:
        """Register a subscriber queue (and, optionally, a lease) with join/leave logging."""
        listeners.add(queue)
        token = self.acquire() if leased else None
        logger.info("Client subscribed to %s (active: %d, device: %s)", kind, len(listeners), self.device_id)
        try:
            yield
        finally:
            listeners.discard(queue)
            self._hevc_resync.discard(queue)
            logger.info("Client unsubscribed from %s (remaining: %d, device: %s)", kind, len(listeners), self.device_id)
            if token is not None:
                self.release(token)

    def _schedule_idle_stop(self, linger: Optional[float] = None) -> None:
        """Arm the idle-stop timer unless leases are held or idle stop is disabled.

        The stop deadline only ever moves forward, so a short linger (e.g. a night vision
        check) can never cut short a longer one already pending (e.g. a departed viewer).
        """
        if self.idle_timeout <= 0 or self._leases or self.keepalive:
            return
        delay = self.idle_timeout if linger is None else linger
        self._stop_deadline = max(self._stop_deadline, time.monotonic() + delay)
        if self._idle_task and not self._idle_task.done():
            return
        try:
            self._idle_task = asyncio.get_running_loop().create_task(self._idle_stop_timer())
        except RuntimeError:
            pass

    def _cancel_idle_stop(self) -> None:
        if self._idle_task and not self._idle_task.done():
            try:
                curr = asyncio.current_task()
            except RuntimeError:
                curr = None
            if self._idle_task is not curr:
                self._idle_task.cancel()
        self._idle_task = None

    async def _idle_stop_timer(self) -> None:
        try:
            remaining = self._stop_deadline - time.monotonic()
            if remaining > 0.5:
                logger.info(
                    "No stream clients connected. Starting idle timer (%ds) before disconnecting cloud stream.",
                    int(remaining),
                )
            while remaining > 0:
                await asyncio.sleep(remaining)
                remaining = self._stop_deadline - time.monotonic()
            if self._running and not self._leases and not self.keepalive:
                logger.info("No stream clients remaining. Disconnecting cloud video stream to conserve resources.")
                await self.stop()
        except asyncio.CancelledError:
            pass

    async def start(self) -> None:
        """Start background streaming task."""
        async with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_deadline = 0.0
            self._stream_task = asyncio.create_task(self._stream_loop())
            logger.info("Video streamer started for device %s", self.device_id)
        self._schedule_idle_stop()

    async def stop(self) -> None:
        """Stop background streaming task."""
        self._cancel_idle_stop()
        async with self._lock:
            self._running = False
            self._connected = False
            self._reset_media_buffers()
            if self._stream_task:
                self._stream_task.cancel()
                try:
                    await self._stream_task
                except asyncio.CancelledError:
                    pass
                self._stream_task = None
            logger.info("Video streamer stopped")

    def _reset_media_buffers(self) -> None:
        """Drop cached frames/keyframes so nothing stale outlives a P2P session."""
        self._latest_frame = None
        self._latest_hevc_keyframe = None
        self._keyframe_time = 0.0
        self._gop_chunks = []

    def _broadcast_hevc(self, chunk: bytes, is_key: bool) -> None:
        """Fan out an HEVC chunk to pass-through listeners.

        A listener whose queue overflows is flushed and resumed at the next keyframe, so it
        never receives delta frames whose reference frames were dropped.
        """
        for q in list(self._hevc_listeners):
            if q in self._hevc_resync:
                if not is_key:
                    continue
                self._hevc_resync.discard(q)
            if q.full():
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                if not is_key:
                    self._hevc_resync.add(q)
                    continue
            try:
                q.put_nowait((chunk, is_key))
            except asyncio.QueueFull:
                pass

    async def _stream_loop(self) -> None:
        """Internal loop fetching frames from the camera P2P stream."""
        backoff = 2
        while self._running:
            try:
                logger.info("Connecting to P2P video stream for %s...", self.device_id)
                self._last_error = None
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
                    stream._is_transcoding_needed = self.is_transcoding_needed
                    stream._is_audio_needed = self.is_audio_needed
                    stream._cached_params = (self._cached_vps, self._cached_sps, self._cached_pps)

                    def update_params(vps: Optional[bytes], sps: Optional[bytes], pps: Optional[bytes]) -> None:
                        if vps:
                            self._cached_vps = vps
                        if sps:
                            self._cached_sps = sps
                        if pps:
                            self._cached_pps = pps

                    stream._update_cached_params = update_params

                    def on_state():
                        if getattr(stream, "needs_power_cycle", False):
                            logger.error(
                                "Camera %s answered busy repeatedly and needs to be power cycled/rebooted.",
                                self.device_id,
                            )
                            self._last_error = f"Camera {self.device_id} reported busy repeatedly. Reboot camera."
                        elif getattr(stream, "motion_detected", False):
                            logger.info("Motion detected on camera %s! Freezing detection snapshot.", self.device_id)
                            self.capture_detection_snapshot(trigger="Motion Detected")
                        elif getattr(stream, "streaming", False):
                            logger.info("P2P session confirmed streaming from camera %s.", self.device_id)

                    stream.add_state_listener(on_state)
                    await stream.async_start()
                    logger.info("P2P stream initiated for %s. Awaiting media frames...", self.device_id)
                    self._last_error = None
                    backoff = 2

                    async def audio_worker():
                        try:
                            async for pcm in stream.async_audio():
                                if not self._running:
                                    break
                                for q in list(self._audio_listeners):
                                    _put_drop_oldest(q, pcm)
                        except asyncio.CancelledError:
                            pass
                        except Exception as exc:
                            logger.debug("Audio consumer worker exited: %s", exc)

                    audio_task = asyncio.create_task(audio_worker())

                    async def jpeg_worker():
                        try:
                            async for jpeg_frame in stream.async_frames():
                                if not self._running:
                                    break
                                self._latest_frame = jpeg_frame
                                self._latest_frame_time = time.monotonic()
                                if not self._latest_detection_snapshot:
                                    self.capture_detection_snapshot(trigger="Session Baseline Snapshot")
                                for q in list(self._listeners):
                                    _put_drop_oldest(q, jpeg_frame)
                        except asyncio.CancelledError:
                            pass
                        except Exception as exc:
                            logger.debug("JPEG consumer worker exited: %s", exc)

                    jpeg_task = asyncio.create_task(jpeg_worker())

                    hevc_iter = stream.async_hevc()
                    chunks_received = 0
                    initial_timeout = 20.0
                    cadence_timeout = 10.0

                    try:
                        while self._running:
                            timeout = initial_timeout if chunks_received == 0 else cadence_timeout
                            try:
                                chunk, is_key = await asyncio.wait_for(hevc_iter.__anext__(), timeout=timeout)
                            except TimeoutError:
                                if chunks_received == 0:
                                    logger.warning(
                                        "Timed out waiting for initial video stream from camera %s (%.0fs). Re-establishing P2P session...",
                                        self.device_id,
                                        timeout,
                                    )
                                    # Do not poison _last_error on initial cold-start reconnect
                                    # so status endpoint keeps reporting stream_running: true, stream_connected: false
                                else:
                                    err_msg = f"Live video stream stalled: no data received from camera {self.device_id} for {timeout:.0f}s"
                                    logger.warning("%s. Reconnecting...", err_msg)
                                    self._last_error = err_msg
                                self._connected = False
                                break
                            except StopAsyncIteration:
                                logger.info("Video stream closed by camera %s.", self.device_id)
                                break

                            chunks_received += 1
                            if chunks_received == 1:
                                logger.info(
                                    "First live video stream chunk received for %s (%d bytes). Stream active.",
                                    self.device_id,
                                    len(chunk),
                                )
                            elif chunks_received % 1000 == 0:
                                logger.info(
                                    "Stream healthy: %d chunks received from camera %s (transcoding: %s)",
                                    chunks_received,
                                    self.device_id,
                                    self.is_transcoding_needed(),
                                )

                            self._connected = True
                            self._last_error = None

                            if is_key:
                                self._latest_hevc_keyframe = chunk
                                self._keyframe_time = time.monotonic()
                                self._gop_chunks = [chunk]
                            elif self._gop_chunks and len(self._gop_chunks) < 150:
                                self._gop_chunks.append(chunk)

                            self._broadcast_hevc(chunk, is_key)
                    finally:
                        self._connected = False
                        # A reconnect starts a new GOP: never serve keyframes from the dead session.
                        self._reset_media_buffers()
                        audio_task.cancel()
                        jpeg_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await asyncio.gather(audio_task, jpeg_task)
                        await stream.async_stop()
            except asyncio.CancelledError:
                self._connected = False
                break
            except Exception as e:
                self._connected = False
                self._last_error = str(e)
                logger.error("P2P stream error for %s: %s. Reconnecting in %ss...", self.device_id, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 1.5)

    async def subscribe(self) -> AsyncIterator[bytes]:
        """Subscribe to live frames."""
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=10)
        if self._latest_frame and self._connected:
            queue.put_nowait(self._latest_frame)
        with self._listening(self._listeners, queue, "live stream"):
            while self._running:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=25.0)
                except TimeoutError:
                    continue

    async def subscribe_audio(self, format: str = "mp3") -> AsyncIterator[bytes]:
        """Subscribe to live audio stream from the camera microphone."""
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=150)
        encoder = StreamingMp3Encoder(sample_rate=8000, bit_rate=32000) if format == "mp3" else None
        with self._listening(self._audio_listeners, queue, f"live audio stream ({format})"):
            if format == "wav":
                yield make_wav_header(sample_rate=8000, channels=1, bits_per_sample=16)

            while self._running:
                try:
                    pcm_chunk = await asyncio.wait_for(queue.get(), timeout=25.0)
                except TimeoutError:
                    continue
                if encoder:
                    mp3_bytes = encoder.encode(pcm_chunk)
                    if mp3_bytes:
                        yield mp3_bytes
                else:
                    yield pcm_chunk

    async def subscribe_hevc(self, leased: bool = True) -> AsyncIterator[tuple[bytes, bool]]:
        """Subscribe to raw HEVC Annex B chunks (chunk_bytes, is_key).

        With leased=False the subscription does not keep the stream alive by itself; use it
        for consumers that already hold their own lease (e.g. the timelapse recorder).
        """
        queue: asyncio.Queue[tuple[bytes, bool]] = asyncio.Queue(maxsize=30)
        with self._listening(self._hevc_listeners, queue, "raw HEVC pass-through stream", leased):
            if self._latest_hevc_keyframe and self._connected:
                yield self._latest_hevc_keyframe, True

            started = False
            while self._running:
                try:
                    chunk, is_key = await asyncio.wait_for(queue.get(), timeout=25.0)
                except TimeoutError:
                    continue
                if not started:
                    if not is_key:
                        continue
                    started = True
                yield chunk, is_key

    # ------------------------------------------------------------------
    # One-shot captures
    # ------------------------------------------------------------------

    async def get_keyframe(self, timeout_seconds: float = 25.0, max_age: float = 0.0) -> bytes:
        """Return an Annex B keyframe (VPS/SPS/PPS + IDR) received at most `max_age` seconds ago.

        Starts the stream if needed. Cheap compared with get_snapshot(): nothing is decoded
        here. Callers that want the stream to disconnect right after should hold a
        `lease(linger=0)` around the call.
        """
        if not self._running:
            await self.start()
        start_time = time.monotonic()
        while True:
            now = time.monotonic()
            if self._latest_hevc_keyframe and self._keyframe_time >= start_time - max_age:
                return self._latest_hevc_keyframe
            if now - start_time >= timeout_seconds:
                break
            if self._last_error and now - start_time > 5.0:
                raise RuntimeError(f"Camera stream error: {self._last_error}")
            await asyncio.sleep(0.05)
        err_suffix = f": {self._last_error}" if self._last_error else ""
        raise TimeoutError(f"Timed out waiting for keyframe from camera{err_suffix}")

    async def get_snapshot(self, timeout_seconds: float = 10.0, max_age: float = 0.5) -> bytes:
        """Capture a single JPEG: a recent live frame, else a burst-decode of the rolling GOP."""
        with self.lease():
            if not self._running:
                await self.start()

            if self._latest_frame and (time.monotonic() - self._latest_frame_time) < max_age:
                return self._latest_frame

            req_id = object()
            self._snapshot_requests.add(req_id)
            start_time = time.monotonic()
            try:
                loop = asyncio.get_running_loop()
                while time.monotonic() - start_time < timeout_seconds:
                    # A frame decoded by the live transcoder since the request started:
                    if self._latest_frame and self._latest_frame_time >= start_time:
                        return self._latest_frame
                    # Otherwise decode the GOP buffered so far:
                    if self._gop_chunks:
                        jpeg_bytes = await loop.run_in_executor(None, decode_hevc_gop_to_jpeg, list(self._gop_chunks))
                        if jpeg_bytes:
                            self._latest_frame = jpeg_bytes
                            self._latest_frame_time = time.monotonic()
                            if not self._latest_detection_snapshot:
                                self.capture_detection_snapshot(trigger="Snapshot Baseline", frame=jpeg_bytes)
                            return jpeg_bytes
                    if self._last_error and time.monotonic() - start_time > 3.0:
                        raise RuntimeError(f"Camera stream error: {self._last_error}")
                    await asyncio.sleep(0.03)

                if self._latest_frame:
                    logger.warning("Timed out waiting for fresh frame; returning latest available frame")
                    return self._latest_frame

                err_suffix = f": {self._last_error}" if self._last_error else ""
                raise TimeoutError(f"Timed out waiting for video frame from camera{err_suffix}")
            finally:
                self._snapshot_requests.discard(req_id)

    async def get_clean_frame(self, timeout_seconds: float = 25.0) -> bytes:
        """Capture a clean, newly decoded JPEG frame from the camera."""
        return await self.get_snapshot(timeout_seconds=timeout_seconds, max_age=0.0)

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
