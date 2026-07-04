"""Minimal ID3v2 tag reader + MP3 frame header decoder.

Reads the common text frames (title, artist, album, year) and inspects the
first MPEG audio frame to get real bitrate / sample rate / rough duration.

Hand-rolled so robly stays pure-Python (no mutagen dependency). Handles
ID3v2.3 (32-bit-size frames) and v2.4 (synchsafe-size frames) and all four
text encodings.
"""
from __future__ import annotations
import os
import re
import struct

# ─── MPEG audio tables ─────────────────────────────────────────────────────
# Layer III bitrate index → kbps. 0 = "free" (variable), 15 = reserved.
_BITRATE_V1_L3 = [None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None]
_BITRATE_V2_L3 = [None,  8, 16, 24, 32, 40, 48, 56,  64,  80,  96, 112, 128, 144, 160, None]
_SAMPLE_RATE = {
    3: [44100, 48000, 32000, None],   # MPEG 1
    2: [22050, 24000, 16000, None],   # MPEG 2
    0: [11025, 12000,  8000, None],   # MPEG 2.5
}


def _synchsafe(bs: bytes) -> int:
    """Decode a 4-byte synchsafe integer (7 bits per byte)."""
    return ((bs[0] & 0x7f) << 21 | (bs[1] & 0x7f) << 14
            | (bs[2] & 0x7f) << 7 | (bs[3] & 0x7f))


def _to_synchsafe(n: int) -> bytes:
    """Encode an int as a 4-byte synchsafe integer."""
    return bytes([(n >> 21) & 0x7f, (n >> 14) & 0x7f,
                  (n >> 7) & 0x7f, n & 0x7f])


def _decode_text(data: bytes) -> str:
    """Decode a text-frame body: 1 byte encoding + text bytes."""
    if not data:
        return ""
    enc, text = data[0], data[1:]
    if enc == 0:
        s = text.decode("latin-1", errors="replace")
    elif enc == 1:
        s = text.decode("utf-16", errors="replace")
    elif enc == 2:
        s = text.decode("utf-16-be", errors="replace")
    elif enc == 3:
        s = text.decode("utf-8", errors="replace")
    else:
        s = text.decode("latin-1", errors="replace")
    # Strip trailing nulls + surrounding whitespace
    return s.rstrip("\x00").strip()


def _parse_one_id3v2_tag(f) -> tuple[dict, int]:
    """Read one ID3v2 tag from the current file position.
    Returns (frames_dict, bytes_consumed). If there's no ID3 tag here,
    returns ({}, 0) and leaves the file position untouched.

    Some MP3s have TWO stacked ID3v2 tags (e.g. broken v2.3 from WMP followed
    by a proper v2.4 from Mp3tag). Caller loops on this to read them all.
    """
    start_pos = f.tell()
    header = f.read(10)
    if len(header) < 10 or header[:3] != b"ID3":
        f.seek(start_pos)
        return {}, 0

    major = header[3]
    tag_size = _synchsafe(header[6:10])
    footer  = 10 if (major >= 4 and (header[5] & 0x10)) else 0
    tag_data = f.read(tag_size)

    out = {}
    pos, hdr_sz = 0, 10
    while pos + hdr_sz <= len(tag_data):
        fid_bytes = tag_data[pos:pos + 4]
        if fid_bytes == b"\x00\x00\x00\x00":
            break  # padding
        size_bytes = tag_data[pos + 4:pos + 8]
        if major >= 4:
            frame_size = _synchsafe(size_bytes)
        else:
            frame_size = struct.unpack(">I", size_bytes)[0]
        body = tag_data[pos + hdr_sz:pos + hdr_sz + frame_size]
        pos += hdr_sz + frame_size
        try:
            fid = fid_bytes.decode("ascii")
        except UnicodeDecodeError:
            continue
        if fid in ("TIT2", "TPE1", "TALB", "TYER", "TDRC", "TCON"):
            val = _decode_text(body)
            if val:
                out[fid] = val

    return out, 10 + tag_size + footer


