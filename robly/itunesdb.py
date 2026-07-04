"""
iTunesDB parser — extracts track metadata + on-device file paths.

The iTunesDB is a chunked binary at /iTunes_Control/iTunes/iTunesDB.
Each chunk has a 4-byte magic, a 4-byte header_size (LE), a 4-byte
total_size (LE), then chunk-specific fields and (often) child chunks.

Top-level structure:
  mhbd                         database
    mhsd (type=1) ──► mhlt ──► mhit*    (master track list)
                                  mhod*  (title, location, album, …)
    mhsd (type=2) ──► mhlp ──► mhyp*    (playlist list)
    mhsd (type=3) ──► album list (in newer DBs)

This implementation walks tracks and pulls the essentials:
  id · title · artist · album · genre · location · size · duration
"""
from __future__ import annotations
import struct
from dataclasses import dataclass


# mhod data-object types we read as strings
MHOD_TITLE    = 1
MHOD_LOCATION = 2
MHOD_ALBUM    = 3
MHOD_ARTIST   = 4
MHOD_GENRE    = 5
MHOD_FILETYPE = 6
MHOD_COMMENT  = 8
MHOD_COMPOSER = 12

_STR_NAMES = {
    MHOD_TITLE:    "title",
    MHOD_LOCATION: "location",
    MHOD_ALBUM:    "album",
    MHOD_ARTIST:   "artist",
    MHOD_GENRE:    "genre",
    MHOD_FILETYPE: "file_type",
    MHOD_COMMENT:  "comment",
    MHOD_COMPOSER: "composer",
}


@dataclass
class Track:
    id:         int = 0
    title:      str = ""
    artist:     str = ""
    album:      str = ""
    genre:      str = ""
    location:   str = ""      # iPod path, e.g. ":iTunes_Control:Music:F00:ABCD.m4a"
    file_size:  int = 0
    duration:   int = 0       # ms
    file_type:  str = ""
    composer:   str = ""
    comment:    str = ""

    @property
    def afc_path(self) -> str:
        """Translate iPod's `:Sep:Path` to AFC `/Sep/Path`."""
        if not self.location: return ""
        p = self.location.replace(":", "/")
        return p if p.startswith("/") else "/" + p


class iTunesDB:
    def __init__(self, data: bytes):
        if data[:4] != b"mhbd":
            raise ValueError(f"not an iTunesDB (magic={data[:4]!r})")
        self.data = data
        self.tracks: list[Track] = []
        self._parse()

    # ── parsing ──────────────────────────────────────────────────────────────
    def _parse(self):
        # mhbd: header_size at 4, total_size at 8
        hdr_sz = _u32(self.data, 4)
        # First child starts at hdr_sz.  Children: mhsd's.
        pos = hdr_sz
        while pos + 12 <= len(self.data):
            magic = self.data[pos:pos+4]
            if magic != b"mhsd":
                break
            mhsd_hdr  = _u32(self.data, pos + 4)
            mhsd_tot  = _u32(self.data, pos + 8)
            mhsd_type = _u32(self.data, pos + 12)
            if mhsd_type == 1:
                self._parse_track_list(pos + mhsd_hdr,
                                       pos + mhsd_tot)
            pos += mhsd_tot

    def _parse_track_list(self, start: int, end: int):
        if self.data[start:start+4] != b"mhlt":
            return
        mhlt_hdr  = _u32(self.data, start + 4)
        # mhlt has total track count at offset 8
        count = _u32(self.data, start + 8)
        pos = start + mhlt_hdr
        for _ in range(count):
            if pos + 4 > end or self.data[pos:pos+4] != b"mhit":
                break
            t, advance = self._parse_track(pos)
            self.tracks.append(t)
            pos += advance

    def _parse_track(self, pos: int) -> tuple[Track, int]:
        hdr_sz  = _u32(self.data, pos + 4)
        tot_sz  = _u32(self.data, pos + 8)
        n_mhod  = _u32(self.data, pos + 12)
        track = Track()
        track.id        = _u32(self.data, pos + 16)
        track.file_size = _u32(self.data, pos + 36)
        track.duration  = _u32(self.data, pos + 40)
        # mhod children begin at pos + hdr_sz
        p = pos + hdr_sz
        end = pos + tot_sz
        for _ in range(n_mhod):
            if p + 4 > end or self.data[p:p+4] != b"mhod":
                break
            mhod_hdr  = _u32(self.data, p + 4)
            mhod_tot  = _u32(self.data, p + 8)
            mhod_type = _u32(self.data, p + 12)
            if mhod_type in _STR_NAMES:
                s = _read_mhod_string(self.data, p, mhod_hdr, mhod_tot)
                if s is not None:
                    setattr(track, _STR_NAMES[mhod_type], s)
            p += mhod_tot
        return track, tot_sz


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _read_mhod_string(data: bytes, pos: int, hdr_sz: int, tot_sz: int) -> str | None:
    """
    For mhod string types, after the standard 24-byte header there's a
    body header containing:
       position (4)  — must be 1 for the data we want (some DBs use 2)
       length   (4)  — bytes of the UTF-16 LE string
       unused   (4)
       unused   (4)
       string   (length bytes, UTF-16 LE)
    """
    body_off = pos + hdr_sz
    if body_off + 16 > pos + tot_sz: return None
    length = _u32(data, body_off + 4)
    string_start = body_off + 16
    if string_start + length > pos + tot_sz: return None
    raw = data[string_start:string_start + length]
    try:
        return raw.decode("utf-16-le")
    except Exception:
        return raw.decode("utf-8", "replace")
