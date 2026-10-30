#!/usr/bin/env python3
"""Dog Trainer CLI: learn a treat-dispenser remote, dispense, and speak tricks.

Stdlib only. Talks to the Pi dashboard over HTTP and to ElevenLabs over REST.

  dogtrainer learn    --pi http://pi.local:8780
  dogtrainer dispense --pi http://pi.local:8780
  dogtrainer voices
  dogtrainer setup-voice --name MyVoice --sample ./sample.wav
  dogtrainer tricks --dog Rex --voice MyVoice [--whistle] [--play-pi pi@pi.local]
  dogtrainer play ./audio/rex-come-here.mp3 [--play-pi pi@pi.local]

ElevenLabs key comes from ELEVENLABS_API_KEY (never commit it).
Sign up: https://elevenlabs.io/app/sign-up
"""
import argparse, json, math, os, struct, subprocess, sys, time, urllib.request, urllib.error, uuid, wave
from pathlib import Path

SIGNUP_URL = "https://elevenlabs.io/app/sign-up"
API = "https://api.elevenlabs.io/v1"
TRICKS = [("come-here", "{dog}, come here"), ("sit", "{dog}, sit"), ("down", "{dog}, down")]


def http_json(url, data=None, headers=None, timeout=30):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **(headers or {})}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} from {url}: {e.read().decode()[:300]}")


def http_bytes(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} from {url}: {e.read().decode()[:300]}")


def api_key():
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        print(f"No ELEVENLABS_API_KEY set. Sign up at {SIGNUP_URL}, create an API key, then:\n  export ELEVENLABS_API_KEY='sk_...'", file=sys.stderr)
        raise SystemExit(2)
    return key


# --- Generic OOK learner (same pulse timing as pi/decoder.py, any 32-bit code) ---
class PulseDecoder:
    """Decode OOK edges [us, level] into 32-bit frames of any value."""
    def __init__(self):
        self.previous = None; self.high = None; self.bits = []; self.primed = False; self.frame_start = None
    def feed(self, timestamp_us, level):
        result = None
        if self.previous is None:
            self.previous = timestamp_us, level; return None
        prev_time, prev_level = self.previous
        duration = timestamp_us - prev_time
        if level == prev_level or duration <= 0:
            self.__init__(); self.previous = timestamp_us, level; return None
        if level == 0:
            self.high = duration
        elif self.high is not None:
            hi, lo = self.high, duration
            if 200 < hi < 700 and 7000 < lo < 16000:
                if self.primed and len(self.bits) == 32:
                    result = {"code": f"0x{int(''.join(self.bits), 2):08X}", "timestamp_us": self.frame_start}
                self.bits = []; self.primed = True; self.frame_start = timestamp_us
            elif 200 < hi < 700 and 800 < lo < 1500:
                self.bits.append("0")
            elif 800 < hi < 1500 and 200 < lo < 700:
                self.bits.append("1")
            else:
                self.bits = []; self.primed = False
            if len(self.bits) > 32:
                self.bits = []; self.primed = False
            self.high = None
        self.previous = timestamp_us, level
        return result


def learn_from_capture(capture):
    """Return the best repeated code in a capture, or None."""
    cfg = capture.get("config", {})
    if cfg.get("modulation") != "OOK":
        return None
    decoder, frames = PulseDecoder(), {}
    for ts, level in capture.get("edges", []):
        frame = decoder.feed(ts, level)
        if frame:
            frames.setdefault(frame["code"], []).append(frame["timestamp_us"])
    best = None
    for code, stamps in frames.items():
        bursts, count, last = 0, 0, None
        for ts in stamps:
            if last is None or ts - last > 250000 or ts <= last:
                if count >= 2: bursts += 1
                count = 0
            count += 1; last = ts
        if count >= 2: bursts += 1
        cand = {"code": code, "matching_frames": len(stamps), "bursts": bursts}
        if bursts >= 1 and (best is None or (cand["bursts"], cand["matching_frames"]) > (best["bursts"], best["matching_frames"])):
            best = cand
    return best