def _read_id3v2(f) -> dict:
    """Read all stacked ID3v2 tags at the start of the file. Later (usually
    newer) tags override earlier ones. Leaves f positioned right after the
    last ID3 tag so the caller can look for the first MPEG audio frame."""
    merged = {}
    while True:
        frames, consumed = _parse_one_id3v2_tag(f)
        if consumed == 0:
            break
        for k, v in frames.items():
            merged[k] = v  # later stack wins
    if not merged:
        # No ID3 at all — rewind so audio-scan starts at byte 0
        f.seek(0)
    return merged


def _read_id3v1(path: str) -> dict:
    """Read the ID3v1 tag from the last 128 bytes of file, if present.
    Format (fixed 128 bytes):
       3B 'TAG' + 30B title + 30B artist + 30B album + 4B year
       + 28B comment + 1B '\0' + 1B track (v1.1) + 1B genre
       or 30B comment + 1B genre (v1.0)
    All text fields are Latin-1, null- or space-padded.
    """
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-128, 2)
            except OSError:
                return {}
            tag = f.read(128)
    except Exception:
        return {}
    if len(tag) < 128 or tag[:3] != b"TAG":
        return {}

    def _s(bs: bytes) -> str:
        return bs.rstrip(b"\x00 ").decode("latin-1", errors="replace").strip()

    out = {}
    title  = _s(tag[3:33])
    artist = _s(tag[33:63])
    album  = _s(tag[63:93])
    year_s = _s(tag[93:97])
    if title:  out["title"]  = title
    if artist: out["artist"] = artist
    if album:  out["album"]  = album
    if year_s.isdigit():
        try: out["year"] = int(year_s)
        except ValueError: pass
    return out


def _find_first_mp3_frame(f, max_bytes: int = 512 * 1024) -> dict:
    """Search for the first MPEG-1/2 Layer III audio frame header."""
    scanned = 0
    while scanned < max_bytes:
        b = f.read(4)
        if len(b) < 4:
            return {}
        # Sync word: 11 bits of 1 → bytes 0xFF and 0xE0-0xFF top bits
        if b[0] == 0xFF and (b[1] & 0xE0) == 0xE0:
            hdr = struct.unpack(">I", b)[0]
            version_bits = (hdr >> 19) & 0x3
            layer_bits   = (hdr >> 17) & 0x3
            bitrate_idx  = (hdr >> 12) & 0xF
            sample_idx   = (hdr >> 10) & 0x3
            # We only care about Layer III
            if layer_bits != 0b01:
                f.seek(-3, 1); scanned += 1; continue
            table = _BITRATE_V1_L3 if version_bits == 3 else _BITRATE_V2_L3
            if not (0 < bitrate_idx < 15):
                f.seek(-3, 1); scanned += 1; continue
            bitrate = table[bitrate_idx]
            rates = _SAMPLE_RATE.get(version_bits)
            if not rates or sample_idx >= 4 or rates[sample_idx] is None:
                f.seek(-3, 1); scanned += 1; continue
            return {"bitrate_kbps": bitrate, "sample_rate": rates[sample_idx]}
        f.seek(-3, 1)
        scanned += 1
    return {}


# ─── Public API ─────────────────────────────────────────────────────────────

