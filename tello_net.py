"""Find Tellos and other players on the local network, so nobody types an IP."""

import hashlib
import ipaddress
import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

TELLO_PORT = 8889
# The local port we send from. A second copy on the same PC (a dummy opponent
# for practice) sets TELLO_LOCAL_PORT=0 so it never takes 8889 from the real one.
LOCAL_PORT = int(os.environ.get("TELLO_LOCAL_PORT", TELLO_PORT))
DIRECT_IP = "192.168.10.1"  # every Tello's address on its own TELLO-xxxx Wi-Fi
PEER_PORT = 47474
# TELLO_CONNECT_DATA lets two copies of the app run side by side (for testing).
DATA_DIR = Path(os.environ.get("TELLO_CONNECT_DATA", Path(__file__).parent))
NAMES_FILE = DATA_DIR / "drones.json"
SETTINGS_FILE = DATA_DIR / "settings.json"


CLOCK_SKEW = float(os.environ.get("CLOCK_SKEW", 0))  # testing only: pretend this PC's clock is off


def now() -> float:
    """Wall clock used for battle times (start, takeoff, fall)."""
    return time.time() + CLOCK_SKEW


def local_ip() -> str | None:
    """This PC's address on the current network (no packet is sent)."""
    # A Tello's own Wi-Fi has no default route, so ask the way to the Tello first.
    for target in (DIRECT_IP, "10.255.255.255"):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((target, 1))
            return probe.getsockname()[0]
        except OSError:
            pass
        finally:
            probe.close()
    try:
        ip = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True,
                            text=True, timeout=2).stdout.strip()
        return ip or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def network_mode(ip: str | None) -> str:
    if ip is None:
        return "offline"
    return "direct" if ip.startswith("192.168.10.") else "router"


class _Hub:
    """The one real UDP socket (local port 8889) that every drone conversation shares.

    A Tello answers the port its 'command' came from, and only one socket can own
    8889. When two drones were flown from one PC, whichever socket was made second
    got a random port and its drone never answered. So there is a single socket here,
    and each drone gets a DroneSocket that only sees the replies from its own IP.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.socks: list["DroneSocket"] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind(("", LOCAL_PORT))
        except OSError:
            self.sock.bind(("", 0))  # another program holds 8889
        self.sock.settimeout(0.5)
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self) -> None:
        while True:
            try:
                data, source = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            self.dispatch(data, source)

    def dispatch(self, data: bytes, source: tuple) -> None:
        with self.lock:
            for drone_socket in self.socks:
                if drone_socket.ip is None or drone_socket.ip == source[0]:
                    drone_socket.queue.put((data, source))


_hub: _Hub | None = None
_hub_lock = threading.Lock()


def _get_hub() -> _Hub:
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = _Hub()
        return _hub


class DroneSocket:
    """Looks like a UDP socket to the callers, but only receives from one drone (ip=None: all)."""

    def __init__(self, hub: _Hub, ip: str | None, timeout: float) -> None:
        self.hub, self.ip, self.timeout = hub, ip, timeout
        self.queue: queue.Queue = queue.Queue()
        self.closed = False
        with hub.lock:
            hub.socks.append(self)

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def sendto(self, data: bytes, address: tuple) -> int:
        if self.closed:
            raise OSError("socket closed")
        return self.hub.sock.sendto(data, address)

    def recvfrom(self, size: int = 1024) -> tuple[bytes, tuple]:
        if self.closed:
            raise OSError("socket closed")
        try:
            if self.timeout == 0:
                return self.queue.get_nowait()
            return self.queue.get(timeout=self.timeout)
        except queue.Empty:
            raise (BlockingIOError() if self.timeout == 0 else socket.timeout()) from None

    def close(self) -> None:
        self.closed = True
        with self.hub.lock:
            if self in self.hub.socks:
                self.hub.socks.remove(self)


def tello_socket(timeout: float, ip: str | None = None) -> DroneSocket:
    """A way to talk to a Tello (or, with no ip, to hear every Tello), sent from local port 8889.

    The real socket is shared (see _Hub), so two drones flown from this PC both
    talk from 8889 and both get their replies.
    """
    return DroneSocket(_get_hub(), ip, timeout)


def talk(ip: str, commands: list[str], timeout: float = 5,
         port: int = TELLO_PORT) -> list[str | None]:
    """Send SDK commands from one socket and return each reply (None on timeout).

    A Tello answers only the address that last sent it 'command', so the
    queries must come from the same socket; 'command' is sent first.
    """
    sock = tello_socket(timeout, ip)
    replies: list[str | None] = []
    try:
        for command in ["command", *commands]:
            sock.sendto(command.encode(), (ip, port))
            reply = None
            try:
                while reply is None:
                    data, source = sock.recvfrom(1024)
                    if source[0] == ip:
                        reply = data.decode("utf-8", "replace").strip()
            except OSError:
                pass
            replies.append(reply)
            if command == "command" and reply != "ok":
                return [None] * len(commands)
    finally:
        sock.close()
    return replies[1:]


# MAC address prefixes seen on Tello / Tello EDU units.
TELLO_OUIS = {"48:1c:b9", "34:d2:62", "60:60:1f"}


def arp_table() -> dict[str, str]:
    """IP -> MAC (lower case, zero-padded) from this PC's ARP cache."""
    try:
        out = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    table = {}
    for ip, mac in re.findall(r"\(([\d.]+)\) at ((?:[0-9a-f]{1,2}:){5}[0-9a-f]{1,2})", out, re.I):
        table[ip] = ":".join(part.zfill(2) for part in mac.lower().split(":"))
    return table


