# robly

A pure-Python iPod browser and iTunes replacement, written from scratch in 2026.

Supports two device families through one GUI:

| Family | Devices | Transport |
|---|---|---|
| **Touch / iPhone** | iPod Touch 1G, iPhone (iOS 1.x) | usbmuxd → lockdownd → AFC |
| **Classic (mass storage)** | iPod Classic, iPod 4G/5G/Video, Nano 1G-5G | Windows drive letter |

## What it does

- Detects any plugged-in iPod (Touch or Classic) and lets you pick which one to browse
- Reads `iTunesDB` and shows real track metadata (artist / album / title / duration)
- Browses the raw filesystem (Music, playlists, prefs, etc.)
- **Uploads MP3s** with a staging queue: review metadata per file, edit inline, detect duplicates, optionally rewrite ID3 tags on the source files too
- **Edits or deletes tracks already on the device** — right-click any track in the Library tab
- **Downloads music** — single file or the whole library, with cancel button and per-file progress
- **Backs up / restores** the iTunesDB before you experiment
- **Safely ejects** the device (dismounts the volume on Classic, closes lockdownd session on Touch)

## Why it exists

iTunes 7.x from 2007 was the only software that could ever fully talk to an iPod Touch 1G / iOS 1.x. Modern iTunes (12.7+) on Windows 11 still *pairs* with them but refuses to browse or write content. Third-party tools (iFunbox, iMazing, iExplorer) all dropped iOS 1.x years ago. iPod Classic tooling has aged similarly — most projects rely on `libgpod` (unmaintained since 2015).

robly is a from-scratch pure-Python implementation of:

