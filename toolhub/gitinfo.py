"""Git-Status eines Projektordners (nur lesend)."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class GitStatus:
    is_repo: bool = False
    branch: str = ""
    last_commit: str = ""        # "abc1234 Nachricht"
    last_date: str = ""
    author: str = ""
    changes: list[str] = field(default_factory=list)   # Zeilen aus `git status --porcelain`
    ahead_behind: str = ""
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return self.error
        if not self.is_repo:
            return "Kein Git-Repository"
        state = (f"{len(self.changes)} uncommittete Änderung(en)" if self.changes
                 else "keine uncommitteten Änderungen")
        return f"{self.branch}: {self.last_commit or '(noch kein Commit)'} – {state}"


def _git(path: str, *args: str, timeout: float = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, r.stdout.decode("utf-8", "replace").strip()


def status(path: str) -> GitStatus:
    st = GitStatus()
    if not shutil.which("git"):
        st.error = "git ist nicht installiert"
        return st
    if not os.path.isdir(os.path.join(path, ".git")):
        return st
    st.is_repo = True
    _, st.branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    rc, out = _git(path, "log", "-1", "--format=%h %s%x1f%cd%x1f%an", "--date=format:%d.%m.%Y %H:%M")
    if rc == 0 and out:
        parts = out.split("\x1f")
        st.last_commit, st.last_date, st.author = (parts + ["", "", ""])[:3]
    rc, out = _git(path, "status", "--porcelain")
    if rc == 0:
        st.changes = [line for line in out.splitlines() if line.strip()]
    rc, out = _git(path, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    if rc == 0 and out:
        a, b = (out.split() + ["0", "0"])[:2]
        st.ahead_behind = f"{a} voraus, {b} zurück (gegenüber Remote)"
    return st
