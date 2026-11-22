"""Agent Office client commands (local supervisor, onboarding, runtime integration). JSON on stdout.

agentlink connect --server URL --token TOKEN [--project P] [--runtime auto] [--specialization S]
agentlink status | doctor | disconnect [--purge]
agentlink supervise [--once]            the model-independent local supervisor
agentlink install|uninstall <runtime>   reversible hook/plugin installation (backups in ~/.agentlink/backups)
agentlink inbox --pending               what the local store holds for the runtime
agentlink ack <id>... | reply <id> TEXT | show <id>
agentlink tasks [channel] | claim <task_id> | renew <task_id> | release <task_id> | checkpoint <task_id> ...
agentlink check-lease <task_id>         pre-push guard: exit 3 if this session no longer holds the task
agentlink session start|end|beat        for generic wrappers;  agentlink activity ...  (plugins/wrappers)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn
from xml.sax.saxutils import escape as xml_escape

from .adapters import ADAPTERS, detect_runtime
from .local import VERSION, Api, ApiError, Store, api_from_config, home, load_config, save_config
from .oscmd import IS_WINDOWS, TASK_NAME, agentlink_argv, systemd_quote, task_xml, windowless, windows_user

COMMANDS = {
    "connect", "disconnect", "status", "doctor", "supervise", "hook", "install", "uninstall", "ack", "reply", "show",
    "claim", "renew", "release", "checkpoint", "check-lease", "activity", "session", "tasks", "inbox",
}  # fmt: skip


def out(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _fail(msg: str, code: int = 2, **extra: Any) -> NoReturn:
    out({"ok": False, "error": msg, **extra})
    sys.exit(code)


def _session(store: Store, explicit: str | None) -> str | None:
    """Which server session acts: --session, $AGENTLINK_SESSION, else the most recently active local session."""
    if explicit:
        return explicit
    if os.environ.get("AGENTLINK_SESSION"):
        return os.environ["AGENTLINK_SESSION"]
    rows = store.q(
        "SELECT server_id FROM sessions WHERE ended_at IS NULL AND server_id <> '' ORDER BY COALESCE(last_activity, started_at) DESC LIMIT 1"
    )
    return rows[0]["server_id"] if rows else None


def _channel(api: Api, cfg: dict[str, Any], explicit: str | None) -> str:
    if explicit:
        return explicit
    if cfg.get("channel"):
        return str(cfg["channel"])
    if cfg.get("project"):
        proj = api.call("GET", f"/api/projects/{cfg['project']}")
        if proj.get("channels"):
            return str(proj["channels"][0]["name"])
    chans = api.call("GET", "/api/channels")
    if not chans:
        _fail("no channel available; pass one explicitly")
    return str(chans[0]["name"])


# -- service management (participant-initiated, reversible) -----------------------------------------------
UNIT = """[Unit]
Description=Agent Office local supervisor (agentlink)
After=network-online.target

[Service]
ExecStart={exec_start}
Restart=always
RestartSec=5
Environment={env}

