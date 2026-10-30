import math
import unittest

from radio import DEFAULT_CONFIG, Radio, rssi_dbm, validate_config


class FakeSPI:
    def __init__(self):
        self.registers = {a: 0 for a in range(0x2F)}
        self.registers.update({0x30: 0, 0x31: 0x14, 0x35: 1})
        self.commands = []
        self.closed = False
        self.pa = [0xC6] + [0] * 7

    def xfer2(self, data):
        self.commands.append(list(data))
        command = data[0]
        if command == 0xFE:
            return [0] + self.pa
        if command == 0x7E:
            self.pa = list(data[1:])
            return [0] * len(data)
        if len(data) == 1:
            if command == 0x34:
                self.registers[0x35] = 13
            elif command in (0x30, 0x36):
                self.registers[0x35] = 1
            return [0]
        if command & 0x80:
            return [0, self.registers.get(command & 0x3F, 0)]
        self.registers[command] = data[1]
        return [0, 0]

    def close(self):
        self.closed = True


class RadioTests(unittest.TestCase):
    def test_transmission_strobes_rejected(self):
        radio = Radio(FakeSPI())
        for command in (0x31, 0x35, 0x38, 0x39):
            with self.assertRaises(ValueError):
                radio.strobe(command)
        self.assertEqual(radio.spi.commands, [])

    def test_configuration_validation(self):
        for key, value in [('frequency_mhz', math.nan), ('frequency_mhz', math.inf),
                           ('frequency_mhz', 315), ('frequency_mhz', True),
                           ('modulation', 'GFSK'), ('threshold_dbm', -200)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_config({**DEFAULT_CONFIG, key: value})

    def test_configure_and_cleanup_do_not_transmit(self):
        spi = FakeSPI()
        radio = Radio(spi)
        initial = dict(spi.registers)
        radio.start(DEFAULT_CONFIG)
        self.assertEqual(spi.registers[0x35], 13)
        self.assertEqual(spi.registers[0x08], 0x30)
        self.assertEqual(spi.registers[0x02], 0x0D)
        word = (spi.registers[0x0D] << 16) | (spi.registers[0x0E] << 8) | spi.registers[0x0F]
        self.assertAlmostEqual(word * 26 / 65536, 433.92, places=3)
        radio.close()
        self.assertEqual(spi.registers[0x35], 1)
        self.assertTrue(spi.closed)
        for address in range(0x2F):
            self.assertEqual(spi.registers[address], initial[address])
        self.assertFalse(any(c == [0x35] or c == [0x31] for c in spi.commands))

    def test_broken_wiring_is_rejected(self):
        spi = FakeSPI()
        spi.registers[0x31] = 0xFF
        with self.assertRaises(RuntimeError):
            Radio(spi).start(DEFAULT_CONFIG)
        self.assertFalse(any(len(c) == 1 for c in spi.commands))

    def test_rssi_signed_conversion(self):
        self.assertEqual(rssi_dbm(200), -102)
        self.assertEqual(rssi_dbm(0), -74)
        self.assertEqual(rssi_dbm(100), -24)

    def test_readback_failure_blocks_receive(self):
        class BrokenSPI(FakeSPI):
            def xfer2(self, data):
                result = super().xfer2(data)
                if data[0] == 0x0D:
                    self.registers[0x0D] = 0
                return result
        spi = BrokenSPI()
        with self.assertRaises(RuntimeError):
            Radio(spi).start(DEFAULT_CONFIG)
        self.assertNotIn([0x34], spi.commands)


if __name__ == '__main__':
    unittest.main()
