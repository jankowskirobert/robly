"""
Add a single Track to an existing iTunesDB.

Strategy:
  - Clone the first existing mhit as a template, change track_id, file_size,
    and the 5 mhods (title, artist, album, file_type, location).
  - Add an mhip pointing to the new track in each master playlist (hidden=1)
    in mhsd type=3 and type=2.
  - Splice all three insertions into the binary and fix parent sizes/counts.

This is the minimum viable approach for iPod Touch 1G / iOS 1.1.2 iTunesDB.
"""
from __future__ import annotations
import struct


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _build_string_mhod(mhod_type: int, text: str) -> bytes:
    """Build a string mhod chunk (UTF-16-LE)."""
    s = text.encode("utf-16-le")
    body = struct.pack("<IIII", 1, len(s), 0, 0) + s
    # 24-byte standard header: magic(4) hdr_sz(4) tot_sz(4) type(4) + 8 zero bytes
    hdr_sz = 24
    tot_sz = hdr_sz + len(body)
    header = struct.pack("<4sIII", b"mhod", hdr_sz, tot_sz, mhod_type) + b"\x00" * 8
    return header + body


def _build_new_mhit(template_mhit: bytes, *, track_id: int, file_size: int,
                    title: str, artist: str, album: str,
                    location: str, file_type_label: str = "plik audio MPEG",
                    duration_ms: int = 0, bitrate: int = 0,
                    sample_rate: int = 0, year: int = 0) -> bytes:
    """Take an existing mhit blob (header + its mhods) and build a NEW one
    keeping the 584-byte header layout but with new id/size and 5 string mhods."""
    hdr_sz = _u32(template_mhit, 4)        # 584
    assert hdr_sz == 584, f"unexpected mhit hdr_sz={hdr_sz}"

    # Copy the 584-byte mhit header verbatim, then patch fields
    header = bytearray(template_mhit[:hdr_sz])
    struct.pack_into("<I", header, 12, 5)            # n_mhod = 5
    struct.pack_into("<I", header, 16, track_id)     # track id
    struct.pack_into("<I", header, 20, 1)            # visible
    # +24..28 = file_type code, e.g. ' 3PM' = 'MP3 ' little-endian
    header[24:28] = b" 3PM"                          # "MP3 " stored as " 3PM" LE
    struct.pack_into("<I", header, 28, 256)          # VBR flag (template uses 256)
    struct.pack_into("<I", header, 36, file_size)
    struct.pack_into("<I", header, 40, duration_ms)
    struct.pack_into("<I", header, 52, year)
    struct.pack_into("<I", header, 56, bitrate)
    struct.pack_into("<I", header, 60, sample_rate)

    # Build 5 mhods in iTunes order
    body = (
        _build_string_mhod(1, title) +
        _build_string_mhod(4, artist) +
        _build_string_mhod(3, album) +
        _build_string_mhod(6, file_type_label) +
        _build_string_mhod(2, location)
    )

    total = hdr_sz + len(body)
    struct.pack_into("<I", header, 8, total)         # mhit total_size
    return bytes(header) + body


def _build_new_mhip(template_mhip: bytes, *, track_id: int, playlist_id: int) -> bytes:
    """Clone an existing mhip + its single inner mhod, patch track_id and
    playlist_id, return new bytes (same length as template)."""
    out = bytearray(template_mhip)
    # mhip header: tot_sz at +8, n_mhods at +12, playlist_id at +20,
    # track_id at +24
    struct.pack_into("<I", out, 20, playlist_id)
    struct.pack_into("<I", out, 24, track_id)
    return bytes(out)


