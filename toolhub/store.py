"""Store: Tools aus GitHub-Releases installieren, aktualisieren und entfernen.

Jedes Tool-Release enthält (gebaut von .github/build_release.py):
    <Name>.exe, <Name>-source.zip, manifest.json (Version + SHA-256)
Installationsarten:
    "python"  Quellcode + Python (alle Funktionen inkl. Claude-Schnittstelle)
    "exe"     fertige .exe, läuft ohne Python (ohne Claude-Schnittstelle)
Ziel: %LOCALAPPDATA%\\ToolHub\\apps\\<Name>\\ mit .toolhub-install.json
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .scanner import INSTALL_FILE

OWNER = "r79jv66tjh-byte"
CATALOG_URL = f"https://raw.githubusercontent.com/{OWNER}/toolhub/main/catalog.json"
API = "https://api.github.com"
UA = {"User-Agent": "ToolHub-Store", "Accept": "application/vnd.github+json"}
CREATE_NO_WINDOW = 0x08000000


class StoreError(Exception):
    pass


def apps_dir() -> Path:
    base = os.environ.get("TOOLHUB_APPS") or os.path.join(
        os.environ.get("LOCALAPPDATA") or str(Path.home()), "ToolHub", "apps")
    return Path(base)


# ------------------------------------------------------------------ Netzwerk
def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 403 and "rate limit" in e.read().decode("utf-8", "replace").lower():
            raise StoreError("GitHub-Abfragelimit erreicht – bitte in einer Stunde erneut versuchen.") from e
        if e.code == 404:
            raise StoreError(f"Nicht gefunden: {url}") from e
        raise StoreError(f"HTTP {e.code} bei {url}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise StoreError(f"Keine Verbindung zu GitHub ({e})") from e


def _download(url: str, dest: Path, progress=None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise StoreError(f"Download fehlgeschlagen ({e})") from e


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ Katalog & Releases
def bundled_catalog() -> list[dict]:
    p = Path(__file__).resolve().parent.parent / "catalog.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))["tools"]
    except (OSError, ValueError, KeyError):
        return []


def load_catalog() -> list[dict]:
    """Aktueller Katalog von GitHub, bei Fehler der mitgelieferte."""
    try:
        return json.loads(_get(CATALOG_URL, timeout=15))["tools"]
    except (StoreError, ValueError, KeyError):
        return bundled_catalog()


def latest_release(repo: str) -> dict:
    data = json.loads(_get(f"{API}/repos/{OWNER}/{repo}/releases/latest"))
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
    if "manifest.json" not in assets:
        raise StoreError(f"Release {data.get('tag_name')} von {repo} hat kein manifest.json")
    manifest = json.loads(_get(assets["manifest.json"]))
    return {"version": data["tag_name"], "assets": assets, "manifest": manifest,
            "notes": data.get("body") or ""}


# ------------------------------------------------------------------ Installationen
def installed(name: str) -> dict | None:
    p = apps_dir() / name / INSTALL_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def installed_all() -> dict[str, dict]:
    out = {}
    root = apps_dir()
    if root.is_dir():
        for d in root.iterdir():
            info = installed(d.name)
            if info:
                out[d.name] = info
    return out


def find_python() -> str | None:
    """Python-Interpreter (nicht der eingefrorene ToolHub.exe)."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    for cand in ("py", "python"):
        exe = shutil.which(cand)
        if exe and "WindowsApps" not in exe:  # Store-Platzhalter ignorieren
            return exe
    return None


def _verify(path: Path, release: dict, fname: str) -> None:
    want = release["manifest"]["files"].get(fname, {}).get("sha256")
    if not want:
        raise StoreError(f"Keine Prüfsumme für {fname} im Release.")
    got = sha256(path)
    if got != want:
        raise StoreError(f"Prüfsumme von {fname} stimmt nicht – Download beschädigt oder manipuliert.")


