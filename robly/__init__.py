"""robly — iPod music browser/uploader.

Supports two device families:
  · iPod Touch / iPhone (iOS) — via usbmuxd + lockdownd + AFC
  · iPod Classic / Nano / Video / older (mass storage) — via Windows drive letter

Use `find_all_devices()` to enumerate BOTH and `connect(descriptor)` to open one.
"""
from .device import Device, find_devices
from .classic import ClassicDevice, find_classic_ipods
from .afc import AFCError
from .itunesdb import iTunesDB, Track
from .mp3_metadata import (
    read_metadata, resolve_metadata, has_missing_metadata, write_id3v2,
)


def find_all_devices() -> list[dict]:
    """List every iPod robly can see, of any type.

    Returned dicts have a `type` key ('touch' or 'classic') plus type-specific
    fields. Pass one to `connect()` to get the corresponding Device object.
    """
    devs: list[dict] = []
    # iOS devices via usbmuxd
    try:
        for d in find_devices():
            devs.append({
                "type": "touch",
                "descriptor": d,
                "name": d.get("SerialNumber", "iOS device")[:16],
                "serial": d.get("SerialNumber", ""),
            })
    except Exception:
        pass
    # Mass storage iPods via drive-letter scan
    try:
        devs.extend(find_classic_ipods())
    except Exception:
        pass
    return devs


def connect(descriptor: dict):
    """Factory: open the right device class for a descriptor from find_all_devices()."""
    t = descriptor.get("type")
    if t == "touch":
        return Device(descriptor["descriptor"])
    if t == "classic":
        return ClassicDevice(descriptor)
    raise ValueError(f"Unknown device type: {t!r}")


__all__ = [
    "Device", "ClassicDevice", "find_devices", "find_classic_ipods",
    "find_all_devices", "connect", "AFCError", "iTunesDB", "Track",
    "read_metadata", "resolve_metadata", "has_missing_metadata", "write_id3v2",
]
__version__ = "0.2.0"
