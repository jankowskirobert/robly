"""iPod Classic / Nano / Video / iPod 4G-5G device access via USB Mass Storage.

Older iPods (everything before iPod Touch and iPhone) don't use usbmuxd —
they mount as a plain USB Mass Storage disk. On Windows this gets a drive
letter (E:\\, F:\\ etc.). All communication is direct filesystem I/O.

Directory layout on the iPod's disk:

    <mount>/
      iPod_Control/          (hidden, HSA attributes on the FAT/HFS+ partition)
        Device/
          SysInfo            (text: model, serial, firmware version)
        iTunes/
          iTunesDB           (main media database — same mhbd format as Touch)
          iTunesPState       (playback state)
          iTunesEQPresets    (EQ)
        Music/
          F00/ F01/ ... F49/ (up to 50 subfolders for load balancing)
        Artwork/             (album art database + thumbnails)

Note the folder name — Classic uses `iPod_Control`, Touch uses `iTunes_Control`.

Hash algorithm required per model:
  - iPod 3G / 4G (2003-2004): NONE (older format, no signature)
  - iPod 5G (Video) / Nano 1G-2G: hash58
  - iPod Classic 6G+ / Nano 3G-5G: hash58
  - iPod Nano 6G / Touch 4G+: hashAB (not implemented)
"""
from __future__ import annotations
import os
import string
import shutil
import ctypes
from ctypes import wintypes
from pathlib import Path

from .itunesdb import iTunesDB
from .itunesdb_writer import (add_track,
                              delete_track as _itdb_delete_track,
                              edit_track_metadata)
from .itunesdb_hash import write_hash58, algo_for_product_type
from .mp3_metadata import resolve_metadata


IPOD_CONTROL = "iPod_Control"


# ─── Detection ──────────────────────────────────────────────────────────────

def find_classic_ipods() -> list[dict]:
    """Scan Windows drive letters for mounted iPods (Classic, Nano, Video).

    Returns descriptor dicts with keys: type, mount, name, model, family,
    serial, sysinfo (dict).
    """
    found = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        try:
            if not os.path.exists(root):
                continue
            control_dir = os.path.join(root, IPOD_CONTROL)
            if not os.path.isdir(control_dir):
                continue
            # This IS an iPod — grab metadata
            sysinfo = _read_sysinfo(control_dir)
            display_letter = letter
            name = (sysinfo.get("visibleName")
                    or sysinfo.get("ModelNumStr")
                    or f"iPod ({display_letter}:)")
            found.append({
                "type": "classic",
                "mount": root,
                "name": name,
                "model": sysinfo.get("ModelNumStr", "unknown"),
                "family": sysinfo.get("FamilyID", "unknown"),
                "serial": sysinfo.get("pszSerialNumber", ""),
                "product_type": _guess_product_type(sysinfo),
                "sysinfo": sysinfo,
            })
        except Exception:
            pass
    return found


def _read_sysinfo(control_dir: str) -> dict:
    """Parse the SysInfo text file if present. Format is `Key: Value` per line."""
    path = os.path.join(control_dir, "Device", "SysInfo")
    if not os.path.exists(path):
        return {}
    try:
        text = open(path, "r", encoding="latin-1", errors="ignore").read()
    except Exception:
        return {}
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def _guess_product_type(sysinfo: dict) -> str:
    """Convert SysInfo family/model to something like 'iPod4,1'."""
    family = sysinfo.get("FamilyID", "")
    # Best-effort mapping. If we don't know, return the raw family so the
    # hash-algo lookup in itunesdb_hash can still try.
    return family or "iPod"


# ─── Device class ───────────────────────────────────────────────────────────

