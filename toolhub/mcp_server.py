"""Schlanker MCP-Server (stdio, JSON-RPC 2.0) – ohne Fremdpakete.

Feste Tools: list_tools, tool_info, start_tool, stop_tool, run_command.
Zusätzlich wird jeder in einer toolhub.json deklarierte Befehl als eigenes Tool
``<Tool>_<Befehl>`` angeboten (Eingabeschema aus den args).

Sicherheit: Es werden nur deklarierte Befehle ausgeführt, als Argumentliste ohne
Shell, mit Timeout. Befehle mit ``"readonly": false`` laufen ohne ``confirm: true``
nur mit ``--dry-run`` (Vorschau).
"""

from __future__ import annotations

import json
import sys
import traceback

from . import __version__
from .hub import Hub
from .manifest import ManifestError, json_schema, run, safe_tool_name

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST = SUPPORTED_VERSIONS[0]

INSTRUCTIONS = (
    "ToolHub bündelt die selbst gebauten Windows-Programme des Nutzers. "
    "Mit list_tools siehst du alle Programme und ihre Befehle. Befehle aus toolhub.json "
    "liefern JSON. Ändernde Befehle (readonly=false) laufen ohne confirm=true nur als "
    "Vorschau (--dry-run): zeige dem Nutzer erst die Vorschau und frage, bevor du confirm=true setzt."
)

TOOL_ARG = {"type": "string", "description": "Name des Programms (wie in list_tools)"}

