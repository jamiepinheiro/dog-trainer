"""CC1101 receiver and bounded transmitter for the confirmed treat command."""

import time
from transmit import command_payload, DRATE_E, DRATE_M, UNIT_US, REPEATS


STATES = {0: 'SLEEP', 1: 'IDLE', 2: 'XOFF', 3: 'CALIBRATING', 4: 'CALIBRATING',
          5: 'CALIBRATING', 6: 'WAKING', 7: 'WAKING', 8: 'CALIBRATING',
          9: 'SETTLING', 10: 'SETTLING', 11: 'SETTLING', 12: 'CALIBRATING',
          13: 'RX', 14: 'RX_END', 15: 'RX_RESET', 16: 'TX_RX_SWITCH',
          17: 'RX_OVERFLOW', 18: 'FSTXON', 19: 'TX', 20: 'TX_END',
          21: 'RX_TX_SWITCH', 22: 'TX_UNDERFLOW'}
DEFAULT_CONFIG = {'frequency_mhz': 433.92, 'modulation': 'OOK', 'threshold_dbm': -75}


def validate_config(data):
    if not isinstance(data, dict) or set(data) != set(DEFAULT_CONFIG):
        raise ValueError('Provide frequency_mhz, modulation and threshold_dbm.')
    if type(data['frequency_mhz']) not in (int, float) or not 387 <= data['frequency_mhz'] <= 464:
        raise ValueError('Frequency must be between 387 and 464 MHz.')
    if data['modulation'] not in ('OOK', '2-FSK'):
        raise ValueError('Choose OOK or 2-FSK.')
    if type(data['threshold_dbm']) not in (int, float) or not -110 <= data['threshold_dbm'] <= -20:
        raise ValueError('Activity threshold must be between -110 and -20 dBm.')
    return {'frequency_mhz': round(float(data['frequency_mhz']), 4),
            'modulation': data['modulation'], 'threshold_dbm': float(data['threshold_dbm'])}


def rssi_dbm(raw):
    return (raw - 256 if raw >= 128 else raw) / 2 - 74


