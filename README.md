# Momcozy BM04 Baby Monitor Python Client & Streaming Server

A comprehensive Python client, streaming server, and CLI for the **Momcozy BM04 Smart Baby Monitor** (and compatible BM04S / BM05 models).

Reverse-engineered from the official Momcozy Android app (`com.lute.momcozy`), this library gives you full local and remote control over your baby monitor without being locked into the mobile app.

---

## 🌟 Key Features

- 📹 **Live Video Streaming & Recording:**
  - P2P Video streaming directly from the camera.
  - Built-in MJPEG HTTP stream (`/stream`) compatible with any web browser, VLC, or Home Assistant (`mjpeg` camera component).
  - Snapshot capture (`/snapshot` or JPEG export) and MP4 clip recording.
- 🌡️ **Sensor Telemetry & Monitoring:**
  - Real-time room temperature in both °C and °F (precision: 0.1°C).
  - Configurable temperature alarm boundaries (min/max thresholds and alert toggles).
  - Baby cry detection switch and sensitivity.
  - Sound & noise detection with configurable sensitivity.
  - Motion detection, sensitivity levels, and auto-tracking.
- 🎛️ **Full Device Control:**
  - **Pan / Tilt / Zoom (PTZ):** Continuous and step movement in all 4 directions (`up`, `down`, `left`, `right`, `stop`).
  - **Night Vision:** `Auto` (automatic IR cut), `On`, or `Off`.
  - **Privacy / Sleep Mode:** Immediately disables the camera lens and stream for privacy.
  - **Lullabies:** Play, stop, track selection (8 soothing tracks including White Noise, Heartbeat, and Brahms' Lullaby), and volume control.
  - **Status LED:** Turn off the bright front LED so it doesn't disturb the baby.
  - **180° Flip:** Flip the camera feed when mounted upside-down on a crib or wall bracket.
- 🌐 **Modern Web UI Dashboard:**
  - Responsive web dashboard accessible from desktop or mobile browser.
  - Live video feed, PTZ directional pad, quick setting toggles, and real-time sensor dials.
- ⚡ **Dual Architecture (Local LAN + Cloud):**
  - Direct local LAN control using Tuya protocol v3.3/3.4 (ultra-fast, zero cloud latency).
  - Momcozy Cloud API integration with official **Argon2id** password hashing.

---

## 🔬 Protocol & Reverse Engineering Details

> [!TIP]
> For a comprehensive technical overview of the architecture, protocol interactions, authentication bridging, and P2P streaming pipeline, see [**`ARCHITECTURE.md`**](ARCHITECTURE.md).

Through decompilation of the Momcozy Android APK (`com.lute.momcozy`), the following architecture was discovered:

1. **Momcozy Cloud Gateway:** `https://app-api.mcozycloud.com`
2. **Password Hashing:** Official app uses **Argon2id** (`time_cost=2`, `memory_cost=4096`, `parallelism=1`, `hash_len=32`) with a static salt `base64.b64decode("5waV6ARzSxRgap4h")`.
3. **Underlying Hardware Platform:** Tuya / ThingSmart IPC SDK.
   - BM04 Product ID: `mzohju3minbjogwz`
   - BM04S Product ID: `vtgopgsohc3xw7eo`
   - BM05 Product ID: `alqpknen7kcdtnf0`
4. **DataPoint (DP) Mapping:**
   | DP ID | Identifier | Type | Description |
   |:---|:---|:---|:---|
   | **142** | `sensor_temperature` | Integer | Room temperature in 0.1°C (e.g., `215` = 21.5°C) |
   | **236** | `temp_alert_switch` | Boolean | Temperature out-of-range alarm toggle |
   | **240** | `temp_max_cvalue` | Integer | Max temp alarm threshold in 0.1°C |
   | **241** | `temp_min_cvalue` | Integer | Min temp alarm threshold in 0.1°C |
   | **101** | `basic_indicator` | Boolean | Front status LED light |
   | **103** | `basic_flip` | Boolean | 180° Video rotation |
   | **104** | `basic_osd` | Boolean | Timestamp watermark on video |
   | **105** | `basic_private` | Boolean | Privacy / Sleep mode |
   | **108** | `basic_nightvision` | Enum | IR Night Vision (`0`: Auto, `1`: Off, `2`: On) |
   | **116** | `ptz_stop` | Boolean | Halt PTZ movement |
   | **119** | `ptz_control` | Enum | PTZ direction (`0`: Up, `2`: Right, `4`: Down, `6`: Left) |
   | **134** | `motion_switch` | Boolean | Motion detection alert |
   | **106** | `motion_sensitivity` | Enum | Motion sensitivity (`0`: Low, `1`: Medium, `2`: High) |
   | **161** | `motion_tracking` | Boolean | Auto-follow motion |
   | **138** | `decibel_switch` | Boolean | Sound / Noise detection |
   | **140** | `decibel_sensitivity` | Enum | Sound sensitivity (`0`: Low, `1`: Medium, `2`: High) |
   | **167** | `cry_detection_switch` | Boolean | Baby cry detection alert |
   | **233** | `ipc_lullaby_control` | Enum | Lullaby playback (`0`: Stop, `1`: Play) |
   | **234** | `ipc_lullaby_list` | Enum | Track selection (`0`..`7`) |
   | **235** | `ipc_lullaby_volume` | Integer | Lullaby volume (0-100) |
   | **160** | `basic_device_volume`| Integer | Monitor speaker master volume (1-100) |

---

## 📦 Installation

Ensure you have Python 3.9+ installed.

```bash
git clone https://github.com/artem/momcozy-client.git
cd momcozy-client

# Install requirements
pip install -r requirements.txt

# Or install package locally:
pip install -e .
```

### System Dependencies for Video Streaming
The video streamer relies on `opencv-python-headless` and `av` (PyAV / FFmpeg). On Debian/Ubuntu Linux:
```bash
sudo apt-get update && sudo apt-get install -y libgl1 libglib2.0-0 ffmpeg
```

---

## 🐳 Docker Deployment

The fastest way to deploy the web dashboard and streaming server without installing local dependencies is using Docker:

> [!IMPORTANT]
> **Prerequisite:** Ensure `.momcozy.json` exists in your project directory before starting Docker (`touch .momcozy.json`). If a bind-mounted file does not exist on the host, the Docker daemon will automatically create it as a directory instead of a file.

### 1. Using Docker Compose (Recommended)

```bash
# 1. Ensure config file exists in the project root
touch .momcozy.json

# 2. Log in to your Momcozy account (credentials will be saved to .momcozy.json)
docker compose run --rm momcozy login

# 3. Start the web dashboard and streaming server (builds automatically)
docker compose up -d

# 4. Check logs
docker compose logs -f
```

The web dashboard is now live at [**http://localhost:8421**](http://localhost:8421).

### 2. Using Docker CLI

```bash
# 1. Ensure config file exists
touch .momcozy.json

# 2. Build the image
docker build -t momcozy-client .

# 3. Log in
docker run -it --rm -v $(pwd)/.momcozy.json:/config/.momcozy.json momcozy-client login

# 4. Run the streaming server
docker run -d \
  --name momcozy-monitor \
  --restart unless-stopped \
  -p 8421:8421 \
  -v $(pwd)/.momcozy.json:/config/.momcozy.json \
  momcozy-client

# Run any CLI command (e.g. status)
docker run --rm -v $(pwd)/.momcozy.json:/config/.momcozy.json momcozy-client status
```

---

## 🚀 Command-Line Usage

The package provides a unified CLI named `momcozy`.

### 1. Authenticate with Momcozy Cloud
Log in with your existing Momcozy mobile app account to retrieve device IDs and Tuya encryption keys:

```bash
momcozy login
```
*Your session is safely stored in `.momcozy.json` (next to `momcozy.py`) for subsequent commands.*

### 2. View Telemetry & Status
Read current room temperature, alarm limits, night vision, and sensor states:

```bash
# Formatted summary
momcozy status

# JSON output (ideal for scripting and Home Assistant command-line sensors)
momcozy status --json
```

Example output:
```text
--- Momcozy BM04 Baby Monitor Status ---
  • Temperature:        22.5°C (72.5°F)
  • Temp Alerts:        Enabled (Range: 18.0°C - 28.0°C)
  • Privacy / Sleep:    OFF (Active)
  • Night Vision:       Auto
  • Status LED:         ON
  • Motion Detection:   ON (Sensitivity: Medium)
  • Motion Tracking:    OFF
  • Sound Detection:    ON
  • Cry Detection:      ON
  • Lullaby:            Stopped
  • Master Volume:      70%
```

To quickly query only temperature:
```bash
momcozy temperature
# 22.5°C (72.5°F)
```

### 3. Adjust Monitor Settings

```bash
# Night Vision (auto, on, off)
momcozy set --night-vision auto

# Enable Privacy / Sleep Mode (turns off camera lens)
momcozy set --privacy on

# Disable Status LED
momcozy set --led off

# Flip Video (for ceiling/crib mounting)
momcozy set --flip on

# Enable Cry & Sound Detection
momcozy set --cry on --sound on --sound-sensitivity 2

# Configure Temperature Alarm (Min 19°C, Max 27°C)
momcozy set --temp-alert on --temp-min 19.0 --temp-max 27.0

# Adjust Master Volume
momcozy set --volume 65
```

### 4. Pan / Tilt / Zoom (PTZ) Controls

```bash
momcozy ptz up
momcozy ptz down --duration 1.0
momcozy ptz left
momcozy ptz right
momcozy ptz stop
```

### 5. Play Lullabies & Soothing Sounds

List available tracks:
```bash
momcozy lullaby list
```
Tracks available:
- `0`: Brahms' Lullaby
- `1`: Hush Little Baby
- `2`: London Bridge Is Falling Down
- `3`: Twinkle, Twinkle, Little Star
- `4`: White Noise
- `5`: Heartbeat
- `6`: Rainforest
- `7`: Flowing Stream

Play a track:
```bash
# Play White Noise at 50% volume
momcozy lullaby play --track 4 --volume 50

# Stop playback
momcozy lullaby stop
```

### 6. Video Capture & Snapshots

```bash
# Capture a still JPEG snapshot
momcozy snapshot -o baby_snapshot.jpg

# Record a 15-second MP4 video clip
momcozy record --duration 15 -o nursery_clip.mp4
```

### 7. Launch Web Dashboard & Streaming Server

Start the local web server hosting the live MJPEG stream and web interface:

```bash
momcozy serve --host 0.0.0.0 --port 8421
```

Now open `http://localhost:8421` in any browser:
- **Web Dashboard:** `http://localhost:8421/`
- **Raw MJPEG Video Stream:** `http://localhost:8421/stream`
- **Snapshot Endpoint:** `http://localhost:8421/snapshot`
- **JSON Telemetry API:** `http://localhost:8421/api/status`
- **Control API:** `POST http://localhost:8421/api/control`
- **PTZ API:** `POST http://localhost:8421/api/ptz`

### 8. Device Sharing

Share your baby monitor access with other Momcozy/Tuya accounts:

```bash
# Share device access with a partner/family member by email
./momcozy.py share add partner@example.com

# Search for a user's member code on Tuya/Momcozy
./momcozy.py share search partner@example.com

# Query email details for member codes
./momcozy.py share query MEM12345

# Revoke device access for a member code
./momcozy.py share remove MEM12345
```

---

## 🐍 Python Library Usage

You can use `momcozy` directly in your Python applications and automations.

### Quick Example: Reading Status & Controlling Settings

```python
from momcozy import MomcozyMonitor, NightVisionMode, PtzDirection

# Initialize monitor using local IP or Tuya local key
monitor = MomcozyMonitor(
    device_id="your_device_id",
    local_key="your_local_key",
    ip="192.168.1.50"  # Optional: local LAN IP for direct communication
)

# 1. Fetch current telemetry
status = monitor.get_status()
print(f"Room Temperature: {status.temperature_c}°C / {status.temperature_f}°F")
print(f"Cry Detection: {status.cry_detection_switch}")

# 2. Control settings
monitor.set_night_vision(NightVisionMode.AUTO)
monitor.set_led(False)  # Turn off front LED
monitor.set_privacy_mode(False)

# 3. PTZ Camera Movement
monitor.ptz_step(PtzDirection.UP, step_duration=0.5)

# 4. Play Lullaby / White Noise
monitor.play_lullaby(track_id="4", volume=60)  # Track 4: White Noise
```

### Streaming Frames Asynchronously

```python
import asyncio
from momcozy import VideoStreamer

async def stream_frames():
    streamer = VideoStreamer(
        device_id="your_device_id",
        local_key="your_local_key",
        email="your_account@example.com",
        password="your_password",
    )
    
    await streamer.start()
    try:
        async for jpeg_bytes in streamer.subscribe():
            # Process raw JPEG bytes (e.g. forward to RTSP, save, OpenCV)
            print(f"Received frame of size {len(jpeg_bytes)} bytes")
    finally:
        await streamer.stop()

asyncio.run(stream_frames())
```

---

## 🏠 Home Assistant Integration

You can easily integrate your Momcozy BM04 with Home Assistant without any custom component!

### 1. MJPEG Camera Entity
Run `momcozy serve --port 8421` (e.g., in a systemd service, Docker, or sidecar container), then add to `configuration.yaml`:

```yaml
camera:
  - platform: mjpeg
    name: "Baby Monitor"
    mjpeg_url: http://192.168.1.100:8421/stream
    still_image_url: http://192.168.1.100:8421/snapshot
```

### 2. REST Temperature & Status Sensor
```yaml
sensor:
  - platform: rest
    name: "Nursery Temperature"
    resource: http://192.168.1.100:8421/api/status
    value_template: "{{ value_json.temperature_c }}"
    unit_of_measurement: "°C"
    device_class: temperature

binary_sensor:
  - platform: rest
    name: "Baby Crying"
    resource: http://192.168.1.100:8421/api/status
    value_template: "{{ value_json.cry_detection_switch }}"
```

---

## 🧪 Running Tests

The test suite validates password hashing, constants, DP mapping, conversions, and CLI argument parsing:

```bash
python3 -m unittest discover tests
```

---

## 📄 License

MIT License. Designed for interoperability and personal home automation.
Not affiliated with or endorsed by Momcozy / Lute Technology.
