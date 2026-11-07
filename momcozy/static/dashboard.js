if (location.hostname === '0.0.0.0') {
  location.hostname = 'localhost';
}

window.addEventListener('error', function(e) {
  console.error('[Window Error]', e.message, 'at', e.filename, ':', e.lineno, e.error);
});
window.addEventListener('unhandledrejection', function(e) {
  console.error('[Unhandled Promise Rejection]', e.reason);
});

window['__onGCastApiAvailable'] = function(isAvailable, reason) {
  console.log(`[Google Cast] __onGCastApiAvailable called: isAvailable=${isAvailable}, reason=${reason}`);
  if (isAvailable && window.cast && window.cast.framework) {
    try {
      const castContext = cast.framework.CastContext.getInstance();
      castContext.setOptions({
        receiverApplicationId: chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
        autoJoinPolicy: chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED
      });
      window.googleCastInitialized = true;
      console.log("[Google Cast] CastContext initialized successfully.");

      castContext.addEventListener(
        cast.framework.CastContextEventType.SESSION_STATE_CHANGED,
        function(event) {
          console.log("[Google Cast] Session state changed:", event.sessionState);
          const castBtn = document.getElementById('cast-btn');
          const castText = document.getElementById('cast-btn-text');

          switch (event.sessionState) {
            case cast.framework.SessionState.SESSION_STARTED:
            case cast.framework.SessionState.SESSION_RESUMED: {
              if (castBtn) castBtn.classList.add('active');
              if (castText) castText.textContent = 'Casting';
              showToast('Connected to Cast device!', 'success', 3000);
              const session = castContext.getCurrentSession();
              if (session && typeof window.loadStreamToCastSession === 'function') {
                window.loadStreamToCastSession(session);
              }
              break;
            }
            case cast.framework.SessionState.SESSION_STARTING: {
              if (castBtn) castBtn.classList.remove('active');
              if (castText) castText.textContent = 'Connecting...';
              break;
            }
            case cast.framework.SessionState.SESSION_ENDING: {
              if (castBtn) castBtn.classList.remove('active');
              if (castText) castText.textContent = 'Disconnecting...';
              break;
            }
            case cast.framework.SessionState.SESSION_ENDED:
            case cast.framework.SessionState.NO_SESSION: {
              if (castBtn) castBtn.classList.remove('active');
              if (castText) castText.textContent = 'Cast';
              break;
            }
            case cast.framework.SessionState.SESSION_START_FAILED: {
              if (castBtn) castBtn.classList.remove('active');
              if (castText) castText.textContent = 'Cast';
              showToast('Failed to connect to Cast device.', 'error', 4000);
              break;
            }
          }
        }
      );
    } catch (e) {
      console.error("[Google Cast] Exception during CastContext initialization:", e);
    }
  } else {
    const isSecure = window.isSecureContext;
    console.warn(`[Google Cast] Cast API is not available (isAvailable=${isAvailable}). isSecureContext=${isSecure}, origin=${window.location.origin}`);
    if (!isSecure) {
      console.error("[Google Cast] Insecure Context: Chrome restricts Google Cast to HTTPS or localhost. On plain HTTP over LAN, Chrome disables the Cast Web Sender API.");
    }
  }
};

function showToast(message, type = 'error', duration = 5000, options = null) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;

  const iconSpan = document.createElement('span');
  iconSpan.style.cssText = 'font-size:1.1rem; line-height:1;';
  iconSpan.textContent = type === 'error' ? '❌' : (type === 'warn' ? '⚠️' : (type === 'success' ? '✓' : 'ℹ️'));

  const msgDiv = document.createElement('div');
  msgDiv.style.flex = '1';
  msgDiv.textContent = String(message ?? '');
  if (options && options.linkUrl && /^https?:\/\//i.test(options.linkUrl)) {
    const link = document.createElement('a');
    link.href = options.linkUrl;
    link.style.cssText = 'color:#2B6CB0; text-decoration:underline; font-weight:bold;';
    link.textContent = options.linkText || options.linkUrl;
    msgDiv.appendChild(link);
    if (options.linkSuffix) {
      msgDiv.appendChild(document.createTextNode(options.linkSuffix));
    }
  }

  const closeBtn = document.createElement('button');
  closeBtn.style.cssText = 'background:none;border:none;cursor:pointer;font-weight:bold;color:#718096;font-size:1rem;line-height:1;';
  closeBtn.textContent = '✕';
  closeBtn.addEventListener('click', () => toast.remove());

  toast.appendChild(iconSpan);
  toast.appendChild(msgDiv);
  toast.appendChild(closeBtn);
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(20px)';
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

function renderStatusBanner(banner, title, detail, showHint = false) {
  banner.textContent = '';
  banner.appendChild(document.createTextNode('⚠️ '));
  const strong = document.createElement('strong');
  strong.textContent = `${title}:`;
  banner.appendChild(strong);
  banner.appendChild(document.createTextNode(` ${String(detail ?? '')}`));
  if (showHint) {
    banner.appendChild(document.createElement('br'));
    const hint = document.createElement('span');
    hint.style.cssText = 'color:#742A2A; font-size:0.82rem;';
    hint.appendChild(document.createTextNode('Note: If the camera is at this IP but not responding, verify its local key or run '));
    const code = document.createElement('code');
    code.textContent = './momcozy.py login';
    hint.appendChild(code);
    hint.appendChild(document.createTextNode('.'));
    banner.appendChild(hint);
  }
}

function setLabelWithMoon(el, prefix, suffix = '') {
  if (!el) return;
  el.textContent = '';
  if (prefix) {
    el.appendChild(document.createTextNode(prefix));
  }
  const moon = document.createElement('span');
  moon.className = 'nv-moon';
  moon.textContent = '🌙';
  el.appendChild(moon);
  if (suffix) {
    el.appendChild(document.createTextNode(suffix));
  }
}

async function apiPost(action, data = {}) {
  try {
    const res = await fetch('/api/control', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, ...data})
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || json.status === 'error') {
      const errDetail = json.error || `Server returned error (${res.status})`;
      showToast(`Action failed: ${errDetail}`, 'error');
      return false;
    }
    return true;
  } catch (e) {
    console.error(e);
    showToast(`Request failed: ${e.message}`, 'error');
    return false;
  }
}

