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