def _replace_dir(new: Path, target: Path) -> None:
    """Zielordner atomar-ish ersetzen; bei Fehlern bleibt die alte Version erhalten."""
    old = target.with_name(target.name + ".old")
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        try:
            target.rename(old)
        except OSError as e:
            raise StoreError(f"{target.name} wird gerade benutzt – bitte das Tool erst beenden.") from e
    try:
        shutil.move(str(new), str(target))
    except OSError:
        if old.exists():
            old.rename(target)
        raise
    shutil.rmtree(old, ignore_errors=True)


def install(entry: dict, mode: str, progress=None, log=print) -> dict:
    """Installiert oder aktualisiert ein Tool. entry stammt aus dem Katalog."""
    name, repo = entry["name"], entry["repo"]
    if mode not in entry.get("modes", ["python", "exe"]):
        raise StoreError(f"{name} gibt es nicht als „{mode}“.")
    python = None
    if mode == "python":
        python = find_python()
        if not python:
            raise StoreError("Python wurde nicht gefunden. Installiere Python 3 oder wähle „als .exe“.")
    log(f"Suche neueste Version von {name} …")
    rel = latest_release(repo)
    fname = f"{name}-source.zip" if mode == "python" else f"{name}.exe"
    if fname not in rel["assets"]:
        raise StoreError(f"{fname} fehlt im Release {rel['version']}.")
    apps = apps_dir()
    apps.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=apps, prefix=".tmp-") as tmp:
        tmp = Path(tmp)
        dl = tmp / fname
        log(f"Lade {fname} ({rel['version']}) …")
        _download(rel["assets"][fname], dl, progress)
        _verify(dl, rel, fname)
        log("Prüfsumme OK")
        stage = tmp / "stage"
        if mode == "python":
            with zipfile.ZipFile(dl) as z:
                for m in z.namelist():  # Pfad-Ausbruch verhindern
                    if m.startswith("/") or ".." in Path(m).parts:
                        raise StoreError("Ungültiger Pfad im Archiv.")
                z.extractall(tmp / "x")
            inner = tmp / "x" / name
            src = inner if inner.is_dir() else tmp / "x"
            shutil.move(str(src), str(stage))
            req = stage / "requirements.txt"
            if req.is_file() and any(l.strip() and not l.lstrip().startswith("#")
                                     for l in req.read_text(encoding="utf-8").splitlines()):
                log("Installiere Python-Pakete …")
                r = subprocess.run([python, "-m", "pip", "install", "--user", "--quiet", "-r", str(req)],
                                   capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                if r.returncode:
                    raise StoreError("pip install fehlgeschlagen:\n" + (r.stderr or r.stdout)[-800:])
        else:
            stage.mkdir()
            shutil.move(str(dl), str(stage / fname))
        info = {"name": name, "repo": repo, "version": rel["version"], "mode": mode,
                "category": entry.get("category", ""), "description": entry.get("description", ""),
                "installed_at": time.strftime("%Y-%m-%d %H:%M")}
        (stage / INSTALL_FILE).write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
        _replace_dir(stage, apps / name)
    log(f"{name} {rel['version']} installiert ({'mit Python' if mode == 'python' else 'als .exe'}).")
    return info


def uninstall(name: str) -> None:
    target = apps_dir() / name
    if not (target / INSTALL_FILE).is_file():
        raise StoreError(f"{name} ist nicht über den Store installiert.")
    try:
        shutil.rmtree(target)
    except OSError as e:
        raise StoreError(f"{name} wird gerade benutzt – bitte erst beenden.") from e


def check_updates(catalog: list[dict] | None = None) -> list[dict]:
    """Liste der installierten Tools mit neuerer Version: [{name, installed, latest}]."""
    catalog = catalog or load_catalog()
    by_name = {e["name"]: e for e in catalog}
    out = []
    for name, info in installed_all().items():
        e = by_name.get(name)
        if not e:
            continue
        try:
            rel = latest_release(e["repo"])
        except StoreError:
            continue
        if rel["version"] != info.get("version"):
            out.append({"name": name, "installed": info.get("version"), "latest": rel["version"],
                        "mode": info.get("mode", "python")})
    return out