let lastDetectTime = null;
function viewFullDetectSnapshot() {
  window.open('/detection_snapshot.jpg?t=' + Date.now(), '_blank');
}

async function captureLiveSnapshot() {
  showToast('Capturing snapshot from live camera...', 'info', 1500);
  const ok = await apiPost('capture_detection_snapshot');
  if (ok) {
    showToast('Detection snapshot captured!', 'success', 2000);
    const detectImg = document.getElementById('detect-img');
    if (detectImg) detectImg.src = '/detection_snapshot.jpg?t=' + Date.now();
    updateStatus();
  }
}

let ptzBusy = false;
async function ptzStep(dir) {
  if (ptzBusy) return;
  ptzBusy = true;
  try {
    await apiPost('ptz_step', {direction: dir, duration: 0.175});
  } finally {
    setTimeout(() => { ptzBusy = false; }, 175);
  }
}

function ptz(dir) { apiPost('ptz_move', {direction: dir}); }
function ptzStop() { apiPost('ptz_stop'); }

// Fullscreen support with mobile iOS fallback
function isFullscreen() {
  const card = document.querySelector('.video-card');
  return !!(
    document.fullscreenElement ||
    document.webkitFullscreenElement ||
    document.mozFullScreenElement ||
    document.msFullscreenElement ||
    (card && card.classList.contains('is-fullscreen'))
  );
}

function updateFullscreenIcons(active) {
  const enterIcon = document.getElementById('fs-enter-icon');
  const exitIcon = document.getElementById('fs-exit-icon');
  const btnText = document.getElementById('fs-btn-text');
  if (enterIcon) enterIcon.style.display = active ? 'none' : 'block';
  if (exitIcon) exitIcon.style.display = active ? 'block' : 'none';
  if (btnText) btnText.textContent = active ? 'Exit' : 'Fullscreen';
}

async function toggleFullscreen() {
  const card = document.querySelector('.video-card');
  if (!card) return;
  const active = isFullscreen();

  if (!active) {
    try {
      if (card.requestFullscreen) {
        await card.requestFullscreen();
      } else if (card.webkitRequestFullscreen) {
        await card.webkitRequestFullscreen();
      } else if (card.mozRequestFullScreen) {
        await card.mozRequestFullScreen();
      } else if (card.msRequestFullscreen) {
        await card.msRequestFullscreen();
      } else {
        card.classList.add('is-fullscreen');
        updateFullscreenIcons(true);
      }
    } catch (e) {
      console.warn('Native fullscreen request failed, falling back to CSS fullscreen:', e);
      card.classList.add('is-fullscreen');
      updateFullscreenIcons(true);
    }
  } else {
    if (card.classList.contains('is-fullscreen')) {
      card.classList.remove('is-fullscreen');
      updateFullscreenIcons(false);
    }
    try {
      if (document.exitFullscreen) {
        await document.exitFullscreen();
      } else if (document.webkitExitFullscreen) {
        await document.webkitExitFullscreen();
      } else if (document.mozCancelFullScreen) {
        await document.mozCancelFullScreen();
      } else if (document.msExitFullscreen) {
        await document.msExitFullscreen();
      }
    } catch (e) {
      console.warn('Exit fullscreen error:', e);
    }
  }
}

['fullscreenchange', 'webkitfullscreenchange', 'mozfullscreenchange', 'MSFullscreenChange'].forEach(ev => {
  document.addEventListener(ev, () => {
    const active = isFullscreen();
    updateFullscreenIcons(active);
    const card = document.querySelector('.video-card');
    if (!active && card && card.classList.contains('is-fullscreen')) {
      card.classList.remove('is-fullscreen');
    }
  });
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const card = document.querySelector('.video-card');
    if (card && card.classList.contains('is-fullscreen')) {
      card.classList.remove('is-fullscreen');
      updateFullscreenIcons(false);
    }
  }
});

// Live Audio streaming
let audioActive = false;
const audioElement = document.getElementById('live-audio');

async function toggleAudio() {
  const audioBtn = document.getElementById('audio-btn');
  const offIcon = document.getElementById('audio-off-icon');
  const onIcon = document.getElementById('audio-on-icon');
  const audioText = document.getElementById('audio-btn-text');
  if (!audioElement) return;

  if (!audioActive) {
    audioElement.src = '/audio?t=' + Date.now();
    try {
      await audioElement.play();
      audioActive = true;
      if (audioBtn) audioBtn.classList.add('active');
      if (offIcon) offIcon.style.display = 'none';
      if (onIcon) onIcon.style.display = 'block';
      if (audioText) audioText.textContent = 'Listening';
      showToast('Live audio unmuted', 'info', 2000);
    } catch (e) {
      console.error('Audio play error:', e);
      showToast('Audio playback failed: ' + e.message, 'error');
    }
  } else {
    audioElement.pause();
    audioElement.removeAttribute('src');
    audioElement.load();
    audioActive = false;
    if (audioBtn) audioBtn.classList.remove('active');
    if (offIcon) offIcon.style.display = 'block';
    if (onIcon) onIcon.style.display = 'none';
    if (audioText) audioText.textContent = 'Audio';
    showToast('Live audio muted', 'info', 2000);
  }
}

if (audioElement) {
  audioElement.onerror = function() {
    if (audioActive) {
      console.warn('Audio stream error, stopping playback');
      audioElement.pause();
      audioActive = false;
      const audioBtn = document.getElementById('audio-btn');
      const offIcon = document.getElementById('audio-off-icon');
      const onIcon = document.getElementById('audio-on-icon');
      const audioText = document.getElementById('audio-btn-text');
      if (audioBtn) audioBtn.classList.remove('active');
      if (offIcon) offIcon.style.display = 'block';
      if (onIcon) onIcon.style.display = 'none';
      if (audioText) audioText.textContent = 'Audio';
      showToast('Live audio disconnected', 'warn', 3000);
    }
  };
}

