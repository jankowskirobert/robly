"""
SSL wrapper using nassl's bundled legacy OpenSSL 1.0.2.

nassl ships its own OpenSSL with SSLv2/SSLv3/all ciphers enabled — exactly
what we need for iOS 1.x lockdownd's SSL 3.0 handshake.  Distributable: nassl
is just a pip install away.
"""
import os
import tempfile
from nassl.legacy_ssl_client import (
    LegacySslClient, OpenSslVersionEnum, OpenSslVerifyEnum, OpenSslFileTypeEnum,
)


class NasslSocket:
    """Drop-in for a socket — exposes sendall(bytes) and recv(n)."""

    def __init__(self, raw_sock, cert_pem: bytes, key_pem: bytes):
        self.raw = raw_sock

        fd_c, self._cert = tempfile.mkstemp(suffix=".pem"); os.close(fd_c)
        fd_k, self._key  = tempfile.mkstemp(suffix=".pem"); os.close(fd_k)
        with open(self._cert, "wb") as f: f.write(cert_pem)
        with open(self._key,  "wb") as f: f.write(key_pem)

        self.cli = LegacySslClient(
            underlying_socket=raw_sock,
            ssl_version=OpenSslVersionEnum.SSLV3,    # force SSL 3.0 (device uses it)
            ssl_verify=OpenSslVerifyEnum.NONE,
            client_certchain_file=self._cert,
            client_key_file=self._key,
            client_key_type=OpenSslFileTypeEnum.PEM,
        )
        # Allow any cipher including SSLv3-era
        self.cli.set_cipher_list("ALL:!aNULL")
        self.cli.do_handshake()

    def sendall(self, data: bytes):
        n = len(data)
        sent = 0
        while sent < n:
            wrote = self.cli.write(data[sent:])
            if wrote <= 0:
                raise IOError(f"nassl.write returned {wrote}")
            sent += wrote

    def recv(self, n: int) -> bytes:
        return self.cli.read(n)

    def close(self):
        try: self.cli.shutdown()
        except Exception: pass
        for p in (self._cert, self._key):
            try: os.unlink(p)
            except Exception: pass
        try: self.raw.close()
        except Exception: pass