def read_metadata(mp3_path: str) -> dict:
    """Return whatever we can find out about a local .mp3 file.

    Possible keys (missing = unknown, caller decides fallback):
      title, artist, album, year (int),
      bitrate_kbps, sample_rate, duration_ms.
    """
    result: dict = {}
    try:
        file_size = os.path.getsize(mp3_path)
        with open(mp3_path, "rb") as f:
            id3 = _read_id3v2(f)
            audio = _find_first_mp3_frame(f)
    except Exception:
        return result

    if id3.get("TIT2"): result["title"]  = id3["TIT2"]
    if id3.get("TPE1"): result["artist"] = id3["TPE1"]
    if id3.get("TALB"): result["album"]  = id3["TALB"]
    # v2.3 = TYER (4-digit), v2.4 = TDRC ("2005", "2005-06-10T…")
    for yk in ("TDRC", "TYER"):
        if id3.get(yk):
            try:
                result["year"] = int(id3[yk][:4])
                break
            except (ValueError, TypeError):
                pass

    # Fall back to ID3v1 for any field missing from ID3v2. Common in files
    # tagged by Windows Media Player, which writes only PRIV frames into v2
    # and puts the actual title/artist/album in the v1 tag at the file end.
    need_v1 = any(k not in result for k in ("title", "artist", "album"))
    if need_v1:
        v1 = _read_id3v1(mp3_path)
        for k in ("title", "artist", "album", "year"):
            if k not in result and v1.get(k):
                result[k] = v1[k]

    if audio:
        result["bitrate_kbps"] = audio["bitrate_kbps"]
        result["sample_rate"]  = audio["sample_rate"]
        # Rough duration estimate (CBR assumption — good enough for iPod)
        result["duration_ms"] = int(file_size * 8 / audio["bitrate_kbps"])

    return result


_FILENAME_RE = re.compile(
    r"^\s*(?:\d+[\.\s\-]+)?(?P<artist>.+?)\s*-\s*(?P<title>.+?)\s*$")


def parse_filename(basename: str) -> dict:
    """Best-effort artist/title from an 'Artist - Title' filename.
    Returns {'artist': str, 'title': str} — both non-empty even for weird names."""
    m = _FILENAME_RE.match(basename)
    if m:
        return {"artist": m.group("artist").strip(),
                "title":  m.group("title").strip()}
    return {"artist": "", "title": basename.strip()}


def resolve_metadata(mp3_path: str, overrides: dict = None) -> dict:
    """Combine ID3 tags + filename parse + explicit overrides into the final
    metadata used by upload_music. Order of precedence:
      explicit override > ID3 tag > filename parse > empty/default.

    Fields returned: title, artist, album, year, bitrate_kbps, sample_rate,
    duration_ms. Album falls back to artist if still unknown. Year defaults
    to 0. Bitrate defaults to 128, sample_rate to 44100 if not detected.
    """
    id3 = read_metadata(mp3_path)
    base = os.path.splitext(os.path.basename(mp3_path))[0]
    parsed = parse_filename(base)
    ov = overrides or {}

    def pick(*sources):
        for s in sources:
            if s:  # non-empty string / non-zero / non-None
                return s
        return None

    # Strip anything from ID3 / filename parse that looks like junk. WMP is
    # famous for writing the track number into artist AND album, and filename
    # patterns like "07-Przesilenie" get parsed as artist="07". Otherwise
    # we'd tell the iPod the artist is "07" and it would create a "07" folder.
    def _clean(v):
        return None if (not v or _looks_bogus(v)) else v

    id3_artist    = _clean(id3.get("artist"))
    id3_album     = _clean(id3.get("album"))
    parsed_artist = _clean(parsed.get("artist"))

    title  = pick(ov.get("title"),  id3.get("title"),  parsed.get("title"))
    artist = pick(ov.get("artist"), id3_artist,        parsed_artist)
    album  = pick(ov.get("album"),  id3_album)
    year   = ov.get("year") or id3.get("year") or 0
    bitrate = ov.get("bitrate_kbps") or id3.get("bitrate_kbps") or 128
    sample  = ov.get("sample_rate")  or id3.get("sample_rate")  or 44100

    if not album and artist:
        album = artist

    duration = ov.get("duration_ms") or id3.get("duration_ms")
    if not duration:
        try:
            file_size = os.path.getsize(mp3_path)
            duration = file_size * 8 // bitrate
        except OSError:
            duration = 0

    return {
        "title": title or "",
        "artist": artist or "",
        "album": album or "",
        "year": int(year) if year else 0,
        "bitrate_kbps": int(bitrate),
        "sample_rate":  int(sample),
        "duration_ms":  int(duration),
    }


