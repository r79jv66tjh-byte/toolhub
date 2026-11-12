"""MCP-Server: Ende-zu-Ende über stdio (initialize, tools/list, tools/call) und Manifest-Logik."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from toolhub import manifest  # noqa: E402

CLI = r'''
import json, sys
args = sys.argv[1:]
if args[0] == "hello":
    name = args[args.index("--name") + 1] if "--name" in args else "Welt"
    count = int(args[args.index("--count") + 1]) if "--count" in args else 1
    print(json.dumps({"greeting": ("Hallo " + name) * count, "loud": "--loud" in args, "rest": args[1:]}))
elif args[0] == "clean":
    print(json.dumps({"dry_run": "--dry-run" in args, "deleted": 0 if "--dry-run" in args else 3}))
elif args[0] == "fail":
    print("kaputt", file=sys.stderr); sys.exit(2)
'''

MANIFEST = {
    "name": "DemoTool", "description": "Testprogramm",
    "commands": [
        {"name": "hello", "description": "Grüßt", "run": ["python", "cli.py", "hello"], "readonly": True,
         "args": [{"name": "name", "flag": "--name", "type": "string"},
                  {"name": "count", "flag": "--count", "type": "integer"},
                  {"name": "loud", "flag": "--loud", "type": "boolean"},
                  {"name": "files", "type": "array"}]},
        {"name": "clean", "description": "Löscht", "run": ["python", "cli.py", "clean"], "readonly": False},
        {"name": "fail", "run": ["python", "cli.py", "fail"]},
    ]}


def make_project(root: Path) -> Path:
    proj = root / "DemoTool"
    proj.mkdir(parents=True)
    (proj / "cli.py").write_text(CLI, encoding="utf-8")
    (proj / "toolhub.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    (proj / "DemoTool.pyw").write_text('import time\ntime.sleep(60)\n', encoding="utf-8")
    (proj / "README.md").write_text("# DemoTool\n\nEin Testprogramm.\n", encoding="utf-8")
    return proj


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = make_project(Path(self.tmp.name))
        self.m = manifest.load(self.proj / "toolhub.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_argv(self):
        argv, dry = manifest.build_argv(self.m, self.m.command("hello"),
                                        {"name": "A B", "count": "2", "loud": True, "files": ["x", "y"]},
                                        python_exe="PY")
        self.assertEqual(argv, ["PY", "cli.py", "hello", "--name", "A B", "--count", "2", "--loud", "x", "y"])
        self.assertFalse(dry)

    def test_rejects_unknown_and_bad_types(self):
        with self.assertRaises(manifest.ManifestError):
            manifest.build_argv(self.m, self.m.command("hello"), {"rm": "-rf"})
        with self.assertRaises(manifest.ManifestError):
            manifest.build_argv(self.m, self.m.command("hello"), {"count": "drei"})
        with self.assertRaises(manifest.ManifestError):
            self.m.command("format_c")

    def test_dry_run_for_writing_commands(self):
        r = manifest.run(self.m, "clean")
        self.assertTrue(r.dry_run)
        self.assertEqual(r.data, {"dry_run": True, "deleted": 0})
        r = manifest.run(self.m, "clean", confirm=True)
        self.assertEqual(r.data, {"dry_run": False, "deleted": 3})

    def test_error_exit_code(self):
        r = manifest.run(self.m, "fail")
        self.assertFalse(r.ok)
        self.assertIn("kaputt", r.stderr)


class McpStdioTest(unittest.TestCase):
    """Startet toolhub_mcp.py als echten Prozess und spricht JSON-RPC über stdio."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(cls.tmp.name)
        projects = base / "projekte"
        make_project(projects)
        home = base / "home"
        home.mkdir()
        (home / "config.json").write_text(json.dumps({"settings": {"scan_roots": [str(projects)]}}), encoding="utf-8")
        env = dict(os.environ, TOOLHUB_HOME=str(home), PYTHONIOENCODING="utf-8")
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "toolhub_mcp.py")], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        cls.next_id = 0

    @classmethod
    def tearDownClass(cls):
        try:
            cls.rpc("tools/call", {"name": "stop_tool", "arguments": {"tool": "DemoTool"}})
        finally:
            cls.proc.stdin.close()
            cls.proc.wait(10)
            cls.proc.stdout.close()
            cls.proc.stderr.close()
            cls.tmp.cleanup()

    @classmethod
    def send(cls, msg):
        cls.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        cls.proc.stdin.flush()

    @classmethod
    def rpc(cls, method, params=None):
        cls.next_id += 1
        cls.send({"jsonrpc": "2.0", "id": cls.next_id, "method": method, "params": params or {}})
        line = cls.proc.stdout.readline()
        resp = json.loads(line)
        assert resp["id"] == cls.next_id, resp
        return resp

    def call(self, name, args=None):
        r = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        self.assertIn("result", r, r)
        return r["result"]

    def test_1_initialize(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "1"}})["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertEqual(r["serverInfo"]["name"], "toolhub")
        self.assertIn("tools", r["capabilities"])
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(self.rpc("ping")["result"], {})

    def test_2_tools_list(self):
        tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}
        for n in ("list_tools", "tool_info", "start_tool", "stop_tool", "run_command",
                  "DemoTool_hello", "DemoTool_clean"):
            self.assertIn(n, tools)
        self.assertTrue(tools["DemoTool_clean"]["annotations"]["destructiveHint"])
        self.assertTrue(tools["DemoTool_hello"]["annotations"]["readOnlyHint"])
        self.assertEqual(tools["DemoTool_hello"]["inputSchema"]["properties"]["count"]["type"], "integer")
        self.assertIn("confirm", tools["DemoTool_clean"]["inputSchema"]["properties"])

    def test_3_list_and_info(self):
        data = self.call("list_tools")["structuredContent"]["tools"]
        demo = next(t for t in data if t["name"] == "DemoTool")
        self.assertTrue(demo["claude_ready"])
        self.assertFalse(demo["running"])
        info = self.call("tool_info", {"tool": "demotool"})["structuredContent"]
        self.assertIn("Ein Testprogramm", info["readme"])

    def test_4_run_commands(self):
        r = self.call("DemoTool_hello", {"name": "Alex", "count": 2, "loud": True})
        self.assertFalse(r["isError"])
        self.assertEqual(r["structuredContent"]["result"]["greeting"], "Hallo AlexHallo Alex")
        r = self.call("run_command", {"tool": "DemoTool", "command": "clean"})
        self.assertTrue(r["structuredContent"]["dry_run"])
        self.assertEqual(r["structuredContent"]["result"]["deleted"], 0)
        r = self.call("DemoTool_clean", {"confirm": True})
        self.assertEqual(r["structuredContent"]["result"]["deleted"], 3)
        r = self.call("run_command", {"tool": "DemoTool", "command": "fail"})
        self.assertTrue(r["isError"])
        self.assertIn("kaputt", r["structuredContent"]["stderr"])
        r = self.call("run_command", {"tool": "DemoTool", "command": "hello", "args": {"evil": "&& del"}})
        self.assertTrue(r["isError"])
        self.assertEqual(self.rpc("tools/call", {"name": "gibtsnicht", "arguments": {}})["error"]["code"], -32602)

    def test_5_start_stop(self):
        r = self.call("start_tool", {"tool": "DemoTool"})["structuredContent"]
        self.assertTrue(r["started"])
        import time
        time.sleep(1.5)
        demo = next(t for t in self.call("list_tools")["structuredContent"]["tools"] if t["name"] == "DemoTool")
        self.assertTrue(demo["running"])
        r = self.call("stop_tool", {"tool": "DemoTool"})["structuredContent"]
        self.assertTrue(r["stopped"])
        time.sleep(3.5)  # Prozess-Cache läuft ab
        demo = next(t for t in self.call("list_tools")["structuredContent"]["tools"] if t["name"] == "DemoTool")
        self.assertFalse(demo["running"])


if __name__ == "__main__":
    unittest.main()
