const video = document.getElementById('timelapse-video');
const loading = document.getElementById('loading');
const errorBox = document.getElementById('error-box');
const dlBtn = document.getElementById('dl-btn');

function canPlayHevc() {
  const v = document.createElement('video');
  const types = [
    'video/mp4; codecs="hvc1.1.6.L150.B0"',
    'video/mp4; codecs="hev1.1.6.L150.B0"',
    'video/mp4; codecs="hvc1.1.6.L93.B0"',
    'video/mp4; codecs="hvc1"'
  ];
  for (const t of types) {
    const can = v.canPlayType(t);
    if (can === 'probably' || can === 'maybe') return true;
  }
  return false;
}

let activeCodec = canPlayHevc() ? 'hevc' : 'h264';
let fallbackAttempted = false;
let initialPlaySucceeded = false;
let recoveryAttempts = 0;
let currentRatio = 60;

function updateDownloadLinks() {
  if (dlBtn) {
    dlBtn.href = `/api/timelapse/video.mp4?codec=hevc&ratio=${currentRatio}&download=1`;
  }
  const dlH264 = document.getElementById('dl-h264-btn');
  if (dlH264) {
    dlH264.href = `/api/timelapse/video.mp4?codec=h264&ratio=${currentRatio}&download=1`;
  }
}

function loadPlayback(codec) {
  activeCodec = codec;
  recoveryAttempts = 0;
  initialPlaySucceeded = false;
  loading.style.display = 'flex';
  errorBox.style.display = 'none';
  const url = `/api/timelapse/video.mp4?codec=${codec}&ratio=${currentRatio}&t=${Date.now()}`;
  video.src = url;
  updateDownloadLinks();
  video.load();
  video.play().catch(function() {});
}

video.onloadeddata = function() {
  loading.style.display = 'none';
  initialPlaySucceeded = true;
};

video.onplaying = function() {
  loading.style.display = 'none';
  initialPlaySucceeded = true;
  recoveryAttempts = 0;
};

video.onerror = function(e) {
  const err = video.error;
  console.warn('[Timelapse Player] Video error with codec ' + activeCodec, err ? { code: err.code, message: err.message } : e);

  // Code 1: MEDIA_ERR_ABORTED - User / browser aborted previous range request while scrubbing
  if (err && err.code === 1) {
    return;
  }

  // If HEVC already successfully loaded metadata / played and recovery is within retry limits
  if (initialPlaySucceeded && recoveryAttempts < 10) {
    recoveryAttempts++;
    console.info('[Timelapse Player] Recovering playback (attempt ' + recoveryAttempts + '/10)...');
    const dur = video.duration || 1.0;
    const cur = Math.max(0.05, Math.min(dur - 0.05, video.currentTime || 0.05));
    const onLoadedMeta = function() {
      video.currentTime = cur;
      video.play().catch(function() {});
    };
    video.addEventListener('loadedmetadata', onLoadedMeta, { once: true });
    video.load();
    return;
  }

  if (activeCodec === 'hevc' && !fallbackAttempted) {
    console.info('[Timelapse Player] Retrying with H.264 fallback...');
    fallbackAttempted = true;
    loadPlayback('h264');
    return;
  }
  loading.style.display = 'none';
  errorBox.style.display = 'flex';
};

let motionEvents = [];

