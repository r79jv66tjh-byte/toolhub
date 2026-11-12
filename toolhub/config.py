"""Einstellungen und Programmliste als JSON unter %APPDATA%\\ToolHub."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CATEGORIES = [
    "Aufräumen & Ordnung",
    "Spiele & Mods",
    "Daten & Dateien untersuchen",
    "Sicherheit & Backup",
    "Trainer",
    "Laufwerke & USB",
    "Sonstiges",
]

DEFAULT_ICONS = {
    "Laufwerke & USB": "💾",
    "Aufräumen & Ordnung": "🧹",
    "Spiele & Mods": "🎮",
    "Daten & Dateien untersuchen": "🔍",
    "Sicherheit & Backup": "🛡",
    "Trainer": "🎯",
    "Sonstiges": "📦",
}

DEFAULT_SETTINGS = {
    # Tools liegen in GIT\Claude\Tools; GIT\Claude bleibt als Fallback (gezeigt werden nur Ordner mit toolhub.json)
    "scan_roots": [str(Path.home() / "GIT" / "Claude" / "Tools"), str(Path.home() / "GIT" / "Claude")],
    "only_tools": True,           # nur Ordner mit toolhub.json anzeigen
    "view": "kacheln",            # "kacheln" oder "liste"
    "scan_on_start": True,
    "show_without_start": False,  # Projekte ohne Startpunkt (z. B. Mods) anzeigen
    "geometry": "1100x700",
}


def config_dir() -> Path:
    base = os.environ.get("TOOLHUB_HOME") or os.path.join(
        os.environ.get("APPDATA") or str(Path.home()), "ToolHub")
    return Path(base)


def config_path() -> Path:
    return config_dir() / "config.json"


def new_program(**kw) -> dict:
    """Ein Programm-Eintrag mit allen Feldern (Standardwerte)."""
    prog = {
        "id": "",
        "name": "",
        "path": "",          # Projektordner
        "command": "",       # Startbefehl (Befehlszeile)
        "cwd": "",           # Arbeitsverzeichnis, leer = path
        "kind": "manuell",   # bat / pyw / py / modul / exe / mod / manuell
        "category": "Sonstiges",
        "icon": "",
        "description": "",
        "readme": "",
        "favorite": False,
        "admin": False,
        "console": False,    # Konsolenfenster anzeigen
        "auto": False,       # vom Scanner gefunden
        "edited": False,     # vom Nutzer bearbeitet -> Scanner überschreibt nicht
        "build": "",         # eigener Build-Befehl (z. B. build.bat)
        "claude": False,     # toolhub.json vorhanden -> Claude-fähig (MCP)
    }
    prog.update(kw)
    return prog


class Config:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config_path()
        self.settings: dict = dict(DEFAULT_SETTINGS)
        self.programs: list[dict] = []
        self.ignored: list[str] = []   # IDs entfernter Scanner-Einträge

    def load(self) -> "Config":
        self._migrate_old()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self
        self.settings.update(data.get("settings", {}))
        self.programs = [new_program(**p) for p in data.get("programs", [])]
        self.ignored = list(data.get("ignored", []))
        return self

    def _migrate_old(self) -> None:
        """Einstellungen vom früheren Namen „ToolManager“ übernehmen (einmalig)."""
        old = self.path.parent.parent / "ToolManager" / "config.json"
        if self.path.exists() or self.path.parent.name != "ToolHub" or not old.is_file():
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = old.read_text(encoding="utf-8")
            self.path.write_text(text.replace("ToolManager", "ToolHub"), encoding="utf-8")
        except OSError:
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"settings": self.settings, "programs": self.programs,
                "ignored": self.ignored}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self, pid: str) -> dict | None:
        return next((p for p in self.programs if p["id"] == pid), None)

    def remove(self, pid: str) -> None:
        prog = self.get(pid)
        if prog is None:
            return
        self.programs.remove(prog)
        if prog.get("auto") and pid not in self.ignored:
            self.ignored.append(pid)

    def merge_scan(self, found: list[dict]) -> tuple[int, int, int]:
        """Scanner-Ergebnis einarbeiten. Gibt (neu, aktualisiert, verschwunden) zurück.

        Vom Nutzer bearbeitete Einträge behalten ihre Felder; Favorit/Admin
        bleiben bei allen Einträgen erhalten. Vom Nutzer entfernte Einträge kommen
        nicht wieder (Liste ``ignored``). Automatisch gefundene, nicht bearbeitete
        Einträge, deren Startpunkt verschwunden ist (Ordner umbenannt/gelöscht),
        fliegen raus."""
        found_ids = {f["id"] for f in found}
        before = len(self.programs)
        self.programs = [p for p in self.programs
                         if not p.get("auto") or p.get("edited") or p["id"] in found_ids]
        gone = before - len(self.programs)
        new = updated = 0
        for f in found:
            if f["id"] in self.ignored:
                continue
            old = self.get(f["id"])
            if old is None:
                self.programs.append(new_program(**f))
                new += 1
            elif not old.get("edited"):
                keep_keys = ["favorite", "admin", "console"]
                if not f.get("category_fixed"):  # Kategorie aus toolhub.json hat Vorrang
                    keep_keys += ["category", "icon"]
                keep = {k: old[k] for k in keep_keys if k in old}
                old.update(f)
                old.update(keep)
                updated += 1
        return new, updated, gone
