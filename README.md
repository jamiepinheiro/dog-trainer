# Dog Trainer

Control a wireless treat dispenser from a Raspberry Pi, and speak training
commands in your own (cloned) voice. Super simple: one stdlib Python CLI plus
a small Pi radio service. No pip installs for the CLI.

**Browser/phone → Pi web server → CC1101 radio → wireless command → dispenser**

## Parts

| Part | What |
|---|---|
| Raspberry Pi 3 Model B | Runs the radio service (`pi/`) |
| Ebyte E07-M1101D-SMA (CC1101) | Receives/transmits at the remote's frequency (example: 433.92 MHz OOK) |
| Existing treat dispenser + remote | The Pi replays the remote's button signal |
| Bluetooth speaker (optional) | Pi plays trick audio, e.g. via PipeWire `pw-play` |

Wiring (physical Pi pin numbers, 3.3V only):

| Radio | Pi pin |
|---|---|
| Ground | 6 |
| 3.3V power | 1 |
| GDO0 | 22 |
| SPI chip select | 24 |
| SPI clock | 23 |
| SPI MOSI | 19 |
| SPI MISO | 21 |

GDO2 is unused. Viewing the radio board from underneath mirrors the pins —
double-check orientation before powering on.

## Quick start

### 1. Set up the Pi

Give this repo to your agent (see [AGENT_SETUP.md](AGENT_SETUP.md)) or, on the Pi:

```sh
git clone https://github.com/jamiepinheiro/dog-trainer.git
cd dog-trainer/pi && sh install.sh
curl http://127.0.0.1:8780/api/health   # {"online":true,...}
```

The example profile in `pi/decoder.py` is `0x545904B9` at 433.92 MHz OOK.
Yours may differ — learn it (next step) and update `SIGNAL` on the Pi.

### 2. Learn your remote

```sh
export DOG_TRAINER_PI=http://pi.local:8780   # or your Pi's host/IP
python3 dogtrainer.py learn                  # press the treat button when prompted
cat signal.json                              # e.g. {"code": "0x........", ...}
```

`learn` records 10 seconds on the Pi, decodes repeated 32-bit OOK frames, and
stores the most-repeated code in `signal.json` (git-ignored). If it cannot find
a repeated code it says so instead of guessing.

### 3. Dispense

```sh
python3 dogtrainer.py dispense
```

The Pi enforces a 30-second cooldown, UUID de-duplication, and no auto-retries.
"Sent" means the radio transmitted; it does not confirm a treat came out.

### 4. Voice tricks (ElevenLabs)

```sh
# No account? Sign up: https://elevenlabs.io/app/sign-up
export ELEVENLABS_API_KEY='sk_...'           # never commit this
python3 dogtrainer.py voices                                   # list voices
python3 dogtrainer.py setup-voice --name MyVoice --sample ./my-voice.wav   # clone (1-3 min sample)
python3 dogtrainer.py tricks --dog Rex --voice MyVoice         # Rex, come here / sit / down -> audio/
python3 dogtrainer.py tricks --dog Rex --whistle --play-pi pi@pi.local     # whistle + voice, played on the Pi
```

Without `ELEVENLABS_API_KEY` the CLI prints the sign-up link and exits.
Audio files land in `audio/` (git-ignored). `--play-pi` copies the file to the
Pi and plays it with `pw-play`; pair your Bluetooth speaker to the Pi and make
it the default PipeWire sink first (`bluetoothctl`, `wpctl status`).

## Safety

- Anyone who can reach the Pi dashboard port can dispense. It has no login —
  keep it on a trusted LAN/tailnet only, and set `RADIO_HOSTS` accordingly.
- Radio "sent" is not dispenser confirmation. Keep the 30s cooldown.
- Never commit API keys, `signal.json`, recordings, or generated audio.
  `.gitignore` covers the common cases; check `git status` before pushing.

## Layout

- `dogtrainer.py` — the CLI (learn / dispense / voices / setup-voice / tricks / play)
- `pi/` — Pi service: `app.py` (dashboard/API), `radio.py` (CC1101), `decoder.py` (known-signal match), `transmit.py` (waveform + cooldown journal), `static/` (dashboard), `dog-trainer.service`, `install.sh`
- `tests/test_learn.py` — learner unit test (synthetic OOK capture)