// Stream player: WebCodecs (HEVC hardware pass-through) with automatic MJPEG fallback
const streamImg = document.getElementById('stream-img');
const streamCanvas = document.getElementById('stream-canvas');
let streamMode = 'none'; // 'webcodecs' | 'mjpeg' | 'none'
let streamRetryTimer = null;
let streamRetryCount = 0;
let decoderErrorCount = 0;
let activeWs = null;
let activeDecoder = null;
let supportedHevcCodec = null;
let isFallbackActive = false;
let hasFatalDecoderError = false;

function setStreamError(title, detail) {
  const streamBadge = document.getElementById('stream-badge');
  const liveDot = document.getElementById('live-dot');
  if (streamBadge) streamBadge.textContent = 'STREAM ERROR';
  if (liveDot) {
    liveDot.style.background = '#E53E3E';
    liveDot.style.animation = 'none';
  }
  const errOverlay = document.getElementById('video-error');
  if (errOverlay) {
    errOverlay.classList.remove('hidden');
    const titleEl = document.getElementById('video-error-title');
    const detailEl = document.getElementById('video-error-detail');
    if (titleEl) titleEl.textContent = title;
    if (detailEl) detailEl.textContent = detail;
  }
}

function cleanupDecoder() {
  if (activeDecoder) {
    try {
      activeDecoder.close();
    } catch (e) {}
    activeDecoder = null;
  }
}

function cleanupWebCodecs() {
  if (activeWs) {
    try {
      activeWs.onopen = null;
      activeWs.onmessage = null;
      activeWs.onerror = null;
      activeWs.onclose = null;
      activeWs.close();
    } catch (e) {}
    activeWs = null;
  }
  cleanupDecoder();
}

function fallbackToMjpeg(reason) {
  if (isFallbackActive) return;
  isFallbackActive = true;
  hasFatalDecoderError = true;
  console.warn('[Stream] Fallback to MJPEG triggered:', reason);
  if (reason && typeof showToast === 'function') {
    showToast('HEVC decoding unavailable: ' + reason + '. Switched to standard stream.', 'warn', 6000);
  }
  cleanupWebCodecs();
  startMjpegStream(reason);
}

function startMjpegStream(reason) {
  streamMode = 'mjpeg';
  if (streamCanvas) streamCanvas.style.display = 'none';
  if (streamImg) {
    streamImg.style.display = 'block';
    streamImg.src = '/stream?t=' + Date.now();
  }
  const streamBadge = document.getElementById('stream-badge');
  if (streamBadge) {
    streamBadge.textContent = 'LIVE STREAM (MJPEG)';
    streamBadge.title = reason ? `Standard MJPEG stream (Fallback: ${reason})` : 'Standard MJPEG stream';
  }
}

async function checkHevcWebCodecsSupport() {
  if (!window.isSecureContext) {
    console.warn('[WebCodecs] Insecure Context: Chrome disables VideoDecoder on plain HTTP unless accessed via localhost or 127.0.0.1.');
  }
  if (!('VideoDecoder' in window) || !('EncodedVideoChunk' in window)) {
    console.log('[WebCodecs] VideoDecoder or EncodedVideoChunk API not present in window. isSecureContext=' + window.isSecureContext);
    return null;
  }
  const codecs = [
    'hev1.1.6.L150.B0',
    'hvc1.1.6.L150.B0',
    'hev1.1.6.L120.B0',
    'hvc1.1.6.L120.B0',
    'hev1.1.6.L93.B0',
    'hvc1.1.6.L93.B0',
    'hev1.1.6.L153.B0',
    'hvc1.1.6.L153.B0',
    'hev1.1.6.L90.90'
  ];
  for (const c of codecs) {
    try {
      const res = await VideoDecoder.isConfigSupported({
        codec: c,
        codedWidth: 1920,
        codedHeight: 1080
      });
      console.log(`[WebCodecs] Codec support probe for ${c}: supported=${res && res.supported}`);
      if (res && res.supported) {
        return c;
      }
    } catch (e) {
      console.warn(`[WebCodecs] Probe ${c} threw:`, e);
    }
  }
  return null;
}