class ClassicDevice:
    """iPod Classic / Nano / Video via USB Mass Storage (Windows drive letter).

    Presents the same public surface as `robly.Device` so the GUI and CLI can
    treat both interchangeably: `info`, `is_alive`, `reconnect`,
    `backup_itunesdb`, `restore_itunesdb`, `download_music`, `upload_music`,
    `upload_music_batch`, `sync_session`.
    """

    device_type = "classic"
    music_root  = "/iPod_Control/Music"
    itunesdb_path = "/iPod_Control/iTunes/iTunesDB"

    def __init__(self, descriptor: dict):
        if descriptor.get("type") != "classic":
            raise ValueError(f"Not a classic-iPod descriptor: {descriptor}")
        self.descriptor = descriptor
        self.mount = descriptor["mount"]
        self.udid = descriptor.get("serial", "")
        self.sysinfo = descriptor.get("sysinfo", {})
        self._product_type = descriptor.get("product_type", "iPod")

        control = os.path.join(self.mount, IPOD_CONTROL)
        if not os.path.isdir(control):
            raise FileNotFoundError(
                f"iPod_Control not found at {control!r} — is the iPod still mounted?"
            )

        # Cached iTunesDB for quick UI reads
        self.afc = _FilesystemAFC(control)

    def __enter__(self): return self
    def __exit__(self, *a): self.close()
    def close(self): pass  # filesystem — nothing to close

    # ── safe eject / dismount ──────────────────────────────────────────────
    def eject(self, physical_eject: bool = True) -> None:
        """Flush + dismount the iPod's volume so it can be unplugged safely.

        Equivalent to Windows' 'Safely Remove Hardware' — locks the volume
        (fails if some file is still open), tells the filesystem to flush,
        then dismounts. If `physical_eject` is True, also tries
        `IOCTL_STORAGE_EJECT_MEDIA` (harmless if the device isn't ejectable
        like a CD).
        """
        drive = self.mount.rstrip("\\").rstrip(":") + ":"
        _win32_eject_volume(drive, physical_eject=physical_eject)

    def _path(self, *parts) -> str:
        return os.path.join(self.mount, IPOD_CONTROL, *parts)

    # ── introspection ──────────────────────────────────────────────────────
    def info(self) -> dict:
        return {
            "DeviceName": self.descriptor.get("name") or "iPod",
            "ProductType": self._product_type,
            "ProductVersion": self.sysinfo.get("visibleBuildID", "?"),
            "SerialNumber": self.udid,
            "Mount": self.mount,
            "Model": self.descriptor.get("model", ""),
            "Family": self.descriptor.get("family", ""),
        }

    def is_alive(self) -> bool:
        return os.path.exists(self._path("iTunes", "iTunesDB"))

    def reconnect(self):
        # Nothing to do — filesystem is always fresh.
        if not self.is_alive():
            raise RuntimeError(
                f"iPod at {self.mount!r} disappeared. Reconnect and eject cleanly?"
            )

    # ── iTunesDB ───────────────────────────────────────────────────────────
    def read_itunesdb(self) -> bytes:
        return open(self._path("iTunes", "iTunesDB"), "rb").read()

    def write_itunesdb(self, data: bytes):
        with open(self._path("iTunes", "iTunesDB"), "wb") as f:
            f.write(data)

    def backup_itunesdb(self, dest_path: str) -> int:
        data = self.read_itunesdb()
        with open(dest_path, "wb") as f: f.write(data)
        return len(data)

    def restore_itunesdb(self, src_path: str):
        data = open(src_path, "rb").read()
        self.write_itunesdb(data)

    # ── sync mode — no-op on filesystem-backed iPods ───────────────────────
    from contextlib import contextmanager
    @contextmanager
    def sync_session(self):
        """No sync locks on Classic-style iPods — just yield.

        Physical safety: user must eject the drive from Windows after upload
        (via the taskbar Safely Remove Hardware) so the iPod flushes buffers
        and re-reads iTunesDB. robly can't force that.
        """
        yield None

    # ── music download ──────────────────────────────────────────────────────
    def download_music(self, dest_dir: str, *,
                       name_template: str = "{artist} - {title}.mp3",
                       skip_existing: bool = True,
                       progress=True,
                       cancel_event=None) -> list[dict]:
        """Copy every track referenced by iTunesDB to a local folder.

        `progress` can be:
          - True (default): print to stdout
          - False: no output
          - callable(n, total, track): called before each file with the track dict
            (or None if skipped early). Great for wiring to a GUI status bar.

        `cancel_event` is an optional `threading.Event`. If set, the loop stops
        at the next iteration and returns partial results.
        """
        os.makedirs(dest_dir, exist_ok=True)
        db_bytes = self.read_itunesdb()
        db = iTunesDB(db_bytes)

        def _tick(n, total, track):
            if callable(progress):
                try: progress(n, total, track)
                except Exception: pass
            elif progress is True:
                who = track.get("display", "?") if isinstance(track, dict) else "?"
                print(f"[{n}/{total}] {who}")

        results = []
        total = len(db.tracks)
        for n, t in enumerate(db.tracks, 1):
            if cancel_event is not None and cancel_event.is_set():
                break
            display = f"{t.artist or '?'} — {t.title or '?'}"
            _tick(n, total, {"id": t.id, "display": display})

            if not t.location:
                results.append({"id": t.id, "status": "no-location",
                                "display": display})
                continue
            fs_rel = t.location.lstrip(":").replace(":", os.sep)
            src = os.path.join(self.mount, fs_rel)
            if not os.path.exists(src):
                results.append({"id": t.id, "status": f"missing: {src}",
                                "display": display})
                continue

            ext = os.path.splitext(src)[1] or ".mp3"
            name = name_template.format(
                artist=t.artist or "Unknown", title=t.title or f"track{t.id}",
                album=t.album or "", year="", id=t.id,
                file_type=t.file_type or "",
            )
            name = os.path.splitext(name)[0] + ext
            name = _sanitize_local(name)
            local = os.path.join(dest_dir, name)

            if skip_existing and os.path.exists(local):
                results.append({"id": t.id, "status": "skipped",
                                "path": local, "display": display})
                continue
            try:
                shutil.copy2(src, local)
                results.append({"id": t.id, "status": "ok", "path": local,
                                "bytes": os.path.getsize(local),
                                "display": display})
            except Exception as e:
                results.append({"id": t.id, "status": f"error: {e}",
                                "display": display})
        return results

    # ── music upload ────────────────────────────────────────────────────────
    def upload_music(self, mp3_path: str, **kwargs) -> dict:
        """Upload one track. Uses upload_music_batch internally so we go through
        the same code path for both single and batch."""
        results = self.upload_music_batch([{"path": mp3_path, **kwargs}],
                                          hash_algo=kwargs.pop("hash_algo", None))
        r = results[0]
        if not r["ok"]:
            raise RuntimeError(r["error"])
        return r

    def upload_music_batch(self, items, *, hash_algo: str = None,
                           progress=None,
                           pre_delete_ids: list = None) -> list[dict]:
        import re
        # Normalize items to dicts
        normalized = []
        for it in items:
            if isinstance(it, str):
                normalized.append({"path": it})
            elif isinstance(it, dict) and "path" in it:
                normalized.append(it)
            else:
                raise TypeError(f"unsupported item: {it!r}")

        # Pick hash algo. Older iPods (4G-) don't sign iTunesDB at all.
        if hash_algo is None:
            hash_algo = algo_for_product_type(self._product_type)
            # Family = iPod (unknown or pre-5G): assume no hash — safest fallback
            if self._product_type == "iPod":
                hash_algo = "none"
        if hash_algo == "hash72":
            raise NotImplementedError("hash72 not implemented for Classic yet")
        if hash_algo == "hashAB":
            raise NotImplementedError("hashAB not implemented")
        if hash_algo not in ("hash58", "none"):
            raise ValueError(f"Unknown hash_algo: {hash_algo!r}")

        # Read DB once, modify in memory, write once
        db = self.read_itunesdb()

        # Pre-delete any 'replace' targets. Remove the physical file first
        # (path exists only while the mhit is still present), then strip DB.
        if pre_delete_ids:
            id_to_rel = {}
            for t in iTunesDB(db).tracks:
                if t.id in pre_delete_ids and t.location:
                    id_to_rel[t.id] = t.location.lstrip(":").replace(":", os.sep)
            for tid in pre_delete_ids:
                rel = id_to_rel.get(tid)
                if rel:
                    full = os.path.join(self.mount, rel)
                    try: os.remove(full)
                    except OSError as e:
                        print(f"[replace] remove {full}: {e}")
                try:
                    db = _itdb_delete_track(db, tid)
                except ValueError:
                    pass

        results = []
        for i, it in enumerate(normalized, 1):
            mp3_path = it["path"]
            if progress:
                progress(i, len(normalized), os.path.basename(mp3_path))
            try:
                if not os.path.exists(mp3_path):
                    raise FileNotFoundError(mp3_path)
                data = open(mp3_path, "rb").read()
                file_size = len(data)

                meta = resolve_metadata(mp3_path, overrides={
                    "title":  it.get("title"),
                    "artist": it.get("artist"),
                    "album":  it.get("album"),
                    "year":   it.get("year"),
                    "bitrate_kbps": it.get("bitrate_kbps"),
                    "sample_rate":  it.get("sample_rate"),
                })
                title, artist, album = meta["title"], meta["artist"], meta["album"]
                year        = meta["year"]
                bitrate     = meta["bitrate_kbps"]
                sample_rate = meta["sample_rate"]
                duration_ms = meta["duration_ms"]

                folder = it.get("folder", "F00")
                on_device_name = it.get("on_device_name") or _sanitize_filename(
                    os.path.basename(mp3_path))

                dest_fs = self._path("Music", folder, on_device_name)
                os.makedirs(os.path.dirname(dest_fs), exist_ok=True)
                shutil.copyfile(mp3_path, dest_fs)

                # iTunesDB stores the path as ":iPod_Control:Music:F00:filename"
                location = f":{IPOD_CONTROL}:Music:{folder}:{on_device_name}"

                db = add_track(
                    db, title=title, artist=artist, album=album,
                    location=location, file_size=file_size,
                    duration_ms=duration_ms, bitrate=bitrate,
                    sample_rate=sample_rate << 16, year=year,
                )
                results.append({
                    "ok": True, "path": mp3_path,
                    "title": title, "artist": artist, "album": album,
                    "on_device": dest_fs,
                })
            except Exception as e:
                results.append({"ok": False, "path": mp3_path, "error": str(e)})

        # Sign + write DB
        if hash_algo == "hash58":
            db = write_hash58(db, self.udid)
        self.write_itunesdb(db)
        return results

    # ── edit / delete existing tracks ──────────────────────────────────────
    def _pick_hash_algo(self, hash_algo: str = None) -> str:
        if hash_algo is None:
            hash_algo = algo_for_product_type(self._product_type)
            if self._product_type == "iPod":
                hash_algo = "none"
        if hash_algo not in ("hash58", "none"):
            raise NotImplementedError(f"hash_algo {hash_algo!r} not supported here")
        return hash_algo

    def edit_track(self, track_id: int, **kw) -> None:
        self.edit_tracks([track_id], **kw)

    def edit_tracks(self, track_ids: list, *,
                    title: str = None, artist: str = None,
                    album: str = None, year: int = None,
                    hash_algo: str = None) -> None:
        if not track_ids: return
        algo = self._pick_hash_algo(hash_algo)
        db = self.read_itunesdb()
        for tid in track_ids:
            try:
                db = edit_track_metadata(db, tid, title=title, artist=artist,
                                         album=album, year=year)
            except ValueError as e:
                print(f"[edit_tracks] id {tid}: {e}")
        if algo == "hash58":
            db = write_hash58(db, self.udid)
        self.write_itunesdb(db)

    def delete_track(self, track_id: int, *, delete_file: bool = True,
                     hash_algo: str = None) -> dict:
        res = self.delete_tracks([track_id], delete_files=delete_file,
                                 hash_algo=hash_algo)
        removed = res["removed_files"][0] if res["removed_files"] else None
        return {"ok": True, "removed_file": removed}

    def delete_tracks(self, track_ids: list, *, delete_files: bool = True,
                      hash_algo: str = None) -> dict:
        if not track_ids:
            return {"ok": True, "removed_files": []}
        algo = self._pick_hash_algo(hash_algo)
        db = self.read_itunesdb()

        # Snapshot paths for each id BEFORE mutating db
        id_to_full = {}
        for t in iTunesDB(db).tracks:
            if t.id in track_ids and t.location:
                rel = t.location.lstrip(":").replace(":", os.sep)
                id_to_full[t.id] = os.path.join(self.mount, rel)

        removed = []
        for tid in track_ids:
            try:
                db = _itdb_delete_track(db, tid)
            except ValueError:
                continue
            if delete_files:
                p = id_to_full.get(tid)
                if p:
                    try:
                        os.remove(p)
                        removed.append(p)
                    except OSError as e:
                        print(f"[delete_tracks] could not remove {p}: {e}")

        if algo == "hash58":
            db = write_hash58(db, self.udid)
        self.write_itunesdb(db)
        return {"ok": True, "removed_files": removed}


