"""
Pure-Python usbmuxd client for Windows.

AMDS listens on TCP 127.0.0.1:27015 and speaks the usbmuxd protocol.  We
bypass Apple's MobileDevice.dll entirely — just send framed plist messages
over the socket.
"""
import socket
import struct
import plistlib
import threading

USBMUXD_HOST = "127.0.0.1"
USBMUXD_PORT = 27015

# header: <length(4) version(4) message_type(4) tag(4)>  all little-endian
_HDR = struct.Struct("<IIII")
VERSION_PLIST = 1
MSG_PLIST = 8


class MuxError(Exception): pass


class MuxConnection:
    """One framed connection to usbmuxd.  After Connect, the same socket
    transitions to a raw tunneled stream to the device — caller takes it."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((USBMUXD_HOST, USBMUXD_PORT))
        self._tag = 0
        self._lock = threading.Lock()

    def _next_tag(self) -> int:
        with self._lock:
            self._tag += 1
            return self._tag

    def send_plist(self, payload: dict) -> int:
        body = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
        tag = self._next_tag()
        hdr = _HDR.pack(_HDR.size + len(body), VERSION_PLIST, MSG_PLIST, tag)
        self.sock.sendall(hdr + body)
        return tag

    def recv_plist(self) -> dict:
        hdr = _recv_exact(self.sock, _HDR.size)
        total, version, msg, tag = _HDR.unpack(hdr)
        body = _recv_exact(self.sock, total - _HDR.size)
        if msg != MSG_PLIST:
            raise MuxError(f"unexpected message type {msg}")
        return plistlib.loads(body)

    def request(self, payload: dict) -> dict:
        self.send_plist(payload)
        return self.recv_plist()

    def close(self):
        try: self.sock.close()
        except Exception: pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise MuxError("connection closed")
        buf += chunk
    return bytes(buf)


# ── High-level helpers ────────────────────────────────────────────────────────

def list_devices() -> list[dict]:
    """Returns a list of dicts: {DeviceID, SerialNumber, ProductID, ConnectionType, ...}"""
    c = MuxConnection()
    try:
        resp = c.request({
            "MessageType": "ListDevices",
            "ClientVersionString": "robly",
            "ProgName": "robly",
            "kLibUSBMuxVersion": 3,
        })
        out = []
        for d in resp.get("DeviceList", []):
            props = d.get("Properties", {})
            out.append({"DeviceID": props.get("DeviceID"),
                        "SerialNumber": props.get("SerialNumber"),
                        "ProductID": props.get("ProductID"),
                        "ConnectionType": props.get("ConnectionType")})
        return out
    finally:
        c.close()


def connect_tunnel(device_id: int, port: int) -> socket.socket:
    """Open a usbmux Connect to a device port.  Returns a raw socket tunneled
    to the device once the Connect succeeds."""
    c = MuxConnection()
    # usbmuxd expects port in network byte order in the PortNumber field
    port_be = socket.htons(port)
    resp = c.request({
        "MessageType": "Connect",
        "ClientVersionString": "robly",
        "ProgName": "robly",
        "DeviceID": device_id,
        "PortNumber": port_be,
    })
    n = resp.get("Number", -1)
    if n != 0:
        c.close()
        raise MuxError(f"Connect to port {port} failed: Number={n} ({resp})")
    return c.sock  # caller now owns this socket


