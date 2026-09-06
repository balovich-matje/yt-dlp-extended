/* yt-dlp-extended — single-page frontend. No build step, no dependencies. */
'use strict';

const $ = (id) => document.getElementById(id);
const state = {
  meta: null,          // yt-dlp metadata for the pasted URL
  mode: 'full',        // download mode
  jobs: new Map(),
  library: [],
  containers: {},
  presets: [],
  probe: null,         // ffprobe result for the convert source
  sizeMode: 'quality',
  slider: null,
  estimateSeq: 0,
};

/* --------------------------------------------------------------- helpers */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body instanceof FormData
      ? undefined
      : { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) {
    const detail = data && data.detail
      ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
      : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body) });

function fmtBytes(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  if (n < 1000) return `${n} B`;
  const units = ['kB', 'MB', 'GB', 'TB'];
  let v = n / 1000, i = 0;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return `${v.toFixed(v < 10 ? 2 : 1)} ${units[i]}`;
}

function fmtBitrate(bps) {
  if (!bps) return '—';
  return bps >= 1e6 ? `${(bps / 1e6).toFixed(2)} Mbps` : `${Math.round(bps / 1000)} kbps`;
}

function fmtDuration(sec) {
  if (sec === null || sec === undefined || Number.isNaN(sec)) return '—';
  sec = Math.max(0, sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
           : `${m}:${String(s).padStart(2, '0')}`;
}

function fmtTimestamp(sec) {
  sec = Math.max(0, sec || 0);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:` +
         `${s.toFixed(3).padStart(6, '0')}`;
}

/** Mirror of the server's parser so the timeline can track typed input. */
function parseTimestamp(value) {
  if (!value) return null;
  value = String(value).trim();
  if (/^\d+(\.\d+)?$/.test(value)) return parseFloat(value);
  const m = /^(?:(\d+):)?([0-5]?\d):([0-5]?\d)(?:[.,](\d{1,3}))?$/.exec(value);
  if (!m) return null;
  return (+(m[1] || 0)) * 3600 + (+m[2]) * 60 + (+m[3]) +
         (m[4] ? parseInt(m[4].padEnd(3, '0'), 10) / 1000 : 0);
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = $('toast');
  el.textContent = message;
  el.classList.toggle('err', isError);
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), isError ? 9000 : 3500);
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

/* ------------------------------------------------------------------ tabs */

$('tabs').addEventListener('click', (ev) => {
  const btn = ev.target.closest('.tab');
  if (!btn) return;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t === btn));
  document.querySelectorAll('.panel').forEach((p) => {
    p.classList.toggle('active', p.id === `tab-${btn.dataset.tab}`);
  });
});

/* ---------------------------------------------------------------- health */

async function loadHealth() {
  try {
    const health = await api('/api/health');
    $('health').innerHTML = Object.entries(health).map(([name, info]) => `
      <span class="bin" title="${info.version ? esc(info.version) : 'not found on PATH'}">
        <span class="dot ${info.ok ? 'ok' : ''}"></span>${name}
      </span>`).join('');
    const missing = Object.entries(health).filter(([, i]) => !i.ok).map(([n]) => n);
    if (missing.length) {
      toast(`Not found on PATH: ${missing.join(', ')}. Set an explicit path under Settings.`, true);
    }
  } catch (err) {
    $('health').textContent = `health check failed: ${err.message}`;
  }
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* =================================================== STAGE 1: DOWNLOAD == */

const urlInput = $('url');

urlInput.addEventListener('paste', () => setTimeout(fetchMetadata, 50));
urlInput.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') fetchMetadata(); });
$('fetch-meta').addEventListener('click', fetchMetadata);

let metaSeq = 0;
async function fetchMetadata() {
  const url = urlInput.value.trim();
  if (!url) return;
  const seq = ++metaSeq;
  const status = $('meta-status');
  status.className = 'status';
  $('fetch-meta').disabled = true;

  // Some extractors are slow — a Pinterest board pages through every pin before
  // answering — so count up rather than showing a frozen-looking spinner.
  const started = Date.now();
  status.textContent = 'Fetching metadata…';
  const ticker = setInterval(() => {
    const secs = Math.round((Date.now() - started) / 1000);
    status.textContent = secs < 5 ? 'Fetching metadata…'
      : `Fetching metadata… ${secs}s (boards and playlists can take a while)`;
  }, 1000);

  try {
    const meta = await post('/api/metadata', { url });
    if (seq !== metaSeq) return;              // a newer paste won the race
    state.meta = meta;
    renderMeta(meta);
    status.textContent = '';
  } catch (err) {
    if (seq !== metaSeq) return;
    state.meta = null;
    $('meta-card').classList.add('hidden');
    status.className = 'status err';
    status.textContent = err.message;
  } finally {
    clearInterval(ticker);
    if (seq === metaSeq) $('fetch-meta').disabled = false;
  }
}

function renderMeta(meta) {
  $('meta-card').classList.remove('hidden');
  $('meta-title').textContent = meta.title;
  $('meta-uploader').textContent = meta.uploader || '';
  $('meta-duration').textContent = meta.duration ? fmtDuration(meta.duration) : '';
  $('meta-extractor').textContent = meta.extractor || '';
  const thumb = $('meta-thumb');
  if (meta.thumbnail) { thumb.src = meta.thumbnail; thumb.classList.remove('hidden'); }
  else thumb.classList.add('hidden');
  renderFormats();
}

$('dl-mode').addEventListener('click', (ev) => {
  const btn = ev.target.closest('.seg');
  if (!btn) return;
  state.mode = btn.dataset.mode;
  document.querySelectorAll('#dl-mode .seg')
    .forEach((s) => s.classList.toggle('active', s === btn));
  $('audiofmt-field').classList.toggle('hidden', state.mode !== 'audio');
  $('container-field').classList.toggle('hidden', state.mode === 'audio');
  renderFormats();
});

function formatsFor(mode) {
  const all = state.meta?.formats || [];
  if (mode === 'audio') return all.filter((f) => f.has_audio && !f.has_video);
  if (mode === 'video') return all.filter((f) => f.has_video && !f.has_audio);
  return all.filter((f) => f.has_video);   // full: any video, audio merged in
}

function describeFormat(f, mode) {
  const bits = [];
  if (f.has_video) bits.push(f.resolution || `${f.height}p`);
  if (f.fps) bits.push(`${Math.round(f.fps)}fps`);
  bits.push(f.ext);
  if (f.has_video) bits.push(f.vcodec);
  if (mode === 'audio' || !f.has_video) {
    if (f.acodec) bits.push(f.acodec);
    if (f.abr) bits.push(`${Math.round(f.abr)}k`);
  } else {
    bits.push(f.has_audio ? 'with audio' : 'video only');
  }
  if (f.filesize) bits.push(`~${fmtBytes(f.filesize)}`);
  if (f.dynamic_range && f.dynamic_range !== 'SDR') bits.push(f.dynamic_range);
  // Codecs are unknown for some sources (Pinterest's direct MP4s), which would
  // otherwise leave an empty ` ·  · ` gap in the label.
  return `${bits.filter(Boolean).join(' · ')}  [${f.format_id}]`;
}

function renderFormats() {
  const select = $('format-select');
  const list = formatsFor(state.mode);
  const auto = state.mode === 'audio' ? 'Best audio (automatic)'
             : state.mode === 'video' ? 'Best video (automatic)'
             : 'Best available (automatic)';
  select.innerHTML = `<option value="">${auto}</option>` + list.map((f) =>
    `<option value="${esc(f.format_id)}" data-has-audio="${f.has_audio}">${esc(describeFormat(f, state.mode))}</option>`
  ).join('');
  updateFormatNote();
}

$('format-select').addEventListener('change', updateFormatNote);

function updateFormatNote() {
  const select = $('format-select');
  const opt = select.selectedOptions[0];
  const note = $('format-note');
  if (!opt || !select.value) {
    note.textContent = state.mode === 'full'
      ? 'yt-dlp will pick the best video and best audio and merge them.'
      : '';
    return;
  }
  const hasAudio = opt.dataset.hasAudio === 'true';
  if (state.mode === 'full') {
    note.textContent = hasAudio
      ? 'This format already contains audio, so no merge is needed.'
      : 'Video-only format — the best audio track will be merged in with ffmpeg.';
  } else if (state.mode === 'video') {
    note.textContent = 'The result will have no audio track.';
  } else {
    note.textContent = '';
  }
}

$('start-download').addEventListener('click', async () => {
  if (!state.meta) return;
  const select = $('format-select');
  const opt = select.selectedOptions[0];
  const btn = $('start-download');
  btn.disabled = true;
  try {
    await post('/api/download', {
      url: state.meta.url,
      mode: state.mode,
      format_id: select.value || null,
      format_has_audio: opt ? opt.dataset.hasAudio === 'true' : false,
      container: $('merge-container').value,
      audio_format: $('audio-format').value,
      title: state.meta.title,
      thumbnail: state.meta.thumbnail,
    });
    toast('Download started.');
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

/* ==================================================== STAGE 2: CONVERT == */

async function loadContainers() {
  const data = await api('/api/containers');
  state.containers = data.containers;
  state.presets = data.presets;
  state.minVideoBitrate = data.min_video_bitrate;
  $('container').innerHTML = Object.entries(data.containers)
    .map(([key, spec]) => `<option value="${key}">${esc(spec.label)}</option>`).join('');
  $('preset').innerHTML = data.presets
    .map((p) => `<option value="${p}"${p === 'medium' ? ' selected' : ''}>${p}</option>`).join('');
  onContainerChange();
}

$('convert-source').addEventListener('change', () => loadSource($('convert-source').value));

$('upload-input').addEventListener('change', async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const status = $('source-status');
  status.className = 'status';
  status.textContent = `Uploading ${file.name}…`;
  const form = new FormData();
  form.append('file', file);
  try {
    const row = await api('/api/upload', { method: 'POST', body: form });
    await loadLibrary();
    $('convert-source').value = row.id;
    await loadSource(row.id);
    status.textContent = `Uploaded ${row.name}.`;
  } catch (err) {
    status.className = 'status err';
    status.textContent = err.message;
  } finally {
    ev.target.value = '';
  }
});

async function loadSource(fileId) {
  if (!fileId) {
    $('convert-card').classList.add('hidden');
    state.probe = null;
    return;
  }
  const status = $('source-status');
  status.className = 'status';
  status.textContent = 'Probing file…';
  try {
    const probe = await api(`/api/files/${fileId}/probe`);
    state.probe = probe;
    $('convert-card').classList.remove('hidden');

    const preview = $('preview');
    preview.src = `/api/files/${fileId}/raw`;
    preview.load();

    $('trim-start').value = '00:00:00.000';
    $('trim-end').value = probe.duration ? fmtTimestamp(probe.duration) : '';
    $('tl-dur').textContent = probe.duration ? fmtTimestamp(probe.duration) : 'unknown duration';
    $('output-name').value = '';
    $('output-name').placeholder = `${probe.name.replace(/\.[^.]+$/, '')}_converted`;

    const v = probe.video;
    status.innerHTML = [
      probe.duration ? fmtDuration(probe.duration) : 'unknown length',
      fmtBytes(probe.size),
      v ? `${v.width}×${v.height} ${v.codec}${v.fps ? ` @ ${v.fps.toFixed(2)}fps` : ''}` : 'no video',
      probe.audio ? `${probe.audio.codec} ${probe.audio.channels}ch` : 'no audio',
    ].map(esc).join('  ·  ');

    // A source with no audio makes the audio controls meaningless.
    $('abr-field').classList.toggle('hidden', !probe.has_audio);
    onContainerChange();
    drawTimeline();
    refreshEstimate();
  } catch (err) {
    state.probe = null;
    $('convert-card').classList.add('hidden');
    status.className = 'status err';
    status.textContent = err.message;
  }
}

/* ------------------------------------------------------- timeline & trim */

function trimSeconds() {
  const dur = state.probe?.duration ?? null;
  let start = parseTimestamp($('trim-start').value) ?? 0;
  let end = parseTimestamp($('trim-end').value);
  if (end === null && dur !== null) end = dur;
  return { start, end, dur };
}

function drawTimeline() {
  const { start, end, dur } = trimSeconds();
  if (!dur) return;
  const clamp = (v) => Math.max(0, Math.min(100, (v / dur) * 100));
  const a = clamp(start), b = clamp(end ?? dur);
  const lo = Math.min(a, b), hi = Math.max(a, b);
  $('tl-selection').style.left = `${lo}%`;
  $('tl-selection').style.width = `${hi - lo}%`;
  $('tl-in').style.left = `${a}%`;
  $('tl-out').style.left = `${b}%`;
}

function updateTrimReadout() {
  const { start, end, dur } = trimSeconds();
  const err = $('trim-error');
  const rawStart = parseTimestamp($('trim-start').value);
  const rawEnd = parseTimestamp($('trim-end').value);

  let problem = null;
  if ($('trim-start').value && rawStart === null) problem = 'Start is not a valid HH:MM:SS.mmm timestamp.';
  else if ($('trim-end').value && rawEnd === null) problem = 'End is not a valid HH:MM:SS.mmm timestamp.';
  else if (end !== null && end <= start) problem = 'The end timestamp must be after the start.';
  else if (dur && start >= dur) problem = `Start is past the source duration (${fmtTimestamp(dur)}).`;
  else if (dur && end !== null && end > dur + 0.25) problem = `End is past the source duration (${fmtTimestamp(dur)}).`;

  err.classList.toggle('hidden', !problem);
  err.textContent = problem || '';
  $('start-convert').disabled = !!problem;
  $('trim-duration').textContent = problem || end === null
    ? '—' : `${fmtTimestamp(end - start)}  (${fmtDuration(end - start)})`;
  drawTimeline();
  return !problem;
}

['trim-start', 'trim-end'].forEach((id) => {
  $(id).addEventListener('input', () => { if (updateTrimReadout()) refreshEstimate(); });
});

$('set-in').addEventListener('click', (ev) => {
  ev.preventDefault();
  $('trim-start').value = fmtTimestamp($('preview').currentTime);
  if (updateTrimReadout()) refreshEstimate();
});
$('set-out').addEventListener('click', (ev) => {
  ev.preventDefault();
  $('trim-end').value = fmtTimestamp($('preview').currentTime);
  if (updateTrimReadout()) refreshEstimate();
});

$('preview').addEventListener('timeupdate', () => {
  const dur = state.probe?.duration;
  if (!dur) return;
  const pct = Math.max(0, Math.min(100, ($('preview').currentTime / dur) * 100));
  $('tl-playhead').style.left = `${pct}%`;
  $('tl-time').textContent = fmtTimestamp($('preview').currentTime);
});

// Dragging the in/out handles, plus click-to-seek on the bare track.
(function setupTimelineDrag() {
  const timeline = $('timeline');
  let dragging = null;

  const posToSeconds = (clientX) => {
    const rect = timeline.getBoundingClientRect();
    const dur = state.probe?.duration || 0;
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return ratio * dur;
  };

  timeline.addEventListener('pointerdown', (ev) => {
    if (!state.probe?.duration) return;
    const handle = ev.target.closest('.tl-handle');
    if (handle) {
      dragging = handle.id === 'tl-in' ? 'start' : 'end';
      timeline.setPointerCapture(ev.pointerId);
    } else {
      // Clicking the track scrubs the player rather than moving a marker.
      $('preview').currentTime = posToSeconds(ev.clientX);
    }
  });

  timeline.addEventListener('pointermove', (ev) => {
    if (!dragging) return;
    const seconds = posToSeconds(ev.clientX);
    const other = dragging === 'start'
      ? parseTimestamp($('trim-end').value)
      : parseTimestamp($('trim-start').value);
    // Keep the handles from crossing; 0.05s keeps the window non-empty.
    let value = seconds;
    if (dragging === 'start' && other !== null) value = Math.min(value, other - 0.05);
    if (dragging === 'end' && other !== null) value = Math.max(value, other + 0.05);
    $(dragging === 'start' ? 'trim-start' : 'trim-end').value =
      fmtTimestamp(Math.max(0, value));
    updateTrimReadout();
  });

  const stop = (ev) => {
    if (!dragging) return;
    dragging = null;
    try { timeline.releasePointerCapture(ev.pointerId); } catch { /* already released */ }
    if (updateTrimReadout()) refreshEstimate();
  };
  timeline.addEventListener('pointerup', stop);
  timeline.addEventListener('pointercancel', stop);
})();

/* ------------------------------------------------------------ output opts */

function onContainerChange() {
  const key = $('container').value;
  const spec = state.containers[key];
  if (!spec) return;
  const isVideo = spec.kind === 'video';
  const isGif = spec.kind === 'gif';

  $('vcodec-field').classList.toggle('hidden', !isVideo);
  $('preset-field').classList.toggle('hidden', !isVideo);
  $('scale-field').classList.toggle('hidden', spec.kind === 'audio');
  $('fps-field').classList.toggle('hidden', spec.kind === 'audio');
  $('abr-field').classList.toggle('hidden', isGif || !state.probe?.has_audio);

  if (isVideo) {
    $('video-codec').innerHTML = spec.video_codecs
      .map((c) => `<option value="${c}">${c}</option>`).join('');
  }
  if (spec.crf_default !== null && spec.crf_default !== undefined) {
    const crf = $('crf');
    crf.min = spec.crf_min; crf.max = spec.crf_max; crf.value = spec.crf_default;
    $('crf-value').textContent = spec.crf_default;
  }
  // Size targeting has no meaning for GIF; the estimator says so too.
  const sizeBtn = document.querySelector('#size-mode .seg[data-mode="size"]');
  sizeBtn.disabled = isGif;
  sizeBtn.style.opacity = isGif ? 0.4 : 1;
  if (isGif && state.sizeMode === 'size') selectSizeMode('quality');
  $('crf-controls').classList.toggle('hidden', state.sizeMode === 'size' || isGif || spec.kind === 'audio');
  refreshEstimate();
}

$('container').addEventListener('change', onContainerChange);
// Wrapped in an arrow so the reference resolves at event time: refreshEstimate
// is a const declared further down, and naming it directly here would read it
// inside its temporal dead zone and abort the whole script.
['video-codec', 'preset', 'scale-height', 'fps', 'audio-bitrate']
  .forEach((id) => $(id).addEventListener('change', () => refreshEstimate()));

$('crf').addEventListener('input', () => {
  $('crf-value').textContent = $('crf').value;
  refreshEstimate();
});

$('size-mode').addEventListener('click', (ev) => {
  const btn = ev.target.closest('.seg');
  if (!btn || btn.disabled) return;
  selectSizeMode(btn.dataset.mode);
});

function selectSizeMode(mode) {
  state.sizeMode = mode;
  document.querySelectorAll('#size-mode .seg')
    .forEach((s) => s.classList.toggle('active', s.dataset.mode === mode));
  const spec = state.containers[$('container').value] || {};
  $('size-controls').classList.toggle('hidden', mode !== 'size');
  $('crf-controls').classList.toggle('hidden',
    mode === 'size' || spec.kind === 'gif' || spec.kind === 'audio');
  refreshEstimate();
}

// Slider and numeric input are two views of one value.
$('size-slider').addEventListener('input', () => {
  $('size-number').value = $('size-slider').value;
  refreshEstimate();
});
$('size-number').addEventListener('input', () => {
  const v = parseFloat($('size-number').value);
  if (!Number.isNaN(v)) $('size-slider').value = v;
  refreshEstimate();
});

function applySliderRange(range) {
  if (!range) return;
  const slider = $('size-slider');
  const number = $('size-number');
  const first = state.slider === null;
  state.slider = range;
  slider.min = range.min; slider.max = range.max; slider.step = range.step;
  number.min = range.min; number.step = range.step;
  if (first || parseFloat(number.value) > range.max || parseFloat(number.value) < range.min) {
    const v = first ? range.default : Math.min(range.max, Math.max(range.min, parseFloat(number.value) || range.default));
    slider.value = v; number.value = v;
  }
  const floorKbps = Math.round((state.minVideoBitrate || 100000) / 1000);
  $('slider-hint').textContent =
    `Range ${range.min} – ${range.max} MB: the floor is a ${floorKbps} kbps video encode, ` +
    `the ceiling is roughly what this span occupies in the source file.`;
}

/* -------------------------------------------------------------- estimate */

function convertRequest() {
  const scale = $('scale-height').value;
  const fps = $('fps').value;
  return {
    file_id: $('convert-source').value,
    container: $('container').value,
    start: $('trim-start').value || null,
    end: $('trim-end').value || null,
    size_mode: state.sizeMode,
    target_mb: state.sizeMode === 'size' ? parseFloat($('size-number').value) : null,
    audio_bitrate: parseInt($('audio-bitrate').value, 10),
    crf: parseInt($('crf').value, 10),
    scale_height: scale ? parseInt(scale, 10) : null,
    fps: fps ? parseFloat(fps) : null,
  };
}

const refreshEstimate = debounce(async () => {
  if (!state.probe || !$('convert-source').value) return;
  if (!updateTrimReadout()) return;
  const seq = ++state.estimateSeq;
  try {
    const data = await post('/api/estimate', convertRequest());
    if (seq !== state.estimateSeq) return;
    applySliderRange(data.slider);
    renderEstimate(data);
  } catch (err) {
    if (seq !== state.estimateSeq) return;
    $('estimate-panel').innerHTML =
      `<div class="est-warn severe">${esc(err.message)}</div>`;
  }
}, 220);

function renderEstimate(data) {
  const e = data.estimate;
  const items = [];
  const predicted = e.predicted_bytes;

  items.push(['Trimmed length', fmtTimestamp(data.trim.duration)]);
  items.push([
    e.approximate ? 'Predicted size (rough)' : 'Predicted size',
    predicted ? fmtBytes(predicted) : 'unknown until encoded',
  ]);
  if (e.target_bytes) items.push(['Target', fmtBytes(e.target_bytes)]);
  if (e.video_bitrate) {
    items.push([e.mode === 'size' ? 'Video bitrate' : 'Video bitrate (est.)',
                fmtBitrate(e.video_bitrate)]);
  }
  if (e.crf !== undefined && e.crf !== null) items.push(['CRF', String(e.crf)]);
  if (e.audio_bitrate) items.push(['Audio bitrate', fmtBitrate(e.audio_bitrate)]);
  if (e.width && e.height) items.push(['Resolution', `${e.width}×${e.height}`]);
  if (e.fps) items.push(['Frame rate', `${(+e.fps).toFixed(2).replace(/\.00$/, '')} fps`]);
  if (e.bpp) items.push(['Bits / pixel', e.bpp.toFixed(3)]);

  const tag = e.quality === 'ok' ? '' :
    `<span class="quality-tag ${e.quality}">${e.quality === 'severe' ? 'quality will suffer badly' : 'quality will suffer'}</span>`;

  const warnings = (e.warnings || [])
    .map((w) => `<div class="est-warn ${e.quality === 'severe' ? 'severe' : 'warn'}">⚠ ${esc(w)}</div>`).join('');
  const notes = (e.notes || [])
    .map((n) => `<div class="est-warn note">${esc(n)}</div>`).join('');

  $('estimate-panel').innerHTML = `
    <div class="row spread" style="margin-bottom:12px">
      <strong>Estimate${tag}</strong>
      <span class="hint" style="margin:0">${e.mode === 'size' ? 'two-pass, size-targeted' : 'single-pass CRF'}</span>
    </div>
    <div class="est-grid">
      ${items.map(([k, v]) => `<div class="est-item"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join('')}
    </div>
    ${warnings}${notes}`;
}

$('start-convert').addEventListener('click', async () => {
  const btn = $('start-convert');
  if (!updateTrimReadout()) return;
  btn.disabled = true;
  try {
    await post('/api/convert', {
      ...convertRequest(),
      video_codec: $('video-codec').value || null,
      preset: $('preset').value,
      output_name: $('output-name').value.trim() || null,
    });
    toast('Encode started.');
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

/* ================================================================ LIBRARY */

async function loadLibrary() {
  state.library = await api('/api/library');
  $('lib-count').textContent = state.library.length;
  renderLibrary();
  renderSourceOptions();
}

function renderSourceOptions() {
  const select = $('convert-source');
  const current = select.value;
  const usable = state.library.filter((f) => f.exists);
  select.innerHTML = '<option value="">— pick a file from the library —</option>' +
    usable.map((f) => `<option value="${f.id}">${esc(f.name)}  (${fmtBytes(f.size)})</option>`).join('');
  if (usable.some((f) => f.id === current)) select.value = current;
}

function renderLibrary() {
  const box = $('library-list');
  if (!state.library.length) {
    box.innerHTML = '<div class="empty">Nothing here yet. Download something, or upload a file on the Convert tab.</div>';
    return;
  }
  box.innerHTML = state.library.map((f) => `
    <div class="lib-item ${f.exists ? '' : 'missing'}">
      <div class="lib-main">
        <div class="lib-name"><span class="origin">${esc(f.origin)}</span>${esc(f.name)}</div>
        <div class="lib-sub">${f.exists ? fmtBytes(f.size) : 'missing from disk'} · ${esc(f.path)}</div>
      </div>
      <div class="row">
        ${f.exists ? `<button class="btn small" data-act="convert" data-id="${f.id}">Convert</button>` : ''}
        ${f.exists ? `<a class="btn small" href="/api/files/${f.id}/download">Save</a>` : ''}
        <button class="btn small danger" data-act="delete" data-id="${f.id}">Delete</button>
      </div>
    </div>`).join('');
}

$('library-list').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button[data-act]');
  if (!btn) return;
  const { act, id } = btn.dataset;
  if (act === 'convert') {
    document.querySelector('.tab[data-tab="convert"]').click();
    $('convert-source').value = id;
    await loadSource(id);
  } else if (act === 'delete') {
    const row = state.library.find((f) => f.id === id);
    if (!confirm(`Delete this file from disk?\n\n${row ? row.path : id}`)) return;
    try {
      await api(`/api/library/${id}`, { method: 'DELETE' });
      await loadLibrary();
      toast('Deleted.');
    } catch (err) { toast(err.message, true); }
  }
});

$('scan-library').addEventListener('click', async () => {
  try {
    const r = await api('/api/library/scan', { method: 'POST' });
    await loadLibrary();
    toast(`Scan complete: ${r.added} added, ${r.removed} stale entries removed.`);
  } catch (err) { toast(err.message, true); }
});

/* =================================================================== JOBS */

function jobPhase(job) {
  const d = job.detail || {};
  const bits = [];
  if (d.phase) bits.push(d.phase);
  if (job.kind === 'download') {
    if (d.stream_count > 1) bits.push(`stream ${d.stream_index}/${d.stream_count}`);
    if (d.speed) bits.push(`${fmtBytes(d.speed)}/s`);
    if (d.eta) bits.push(`ETA ${fmtDuration(d.eta)}`);
    // HLS/DASH have no real byte total — only a guess that swings wildly — so
    // show the fragment position instead of an untrustworthy "x of y".
    const fragmented = d.fragment_count > 0;
    if (fragmented) {
      bits.push(`fragment ${Math.round(d.fragment_index || 0)}/${Math.round(d.fragment_count)}`);
    }
    if (d.downloaded_bytes) {
      bits.push(fragmented
        ? fmtBytes(d.downloaded_bytes)
        : `${fmtBytes(d.downloaded_bytes)}${d.total_bytes ? ` / ${fmtBytes(d.total_bytes)}` : ''}`);
    }
  } else {
    if (d.speed) bits.push(`${d.speed}`);
    if (d.eta) bits.push(`ETA ${fmtDuration(d.eta)}`);
    if (d.encoded_bytes) bits.push(fmtBytes(d.encoded_bytes));
  }
  return bits;
}

function jobResult(job) {
  const d = job.detail || {};
  if (job.status !== 'done') return '';
  const bits = [];
  if (d.actual_bytes !== undefined && d.actual_bytes !== null) {
    bits.push(`actual ${fmtBytes(d.actual_bytes)}`);
    if (d.predicted_bytes) {
      const sign = d.accuracy_pct >= 0 ? '+' : '';
      bits.push(`predicted ${fmtBytes(d.predicted_bytes)} (${sign}${d.accuracy_pct.toFixed(1)}%)`);
    }
    if (d.target_bytes) bits.push(`target ${fmtBytes(d.target_bytes)}`);
  } else if (d.output_size) {
    bits.push(fmtBytes(d.output_size));
  }
  return bits.join('  ·  ');
}

function renderJob(job) {
  const pct = Math.max(0, Math.min(100, job.progress || 0));
  const active = job.status === 'running' || job.status === 'queued';
  const meta = jobPhase(job);
  const result = jobResult(job);
  const log = typeof job.log === 'string' ? job.log : '';

  return `
    <div class="job ${job.status}" data-job="${job.id}">
      <div class="job-head">
        <span class="job-title" title="${esc(job.title)}">${esc(job.title)}</span>
        <span class="job-status ${job.status}">${job.status}</span>
      </div>
      <div class="bar"><i style="width:${pct}%"></i></div>
      <div class="job-meta">
        <span>${pct.toFixed(1)}%</span>
        ${meta.map((m) => `<span>${esc(m)}</span>`).join('')}
      </div>
      ${result ? `<div class="job-meta"><span>${esc(result)}</span></div>` : ''}
      ${job.detail?.output_path ? `<div class="job-meta"><span>${esc(job.detail.output_path)}</span></div>` : ''}
      ${job.error ? `<div class="job-error">${esc(job.error)}</div>` : ''}
      <div class="job-actions">
        ${active ? `<button class="btn small danger" data-act="cancel" data-id="${job.id}">Cancel</button>` : ''}
        ${!active ? `<button class="btn small" data-act="remove" data-id="${job.id}">Remove</button>` : ''}
      </div>
      ${log ? `<details><summary>Process output (${log.split('\n').length} lines)</summary><div class="job-log">${esc(log)}</div></details>` : ''}
    </div>`;
}

function renderJobs() {
  const all = [...state.jobs.values()].sort((a, b) => b.created_at - a.created_at);
  const active = all.filter((j) => j.status === 'running' || j.status === 'queued');
  $('job-count').textContent = active.length;

  const paint = (elementId, list, emptyText) => {
    const el = $(elementId);
    // Preserve an open <details> across re-renders so a live log stays readable.
    const open = new Set([...el.querySelectorAll('details[open]')]
      .map((d) => d.closest('.job')?.dataset.job));
    el.innerHTML = list.length ? list.map(renderJob).join('')
                              : `<div class="empty">${emptyText}</div>`;
    open.forEach((id) => {
      const d = el.querySelector(`.job[data-job="${id}"] details`);
      if (d) d.open = true;
    });
  };

  paint('download-jobs', all.filter((j) => j.kind === 'download').slice(0, 12),
        'No downloads yet.');
  paint('convert-jobs', all.filter((j) => j.kind === 'convert').slice(0, 12),
        'No conversions yet.');
  paint('all-jobs', all, 'No jobs yet.');
}

document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button[data-act]');
  if (!btn || !btn.closest('.job')) return;
  const { act, id } = btn.dataset;
  btn.disabled = true;
  try {
    if (act === 'cancel') {
      await api(`/api/jobs/${id}/cancel`, { method: 'POST' });
      toast('Cancelled.');
    } else if (act === 'remove') {
      await api(`/api/jobs/${id}`, { method: 'DELETE' });
      state.jobs.delete(id);
      renderJobs();
    }
  } catch (err) {
    toast(err.message, true);
    btn.disabled = false;
  }
});

$('clear-jobs').addEventListener('click', async () => {
  try {
    await api('/api/jobs/clear', { method: 'POST' });
    await loadJobs();
    toast('Finished jobs cleared.');
  } catch (err) { toast(err.message, true); }
});

async function loadJobs() {
  const list = await api('/api/jobs?limit=100');
  state.jobs = new Map(list.map((j) => [j.id, j]));
  renderJobs();
}

/* ============================================================ SSE STREAM */

function connectEvents() {
  const source = new EventSource('/api/events');

  source.onopen = () => $('sse-state').classList.add('live');
  source.onerror = () => $('sse-state').classList.remove('live');

  source.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }

    if (msg.type === 'hello') {
      state.jobs = new Map(msg.jobs.map((j) => [j.id, j]));
      renderJobs();
    } else if (msg.type === 'job') {
      const previous = state.jobs.get(msg.job.id);
      // Progress events omit the log; keep whatever we already had.
      if (previous && !msg.job.log) msg.job.log = previous.log;
      state.jobs.set(msg.job.id, msg.job);
      renderJobs();
      if (previous && previous.status !== msg.job.status) {
        if (msg.job.status === 'done') toast(`Finished: ${msg.job.title}`);
        if (msg.job.status === 'error') toast(`Failed: ${msg.job.title}`, true);
      }
    } else if (msg.type === 'log') {
      const job = state.jobs.get(msg.job_id);
      if (job) job.log = (job.log ? `${job.log}\n` : '') + msg.line;
    } else if (msg.type === 'library') {
      loadLibrary();
    } else if (msg.type === 'jobs_changed') {
      loadJobs();
    }
  };
}

