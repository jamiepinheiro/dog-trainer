# Agent setup: install Dog Trainer on a fresh Raspberry Pi

You are setting this up for a human. Work step by step, verify each step, and
stop and report (do not guess) if hardware checks fail. Do not invent API keys,
hostnames, or IP addresses — ask the human for anything not discoverable.

## 0. You need from the human

- Pi SSH target (host/IP + user, normally `pi@<host>`) and that you may install packages / write systemd units.
- Their treat dispenser's remote in hand (they must press it during `learn`).
- Optional: ElevenLabs API key (as env var only, never write it to the repo/disk in plaintext beyond their shell env), their dog's name, and a 1-3 min voice sample file if they want a cloned voice.
- Optional: Bluetooth speaker name if they want Pi playback.

## 1. Wire the radio (human does this, power OFF)

Ebyte E07-M1101D-SMA (CC1101), 3.3V only. Physical Pi pins:
GND→6, 3.3V→1, GDO0→22, CS→24, SCK→23, MOSI→19, MISO→21. GDO2 unused.
Warning: viewing the board from underneath mirrors the pins.

## 2. Install on the Pi

```sh
ssh <pi>
git clone https://github.com/jamiepinheiro/dog-trainer.git
cd dog-trainer/pi && sh install.sh
```

Enable SPI if prompted (`sudo raspi-config` → Interface → SPI → reboot).
Ensure no old touchscreen config claims the SPI/GPIO pins.

Verify:

```sh
systemctl is-active dog-trainer
curl -s http://127.0.0.1:8780/api/health   # expect {"online":true,"error":null}
curl -s http://127.0.0.1:8780/api/live?since=0 | head -c 400  # identity partnum 0x00 version 0x14, state RX
```

If `online:false` or the identity check fails, check wiring/orientation/3.3V before changing code.

If reaching the dashboard by LAN/tailnet IP is refused (403), set
`RADIO_HOSTS=pi,pi.local,localhost,127.0.0.1,<your-host-or-IP>` in the unit's
environment, `daemon-reload`, restart. Never expose the port publicly — no login.

## 3. Learn the remote (human presses the button)

From any machine that can reach the Pi (or on the Pi):

```sh
export DOG_TRAINER_PI=http://<pi-host>:8780
python3 dogtrainer.py learn   # human: one firm treat-button press during the 10s window
cat signal.json
```

Apply the learned code on the Pi: edit `/opt/dog-trainer/decoder.py` `SIGNAL`
(`code`, keep 433.92/OOK unless the capture says otherwise), then
`sudo systemctl restart dog-trainer`. Confirm the dashboard recognises the
remote (press it again; look for the recognised burst in `/api/live`).

## 4. Test dispense (human watches the dispenser)

```sh
python3 dogtrainer.py dispense
```

Expect `Result: sent`. Ask the human whether a treat physically came out —
the radio cannot confirm this. Respect the 30s cooldown; never auto-retry.

## 5. Voice (optional)

```sh
export ELEVENLABS_API_KEY='sk_...'   # human's key; no key -> https://elevenlabs.io/app/sign-up
python3 dogtrainer.py voices
python3 dogtrainer.py setup-voice --name <VoiceName> --sample <sample.wav>   # only if cloning
python3 dogtrainer.py tricks --dog <DogName> --voice <VoiceName>
```

## 6. Bluetooth speaker playback (optional)

On the Pi: the speaker must expose an A2DP source profile from the Pi
(WirePlumber `bluez5.roles` must include `a2dp_source`/`a2dp_sink`; BlueALSA-only
HFP/HSP setups will pair but fail with `br-connection-profile-unavailable`).

```sh
bluetoothctl  # scan bredr, pair, trust, connect <speaker>
wpctl status  # speaker must be the default (*) sink
python3 dogtrainer.py tricks --dog <DogName> --whistle --play-pi <user>@<pi-host>
```

## Done criteria

- `/api/health` online, dashboard reachable only on trusted network.
- Remote press recognised; one `dispense` → radio `sent` + human confirms treat.
- (Optional) trick audio plays from the intended speaker.
- No secrets (`sk_...`, keys, `signal.json`, audio, recordings) committed anywhere.
