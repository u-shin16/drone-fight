"""The attack-drn colour chase (tello_edu/) driving the hand-flight drone, with a stub drone and synthetic frames."""
import time
import unittest

try:
    import numpy as np

    from tello_edu.battle.auto_battle import AutoBattleConfig, AutoBattleSession
    from tello_edu.battle.profiles import profile_from_settings
    from tello_edu.battle.settings import load_settings
    from tello_edu.session.edu_drone import EduDrone
    HAVE = True
except ImportError:  # tello_edu/ is another person's code and is not in the repo
    HAVE = False


class Monitor:
    def __init__(self, state):
        self.latest = {"1.2.3.4": {"at": time.monotonic(), "raw": state}}


class StubCamera:
    on = True
    frame = None

    def start(self):
        self.on = True


class StubFlight:
    def __init__(self, state):
        self.ip, self.airborne, self.sent, self.landed = "1.2.3.4", True, [], False
        self.monitor = Monitor(state)
        self.camera = StubCamera()

    def raw(self, command):
        self.sent.append(command)

    def send(self, command, timeout=12):
        self.sent.append(command)
        return "ok"

    def land_now(self):
        self.landed, self.airborne = True, False


def frame_with_green(x_center):
    frame = np.full((480, 640, 3), 40, dtype=np.uint8)
    frame[200:280, x_center - 40:x_center + 40] = (0, 200, 0)  # BGR green
    return frame


@unittest.skipUnless(HAVE, "tello_edu/ (attack-drn) is not here")
class ChaseTest(unittest.TestCase):
    def run_chase(self, frame, loops=25):
        flight = StubFlight({"pitch": 0, "roll": 0, "yaw": 0, "h": 80, "tof": 80, "bat": 15})
        drone = EduDrone(flight)
        flight.camera.frame = frame
        settings = load_settings()
        session = AutoBattleSession(drone=drone, settings=settings, profile=profile_from_settings(settings),
                                    config=AutoBattleConfig(loop_hz=200, max_loops=loops, marker="green"),
                                    frame_source=drone.read_frame)
        result = session.run()
        rcs = [tuple(int(v) for v in c.split()[1:]) for c in flight.sent if c.startswith("rc ")]
        return result, rcs, flight

    def test_safety_guards_are_off_so_low_battery_does_not_land_it(self):
        result, rcs, flight = self.run_chase(frame_with_green(320))
        self.assertEqual(result["status"], "completed")  # battery 15 % is below the original 20 % guard

    def test_it_charges_a_green_target_in_view(self):
        _, rcs, _ = self.run_chase(frame_with_green(320))
        self.assertTrue(any(fb > 30 for _, fb, _, _ in rcs), rcs)  # forward

    def test_it_turns_toward_a_target_on_the_right(self):
        _, rcs, _ = self.run_chase(frame_with_green(560))
        self.assertTrue(any(yaw > 0 for _, _, _, yaw in rcs), rcs)

    def test_it_spins_to_search_when_nothing_is_green(self):
        _, rcs, _ = self.run_chase(np.full((480, 640, 3), 40, dtype=np.uint8))
        self.assertTrue(any(yaw != 0 for _, _, _, yaw in rcs), rcs)
        self.assertTrue(all(fb == 0 for _, fb, _, _ in rcs))

    def test_it_ends_by_landing_like_the_original(self):
        _, rcs, flight = self.run_chase(frame_with_green(320))
        self.assertTrue(flight.landed)
        self.assertEqual(rcs[-1], (0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
