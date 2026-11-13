# ToolHub

Zentrale Windows-App (Python + tkinter), die alle selbst gebauten Programme an einem Ort bündelt:
findet sie automatisch (Standard: `%USERPROFILE%\GIT\Claude\Tools`), startet und beendet sie und zeigt Git-Status
und README. Keine Abhängigkeiten außer Python 3.10+ (getestet mit 3.13).

## Starten

Doppelklick auf `start.bat` (oder `ToolHub.pyw`). Alternativ:

```bash
python -m toolhub
```

Als eigenständige `.exe` bauen: `build.bat` → `dist\ToolHub.exe`.

## Funktionen

- **Automatisch finden:** Jeder Ordner direkt unter den Scan-Ordnern (Standard: `%USERPROFILE%\GIT\Claude\Tools`,
  danach `%USERPROFILE%\GIT\Claude`) mit einer `toolhub.json` wird nach Startpunkten durchsucht – nur lesend:
  - `start.bat` (die darin gestartete `.py` wird nicht zusätzlich gelistet)
  - `*.pyw` und `*.py` mit `if __name__ == "__main__":`
  - Pakete mit `__main__.py` (`python -m paket`)
  - `dist\*.exe`
  - Spiel-Mods (`manifest.xml` + `bin\*.dll`) und Projekte ohne Startpunkt – werden nur angezeigt,
    wenn das in den Einstellungen eingeschaltet ist
  - Beschreibung aus dem Modul-Docstring bzw. dem ersten Absatz der README, Kategorie per Stichwort geraten

  Neue Projekte tauchen beim nächsten Start bzw. mit **⟳ Neu scannen** (F5) auf. Einträge, deren
  Startpunkt verschwunden ist (Ordner umbenannt/gelöscht), fliegen raus – außer du hast sie bearbeitet.
- **Manuell hinzufügen, bearbeiten, entfernen:** Name, Ordner, Startbefehl, Arbeitsverzeichnis, Kategorie,
  Symbol (Emoji), Beschreibung, Build-Befehl, „immer als Administrator“, „Konsolenfenster anzeigen“.
  Bearbeitete Einträge überschreibt der Scanner nicht mehr; entfernte kommen nicht wieder.
- **Übersicht:** Kacheln oder Liste, Kategorien (Aufräumen & Ordnung, Spiele & Mods,
  Daten & Dateien untersuchen, Sicherheit & Backup, Trainer, Sonstiges), Favoriten, Suche (Strg+F).
- **Starten** per Doppelklick/Enter, optional **als Administrator** (UAC-Abfrage). Verlangt eine `.exe`
  selbst Adminrechte (z. B. mit `--uac-admin` gebaut), fragt ToolHub automatisch per UAC nach, statt mit
  „Fehler 740“ abzubrechen.
- **Laufende Programme** unten: PIDs und Laufzeit, **Beenden** (schließt erst die Fenster, nach 3 s hart)
  oder **Hart beenden**. Programme laufen weiter, wenn ToolHub geschlossen wird.
