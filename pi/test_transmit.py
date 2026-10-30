import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from decoder import analyze_capture
from radio import Radio, DEFAULT_CONFIG
from test_radio import FakeSPI
from transmit import command_payload, UNIT_US, TARGET_UNIT_US, REPEATS, TxJournal


class TransmitSPI(FakeSPI):
    def __init__(self, underrun=False):
        super().__init__()
        self.fifo = []
        self.air = []
        self.underrun = underrun
        self.tx_reads = 0
        self.state_reads = 0

    def xfer2(self, data):
        command = data[0]
        if command == 0x7F:
            if len(self.fifo) + len(data) - 1 > 64:
                raise AssertionError('FIFO overflow')
            self.fifo += data[1:]
            self.commands.append(data[:])
            return [0] * len(data)
        if command == 0x35 and len(data) == 1:
            self.registers[0x35] = 19
        if command == 0x3B and len(data) == 1:
            self.fifo = []
        if command == 0xF5 and self.registers[0x35] == 19:
            self.state_reads += 1
            # Drain once per pair of state reads, preserving stable-status reads.
            if self.state_reads % 2 == 1 and self.tx_reads:
                if self.underrun:
                    self.registers[0x35] = 22
                else:
                    n = min(24, len(self.fifo))
                    self.air += self.fifo[:n]
                    self.fifo = self.fifo[n:]
                    if len(self.air) == self.registers[0x06]:
                        self.registers[0x35] = 1
        if command == 0xFA:
            self.tx_reads += 1
            return [0, len(self.fifo) | (0x80 if self.registers[0x35] == 22 else 0)]
        return super().xfer2(data)


class TransmitTests(unittest.TestCase):
    def test_monitor_serializes_requests_and_blocks_conflicting_actions(self):
        import app
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'STATE_DIR', Path(directory)):
            monitor = app.Monitor()
            monitor.online = True
            request = {'command': 'dispense_treat', 'request_id': str(uuid.uuid4())}
            monitor.request_transmit(request)
            self.assertEqual(monitor.request_transmit(request)['status'], 'queued')
            self.assertEqual(monitor.commands.qsize(), 1)
            with self.assertRaises(RuntimeError):
                monitor.start_capture()
            with self.assertRaises(RuntimeError):
                monitor.request_config(DEFAULT_CONFIG)
            with self.assertRaises(RuntimeError):
                monitor.request_transmit({'command': 'dispense_treat', 'request_id': str(uuid.uuid4())})
            class MockRadio:
                def transmit_treat(self, config, progress):
                    progress(19, 32)
                    return {'frames': 10, 'duration_ms': 650, 'radio_completed': True}
            monitor.commands.get_nowait()
            monitor.transmit_command(MockRadio(), request['request_id'])
            self.assertFalse(monitor.tx_busy)
            self.assertEqual(monitor.tx.sent, 1)
            self.assertEqual(monitor.request_transmit(request)['status'], 'sent')
            self.assertTrue(monitor.commands.empty())

    def test_waveform_round_trips_through_receive_decoder(self):
        payload = command_payload()
        bits = ''.join(f'{b:08b}' for b in payload)
        edges, previous = [], None
        for index, bit in enumerate(bits):
            if bit != previous:
                edges.append([index * UNIT_US, int(bit)])
                previous = bit
        result = analyze_capture({'config': DEFAULT_CONFIG, 'edges': edges})
        self.assertEqual(result['matching_frames'], REPEATS)
        self.assertEqual(len(result['bursts']), 1)
        self.assertLessEqual(len(payload), 255)
        self.assertLess(abs(UNIT_US - TARGET_UNIT_US), 1)

    def test_fifo_completion_and_rx_restore(self):
        spi = TransmitSPI()
        radio = Radio(spi)
        radio.start(DEFAULT_CONFIG)
        pa_before = spi.pa[:]
        with patch('radio.time.sleep'):
            result = radio.transmit_treat(DEFAULT_CONFIG, pa_off=True)
        self.assertTrue(result['radio_completed'])
        self.assertEqual(bytes(spi.air), command_payload())
        self.assertEqual(spi.registers[0x35], 13)
        self.assertEqual(spi.registers[0x08], 0x30)
        self.assertEqual(spi.pa, pa_before)
        self.assertEqual(spi.commands.count([0x35]), 1)
        self.assertIn([0x7E] + [0] * 8, spi.commands)

    def test_underrun_stops_and_restores_rx_without_retry(self):
        spi = TransmitSPI(underrun=True)
        radio = Radio(spi)
        radio.start(DEFAULT_CONFIG)
        with patch('radio.time.sleep'), self.assertRaisesRegex(RuntimeError, 'underrun'):
            radio.transmit_treat(DEFAULT_CONFIG, pa_off=True)
        self.assertEqual(spi.registers[0x35], 13)
        self.assertEqual(spi.commands.count([0x35]), 1)

    def test_readback_error_never_starts_tx(self):
        spi = TransmitSPI()
        radio = Radio(spi)
        radio.start(DEFAULT_CONFIG)
        original_read = radio.read
        def broken_read(address):
            if address == 0x06 and spi.registers[0x06] == len(command_payload()):
                return 0
            return original_read(address)
        with patch.object(radio, 'read', side_effect=broken_read), self.assertRaisesRegex(RuntimeError, 'readback'):
            radio.transmit_treat(DEFAULT_CONFIG)
        self.assertNotIn([0x35], spi.commands)
        self.assertEqual(spi.registers[0x35], 13)

    def test_duplicate_cooldown_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.]
            path = Path(directory) / 'transmissions.json'
            log = TxJournal(path, clock=lambda: now[0])
            request = str(uuid.uuid4())
            self.assertTrue(log.accept(request)[1])
            self.assertFalse(log.accept(request)[1])
            with self.assertRaises(RuntimeError):
                log.accept(str(uuid.uuid4()))
            log.mark(request, 'sent')
            new = TxJournal(path, clock=lambda: now[0])
            self.assertEqual(new.sent, 1)
            self.assertFalse(new.accept(request)[1])
            with self.assertRaises(RuntimeError):
                new.accept(str(uuid.uuid4()))
            now[0] += 31
            self.assertTrue(new.accept(str(uuid.uuid4()))[1])

    def test_interrupted_queue_is_never_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'transmissions.json'
            request = str(uuid.uuid4())
            log = TxJournal(path)
            log.accept(request)
            new = TxJournal(path)
            self.assertEqual(new.find(request)['status'], 'interrupted')
            self.assertFalse(new.accept(request)[1])
            self.assertEqual(new.sent, 0)

    def test_corrupt_log_disables_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'transmissions.json'
            path.write_text('broken')
            with self.assertRaises(RuntimeError):
                TxJournal(path).accept(str(uuid.uuid4()))


if __name__ == '__main__':
    unittest.main()