function startWebCodecsStream(codec) {
  cleanupWebCodecs();
  streamMode = 'webcodecs';
  isFallbackActive = false;

  let hasKeyframe = false;
  let frameCount = 0;
  let chunksFed = 0;
  let lastPts = 0;

  const streamBadgeInit = document.getElementById('stream-badge');
  if (streamBadgeInit) streamBadgeInit.textContent = 'CONNECTING...';

  function createDecoder() {
    cleanupDecoder();
    hasKeyframe = false;
    console.log(`[WebCodecs] Initializing VideoDecoder with codec: ${codec}`);
    try {
      activeDecoder = new VideoDecoder({
        output: (frame) => {
          if (streamMode !== 'webcodecs') {
            frame.close();
            return;
          }
          if (streamCanvas.width !== frame.displayWidth || streamCanvas.height !== frame.displayHeight) {
            streamCanvas.width = frame.displayWidth;
            streamCanvas.height = frame.displayHeight;
          }
          const ctx = streamCanvas.getContext('2d');
          ctx.drawImage(frame, 0, 0, streamCanvas.width, streamCanvas.height);
          frame.close();

          if (frameCount === 0) {
            streamRetryCount = 0;
            decoderErrorCount = 0;
            console.log(`[WebCodecs] First video frame rendered successfully (${streamCanvas.width}x${streamCanvas.height})`);
            if (streamCanvas) streamCanvas.style.display = 'block';
            if (streamImg) {
              streamImg.style.display = 'none';
              streamImg.src = '';
            }
            const streamBadge = document.getElementById('stream-badge');
            const liveDot = document.getElementById('live-dot');
            if (streamBadge) {
              streamBadge.textContent = 'LIVE STREAM (HEVC)';
              streamBadge.title = `Hardware-accelerated HEVC stream (${codec})`;
            }
            if (liveDot) {
              liveDot.style.background = '#48BB78';
              liveDot.style.animation = 'pulse 1.5s infinite';
            }
            const errOverlay = document.getElementById('video-error');
            if (errOverlay) errOverlay.classList.add('hidden');
          }
          frameCount++;
        },
        error: (err) => {
          const errName = err ? (err.name || 'Error') : 'UnknownError';
          const errMsg = err ? (err.message || String(err)) : 'Unknown decoder error';
          const diag = `codec=${codec}, state=${activeDecoder ? activeDecoder.state : 'null'}, qSize=${activeDecoder ? activeDecoder.decodeQueueSize : 'null'}, chunksFed=${chunksFed}, framesRendered=${frameCount}, hasKeyframe=${hasKeyframe}`;
          console.error(`[WebCodecs] Decoder error (${errName}): ${errMsg}. Details: [${diag}]`, err);
          decoderErrorCount++;
          if (decoderErrorCount < 3) {
            console.warn(`[WebCodecs] Recovering from decoder error (${decoderErrorCount}/3). Awaiting fresh keyframe...`);
            createDecoder();
          } else {
            fallbackToMjpeg(`Persistent decoder error (${errName}: ${errMsg})`);
          }
        }
      });

      activeDecoder.configure({
        codec: codec,
        codedWidth: 1920,
        codedHeight: 1080,
        optimizeForLatency: true
      });
      return true;
    } catch (err) {
      const errName = err ? (err.name || 'Error') : 'UnknownError';
      const errMsg = err ? (err.message || String(err)) : 'Configuration exception';
      console.error(`[WebCodecs] Failed configuring decoder (${errName}): ${errMsg}`, err);
      fallbackToMjpeg(`Decoder config failed: ${errMsg}`);
      return false;
    }
  }

  if (!createDecoder()) {
    return;
  }

  const wsProto = (location.protocol === 'https:') ? 'wss:' : 'ws:';
  const wsUrl = `${wsProto}//${location.host}/ws/stream`;
  console.log(`[WebCodecs] Connecting WebSocket to ${wsUrl}`);
  const ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  activeWs = ws;

  ws.onopen = () => {
    console.log('[WebCodecs] WebSocket connected to /ws/stream');
  };

  ws.onmessage = (event) => {
    if (streamMode !== 'webcodecs' || !activeDecoder || activeDecoder.state === 'closed') return;
    if (!(event.data instanceof ArrayBuffer)) return;
    const bytes = new Uint8Array(event.data);
    if (bytes.length <= 1) return;
    const isKey = (bytes[0] === 1);
    if (!hasKeyframe) {
      if (!isKey) return; // Wait for first keyframe
      hasKeyframe = true;
      console.log(`[WebCodecs] Received first keyframe Access Unit (${bytes.length - 1} bytes). Feeding decoder.`);
    }
    const chunkData = bytes.subarray(1);
    let pts = Math.round(performance.now() * 1000);
    if (pts <= lastPts) {
      pts = lastPts + 1000;
    }
    lastPts = pts;
    try {
      const chunk = new EncodedVideoChunk({
        type: isKey ? 'key' : 'delta',
        timestamp: pts,
        data: chunkData
      });
      chunksFed++;
      activeDecoder.decode(chunk);
    } catch (err) {
      const errName = err ? (err.name || 'Error') : 'Error';
      const errMsg = err ? (err.message || String(err)) : 'Decode enqueue failed';
      const diag = `codec=${codec}, state=${activeDecoder ? activeDecoder.state : 'null'}, chunksFed=${chunksFed}, isKey=${isKey}, size=${chunkData.byteLength}`;
      console.error(`[WebCodecs] Chunk decode exception (${errName}): ${errMsg}. Details: [${diag}]`, err);
      decoderErrorCount++;
      if (decoderErrorCount < 3) {
        console.warn(`[WebCodecs] Recovering from chunk decode exception (${decoderErrorCount}/3). Awaiting fresh keyframe...`);
        createDecoder();
      } else {
        fallbackToMjpeg(`Persistent chunk decode exception: ${errMsg}`);
      }
    }
  };

  ws.onerror = (e) => {
    console.warn('[WebCodecs] WebSocket error:', e);
  };

  ws.onclose = (e) => {
    console.log('[WebCodecs] WebSocket closed:', e.code, e.reason);
    if (streamMode === 'webcodecs' && !isFallbackActive && !hasFatalDecoderError) {
      streamRetryCount++;
      const delay = Math.min(3000, 1000 + streamRetryCount * 200);
      console.log(`[WebCodecs] Waiting for camera stream (attempt ${streamRetryCount} in ${delay}ms)...`);
      const streamBadge = document.getElementById('stream-badge');
      const liveDot = document.getElementById('live-dot');
      if (streamBadge) streamBadge.textContent = 'CONNECTING...';
      if (liveDot) {
        liveDot.style.background = '#ED8936';
        liveDot.style.animation = 'pulse 1s infinite';
      }
      if (!streamRetryTimer) {
        streamRetryTimer = setTimeout(() => {
          streamRetryTimer = null;
          retryStream();
        }, delay);
      }
    }
  };
}

async function initPlayer() {
  supportedHevcCodec = await checkHevcWebCodecsSupport();
  if (supportedHevcCodec) {
    console.log('[Stream] WebCodecs HEVC hardware decoding supported (' + supportedHevcCodec + '). Using WebCodecs.');
    startWebCodecsStream(supportedHevcCodec);
  } else {
    const reason = (!window.isSecureContext)
      ? 'Requires HTTPS or localhost'
      : 'WebCodecs HEVC not supported in this browser';
    console.log('[Stream] ' + reason + '. Using MJPEG fallback.');
    startMjpegStream(reason);
  }
}

function retryStream() {
  const errOverlay = document.getElementById('video-error');
  if (errOverlay) errOverlay.classList.add('hidden');
  const streamBadge = document.getElementById('stream-badge');
  const liveDot = document.getElementById('live-dot');
  if (streamBadge) streamBadge.textContent = 'CONNECTING...';
  if (liveDot) {
    liveDot.style.background = '#ED8936';
    liveDot.style.animation = 'pulse 1s infinite';
  }
  showToast('Connecting to camera stream...', 'info', 2500);

  decoderErrorCount = 0;
  hasFatalDecoderError = false;
  isFallbackActive = false;

  if (supportedHevcCodec) {
    startWebCodecsStream(supportedHevcCodec);
  } else {
    startMjpegStream();
  }
  updateStatus();
}

