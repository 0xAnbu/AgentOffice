"""Command lines that survive every OS: paths with spaces / non-ASCII, the Windows service, ACLs, batch shims.

Windows-only branches are exercised here by simulating the platform (no Windows host is needed); the real
Windows run is the `windows` CI job (tests/test_client_smoke.py).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from agentlink import adapters, clientcli, local, oscmd, supervisor
from agentlink.adapters import ADAPTERS

SPACED = ["/opt/my tools/ñandú/bin/python3", "-m", "agentlink.cli"]
WIN = [r"C:\Program Files\Python 3.12\python.exe", "-m", "agentlink.cli"]


class Calls:
    """Records subprocess calls instead of running them."""

    def __init__(self, codes: dict[str, int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.codes = codes or {}

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self.codes.get(" ".join(cmd[:2]), 0), "", "")


# -- quoting ------------------------------------------------------------------------------------------------


def test_posix_shell_command_round_trips() -> None:
    assert shlex.split(oscmd.shell_command([*SPACED, "hook", "x"], windows=False)) == [*SPACED, "hook", "x"]


def test_windows_shell_command_is_quoted_for_git_bash_and_cmd() -> None:
    cmd = oscmd.shell_command([*WIN, "hook", "codex", "Stop"], windows=True)
    assert cmd == '"C:\\Program Files\\Python 3.12\\python.exe" -m agentlink.cli hook codex Stop'
    assert cmd.count('"') == 2  # cmd /c keeps exactly two quotes around an existing program
    assert shlex.split(cmd) == [*WIN, "hook", "codex", "Stop"]  # Git Bash (sh rules) keeps the backslashes
    assert adapters.ours(cmd)  # still recognised as ours (install/uninstall/doctor)
    for bad in (["C:\\a$b\\python.exe", "hook"], ["\\\\srv\\share\\python.exe", "hook"], [*WIN, "a b"]):
        with pytest.raises(ValueError):
            oscmd.shell_command(bad, windows=True)


def test_systemd_exec_start_quoting() -> None:
    q = oscmd.systemd_quote(["/home/a b/100%/agentlink", 'say "hi"\\'])
    assert q == '"/home/a b/100%%/agentlink" "say \\"hi\\"\\\\"'
    assert [w.replace("%%", "%") for w in shlex.split(q)] == ["/home/a b/100%/agentlink", 'say "hi"\\']


# -- hook settings ------------------------------------------------------------------------------------------


@pytest.fixture
def spaced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("AGENTLINK_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(adapters, "agentlink_argv", lambda: list(SPACED))
    proj = tmp_path / "repo"
    (proj / ".claude").mkdir(parents=True)
    return proj


def _commands(path: Path) -> dict[str, list[str]]:
    data = json.loads(path.read_text("utf-8"))
    return {ev: [h["command"] for e in es for h in e["hooks"]] for ev, es in data.get("hooks", {}).items()}


def test_hook_commands_survive_spaces_and_non_ascii(spaced: Path) -> None:
    ADAPTERS["claude-code"].install("project", str(spaced))
    cmds = _commands(spaced / ".claude" / "settings.json")
    assert shlex.split(cmds["Stop"][0]) == [*SPACED, "hook", "claude-code", "Stop"]
    assert "ñandú" in cmds["SessionStart"][0]


def test_reinstall_upgrades_an_old_unquoted_install_and_keeps_foreign_hooks(spaced: Path) -> None:
    settings = spaced / ".claude" / "settings.json"
    foreign = {"type": "command", "command": "notify-send done"}
    legacy = {"type": "command", "command": "/opt/my tools/agentlink hook claude-code Stop", "timeout": 15}
    original = {"model": "x", "hooks": {"Stop": [{"hooks": [foreign, legacy]}]}}
    settings.write_text(json.dumps(original))
    ad = ADAPTERS["claude-code"]
    assert "Stop" in ad.install("project", str(spaced))["added"]
    cmds = _commands(settings)
    assert cmds["Stop"] == ["notify-send done", oscmd.shell_command([*SPACED, "hook", "claude-code", "Stop"])]
    assert ad.install("project", str(spaced))["added"] == []  # idempotent once upgraded
    assert ad.doctor("project", str(spaced))["hooks_missing"] == []
    ad.uninstall("project", str(spaced))
    assert json.loads(settings.read_text()) == {"model": "x", "hooks": {"Stop": [{"hooks": [foreign]}]}}


def test_legacy_install_is_removed_by_uninstall(spaced: Path) -> None:
    settings = spaced / ".claude" / "settings.json"
    legacy = {"hooks": [{"type": "command", "command": "/usr/bin/python3 -m agentlink.cli hook claude-code Stop"}]}
    settings.write_text(json.dumps({"hooks": {"Stop": [legacy]}}))
    assert ADAPTERS["claude-code"].uninstall("project", str(spaced))["removed"] == ["Stop"]
    assert json.loads(settings.read_text()) == {}


# -- OpenCode plugin ----------------------------------------------------------------------------------------


def test_opencode_plugin_spawns_the_argv_without_a_shell(spaced: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(spaced / "cfg"))
    ad = ADAPTERS["opencode"]
    ad.install()
    js = ad.plugin_path().read_text("utf-8")
    assert f"const AGENTLINK = {json.dumps(SPACED, ensure_ascii=False)};" in js
    assert 'spawn("sh"' not in js and "-c" not in js and "__AGENTLINK__" not in js
    assert ad.uninstall()["removed"] == [str(ad.plugin_path())]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_opencode_plugin_reports_activity_through_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real plugin under node: the event reaches a fake `agentlink` whose path has spaces."""
    bindir = tmp_path / "bin dir ñ"
    bindir.mkdir()
    record = tmp_path / "argv.json"
    fake = bindir / "fake agentlink.py"
    fake.write_text(f"import json, sys\nopen({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n", "utf-8")
    monkeypatch.setattr(adapters, "agentlink_argv", lambda: [sys.executable, str(fake)])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("AGENTLINK_HOME", str(tmp_path / "home"))
    ad = ADAPTERS["opencode"]
    ad.install()
    plugin = ad.plugin_path().with_suffix(".mjs")
    shutil.copy(ad.plugin_path(), plugin)
    script = (
        f"const m = await import({json.dumps(plugin.as_uri())});"
        "const hooks = await m.AgentOffice();"
        'await hooks["tool.execute.after"]({sessionID: "s 1", tool: "bash & echo pwned"});'
        "await new Promise((r) => setTimeout(r, 1500));"
    )
    subprocess.run(["node", "--input-type=module", "-e", script], check=True, timeout=30)
    for _ in range(50):
        if record.exists() and record.stat().st_size:
            break
        __import__("time").sleep(0.1)
    assert json.loads(record.read_text("utf-8")) == [
        "activity", "--runtime", "opencode", "--local-id", "s 1", "--kind", "tool", "--detail", "bash & echo pwned"
    ]  # fmt: skip


