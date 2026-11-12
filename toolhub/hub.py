"""Gemeinsame Sicht auf alle Tools für den MCP-Server.

Ein „Tool“ ist ein Projektordner unter den Scan-Ordnern, der entweder eine
``toolhub.json`` hat oder einen erkannten Startpunkt. Name und Beschreibung kommen
aus der ``toolhub.json``, sonst aus dem Scanner.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import launcher, manifest, scanner
from .config import Config

START_PRIORITY = {"bat": 0, "pyw": 1, "modul": 2, "py": 3, "exe": 4, "manuell": 5}


@dataclass
class Tool:
    name: str
    folder: Path
    description: str = ""
    manifest: manifest.Manifest | None = None
    entries: list[dict] = field(default_factory=list)   # Scanner-Startpunkte

    @property
    def start_entry(self) -> dict | None:
        if self.manifest and self.manifest.start:
            return {"id": f"{self.folder}|toolhub.json", "name": self.name, "path": str(self.folder),
                    "command": " ".join(f'"{x}"' if " " in x else x for x in self._start_argv()),
                    "kind": "manifest", "console": False}
        cands = [e for e in self.entries if e.get("command")]
        cands.sort(key=lambda e: START_PRIORITY.get(e.get("kind"), 9))
        return cands[0] if cands else None

    def _start_argv(self) -> list[str]:
        argv = list(self.manifest.start) if self.manifest and self.manifest.start else []
        if argv and argv[0].lower() in ("python", "pythonw"):
            argv[0] = scanner.pythonw() if argv[0].lower() == "pythonw" else scanner.python()
        return argv

    def script_names(self) -> set[str]:
        """Dateinamen, an denen sich ein laufendes Programm erkennen lässt."""
        names = set()
        for e in self.entries:
            rel = e["id"].split("|", 1)[1] if "|" in e["id"] else ""
            if rel.endswith((".py", ".pyw", ".exe")):
                names.add(Path(rel).name.lower())
            for m in re.findall(r"([\w.\-]+\.pyw?)\b", e.get("command", ""), re.I):
                names.add(m.lower())
            m = re.search(r"-m\s+([\w.]+)", e.get("command", ""))
            if m:
                names.add(f"-m {m.group(1).lower()}")
        if self.manifest and self.manifest.start:
            names |= {Path(x).name.lower() for x in self.manifest.start[1:] if x.endswith((".py", ".pyw"))}
        names.discard("toolhub_mcp.py")
        return names


class Hub:
    def __init__(self, roots: list[str] | None = None):
        settings = Config().load().settings
        from .store import apps_dir  # vom Store installierte Tools immer mit einbeziehen
        self.roots = roots or list(dict.fromkeys([str(apps_dir())] + list(settings.get("scan_roots") or [])))
        self.only_tools = settings.get("only_tools", True)
        self.running: dict[str, list[launcher.Running]] = {}
        self._proc_cache: tuple[float, list[dict]] = (0.0, [])

    # ------------------------------------------------------------- Tools
    def tools(self) -> list[Tool]:
        by_folder: dict[str, Tool] = {}
        for m in manifest.find_manifests(self.roots):
            key = str(m.folder.resolve()).lower()
            by_folder[key] = Tool(name=m.name, folder=m.folder, description=m.description, manifest=m)
        for e in scanner.scan_roots(self.roots, self.only_tools):
            key = str(Path(e["path"]).resolve()).lower()
            t = by_folder.get(key)
            if t is None:
                if not e.get("command"):
                    continue
                t = Tool(name=Path(e["path"]).name, folder=Path(e["path"]), description=e.get("description", ""))
                by_folder[key] = t
            t.entries.append(e)
            if not t.description:
                t.description = e.get("description", "")
        return sorted(by_folder.values(), key=lambda t: t.name.lower())

    def find(self, name: str) -> Tool:
        if not name:
            raise manifest.ManifestError("Parameter „tool“ fehlt.")
        n = re.sub(r"[^a-z0-9]", "", str(name).lower())
        tools = self.tools()
        for t in tools:
            if n in (re.sub(r"[^a-z0-9]", "", t.name.lower()), re.sub(r"[^a-z0-9]", "", t.folder.name.lower())):
                return t
        raise manifest.ManifestError(f"Tool „{name}“ nicht gefunden. Verfügbar: {', '.join(t.name for t in tools)}")

    # ------------------------------------------------------------ Laufend?
    def _processes(self) -> list[dict]:
        ts, procs = self._proc_cache
        if time.time() - ts > 3:
            procs = launcher.process_commandlines()
            self._proc_cache = (time.time(), procs)
        return procs

    def running_pids(self, t: Tool) -> list[int]:
        pids: set[int] = set()
        for r in self.running.get(str(t.folder).lower(), []):
            pids |= set(r.pids())
        folder = str(t.folder.resolve()).lower()
        scripts = t.script_names()
        me = os.getpid()
        for p in self._processes():
            if p["pid"] == me:
                continue
            exe, cmd = p["exe"].lower(), p["cmdline"].lower()
            if "toolhub_mcp.py" in cmd:
                continue
            if exe.startswith(folder + os.sep):
                pids.add(p["pid"])
                continue
            is_py = os.path.basename(exe) in ("python.exe", "pythonw.exe", "py.exe", "pyw.exe")
            if is_py and (folder in cmd or any(_has_token(cmd, s) for s in scripts)):
                pids.add(p["pid"])
        return sorted(pids)

    # ------------------------------------------------------------ Aktionen
    def start(self, t: Tool) -> dict:
        entry = t.start_entry
        if not entry:
            raise manifest.ManifestError(f"{t.name} hat keinen Startbefehl.")
        r = launcher.launch(dict(entry, admin=False))
        self.running.setdefault(str(t.folder).lower(), []).append(r)
        self._proc_cache = (0.0, [])
        time.sleep(0.5)
        return {"started": True, "tool": t.name, "command": entry["command"], "pids": r.pids()}

    def stop(self, t: Tool) -> dict:
        self._proc_cache = (0.0, [])
        pids = self.running_pids(t)
        if not pids:
            return {"stopped": False, "tool": t.name, "message": "läuft nicht"}
        for r in self.running.pop(str(t.folder).lower(), []):
            r.stop()
            r.close()
        ok = launcher.stop_pids(pids)
        self._proc_cache = (0.0, [])
        return {"stopped": ok, "tool": t.name, "pids": pids}

    # ------------------------------------------------------------ Ausgaben
    def summary(self, t: Tool, running: bool | None = None) -> dict:
        d = {"name": t.name, "description": t.description, "folder": str(t.folder),
             "claude_ready": t.manifest is not None,
             "running": bool(self.running_pids(t)) if running is None else running,
             "can_start": t.start_entry is not None,
             "commands": [{"name": c.name, "description": c.description, "readonly": c.readonly,
                           "args": [a.name for a in c.args]} for c in (t.manifest.commands if t.manifest else [])]}
        if t.manifest and t.manifest.errors:
            d["manifest_errors"] = t.manifest.errors
        return d

    def info(self, t: Tool) -> dict:
        d = self.summary(t)
        d["running_pids"] = self.running_pids(t)
        d["start_command"] = (t.start_entry or {}).get("command", "")
        if t.manifest:
            d["manifest"] = str(t.manifest.path)
            d["commands"] = [{"name": c.name, "description": c.description, "readonly": c.readonly,
                              "args": manifest.json_schema(c)["properties"],
                              "required": manifest.json_schema(c).get("required", [])}
                             for c in t.manifest.commands]
        readme = scanner.find_readme(t.folder)
        if readme:
            d["readme"] = scanner.read_text(readme, 30_000)
        return d


def _has_token(cmdline: str, script: str) -> bool:
    """Skriptname als eigenes Wort in der Befehlszeile (nicht nur als Teilstring)."""
    if script.startswith("-m "):
        return re.search(rf"(^|\s)-m\s+{re.escape(script[3:])}(\s|$)", cmdline) is not None
    return re.search(rf'(^|[\s"\\/]){re.escape(script)}(["\s]|$)', cmdline) is not None