[Install]
WantedBy=default.target
"""
TASK_XML = "supervisor-task.xml"


def _supervise_argv(base: list[str] | None = None) -> list[str]:
    """`agentlink supervise` as an argument list; a non-default AGENTLINK_HOME travels as `--home`."""
    argv = [*(base or agentlink_argv()), "supervise"]
    if os.environ.get("AGENTLINK_HOME"):
        argv += ["--home", str(home())]
    return argv


def _quiet(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def install_service() -> dict[str, Any]:
    """Keep the supervisor alive with the OS's own mechanism; never assume one that is not there."""
    system = platform.system()
    if system == "Linux" and shutil.which("systemctl"):
        unit = Path.home() / ".config" / "systemd" / "user" / "agentlink-supervisor.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        exec_start = systemd_quote([*agentlink_argv(), "supervise"])
        unit.write_text(UNIT.format(exec_start=exec_start, env=systemd_quote([f"AGENTLINK_HOME={home()}"])), "utf-8")
        if _quiet(["systemctl", "--user", "enable", "--now", "agentlink-supervisor.service"]).returncode == 0:
            return {"mechanism": "systemd --user", "unit": str(unit)}
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "dev.agentoffice.supervisor.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"<string>{xml_escape(x)}</string>" for x in _supervise_argv())
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key>
<string>dev.agentoffice.supervisor</string><key>ProgramArguments</key><array>{args}</array><key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/></dict></plist>""", "utf-8")  # fmt: skip
        subprocess.run(["launchctl", "load", "-w", str(plist)], check=False)
        return {"mechanism": "launchd", "plist": str(plist)}
    if system == "Windows" and shutil.which("schtasks"):
        # a per-user task: no admin rights, starts at logon, restarted by Task Scheduler if it fails
        xml = home() / TASK_XML
        xml.write_text(task_xml(_supervise_argv(windowless(agentlink_argv())), windows_user()), encoding="utf-16")
        r = _quiet(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml), "/F"])
        if r.returncode == 0:
            _quiet(["schtasks", "/Run", "/TN", TASK_NAME])
            return {"mechanism": "Task Scheduler", "task": TASK_NAME}
        xml.unlink(missing_ok=True)
    # no service manager (or it refused): a detached background process, not restarted after a reboot
    logf = open(home() / "supervisor.log", "a")  # noqa: SIM115
    if IS_WINDOWS:  # start_new_session means nothing on Windows; without these flags it dies with the console
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        p = subprocess.Popen(_supervise_argv(), stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, creationflags=flags)
    else:
        p = subprocess.Popen(_supervise_argv(), stdout=logf, stderr=logf, start_new_session=True)
    (home() / "supervisor.pid").write_text(str(p.pid))
    return {
        "mechanism": "detached process",
        "pid": p.pid,
        "note": "not restarted after reboot; see docs/AGENT_INSTALLATION.md",
    }


def remove_service() -> dict[str, Any]:
    removed = []
    unit = Path.home() / ".config" / "systemd" / "user" / "agentlink-supervisor.service"
    if unit.exists():
        _quiet(["systemctl", "--user", "disable", "--now", "agentlink-supervisor.service"])
        unit.unlink()
        removed.append(str(unit))
    plist = Path.home() / "Library" / "LaunchAgents" / "dev.agentoffice.supervisor.plist"
    if plist.exists():
        subprocess.run(["launchctl", "unload", "-w", str(plist)], check=False)
        plist.unlink()
        removed.append(str(plist))
    if IS_WINDOWS and shutil.which("schtasks") and _quiet(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0:
        _quiet(["schtasks", "/End", "/TN", TASK_NAME])
        _quiet(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        removed.append(f"task {TASK_NAME}")
    (home() / TASK_XML).unlink(missing_ok=True)
    pid = home() / "supervisor.pid"
    if pid.exists():
        try:
            os.kill(int(pid.read_text()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
        pid.unlink()
        removed.append("detached process")
    return {"removed": removed}


# -- commands ----------------------------------------------------------------------------------------------
def cmd_connect(a: argparse.Namespace) -> None:
    api = Api(a.server, a.token)
    checks: dict[str, Any] = {}
    try:
        checks["health"] = api.call("GET", "/api/health")
        me = api.call("GET", "/api/me")
    except (ApiError, OSError) as e:
        _fail(f"cannot reach or authenticate against {a.server}: {e}")
    runtime = detect_runtime() if a.runtime == "auto" else a.runtime
    if runtime not in ADAPTERS:
        _fail(f"unknown runtime {runtime}", runtime_options=sorted(ADAPTERS))
    projects = [p["name"] for p in me.get("projects", [])]
    project = a.project or (projects[0] if len(projects) == 1 else None)
    if project is None:
        _fail("you belong to several projects: pass --project", projects=projects)
    cfg = load_config()
    cfg.update({"server": a.server.rstrip("/"), "token": a.token, "project": project, "agent": me["agent"], "runtime": runtime,
                "specialization": a.specialization or me.get("specialization") or "", "device": a.device or cfg.get("device")})  # fmt: skip
    path = save_config(cfg)
    store = Store()
    result: dict[str, Any] = {"ok": True, "agent": me["agent"], "owner": me.get("owner"), "project": project, "config": str(path),
                              "runtime": runtime}  # fmt: skip
    ad = ADAPTERS[runtime]
    if not a.no_install:
        result["install"] = ad.install(a.scope, a.dir)
    adapter_cfg = dict(store.get(f"adapter.{runtime}", {}) or {})
    if a.auto_resume:
        adapter_cfg["auto_resume"] = True
    if a.headless_delivery:
        if runtime not in ("codex", "opencode"):
            _fail("--headless-delivery is only available for codex and opencode")
        adapter_cfg.update({"headless": True, "workdir": os.path.abspath(a.workdir or os.getcwd())})
    if a.model:
        adapter_cfg["model"] = a.model
    store.put(f"adapter.{runtime}", adapter_cfg)
    api.call(
        "POST",
        "/api/devices/heartbeat",
        {"device": cfg["device"], "supervisor_version": VERSION, "supervisor_ok": False},
    )
    if not a.no_service:
        result["service"] = install_service()
    result["doctor"] = ad.doctor(a.scope, a.dir)
    result["unavailable"] = {k: v for k, v in ad.capabilities.items() if v in ("UNSUPPORTED", "UNKNOWN")}
    out(result)


def cmd_disconnect(a: argparse.Namespace) -> None:
    cfg = load_config()
    store = Store()
    api = None
    try:
        api = api_from_config(cfg)
    except SystemExit:
        pass
    ended = []
    for s in store.live_sessions():
        if api and s["server_id"]:
            try:
                api.call("POST", f"/api/sessions/{s['server_id']}/end", {"reason": "disconnect"})
            except ApiError:
                pass
        store.x(
            "UPDATE sessions SET ended_at = ?, end_reason = 'disconnect' WHERE local_id = ?",
            (time.time(), s["local_id"]),
        )
        ended.append(s["server_id"])
    res: dict[str, Any] = {"ok": True, "sessions_ended": ended, "service": remove_service()}
    runtime = cfg.get("runtime")
    if runtime in ADAPTERS and not a.keep_hooks:
        res["uninstall"] = ADAPTERS[runtime].uninstall(a.scope, a.dir)
    if a.purge:
        for f in ("config.json", "state.db", "state.db-wal", "state.db-shm"):
            (home() / f).unlink(missing_ok=True)
        res["purged"] = str(home())
    out(res)


def cmd_status(a: argparse.Namespace) -> None:
    cfg = load_config()
    store = Store()
    st = store.get("supervisor_status") or {}
    age = time.time() - st["at"] if st.get("at") else None
    sessions = store.live_sessions()
    counts = {r["state"]: r["n"] for r in store.q("SELECT state, COUNT(*) AS n FROM inbox GROUP BY state")}
    out({
        "device": cfg.get("device"), "server": cfg.get("server"), "project": cfg.get("project"), "agent": cfg.get("agent"),
        "supervisor_running": bool(age is not None and age < 120), "supervisor_last_iteration_s": round(age, 1) if age else None,
        "runtime": cfg.get("runtime"), "runtime_available": ADAPTERS.get(cfg.get("runtime") or "generic", ADAPTERS["generic"]).detect().get("available"),
        "sessions": [{"local_id": s["local_id"], "server_id": s["server_id"], "state": s["state"],
                      "last_activity_s": round(time.time() - (s["last_activity"] or s["started_at"]), 1)} for s in sessions],
        "inbox": counts, "held_tasks": store.q("SELECT task_id, epoch, lost_reason FROM held"),
        "last_error": store.get("supervisor_last_error"), "last_hook_error": store.get("last_hook_error"),
    })  # fmt: skip


def cmd_doctor(a: argparse.Namespace) -> None:
    cfg = load_config()
    res: dict[str, Any] = {"config": {k: ("***" if k == "token" else v) for k, v in cfg.items()}}
    try:
        api = api_from_config(cfg)
        res["server"] = api.call("GET", "/api/health")
        res["me"] = api.call("GET", "/api/me")
        res["server_ok"] = True
    except (ApiError, OSError, SystemExit) as e:
        res["server_ok"] = False
        res["server_error"] = str(e)
    res["runtimes"] = {name: ad.doctor(a.scope, a.dir) for name, ad in ADAPTERS.items() if name != "synthetic"}
    st = Store().get("supervisor_status") or {}
    res["supervisor_running"] = bool(st.get("at") and time.time() - st["at"] < 120)
    out(res)


def cmd_supervise(a: argparse.Namespace) -> None:
    from .supervisor import Supervisor

    if a.home:  # the service manager cannot always pass environment variables (Task Scheduler, launchd)
        os.environ["AGENTLINK_HOME"] = a.home
    if sys.stdout is None or sys.stderr is None:  # pythonw.exe (Windows task): no console, log to a file
        sys.stdout = sys.stderr = open(home() / "supervisor.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    cfg = load_config()
    sup = Supervisor(api_from_config(cfg), Store(), cfg, poll_wait=a.wait)
    if a.once:
        sup.reconcile_pending()
        out(sup.once(wait=a.wait))
        return
    sup.run_forever()


def cmd_install(a: argparse.Namespace, remove: bool = False) -> None:
    ad = ADAPTERS.get(a.runtime)
    if ad is None:
        _fail(f"unknown runtime {a.runtime}")
    out(ad.uninstall(a.scope, a.dir) if remove else ad.install(a.scope, a.dir))


def cmd_inbox_pending(a: argparse.Namespace) -> None:
    store = Store()
    rows = store.pending() if not a.all else store.q("SELECT * FROM inbox ORDER BY message_id DESC LIMIT 100")
    out([{k: r[k] for k in ("message_id", "channel", "author", "kind", "priority", "state", "body")} for r in rows])


def _report(
    api: Api, store: Store, ids: list[int], state: str, sid: str | None, evidence: dict[str, Any] | None = None
) -> Any:
    real = [i for i in ids if i > 0]
    store.mark(ids, state, sid, evidence=evidence)  # kept so a failed POST is retried with the same evidence
    if not real:
        return {"local_only": ids}
    r = api.call(
        "POST", "/api/deliveries", {"message_ids": real, "state": state, "session_id": sid, "evidence": evidence}
    )
    store.set_reported(real, state)
    return r


def cmd_ack(a: argparse.Namespace) -> None:
    api, store = api_from_config(), Store()
    out(_report(api, store, a.ids, "acknowledged", _session(store, a.session)))


def cmd_reply(a: argparse.Namespace) -> None:
    api, store = api_from_config(), Store()
    sid = _session(store, a.session)
    rows = store.q("SELECT channel, author FROM inbox WHERE message_id = ?", (a.id,))
    channel = a.channel or (rows[0]["channel"] if rows else None) or _channel(api, load_config(), None)
    author = rows[0]["author"] if rows else None  # the asker gets the answer in their inbox
    msg = api.call("POST", f"/api/channels/{channel}/messages", {"body": a.text, "reply_to": a.id, "kind": a.kind,
                   "data": {"session_id": sid}, "mentions": [author] if author else None})  # fmt: skip
    rep = _report(api, store, [a.id], "processed", sid, {"reply_message_id": msg["id"]})
    out({"ok": True, "reply": msg["id"], "delivery": rep})


def cmd_show(a: argparse.Namespace) -> None:
    store = Store()
    rows = store.q("SELECT * FROM inbox WHERE message_id = ?", (a.id,))
    if not rows:
        _fail(f"message {a.id} not in the local store")
    r = rows[0]
    api = api_from_config()
    thread = (
        api.call(
            "GET",
            f"/api/channels/{r['channel']}/messages",
            query={"thread": r["thread_id"] or r["message_id"], "limit": 50},
        )
        if r["channel"] != "local"
        else {}
    )
    out({"message": r, "thread": thread.get("messages", [])})


def cmd_tasks(a: argparse.Namespace) -> None:
    cfg = load_config()
    api = api_from_config(cfg)
    out(
        api.call(
            "GET",
            f"/api/channels/{_channel(api, cfg, a.channel)}/tasks",
            query={"status": a.status, "all": "1" if a.all else None},
        )
    )


def cmd_claim(a: argparse.Namespace) -> None:
    api, store = api_from_config(), Store()
    sid = _session(store, a.session)
    try:
        t = api.call(
            "POST",
            f"/api/tasks/{a.task_id}/claim",
            {"session_id": sid, "ttl_seconds": a.ttl} if sid else {"ttl_seconds": a.ttl},
        )
    except ApiError as e:
        _fail(e.payload.get("message", str(e)), 1, **{k: v for k, v in e.payload.items() if k != "message"})
    if sid:
        store.x("INSERT INTO held(task_id, server_session, epoch, claimed_at) VALUES (?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE "
                "SET server_session = excluded.server_session, epoch = excluded.epoch, lost_reason = NULL",
                (a.task_id, sid, t["lease_epoch"], time.time()))  # fmt: skip
    out(t)


def _held(store: Store, task_id: int, session: str | None) -> dict[str, Any]:
    rows = store.q("SELECT * FROM held WHERE task_id = ?", (task_id,))
    if rows:
        return {"session_id": session or rows[0]["server_session"], "lease_epoch": rows[0]["epoch"]}
    return {"session_id": session} if session else {}


def _holder_call(a: argparse.Namespace, method: str, path: str, body: dict[str, Any]) -> None:
    api, store = api_from_config(), Store()
    fence = _held(store, a.task_id, a.session or os.environ.get("AGENTLINK_SESSION"))
    try:
        res = api.call(method, path, {**fence, **body})
    except ApiError as e:
        if e.reason == "stale_lease":
            store.x("UPDATE held SET lost_reason = 'stale_lease' WHERE task_id = ?", (a.task_id,))
        _fail(e.payload.get("message", str(e)), 1, **{k: v for k, v in e.payload.items() if k != "message"})
    out(res)


def cmd_check_lease(a: argparse.Namespace) -> None:
    api, store = api_from_config(), Store()
    fence = _held(store, a.task_id, a.session or os.environ.get("AGENTLINK_SESSION"))
    r = api.call("GET", f"/api/tasks/{a.task_id}/lease", query=fence)
    out(r)
    if not r.get("holds_lease"):
        sys.exit(3)


def cmd_activity(a: argparse.Namespace) -> None:
    """Called by plugins/wrappers: record observable runtime activity for a local session (registers it if new)."""
    store = Store()
    if a.kind == "session.start" or not store.session(a.local_id):
        from .hooks import _ensure_session

        cfg = load_config()
        try:
            api = api_from_config(cfg)
        except SystemExit:
            api = None
        _ensure_session(store, api, cfg, a.runtime, {"session_id": a.local_id, "cwd": os.getcwd()})
    if a.kind == "session.end":
        store.x(
            "UPDATE sessions SET ended_at = ?, end_reason = 'runtime ended' WHERE local_id = ?",
            (time.time(), a.local_id),
        )
    elif a.kind == "idle":
        store.x("UPDATE sessions SET state = 'idle' WHERE local_id = ?", (a.local_id,))
    else:
        store.x("UPDATE sessions SET state = 'executing' WHERE local_id = ?", (a.local_id,))
        store.activity(a.local_id, a.kind, a.detail or "", progress=a.kind == "tool")
    out({"ok": True})


def cmd_session(a: argparse.Namespace) -> None:
    from .hooks import handle

    if a.action == "start":
        r = handle(a.runtime, "SessionStart", {"session_id": a.local_id, "cwd": os.getcwd()})
        s = Store().session(a.local_id)
        out({"session": s, "context": (r or {}).get("hookSpecificOutput", {}).get("additionalContext")})
    elif a.action == "end":
        handle(a.runtime, "SessionEnd", {"session_id": a.local_id, "reason": "wrapper ended"})
        out({"ok": True})
    else:
        Store().activity(a.local_id, "tool", a.detail or "", progress=True)
        out({"ok": True})


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agentlink", description="Agent Office client")
    sp = ap.add_subparsers(dest="cmd", required=True)

    def scope(p: argparse.ArgumentParser) -> None:
        p.add_argument("--scope", choices=["user", "project"], default="user")
        p.add_argument("--dir", help="project directory for --scope project")

    p = sp.add_parser("connect")
    p.add_argument("--server", required=True)
    p.add_argument("--token", default=os.environ.get("AGENTLINK_TOKEN", ""))
    p.add_argument("--project")
    p.add_argument("--runtime", default="auto")
    p.add_argument("--device")
    p.add_argument("--specialization")
    p.add_argument("--no-install", action="store_true")
    p.add_argument("--no-service", action="store_true")
    p.add_argument(
        "--auto-resume", action="store_true", help="allow the supervisor to resume idle sessions (spends quota)"
    )
    p.add_argument(
        "--headless-delivery",
        action="store_true",
        help="codex/opencode: answer new messages with a headless turn when no hooked session is running (spends quota)",
    )
    p.add_argument("--workdir", help="working directory for headless turns (default: current directory)")
    p.add_argument("--model", help="opencode model for headless turns, e.g. opencode-go/deepseek-v4.1-flash")
    scope(p)
    p = sp.add_parser("disconnect")
    p.add_argument("--purge", action="store_true")
    p.add_argument("--keep-hooks", action="store_true")
    scope(p)
    sp.add_parser("status")
    scope(sp.add_parser("doctor"))
    p = sp.add_parser("supervise")
    p.add_argument("--once", action="store_true")
    p.add_argument("--wait", type=int, default=20)
    p.add_argument("--home", help="AGENTLINK_HOME for this supervisor (set by the installed service)")
    p = sp.add_parser("hook")
    p.add_argument("runtime")
    p.add_argument("event")
    for name in ("install", "uninstall"):
        p = sp.add_parser(name)
        p.add_argument("runtime")
        scope(p)
    p = sp.add_parser("inbox")
    p.add_argument("--pending", action="store_true")
    p.add_argument("--all", action="store_true")
    p = sp.add_parser("ack")
    p.add_argument("ids", type=int, nargs="+")
    p.add_argument("--session")
    p = sp.add_parser("reply")
    p.add_argument("id", type=int)
    p.add_argument("text")
    p.add_argument("--channel")
    p.add_argument("--kind", default="answer")
    p.add_argument("--session")
    p = sp.add_parser("show")
    p.add_argument("id", type=int)
    p = sp.add_parser("tasks")
    p.add_argument("channel", nargs="?")
    p.add_argument("--status")
    p.add_argument("--all", action="store_true")
    p = sp.add_parser("claim")
    p.add_argument("task_id", type=int)
    p.add_argument("--session")
    p.add_argument("--ttl", type=int)
    for name in ("renew", "check-lease"):
        p = sp.add_parser(name)
        p.add_argument("task_id", type=int)
        p.add_argument("--session")
        p.add_argument("--note")
    p = sp.add_parser("release")
    p.add_argument("task_id")
    p.add_argument("resource", nargs="?", help="legacy: `release <channel> <resource>` releases a path lease")
    p.add_argument("--session")
    p = sp.add_parser("checkpoint")
    p.add_argument("task_id", type=int)
    p.add_argument("--session")
    for k in ("summary", "branch", "commit", "base-commit", "worktree", "tests", "risks", "notes"):
        p.add_argument(f"--{k}")
    for k in ("done", "pending", "files"):
        p.add_argument(f"--{k}", help="comma separated")
    p = sp.add_parser("activity")
    p.add_argument("--runtime", default="generic")
    p.add_argument("--local-id", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--detail")
    p = sp.add_parser("session")
    p.add_argument("action", choices=["start", "end", "beat"])
    p.add_argument("--runtime", default="generic")
    p.add_argument("--local-id", required=True)
    p.add_argument("--detail")
    return ap


def main(argv: list[str]) -> None:
    a = build_parser().parse_args(argv)
    if a.cmd == "hook":
        from .hooks import main as hook_main

        hook_main(a.runtime, a.event)
    elif a.cmd == "release" and a.resource:
        api = api_from_config()
        out(api.call("DELETE", f"/api/channels/{a.task_id}/leases", query={"resource": a.resource}))
    elif a.cmd == "release":
        a.task_id = int(a.task_id)
        _holder_call(a, "POST", f"/api/tasks/{a.task_id}/release", {})
    elif a.cmd == "renew":
        _holder_call(a, "POST", f"/api/tasks/{a.task_id}/renew", {"progress": bool(a.note), "progress_note": a.note})
    elif a.cmd == "checkpoint":
        cp = {
            k.replace("-", "_"): getattr(a, k.replace("-", "_"))
            for k in ("summary", "branch", "commit", "base-commit", "worktree", "tests", "risks", "notes")
        }
        cp.update(
            {
                k: [x.strip() for x in getattr(a, k).split(",") if x.strip()]
                for k in ("done", "pending", "files")
                if getattr(a, k)
            }
        )
        _holder_call(
            a, "POST", f"/api/tasks/{a.task_id}/checkpoints", {"checkpoint": {k: v for k, v in cp.items() if v}}
        )
    elif a.cmd == "inbox":
        cmd_inbox_pending(a)
    else:
        {
            "connect": cmd_connect, "disconnect": cmd_disconnect, "status": cmd_status, "doctor": cmd_doctor,
            "supervise": cmd_supervise, "install": cmd_install, "uninstall": lambda x: cmd_install(x, True),
            "ack": cmd_ack, "reply": cmd_reply, "show": cmd_show, "tasks": cmd_tasks, "claim": cmd_claim,
            "check-lease": cmd_check_lease, "activity": cmd_activity, "session": cmd_session,
        }[a.cmd](a)  # fmt: skip
