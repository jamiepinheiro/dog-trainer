# Dog Cam

Live dog flag: Pi camera snapshot -> motion gate -> macOS Vision dog check -> annotated web feed.

Runs on the Mac (keeps the Pi free for radio/audio):

```sh
swiftc -O vision_flag.swift -o vision_flag   # once
PI_CAM=http://<pi-ip>:8770 python3 server.py  # http://localhost:8781
```

Notes:

- The Pi camera service (`camera_stream.py`, port 8770) may be blocked by its
  presence guard; clear/override it there before Dog Cam can fetch frames.
- The Pi camera is mounted upside down, so frames are rotated 180° in
  `upright_jpeg` before motion gating, detection, and display.
- Detection uses `VNRecognizeAnimalsRequest` (`vision_flag.swift`); the flag
  needs 2 of the last 5 positive checks to show DOG IN FRAME.
