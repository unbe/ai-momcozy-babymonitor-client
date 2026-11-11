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
  * **Tuya P2P SDK Watchdog Stall Prevention:** Native HEVC pass-through disables JPEG transcoding (`transcoding: False`). The Tuya IPC SDK watchdog (`_async_watch_for_stalls`) checks `time.monotonic() - self._last_frame_at >= 60s`, but the SDK only updates `_last_frame_at` when `_on_frame(jpeg_bytes)` is called. In `momcozy/stream.py`, `momcozy_on_hevc` updates `self._last_frame_at = time.monotonic()` for every arriving HEVC chunk, preventing the SDK from erroneously tearing down and restarting healthy sessions every 60 seconds.
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

### 4. Snapshots & Single Keyframe Extraction
* **Single-Flight Snapshot Coalescing (`VideoStreamer.get_snapshot`):** Live snapshots burst-decode the in-memory rolling GOP (Group of Pictures) buffer using PyAV/OpenCV without interrupting pass-through HEVC consumers. Concurrent `get_snapshot()` callers share a single in-flight `_capture_fresh_snapshot` task (`asyncio.shield`), preventing duplicate simultaneous PyAV GOP decodes.
* **Bounded Streaming MP3 Sink (`_ChunkSink`):** `StreamingMp3Encoder` writes muxed MP3 packets into a bounded `bytearray`-backed `_ChunkSink` drained on every `encode()` call rather than an append-only `io.BytesIO`, ensuring $O(1)$ memory across multi-hour audio sessions.
* **Idempotent Tuya P2P SDK Patching:** `_patch_tuya_stream_session()` guards `StreamSession` method wrapping behind `_momcozy_patched` so repeated `create_p2p_client()` calls never stack recursive wrappers, and `_configure_tuya_p2p_sdk()` preserves `sdk_crypto.hmac_sha1 = fast_hmac_sha1`.
* Single-keyframe extraction (`get_keyframe()`) resolves the next arriving or cached HEVC keyframe Access Unit with SPS/PPS prepended. This allows fast direct analysis (e.g., night vision thumbnail decoding) without holding or decoding the entire GOP.

### 5. Lease-Based Stream Lifecycle & Monotonic Deadlines
* **Problem:** Previously, stream lifetime relied on manual, ad-hoc cancellations and reschedulings of `_schedule_idle_stop` across multiple callers (WebSocket viewer connect/disconnect, timelapse start/stop, periodic night vision checks, snapshot requests). This led to race conditions where short-lived checks could either prematurely kill viewer lingering or postpone viewer disconnect deadlines.
* **Lease Architecture (`VideoStreamer`):**
  * Multiple components hold concurrent leases (`acquire(linger=...)` and `release(lease, linger=...)` or `with streamer.lease(...)`) — including `HlsTranscoder`, `TimelapseRecorder`, WebSocket/MJPEG/audio subscribers, and snapshot checks.
  * While active leases exist, the camera P2P connection stays alive.
  * When the last lease is released, `VideoStreamer` sets an idle shutdown deadline:
    $$\text{deadline} = \max(\text{current\_deadline}, \text{now} + \text{linger})$$
    This monotonic deadline guarantee ensures that a zero-linger release (such as an automatic 5-minute night vision check) never truncates an existing 300-second grace period from a disconnected user session.
  * Re-acquiring a lease cancels any scheduled idle shutdown timer immediately.
* **Keyframe-Aware Queue Resynchronization (`_hevc_resync` & `HlsTranscoder._feed_frames_task`):**
  * When a consumer's queue overflows under backpressure (both in `VideoStreamer` WebSocket queues and `HlsTranscoder`'s FFmpeg feed queue), stale queued chunks are dropped and subsequent delta frames are skipped until the next valid keyframe (IDR slice with SPS/PPS) arrives, preventing decoder corruption in downstream clients.

### 6. Device Discovery, Non-Blocking Startup & Dynamic IP Handling
* **Local LAN Control:** Momcozy/Tuya Protocol 3.3 communicates directly with the camera over local TCP port 6668 for instant PTZ commands and sensor telemetry.
* **Deferred LAN Probe on `serve` Startup (`probe_lan=False`):**
  * During `momcozy.py serve`, `MomcozyMonitor` initializes with `probe_lan=False` and `WebServer.run()` binds HTTP/HTTPS TCP ports *before* launching `VideoStreamer` and a background `asyncio.to_thread(monitor.test_local_lan_connection)` task. This guarantees `/health` and dashboard pages respond immediately even when the camera IP is offline or being rediscovered via UDP broadcast.
  * `WebServer.get_local_lan_ip()` caches the outbound interface IP with a 60-second TTL instead of opening a UDP socket on every `/api/status` request.
* **Auto-Rediscovery on IP Change (DHCP):**
  * When a camera's IP changes (e.g. DHCP lease renewal), connection attempts to the stale IP fail with `ConnectionRefusedError` or timeout.
  * `test_local_lan_connection(allow_rediscovery=True)` detects this failure and automatically triggers an in-band UDP broadcast scan using `tinytuya.deviceScan(False, 3)` matching the camera's `device_id` / `gwId`.
  * When rediscovered at a new IP, `MomcozyMonitor` updates its address, fires `on_ip_discovered(new_ip)` to persist the new IP via `ConfigStore`, and reconnects via Local LAN.

### 7. 12-Hour Circular Disk-Backed Timelapse Buffer
* **Selectable Speed Ratios:** Supports selectable speed ratios: `1:15`, `1:30`, `1:60` (default), and `1:90`.
  * `1:60`: 12 hours of real time in 12 minutes (720 seconds) of video.
  * `1:30`: 12 hours in 24 minutes.
  * `1:15`: 12 hours in 48 minutes.
  * `1:90`: 12 hours in 8 minutes.
  * Dynamically configures the video export playback framerate (`export_fps = round(total_frames / target_video_sec, 2)`) during stream-copy packaging (`-r <fps> -c:v copy`), ensuring exact playback duration without transcoding.