# ─── Helpers ───────────────────────────────────────────────────────────────

def _parse_filename(base: str) -> dict:
    import re
    m = re.match(r"^\s*(?:\d+[\.\s\-]+)?(?P<artist>.+?)\s*-\s*(?P<title>.+?)\s*$",
                 base)
    if m:
        return {"artist": m.group("artist").strip(),
                "title": m.group("title").strip()}
    return {"artist": "Unknown", "title": base.strip()}

def _sanitize_filename(name: str) -> str:
    import re
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return safe or "track.mp3"

def _sanitize_local(name: str) -> str:
    import re
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "track.mp3"


# ─── Win32 volume eject ────────────────────────────────────────────────────

_GENERIC_READ  = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_RW = 0x00000003
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_FSCTL_LOCK_VOLUME        = 0x00090018
_FSCTL_DISMOUNT_VOLUME    = 0x00090020
_IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808

def _win32_eject_volume(drive_letter_colon: str, *, physical_eject: bool = True):
    """Windows-native safe eject. drive_letter_colon = 'E:', 'F:' etc."""
    if os.name != "nt":
        raise RuntimeError("safe eject is Windows-only")

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype  = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.DeviceIoControl.restype  = wintypes.BOOL
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD),
                                    ctypes.c_void_p]
    k32.CloseHandle.restype  = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    path = "\\\\.\\" + drive_letter_colon
    handle = k32.CreateFileW(
        path, _GENERIC_READ | _GENERIC_WRITE, _FILE_SHARE_RW,
        None, _OPEN_EXISTING, 0, None,
    )
    if not handle or handle == _INVALID_HANDLE:
        raise OSError(f"CreateFile({path}) failed: WinError {ctypes.get_last_error()}")

    try:
        n = wintypes.DWORD(0)

        # 1) Lock — fails if any file is still open on the volume.
        # Retry a few times because our own AFC read may have just closed
        # a handle and Windows may need a moment to release it.
        import time
        locked = False
        for _ in range(6):
            if k32.DeviceIoControl(handle, _FSCTL_LOCK_VOLUME,
                                    None, 0, None, 0, ctypes.byref(n), None):
                locked = True
                break
            time.sleep(0.3)
        if not locked:
            raise OSError(
                f"Lock volume {drive_letter_colon} failed (WinError "
                f"{ctypes.get_last_error()}). Close any programs that "
                f"have files open on it, then try again.")

        # 2) Dismount — tells the filesystem to flush and mark itself gone.
        if not k32.DeviceIoControl(handle, _FSCTL_DISMOUNT_VOLUME,
                                   None, 0, None, 0, ctypes.byref(n), None):
            raise OSError(
                f"Dismount volume {drive_letter_colon} failed: WinError "
                f"{ctypes.get_last_error()}")

        # 3) (Optional) Physical eject — harmless if the device isn't ejectable.
        if physical_eject:
            k32.DeviceIoControl(handle, _IOCTL_STORAGE_EJECT_MEDIA,
                                None, 0, None, 0, ctypes.byref(n), None)
    finally:
        k32.CloseHandle(handle)


