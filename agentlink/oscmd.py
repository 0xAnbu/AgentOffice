"""Building command lines that survive every OS (standard library only).

The client writes its own command into files other programs parse: runtime hook settings (run by a shell),
the OpenCode plugin (JavaScript), a systemd unit, a launchd plist and a Windows scheduled task. The command is
kept as an argument list and quoted for each destination, so paths with spaces or non-ASCII characters work.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

IS_WINDOWS = os.name == "nt"
_PLAIN = re.compile(r"[A-Za-z0-9._:/=+-]+")
TASK_NAME = "AgentOffice\\agentlink-supervisor"


def agentlink_argv() -> list[str]:
    """Command that runs this package's CLI (works from a venv, pipx or a checkout)."""
    exe = shutil.which("agentlink")
    return [exe] if exe else [sys.executable, "-m", "agentlink.cli"]


def shell_command(argv: list[str], windows: bool = IS_WINDOWS) -> str:
    """One command line for a runtime's hook settings, which hand it to a shell.

    POSIX: ``sh`` quoting. Windows: runtimes run hooks through Git Bash or ``cmd.exe``. Only the program is
    double-quoted, so the line holds exactly two quotes (``cmd /c`` keeps them; with more it strips the first and
    the last) and Git Bash leaves its backslashes alone (inside double quotes it only escapes ``$ ` " \\``).
    The other words are plain (``-m``, ``agentlink.cli``, ``hook``, runtime and event names); anything else
    is rejected rather than quoted in a way one of the two shells would misread. PowerShell is not targeted.
    """
    if not windows:
        return shlex.join(argv)
    exe, rest = argv[0], argv[1:]
    if any(c in exe for c in '"$`') or exe.startswith("\\\\") or any(not _PLAIN.fullmatch(a) for a in rest):
        raise ValueError(f"cannot write a hook command for both cmd.exe and Git Bash: {argv!r}")
    return " ".join([f'"{exe}"', *rest])


def systemd_quote(argv: list[str]) -> str:
    """An ``ExecStart=`` value: double-quoted words with C escapes; ``%`` doubled (systemd specifiers)."""
    return " ".join('"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"' for a in argv)


def js_array(argv: list[str]) -> str:
    """A JavaScript array literal (JSON is valid JavaScript)."""
    return json.dumps(argv, ensure_ascii=False)


def batch_shim(argv0: str) -> bool:
    """True if a program resolves to a .cmd/.bat file on Windows (npm installs runtimes that way).

    Windows runs those through ``cmd.exe``, which re-parses the arguments: message text from other agents must
    never reach it (``&``, ``|`` ... would run commands). Callers refuse to pass untrusted text to them.
    """
    if not IS_WINDOWS:
        return False
    found = shutil.which(argv0) or argv0
    return Path(found).suffix.lower() in (".cmd", ".bat")


# -- Windows: per-user scheduled task and file permissions --------------------------------------------------


def windowless(argv: list[str]) -> list[str]:
    """Prefer ``pythonw.exe -m agentlink.cli`` so the supervisor does not open a console window at logon."""
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return [str(pyw), "-m", "agentlink.cli"] if pyw.exists() else argv


def task_xml(argv: list[str], user: str) -> str:
    """Task Scheduler definition: runs as the current user at logon, restarts on failure, never times out."""
    args = subprocess.list2cmdline(argv[1:])
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Agent Office local supervisor (agentlink)</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
  <Principals>
    <Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(argv[0])}</Command><Arguments>{escape(args)}</Arguments></Exec></Actions>
</Task>
"""


def windows_user() -> str:
    domain, name = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    return f"{domain}\\{name}" if domain else name


def restrict_to_user(path: Path) -> bool:
    """Windows ignores ``chmod``: drop inherited ACEs and grant only the current user (inheritable for dirs)."""
    if not IS_WINDOWS or not shutil.which("icacls"):
        return False
    grant = f"{windows_user()}:(OI)(CI)F" if path.is_dir() else f"{windows_user()}:F"
    r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant], capture_output=True, check=False)
    return r.returncode == 0