* **Real-Time Duration & Cadence Tracking:**
  * Real time elapsed is tracked via monotonic timestamps per slot (`time.monotonic() - slot_start_monotonic`) rather than multiplying frame count by nominal sampling interval, ensuring accurate 1:1 real-time second counting regardless of camera GOP length.
* **Direct Overwrite Circular Ring Buffer (Zero Periodic Scavengers):**
  * 72 static slot files on disk (`slot_00.hevc` .. `slot_71.hevc`), each representing 10 minutes of real time.
  * As the buffer advances (`current_slot = (current_slot + 1) % 72`), the incoming slot is opened in `"wb"` mode, immediately truncating and overwriting the 12-hour-old file on disk.
  * Total disk usage is strictly hard-capped (~700 MB - 1 GB), eliminating background cron jobs or periodic file deletion scans.
  * **Active Recording Slot Chronological Ordering & Timeline Protection:**
    * When the circular buffer wraps around (e.g. 72/72 filled slots), slots from the previous cycle retain 12-hour-old metadata in `meta.json` until overwritten.
    * Previously, when rotating into an old slot, `_rotate_slot()` did not clear `_slots_meta[str(slot)]`, leaving the 12-hour-old `mtime` in memory. In `get_ordered_slots()`, sorting strictly by `meta.get("mtime")` caused the newly active recording slot (`current_slot`) to sort to index 0 (as the "oldest" slot). In `get_status()`, slot 0 was assigned `end_t = now`, setting `earliest_slot_start = now - current_slot_dur`. This caused all motion events from the preceding 12 hours to be filtered out as "earlier than earliest slot", returning `"events": []` while `"motion_events_count" > 0`.
    * **Fix:** `get_ordered_slots()` evaluates `i == self._current_slot and self.is_recording` as `mtime = time.time()`, placing the active slot strictly at the end of chronological order. `_rotate_slot()` immediately resets `_slots_meta[str(self._current_slot)]` with `mtime = now, duration = 0, frames = 0, bytes = 0` upon rotation. `detect_motion_events()` calculates the active slot's duration and `slot_end = time.time()` dynamically. Furthermore, `get_status()` computes `earliest_slot_start = min(s[0] for s in slot_spans)` and `latest_slot_end = max(s[1] for s in slot_spans)` across all spans for guaranteed monotonic bounds.
* **Zero Transcoding During Recording:**
  * Directly writes Annex B HEVC keyframes (VPS/SPS/PPS + IDR slices) from `VideoStreamer.subscribe_hevc()`.
  * Non-keyframes are dropped without decoding; server CPU consumption during recording is literally 0.0%.
* **Standalone Browser Playback (`/timelapse`):**
  * The dashboard "▶ Watch" button opens `/timelapse` in a dedicated new browser window.
  * Automatically detects native HEVC support using `canPlayType` with `hvc1.1.6.L150.B0`. If supported, plays native HEVC with 0% CPU; silently falls back to `codec=h264` on decoding errors. No codec dropdowns are exposed to users.
  * Stream-copy packaging via FFmpeg (`-c:v copy -tag:v hvc1 -movflags +faststart`) is instantaneous (< 1s) and cached for instant scrub-seeking with HTTP Range support (`Accept-Ranges: bytes`).
* **`SafeFileResponse` & `AIOHTTP_NOSENDFILE` (Asyncio SSL Protocol Teardown Fix):**
  * When streaming video files via `web.FileResponse` over HTTPS, client scrub/seek actions or window closures trigger `ConnectionResetError: [Errno 104] Connection reset by peer`.
  * Python standard library `asyncio` (`base_events.py` `_sendfile_fallback`) wraps SSL transports with `_SendfileProtocol`. Upon connection reset, `_SendfileProtocol.restore()` attempts `self._transport.set_protocol(self._proto)`. On Python 3.12/3.13, `_SSLProtocolTransport`'s internal `_ssl_protocol` is already `None`, crashing with `AttributeError: 'NoneType' object has no attribute '_set_app_protocol'`. This masked `ConnectionError` and logged unhandled server exceptions.
  * Resolved by forcing `AIOHTTP_NOSENDFILE=1` and utilizing `SafeFileResponse` in `handle_timelapse_video`. This streams file chunks directly via aiohttp's writer without invoking `loop.sendfile()`, allowing `aiohttp.web_protocol` to cleanly catch `ConnectionError` as an expected premature client disconnect.
* **Timelapse Export Performance, Caching & Hardware Acceleration (`momcozy/hwaccel.py`):**
  * **Headless VA-API Direct Rendering (DRM):** In headless Linux/Docker environments, libva outputs cosmetic warnings (`error: can't connect to X server!`) before automatically falling back to direct DRM render nodes (`/dev/dri/renderD128`). There is **no requirement for an X server** to use DRI / VA-API. Centralized `detect_vaapi_device()` (`momcozy/hwaccel.py`) auto-detects `/dev/dri/renderD128` without forcing `LIBVA_DRIVER_NAME=i965`, allowing libva to select the native driver (`iHD` on Intel Gen 8+, `i965` on Haswell, `radeonsi` on AMD) while honoring any explicit `LIBVA_DRIVER_NAME` set in the environment.
  * **Download Intent & Instant Stream-Copy:** When saving/downloading timelapses (`download=1` or UI download buttons), the default is always native stream-copy HEVC (`codec=hevc`). This remuxes 12 hours of video in < 0.5s at 0% CPU with pristine original 1080p quality, natively supported by all major OS video players (QuickTime, VLC, Windows 10/11 Films & TV, Android, iOS). A dedicated "⬇ Download H.264" option is also provided for legacy players.
  * **In-Flight Export Deduplication (Thundering Herd Prevention):** Multiple concurrent requests or HTTP retries (e.g. `wget` reconnecting on timeout) for the same `(codec, ratio)` export await a shared running `asyncio.Task` (`_in_flight_exports`), eliminating duplicate concurrent FFmpeg processes from pegging host CPU/GPU cores.
  * **Cache TTL Window During Active Recording:** During active recording, slot files are appended every 2 seconds. A sliding cache TTL (20s for HEVC, 60s for H.264) avoids invalidating and regenerating exports on every keyframe, providing instant (< 5ms) responses for scrubbing, page reloads, and downloads.
  * **Hardware-Accelerated H.264 Encoding:** `TimelapseRecorder._generate_mp4` auto-detects `/dev/dri/renderD128` and runs `-c:v h264_vaapi -vf format=nv12,hwupload` on the GPU, with a clean multithreaded fallback to `libx264 -preset ultrafast -threads 0`.
