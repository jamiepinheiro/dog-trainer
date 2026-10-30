#!/bin/sh
# Install Dog Trainer on a Raspberry Pi (run on the Pi, from this repo clone).
set -eu
sudo apt-get update
sudo apt-get install -y python3 python3-spidev python3-gpiod pipewire wireplumber bluez alsa-utils
# Enable SPI (needed for the CC1101): uncomment dtparam=spi=on and reboot if changed.
if ! grep -q '^dtparam=spi=on' /boot/firmware/config.txt 2>/dev/null && ! grep -q '^dtparam=spi=on' /boot/config.txt 2>/dev/null; then
  echo "Enable SPI: sudo raspi-config -> Interface Options -> SPI, then reboot." >&2
fi
sudo install -d /opt/dog-trainer /var/lib/dog-trainer
sudo cp app.py radio.py decoder.py transmit.py /opt/dog-trainer/
sudo cp -r static /opt/dog-trainer/
sudo cp dog-trainer.service /etc/systemd/system/
# Allow the dashboard on your LAN/tailnet hostnames/IPs, e.g.:
#   Environment=RADIO_HOSTS=pi,pi.local,localhost,127.0.0.1,100.x.y.z
sudo systemctl daemon-reload
sudo systemctl enable --now dog-trainer
echo "Dashboard: http://pi.local:8780/  (health: /api/health)"
