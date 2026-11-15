import queue
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from toolhub import gui  # noqa: E402


class InlineThread:
    """Runs the worker on start(); the result still reaches the window only through ui_queue."""

    def __init__(self, target, daemon):
        self.target = target

    def start(self):
        self.target()


class RescanTest(unittest.TestCase):
    def test_failed_scan_is_reported_in_the_status_line(self):
        statuses = []
        app = types.SimpleNamespace(
            cfg=types.SimpleNamespace(settings={"scan_roots": []}),
            set_status=statuses.append,
            ui_queue=queue.Queue(),
        )
        # Stands in for a folder Windows refuses to list.
        refused = PermissionError("Zugriff verweigert")
        with mock.patch.object(gui.threading, "Thread", InlineThread), \
                mock.patch.object(gui.scanner, "scan_roots", side_effect=refused):
            gui.App.rescan(app)
        while not app.ui_queue.empty():
            app.ui_queue.get_nowait()()
        self.assertEqual(statuses[-1], "Scan fehlgeschlagen: Zugriff verweigert")


if __name__ == "__main__":
    unittest.main()
