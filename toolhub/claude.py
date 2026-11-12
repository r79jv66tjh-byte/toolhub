"""Claude anbinden: Copy-Prompt für die Einrichtung und „Claude fragen“ über die CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import scanner
from .config import config_dir

ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = ROOT / "toolhub_mcp.py"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
ASK_TIMEOUT = 300


def server_command() -> tuple[str, list[str]]:
    return scanner.python(), [str(SERVER_SCRIPT)]


def desktop_snippet() -> str:
    cmd, args = server_command()
    return json.dumps({"mcpServers": {"toolhub": {"command": cmd, "args": args}}}, indent=2)


def add_command() -> str:
    cmd, args = server_command()
    return f'claude mcp add --scope user toolhub -- "{cmd}" "{args[0]}"'


def connect_prompt() -> str:
    return f"""Bitte verbinde dich mit meinem ToolHub – das ist ein MCP-Server, über den du meine selbst gebauten Windows-Programme sehen, starten, beenden und deren Befehle ausführen kannst.

1. Führe diesen Befehl aus (richtet den Server für alle meine Projekte ein):

{add_command()}

2. Ruf danach das Tool `list_tools` vom MCP-Server `toolhub` auf und zeig mir kurz als Liste, welche Programme und Befehle verfügbar sind. Falls die Tools erst nach einem Neustart der Sitzung erscheinen, sag mir das (ich starte dann neu bzw. prüfe mit /mcp).

Falls du kein Claude Code bist, sondern Claude Desktop: Trag stattdessen in %APPDATA%\\Claude\\claude_desktop_config.json unter "mcpServers" diesen Eintrag ein und starte Claude Desktop neu:

{desktop_snippet()}

Wichtig: Befehle mit readonly=false ändern Daten. Ohne confirm=true laufen sie nur als Vorschau (--dry-run) – zeig mir erst die Vorschau und frag mich, bevor du confirm=true setzt.
"""


def find_cli() -> str | None:
    """Pfad der claude-CLI (bevorzugt .exe, sonst .cmd)."""
    for name in ("claude.exe", "claude"):
        p = shutil.which(name)
        if p:
            return p
    cand = Path.home() / ".local" / "bin" / "claude.exe"
    return str(cand) if cand.exists() else None


def mcp_config_file() -> Path:
    cmd, args = server_command()
    path = config_dir() / "mcp-config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"toolhub": {"command": cmd, "args": args}}}, indent=2),
                    encoding="utf-8")
    return path


def readonly_tool_names() -> list[str]:
    """Nur lesende ToolHub-Tools – nur diese darf „Claude fragen“ ohne Rückfrage benutzen."""
    from .mcp_server import Server
    return [f"mcp__toolhub__{t['name']}" for t in Server().list_tools()
            if t.get("annotations", {}).get("readOnlyHint")]


def ask(question: str, timeout: float = ASK_TIMEOUT) -> tuple[bool, str]:
    """``claude -p`` mit dem ToolHub-MCP-Server ausführen. Gibt (ok, Antwort/Fehler) zurück."""
    cli = find_cli()
    if not cli:
        return False, ("Die claude-CLI wurde nicht gefunden. Nutze „Claude verbinden“ und füge den "
                       "kopierten Prompt in Claude ein.")
    argv = [cli, "-p", "--output-format", "text", "--mcp-config", str(mcp_config_file()),
            "--strict-mcp-config", "--allowedTools", *readonly_tool_names()]
    if cli.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", *argv]
    try:
        r = subprocess.run(argv, input=question.encode("utf-8"), capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, cwd=str(Path.home()))
    except subprocess.TimeoutExpired:
        return False, f"Keine Antwort nach {timeout:.0f} Sekunden."
    except OSError as e:
        return False, f"claude-CLI konnte nicht gestartet werden: {e}"
    out = r.stdout.decode("utf-8", "replace").strip()
    err = r.stderr.decode("utf-8", "replace").strip()
    if r.returncode != 0:
        return False, err or out or f"claude-CLI beendet mit Code {r.returncode}"
    return True, out
