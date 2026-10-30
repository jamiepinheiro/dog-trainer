#!/usr/bin/env python3
"""Live dog-flag web app: Pi camera -> motion gate -> Vision dog check -> annotated feed.

Run on the Mac (keeps the Pi free for radio/audio):
  PI_CAM=http://10.0.0.204:8770 python3 server.py   # http://localhost:8781
"""
import io, json, os, subprocess, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
PI_CAM = os.environ.get("PI_CAM", "http://10.0.0.204:8770").rstrip("/")
PORT = int(os.environ.get("PORT", "8781"))
POLL_S = 1.2
MOTION_THRESHOLD = 2.0  # mean abs pixel diff (160x120 gray) to trigger Vision

state = {"jpeg": None, "annotated": None, "flag": {"present": False, "label": None, "confidence": 0.0, "bbox": None},
         "motion": 0.0, "checked_at": 0.0, "frame_at": 0.0, "error": "", "votes": []}
lock = threading.Lock()


def fetch_snapshot():
    with urllib.request.urlopen(f"{PI_CAM}/snapshot.jpg", timeout=8) as r:
        return r.read()


def upright_jpeg(raw):
    """Pi camera is mounted upside down; rotate 180 so feed/detection are upright."""
    img = Image.open(io.BytesIO(raw)).convert("RGB").transpose(Image.ROTATE_180)
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=88)
    return buf.getvalue(), img


def vision_check(jpeg):
    tmp = HERE / ".frame.jpg"
    tmp.write_bytes(jpeg)
    out = subprocess.run([str(HERE / "vision_flag"), str(tmp)], capture_output=True, text=True, timeout=20)
    hits = json.loads(out.stdout.strip() or "[]")
    dogs = [h for h in hits if "dog" in h["label"].lower()]
    return dogs[0] if dogs else None


def annotate(jpeg, hit):
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if hit:
        w, h = img.size
        x, y, bw, bh = hit["bbox"]
        d = ImageDraw.Draw(img)
        box = [x * w, y * h, (x + bw) * w, (y + bh) * h]
        d.rectangle(box, outline=(46, 204, 113), width=5)
        d.rectangle([box[0], max(0, box[1] - 28), box[0] + 190, box[1]], fill=(46, 204, 113))
        d.text((box[0] + 8, box[1] - 22 if box[1] >= 28 else box[1] + 6), f"DOG {hit['confidence']:.0%}", fill=(10, 20, 14))
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def worker():
    prev_small = None
    while True:
        try:
            jpeg, upright = upright_jpeg(fetch_snapshot())
            img = upright.convert("L").resize((160, 120))
            small = np.asarray(img, dtype=np.float32)
            motion = 99.0 if prev_small is None else float(np.abs(small - prev_small).mean())
            prev_small = small
            hit, checked = None, False
            with lock:
                state["jpeg"] = jpeg; state["motion"] = round(motion, 2); state["frame_at"] = time.time(); state["error"] = ""
            if motion >= MOTION_THRESHOLD:
                hit = vision_check(jpeg); checked = True
                with lock:
                    state["votes"] = (state["votes"] + [bool(hit)])[-5:]
                    votes = list(state["votes"])
                present = sum(votes) >= 2  # need 2 of last 5 positive checks
                with lock:
                    state["flag"] = {"present": present, "label": "dog" if hit else state["flag"].get("label"),
                                      "confidence": hit["confidence"] if hit else 0.0, "bbox": hit["bbox"] if hit else None}
                    state["checked_at"] = time.time()
            with lock:
                current_hit = hit if checked else ({"bbox": state["flag"]["bbox"], "confidence": state["flag"]["confidence"]} if state["flag"]["bbox"] else None)
            annotated = annotate(jpeg, current_hit)
            with lock:
                state["annotated"] = annotated
        except Exception as exc:  # keep serving last frame; surface the error
            with lock:
                state["error"] = str(exc)[:200]
        time.sleep(POLL_S)


PAGE = """<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dog Cam — live flag</title>
<style>
 body{margin:0;background:#101418;color:#e8ecef;font:16px/1.45 system-ui,sans-serif}
 header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid #2a3138}
 h1{font-size:19px;margin:0;font-weight:650} .sub{color:#9aa6b2;font-size:13px}
 #flag{font-weight:700;padding:7px 14px;border-radius:999px;background:#2a3138}
 #flag.on{background:#2ecc71;color:#0a140e}
 main{padding:14px 18px;max-width:980px;margin:0 auto}
 img{width:100%;border-radius:10px;background:#000;display:block}
 .row{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px;color:#c6cfd8;font-size:14px}
 code{background:#1c232a;padding:1px 6px;border-radius:5px}
</style>
<header><div><h1>Dog Cam</h1><div class="sub">Pi camera · motion gate · Vision dog flag</div></div><div id="flag">NO DOG</div></header>
<main><img id="feed" alt="Live annotated camera feed">
<div class="row"><span id="status">starting…</span><span>Green box = dog flagged. Detection runs only when motion is seen.</span></div></main>
<script>
 const feed=document.getElementById('feed'), flag=document.getElementById('flag'), status=document.getElementById('status');
 setInterval(()=>{ feed.src='/annotated.jpg?t='+Date.now(); }, 1200);
 setInterval(async()=>{ try{ const s=await (await fetch('/api/status')).json();
   flag.textContent=s.flag.present?'DOG IN FRAME':'NO DOG'; flag.className=s.flag.present?'on':'';
   status.textContent=`motion ${s.motion} · last check ${s.checked_at?new Date(s.checked_at*1000).toLocaleTimeString():'—'} · confidence ${Math.round((s.flag.confidence||0)*100)}%`+(s.error?` · error: ${s.error}`:'');
 }catch(e){ status.textContent='server unreachable'; } }, 1200);
 feed.src='/annotated.jpg?t='+Date.now();
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send_bytes(self, code, body, ctype):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError): pass
    def do_GET(self):
        with lock:
            annotated, status = state["annotated"], {k: state[k] for k in ("flag", "motion", "checked_at", "frame_at", "error")}
        if self.path == "/":
            return self.send_bytes(200, PAGE.encode(), "text/html; charset=utf-8")
        if self.path.startswith("/annotated.jpg"):
            if not annotated: return self.send_bytes(503, b"no frame yet", "text/plain")
            return self.send_bytes(200, annotated, "image/jpeg")
        if self.path == "/api/status":
            return self.send_bytes(200, json.dumps(status).encode(), "application/json")
        return self.send_bytes(404, b"not found", "text/plain")


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    print(f"Dog Cam on http://localhost:{PORT}  (camera: {PI_CAM})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