def add_track(db: bytes, *, title: str, artist: str, album: str,
              location: str, file_size: int,
              duration_ms: int = 0, bitrate: int = 0,
              sample_rate: int = 0, year: int = 0,
              file_type_label: str = "plik audio MPEG") -> bytes:
    """Return a NEW iTunesDB byte string with one track inserted."""
    if db[:4] != b"mhbd":
        raise ValueError("not an iTunesDB")

    mhbd_hdr = _u32(db, 4)
    mhbd_tot = _u32(db, 8)
    assert mhbd_tot == len(db), f"mhbd tot={mhbd_tot} but db len={len(db)}"

    # Locate the chunks we need: mhsd type=1 (tracks), mhsd type=2/3 (master plists)
    mhsd_locs = {}     # type -> (start_off, hdr, tot)
    pos = mhbd_hdr
    while pos + 12 <= len(db):
        if db[pos:pos+4] != b"mhsd":
            break
        h = _u32(db, pos+4); t = _u32(db, pos+8); ty = _u32(db, pos+12)
        mhsd_locs[ty] = (pos, h, t)
        pos += t

    # === Tracks list ===
    if 1 not in mhsd_locs:
        raise ValueError("no mhsd type=1 (track list)")
    mhsd1_off, mhsd1_hdr, mhsd1_tot = mhsd_locs[1]
    mhlt_off = mhsd1_off + mhsd1_hdr
    assert db[mhlt_off:mhlt_off+4] == b"mhlt"
    mhlt_hdr = _u32(db, mhlt_off + 4)
    mhlt_count = _u32(db, mhlt_off + 8)
    # Walk to end of last mhit (so we know where to splice)
    track_ids = []
    p = mhlt_off + mhlt_hdr
    template_mhit_off = p              # first mhit position
    for _ in range(mhlt_count):
        if db[p:p+4] != b"mhit":
            raise ValueError(f"expected mhit at 0x{p:x}")
        track_ids.append(_u32(db, p + 16))
        p += _u32(db, p + 8)
    insert_mhit_off = p                # right after last mhit
    template_mhit_size = _u32(db, template_mhit_off + 8)
    template_mhit = db[template_mhit_off:template_mhit_off + template_mhit_size]

    new_track_id = max(track_ids) + 1 if track_ids else 1

    # === Master playlists in mhsd type=3 and type=2 ===
    plist_targets = []   # list of (mhsd_type, mhyp_off, last_mhip_off, last_mhip_size)
    for plist_type in (3, 2):
        if plist_type not in mhsd_locs:
            continue
        mhsd_off, mhsd_hdr_sz, mhsd_tot = mhsd_locs[plist_type]
        mhlp_off = mhsd_off + mhsd_hdr_sz
        assert db[mhlp_off:mhlp_off+4] == b"mhlp"
        mhlp_hdr = _u32(db, mhlp_off + 4)
        mhlp_count = _u32(db, mhlp_off + 8)

        # Find FIRST master playlist (hidden=1)
        pp = mhlp_off + mhlp_hdr
        master_off = None
        for _ in range(mhlp_count):
            if db[pp:pp+4] != b"mhyp": break
            hidden = _u32(db, pp + 20)
            if hidden == 1 and master_off is None:
                master_off = pp
            pp += _u32(db, pp + 8)
        if master_off is None:
            continue

        mhyp_hdr = _u32(db, master_off + 4)
        mhyp_tot = _u32(db, master_off + 8)
        n_mhip   = _u32(db, master_off + 16)
        # Walk children: title mhod + assorted mhods + n_mhip mhip's
        c_pos = master_off + mhyp_hdr
        c_end = master_off + mhyp_tot
        last_mhip_off = None
        max_pl_id = 0
        # Skip non-mhip mhods first, then count mhip's
        seen_mhip = 0
        while c_pos < c_end and seen_mhip < n_mhip:
            magic = db[c_pos:c_pos+4]
            tot = _u32(db, c_pos + 8)
            if magic == b"mhip":
                pl_id = _u32(db, c_pos + 20)
                if pl_id > max_pl_id: max_pl_id = pl_id
                last_mhip_off = c_pos
                seen_mhip += 1
            c_pos += tot
        if last_mhip_off is None:
            continue
        last_mhip_size = _u32(db, last_mhip_off + 8)
        template_mhip = db[last_mhip_off:last_mhip_off + last_mhip_size]
        insert_mhip_off = last_mhip_off + last_mhip_size  # right after last mhip
        new_pl_id = max_pl_id + 1
        plist_targets.append((plist_type, master_off, insert_mhip_off,
                              template_mhip, new_pl_id))

    # Build the new chunks
    new_mhit = _build_new_mhit(
        template_mhit,
        track_id=new_track_id, file_size=file_size,
        title=title, artist=artist, album=album,
        location=location, file_type_label=file_type_label,
        duration_ms=duration_ms, bitrate=bitrate,
        sample_rate=sample_rate, year=year,
    )

    new_mhips = []   # list parallel to plist_targets
    for plist_type, master_off, ins_off, tmpl, new_pl_id in plist_targets:
        nm = _build_new_mhip(tmpl, track_id=new_track_id, playlist_id=new_pl_id)
        new_mhips.append(nm)

    # === Splice in order from HIGHEST offset to LOWEST so earlier offsets are stable ===
    # Order: mhit_insert (lowest) < mhip1_insert < mhip2_insert (highest, if both present)
    insertions = [(insert_mhit_off, new_mhit, "mhit")]
    for i, (_, _, ins_off, _, _) in enumerate(plist_targets):
        insertions.append((ins_off, new_mhips[i], f"mhip{i}"))
    insertions.sort(key=lambda x: x[0])

    out = bytearray(db)
    # Apply insertions from high to low so unspliced offsets don't shift mid-insert
    for ins_off, chunk, label in reversed(insertions):
        out[ins_off:ins_off] = chunk

    # Now compute new offsets for each "anchor" we need to update.
    # We know: each insertion shifts everything at or after its position by len(chunk).
    def shift_for(orig_off):
        s = 0
        for ins_off, chunk, _ in insertions:
            if ins_off <= orig_off:
                s += len(chunk)
        return s

    # Re-locate mhsd1, mhlt, mhsd2, mhsd3, master mhyp's
    # mhlt is inside mhsd type=1; mhit insertion is BEFORE mhlt's parent chunks of type 2/3
    # Update mhlt count: +1
    mhlt_new_off = mhlt_off + shift_for(mhlt_off)
    new_count = _u32(out, mhlt_new_off + 8) + 1
    struct.pack_into("<I", out, mhlt_new_off + 8, new_count)

    # mhsd type=1 total += len(new_mhit)
    mhsd1_new_off = mhsd1_off + shift_for(mhsd1_off)
    struct.pack_into("<I", out, mhsd1_new_off + 8,
                     _u32(out, mhsd1_new_off + 8) + len(new_mhit))

    # For each playlist target: bump n_mhip on its mhyp and bump tot's
    for i, (plist_type, master_off, ins_off, tmpl, new_pl_id) in enumerate(plist_targets):
        new_mhip = new_mhips[i]
        # The master_off itself shifts only by insertions that happened BEFORE master_off
        master_new = master_off + shift_for(master_off)
        # mhyp n_mhip += 1
        struct.pack_into("<I", out, master_new + 16, _u32(out, master_new + 16) + 1)
        # mhyp tot += len(new_mhip)
        struct.pack_into("<I", out, master_new + 8, _u32(out, master_new + 8) + len(new_mhip))
        # mhsd type=plist_type tot += len(new_mhip)
        mhsd_off, _, _ = mhsd_locs[plist_type]
        mhsd_new = mhsd_off + shift_for(mhsd_off)
        struct.pack_into("<I", out, mhsd_new + 8, _u32(out, mhsd_new + 8) + len(new_mhip))

    # mhbd tot grows by all insertions
    delta_total = sum(len(c) for _, c, _ in insertions)
    struct.pack_into("<I", out, 8, _u32(out, 8) + delta_total)

    assert len(out) == len(db) + delta_total
    return bytes(out)