if (streamCanvas) {
  streamCanvas.addEventListener('dblclick', (e) => {
    e.preventDefault();
    toggleFullscreen();
  });
}

if (streamImg) {
  streamImg.addEventListener('dblclick', (e) => {
    e.preventDefault();
    toggleFullscreen();
  });

  streamImg.onerror = function() {
    if (streamMode !== 'mjpeg') return;
    streamRetryCount++;
    const delay = Math.min(10, 2 * streamRetryCount);
    setStreamError('Live Stream Reconnecting', `Video feed connection lost. Retrying in ${delay}s...`);
    if (!streamRetryTimer) {
      streamRetryTimer = setTimeout(() => {
        streamRetryTimer = null;
        if (streamImg) streamImg.src = '/stream?t=' + Date.now();
      }, delay * 1000);
    }
  };

  streamImg.onload = function() {
    if (streamMode !== 'mjpeg') return;
    streamRetryCount = 0;
    const errOverlay = document.getElementById('video-error');
    if (errOverlay) errOverlay.classList.add('hidden');
    const streamBadge = document.getElementById('stream-badge');
    const liveDot = document.getElementById('live-dot');
    if (streamBadge) {
      streamBadge.textContent = 'LIVE STREAM';
      streamBadge.title = 'Motion JPEG stream (Fallback)';
    }
    if (liveDot) {
      liveDot.style.background = '#48BB78';
      liveDot.style.animation = 'pulse 1.5s infinite';
    }
  };

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      if (streamMode === 'webcodecs') {
        if (!activeWs || activeWs.readyState !== WebSocket.OPEN) {
          retryStream();
        }
      } else if (streamMode === 'mjpeg') {
        if (streamImg && (!streamImg.complete || streamImg.naturalWidth === 0)) {
          streamImg.src = '/stream?t=' + Date.now();
        }
      }
      updateStatus();
    }
  });
}

function setNightVision(mode) { apiPost('night_vision', {mode}); }
function togglePrivacy() { apiPost('toggle_privacy'); }
function toggleLed() { apiPost('toggle_led'); }
function toggleFlip() { apiPost('toggle_flip'); }
function toggleLullaby(play) {
  const track = document.getElementById('lullaby-track').value;
  const vol = document.getElementById('lullaby-vol').value;
  apiPost('lullaby', {play, track, volume: parseInt(vol, 10)});
}
function setLullabyVol(vol) { apiPost('lullaby_volume', {volume: parseInt(vol, 10)}); }
function changeLullaby() { toggleLullaby(true); }

