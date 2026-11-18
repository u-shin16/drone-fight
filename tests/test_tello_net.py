"""Shared command socket, and finding a Tello whose ARP entry disappears."""
import os
import socket
import unittest

os.environ.setdefault("TELLO_LOCAL_PORT", "0")
import gesture_fly  # noqa: E402
import tello_net  # noqa: E402


class SharedSocketTest(unittest.TestCase):
    def setUp(self):
        self.hub = tello_net._get_hub()
        self.a = tello_net.tello_socket(0.05, "10.0.0.1")
        self.b = tello_net.tello_socket(0.05, "10.0.0.2")

    def tearDown(self):
        self.a.close()
        self.b.close()

    def test_replies_go_to_the_drone_they_came_from(self):
        self.hub.dispatch(b"ok", ("10.0.0.1", 8889))
        self.hub.dispatch(b"92", ("10.0.0.2", 8889))
        self.assertEqual(self.a.recvfrom(1024)[0], b"ok")
        self.assertEqual(self.b.recvfrom(1024)[0], b"92")
        with self.assertRaises(socket.timeout):
            self.a.recvfrom(1024)

    def test_throwing_away_late_replies_of_one_drone_leaves_the_other_alone(self):
        self.hub.dispatch(b"late", ("10.0.0.1", 8889))
        self.hub.dispatch(b"ok", ("10.0.0.2", 8889))
        self.a.settimeout(0)
        with self.assertRaises(OSError):  # the same drain loop the runners use
            while True:
                self.a.recvfrom(1024)
        self.assertEqual(self.b.recvfrom(1024)[0], b"ok")

    def test_a_socket_with_no_ip_hears_every_drone(self):
        everyone = tello_net.tello_socket(0.05)
        try:
            self.hub.dispatch(b"ok", ("10.0.0.9", 8889))
            self.assertEqual(everyone.recvfrom(1024)[1][0], "10.0.0.9")
        finally:
            everyone.close()

    def test_two_drones_send_from_the_same_local_port(self):
        self.assertIs(self.a.hub, self.b.hub)
        self.assertIs(self.a.hub.sock, self.b.hub.sock)

    def test_closed_sockets_stop_receiving(self):
        self.a.close()
        self.hub.dispatch(b"ok", ("10.0.0.1", 8889))
        with self.assertRaises(OSError):
            self.a.recvfrom(1024)


class TelloNetTest(unittest.TestCase):
    def test_a_tello_stays_found_after_the_arp_cache_drops_it(self):
        targets = {"192.168.111.188", "192.168.111.182"}
        found = {}
        tello_net.remember_tellos(found, {"192.168.111.188": "34:d2:62:f1:8b:b1"}, targets)
        tello_net.remember_tellos(found, {"192.168.111.182": "34:d2:62:9f:1d:09"}, targets)
        # The next dump is what a full cache looks like: the drones are gone.
        tello_net.remember_tellos(found, {"192.168.111.140": "aa:bb:cc:dd:ee:ff"}, targets)
        self.assertEqual(set(found), targets)

    def test_probe_order_checks_known_addresses_then_the_top_of_the_subnet(self):
        targets = [f"192.168.111.{n}" for n in range(1, 6)]
        ordered = tello_net.probe_order(targets, {"192.168.111.2"})
        self.assertEqual(ordered, [
            "192.168.111.2",
            "192.168.111.5", "192.168.111.4", "192.168.111.3", "192.168.111.1",
        ])

    def test_a_practice_hop_stays_near_takeoff(self):
        moved = gesture_fly.hop(0, 0, 0, 1)
        self.assertEqual(moved[0], "go 30 0 0 40")
        self.assertIsNone(gesture_fly.hop(0, 0, 0, 0))
        self.assertIsNone(gesture_fly.hop(120, 0, 0, 1))


if __name__ == "__main__":
    unittest.main()
