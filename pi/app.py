#!/usr/bin/env python3
import collections
import json
import logging
import os
from pathlib import Path
import queue
import re
import signal
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from radio import DEFAULT_CONFIG, Radio, STATES, validate_config
from decoder import SIGNAL, PulseDecoder, RepeatMatcher, analyze_capture, profile_matches
from transmit import TxJournal, COOLDOWN_SECONDS

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get('RADIO_STATE_DIR', '/var/lib/dog-trainer'))
ALLOWED_HOSTS = set(os.environ.get('RADIO_HOSTS', 'pi,pi.local,localhost,127.0.0.1').split(','))
MAX_CAPTURE_EDGES = 50000


def atomic_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, separators=(',', ':'), allow_nan=False))
    temporary.replace(path)


class Monitor:
    def __init__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.commands = queue.Queue(maxsize=1)
        self.config = dict(DEFAULT_CONFIG)
        try:
            self.config = validate_config(json.loads((STATE_DIR / 'config.json').read_text()))
        except (OSError, ValueError, TypeError):
            pass
        self.boot_id = str(uuid.uuid4())
        self.started = time.time()
        self.online = False
        self.error = 'Connecting to the radio…'
        self.identity = {}
        self.samples = collections.deque(maxlen=600)
        self.events = collections.deque(maxlen=40)
        self.pulses = collections.deque(maxlen=160)
        self.total_edges = self.dropped_edges = 0
        self.latest = None
        self.capture = None
        self.captures = []
        self.level = 0
        self.seq = 0
        self.pending = False
        self.tx = TxJournal(STATE_DIR / 'transmissions.json')
        self.tx_busy = False
        self.tx_state = None
        self.decoder = PulseDecoder()
        self.matcher = RepeatMatcher()
        self.recognized_frames = self.recognized_bursts = 0
        self.last_signal = None
        self.event('system', 'Dashboard started. Listening for the treat remote.')

    def event(self, kind, message):
        with self.lock:
            self.events.appendleft({'time': time.time(), 'kind': kind, 'message': message})

    def request_config(self, data):
        config = validate_config(data)
        with self.lock:
            if not self.online:
                raise RuntimeError('Radio is offline. Check its connection first.')
            if self.capture or self.pending or self.tx_busy:
                raise RuntimeError('Wait for the current recording or tuning to finish.')
            self.pending = True
            self.commands.put_nowait(config)

    def start_capture(self):
        with self.lock:
            if not self.online or self.pending or self.tx_busy:
                raise RuntimeError('Wait until the receiver is listening.')
            if self.capture:
                raise RuntimeError('A recording is already in progress.')
            now = time.time()
            self.capture = {'id': time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now)) + '-' + uuid.uuid4().hex[:6],
                            'started_at': now, 'started_monotonic_ns': time.monotonic_ns(),
                            'duration_seconds': 10, 'config': dict(self.config),
                            'initial_level': self.level, 'edges': [], 'rssi': [],
                            'dropped_edges_start': self.dropped_edges, 'truncated': False}
            self.event('recording', 'Recording 10 seconds of raw radio activity.')
            return self.capture['id']

    def request_transmit(self, data):
        if not isinstance(data, dict) or set(data) != {'command', 'request_id'} or data['command'] != 'dispense_treat':
            raise ValueError('Only the confirmed dispense_treat command is supported.')
        with self.lock:
            previous = self.tx.find(data['request_id'])
            if previous:
                return dict(previous)
            if not self.online or self.pending or self.capture or self.tx_busy:
                raise RuntimeError('Wait until the radio is listening and any recording is finished.')
            if not profile_matches(self.config):
                raise RuntimeError('Select 433.92 MHz OOK before sending the treat command.')
            record, fresh = self.tx.accept(data['request_id'])
            if fresh:
                self.tx_busy = True
                self.tx_state = {'state': 'QUEUED', 'state_code': None, 'tx_fifo_bytes': 0, 'tx_active': False}
                self.commands.put_nowait({'transmit_request_id': record['request_id']})
                self.event('transmit', 'Dispense treat command queued.')
            return dict(record)

    def transmit_command(self, radio, request_id):
        def progress(state, count):
            with self.lock:
                self.tx_state = {'state': STATES.get(state, 'UNKNOWN'), 'state_code': state,
                                 'tx_fifo_bytes': count, 'tx_active': state == 19}
        try:
            with self.lock:
                self.tx.mark(request_id, 'sending')
            result = radio.transmit_treat(self.config, progress=progress)
            with self.lock:
                self.tx.mark(request_id, 'sent', result=result)
                self.event('transmit', 'Dispense treat command sent. Radio returned to listening; dispensing is not confirmed.')
        except Exception as exc:
            logging.exception('Transmit command failed; it will not be retried')
            with self.lock:
                try:
                    self.tx.mark(request_id, 'failed', error=str(exc))
                except Exception:
                    logging.exception('Failed to save transmit failure')
                self.event('error', f'Transmit failed: {exc}. Delivery is uncertain; not retried.')
        finally:
            with self.lock:
                self.decoder.reset()
                self.matcher.reset()
                self.pulses.clear()
                self.tx_busy = False
                self.tx_state = None

    def finish_capture(self, interrupted=False):
        with self.lock:
            capture = self.capture
            if not capture:
                return
            self.capture = None
            capture['ended_at'] = time.time()
            capture['interrupted'] = interrupted
            capture['dropped_edges'] = self.dropped_edges - capture.pop('dropped_edges_start')
            capture.pop('started_monotonic_ns')
            capture['edge_format'] = '[microseconds since recording start, new logic level]'
            capture['notes'] = 'Raw demodulator edges include noise. Labelled matches identify the confirmed remote signal, not successful dispensing. RSSI is approximate. Sending is blocked while recording.'
            capture['schema_version'] = 2
        capture['decoded'] = analyze_capture(capture)
        capture['known_signal'] = dict(SIGNAL)
        try:
            atomic_json(STATE_DIR / (capture['id'] + '.json'), capture)
            with self.lock:
                self.captures.insert(0, self.capture_summary(capture))
                self.captures = self.captures[:20]
            # Keep storage bounded to the most recent 20 recordings.
            files = sorted(STATE_DIR.glob('20*T*.json'), reverse=True)
            for path in files[20:]:
                path.unlink()
            self.event('recording', f"Saved {len(capture['edges']):,} edges" + (' (interrupted).' if interrupted else '.'))
        except OSError as exc:
            logging.exception('Saving recording failed')
            self.event('error', f'Recording could not be saved: {exc}')

    @staticmethod
    def capture_summary(capture):
        return {'id': capture['id'], 'started_at': capture['started_at'],
                'edges': len(capture['edges']), 'config': capture['config'],
                'truncated': capture['truncated'], 'interrupted': capture['interrupted'],
                'dropped_edges': capture['dropped_edges'],
                'decoded': capture['decoded'] if 'decoded' in capture else analyze_capture(capture)}

    def load_captures(self):
        for path in sorted(STATE_DIR.glob('20*T*.json'), reverse=True)[:20]:
            try:
                self.captures.append(self.capture_summary(json.loads(path.read_text())))
            except (OSError, KeyError, ValueError):
                logging.warning('Cannot read recording %s', path.name)

    def gpio_loop(self, request, halt):
        import gpiod
        previous_ns = None
        previous_seq = None
        try:
            with self.lock:
                self.level = int(request.get_value(25) == gpiod.line.Value.ACTIVE)
                self.decoder.reset()
                self.matcher.reset()
            while not halt.is_set() and not self.stop.is_set():
                if not request.wait_edge_events(timeout=0.1):
                    continue
                edges = request.read_edge_events(max_events=1024)
                with self.lock:
                    for edge in edges:
                        if previous_seq is not None and edge.global_seqno != previous_seq + 1:
                            self.dropped_edges += max(0, edge.global_seqno - previous_seq - 1)
                            previous_ns = None
                            self.decoder.reset()
                            self.matcher.reset()
                        previous_seq = edge.global_seqno
                        new_level = int(edge.event_type == gpiod.EdgeEvent.Type.RISING_EDGE)
                        if self.tx_busy:
                            previous_ns = None
                            self.decoder.reset()
                            self.matcher.reset()
                            continue
                        self.total_edges += 1
                        if previous_ns is not None:
                            self.pulses.append([round((edge.timestamp_ns - previous_ns) / 1000, 2), self.level])
                        previous_ns = edge.timestamp_ns
                        self.level = new_level
                        if not self.pending and profile_matches(self.config):
                            frame = self.decoder.feed(edge.timestamp_ns / 1000, new_level)
                            if frame and frame['code'] == SIGNAL['code']:
                                self.recognized_frames += 1
                                match = self.matcher.feed(frame)
                                if match:
                                    if match['new']:
                                        self.recognized_bursts += 1
                                        self.last_signal = {'time': time.time() - time.monotonic() + match['start_us'] / 1e6,
                                                            'frames': match['frames'], 'code': SIGNAL['code'], 'label': SIGNAL['label']}
                                        self.event('remote', f"{SIGNAL['label']} signal received · {SIGNAL['code']}.")
                                    else:
                                        self.last_signal['frames'] = match['frames']
                        capture = self.capture
                        if capture:
                            offset = (edge.timestamp_ns - capture['started_monotonic_ns']) / 1000
                            if 0 <= offset <= capture['duration_seconds'] * 1_000_000:
                                if len(capture['edges']) < MAX_CAPTURE_EDGES:
                                    capture['edges'].append([round(offset, 2), new_level])
                                else:
                                    capture['truncated'] = True
        except Exception as exc:
            logging.exception('GPIO reader failed')
            with self.lock:
                self.error = f'GPIO reader failed: {exc}'
            halt.set()

    def run(self):
        import gpiod
        import spidev
        while not self.stop.is_set():
            radio = request = pulse_thread = None
            halt = threading.Event()
            try:
                spi = spidev.SpiDev()
                spi.open(0, 0)
                spi.mode = 0
                spi.max_speed_hz = 100000
                radio = Radio(spi)
                identity = radio.start(self.config)
                backup = STATE_DIR / 'original-registers.json'
                if not backup.exists():
                    atomic_json(backup, radio.original)
                request = gpiod.request_lines('/dev/gpiochip0', consumer='dog-trainer',
                    config={25: gpiod.LineSettings(direction=gpiod.line.Direction.INPUT,
                        edge_detection=gpiod.line.Edge.BOTH, event_clock=gpiod.line.Clock.MONOTONIC)},
                    event_buffer_size=4096)
                pulse_thread = threading.Thread(target=self.gpio_loop, args=(request, halt), daemon=True)
                pulse_thread.start()
                with self.lock:
                    self.online, self.error, self.identity = True, None, identity
                self.event('receiver', f"Listening at {self.config['frequency_mhz']:.4f} MHz · {self.config['modulation']}.")
                last_count, last_time, last_activity = self.total_edges, time.monotonic(), 0
                while not self.stop.is_set() and not halt.is_set():
                    tick = time.monotonic()
                    try:
                        config = self.commands.get_nowait()
                    except queue.Empty:
                        config = None
                    if config:
                        if 'transmit_request_id' in config:
                            self.transmit_command(radio, config['transmit_request_id'])
                            last_count, last_time = self.total_edges, time.monotonic()
                            continue
                        radio.configure(config)
                        atomic_json(STATE_DIR / 'config.json', config)
                        with self.lock:
                            self.config = config
                            self.samples.clear()
                            self.pulses.clear()
                            self.decoder.reset()
                            self.matcher.reset()
                            self.pending = False
                        self.event('receiver', f"Tuned to {config['frequency_mhz']:.4f} MHz · {config['modulation']}.")
                    sample = radio.sample()
                    now = time.monotonic()
                    with self.lock:
                        self.seq += 1
                        sample.update(seq=self.seq, time=time.time(),
                            edges_per_second=round((self.total_edges - last_count) / max(0.001, now - last_time)),
                            above_threshold=sample['rssi_dbm'] >= self.config['threshold_dbm'])
                        last_count, last_time = self.total_edges, now
                        self.latest = sample
                        self.samples.append(sample)
                        if self.capture:
                            self.capture['rssi'].append([sample['time'], sample['rssi_dbm']])
                        if sample['above_threshold'] and now - last_activity > 2:
                            self.event('activity', f"Signal above threshold: {sample['rssi_dbm']:.1f} dBm. Source unknown.")
                            last_activity = now
                        done = self.capture and time.monotonic_ns() >= self.capture['started_monotonic_ns'] + 10_000_000_000
                    if done:
                        self.finish_capture()
                    self.stop.wait(max(0.01, 0.1 - (time.monotonic() - tick)))
                if halt.is_set():
                    raise RuntimeError(self.error or 'GPIO reader stopped.')
            except Exception as exc:
                logging.exception('Radio monitor error')
                with self.lock:
                    self.online, self.error = False, str(exc)
                self.event('error', str(exc))
            finally:
                with self.lock:
                    self.online = False
                    self.pending = False
                    while not self.commands.empty():
                        abandoned = self.commands.get_nowait()
                        if 'transmit_request_id' in abandoned:
                            try:
                                self.tx.mark(abandoned['transmit_request_id'], 'interrupted', error='Radio became unavailable before sending. Not retried.')
                            except Exception:
                                logging.exception('Failed to persist interrupted transmit request')
                    self.tx_busy = False
                    self.tx_state = None
                halt.set()
                if pulse_thread:
                    pulse_thread.join(timeout=2)
                if request:
                    request.release()
                self.finish_capture(interrupted=True)
                if radio:
                    try:
                        radio.close()
                    except Exception:
                        logging.exception('Radio cleanup failed')
            self.stop.wait(5)

    def snapshot(self, since):
        with self.lock:
            capture = None
            if self.capture:
                capture = {k: self.capture[k] for k in ('id', 'started_at', 'duration_seconds', 'truncated')}
                capture['edges'] = len(self.capture['edges'])
            return {'boot_id': self.boot_id, 'server_time': time.time(),
                    'uptime_seconds': round(time.time() - self.started), 'online': self.online,
                    'error': self.error, 'identity': self.identity, 'config': dict(self.config),
                    'pending': self.pending, 'latest': self.latest if self.online and not self.tx_busy else None,
                    'samples': [s for s in self.samples if s['seq'] > since],
                    'events': list(self.events), 'pulses': list(self.pulses),
                    'total_edges': self.total_edges, 'dropped_edges': self.dropped_edges,
                    'recording': capture, 'captures': list(self.captures),
                    'known_signal': dict(SIGNAL),
                    'recognition': {'active': self.online and not self.pending and not self.tx_busy and profile_matches(self.config),
                                    'frames': self.recognized_frames, 'bursts': self.recognized_bursts,
                                    'last': dict(self.last_signal) if self.last_signal else None},
                    'transmit': {'enabled': self.tx.fault is None, 'sent_by_app': self.tx.sent,
                                 'busy': self.tx_busy, 'state': self.tx_state,
                                 'cooldown_seconds': COOLDOWN_SECONDS,
                                 'cooldown_remaining': round(self.tx.remaining(), 1),
                                 'history': [dict(r) for r in self.tx.records[:20]],
                                 'reason': self.tx.fault or 'One command burst per click. Sent does not confirm dispensing.'},
                    'receiver': {'bandwidth_khz': 325, 'data_rate_baud': 4798,
                                 'rssi_offset_db': 74, 'crystal_mhz': 26}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'DogTrainer/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, fmt, *args):
        if args and str(args[1] if len(args) > 1 else '') not in ('200', '202'):
            logging.info(fmt, *args)

    def send(self, status, data, content_type='application/json', filename=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, separators=(',', ':'), allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def valid_host(self):
        return self.headers.get('Host', '').split(':')[0].lower() in ALLOWED_HOSTS

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {'error': 'Unknown host.'})
        url = urlsplit(self.path)
        if url.path == '/api/live':
            try:
                since = max(0, int(parse_qs(url.query).get('since', ['0'])[0]))
            except ValueError:
                return self.send(400, {'error': 'Invalid sample cursor.'})
            return self.send(200, self.server.monitor.snapshot(since))
        if url.path == '/api/health':
            online = self.server.monitor.online
            return self.send(200 if online else 503, {'online': online, 'error': self.server.monitor.error})
        if url.path.startswith('/api/captures/'):
            name = url.path.rsplit('/', 1)[-1]
            if not re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{6}\.json', name):
                return self.send(404, {'error': 'Recording not found.'})
            path = STATE_DIR / name
            if not path.is_file():
                return self.send(404, {'error': 'Recording not found.'})
            return self.send(200, path.read_bytes(), filename=name)
        assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                  '/style.css': ('style.css', 'text/css; charset=utf-8'),
                  '/favicon.svg': ('favicon.svg', 'image/svg+xml')}
        if url.path in assets:
            filename, mime = assets[url.path]
            return self.send(200, (ROOT / 'static' / filename).read_bytes(), mime)
        self.send(404, {'error': 'Not found.'})

    def do_POST(self):
        origin = self.headers.get('Origin')
        if not self.valid_host() or (origin and urlsplit(origin).netloc != self.headers.get('Host')):
            return self.send(403, {'error': 'Requests must come from this dashboard.'})
        if self.headers.get('Content-Type') != 'application/json':
            return self.send(415, {'error': 'Expected application/json.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024:
                raise ValueError('Invalid request size.')
            data = json.loads(self.rfile.read(length))
            if self.path == '/api/config':
                self.server.monitor.request_config(data)
                return self.send(202, {'accepted': True})
            if self.path == '/api/record':
                if data != {}:
                    raise ValueError('Recording takes an empty JSON object.')
                return self.send(202, {'id': self.server.monitor.start_capture()})
            if self.path == '/api/transmit':
                return self.send(202, self.server.monitor.request_transmit(data))
            return self.send(404, {'error': 'Not found.'})
        except (ValueError, TypeError) as exc:
            self.send(400, {'error': str(exc)})
        except (RuntimeError, queue.Full) as exc:
            self.send(409, {'error': str(exc)})


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    monitor = Monitor()
    monitor.load_captures()
    server = ThreadingHTTPServer((os.environ.get('RADIO_BIND', '0.0.0.0'), int(os.environ.get('RADIO_PORT', '8780'))), Handler)
    server.daemon_threads = True
    server.monitor = monitor
    worker = threading.Thread(target=monitor.run, name='radio', daemon=True)
    worker.start()
    def shutdown(*_):
        monitor.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    logging.info('Radio dashboard listening on %s:%s', *server.server_address)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        monitor.stop.set()
        worker.join(timeout=4)
        server.server_close()


if __name__ == '__main__':
    main()
