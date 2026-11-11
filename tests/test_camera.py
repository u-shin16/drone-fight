import unittest

import numpy as np

import camera


class CameraTest(unittest.TestCase):
    def test_jpeg_is_the_newest_frame_or_the_overlay(self):
        cam = camera.Camera()
        self.assertIsNone(cam.jpeg())
        cam._frame = np.full((48, 64, 3), 90, dtype=np.uint8)
        self.assertTrue(cam.jpeg().startswith(b"\xff\xd8"))
        cam.show(np.zeros((48, 64, 3), dtype=np.uint8))
        self.assertNotEqual(cam.jpeg(), camera.Camera.jpeg(type("C", (), {"_lock": cam._lock, "_shown": None,
                                                                           "_frame": cam._frame})()))
        cam.clear_overlay()
        self.assertIsNone(cam._shown)

    def test_video_endpoint_streams_jpeg_while_flying(self):
        import app  # late: other tests set their data folder before importing it
        app.flyer.camera._frame = np.full((48, 64, 3), 90, dtype=np.uint8)
        app.flyer.phase = "flying"
        try:
            response = app.app.test_client().get("/api/fly/video", buffered=False, headers={"Host": "127.0.0.1:5099"})
            self.assertIn("multipart/x-mixed-replace", response.content_type)
            chunk = next(response.response)
            self.assertIn(b"image/jpeg", chunk)
            response.close()
        finally:
            app.flyer.phase = "idle"
            app.flyer.camera._frame = None


if __name__ == "__main__":
    unittest.main()