# ─── delete_track / edit_track_metadata ─────────────────────────────────────

def _walk_mhsds(db: bytes) -> dict:
    """Return {type: (offset, hdr_size, total_size)} for every top-level mhsd."""
    out = {}
    pos = _u32(db, 4)  # mhbd hdr
    while pos + 12 <= len(db):
        if db[pos:pos+4] != b"mhsd":
            break
        h  = _u32(db, pos + 4)
        t  = _u32(db, pos + 8)
        ty = _u32(db, pos + 12)
        out[ty] = (pos, h, t)
        pos += t
    return out


def _find_mhit_offset(db: bytes, track_id: int) -> tuple[int, int]:
    """Return (offset, total_size) of the mhit for the given track id.
    Raises ValueError if not found."""
    mhsd_locs = _walk_mhsds(db)
    if 1 not in mhsd_locs:
        raise ValueError("no mhsd type=1 (track list)")
    mhsd1_off, mhsd1_hdr, _ = mhsd_locs[1]
    mhlt_off = mhsd1_off + mhsd1_hdr
    assert db[mhlt_off:mhlt_off+4] == b"mhlt", "expected mhlt"
    mhlt_hdr = _u32(db, mhlt_off + 4)
    mhlt_count = _u32(db, mhlt_off + 8)
    p = mhlt_off + mhlt_hdr
    for _ in range(mhlt_count):
        if db[p:p+4] != b"mhit":
            raise ValueError(f"expected mhit at 0x{p:x}")
        if _u32(db, p + 16) == track_id:
            return p, _u32(db, p + 8)
        p += _u32(db, p + 8)
    raise ValueError(f"track_id {track_id} not found")


