"""Two drones from one PC share one UDP socket; each only hears its own replies."""
import os
import socket
import unittest

os.environ.setdefault("TELLO_LOCAL_PORT", "0")
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


if __name__ == "__main__":
    unittest.main()