- **usbmuxd** client — enumerates iOS devices and opens TCP tunnels through the Apple Mobile Device Service
- **lockdownd** SSL handshake — the SSL 3.0 handshake that iOS 1.x still uses, worked around via [`nassl`](https://pypi.org/project/nassl/) (which bundles an OpenSSL 1.0.2 binary with SSLv3 compiled in)
- **AFC** client — the "Apple File Conduit" file-transfer protocol used inside the tunnel
- **iTunesDB** parser and writer — reverse-engineered `mhbd` binary format, adds/edits/deletes `mhit` and `mhip` chunks
- **hash58** signature — the HMAC-SHA1 mac in the iTunesDB header that gates whether the iPod actually shows added tracks
- **ID3v1/v2.3/v2.4** reader and writer — pure-Python, no `mutagen` dependency, handles stacked tags left by WMP + retaggers

## Setup

### Requirements

- **Windows 10/11**
- **Python 3.9+** (also tested on 3.14)
- **iTunes installed** — needed for the Apple Mobile Device Service (Windows kernel driver) and, on first plug-in, to create the trust-pair record for iOS devices in `%PROGRAMDATA%\Apple\Lockdown\`. You don't need to *use* iTunes; installing it is enough. Not required at all for iPod Classic — that uses plain USB Mass Storage.
- **iPod paired at least once with iTunes** — for iOS devices only. You'll see the "Trust this computer?" prompt on the device screen. This creates the `.plist` under `%PROGRAMDATA%\Apple\Lockdown\` that robly needs to open a session.

### Install

```
git clone https://github.com/yourname/robly
cd robly
python -m pip install nassl
```

That's it. Everything else robly needs is in the standard library.

`nassl` is the only third-party dependency — it ships a legacy OpenSSL binary so that the SSL 3.0 handshake required by iOS 1.x can succeed on modern Windows. Not needed if you only ever use iPod Classic / Nano / Video, but installing it doesn't hurt.

## Running

```
python app.py
```

The GUI opens on a device picker. Plug in your iPod, click the row that matches, and you're browsing. Two tabs on the main screen:

- **Library** — real iTunesDB-parsed tracks grouped by artist → album → track
- **Filesystem** — raw AFC / drive-letter view of the iPod's storage

Buttons across the top:

| Button | What it does |
|---|---|
| ← Devices | Back to the picker (multi-iPod switching without restart) |
| ⬇ Download | Save the currently selected file to your PC |
| ⬇ Download all | Bulk-download every track from iTunesDB (with per-track progress and Cancel) |
| ⬆ Upload Music | Open the staging queue (pick files → review → send) |
| 💾 Backup DB | Save iTunesDB to a local file — always do this before your first upload |
| ↩ Restore DB | Push a previously backed-up iTunesDB back |
| ⏏ Eject | Cleanly close the session / dismount the volume so the cable can be unplugged |

**Right-click** on any track in the Library tab for `Edit metadata…` and `Delete from iPod`. Double-click also opens the edit dialog.

### Upload queue

Clicking **⬆ Upload Music** and picking files doesn't upload immediately. It opens a staging window:

- **Left column**: list of picked files with a checkbox and status icon
  - `✓` green: has full metadata, ready to send
  - `⚠` yellow: missing title/artist/album — will show as "Unknown" on the iPod if you send anyway
  - `🔁 skip` / `🔁 repl` / `🔁 add`: this song is already on the iPod, current action shown
- **Right panel**: editable title / artist / album / year / genre for the selected file, plus read-only info (bitrate, sample rate, duration, size, path). Duplicate section (with Skip/Replace/Add radio) appears only when relevant.
- **Bottom bar**:
  - `☐ Also write these tags to the MP3 files on disk` — if checked, robly rewrites the ID3v2 tags on the *source* MP3 too, so your local library stays clean
  - `Send →` — starts the actual upload

Metadata resolution when the queue opens:

1. ID3v2 tags in the file (handles stacked tags — WMP-style v2.3 followed by a proper v2.4)
2. ID3v1 tag at end of file, if v2 was empty or junk
3. Filename pattern `Artist - Title.mp3`
4. Fields you edit in the queue window override everything

Values like `Unknown` or pure-digit tags (like `07` — a track-number leaked into the artist field by broken taggers) are auto-detected and treated as missing, so the queue will highlight them for you to fix.

## Project layout

```
robly/
├── app.py                  GUI entry point (Tkinter)
├── robly/                  Library — no GUI, importable in scripts
│   ├── usbmux.py           usbmuxd IPC over the Windows named pipe
│   ├── lockdown.py         lockdownd handshake + StartSession
│   ├── nassl_wrap.py       SSL 3.0 socket wrapper via nassl
│   ├── afc.py              AFC file-transfer protocol
│   ├── notification_proxy.py   sync-mode notifications
│   ├── device.py           Device class (Touch / iOS)
│   ├── classic.py          ClassicDevice class (mass storage)
│   ├── itunesdb.py         iTunesDB parser
│   ├── itunesdb_writer.py  Adds / edits / deletes tracks in iTunesDB
│   ├── itunesdb_hash.py    hash58 / hash72 signatures
│   ├── mp3_metadata.py     ID3v1 + ID3v2 reader / writer, MP3 frame decoder
│   └── backup.py           mobilebackup protocol
├── scripts/                Reverse-engineering artifacts, tests, dumps
├── docs/                   Static HTML docs site
├── DOCUMENTATION.md        Long-form architecture notes (Polish)
└── README.md               This file
```

## Library usage (no GUI)

```python
from robly import find_all_devices, connect

for d in find_all_devices():
    print(d["type"], d.get("name"), d.get("serial"))

# Open the first one
with connect(find_all_devices()[0]) as dev:
    print(dev.info())

    # Read library
    db = dev.read_itunesdb()
    from robly import iTunesDB
    for t in iTunesDB(db).tracks[:10]:
        print(t.id, t.artist, "—", t.title)

    # Upload with ID3 auto-detection
    dev.upload_music("C:/Music/Coma - Przesilenie.mp3")

    # Bulk upload with duplicate replacement
    dev.upload_music_batch(
        [{"path": "song1.mp3"}, {"path": "song2.mp3"}],
        pre_delete_ids=[27353],   # replace track 27353 in the same sync
    )

    # Edit metadata on a track already there
    dev.edit_track(27353, artist="Coma", album="Hipertrofia CD 1")

    # Delete
    dev.delete_track(27353)

    # Safe eject
    dev.eject()
```

## Troubleshooting

**iPod Classic doesn't mount as a drive**
Force Disk Mode: toggle Hold off, hold Menu + Select (center) for ~8s until the Apple logo appears, then immediately hold Select + Play until you see "Disk Mode". Windows should assign a drive letter within a few seconds. If not, open Disk Management and add a letter manually.

**iOS device shows in Device Manager but robly says "no pair record"**
Open iTunes once — accept the "Trust this computer?" prompt on the iPod. This creates `%PROGRAMDATA%\Apple\Lockdown\<UDID>.plist` which robly reads.

**Upload succeeds but track shows as "Unknown Artist" on iPod**
Your MP3 has broken tags. Open the upload queue, edit metadata in the Details panel, and check the "Also write these tags to the MP3 files on disk" box — robly will fix your source files while uploading.

**`WinError 10053` (WSAECONNABORTED) during upload**
The lockdownd SSL session timed out. robly auto-heals: it detects the dead connection and reconnects before your next operation. If it persists, unplug and re-plug the iPod.

**Upload adds the track but iPod UI still shows old library**
The hash58 signature didn't match. Restart the iPod (Menu + center for 8s). If the problem repeats, back up your iTunesDB before more uploads so you can restore.

## Status

| Feature | Touch 1G | Classic (mass storage) |
|---|---|---|
| Enumerate + info | ✅ | ✅ |
| Browse filesystem | ✅ | ✅ |
| Read iTunesDB | ✅ | ✅ |
| Download tracks | ✅ | ✅ |
| Backup DB | ✅ | ✅ |
| Restore DB | ✅ | ✅ |
| Upload MP3 + iTunesDB entry | ✅ (hash58) | ✅ (no signature) |
| Edit track metadata | ✅ | ✅ |
| Delete track | ✅ | ✅ |
| Duplicate detection | ✅ | ✅ |
| Safe eject | ✅ (session close) | ✅ (volume dismount) |
| Touch 2G+ / hash72 | ❌ (not implemented) | — |
| Nano 6G / hashAB | — | ❌ (not implemented) |

## License

MIT. Do whatever you want. Just don't expect Apple to bless it.
