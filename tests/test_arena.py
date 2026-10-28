import unittest

import arena


class ArenaTest(unittest.TestCase):
    def setUp(self):
        self.layout = arena.normalize({"preset": "rows8", "spacing": 60})

    def test_eight_pad_layout_has_shared_start_pads(self):
        self.assertEqual(set(self.layout["pads"]), set("12345678"))
        self.assertEqual(self.layout["starts"]["A"][:2], self.layout["pads"]["4"])
        self.assertEqual(self.layout["starts"]["B"][:2], self.layout["pads"]["6"])
        self.assertEqual(arena.fingerprint(self.layout), arena.fingerprint(arena.normalize(self.layout)))

    def test_rejects_missing_pad_and_unsafe_boundary(self):
        bad = {**self.layout, "preset": None, "pads": {k: v for k, v in self.layout["pads"].items() if k != "8"}}
        with self.assertRaises(ValueError):
            arena.normalize(bad)
        # A boundary that leaves pads 1-3 and 7-8 outside the flying area.
        bad = {**self.layout, "preset": None, "field": [-120, 120, -40, 40]}
        with self.assertRaises(ValueError):
            arena.normalize(bad)

    def test_rejects_wrong_numbered_pattern(self):
        bad = {**self.layout, "preset": None, "pads": dict(self.layout["pads"])}
        bad["pads"]["4"], bad["pads"]["6"] = bad["pads"]["6"], bad["pads"]["4"]
        with self.assertRaisesRegex(ValueError, "左右の並び"):
            arena.normalize(bad)


if __name__ == "__main__":
    unittest.main()
