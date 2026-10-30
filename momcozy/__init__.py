"""Momcozy BM04 Baby Monitor Python Client.

A Python library and CLI for the Momcozy BM04 Smart Baby Monitor.
Supports video streaming, sensor readings (temperature, crying, noise, motion),
and settings control (night vision, PTZ, alarms, lullabies).
"""

import os

# Disable aiohttp sendfile system call to prevent Python asyncio sslproto _set_app_protocol crash on client disconnect over HTTPS
os.environ.setdefault("AIOHTTP_NOSENDFILE", "1")

from .cloud import MomcozyCloudApi, TuyaCredentials, hash_password
from .constants import (
    DP,
    LULLABY_TRACKS,
    PRODUCT_ID_BM04,
    PRODUCT_ID_BM04S,
    LullabyMode,
    NightVisionMode,
    PtzDirection,
    Sensitivity,
)
from .device import BM04Status, MomcozyMonitor
from .stream import VideoStreamer
from .timelapse import TimelapseRecorder
from .web import WebServer

__version__ = "0.1.0"
__all__ = [
    "BM04Status",
    "DP",
    "LULLABY_TRACKS",
    "LullabyMode",
    "MomcozyCloudApi",
    "MomcozyMonitor",
    "NightVisionMode",
    "PRODUCT_ID_BM04",
    "PRODUCT_ID_BM04S",
    "PtzDirection",
    "Sensitivity",
    "TimelapseRecorder",
    "TuyaCredentials",
    "VideoStreamer",
    "WebServer",
    "hash_password",
]