* **Motion Ranges on Recording Timeline (0% CPU Hardware Integration & Merged Intervals):**
  * **Motion Ranges Model (`[time, end_time]`):** Instead of isolated point timestamps that fragment sustained movement into repetitive markers, motion events are stored and projected as continuous intervals `{"time": start_ts, "end_time": end_ts, "type": ..., "label": ...}` (`time` retains the start timestamp for backward compatibility).
  * **Nearby Range Merging (`merge_motion_ranges`, `DEFAULT_MOTION_MERGE_GAP_SEC = 30.0`):** Overlapping or nearby motion intervals of the same type separated by $\le 30.0$ seconds (`next.time - cur.end_time <= 30.0`, corresponding to $\le 0.5\text{s}$ of video at `1:60`) are merged into a single cohesive range. When generic scan/alert labels (`"Motion Detected"`, `"Motion Alert"`) merge with specific hardware labels (e.g. `"Human Detected"`, `"Baby Motion"`), the specific label and non-scan provenance are preserved.
  * **Zero Recording Overhead:** No video frames are analyzed or decoded during recording. `MomcozyMonitor`'s native camera hardware detection alerts (DP 185 `ipc_motion`, `ipc_human`, `ipc_crying` and DP 115/127 alarm statuses) arriving over the local network socket extend active ranges in `meta.json` without redundant disk writes when unchanged. Retention pruning (`_retention_cutoff`) respects the earliest slot still present on disk so multi-night buffers separated by daytime pauses never lose earlier nights' motion ranges.
  * **Timeline Coordinate Projection:** Both `time` (`start_time`) and `end_time` are projected onto cumulative slot-aware elapsed time (`elapsed_sec`..`end_elapsed_sec`), video timestamps (`video_sec`..`end_video_sec`), and scrubber percentages (`percent`..`end_percent`).
  * **Interactive Range Scrubber & Navigation:** In `/timelapse`, motion ranges render as glowing amber bands spanning `percent` to `end_percent` (`min-width: 6px`, playhead `.timeline-thumb` layered above at `z-index: 20`). Hovering displays the label, wall-clock range, real-time duration, and video span. Clicking a narrow range pill (`<= 20px`) or the onset edge (`<= 12px`) snaps to the range start, while clicking or dragging inside a wide range scrubs smoothly to the cursor position. Next (`N`) and Previous (`P`) motion buttons jump cleanly between range onsets.