/* ============================================================== SETTINGS */

async function loadSettings() {
  const s = await api('/api/settings');
  $('set-download-dir').value = s.download_dir;
  $('set-output-dir').value = s.output_dir;
  $('set-ytdlp').value = s.yt_dlp_path;
  $('set-ffmpeg').value = s.ffmpeg_path;
  $('set-ffprobe').value = s.ffprobe_path;
  $('set-cookies-browser').value = s.cookies_browser || '';
  $('set-cookies-file').value = s.cookies_file || '';
}

$('save-settings').addEventListener('click', async () => {
  const status = $('settings-status');
  status.className = 'status';
  status.textContent = 'Saving…';
  try {
    await post('/api/settings', {
      download_dir: $('set-download-dir').value.trim(),
      output_dir: $('set-output-dir').value.trim(),
      yt_dlp_path: $('set-ytdlp').value.trim(),
      ffmpeg_path: $('set-ffmpeg').value.trim(),
      ffprobe_path: $('set-ffprobe').value.trim(),
      cookies_browser: $('set-cookies-browser').value,
      cookies_file: $('set-cookies-file').value.trim(),
    });
    status.textContent = 'Saved.';
    loadHealth();
  } catch (err) {
    status.className = 'status err';
    status.textContent = err.message;
  }
});

/* ================================================================== BOOT */

(async function boot() {
  connectEvents();
  try {
    await Promise.all([loadHealth(), loadContainers(), loadLibrary(), loadJobs(), loadSettings()]);
  } catch (err) {
    toast(`Startup problem: ${err.message}`, true);
  }
})();
