"""Hardware acceleration (VA-API) device discovery helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def detect_vaapi_device(
    hwaccel: Optional[str] = None,
    vaapi_device: Optional[str] = None,
) -> Optional[str]:
    """Detect available VA-API DRM render node path, or None if unavailable or disabled."""
    mode = (hwaccel or os.getenv("MOMCOZY_HWACCEL", "auto")).strip().lower()
    if mode in ("none", "cpu", "disabled", "false", "0"):
        return None

    explicit_dev = vaapi_device or os.getenv("MOMCOZY_VAAPI_DEVICE")
    if explicit_dev:
        if Path(explicit_dev).exists() or mode in ("vaapi", "force"):
            return explicit_dev

    for dev in ("/dev/dri/renderD128", "/dev/dri/card0"):
        if Path(dev).exists():
            return dev

    if mode in ("vaapi", "force"):
        return "/dev/dri/renderD128"

    return None