* **Server-Side Night Vision Timelapse Automation (0% Client Overhead):**
  * **Completely Headless & Server-Side:** The automation runs entirely inside `WebServer._background_monitor_loop()` on the host/container. It operates autonomously without requiring any browser window, tab, or client socket to remain open.
  * **Decoupled State Machine Architecture (`momcozy/nightvision.py`):**
    * The debounce timing, suppression, and activation/deactivation logic is implemented as a pure, testable state machine `NightVisionAutomation`.
    * It takes inputs `(now, is_dark, recording, session_auto)` and returns actions (`Action.NONE`, `Action.START_AUTO`, `Action.STOP_AUTO`, `Action.MARK_AUTO`). It has zero I/O or network dependencies, making testing instantaneous and deterministic without mocks.
  * **Direct HEVC Keyframe Analysis & Planar Stride Safety (`analyze_hevc_keyframe`, `extract_hevc_keyframe_luma`):**
    * Decodes only the single incoming IDR HEVC keyframe using single-threaded decode (`thread_count=1`), avoiding worker thread pool allocation and teardown per check.
    * Inspects the decoded YUV420p chroma planes $U$ (plane 1) and $V$ (plane 2) directly ($|U-128| + |V-128|$). In infrared monochrome, chroma values are neutral 128 (0.0 saturation). Direct chroma plane inspection eliminates `libswscale` software rescaling, RGB24 format conversions, and buffer allocations.
    * **Planar Row-Stride (`plane.line_size`) Requirement:** PyAV/FFmpeg aligns planar YUV row buffers to SIMD boundaries (32/64 bytes), so `plane.line_size >= plane_width`. Both `analyze_hevc_keyframe` (`momcozy/nightvision.py`) and `extract_hevc_keyframe_luma` (`momcozy/timelapse.py`) MUST index pixels using `py * plane.line_size + px` (and clamp sample coordinates within `[0, plane_w - 1]` / `[0, plane_h - 1]`) rather than `py * width + px` to avoid reading skewed rows or padding bytes.
    * Fallback functions (`analyze_night_vision_frame`, `detect_night_vision_from_jpeg`) remain available for JPEG inputs and backward compatibility.
  * **Keyframe-Only Subscription Filtering & Headless Delta-Frame Discard:**
    * In `VideoStreamer._broadcast_hevc`, delta frames are filtered at broadcast time and never queued to keyframe listeners.
    * **Headless Delta-Frame Discard (`is_full_rate_needed`):** When only keyframe listeners (such as `TimelapseRecorder` or background night vision checks) are active and no live browser viewers (`_hevc_listeners` / `_listeners`) or snapshots are requested, `MomcozyHevcHandler` drops non-intra delta fragments (`ftype < 16` and non-IDR slices) immediately at the NAL fragment stage. This avoids assembling ~19 delta frames/sec into bytearrays, constructing Access Units, and pushing them into asyncio queues. When a viewer connects (e.g., via WebSocket `/ws/stream`), `is_full_rate_needed()` dynamically returns `True`, and full 20 fps frame assembly resumes instantaneously.
  * **Tuya P2P Transport & Crypto Fast Paths:**
    * **Precomputed HMAC-SHA1 Padding Cache (`fast_hmac_sha1`):** `tuya_ipc_p2p_sdk` originally called `hmac.new(key, msg, hashlib.sha1)` on every packet and ACK. Because the session `media_key` is static, `fast_hmac_sha1` precomputes the inner/outer SHA1 padding hashes (`key ^ 0x36`, `key ^ 0x5C`) once per key, using `.copy().update(...)`. This eliminates Python `HMAC` class allocations on 100–200 packets/sec and speeds up HMAC calculation by ~1.6x.
    * **Fast KCP Segment & Framing (`fast_parse_segment`, `fast_build_segment`, `fast_unwrap_media_frame`):** Replaces iterative `int.from_bytes()` and `int.to_bytes()` with pre-compiled `struct.Struct("<IBBHIIII")` packing and unpacking (3.4x faster ACK packing).
  * **Adaptive Check Cadence:**
    * Night vision sampling operates adaptively:
      * **60s:** During active timelapse recording in stable darkness, checks occur once every 60 seconds (cutting nighttime CPU decode overhead by 75%).
      * **15s:** When daylight is detected while recording, checks accelerate to every 15 seconds during the 120s deactivation countdown for prompt auto-stop.
      * **30s:** While a client viewer is watching the live stream.
      * **300s:** While the stream is idle (stopped).
  * **Idle Stream Disconnect & 5-Minute Periodic Detection Sampling:**
    * To conserve camera battery/heat, bandwidth, and CPU, the camera P2P stream is **NOT kept running 24/7** when idle.
    * When timelapse is not recording and no client viewers are connected, the stream disconnects.
    * When `sync_night_vision` is enabled and the stream is stopped, `WebServer._background_monitor_loop` wakes up every 5 minutes (300s), acquires a zero-linger lease (`async with streamer.lease(linger=0)`), fetches a keyframe via `get_keyframe()`, and evaluates night vision status.
    * If daylight is detected, the lease exits and the stream disconnects immediately without lingering, remaining stopped until the next 5-minute check.
    * If the stream was started by a viewer (or manual start), user disconnection triggers the standard 300s idle timer without being cut short by background night vision checks.
    * As soon as night vision is detected, timelapse recording auto-starts immediately (no activation delay).
    * While recording is active, the timelapse recorder holds an active stream lease (`streamer.acquire("timelapse")`), keeping the stream running.
    * When daylight persists for 120s, timelapse stops, releases its lease, and if no client viewers are connected, the stream disconnects and returns to 5-minute periodic checks.
  * **Idle Monitor Loop Backoff:**
    * In `WebServer._monitor_tick_delay()`, the background loop distinguishes between active recording and idle states.
    * While recording, it polls every 4.0s to log hardware motion events (DPs 115, 127, 185) on the video timeline in real-time.
    * When idle (stream stopped, timelapse not recording), it sleeps in 30.0s blocks (or until the 300s sample is due), reducing idle loop wakeups and telemetry dispatches from 75 down to 10 per 5 minutes. On-demand API requests (`/api/status`) continue to query the device directly, preserving instant UI responsiveness.
  * **Logging Conventions:**
    * Log messages must use clean, readable plain text without emojis or non-standard Unicode symbols.
  * **Auto Mode ("0") Night Vision Physical Detection via Frame Saturation:**
    * On Tuya IPC cameras, `basic_nightvision` (DP 108) represents the configured mode (`"0"` = Auto, `"1"` = Off, `"2"` = On). In Auto mode (`"0"`), the camera hardware physically flips the IR-cut filter and activates the IR LEDs locally in darkness, but **DP 108 remains `"0"`** in telemetry.
    * `detect_night_vision_from_jpeg()` inspects a 16x16 subsampled thumbnail of the camera frame (< 1.5ms computation). Daytime frames show strong color saturation ($|R-G| + |G-B| + |B-R| \gg 10.0$), whereas infrared night vision frames are monochrome grayscale where $R \approx G \approx B$ (mean diff $< 8.0$).
    * Timelapse automation operates **exclusively in Auto mode (`"0"`)**, ignoring manually set states (`"1"` or `"2"`). In Auto mode, `is_night_vision_active()` checks frame saturation to accurately detect active IR night vision kicking in or out.
  * **Debounce Timing & Session Protection Logic:**
    * **Immediate Activation (0s Delay):** When camera reports Night Vision active, timelapse recording auto-starts/resumes immediately without any countdown delay.
    * **Deactivation Threshold (2 Minutes = 120s):** When camera reports daylight / Night Vision inactive, an inactive monotonic timer starts. If daylight persists continuously for $\ge 120.0$ seconds and timelapse is recording, it stops *only if* the recording was auto-started or had night vision active. Manual daytime recordings are never killed by daylight.
    * **Manual Stop Honor:** If the user manually stops recording during the night, auto-restart is suppressed for the remainder of that dark period until daylight resets it or the user clicks resume.
  * **Manual Recording Resume vs. Erase:**
    * `handle_timelapse_start` and `TimelapseRecorder.start()` default to `fresh=False`. When recording is stopped manually, previous slots are preserved. Starting again resumes and appends to the circular buffer.
    * In the web UI, stopped recordings with existing footage display `[▶ Resume Recording]`.
    * An explicit `[🗑️ Erase]` button (calling `POST /api/timelapse/clear`) allows users to wipe the recording buffer and reset counters to 0m 00s on demand.
  * **Night Vision Sync Setting Persistence Across Server Restarts:**
    * Night Vision sync preference (`sync_night_vision`) is persisted strictly in the user configuration file `.momcozy.json` (`timelapse_sync_night_vision`), not in `timelapse/meta.json`.
    * On server startup, `TimelapseRecorder` checks for persisted preferences from (1) environment variable `MOMCOZY_TIMELAPSE_SYNC_NV`, (2) `.momcozy.json` (or CLI `--sync-night-vision`).
    * **Thread-Safe In-Place Config Persistence (`ConfigStore`, `momcozy/config.py`):** All reads and mutations of `.momcozy.json` go through `ConfigStore.read()`, `ConfigStore.mutate()`, and `ConfigStore.update()`, guarded by per-resolved-path `threading.RLock` locks. Updates merge into existing JSON keys (never clobbering `local_key`, `camera_name`, `mac`, `ip`, `dps`, or `tuya_session`) and write directly in-place via `cfg_path.write_text()`. In Docker deployments (`./.momcozy.json:/config/.momcozy.json`), atomic rename (`os.replace`) fails with `[Errno 16] EBUSY` or breaks single-file bind-mount inodes; in-place locked writes preserve the inode safely across concurrent threads. Similarly, `TimelapseRecorder` guards `_slots_meta` and `_events` with a `threading.RLock` and writes `meta.json` via unique `tempfile.NamedTemporaryFile` staging before `os.replace`.
    * Decoupling user configuration from slot buffer metadata (`meta.json`) ensures single source of truth: clearing footage (`[🗑️ Erase]`), slot rotations, or restoring slot files from an external backup never modifies or reverts the user's Night Vision automation preference.
  * **Session Continuity (Append vs. Restart):**
    * When auto-start triggers:
      * If `last_session_type == "auto"` (the previous timelapse was automatic), it calls `timelapse.start(fresh=False, session_type="auto")`. This appends the new night session directly into the circular buffer without clearing previous slots.
      * If `last_session_type == "manual"` (previous recording was manually started), it starts fresh (`fresh=True`), wiping manual records to begin the auto session.
    * When auto-stop triggers, it marks `last_session_type = "auto"`.
  * **Zero Recording Overhead Preserved:** Video recording continues to write pure Annex B HEVC keyframes via stream-copy directly from `VideoStreamer.subscribe_hevc()` with 0% CPU consumption.
