"""Tests für Scanner, Konfiguration und Prozess-Start/-Stopp.

Ausführen:  python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from toolhub import launcher, scanner  # noqa: E402
from toolhub.config import Config  # noqa: E402


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_bat_hides_its_script(self):
        proj = self.root / "Sortierer"
        write(proj / "start.bat", '@echo off\ncd /d "%~dp0"\nstart "" pythonw sorter_app.py\n')
        write(proj / "sorter_app.py", '"""Sortiert Downloads."""\nimport tkinter\nif __name__ == "__main__":\n    pass\n')
        write(proj / "README.md", "# Sortierer\n\nRäumt den Download-Ordner auf.\n\n## Start\n")
        found = scanner.scan_project(proj)
        self.assertEqual([e["kind"] for e in found], ["bat"])
        e = found[0]
        self.assertEqual(e["name"], "Sorter App")
        self.assertIn(".\\start.bat", e["command"])
        self.assertEqual(e["description"], "Sortiert Downloads.")
        self.assertEqual(e["category"], "Aufräumen & Ordnung")

    def test_package_pyw_and_exe(self):
        proj = self.root / "Datenwacht"
        write(proj / "datenwacht" / "__init__.py", "")
        write(proj / "datenwacht" / "__main__.py", "from .gui import main\nmain()\n")
        write(proj / "datenwacht" / "gui.py", "import tkinter\n")
        write(proj / "tool.py", 'if __name__ == "__main__":\n    print(1)\n')
        write(proj / "helper.py", "x = 1\n")
        write(proj / "dist" / "Datenwacht.exe", "")
        kinds = {e["kind"]: e for e in scanner.scan_project(proj)}
        self.assertEqual(set(kinds), {"modul", "py", "exe"})
        self.assertIn("-m datenwacht", kinds["modul"]["command"])
        self.assertIn("pythonw", kinds["modul"]["command"].lower())
        self.assertFalse(launcher.split_first(kinds["py"]["command"])[0].lower().endswith("pythonw.exe"))
        self.assertEqual(kinds["modul"]["category"], "Sicherheit & Backup")

    def test_pyw_covers_package(self):
        proj = self.root / "GameWarden"
        write(proj / "GameWarden.pyw", "from gamewarden.gui import main\nmain()\n")
        write(proj / "gamewarden" / "__init__.py", "")
        write(proj / "gamewarden" / "__main__.py", "pass\n")
        found = scanner.scan_project(proj)
        self.assertEqual([e["kind"] for e in found], ["pyw"])

    def test_mod_and_plain_project(self):
        mod = self.root / "ProbablyMod"
        write(mod / "manifest.xml", "<x/>")
        write(mod / "bin" / "Mod.dll", "")
        write(mod / "build.ps1", "")
        e = scanner.scan_project(mod)[0]
        self.assertEqual((e["kind"], e["command"], e["category"]), ("mod", "", "Spiele & Mods"))
        self.assertIn("build.ps1", e["build"])
        plain = self.root / "Leer"
        plain.mkdir()
        self.assertEqual(scanner.scan_project(plain)[0]["kind"], "projekt")

    def test_known_program_names(self):
        proj = self.root / "MemTrainer"
        write(proj / "start.bat", '@echo off\nstart "" pythonw memtrainer.py\n')
        write(proj / "memtrainer.py", 'import tkinter\nif __name__ == "__main__":\n    pass\n')
        self.assertEqual(scanner.scan_project(proj)[0]["category"], "Trainer")
        proj = self.root / "BackupVault"
        write(proj / "BackupVault.pyw", "pass\n")
        self.assertEqual(scanner.scan_project(proj)[0]["category"], "Sicherheit & Backup")

    def test_manifest_cli_is_not_a_program(self):
        proj = self.root / "FileProbe"
        write(proj / "FileProbe.pyw", "pass\n")
        write(proj / "cli.py", 'if __name__ == "__main__":\n    pass\n')
        write(proj / "toolhub.json", '{"name": "FileProbe", "commands": [{"name": "x", "run": ["python", "cli.py", "x"]}]}')
        found = scanner.scan_project(proj)
        self.assertEqual([(e["name"], e["kind"]) for e in found], [("FileProbe", "pyw")])
        self.assertTrue(found[0]["claude"])
        self.assertEqual(found[0]["category"], "Daten & Dateien untersuchen")

    def test_readme_summary(self):
        text = "# Titel\n\n🇬🇧 English | 🇩🇪 Deutsch\n\n> Hinweis\n\n**Fett** und `code`.\nZweite Zeile.\n\nMehr."
        self.assertEqual(scanner.readme_summary(text), "Fett und code. Zweite Zeile.")


class ConfigTest(unittest.TestCase):
    def test_roundtrip_and_merge(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(Path(d) / "config.json")
            found = [{"id": "a", "name": "A", "auto": True, "command": "x"},
                     {"id": "b", "name": "B", "auto": True, "command": "y"}]
            self.assertEqual(cfg.merge_scan(found), (2, 0, 0))
            cfg.get("a")["favorite"] = True
            cfg.get("b").update(name="Eigener Name", edited=True)
            cfg.remove("a")
            cfg.save()
            cfg2 = Config(Path(d) / "config.json").load()
            found[1]["name"] = "B neu"
            self.assertEqual(cfg2.merge_scan(found), (0, 0, 0))  # a ignoriert, b bearbeitet
            self.assertIsNone(cfg2.get("a"))
            self.assertEqual(cfg2.get("b")["name"], "Eigener Name")
            # verschwundener, unbearbeiteter Scanner-Eintrag fliegt raus, bearbeiteter bleibt
            cfg2.merge_scan([{"id": "c", "name": "C", "auto": True, "command": "z"}])
            self.assertEqual(cfg2.merge_scan([]), (0, 0, 1))
            self.assertIsNotNone(cfg2.get("b"))


@unittest.skipUnless(os.name == "nt", "nur Windows")
class LauncherTest(unittest.TestCase):
    def test_start_bat_child_is_tracked_and_stopped(self):
        # ignore_cleanup_errors: Windows gibt das Arbeitsverzeichnis eines gerade
        # beendeten Prozesses manchmal erst Millisekunden später frei.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            write(Path(d) / "start.bat",
                  '@echo off\ncd /d "%~dp0"\nstart "" pythonw -c "import time; time.sleep(60)"\n')
            prog = {"id": "t", "name": "Test", "path": d, "command": "cmd /c .\\start.bat"}
            r = launcher.launch(prog)
            deadline = time.time() + 10
            pids = []
            while time.time() < deadline:
                time.sleep(0.5)
                pids = [p for p in r.pids() if p != getattr(r.popen, "pid", 0)]
                if pids:
                    break
            self.assertTrue(pids, "Kindprozess aus start.bat wurde nicht erkannt")
            time.sleep(1.0)
            self.assertTrue(r.alive())
            self.assertTrue(r.stop(grace=0.5))
            self.assertFalse(r.alive())
            r.close()

    def test_missing_command(self):
        with self.assertRaises(launcher.LaunchError):
            launcher.launch({"id": "x", "name": "x", "path": ".", "command": ""})

    def test_exe_that_demands_admin_rights_is_started_via_uac(self):
        # Windows refuses CreateProcess for an .exe whose manifest requires administrator rights
        # (ERROR_ELEVATION_REQUIRED, e.g. PyInstaller --uac-admin). Popen and ShellExecuteExW are the
        # OS boundary and are replaced here, so no UAC prompt appears during the test.
        def refuse(*args, **kwargs):
            raise OSError(22, "Der angeforderte Vorgang erfordert erhöhte Rechte.", None, 740)

        elevated = []

        def shell_execute(info_ref):
            info = info_ref._obj
            elevated.append((info.lpVerb, info.lpFile, info.lpParameters))
            return 1

        original_popen, original_shell = launcher.subprocess.Popen, launcher.shell32.ShellExecuteExW
        launcher.subprocess.Popen = refuse
        launcher.shell32.ShellExecuteExW = shell_execute
        try:
            with tempfile.TemporaryDirectory() as d:
                write(Path(d) / "dist" / "App.exe", "")
                r = launcher._launch({"id": "a", "name": "App", "path": d, "command": r"dist\App.exe"})
                self.assertTrue(r.admin)
                self.assertEqual(elevated, [("runas", "cmd.exe", f'/c cd /d "{d}" && dist\\App.exe')])
        finally:
            launcher.subprocess.Popen, launcher.shell32.ShellExecuteExW = original_popen, original_shell

    def test_other_start_errors_are_reported(self):
        def refuse(*args, **kwargs):
            raise OSError(2, "Das System kann die angegebene Datei nicht finden.", None, 2)

        original = launcher.subprocess.Popen
        launcher.subprocess.Popen = refuse
        try:
            with self.assertRaisesRegex(launcher.LaunchError, "Start fehlgeschlagen"):
                launcher._launch({"id": "a", "name": "App", "path": ".", "command": "gibtsnicht.exe"})
        finally:
            launcher.subprocess.Popen = original

    def test_resolve_relative_exe(self):
        with tempfile.TemporaryDirectory() as d:
            write(Path(d) / "dist" / "App.exe", "")
            cmd = launcher.resolve_command(r"dist\App.exe --x", d)
            self.assertTrue(cmd.lower().startswith(str(Path(d)).lower()[:3]))
            self.assertTrue(cmd.endswith("--x"))


if __name__ == "__main__":
    unittest.main()
