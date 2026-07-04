"""robly download — sciagnij utwory z iPoda na dysk lokalny.

Uzycie:
  python download.py <folder_lokalny>            # wszystkie utwory
  python download.py <folder> --template "{artist}/{album}/{title}.mp3"
  python download.py <folder> --no-skip-existing # nadpisz istniejace

Backup samej bazy iTunesDB:
  python download.py --db-only iTunesDB.bin

Restore zapisanej bazy:
  python download.py --restore iTunesDB.bin
"""
import sys, os, argparse
sys.stdout.reconfigure(encoding="utf-8")
from robly import find_devices, Device


def main():
    ap = argparse.ArgumentParser(description="Download music + iTunesDB from iPod")
    ap.add_argument("path", nargs="?", help="Local dir for music OR iTunesDB filename")
    ap.add_argument("--template", default="{artist} - {title}.mp3",
                    help="Filename pattern (default: '{artist} - {title}.mp3')")
    ap.add_argument("--no-skip-existing", dest="skip_existing", action="store_false")
    ap.add_argument("--db-only", action="store_true",
                    help="Only download iTunesDB, not music files")
    ap.add_argument("--restore", action="store_true",
                    help="UPLOAD given iTunesDB file BACK to device (overwrite)")
    args = ap.parse_args()

    if not args.path:
        ap.print_help(); sys.exit(1)

    devs = find_devices()
    if not devs:
        print("No iPod detected.")
        sys.exit(1)

    with Device(devs[0]) as dev:
        info = dev.info()
        print(f"Connected: {info.get('DeviceName')} ({info.get('ProductType')}, "
              f"iOS {info.get('ProductVersion')})")

        if args.restore:
            print(f"Restoring iTunesDB from {args.path}...")
            dev.restore_itunesdb(args.path)
            print("Done.")
            return

        if args.db_only:
            print(f"Saving iTunesDB to {args.path}...")
            n = dev.backup_itunesdb(args.path)
            print(f"  {n:,} bytes written.")
            return

        # Download all tracks
        print(f"Downloading to {args.path}/ with template '{args.template}'")
        results = dev.download_music(
            args.path, name_template=args.template,
            skip_existing=args.skip_existing, progress=True,
        )
        ok = sum(1 for r in results if r["status"] == "ok")
        skip = sum(1 for r in results if r["status"] == "skipped")
        err = sum(1 for r in results if r["status"].startswith("error"))
        print(f"\nDone: {ok} downloaded, {skip} skipped, {err} errors out of {len(results)} tracks")


if __name__ == "__main__":
    main()