def _looks_bogus(v: str) -> bool:
    """Detect obviously-broken tag values that shouldn't be trusted as
    real metadata. Common WMP bug: writes the track number into artist/album."""
    v = (v or "").strip()
    if not v: return True
    lo = v.lower()
    if lo in ("unknown", "unknown artist", "unknown album", "unknown title",
              "various", "various artists", "n/a", "none"):
        return True
    # Pure digits (like "02", "17") — nearly always a bogus track-number leak
    if v.isdigit(): return True
    return False


def _build_text_frame_v23(fid: str, text: str) -> bytes:
    """Build one ID3v2.3 text frame using UTF-16-LE (encoding byte = 1).
    UTF-16 with BOM is the only Unicode encoding allowed in v2.3."""
    body = b"\x01" + b"\xff\xfe" + text.encode("utf-16-le") + b"\x00\x00"
    header = fid.encode("ascii") + struct.pack(">I", len(body)) + b"\x00\x00"
    return header + body


def write_id3v2(mp3_path: str, meta: dict,
                strip_id3v1: bool = True) -> None:
    """Replace (or add) the ID3v2.3 tag on an MP3 file with the given meta.

    meta may include: title, artist, album, year, genre. Empty/None fields
    are skipped (no frame written), which effectively removes them.

    If `strip_id3v1` is True, also removes the legacy 128-byte ID3v1 tag at
    the end of the file — mostly a good idea because if we've corrected the
    tags we don't want the old broken v1 hanging around and confusing readers.

    Writes atomically via `os.replace(tmp, dest)` so a crash mid-write won't
    corrupt the file. Audio data is preserved exactly.
    """
    # 1) Find how many bytes of existing (possibly stacked) ID3v2 tags
    #    live at the start — we'll replace them all with our new tag.
    old_tag_bytes = 0
    with open(mp3_path, "rb") as f:
        while True:
            f.seek(old_tag_bytes)
            head = f.read(10)
            if len(head) < 10 or head[:3] != b"ID3":
                break
            step = 10 + _synchsafe(head[6:10])
            if head[3] >= 4 and (head[5] & 0x10):  # v2.4 footer
                step += 10
            old_tag_bytes += step

    # 2) Build the new frames
    frames = b""
    for fid, text in (("TIT2", meta.get("title")),
                      ("TPE1", meta.get("artist")),
                      ("TALB", meta.get("album"))):
        text = (text or "").strip()
        if text:
            frames += _build_text_frame_v23(fid, text)
    year = meta.get("year")
    if year:
        frames += _build_text_frame_v23("TYER", str(int(year)))
    genre = (meta.get("genre") or "").strip()
    if genre:
        frames += _build_text_frame_v23("TCON", genre)

    # Small padding so future retags don't have to rewrite the audio if the
    # new tag is a bit smaller than the padded slot.
    padding = b"\x00" * 256
    tag_body_size = len(frames) + len(padding)
    id3_header = b"ID3\x03\x00\x00" + _to_synchsafe(tag_body_size)
    new_tag = id3_header + frames + padding

    # 3) Copy audio (everything after old tag, minus ID3v1 if we're stripping)
    tmp_path = mp3_path + ".robly-tmp"
    with open(mp3_path, "rb") as fin:
        fin.seek(old_tag_bytes)
        audio = fin.read()

    if strip_id3v1 and len(audio) >= 128 and audio[-128:-125] == b"TAG":
        audio = audio[:-128]

    with open(tmp_path, "wb") as fout:
        fout.write(new_tag)
        fout.write(audio)

    os.replace(tmp_path, mp3_path)


def has_missing_metadata(meta: dict) -> bool:
    """Return True if title / artist / album is missing, 'Unknown', or bogus
    (e.g. a track number leaked into the artist field)."""
    for k in ("title", "artist", "album"):
        if _looks_bogus(meta.get(k) or ""):
            return True
    # Extra check: WMP-broken files write track_num into BOTH artist and album.
    # If those two match and are short digits/junk, this batch clearly needs help.
    a = (meta.get("artist") or "").strip()
    al = (meta.get("album") or "").strip()
    if a and a == al and (a.isdigit() or len(a) <= 3):
        return True
    return False
