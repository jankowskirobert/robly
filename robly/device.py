"""
High-level Device API for robly.

    from robly import find_devices, Device

    devs = find_devices()
    with Device(devs[0]) as dev:
        for f in dev.afc.listdir("/iTunes_Control/Music"):
            print(f)
"""
import os
import re
import time
import plistlib
import contextlib

from . import usbmux
from .lockdown import LockdownClient, LOCKDOWN_PORT
from .nassl_wrap import NasslSocket
from .afc import AFCClient, MODE_WRONLY, MODE_RW, LOCK_EX, LOCK_NB, LOCK_UN
from .itunesdb import iTunesDB
from .itunesdb_writer import (add_track,
                              delete_track as _itdb_delete_track,
                              edit_track_metadata)
from .itunesdb_hash import write_hash58, algo_for_product_type
from .notification_proxy import NotificationProxy
from .mp3_metadata import resolve_metadata


def find_devices() -> list[dict]:
    """Return a list of device descriptors from usbmuxd.  Each has
    DeviceID, SerialNumber (= UDID), ProductID, ConnectionType."""
    return usbmux.list_devices()


def _pair_path(udid: str) -> str:
    return os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                        "Apple", "Lockdown", f"{udid}.plist")


class Device:
    """High-level handle: lockdownd session + AFC client."""

    def __init__(self, descriptor: dict):
        self.descriptor = descriptor
        self.udid = descriptor["SerialNumber"]
        self.device_id = descriptor["DeviceID"]
        self._pair = self._load_pair()

        # lockdownd session
        self._ld_sock = usbmux.connect_tunnel(self.device_id, LOCKDOWN_PORT)
        self.lockdown = LockdownClient(self._ld_sock)
        self.lockdown.query_type()

        vp = self.lockdown.validate_pair(self._pair["HostID"])
        if vp.get("Result") != "Success":
            raise RuntimeError(f"ValidatePair failed: {vp}")

        r = self.lockdown.request({
            "Request": "StartSession",
            "ProtocolVersion": "2",
            "HostID":     self._pair["HostID"],
            "SystemBUID": self._pair["SystemBUID"],
        })
        if r.get("Error"):
            raise RuntimeError(f"StartSession failed: {r}")
        if r.get("EnableSessionSSL"):
            self.lockdown.sock = NasslSocket(self.lockdown.sock,
                                             self._pair["HostCertificate"],
                                             self._pair["HostPrivateKey"])

        # AFC service
        afc_port = self.lockdown.start_service("com.apple.afc")
        afc_sock = usbmux.connect_tunnel(self.device_id, afc_port)
        self.afc = AFCClient(afc_sock)

    def _load_pair(self) -> dict:
        path = _pair_path(self.udid)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"No pair record at {path}.  Plug the iPod in with Apple Mobile "
                "Device Service running so iTunes creates the pairing.")
        with open(path, "rb") as f:
            return plistlib.load(f)

    device_type = "touch"
    music_root  = "/iTunes_Control/Music"
    itunesdb_path = "/iTunes_Control/iTunes/iTunesDB"

    def read_itunesdb(self) -> bytes:
        return self.afc.read_file(self.itunesdb_path)

    # ── convenience ──────────────────────────────────────────────────────────
    def info(self) -> dict:
        out = {}
        for k in ("DeviceName", "ProductType", "ProductVersion",
                  "BuildVersion", "DeviceClass", "SerialNumber",
                  "UniqueDeviceID"):
            try:
                v = self.lockdown.get_value(k)
                if v is not None: out[k] = v
            except Exception:
                pass
        out["UDID"] = self.udid
        try:
            di = self.afc.devinfo()
            for k in ("FSTotalBytes", "FSFreeBytes", "Model"):
                if k in di: out[k] = di[k]
        except Exception:
            pass
        return out

    def close(self):
        try: self.afc.sock.close()
        except Exception: pass
        try: self.lockdown.close()
        except Exception: pass

    def __enter__(self): return self
    def __exit__(self, *a): self.close()

    def reconnect(self):
        """Tear down and re-establish lockdown + AFC. Use if you hit a
        WinError 10053 (WSAECONNABORTED) from an idle session."""
        try: self.close()
        except Exception: pass
        # Re-run __init__ on the same descriptor
        Device.__init__(self, self.descriptor)

    def is_alive(self) -> bool:
        """Quick socket health check — does a no-op AFC call."""
        try:
            self.afc.devinfo()
            return True
        except Exception:
            return False

    def eject(self, physical_eject: bool = True):
        """Gracefully close the session so the cable can be unplugged.

        On iOS via usbmuxd there's no OS-level 'safely remove' step — the
        device is always ready to unplug. We just tear down lockdown + AFC
        cleanly. `physical_eject` is accepted for API symmetry with
        ClassicDevice but ignored here.
        """
        self.close()

    # ── sync mode ─────────────────────────────────────────────────────────────
    @contextlib.contextmanager
    def sync_session(self):
        """Acquire the iTunes-style sync locks so the iPod shows its
        'Synchronizing' screen and will reload iTunesDB when we release.

        Yields a `NotificationProxy` for posting custom messages if needed.

        On exit: removes the syncing flag, releases flock, closes handles,
        posts syncDidFinish, removes iTunesLock. iPod then reloads iTunesDB.
        """
        np_port = self.lockdown.start_service("com.apple.mobile.notification_proxy")
        np_sock = usbmux.connect_tunnel(self.device_id, np_port)
        np = NotificationProxy(np_sock)

        # Cleanup any stale locks from previous run
        for stale in ("/com.apple.itunes.syncing", "/iTunes_Control/iTunes/iTunesLock"):
            try: self.afc.remove(stale)
            except Exception: pass

        # iTunes-style acquire sequence (replicated exactly from iTunes 11.2)
        h_lock = self.afc.open("/iTunes_Control/iTunes/iTunesLock", MODE_WRONLY)
        self.afc.close_file(h_lock)
        h_ctrl = self.afc.open("/iTunes_Control/iTunes/iTunesControl", MODE_RW)
        np.post(NotificationProxy.SYNC_WILL_START)
        # iTunes does filler stats here — gives the iPod a moment to release its lock
        for p in ("/com.apple.itunes.lock_sync",
                  "/iTunes_Control/iTunes/iTunesUMediaLibraryDeletes.plist"):
            try: self.afc.stat(p)
            except Exception: pass
        h_sync = self.afc.open("/com.apple.itunes.lock_sync", MODE_RW)
        self.afc.flock(h_sync, LOCK_EX | LOCK_NB)
        np.post(NotificationProxy.SYNC_DID_START)
        h_syncing = self.afc.open("/com.apple.itunes.syncing", MODE_WRONLY)
        self.afc.close_file(h_syncing)

        try:
            yield np
        finally:
            for action in (
                lambda: self.afc.remove("/com.apple.itunes.syncing"),
                lambda: self.afc.flock(h_sync, LOCK_UN | LOCK_NB),
                lambda: self.afc.close_file(h_sync),
                lambda: self.afc.close_file(h_ctrl),
                lambda: np.post(NotificationProxy.SYNC_DID_FINISH),
                lambda: self.afc.remove("/iTunes_Control/iTunes/iTunesLock"),
            ):
                try: action()
                except Exception: pass
            try: np.close()
            except Exception: pass

    # ── download / backup ─────────────────────────────────────────────────────
    def backup_itunesdb(self, dest_path: str) -> int:
        """Download iPod's iTunesDB to local file. Returns size in bytes.

        Read-only — works even WITHOUT pair record (any plugged iPod).
        Save this file before any upload_music() call so you can restore if needed.
        """
        if not self.is_alive(): self.reconnect()
        data = self.afc.read_file("/iTunes_Control/iTunes/iTunesDB")
        with open(dest_path, "wb") as f:
            f.write(data)
        return len(data)

    def restore_itunesdb(self, src_path: str):
        """Upload a previously-backed-up iTunesDB back to the iPod.
        Requires trust state (paired host)."""
        if not self.is_alive(): self.reconnect()
        data = open(src_path, "rb").read()
        with self.sync_session():
            self.afc.write_file("/iTunes_Control/iTunes/iTunesDB", data, chunk=256*1024)

    def download_music(self, dest_dir: str, *,
                       name_template: str = "{artist} - {title}.mp3",
                       skip_existing: bool = True,
                       progress=True,
                       cancel_event=None) -> list[dict]:
        """Download every track in iTunesDB to `dest_dir` as regular MP3 files.

        Each track is named via `name_template` (placeholders: artist, title,
        album, year, id, file_type). Falls back to track id if metadata missing.

        Returns a list of dicts with what was downloaded (or skipped).

        Read-only — works without trust state, but iTunesDB must be readable
        (it is — `/iTunes_Control/iTunes/iTunesDB` is world-readable on all
        iPod generations).
        """
        if not self.is_alive(): self.reconnect()
        os.makedirs(dest_dir, exist_ok=True)
        db_bytes = self.afc.read_file("/iTunes_Control/iTunes/iTunesDB")
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

            if not t.afc_path:
                results.append({"id": t.id, "status": "no-location",
                                "display": display})
                continue
            ext = os.path.splitext(t.afc_path)[1] or ".mp3"
            name = name_template.format(
                artist=t.artist or "Unknown", title=t.title or f"track{t.id}",
                album=t.album or "", year="", id=t.id,
                file_type=t.file_type or "",
            )
            name = os.path.splitext(name)[0] + ext
            name = _sanitize_path_component(name)
            local = os.path.join(dest_dir, name)

            if skip_existing and os.path.exists(local):
                results.append({"id": t.id, "status": "skipped",
                                "path": local, "display": display})
                continue

            try:
                data = self.afc.read_file(t.afc_path)
                with open(local, "wb") as f: f.write(data)
                results.append({"id": t.id, "status": "ok", "path": local,
                                "bytes": len(data), "display": display})
            except Exception as e:
                results.append({"id": t.id, "status": f"error: {e}",
                                "display": display})

        return results

    # ── public music upload ───────────────────────────────────────────────────
    def upload_music(self, mp3_path: str, *,
                     title: str = None, artist: str = None, album: str = None,
                     year: int = 0, bitrate_kbps: int = 128,
                     sample_rate: int = 44100, folder: str = "F00",
                     on_device_name: str = None,
                     hash_algo: str = None) -> dict:
        """Upload a single MP3 to the iPod and make it appear in Music UI.

        Performs the full iTunes-replacement flow: enters sync mode, uploads
        the file via AFC, modifies iTunesDB to add a track entry, computes a
        valid hash58 signature, and exits sync mode so the iPod reloads.

        Required: `mp3_path` exists locally.

        Metadata: if title/artist/album are not provided, robly tries to
        parse them from the filename pattern `Artist - Title.mp3` or just
        `Title.mp3`. Album defaults to artist.

        Returns a dict with paths and parsed/used metadata.
        """
        if not os.path.exists(mp3_path):
            raise FileNotFoundError(mp3_path)

        data = open(mp3_path, "rb").read()
        file_size = len(data)

        # ID3 → filename → explicit overrides
        meta = resolve_metadata(mp3_path, overrides={
            "title": title, "artist": artist, "album": album,
            "year": year,
            "bitrate_kbps": bitrate_kbps if bitrate_kbps != 128 else None,
            "sample_rate":  sample_rate  if sample_rate  != 44100 else None,
        })
        title, artist, album = meta["title"], meta["artist"], meta["album"]
        year = meta["year"]
        bitrate_kbps = meta["bitrate_kbps"]
        sample_rate  = meta["sample_rate"]
        duration_ms  = meta["duration_ms"]

        # Build on-device path. Default name = sanitized version of mp3 basename.
        if on_device_name is None:
            on_device_name = _sanitize_filename(os.path.basename(mp3_path))
        on_device = f"/iTunes_Control/Music/{folder}/{on_device_name}"
        location  = on_device.replace("/", ":")

        # Heal stale connections before entering sync mode
        if not self.is_alive():
            self.reconnect()

        with self.sync_session():
            # 1. Upload the MP3
            self.afc.makedirs(f"/iTunes_Control/Music/{folder}")
            self.afc.write_file(on_device, data, chunk=256 * 1024)

            # 2. Pull, modify, sign, push iTunesDB
            db = self.afc.read_file("/iTunes_Control/iTunes/iTunesDB")
            db = add_track(
                db, title=title, artist=artist, album=album,
                location=location, file_size=file_size,
                duration_ms=duration_ms, bitrate=bitrate_kbps,
                sample_rate=sample_rate << 16, year=year,
            )
            # Pick hash algorithm. Auto-detect by device ProductType unless
            # the caller forced one with hash_algo=...
            if hash_algo is None:
                product = self.lockdown.get_value("ProductType") or ""
                hash_algo = algo_for_product_type(product)
            if hash_algo == "hash58":
                db = write_hash58(db, self.udid)
            elif hash_algo == "none":
                pass  # write DB as-is, older iPods don't check
            elif hash_algo == "hash72":
                raise NotImplementedError(
                    "hash72 (Touch 2G+ / iPhone) not implemented yet — "
                    "needs per-device HashInfo AES IV+random. "
                    "Pass hash_algo='hash58' to bypass auto-detect for now."
                )
            elif hash_algo == "hashAB":
                raise NotImplementedError(
                    "hashAB (Nano 6G, iOS 4+) not implemented yet."
                )
            else:
                raise ValueError(f"Unknown hash_algo: {hash_algo!r}")

            self.afc.write_file("/iTunes_Control/iTunes/iTunesDB", db, chunk=256 * 1024)

        return {
            "title": title, "artist": artist, "album": album,
            "on_device": on_device, "file_size": file_size,
            "udid": self.udid,
        }

    def upload_music_batch(self, items, *, hash_algo: str = None,
                            progress=None,
                            pre_delete_ids: list = None) -> list[dict]:
        """Upload many MP3s in ONE sync session (much safer than calling
        upload_music() in a loop, which opens/closes sync mode each time and
        eventually hits WSAECONNABORTED on the notification_proxy).

        items can be either:
          - list of file paths (uses filename for metadata)
          - list of dicts: {'path': ..., 'title': ..., 'artist': ..., 'album': ...,
                             'year': ..., 'folder': ..., 'on_device_name': ...}

        progress: optional callable(i, total, current_filename) for UI updates.
        """
        normalized = []
        for it in items:
            if isinstance(it, str):
                normalized.append({"path": it})
            elif isinstance(it, dict) and "path" in it:
                normalized.append(it)
            else:
                raise TypeError(f"unsupported item: {it!r}")

        # Resolve hash algorithm ONCE for the whole batch
        if hash_algo is None:
            product = self.lockdown.get_value("ProductType") or ""
            hash_algo = algo_for_product_type(product)
        if hash_algo == "hash72":
            raise NotImplementedError("hash72 not implemented yet")
        if hash_algo == "hashAB":
            raise NotImplementedError("hashAB not implemented yet")
        if hash_algo not in ("hash58", "none"):
            raise ValueError(f"Unknown hash_algo: {hash_algo!r}")

        # Heal stale connections — if the GUI / process has been idle for a
        # while, lockdownd's SSL session may have timed out, causing WinError
        # 10053 on the very first call inside sync_session().
        if not self.is_alive():
            self.reconnect()

        results = []
        with self.sync_session():
            # Pull iTunesDB ONCE, modify in memory across all uploads, push ONCE
            db = self.afc.read_file("/iTunes_Control/iTunes/iTunesDB")

            # Pre-delete any 'replace' targets from the same sync session.
            # Remove the physical MP3 first (path only exists while the mhit
            # is still in DB), then strip the DB entry.
            if pre_delete_ids:
                id_to_afc = {t.id: t.afc_path
                             for t in iTunesDB(db).tracks if t.afc_path}
                for tid in pre_delete_ids:
                    p = id_to_afc.get(tid)
                    if p:
                        try: self.afc.remove(p)
                        except Exception as e:
                            print(f"[replace] remove {p}: {e}")
                    try:
                        db = _itdb_delete_track(db, tid)
                    except ValueError:
                        pass  # id already gone

            for i, it in enumerate(normalized, 1):
                mp3_path = it["path"]
                if progress: progress(i, len(normalized), os.path.basename(mp3_path))

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
                    on_device = f"/iTunes_Control/Music/{folder}/{on_device_name}"
                    location = on_device.replace("/", ":")

                    self.afc.makedirs(f"/iTunes_Control/Music/{folder}")
                    self.afc.write_file(on_device, data, chunk=256*1024)
                    db = add_track(
                        db, title=title, artist=artist, album=album,
                        location=location, file_size=file_size,
                        duration_ms=duration_ms, bitrate=bitrate,
                        sample_rate=sample_rate << 16, year=year,
                    )
                    results.append({"ok": True, "path": mp3_path,
                                    "title": title, "artist": artist,
                                    "on_device": on_device})
                except Exception as e:
                    results.append({"ok": False, "path": mp3_path, "error": str(e)})

            # Sign + push the final DB once for the whole batch
            if hash_algo == "hash58":
                db = write_hash58(db, self.udid)
            self.afc.write_file("/iTunes_Control/iTunes/iTunesDB", db, chunk=256*1024)

        return results

    # ── edit / delete existing tracks ─────────────────────────────────────────
    def _pick_hash_algo(self, hash_algo: str = None) -> str:
        """Same auto-detect the upload flow uses."""
        if hash_algo is None:
            product = self.lockdown.get_value("ProductType") or ""
            hash_algo = algo_for_product_type(product)
        if hash_algo not in ("hash58", "none"):
            raise NotImplementedError(f"hash_algo {hash_algo!r} not supported here")
        return hash_algo

    def edit_track(self, track_id: int, **kw) -> None:
        """Rewrite this track's metadata in iTunesDB. Pass only the fields
        you want to change — others are left untouched. The physical MP3 file
        is not moved; only iTunesDB entries are changed."""
        self.edit_tracks([track_id], **kw)

    def edit_tracks(self, track_ids: list, *,
                    title: str = None, artist: str = None,
                    album: str = None, year: int = None,
                    hash_algo: str = None) -> None:
        """Bulk metadata edit — same values applied to every track_id.
        Runs inside ONE sync session so the iPod reloads exactly once."""
        if not track_ids: return
        algo = self._pick_hash_algo(hash_algo)
        if not self.is_alive(): self.reconnect()
        with self.sync_session():
            db = self.afc.read_file("/iTunes_Control/iTunes/iTunesDB")
            for tid in track_ids:
                try:
                    db = edit_track_metadata(db, tid,
                                             title=title, artist=artist,
                                             album=album, year=year)
                except ValueError as e:
                    print(f"[edit_tracks] id {tid}: {e}")
            if algo == "hash58":
                db = write_hash58(db, self.udid)
            self.afc.write_file("/iTunes_Control/iTunes/iTunesDB", db,
                                chunk=256*1024)

    def delete_track(self, track_id: int, *, delete_file: bool = True,
                     hash_algo: str = None) -> dict:
        """Remove a single track. Delegates to delete_tracks."""
        res = self.delete_tracks([track_id], delete_files=delete_file,
                                 hash_algo=hash_algo)
        removed = res["removed_files"][0] if res["removed_files"] else None
        return {"ok": True, "removed_file": removed}

    def delete_tracks(self, track_ids: list, *, delete_files: bool = True,
                      hash_algo: str = None) -> dict:
        """Bulk-delete tracks + their MP3 files in ONE sync session.
        Returns {'ok': True, 'removed_files': [paths_that_got_deleted]}."""
        if not track_ids:
            return {"ok": True, "removed_files": []}
        algo = self._pick_hash_algo(hash_algo)
        if not self.is_alive(): self.reconnect()

        # Snapshot the AFC path for each id BEFORE the DB is mutated.
        db0 = self.afc.read_file("/iTunes_Control/iTunes/iTunesDB")
        id_to_path = {t.id: t.afc_path
                      for t in iTunesDB(db0).tracks if t.afc_path}

        removed = []
        with self.sync_session():
            db = self.afc.read_file("/iTunes_Control/iTunes/iTunesDB")
            for tid in track_ids:
                try:
                    db = _itdb_delete_track(db, tid)
                except ValueError:
                    continue
                if delete_files:
                    p = id_to_path.get(tid)
                    if p:
                        try:
                            self.afc.remove(p)
                            removed.append(p)
                        except Exception as e:
                            print(f"[delete_tracks] could not remove {p}: {e}")
            if algo == "hash58":
                db = write_hash58(db, self.udid)
            self.afc.write_file("/iTunes_Control/iTunes/iTunesDB", db,
                                chunk=256*1024)
        return {"ok": True, "removed_files": removed}


# ── helpers ──────────────────────────────────────────────────────────────────
_FILENAME_RE = re.compile(r"^\s*(?:\d+[\.\s\-]+)?(?P<artist>.+?)\s*-\s*(?P<title>.+?)\s*$")

def _parse_filename(base: str) -> dict:
    """Best-effort artist/title parse from filename. Falls back to (Unknown, base)."""
    m = _FILENAME_RE.match(base)
    if m:
        return {"artist": m.group("artist").strip(), "title": m.group("title").strip()}
    return {"artist": "Unknown", "title": base.strip()}

def _sanitize_filename(name: str) -> str:
    """Sanitize for AFC + iPod filesystem. Replaces spaces with underscores,
    strips characters that often break on the device."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return safe or "track.mp3"

def _sanitize_path_component(name: str) -> str:
    """Sanitize for local filesystem (Windows-safe). Keeps spaces, replaces
    only characters that are invalid in filenames."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    safe = re.sub(r"\s+", " ", safe)
    return safe or "track.mp3"
