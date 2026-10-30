"""Fixed command waveform and persistent duplicate protection."""

import json
import os
import time
import uuid
from decoder import SIGNAL

REPEATS = 10
COOLDOWN_SECONDS = 30
TARGET_UNIT_US = 392.675
DRATE_E, DRATE_M = 6, 155
SYMBOL_RATE = (256 + DRATE_M) * 2 ** DRATE_E * 26_000_000 / 2 ** 28
UNIT_US = 1_000_000 / SYMBOL_RATE


def command_payload():
    encoded = ''.join('1000' if bit == '0' else '1110' for bit in f'{int(SIGNAL["code"], 16):032b}')
    sync = '1' + '0' * 30
    bits = sync + (encoded + sync) * REPEATS + sync
    bits += '0' * (-len(bits) % 8)
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))


class TxJournal:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock
        self.records, self.sent = [], 0
        self.fault = None
        try:
            state = json.loads(path.read_text())
            self.records, self.sent = state['records'], state['sent']
            if not isinstance(self.records, list) or not isinstance(self.sent, int):
                raise ValueError('Invalid transmit log')
            for record in self.records:
                if record['status'] in ('queued', 'sending'):
                    record['status'] = 'interrupted'
                    record['error'] = 'Service restarted. Delivery unknown; this request will not be retried.'
            self.save()
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            self.fault = 'Transmit log could not be loaded. Sending is disabled until it is repaired.'

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump({'records': self.records, 'sent': self.sent}, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def find(self, request_id):
        return next((r for r in self.records if r['request_id'] == request_id), None)

    def remaining(self):
        if not self.records:
            return 0
        last = self.records[0]
        return max(0, COOLDOWN_SECONDS - (self.clock() - last.get('finished_at', last['time'])))

    def accept(self, request_id):
        if not isinstance(request_id, str):
            raise ValueError('A UUID request_id is required.')
        try:
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('A canonical UUID request_id is required.')
        if self.fault:
            raise RuntimeError(self.fault)
        previous = self.find(request_id)
        if previous:
            return previous, False
        if self.records and self.records[0]['status'] in ('queued', 'sending'):
            raise RuntimeError('A transmit command is already in progress.')
        if self.remaining() > 0:
            raise RuntimeError(f'Wait {int(self.remaining()) + 1} seconds before sending again.')
        record = {'request_id': request_id, 'time': self.clock(), 'status': 'queued',
                  'label': SIGNAL['label'], 'code': SIGNAL['code'], 'repeats': REPEATS}
        self.records.insert(0, record)
        self.records = self.records[:200]
        try:
            self.save()
        except OSError:
            self.fault = 'Transmit log could not be saved. Sending is disabled.'
            raise RuntimeError(self.fault)
        return record, True

    def mark(self, request_id, status, **details):
        record = self.find(request_id)
        record.update(status=status, **details)
        if status == 'sent':
            self.sent += 1
        if status in ('sent', 'failed', 'interrupted'):
            record['finished_at'] = self.clock()
        try:
            self.save()
        except OSError:
            self.fault = 'Transmit log could not be saved. Sending is disabled.'
            raise RuntimeError(self.fault)