def cmd_learn(args):
    base = args.pi.rstrip("/")
    _, rec = http_json(f"{base}/api/record", {})
    print(f"Recording 10s on the Pi (id {rec['id']}). Press the treat button now...")
    time.sleep(11)
    _, capture = http_json(f"{base}/api/captures/{rec['id']}.json", timeout=60)
    found = learn_from_capture(capture)
    if not found:
        raise SystemExit("Could not figure out a repeated 32-bit OOK signal. Try again, closer to the Pi, one firm press.")
    signal = {"label": "Dispense treat", "bits": 32, "frequency_mhz": capture["config"]["frequency_mhz"],
              "modulation": "OOK", "evidence_capture": rec["id"], **found}
    Path(args.out).write_text(json.dumps(signal, indent=2) + "\n")
    print(f"Stored {found['code']} ({found['matching_frames']} frames, {found['bursts']} burst(s)) -> {args.out}")
    print("To make the Pi send it, set this code in pi/decoder.py SIGNAL on the Pi and restart dog-trainer (see AGENT_SETUP.md).")


def cmd_dispense(args):
    base = args.pi.rstrip("/")
    rid = str(uuid.uuid4())
    _, rec = http_json(f"{base}/api/transmit", {"command": "dispense_treat", "request_id": rid})
    print(f"Queued {rec.get('code', '')} request {rid} (respects the Pi 30s cooldown; sent != treat confirmed).")
    for _ in range(20):
        time.sleep(1)
        _, live = http_json(f"{base}/api/live?since=0")
        mine = next((r for r in live["transmit"]["history"] if r["request_id"] == rid), None)
        if mine and mine["status"] in ("sent", "failed", "interrupted"):
            print(f"Result: {mine['status']} {mine.get('result', mine.get('error', ''))}")
            return
    print("Still queued/sending; check the dashboard.")


def list_voices(key):
    _, data = http_json(f"{API}/voices", headers={"xi-api-key": key})
    return data.get("voices", [])


def resolve_voice(key, want):
    voices = list_voices(key)
    if want:
        for v in voices:
            if want in (v["voice_id"], v["name"]) or want.lower() == v["name"].lower():
                return v
        raise SystemExit(f"Voice '{want}' not found. Run: dogtrainer voices")
    cloned = [v for v in voices if v.get("category") == "cloned"]
    if cloned:
        return cloned[0]
    raise SystemExit("No cloned voice found and no --voice given. Run `dogtrainer voices` and pass --voice, or `dogtrainer setup-voice`.")


def cmd_voices(args):
    for v in list_voices(api_key()):
        print(f"{v.get('category', '?'):8} {v['voice_id']}  {v['name']}")


