"""The drone's front camera: ask it to stream, read the frames, hand out the newest as JPEG."""

import os
import threading
import time

VIDEO_URL = "udp://0.0.0.0:11111?overrun_nonfatal=1&fifo_size=500000"


class Camera:
    def __init__(self) -> None:
        self.on = False
        self._frame = None
        self._shown = None  # what the page shows: the frame, or one the chase drew boxes on
        self._at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self) -> None:
        if self.on:
            return
        self._stop.clear()
        self.on = True
        threading.Thread(target=self._read, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.on = False
        with self._lock:
            self._frame = self._shown = None

    def _read(self) -> None:
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "fflags;nobuffer|flags;low_delay")
        import cv2

        capture = cv2.VideoCapture(VIDEO_URL, cv2.CAP_FFMPEG)
        try:
            while not self._stop.is_set():
                ok, frame = capture.read()
                if ok:
                    with self._lock:
                        self._frame, self._at = frame, time.monotonic()
                else:
                    time.sleep(0.02)
        finally:
            capture.release()

    @property
    def frame(self):
        with self._lock:
            return self._frame

    def show(self, annotated) -> None:
        with self._lock:
            self._shown = annotated

    def clear_overlay(self) -> None:
        with self._lock:
            self._shown = None

    def jpeg(self) -> bytes | None:
        import cv2

        with self._lock:
            image = self._shown if self._shown is not None else self._frame
        if image is None:
            return None
        ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return data.tobytes() if ok else None
