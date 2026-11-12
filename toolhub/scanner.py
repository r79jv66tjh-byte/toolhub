"""Findet Programme in Projektordnern (nur lesend – es wird nichts verändert).

Erkannte Startpunkte pro Projektordner:
  * ``start.bat``                      -> Batch-Start (die darin gestartete .py wird
                                          nicht zusätzlich gelistet)
  * ``*.pyw`` im Projektordner          -> Python-GUI ohne Konsole
  * ``*.py`` mit ``if __name__ == "__main__"`` im Projektordner
  * Pakete ``<paket>/__main__.py``      -> ``python -m <paket>``
  * ``dist/*.exe``                      -> gebaute Programme
  * ``manifest.xml`` + ``bin/*.dll``    -> Spiel-Mod (kein Startbefehl)
Die Beschreibung kommt aus dem ersten Absatz der README.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from .config import DEFAULT_ICONS

SKIP_DIRS = {".git", "node_modules", "__pycache__", "build", "dist", "tests", "test",
             ".venv", "venv", "env", "bin", "obj", ".idea", ".vscode"}
README_NAMES = ("README.de.md", "README.md", "README.txt", "readme.md", "LIESMICH.md")
MAIN_RE = re.compile(r"""^if\s+__name__\s*==\s*['"]__main__['"]\s*:""", re.M)

CATEGORY_KEYWORDS = [
    ("Trainer", ("trainer", "scanner", "cheat", "speedhack", "speicher-scanner")),
    ("Laufwerke & USB", ("partition", "laufwerk", "datenträger", "usb", "iso", "flash", "diskformat", "drive")),
    ("Sicherheit & Backup", ("backup", "sicherung", "sicherheit", "security", "datenwacht", "safekeep", "schutz",
                             "passwort", "verschlüssel", "wacht")),
    ("Spiele & Mods", ("game", "spiel", "mod", "steam", "probably", "warden")),
    ("Aufräumen & Ordnung", ("sort", "aufräum", "ordnung", "clean", "tidy", "download", "duplikat",
                             "papierkorb")),
    ("Daten & Dateien untersuchen", ("daten", "inspect", "analy", "untersuch", "hash",
                                     "metadaten", "dateien")),
]


def _interpreter() -> Path:
    """Python-Interpreter; in der gebauten ToolHub.exe der aus dem PATH."""
    if getattr(sys, "frozen", False):
        import shutil
        found = shutil.which("python") or shutil.which("pythonw") or "python"
        return Path(found)
    return Path(sys.executable)


def pythonw() -> str:
    exe = _interpreter()
    cand = exe.with_name("pythonw.exe")
    return str(cand) if cand.exists() else str(exe)


def python() -> str:
    exe = _interpreter()
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) and not arg.startswith('"') else arg