let offlineFailures = 0;
async function updateStatus() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    offlineFailures = 0;
    const d = await res.json();
    if (d.server_lan_ip) window.serverLanIp = d.server_lan_ip;
    if (d.server_port) window.serverPort = d.server_port;
    if (d.server_http_port) window.serverHttpPort = d.server_http_port;
    const badge = document.getElementById('conn-badge');
    let banner = document.getElementById('status-error-banner');

    if (d.online === false) {
      if (badge) {
        badge.className = 'badge';
        badge.style.background = '#FED7D7';
        badge.style.color = '#C53030';
        badge.textContent = 'Offline / Communication Notice';
      }
      if (d.error && !banner) {
        banner = document.createElement('div');
        banner.id = 'status-error-banner';
        banner.style.cssText = 'background:#FFF5F5; border:1px solid #FEB2B2; color:#9B2C2C; padding:12px 16px; border-radius:8px; margin-bottom:16px; font-size:0.88rem; line-height:1.4;';
        const grid = document.querySelector('.grid');
        if (grid && grid.parentNode) {
          grid.parentNode.insertBefore(banner, grid);
        }
      }
      if (banner) {
        renderStatusBanner(banner, 'Device Communication Notice', d.error, true);
      }
    } else {
      if (badge) {
        badge.className = 'badge active';
        badge.style.background = '#C6F6D5';
        badge.style.color = '#22543D';
        badge.textContent = 'Connected';
      }
      if (banner) {
        banner.remove();
      }
    }

    // Live stream status inspection
    if (d.stream_configured === false) {
      setStreamError('Video Stream Not Configured', 'No Tuya cloud credentials found in .momcozy.json. Run "./momcozy.py login" to link your credentials.');
    } else if (d.stream_error && !d.stream_running) {
      setStreamError('Video Stream Error', d.stream_error);
    } else if (d.stream_connected) {
      const errOverlay = document.getElementById('video-error');
      if (errOverlay) errOverlay.classList.add('hidden');
      // Auto-promote to WebCodecs HEVC if stream is now connected and client is in temporary MJPEG mode without decoder errors
      if (supportedHevcCodec && !hasFatalDecoderError && streamMode === 'mjpeg') {
        console.log('[Stream] Camera live stream is now connected. Auto-promoting from MJPEG to WebCodecs HEVC.');
        isFallbackActive = false;
        streamRetryCount = 0;
        decoderErrorCount = 0;
        startWebCodecsStream(supportedHevcCodec);
      }
      const streamBadge = document.getElementById('stream-badge');
      const liveDot = document.getElementById('live-dot');
      if (streamBadge) {
        if (streamMode === 'webcodecs') {
          streamBadge.textContent = 'LIVE STREAM (HEVC)';
        } else if (streamMode === 'mjpeg') {
          streamBadge.textContent = 'LIVE STREAM (MJPEG)';
        } else {
          streamBadge.textContent = 'LIVE STREAM';
        }
      }
      if (liveDot) {
        liveDot.style.background = '#48BB78';
        liveDot.style.animation = 'pulse 1.5s infinite';
      }
    } else if (d.stream_running) {
      const streamBadge = document.getElementById('stream-badge');
      const liveDot = document.getElementById('live-dot');
      if (streamBadge) streamBadge.textContent = 'CONNECTING...';
      if (liveDot) {
        liveDot.style.background = '#ED8936';
        liveDot.style.animation = 'pulse 1s infinite';
      }
    }

    // Update transport badges
    if (d.control_transport) {
      const ctrlEl = document.getElementById('hdr-control-transport');
      if (ctrlEl) {
        ctrlEl.textContent = d.control_transport;
        if (d.control_diagnostic) {
          ctrlEl.title = d.control_diagnostic;
        }
      }
      const diagEl = document.getElementById('hdr-control-diag');
      if (diagEl) {
        if (d.control_mode !== 'local_lan' && d.control_diagnostic) {
          diagEl.textContent = `(${d.control_diagnostic})`;
          diagEl.title = d.control_diagnostic;
          diagEl.style.display = 'inline';
        } else {
          diagEl.style.display = 'none';
        }
      }
    }
    if (d.stream_transport) {
      const stEl = document.getElementById('hdr-stream-transport');
      if (stEl) stEl.textContent = d.stream_transport;
    }

    if (d.temperature_c !== null && d.temperature_c !== undefined) {
      const tempEl = document.getElementById('temp-val');
      if (tempEl) {
        tempEl.textContent = `${Number(d.temperature_c).toFixed(1)}°C `;
        const fSpan = document.createElement('span');
        fSpan.textContent = `(${Number(d.temperature_f).toFixed(1)}°F)`;
        tempEl.appendChild(fSpan);
      }
    }
    document.getElementById('motion-stat').textContent = d.motion_switch ? "Active" : "Off";
    document.getElementById('cry-stat').textContent = d.cry_detection_switch ? "Active" : "Off";
    document.getElementById('priv-stat').textContent = d.privacy_mode ? "ON" : "OFF";
    const nvMap = {"0": "Auto", "1": "Off", "2": "On"};
    document.getElementById('nv-stat').textContent = nvMap[d.night_vision] || d.night_vision;

    // Detection snapshot telemetry
    const detectTrigger = d.detection_trigger || d.latest_event_name || 'Event Alert';
    const detectTime = d.detection_time_str || d.latest_event_time_str || '';
    const trigEl = document.getElementById('detect-trigger');
    if (trigEl) trigEl.textContent = detectTrigger;
    const timeEl = document.getElementById('detect-time');
    if (timeEl) timeEl.textContent = detectTime || 'No recent events';
    if (d.latest_event_file) {
      const rowEl = document.getElementById('detect-cloud-row');
      const fileEl = document.getElementById('detect-cloud-file');
      if (rowEl) rowEl.style.display = 'flex';
      if (fileEl) {
        fileEl.textContent = d.latest_event_file;
        fileEl.title = `${d.latest_event_bucket || 'cloud'}: ${d.latest_event_file}`;
      }
    }
    if (d.detection_time && d.detection_time !== lastDetectTime) {
      lastDetectTime = d.detection_time;
      const detectImg = document.getElementById('detect-img');
      const detectEmpty = document.getElementById('detect-empty');
      if (detectImg) {
        detectImg.src = '/detection_snapshot.jpg?t=' + Date.now();
        detectImg.style.display = 'block';
      }
      if (detectEmpty) detectEmpty.style.display = 'none';
    } else if (!d.has_detection_snapshot && !lastDetectTime) {
      const detectImg = document.getElementById('detect-img');
      const detectEmpty = document.getElementById('detect-empty');
      if (detectImg) detectImg.style.display = 'none';
      if (detectEmpty) detectEmpty.style.display = 'block';
    } else if (d.has_detection_snapshot && !lastDetectTime) {
      lastDetectTime = d.detection_time || 1;
      const detectImg = document.getElementById('detect-img');
      const detectEmpty = document.getElementById('detect-empty');
      if (detectImg) {
        detectImg.src = '/detection_snapshot.jpg?t=' + Date.now();
        detectImg.style.display = 'block';
      }
      if (detectEmpty) detectEmpty.style.display = 'none';
    }
    if (d.timelapse) {
      updateTimelapseUI(d.timelapse);
    }
  } catch (e) {
    offlineFailures++;
    if (offlineFailures >= 2) {
      const badge = document.getElementById('conn-badge');
      if (badge) {
        badge.className = 'badge';
        badge.style.background = '#FED7D7';
        badge.style.color = '#C53030';
        badge.textContent = 'Server Unreachable';
      }
      let banner = document.getElementById('status-error-banner');
      if (!banner) {
        banner = document.createElement('div');
        banner.id = 'status-error-banner';
        banner.style.cssText = 'background:#FFF5F5; border:1px solid #FEB2B2; color:#9B2C2C; padding:12px 16px; border-radius:8px; margin-bottom:16px; font-size:0.88rem; line-height:1.4;';
        const grid = document.querySelector('.grid');
        if (grid && grid.parentNode) {
          grid.parentNode.insertBefore(banner, grid);
        }
      }
      if (banner) {
        renderStatusBanner(banner, 'Dashboard Server Connection Notice', `${e.message}. Reconnecting...`, false);
      }
    }
  }
}

window.loadStreamToCastSession = function(session) {
  if (!session) {
    console.warn("[Google Cast] loadStreamToCastSession called without active session.");
    return;
  }
  const state = session.getSessionState ? session.getSessionState() : null;
  if (state && state !== cast.framework.SessionState.SESSION_STARTED && state !== cast.framework.SessionState.SESSION_RESUMED) {
    console.log("[Google Cast] Session not ready for media load (state=" + state + ").");
    return;
  }

  // Chromecast strictly rejects self-signed HTTPS certificates.
  // Therefore, always provide an HTTP stream URL for Chromecast over local LAN:
  const castProto = 'http:';
  const castPort = window.serverHttpPort ? (':' + window.serverHttpPort) : (window.location.protocol === 'http:' && window.location.port ? (':' + window.location.port) : ':8420');
  const streamUrl = `${castProto}//${window.location.hostname}${castPort}/hls/stream.m3u8`;
  console.log("[Google Cast] Loading stream to receiver:", streamUrl);

  const mediaInfo = new chrome.cast.media.MediaInfo(streamUrl, 'application/x-mpegurl');
  mediaInfo.streamType = chrome.cast.media.StreamType.LIVE;
  mediaInfo.metadata = new chrome.cast.media.GenericMediaMetadata();
  mediaInfo.metadata.metadataType = chrome.cast.media.MetadataType.GENERIC;
  mediaInfo.metadata.title = 'Momcozy BM04 Baby Monitor';
  mediaInfo.metadata.subtitle = 'Live Camera Feed';

  const request = new chrome.cast.media.LoadRequest(mediaInfo);
  request.autoplay = true;

  session.loadMedia(request).then(
    function() {
      console.log("[Google Cast] Stream loaded onto Cast receiver successfully.");
      showToast('Live stream casting to TV!', 'success', 3000);
    },
    function(err) {
      console.error("[Google Cast] Error loading cast media:", err);
      const errDetail = (err && (err.description || err.message || JSON.stringify(err))) || String(err);
      showToast('Could not load stream on Cast device: ' + errDetail, 'error', 5000);
    }
  );
};

