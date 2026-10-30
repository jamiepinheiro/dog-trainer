"""Receive-side pulse matching for the confirmed treat button (example profile)."""

SIGNAL = {'label': 'Dispense treat', 'code': '0x545904B9', 'bits': 32,
          'frequency_mhz': 433.92, 'modulation': 'OOK',
          'confirmation': 'Confirmed from two presses of the treat-dispense button.',
          'evidence_capture': '20261030T184251Z-50ae40',
          'evidence_frames': 10, 'evidence_bursts': 2}


def profile_matches(config):
    return config['modulation'] == 'OOK' and abs(config['frequency_mhz'] - 433.92) < 0.0001


class PulseDecoder:
    def __init__(self):
        self.reset()

    def reset(self):
        self.previous = None
        self.high = None
        self.bits = []
        self.primed = False
        self.frame_start = None

    def feed(self, timestamp_us, level):
        result = None
        if self.previous is None:
            self.previous = timestamp_us, level
            return None
        previous_time, previous_level = self.previous
        duration = timestamp_us - previous_time
        if level == previous_level or duration <= 0:
            self.reset()
        elif level == 0:
            self.high = duration
        elif self.high is not None:
            hi, lo = self.high, duration
            if 200 < hi < 700 and 7000 < lo < 16000:
                if self.primed and len(self.bits) == 32:
                    result = {'code': f'0x{int("".join(self.bits), 2):08X}',
                              'timestamp_us': self.frame_start}
                self.bits = []
                self.primed = True
                self.frame_start = timestamp_us
            elif 200 < hi < 700 and 800 < lo < 1500:
                self.bits.append('0')
            elif 800 < hi < 1500 and 200 < lo < 700:
                self.bits.append('1')
            else:
                self.bits = []
                self.primed = False
            if len(self.bits) > 32:
                self.bits = []
                self.primed = False
            self.high = None
        self.previous = timestamp_us, level
        return result


class RepeatMatcher:
    """Require two full matching frames before labelling a burst."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.last_us = None
        self.frames = 0
        self.burst_start = None

    def feed(self, frame):
        if frame['code'] != SIGNAL['code']:
            return None
        timestamp = frame['timestamp_us']
        gap = None if self.last_us is None else timestamp - self.last_us
        if gap is None or gap > 250000 or gap <= 0:
            self.frames = 0
            self.burst_start = timestamp
        self.frames += 1
        self.last_us = timestamp
        if self.frames < 2:
            return None
        return {'start_us': self.burst_start, 'frames': self.frames, 'new': self.frames == 2}


def analyze_capture(capture):
    if not profile_matches(capture['config']):
        return {'matching_frames': 0, 'bursts': [], 'profile_applied': False}
    decoder, matcher = PulseDecoder(), RepeatMatcher()
    frames, bursts = 0, []
    for timestamp, level in capture['edges']:
        frame = decoder.feed(timestamp, level)
        if frame and frame['code'] == SIGNAL['code']:
            frames += 1
            match = matcher.feed(frame)
            if match:
                if match['new']:
                    bursts.append({'at_seconds': round(match['start_us'] / 1e6, 3), 'frames': match['frames']})
                else:
                    bursts[-1]['frames'] = match['frames']
    return {'matching_frames': frames, 'bursts': bursts, 'profile_applied': True}
