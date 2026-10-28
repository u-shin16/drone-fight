import math
import unittest

import scan

# Pads around pad 4 (the origin) in the 1 2 3 / 4 5 6 / 7 8 order, irregular spacing.
CENTERS = {1: (-40, -110), 2: (60, -120), 3: (170, -100), 4: (0, 0),
           5: (90, 10), 6: (180, 5), 7: (30, 115), 8: (150, 120)}
FIELD = scan.boundary(80, 260, 170, 180)


def scanned(spread=0.0, seen=3) -> scan.PadMap:
    pads = scan.PadMap()
    for k, (x, y) in CENTERS.items():
        for i in range(seen):
            wobble = spread if i % 2 else -spread
            pads.sightings[k].append((x + wobble, y))
    return pads


class BoundaryAndRouteTest(unittest.TestCase):
    def test_boundary_is_checked(self):
        self.assertEqual(scan.boundary(80, 260, 170, 180), (-80.0, 260.0, -170.0, 180.0))
        for bad in ((10, 260, 170, 180), (80, 900, 170, 180), (80, None, 170, 180), (80, True, 170, 180)):
            with self.assertRaises(ValueError):
                scan.boundary(*bad)

    def test_route_stays_inside_and_covers_with_small_gaps(self):
        points = scan.route(FIELD)
        x_min, x_max, y_min, y_max = FIELD
        for x, y in points:
            self.assertTrue(x_min + scan.EDGE - 1e-6 <= x <= x_max - scan.EDGE + 1e-6)
            self.assertTrue(y_min + scan.EDGE - 1e-6 <= y <= y_max - scan.EDGE + 1e-6)
        for a, b in zip(points, points[1:]):
            self.assertLessEqual(math.dist(a, b), max(scan.STEP, scan.LANE) + 1e-6)
        # Every pad lies within the camera's reach (about 42 cm at 1 m) of some stop.
        for k, c in CENTERS.items():
            self.assertLess(min(math.dist(c, p) for p in points), 42, f"pad {k}")

    def test_hops_add_up_and_stay_short(self):
        moves = scan.hops((0, 0), (130, -40))
        self.assertAlmostEqual(sum(m[0] for m in moves), 130)
        self.assertAlmostEqual(sum(m[1] for m in moves), -40)
        self.assertTrue(all(math.hypot(*m) <= scan.STEP + 1e-6 for m in moves))


class PadMapTest(unittest.TestCase):
    def test_reanchoring_removes_drift(self):
        pads = scan.PadMap()
        # Seen pad 5 once from a good estimate; later our tracking is 30 cm off.
        pads.observe(5, (5, 0), (95, 10))
        corrected = pads.observe(5, (-5, 2), (115, 12))
        self.assertAlmostEqual(corrected[0], 90 + 15 - 5)  # centre (100) moved halfway by the bad sighting
        self.assertEqual(pads.center(4), (0.0, 0.0))

    def test_good_scan_builds_the_layout(self):
        layout = scan.build_layout(scanned(spread=8, seen=4), FIELD, closure=12)
        self.assertEqual(layout["starts"]["A"][:2], [0.0, 0.0])
        self.assertEqual(layout["starts"]["B"][:2], list(map(float, CENTERS[6])))
        self.assertEqual(layout["field"], list(FIELD))
        self.assertIn("scanned_at", layout)

    def test_doubtful_scans_are_rejected(self):
        missing = scanned()
        missing.sightings[7] = []
        cases = {
            "7が見つかりません": (missing, 10),
            "回数が足りません": (scanned(seen=1), 10),
            "安定しません": (scanned(spread=40), 10),
            "戻ったときのズレ": (scanned(), 45),
            "戻って確認": (scanned(), None),
        }
        for text, (pads, closure) in cases.items():
            with self.assertRaisesRegex(ValueError, text):
                scan.build_layout(pads, FIELD, closure)
        swapped = scanned()
        swapped.sightings[4], swapped.sightings[6] = swapped.sightings[6], swapped.sightings[4]
        swapped.sightings[4] = [(0, 0)] * 3
        swapped.sightings[6] = [(-80, 0)] * 3  # pad 6 left of pad 4: wrong order
        with self.assertRaises(ValueError):
            scan.build_layout(swapped, FIELD, 10)


if __name__ == "__main__":
    unittest.main()
