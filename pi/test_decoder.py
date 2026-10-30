import unittest
from decoder import SIGNAL, PulseDecoder, analyze_capture


def make_capture(frames=3, code=SIGNAL['code']):
    edges, now = [[0, 1]], 0
    def pulse(high, low):
        nonlocal now
        now += high
        edges.append([now, 0])
        now += low
        edges.append([now, 1])
    pulse(400, 11600)
    for _ in range(frames):
        for bit in f'{int(code, 16):032b}':
            pulse(400 if bit == '0' else 1200, 1200 if bit == '0' else 400)
        pulse(400, 11600)
    return {'config': {'frequency_mhz':433.92, 'modulation':'OOK'}, 'edges':edges}


class DecoderTests(unittest.TestCase):
    def test_repeats_form_one_burst(self):
        result = analyze_capture(make_capture())
        self.assertEqual(result['matching_frames'], 3)
        self.assertEqual(len(result['bursts']), 1)
        self.assertEqual(result['bursts'][0]['frames'], 3)

    def test_one_frame_does_not_label_a_burst(self):
        result = analyze_capture(make_capture(1))
        self.assertEqual(result['matching_frames'], 1)
        self.assertEqual(result['bursts'], [])

    def test_different_command_not_labelled(self):
        result = analyze_capture(make_capture(3, '0x545904B8'))
        self.assertEqual(result['matching_frames'], 0)
        self.assertEqual(result['bursts'], [])

    def test_two_separated_presses(self):
        capture = make_capture()
        capture['edges'] += [[t + 1000000, level] for t, level in make_capture()['edges']]
        result = analyze_capture(capture)
        self.assertEqual(len(result['bursts']), 2)
        self.assertEqual(result['matching_frames'], 6)

    def test_wrong_receiver_profile_ignored(self):
        capture = make_capture()
        capture['config']['frequency_mhz'] = 434.42
        self.assertFalse(analyze_capture(capture)['profile_applied'])

    def test_missing_edge_invalidates_frame_and_resynchronizes(self):
        capture = make_capture(4)
        del capture['edges'][12]
        result = analyze_capture(capture)
        self.assertEqual(result['matching_frames'], 3)
        self.assertEqual(result['bursts'][0]['frames'], 3)

    def test_long_noise_stream_does_not_grow_decoder_buffer(self):
        decoder = PulseDecoder()
        for i in range(10000):
            decoder.feed(i * 400, i % 2)
        self.assertLessEqual(len(decoder.bits), 32)


if __name__ == '__main__':
    unittest.main()
