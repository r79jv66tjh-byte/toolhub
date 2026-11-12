"""ToolHub als MCP-Server (stdio) – gibt Claude Zugriff auf alle Programme.

Einrichten in Claude Code:
    claude mcp add --scope user toolhub -- "<python.exe>" "<Pfad>\\toolhub_mcp.py"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from toolhub.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
