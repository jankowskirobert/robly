"""
robly GUI — iPod Touch 1G music browser/uploader.

  · Auto-detects the device via usbmuxd
  · Opens a lockdownd SSL session via nassl
  · Browses /iTunes_Control/Music via AFC
  · Parses iTunesDB (if present) for real track metadata
  · Lets you download tracks and upload arbitrary files to the music partition
"""
from __future__ import annotations
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import PurePosixPath

# Make sure print() with emoji doesn't crash on Windows cp1252
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from robly import (find_devices, Device, ClassicDevice, find_all_devices,
                   connect as robly_connect, AFCError, iTunesDB, Track,
                   resolve_metadata, has_missing_metadata)


# ── Theme ────────────────────────────────────────────────────────────────────
BG       = "#1e1e2e"
PANEL    = "#181825"
ROW_HI   = "#45475a"
TOP_BG   = "#313244"
FG       = "#cdd6f4"
DIM      = "#a6adc8"
ACCENT   = "#cba6f7"
LINK     = "#89b4fa"
GREEN    = "#a6e3a1"
RED      = "#f38ba8"

ICON_DIR  = "📁"
ICON_FILE = "🎵"


def _sanitize_local(name: str) -> str:
    """Windows-safe filename (replaces invalid characters, keeps spaces)."""
    import re
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "download"


def _fmt_size(b) -> str:
    try: b = int(b)
    except: return "?"
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024: return f"{b} {u}"
        b //= 1024
    return f"{b} TB"


def _fmt_duration_ms(ms) -> str:
    try: ms = int(ms)
    except: return ""
    s = ms // 1000
    return f"{s//60}:{s%60:02d}"


class RoblyApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("robly — iPod music")
        self.geometry("1100x680")
        self.configure(bg=BG)

        self._device = None
        self._db:     iTunesDB | None = None
        self._poll_stop = threading.Event()
        self._picker_frame = None
        self._main_frame = None
        self._download_cancel = threading.Event()  # set to abort in-flight Download All

        # Build device picker first — we'll build the main UI after user picks
        self._build_picker()
        threading.Thread(target=self._picker_poll_loop, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Device picker (startup screen) ──────────────────────────────────────
    def _build_picker(self):
        """Show a big 'pick your iPod' screen before the main UI."""
        if self._picker_frame is not None:
            self._picker_frame.destroy()
        f = tk.Frame(self, bg=BG)
        f.pack(fill=tk.BOTH, expand=True)
        self._picker_frame = f

        # Header
        header = tk.Frame(f, bg=BG); header.pack(fill=tk.X, pady=(40, 20))
        tk.Label(header, text="robly", fg=ACCENT, bg=BG,
                 font=("Consolas", 32, "bold")).pack()
        tk.Label(header, text="Pick a device to browse",
                 fg=DIM, bg=BG, font=("Consolas", 11)).pack(pady=(4, 0))

        # Device list frame (populated by _refresh_picker)
        self._picker_list = tk.Frame(f, bg=BG)
        self._picker_list.pack(pady=(20, 20), padx=100, fill=tk.X)

        # Footer
        footer = tk.Frame(f, bg=BG); footer.pack(pady=20)
        self._picker_status = tk.Label(
            footer, text="", fg=DIM, bg=BG, font=("Consolas", 9))
        self._picker_status.pack()
        tk.Button(footer, text="↺ Refresh", command=self._refresh_picker,
                  bg=ROW_HI, fg=FG, relief=tk.FLAT, font=("Consolas", 10),
                  padx=14, pady=6, cursor="hand2").pack(pady=(10, 0))

        self._refresh_picker()

    def _refresh_picker(self):
        for w in self._picker_list.winfo_children():
            w.destroy()
        try:
            devices = find_all_devices()
        except Exception as e:
            devices = []
            self._picker_status.config(text=f"Scan error: {e}")

        if not devices:
            self._picker_status.config(text=(
                "  No iPods detected.\n\n"
                "  Plug one in via USB.  Touch/iPhone → shows up over usbmuxd;\n"
                "  Classic / Nano / Video → mounts as a drive letter (E:, F:, …)."
            ))
            return
        self._picker_status.config(text=f"  Found {len(devices)} device(s)")

        for d in devices:
            self._picker_add_row(d)

    def _picker_add_row(self, descriptor: dict):
        row = tk.Frame(self._picker_list, bg=PANEL, cursor="hand2")
        row.pack(fill=tk.X, pady=6, ipady=10)

        icon = "🎧" if descriptor["type"] == "touch" else "💿"
        name = descriptor.get("name", "iPod")
        tk.Label(row, text=f"  {icon}  ", fg=ACCENT, bg=PANEL,
                 font=("Consolas", 20)).pack(side=tk.LEFT)
        info = tk.Frame(row, bg=PANEL); info.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(info, text=name, fg=FG, bg=PANEL,
                 font=("Consolas", 12, "bold"),
                 anchor=tk.W).pack(fill=tk.X)
        subtitle = self._picker_subtitle(descriptor)
        tk.Label(info, text=subtitle, fg=DIM, bg=PANEL,
                 font=("Consolas", 9), anchor=tk.W).pack(fill=tk.X)

        tk.Label(row, text="  Open →  ", fg=LINK, bg=PANEL,
                 font=("Consolas", 11, "bold")).pack(side=tk.RIGHT)

        for w in (row,) + tuple(row.winfo_children()) + tuple(info.winfo_children()):
            w.bind("<Button-1>", lambda _e, d=descriptor: self._pick_device(d))
            # Hover effect
            w.bind("<Enter>", lambda _e, r=row: r.configure(bg=ROW_HI))
            w.bind("<Leave>", lambda _e, r=row: r.configure(bg=PANEL))

    def _picker_subtitle(self, d: dict) -> str:
        if d["type"] == "touch":
            uid = d.get("serial", "")
            return f"    iOS device via usbmuxd  ·  UDID {uid[:16]}…"
        m = d.get("model", "?")
        f = d.get("family", "?")
        mount = d.get("mount", "")
        return f"    Mass storage  ·  {mount}  ·  Family {f}  ·  Model {m}"

    def _pick_device(self, descriptor: dict):
        """User clicked a device — connect and switch to the main UI."""
        self._picker_status.config(text=f"  Connecting to {descriptor.get('name')}…")
        self.update_idletasks()
        try:
            dev = robly_connect(descriptor)
        except Exception as e:
            messagebox.showerror("Connect failed", str(e))
            self._picker_status.config(text=f"  Failed: {e}")
            return
        self._device = dev

        # Tear down picker, build main UI
        self._picker_frame.destroy(); self._picker_frame = None
        self._build_ui()
        self._on_connected()

    def _picker_poll_loop(self):
        """While the picker is up, re-scan devices every 3s so new plug-ins
        appear without the user having to click Refresh."""
        while not self._poll_stop.is_set() and self._picker_frame is not None:
            try: self.after(0, self._refresh_picker)
            except Exception: break
            self._poll_stop.wait(3)

    def _go_back_to_picker(self):
        """Disconnect current device and return to picker."""
        if self._device:
            try: self._device.close()
            except Exception: pass
            self._device = None
            self._db = None
        if self._main_frame is not None:
            self._main_frame.destroy(); self._main_frame = None
        self._build_picker()
        threading.Thread(target=self._picker_poll_loop, daemon=True).start()

    # ── Main UI ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Wrap the whole main UI in a Frame so _go_back_to_picker can nuke it
        self._main_frame = tk.Frame(self, bg=BG)
        self._main_frame.pack(fill=tk.BOTH, expand=True)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background=PANEL, foreground=FG,
                        fieldbackground=PANEL, rowheight=24,
                        font=("Consolas", 10))
        style.configure("Treeview.Heading", background=TOP_BG, foreground=ACCENT,
                        font=("Consolas", 10, "bold"))
        style.map("Treeview", background=[("selected", ROW_HI)])
        # Notebook (tab bar) styling matched to the dark theme
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=DIM,
                        padding=(20, 8), font=("Consolas", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", ROW_HI)],
                  foreground=[("selected", ACCENT)])

        # Top bar
        top = tk.Frame(self._main_frame, bg=TOP_BG, pady=6); top.pack(fill=tk.X)
        tk.Button(top, text="← Devices", command=self._go_back_to_picker,
                  bg=TOP_BG, fg=DIM, relief=tk.FLAT, font=("Consolas", 9),
                  padx=8, pady=2, cursor="hand2",
                  activebackground=ROW_HI, activeforeground=FG).pack(side=tk.LEFT, padx=(8, 4))
        tk.Button(top, text="⏏ Eject", command=self._eject_device,
                  bg=TOP_BG, fg=GREEN, relief=tk.FLAT, font=("Consolas", 9, "bold"),
                  padx=8, pady=2, cursor="hand2",
                  activebackground=ROW_HI, activeforeground=FG).pack(side=tk.LEFT)
        self.dot = tk.Label(top, text="●", fg=RED, bg=TOP_BG, font=("Consolas", 14))
        self.dot.pack(side=tk.LEFT, padx=(8, 4))
        self.status = tk.Label(top, text="Waiting for device…",
                               fg=FG, bg=TOP_BG, font=("Consolas", 10))
        self.status.pack(side=tk.LEFT)

        btn_kwargs = {"bg": ROW_HI, "fg": FG, "relief": tk.FLAT,
                      "font": ("Consolas", 9), "padx": 10, "pady": 4,
                      "cursor": "hand2", "activebackground": "#585b70",
                      "activeforeground": FG}
        # Right-to-left: rightmost button packs first
        tk.Button(top, text="⬆ Upload Music",  command=self._upload_music, **btn_kwargs).pack(side=tk.RIGHT, padx=(6, 12))
        tk.Button(top, text="⬇ Download All",  command=self._download_all, **btn_kwargs).pack(side=tk.RIGHT)
        tk.Button(top, text="⬇ Download",      command=self._download,     **btn_kwargs).pack(side=tk.RIGHT, padx=6)
        tk.Button(top, text="⏮ Restore DB",    command=self._restore_db,   **btn_kwargs).pack(side=tk.RIGHT)
        tk.Button(top, text="💾 Backup DB",    command=self._backup_db,    **btn_kwargs).pack(side=tk.RIGHT, padx=6)
        tk.Button(top, text="↺ Refresh",       command=self._refresh,      **btn_kwargs).pack(side=tk.RIGHT)

        # Context bar (shows what the active tab is looking at)
        pb = tk.Frame(self._main_frame, bg=PANEL, pady=4); pb.pack(fill=tk.X)
        self.context_var = tk.StringVar(value=" Connect an iPod to browse its library")
        tk.Label(pb, textvariable=self.context_var, fg=LINK,
                 bg=PANEL, font=("Consolas", 9, "bold")).pack(side=tk.LEFT)

        # Main pane: Notebook (Library / Filesystem) | Details
        pane = tk.PanedWindow(self._main_frame, orient=tk.HORIZONTAL, bg=BG,
                              sashwidth=4, sashrelief=tk.FLAT)
        pane.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.notebook = ttk.Notebook(pane)

        # ── Library tab (iTunesDB, grouped by artist/album) ─────────────
        lib_frame = tk.Frame(self.notebook, bg=PANEL)
        self.lib_tree = ttk.Treeview(
            lib_frame, show="tree headings",
            columns=("artist", "album", "duration", "size"),
            selectmode="extended",
        )
        self.lib_tree.heading("#0",       text="Title")
        self.lib_tree.heading("artist",   text="Artist")
        self.lib_tree.heading("album",    text="Album")
        self.lib_tree.heading("duration", text="Time", anchor=tk.E)
        self.lib_tree.heading("size",     text="Size", anchor=tk.E)
        self.lib_tree.column("#0",       width=280, minwidth=140)
        self.lib_tree.column("artist",   width=180, minwidth=100)
        self.lib_tree.column("album",    width=180, minwidth=100)
        self.lib_tree.column("duration", width=70,  anchor=tk.E)
        self.lib_tree.column("size",     width=80,  anchor=tk.E)
        lib_vsb = ttk.Scrollbar(lib_frame, orient=tk.VERTICAL, command=self.lib_tree.yview)
        self.lib_tree.configure(yscrollcommand=lib_vsb.set)
        lib_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.lib_tree.pack(fill=tk.BOTH, expand=True)
        self.lib_tree.bind("<<TreeviewSelect>>", self._on_lib_select)
        self.lib_tree.bind("<Button-3>", self._on_lib_right_click)
        self.lib_tree.bind("<Double-Button-1>", lambda e: self._edit_selected_tracks())

        # ── Filesystem tab (raw AFC browser) ────────────────────────────
        fs_frame = tk.Frame(self.notebook, bg=PANEL)
        self.fs_tree = ttk.Treeview(
            fs_frame, show="tree headings",
            columns=("size", "kind"),
            selectmode="browse",
        )
        self.fs_tree.heading("#0",   text="Name")
        self.fs_tree.heading("size", text="Size")
        self.fs_tree.heading("kind", text="Kind")
        self.fs_tree.column("#0",   width=400, minwidth=200)
        self.fs_tree.column("size", width=90,  anchor=tk.E)
        self.fs_tree.column("kind", width=60,  anchor=tk.CENTER)
        fs_vsb = ttk.Scrollbar(fs_frame, orient=tk.VERTICAL, command=self.fs_tree.yview)
        self.fs_tree.configure(yscrollcommand=fs_vsb.set)
        fs_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.fs_tree.pack(fill=tk.BOTH, expand=True)
        self.fs_tree.bind("<<TreeviewOpen>>",   self._on_fs_expand)
        self.fs_tree.bind("<<TreeviewSelect>>", self._on_fs_select)

        # Library first = default active tab
        self.notebook.add(lib_frame, text="  🎵 Library  ")
        self.notebook.add(fs_frame,  text="  📂 Filesystem  ")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        pane.add(self.notebook, minsize=460)

        # Details pane (shared, right-hand side)
        df = tk.Frame(pane, bg=PANEL, padx=12, pady=12)
        tk.Label(df, text="Details", fg=ACCENT, bg=PANEL,
                 font=("Consolas", 11, "bold")).pack(anchor=tk.W)
        self.detail = tk.Text(df, bg=PANEL, fg=FG,
                              font=("Consolas", 9), relief=tk.FLAT,
                              state=tk.DISABLED, wrap=tk.WORD)
        self.detail.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        pane.add(df, minsize=240)

    # ── Device lifecycle ────────────────────────────────────────────────────
    def _on_connected(self):
        info = {}
        try: info = self._device.info()
        except Exception: pass
        name = info.get("DeviceName") or self._device.udid[:8]
        ver  = info.get("ProductVersion") or "?"
        self._set_status(f"Connected — {name} (iOS {ver})", True)

        # Try to load iTunesDB if present
        try:
            db_bytes = self._device.read_itunesdb()
            self._db = iTunesDB(db_bytes)
        except Exception as e:
            self._db = None
            print(f"(no iTunesDB: {e})")

        self._refresh()

    def _set_status(self, text: str, connected: bool):
        self.dot.config(fg=GREEN if connected else RED)
        self.status.config(text=text)

    # ── Tab switching / context bar ─────────────────────────────────────────
    def _current_tab(self) -> str:
        """Return 'library' or 'filesystem' based on the selected notebook tab."""
        try:
            idx = self.notebook.index(self.notebook.select())
            return "library" if idx == 0 else "filesystem"
        except tk.TclError:
            return "library"

    def _on_tab_changed(self, _e=None):
        self._set_detail("")
        self._update_context_bar()

    def _update_context_bar(self):
        if self._device is None:
            self.context_var.set(" Connect an iPod to browse its library")
            return
        tab = self._current_tab()
        if tab == "library":
            if self._db and self._db.tracks:
                artists = len({t.artist for t in self._db.tracks if t.artist})
                self.context_var.set(
                    f" 🎵 iTunesDB — {len(self._db.tracks)} tracks by {artists} artists")
            else:
                self.context_var.set(" 🎵 iTunesDB — no tracks (or database not readable)")
        else:
            self.context_var.set(f" 📂 {self._device.music_root}")

    # ── Refresh (populates both tabs) ───────────────────────────────────────
    def _refresh(self):
        self._set_detail("")
        self.lib_tree.delete(*self.lib_tree.get_children())
        self.fs_tree.delete(*self.fs_tree.get_children())
        self._update_context_bar()

        if self._device is None:
            return

        # Populate Library tab (grouped: artist → album → track)
        self._lib_iid_to_track = {}  # tree iid -> Track
        if not self._db or not self._db.tracks:
            self.lib_tree.insert("", tk.END,
                                 text="⚠ No iTunesDB on device (or empty)",
                                 values=("", "", "", ""))
        else:
            grouped = {}
            for t in self._db.tracks:
                a  = t.artist or "(Unknown Artist)"
                al = t.album  or "(Unknown Album)"
                grouped.setdefault(a, {}).setdefault(al, []).append(t)
            for artist in sorted(grouped):
                a_iid = self.lib_tree.insert("", tk.END,
                                             text=f"👤 {artist}",
                                             values=("", "", "", ""),
                                             open=False)
                for album in sorted(grouped[artist]):
                    al_iid = self.lib_tree.insert(a_iid, tk.END,
                                                  text=f"💿 {album}",
                                                  values=("", "", "", ""),
                                                  open=False)
                    for t in grouped[artist][album]:
                        iid = self.lib_tree.insert(
                            al_iid, tk.END,
                            text=f"{ICON_FILE} {t.title or '(untitled)'}",
                            values=(t.artist or "", t.album or "",
                                    _fmt_duration_ms(t.duration),
                                    _fmt_size(t.file_size)),
                            tags=(t.afc_path,))
                        self._lib_iid_to_track[iid] = t

        # Populate Filesystem tab (device-appropriate root, lazy children)
        music_root = self._device.music_root  # /iTunes_Control/Music or /iPod_Control/Music
        root = self.fs_tree.insert(
            "", tk.END, iid="__root__",
            text=f"{ICON_DIR} {music_root}",
            values=("", "dir"),
            tags=(music_root,), open=True,
        )
        self._load_dir(music_root, root)

    # ── Filesystem tab (raw AFC) ────────────────────────────────────────────
    def _load_dir(self, path: str, parent_iid: str):
        afc = self._device.afc
        def task():
            try:
                names = afc.listdir(path)
                entries = []
                for n in names:
                    full = path.rstrip("/") + "/" + n
                    try: info = afc.stat(full)
                    except Exception: info = {}
                    is_dir = info.get("st_ifmt") == "S_IFDIR"
                    size   = int(info.get("st_size", 0))
                    entries.append({"name": n, "path": full,
                                    "is_dir": is_dir, "size": size})
                entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
                self.after(0, self._insert_entries, parent_iid, entries)
            except Exception as e:
                self.after(0, self._insert_error, parent_iid, str(e))
        threading.Thread(target=task, daemon=True).start()

    def _insert_entries(self, parent_iid, entries):
        for c in self.fs_tree.get_children(parent_iid):
            if self.fs_tree.item(c, "values") == ("…",):
                self.fs_tree.delete(c)
        for e in entries:
            icon = ICON_DIR if e["is_dir"] else ICON_FILE
            size_str = "" if e["is_dir"] else _fmt_size(e["size"])
            kind = "dir" if e["is_dir"] else "file"
            iid = self.fs_tree.insert(parent_iid, tk.END,
                                      text=f"{icon} {e['name']}",
                                      values=(size_str, kind),
                                      tags=(e["path"],))
            if e["is_dir"]:
                self.fs_tree.insert(iid, tk.END, values=("…", ""))

    def _insert_error(self, parent_iid, msg):
        self.fs_tree.insert(parent_iid, tk.END,
                            text=f"⚠ {msg}", values=("", "error"))

    def _on_fs_expand(self, _e):
        iid = self.fs_tree.focus()
        children = self.fs_tree.get_children(iid)
        if len(children) == 1 and self.fs_tree.item(children[0], "values")[0] == "…":
            tags = self.fs_tree.item(iid, "tags")
            if tags:
                self._load_dir(tags[0], iid)

    def _on_fs_select(self, _e):
        iid = self.fs_tree.focus()
        if not iid: return
        tags = self.fs_tree.item(iid, "tags")
        path = tags[0] if tags else ""
        if path:
            self.context_var.set(f" 📂 {path}")
            self._show_detail(path)

    # ── Library tab select ──────────────────────────────────────────────────
    def _on_lib_select(self, _e):
        iid = self.lib_tree.focus()
        if not iid: return
        tags = self.lib_tree.item(iid, "tags")
        path = tags[0] if tags else ""
        if path:
            self._show_detail(path)

    def _show_detail(self, path: str):
        # If we have a track for this path, show its metadata
        if self._db:
            for t in self._db.tracks:
                if t.afc_path == path:
                    lines = [f"Title:    {t.title}",
                             f"Artist:   {t.artist}",
                             f"Album:    {t.album}",
                             f"Genre:    {t.genre}",
                             f"Duration: {_fmt_duration_ms(t.duration)}",
                             f"Size:     {_fmt_size(t.file_size)}",
                             f"Path:     {path}"]
                    self._set_detail("\n".join(lines))
                    return
        if not self._device: return
        def task():
            try:
                info = self._device.afc.stat(path)
                lines = [f"Path:  {path}", ""]
                for k, v in sorted(info.items()):
                    lines.append(f"{k:<24} {v}")
                self.after(0, self._set_detail, "\n".join(lines))
            except Exception as e:
                self.after(0, self._set_detail, f"Error: {e}")
        threading.Thread(target=task, daemon=True).start()

    def _set_detail(self, text: str):
        self.detail.config(state=tk.NORMAL)
        self.detail.delete("1.0", tk.END)
        self.detail.insert(tk.END, text)
        self.detail.config(state=tk.DISABLED)

    # ── Toolbar actions ─────────────────────────────────────────────────────
    def _active_tree(self):
        """Return (tree, item_type_for_focused_row)."""
        return self.lib_tree if self._current_tab() == "library" else self.fs_tree

    def _selected_path(self) -> str | None:
        tree = self._active_tree()
        iid = tree.focus()
        if not iid: return None
        tags = tree.item(iid, "tags")
        return tags[0] if tags else None

    def _selected_track_title(self) -> str | None:
        """If a track is selected in the Library tab, return a display name for it."""
        if self._current_tab() != "library": return None
        iid = self.lib_tree.focus()
        if not iid: return None
        text = self.lib_tree.item(iid, "text")
        return text.replace(f"{ICON_FILE} ", "") if text else None

    def _download(self):
        if not self._device:
            messagebox.showinfo("Download", "No device connected."); return
        path = self._selected_path()
        if not path:
            messagebox.showinfo("Download",
                                "Select a track (Library tab) or file (Filesystem tab).")
            return
        # Suggest a nice filename: track title if from Library, else basename
        default_name = None
        if self._current_tab() == "library":
            title = self._selected_track_title()
            if title:
                default_name = _sanitize_local(title) + os.path.splitext(path)[1]
        default_name = default_name or PurePosixPath(path).name

        dest = filedialog.asksaveasfilename(initialfile=default_name)
        if not dest: return

        def task():
            try:
                data = self._device.afc.read_file(path)
                with open(dest, "wb") as f: f.write(data)
                self.after(0, messagebox.showinfo, "Done", f"Saved to:\n{dest}")
            except Exception as e:
                self.after(0, messagebox.showerror, "Download failed", str(e))
        threading.Thread(target=task, daemon=True).start()

    def _upload_music(self):
        """Pick files, then open the staging queue window."""
        if not self._device:
            messagebox.showinfo("Upload Music", "No device connected."); return
        srcs = filedialog.askopenfilenames(
            title="Upload music to iPod",
            filetypes=[("MP3 files", "*.mp3"), ("All files", "*.*")])
        if not srcs: return
        self._open_upload_queue(list(srcs))

    # ─── Upload staging queue ───────────────────────────────────────────────
    def _open_upload_queue(self, files: list):
        """Modal window: user reviews / edits per-file metadata, chooses which
        files to include, optionally writes tags back to disk, then Sends."""
        from robly import write_id3v2

        # Pre-scan metadata + check for duplicates already on the iPod.
        # A track counts as a duplicate if artist+title (case-insensitive)
        # matches an existing one. Album is not used because the same song
        # can legitimately live on multiple albums (e.g. singles vs LP).
        def _dup_key(artist: str, title: str) -> tuple:
            return ((artist or "").strip().lower(),
                    (title  or "").strip().lower())

        existing_by_key = {}
        if self._db and self._db.tracks:
            for t in self._db.tracks:
                k = _dup_key(t.artist, t.title)
                if k[1]:  # ignore titleless entries
                    existing_by_key.setdefault(k, t)

        queue = []
        for src in files:
            meta = resolve_metadata(src)
            dup = existing_by_key.get(_dup_key(meta.get("artist"),
                                              meta.get("title")))
            queue.append({
                "path": src, "meta": meta, "included": True,
                "dup": dup,             # existing Track or None
                "dup_action": "skip" if dup else None,
            })

        win = tk.Toplevel(self)
        win.title(f"Upload queue — {len(files)} file(s)")
        win.configure(bg=BG)
        win.geometry("1080x640")
        win.transient(self)

        body = tk.Frame(win, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=14, pady=(14, 0))

        # ── LEFT: file list ──────────────────────────────────────────────
        left = tk.Frame(body, bg=BG, width=440)
        left.pack(side=tk.LEFT, fill=tk.BOTH)
        left.pack_propagate(False)

        tk.Label(left, text="Files to upload", fg=FG, bg=BG,
                 font=("Consolas", 10, "bold")).pack(anchor=tk.W, padx=4)
        counter_var = tk.StringVar()
        tk.Label(left, textvariable=counter_var, fg=DIM, bg=BG,
                 font=("Consolas", 8)).pack(anchor=tk.W, padx=4, pady=(0, 6))

        tree_frame = tk.Frame(left, bg=BG)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        tree = ttk.Treeview(tree_frame, columns=("chk", "st"),
                            show="tree headings", selectmode="browse")
        tree.column("#0", width=320, anchor=tk.W)
        tree.column("chk", width=40, anchor=tk.CENTER, stretch=False)
        tree.column("st",  width=70, anchor=tk.CENTER, stretch=False)
        tree.heading("#0", text="File")
        tree.heading("chk", text="")
        tree.heading("st",  text="")
        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        tree.tag_configure("ok",       foreground=GREEN)
        tree.tag_configure("warn",     foreground="#f9e2af")
        tree.tag_configure("skipped",  foreground=DIM)
        tree.tag_configure("dup_skip", foreground=DIM)
        tree.tag_configure("dup_repl", foreground="#f9e2af")
        tree.tag_configure("dup_add",  foreground=LINK)

        def _row_state(it):
            if not it["included"]:
                return "☐", "—", "skipped"
            if it["dup"]:
                if it["dup_action"] == "skip":
                    return "☑", "🔁 skip", "dup_skip"
                if it["dup_action"] == "replace":
                    return "☑", "🔁 repl", "dup_repl"
                if it["dup_action"] == "add":
                    return "☑", "🔁 add",  "dup_add"
            if has_missing_metadata(it["meta"]):
                return "☑", "⚠", "warn"
            return "☑", "✓", "ok"

        def _refresh_row(idx: int):
            chk, st, tag = _row_state(queue[idx])
            tree.item(str(idx), text=os.path.basename(queue[idx]["path"]),
                      values=(chk, st), tags=(tag,))

        def _refresh_counter():
            inc  = sum(1 for it in queue if it["included"])
            miss = sum(1 for it in queue
                       if it["included"] and has_missing_metadata(it["meta"]))
            dups = sum(1 for it in queue if it["dup"])
            s = f"{inc} of {len(queue)} selected"
            if miss: s += f"  ·  {miss} with missing metadata"
            if dups: s += f"  ·  {dups} duplicate(s)"
            counter_var.set(s)

        for idx in range(len(queue)):
            tree.insert("", "end", iid=str(idx),
                        text=os.path.basename(queue[idx]["path"]),
                        values=("☑", "✓"))
            _refresh_row(idx)
        _refresh_counter()

        small_btn = dict(bg=ROW_HI, fg=FG, activebackground=TOP_BG,
                         activeforeground=FG, relief=tk.FLAT,
                         font=("Consolas", 8), padx=8, pady=3,
                         borderwidth=0, cursor="hand2")
        left_btns = tk.Frame(left, bg=BG)
        left_btns.pack(fill=tk.X, pady=(6, 0))

        def _all_or_none(inc: bool):
            for it in queue: it["included"] = inc
            for i in range(len(queue)): _refresh_row(i)
            _refresh_counter()
        tk.Button(left_btns, text="Select all",
                  command=lambda: _all_or_none(True), **small_btn
                  ).pack(side=tk.LEFT)
        tk.Button(left_btns, text="Select none",
                  command=lambda: _all_or_none(False), **small_btn
                  ).pack(side=tk.LEFT, padx=(4, 0))

        # ── RIGHT: details panel ─────────────────────────────────────────
        right = tk.Frame(body, bg=PANEL)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))

        tk.Label(right, text="Details", fg=FG, bg=PANEL,
                 font=("Consolas", 10, "bold")
                 ).pack(anchor=tk.W, padx=16, pady=(14, 0))
        name_var = tk.StringVar()
        tk.Label(right, textvariable=name_var, fg=ACCENT, bg=PANEL,
                 font=("Consolas", 10), wraplength=560, justify=tk.LEFT,
                 anchor=tk.W).pack(anchor=tk.W, padx=16, pady=(2, 12), fill=tk.X)

        fields: dict = {}
        for label, key in [("Title:", "title"), ("Artist:", "artist"),
                           ("Album:", "album"), ("Year:", "year"),
                           ("Genre:", "genre")]:
            row = tk.Frame(right, bg=PANEL)
            row.pack(fill=tk.X, padx=16, pady=3)
            tk.Label(row, text=label, fg=FG, bg=PANEL,
                     font=("Consolas", 10), width=8, anchor=tk.W
                     ).pack(side=tk.LEFT)
            var = tk.StringVar()
            tk.Entry(row, textvariable=var, bg=BG, fg=FG,
                     insertbackground=FG, font=("Consolas", 10),
                     relief=tk.FLAT
                     ).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
            fields[key] = var

        info_var = tk.StringVar(value="")
        tk.Label(right, textvariable=info_var, fg=DIM, bg=PANEL,
                 font=("Consolas", 9), justify=tk.LEFT, anchor=tk.W,
                 wraplength=560
                 ).pack(anchor=tk.W, padx=16, pady=(14, 0), fill=tk.X)

        # ── Duplicate handling (shown only when current file is a dup) ──
        dup_frame = tk.Frame(right, bg="#3d2b3f")  # subtle wine-ish bg
        dup_msg_var = tk.StringVar()
        tk.Label(dup_frame, textvariable=dup_msg_var,
                 fg="#f9e2af", bg="#3d2b3f", font=("Consolas", 9),
                 wraplength=540, justify=tk.LEFT, anchor=tk.W
                 ).pack(anchor=tk.W, padx=12, pady=(8, 4), fill=tk.X)
        dup_action_var = tk.StringVar(value="skip")

        def _on_dup_action(*a):
            it = queue[current_idx[0]]
            if it["dup"]:
                it["dup_action"] = dup_action_var.get()
                _refresh_row(current_idx[0])
                _refresh_counter()

        dup_radio_row = tk.Frame(dup_frame, bg="#3d2b3f")
        dup_radio_row.pack(anchor=tk.W, padx=12, pady=(0, 4))
        for label, val in [("Skip", "skip"),
                           ("Replace existing", "replace"),
                           ("Add as new anyway", "add")]:
            tk.Radiobutton(
                dup_radio_row, text=label, value=val, variable=dup_action_var,
                bg="#3d2b3f", fg=FG, selectcolor=BG,
                activebackground="#3d2b3f", activeforeground=FG,
                font=("Consolas", 9), borderwidth=0,
                command=_on_dup_action,
            ).pack(side=tk.LEFT, padx=(0, 8))

        # Quick "apply to all duplicates" row
        dup_bulk_row = tk.Frame(dup_frame, bg="#3d2b3f")
        dup_bulk_row.pack(anchor=tk.W, padx=12, pady=(0, 8))
        def _apply_to_all_dups(action: str):
            for it in queue:
                if it["dup"]: it["dup_action"] = action
            for i in range(len(queue)): _refresh_row(i)
            _refresh_counter()
            dup_action_var.set(action)
        bulk_kw = dict(bg=ROW_HI, fg=FG, activebackground=TOP_BG,
                       activeforeground=FG, relief=tk.FLAT,
                       font=("Consolas", 8), padx=6, pady=2,
                       borderwidth=0, cursor="hand2")
        tk.Label(dup_bulk_row, text="All dups:", fg=DIM, bg="#3d2b3f",
                 font=("Consolas", 8)).pack(side=tk.LEFT, padx=(0, 6))
        for label, act in [("Skip all", "skip"),
                           ("Replace all", "replace"),
                           ("Add all", "add")]:
            tk.Button(dup_bulk_row, text=label,
                      command=lambda a=act: _apply_to_all_dups(a), **bulk_kw
                      ).pack(side=tk.LEFT, padx=(0, 4))

        current_idx = [0]

        def _collect() -> dict:
            v = {"title":  fields["title"].get().strip(),
                 "artist": fields["artist"].get().strip(),
                 "album":  fields["album"].get().strip(),
                 "genre":  fields["genre"].get().strip()}
            y = fields["year"].get().strip()
            v["year"] = int(y) if y.isdigit() else 0
            return v

        def _apply_here():
            vals = _collect()
            it = queue[current_idx[0]]
            for k in ("title", "artist", "album", "genre"):
                it["meta"][k] = vals[k]
            it["meta"]["year"] = vals["year"]
            if not it["meta"]["album"] and it["meta"]["artist"]:
                it["meta"]["album"] = it["meta"]["artist"]
            _refresh_row(current_idx[0])
            _refresh_counter()

        def _apply_all():
            vals = _collect()
            # Only overwrite fields the user actually filled in — empty fields
            # are left alone so per-file titles don't get wiped.
            for it in queue:
                if not it["included"]: continue
                for k in ("artist", "album", "genre"):
                    if vals[k]: it["meta"][k] = vals[k]
                if vals["title"]:  it["meta"]["title"]  = vals["title"]
                if vals["year"]:   it["meta"]["year"]   = vals["year"]
                if not it["meta"].get("album") and it["meta"].get("artist"):
                    it["meta"]["album"] = it["meta"]["artist"]
            for i in range(len(queue)): _refresh_row(i)
            _refresh_counter()

        prim = dict(bg=ACCENT, fg=BG, activebackground=LINK,
                    activeforeground=BG, relief=tk.FLAT,
                    font=("Consolas", 9, "bold"), padx=10, pady=6,
                    borderwidth=0, cursor="hand2")
        secn = dict(bg=ROW_HI, fg=FG, activebackground=TOP_BG,
                    activeforeground=FG, relief=tk.FLAT,
                    font=("Consolas", 9), padx=10, pady=6,
                    borderwidth=0, cursor="hand2")

        apply_row = tk.Frame(right, bg=PANEL)
        apply_row.pack(fill=tk.X, padx=16, pady=(16, 12))
        tk.Button(apply_row, text="Save for this track",
                  command=_apply_here, **prim).pack(side=tk.LEFT)
        tk.Button(apply_row, text="Apply filled fields to all",
                  command=_apply_all, **secn).pack(side=tk.LEFT, padx=(6, 0))

        def _load_selected(*a):
            sel = tree.selection()
            if not sel: return
            idx = int(sel[0])
            # Auto-save edits for the row we're leaving so clicking around
            # doesn't silently discard the user's typing.
            if idx != current_idx[0] and 0 <= current_idx[0] < len(queue):
                _apply_here()
            current_idx[0] = idx
            it = queue[idx]
            m = it["meta"]
            name_var.set(os.path.basename(it["path"]))
            fields["title"].set(m.get("title", "") or "")
            fields["artist"].set(m.get("artist", "") or "")
            fields["album"].set(m.get("album", "") or "")
            fields["genre"].set(m.get("genre", "") or "")
            fields["year"].set(str(m["year"]) if m.get("year") else "")
            try: sz = os.path.getsize(it["path"])
            except OSError: sz = 0
            dur = m.get("duration_ms", 0)
            dur_str = f"{dur//60000}:{(dur//1000)%60:02d}"
            info_var.set(
                f"Bitrate: {m.get('bitrate_kbps','?')} kbps    "
                f"Sample rate: {m.get('sample_rate','?')} Hz\n"
                f"Duration: {dur_str}    Size: {_fmt_size(sz)}\n\n"
                f"Path: {it['path']}"
            )
            # Show / hide the duplicate handling section for this file
            if it["dup"]:
                d = it["dup"]
                dup_msg_var.set(
                    f"🔁 Already on iPod — id {d.id}:  "
                    f"{d.artist or '?'} — {d.title or '?'}"
                    + (f"  ({d.album})" if d.album else "")
                )
                dup_action_var.set(it["dup_action"] or "skip")
                dup_frame.pack(fill=tk.X, padx=16, pady=(14, 0), before=apply_row)
            else:
                dup_frame.pack_forget()
        tree.bind("<<TreeviewSelect>>", _load_selected)

        def _on_click(evt):
            row = tree.identify_row(evt.y)
            col = tree.identify_column(evt.x)
            if row and col == "#1":  # checkbox column
                idx = int(row)
                queue[idx]["included"] = not queue[idx]["included"]
                _refresh_row(idx)
                _refresh_counter()
                return "break"
        tree.bind("<Button-1>", _on_click, add="+")

        if queue:
            tree.selection_set("0"); tree.focus("0")
            _load_selected()

        # ── BOTTOM bar: disk-write toggle + Cancel / Send ────────────────
        bottom = tk.Frame(win, bg=TOP_BG)
        bottom.pack(fill=tk.X, side=tk.BOTTOM)

        write_disk_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            bottom,
            text="Also write these tags to the MP3 files on disk (rewrites ID3v2)",
            variable=write_disk_var, bg=TOP_BG, fg=FG, selectcolor=BG,
            activebackground=TOP_BG, activeforeground=FG,
            font=("Consolas", 9), borderwidth=0,
        ).pack(side=tk.LEFT, padx=16, pady=10)

        def _do_send():
            # First, auto-save any pending edits for the currently focused row
            _apply_here()

            upload_items = []
            replace_ids: list = []
            skipped_dups = 0
            for it in queue:
                if not it["included"]: continue
                # Duplicate policy
                if it["dup"]:
                    if it["dup_action"] == "skip":
                        skipped_dups += 1
                        continue
                    if it["dup_action"] == "replace":
                        replace_ids.append(it["dup"].id)
                    # "add" falls through — track goes in as new
                m = dict(it["meta"])
                if not m.get("album") and m.get("artist"):
                    m["album"] = m["artist"]
                upload_items.append({"path": it["path"], "meta": m})

            if not upload_items:
                if skipped_dups:
                    messagebox.showinfo("Upload",
                        f"Nothing to upload — all {skipped_dups} selected "
                        f"file(s) were duplicates set to Skip.", parent=win)
                else:
                    messagebox.showinfo("Upload", "No files selected.", parent=win)
                return

            n_missing = sum(1 for it in upload_items
                            if has_missing_metadata(it["meta"]))
            if n_missing and not messagebox.askyesno(
                "Upload",
                f"{n_missing} file(s) still have missing metadata.\n\n"
                f"Continue anyway? (They'll appear as 'Unknown' on the iPod.)",
                parent=win):
                return

            will_write = write_disk_var.get()
            win.destroy()

            def task():
                if will_write:
                    self.after(0, self._set_status,
                               f"Writing tags to {len(upload_items)} file(s)…", True)
                    for it in upload_items:
                        try:
                            write_id3v2(it["path"], it["meta"])
                        except Exception as e:
                            print(f"[disk-tag] {it['path']}: {e}")

                batch = [{
                    "path": it["path"],
                    "title":  it["meta"]["title"],
                    "artist": it["meta"]["artist"],
                    "album":  it["meta"]["album"],
                    "year":   it["meta"].get("year", 0),
                    "bitrate_kbps": it["meta"].get("bitrate_kbps"),
                    "sample_rate":  it["meta"].get("sample_rate"),
                } for it in upload_items]
                try:
                    def on_progress(i, total, name):
                        self.after(0, self._set_status,
                                   f"Uploading [{i}/{total}] {name}…", True)
                    results = self._device.upload_music_batch(
                        batch, progress=on_progress,
                        pre_delete_ids=replace_ids or None)
                except Exception as e:
                    self.after(0, messagebox.showerror,
                               "Upload Music failed", str(e))
                    return
                ok = sum(1 for r in results if r["ok"])
                errs = [(r["path"], r.get("error", "?"))
                        for r in results if not r["ok"]]
                msg = f"Uploaded {ok}/{len(results)} file(s)."
                if replace_ids:
                    msg += f"\nReplaced {len(replace_ids)} duplicate(s)."
                if skipped_dups:
                    msg += f"\nSkipped {skipped_dups} duplicate(s)."
                if errs:
                    msg += "\n\nErrors:\n" + "\n".join(
                        f"  {os.path.basename(s)}: {e}" for s, e in errs[:5])
                self.after(0, self._on_connected)
                self.after(0, messagebox.showinfo, "Upload Music", msg)
            threading.Thread(target=task, daemon=True).start()

        tk.Button(bottom, text="Send →", command=_do_send, **prim
                  ).pack(side=tk.RIGHT, padx=(0, 16), pady=8)
        tk.Button(bottom, text="Cancel", command=win.destroy, **secn
                  ).pack(side=tk.RIGHT, padx=6, pady=8)

        win.bind("<Escape>", lambda e: win.destroy())

    # ─── On-device track edit / delete ──────────────────────────────────────
    def _collect_tracks_under(self, iid: str) -> list:
        """Every Track under this row (recursive). For track rows, returns
        [self]; for artist/album grouping rows, returns all track descendants."""
        t = self._lib_iid_to_track.get(iid)
        if t is not None:
            return [t]
        out = []
        for child in self.lib_tree.get_children(iid):
            out.extend(self._collect_tracks_under(child))
        return out

    def _selected_tracks(self) -> list:
        """Every Track object currently selected in the Library tree.
        Includes tracks under selected artist/album grouping rows."""
        sel = list(self.lib_tree.selection())
        # De-dup by id so an artist + its child track don't count twice
        seen = set()
        out = []
        for iid in sel:
            for t in self._collect_tracks_under(iid):
                if t.id not in seen:
                    seen.add(t.id); out.append(t)
        return out

    def _on_lib_right_click(self, evt):
        iid = self.lib_tree.identify_row(evt.y)
        if not iid: return
        # If the right-clicked row isn't already in the selection, take it
        # over — matches every file manager on the planet.
        if iid not in self.lib_tree.selection():
            self.lib_tree.selection_set(iid)
        self.lib_tree.focus(iid)

        tracks = self._selected_tracks()
        if not tracks: return
        n = len(tracks)
        label_edit   = "Edit metadata…" if n == 1 else f"Edit metadata for {n} tracks…"
        label_delete = "Delete from iPod" if n == 1 else f"Delete {n} tracks from iPod"

        menu = tk.Menu(self, tearoff=0, bg=PANEL, fg=FG,
                       activebackground=ROW_HI, activeforeground=FG,
                       font=("Consolas", 9), borderwidth=0)
        menu.add_command(label=label_edit,   command=self._edit_selected_tracks)
        menu.add_separator()
        menu.add_command(label=label_delete, command=self._delete_selected_tracks)
        try:
            menu.tk_popup(evt.x_root, evt.y_root)
        finally:
            menu.grab_release()

    def _edit_selected_tracks(self):
        if not self._device: return
        tracks = self._selected_tracks()
        if not tracks:
            messagebox.showinfo("Edit track", "Pick a track first."); return
        if len(tracks) == 1:
            self._open_edit_track_dialog(tracks[0])
        else:
            self._open_bulk_edit_dialog(tracks)

    def _delete_selected_tracks(self):
        if not self._device: return
        tracks = self._selected_tracks()
        if not tracks: return
        n = len(tracks)
        if n == 1:
            t = tracks[0]
            label = f"{t.artist or '?'} — {t.title or '?'}"
            confirm_msg = (f"Really delete this track from the iPod?\n\n"
                           f"{label}\n\nThe MP3 file will also be removed.")
        else:
            preview = "\n".join(f"  {t.artist or '?'} — {t.title or '?'}"
                                for t in tracks[:8])
            if n > 8:
                preview += f"\n  … and {n - 8} more"
            confirm_msg = (f"Really delete {n} tracks from the iPod?\n\n"
                           f"{preview}\n\n"
                           f"All matching MP3 files will also be removed.")
        if not messagebox.askyesno("Delete from iPod", confirm_msg):
            return

        ids = [t.id for t in tracks]
        def task():
            try:
                self.after(0, self._set_status,
                           f"Deleting {n} track(s)…", True)
                self._device.delete_tracks(ids)
            except Exception as e:
                self.after(0, messagebox.showerror, "Delete failed", str(e))
                return
            self.after(0, self._on_connected)
            self.after(0, self._set_status, f"Deleted {n} track(s)", True)
        threading.Thread(target=task, daemon=True).start()

    def _open_bulk_edit_dialog(self, tracks: list):
        """Edit N tracks together — only fields you fill in get applied.
        Empty fields leave each track's existing value alone."""
        n = len(tracks)
        win = tk.Toplevel(self)
        win.title(f"Bulk edit — {n} tracks")
        win.configure(bg=BG)
        win.geometry("600x420")
        win.transient(self); win.grab_set()

        tk.Label(win, text=f"Editing {n} tracks together:",
                 fg=DIM, bg=BG, font=("Consolas", 9)
                 ).pack(anchor=tk.W, padx=20, pady=(16, 0))

        # Preview which tracks are affected
        preview = tk.Frame(win, bg=PANEL)
        preview.pack(fill=tk.X, padx=20, pady=(4, 8))
        prev_lines = []
        for t in tracks[:6]:
            prev_lines.append(f"  {t.artist or '?'} — {t.title or '?'}")
        if n > 6:
            prev_lines.append(f"  … and {n - 6} more")
        tk.Label(preview, text="\n".join(prev_lines),
                 fg=ACCENT, bg=PANEL, font=("Consolas", 9),
                 anchor=tk.W, justify=tk.LEFT, wraplength=550
                 ).pack(anchor=tk.W, padx=10, pady=8, fill=tk.X)

        # ── Field entries. All empty by default. ──
        fields = {}
        for label, key in [("Title:",  "title"),
                           ("Artist:", "artist"),
                           ("Album:",  "album"),
                           ("Year:",   "year")]:
            row = tk.Frame(win, bg=BG)
            row.pack(fill=tk.X, padx=20, pady=3)
            tk.Label(row, text=label, fg=FG, bg=BG,
                     font=("Consolas", 10), width=8, anchor=tk.W
                     ).pack(side=tk.LEFT)
            v = tk.StringVar()
            tk.Entry(row, textvariable=v, bg=PANEL, fg=FG,
                     insertbackground=FG, font=("Consolas", 10),
                     relief=tk.FLAT
                     ).pack(side=tk.LEFT, fill=tk.X, expand=True,
                            padx=(4, 0), ipady=5)
            fields[key] = v

        tk.Label(win,
            text="Only fields you fill in will change. Empty fields leave "
                 "each track's existing value alone. Setting Title in bulk "
                 "gives every track the same title — usually not what you "
                 "want (leave it blank).",
            fg=DIM, bg=BG, font=("Consolas", 8),
            wraplength=550, justify=tk.LEFT
            ).pack(anchor=tk.W, padx=20, pady=(10, 0))

        def _do_save():
            new_title  = fields["title"].get().strip()  or None
            new_artist = fields["artist"].get().strip() or None
            new_album  = fields["album"].get().strip()  or None
            y = fields["year"].get().strip()
            new_year = int(y) if y.isdigit() else None
            if not any((new_title, new_artist, new_album, new_year)):
                messagebox.showinfo("Bulk edit",
                    "Nothing to change — fill in at least one field.",
                    parent=win)
                return
            ids = [t.id for t in tracks]
            win.destroy()

            def task():
                try:
                    self.after(0, self._set_status,
                               f"Updating {n} track(s)…", True)
                    self._device.edit_tracks(
                        ids, title=new_title, artist=new_artist,
                        album=new_album, year=new_year)
                except Exception as e:
                    self.after(0, messagebox.showerror,
                               "Bulk edit failed", str(e))
                    return
                self.after(0, self._on_connected)
                self.after(0, self._set_status,
                           f"Updated {n} track(s)", True)
            threading.Thread(target=task, daemon=True).start()

        prim = dict(bg=ACCENT, fg=BG, activebackground=LINK,
                    activeforeground=BG, relief=tk.FLAT,
                    font=("Consolas", 9, "bold"), padx=10, pady=6,
                    borderwidth=0, cursor="hand2")
        secn = dict(bg=ROW_HI, fg=FG, activebackground=TOP_BG,
                    activeforeground=FG, relief=tk.FLAT,
                    font=("Consolas", 9), padx=10, pady=6,
                    borderwidth=0, cursor="hand2")

        btnbar = tk.Frame(win, bg=BG)
        btnbar.pack(fill=tk.X, padx=20, pady=(16, 14), side=tk.BOTTOM)
        tk.Button(btnbar, text="Cancel", command=win.destroy, **secn
                  ).pack(side=tk.LEFT)
        tk.Button(btnbar, text=f"Apply to {n} tracks →",
                  command=_do_save, **prim
                  ).pack(side=tk.RIGHT)
        win.bind("<Return>", lambda e: _do_save())
        win.bind("<Escape>", lambda e: win.destroy())

    def _open_edit_track_dialog(self, t):
        """Modal editor for a single already-on-device track."""
        win = tk.Toplevel(self)
        win.title("Edit track on iPod")
        win.configure(bg=BG)
        win.geometry("560x340")
        win.transient(self); win.grab_set()

        tk.Label(win, text="Editing track on iPod:",
                 fg=DIM, bg=BG, font=("Consolas", 9)
                 ).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(win, text=f"[id {t.id}] {t.title or '?'}",
                 fg=ACCENT, bg=BG, font=("Consolas", 10, "bold"),
                 wraplength=520, justify=tk.LEFT, anchor=tk.W
                 ).pack(anchor=tk.W, padx=20, pady=(0, 12))

        fields = {}
        for label, key, val in [
            ("Title:",  "title",  t.title or ""),
            ("Artist:", "artist", t.artist or ""),
            ("Album:",  "album",  t.album or ""),
            ("Year:",   "year",   str(t.year) if getattr(t, "year", 0) else ""),
        ]:
            row = tk.Frame(win, bg=BG)
            row.pack(fill=tk.X, padx=20, pady=4)
            tk.Label(row, text=label, fg=FG, bg=BG,
                     font=("Consolas", 10), width=8, anchor=tk.W
                     ).pack(side=tk.LEFT)
            v = tk.StringVar(value=val)
            tk.Entry(row, textvariable=v, bg=PANEL, fg=FG,
                     insertbackground=FG, font=("Consolas", 10),
                     relief=tk.FLAT
                     ).pack(side=tk.LEFT, fill=tk.X, expand=True,
                            padx=(4, 0), ipady=5)
            fields[key] = v

        tk.Label(win,
            text="Saving will re-sync iTunesDB. The MP3 file itself is not "
                 "moved — only how the iPod indexes it changes.",
            fg=DIM, bg=BG, font=("Consolas", 8),
            wraplength=520, justify=tk.LEFT
            ).pack(anchor=tk.W, padx=20, pady=(10, 0))

        def _do_save():
            new_title  = fields["title"].get().strip()  or None
            new_artist = fields["artist"].get().strip() or None
            new_album  = fields["album"].get().strip()  or None
            y = fields["year"].get().strip()
            new_year = int(y) if y.isdigit() else None
            win.destroy()

            def task():
                try:
                    self.after(0, self._set_status,
                               f"Updating {new_title or t.title}…", True)
                    self._device.edit_track(
                        t.id, title=new_title, artist=new_artist,
                        album=new_album, year=new_year)
                except Exception as e:
                    self.after(0, messagebox.showerror,
                               "Edit failed", str(e))
                    return
                self.after(0, self._on_connected)
                self.after(0, self._set_status,
                           f"Updated: {new_artist or '?'} — {new_title or '?'}",
                           True)
            threading.Thread(target=task, daemon=True).start()

        prim = dict(bg=ACCENT, fg=BG, activebackground=LINK,
                    activeforeground=BG, relief=tk.FLAT,
                    font=("Consolas", 9, "bold"), padx=10, pady=6,
                    borderwidth=0, cursor="hand2")
        secn = dict(bg=ROW_HI, fg=FG, activebackground=TOP_BG,
                    activeforeground=FG, relief=tk.FLAT,
                    font=("Consolas", 9), padx=10, pady=6,
                    borderwidth=0, cursor="hand2")

        btnbar = tk.Frame(win, bg=BG)
        btnbar.pack(fill=tk.X, padx=20, pady=(16, 14), side=tk.BOTTOM)
        tk.Button(btnbar, text="Cancel", command=win.destroy, **secn
                  ).pack(side=tk.LEFT)
        tk.Button(btnbar, text="Save →", command=_do_save, **prim
                  ).pack(side=tk.RIGHT)
        win.bind("<Return>", lambda e: _do_save())
        win.bind("<Escape>", lambda e: win.destroy())

    def _download_all(self):
        """Download every track from iTunesDB to a local folder."""
        if not self._device:
            messagebox.showinfo("Download All", "No device connected."); return
        if not self._db or not self._db.tracks:
            messagebox.showinfo("Download All",
                                "No iTunesDB / no tracks on device."); return
        dest = filedialog.askdirectory(title="Pick local folder for downloaded music")
        if not dest: return

        if not messagebox.askyesno("Download All",
                f"Download {len(self._db.tracks)} tracks to:\n  {dest}\n\n"
                f"Files already present in target folder will be skipped.\n"
                f"You can Cancel from the popup that will appear."):
            return

        # Show a modal-ish progress window with Cancel
        self._download_cancel.clear()
        prog = self._open_progress_window("Download All", len(self._db.tracks),
                                          on_cancel=self._download_cancel.set)

        def on_track(n, total, track):
            # Called by download_music BEFORE each track
            self.after(0, self._update_progress_window, prog, n, total,
                       track.get("display", "?"))

        def task():
            err_msg = None
            results = []
            try:
                results = self._device.download_music(
                    dest,
                    name_template="{artist} - {title}.mp3",
                    skip_existing=True,
                    progress=on_track,
                    cancel_event=self._download_cancel,
                )
            except Exception as e:
                err_msg = str(e)
            self.after(0, self._close_progress_window, prog)
            if err_msg:
                self.after(0, messagebox.showerror, "Download All failed", err_msg)
                return
            ok = sum(1 for r in results if r["status"] == "ok")
            sk = sum(1 for r in results if r["status"] == "skipped")
            er = sum(1 for r in results if r["status"].startswith("error"))
            miss = sum(1 for r in results
                       if r["status"].startswith("missing")
                       or r["status"] == "no-location")
            cancelled = self._download_cancel.is_set()
            title = "Download cancelled" if cancelled else "Download All"
            msg = (
                f"{'CANCELLED. ' if cancelled else ''}"
                f"{ok} downloaded, {sk} skipped, "
                f"{miss} missing on device, {er} errors "
                f"— out of {len(results)} processed / {len(self._db.tracks)} total."
            )
            self.after(0, messagebox.showinfo, title, msg)
            self.after(0, self._on_connected)

        threading.Thread(target=task, daemon=True).start()

    # ── Progress-window helpers (used by Download All, maybe more later) ────
    def _open_progress_window(self, title: str, total: int, on_cancel):
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("520x180")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self); win.grab_set()

        tk.Label(win, text=title, fg=ACCENT, bg=BG,
                 font=("Consolas", 12, "bold")).pack(pady=(14, 4))
        count_var = tk.StringVar(value=f"0 / {total}")
        tk.Label(win, textvariable=count_var, fg=FG, bg=BG,
                 font=("Consolas", 10)).pack()
        file_var = tk.StringVar(value="Starting…")
        tk.Label(win, textvariable=file_var, fg=DIM, bg=BG,
                 font=("Consolas", 9), wraplength=480,
                 justify="center").pack(pady=(8, 4))
        bar = ttk.Progressbar(win, mode="determinate", maximum=total, length=460)
        bar.pack(pady=(6, 4))

        def _cancel():
            file_var.set("Cancelling — waiting for the current file to finish…")
            on_cancel()
        tk.Button(win, text="Cancel", command=_cancel,
                  bg=RED, fg=BG, relief=tk.FLAT, font=("Consolas", 9, "bold"),
                  padx=14, pady=4, cursor="hand2").pack(pady=(6, 10))

        # Prevent close via [X] mid-download without confirming
        win.protocol("WM_DELETE_WINDOW", _cancel)

        return {"win": win, "count": count_var, "file": file_var, "bar": bar}

    def _update_progress_window(self, prog: dict, n: int, total: int, name: str):
        try:
            prog["count"].set(f"{n} / {total}")
            prog["file"].set(name)
            prog["bar"]["value"] = n
        except tk.TclError:
            pass  # window was destroyed

    def _close_progress_window(self, prog: dict):
        try:
            prog["win"].grab_release()
            prog["win"].destroy()
        except tk.TclError:
            pass

    def _backup_db(self):
        """Save the iPod's iTunesDB to a local file."""
        if not self._device:
            messagebox.showinfo("Backup DB", "No device connected."); return
        dest = filedialog.asksaveasfilename(
            title="Save iTunesDB backup",
            defaultextension=".bin",
            initialfile="iTunesDB.bin",
            filetypes=[("iTunesDB binary", "*.bin *.itdb"), ("All files", "*.*")])
        if not dest: return

        def task():
            try:
                n = self._device.backup_itunesdb(dest)
                self.after(0, messagebox.showinfo, "Backup DB",
                           f"Saved {n:,} bytes to:\n{dest}")
            except Exception as e:
                self.after(0, messagebox.showerror, "Backup DB failed", str(e))
        threading.Thread(target=task, daemon=True).start()

    def _restore_db(self):
        """Restore a previously saved iTunesDB back to the iPod."""
        if not self._device:
            messagebox.showinfo("Restore DB", "No device connected."); return
        src = filedialog.askopenfilename(
            title="Pick iTunesDB to restore",
            filetypes=[("iTunesDB binary", "*.bin *.itdb"), ("All files", "*.*")])
        if not src: return

        if not messagebox.askyesno("Restore DB",
                f"OVERWRITE iPod's iTunesDB with:\n  {src}\n\n"
                f"This will enter sync mode and replace the on-device DB. "
                f"Continue?"):
            return

        def task():
            try:
                self.after(0, self._set_status, "Restoring iTunesDB…", True)
                self._device.restore_itunesdb(src)
                self.after(0, messagebox.showinfo, "Restore DB",
                           f"Restored {os.path.basename(src)}.")
                self.after(0, self._on_connected)
            except Exception as e:
                self.after(0, messagebox.showerror, "Restore DB failed", str(e))
        threading.Thread(target=task, daemon=True).start()

    def _eject_device(self):
        """Safe disconnect. For Classic iPods this does Windows-native
        lock+dismount so the cable can be unplugged without corrupting the DB.
        For Touch iPods it's just a graceful session close."""
        if not self._device:
            self._go_back_to_picker(); return
        name = self._device.info().get("DeviceName", "iPod")
        is_classic = self._device.device_type == "classic"
        prompt = (f"Safely eject '{name}'?\n\n"
                  f"For Classic/Mass-Storage iPods this flushes the disk cache "
                  f"and dismounts the volume — the same as clicking 'Safely "
                  f"Remove Hardware' in Windows tray.\n\n"
                  f"After this you can safely unplug the cable."
                  if is_classic else
                  f"Disconnect from '{name}'?\n\n"
                  f"iOS devices don't need a special 'safely remove' — you can "
                  f"unplug at any time. This just closes the robly session.")
        if not messagebox.askyesno("Eject", prompt):
            return

        def task():
            err = None
            try:
                self.after(0, self._set_status, f"Ejecting {name}…", True)
                self._device.eject()
            except Exception as e:
                err = str(e)
            self.after(0, self._eject_done, name, is_classic, err)
        threading.Thread(target=task, daemon=True).start()

    def _eject_done(self, name, is_classic, err):
        if err:
            messagebox.showerror("Eject failed",
                f"{err}\n\nClose any file managers or media players that might "
                f"have files open on the iPod, then try again.")
            return
        if is_classic:
            messagebox.showinfo("Ejected",
                f"'{name}' is now safe to unplug.\nThe drive letter will "
                f"disappear from Windows Explorer.")
        else:
            messagebox.showinfo("Disconnected", f"Session with '{name}' closed.")
        self._device = None
        self._db = None
        if self._main_frame is not None:
            self._main_frame.destroy(); self._main_frame = None
        self._build_picker()
        threading.Thread(target=self._picker_poll_loop, daemon=True).start()

    def _on_close(self):
        self._poll_stop.set()
        if self._device:
            try: self._device.close()
            except Exception: pass
        self.destroy()


if __name__ == "__main__":
    RoblyApp().mainloop()
