"""The drone's front camera: take the H.264 video the Tello sends over UDP, decode it with ffmpeg, keep the newest frame.

We open the UDP port ourselves (so we can count what arrives and say where it breaks) and feed it
to an ffmpeg process that turns it into a stream of JPEG pictures.
"""

import os
import shutil
import socket
import subprocess
import threading
import time

VIDEO_PORT = int(os.environ.get("VIDEO_PORT", 11111))
SOI, EOI = b"\xff\xd8", b"\xff\xd9"


class Camera:
    def __init__(self) -> None:
        self.on = False
        self.reply = ""      # what the drone answered to streamon
        self.packets = 0     # UDP packets of video that arrived
        self.frames = 0      # pictures decoded
        self.decoded_bytes = 0
        self.error = ""
        self._jpeg: bytes | None = None
        self._shown = None   # what the page shows instead: a frame the chase drew boxes on
        self._decoded = (None, None)  # (the jpeg it came from, its picture)
        self._at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None

    def status(self) -> dict:
        return {"on": self.on, "reply": self.reply, "packets": self.packets, "frames": self.frames,
                "error": self.error, "age": round(time.monotonic() - self._at, 1) if self._at else None}

    def start(self) -> None:
        if self.on:
            return
        self._stop.clear()
        self.packets = self.frames = 0
        self.error, self._at, self._jpeg, self._shown = "", 0.0, None, None
        self.on = True
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.error = "ffmpeg が見つかりません（brew install ffmpeg）"
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", VIDEO_PORT))
            sock.settimeout(0.5)
        except OSError as exc:
            self.error = f"映像の受信口（{VIDEO_PORT}番）を開けません（{exc}）"
            return
        self._proc = subprocess.Popen(
            [ffmpeg, "-loglevel", os.environ.get("CAM_LOG", "quiet"), "-flags", "low_delay", "-probesize", "32768",  # (with -fflags nobuffer nothing comes out)
             "-analyzeduration", "0", "-f", "h264", "-i", "pipe:0", "-f", "image2pipe", "-c:v", "mjpeg",
             "-q:v", "6", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(os.environ["CAM_ERR"], "w") if os.environ.get("CAM_ERR") else subprocess.DEVNULL)
        threading.Thread(target=self._receive, args=(sock, self._proc), daemon=True).start()
        threading.Thread(target=self._decode, args=(self._proc,), daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.on = False
        proc, self._proc = self._proc, None
        if proc:
            try:
                proc.stdin.close()
            except OSError:
                pass
            proc.kill()
        with self._lock:
            self._jpeg = self._shown = None

    def _receive(self, sock: socket.socket, proc: subprocess.Popen) -> None:
        try:
            while not self._stop.is_set():
                try:
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                self.packets += 1
                try:
                    proc.stdin.write(data)
                    proc.stdin.flush()
                except (OSError, ValueError):
                    return
        finally:
            sock.close()

    def _decode(self, proc: subprocess.Popen) -> None:
        buffer = b""
        while not self._stop.is_set():
            chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
            if not chunk:
                break
            buffer += chunk
            self.decoded_bytes += len(chunk)
            while True:
                start = buffer.find(SOI)
                end = buffer.find(EOI, start + 2) if start >= 0 else -1
                if start < 0 or end < 0:
                    buffer = buffer[start:] if start >= 0 else b""
                    break
                with self._lock:
                    self._jpeg, self._at = buffer[start:end + 2], time.monotonic()
                    self.frames += 1
                buffer = buffer[end + 2:]

    @property
    def frame(self):
        """The newest picture as an image array (for the chase), or None."""
        import cv2
        import numpy as np

        with self._lock:
            jpeg = self._jpeg
        if jpeg is None:
            return None
        if self._decoded[0] is not jpeg:
            self._decoded = (jpeg, cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR))
        return self._decoded[1]

    def show(self, annotated) -> None:
        with self._lock:
            self._shown = annotated

    def clear_overlay(self) -> None:
        with self._lock:
            self._shown = None

    def jpeg(self) -> bytes | None:
        """What the page should show now, as JPEG."""
        import cv2

        with self._lock:
            shown, jpeg = self._shown, self._jpeg
        if shown is not None:
            ok, data = cv2.imencode(".jpg", shown, [cv2.IMWRITE_JPEG_QUALITY, 70])
            return data.tobytes() if ok else jpeg
        return jpeg