# -- supervisor service --------------------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "user home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user home"))
    monkeypatch.setenv("AGENTLINK_HOME", str(tmp_path / "agent home ñ"))
    (tmp_path / "agent home ñ").mkdir()
    monkeypatch.setattr(clientcli, "agentlink_argv", lambda: list(SPACED))
    return tmp_path


def test_systemd_unit_quotes_exec_start_and_home(svc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    monkeypatch.setattr(clientcli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(clientcli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(clientcli.subprocess, "run", calls)
    assert clientcli.install_service()["mechanism"] == "systemd --user"
    unit = (svc / "user home" / ".config" / "systemd" / "user" / "agentlink-supervisor.service").read_text("utf-8")
    exec_start = next(x for x in unit.splitlines() if x.startswith("ExecStart="))[len("ExecStart=") :]
    env = next(x for x in unit.splitlines() if x.startswith("Environment="))[len("Environment=") :]
    assert shlex.split(exec_start) == [*SPACED, "supervise"]
    assert shlex.split(env) == [f"AGENTLINK_HOME={svc / 'agent home ñ'}"]
    clientcli.remove_service()
    assert ["systemctl", "--user", "disable", "--now", "agentlink-supervisor.service"] in calls.calls


def test_launchd_plist_keeps_arguments_apart(svc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clientcli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(clientcli.subprocess, "run", Calls())
    clientcli.install_service()
    plist = svc / "user home" / "Library" / "LaunchAgents" / "dev.agentoffice.supervisor.plist"
    args = [e.text for arr in ET.parse(plist).getroot().iter("array") for e in arr]
    assert args == [*SPACED, "supervise", "--home", str(svc / "agent home ñ")]


def _simulate_windows(monkeypatch: pytest.MonkeyPatch, calls: Calls) -> None:
    monkeypatch.setattr(clientcli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(clientcli, "IS_WINDOWS", True)
    monkeypatch.setattr(clientcli.shutil, "which", lambda name: f"C:\\Windows\\System32\\{name}.exe")
    monkeypatch.setattr(clientcli.subprocess, "run", calls)
    monkeypatch.setenv("USERDOMAIN", "LAPTOP")
    monkeypatch.setenv("USERNAME", "ana")


def test_windows_supervisor_is_a_per_user_logon_task(svc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    _simulate_windows(monkeypatch, calls)
    monkeypatch.setattr(clientcli, "windowless", lambda argv: [r"C:\Py 3\pythonw.exe", "-m", "agentlink.cli"])
    res = clientcli.install_service()
    assert res == {"mechanism": "Task Scheduler", "task": oscmd.TASK_NAME}
    xml_path = svc / "agent home ñ" / clientcli.TASK_XML
    create = ["schtasks", "/Create", "/TN", oscmd.TASK_NAME, "/XML", str(xml_path), "/F"]
    assert calls.calls == [create, ["schtasks", "/Run", "/TN", oscmd.TASK_NAME]]
    raw = xml_path.read_bytes()
    assert raw[:2] == b"\xff\xfe"  # UTF-16 LE with BOM, as schtasks expects
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    root = ET.fromstring(raw.decode("utf-16").split("?>", 1)[1])
    assert root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=ns) == "LAPTOP\\ana"
    assert root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns) == "LeastPrivilege"
    assert root.findtext("t:Settings/t:RestartOnFailure/t:Count", namespaces=ns) == "999"
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=ns) == "PT0S"
    assert root.findtext("t:Actions/t:Exec/t:Command", namespaces=ns) == r"C:\Py 3\pythonw.exe"
    home_arg = subprocess.list2cmdline([str(svc / "agent home ñ")])
    assert (
        root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns) == f"-m agentlink.cli supervise --home {home_arg}"
    )

    calls.calls.clear()
    assert clientcli.remove_service()["removed"] == [f"task {oscmd.TASK_NAME}"]
    assert [c[1] for c in calls.calls] == ["/Query", "/End", "/Delete"]
    assert not xml_path.exists()


def test_windows_falls_back_to_a_really_detached_process(svc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _simulate_windows(monkeypatch, Calls({"schtasks /Create": 1}))
    seen: dict[str, Any] = {}

    class FakePopen:
        pid = 4242

        def __init__(self, argv: list[str], **kw: Any) -> None:
            seen.update(argv=argv, **kw)

    monkeypatch.setattr(clientcli.subprocess, "Popen", FakePopen)
    res = clientcli.install_service()
    assert res["mechanism"] == "detached process" and seen["argv"][: len(SPACED) + 1] == [*SPACED, "supervise"]
    assert seen["creationflags"] == 0x8 | 0x200 and "start_new_session" not in seen
    assert not (svc / "agent home ñ" / clientcli.TASK_XML).exists()
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(clientcli.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(clientcli, "IS_WINDOWS", False)
    assert "detached process" in clientcli.remove_service()["removed"] and kills == [(4242, clientcli.signal.SIGTERM)]


def test_supervise_home_flag_and_windowless_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "h ñ"
    home.mkdir()
    monkeypatch.delenv("AGENTLINK_HOME", raising=False)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(clientcli, "api_from_config", lambda cfg: seen.setdefault("cfg", cfg))

    class FakeSup:
        def __init__(self, *a: Any, **kw: Any) -> None:
            seen["home"] = os.environ["AGENTLINK_HOME"]

        def run_forever(self) -> None:
            print("supervisor running")

    monkeypatch.setattr("agentlink.supervisor.Supervisor", FakeSup)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        clientcli.main(["supervise", "--home", str(home)])
    finally:
        stream = sys.stdout
        sys.stdout = sys.__stdout__
        root.handlers[:], root.level = handlers, level
        if stream is not None:
            stream.close()
    assert seen["home"] == str(home) and "supervisor running" in (home / "supervisor.log").read_text("utf-8")


# -- Windows file permissions and batch shims ----------------------------------------------------------------


def test_token_files_get_an_owner_only_acl_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    monkeypatch.setattr(oscmd, "IS_WINDOWS", True)
    monkeypatch.setattr(local, "IS_WINDOWS", True)
    monkeypatch.setattr(oscmd.shutil, "which", lambda name: "C:\\Windows\\System32\\icacls.exe")
    monkeypatch.setattr(oscmd.subprocess, "run", calls)
    monkeypatch.setenv("USERDOMAIN", "LAPTOP")
    monkeypatch.setenv("USERNAME", "ana")
    monkeypatch.setenv("AGENTLINK_HOME", str(tmp_path / "home"))
    path = local.save_config({"token": "al_secret"})
    d = str(tmp_path / "home")
    assert calls.calls == [
        ["icacls", d, "/inheritance:r", "/grant:r", "LAPTOP\\ana:(OI)(CI)F"],  # created
        ["icacls", d, "/inheritance:r", "/grant:r", "LAPTOP\\ana:(OI)(CI)F"],  # re-applied by save_config
        ["icacls", str(path), "/inheritance:r", "/grant:r", "LAPTOP\\ana:F"],
    ]
    calls.calls.clear()
    local._private_dir(tmp_path / "home")  # hooks call this often: no process per call once created
    assert calls.calls == []


def test_message_text_never_reaches_a_batch_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oscmd, "IS_WINDOWS", True)
    monkeypatch.setattr(oscmd.shutil, "which", lambda name: f"C:\\Users\\ana\\AppData\\Roaming\\npm\\{name}.CMD")
    ran = Calls()
    monkeypatch.setattr(supervisor.subprocess, "run", ran)
    res = supervisor._run(["codex", "exec", "hi & calc.exe"], None, 5)
    assert res.returncode == 126 and "batch file" in res.stderr and ran.calls == []
    monkeypatch.setattr(oscmd.shutil, "which", lambda name: f"C:\\tools\\{name}.exe")
    assert supervisor._run(["codex", "exec", "hi"], None, 5).returncode == 0 and len(ran.calls) == 1


def test_auto_resume_refuses_a_batch_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINK_HOME", str(tmp_path))
    monkeypatch.setattr(oscmd, "IS_WINDOWS", True)
    monkeypatch.setattr(oscmd.shutil, "which", lambda name: "C:\\npm\\claude.cmd")
    started: list[Any] = []
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *a, **kw: started.append(a))
    store = local.Store()
    store.put("adapter.claude-code", {"auto_resume": True})
    assert ADAPTERS["claude-code"].push(store, {"local_id": "s1"}, "hi & calc") is None and started == []