def read_text(path: Path, limit: int = 200_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def find_readme(folder: Path) -> Path | None:
    for name in README_NAMES:
        p = folder / name
        if p.is_file():
            return p
    return None


def readme_summary(text: str, max_len: int = 220) -> str:
    """Erster normaler Absatz einer Markdown-README (ohne Überschriften/Code)."""
    para: list[str] = []
    in_code = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_code = not in_code
            if para:
                break
            continue
        if in_code:
            continue
        if not s:
            if para:
                break
            continue
        if s.startswith(("#", "|", "![", "<", "---", "===", ">")) or " | " in s:
            if para:
                break
            continue
        para.append(s)
    out = " ".join(para)
    out = re.sub(r"\*\*|__|`|(?<!\w)\*|\*(?!\w)", "", out)
    out = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", out)
    return out if len(out) <= max_len else out[: max_len - 1].rstrip() + "…"


def docstring_summary(path: Path, max_len: int = 220) -> str:
    """Erster Absatz des Modul-Docstrings einer .py/.pyw-Datei."""
    import ast
    try:
        doc = ast.get_docstring(ast.parse(read_text(path, 2_000_000))) or ""
    except (SyntaxError, ValueError):
        return ""
    para = doc.strip().split("\n\n", 1)[0]
    out = " ".join(line.strip() for line in para.splitlines())
    return out if len(out) <= max_len else out[: max_len - 1].rstrip() + "…"


# Feste Zuordnung der geplanten Programme (englische Namen, siehe README)
KNOWN_PROGRAMS = {
    "memtrainer": "Trainer",
    "downloadsorter": "Aufräumen & Ordnung",
    "leftoverfinder": "Aufräumen & Ordnung",
    "startupcontrol": "Aufräumen & Ordnung",
    "batchrenamer": "Aufräumen & Ordnung",
    "foldersync": "Aufräumen & Ordnung",
    "musicdupes": "Aufräumen & Ordnung",
    "librarystats": "Spiele & Mods",
    "gamemover": "Spiele & Mods",
    "screenshothub": "Spiele & Mods",
    "padprofiles": "Spiele & Mods",
    "fileidentifier": "Daten & Dateien untersuchen",
    "fileprobe": "Daten & Dateien untersuchen",
    "pakextractor": "Daten & Dateien untersuchen",
    "registrydiff": "Daten & Dateien untersuchen",
    "writewatcher": "Daten & Dateien untersuchen",
    "backupvault": "Sicherheit & Backup",
}


def manifest_category(folder: Path) -> str:
    """Kategorie aus toolhub.json ("category"), falls dort eine gültige angegeben ist."""
    import json
    from .config import CATEGORIES
    for fn in ("toolhub.json", INSTALL_FILE):
        try:
            data = json.loads((folder / fn).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        cat = data.get("category") if isinstance(data, dict) else None
        if cat in CATEGORIES:
            return cat
    return ""


def known_category(*names: str) -> str:
    for n in names:
        cat = KNOWN_PROGRAMS.get(re.sub(r"[^a-z0-9]", "", n.lower()))
        if cat:
            return cat
    return ""


def guess_category(*texts: str) -> str:
    blob = " ".join(texts).lower()
    for cat, words in CATEGORY_KEYWORDS:
        if any(w in blob for w in words):
            return cat
    return "Sonstiges"


def pretty_name(stem: str) -> str:
    s = stem.replace("_", " ").replace("-", " ").strip()
    if s.islower():
        s = " ".join(w.capitalize() for w in s.split())
    return s


GENERIC_STEMS = {"main", "app", "run", "start", "gui", "launcher", "program"}


def script_name(stem: str, folder: Path) -> str:
    """Anzeigename für ein Skript; bei Allerweltsnamen (main.py …) der Ordnername."""
    if stem.lower() in GENERIC_STEMS or re.sub(r"[^a-z0-9]", "", stem.lower()) == \
            re.sub(r"[^a-z0-9]", "", folder.name.lower()):
        return folder.name
    return pretty_name(stem)


def manifest_scripts(folder: Path) -> set[str]:
    """Dateinamen/Module, die in toolhub.json unter "run" verwendet werden (klein geschrieben)."""
    import json
    try:
        data = json.loads((folder / "toolhub.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return set()
    out: set[str] = set()
    # optional: "ignore": ["vorlage.py", …] – Skripte, die kein eigenes Programm sind
    for x in (data.get("ignore") or []) if isinstance(data, dict) else []:
        if isinstance(x, str):
            out.add(Path(x).name.lower())
    for c in data.get("commands") or [] if isinstance(data, dict) else []:
        run = c.get("run") if isinstance(c, dict) else None
        if not isinstance(run, list):
            continue
        for i, x in enumerate(run):
            if not isinstance(x, str):
                continue
            if x.lower().endswith((".py", ".pyw")):
                out.add(Path(x).name.lower())
            if x == "-m" and i + 1 < len(run) and isinstance(run[i + 1], str):
                out.add(run[i + 1].split(".")[0].lower())
    return out


def bat_rel(name: str) -> str:
    """Relativen Batch-Pfad mit Punkt-Backslash davor – sonst liest cmd z. B. "start.bat" als
    eingebauten Befehl START mit dem Argument ".bat"."""
    if os.path.isabs(name) or name.startswith((".\\", "./", "..")):
        return name
    return ".\\" + name


def bat_targets(text: str) -> set[str]:
    """Dateinamen (klein), die eine .bat startet – damit sie nicht doppelt erscheinen."""
    return {m.lower() for m in re.findall(r"([\w.\-]+\.pyw?)\b", text, re.I)} | {
        m.lower() for m in re.findall(r"-m\s+([\w.]+)", text)}


def _entry(folder: Path, rel: str, **kw) -> dict:
    pid = f"{folder.resolve()}|{rel}".lower()
    return {"id": pid, "path": str(folder), "auto": True, **kw}


def scan_project(folder: Path) -> list[dict]:
    folder = Path(folder)
    readme = find_readme(folder)
    readme_text = read_text(readme) if readme else ""
    desc = readme_summary(readme_text)
    entries: list[dict] = []
    covered: set[str] = set()
    pyw, py = pythonw(), python()
    # Skripte, die nur als Befehle für Claude dienen (toolhub.json "run"), sind keine Programme
    covered |= manifest_scripts(folder)

    def add(rel: str, name: str, command: str, kind: str, console: bool, extra_text: str = "",
            script: Path | None = None):
        own = docstring_summary(script) if script and script.is_file() else ""
        if kind == "pyw" and desc:
            own = ""  # .pyw sind meist nur Doppelklick-Starter -> README ist aussagekräftiger
        fixed = manifest_category(folder)
        cat = fixed or known_category(name, Path(rel).stem, folder.name) or \
            guess_category(folder.name, name, rel, extra_text, own or desc)
        entries.append(_entry(
            folder, rel, name=name, command=command, kind=kind, console=console,
            category=cat, icon=DEFAULT_ICONS.get(cat, "📦"), category_fixed=bool(fixed),
            description=own or desc, readme=str(readme) if readme else ""))

    # 1. start.bat
    for bat in sorted(folder.glob("start*.bat")):
        text = read_text(bat)
        targets = bat_targets(text)
        covered |= targets
        target_name = next((t for t in targets if t.endswith((".py", ".pyw"))), "")
        name = script_name(Path(target_name).stem, folder) if target_name else folder.name
        if bat.stem.lower() != "start":
            name = f"{name} ({bat.stem})"
        add(bat.name, name, f'cmd /c {quote(bat_rel(bat.name))}', "bat",
            console="pythonw" not in text.lower() and "start \"\"" not in text.lower(),
            extra_text=target_name, script=folder / target_name if target_name else None)

    # 2. *.pyw / *.py mit __main__ im Projektordner
    for f in sorted(folder.glob("*.py*")):
        if f.suffix.lower() not in (".py", ".pyw") or f.name.lower() in covered:
            continue
        if f.name.lower() in ("setup.py", "conftest.py", "build.py"):
            continue
        if f.suffix.lower() == ".pyw":
            add(f.name, script_name(f.stem, folder), f"{quote(pyw)} {quote(f.name)}", "pyw", False, script=f)
            covered.add(f.name.lower())
        else:
            src = read_text(f)
            if not MAIN_RE.search(src):
                continue
            gui = "tkinter" in src or "PySide" in src or "PyQt" in src
            add(f.name, script_name(f.stem, folder),
                f"{quote(pyw if gui else py)} {quote(f.name)}", "py", not gui, script=f)

    # .pyw, die ein Paket startet, deckt das Paket ab
    pyw_texts = " ".join(read_text(folder / e["id"].split("|", 1)[1])
                         for e in entries if e["kind"] in ("pyw", "bat"))

    # 3. Pakete mit __main__.py
    for main in sorted(folder.glob("*/__main__.py")):
        pkg = main.parent.name
        if pkg.lower() in SKIP_DIRS or pkg.lower() in covered:
            continue
        if re.search(rf"\b(from|import)\s+{re.escape(pkg)}\b", pyw_texts):
            continue
        gui = (main.parent / "gui.py").exists() or (main.parent / "gui").is_dir() \
            or "tkinter" in read_text(main)
        add(f"{pkg}/__main__.py", pretty_name(pkg) if pkg.lower() != folder.name.lower()
            else folder.name, f"{quote(pyw if gui else py)} -m {pkg}", "modul", not gui,
            script=main.parent / "__init__.py")

    # 4a. vom Store als .exe installiert: <Tool>.exe direkt im Ordner
    if (folder / INSTALL_FILE).is_file():
        for exe in sorted(folder.glob("*.exe")):
            add(exe.name, exe.stem, quote(exe.name), "exe", False)

    # 4. dist/*.exe
    norm = lambda n: re.sub(r"[^a-z0-9]", "", n.lower())  # noqa: E731
    for exe in sorted((folder / "dist").glob("*.exe")):
        src = next((f for f in folder.glob("*.py*") if norm(f.stem) == norm(exe.stem)), None)
        add(f"dist/{exe.name}", f"{pretty_name(exe.stem)} (.exe)",
            quote(str(Path("dist") / exe.name)), "exe", False, script=src)

    # 5. Spiel-Mod (z. B. Probably-Mods): manifest.xml + bin/*.dll
    if not entries and (folder / "manifest.xml").is_file() and list((folder / "bin").glob("*.dll")):
        entries.append(_entry(
            folder, "manifest.xml", name=folder.name, command="", kind="mod", console=False,
            category="Spiele & Mods", icon=DEFAULT_ICONS["Spiele & Mods"],
            description=desc or "Spiel-Mod (wird vom Spiel geladen, nicht direkt gestartet).",
            readme=str(readme) if readme else ""))

    # 6. Projekt ohne erkannten Startpunkt (wird nur auf Wunsch angezeigt)
    if not entries:
        cat = known_category(folder.name) or guess_category(folder.name, desc)
        entries.append(_entry(
            folder, "", name=folder.name, command="", kind="projekt", console=False,
            category=cat, icon=DEFAULT_ICONS.get(cat, "📦"), description=desc,
            readme=str(readme) if readme else ""))

    # eigener Build-Befehl
    build = ""
    if (folder / "build.bat").is_file():
        build = r"cmd /c .\build.bat"
    elif (folder / "build.ps1").is_file():
        build = "powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1"
    claude = (folder / "toolhub.json").is_file()
    for e in entries:
        e["build"] = build
        e["claude"] = claude   # hat eine toolhub.json -> per MCP für Claude nutzbar
    return entries


INSTALL_FILE = ".toolhub-install.json"   # schreibt der Store bei jeder Installation


def is_tool(folder: Path) -> bool:
    """Ein Ordner gilt als Tool, wenn er eine toolhub.json hat (Claude-Schnittstelle)
    oder vom Store installiert wurde."""
    return (folder / "toolhub.json").is_file() or (folder / INSTALL_FILE).is_file()


def scan_roots(roots: list[str], only_tools: bool = True) -> list[dict]:
    """Alle Projektordner direkt unterhalb der Wurzeln durchsuchen.

    only_tools: nur Ordner mit toolhub.json (echte Tools) – andere Projekte werden ausgeblendet.
    """
    found: list[dict] = []
    seen: set[Path] = set()
    me = Path(__file__).resolve().parent.parent
    for root in roots:
        r = Path(os.path.expandvars(root)).expanduser()
        if not r.is_dir():
            continue
        for folder in sorted(r.iterdir(), key=lambda p: p.name.lower()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            res = folder.resolve()
            if res == me or res in seen:
                continue  # ToolHub selbst nicht listen, keine Doppelten
            seen.add(res)
            if only_tools and not is_tool(folder):
                continue
            try:
                found.extend(scan_project(folder))
            except OSError:
                continue
    return found