async function handleCastClick(event) {
  if (event) {
    event.stopPropagation();
    event.preventDefault();
  }

  console.log("[Google Cast] Cast button clicked. isSecureContext:", window.isSecureContext, "googleCastInitialized:", !!window.googleCastInitialized);

  // Pre-warm HLS pipeline so stream chunks are ready when TV connects
  fetch('/hls/stream.m3u8').catch(() => {});

  // 1. If currently in a Cast session, clicking ends session
  if (window.googleCastInitialized && window.cast && window.cast.framework) {
    try {
      const castContext = cast.framework.CastContext.getInstance();
      const session = castContext.getCurrentSession();
      if (session) {
        console.log("[Google Cast] Ending active Cast session...");
        castContext.endCurrentSession(true);
        showToast('Cast session ended', 'info', 2000);
        return;
      }
    } catch (e) {
      console.error("[Google Cast] Error inspecting/ending session:", e);
    }
  }

  // 2. If Google Cast SDK is initialized, open native Cast device picker
  if (window.googleCastInitialized && window.cast && window.cast.framework) {
    try {
      const castContext = cast.framework.CastContext.getInstance();
      console.log("[Google Cast] Triggering native cast dialog via CastContext.requestSession()...");
      await castContext.requestSession();
      console.log("[Google Cast] requestSession completed successfully.");
      return;
    } catch (err) {
      if (err === 'cancel' || (err && typeof err === 'string' && err.includes('cancel'))) {
        console.log("[Google Cast] User dismissed cast dialog.");
        return;
      }
      console.error("[Google Cast] requestSession failed:", err);
      showToast('Cast request failed: ' + (err.description || err.message || err), 'error', 5000);
      return;
    }
  }

  // 3. Cast SDK not initialized: log why and show actionable user error
  console.error("[Google Cast] Cast framework is not available.");
  if (!window.isSecureContext) {
    const httpsPort = window.serverPort ? (':' + window.serverPort) : (window.location.port ? (':' + window.location.port) : '');
    const httpsUrl = `https://${window.location.hostname}${httpsPort}/`;
    console.error(`[Google Cast] INSECURE ORIGIN: Chrome strictly disables browser-side Cast on HTTP (${window.location.origin}).`);
    console.error(`[Google Cast] Switch to HTTPS dashboard at: ${httpsUrl}`);
    showToast('Google Cast requires HTTPS. Open ', 'error', 12000, {
      linkUrl: httpsUrl,
      linkText: httpsUrl,
      linkSuffix: ' to cast.'
    });
  } else if (!window.cast) {
    console.error("[Google Cast] Google Cast Web SDK (cast_sender.js) did not load. Check network connection or script blockers.");
    showToast("Google Cast script could not be reached. Check network connection.", "error", 5000);
  } else {
    console.error("[Google Cast] CastContext could not be initialized or no devices were discovered yet.");
    showToast("Cast device discovery is still initializing. Ensure your Chromecast is on and on the same Wi-Fi.", "warn", 5000);
  }
}