def _find_mhips_for_track(db: bytes, track_id: int) -> list:
    """Every mhip pointing to this track_id, across all playlists.
    Returns list of tuples (offset, size, mhsd_type, parent_mhyp_offset)."""
    hits = []
    mhsd_locs = _walk_mhsds(db)
    for plist_type in (2, 3):
        if plist_type not in mhsd_locs:
            continue
        mhsd_off, mhsd_hdr_sz, mhsd_tot = mhsd_locs[plist_type]
        mhlp_off = mhsd_off + mhsd_hdr_sz
        if db[mhlp_off:mhlp_off+4] != b"mhlp":
            continue
        mhlp_hdr = _u32(db, mhlp_off + 4)
        mhlp_count = _u32(db, mhlp_off + 8)
        pp = mhlp_off + mhlp_hdr
        for _ in range(mhlp_count):
            if db[pp:pp+4] != b"mhyp":
                break
            mhyp_hdr = _u32(db, pp + 4)
            mhyp_tot = _u32(db, pp + 8)
            n_mhip   = _u32(db, pp + 16)
            c_pos = pp + mhyp_hdr
            c_end = pp + mhyp_tot
            seen = 0
            while c_pos < c_end and seen < n_mhip:
                magic = db[c_pos:c_pos+4]
                tot = _u32(db, c_pos + 8)
                if magic == b"mhip":
                    if _u32(db, c_pos + 24) == track_id:
                        hits.append((c_pos, tot, plist_type, pp))
                    seen += 1
                c_pos += tot
            pp += mhyp_tot
    return hits


def delete_track(db: bytes, track_id: int) -> bytes:
    """Remove a track's mhit + every mhip that references it.

    Fixes parent counts / total_sizes on: mhlt, mhsd type=1, each affected
    mhyp, mhsd type=2 and =3, and mhbd. Does NOT touch the actual MP3 file
    on the iPod — caller is responsible for that.
    """
    if db[:4] != b"mhbd":
        raise ValueError("not an iTunesDB")

    mhit_off, mhit_size = _find_mhit_offset(db, track_id)
    mhip_hits = _find_mhips_for_track(db, track_id)

    mhsd_locs = _walk_mhsds(db)
    mhsd1_off, mhsd1_hdr, _ = mhsd_locs[1]
    mhlt_off = mhsd1_off + mhsd1_hdr

    all_removals = [(mhit_off, mhit_size)]
    all_removals += [(off, sz) for off, sz, _, _ in mhip_hits]
    all_removals.sort(key=lambda x: x[0], reverse=True)

    out = bytearray(db)
    for off, sz in all_removals:
        del out[off:off + sz]

    def shift(orig: int) -> int:
        return sum(sz for off, sz in all_removals if off < orig)

    def new(orig: int) -> int:
        return orig - shift(orig)

    # mhlt count -= 1
    struct.pack_into("<I", out, new(mhlt_off) + 8,
                     _u32(out, new(mhlt_off) + 8) - 1)
    # mhsd type=1 tot -= mhit size
    struct.pack_into("<I", out, new(mhsd1_off) + 8,
                     _u32(out, new(mhsd1_off) + 8) - mhit_size)

    # mhsd type=2 / type=3: subtract total mhip bytes removed from each
    per_type: dict = {}
    for off, sz, pt, _mhyp in mhip_hits:
        per_type[pt] = per_type.get(pt, 0) + sz
    for pt, removed in per_type.items():
        orig = mhsd_locs[pt][0]
        struct.pack_into("<I", out, new(orig) + 8,
                         _u32(out, new(orig) + 8) - removed)

    # Each affected mhyp: n_mhip and tot
    per_mhyp: dict = {}
    for off, sz, _pt, mhyp_off in mhip_hits:
        per_mhyp.setdefault(mhyp_off, [0, 0])
        per_mhyp[mhyp_off][0] += 1     # count
        per_mhyp[mhyp_off][1] += sz    # bytes
    for orig_mhyp, (cnt, size) in per_mhyp.items():
        struct.pack_into("<I", out, new(orig_mhyp) + 16,
                         _u32(out, new(orig_mhyp) + 16) - cnt)
        struct.pack_into("<I", out, new(orig_mhyp) + 8,
                         _u32(out, new(orig_mhyp) + 8) - size)

    # mhbd tot -= total bytes removed
    delta = sum(sz for _, sz in all_removals)
    struct.pack_into("<I", out, 8, _u32(out, 8) - delta)

    assert len(out) == len(db) - delta
    return bytes(out)


