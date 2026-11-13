"""Programme starten, verfolgen und beenden.

Jedes gestartete Programm kommt in ein eigenes Windows-Job-Objekt. Dadurch
werden auch Kindprozesse erfasst – z. B. wenn eine ``start.bat`` per
``start "" pythonw app.py`` das eigentliche Programm startet und sich selbst
sofort beendet. „Beenden“ schließt erst die Fenster (WM_CLOSE) und beendet
nach kurzer Wartezeit alle übrigen Prozesse des Jobs.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

IS_WIN = os.name == "nt"

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
JobObjectBasicProcessIdList = 3
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_SHOWNORMAL = 1
ERROR_ELEVATION_REQUIRED = 740
WM_CLOSE = 0x0010
WAIT_TIMEOUT = 0x102

if IS_WIN:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.GetProcessId.argtypes = [wintypes.HANDLE]
    k32.GetProcessId.restype = wintypes.DWORD
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u32.IsWindowVisible.argtypes = [wintypes.HWND]
    u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL


class LaunchError(Exception):
    pass


class ElevationRequired(LaunchError):
    """The program's manifest demands administrator rights, which CreateProcess cannot grant."""


# ------------------------------------------------------------ Prozessbaum
TH32CS_SNAPPROCESS = 0x2
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000

if IS_WIN:
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4