function formatTimeSec(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}:${rem < 10 ? '0' : ''}${rem}`;
}

function getEventStartSec(ev) {
  return (video.duration && ev.percent !== undefined)
    ? (ev.percent / 100) * video.duration
    : ev.video_sec;
}

function formatRangeTooltip(ev) {
  const hasDuration = ev.duration_sec !== undefined && ev.duration_sec >= 1;
  const vStart = ev.video_formatted || '0:00';
  const vEnd = ev.end_video_formatted || vStart;
  const vSpan = vEnd !== vStart ? `${vStart}–${vEnd}` : vStart;
  if (hasDuration && ev.duration_formatted) {
    return `🏃 ${ev.label} • ${ev.time_str} (${ev.duration_formatted} • ${vSpan})`;
  }
  return `🏃 ${ev.label} • ${ev.time_str} (${vSpan})`;
}

function renderTimelineMarkers() {
  const container = document.getElementById('timeline-markers');
  if (!container) return;
  container.textContent = '';
  const counter = document.getElementById('events-counter-badge');
  if (counter) {
    counter.textContent = `🏃 ${motionEvents.length} Motion Range${motionEvents.length === 1 ? '' : 's'}`;
  }

  motionEvents.forEach((ev) => {
    const el = document.createElement('div');
    el.className = 'timeline-marker';
    const startPct = ev.percent !== undefined ? ev.percent : 0;
    const endPct = ev.end_percent !== undefined ? Math.max(startPct, ev.end_percent) : startPct;
    const widthPct = Math.max(0, endPct - startPct);
    el.style.left = `clamp(0px, ${startPct}%, calc(100% - 6px))`;
    el.style.width = `${widthPct}%`;

    const updateTooltip = (e) => {
      const tt = document.getElementById('timeline-tooltip');
      const bar = document.getElementById('timeline-bar');
      if (!tt) return;
      tt.textContent = formatRangeTooltip(ev);
      let hoverPct = (startPct + endPct) / 2;
      if (bar && e && typeof e.clientX === 'number') {
        const barRect = bar.getBoundingClientRect();
        if (barRect.width > 0) {
          hoverPct = ((e.clientX - barRect.left) / barRect.width) * 100;
        }
      }
      tt.style.left = `${Math.max(8, Math.min(92, hoverPct))}%`;
      tt.style.display = 'block';
    };

    el.addEventListener('mouseenter', updateTooltip);
    el.addEventListener('mousemove', updateTooltip);
    el.addEventListener('mouseleave', () => {
      const tt = document.getElementById('timeline-tooltip');
      if (tt) tt.style.display = 'none';
    });

    const shouldSnapToStart = (e) => {
      const rect = el.getBoundingClientRect();
      return rect.width <= 20 || (e.clientX - rect.left) <= 12;
    };

    el.addEventListener('mousedown', (e) => {
      if (shouldSnapToStart(e)) {
        e.stopPropagation();
        seekToSec(getEventStartSec(ev));
      }
    });
    el.addEventListener('click', (e) => {
      if (shouldSnapToStart(e)) {
        e.stopPropagation();
        seekToSec(getEventStartSec(ev));
      }
    });
    container.appendChild(el);
  });
}

function seekToSec(sec) {
  if (video.duration) {
    const target = Math.max(0.05, Math.min(video.duration - 0.05, sec));
    video.currentTime = target;
    video.play().catch(() => {});
  }
}

function seekFromTimeline(e) {
  const bar = document.getElementById('timeline-bar');
  if (!bar || !video.duration) return;
  const rect = bar.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const target = Math.max(0.05, Math.min(video.duration - 0.05, ratio * video.duration));
  video.currentTime = target;
}

const timelineWrapper = document.getElementById('timeline-wrapper');
let isDraggingTimeline = false;
let seekRaf = null;

function throttledTimelineSeek(e) {
  if (seekRaf) cancelAnimationFrame(seekRaf);
  seekRaf = requestAnimationFrame(() => {
    seekFromTimeline(e);
  });
}

if (timelineWrapper) {
  timelineWrapper.addEventListener('mousedown', (e) => {
    isDraggingTimeline = true;
    throttledTimelineSeek(e);
  });
  window.addEventListener('mousemove', (e) => {
    if (isDraggingTimeline) {
      throttledTimelineSeek(e);
    }
  });
  window.addEventListener('mouseup', () => {
    isDraggingTimeline = false;
  });
}

function jumpToNextEvent() {
  if (!motionEvents.length || !video.duration) return;
  const cur = video.currentTime;
  const next = motionEvents.find(ev => getEventStartSec(ev) > cur + 0.8);
  if (next) {
    seekToSec(getEventStartSec(next));
  } else if (motionEvents.length) {
    seekToSec(getEventStartSec(motionEvents[0]));
  }
}

function jumpToPrevEvent() {
  if (!motionEvents.length || !video.duration) return;
  const cur = video.currentTime;
  const prev = [...motionEvents].reverse().find(ev => getEventStartSec(ev) < cur - 0.8);
  if (prev) {
    seekToSec(getEventStartSec(prev));
  } else if (motionEvents.length) {
    seekToSec(getEventStartSec(motionEvents[motionEvents.length - 1]));
  }
}

window.addEventListener('keydown', (e) => {
  if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT')) return;
  if (e.key === 'n' || e.key === 'N') jumpToNextEvent();
  else if (e.key === 'p' || e.key === 'P') jumpToPrevEvent();
});

video.addEventListener('timeupdate', () => {
  const curEl = document.getElementById('current-time-text');
  const durEl = document.getElementById('duration-time-text');
  const progress = document.getElementById('timeline-progress');
  const thumb = document.getElementById('timeline-thumb');
  if (curEl) curEl.textContent = formatTimeSec(video.currentTime);
  if (durEl && video.duration) durEl.textContent = formatTimeSec(video.duration);
  if (video.duration) {
    const pct = Math.min(100, Math.max(0, (video.currentTime / video.duration) * 100));
    if (progress) progress.style.width = `${pct}%`;
    if (thumb) thumb.style.left = `${pct}%`;
  }
});

video.addEventListener('loadedmetadata', () => {
  const durEl = document.getElementById('duration-time-text');
  if (durEl && video.duration) durEl.textContent = formatTimeSec(video.duration);
});

function setSpeed(rate, btn) {
  video.playbackRate = rate;
  document.querySelectorAll('.speed-btn').forEach(function(b) {
    b.classList.remove('active');
  });
  if (btn) btn.classList.add('active');
}

async function reloadVideo() {
  fallbackAttempted = false;
  initialPlaySucceeded = false;
  recoveryAttempts = 0;
  try {
    const res = await fetch('/api/timelapse/status?scan=1');
    const st = await res.json();
    if (st.events) {
      motionEvents = st.events;
      renderTimelineMarkers();
    }
  } catch (e) {}
  loadPlayback(activeCodec);
}

async function initPlayer() {
  try {
    const res = await fetch('/api/timelapse/status?scan=1');
    const st = await res.json();
    if (st.speed_ratio_val) {
      currentRatio = st.speed_ratio_val;
      const badge = document.getElementById('player-ratio-badge');
      if (badge) badge.textContent = `1:${currentRatio}`;
    }
    if (st.events) {
      motionEvents = st.events;
      renderTimelineMarkers();
    }
  } catch (e) {}
  loadPlayback(activeCodec);
}

initPlayer();
