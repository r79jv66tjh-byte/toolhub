"""Tkinter-Oberfläche des ToolHubs."""

from __future__ import annotations

import os
import queue
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, autostart, claude, gitinfo, launcher, manifest, scanner
from .config import CATEGORIES, DEFAULT_ICONS, Config, new_program

FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
FONT_H = ("Segoe UI", 13, "bold")
FONT_ICON = ("Segoe UI Emoji", 22)
TILE_W = 215
TILE_H = 128

COL_BG = "#f3f4f6"
COL_TILE = "#ffffff"
COL_SEL = "#dbeafe"
COL_RUN = "#16a34a"
COL_MUTED = "#6b7280"
COL_BORDER = "#d1d5db"

KIND_LABEL = {"bat": "start.bat", "pyw": "Python (GUI)", "py": "Python", "modul": "Python-Modul",
              "exe": ".exe", "mod": "Spiel-Mod", "projekt": "ohne Startpunkt", "manuell": "manuell"}

F_ALL, F_FAV, F_RUN = "Alle Programme", "★ Favoriten", "▶ Laufend"


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def open_path(path: str) -> None:
    if os.name == "nt":
        os.startfile(path)  # noqa: S606
    else:
        import subprocess
        subprocess.Popen(["xdg-open", path])


class App:
    def __init__(self, root: tk.Tk, cfg: Config):
        self.root = root
        self.cfg = cfg
        self.running: dict[str, list[launcher.Running]] = {}
        self.selected: str | None = None
        self.filter = F_ALL
        self.git_cache: dict[str, gitinfo.GitStatus] = {}
        self._dupes: dict[str, int] = {}
        self.ui_queue: queue.Queue = queue.Queue()
        self._tile_widgets: dict[str, tk.Frame] = {}
        self._last_running_ids: set[str] = set()
        self._tile_cols = 0

        root.title(f"ToolHub {__version__}")
        root.geometry(cfg.settings.get("geometry", "1100x700"))
        root.minsize(820, 520)
        root.configure(bg=COL_BG)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=24, font=FONT)
        style.configure("Treeview.Heading", font=FONT_B)
        style.configure("Side.Treeview", rowheight=28, font=FONT)

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._build_menu()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        root.bind("<F5>", lambda e: self.rescan())
        root.bind("<Return>", lambda e: self.start_selected())
        root.bind("<Delete>", lambda e: self.remove_selected())

        if cfg.settings.get("scan_on_start", True) or not cfg.programs:
            self.rescan(quiet=True)
        else:
            self.refresh()
        self.root.after(1000, self._tick)
        self.root.after(100, self._drain_queue)
        self.root.after(150, self._initial_layout)
        self.root.after(3000, self._check_updates)

    # ------------------------------------------------------------------ Store
    def open_store(self):
        from .store_gui import StoreWindow
        StoreWindow(self)

    def _check_updates(self):
        """Beim Start im Hintergrund nach Updates installierter Tools suchen (nur Anzeige)."""
        from . import store

        def work():
            try:
                ups = store.check_updates()
            except Exception:  # noqa: BLE001 – Netzwerkprobleme nicht stören lassen
                return
            if ups:
                names = ", ".join(u["name"] for u in ups)
                self.ui_queue.put(lambda: (self.btn_store.config(text=f"🛒 Store ({len(ups)} ⬆)"),
                                           self.set_status(f"Updates verfügbar: {names} – im Store installieren")))
        threading.Thread(target=work, daemon=True).start()

    def _initial_layout(self, tries: int = 20):
        """Bereich „Laufende Programme“ klein halten (ca. 4 Zeilen)."""
        h = self.outer.winfo_height()
        if h > 300:
            self.outer.sashpos(0, h - 150)
        elif tries:
            self.root.after(100, self._initial_layout, tries - 1)

    # ------------------------------------------------------------------ Aufbau
    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text="🔎").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh())
        self.search_entry = ttk.Entry(bar, textvariable=self.search_var, width=34, font=FONT)
        self.search_entry.pack(side="left", padx=(4, 12))
        self.view_var = tk.StringVar(value=self.cfg.settings.get("view", "kacheln"))
        ttk.Radiobutton(bar, text="Kacheln", value="kacheln", variable=self.view_var,
                        command=self._switch_view).pack(side="left")
        ttk.Radiobutton(bar, text="Liste", value="liste", variable=self.view_var,
                        command=self._switch_view).pack(side="left", padx=(4, 12))
        ttk.Button(bar, text="Einstellungen", command=self.open_settings).pack(side="right")
        ttk.Button(bar, text="💬 Claude fragen", command=self.ask_claude).pack(side="right", padx=4)
        ttk.Button(bar, text="🤖 Claude verbinden", command=self.connect_claude).pack(side="right", padx=4)
        ttk.Button(bar, text="+ Programm hinzufügen", command=self.add_program).pack(side="right", padx=4)
        self.btn_store = ttk.Button(bar, text="🛒 Store", command=self.open_store)
        self.btn_store.pack(side="right", padx=4)
        ttk.Button(bar, text="⟳ Neu scannen", command=self.rescan).pack(side="right", padx=4)

    def _build_body(self):
        outer = ttk.PanedWindow(self.root, orient="vertical")
        outer.pack(fill="both", expand=True, padx=10)
        self.outer = outer
        pw = ttk.PanedWindow(outer, orient="horizontal")
        outer.add(pw, weight=5)

        # Seitenleiste mit Filtern
        side = ttk.Frame(pw)
        self.side = ttk.Treeview(side, show="tree", selectmode="browse", style="Side.Treeview")
        self.side.column("#0", width=215)
        self.side.pack(fill="both", expand=True)
        self.side.bind("<<TreeviewSelect>>", self._on_filter)
        pw.add(side, weight=0)

        # Mitte: Kacheln oder Liste
        self.center = ttk.Frame(pw)
        pw.add(self.center, weight=4)
        self._build_tiles(self.center)
        self._build_list(self.center)

        # Rechts: Details
        self.detail = ttk.Frame(pw, padding=(10, 4), width=320)
        pw.add(self.detail, weight=1)
        self._build_detail(self.detail)

        # Unten: laufende Programme
        runf = ttk.LabelFrame(outer, text="Laufende Programme", padding=4)
        outer.add(runf, weight=0)
        cols = ("name", "pids", "zeit", "art")
        self.run_tree = ttk.Treeview(runf, columns=cols, show="headings", height=4, selectmode="browse")
        for c, t, w in (("name", "Programm", 260), ("pids", "Prozesse (PID)", 220),
                        ("zeit", "Laufzeit", 90), ("art", "Hinweis", 200)):
            self.run_tree.heading(c, text=t)
            self.run_tree.column(c, width=w, anchor="w")
        self.run_tree.pack(side="left", fill="both", expand=True)
        btns = ttk.Frame(runf)
        btns.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(btns, text="Beenden", command=self.stop_running_selected).pack(fill="x")
        ttk.Button(btns, text="Hart beenden", command=lambda: self.stop_running_selected(force=True)).pack(fill="x", pady=4)
        ttk.Button(btns, text="Alle beenden", command=self.stop_all).pack(fill="x")

    def _build_tiles(self, parent):
        self.tile_frame = ttk.Frame(parent)
        self.canvas = tk.Canvas(self.tile_frame, bg=COL_BG, highlightthickness=0)
        sb = ttk.Scrollbar(self.tile_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.tiles = tk.Frame(self.canvas, bg=COL_BG)
        self._tiles_win = self.canvas.create_window((0, 0), window=self.tiles, anchor="nw")
        self.tiles.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _build_list(self, parent):
        self.list_frame = ttk.Frame(parent)
        cols = ("fav", "name", "claude", "kat", "art", "status", "beschr")
        self.tree = ttk.Treeview(self.list_frame, columns=cols, show="headings", selectmode="browse")
        for c, t, w in (("fav", "★", 30), ("name", "Name", 230), ("claude", "Claude", 60), ("kat", "Kategorie", 150),
                        ("art", "Start", 95), ("status", "Status", 60), ("beschr", "Beschreibung", 300)):
            self.tree.heading(c, text=t, command=lambda c=c: self._sort_list(c))
            self.tree.column(c, width=w, anchor="w", stretch=(c == "beschr"))
        sb = ttk.Scrollbar(self.list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.select(self.tree.selection()[0] if self.tree.selection() else None))
        self.tree.bind("<Double-1>", lambda e: self.start_selected())
        self.tree.bind("<Button-3>", self._list_context)
        self.tree.tag_configure("run", foreground=COL_RUN)
        self.tree.tag_configure("muted", foreground=COL_MUTED)
        self._sort_col = "name"

    def _build_detail(self, f):
        self.d_title = ttk.Label(f, text="Kein Programm ausgewählt", font=FONT_H, wraplength=300)
        self.d_title.pack(anchor="w")
        self.d_sub = ttk.Label(f, text="", foreground=COL_MUTED, wraplength=300)
        self.d_sub.pack(anchor="w", pady=(0, 6))
        self.d_desc = ttk.Label(f, text="", wraplength=300, justify="left")
        self.d_desc.pack(anchor="w", fill="x")

        b = ttk.Frame(f)
        b.pack(fill="x", pady=8)
        self.btn_start = ttk.Button(b, text="▶ Starten", command=self.start_selected)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        self.btn_admin = ttk.Button(b, text="🛡 Als Admin", command=lambda: self.start_selected(admin=True))
        self.btn_admin.grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        self.btn_stop = ttk.Button(b, text="■ Beenden", command=self.stop_selected)
        self.btn_stop.grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        self.btn_fav = ttk.Button(b, text="☆ Favorit", command=self.toggle_favorite)
        self.btn_fav.grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(b, text="📁 Ordner öffnen", command=self.open_folder).grid(row=2, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(b, text="📄 README", command=self.show_readme).grid(row=2, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(b, text="🔨 Als .exe bauen", command=self.build_exe).grid(row=3, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(b, text="⬇ git pull", command=self.git_pull).grid(row=3, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(b, text="✎ Bearbeiten", command=self.edit_selected).grid(row=4, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(b, text="✕ Entfernen", command=self.remove_selected).grid(row=4, column=1, sticky="ew", padx=2, pady=2)
        b.columnconfigure((0, 1), weight=1)

        g = ttk.LabelFrame(f, text="Git", padding=6)
        g.pack(fill="x", pady=(4, 0))
        ttk.Button(g, text="⟳", width=3, command=lambda: self.load_git(force=True)).pack(side="right", anchor="n")
        self.d_git = ttk.Label(g, text="–", wraplength=260, justify="left")
        self.d_git.pack(side="left", anchor="w", fill="x")
        self.d_cmd = ttk.Label(f, text="", foreground=COL_MUTED, wraplength=300, justify="left", font=("Consolas", 8))
        self.d_cmd.pack(anchor="w", pady=(8, 0))

    def _build_statusbar(self):
        self.status = ttk.Label(self.root, text="", anchor="w", padding=(10, 3), foreground=COL_MUTED)
        self.status.pack(fill="x", side="bottom")

    def _build_menu(self):
        self.ctx = tk.Menu(self.root, tearoff=0)
        self.ctx.add_command(label="Starten", command=self.start_selected)
        self.ctx.add_command(label="Als Administrator starten", command=lambda: self.start_selected(admin=True))
        self.ctx.add_command(label="Beenden", command=self.stop_selected)
        self.ctx.add_separator()
        self.ctx.add_command(label="Favorit an/aus", command=self.toggle_favorite)
        self.ctx.add_command(label="Ordner öffnen", command=self.open_folder)
        self.ctx.add_command(label="README anzeigen", command=self.show_readme)
        self.ctx.add_command(label="Als .exe bauen", command=self.build_exe)
        self.ctx.add_separator()
        self.ctx.add_command(label="Bearbeiten …", command=self.edit_selected)
        self.ctx.add_command(label="Entfernen", command=self.remove_selected)

    # ------------------------------------------------------------- Hilfsfunktionen
    def set_status(self, text: str):
        self.status.configure(text=text)

    def prog(self) -> dict | None:
        return self.cfg.get(self.selected) if self.selected else None

    def is_running(self, pid: str) -> bool:
        return any(r.alive() for r in self.running.get(pid, []))

    def display_name(self, p: dict) -> str:
        """Name, bei Namensgleichheit mit dem Projektordner dahinter."""
        if self._dupes.get(p["name"], 0) > 1 and p.get("path"):
            return f'{p["name"]} ({os.path.basename(p["path"])})'
        return p["name"]

    def visible_programs(self) -> list[dict]:
        q = self.search_var.get().strip().lower()
        show_all = self.cfg.settings.get("show_without_start", False)
        out = []
        for p in self.cfg.programs:
            if not show_all and not p.get("command") and p.get("kind") in ("projekt", "mod"):
                continue
            if self.filter == F_FAV and not p.get("favorite"):
                continue
            if self.filter == F_RUN and not self.is_running(p["id"]):
                continue
            if self.filter in CATEGORIES and p.get("category") != self.filter:
                continue
            if q and not all(w in " ".join((p["name"], p.get("description", ""), p.get("category", ""),
                                            p.get("path", ""))).lower() for w in q.split()):
                continue
            out.append(p)
        out.sort(key=lambda p: (not p.get("favorite"), p["name"].lower()))
        return out

    # ----------------------------------------------------------------- Anzeige
    def refresh(self):
        self._dupes = {}
        for p in self.cfg.programs:
            self._dupes[p["name"]] = self._dupes.get(p["name"], 0) + 1
        self._refresh_sidebar()
        progs = self.visible_programs()
        if self.view_var.get() == "liste":
            self.tile_frame.pack_forget()
            self.list_frame.pack(fill="both", expand=True)
            self._render_list(progs)
        else:
            self.list_frame.pack_forget()
            self.tile_frame.pack(fill="both", expand=True)
            self._render_tiles(progs)
        self._render_detail()

    def _refresh_sidebar(self):
        show_all = self.cfg.settings.get("show_without_start", False)
        progs = [p for p in self.cfg.programs
                 if show_all or p.get("command") or p.get("kind") not in ("projekt", "mod")]
        counts = {c: sum(1 for p in progs if p.get("category") == c) for c in CATEGORIES}
        items = [(F_ALL, f"{F_ALL} ({len(progs)})"),
                 (F_FAV, f"{F_FAV} ({sum(1 for p in progs if p.get('favorite'))})"),
                 (F_RUN, f"{F_RUN} ({sum(1 for p in progs if self.is_running(p['id']))})")]
        items += [(c, f"{DEFAULT_ICONS[c]} {c} ({counts[c]})") for c in CATEGORIES]
        existing = set(self.side.get_children())
        for key, text in items:
            if key in existing:
                self.side.item(key, text=text)
            else:
                self.side.insert("", "end", iid=key, text=text)
        if self.side.selection() != (self.filter,):
            self.side.selection_set(self.filter)

    def _render_list(self, progs):
        self.tree.delete(*self.tree.get_children())
        key = {"name": lambda p: p["name"].lower(), "kat": lambda p: p.get("category", ""),
               "art": lambda p: p.get("kind", ""), "fav": lambda p: not p.get("favorite"),
               "status": lambda p: not self.is_running(p["id"]),
               "claude": lambda p: not self.is_claude(p),
               "beschr": lambda p: p.get("description", "")}[self._sort_col]
        for p in sorted(progs, key=key):
            run = self.is_running(p["id"])
            tags = ("run",) if run else (("muted",) if not p.get("command") else ())
            self.tree.insert("", "end", iid=p["id"], tags=tags, values=(
                "★" if p.get("favorite") else "", f'{p.get("icon") or ""} {self.display_name(p)}',
                "🤖 ja" if self.is_claude(p) else "", p.get("category", ""), KIND_LABEL.get(p.get("kind"), p.get("kind", "")),
                "läuft" if run else "", p.get("description", "")))
        if self.selected and self.tree.exists(self.selected):
            self.tree.selection_set(self.selected)
            self.tree.see(self.selected)

    def _sort_list(self, col):
        self._sort_col = col
        self.refresh()

    def _on_canvas_resize(self, e):
        self.canvas.itemconfigure(self._tiles_win, width=e.width)
        cols = max(1, e.width // (TILE_W + 12))
        if cols != self._tile_cols:
            self._tile_cols = cols
            self._layout_tiles()

    def _on_wheel(self, e):
        if self.view_var.get() != "kacheln":
            return
        w = self.root.winfo_containing(e.x_root, e.y_root)
        while w is not None and w is not self.canvas:
            w = w.master
        if w is self.canvas:
            self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def _render_tiles(self, progs):
        for w in self.tiles.winfo_children():
            w.destroy()
        self._tile_widgets.clear()
        self._tile_order = [p["id"] for p in progs]
        if not progs:
            tk.Label(self.tiles, text="Keine Programme gefunden.\n\n„⟳ Neu scannen“ oder „+ Programm hinzufügen“.",
                     bg=COL_BG, fg=COL_MUTED, font=FONT, justify="center").grid(row=0, column=0, padx=20, pady=40)
            return
        for p in progs:
            self._tile_widgets[p["id"]] = self._make_tile(p)
        self._layout_tiles()

    def _layout_tiles(self):
        cols = max(1, self._tile_cols or 3)
        for i, pid in enumerate(getattr(self, "_tile_order", [])):
            t = self._tile_widgets.get(pid)
            if t:
                t.grid(row=i // cols, column=i % cols, padx=6, pady=6, sticky="nw")

    def _make_tile(self, p) -> tk.Frame:
        sel = p["id"] == self.selected
        run = self.is_running(p["id"])
        bg = COL_SEL if sel else COL_TILE
        t = tk.Frame(self.tiles, bg=bg, width=TILE_W, height=TILE_H, highlightthickness=2,
                     highlightbackground=COL_RUN if run else COL_BORDER, cursor="hand2")
        t.grid_propagate(False)
        t.pack_propagate(False)
        top = tk.Frame(t, bg=bg)
        top.pack(fill="x", padx=8, pady=(6, 0))
        tk.Label(top, text=p.get("icon") or "📦", font=FONT_ICON, bg=bg).pack(side="left")
        right = tk.Frame(top, bg=bg)
        right.pack(side="left", fill="x", expand=True, padx=(6, 0))
        name = ("★ " if p.get("favorite") else "") + self.display_name(p)
        tk.Label(right, text=name, font=FONT_B, bg=bg, anchor="w", wraplength=TILE_W - 80,
                 justify="left").pack(fill="x")
        sub = p.get("category", "")
        if self.is_claude(p):
            sub = f"🤖 Claude-fähig · {sub}"
        if run:
            sub = "● läuft"
        elif not p.get("command"):
            sub = f'{sub} · {KIND_LABEL.get(p.get("kind"), "")}'
        tk.Label(right, text=sub, font=("Segoe UI", 8), bg=bg, anchor="w",
                 fg=COL_RUN if run else COL_MUTED).pack(fill="x")
        desc = p.get("description") or p.get("path", "")
        if len(desc) > 110:
            desc = desc[:108].rstrip() + "…"
        tk.Label(t, text=desc, font=("Segoe UI", 8), bg=bg, fg="#374151", anchor="nw",
                 justify="left", wraplength=TILE_W - 30).pack(fill="both", expand=True, padx=10, pady=(4, 6))

        def bind(w):
            w.bind("<Button-1>", lambda e, pid=p["id"]: self.select(pid))
            w.bind("<Double-1>", lambda e, pid=p["id"]: (self.select(pid), self.start_selected()))
            w.bind("<Button-3>", lambda e, pid=p["id"]: (self.select(pid), self.ctx.tk_popup(e.x_root, e.y_root)))
            for c in w.winfo_children():
                bind(c)
        bind(t)
        return t

    def _list_context(self, e):
        row = self.tree.identify_row(e.y)
        if row:
            self.tree.selection_set(row)
            self.select(row)
            self.ctx.tk_popup(e.x_root, e.y_root)

    def _render_detail(self):
        p = self.prog()
        if not p:
            self.d_title.configure(text="Kein Programm ausgewählt")
            self.d_sub.configure(text="Kachel anklicken, Doppelklick startet.")
            self.d_desc.configure(text="")
            self.d_git.configure(text="–")
            self.d_cmd.configure(text="")
            for b in (self.btn_start, self.btn_admin, self.btn_stop):
                b.configure(state="disabled")
            return
        run = self.is_running(p["id"])
        self.d_title.configure(text=f'{p.get("icon") or ""} {self.display_name(p)}')
        self.d_sub.configure(text=f'{p.get("category", "")} · {KIND_LABEL.get(p.get("kind"), p.get("kind"))}'
                             + (" · ● läuft" if run else ""))
        self.d_desc.configure(text=p.get("description") or "(keine Beschreibung)")
        cmd_text = f'Ordner: {p.get("path")}\nStart: {p.get("command") or "–"}'
        if self.is_claude(p):
            try:
                m = manifest.load(Path(p["path"]) / manifest.MANIFEST_NAME)
                cmds = ", ".join(c.name + ("" if c.readonly else " ✎") for c in m.commands) or "–"
                cmd_text += f"\n🤖 Claude-Befehle (toolhub.json): {cmds}"
                if m.errors:
                    cmd_text += "\n⚠ " + "; ".join(m.errors)
            except manifest.ManifestError as e:
                cmd_text += f"\n⚠ toolhub.json fehlerhaft: {e}"
        self.d_cmd.configure(text=cmd_text)
        state = "normal" if p.get("command") else "disabled"
        self.btn_start.configure(state=state)
        self.btn_admin.configure(state=state)
        self.btn_stop.configure(state="normal" if run else "disabled")
        self.btn_fav.configure(text="★ Favorit" if p.get("favorite") else "☆ Favorit")
        st = self.git_cache.get(p.get("path", ""))
        self.d_git.configure(text=self._git_text(st) if st else "wird geladen …")

    @staticmethod
    def _git_text(st: gitinfo.GitStatus) -> str:
        if st.error or not st.is_repo:
            return st.summary()
        commit = st.last_commit or "(noch keiner)"
        if len(commit) > 70:
            commit = commit[:68].rstrip() + "…"
        lines = [f"Branch: {st.branch}", f"Letzter Commit: {commit}"]
        if st.last_date:
            lines.append(f"am {st.last_date} von {st.author}")
        if st.changes:
            lines.append(f"⚠ {len(st.changes)} uncommittete Änderung(en):")
            lines += ["   " + c for c in st.changes[:6]]
            if len(st.changes) > 6:
                lines.append(f"   … und {len(st.changes) - 6} weitere")
        else:
            lines.append("✓ keine uncommitteten Änderungen")
        if st.ahead_behind:
            lines.append(st.ahead_behind)
        return "\n".join(lines)

    # ---------------------------------------------------------------- Ereignisse
    def _on_filter(self, _e):
        sel = self.side.selection()
        if sel and sel[0] != self.filter:
            self.filter = sel[0]
            self.refresh()

    def _switch_view(self):
        self.cfg.settings["view"] = self.view_var.get()
        self.cfg.save()
        self.refresh()

    def select(self, pid: str | None):
        if pid == self.selected:
            return
        old = self.selected
        self.selected = pid
        if self.view_var.get() == "kacheln":
            for x in (old, pid):
                p = self.cfg.get(x) if x else None
                if p and x in self._tile_widgets:
                    self._tile_widgets[x].destroy()
                    self._tile_widgets[x] = self._make_tile(p)
            self._layout_tiles()
        self._render_detail()
        self.load_git()

    def load_git(self, force=False):
        p = self.prog()
        if not p or not p.get("path"):
            return
        path = p["path"]
        if path in self.git_cache and not force:
            return
        if force:
            self.git_cache.pop(path, None)
            self._render_detail()

        def work():
            st = gitinfo.status(path)
            self.ui_queue.put(lambda: (self.git_cache.__setitem__(path, st), self._render_detail()))
        threading.Thread(target=work, daemon=True).start()

    def _drain_queue(self):
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _tick(self):
        """Jede Sekunde: Liste der laufenden Programme aktualisieren."""
        alive_ids = set()
        rows = []
        for pid, runs in list(self.running.items()):
            for r in runs:
                pids = r.pids()
                if pids:
                    alive_ids.add(pid)
                    note = "als Administrator" if r.admin else ""
                    rows.append((f"{pid}#{id(r)}", r.name, ", ".join(map(str, pids[:8])),
                                 fmt_duration(r.runtime()), note))
                elif r.ended is None:
                    r.ended = time.time()
            for r in [r for r in runs if r.ended]:
                r.close()
                runs.remove(r)
            if not runs:
                del self.running[pid]
        sel = self.run_tree.selection()
        self.run_tree.delete(*self.run_tree.get_children())
        for iid, *vals in rows:
            self.run_tree.insert("", "end", iid=iid, values=vals)
        if sel and self.run_tree.exists(sel[0]):
            self.run_tree.selection_set(sel[0])
        if alive_ids != self._last_running_ids:
            self._last_running_ids = alive_ids
            self.refresh()
        self.root.after(1000, self._tick)

    # ------------------------------------------------------------------ Aktionen
    def rescan(self, quiet=False):
        from .store import apps_dir
        roots = list(dict.fromkeys([str(apps_dir())] + list(self.cfg.settings.get("scan_roots", []))))
        self.set_status("Suche Programme in " + "; ".join(roots) + " …")

        def work():
            try:
                found = scanner.scan_roots(roots, self.cfg.settings.get("only_tools", True))
            except Exception as e:  # noqa: BLE001
                self.ui_queue.put(lambda: self.set_status(f"Scan fehlgeschlagen: {e}"))
                return

            def done():
                new, upd, gone = self.cfg.merge_scan(found)
                self.cfg.save()
                self.git_cache.clear()
                self.refresh()
                self.set_status(f"Scan fertig: {len(found)} Startpunkte gefunden, {new} neu, "
                                f"{upd} aktualisiert, {gone} verschwunden. Einstellungen: {self.cfg.path}")
            self.ui_queue.put(done)
        threading.Thread(target=work, daemon=True).start()

    def start_selected(self, admin=False):
        p = self.prog()
        if not p:
            return
        if not p.get("command"):
            messagebox.showinfo("Kein Startbefehl", f"„{p['name']}“ hat keinen Startbefehl.\n"
                                "Über „Bearbeiten“ kannst du einen eintragen.", parent=self.root)
            return
        if self.is_running(p["id"]) and not messagebox.askyesno(
                "Läuft schon", f"„{p['name']}“ läuft bereits. Noch einmal starten?", parent=self.root):
            return
        prog = dict(p, admin=admin or p.get("admin"))
        try:
            r = launcher.launch(prog)
        except launcher.LaunchError as e:
            messagebox.showerror("Start fehlgeschlagen", str(e), parent=self.root)
            return
        except OSError as e:
            messagebox.showerror("Start fehlgeschlagen", str(e), parent=self.root)
            return
        self.running.setdefault(p["id"], []).append(r)
        self.set_status(f"Gestartet: {p['name']}" + (" (als Administrator)" if prog["admin"] else ""))
        self.root.after(300, self._tick_once)

    def _tick_once(self):
        self._last_running_ids = set()  # Neuzeichnen erzwingen

    def stop_selected(self):
        p = self.prog()
        if p:
            self._stop(p["id"])

    def _stop(self, pid: str, force=False, only: launcher.Running | None = None):
        runs = [only] if only else list(self.running.get(pid, []))
        if not runs:
            return
        name = runs[0].name
        self.set_status(f"Beende {name} …")

        def work():
            msgs = []
            for r in runs:
                try:
                    ok = r.kill() if force else r.stop()
                    if not ok:
                        msgs.append("Einige Prozesse laufen noch.")
                except launcher.LaunchError as e:
                    msgs.append(str(e))
            text = f"Beendet: {name}" if not msgs else f"{name}: " + " ".join(msgs)
            self.ui_queue.put(lambda: self.set_status(text))
        threading.Thread(target=work, daemon=True).start()

    def stop_running_selected(self, force=False):
        sel = self.run_tree.selection()
        if not sel:
            return
        pid, rid = sel[0].rsplit("#", 1)
        r = next((r for r in self.running.get(pid, []) if str(id(r)) == rid), None)
        if r:
            self._stop(pid, force=force, only=r)

    def stop_all(self):
        if self.running and messagebox.askyesno("Alle beenden", "Alle von ToolHub gestarteten Programme beenden?",
                                                parent=self.root):
            for pid in list(self.running):
                self._stop(pid)

    def toggle_favorite(self):
        p = self.prog()
        if p:
            p["favorite"] = not p.get("favorite")
            self.cfg.save()
            self.refresh()

    def open_folder(self):
        p = self.prog()
        if p and p.get("path") and os.path.isdir(p["path"]):
            open_path(p["path"])
        elif p:
            messagebox.showerror("Ordner fehlt", f"Ordner nicht gefunden:\n{p.get('path')}", parent=self.root)

    def show_readme(self):
        p = self.prog()
        if not p:
            return
        path = p.get("readme") or (str(scanner.find_readme(Path(p["path"]))) if p.get("path") and
                                   scanner.find_readme(Path(p["path"])) else "")
        if not path or not os.path.isfile(path):
            messagebox.showinfo("README", "Für dieses Programm gibt es keine README.", parent=self.root)
            return
        ReadmeWindow(self.root, p["name"], Path(path))

    def git_pull(self):
        p = self.prog()
        if not p or not os.path.isdir(os.path.join(p.get("path", ""), ".git")):
            messagebox.showinfo("git pull", "Kein Git-Repository.", parent=self.root)
            return
        if not messagebox.askyesno("Update per git pull",
                                   f"In\n{p['path']}\n„git pull --ff-only“ ausführen?", parent=self.root):
            return
        OutputWindow(self.root, f"git pull – {p['name']}", "git pull --ff-only", p["path"],
                     on_done=lambda rc: self.load_git(force=True))

    def build_exe(self):
        p = self.prog()
        if not p:
            return
        cmd = p.get("build") or self._pyinstaller_cmd(p)
        if not cmd:
            messagebox.showinfo("Als .exe bauen", "Für diese Art von Programm weiß ich nicht, wie man eine .exe baut.\n"
                                "Trage unter „Bearbeiten“ einen Build-Befehl ein.", parent=self.root)
            return
        if not messagebox.askyesno(
                "Als .exe bauen", f"Im Ordner\n{p['path']}\nwird gebaut mit:\n\n{cmd}\n\n"
                "Das Ergebnis landet im Unterordner „dist“. Fortfahren?", parent=self.root):
            return
        OutputWindow(self.root, f"Build – {p['name']}", cmd, p["path"],
                     on_done=lambda rc: rc == 0 and self.rescan(quiet=True))

    @staticmethod
    def _pyinstaller_cmd(p: dict) -> str:
        kind = p.get("kind")
        exe, rest = launcher.split_first(p.get("command", ""))
        name = re.sub(r"[^\w\-]", "", p["name"].replace(" ", "")) or "Programm"
        work = Path(tempfile.gettempdir()) / "toolhub-build" / name
        py = scanner.python()
        windowed = "--windowed " if "pythonw" in exe.lower() else ""
        base = (f'"{py}" -m PyInstaller --noconfirm --onefile {windowed}--name {name} '
                f'--workpath "{work}" --specpath "{work}" --distpath dist')
        if kind in ("py", "pyw"):
            script = launcher.split_first(rest)[0]
            return f'{base} "{script}"'
        if kind == "modul":
            m = re.search(r"-m\s+([\w.]+)", rest)
            if not m:
                return ""
            pkg = m.group(1)
            work.mkdir(parents=True, exist_ok=True)
            entry = work / f"{name}_start.py"
            entry.write_text(f"import runpy\nrunpy.run_module({pkg!r}, run_name='__main__')\n", encoding="utf-8")
            return f'{base} --paths . --collect-submodules {pkg} "{entry}"'
        return ""

    def add_program(self):
        EditDialog(self.root, self, new_program(), new=True)

    def edit_selected(self):
        p = self.prog()
        if p:
            EditDialog(self.root, self, p, new=False)

    def remove_selected(self):
        p = self.prog()
        if not p:
            return
        extra = "\n\n(Automatisch gefunden – er wird beim Neu-Scannen nicht wieder hinzugefügt.)" if p.get("auto") else ""
        if messagebox.askyesno("Entfernen", f"„{p['name']}“ aus ToolHub entfernen?\n"
                               f"Die Dateien bleiben unverändert.{extra}", parent=self.root):
            self.cfg.remove(p["id"])
            self.cfg.save()
            self.selected = None
            self.refresh()

    def is_claude(self, p: dict) -> bool:
        return bool(p.get("path")) and os.path.isfile(os.path.join(p["path"], manifest.MANIFEST_NAME))

    def connect_claude(self):
        text = claude.connect_prompt()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("Prompt „Claude verbinden“ in die Zwischenablage kopiert.")
        TextWindow(self.root, "Claude verbinden",
                   "Dieser Prompt ist in der Zwischenablage. Füge ihn in Claude (Code oder Desktop) ein:", text)

    def ask_claude(self):
        AskWindow(self.root)

    def open_settings(self):
        SettingsDialog(self.root, self)

    def on_close(self):
        self.cfg.settings["geometry"] = self.root.geometry()
        self.cfg.save()
        self.root.destroy()


# ---------------------------------------------------------------------- Dialoge
class EditDialog(tk.Toplevel):
    def __init__(self, master, app: App, prog: dict, new: bool):
        super().__init__(master)
        self.app, self.prog, self.new = app, prog, new
        self.title("Programm hinzufügen" if new else f"Bearbeiten – {prog['name']}")
        self.transient(master)
        self.resizable(True, False)
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        self.vars: dict[str, tk.Variable] = {}
        row = 0

        def field(label, key, browse=None):
            nonlocal row
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=prog.get(key, ""))
            self.vars[key] = v
            ttk.Entry(f, textvariable=v, width=60).grid(row=row, column=1, sticky="ew", pady=3)
            if browse:
                ttk.Button(f, text="…", width=3, command=browse).grid(row=row, column=2, padx=(4, 0))
            row += 1

        field("Name", "name")
        field("Ordner", "path", self._browse_folder)
        field("Startbefehl", "command", self._browse_file)
        field("Arbeitsverzeichnis", "cwd")
        ttk.Label(f, text="Kategorie").grid(row=row, column=0, sticky="w", pady=3)
        self.vars["category"] = tk.StringVar(value=prog.get("category") or "Sonstiges")
        cb = ttk.Combobox(f, textvariable=self.vars["category"], values=CATEGORIES, state="readonly")
        cb.grid(row=row, column=1, sticky="w", pady=3)
        cb.bind("<<ComboboxSelected>>", self._cat_changed)
        row += 1
        field("Symbol (Emoji)", "icon")
        field("Build-Befehl", "build")
        ttk.Label(f, text="Beschreibung").grid(row=row, column=0, sticky="nw", pady=3)
        self.desc = tk.Text(f, width=60, height=4, wrap="word", font=FONT)
        self.desc.insert("1.0", prog.get("description", ""))
        self.desc.grid(row=row, column=1, sticky="ew", pady=3)
        row += 1
        checks = ttk.Frame(f)
        checks.grid(row=row, column=1, sticky="w", pady=6)
        for key, text in (("favorite", "Favorit"), ("admin", "Immer als Administrator starten"),
                          ("console", "Konsolenfenster anzeigen")):
            v = tk.BooleanVar(value=bool(prog.get(key)))
            self.vars[key] = v
            ttk.Checkbutton(checks, text=text, variable=v).pack(side="left", padx=(0, 12))
        row += 1
        ttk.Label(f, text="Tipp: Startbefehl wird im Ordner (bzw. Arbeitsverzeichnis) ausgeführt, "
                  "z. B.  cmd /c .\\start.bat  oder  pythonw app.py", foreground=COL_MUTED
                  ).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        b = ttk.Frame(f)
        b.grid(row=row, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right")
        ttk.Button(b, text="Speichern", command=self.save).pack(side="right", padx=6)
        f.columnconfigure(1, weight=1)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _cat_changed(self, _e=None):
        if not self.vars["icon"].get() or self.vars["icon"].get() in DEFAULT_ICONS.values():
            self.vars["icon"].set(DEFAULT_ICONS.get(self.vars["category"].get(), "📦"))

    def _browse_folder(self):
        d = filedialog.askdirectory(parent=self, initialdir=self.vars["path"].get() or None)
        if d:
            self.vars["path"].set(os.path.normpath(d))
            if not self.vars["name"].get():
                self.vars["name"].set(os.path.basename(d))

    def _browse_file(self):
        fn = filedialog.askopenfilename(parent=self, initialdir=self.vars["path"].get() or None,
                                        filetypes=[("Programme", "*.exe *.bat *.cmd *.py *.pyw"), ("Alle", "*.*")])
        if not fn:
            return
        fn = os.path.normpath(fn)
        folder = self.vars["path"].get() or os.path.dirname(fn)
        self.vars["path"].set(folder)
        try:
            rel = os.path.relpath(fn, folder)
        except ValueError:
            rel = fn
        q = scanner.quote
        ext = os.path.splitext(fn)[1].lower()
        if ext in (".bat", ".cmd"):
            cmd, kind = f"cmd /c {q(scanner.bat_rel(rel))}", "bat"
        elif ext == ".pyw":
            cmd, kind = f"{q(scanner.pythonw())} {q(rel)}", "pyw"
        elif ext == ".py":
            cmd, kind = f"{q(scanner.python())} {q(rel)}", "py"
        else:
            cmd, kind = q(rel), "exe"
        self.vars["command"].set(cmd)
        self.prog["kind"] = kind
        if not self.vars["name"].get():
            self.vars["name"].set(scanner.pretty_name(Path(fn).stem))

    def save(self):
        data = {k: v.get() for k, v in self.vars.items()}
        data["name"] = data["name"].strip()
        data["description"] = self.desc.get("1.0", "end").strip()
        if not data["name"]:
            messagebox.showerror("Fehlt", "Bitte einen Namen eingeben.", parent=self)
            return
        if data["path"] and not os.path.isdir(data["path"]):
            messagebox.showerror("Ordner", "Der Ordner existiert nicht.", parent=self)
            return
        self.prog.update(data)
        self.prog["edited"] = True
        if self.prog.get("kind") in ("projekt", "mod") and data["command"]:
            self.prog["kind"] = "manuell"
        if self.new:
            self.prog["id"] = f"manuell|{time.time_ns()}"
            self.app.cfg.programs.append(self.prog)
            self.app.selected = self.prog["id"]
        self.app.cfg.save()
        self.app.refresh()
        self.destroy()


class SettingsDialog(tk.Toplevel):
    def __init__(self, master, app: App):
        super().__init__(master)
        self.app = app
        s = app.cfg.settings
        self.title("Einstellungen")
        self.transient(master)
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Ordner, in denen nach Projekten gesucht wird (einer pro Zeile):").pack(anchor="w")
        self.roots = tk.Text(f, width=70, height=4, font=FONT)
        self.roots.insert("1.0", "\n".join(s.get("scan_roots", [])))
        self.roots.pack(fill="x", pady=(2, 8))
        self.v_scan = tk.BooleanVar(value=s.get("scan_on_start", True))
        self.v_all = tk.BooleanVar(value=s.get("show_without_start", False))
        self.v_auto = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(f, text="Beim Start automatisch nach neuen Projekten suchen", variable=self.v_scan).pack(anchor="w")
        ttk.Checkbutton(f, text="Projekte ohne Startpunkt anzeigen (z. B. Spiel-Mods, Web-Projekte)",
                        variable=self.v_all).pack(anchor="w")
        ttk.Checkbutton(f, text="ToolHub beim Windows-Login starten", variable=self.v_auto).pack(anchor="w")
        ttk.Label(f, text=f"Einstellungsdatei: {app.cfg.path}", foreground=COL_MUTED).pack(anchor="w", pady=(10, 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(10, 0))
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="right")
        ttk.Button(b, text="Speichern", command=self.save).pack(side="right", padx=6)
        ttk.Button(b, text="Einstellungsordner öffnen",
                   command=lambda: open_path(str(app.cfg.path.parent))).pack(side="left")
        self.grab_set()

    def save(self):
        s = self.app.cfg.settings
        old_roots = s.get("scan_roots", [])
        s["scan_roots"] = [r.strip() for r in self.roots.get("1.0", "end").splitlines() if r.strip()]
        s["scan_on_start"] = self.v_scan.get()
        s["show_without_start"] = self.v_all.get()
        if self.v_auto.get() != autostart.is_enabled():
            try:
                autostart.set_enabled(self.v_auto.get())
            except OSError as e:
                messagebox.showerror("Autostart", f"Konnte Autostart nicht ändern:\n{e}", parent=self)
        self.app.cfg.save()
        self.destroy()
        if s["scan_roots"] != old_roots:
            self.app.rescan()
        else:
            self.app.refresh()


class ReadmeWindow(tk.Toplevel):
    def __init__(self, master, name: str, path: Path):
        super().__init__(master)
        self.title(f"README – {name}")
        self.geometry("760x620")
        t = tk.Text(self, wrap="word", font=FONT, padx=14, pady=10, relief="flat")
        sb = ttk.Scrollbar(self, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        t.tag_configure("h1", font=("Segoe UI", 16, "bold"), spacing3=6)
        t.tag_configure("h2", font=("Segoe UI", 13, "bold"), spacing1=8, spacing3=4)
        t.tag_configure("h3", font=("Segoe UI", 11, "bold"), spacing1=6)
        t.tag_configure("code", font=("Consolas", 9), background="#f3f4f6")
        in_code = False
        text = scanner.read_text(path, 1_000_000)
        for line in text.splitlines():
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                t.insert("end", line + "\n", "code")
            elif line.startswith("### "):
                t.insert("end", line[4:] + "\n", "h3")
            elif line.startswith("## "):
                t.insert("end", line[3:] + "\n", "h2")
            elif line.startswith("# "):
                t.insert("end", line[2:] + "\n", "h1")
            else:
                t.insert("end", re.sub(r"\*\*(.+?)\*\*", r"\1", line) + "\n")
        t.configure(state="disabled")
        ttk.Button(self, text="Im Editor öffnen", command=lambda: open_path(str(path))).pack(side="bottom", anchor="e")


class OutputWindow(tk.Toplevel):
    """Führt einen Befehl aus und zeigt die Ausgabe live an."""

    def __init__(self, master, title: str, command: str, cwd: str, on_done=None):
        super().__init__(master)
        self.title(title)
        self.geometry("820x480")
        self.on_done = on_done
        self.q: queue.Queue = queue.Queue()
        self.text = tk.Text(self, wrap="none", font=("Consolas", 9), bg="#111827", fg="#e5e7eb")
        sb = ttk.Scrollbar(self, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self.text.insert("end", f"> {command}\n  (in {cwd})\n\n")
        threading.Thread(target=self._run, args=(command, cwd), daemon=True).start()
        self.after(100, self._poll)

    def _run(self, command, cwd):
        try:
            rc = launcher.run_captured(command, cwd, self.q.put)
        except OSError as e:
            self.q.put(f"Fehler: {e}")
            rc = -1
        self.q.put(("__done__", rc))

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple):
                    rc = item[1]
                    self.text.insert("end", f"\n{'✓ Fertig' if rc == 0 else f'✗ Beendet mit Code {rc}'}\n")
                    self.text.see("end")
                    if self.on_done:
                        self.on_done(rc)
                    return
                self.text.insert("end", item + "\n")
                self.text.see("end")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)


class TextWindow(tk.Toplevel):
    """Zeigt einen (kopierbaren) Text an."""

    def __init__(self, master, title: str, intro: str, text: str):
        super().__init__(master)
        self.title(title)
        self.geometry("820x560")
        ttk.Label(self, text=intro, padding=(10, 8)).pack(anchor="w")
        t = tk.Text(self, wrap="word", font=("Consolas", 9), padx=10, pady=8)
        t.insert("1.0", text)
        t.pack(fill="both", expand=True, padx=10)
        b = ttk.Frame(self, padding=8)
        b.pack(fill="x")
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="right")
        ttk.Button(b, text="Nochmal kopieren", command=lambda: (self.clipboard_clear(),
                                                                 self.clipboard_append(text))).pack(side="right", padx=6)


class AskWindow(tk.Toplevel):
    """„Claude fragen“: Frage per claude-CLI mit dem ToolHub-MCP-Server beantworten lassen."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Claude fragen")
        self.geometry("760x560")
        self.q: queue.Queue = queue.Queue()
        cli = claude.find_cli()
        info = (f"Claude beantwortet die Frage mit Zugriff auf deine Tools (nur lesende Befehle). CLI: {cli}"
                if cli else "Die claude-CLI wurde nicht gefunden – nutze „Claude verbinden“ und den kopierten Prompt.")
        ttk.Label(self, text=info, padding=(10, 8), wraplength=720, foreground=COL_MUTED).pack(anchor="w")
        top = ttk.Frame(self, padding=(10, 0))
        top.pack(fill="x")
        self.entry = tk.Text(top, height=3, wrap="word", font=FONT)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.insert("1.0", "Welche Programme habe ich und was können sie?")
        self.btn = ttk.Button(top, text="Senden", command=self.send)
        self.btn.pack(side="left", padx=(8, 0), fill="y")
        self.entry.bind("<Control-Return>", lambda e: (self.send(), "break")[1])
        self.out = tk.Text(self, wrap="word", font=FONT, padx=10, pady=8, state="disabled")
        self.out.pack(fill="both", expand=True, padx=10, pady=10)
        if not cli:
            self.btn.configure(state="disabled")

    def _write(self, text: str):
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("1.0", text)
        self.out.configure(state="disabled")

    def send(self):
        question = self.entry.get("1.0", "end").strip()
        if not question:
            return
        self.btn.configure(state="disabled")
        self._write("Claude denkt nach … (kann eine Minute dauern)")
        threading.Thread(target=lambda: self.q.put(claude.ask(question)), daemon=True).start()
        self.after(200, self._poll)

    def _poll(self):
        try:
            ok, text = self.q.get_nowait()
        except queue.Empty:
            if self.winfo_exists():
                self.after(200, self._poll)
            return
        self._write(text if ok else f"⚠ {text}")
        self.btn.configure(state="normal")


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ToolHub")
        except (AttributeError, OSError):
            pass
    cfg = Config().load()
    root = tk.Tk()
    App(root, cfg)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
