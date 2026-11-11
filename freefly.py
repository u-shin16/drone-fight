"""Fly the chosen drone by hand, with no battle and no map.

Built on the pad_test.py a friend sent: take off, hold the hover, accept small stick commands
one at a time, keep the link alive, and show the mission pad under the drone. It never runs
while a battle or a scan is using the drone.
"""

import queue
import threading
import time

import camera
import tello_net

MIN_BATTERY = 20
LOW_BATTERY = 12
FLIGHT_SECONDS = 540   # a Tello's battery lasts about 10 minutes
STATE_TIMEOUT = 1.5
HEIGHT_RANGE = (30, 250)  # cm; the drone does not go outside it
LIMITS = {
    "forward": (20, 100), "back": (20, 100), "left": (20, 100), "right": (20, 100),
    "up": (20, 100), "down": (20, 100), "cw": (15, 90), "ccw": (15, 90),
}


class FreeFlight:
    def __init__(self, monitor, tello_port: int = tello_net.TELLO_PORT) -> None:
        self.monitor = monitor
        self.tello_port = tello_port
        self.lock = threading.Lock()
        self.sock = None
        self.camera = camera.Camera()
        self._idle()

    def _idle(self) -> None:
        self.phase = "idle"  # idle, starting, flying, landing, done
        self.ip: str | None = None
        self.airborne = False
        self.cut = False
        self.moving = False
        self.message = ""
        self.log: list[str] = []
        self.started = 0.0
        self.chasing = False  # the attack-drn colour chase is flying the drone
        self.chase_message = ""
        self._chase_req: str | None = None
        self._chase_stop = threading.Event()
        self._moves: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._last_sent = 0.0

    def busy(self) -> bool:
        return self.phase in ("starting", "flying", "landing")

    def start(self, ip: str, chase: str | None = None) -> str | None:
        """Take off. With chase="green", start the colour chase as soon as it is up."""
        with self.lock:
            if self.busy():
                return "もう飛んでいます"
            if not self.monitor.listening:
                return "高さやパッドの情報を受け取れません（8890番が使用中）"
            self._idle()
            self.phase, self.ip, self.started = "starting", ip, time.monotonic()
            self._chase_req = chase
            self.message = "接続しています…"
        threading.Thread(target=self._run, daemon=True).start()
        return None

    def move(self, command: str, distance) -> str | None:
        """Queue one move; returns why it was refused, else None."""
        if command not in LIMITS or isinstance(distance, bool) or not isinstance(distance, (int, float)):
            return "その操作はできません"
        distance = int(distance)
        low, high = LIMITS[command]
        if not low <= distance <= high:
            return f"{low}〜{high}で動かしてください"
        if self.phase != "flying":
            return "飛んでいる間だけ操作できます"
        if self.chasing:
            return "追いかけている間は操作できません"
        height = self._height()
        if command == "up" and height is not None and height + distance > HEIGHT_RANGE[1]:
            return f"{HEIGHT_RANGE[1]}cmより上には上げません"
        if command == "down" and height is not None and height - distance < HEIGHT_RANGE[0]:
            return f"{HEIGHT_RANGE[0]}cmより下には下げません"
        if self.moving or not self._moves.empty():
            return "いま動いています。止まってから押してください"
        self._moves.put(f"{command} {distance}")
        return None

    def chase(self, marker: str = "green") -> str | None:
        """While flying: hand the drone to the attack-drn chase (chases the colour, charges forward)."""
        if self.phase != "flying":
            return "飛んでいる間だけ使えます"
        if self.chasing or self._chase_req:
            return "もう追いかけています"
        self._chase_req = marker
        return None

    def stop_chase(self) -> None:
        """Stop chasing; the chase ends by landing, as in the original."""
        self._chase_req = None
        self._chase_stop.set()

    def send(self, command: str, timeout: float = 12) -> str | None:
        return self._send(command, timeout)

    def raw(self, command: str) -> None:
        self._raw(command)

    def land_now(self) -> None:
        self._land()

    def land(self) -> None:
        self._chase_stop.set()
        self._stop.set()
        self._raw("stop")

    def emergency(self) -> None:
        """Cut the motors now (the drone drops)."""
        self._stop.set()
        self._chase_stop.set()
        self.cut = True
        for _ in range(3):
            self._raw("emergency")
        self.airborne = False
        self.phase = "done"
        self.message = "緊急停止を送りました（プロペラ停止）"
        self._note(self.message)

    def shutdown(self) -> None:
        """The app is closing: land even though the flight thread is about to die."""
        if not self.busy():
            return
        self._stop.set()
        self._chase_stop.set()
        self.cut = True
        self.sock = self.sock or tello_net.tello_socket(0.5, self.ip)
        for _ in range(5):
            self._raw("land")
            time.sleep(0.3)

    def _height(self) -> int | None:
        reading = self.monitor.latest.get(self.ip or "")
        return reading.get("h") if reading and time.monotonic() - reading["at"] < STATE_TIMEOUT else None

    def public(self) -> dict:
        reading = self.monitor.latest.get(self.ip or "") if self.ip else None
        fresh = bool(reading) and time.monotonic() - reading["at"] < STATE_TIMEOUT
        live = {"mid": reading.get("mid"), "x": reading.get("x"), "y": reading.get("y"),
                "z": reading.get("z"), "h": reading.get("h"), "bat": reading.get("bat")} if fresh else None
        return {"phase": self.phase, "message": self.message, "airborne": self.airborne,
                "moving": self.moving, "chasing": self.chasing, "chase_message": self.chase_message, "live": live, "camera": self.camera.status(), "log": self.log[-8:],
                "elapsed": round(time.monotonic() - self.started) if self.started and self.busy() else 0,
                "limit": FLIGHT_SECONDS}

    def _note(self, text: str) -> None:
        self.log.append(time.strftime("%H:%M:%S ") + text)

    def _raw(self, command: str) -> None:
        if self.sock and self.ip:
            try:
                self.sock.sendto(command.encode(), (self.ip, self.tello_port))
            except OSError:
                pass

    def _send(self, command: str, timeout: float = 12) -> str | None:
        sock = self.sock
        if not sock or not self.ip:
            return None
        sock.settimeout(0)
        try:
            while True:
                sock.recvfrom(1024)  # drop late replies
        except OSError:
            pass
        sock.settimeout(0.2)
        self._last_sent = time.monotonic()
        self._raw(command)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stop.is_set() and command not in ("land", "stop"):
                return None
            try:
                data, source = sock.recvfrom(1024)
            except OSError:
                continue
            if source[0] == self.ip:
                return data.decode("utf-8", "replace").strip()
        return None

    def _run(self) -> None:
        self.sock = tello_net.tello_socket(0.2, self.ip)
        try:
            if self._send("command", 5) != "ok":
                return self._finish("ドローンから返事がありません")
            battery = self._send("battery?", 5)
            if not battery or not battery.isdigit() or int(battery) < MIN_BATTERY:
                return self._finish(f"電池が{MIN_BATTERY}%未満なので離陸しません（{battery}）")
            self._send("mon", 5)  # so the pad under the drone shows up; not needed to fly
            self._send("mdirection 0", 5)
            self._note(f"離陸します（電池 {battery}%）")
            self.airborne = True
            if self._send("takeoff", 20) != "ok":
                self._send("land", 8)
                return self._finish("離陸できませんでした")
            self.phase = "flying"
            for _ in range(3):  # the front camera, shown on the page
                self.camera.reply = self._send("streamon", 5) or "返事なし"
                if self.camera.reply == "ok":
                    break
                time.sleep(0.5)
            self._note(f"カメラを起動: {self.camera.reply}")
            self.camera.start()
            self.message = "飛んでいます。矢印キーかボタンで動かせます"
            self._note("離陸しました")
            self._loop()
        finally:
            if self.airborne and not self.cut:
                self._land()
            self.camera.stop()
            sock, self.sock = self.sock, None
            if sock:
                sock.close()

    def _run_chase(self, marker: str) -> None:
        """attack-drn の追跡（tello_edu/）をそのまま動かす。終わりは元と同じく着陸。"""
        self.chasing, self.chase_message = True, f"{marker}色を追いかけています"
        self._note(self.chase_message)
        self._chase_stop = threading.Event()
        try:
            from tello_edu.battle.auto_battle import AutoBattleConfig, AutoBattleSession
            from tello_edu.battle.profiles import profile_from_settings
            from tello_edu.battle.settings import load_settings
            from tello_edu.session.edu_drone import EduDrone

            settings = load_settings()
            drone = EduDrone(self)
            session = AutoBattleSession(
                drone=drone, settings=settings, profile=profile_from_settings(settings),
                config=AutoBattleConfig(marker=marker, stop_event=self._chase_stop),
                frame_source=drone.read_frame, frame_sink=self.camera.show,  # the page sees what it sees
                keep_video_alive=True)
            result = session.run()
            reason = f"（{result.get('reason')}）" if result.get("reason") else ""
            self.chase_message = f"追跡が終わりました: {result.get('status')}{reason}"
        except Exception as exc:  # the drone must not be left flying
            self.chase_message = f"追跡に失敗しました: {exc}"
        finally:
            self.chasing = False
            self.camera.clear_overlay()
            self._note(self.chase_message)
            if self.airborne and not self.cut:
                self._raw("rc 0 0 0 0")

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._chase_req:
                marker, self._chase_req = self._chase_req, None
                self._run_chase(marker)
                if not self.airborne:
                    return
                continue
            if time.monotonic() - self.started > FLIGHT_SECONDS:
                self._note("時間が来たので着陸します")
                return
            reading = self.monitor.latest.get(self.ip or "")
            bat = reading.get("bat") if reading else None
            if bat and str(bat).isdigit() and int(bat) < LOW_BATTERY:
                self._note(f"電池が{LOW_BATTERY}%未満になったので着陸します")
                return
            try:
                command = self._moves.get(timeout=0.3)
            except queue.Empty:
                if time.monotonic() - self._last_sent > 4:  # a Tello lands by itself after 15 s of silence
                    self._send("battery?", 3)
                continue
            self.moving = True
            reply = self._send(command, 15)
            self.moving = False
            if self._stop.is_set():
                break
            if reply != "ok":
                self.message = f"{command} は動きませんでした（{reply or '返事なし'}）"
                self._note(self.message)

    def _land(self) -> None:
        self.phase = "landing"
        self.message = "着陸しています…"
        self._raw("stop")
        for _ in range(4):
            if self._send("land", 8) == "ok":
                break
        self.airborne = False
        self._finish("着陸しました")

    def _finish(self, message: str) -> None:
        self.airborne = False
        self.moving = False
        self.phase = "done"
        self.message = message
        self._note(message)