class Radio:
    def __init__(self, spi):
        self.spi = spi
        self.original = None
        self.original_pa = None

    def read(self, address):
        return self.spi.xfer2([address | (0xC0 if address >= 0x30 else 0x80), 0])[1]

    def write(self, address, value):
        if not 0 <= address <= 0x2E:
            raise ValueError('Only configuration registers may be written.')
        self.spi.xfer2([address, value])

    def strobe(self, command):
        if command not in (0x30, 0x33, 0x34, 0x36, 0x3A, 0x3B, 0x3D):
            raise ValueError('Transmit and other unsupported strobes are disabled.')
        return self.spi.xfer2([command])[0]

    def identify(self):
        readings = [(self.read(0x30), self.read(0x31)) for _ in range(3)]
        if len(set(readings)) != 1 or readings[0] != (0, 0x14):
            raise RuntimeError(f'CC1101 identity check failed: {readings}. Check SPI wiring and 3.3 V.')
        return {'partnum': '0x00', 'version': '0x14'}

    def start(self, config):
        identity = self.identify()
        self.original = [self.read(a) for a in range(0x2F)]
        self.original_pa = self.spi.xfer2([0xFE] + [0] * 8)[1:]
        self.strobe(0x30)
        time.sleep(0.01)
        self.configure(config)
        return identity

    def wait_idle(self):
        self.strobe(0x36)
        for _ in range(20):
            if self.read(0x35) & 0x1F == 1:
                return
            time.sleep(0.005)
        raise RuntimeError('Radio did not enter IDLE.')

    def configure(self, config):
        config = validate_config(config)
        self.wait_idle()
        word = round(config['frequency_mhz'] * 65536 / 26)
        # 26 MHz crystal; 325 kHz RX filter; ~4.8 kBaud; asynchronous GDO0 data.
        values = {0x00: 0x2E, 0x01: 0x2E, 0x02: 0x0D, 0x03: 0x47,
                  0x07: 0x00, 0x08: 0x30, 0x09: 0x00, 0x0A: 0x00,
                  0x0B: 0x06, 0x0C: 0x00, 0x0D: word >> 16,
                  0x0E: (word >> 8) & 255, 0x0F: word & 255,
                  0x10: 0x67, 0x11: 0x83, 0x12: 0x30 if config['modulation'] == 'OOK' else 0x00,
                  0x13: 0x02, 0x14: 0xF8, 0x15: 0x47,
                  0x16: 0x07, 0x17: 0x3C, 0x18: 0x18, 0x19: 0x14,
                  0x1A: 0x6C, 0x1B: 0x03, 0x1C: 0x40, 0x1D: 0x91,
                  0x21: 0x56, 0x22: 0x10, 0x23: 0xE9, 0x24: 0x2A,
                  0x25: 0x00, 0x26: 0x1F, 0x2C: 0x81, 0x2D: 0x35, 0x2E: 0x09}
        for address, value in values.items():
            self.write(address, value)
        for address, value in values.items():
            if self.read(address) != value:
                raise RuntimeError(f'SPI readback failed at register {address:#04x}.')
        self.strobe(0x3A)
        self.strobe(0x3B)
        self.strobe(0x34)
        time.sleep(0.03)
        if self.read(0x35) & 0x1F != 13:
            raise RuntimeError('Radio did not enter receive mode.')

    def stable_status(self, address):
        # FIFO counters can change during SPI reads; require consecutive agreement.
        previous = self.read(address)
        for _ in range(8):
            current = self.read(address)
            if previous == current:
                return current
            previous = current
        raise RuntimeError('Radio status was unstable.')

    def transmit_treat(self, config, *, pa_off=False, progress=None):
        payload = command_payload()
        if len(payload) > 255:
            raise RuntimeError('Transmit waveform exceeds the fixed packet limit.')
        saved = [self.read(a) for a in range(0x2F)]
        saved_pa = self.spi.xfer2([0xFE] + [0] * 8)[1:]
        result = None
        try:
            self.wait_idle()
            word = round(433.92 * 65536 / 26)
            settings = {0x02: 0x2E, 0x06: len(payload), 0x07: 0, 0x08: 0,
                        0x0A: 0, 0x0C: 0, 0x0D: word >> 16, 0x0E: (word >> 8) & 255, 0x0F: word & 255,
                        0x10: 0x60 | DRATE_E, 0x11: DRATE_M, 0x12: 0x30,
                        0x13: 0x02, 0x17: 0, 0x18: 0x18, 0x22: 0x11}
            for address, value in settings.items():
                self.write(address, value)
            for address, value in settings.items():
                if self.read(address) != value:
                    raise RuntimeError(f'Transmit readback failed at {address:#04x}.')
            # ASK/OOK uses entry 0 for low and entry 1 for high. 0x60 is ~0 dBm at 433 MHz.
            table = [0, 0 if pa_off else 0x60] + [0] * 6
            self.spi.xfer2([0x7E] + table)
            if self.spi.xfer2([0xFE] + [0] * 8)[1:] != table:
                raise RuntimeError('Transmit power table readback failed.')
            self.strobe(0x3B)
            position = min(64, len(payload))
            self.spi.xfer2([0x7F] + list(payload[:position]))
            started = time.monotonic()
            self.spi.xfer2([0x35])
            observed_tx = False
            while time.monotonic() - started < 1.5:
                state = self.stable_status(0x35) & 0x1F
                count = self.stable_status(0x3A)
                if count & 0x80 or state == 22:
                    raise RuntimeError('Transmit FIFO underrun. Command may be incomplete; not retried.')
                if state == 19:
                    observed_tx = True
                if progress:
                    progress(state, count & 0x7F)
                if state == 1 and observed_tx:
                    if position != len(payload) or count & 0x7F:
                        raise RuntimeError('Radio stopped before the whole command was sent.')
                    result = {'duration_ms': round((time.monotonic() - started) * 1000),
                              'bytes': len(payload), 'frames': REPEATS, 'unit_us': round(UNIT_US, 3),
                              'pa_off': pa_off, 'radio_completed': True}
                    break
                if position < len(payload) and (count & 0x7F) <= 32:
                    end = min(len(payload), position + 32)
                    self.spi.xfer2([0x7F] + list(payload[position:end]))
                    position = end
                time.sleep(0.01)
            if result is None:
                raise RuntimeError('Transmit timed out. Command may be incomplete; not retried.')
        finally:
            self.wait_idle()
            self.strobe(0x3B)
            self.spi.xfer2([0x7E] + saved_pa)
            for address, value in enumerate(saved):
                self.write(address, value)
            self.configure(config)
        return result

    def sample(self):
        state = self.read(0x35) & 0x1F
        if state != 13:
            raise RuntimeError(f'Unexpected radio state: {STATES.get(state, state)}. Receiver will restart.')
        raw = self.read(0x34)
        return {'rssi_dbm': rssi_dbm(raw), 'rssi_raw': raw, 'state_code': state,
                'state': STATES.get(state, 'UNKNOWN'), 'tx_active': state in (19, 20, 21),
                'carrier_sense': bool(self.read(0x38) & 0x40),
                'rx_fifo_bytes': self.read(0x3B) & 0x7F, 'tx_fifo_bytes': self.read(0x3A) & 0x7F}

    def close(self):
        try:
            self.wait_idle()
            if self.original:
                for a, v in enumerate(self.original):
                    self.write(a, v)
            if self.original_pa:
                self.spi.xfer2([0x7E] + self.original_pa)
        finally:
            self.spi.close()
