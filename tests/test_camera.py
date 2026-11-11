import os
import shutil
import subprocess
import time
import unittest

import numpy as np

import camera


class CameraTest(unittest.TestCase):
    def test_jpeg_is_the_newest_frame_or_the_overlay(self):
        cam = camera.Camera()
        self.assertIsNone(cam.jpeg())
        import cv2
        cam._jpeg = cv2.imencode(".jpg", np.full((48, 64, 3), 90, dtype=np.uint8))[1].tobytes()
        self.assertTrue(cam.jpeg().startswith(b"\xff\xd8"))
        self.assertEqual(cam.frame.shape, (48, 64, 3))
        plain = cam.jpeg()
        cam.show(np.zeros((48, 64, 3), dtype=np.uint8))
        self.assertNotEqual(cam.jpeg(), plain)
        cam.clear_overlay()
        self.assertIsNone(cam._shown)

    def test_video_endpoint_streams_jpeg_while_flying(self):
        import app  # late: other tests set their data folder before importing it
        import cv2
        app.flyer.camera._jpeg = cv2.imencode(".jpg", np.full((48, 64, 3), 90, dtype=np.uint8))[1].tobytes()
        app.flyer.phase = "flying"
        try:
            response = app.app.test_client().get("/api/fly/video", buffered=False, headers={"Host": "127.0.0.1:5099"})
            self.assertIn("multipart/x-mixed-replace", response.content_type)
            chunk = next(response.response)
            self.assertIn(b"image/jpeg", chunk)
            response.close()
        finally:
            app.flyer.phase = "idle"
            app.flyer.camera._jpeg = None


class CameraStreamTest(unittest.TestCase):
    def test_h264_over_udp_becomes_pictures(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or b"libx264" not in subprocess.run([ffmpeg, "-encoders"], capture_output=True).stdout:
            self.skipTest("ffmpeg with libx264 is not here")
        camera.VIDEO_PORT = 11188
        cam = camera.Camera()
        cam.start()
        gen = subprocess.Popen([ffmpeg, "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc=size=640x480:rate=30",
                                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-f", "h264",
                                "udp://127.0.0.1:11188?pkt_size=1460"])
        try:
            deadline = time.time() + 8
            while cam.frames < 5 and time.time() < deadline:
                time.sleep(0.1)
            self.assertGreaterEqual(cam.frames, 5, cam.status())
            self.assertEqual(cam.frame.shape, (480, 640, 3))
            self.assertTrue(cam.jpeg().startswith(b"\xff\xd8"))
        finally:
            gen.kill()
            cam.stop()


if __name__ == "__main__":
    unittest.main()
