"""
Pure-Python AFC (Apple File Conduit) client.

AFC framing:
  magic  8 bytes  "CFA6LPAA"
  total  u64 LE  total bytes in this AFC message (header + payload)
  hdr_sz u64 LE  bytes of this header + the "head" portion of the payload
                 (for many ops, equals total)
  pkt_no u64 LE  monotonically increasing per-direction
  op     u64 LE  operation code
  ...    payload follows
"""
import struct

MAGIC = b"CFA6LPAA"

# Operations
OP_STATUS         = 0x00000001
OP_DATA           = 0x00000002
OP_READ_DIR       = 0x00000003
OP_GET_FILE_INFO  = 0x0000000A
OP_GET_DEVINFO    = 0x0000000B
OP_FILE_OPEN      = 0x0000000D
OP_FILE_OPEN_RES  = 0x0000000E
OP_FILE_READ      = 0x0000000F
OP_FILE_WRITE     = 0x00000010
OP_FILE_CLOSE     = 0x00000014
OP_MAKE_DIR       = 0x00000009
OP_REMOVE_PATH    = 0x00000008
OP_FILE_LOCK      = 0x0000001B

# flock constants
LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

# Open modes (AFC O_*)
MODE_RDONLY = 1   # r
MODE_RW     = 2   # r+
MODE_WRONLY = 3   # w  (create + truncate)
MODE_WR     = 4   # w+ (create + truncate, also read)
MODE_APPEND = 5   # a
MODE_RDAPPEND = 6 # a+


class AFCError(Exception): pass


class AFCClient:
    def __init__(self, sock):
        """sock is anything with sendall(bytes) and recv(n) -> bytes (the SSL'd
        usbmux tunnel to the AFC port)."""
        self.sock = sock
        self._pkt = 0

    # ── framing ──────────────────────────────────────────────────────────────
    def _send(self, op: int, head: bytes = b"", body: bytes = b""):
        self._pkt += 1
        hdr_sz = 40 + len(head)
        total  = hdr_sz + len(body)
        frame  = MAGIC + struct.pack("<QQQQ", total, hdr_sz, self._pkt, op)
        self.sock.sendall(frame + head + body)

    def _recv(self) -> tuple[int, bytes]:
        hdr = _recv_exact(self.sock, 40)
        if hdr[:8] != MAGIC:
            raise AFCError(f"bad magic: {hdr[:8]!r}")
        total, hdr_sz, pkt, op = struct.unpack("<QQQQ", hdr[8:40])
        body = _recv_exact(self.sock, total - 40)
        return op, body

    def _call(self, op: int, head: bytes = b"", body: bytes = b"") -> tuple[int, bytes]:
        self._send(op, head, body)
        rop, rbody = self._recv()
        return rop, rbody

    # ── high-level ───────────────────────────────────────────────────────────
    def devinfo(self) -> dict:
        op, body = self._call(OP_GET_DEVINFO)
        return _parse_nul_dict(body)

    def listdir(self, path: str) -> list[str]:
        op, body = self._call(OP_READ_DIR, path.encode("utf-8") + b"\x00")
        if op != OP_DATA:
            raise AFCError(f"listdir failed op={op:#x} status={_status(body)}")
        return [s.decode("utf-8", "replace")
                for s in body.split(b"\x00")
                if s and s not in (b".", b"..")]

    def stat(self, path: str) -> dict:
        op, body = self._call(OP_GET_FILE_INFO, path.encode("utf-8") + b"\x00")
        if op != OP_DATA:
            raise AFCError(f"stat failed op={op:#x} status={_status(body)}")
        return _parse_nul_dict(body)

    def open(self, path: str, mode: int = MODE_RDONLY) -> int:
        head = struct.pack("<Q", mode) + path.encode("utf-8") + b"\x00"
        op, body = self._call(OP_FILE_OPEN, head, b"")
        if op != OP_FILE_OPEN_RES:
            raise AFCError(f"open failed op={op:#x} status={_status(body)}")
        (handle,) = struct.unpack("<Q", body[:8])
        return handle

    def read(self, handle: int, size: int) -> bytes:
        head = struct.pack("<QQ", handle, size)
        op, body = self._call(OP_FILE_READ, head)
        if op != OP_DATA:
            raise AFCError(f"read failed op={op:#x} status={_status(body)}")
        return body

    def write(self, handle: int, data: bytes):
        head = struct.pack("<Q", handle)
        op, body = self._call(OP_FILE_WRITE, head, data)
        if op != OP_STATUS or _status(body) != 0:
            raise AFCError(f"write failed op={op:#x} status={_status(body)}")

    def close_file(self, handle: int):
        head = struct.pack("<Q", handle)
        self._call(OP_FILE_CLOSE, head)

    def remove(self, path: str):
        op, body = self._call(OP_REMOVE_PATH, path.encode("utf-8") + b"\x00")
        if op != OP_STATUS or _status(body) != 0:
            raise AFCError(f"remove({path!r}) op={op:#x} status={_status(body)}")

    def flock(self, handle: int, lock_type: int):
        """flock-style advisory lock. lock_type = LOCK_SH|LOCK_EX|LOCK_NB|LOCK_UN."""
        head = struct.pack("<QQ", handle, lock_type)
        op, body = self._call(OP_FILE_LOCK, head)
        if op != OP_STATUS or _status(body) != 0:
            raise AFCError(f"flock(h={handle},t={lock_type}) op={op:#x} status={_status(body)}")

    def mkdir(self, path: str):
        op, body = self._call(OP_MAKE_DIR, path.encode("utf-8") + b"\x00")
        if op != OP_STATUS:
            return
        st = _status(body)
        if st not in (0, 8):  # 0 ok, 8 "already exists" -ish
            raise AFCError(f"mkdir({path!r}) status={st}")

    def makedirs(self, path: str):
        """Recursively create parent + leaf dirs.  Tolerates already-exists."""
        parts, cur = [p for p in path.split("/") if p], ""
        for p in parts:
            cur += "/" + p
            try: self.mkdir(cur)
            except AFCError: pass

    def write_file(self, path: str, data: bytes, chunk: int = 64 * 1024):
        """Create or overwrite path and write all bytes.  Auto-creates parents."""
        parent = path.rsplit("/", 1)[0]
        if parent and parent != "/":
            self.makedirs(parent)
        h = self.open(path, MODE_WRONLY)
        try:
            view = memoryview(data)
            for i in range(0, len(view), chunk):
                self.write(h, bytes(view[i:i+chunk]))
        finally:
            self.close_file(h)

    def read_file(self, path: str) -> bytes:
        h = self.open(path)
        try:
            chunks = []
            while True:
                chunk = self.read(h, 65536)
                if not chunk: break
                chunks.append(chunk)
                if len(chunk) < 65536: break
            return b"".join(chunks)
        finally:
            self.close_file(h)


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk: raise AFCError("connection closed mid-frame")
        buf += chunk
    return bytes(buf)


def _status(body: bytes) -> int:
    if len(body) >= 8:
        return struct.unpack("<Q", body[:8])[0]
    return -1


def _parse_nul_dict(body: bytes) -> dict:
    parts = body.split(b"\x00")
    if parts and parts[-1] == b"": parts = parts[:-1]
    out = {}
    for i in range(0, len(parts) - 1, 2):
        out[parts[i].decode("utf-8", "replace")] = parts[i+1].decode("utf-8", "replace")
    return out
