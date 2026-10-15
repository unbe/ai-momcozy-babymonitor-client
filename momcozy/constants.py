"""Momcozy BM04 Constants, DP Definitions, and Protocols.

Reverse-engineered from the official Momcozy Android app (com.lute.momcozy).
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Final

# Momcozy Cloud API endpoints
API_BASE_URL: Final[str] = "https://app-api.mcozycloud.com"
API_BASE_URL_CN: Final[str] = "https://app-api-cn-1.mcozycloud.com"
APP_ID: Final[str] = "momcozy-0719"
APP_VERSION: Final[str] = "3.5.0"
CLIENT_TYPE: Final[str] = "Android"

# Password Argon2 Hashing parameters (from LTPasswordCredentialProtocol)
ARGON2_FIXED_SALT_B64: Final[str] = "5waV6ARzSxRgap4h"
ARGON2_TIME_COST: Final[int] = 2
ARGON2_MEMORY_COST_KB: Final[int] = 4096
ARGON2_PARALLELISM: Final[int] = 1
ARGON2_HASH_LEN_BYTES: Final[int] = 32

# Device Product IDs (Tuya PID)
PRODUCT_ID_BM04: Final[str] = "mzohju3minbjogwz"
PRODUCT_ID_BM04S: Final[str] = "vtgopgsohc3xw7eo"
PRODUCT_ID_BM05: Final[str] = "alqpknen7kcdtnf0"
PRODUCT_ID_BM08: Final[str] = "iw3hw9j4ica3rj1l"

BBM_PRODUCT_IDS: Final[tuple[str, ...]] = (
    PRODUCT_ID_BM04,
    PRODUCT_ID_BM04S,
    PRODUCT_ID_BM05,
    PRODUCT_ID_BM08,
)


class DP(int, Enum):
    """Tuya DataPoint IDs for Momcozy BM04 Baby Monitor."""

    # Basic device controls
    BASIC_INDICATOR = 101       # Boolean: Status LED on/off
    BASIC_FLIP = 103            # Boolean: Flip video 180 degrees (false=normal, true=flipped)
    BASIC_OSD = 104             # Boolean: Timestamp watermark on video
    BASIC_PRIVATE = 105         # Boolean: Privacy/Sleep mode (true=sleep, false=awake)
    MOTION_SENSITIVITY = 106    # String/Enum: "0"=Low, "1"=Medium, "2"=High
    BASIC_NIGHTVISION = 108     # String/Enum: "0"=Auto, "1"=Off, "2"=On
    BASIC_DEVICE_VOLUME = 160   # Integer: Master speaker volume (1-100)
    DEVICE_RESTART = 162        # Boolean: Restart camera

    # Pan, Tilt & Zoom (PTZ)
    PTZ_CONTROL = 119           # String: "0"=Up, "2"=Right, "4"=Down, "6"=Left
    PTZ_STOP = 116              # Boolean: Stop PTZ motion
    PTZ_CALIBRATION = 132       # Boolean: Recalibrate motor positions

    # Sensors & Detections
    SENSOR_TEMPERATURE = 142    # Integer: Current temperature in 0.1 deg C (e.g., 215 = 21.5 C)
    TEMP_ALERT_SWITCH = 236     # Boolean: Temperature alarm enable
    TEMP_MAX_CVALUE = 240       # Integer: Max temp alert in 0.1 deg C (e.g., 300 = 30.0 C)
    TEMP_MIN_CVALUE = 241       # Integer: Min temp alert in 0.1 deg C (e.g., 180 = 18.0 C)
    TEMP_SETTING = 239          # Temperature configuration

    # Motion & Sound Detection
    MOTION_SWITCH = 134         # Boolean: Motion detection on/off
    MOTION_TRACKING = 161       # Boolean: Auto-follow moving objects
    MOTION_AREA_SWITCH = 168    # Boolean: Custom detection zone on/off
    MOTION_AREA = 169           # String: Custom detection zone coordinates
    DECIBEL_SWITCH = 138        # Boolean: Sound/Noise detection on/off
    DECIBEL_SENSITIVITY = 140   # String: Sound sensitivity
    CRY_DETECTION_SWITCH = 167  # Boolean: Cry detection on/off

    # Electronic Fence (Safe / Danger Zone)
    FENCE_AREA_SWITCH = 247     # Boolean: Danger zone alert on/off
    FENCE_AREA_SET_RAW = 249    # String: Polygon coordinates for danger zone

    # Lullabies & Soothing Sounds
    IPC_LULLABY = 231           # Integer/String: Lullaby active state
    IPC_LULLABY_MODE = 232      # String: "0"=Repeat Current, "1"=Repeat All, "2"=Play Once
    IPC_LULLABY_CONTROL = 233   # String: "0"=Stop/Pause, "1"=Play
    IPC_LULLABY_LIST = 234      # String: Track ID ("0", "1", "2", "3", "4", "5", "6", "7")
    IPC_LULLABY_VOLUME = 235    # Integer: Lullaby playback volume (0-100)

    # SD Card & Recording
    SD_STATUS = 110             # Integer: 1=Normal, 2=Formatting, 3=Error, 4=No Card, 5=Full
    SD_STORAGE = 109            # String: Capacity in MB "Total|Used|Free"
    SD_FORMAT = 111             # Boolean: Trigger SD format
    SD_FORMAT_STATUS = 117      # Integer: Formatting progress/result
    RECORD_SWITCH = 150         # Boolean: Continuous SD recording on/off
    RECORD_MODE = 151           # String: "1"=Event record, "2"=Continuous


class PtzDirection(str, Enum):
    """PTZ Direction string codes."""

    UP = "0"
    RIGHT = "2"
    DOWN = "4"
    LEFT = "6"
    UP_RIGHT = "1"
    DOWN_RIGHT = "3"
    DOWN_LEFT = "5"
    UP_LEFT = "7"


class NightVisionMode(str, Enum):
    """Night Vision operational modes."""

    AUTO = "0"
    OFF = "1"
    ON = "2"


class Sensitivity(str, Enum):
    """Sensitivity levels for motion and sound."""

    LOW = "0"
    MEDIUM = "1"
    HIGH = "2"


class LullabyMode(str, Enum):
    """Playback modes for lullabies."""

    SINGLE_REPEAT = "0"
    ALL_REPEAT = "1"


LULLABY_TRACKS: Final[Dict[str, str]] = {
    "0": "Brahms' Lullaby",
    "1": "Hush Little Baby",
    "2": "London Bridge is Falling Down",
    "3": "Twinkle Twinkle Little Star",
    "4": "White Noise",
    "5": "Heartbeat",
    "6": "Rainforest",
    "7": "Gentle Stream",
}
