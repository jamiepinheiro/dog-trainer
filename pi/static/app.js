'use strict';
const $ = id => document.getElementById(id);
let samples = [], cursor = 0, boot = null, current = null, frozen = false, frozenPulses = [], dirty = false, connected = false;
let sendingRequest = false, transmitMessage = '', txListSignature = '', lastRequestId = null;
const fmt = value => Number(value).toLocaleString();
const clock = value => new Date(value * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
function setText(id, text) { $(id).textContent = text; }
function showError(message) { $('error').hidden = !message; $('error').textContent = message || ''; }
function connection(ok, label) {
  connected = ok;
  $('connection').className = `connection ${ok ? 'live' : 'offline'}`;
  $('connection').replaceChildren(document.createElement('i'), document.createTextNode(label));
}
function canvas(id) {
  const element = $(id), rect = element.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
  if (element.width !== Math.round(rect.width * ratio) || element.height !== Math.round(rect.height * ratio)) {
    element.width = Math.round(rect.width * ratio); element.height = Math.round(rect.height * ratio);
  }
  const ctx = element.getContext('2d'); ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height); ctx.font = '10px system-ui';
  return {ctx, w: rect.width, h: rect.height};
}
function drawSignal() {
  const {ctx, w, h} = canvas('signal-chart'), l = 37, r = w - 8, top = 10, bottom = h - 22;
  const y = dbm => bottom - (Math.max(-110, Math.min(-20, dbm)) + 110) / 90 * (bottom - top);
  ctx.textAlign = 'right';
  for (const n of [-110, -80, -50, -20]) {
    ctx.fillStyle = '#71816a'; ctx.fillText(n, l - 8, y(n) + 3);
    ctx.strokeStyle = '#e4e9dc'; ctx.beginPath(); ctx.moveTo(l, y(n)); ctx.lineTo(r, y(n)); ctx.stroke();
  }
  ctx.fillStyle = '#71816a'; ctx.textAlign = 'left'; ctx.fillText('60s ago', l, h - 2); ctx.textAlign = 'right'; ctx.fillText('Now', r, h - 2);
  if (!current) return;
  ctx.setLineDash([4, 5]); ctx.strokeStyle = '#959c79'; ctx.beginPath(); ctx.moveTo(l, y(current.config.threshold_dbm)); ctx.lineTo(r, y(current.config.threshold_dbm)); ctx.stroke(); ctx.setLineDash([]);
  if (!samples.length) return;
  const now = current.server_time, recent = samples.filter(s => s.time >= now - 60);
  const x = t => l + (t - (now - 60)) / 60 * (r - l);
  ctx.save(); ctx.beginPath(); ctx.rect(l, top, r - l, bottom - top); ctx.clip();
  ctx.strokeStyle = '#296d51'; ctx.lineWidth = 1.8; ctx.beginPath();
  recent.forEach((s, i) => { if (!i || s.time - recent[i - 1].time > 1) ctx.moveTo(x(s.time), y(s.rssi_dbm)); else ctx.lineTo(x(s.time), y(s.rssi_dbm)); }); ctx.stroke();
  if (recent.length) {
    const last = recent.at(-1); ctx.fillStyle = '#296d51'; ctx.beginPath(); ctx.arc(x(last.time), y(last.rssi_dbm), 3, 0, Math.PI * 2); ctx.fill();
    const values = recent.map(s => s.rssi_dbm);
    setText('range', `Low ${Math.min(...values).toFixed(1)} · Peak ${Math.max(...values).toFixed(1)} dBm`);
  }
  ctx.restore();
}
function drawPulses() {
  const {ctx, w, h} = canvas('pulse-chart'), pulses = frozen ? frozenPulses : current?.pulses || [];
  const width = w - 28, left = 22, high = 26, low = 72;
  ctx.fillStyle = '#75846b'; ctx.fillText('1', 1, high + 3); ctx.fillText('0', 1, low + 3);
  ctx.strokeStyle = '#e1e7d8';
  for (const v of [high, low]) {ctx.beginPath(); ctx.moveTo(left, v); ctx.lineTo(w, v); ctx.stroke();}
  // Show complete pulse widths inside a fixed 40 ms window; do not compress long gaps.
  let time = 0, selected = [];
  for (let i = pulses.length - 1; i >= 0 && time < 40000; i--) {
    selected.unshift(pulses[i]); time += pulses[i][0];
  }
  ctx.save(); ctx.beginPath(); ctx.rect(left, 0, width, h); ctx.clip();
  ctx.strokeStyle = '#668239'; ctx.lineWidth = 1.6; ctx.beginPath();
  let position = width - time / 40000 * width;
  selected.forEach(([duration, level], i) => {
    const y = level ? high : low;
    if (!i) ctx.moveTo(left + position, y); else ctx.lineTo(left + position, y);
    position += duration / 40000 * width; ctx.lineTo(left + position, y);
  }); ctx.stroke(); ctx.restore();
  setText('pulse-span', pulses.length ? `${frozen ? 'Frozen' : 'Latest'} trace · 40 ms window` : 'Waiting for edges…');
}
function listRow(primary, secondary) {
  const row = document.createElement('div'), text = document.createElement('div'), detail = document.createElement('span');
  row.className = 'list-row'; text.textContent = primary; detail.textContent = secondary; text.append(detail); row.append(text); return row;
}
let listSignature = '', eventSignature = '';
function updateLists(data) {
  const txSignature = JSON.stringify(data.transmit.history || []);
  if (txSignature !== txListSignature) {
    txListSignature = txSignature;
    if (data.transmit.history?.length) {
      $('transmissions').replaceChildren(...data.transmit.history.map(t => {
        const status = {queued:'Queued',sending:'Sending',sent:'Command sent',failed:'Failed · delivery uncertain',interrupted:'Interrupted · delivery unknown'}[t.status] || t.status;
        const detail = t.result ? `${t.result.frames} repeated frames · ${t.result.duration_ms} ms · back to listening` : t.error || 'One command burst';
        return listRow(`${clock(t.time)} · Dispense treat · ${status}`, detail);
      }));
    }
  }
  const signature = JSON.stringify(data.captures);
  if (signature !== listSignature) {
    listSignature = signature; setText('capture-count', data.captures.length);
    if (data.captures.length) {
      $('captures').replaceChildren(...data.captures.map(c => {
        const warning = c.interrupted ? ' · Interrupted' : c.truncated ? ' · Edge limit reached' : c.dropped_edges ? ` · ${c.dropped_edges} missed` : '';
        const match = c.decoded?.bursts.length ? ` · Dispense treat: ${c.decoded.bursts.length} burst${c.decoded.bursts.length === 1 ? '' : 's'}, ${c.decoded.matching_frames} frames` : '';
        const row = listRow(new Date(c.started_at * 1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}), `${fmt(c.edges)} edges · ${c.config.frequency_mhz} MHz · ${c.config.modulation}${match}${warning}`);
        const a = document.createElement('a'); a.href = `/api/captures/${c.id}.json`; a.textContent = 'Download ↓'; a.setAttribute('download', `${c.id}.json`); row.append(a); return row;
      }));
    }
  }
  const signature2 = JSON.stringify(data.events);
  if (signature2 !== eventSignature) {
    eventSignature = signature2;
    $('events').replaceChildren(...data.events.map(e => {
      const row = document.createElement('div'), t = document.createElement('time'), message = document.createElement('div');
      row.className = `list-row ${e.kind === 'error' ? 'error-row' : e.kind === 'remote' ? 'remote-row' : ''}`; t.className = 'log-time'; t.textContent = clock(e.time);
      message.className = 'log-message'; message.textContent = e.message; row.append(t, message); return row;
    }));
  }
}
function render(data) {
  current = data;
  connection(data.online, data.online ? 'Live from the Pi' : 'Radio offline');
  showError(data.error ? `${data.error} The dashboard will retry automatically.` : null);
  const s = data.latest;
  const tx = data.transmit, txState = tx.busy ? tx.state : s;
  setText('rssi', s ? s.rssi_dbm.toFixed(1) : '—');
  setText('rx-badge', tx.busy ? 'Paused for sending' : data.pending ? 'Tuning…' : s ? s.above_threshold ? 'Activity' : 'Listening' : 'Offline');
  setText('rx-description', tx.busy ? 'Reception resumes when the command finishes.' : s ? s.above_threshold ? 'Signal above threshold. Source not yet identified.' : 'Listening for activity. Background noise is normal.' : 'Waiting for the radio to reconnect.');
  $('signal-meter').style.width = `${s ? Math.max(0, Math.min(100, (s.rssi_dbm + 110) / 90 * 100)) : 0}%`;
  setText('frequency', `${data.config.frequency_mhz.toFixed(4)} MHz`); setText('edge-rate', s ? fmt(s.edges_per_second) : '—');
  setText('sent-count', tx.sent_by_app); setText('tx-state', tx.busy ? txState?.tx_active ? 'Sending treat command' : 'Preparing command…' : s ? 'No transmission active' : 'Radio state unavailable');
  setText('chip-state', txState ? txState.state : 'Unavailable'); setText('tx-queue', txState ? `${txState.tx_fifo_bytes} bytes` : '—');
  $('tx-dot').classList.toggle('active', !!txState?.tx_active);
  const cooling = Math.ceil(tx.cooldown_remaining || 0);
  const profileReady = data.config.modulation === 'OOK' && Math.abs(data.config.frequency_mhz - 433.92) < .0001;
  $('transmit').disabled = sendingRequest || !data.online || !tx.enabled || tx.busy || cooling > 0 || data.pending || !!data.recording || !profileReady;
  setText('transmit', tx.busy || sendingRequest ? 'Sending…' : cooling ? `Wait ${cooling}s` : 'Dispense treat ↑');
  setText('tx-badge', !data.online ? 'Offline' : tx.busy ? 'Sending' : cooling ? 'Cooling down' : tx.enabled ? 'Ready to send' : 'Unavailable');
  const lastRequest = tx.history?.find(t => t.request_id === lastRequestId);
  if (lastRequest?.status === 'sent') transmitMessage = `Command sent at ${clock(lastRequest.time)}. Check the feeder for delivery.`;
  if (lastRequest && ['failed','interrupted'].includes(lastRequest.status)) transmitMessage = `${lastRequest.error} No automatic retry was made.`;
  setText('transmit-feedback', !tx.enabled ? tx.reason : !profileReady ? 'Set the receiver to 433.92 MHz OOK to send.' : data.recording ? 'Finish the recording before sending.' : transmitMessage || '30-second cooldown between commands.');
  setText('dropped', `${fmt(data.dropped_edges)} missed edges`);
  if (data.recognition) {
    const match = data.recognition;
    setText('recognition-status', match.active ? 'Listening for treat button' : 'Matching paused');
    setText('last-signal', match.last ? `${clock(match.last.time)} · ${match.last.frames} matching frames` : 'Waiting for a matching signal…');
    setText('matched-bursts', fmt(match.bursts)); setText('matched-frames', fmt(match.frames));
  }
  $('chart-empty').hidden = data.online && samples.length > 0; $('chart-empty').textContent = data.online ? 'Waiting for live samples…' : 'Radio offline · chart is not live';
  setText('updated', `Updated ${clock(data.server_time)} · Running ${Math.floor(data.uptime_seconds / 60)}m`);
  if (data.identity.version) setText('hardware', `Pi 3B · CC1101 ${data.identity.version} · SPI0 / CE0 · GPIO25`);
  if (!dirty) {
    $('tune-frequency').value = data.config.frequency_mhz; $('modulation').value = data.config.modulation; $('threshold').value = data.config.threshold_dbm;
  }
  $('apply').disabled = !data.online || data.pending || !!data.recording || tx.busy;
  $('apply').textContent = data.pending ? 'Applying…' : 'Apply settings';
  $('record').disabled = !data.online || data.pending || !!data.recording || tx.busy;
  if (data.recording) {
    const left = Math.max(0, Math.ceil(10 - (data.server_time - data.recording.started_at)));
    $('record').textContent = `Recording… ${left}s remaining`;
    setText('record-status', `${fmt(data.recording.edges)} raw edges captured. Press the remote now.`);
  } else {
    $('record').textContent = 'Record 10 seconds ●';
    setText('record-status', 'Recordings stay on the Pi. The latest 20 are kept.');
  }
  updateLists(data); drawSignal(); drawPulses();
}
async function poll() {
  try {
    const response = await fetch(`/api/live?since=${cursor}`, {cache:'no-store', signal:AbortSignal.timeout(4000)});
    if (!response.ok) throw new Error(`Pi returned HTTP ${response.status}`);
    const data = await response.json();
    if (boot && boot !== data.boot_id) {cursor = 0; samples = []; listSignature = ''; eventSignature = '';}
    if (current && (current.config.frequency_mhz !== data.config.frequency_mhz || current.config.modulation !== data.config.modulation)) samples = [];
    boot = data.boot_id;
    for (const s of data.samples) if (s.seq > cursor) {samples.push(s); cursor = s.seq;}
    samples = samples.filter(s => s.time > data.server_time - 60).slice(-600); render(data);
  } catch (error) {
    connection(false, 'Connection lost'); showError(`Cannot reach the Pi. ${error.message}. Reconnecting…`);
    ['rssi','edge-rate','chip-state','tx-queue'].forEach(id => setText(id, '—'));
    setText('rx-badge', 'Offline'); setText('rx-description', 'No current reading. Check the Pi or your network.'); setText('tx-state', 'Radio state unavailable');
    setText('recognition-status', 'Matching paused · offline');
    $('transmit').disabled = true; setText('tx-badge', 'Offline');
    $('record').disabled = true; $('apply').disabled = true; $('chart-empty').hidden = false; $('chart-empty').textContent = 'Connection lost · chart is not live';
  } finally {setTimeout(poll, document.hidden ? 1500 : 500);}
}
async function post(path, data) {
  const response = await fetch(path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),signal:AbortSignal.timeout(6000)});
  const result = await response.json(); if (!response.ok) throw new Error(result.error || 'Request failed.'); return result;
}
$('tuning').addEventListener('input', () => {dirty = true; setText('tune-status', 'Unsaved receiver settings.');});
$('tuning').addEventListener('submit', async event => {
  event.preventDefault(); $('apply').disabled = true;
  try {
    await post('/api/config', {frequency_mhz:Number($('tune-frequency').value),modulation:$('modulation').value,threshold_dbm:Number($('threshold').value)});
    dirty = false; setText('tune-status', 'Applying settings on the Pi…');
    setTimeout(() => {if (connected && !current.pending) setText('tune-status', 'Settings saved on the Pi.');}, 1200);
  } catch (error) {setText('tune-status', error.message); $('apply').disabled = !connected;}
});
$('record').addEventListener('click', async () => {
  $('record').disabled = true;
  try {await post('/api/record', {}); setText('record-status', 'Recording started. Press the remote now.');}
  catch (error) {setText('record-status', error.message); $('record').disabled = !connected;}
});
$('transmit').addEventListener('click', async () => {
  if (sendingRequest || $('transmit').disabled) return;
  sendingRequest = true; $('transmit').disabled = true; setText('transmit', 'Sending…');
  // getRandomValues works on this private HTTP origin; randomUUID requires HTTPS.
  const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
  const requestId = `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  lastRequestId = requestId;
  try {
    const result = await post('/api/transmit', {command:'dispense_treat', request_id:requestId});
    transmitMessage = result.status === 'sent' ? 'Command already sent. Check the feeder for delivery.' : 'Command accepted. Watch the transmit log for the result.';
  } catch (error) {
    transmitMessage = `${error.message} Check the transmit log before trying again; no automatic retry was made.`;
  } finally {
    sendingRequest = false; setText('transmit-feedback', transmitMessage);
  }
});
$('freeze').addEventListener('click', () => {
  frozen = !frozen; frozenPulses = current ? current.pulses.map(p => [...p]) : [];
  $('freeze').setAttribute('aria-pressed', String(frozen)); $('freeze').textContent = frozen ? 'Resume trace' : 'Freeze trace'; drawPulses();
});
new ResizeObserver(() => {drawSignal(); drawPulses();}).observe($('signal-chart'));
poll();