def cmd_setup_voice(args):
    key, sample = api_key(), Path(args.sample)
    if not sample.is_file():
        raise SystemExit(f"Sample not found: {sample}. Record 1-3 min of clear speech first.")
    boundary = "----dogtrainer" + uuid.uuid4().hex
    parts = []
    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    field("name", args.name); field("description", args.description or "Dog trainer voice")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{sample.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode() + sample.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    _, data = http_bytes(f"{API}/voices/add", data=b"".join(parts), headers={"xi-api-key": key, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    result = json.loads(data)
    print(f"Created voice {args.name}: {result.get('voice_id')}. Use it with --voice {args.name}")


def tts(key, voice_id, text, output_format="mp3_44100_128"):
    body = json.dumps({"text": text, "model_id": "eleven_flash_v2_5", "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}).encode()
    _, data = http_bytes(f"{API}/text-to-speech/{voice_id}?output_format={output_format}", data=body, headers={"xi-api-key": key, "Content-Type": "application/json"})
    return data


def whistle_wav(pcm, rate=44100):
    """Prepend two whistle peeps to 16-bit mono PCM and return WAV bytes."""
    import io
    samples = []
    def peep(dur, f0, f1):
        n = int(rate * dur)
        for i in range(n):
            t = i / rate
            env = math.sin(math.pi * i / n) ** 1.5
            samples.append(int(0.55 * 32767 * env * math.sin(2 * math.pi * (f0 * t + (f1 - f0) * t * t / (2 * dur)))))
    peep(0.22, 1500, 2300); samples += [0] * int(rate * 0.09); peep(0.32, 1500, 2400); samples += [0] * int(rate * 0.30)
    voice = struct.unpack(f"<{len(pcm)//2}h", pcm)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)+len(voice)}h", *(samples + list(voice))))
    return buf.getvalue()


def play_file(path, via_pi):
    if via_pi:
        remote = f"/tmp/{Path(path).name}"
        subprocess.run(["scp", "-o", "BatchMode=yes", str(path), f"{via_pi}:{remote}"], check=True)
        subprocess.run(["ssh", "-o", "BatchMode=yes", via_pi, f"pw-play {remote}"], check=True)
        print(f"Played {path} on the Pi ({via_pi}). Make sure the Bluetooth speaker is the Pi default sink.")
    else:
        player = "afplay" if sys.platform == "darwin" else "pw-play"
        subprocess.run([player, str(path)], check=True)
        print(f"Played {path} locally.")


def cmd_tricks(args):
    key = api_key()
    voice = resolve_voice(key, args.voice)
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    slug = args.dog.lower().replace(" ", "-")
    for stem, template in TRICKS:
        text = template.format(dog=args.dog)
        path = out / f"{slug}-{stem}.mp3"
        path.write_bytes(tts(key, voice["voice_id"], text))
        print(f"{text!r} ({voice['name']}) -> {path}")
        if args.whistle:
            wav_path = out / f"{slug}-{stem}-whistle.wav"
            wav_path.write_bytes(whistle_wav(tts(key, voice["voice_id"], text, "pcm_44100")))
            print(f"  whistled version -> {wav_path}")
            if args.play_pi:
                play_file(wav_path, args.play_pi)
        elif args.play_pi:
            play_file(path, args.play_pi)


def cmd_play(args):
    play_file(args.file, args.play_pi)


def main():
    p = argparse.ArgumentParser(prog="dogtrainer", description="Learn a treat remote, dispense, and speak dog tricks.")
    p.add_argument("--pi", default=os.environ.get("DOG_TRAINER_PI", "http://pi.local:8780"), help="Pi dashboard base URL")
    sub = p.add_subparsers(required=True)
    s = sub.add_parser("learn", help="Record the remote on the Pi and store the repeated signal")
    s.add_argument("--out", default="signal.json"); s.set_defaults(fn=cmd_learn)
    s = sub.add_parser("dispense", help="Send the dispense command via the Pi"); s.set_defaults(fn=cmd_dispense)
    s = sub.add_parser("voices", help="List ElevenLabs voices"); s.set_defaults(fn=cmd_voices)
    s = sub.add_parser("setup-voice", help="Clone a voice from an audio sample")
    s.add_argument("--name", required=True); s.add_argument("--sample", required=True); s.add_argument("--description"); s.set_defaults(fn=cmd_setup_voice)
    s = sub.add_parser("tricks", help="Generate come-here/sit/down audio for --dog")
    s.add_argument("--dog", required=True, help="Dog's name, e.g. Rex")
    s.add_argument("--voice", help="Voice name or ID (default: first cloned voice)")
    s.add_argument("--outdir", default="audio"); s.add_argument("--whistle", action="store_true", help="Also save a whistle-prefixed WAV")
    s.add_argument("--play-pi", metavar="USER@HOST", help="Play each file on the Pi (pw-play; Bluetooth speaker as default sink)"); s.set_defaults(fn=cmd_tricks)
    s = sub.add_parser("play", help="Play an audio file locally or on the Pi")
    s.add_argument("file"); s.add_argument("--play-pi", metavar="USER@HOST"); s.set_defaults(fn=cmd_play)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

