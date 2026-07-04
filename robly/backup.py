"""
iOS 1.x mobilebackup / DeviceLink client.

Reverse-engineered from string scans of AppleMobileBackup_main.dll (both
iTunes 11 from XPk and iTunes 12 from current install) plus live probing
of the device's BackupAgent.  Status:

 * Backup direction (device → host)  — FULLY WORKING
       kBackupMessageRestoreRequest is wrong, use BackupMessageBackupRequest
       request → SendFilePiece (×N, each ACKed with kBackupMessageBackupFileReceived)
       → BackupMessageBackupFinished

 * Restore direction (host → device) — ~98% reverse-engineered.
       Wire format for DLSendFile recovered via capstone disassembly
       of DeviceLink.dll's `_DLCreateMessageDataWithArgs` (called from
       inside DLSendFile):

           arr = CFArrayCreateMutable(count+1)
           CFArrayAppendValue(arr, "DLSendFile")    # header
           for arg in args:                          # count = 2 from caller
               if arg == NULL: arg = "___EmptyParameterString___"
               CFArrayAppendValue(arr, arg)
           # plist-encode arr, write to socket

       So the wire body is **`["DLSendFile", arg1, arg2]`** — 3 elements.
       DLSendFile prototype: `DLSendFile(conn, arg1, arg2)` where arg1
       is NULL-checked (so it's a pointer object — string/dict/data).

       Outstanding: the exact CFType identities of arg1 and arg2 expected
       by iOS 1.x BackupAgent's `_DLHandleIncomingMessage` handler.
       We've tried:
         (path_str, file_data)          → silent close
         (info_dict, file_data)         → silent close
         (file_data, info_dict)         → silent close (1 test)
         (status_int, err, info_dict)   → silent close (4-elem variant)
       Silent close = message is recognized but the inner shape is wrong.

       Also discovered: sending `kBackupMessageBackupFileReceived` after
       RestoreReplyOK triggers the device to send `BackupMessageBackupFinished`,
       indicating the device's restore-side state-machine may fall back to
       backup-finished after our wrong ACK.  Worth exploring whether
       there's a different ack message that progresses to file-request
       phase instead.

       Future work: dump iOS 1.x BackupAgent from
       iPodTouch_1.1.2_3B48b_Restore.ipsw and disassemble its
       _DLHandleIncomingMessage dispatch for DLSendFile, or capture
       iTunes 7 talking to an iOS 1.x device over USB with Wireshark.

Wire findings (verified):

 * Message names sent BY THE HOST need the `k` prefix:
       kBackupMessageRestoreRequest, kBackupMessageBackupFileReceived
 * Message names FROM THE DEVICE don't (BackupMessageRestoreReplyOK, etc.)
 * `BackupComputerBasePathKey` — note the "Key" suffix in this one field
 * Manifest "signature" is plain SHA-1 of the inner Data plist;
   AuthData is the literal constant b"Forty Two" (a Douglas Adams joke)
 * Backup paths are relative to /var/mobile/  (e.g. Library/Preferences/...)
 * Per-file hash key is sha1(path).hex()
 * Per-file piece is plistlib.dumps({Path, Version: "1.0", Data, Greylist})
"""
from __future__ import annotations
import datetime
import hashlib
import os
import plistlib
import struct
from dataclasses import dataclass, field

from .usbmux import connect_tunnel
from .nassl_wrap import NasslSocket
from .lockdown import LockdownClient, LOCKDOWN_PORT


