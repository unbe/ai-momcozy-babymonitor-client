# Architecture and System Memory

## Overview
Momcozy client and streaming server for Momcozy BM04/BM04S/BM05 baby monitors.
Provides device control (PTZ, sensors, night vision, lullabies, privacy mode), live video streaming (hardware HEVC pass-through, HLS, MJPEG fallback), live audio streaming, and on-demand snapshots.

## Key Architectural Decisions & Protocols

### 1. Video Streaming Pipeline
* **Native Camera Stream:** Momcozy cameras output 1080p raw HEVC (H.265) video packets over Tuya P2P UDP sessions with Annex B start codes (`00 00 00 01`).
* **WebCodecs Pass-Through (`/ws/stream`):**
  * Direct low-latency hardware decoding inside modern browsers (Chrome, Safari, Edge) via the WebCodecs API (`VideoDecoder`).
  * Emits raw HEVC Access Units prefixed with a 1-byte binary flag (`\x01` for keyframe, `\x00` for delta frame).
  * Consumes near-zero server CPU because no video transcoding is performed.
* **MJPEG Fallback (`/stream`):**
  * Transcodes HEVC into MJPEG using PyAV/OpenCV only when WebCodecs/HEVC is genuinely unsupported or repeatedly fails fatal decoding.
* **HLS Pipeline (`/hls/stream.m3u8`):**
  * Direct HEVC remuxing/transcoding via FFmpeg (supports Intel VA-API hardware acceleration where available, software fallback otherwise).

### 2. HEVC Parameter Sets (VPS, SPS, PPS) & Keyframe Handling
* **Edge Case / Root Cause:** WebCodecs `VideoDecoder` strictly requires Annex B in-band VPS (NAL 32), SPS (NAL 33), and PPS (NAL 34) parameter sets prepended to the first keyframe (IDR slice, NAL 19/20). If fed an IDR slice without SPS/PPS, the browser hardware decoder fails immediately with a decoding error.
* **Enforcement:**
  * `MomcozyHevcHandler._build_au()` guarantees that an Access Unit is marked `is_key = True` only if SPS and PPS are present.
  * Incomplete IDR slices arriving before parameter sets are treated as non-keyframes (`is_key = False`) to prevent sending incomplete frames to clients.
  * `VideoStreamer` caches VPS, SPS, and PPS across P2P sessions so subsequent viewers and reconnections immediately receive complete parameter sets.

### 3. Startup & Waiting vs. Fallback Logic
* **WebCodecs Secure Context Requirement (Chrome):**
  * In Chromium/Chrome, the `VideoDecoder` API is strictly restricted to **Secure Contexts** (`window.isSecureContext === true`).
  * If accessed via `http://0.0.0.0:8421` or a LAN IP over HTTP, Chrome strips `VideoDecoder` (`'VideoDecoder' in window` is `false`), causing immediate fallback to MJPEG.
  * Accessing via `http://localhost:8421`, `http://127.0.0.1:8421`, or `https://` enables the full WebCodecs API. The frontend automatically normalizes `0.0.0.0` to `localhost`.
* **Camera HEVC Profile & Level:**
  * Momcozy BM04/BM05 cameras emit 1080p HEVC Main Profile Level 5.0 (`level_idc=150`).
  * WebCodecs capability probing must include `hev1.1.6.L150.B0` and `hvc1.1.6.L150.B0` (lower level strings like `L93` or `L120` alone can fail configuration).
* **Waiting Instead of Premature Fallback:**
  * Initial camera P2P connection from cold start can take 15–30 seconds.
  * The frontend client must wait in `CONNECTING...` status and continue reconnecting rather than timing out and falling back to MJPEG.
  * `fallbackToMjpeg` is only called if:
    1. The browser does not support WebCodecs / HEVC (`checkHevcWebCodecsSupport()` returns null).
    2. Decoder configuration fails (`activeDecoder.configure()` throws).
    3. Persistent decoder failure occurs (3 consecutive decoder crashes without successful frame rendering).
* **Transient Error Recovery:**
  * Up to 3 decoder errors or decode exceptions are recovered automatically by re-initializing the `VideoDecoder` and waiting for the next clean keyframe.
  * Successful frame rendering resets all retry and error counters.

### 4. Snapshots & Rolling GOP Buffer
* Live snapshots burst-decode the in-memory rolling GOP (Group of Pictures) buffer using PyAV/OpenCV without interrupting or blocking pass-through HEVC consumers.

### 5. Device Discovery & Dynamic IP Handling
* **Local LAN Control:** Momcozy/Tuya Protocol 3.3 communicates directly with the camera over local TCP port 6668 for instant PTZ commands and sensor telemetry.
* **Auto-Rediscovery on IP Change (DHCP):**
  * When a camera's IP changes (e.g. DHCP lease renewal), connection attempts to the stale IP fail with `ConnectionRefusedError` or timeout.
  * `test_local_lan_connection(allow_rediscovery=True)` detects this failure and automatically triggers an in-band UDP broadcast scan using `tinytuya.deviceScan(False, 3)` matching the camera's `device_id` / `gwId`.
  * When rediscovered at a new IP, `MomcozyMonitor` updates its address, fires `on_ip_discovered(new_ip)` to persist the new IP to `.momcozy.json`, and reconnects via Local LAN.

### 6. 12-Hour Circular Disk-Backed Timelapse Buffer
* **Rate & Duration:** 1:60 speedup ratio compresses 12 hours of real time into 12 minutes (720 seconds) of video.
* **Framerate & Sampling:** At 30 fps playback, 1 frame is sampled every 2.0 seconds of real time.
* **Direct Overwrite Circular Ring Buffer (Zero Periodic Scavengers):**
  * 72 static slot files on disk (`slot_00.hevc` .. `slot_71.hevc`), each representing 10 minutes of real time (300 keyframes, 10 seconds of video).
  * As the buffer advances (`current_slot = (current_slot + 1) % 72`), the incoming slot is opened in `"wb"` mode, immediately truncating and overwriting the 12-hour-old file on disk.
  * Total disk usage is strictly hard-capped (~700 MB - 1 GB), eliminating background cron jobs or periodic file deletion scans.
* **Zero Transcoding During Recording:**
  * Directly writes Annex B HEVC keyframes (VPS/SPS/PPS + IDR slices) from `VideoStreamer.subscribe_hevc()`.
  * Non-keyframes are dropped without decoding; server CPU consumption during recording is literally 0.0%.
* **Stream-Copy Browser Playback:**
  * `export_mp4()` concatenates chronologically ordered slots using FFmpeg's concat demuxer in stream-copy mode (`-c:v copy -tag:v hvc1 -movflags +faststart`).
  * Instantaneous packaging (< 1 second) with zero transcoding, cached for instant repeat requests.
  * Served via `GET /api/timelapse/video.mp4` with HTTP Range support (`Accept-Ranges: bytes`) enabling seamless timeline scrubbing in HTML5 `<video>` controls with native 1x/2x/4x speed adjustment.
  * Provides optional on-demand H.264 fallback (`?codec=h264`) for older browsers.


