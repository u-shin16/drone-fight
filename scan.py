"""Map the eight mission pads by flying over them with the player's own Tello.

No photo and no guessed layout: the drone takes off from pad 4, sweeps a
rectangle the player has declared safe, reads each pad's id and its offset
from the pad centre with the downward camera, and keeps the map only if every
pad was seen several times in agreeing places and the loop back to pad 4
closes. Any doubt lands the drone and saves nothing.

Map coordinates (cm): pad 4 is the origin, x follows the rocket arrows (from
side A toward side B), y points to the right of them (toward pads 7-8).
"""

import math
import queue
import statistics
import threading
import time

import arena
import tello_net

SCAN_HEIGHT = 100     # cm; the pads are readable from 30-120 cm
SPEED = 60            # cm/s; 100 would overshoot, 40 made the scan slow
STEP = 50             # cm per move between readings
LANE = 60             # cm between sweep lanes (the camera sees about ±40 cm at 1 m)
EDGE = 30             # cm kept clear of the declared boundary
SETTLE = 0.4          # s hovering to take a reading when a pad is in view
SETTLE_EMPTY = 0.25   # s hovering before giving up on a stop with no pad in view
MIN_SEEN = 2          # separate stops at which every pad must be seen
DONE_SPREAD = 15      # cm: sightings this close ends the sweep early (the saved map allows MAX_SPREAD)
MAX_SPREAD = 25       # cm: a pad's sightings must agree this well
MAX_CLOSURE = 30      # cm: back over pad 4, our tracked position may be this far off
MAX_YAW = 20          # degrees away from the rockets before we give up
TIME_LIMIT = 360      # s at most for the whole scan (the route sets the actual limit)
SECONDS_PER_STOP = 4.5  # move + settle per stop, used to size the time limit
WALK_STEP = 20        # cm per hop while walking the square (the SDK's smallest move)
WALK_SPEED = 30       # cm/s: slow, so the camera reads every pad it passes
WALK_LEG_MAX = 260    # cm: a side of the square that long without a new pad means we are lost
WALK_PAST = 3         # hops taken past a pad's edge to cross it, then back to its middle
WALK_TIME = 300       # s at most for the whole walk
MANUAL_LIMIT = 400    # cm: how far from the origin pad a hand-flown scan may go (each way)
MANUAL_TIME = 480     # s at most for a hand-flown scan (a Tello's battery lasts about 10 minutes)
MANUAL_STEP_MAX = 100  # cm: the longest single move the player can order
MANUAL_MARGIN = 60    # cm kept around the flown area and pads when the field is worked out
START_BATTERY = 50
LOW_BATTERY = 25
STATE_TIMEOUT = 1.5   # s without a state packet = lost
RANGE_LIMITS = (50, 600)  # each boundary distance from pad 4, cm


def boundary(back: float, front: float, far: float, near: float) -> tuple[float, float, float, float]:
    """The safe rectangle around pad 4 as the field (x min, x max, y min, y max)."""
    values = {"後ろ": back, "前": front, "奥": far, "手前": near}
    for label, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{label}の距離を数字で入れてください")
        if not RANGE_LIMITS[0] <= value <= RANGE_LIMITS[1]:
            raise ValueError(f"{label}の距離は{RANGE_LIMITS[0]}〜{RANGE_LIMITS[1]}cmにしてください")
    return (-float(back), float(front), -float(far), float(near))