* **Docker Volume Persistence & Configuration Architecture:**
  * Persistent configuration is mapped via `/config/.momcozy.json` (`MOMCOZY_CONFIG=/config/.momcozy.json`).
  * Timelapse recordings and ring buffer data are stored in `/timelapse`, exposed as a Docker `VOLUME ["/timelapse"]` with `MOMCOZY_TIMELAPSE_DIR=/timelapse`.
  * For seamless backward compatibility, `/app/timelapse` is symlinked to `/timelapse` in the container.
  * In `docker-compose.yml`, `./timelapse:/timelapse` is mounted alongside `./.momcozy.json:/config/.momcozy.json`.
  * `.dockerignore` excludes `timelapse/` and `*.hevc` so local video recordings are never included in Docker build contexts.
* **Dev Server Port & SSL:**
  * Port 8421 runs HTTPS (`momcozy.py serve --ssl`). Healthchecks and WebCodecs APIs require HTTPS on this port. Plain HTTP for LAN/Chromecast is hosted on port 8420.
* **Docker Health Check & Local Telemetry Endpoints:**
  * Dedicated `/health` endpoint returns `200 OK` with JSON (`{"status": "ok", ...}`) reading strictly local in-memory server state.
  * Unlike `/api/status` (which queries Tuya Cloud when camera DP status is needed), `/health` never issues network requests to Tuya Cloud or camera hardware, ensuring Docker container healthchecks run with zero external network activity.
  * **No Aliases Rule:** Never create alias routes or multiple endpoints for the same purpose (e.g. choose `/health`, do not add `/api/health`). One canonical endpoint is sufficient.
* **Active Timelapse UI Representation:**
  * `#timelapse-ctrl-btn` was removed from the video player controls to prevent duplicate controls (the dedicated `[▶ Watch]` button on the Timelapse card handles playback).
  * On the dashboard, active timelapse is unified between the timelapse card header badge (`Recording 🌙`) and the night vision sync button (`Sync: Recording 🌙`), both sharing the consistent red recording status palette (`#FED7D7` background, `#C53030` text).
