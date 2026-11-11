"""Fly the hand-flight page's API against the fake Tello: take off, move, land, and the refusals."""
import json, os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
PY = sys.executable
SP = Path(tempfile.mkdtemp(prefix="tello-fly-"))
(SP / "world.json").write_text(json.dumps({"pads": {"1": [0, 0]}}))
(SP / "data").mkdir()
fake = subprocess.Popen([PY, str(APP / "tests/fake_pad_tello.py"), "9061", "9161", "FAKE", "5", "5", "0",
                         str(SP / "true.json"), "1", str(SP / "world.json")],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
app = subprocess.Popen([PY, "app.py"], cwd=APP, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env={"PORT": "5101", "TELLO_PORT": "9061", "STATE_PORT": "9161", "TELLO_LOCAL_PORT": "0",
                            "SCAN_HOSTS": "127.0.0.1", "PEER_PORT": "47501",
                            "TELLO_CONNECT_DATA": str(SP / "data"), "PATH": "/usr/bin:/bin"})


def call(path, body=None):
    url = f"http://127.0.0.1:5101{path}"
    if body is None:
        return json.load(urllib.request.urlopen(url))
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))


def truth():
    return json.loads((SP / "true.json").read_text())


try:
    time.sleep(3)
    call("/api/scan", {}); time.sleep(3)
    assert call("/api/fly/start", {})["ok"] is False  # no drone chosen yet
    sn = call("/api/state")["drones"][0]["sn"]
    assert call("/api/claim", {"sn": sn})["ok"]
    assert call("/api/fly/move", {"cmd": "forward", "n": 40})["ok"] is False  # not flying
    assert call("/api/fly/view", {})["ok"]  # the camera alone: it does not take off
    time.sleep(2)
    f = call("/api/state")["fly"]; assert f["phase"] == "viewing" and not truth()["flying"], f
    assert call("/api/fly/start", {})["ok"] is False  # busy looking
    call("/api/fly/land", {})
    for _ in range(50):
        if call("/api/state")["fly"]["phase"] == "done": break
        time.sleep(0.2)
    assert call("/api/state")["fly"]["phase"] == "done"
    r = call("/api/fly/start", {}); assert r["ok"], r
    for _ in range(100):
        if call("/api/state")["fly"]["phase"] == "flying": break
        time.sleep(0.2)
    f = call("/api/state")["fly"]; assert f["phase"] == "flying", f
    for _ in range(50):  # the fake writes its true state every 50 ms
        if truth()["flying"]: break
        time.sleep(0.1)
    assert truth()["flying"]
    assert call("/api/fly/start", {})["ok"] is False  # already flying
    assert call("/api/arena/scan/walk", {})["ok"] is False  # the drone is in use
    x0 = truth()["x"]
    assert call("/api/fly/move", {"cmd": "forward", "n": 50})["ok"]
    assert call("/api/fly/move", {"cmd": "forward", "n": 50})["ok"] is False  # still moving
    assert call("/api/fly/move", {"cmd": "forward", "n": 500})["ok"] is False  # too far
    assert call("/api/fly/move", {"cmd": "spin", "n": 40})["ok"] is False
    while call("/api/state")["fly"]["moving"] or truth()["x"] == x0:
        time.sleep(0.2)
    time.sleep(1)
    moved = truth()["x"] - x0
    print("moved", round(moved), "cm; live:", call("/api/state")["fly"]["live"])
    assert 35 < moved < 65, moved
    # the attack-drn chase takes over while flying (the fake drone has no camera, so it only searches)
    assert call("/api/fly/chase", {"marker": "purple"})["ok"] is False
    assert call("/api/fly/chase", {"marker": "green"})["ok"]
    for _ in range(50):
        if call("/api/state")["fly"]["chasing"]: break
        time.sleep(0.2)
    f = call("/api/state")["fly"]; assert f["chasing"], f
    assert call("/api/fly/move", {"cmd": "forward", "n": 40})["ok"] is False  # no stick moves while chasing
    time.sleep(3)
    assert truth()["flying"]
    call("/api/fly/chase/stop", {})
    for _ in range(100):
        if call("/api/state")["fly"]["phase"] == "done": break
        time.sleep(0.2)
    f = call("/api/state")["fly"]; print(f["phase"], f["message"], "|", f["chase_message"]); print("\n".join(f["log"]))
    for _ in range(50):  # the fake writes its true state every 50 ms
        if not truth()["flying"]: break
        time.sleep(0.1)
    assert f["phase"] == "done" and not truth()["flying"] and not f["chasing"]
    print("OK")
finally:
    app.terminate(); fake.terminate()
