"""Regression test: upside-down Pi frames are rotated 180 before use."""
import io, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dogcam"))
from PIL import Image
from server import upright_jpeg


def test_upright_flips_image():
    src = Image.new("RGB", (40, 20), (10, 10, 10))
    for x in range(10):
        for y in range(5):
            src.putpixel((x, y), (255, 0, 0))  # top-left red block must end up bottom-right
    buf = io.BytesIO(); src.save(buf, "JPEG", quality=95)
    _, img = upright_jpeg(buf.getvalue())
    r, g, b = img.getpixel((35, 17))
    assert r > 150 and g < 120 and b < 120, f"expected red at bottom-right, got {(r, g, b)}"


if __name__ == "__main__":
    test_upright_flips_image(); print("ok")