- **Pro Programm:** Ordner öffnen, README anzeigen, Git-Status (Branch, letzter Commit, uncommittete
  Änderungen, Stand gegenüber Remote), **git pull** (`--ff-only`, nach Rückfrage),
  **Als .exe bauen** (vorhandene `build.bat`/`build.ps1`, sonst PyInstaller – nach Rückfrage, Ergebnis in `dist\`).
- **Einstellungen:** Scan-Ordner, Scan beim Start, Projekte ohne Startpunkt anzeigen,
  Autostart beim Windows-Login (Registry `HKCU\…\Run`, nur für den eigenen Benutzer).

## Claude-Anbindung (MCP)

`toolhub_mcp.py` ist ein MCP-Server (stdio, JSON-RPC 2.0, ohne Fremdpakete). Er gibt Claude Zugriff
auf alle Programme unter den Scan-Ordnern:

| MCP-Tool | Wirkung |
|---|---|
| `list_tools` | alle Programme mit Beschreibung, Befehlen, „läuft“ ja/nein, Claude-fähig ja/nein |
| `tool_info(tool)` | Details, Startbefehl, laufende PIDs, Befehle mit Argumenten, README |
| `start_tool(tool)` / `stop_tool(tool)` | Oberfläche starten / beenden (erkennt auch nicht von ToolHub gestartete Instanzen) |
| `run_command(tool, command, args, confirm)` | einen in `toolhub.json` deklarierten Befehl ausführen |
| `<Tool>_<Befehl>` | jeder deklarierte Befehl zusätzlich als eigenes Tool mit JSON-Schema |

**Sicherheit:** Nur deklarierte Befehle, als Argumentliste ohne Shell, mit Timeout (Standard 120 s),
unbekannte Argumente werden abgelehnt. Befehle mit `"readonly": false` tragen die Annotation
`destructiveHint` und laufen ohne `confirm: true` nur mit `--dry-run` (Vorschau).

**Einrichten:** Button **🤖 Claude verbinden** kopiert einen Prompt mit den echten Pfaden in die
Zwischenablage. Er enthält den Befehl
`claude mcp add --scope user toolhub -- "<python.exe>" "<Pfad>\toolhub_mcp.py"` und den
`mcpServers`-Eintrag für Claude Desktop (`claude_desktop_config.json`).

**💬 Claude fragen:** Schickt die Frage an `claude -p` (claude-CLI) mit dem ToolHub-Server
(`--mcp-config %APPDATA%\ToolHub\mcp-config.json --strict-mcp-config`). Freigegeben sind dabei nur
lesende Tools (`list_tools`, `tool_info` und Befehle mit `readonly: true`) – „Claude fragen“ kann also
nichts starten, beenden oder verändern.

In der Liste/den Kacheln steht bei Programmen mit `toolhub.json` „🤖 Claude-fähig“; im Detailbereich
stehen ihre Befehle (✎ = ändert Daten).

### Format `toolhub.json` (im Projektordner)

```json
{"name": "DownloadSorter", "description": "…",
 "commands": [{"name": "scan", "description": "…",
               "run": ["python", "cli.py", "scan", "--json"],
               "args": [{"name": "folder", "flag": "--folder", "type": "string",
                         "required": false, "description": "…"}],
               "readonly": true}]}
```

- `run` ist relativ zum Projektordner; `python` wird durch den aktuellen Interpreter ersetzt.
- Typen: `string`, `integer`, `number`, `boolean` (Flag ohne Wert), `array` (Flag je Wert wiederholt).
  Ohne `flag` wird ein Argument positional angehängt. Optional: `default`, `enum`, pro Befehl `timeout`.
- Ausgabe: JSON auf stdout; Exit-Code ≠ 0 = Fehler (stderr wird zurückgegeben).
- Optional `"start": ["pythonw", "app.pyw"]` als eigener Startbefehl für `start_tool`.
- Skripte, die nur in `run` vorkommen (z. B. `cli.py`), zeigt ToolHub nicht als eigenes Programm an.

## Wie Starten/Beenden funktioniert

Jedes Programm wird in einem eigenen Windows-**Job-Objekt** gestartet. So werden auch Kindprozesse
erfasst – z. B. wenn `start.bat` per `start "" pythonw app.py` das eigentliche Programm startet und sich
sofort selbst beendet. Als zweite Absicherung verfolgt ToolHub den Prozessbaum über Eltern-PID und
Startzeit (falls ein Kindprozess dem Job entkommt).

Batch-Dateien werden als `cmd /c .\start.bat` gestartet – ohne `.\` würde cmd `start.bat` als
eingebauten Befehl `START` mit dem Argument `.bat` lesen.

Bei „Als Administrator“ kann ein nicht erhöhter ToolHub das Programm eventuell nicht beenden; dann gibt es
eine Meldung.

## Daten

Einstellungen und Programmliste: `%APPDATA%\ToolHub\config.json`
(für Tests per Umgebungsvariable `TOOLHUB_HOME` umleitbar).

## Aufbau

| Datei | Inhalt |
|---|---|
| `toolhub/scanner.py` | Projektordner durchsuchen, Startpunkte/Beschreibung/Kategorie erkennen |
| `toolhub/launcher.py` | Starten (Job-Objekt, Admin per `runas`), Prozesse verfolgen und beenden |
| `toolhub/config.py` | JSON-Konfiguration, Zusammenführen mit Scan-Ergebnissen |
| `toolhub/gitinfo.py` | Git-Status lesen |
| `toolhub/autostart.py` | Autostart beim Login |
| `toolhub/gui.py` | Oberfläche |
| `toolhub/manifest.py` | `toolhub.json` lesen, Befehle sicher ausführen |
| `toolhub/hub.py` | Sicht auf alle Tools für den MCP-Server (Start/Stopp, „läuft“-Erkennung) |
| `toolhub/mcp_server.py`, `toolhub_mcp.py` | MCP-Server (stdio) |
| `toolhub/claude.py` | Copy-Prompt „Claude verbinden“ und „Claude fragen“ |

## Tests

```bash
python -m unittest discover -s tests
```

## Später / Ideen

- Tray-Icon (bräuchte z. B. `pystray` + `Pillow`)
- Update-Prüfung für alle Projekte auf einmal (`git fetch` + Anzeige „Update verfügbar“)
- Startparameter pro Programm, mehrere Startpunkte zu einem Programm zusammenfassen

## Lizenz

MIT – siehe [LICENSE](LICENSE).
