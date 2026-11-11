"""Fly a whole field scan on the fake Tello and check the saved map against the truth.

argv: scenario [index]
  normal     every pad present, sloppy moves (MOVE_ERROR 10 %, ANGLE_ERROR 5°)
  rough      sloppier moves (20 %, 10°)
  missing7   pad 7 is not on the floor
  state-drop the drone stops sending state 2 s after takeoff
  stop       the stop button is pressed mid-scan
  start5     the drone is put on pad 5 instead of pad 4
  yaw        the body slowly turns away from the rockets
  square4    four pads in a square, 1 2 / 3 4, taking off from pad 3
  manual     8 pads, hand-flown: a pilot script orders the moves, like the player's buttons
  manual4    the square, hand-flown
  angle      8 pads, hand-flown, taken off beside pad 4 facing 55 degrees away from the rockets
  angle4     the square, the same but facing -130 degrees and the camera's yaw sign flipped
The fake world puts pad 4 away from (0, 0) so the "relative to pad 4" maths is tested too.
"""
import json, math, os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

scenario = sys.argv[1]
index = int(sys.argv[2]) if len(sys.argv) > 2 else 0
APP = Path(__file__).resolve().parents[1]
PY = sys.executable
SP = Path(tempfile.mkdtemp(prefix=f"tello-scan-{scenario}-"))
SQUARE = scenario in ("square4", "manual4", "angle4")
MANUAL = scenario.startswith(("manual", "angle"))
PAD4 = (37.0, -12.0)  # where the origin pad is (pad 3 for the square)
# 1 2 3 / 4 5 6 / 7 8, irregular, relative to pad 4.
RELATIVE = {1: (-40, -110), 2: (60, -120), 3: (170, -100), 4: (0, 0),
            5: (90, 10), 6: (180, 5), 7: (30, 115), 8: (150, 120)}
if SQUARE:
    RELATIVE = {1: (-3, -90), 2: (118, -85), 3: (0, 0), 4: (120, 5)}
world = {str(k): [PAD4[0] + x, PAD4[1] + y] for k, (x, y) in RELATIVE.items()
         if not (scenario == "missing7" and k == 7)}
(SP / "world.json").write_text(json.dumps({"pads": world}))
start = RELATIVE[5] if scenario == "start5" else (0, 0)
FACING = 0
if scenario.startswith("angle"):
    start, FACING = (14, 10), (55 if scenario == "angle" else -130)
tello_port, state_port, port, peer_port = 9031 + index, 9131 + index, 5081 + index, 47480 + index
env = dict(os.environ, FAKE_MOVE_ERROR="0.2" if scenario == "rough" else "0.1",
           FAKE_ANGLE_ERROR="10" if scenario == "rough" else "5",
           FAKE_YAW_DRIFT="2.5" if scenario == "yaw" else "0",
           FAKE_YAW_SIGN="-1" if scenario == "angle4" else "1")
fake = subprocess.Popen([PY, str(APP / "tests/fake_pad_tello.py"), str(tello_port), str(state_port), "FAKE",
                         str(PAD4[0] + start[0] + 6), str(PAD4[1] + start[1] - 4), str(FACING), str(SP / "true.json"),
                         "state-drop" if scenario == "state-drop" else "1", str(SP / "world.json")],
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
(SP / "data").mkdir()
app = subprocess.Popen([PY, "app.py"], cwd=APP, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env={"PORT": str(port), "TELLO_PORT": str(tello_port), "STATE_PORT": str(state_port),
                            "TELLO_LOCAL_PORT": "0", "SCAN_HOSTS": "127.0.0.1", "PEER_PORT": str(peer_port),
                            "TELLO_CONNECT_DATA": str(SP / "data"), "PATH": "/usr/bin:/bin"})


def call(path, body=None):
    url = f"http://127.0.0.1:{port}{path}"
    if body is None:
        return json.load(urllib.request.urlopen(url))
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))


