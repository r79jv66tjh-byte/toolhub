"""toolhub.json lesen und deklarierte Befehle sicher ausführen.

Format (eine Datei pro Projektordner)::

    {"name": "DownloadSorter", "description": "…",
     "commands": [{"name": "scan", "description": "…",
                   "run": ["python", "cli.py", "scan", "--json"],
                   "args": [{"name": "folder", "flag": "--folder", "type": "string",
                             "required": false, "description": "…"}],
                   "readonly": true}]}

* ``run`` ist relativ zum Projektordner; ein führendes ``python`` wird durch den
  aktuellen Interpreter ersetzt.
* Argument-Typen: string, integer, number, boolean (Flag ohne Wert), array
  (Flag wird pro Wert wiederholt). Ohne ``flag`` wird das Argument positional angehängt.
* Ausgabe: JSON auf stdout; Exit-Code ≠ 0 = Fehler (stderr wird zurückgegeben).
* ``"readonly": false`` -> ohne ``confirm: true`` wird nur mit ``--dry-run`` ausgeführt.

Es wird nie eine Shell benutzt; nur deklarierte Befehle sind ausführbar.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import scanner

MANIFEST_NAME = "toolhub.json"
DEFAULT_TIMEOUT = 120
MAX_OUTPUT = 1_000_000  # Zeichen
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
ARG_TYPES = {"string", "integer", "number", "boolean", "array"}


class ManifestError(Exception):
    pass


@dataclass
class Arg:
    name: str
    flag: str = ""
    type: str = "string"
    required: bool = False
    description: str = ""
    default: object = None
    enum: list | None = None


@dataclass
class Command:
    name: str
    description: str
    run: list[str]
    args: list[Arg] = field(default_factory=list)
    readonly: bool = True
    timeout: float = DEFAULT_TIMEOUT


@dataclass
class Manifest:
    name: str
    description: str
    folder: Path
    path: Path
    commands: list[Command] = field(default_factory=list)
    start: list[str] | None = None   # optional: eigener Startbefehl
    errors: list[str] = field(default_factory=list)

    def command(self, name: str) -> Command:
        for c in self.commands:
            if c.name.lower() == str(name).lower():
                return c
        raise ManifestError(f"Befehl „{name}“ ist für {self.name} nicht deklariert. "
                            f"Verfügbar: {', '.join(c.name for c in self.commands) or '–'}")


def _parse_arg(raw: dict, where: str) -> Arg:
    if not isinstance(raw, dict) or not raw.get("name"):
        raise ManifestError(f"{where}: Argument ohne Namen")
    typ = str(raw.get("type", "string")).lower()
    if typ not in ARG_TYPES:
        raise ManifestError(f"{where}: unbekannter Typ „{typ}“ für {raw['name']}")
    flag = str(raw.get("flag") or "")
    if typ == "boolean" and not flag:
        raise ManifestError(f"{where}: boolean-Argument {raw['name']} braucht ein flag")
    return Arg(name=str(raw["name"]), flag=flag, type=typ, required=bool(raw.get("required", False)),
               description=str(raw.get("description", "")), default=raw.get("default"),
               enum=raw.get("enum") if isinstance(raw.get("enum"), list) else None)


def load(path: Path) -> Manifest:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ManifestError(f"{path}: nicht lesbar ({e})") from e
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: kein JSON-Objekt")
    folder = path.parent
    m = Manifest(name=str(data.get("name") or folder.name), description=str(data.get("description", "")),
                 folder=folder, path=path)
    start = data.get("start")
    if isinstance(start, list) and start and all(isinstance(x, str) for x in start):
        m.start = start
    for i, raw in enumerate(data.get("commands") or []):
        where = f"{path.name}: commands[{i}]"
        try:
            if not isinstance(raw, dict) or not raw.get("name"):
                raise ManifestError(f"{where}: Befehl ohne Namen")
            run = raw.get("run")
            if not (isinstance(run, list) and run and all(isinstance(x, str) for x in run)):
                raise ManifestError(f"{where}: „run“ muss eine nicht-leere Liste von Strings sein")
            m.commands.append(Command(
                name=str(raw["name"]), description=str(raw.get("description", "")), run=run,
                args=[_parse_arg(a, f"{where}.args") for a in (raw.get("args") or [])],
                readonly=bool(raw.get("readonly", True)),
                timeout=float(raw.get("timeout") or DEFAULT_TIMEOUT)))
        except ManifestError as e:
            m.errors.append(str(e))
    return m


def find_manifests(roots: list[str]) -> list[Manifest]:
    """Alle toolhub.json direkt in Projektordnern unter den Wurzeln."""
    out = []
    for root in roots:
        r = Path(os.path.expandvars(root)).expanduser()
        if not r.is_dir():
            continue
        for folder in sorted(r.iterdir(), key=lambda p: p.name.lower()):
            f = folder / MANIFEST_NAME
            if folder.is_dir() and f.is_file():
                try:
                    out.append(load(f))
                except ManifestError as e:
                    out.append(Manifest(name=folder.name, description="", folder=folder, path=f, errors=[str(e)]))
    return out


# ------------------------------------------------------------- Ausführung
def _coerce(arg: Arg, value):
    t = arg.type
    try:
        if t == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "ja", "yes", "on")
            return bool(value)
        if t == "integer":
            if isinstance(value, bool):
                raise ValueError
            return int(value)
        if t == "number":
            if isinstance(value, bool):
                raise ValueError
            return float(value)
        if t == "array":
            if isinstance(value, (list, tuple)):
                return [str(v) for v in value]
            return [str(value)]
        if isinstance(value, (dict, list)):
            raise ValueError
        return str(value)
    except (TypeError, ValueError) as e:
        raise ManifestError(f"Argument {arg.name}: Wert {value!r} passt nicht zum Typ {t}") from e


def _fmt(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def build_argv(m: Manifest, cmd: Command, args: dict | None, confirm: bool = False,
               python_exe: str | None = None) -> tuple[list[str], bool]:
    """Befehlszeile (Liste) bauen. Gibt (argv, dry_run) zurück."""
    args = dict(args or {})
    known = {a.name for a in cmd.args}
    unknown = sorted(set(args) - known)
    if unknown:
        raise ManifestError(f"Unbekannte Argumente: {', '.join(unknown)}. "
                            f"Erlaubt: {', '.join(sorted(known)) or 'keine'}")
    argv = list(cmd.run)
    if argv[0].lower() in ("python", "python3", "python.exe", "py"):
        argv[0] = python_exe or scanner.python()
    elif not os.path.isabs(argv[0]) and (m.folder / argv[0]).exists():
        argv[0] = str(m.folder / argv[0])
    positional: list[str] = []
    for a in cmd.args:
        value = args.get(a.name, a.default)
        if value is None:
            if a.required:
                raise ManifestError(f"Pflicht-Argument fehlt: {a.name}")
            continue
        value = _coerce(a, value)
        if a.enum and value not in a.enum:
            raise ManifestError(f"Argument {a.name}: erlaubt sind {a.enum}")
        if a.type == "boolean":
            if value:
                argv.append(a.flag)
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            if a.flag:
                argv += [a.flag, _fmt(v)]
            else:
                positional.append(_fmt(v))
    argv += positional
    dry = (not cmd.readonly) and not confirm
    if dry and "--dry-run" not in argv:
        argv.append("--dry-run")
    return argv, dry


@dataclass
class RunResult:
    ok: bool
    exit_code: int
    data: object          # geparstes JSON oder None
    stdout: str
    stderr: str
    dry_run: bool
    argv: list[str]

    def as_dict(self) -> dict:
        d = {"ok": self.ok, "exit_code": self.exit_code, "dry_run": self.dry_run}
        if self.data is not None:
            d["result"] = self.data
        elif self.stdout.strip():
            d["output"] = self.stdout
        if self.stderr.strip() and (not self.ok or self.data is None):
            d["stderr"] = self.stderr
        return d


def run(m: Manifest, cmd_name: str, args: dict | None = None, confirm: bool = False,
        timeout: float | None = None) -> RunResult:
    cmd = m.command(cmd_name)
    argv, dry = build_argv(m, cmd, args, confirm)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    try:
        p = subprocess.run(argv, cwd=str(m.folder), capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=timeout or cmd.timeout, creationflags=CREATE_NO_WINDOW, env=env)
    except subprocess.TimeoutExpired as e:
        raise ManifestError(f"Zeitüberschreitung nach {e.timeout:.0f} s: {cmd.name}") from e
    except OSError as e:
        raise ManifestError(f"Start fehlgeschlagen ({argv[0]}): {e}") from e
    out = p.stdout.decode("utf-8", "replace")[:MAX_OUTPUT]
    err = p.stderr.decode("utf-8", "replace")[:MAX_OUTPUT]
    data = None
    try:
        data = json.loads(out) if out.strip() else None
    except ValueError:
        data = None
    return RunResult(ok=p.returncode == 0, exit_code=p.returncode, data=data, stdout=out,
                     stderr=err, dry_run=dry, argv=argv)


def json_schema(cmd: Command) -> dict:
    """JSON-Schema der Argumente eines Befehls (für MCP inputSchema)."""
    props, req = {}, []
    for a in cmd.args:
        s: dict = {"type": a.type} if a.type != "array" else {"type": "array", "items": {"type": "string"}}
        if a.description:
            s["description"] = a.description
        if a.enum:
            s["enum"] = a.enum
        if a.default is not None:
            s["default"] = a.default
        props[a.name] = s
        if a.required:
            req.append(a.name)
    schema: dict = {"type": "object", "properties": props}
    if req:
        schema["required"] = req
    return schema


def safe_tool_name(*parts: str) -> str:
    name = "_".join(re.sub(r"[^A-Za-z0-9_-]+", "_", p).strip("_") for p in parts if p)
    return name[:64] or "tool"