* **Timelapse HEVC Playback & Scrub Seek Stability:**
  * **Session-Aware MP4 Caching (`session_id` / `t` query param) & Mid-Stream Invalidation Prevention:**
    * During active recording, slot files are continuously appended and the cache TTL (20s) periodically expires.
    * **Root Cause of Mid-Stream Demuxer Crashes:** When a user scrubbed near the beginning or the player reloaded, Chrome sent `Range: bytes=0-`. Previously, `is_midstream_seek` only treated `start_spec > 0` as cache-safe. `bytes=0-` triggered `allow_cache_only=False`, causing FFmpeg to overwrite the cached MP4 file on disk with new frames/duration (e.g. jumping from 300MB to 314MB). Subsequent seek requests for higher byte ranges hit the newly overwritten file with mismatched byte offsets and duration, immediately crashing Chrome's demuxer with `MEDIA_ERR_DECODE` (code 3).
    * **Fix:** The frontend supplies a unique session timestamp `t=${Date.now()}` on `video.src`. `handle_timelapse_video` extracts `session_id = request.query.get("t")` and passes it to `export_mp4()`. If `cache_entry.get("session_id") == session_id`, the cached export is returned directly without regenerating mid-stream, guaranteeing an immutable, stable file on disk for the lifetime of that viewing session.
  * **Fatal Decode / Format Error Handling vs Transient Network Recovery:**
    * In `video.onerror`, fatal decode errors (`err.code === 3`) and unsupported format errors (`err.code === 4`) on HEVC immediately fall back to H.264 (`loadPlayback('h264')`) without retrying broken HEVC bitstreams.
    * For transient network errors, retries are capped at 10. `onplaying` only resets `recoveryAttempts = 0` after 3 seconds of sustained successful playback via a debounced `stablePlayTimer` (cleared on pause, error, or load), preventing momentary playback ticks from resetting the retry counter into an infinite reload loop.
    * Network error recovery seeks slightly before the error point (`cur - 0.5`) to land on a preceding keyframe rather than re-triggering the same damaged boundary.
  * **Responsive Decoupled Scrubber Dragging:**
    * Visual playhead indicators (`timeline-progress`, `timeline-thumb`, `current-time-text`) update synchronously at 60fps on `mousemove` without waiting for the video decoder.
    * Media element seeking is throttled to 150ms intervals during drag (using `video.fastSeek` when available), and an exact seek is applied on `mouseup`, eliminating connection saturation and demuxer race conditions.
    * The `loop` attribute was removed from `<video id="timelapse-video">` to prevent unwanted auto-restart loops.
  * **HEVC Stream-Copy Timestamp Generation & No-BSF Remux:**
    * Concat of raw Annex B HEVC keyframe streams (`slot_*.hevc`) without container timestamps previously attempted using the FFmpeg bitstream filter `-bsf:v setts=pts=N/(TB*fps):dts=N/(TB*fps)`.
    * **Root Cause of Sub-Second Collapse (0.05s / 3369+ fps):** In FFmpeg 5.x / Debian 12 Bookworm, the input stream timebase `TB` in the `setts` bitstream filter for raw HEVC is unset (`0/1` = 0.0). Evaluating `N / (TB * fps)` causes division by zero in FFmpeg's `eval.c`, evaluating to `NAN` / 0, which advances timestamps by only 1 clock tick of the MP4 container (1/15,360th second) per frame, collapsing the video to 0.05 seconds at astronomical framerates (1,500–3,900 fps) and gigabit bitrates.
    * **Fix:** The broken `setts` bitstream filter was removed. Pure stream-copy with `-r <fps> -f concat -safe 0 -i list.txt -c:v copy -tag:v hvc1 -movflags +faststart` allows FFmpeg's MP4 muxer to generate clean, monotonic container timestamps strictly matching the specified `-r <fps>` (15, 30, 60) at 0% CPU with perfect duration and seeking support.
    * **Framerate Bounds Clamping:** In `export_mp4()`, `export_fps` is clamped to `[5.0, 60.0]` (`min(60.0, max(5.0, round(total_frames / target_video_sec, 2)))`), and `target_video_sec` has a floor of `0.5s`, preventing short buffer intervals from ever requesting absurd framerates.
    * **Slot Rotation Metadata Synchronization:** `_rotate_slot()` saves `meta.json` after updating `_current_slot`, and `_load_meta()` aligns `_current_slot` with the most recently modified slot file on disk.
  * **Zero-Boundary Clamp & Hung Player Loop Prevention:** In `/timelapse` (`momcozy/static/timelapse.js`), user seek functions clamp targets to `Math.max(0.05, Math.min(video.duration - 0.05, target))`. Removed any event listeners that mutated `currentTime` inside `seeking` callbacks. In `video.onerror`, recovery is capped (`recoveryAttempts < 10`, switching to `codec=h264` on the first failure when native HEVC decode fails) and waits for `loadedmetadata` before applying `currentTime`, eliminating recursive `Range: bytes=0-` fetch loops.