try:
    time.sleep(3)
    call("/api/scan", {})
    time.sleep(3)
    sn = call("/api/state")["drones"][0]["sn"]
    assert call("/api/claim", {"sn": sn})["ok"]
    began = time.time()
    if MANUAL:
        reply = call("/api/arena/scan/manual", {"pads": 4 if SQUARE else 8})
    else:
        reply = call("/api/arena/scan", {"pads": 4 if SQUARE else 8, "back": 80, "front": 260, "far": 170, "near": 180})
    assert reply["ok"], reply
    if MANUAL:
        # The player's job: wait until it is up, then order moves over every pad twice, then finish.
        origin = 3 if SQUARE else 4
        while call("/api/state")["arena_scan"]["airborne"] is False:
            time.sleep(0.3)
            assert time.time() - began < 60, "never took off"
        visits = []
        for k, (x, y) in RELATIVE.items():
            if True:
                visits += [(x, y), (x + 25, y + 20)]  # the second pass comes from another side
        for target in visits:
            for _ in range(40):
                sc0 = call("/api/state")["arena_scan"]
                px, py = sc0["pose"][0], sc0["pose"][1]
                dx, dy = target[0] - px, target[1] - py
                if math.hypot(dx, dy) < 25:
                    break
                scale = min(1.0, 80 / max(abs(dx), abs(dy)))
                step = {"dx": round(dx * scale), "dy": round(dy * scale)}
                if abs(step["dx"]) < 20 and abs(step["dy"]) < 20:
                    break
                done = sc0["progress"][0]
                r = call("/api/arena/scan/move", step)
                assert r["ok"], r
                while call("/api/state")["arena_scan"]["progress"][0] == done:
                    time.sleep(0.2)
                    assert time.time() - began < 300, "move never finished"
        # Like the player: look at the counts and go back over any pad that was passed too few times.
        for attempt in range(4):
            sc0 = call("/api/state")["arena_scan"]
            lacking = [k for k, v in sc0["pads"].items() if v["seen"] < 2]
            print("pads seen:", {k: v["seen"] for k, v in sc0["pads"].items()}, "lacking:", lacking)
            if not lacking:
                break
            for k in lacking:
                cx, cy = sc0["pads"][k]["center"] or RELATIVE[int(k)]  # a player knows roughly where a pad is
                for target in ((cx + 15 * (attempt + 1), cy), (cx, cy + 15 * (attempt + 1))):
                    for _ in range(20):
                        px, py = call("/api/state")["arena_scan"]["pose"][:2]
                        dx, dy = target[0] - px, target[1] - py
                        if abs(dx) < 20 and abs(dy) < 20:
                            break
                        scale = min(1.0, 80 / max(abs(dx), abs(dy)))
                        done = call("/api/state")["arena_scan"]["progress"][0]
                        assert call("/api/arena/scan/move", {"dx": round(dx * scale), "dy": round(dy * scale)})["ok"]
                        while call("/api/state")["arena_scan"]["progress"][0] == done:
                            time.sleep(0.2)
        print("finish:", call("/api/arena/scan/finish", {}))
    stopped = False
    while True:
        s = call("/api/state")
        sc = s["arena_scan"]
        if sc["phase"] != "running":
            break
        if scenario == "stop" and time.time() - began > 15 and not stopped:
            call("/api/arena/scan/stop", {}); stopped = True
        if time.time() - began > (440 if MANUAL else 240):
            print("\n".join(sc["log"][-6:]))
            raise SystemExit("scan did not finish in time")
        time.sleep(0.5)
    time.sleep(1)
    true = json.loads((SP / "true.json").read_text())
    print(f"[{scenario}] {sc['phase']} in {time.time() - began:.0f}s: {sc['message']}")
    print(f"  stops {sc['progress'][0]}/{sc['progress'][1]}, landed: {not true['flying']}, "
          f"saved map: {bool(s['arena'])}")
    if s["arena"]:
        errors = {k: math.dist(s["arena"]["pads"][k], RELATIVE[int(k)]) for k in s["arena"]["pads"]}
        print("  pad error cm: " + ", ".join(f"{k}:{e:.0f}" for k, e in sorted(errors.items()))
              + f"  (max {max(errors.values()):.0f})")
    print("  " + "\n  ".join(sc["log"][-4:]))
finally:
    if "hold" in sys.argv:
        time.sleep(float(os.environ.get("HOLD", 20)))
    app.terminate(); fake.terminate()