def lanes(y_min: float, y_max: float, upper: float | None = None) -> list[float]:
    """Sweep lines: the origin pad's own row (y = 0), then outward, LANE apart.

    With eight pads, the pads of the far row (1-3) and the near row (7-8) lie on
    both sides of the start row, so lanes alternate sides; working outward finds
    them all without flying to the far edge of a roomy boundary. 'upper' cuts off
    the near side when no pad can be there (the 4-pad square has none).
    """
    low, high = y_min + EDGE, y_max - EDGE
    if upper is not None:
        high = min(high, upper)
    found = [k * LANE for k in range(-int(abs(low) // LANE), int(max(high, 0) // LANE) + 1) if low <= k * LANE <= high]
    if not found:
        return [(low + high) / 2]
    # A boundary edge farther than half a lane from the nearest sweep line still needs one.
    for edge in (low, high):
        if min(abs(edge - y) for y in found) > LANE / 1.5:
            found.append(edge)
    return sorted(found, key=lambda y: (abs(y), y))


def route(field: tuple, kind: str = "pads8") -> list[tuple[float, float]]:
    """Back-and-forth sweeps, starting along the start row, keeping EDGE clear of the boundary."""
    x_min, x_max, y_min, y_max = field
    xs = _ticks(x_min + EDGE, x_max - EDGE, STEP)
    points = []
    # The 4-pad square 1 2 / 3 4 has nothing on the near side of the start row.
    for i, y in enumerate(lanes(y_min, y_max, upper=0 if kind == "square4" else None)):
        sweep = xs if i % 2 == 0 else list(reversed(xs))
        points += [(x, y) for x in sweep]
    return points


def _ticks(low: float, high: float, step: float) -> list[float]:
    if high < low:
        return [(low + high) / 2]
    count = max(1, math.ceil((high - low) / step))
    return [low + (high - low) * i / count for i in range(count + 1)]


def hops(start: tuple[float, float], end: tuple[float, float], step: float = STEP) -> list[tuple[float, float]]:
    """Moves of at most 'step' that take us from start to end."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    count = max(1, math.ceil(math.hypot(dx, dy) / step))
    return [(dx / count, dy / count)] * count


def _rot(v: tuple[float, float], angle: float) -> tuple[float, float]:
    c, s = math.cos(angle), math.sin(angle)
    return (c * v[0] - s * v[1], s * v[0] + c * v[1])


def _wrap(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


class PadMap:
    """Every sighting of every pad, and where that puts its centre.

    Normally the drone starts on the origin pad facing the rockets, so everything is in the
    pad's own axes. With free=True (a hand-flown scan) the drone may take off anywhere, facing
    any way: positions are kept in the takeoff frame (x = the nose, y = right), and the turn
    between that frame and the pads' axes (phi) is worked out from what the camera reports.
    """

    def __init__(self, kind: str = "pads8", free: bool = False) -> None:
        self.kind = kind
        self.free = free
        self.ids = tuple(int(k) for k in arena.SETS[kind]["ids"])
        # The pad that A takes off from is the origin of the map.
        self.origin = int(arena.SETS[kind]["starts"]["A"])
        self._sightings: dict[int, list[tuple[float, float]]] = {k: [] for k in self.ids}
        # free: (where we thought we were, [and by moves alone as the last item], where the pad says we are in its axes, our yaw to the pad in radians)
        self.raw: dict[int, list[tuple]] = {k: [] for k in self.ids}
        self._phi_cache: tuple[int, float] | None = None

    @property
    def sightings(self) -> dict[int, list[tuple[float, float]]]:
        if not self.free:
            return self._sightings
        phi = self.phi()
        out = {}
        for k in self.ids:
            out[k] = []
            for tracked, offset, *_ in self.raw[k]:
                r = _rot(offset, phi)
                out[k].append((tracked[0] - r[0], tracked[1] - r[1]))
        return out

    def phi(self) -> float:
        """The turn from the pads' axes to the takeoff frame (free scans), in radians."""
        if not self.free:
            return 0.0
        count = sum(len(v) for v in self.raw.values())
        if self._phi_cache and self._phi_cache[0] == count:
            return self._phi_cache[1]
        psis = [r[2] for v in self.raw.values() for r in v if r[2] is not None]
        phi = 0.0
        if psis:
            # The camera's yaw has an unknown sign; try both and let the drone's own moves decide.
            cands = []
            for sign in (1, -1):
                cands.append(math.atan2(sum(math.sin(-sign * p) for p in psis), sum(math.cos(-sign * p) for p in psis)))
            sin_sum = cos_sum = 0.0
            for v in self.raw.values():
                for i in range(len(v)):
                    for j in range(i + 1, len(v)):
                        dt = (v[j][3][0] - v[i][3][0], v[j][3][1] - v[i][3][1])  # moves alone: not bent by corrections
                        dp = (v[j][1][0] - v[i][1][0], v[j][1][1] - v[i][1][1])
                        if math.hypot(*dt) < 25 or math.hypot(*dp) < 25:
                            continue
                        w = math.hypot(*dp) ** 2
                        sin_sum += w * (dp[0] * dt[1] - dp[1] * dt[0]) / (math.hypot(*dp) * math.hypot(*dt))
                        cos_sum += w * (dp[0] * dt[0] + dp[1] * dt[1]) / (math.hypot(*dp) * math.hypot(*dt))
            if sin_sum or cos_sum:
                moved = math.atan2(sin_sum, cos_sum)
                phi = min(cands, key=lambda a: abs(_wrap(a - moved)))
            else:
                phi = cands[0]
        self._phi_cache = (count, phi)
        return phi

    def to_map(self, point: tuple[float, float]) -> tuple[float, float]:
        """A takeoff-frame point in the map's axes: origin pad at (0, 0), rockets to the right."""
        if not self.free:
            return point
        home = self.center(self.origin) or (0.0, 0.0)
        return _rot((point[0] - home[0], point[1] - home[1]), -self.phi())

    def from_map(self, delta: tuple[float, float]) -> tuple[float, float]:
        """A move in the map's axes as a move in the takeoff frame."""
        return _rot(delta, self.phi()) if self.free else delta

    def map_center(self, pad: int) -> tuple[float, float] | None:
        c = self.center(pad)
        return self.to_map(c) if c else None

    def center(self, pad: int) -> tuple[float, float] | None:
        if pad == self.origin and not self.free:
            return (0.0, 0.0)  # the origin, by definition
        seen = self.sightings[pad]
        if not seen:
            return None
        return (statistics.fmean(p[0] for p in seen), statistics.fmean(p[1] for p in seen))

    def observe(self, pad: int, offset: tuple[float, float], tracked: tuple[float, float],
                yaw: float | None = None, dead: tuple[float, float] | None = None) -> tuple[float, float]:
        """Record a sighting; returns our corrected position.

        'offset' is where we are relative to the pad centre (map axes), and
        'tracked' is where our own moves say we are. The centre implied by this
        sighting is tracked - offset; once the pad is known we re-anchor on it
        so move errors never pile up between pads. 'yaw' (degrees) and 'dead' (our position by moves alone, never corrected by a pad) only matter to a free scan.
        """
        if self.free:
            self.raw[pad].append((tracked, offset, math.radians(yaw) if yaw is not None else None, dead or tracked))
            cx, cy = self.center(pad)
            r = _rot(offset, self.phi())
            return (cx + r[0], cy + r[1])
        self._sightings[pad].append((tracked[0] - offset[0], tracked[1] - offset[1]))
        cx, cy = self.center(pad)
        return (cx + offset[0], cy + offset[1])

    def spread(self, pad: int) -> float:
        seen = self.sightings[pad]
        if not seen:
            return 0.0
        cx, cy = self.center(pad)
        return max(math.hypot(p[0] - cx, p[1] - cy) for p in seen)

    def solid(self, pad: int) -> bool:
        """Seen from enough separate stops, in places that agree (the origin pad is 0,0 by definition)."""
        if len(self.sightings[pad]) < MIN_SEEN:
            return False
        return (pad == self.origin and not self.free) or self.spread(pad) <= DONE_SPREAD

    def done(self) -> bool:
        return all(self.solid(k) for k in self.ids)

    def summary(self) -> dict:
        return {str(k): {"center": [round(v, 1) for v in self.map_center(k)] if self.map_center(k) else None,
                         "seen": len(self.sightings[k]), "spread": round(self.spread(k), 1)}
                for k in self.ids}


def build_layout(pads: PadMap, field: tuple, closure: float | None) -> dict:
    """The arena from a finished scan, or ValueError saying why it cannot be trusted."""
    ids, origin = pads.ids, pads.origin
    missing = [str(k) for k in ids if not pads.sightings[k]]
    if missing:
        raise ValueError(f"パッド{'・'.join(missing)}が見つかりませんでした")
    # A normal scan takes the origin pad as (0, 0) by definition and checks it by the loop back to it.
    few = [str(k) for k in ids if (k != origin or pads.free) and len(pads.sightings[k]) < MIN_SEEN]
    if few:
        raise ValueError(f"パッド{'・'.join(few)}は見えた回数が足りません（{MIN_SEEN}回以上必要）")
    for k in ids:
        if pads.spread(k) > MAX_SPREAD:
            raise ValueError(f"パッド{k}の位置が安定しません（ズレ{pads.spread(k):.0f}cm、{MAX_SPREAD}cmまで）")
    if closure is None:
        raise ValueError(f"最後にパッド{origin}へ戻って確認できませんでした")
    if closure > MAX_CLOSURE:
        raise ValueError(f"1周してパッド{origin}に戻ったときのズレが{closure:.0f}cmでした（{MAX_CLOSURE}cmまで）")
    centers = {str(k): [round(c, 1) for c in pads.map_center(k)] for k in ids}
    b_pad = arena.SETS[pads.kind]["starts"]["B"]
    return arena.normalize({"pads": centers, "field": list(field),
                            "starts": {"A": [*centers[str(origin)], 0], "B": [*centers[b_pad], 180]},
                            "scanned_at": tello_net.now()})


class ScanAbort(Exception):
    pass


class ArenaScanner:
    """Flies one scan in a background thread. One drone, before any battle."""

    def __init__(self, monitor, tello_port: int = tello_net.TELLO_PORT) -> None:
        self.monitor = monitor
        self.tello_port = tello_port
        self.lock = threading.Lock()
        self._clear()

    def _clear(self) -> None:
        self.phase = "idle"  # idle, running, done, failed
        self.message = ""
        self.ip: str | None = None
        self.field: tuple | None = None
        self.kind = "pads8"
        self.pads = PadMap()
        self.pose = (0.0, 0.0)
        self.dead = (0.0, 0.0)  # where the moves alone say we are
        self.closure: float | None = None
        self.log: list[str] = []
        self.stops_done = 0
        self.stops_total = 0
        self.started = 0.0
        self.time_limit = TIME_LIMIT
        self.stop_event = threading.Event()
        self.sock = None
        self.airborne = False
        self.manual = False  # hand-flown: the player orders each move
        self.walk = False    # walks the square by itself: straight to the next pad, turn right, repeat
        self.moving = False
        self.commands: queue.Queue = queue.Queue()
        self.last_sent = 0.0
        self.trail: list[tuple[float, float]] = []  # where we have been (tracked), to size the field

    @property
    def active(self) -> bool:
        return self.phase == "running"

    def start(self, ip: str, field: tuple, on_success, kind: str = "pads8") -> str | None:
        """Begin a scan of the 8-pad ('pads8') or 4-pad square ('square4') field.

        Returns an error message instead if it cannot start.
        """
        with self.lock:
            if self.active:
                return "スキャン中です"
            if kind not in arena.SETS:
                return "パッドの枚数は4枚か8枚にしてください"
            if not self.monitor.listening:
                return "高さやパッドの情報を受け取れません（8890番が使用中）"
            self._clear()
            self.kind = kind
            self.pads = PadMap(kind)
            self.phase, self.ip, self.field = "running", ip, field
            self.stops_total = len(route(field, kind))
            # Sweep, revisits and the trip home, with room to spare; never more than TIME_LIMIT.
            self.time_limit = min(TIME_LIMIT, 90 + SECONDS_PER_STOP * self.stops_total)
            self.started = time.monotonic()
        threading.Thread(target=self._run, args=(on_success,), daemon=True).start()
        return None

    def start_manual(self, ip: str, on_success, kind: str = "pads8") -> str | None:
        """Begin a scan the player flies by hand: they order each move, the app reads the pads.

        The safe field is worked out at the end from where the drone went. Returns an
        error message instead if it cannot start.
        """
        with self.lock:
            if self.active:
                return "スキャン中です"
            if kind not in arena.SETS:
                return "パッドの枚数は4枚か8枚にしてください"
            if not self.monitor.listening:
                return "高さやパッドの情報を受け取れません（8890番が使用中）"
            self._clear()
            self.kind = kind
            self.pads = PadMap(kind, free=True)
            self.manual = True
            self.phase, self.ip = "running", ip
            self.field = (-MANUAL_LIMIT, MANUAL_LIMIT, -MANUAL_LIMIT, MANUAL_LIMIT)  # a limit to stay inside
            self.time_limit = MANUAL_TIME
            self.started = time.monotonic()
        threading.Thread(target=self._run, args=(on_success,), daemon=True).start()
        return None

    def start_walk(self, ip: str, on_success, kind: str = "square4") -> str | None:
        """Walk the 4-pad square on its own: straight until the next pad, stop on it, turn right, repeat.

        The drone takes off over any one pad with its nose along a side of the square, the square
        on its right. Returns an error message instead if it cannot start.
        """
        with self.lock:
            if self.active:
                return "スキャン中です"
            if kind != "square4":
                return "四角を歩くスキャンは4枚（正方形）のときだけ使えます"
            if not self.monitor.listening:
                return "高さやパッドの情報を受け取れません（8890番が使用中）"
            self._clear()
            self.kind = kind
            self.pads = PadMap(kind, free=True)
            self.walk = True
            self.phase, self.ip = "running", ip
            self.field = (-MANUAL_LIMIT, MANUAL_LIMIT, -MANUAL_LIMIT, MANUAL_LIMIT)
            self.stops_total = len(self.pads.ids)
            self.time_limit = WALK_TIME
            self.started = time.monotonic()
        threading.Thread(target=self._run, args=(on_success,), daemon=True).start()
        return None

    def command(self, action: str, dx: float = 0.0, dy: float = 0.0) -> str | None:
        """An order from the player in a hand-flown scan; returns why it was refused, else None."""
        if not (self.active and self.manual and self.airborne):
            return "手動スキャンで飛んでいる間だけ使えます"
        if action == "finish":
            self.commands.put(("finish",))
            return None
        if action != "move":
            return "命令が不正です"
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (dx, dy)):
            return "動かす距離を数字で入れてください"
        if abs(dx) > MANUAL_STEP_MAX or abs(dy) > MANUAL_STEP_MAX or (abs(dx) < 20 and abs(dy) < 20):
            return f"20〜{MANUAL_STEP_MAX}cmで動かしてください"
        if self.moving or not self.commands.empty():
            return "いま動いています。止まってから押してください"
        self.commands.put(("move", float(dx), float(dy)))
        return None

    def stop(self) -> None:
        self.stop_event.set()
        sock = self.sock
        if sock and self.ip and self.airborne:  # halt a move in progress now, not after it ends
            try:
                sock.sendto(b"stop", (self.ip, self.tello_port))
            except OSError:
                pass

    def _live(self) -> dict | None:
        """What the drone's camera sees right now, for the player flying by hand."""
        reading = self.monitor.latest.get(self.ip or "")
        if not reading or time.monotonic() - reading["at"] > STATE_TIMEOUT:
            return None
        return {"mid": reading.get("mid"), "x": reading.get("x"), "y": reading.get("y"), "h": reading.get("h")}

    def status(self) -> dict:
        return {"phase": self.phase, "message": self.message, "log": self.log[-10:],
                "kind": self.kind, "origin": self.pads.origin,
                "pads": self.pads.summary(), "pose": [round(v) for v in self.pads.to_map(self.pose)],
                "field": list(self.field) if self.field else None,
                "live": self._live() if self.manual else None,
                "manual": self.manual, "walk": self.walk, "moving": self.moving, "airborne": self.airborne,
                "progress": [self.stops_done, self.stops_total], "time_limit": round(self.time_limit),
                "elapsed": round(time.monotonic() - self.started) if self.started else 0}

    # ---- flight ------------------------------------------------------------
    def _note(self, text: str) -> None:
        self.log.append(time.strftime("%H:%M:%S ") + text)

    def _send(self, command: str, timeout: float = 10) -> str | None:
        sock = self.sock
        sock.settimeout(0)
        try:
            while True:
                sock.recvfrom(1024)  # drop late replies
        except OSError:
            pass
        sock.settimeout(0.2)
        sock.sendto(command.encode(), (self.ip, self.tello_port))
        self.last_sent = time.monotonic()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, source = sock.recvfrom(1024)
            except OSError:
                continue
            if source[0] == self.ip:
                return data.decode("utf-8", "replace").strip()
        return None

    def _reading(self) -> dict:
        reading = self.monitor.latest.get(self.ip)
        if not reading or time.monotonic() - reading["at"] > STATE_TIMEOUT:
            raise ScanAbort("ドローンからの情報が途切れました")
        return reading

    def _guard(self) -> None:
        if self.stop_event.is_set():
            raise ScanAbort("停止ボタンで中止しました")
        if time.monotonic() - self.started > self.time_limit:
            raise ScanAbort(f"{self.time_limit:.0f}秒を超えたので中止しました")
        reading = self._reading()
        battery = reading.get("bat")
        if battery and str(battery).isdigit() and int(battery) < LOW_BATTERY:
            raise ScanAbort(f"電池が{LOW_BATTERY}%未満になりました")
        yaw = reading.get("pad_yaw")
        if not self.pads.free and reading.get("mid") in self.pads.ids and yaw is not None and abs(yaw) > MAX_YAW:
            raise ScanAbort(f"機体の向きがロケットから{abs(yaw)}°ずれました（{MAX_YAW}°まで）")

    def _look(self) -> tuple[int, tuple[float, float], float | None] | None:
        """Hover briefly, then the pad most seen and our median offset from its centre.

        A stop with nothing in view is left after SETTLE_EMPTY; once a pad shows up we
        stay for SETTLE so the median is taken over several readings.
        """
        began = time.monotonic()
        end = began + SETTLE_EMPTY
        seen: dict[int, list[tuple]] = {}
        last = None
        while time.monotonic() < end:
            self._guard()
            reading = self._reading()
            if reading["at"] != last:
                last = reading["at"]
                pad = reading.get("mid")
                if pad in self.pads.ids and reading.get("x") is not None and reading.get("y") is not None:
                    seen.setdefault(pad, []).append((reading["x"], reading["y"], reading.get("pad_yaw")))
                    end = max(end, began + SETTLE)
            time.sleep(0.05)
        if not seen:
            return None
        pad, offsets = max(seen.items(), key=lambda item: len(item[1]))
        # Pad y points left; the map's y points right.
        yaws = [o[2] for o in offsets if o[2] is not None]
        return (pad, (statistics.median(o[0] for o in offsets), -statistics.median(o[1] for o in offsets)),
                statistics.median(yaws) if yaws else None)

    def _hop(self, dx: float, dy: float) -> None:
        """One short move with the drone still facing the rockets."""
        x, y = self.pose[0] + dx, self.pose[1] + dy
        x_min, x_max, y_min, y_max = self.field
        if not (x_min + 10 <= x <= x_max - 10 and y_min + 10 <= y <= y_max - 10):
            raise ScanAbort("安全な範囲の外へ出そうになったので中止しました")
        command = f"go {round(dx)} {round(-dy)} 0 {SPEED}"  # go's y points left
        if all(-20 < v < 20 for v in (round(dx), round(-dy))):
            return  # too small for the SDK; skip it
        for attempt in range(3):
            self._guard()
            reply = self._send(command, 15)
            if reply == "ok":
                self.pose = (x, y)
                self.dead = (self.dead[0] + dx, self.dead[1] + dy)
                return
            self._note(f"{command} → {reply or '返事なし'}")
        raise ScanAbort("移動の命令が3回続けて失敗しました")

    def _take_reading(self) -> None:
        sighting = self._look()
        if sighting:
            pad, offset, yaw = sighting
            self.pose = self.pads.observe(pad, offset, self.pose, yaw, self.dead)
        self.trail.append(self.pose)

    def _run(self, on_success) -> None:
        try:
            self.sock = tello_net.tello_socket(0.2, self.ip)
            self._fly()
            self._land()
            if self.pads.free:
                self.field = self._worked_out_field()
            layout = build_layout(self.pads, self.field, self.closure)
            on_success(layout)
            self.phase, self.message = "done", f"{len(self.pads.ids)}枚の地図を保存しました"
            self._note(self.message)
        except (ScanAbort, ValueError) as exc:
            self.phase, self.message = "failed", f"{exc}。地図は保存していません"
            self._note(self.message)
        except Exception as exc:  # never leave the drone in the air
            self.phase, self.message = "failed", f"予期しないエラー（{exc}）。地図は保存していません"
            self._note(self.message)
        finally:
            if self.airborne:
                self._land()
            if self.sock:
                self.sock.close()
                self.sock = None

    def _fly(self) -> None:
        if self._send("command", 5) != "ok":
            raise ScanAbort("ドローンから返事がありません")
        battery = self._send("battery?", 5)
        if not battery or not battery.isdigit() or int(battery) < START_BATTERY:
            raise ScanAbort(f"電池が{START_BATTERY}%未満なのでスキャンしません（{battery}）")
        if self._send("mon", 5) != "ok" or self._send("mdirection 0", 5) != "ok":
            raise ScanAbort("ミッションパッドの検出をオンにできませんでした")
        if self.stop_event.is_set():
            raise ScanAbort("停止ボタンで中止しました")
        origin = self.pads.origin
        self._note(f"パッド{origin}から離陸します（電池 {battery}%）")
        self.airborne = True
        if self._send("takeoff", 20) != "ok":
            raise ScanAbort("離陸できませんでした")
        height = (self._reading().get("h") or 80)
        if SCAN_HEIGHT - height >= 20:
            self._send(f"up {round(SCAN_HEIGHT - height)}", 10)
        self._send(f"speed {SPEED}", 5)
        start = self._look()
        if self.manual or self.walk:
            if start:
                self.pose = self.pads.observe(start[0], start[1], (0.0, 0.0), start[2], (0.0, 0.0))
                self._note(f"離陸した場所でパッド{start[0]}が見えました")
            else:
                self._note("離陸した場所にパッドは見えません。動かして探してください")
        elif not start or start[0] != origin:
            seen = f"パッド{start[0]}" if start else "パッドなし"
            raise ScanAbort(f"離陸した場所でパッド{origin}が見えません（{seen}）。"
                            f"パッド{origin}の中心から離陸してください")
        else:
            self.pose = self.pads.observe(origin, start[1], (0.0, 0.0))
            self._note(f"パッド{origin}を確認。スキャンを始めます")

        if self.walk:
            if not start:
                raise ScanAbort("離陸した場所でパッドが見えません。どれか1枚の真ん中から離陸してください")
            self.trail.append(self.pose)
            self._send(f"speed {WALK_SPEED}", 5)
            self._walk(start[0])
            return
        if self.manual:
            self._manual_loop()
        else:
            for point in route(self.field, self.kind):
                self._go_to(point)
                self.stops_done += 1
                if self.pads.done():
                    self._note(f"{len(self.pads.ids)}枚とも十分に見えたので、パッド{origin}へ戻ります")
                    break
            self._revisit()
        # Back to the origin pad, reading pads on the way. How far our tracked position is
        # from it when we get there shows whether the map can be trusted.
        home = self.pads.center(origin) if self.manual else (0.0, 0.0)
        for dx, dy in hops(self.pose, home):
            self._hop(dx, dy)
            if self.pose != home:
                self._take_reading()
        tracked = self.pose
        sighting = self._look()
        if sighting and sighting[0] == origin and self.manual:
            c, r = self.pads.center(origin), _rot(sighting[1], self.pads.phi())
            self.closure = math.hypot(tracked[0] - (c[0] + r[0]), tracked[1] - (c[1] + r[1]))  # before this sighting joins the map
            self.pose = self.pads.observe(origin, sighting[1], tracked, sighting[2], self.dead)
            self._note(f"パッド{origin}に戻りました（1周のズレ {self.closure:.0f}cm）")
        elif sighting and sighting[0] == origin:
            offset = sighting[1]
            self.closure = math.hypot(tracked[0] - offset[0], tracked[1] - offset[1])
            self.pose = offset  # the origin pad is (0, 0): this is exactly where we are
            self._note(f"パッド{origin}に戻りました（1周のズレ {self.closure:.0f}cm）")
        else:
            self._note(f"戻った場所でパッド{origin}が見えませんでした")

    def _sight(self) -> tuple[int, tuple[float, float], float | None] | None:
        """Look once, record what is seen and keep our position corrected by it."""
        sighting = self._look()
        if sighting:
            pad, offset, yaw = sighting
            self.pose = self.pads.observe(pad, offset, self.pose, yaw, self.dead)
        self.trail.append(self.pose)
        return sighting

    def _walk_step(self, direction: tuple[float, float]) -> None:
        """One hop along 'direction' (a unit vector), long enough that the SDK does not ignore it."""
        k = WALK_STEP / max(abs(direction[0]), abs(direction[1]))
        self._hop(direction[0] * k, direction[1] * k)

    def _walk_to(self, target: tuple[float, float]) -> None:
        """Back to a point on the pad, reading as we go; moves too small for the SDK are not made."""
        for _ in range(6):
            dx, dy = target[0] - self.pose[0], target[1] - self.pose[1]
            big = max(abs(dx), abs(dy))
            if big < WALK_STEP:
                return
            count = max(1, min(math.ceil(math.hypot(dx, dy) / 50), int(big // WALK_STEP)))
            self._hop(dx / count, dy / count)
            self._sight()

    def _walk(self, start: int) -> None:
        """Around the square: hop straight on, stop on the next pad, turn right (in the map), repeat.

        The drone does not really turn: flying the same sides without spinning keeps the camera's
        reference steady and avoids a turn's error. After the first side, each side's direction is
        taken from the two pads just measured, so a nose that was a little off only costs the first side.
        """
        visited, current = [start], start
        direction = (1.0, 0.0)  # the nose, in the takeoff frame
        for leg in range(4):
            wanted = {start} if leg == 3 else set(self.pads.ids) - set(visited)
            self._note(f"パッド{current}から次のパッドへまっすぐ進みます（{leg + 1}/4辺目）")
            found = None
            for _ in range(WALK_LEG_MAX // WALK_STEP):
                self._walk_step(direction)
                before = self.pose
                sighting = self._look()
                if sighting:
                    pad, offset, yaw = sighting
                    if pad == start and leg == 3 and self.closure is None:
                        c, r = self.pads.center(pad), _rot(offset, self.pads.phi())
                        self.closure = math.hypot(before[0] - (c[0] + r[0]), before[1] - (c[1] + r[1]))
                    self.pose = self.pads.observe(pad, offset, self.pose, yaw, self.dead)
                    if pad in wanted:
                        found = pad
                    elif pad != current:
                        raise ScanAbort(f"予想外のパッド{pad}が見えました。パッドの並びか、離陸したときの向きを確かめてください")
                self.trail.append(self.pose)
                if found:
                    break
            if found is None:
                raise ScanAbort(f"{WALK_LEG_MAX}cm進んでも次のパッドが見つかりませんでした")
            # Cross the pad so it is read from both sides of its middle, then go back to the middle.
            for _ in range(WALK_PAST):
                self._walk_step(direction)
                seen = self._sight()
                if not seen or seen[0] != found:
                    break
            self._walk_to(self.pads.center(found))
            self._note(f"パッド{found}の真ん中で止まりました")
            visited.append(found)
            self.stops_done += 1
            if leg < 3:
                here, there = self.pads.center(current), self.pads.center(found)
                side = (there[0] - here[0], there[1] - here[1])
                length = math.hypot(*side)
                if length > 40:
                    direction = (side[0] / length, side[1] / length)
                direction = (-direction[1], direction[0])  # turn right
            current = found
        if self.closure is None:
            raise ScanAbort("最初のパッドに戻ったことを確認できませんでした")
        self._note(f"四角を1周しました（1周のズレ {self.closure:.0f}cm）")

    def _manual_loop(self) -> None:
        """Wait for the player's orders; 'finish' is accepted once every pad is measured well enough."""
        self._note("手動で動かしてください。全部のパッドの上を2回以上通ったら「終わって戻る」を押します")
        while True:
            self._guard()
            try:
                order = self.commands.get(timeout=0.2)
            except queue.Empty:
                if time.monotonic() - self.last_sent > 4:  # a Tello lands by itself after 15 s of silence
                    self._send("battery?", 3)
                continue
            if order[0] == "finish":
                problem = self._not_measured()
                if problem:
                    self._note(problem)
                    continue
                self._note("全部のパッドを測れました。基準のパッドへ戻ります")
                return
            self.moving = True
            try:
                # The player's arrows are in the map's axes; the drone flies in its takeoff frame.
                d = self.pads.from_map((order[1], order[2]))
                self._go_to((self.pose[0] + d[0], self.pose[1] + d[1]))
                self.stops_done += 1
            finally:
                self.moving = False

    def _not_measured(self) -> str | None:
        """Why a hand-flown scan cannot finish yet, or None when every pad is measured well enough."""
        for pad in self.pads.ids:
            if (pad != self.pads.origin or self.pads.free) and len(self.pads.sightings[pad]) < MIN_SEEN:
                return f"まだ終われません: パッド{pad}の上を通った回数が足りません（{MIN_SEEN}回以上必要）"
            if self.pads.spread(pad) > MAX_SPREAD:
                return (f"まだ終われません: パッド{pad}の位置がそろいません（ズレ{self.pads.spread(pad):.0f}cm）。"
                        "もう一度、別の向きから通ってください")
        return None

    def _worked_out_field(self) -> tuple[float, float, float, float]:
        """The area the drone flew and the pads cover, plus a margin: the field of a hand-flown scan."""
        points = [self.pads.to_map(p) for p in self.trail] + [c for c in (self.pads.map_center(k) for k in self.pads.ids) if c]
        xs, ys = [p[0] for p in points] + [0.0], [p[1] for p in points] + [0.0]
        return (round(min(xs) - MANUAL_MARGIN, 1), round(max(xs) + MANUAL_MARGIN, 1),
                round(min(ys) - MANUAL_MARGIN, 1), round(max(ys) + MANUAL_MARGIN, 1))

    def _go_to(self, point: tuple[float, float]) -> None:
        # Look after every hop, not only at the end: a long way (changing sweep lane,
        # fetching a pad again) passes pads, and each one seen corrects the drift.
        for dx, dy in hops(self.pose, point):
            self._hop(dx, dy)
            self._take_reading()

    def _revisit(self) -> None:
        """Fly back over pads seen too few times, so each is measured from more than one stop."""
        x_min, x_max, y_min, y_max = self.field
        for pad in self.pads.ids:
            for nudge in ((0, 0), (15, 0), (0, 15)):
                center = self.pads.center(pad)
                if center is None or self.pads.solid(pad):
                    break
                target = (min(max(center[0] + nudge[0], x_min + EDGE), x_max - EDGE),
                          min(max(center[1] + nudge[1], y_min + EDGE), y_max - EDGE))
                self._note(f"パッド{pad}をもう一度見に行きます")
                self._go_to(target)

    def _land(self) -> None:
        if not self.airborne or not self.sock:
            return
        for _ in range(4):
            if self._send("land", 10) == "ok":
                break
        self.airborne = False
        self._note("着陸しました")