# ── wire framing ─────────────────────────────────────────────────────────────
def _recv(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c: raise IOError("connection closed mid-frame")
        buf += c
    return buf


def recv_msg(sock):
    (n,) = struct.unpack(">I", _recv(sock, 4))
    return plistlib.loads(_recv(sock, n))


def send_msg(sock, msg):
    body = plistlib.dumps(msg, fmt=plistlib.FMT_BINARY)
    sock.sendall(struct.pack(">I", len(body)) + body)


# ── data model ───────────────────────────────────────────────────────────────
@dataclass
class BackupFile:
    """One file inside an iOS 1.x backup."""
    path: str                 # relative to /var/mobile/, e.g. "Library/SMS/sms.db"
    data: bytes               # file contents
    mode: int = 0o644
    modified: datetime.datetime = field(default_factory=datetime.datetime.now)
    greylist: bool = False

    @property
    def hash_id(self) -> str:
        return hashlib.sha1(self.path.encode()).hexdigest()

    def piece_bytes(self) -> bytes:
        return plistlib.dumps({
            "Path":     self.path,
            "Version":  "1.0",
            "Data":     self.data,
            "Greylist": self.greylist,
        }, fmt=plistlib.FMT_BINARY)


def make_manifest(udid: str, files: list[BackupFile]) -> dict:
    """Build the BackupManifestKey dict (self-signed via plain sha1)."""
    entries = {}
    for f in files:
        piece = f.piece_bytes()
        entries[f.hash_id] = {
            "ModificationTime": f.modified,
            "FileLength":       len(piece),
            "DataHash":         hashlib.sha1(piece).digest(),
            "Mode":             f.mode,
        }
    data = plistlib.dumps({"Files": entries, "DeviceId": udid, "Version": "1.0"},
                          fmt=plistlib.FMT_BINARY)
    return {
        "AuthData":      b"Forty Two",
        "AuthSignature": hashlib.sha1(data).digest(),
        "AuthVersion":   "1.0",
        "Data":          data,
    }


# ── high level session ───────────────────────────────────────────────────────
class BackupSession:
    """Open mobilebackup channel with DL handshake completed."""

    def __init__(self, device, ld_session_only_mode=False):
        self._device = device
        self.sock = None
        self._open()

    def _open(self):
        port = self._device.lockdown.start_service("com.apple.mobilebackup")
        self.sock = connect_tunnel(self._device.device_id, port)
        self.sock.settimeout(30)
        recv_msg(self.sock)  # device sends VersionExchange (offers v100)
        # iTunes 7 capture: replies with version "DLVersionsOk", 300
        send_msg(self.sock, ["DLMessageVersionExchange", "DLVersionsOk", 300])
        ready = recv_msg(self.sock)
        if ready[0] != "DLMessageDeviceReady":
            raise IOError(f"expected DeviceReady, got {ready}")

    # ── BACKUP (device → host) ───────────────────────────────────────────────
    def backup(self, host_basepath: str = "/tmp/robly"):
        """Yields (path, data) tuples as the device streams its filesystem."""
        send_msg(self.sock, ["DLMessageProcessMessage",
                             {"BackupMessageTypeKey":      "BackupMessageBackupRequest",
                              "BackupComputerBasePathKey": host_basepath,
                              "BackupProtocolVersion":     "1.6",
                              "TargetIdentifier":          self._device.udid}])
        while True:
            msg = recv_msg(self.sock)
            if not isinstance(msg, list) or len(msg) < 2: continue
            inner = msg[1] if isinstance(msg[1], dict) else {}
            t = inner.get("BackupMessageTypeKey")
            if t == "BackupMessageSendFilePiece":
                piece_data = inner.get("BackupFileDataKey", b"")
                try:
                    parsed = plistlib.loads(piece_data)
                    yield parsed.get("Path", "?"), parsed.get("Data", b"")
                except Exception:
                    yield "?", piece_data
                # ACK
                send_msg(self.sock, ["DLMessageProcessMessage",
                                     {"BackupMessageTypeKey":
                                          "kBackupMessageBackupFileReceived"}])
            elif t == "BackupMessageBackupFinished":
                return
            elif t == "BackupMessageBackupReplyRefused":
                raise IOError(f"device refused backup: {inner}")
            elif msg[0] == "DLMessageDisconnect":
                raise IOError(f"device disconnected: {msg}")

    # ── RESTORE (host → device) ─ WORKING (since wire-capture session) ───────
    def restore(self, files: list[BackupFile], host_basepath: str = "/tmp/robly"):
        """
        Send a forged backup back to the device so it writes the files.

        Wire protocol reconstructed from USBPcap capture of iTunes 7 →
        iPod Touch 1G (iOS 1.1.2):

          1. Host: kBackupMessageRestoreRequest (with manifest, BPV=1.7,
             BackupNotifySpringBoard=True, BackupRestoreSystemFiles=True)
          2. Device: BackupMessageRestoreReplyOK
          3. For each file:
             a. Host: ["DLSendFile", chunk_bytes, info_dict]
                where chunk_bytes is up to 8192 bytes of the FILE PIECE
                (the bplist envelope from our manifest building code)
                and info_dict has:
                  DLFileDest        = "/tmp/RestoreFile.plist"  (literal!)
                  DLFileStatusKey   = 1 for "more coming", 2 for "last/only"
                  DLFileOffsetKey   = byte offset within the file
                  DLFileSource      = some path string (iTunes uses local file path)
                  DLFileIsEncrypted = 0
                  DLFileAttributesKey = {filename, mode, size, ...}
             b. Device: BackupMessageRestoreFileReceived (ack)
          4. Host: DLMessageDisconnect "Thanks for the Memories"
        """
        manifest = make_manifest(self._device.udid, files)
        send_msg(self.sock, ["DLMessageProcessMessage",
                             {"BackupMessageTypeKey":      "kBackupMessageRestoreRequest",
                              "BackupComputerBasePathKey": host_basepath,
                              "BackupProtocolVersion":     "1.7",
                              "TargetIdentifier":          self._device.udid,
                              "BackupManifestKey":         manifest,
                              "BackupNotifySpringBoard":   True,
                              "BackupRestoreSystemFiles":  True,
                              "BackupPreserveSettings":    False,
                              "BackupPreserveCameraRoll":  False}])
        msg = recv_msg(self.sock)
        if msg[0] == "DLMessageDisconnect":
            raise IOError(f"refused before reply: {msg}")
        inner = msg[1] if isinstance(msg[1], dict) else {}
        if inner.get("BackupMessageTypeKey") == "BackupMessageRestoreReplyRefused":
            raise IOError(f"refused: {inner}")
        if inner.get("BackupMessageTypeKey") != "BackupMessageRestoreReplyOK":
            raise IOError(f"unexpected reply: {msg}")

        # Send each file in 8192-byte chunks
        CHUNK = 8192
        for f in files:
            piece = f.piece_bytes()    # bplist envelope (what we'd save as <hash>.mdbackup)
            total = len(piece)
            offset = 0
            while offset < total:
                chunk = piece[offset:offset + CHUNK]
                is_last = (offset + len(chunk) >= total)
                info = {
                    "DLFileAttributesKey": {
                        "LinkCount":            1,
                        "FileMode":             -32330,           # iTunes' value
                        "Filename":             f"{host_basepath}/{f.hash_id}.mdbackup",
                        "FileSystemFileNumber": 0,
                        "GroupOwnerAccountID":  0,
                        "DeviceType":           2,
                        "FileSize":             total,
                        "OwnerAccountID":       0,
                        "DeviceIdentifier":     2,
                    },
                    "DLFileSource":      f"{host_basepath}/{f.hash_id}.mdbackup",
                    "DLFileDest":        "/tmp/RestoreFile.plist",  # literal — device knows what to do
                    "DLFileStatusKey":   2 if is_last else 1,
                    "DLFileIsEncrypted": 0,
                    "DLFileOffsetKey":   offset,
                }
                send_msg(self.sock, ["DLSendFile", chunk, info])
                offset += len(chunk)
                # ACK comes ONLY after the last chunk of a file, not per chunk
                if is_last:
                    ack = recv_msg(self.sock)
                    if not (isinstance(ack, list) and ack[0] == "DLMessageProcessMessage"
                            and isinstance(ack[1], dict)
                            and ack[1].get("BackupMessageTypeKey") == "BackupMessageRestoreFileReceived"):
                        raise IOError(f"unexpected ack for file: {ack}")

        # CRITICAL: send BackupMessageRestoreComplete to tell device "we're done".
        # Without this, SpringBoard stays in sync mode (cable+music icon).
        send_msg(self.sock, ["DLMessageProcessMessage",
                             {"BackupMessageTypeKey": "BackupMessageRestoreComplete"}])
        # Device replies with DLMessageDisconnect "Thanks for the Memories".
        self.sock.settimeout(15)
        try:
            final = recv_msg(self.sock)
            if isinstance(final, list) and final[0] == "DLMessageDisconnect":
                pass  # device closed cleanly — SpringBoard returns to normal
        except Exception:
            pass

    def close(self):
        try:
            send_msg(self.sock, ["DLMessageDisconnect", "Done"])
        except Exception: pass
        try: self.sock.close()
        except Exception: pass