* **On-Demand Playback Motion Detection via Adaptive Binary Search:**
  * **0% Recording CPU & Silent Handheld Monitor:** No video frames are analyzed during recording, and the camera's hardware `motion_switch` remains completely OFF, guaranteeing the 5" handheld parent monitor tablet never beeps or wakes up.
  * **State-Difference Invariance:** Leveraging that scene states do not spontaneously return to the identical millimeter posture after motion, the detector evaluates coarse anchor intervals (e.g. 1 minute). If $\Delta(Frame(A), Frame(B)) < \text{threshold}$, the entire interval is skipped in $O(1)$.
  * **Divide-and-Conquer Subdivision & Adaptive Edge Detection Optimizations:**
    * **Active Coarse Window Clustering (#1):** In sustained motion runs spanning multiple coarse windows (e.g., [30, 60], [60, 90], [90, 120]), coarse windows are grouped into contiguous active clusters. The detector binary-searches only the onset in the first window and the offset in the last window, completely bypassing subdivision of interior windows (reducing decodes by up to 90% during extended movement).
    * **Quiet Coarse Window Gating (#2):** Boundary probing aborts outward steps into adjacent coarse windows that the coarse pass already verified were quiet ($\Delta < \text{threshold}$), preventing redundant outward decoding into quiescent sleep spans.
    * **Binary Search Edge Probing (#3):** When exponential boundary probing detects activity at step distance `cand`, a binary search between `cand` and the boundary finds the exact transition frame in $O(\log K)$ decodes rather than linearly walking every single frame.
    * **Default Leaf Window Granularity (`min_window = 2`, #5):** Subdivision defaults to `min_window = 2`, eliminating an entire level of unnecessary single-frame leaf depth while preserving sub-2-second accuracy.
    * **HEVC In-Loop Filter Bypass & Decoder Context Reuse (#6):** `extract_hevc_keyframe_luma` configures PyAV with `skip_loop_filter = 'all'` and `thread_count = 1`, and `detect_motion_events` reuses a single shared `av.CodecContext` instance across all frame decodes with clean buffer flushing, avoiding repeated C decoder allocations and HEVC deblocking filter computations on 16x16 luma fingerprints.
    * **Stage-Aware Decode Telemetry (#9):** Keyframe decodes are tracked and logged broken down by pipeline stage (`coarse`, `search`, and `probe`): `"Motion scan complete: indexed %d frames (decoded %d: %d coarse, %d search, %d probe), detected %d motion ranges"`.
    * **Contiguous Segment Partitioning Across Paused Sessions:** Keyframes separated by $> 30\text{s}$ of wall-clock time (e.g. daytime pause between Night 1 and Night 2) are partitioned into separate segments before coarse comparison so session cuts never fabricate artificial cross-pause motion ranges.
    * **Fresh Scan Replacement (`source: "scan"`):** Re-scanning replaces stale `"scan"` ranges from partial or overwritten slots while retaining and merging external/hardware events.
  * **Localized Sub-Block Luma Metric & Single-Pixel Outlier Trimming:**
    * When a baby occupies ~1/10th of the full frame and the surrounding room remains static, global mean difference over a 16x16 grid (256 points) dilutes large localized movement (individual pixel changes of 30–50) down to < 2.5, falling below static noise thresholds.
    * `luma_fingerprint_diff` slides a 4x4 sub-block window (step 2) across the 16x16 fingerprint and computes the maximum localized block mean difference. Each 4x4 block covers 1/16th (~6.25%) of the frame, matching the subject's area.
    * **OSD Clock & Single-Pixel Flicker Immunity (`drop_outliers=1`):** In camera video streams, the top-left on-screen display (OSD) clock digits periodically change numbers (e.g. at `0:02`, pixel `(1, 3)` jumped by 216 while all adjacent 15 pixels in the 4x4 block were quiet $\le 3$). Dividing 216 by 16 produced an artificial 13.5 delta. By trimming the single highest outlier pixel in each 4x4 block, isolated clock digit ticks drop to 2.2, completely eliminating false positives at `0:02`.
    * **Calibrated Threshold (`threshold = 3.5`):** With outlier pixel trimming active, still room sensor noise is $\le 2.4$. Setting `threshold = 3.5` captures all subtle movements (such as at `0:10` and `0:40` where baby posture shifts produce block deltas of $4.1\text{--}5.5$) as well as large movements (`0:27` and `0:30` at $35.0\text{--}50.0+$) while keeping static background intervals completely clean.
  * **Slot-Aware Timeline Mapping Across Gapped Slots:**
    * If recording was paused and resumed, gaps between slot files exist in wall-clock time, but FFmpeg concatenates slots consecutively with linear presentation timestamps.
    * Timeline position mapping in `get_status()` projects each event's wall-clock timestamp onto its containing slot and adds the cumulative duration of preceding slots, matching the concatenated video player timeline exactly.
  * **External Directory Restoration & Filesystem Reconciliation:**
    * When `timelapse/` files (`slot_*.hevc` or `meta.json`) are restored, copied, or modified from the host filesystem while the server is running, `_sync_meta_if_changed()` automatically detects the changes on disk (tracking `meta.json` modification time and slot file presence) and re-evaluates slots without requiring a server restart.
    * If `meta.json` was omitted or contains 0-frame entries during restore, `_load_meta` and `get_status` automatically reconcile against actual `slot_*.hevc` files on disk, approximating frame counts from file sizes (`max(1, size // 35000)`) and computing valid slot durations.
    * `has_content` in `get_status()` aligns with `self.has_content` (verifying whether valid slot files exist on disk), ensuring UI buttons (like `[▶ Watch]` and `[🗑️ Erase]`) enable as expected.
* **Tuya Cloud Authentication, Rate Limit Backoff & Session Reuse (`TuyaSessionManager`):**
  * **Centralized `TuyaSessionManager` (`momcozy/tuya_cloud.py`):**
    * Thread-safe session hub managing `TuyaSession` / `AccountSession` coercion, auth failure backoff, persistence callbacks, and bidirectional listener propagation between `MomcozyMonitor`, CLI/cloud DP pollers (`_fetch_cloud_dps`, `_publish_cloud_dps`), and `VideoStreamer`.
    * Shared `resolve_tuya_region`, `build_uid_token_request`, and `build_uid_login_payload` helpers deduplicate OEM Tuya gateway authentication across synchronous (`TuyaGatewayClient`) and async (`MomcozyGatewayClient`) clients.
    * Device share removal (`MomcozyCloudApi.remove_device_share` / `momcozy share remove`) revokes both the Tuya Home `relationId` (`smartlife.m.sharing.device.delete` via `remove_tuya_device_share`) and Momcozy Cloud share metadata so revoked users immediately lose live P2P stream access.
    * On generic auth errors (`LOGIN_TOKEN_FAILED`, `PERMISSION_DENIED`), backoff escalates exponentially (30s -> 60s -> 300s -> 900s).
    * On `USER_SESSION_LIMIT` ("You have loged in too many times today"), backoff starts at 30 minutes (1800s) up to 2 hours (7200s) to allow Tuya's rate-limit window to reset without burning requests.
    * Re-authentication is restricted strictly to true session expiration/invalidation errors (`USER_SESSION_INVALID`, `NOT_EXISTS_SESSION`, `SIGN_INVALID`, `TOKEN_INVALID`, `USER_SESSION_HAS_EXPIRED`, `PERMISSION_DENIED`), avoiding burn-down on generic network timeouts or device offline errors.
  * **Sliding-Window Re-Authentication Cooldown & Multi-Session Protection:**
    * **Concurrent Session Invalidation (Mobile App Conflict):** When official Momcozy/Tuya mobile apps or other processes log in, Tuya invalidates existing sessions with `USER_SESSION_INVALID`. Previously, `_fetch_cloud_dps` detected this and immediately invoked `get_or_login(force_refresh=True)`. Because `login_with_uid` succeeded, it reset `auth_backoff = 15.0`. This created an alternating ping-pong login war with the mobile app every 15 seconds, rapidly exhausting Tuya's daily login quota (`USER_SESSION_LIMIT`).
    * **Sliding-Window Cooldown (`_recent_logins` in `TuyaSessionManager`):** `TuyaSessionManager` tracks successful logins within a 10-minute (600s) window. While a single re-login is permitted, consecutive re-logins within 10 minutes trigger progressive cooldowns:
      - 2nd login in 10m: 60s cooldown.
      - 3rd login in 10m: 300s (5m) cooldown.
      - 4th+ login in 10m: 900s (15m) cooldown.
      During cooldown, `can_attempt_auth()` returns `False`, and `force_refresh` aborts network requests, gracefully preserving cached telemetry instead of fighting conflicting sessions.
    * **Disk Session Adoption (`record_login=False`):** `_fetch_cloud_dps` inspects `.momcozy.json` on disk before initiating network login. If another container/process updated `tuya_session["sid"]`, the daemon adopts it in-memory immediately without issuing a login request or incrementing the re-login quota.
    * **DPS Polling Cadence & Negative Caching:** `_fetch_cloud_dps` cache TTL is set to 20.0s, and `_dps_cache["ts"] = now` is updated on all execution branches (including exceptions and throttled cooldowns) to eliminate loop spinning.
  * **Persistent Session Reuse Across Streamer Lifecycles (`tuya_session` in `.momcozy.json`):**
    * Tuya sessions (`sid`, `ecode`, `uid`, `device_fingerprint`) are unified across `TuyaSession` and `AccountSession`, serialized to JSON, and persisted directly in `.momcozy.json` (`"tuya_session"`).
    * **Eliminating Login on 5-Minute Night Vision Sampling:** Previously, idle night vision checks woke up every 5 minutes and tore down `TuyaIpcP2pClient`, triggering a fresh `smartlife.m.user.uid.password.login` call ~288 times/day and rapidly hitting Tuya's account rate limit (`USER_SESSION_LIMIT`).
    * `VideoStreamer` retains its `_account_session` across `start()` and `stop()` cycles and pre-populates `client._account` in `create_p2p_client`.
    * When `client.async_stream_config()` runs, `client.async_account()` returns the cached `AccountSession` immediately, executing zero login HTTP requests during idle checks and viewer connections.
    * Whenever Tuya returns session expiration, the client re-authenticates once, fires `on_session_updated`, and `TuyaSessionManager` updates `MomcozyMonitor`, `VideoStreamer`, and `.momcozy.json`.
  * **Video Streamer (`VideoStreamer._stream_loop`):**
    * Reconnect backoff differentiates between transient network glitches (up to 120s) and authentication/rate-limit failures.
    * On `USER_SESSION_LIMIT`, backoff escalates to 1800s–7200s (30m–2h). On token rejection (`LOGIN_TOKEN_FAILED`), backoff scales up to 900s (15m).
  * **Detection Snapshot Loading (`/detection_snapshot.jpg`):**
    * The dashboard avoids unconditionally requesting `/detection_snapshot.jpg` on cold initial load before `has_detection_snapshot` is reported.
    * `handle_detection_snapshot` returns an immediate `404` when the stream is not connected and no snapshot/frame is cached, avoiding 5-second `504 Gateway Timeout` errors on cold dashboard loads.

### 8. Frontend Static Architecture, Web Security & Packaging
* **Static Frontend Assets (`momcozy/static/`):**
  * Dashboard and timelapse HTML, CSS, JavaScript, and SVG assets live as standalone files in `momcozy/static/` (`dashboard.html`, `dashboard.css`, `dashboard.js`, `timelapse.html`, `timelapse.css`, `timelapse.js`, `favicon.svg`) and are served via `/static/{filename}` with strict path-traversal rejection.
  * **DOM XSS Prevention & JS Syntax Verification:** Both `dashboard.js` and `timelapse.js` strictly forbid `.innerHTML` assignments, building UI elements exclusively with `document.createElement()` and `.textContent`. Automated tests in `tests/test_web_and_hls.py` enforce zero `.innerHTML` usage and run `node --check` on every static `.js` file.
* **CSRF Protection Without Authentication (`momcozy/web.py`):**
  * By design for frictionless home LAN / baby monitor usage, HTTP authentication is **not** enabled.
  * State-changing HTTP methods (`POST`, `PUT`, `DELETE`, `PATCH`) are protected by `_csrf_middleware` + `is_trusted_request_origin()`, which rejects cross-origin browser requests whenever `Sec-Fetch-Site: cross-site` or a mismatched `Origin` header is present while allowing same-origin UI fetches and non-browser CLI/curl tools (which send neither header).
* **Private Temporary Directories & Docker Build Hygiene:**
  * `HlsTranscoder` allocates a private temporary directory via `tempfile.mkdtemp(prefix="momcozy_hls_")` when `output_dir` is omitted (avoiding predictable shared `/tmp/momcozy_hls` symlink collisions) and removes owned directories in `stop()`.
  * `.dockerignore` excludes `.momcozy.json`, `ssl/`, `.momcozy_ssl/`, `*.pem`, `timelapse/`, and `*.hevc` so credentials, private keys, and recordings never leak into Docker build contexts.
* **Canonical Routes & Subcommands (Strict No-Aliases Policy):**
  * Only one canonical route/command exists per capability: `/audio` (no `/audio.mp3`), `/detection_snapshot.jpg` (no `/api/detection_snapshot`), `POST /api/timelapse/clear` (no `/api/timelapse/erase`), dedicated `/api/timelapse/*` routes (no duplicate timelapse actions inside `/api/control`), and `momcozy sync` (no `keys` alias).
* **Single-Source Packaging & Modular Tests:**
  * `pyproject.toml` is the single source of truth for runtime dependencies (including `cryptography>=38.0.0`) and `package-data` (`momcozy = ["static/*"]`); `requirements.txt` delegates via `-e .`, and `Dockerfile` (`python:3.15-slim-trixie`, `pip>=26.2`) caches dependencies via `pip install --no-cache-dir --only-deps .` followed by `pip install --no-cache-dir --no-deps -e .`.
  * Developer ADB key-extraction utility lives in `scripts/adb_extractor.py` outside the installable `momcozy` package.
  * Unit and regression tests are split by domain across `tests/test_device_and_cli.py`, `tests/test_cloud.py`, `tests/test_stream.py`, `tests/test_timelapse_and_nv.py`, and `tests/test_web_and_hls.py`.






