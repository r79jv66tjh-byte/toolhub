"""Store-Fenster: Tools installieren (mit Python oder als .exe), aktualisieren, entfernen."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import store

MODE_LABEL = {"python": "mit Python", "exe": "als .exe"}


class StoreWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("ToolHub Store")
        self.geometry("1000x560")
        self.minsize(760, 420)
        self.catalog: list[dict] = []
        self.latest: dict[str, str] = {}
        self.q: queue.Queue = queue.Queue()
        self.busy = False

        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Tools aus GitHub installieren und aktuell halten", font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(top, text="⟳ Auf Updates prüfen", command=self.reload).pack(side="right")

        cols = (("name", "Tool", 140), ("cat", "Kategorie", 170), ("inst", "Installiert", 150),
                ("latest", "Neueste", 90), ("desc", "Beschreibung", 420))
        self.tv = ttk.Treeview(self, columns=[c[0] for c in cols], show="headings", selectmode="browse")
        for c, t, w in cols:
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor="w", stretch=c == "desc")
        self.tv.tag_configure("update", foreground="#b45309")
        self.tv.tag_configure("ok", foreground="#15803d")
        self.tv.pack(fill="both", expand=True, padx=10)
        self.tv.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())

        self.note = ttk.Label(self, text="", foreground="#6b7280", wraplength=960, padding=(10, 4))
        self.note.pack(fill="x")
        bar = ttk.Frame(self, padding=(10, 4, 10, 4))
        bar.pack(fill="x")
        self.b_py = ttk.Button(bar, text="⬇ Installieren (mit Python)", command=lambda: self.install("python"))
        self.b_exe = ttk.Button(bar, text="⬇ Installieren (als .exe)", command=lambda: self.install("exe"))
        self.b_upd = ttk.Button(bar, text="⟳ Aktualisieren", command=self.update_selected)
        self.b_all = ttk.Button(bar, text="Alle aktualisieren", command=self.update_all)
        self.b_rm = ttk.Button(bar, text="✕ Deinstallieren", command=self.uninstall)
        for b in (self.b_py, self.b_exe, self.b_upd, self.b_all, self.b_rm):
            b.pack(side="left", padx=(0, 6))
        self.pb = ttk.Progressbar(self, maximum=1.0)
        self.pb.pack(fill="x", padx=10)
        self.status = ttk.Label(self, text="", padding=(10, 4, 10, 8))
        self.status.pack(fill="x")
        ttk.Label(self, text="„mit Python“: alle Funktionen inkl. Claude-Schnittstelle (braucht Python). "
                             "„als .exe“: läuft ohne Python, ohne Claude-Schnittstelle. "
                             "Jeder Download wird per SHA-256 geprüft.",
                  foreground="#6b7280", padding=(10, 0, 10, 8)).pack(fill="x")
        self.after(100, self._drain)
        self.reload()

    # ---------------------------------------------------------------- Hintergrund
    def _bg(self, work, done=None):
        if self.busy:
            return
        self.busy = True
        self._update_buttons()

        def run():
            try:
                res, err = work(), None
            except Exception as e:  # noqa: BLE001
                res, err = None, e
            self.q.put(("done", (done, res, err)))
        threading.Thread(target=run, daemon=True).start()

    def _drain(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.status.config(text=val)
                elif kind == "p":
                    self.pb["value"] = val
                else:
                    done, res, err = val
                    self.busy = False
                    self.pb["value"] = 0
                    if err:
                        self.status.config(text=f"Fehler: {err}")
                        messagebox.showerror("Store", str(err), parent=self)
                    elif done:
                        done(res)
                    self.render()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._drain)

    def log(self, text):
        self.q.put(("log", text))

    def prog(self, f):
        self.q.put(("p", f))

    # ---------------------------------------------------------------- Daten
    def reload(self):
        self.status.config(text="Lade Katalog und Versionen von GitHub …")

        def work():
            cat = store.load_catalog()
            latest = {}
            for e in cat:
                try:
                    latest[e["name"]] = store.latest_release(e["repo"])["version"]
                except store.StoreError:
                    latest[e["name"]] = "–"
            return cat, latest

        def done(res):
            self.catalog, self.latest = res
            n = sum(1 for e in self.catalog if self._has_update(e["name"]))
            self.status.config(text=f"{len(self.catalog)} Tools im Katalog" + (f" · {n} Update(s) verfügbar" if n else ""))
        self._bg(work, done)

    def _has_update(self, name):
        inst = store.installed(name)
        latest = self.latest.get(name)
        return bool(inst and latest and latest != "–" and inst.get("version") != latest)

    def render(self):
        sel = self.tv.selection()
        self.tv.delete(*self.tv.get_children())
        for e in self.catalog:
            inst = store.installed(e["name"])
            itxt = f'{inst["version"]} ({MODE_LABEL.get(inst.get("mode"), "?")})' if inst else "–"
            tag = "update" if self._has_update(e["name"]) else ("ok" if inst else "")
            latest = self.latest.get(e["name"], "…")
            if tag == "update":
                latest = f"⬆ {latest}"
            self.tv.insert("", "end", iid=e["name"], tags=(tag,),
                           values=(e["name"], e["category"], itxt, latest, e["description"]))
        if sel and self.tv.exists(sel[0]):
            self.tv.selection_set(sel[0])
        self._update_buttons()

    def _entry(self):
        sel = self.tv.selection()
        return next((e for e in self.catalog if sel and e["name"] == sel[0]), None)

    def _update_buttons(self):
        e = self._entry()
        inst = store.installed(e["name"]) if e else None
        st = lambda ok: "normal" if ok and not self.busy else "disabled"  # noqa: E731
        self.b_py.config(state=st(e and "python" in e.get("modes", [])))
        self.b_exe.config(state=st(e and "exe" in e.get("modes", [])))
        self.b_upd.config(state=st(e and self._has_update(e["name"])))
        self.b_all.config(state=st(any(self._has_update(x["name"]) for x in self.catalog)))
        self.b_rm.config(state=st(bool(inst)))
        note = (e or {}).get("note", "")
        if e and e.get("admin"):
            note = (note + " " if note else "") + "Braucht Administratorrechte."
        self.note.config(text=note)

    # ---------------------------------------------------------------- Aktionen
    def _running(self, name):
        return any(self.app.is_running(p["id"]) for p in self.app.cfg.programs
                   if p.get("path", "").rstrip("\\/").lower() == str(store.apps_dir() / name).lower())

    def install(self, mode, entry=None):
        e = entry or self._entry()
        if not e:
            return
        if self._running(e["name"]):
            messagebox.showinfo("Store", f"{e['name']} läuft gerade – bitte erst beenden.", parent=self)
            return
        inst = store.installed(e["name"])
        if inst and inst.get("mode") != mode and not messagebox.askyesno(
                "Store", f"{e['name']} ist {MODE_LABEL[inst['mode']]} installiert. "
                         f"Auf „{MODE_LABEL[mode]}“ umstellen?", parent=self):
            return
        self._bg(lambda: store.install(e, mode, self.prog, self.log), lambda _r: self._after_change())

    def update_selected(self):
        e = self._entry()
        inst = store.installed(e["name"]) if e else None
        if inst:
            self.install(inst.get("mode", "python"), e)

    def update_all(self):
        todo = [(e, store.installed(e["name"])) for e in self.catalog if self._has_update(e["name"])]

        def work():
            done = []
            for e, inst in todo:
                if not self._running(e["name"]):
                    store.install(e, inst.get("mode", "python"), self.prog, self.log)
                    done.append(e["name"])
            return done
        self._bg(work, lambda names: (self.log(f"Aktualisiert: {', '.join(names) or 'nichts'}"), self._after_change()))

    def uninstall(self):
        e = self._entry()
        if not e or not messagebox.askyesno("Store", f"{e['name']} deinstallieren?\n"
                                                     "Einstellungen des Tools bleiben erhalten.", parent=self):
            return
        if self._running(e["name"]):
            messagebox.showinfo("Store", f"{e['name']} läuft gerade – bitte erst beenden.", parent=self)
            return
        self._bg(lambda: store.uninstall(e["name"]),
                 lambda _r: (self.log(f"{e['name']} entfernt."), self._after_change()))

    def _after_change(self):
        self.app.rescan(quiet=True)
