"""Startet den ToolHub ohne Konsolenfenster (Doppelklick oder pythonw)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from toolhub.gui import main  # noqa: E402

main()