# ─── Filesystem-backed AFC-like adapter ────────────────────────────────────

class _FilesystemAFC:
    """Minimal AFC-look-alike so the GUI can browse a Classic iPod's files
    with the same code path it uses for Touch's AFC."""

    def __init__(self, root_control_dir: str):
        # root_control_dir = e.g. "E:\iPod_Control"
        self._root = os.path.dirname(root_control_dir)  # e.g. "E:\"

    def _abs(self, path: str) -> str:
        # `path` from the GUI is POSIX-style relative to the iPod's root,
        # e.g. "/iPod_Control/Music" or "/iPod_Control/Music/F00/foo.mp3"
        rel = path.lstrip("/").replace("/", os.sep)
        return os.path.join(self._root, rel)

    def listdir(self, path: str) -> list[str]:
        return sorted(os.listdir(self._abs(path)))

    def stat(self, path: str) -> dict:
        st = os.stat(self._abs(path))
        return {
            "st_size": st.st_size,
            "st_blocks": (st.st_size + 511) // 512,
            "st_ifmt": "S_IFDIR" if os.path.isdir(self._abs(path)) else "S_IFREG",
            "st_mtime": st.st_mtime,
        }

    def read_file(self, path: str) -> bytes:
        with open(self._abs(path), "rb") as f: return f.read()