def edit_track_metadata(db: bytes, track_id: int, *,
                        title: str | None = None,
                        artist: str | None = None,
                        album: str | None = None,
                        year: int | None = None) -> bytes:
    """Rewrite mhit's title/artist/album mhods (any field passed as non-None)
    and optionally the year field in the mhit header. Returns new DB bytes.

    Only the 3 string mhods and the year int are touched — the location,
    file_type, and audio properties (size, bitrate, duration) are preserved
    exactly. The mhit's total_size and its parent mhsd type=1's total_size
    plus mhbd's total_size are re-computed to reflect any length change from
    replacing the UTF-16 strings.
    """
    if db[:4] != b"mhbd":
        raise ValueError("not an iTunesDB")

    mhit_off, mhit_tot = _find_mhit_offset(db, track_id)
    mhit_hdr_sz = _u32(db, mhit_off + 4)          # 584 on Touch 1G
    n_mhod      = _u32(db, mhit_off + 12)

    # Rebuild mhod body: for each mhod, either substitute new one or copy verbatim
    new_body = b""
    q = mhit_off + mhit_hdr_sz
    for _ in range(n_mhod):
        if db[q:q+4] != b"mhod":
            raise ValueError(f"expected mhod at 0x{q:x}")
        mhod_tot  = _u32(db, q + 8)
        mhod_type = _u32(db, q + 12)
        if   mhod_type == 1 and title  is not None:
            new_body += _build_string_mhod(1, title)
        elif mhod_type == 4 and artist is not None:
            new_body += _build_string_mhod(4, artist)
        elif mhod_type == 3 and album  is not None:
            new_body += _build_string_mhod(3, album)
        else:
            new_body += db[q:q + mhod_tot]
        q += mhod_tot

    # Rebuild mhit header + patch year + new total_size
    header = bytearray(db[mhit_off:mhit_off + mhit_hdr_sz])
    if year is not None:
        struct.pack_into("<I", header, 52, int(year))
    new_mhit_tot = mhit_hdr_sz + len(new_body)
    struct.pack_into("<I", header, 8, new_mhit_tot)
    new_mhit = bytes(header) + new_body

    delta = len(new_mhit) - mhit_tot
    out = bytearray(db)
    out[mhit_off:mhit_off + mhit_tot] = new_mhit
    if delta:
        # mhsd type=1 tot
        mhsd_locs = _walk_mhsds(db)
        mhsd1_off = mhsd_locs[1][0]
        struct.pack_into("<I", out, mhsd1_off + 8,
                         _u32(out, mhsd1_off + 8) + delta)
        # mhbd tot
        struct.pack_into("<I", out, 8, _u32(out, 8) + delta)

    return bytes(out)
