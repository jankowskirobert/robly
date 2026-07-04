"""robly upload — pojedyncze pliki lub całe foldery na podłączonego iPoda.

Wymagania:
  - iPod Touch 1G/iOS 1.1.2 podłączony przez USB
  - Apple Mobile Device Service uruchomiony (działa po instalacji iTunes)
  - iPod musi być wcześniej sparowany przez iTunes (pair record w ProgramData)

Uzycie:
  python upload.py <plik.mp3>
  python upload.py <plik.mp3> --title "X" --artist "Y" --album "Z" --year 2024
  python upload.py "C:\\Music\\folder\\*.mp3"
  python upload.py "C:\\Music\\folder"        (cały folder rekursywnie)
"""
import sys, os, glob, argparse
sys.stdout.reconfigure(encoding="utf-8")

from robly import find_devices, Device


def find_mp3s(arg):
    """arg can be: file, folder, or glob pattern."""
    if os.path.isfile(arg):
        return [arg]
    if os.path.isdir(arg):
        results = []
        for root, _, files in os.walk(arg):
            for f in files:
                if f.lower().endswith(".mp3"):
                    results.append(os.path.join(root, f))
        return sorted(results)
    # try glob
    return sorted(glob.glob(arg, recursive=True))


def main():
    ap = argparse.ArgumentParser(description="Upload MP3(s) to iPod Touch 1G")
    ap.add_argument("path", help="MP3 file, folder (recursive), or glob")
    ap.add_argument("--title")
    ap.add_argument("--artist")
    ap.add_argument("--album")
    ap.add_argument("--year", type=int, default=0)
    ap.add_argument("--bitrate", type=int, default=128, help="kbps for duration estimate")
    ap.add_argument("--folder", default="F00", help="iPod folder (F00..F19)")
    args = ap.parse_args()

    files = find_mp3s(args.path)
    if not files:
        print(f"No MP3 files found at: {args.path}")
        sys.exit(1)

    devs = find_devices()
    if not devs:
        print("No iPod detected. Plug it in via USB.")
        sys.exit(1)

    print(f"Found {len(files)} MP3 file(s), uploading to first device...")
    with Device(devs[0]) as dev:
        info = dev.info()
        print(f"Connected: {info.get('DeviceName')} ({info.get('ProductType')}, "
              f"iOS {info.get('ProductVersion')})")
        print(f"  Trust: {dev.lockdown.get_value('TrustedHostAttached')}")
        print()

        # Build batch items (one sync_session for all files - avoids 10053)
        items = []
        for f in files:
            d = {"path": f, "year": args.year,
                 "bitrate_kbps": args.bitrate, "folder": args.folder}
            if len(files) == 1:
                if args.title:  d["title"]  = args.title
                if args.artist: d["artist"] = args.artist
                if args.album:  d["album"]  = args.album
            items.append(d)

        def progress(i, total, name):
            print(f"[{i}/{total}] {name}")

        results = dev.upload_music_batch(items, progress=progress)
        for r in results:
            if r["ok"]:
                print(f"    OK: {r['artist']} - {r['title']} -> {r['on_device']}")
            else:
                print(f"    FAILED {os.path.basename(r['path'])}: {r['error']}")

    print("\nDone. Check Music app on iPod.")


if __name__ == "__main__":
    main()