def process_list() -> list[tuple[int, int]]:
    """Alle Prozesse als (PID, Eltern-PID)."""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    out = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((e.th32ProcessID, e.th32ParentProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def create_time(pid: int) -> int:
    """Startzeit eines laufenden Prozesses (FILETIME) oder 0, wenn er nicht (mehr) läuft."""
    # SYNCHRONIZE ist nötig, sonst schlägt WaitForSingleObject fehl und jeder
    # Prozess gälte als beendet.
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not h:
        return 0
    try:
        c, x, k, u = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(x), ctypes.byref(k), ctypes.byref(u)):
            return 0
        if k32.WaitForSingleObject(h, 0) != WAIT_TIMEOUT:
            return 0
        return (c.dwHighDateTime << 32) | c.dwLowDateTime
    finally:
        k32.CloseHandle(h)


def terminate_pid(pid: int) -> bool:
    h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(k32.TerminateProcess(h, 1))
    finally:
        k32.CloseHandle(h)


def split_first(command: str) -> tuple[str, str]:
    """Befehlszeile in (Programm, Rest) zerlegen (Windows-Anführungszeichen)."""
    command = command.strip()
    m = re.match(r'\s*"([^"]*)"\s*(.*)', command, re.S) or re.match(r"\s*(\S+)\s*(.*)", command, re.S)
    if not m:
        return "", ""
    return m.group(1), m.group(2)


def resolve_command(command: str, cwd: str) -> str:
    """Relatives Programm (z. B. ``dist\\App.exe``) relativ zu ``cwd`` auflösen.

    CreateProcess sucht relative Pfade sonst im Ordner von ToolHub."""
    exe, rest = split_first(command)
    if exe and not os.path.isabs(exe) and ("\\" in exe or "/" in exe or exe.lower().endswith(".exe")):
        cand = Path(cwd) / exe
        if cand.exists():
            exe = str(cand)
    exe_q = f'"{exe}"' if " " in exe else exe
    return f"{exe_q} {rest}".strip()


def _window_pids_close(pids: set[int]) -> int:
    """WM_CLOSE an alle sichtbaren Fenster der Prozesse schicken."""
    hits = []

    def cb(hwnd, _):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and u32.IsWindowVisible(hwnd):
            hits.append(hwnd)
        return True

    u32.EnumWindows(WNDENUMPROC(cb), 0)
    for h in hits:
        u32.PostMessageW(h, WM_CLOSE, 0, 0)
    return len(hits)


class Running:
    """Ein laufendes (oder schon beendetes) Programm."""

    def __init__(self, prog: dict, job=None, popen=None, handle=None, admin=False):
        self.prog_id = prog["id"]
        self.name = prog.get("name") or prog["id"]
        self.started = time.time()
        self.job = job
        self.popen = popen
        self.handle = handle      # nur bei Admin-Start ohne Job
        self.admin = admin
        self.ended: float | None = None
        # Prozessbaum-Verfolgung (falls Kindprozesse dem Job entkommen):
        # PID -> Startzeit; Kinder werden über ihre Eltern-PID erkannt.
        self.tree: dict[int, int] = {}
        self._lock = threading.Lock()
        root = popen.pid if popen else (k32.GetProcessId(handle) if handle else 0)
        if root and IS_WIN:
            ct = create_time(root)
            if ct:
                self.tree[root] = ct

    def _update_tree(self) -> list[int]:
        if not self.tree:
            return []
        with self._lock:
            procs = process_list()
            changed = True
            while changed:
                changed = False
                for pid, ppid in procs:
                    if ppid in self.tree and pid not in self.tree:
                        ct = create_time(pid)
                        if ct and ct >= self.tree[ppid]:  # schützt vor wiederverwendeten PIDs
                            self.tree[pid] = ct
                            changed = True
            alive = [pid for pid, ct in self.tree.items() if create_time(pid) == ct]
            # Tote Einträge behalten (ihre Kinder verweisen noch auf sie), aber nur begrenzt
            return alive

    # -- Abfragen ---------------------------------------------------------
    def pids(self) -> list[int]:
        found = set(self._job_pids())
        if IS_WIN:
            found |= set(self._update_tree())
        return sorted(found)

    def _job_pids(self) -> list[int]:
        if self.job:
            count = 256
            size = 8 + count * ctypes.sizeof(ctypes.c_size_t)
            buf = ctypes.create_string_buffer(size)
            if k32.QueryInformationJobObject(self.job, JobObjectBasicProcessIdList, buf, size, None):
                n = ctypes.c_uint32.from_buffer(buf, 4).value
                arr = (ctypes.c_size_t * n).from_buffer(buf, 8)
                return [int(x) for x in arr]
            return []
        if self.handle:
            if k32.WaitForSingleObject(self.handle, 0) == WAIT_TIMEOUT:
                return [k32.GetProcessId(self.handle)]
            return []
        if self.popen and self.popen.poll() is None:
            return [self.popen.pid]
        return []

    def alive(self) -> bool:
        alive = bool(self.pids())
        if not alive and self.ended is None:
            self.ended = time.time()
        return alive

    def runtime(self) -> float:
        return (self.ended or time.time()) - self.started

    # -- Beenden ----------------------------------------------------------
    def stop(self, grace: float = 3.0) -> bool:
        """Freundlich schließen, notfalls hart beenden. True, wenn danach nichts mehr läuft."""
        pids = set(self.pids())
        if not pids:
            return True
        if IS_WIN and _window_pids_close(pids):
            end = time.time() + grace
            while time.time() < end:
                if not self.pids():
                    return True
                time.sleep(0.1)
        return self.kill()

    def kill(self) -> bool:
        denied = False
        if self.job:
            k32.TerminateJobObject(self.job, 1)
        elif self.handle:
            if not k32.TerminateProcess(self.handle, 1):
                denied = True
        elif self.popen and not IS_WIN:
            self.popen.kill()
        for pid in self.pids():
            if not terminate_pid(pid):
                denied = True
        for _ in range(30):
            if not self.pids():
                return True
            time.sleep(0.1)
        if denied:
            raise LaunchError("Beenden verweigert (Programm läuft als Administrator?).")
        return not self.pids()

    def close(self):
        if self.popen:
            self.popen.poll()  # Rückgabecode abholen (vermeidet ResourceWarning)
        for h in (self.job, self.handle):
            if h:
                k32.CloseHandle(h)
        self.job = self.handle = None


def _watch_start(r: Running, seconds: float = 10.0) -> None:
    """In den ersten Sekunden oft nachsehen, damit kurzlebige Zwischenprozesse
    (z. B. cmd aus start.bat) ihre Kinder noch vererben können."""
    def loop():
        end = time.time() + seconds
        while time.time() < end and r.ended is None:
            try:
                r.pids()
            except OSError:
                pass
            time.sleep(0.15)
    threading.Thread(target=loop, daemon=True).start()


def launch(prog: dict) -> Running:
    r = _launch(prog)
    if IS_WIN:
        _watch_start(r)
    return r


def _launch(prog: dict) -> Running:
    command = (prog.get("command") or "").strip()
    if not command:
        raise LaunchError("Für dieses Programm ist kein Startbefehl hinterlegt.")
    cwd = prog.get("cwd") or prog.get("path") or None
    cwd = os.path.normpath(os.path.expandvars(cwd)) if cwd else None
    if cwd and not os.path.isdir(cwd):
        raise LaunchError(f"Ordner nicht gefunden: {cwd}")
    if prog.get("admin"):
        return _launch_admin(prog, command, cwd)
    try:
        return _launch_job(prog, resolve_command(command, cwd or "."), cwd, bool(prog.get("console")))
    except ElevationRequired:
        # e.g. a PyInstaller --uac-admin build: start it the way Explorer does, with a UAC prompt
        return _launch_admin(prog, command, cwd)


def _launch_job(prog: dict, command: str, cwd, console: bool) -> Running:
    flags = CREATE_NEW_PROCESS_GROUP | (CREATE_NEW_CONSOLE if console else CREATE_NO_WINDOW)
    if not IS_WIN:
        p = subprocess.Popen(command, cwd=cwd, shell=True)
        return Running(prog, popen=p)
    try:
        p = subprocess.Popen(command, cwd=cwd, creationflags=flags | CREATE_SUSPENDED,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as e:
        if getattr(e, "winerror", None) == ERROR_ELEVATION_REQUIRED:
            raise ElevationRequired(str(e)) from e
        raise LaunchError(f"Start fehlgeschlagen: {e}") from e
    job = k32.CreateJobObjectW(None, None)
    handle = wintypes.HANDLE(int(p._handle))  # noqa: SLF001 - Prozess-Handle von Popen
    if not job or not k32.AssignProcessToJobObject(job, handle):
        if job:
            k32.CloseHandle(job)
        job = None  # Fallback: nur den Hauptprozess verfolgen
    ntdll.NtResumeProcess(handle)
    return Running(prog, job=job, popen=p)


def _launch_admin(prog: dict, command: str, cwd) -> Running:
    if not IS_WIN:
        raise LaunchError("Start als Administrator gibt es nur unter Windows.")
    # cmd setzt das Arbeitsverzeichnis selbst, weil "runas" es nicht zuverlässig übernimmt
    params = f'/c cd /d "{cwd}" && {command}' if cwd else f"/c {command}"
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = "cmd.exe"
    info.lpParameters = params
    info.lpDirectory = cwd
    info.nShow = SW_SHOWNORMAL if prog.get("console") else 0
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == 1223:
            raise LaunchError("Start als Administrator wurde abgebrochen.")
        raise LaunchError(f"Start als Administrator fehlgeschlagen (Fehler {err}).")
    job = k32.CreateJobObjectW(None, None)
    if job and info.hProcess and k32.AssignProcessToJobObject(job, info.hProcess):
        k32.CloseHandle(info.hProcess)
        return Running(prog, job=job, admin=True)
    if job:
        k32.CloseHandle(job)
    return Running(prog, handle=info.hProcess, admin=True)


def run_captured(command: str, cwd: str, on_line, timeout: float | None = None) -> int:
    """Befehl ohne Fenster ausführen und Ausgabe zeilenweise an ``on_line`` geben."""
    flags = CREATE_NO_WINDOW if IS_WIN else 0
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    p = subprocess.Popen(resolve_command(command, cwd), cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=flags, env=env)
    assert p.stdout
    for raw in p.stdout:
        for enc in ("utf-8", "cp850", "cp1252"):
            try:
                line = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        on_line(line.rstrip("\r\n"))
    return p.wait(timeout)


# ------------------------------------------- fremd gestartete Programme erkennen
def process_commandlines(timeout: float = 20) -> list[dict]:
    """Alle Prozesse mit PID, Programmpfad und Befehlszeile (über WMI/CIM)."""
    if not IS_WIN:
        return []
    ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath,CommandLine "
          "| ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
                           stdin=subprocess.DEVNULL)
        import json
        data = json.loads(r.stdout.decode("utf-8", "replace") or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    if isinstance(data, dict):
        data = [data]
    return [{"pid": int(d.get("ProcessId") or 0), "exe": d.get("ExecutablePath") or "",
             "cmdline": d.get("CommandLine") or ""} for d in data if d.get("ProcessId")]


def stop_pids(pids: list[int], grace: float = 3.0) -> bool:
    """Fenster der Prozesse schließen, nach ``grace`` Sekunden hart beenden."""
    pids = [p for p in pids if p and p != os.getpid()]
    alive = [p for p in pids if create_time(p)]
    if not alive:
        return True
    if _window_pids_close(set(alive)):
        end = time.time() + grace
        while time.time() < end:
            alive = [p for p in alive if create_time(p)]
            if not alive:
                return True
            time.sleep(0.1)
    for p in alive:
        terminate_pid(p)
    time.sleep(0.3)
    return not any(create_time(p) for p in alive)
