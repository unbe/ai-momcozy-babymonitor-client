"""Google Chromecast integration for Momcozy BM04 live streaming.

Discovers local Chromecast devices (TVs, Nest Hubs, Google Home / Nest Audio speakers)
via mDNS/DNS-SD (pychromecast) and manages remote live video/audio casting.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import pychromecast
    from pychromecast import Chromecast
    from pychromecast.discovery import CastBrowser, stop_discovery
    PYCHROMECAST_AVAILABLE = True
except ImportError:
    pychromecast = None
    Chromecast = Any  # type: ignore
    CastBrowser = Any  # type: ignore
    stop_discovery = None  # type: ignore
    PYCHROMECAST_AVAILABLE = False


def get_local_ip() -> str:
    """Detect LAN IP address of this host via route lookup."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class ChromecastManager:
    """Manages discovery and casting to local Google Cast devices."""

    def __init__(self, default_port: int = 8421) -> None:
        self.default_port = default_port
        self._browser: Optional[CastBrowser] = None
        self._devices: Dict[str, Any] = {}
        self._active_casts: Dict[str, Dict[str, Any]] = {}
        self._known_hosts: set[str] = set()

    @property
    def is_available(self) -> bool:
        """Return True if pychromecast is installed and usable."""
        return PYCHROMECAST_AVAILABLE

    def start_discovery(self) -> None:
        """Start asynchronous background mDNS discovery for Chromecast devices."""
        if not PYCHROMECAST_AVAILABLE:
            logger.info("pychromecast is not installed; Chromecast discovery disabled.")
            return

        if self._browser is not None:
            return

        try:
            logger.info("Starting background Chromecast mDNS discovery...")
            self._browser = pychromecast.get_chromecasts(
                blocking=False,
                callback=self._on_device_discovered,
                known_hosts=list(self._known_hosts) if self._known_hosts else None,
            )
        except Exception as exc:
            logger.warning("Failed to start Chromecast discovery: %s", exc)

    def stop_discovery(self) -> None:
        """Stop background mDNS discovery."""
        if self._browser is not None and stop_discovery:
            try:
                stop_discovery(self._browser)
            except Exception:
                pass
            self._browser = None

    def add_known_host(self, host: str) -> None:
        """Add a specific IP address to scan for Chromecasts (useful for cross-subnet or Docker)."""
        host = host.strip()
        if not host:
            return
        self._known_hosts.add(host)
        if PYCHROMECAST_AVAILABLE:
            try:
                pychromecast.get_chromecasts(
                    known_hosts=[host],
                    blocking=False,
                    callback=self._on_device_discovered,
                )
            except Exception as exc:
                logger.debug("Failed probing Chromecast host %s: %s", host, exc)

    def _on_device_discovered(self, cast: Any) -> None:
        """Callback invoked whenever a Chromecast is discovered or updated."""
        name = getattr(cast, "name", "") or str(getattr(cast, "uuid", ""))
        host = ""
        port = 8009
        if hasattr(cast, "cast_info") and cast.cast_info:
            host = getattr(cast.cast_info, "host", "") or ""
            port = getattr(cast.cast_info, "port", 8009) or 8009

        logger.info("Discovered Chromecast: '%s' (%s at %s:%d)", name, getattr(cast, "model_name", "Cast"), host, port)
        self._devices[name] = cast
        if host:
            self._devices[host] = cast
        uuid_str = str(getattr(cast, "uuid", ""))
        if uuid_str:
            self._devices[uuid_str] = cast

    def get_devices(self) -> List[Dict[str, Any]]:
        """Return list of known Chromecast devices with live metadata."""
        seen_names = set()
        result: List[Dict[str, Any]] = []

        for cast in list(self._devices.values()):
            name = getattr(cast, "name", "")
            if not name or name in seen_names:
                continue
            seen_names.add(name)

            host = ""
            port = 8009
            if hasattr(cast, "cast_info") and cast.cast_info:
                host = getattr(cast.cast_info, "host", "") or ""
                port = getattr(cast.cast_info, "port", 8009) or 8009

            model = getattr(cast, "model_name", "Google Cast")
            cast_type = getattr(cast, "cast_type", "cast")
            is_audio = cast_type == "audio" or "audio" in model.lower() or "speaker" in model.lower()

            active_info = self._active_casts.get(name) or self._active_casts.get(host)
            status = "idle"
            media_type = ""
            if active_info:
                status = "casting"
                media_type = active_info.get("media_type", "")

            result.append({
                "name": name,
                "model_name": model,
                "host": host,
                "port": port,
                "uuid": str(getattr(cast, "uuid", "")),
                "cast_type": cast_type,
                "is_audio_only": is_audio,
                "status": status,
                "active_media_type": media_type,
            })

        return sorted(result, key=lambda d: d["name"].lower())

    def _resolve_cast(self, target: str) -> Optional[Any]:
        """Find a Chromecast instance by name, UUID, or host IP."""
        target = target.strip()
        if not target:
            return None

        # 1. Exact match in cached devices
        if target in self._devices:
            return self._devices[target]

        # 2. Case-insensitive name match
        for key, cast in self._devices.items():
            if getattr(cast, "name", "").lower() == target.lower():
                return cast

        # 3. Direct probe if target looks like an IP address
        if PYCHROMECAST_AVAILABLE:
            try:
                casts, _ = pychromecast.get_chromecasts(known_hosts=[target], timeout=3)
                if casts:
                    found = casts[0]
                    self._on_device_discovered(found)
                    return found
            except Exception as exc:
                logger.debug("Failed direct probe for %s: %s", target, exc)

        return None

    def cast_media(
        self,
        target: str,
        media_url: str,
        content_type: str = "application/x-mpegURL",
        title: str = "Momcozy Baby Monitor",
        thumb_url: Optional[str] = None,
        media_type: str = "video",
    ) -> Dict[str, Any]:
        """Command target Chromecast to start playback of the given media URL."""
        if not PYCHROMECAST_AVAILABLE:
            raise RuntimeError("pychromecast is not installed. Install with: pip install pychromecast")

        cast = self._resolve_cast(target)
        if not cast:
            raise ValueError(f"Chromecast device '{target}' not found. Ensure it is turned on and on the same network.")

        name = getattr(cast, "name", target)
        host = getattr(getattr(cast, "cast_info", None), "host", target)

        logger.info("Connecting to Chromecast '%s' (%s) to stream %s...", name, host, media_url)
        cast.wait()

        mc = cast.media_controller
        mc.play_media(
            media_url,
            content_type=content_type,
            title=title,
            thumb=thumb_url,
            stream_type="LIVE",
            autoplay=True,
        )

        record = {
            "name": name,
            "host": host,
            "media_url": media_url,
            "media_type": media_type,
            "content_type": content_type,
            "started_at": time.time(),
        }
        self._active_casts[name] = record
        if host:
            self._active_casts[host] = record

        return {
            "status": "ok",
            "device": name,
            "host": host,
            "media_url": media_url,
            "media_type": media_type,
        }

    def stop_cast(self, target: str) -> Dict[str, Any]:
        """Stop playback and quit app on target Chromecast."""
        if not PYCHROMECAST_AVAILABLE:
            raise RuntimeError("pychromecast is not installed.")

        cast = self._resolve_cast(target)
        if not cast:
            raise ValueError(f"Chromecast device '{target}' not found.")

        name = getattr(cast, "name", target)
        host = getattr(getattr(cast, "cast_info", None), "host", target)

        logger.info("Stopping Chromecast playback on '%s'...", name)
        try:
            cast.wait()
            cast.media_controller.stop()
            cast.quit_app()
        except Exception as exc:
            logger.warning("Error stopping cast on %s: %s", name, exc)

        self._active_casts.pop(name, None)
        if host:
            self._active_casts.pop(host, None)

        return {"status": "ok", "device": name, "stopped": True}

    def stop_all(self) -> None:
        """Stop all active casts and shutdown discovery."""
        for name in list(self._active_casts.keys()):
            try:
                self.stop_cast(name)
            except Exception:
                pass
        self.stop_discovery()
