"""Learner tests: build synthetic OOK edges and check the code is recovered."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dogtrainer import learn_from_capture


def edges_for(code_hex, frames=4):
    edges, t = [], 1000.0
    bits = f"{int(code_hex, 16):032b}"
    for _ in range(frames):
        # sync: short high, long low
        edges += [[t, 1], [t + 400, 0]]; t += 400 + 11000
        for bit in bits:
            hi, lo = (400, 1100) if bit == "0" else (1100, 400)
            edges += [[t, 1], [t + hi, 0]]; t += hi + lo
    return {"config": {"frequency_mhz": 433.92, "modulation": "OOK"}, "edges": edges}


def test_recovers_code():
    found = learn_from_capture(edges_for("0x545904B9"))
    assert found and found["code"] == "0x545904B9", found
    assert found["matching_frames"] >= 2


def test_ignores_non_ook():
    cap = edges_for("0x12345678")
    cap["config"]["modulation"] = "2-FSK"
    assert learn_from_capture(cap) is None


if __name__ == "__main__":
    test_recovers_code(); test_ignores_non_ook(); print("ok")
