"""Minimal client for com.apple.mobile.notification_proxy.

Same framing as lockdown: 4-byte BE length prefix + plist body.
"""
import struct
import plistlib


class NotificationProxy:
    # Sync notifications iTunes posts (verified in WS_SEND capture)
    SYNC_WILL_START = "com.apple.itunes-mobdev.syncWillStart"
    SYNC_DID_START  = "com.apple.itunes-mobdev.syncDidStart"
    SYNC_DID_FINISH = "com.apple.itunes-mobdev.syncDidFinish"
    # Device-to-host notifications iTunes observes
    SYNC_CANCEL_REQUEST  = "com.apple.itunes-client.syncCancelRequest"
    SYNC_RESUME_REQUEST  = "com.apple.itunes-client.syncResumeRequest"
    SYNC_SUSPEND_REQUEST = "com.apple.itunes-client.syncSuspendRequest"

    def __init__(self, sock):
        self.sock = sock

    def post(self, name: str):
        msg = {"Command": "PostNotification", "Name": name}
        body = plistlib.dumps(msg)
        self.sock.sendall(struct.pack(">I", len(body)) + body)

    def observe(self, name: str):
        msg = {"Command": "ObserveNotification", "Name": name}
        body = plistlib.dumps(msg)
        self.sock.sendall(struct.pack(">I", len(body)) + body)

    def shutdown(self):
        msg = {"Command": "Shutdown"}
        body = plistlib.dumps(msg)
        self.sock.sendall(struct.pack(">I", len(body)) + body)

    def close(self):
        try: self.sock.close()
        except Exception: pass
