"""
Pure-Python lockdownd client.

Once usbmux tunnels a socket to device port 62078, lockdownd speaks framed
plist (4-byte BE length prefix).  iOS 1.x does NOT use SSL on lockdownd, so
we just exchange plain XML plists.
"""
import os
import ssl
import socket
import struct
import tempfile
import plistlib

LOCKDOWN_PORT = 62078


def wrap_ssl(sock: socket.socket, host_cert: bytes, host_key: bytes,
             root_cert: bytes) -> ssl.SSLSocket:
    """Upgrade a plain socket to TLS using the pairing record's certs."""
    # ssl.SSLContext needs files on disk for the keypair
    fd_c, cert_path = tempfile.mkstemp(suffix=".pem"); os.close(fd_c)
    fd_k, key_path  = tempfile.mkstemp(suffix=".pem"); os.close(fd_k)
    fd_r, root_path = tempfile.mkstemp(suffix=".pem"); os.close(fd_r)
    try:
        with open(cert_path, "wb") as f: f.write(host_cert)
        with open(key_path,  "wb") as f: f.write(host_key)
        with open(root_path, "wb") as f: f.write(root_cert)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers("ALL:@SECLEVEL=0")
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        return ctx.wrap_socket(sock, server_side=False, do_handshake_on_connect=True)
    finally:
        for p in (cert_path, key_path, root_path):
            try: os.unlink(p)
            except Exception: pass


class LockdownError(Exception): pass


class LockdownClient:
    def __init__(self, sock: socket.socket):
        self.sock = sock

    def send_plist(self, payload: dict):
        body = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
        self.sock.sendall(struct.pack(">I", len(body)) + body)

    def recv_plist(self) -> dict:
        (n,) = struct.unpack(">I", _recv_exact(self.sock, 4))
        return plistlib.loads(_recv_exact(self.sock, n))

    def request(self, payload: dict) -> dict:
        self.send_plist(payload)
        return self.recv_plist()

    def query_type(self) -> str:
        r = self.request({"Request": "QueryType"})
        return r.get("Type", "")

    def validate_pair(self, host_id: str) -> dict:
        return self.request({
            "Request": "ValidatePair",
            "ProtocolVersion": "2",
            "PairRecord": {"HostID": host_id},
        })

    def get_value(self, key: "str | None" = None, domain: "str | None" = None):
        msg = {"Request": "GetValue"}
        if key:    msg["Key"]    = key
        if domain: msg["Domain"] = domain
        r = self.request(msg)
        return r.get("Value")

    def start_service(self, name: str) -> int:
        r = self.request({"Request": "StartService", "Service": name})
        if "Error" in r:
            raise LockdownError(f"StartService({name}) failed: {r['Error']}")
        port = r.get("Port")
        if port is None:
            raise LockdownError(f"StartService({name}) no Port in response: {r}")
        return int(port)

    def close(self):
        try: self.sock.close()
        except Exception: pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise LockdownError("connection closed")
        buf += chunk
    return bytes(buf)


if False and __name__ == "__main__":
    import usbmux
    devs = usbmux.list_devices()
    if not devs:
        print("no device"); exit(1)
    d = devs[0]
    print(f"device: {d['SerialNumber']}")
    sock = usbmux.connect_tunnel(d["DeviceID"], LOCKDOWN_PORT)
    ld = LockdownClient(sock)
    print(f"  QueryType: {ld.query_type()}")
    pair = plistlib.load(open(r"C:\ProgramData\Apple\Lockdown\bc6aedec6753639f74a380237f3719c74a265020.plist", "rb"))
    r = ld.request({"Request": "StartSession",
                    "HostID": pair["HostID"],
                    "SystemBUID": pair["SystemBUID"]})
    print(f"  StartSession -> {r}")
    if r.get("EnableSessionSSL"):
        print("  upgrading via nassl (legacy OpenSSL 1.0.2)…")
        from nassl_wrap import NasslSocket
        ld.sock = NasslSocket(ld.sock, pair["HostCertificate"], pair["HostPrivateKey"])
        print("  SSL handshake done!")
    try:
        afc_port = ld.start_service("com.apple.afc")
        print(f"  AFC port: {afc_port}")
    except Exception as e:
        print(f"  AFC start failed: {e}")
    for key in ("DeviceName", "ProductType", "ProductVersion",
                "BuildVersion", "DeviceClass", "HardwareModel",
                "SerialNumber", "UniqueDeviceID"):
        try:
            v = ld.get_value(key)
            print(f"  {key}: {v}")
        except Exception as e:
            print(f"  {key}: ERROR {e}")
    ld.close()