def ssid_from_mac(mac: str) -> str:
    """A Tello's own Wi-Fi name is TELLO- plus the last 3 bytes of its MAC."""
    return "TELLO-" + "".join(mac.split(":")[-3:]).upper()


def tello_ssid(ip: str) -> str | None:
    mac = arp_table().get(ip)
    return ssid_from_mac(mac) if mac else None


def scan(hosts: list[str] | None = None, wait: float = 2.5, port: int = TELLO_PORT) -> list[dict]:
    """List the Tellos on this network WITHOUT talking to them.

    A Tello only answers the first PC that sends it 'command', so sending
    'command' to every drone would grab the other player's drone too. Instead
    each address gets a harmless packet (so its MAC lands in the ARP cache) and
    Tellos are picked out by MAC prefix. Only the player's own drone is ever
    sent SDK commands (see claim()).
    """
    me = local_ip()
    if hosts is None and (me is None or network_mode(me) == "direct"):
        # On a Tello's own Wi-Fi there is exactly one drone, and it is ours.
        return probe([DIRECT_IP], wait, port)
    if hosts is not None:
        return probe(hosts, wait, port)  # testing: fake Tellos that have no real MAC
    network = ipaddress.ip_network(f"{me}/24", strict=False)
    targets = [str(h) for h in network.hosts() if str(h) != me]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _ in range(2):  # the first packet can be dropped while ARP resolves
            for host in targets:
                try:
                    sock.sendto(b"", (host, 9))  # port 9 = discard; Tellos ignore it
                except OSError:
                    pass
            time.sleep(wait / 2)
    finally:
        sock.close()
    drones = []
    for ip, mac in sorted(arp_table().items(), key=lambda item: ipaddress.ip_address(item[0])):
        if mac[:8] in TELLO_OUIS and ip in targets:
            ssid = ssid_from_mac(mac)
            drones.append({"ip": ip, "sn": ssid, "ssid": ssid, "battery": None, "serial": None})
    return drones


def probe(hosts: list[str], wait: float = 2.5, port: int = TELLO_PORT) -> list[dict]:
    """Find Tellos by sending 'command' (only where every drone found is ours)."""
    sock = tello_socket(0.1)
    found: set[str] = set()
    deadline = time.monotonic() + wait
    resend_at = time.monotonic() + wait / 3
    try:
        for round_ in range(2):
            for host in hosts:
                if host not in found:
                    try:
                        sock.sendto(b"command", (host, port))
                    except OSError:
                        pass
            until = resend_at if round_ == 0 else deadline
            while time.monotonic() < until:
                try:
                    reply, source = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                if reply.strip().lower() == b"ok":
                    found.add(source[0])
    finally:
        sock.close()
    drones = []
    for ip in sorted(found, key=ipaddress.ip_address):
        serial, battery = talk(ip, ["sn?", "battery?"], 2, port)
        ssid = tello_ssid(ip)
        drones.append({"ip": ip, "sn": ssid or serial or ip, "ssid": ssid, "battery": battery,
                       "serial": serial})
    return drones


def claim(ip: str, port: int = TELLO_PORT) -> dict | None:
    """Become this drone's controller and read its battery; None if it does not answer."""
    serial, battery = talk(ip, ["sn?", "battery?"], 3, port)
    if serial is None and battery is None:
        return None
    return {"serial": serial, "battery": battery}


def is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def room_id(code: str | None) -> str | None:
    """Players with the same 4-digit code meet; only a hash goes on the network."""
    if not code or not re.fullmatch(r"\d{4}", code):
        return None
    return hashlib.sha256(f"tello_connect:{code}".encode()).hexdigest()[:16]


def load_settings() -> dict:
    """This player's name and chosen drone (by serial number)."""
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(**changes) -> None:
    settings = {**load_settings(), **changes}
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def load_names() -> dict:
    try:
        return json.loads(NAMES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_name(serial: str, name: str) -> None:
    names = load_names()
    if name:
        names[serial] = name
    else:
        names.pop(serial, None)
    # Names belong to serial numbers; drop ones saved under an IP by mistake.
    names = {key: value for key, value in names.items() if value and not is_ip(key)}
    NAMES_FILE.write_text(json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8")


class PeerFinder:
    """Announce this app on the LAN and collect other players' announcements."""

    def __init__(self, info, port: int = PEER_PORT) -> None:
        self.info = info  # callable returning this player's public details
        self.port = port
        self.instance = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.peers: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("", port))
        self.sock.settimeout(0.5)
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._announce, daemon=True).start()

    def send(self, message: dict, ip: str = "255.255.255.255") -> None:
        # 'sent' lets the receiver work out how far apart the two clocks are.
        message = {**message, "app": "tello_connect", "instance": self.instance, "sent": now()}
        try:
            self.sock.sendto(json.dumps(message).encode(), (ip, self.port))
        except OSError:
            pass

    def announce(self) -> None:
        self.send({"type": "hello", **self.info()})

    def _announce(self) -> None:
        while True:
            self.announce()
            time.sleep(2)

    def _listen(self) -> None:
        while True:
            try:
                data, source = self.sock.recvfrom(4096)
                message = json.loads(data)
            except (socket.timeout, ValueError):
                continue
            except OSError:
                return
            if message.get("app") != "tello_connect" or message.get("instance") == self.instance:
                continue
            with self.lock:
                received = now()
                # Their clock minus ours (LAN delay is a few ms, so it is ignored).
                offset = message.get("sent", received) - received
                self.peers[message["instance"]] = {**message, "ip": source[0], "seen": time.time(),
                                                   "offset": offset}

    def current(self) -> list[dict]:
        with self.lock:
            return [p for p in self.peers.values() if time.time() - p["seen"] < 7]