function updateTimelapseUI(tl) {
  if (!tl) return;
  const badge = document.getElementById('timelapse-badge');
  const toggleBtn = document.getElementById('timelapse-toggle-btn');
  const watchBtn = document.getElementById('timelapse-watch-btn');
  const eraseBtn = document.getElementById('timelapse-erase-btn');
  const bufferedEl = document.getElementById('timelapse-buffered');
  const vlenEl = document.getElementById('timelapse-vlen');
  const storageEl = document.getElementById('timelapse-storage');
  const ratioSelect = document.getElementById('timelapse-ratio-select');

  if (bufferedEl) bufferedEl.textContent = tl.buffered_formatted || '0m 00s';
  if (vlenEl) vlenEl.textContent = tl.video_duration_formatted || '0:00';
  if (storageEl) storageEl.textContent = tl.disk_formatted || '0.0 MB';
  if (ratioSelect && tl.speed_ratio_val) ratioSelect.value = String(tl.speed_ratio_val);

  const evBadge = document.getElementById('timelapse-events-badge');
  if (evBadge) {
    const evCount = tl.motion_events_count || 0;
    evBadge.textContent = `${evCount} event${evCount === 1 ? '' : 's'}`;
    if (evCount > 0) {
      evBadge.style.background = 'rgba(245, 158, 11, 0.2)';
      evBadge.style.color = '#f59e0b';
      evBadge.style.border = '1px solid rgba(245, 158, 11, 0.4)';
    } else {
      evBadge.style.background = 'rgba(255, 255, 255, 0.08)';
      evBadge.style.color = 'var(--text-muted)';
      evBadge.style.border = '1px solid transparent';
    }
  }

  if (tl.is_recording) {
    if (badge) {
      badge.className = 'badge recording';
      badge.style.background = '#C53030';
      badge.style.color = '#FFFFFF';
      if (tl.last_session_type === 'auto') {
        setLabelWithMoon(badge, 'Recording ', '');
      } else {
        badge.textContent = 'Recording';
      }
    }
    if (toggleBtn) {
      toggleBtn.textContent = '⏹ Stop Recording';
      toggleBtn.className = 'btn';
    }
    if (eraseBtn) eraseBtn.disabled = true;
  } else {
    if (badge) {
      badge.className = 'badge';
      if (tl.sync_state === 'activating') {
        badge.style.background = '#EDE9FE';
        badge.style.color = '#7C3AED';
        setLabelWithMoon(badge, '', ` Activating (${Math.round(tl.sync_timer_remaining || 0)}s)`);
      } else {
        badge.style.background = '#EDF2F7';
        badge.style.color = 'var(--text-muted)';
        badge.textContent = tl.has_content ? 'Ready' : 'Stopped';
      }
    }
    if (toggleBtn) {
      if (tl.has_content) {
        toggleBtn.textContent = '▶ Resume Recording';
      } else {
        toggleBtn.textContent = '⏺ Start Recording';
      }
      toggleBtn.className = 'btn primary';
    }
    if (eraseBtn) eraseBtn.disabled = !tl.has_content;
  }

  const syncBtn = document.getElementById('timelapse-sync-nv-btn');
  if (syncBtn) {
    const syncOn = !!tl.sync_night_vision;
    if (syncOn) {
      if (tl.sync_state === 'activating') {
        const sec = Math.round(tl.sync_timer_remaining || 0);
        setLabelWithMoon(syncBtn, '', ` Night Vision Active (Starts in ${sec}s)`);
      } else if (tl.sync_state === 'stopping') {
        const sec = Math.round(tl.sync_timer_remaining || 0);
        syncBtn.textContent = `☀️ Day Mode Detected (Stops in ${sec}s)`;
      } else if (tl.is_recording && tl.last_session_type === 'auto') {
        setLabelWithMoon(syncBtn, 'Sync: Recording ', '');
      } else if (tl.night_vision_active) {
        setLabelWithMoon(syncBtn, '', ' Sync with Night Vision: Active');
      } else {
        setLabelWithMoon(syncBtn, '', ' Sync with Night Vision: ON');
      }
      if (tl.is_recording && tl.last_session_type === 'auto') {
        syncBtn.style.background = '#C53030';
        syncBtn.style.borderColor = '#9B2C2C';
        syncBtn.style.color = '#FFFFFF';
        syncBtn.style.fontWeight = '600';
      } else {
        syncBtn.style.background = '#EDE9FE';
        syncBtn.style.borderColor = '#C4B5FD';
        syncBtn.style.color = '#6D28D9';
        syncBtn.style.fontWeight = '600';
      }
    } else {
      setLabelWithMoon(syncBtn, '', ' Sync with Night Vision: OFF');
      syncBtn.style.background = '';
      syncBtn.style.borderColor = '';
      syncBtn.style.color = '';
      syncBtn.style.fontWeight = '';
    }
  }

  if (watchBtn) {
    watchBtn.disabled = !tl.has_content;
  }
}

async function changeTimelapseRatio(val) {
  try {
    const res = await fetch('/api/timelapse/ratio', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ratio: parseInt(val, 10) })
    });
    const d = await res.json();
    if (d.success) {
      updateTimelapseUI(d.timelapse);
    } else {
      showToast('Failed to change speed ratio: ' + (d.error || ''), 'error', 3500);
    }
  } catch (e) {
    showToast('Network error setting speed ratio: ' + e.message, 'error', 3500);
  }
}

async function toggleTimelapseRecord() {
  const toggleBtn = document.getElementById('timelapse-toggle-btn');
  const btnText = toggleBtn ? (toggleBtn.textContent || '') : '';
  const isCurrentlyRecording = btnText.includes('Stop');
  const isResume = btnText.includes('Resume');
  const endpoint = isCurrentlyRecording ? '/api/timelapse/stop' : '/api/timelapse/start';

  try {
    if (toggleBtn) toggleBtn.disabled = true;
    const res = await fetch(endpoint, { method: 'POST' });
    const d = await res.json();
    if (d.success) {
      let msg = 'Timelapse recording stopped.';
      if (!isCurrentlyRecording) {
        msg = isResume ? 'Timelapse recording resumed.' : 'New timelapse recording started!';
      }
      showToast(msg, 'success', 3500);
      updateTimelapseUI(d.timelapse);
    } else {
      showToast('Timelapse error: ' + (d.error || 'Failed to toggle recording'), 'error', 4000);
    }
  } catch (e) {
    showToast('Network error toggling timelapse: ' + e.message, 'error', 4000);
  } finally {
    if (toggleBtn) toggleBtn.disabled = false;
    updateStatus();
  }
}

async function eraseTimelapse() {
  if (!confirm("Are you sure you want to erase all recorded timelapse footage? This cannot be undone.")) {
    return;
  }
  const eraseBtn = document.getElementById('timelapse-erase-btn');
  try {
    if (eraseBtn) eraseBtn.disabled = true;
    const res = await fetch('/api/timelapse/clear', { method: 'POST' });
    const d = await res.json();
    if (d.success) {
      showToast('Timelapse footage erased.', 'info', 3000);
      updateTimelapseUI(d.timelapse);
    } else {
      showToast('Failed to erase timelapse: ' + (d.error || ''), 'error', 4000);
    }
  } catch (e) {
    showToast('Network error erasing timelapse: ' + e.message, 'error', 4000);
  } finally {
    updateStatus();
  }
}

async function toggleNightVisionSync() {
  const syncBtn = document.getElementById('timelapse-sync-nv-btn');
  try {
    if (syncBtn) syncBtn.disabled = true;
    const res = await fetch('/api/timelapse/sync_night_vision', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const d = await res.json();
    if (d.success) {
      showToast(d.sync_night_vision ? '🌙 Night Vision timelapse sync ENABLED.' : '🌙 Night Vision timelapse sync DISABLED.', 'info', 4000);
      if (d.timelapse) updateTimelapseUI(d.timelapse);
    } else {
      showToast('Failed to toggle Night Vision sync: ' + (d.error || ''), 'error', 4000);
    }
  } catch (e) {
    showToast('Network error setting Night Vision sync: ' + e.message, 'error', 4000);
  } finally {
    if (syncBtn) syncBtn.disabled = false;
    updateStatus();
  }
}

function openTimelapsePlayback() {
  window.open('/timelapse', '_blank');
}

setInterval(updateStatus, 3000);
updateStatus();
initPlayer();