FIXED_TOOLS = [
    {"name": "list_tools", "title": "Programme auflisten",
     "description": "Alle Programme mit Name, Beschreibung, verfügbaren Befehlen und ob sie gerade laufen.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "tool_info", "title": "Programm-Details",
     "description": "Details zu einem Programm: Befehle mit Argumenten, Startbefehl, laufende Prozesse und README.",
     "inputSchema": {"type": "object", "properties": {"tool": TOOL_ARG}, "required": ["tool"]},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "start_tool", "title": "Programm starten",
     "description": "Startet die Oberfläche eines Programms (wie ein Doppelklick).",
     "inputSchema": {"type": "object", "properties": {"tool": TOOL_ARG}, "required": ["tool"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}},
    {"name": "stop_tool", "title": "Programm beenden",
     "description": "Beendet ein laufendes Programm (Fenster schließen, notfalls hart beenden). "
                    "Ungespeicherte Daten im Programm können verloren gehen.",
     "inputSchema": {"type": "object", "properties": {"tool": TOOL_ARG}, "required": ["tool"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}},
    {"name": "run_command", "title": "Befehl ausführen",
     "description": "Führt einen in der toolhub.json eines Programms deklarierten Befehl aus und gibt "
                    "dessen JSON-Ausgabe zurück. Ändernde Befehle laufen ohne confirm=true nur als Vorschau (--dry-run).",
     "inputSchema": {"type": "object", "properties": {
         "tool": TOOL_ARG,
         "command": {"type": "string", "description": "Befehlsname aus der toolhub.json"},
         "args": {"type": "object", "description": "Argumente als Objekt {name: wert}", "additionalProperties": True},
         "confirm": {"type": "boolean", "description": "Nur für ändernde Befehle: true = wirklich ausführen statt Vorschau",
                     "default": False}},
         "required": ["tool", "command"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}},
]
FIXED_NAMES = {t["name"] for t in FIXED_TOOLS}


class Server:
    def __init__(self, hub: Hub | None = None):
        self.hub = hub or Hub()
        self.dynamic: dict[str, tuple[str, str]] = {}   # MCP-Toolname -> (Programm, Befehl)

    # ------------------------------------------------------------ Tool-Liste
    def list_tools(self) -> list[dict]:
        tools = [dict(t) for t in FIXED_TOOLS]
        self.dynamic.clear()
        used = set(FIXED_NAMES)
        for t in self.hub.tools():
            if not t.manifest:
                continue
            for c in t.manifest.commands:
                name = base = safe_tool_name(t.name, c.name)
                i = 2
                while name in used:
                    name = safe_tool_name(base[:60], str(i))
                    i += 1
                used.add(name)
                self.dynamic[name] = (t.name, c.name)
                schema = json_schema(c)
                desc = c.description or f"{c.name} von {t.name}"
                if not c.readonly:
                    schema["properties"]["confirm"] = {
                        "type": "boolean", "default": False,
                        "description": "true = wirklich ausführen; sonst nur Vorschau mit --dry-run"}
                    desc += " (ändert Daten – ohne confirm=true nur Vorschau)"
                tools.append({"name": name, "title": f"{t.name}: {c.name}",
                              "description": f"[{t.name}] {desc}", "inputSchema": schema,
                              "annotations": {"readOnlyHint": c.readonly, "destructiveHint": not c.readonly,
                                              "openWorldHint": False}})
        return tools

    # ------------------------------------------------------------ Tool-Aufruf
    def call(self, name: str, a: dict) -> dict:
        a = a or {}
        if name == "list_tools":
            tools = self.hub.tools()
            return {"tools": [self.hub.summary(t) for t in tools]}
        if name == "tool_info":
            return self.hub.info(self.hub.find(a.get("tool")))
        if name == "start_tool":
            return self.hub.start(self.hub.find(a.get("tool")))
        if name == "stop_tool":
            return self.hub.stop(self.hub.find(a.get("tool")))
        if name == "run_command":
            args = a.get("args") or {}
            if not isinstance(args, dict):
                raise ManifestError("„args“ muss ein Objekt {name: wert} sein.")
            return self._run(a.get("tool"), a.get("command"), args, bool(a.get("confirm")))
        if name not in self.dynamic:
            self.list_tools()
        if name in self.dynamic:
            tool, cmd = self.dynamic[name]
            args = {k: v for k, v in a.items() if k != "confirm"}
            return self._run(tool, cmd, args, bool(a.get("confirm")))
        raise KeyError(name)

    def _run(self, tool: str, command: str, args: dict, confirm: bool) -> dict:
        if not command:
            raise ManifestError("Parameter „command“ fehlt.")
        t = self.hub.find(tool)
        if not t.manifest:
            raise ManifestError(f"{t.name} hat keine toolhub.json – keine Befehle verfügbar.")
        res = run(t.manifest, command, args, confirm=confirm)
        d = res.as_dict()
        d["tool"], d["command"] = t.name, command
        if res.dry_run:
            d["note"] = "Nur Vorschau (--dry-run). Zum Ausführen erneut mit confirm=true aufrufen."
        return d

    # ------------------------------------------------------------ JSON-RPC
    def handle(self, msg: dict) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        is_request = "id" in msg
        if method is None:  # Antwort des Clients auf eine eigene Anfrage – gibt es hier nicht
            return None
        try:
            if method == "initialize":
                req = params.get("protocolVersion")
                result = {"protocolVersion": req if req in SUPPORTED_VERSIONS else LATEST,
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "toolhub", "title": "ToolHub", "version": __version__},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.list_tools()}
            elif method == "tools/call":
                name = params.get("name")
                try:
                    data = self.call(name, params.get("arguments") or {})
                except KeyError:
                    return _error(mid, -32602, f"Unbekanntes Tool: {name}")
                except ManifestError as e:
                    result = _tool_result({"error": str(e)}, is_error=True)
                except Exception as e:  # noqa: BLE001 - als Tool-Fehler melden, Server läuft weiter
                    traceback.print_exc(file=sys.stderr)
                    result = _tool_result({"error": f"{type(e).__name__}: {e}"}, is_error=True)
                else:
                    failed = isinstance(data, dict) and data.get("ok") is False
                    result = _tool_result(data, is_error=failed)
            elif method.startswith("notifications/"):
                return None
            elif method in ("resources/list", "prompts/list"):
                result = {method.split("/")[0]: []}
            else:
                return _error(mid, -32601, f"Methode nicht gefunden: {method}") if is_request else None
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return _error(mid, -32603, str(e)) if is_request else None
        return {"jsonrpc": "2.0", "id": mid, "result": result} if is_request else None

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin.buffer
        stdout = stdout or sys.stdout.buffer
        for raw in stdin:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                out = _error(None, -32700, "Parse error")
            else:
                if isinstance(msg, list):
                    out = [r for r in (self.handle(m) for m in msg) if r is not None] or None
                else:
                    out = self.handle(msg)
            if out is not None:
                stdout.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                stdout.flush()


def _tool_result(data, is_error: bool = False) -> dict:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    res = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if isinstance(data, dict):
        res["structuredContent"] = data
    return res


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main() -> None:
    # stdout gehört allein dem Protokoll – alles andere nach stderr
    real_stdout = sys.stdout.buffer
    sys.stdout = sys.stderr
    Server().serve(sys.stdin.buffer, real_stdout)


if __name__ == "__main__":
    main()
