"""Runtime adapters: how the local supervisor reaches each AI runtime, and what each one can really do.

Every adapter declares the eight capabilities of the adapter contract with an honest status. The
status reflects what this codebase has *verified*, recorded in docs/AGENT_INSTALLATION.md with the
evidence; "implemented" is never promoted to "tested" by declaration.

    SUPPORTED_AND_TESTED      exercised end-to-end against the real runtime (evidence recorded)
    IMPLEMENTED_NOT_VERIFIED  code exists, not yet proven against the real runtime
    PARTIALLY_SUPPORTED       works with documented limits
    UNSUPPORTED               the runtime offers no mechanism
    UNKNOWN                   not investigated

Delivery into the model context happens only at *safe points* the runtime offers (session start,
prompt submit, after a tool call, when the agent tries to stop). Nothing interrupts a running tool.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .local import Store, _private_dir, home
from .oscmd import agentlink_argv, batch_shim, js_array, shell_command

CAPABILITIES = (
    "observation",
    "notification",
    "injection",
    "resume",
    "acknowledgement",
    "processing",
    "subagents",
    "lifecycle",
)
HOOK_MARK = "agentlink hook"  # marker in files we own (plugins)
PUSH_BACKOFF = 120.0  # seconds a session is left alone after an active push failed


def ours(command: str) -> bool:
    """A hook command installed by us (`agentlink hook ...` or `python -m agentlink.cli hook ...`), quoted or not."""
    return "agentlink" in command and re.search(r'(^|\s)"?hook"?\s', command) is not None


def _bin() -> str:
    """Hook command prefix, quoted for the shell the runtime hands it to (paths may contain spaces)."""
    return shell_command(agentlink_argv())


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    d = _private_dir(_private_dir(home()) / "backups")
    dst = d / f"{path.name}.{int(time.time())}.bak"
    shutil.copy2(path, dst)
    return dst


def _without_ours(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hook entries with our commands removed (entries left empty disappear)."""
    keep = []
    for e in entries:
        hs = [h for h in e.get("hooks", []) if not ours(h.get("command", ""))]
        if len(hs) == len(e.get("hooks", [])):
            keep.append(e)
        elif hs:
            keep.append({**e, "hooks": hs})
    return keep


def _version(cmd: list[str]) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return None


class Adapter:
    runtime = "generic"
    binary: str | None = None
    capabilities: dict[str, str] = dict.fromkeys(CAPABILITIES, "UNKNOWN")
    notes: dict[str, str] = {}

    def detect(self) -> dict[str, Any]:
        if not self.binary:
            return {"runtime": self.runtime, "available": True}
        ver = _version([self.binary, "--version"])
        return {"runtime": self.runtime, "available": ver is not None, "version": ver}

    def install(self, scope: str = "user", directory: str | None = None) -> dict[str, Any]:
        return {"runtime": self.runtime, "changed": [], "note": "nothing to install"}

    def uninstall(self, scope: str = "user", directory: str | None = None) -> dict[str, Any]:
        return {"runtime": self.runtime, "changed": []}

    def doctor(self, scope: str = "user", directory: str | None = None) -> dict[str, Any]:
        return {**self.detect(), "capabilities": self.capabilities, "notes": self.notes}

    def push(self, store: Store, session: dict[str, Any], text: str) -> str | None:
        """Active delivery outside a hook (resume/queue). Returns the mechanism used, or None if unavailable."""
        return None


# -- hook-based runtimes (Claude Code; Codex uses the same event model) ---------------------------------
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "SessionEnd",
)
REWAKE_EVENTS = ("SessionStart", "Stop")  # where the background waiter is (re)armed
REWAKE_TIMEOUT = 24 * 3600  # seconds; hooks.WAIT_MAX gives up a little earlier


class ClaudeCode(Adapter):
    runtime = "claude-code"
    binary = "claude"
    rewake = True  # Claude Code's `asyncRewake` hooks: a background hook that exits 2 wakes an idle session
    capabilities = {
        "observation": "SUPPORTED_AND_TESTED",  # hook events recorded as activity (real run)
        "notification": "SUPPORTED_AND_TESTED",  # supervisor stores; next hook surfaces it (real run)
        "injection": "SUPPORTED_AND_TESTED",  # SessionStart additionalContext verified; UserPromptSubmit/PostToolUse/Stop: automated tests only
        "resume": "PARTIALLY_SUPPORTED",  # `claude --resume <id> -p` re-injects at SessionStart (real run); supervisor auto-resume opt-in not verified
        "acknowledgement": "SUPPORTED_AND_TESTED",  # the agent ran `agentlink ack` itself (real run)
        "processing": "SUPPORTED_AND_TESTED",  # the agent ran `agentlink reply`; reply id is the evidence (real run)
        "subagents": "IMPLEMENTED_NOT_VERIFIED",  # SubagentStart/SubagentStop hooks; automated tests only
        "lifecycle": "PARTIALLY_SUPPORTED",  # SessionStart registration verified for real; SessionEnd only in automated tests
    }
    notes = {
        "injection": "Only at safe points: session start, prompt submit, after a tool call, or when the agent tries to stop "
        "(Stop hook blocks once per batch so the agent handles it). A running tool is never interrupted.",
        "rewake": "An open but idle session is woken by a background `asyncRewake` hook (`agentlink hook claude-code "
        "Wait`), armed at SessionStart and after every Stop; one waiter per session. Needs a Claude Code version with "
        "asyncRewake hooks; older versions ignore the field and keep delivering at the next safe point.",
        "resume": "A stopped session is not woken by hooks. Opt-in auto-resume runs `claude --resume <session> -p` with the "
        "message; disabled by default because it spends the participant's quota.",
    }

    def settings_path(self, scope: str, directory: str | None) -> Path:
        if scope == "project":
            return Path(directory or os.getcwd()) / ".claude" / "settings.json"
        return Path.home() / ".claude" / "settings.json"

    def _entries(self) -> dict[str, list[dict[str, Any]]]:
        cmd = _bin()
        out: dict[str, list[dict[str, Any]]] = {}
        for ev in HOOK_EVENTS:
            entry: dict[str, Any] = {
                "hooks": [{"type": "command", "command": f"{cmd} hook {self.runtime} {ev}", "timeout": 15}]
            }
            if self.rewake and ev in REWAKE_EVENTS:
                entry["hooks"].append({"type": "command", "command": f"{cmd} hook {self.runtime} Wait",
                                       "asyncRewake": True, "timeout": REWAKE_TIMEOUT})  # fmt: skip
            if ev == "PostToolUse":
                entry["matcher"] = "*"
            out[ev] = [entry]
        return out

    def install(self, scope: str = "user", directory: str | None = None) -> dict[str, Any]:
        path = self.settings_path(scope, directory)
        data: dict[str, Any] = {}
        if path.exists():
            data = json.loads(path.read_text("utf-8") or "{}")
        backup = _backup(path)
        hooks = data.setdefault("hooks", {})
        added = []
        for ev, entries in self._entries().items():
            cur = hooks.setdefault(ev, [])
            mine = [h for e in cur for h in e.get("hooks", []) if ours(h.get("command", ""))]
            if mine == entries[0]["hooks"]:
                continue  # already installed as is: never duplicate
            # replace only our own entries (an older install may hold an unquoted, broken command);
            # someone else's hooks are never touched
            hooks[ev] = cur = _without_ours(cur)
            cur.extend(entries)
            added.append(ev)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
        return {"runtime": self.runtime, "file": str(path), "added": added, "backup": str(backup) if backup else None}

    def uninstall(self, scope: str = "user", directory: str | None = None) -> dict[str, Any]:
        path = self.settings_path(scope, directory)
        if not path.exists():
            return {"runtime": self.runtime, "removed": []}
        data = json.loads(path.read_text("utf-8") or "{}")
        _backup(path)
        removed = []
        for ev, entries in list((data.get("hooks") or {}).items()):
            keep = _without_ours(entries)
            if keep != entries:
                removed.append(ev)
            if keep:
                data["hooks"][ev] = keep
            else:
                del data["hooks"][ev]
        if data.get("hooks") == {}:
            del data["hooks"]
        path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
        return {"runtime": self.runtime, "file": str(path), "removed": sorted(set(removed))}

    def doctor(self, scope: str = "user", directory: str | None = None) -> dict[str, Any]:
        path = self.settings_path(scope, directory)
        installed: list[str] = []
        if path.exists():
            data = json.loads(path.read_text("utf-8") or "{}")
            installed = [ev for ev, es in (data.get("hooks") or {}).items()
                         if any(ours(h.get("command", "")) for e in es for h in e.get("hooks", []))]  # fmt: skip
        res = {**super().doctor(), "settings": str(path), "hooks_installed": sorted(installed),
               "hooks_missing": sorted(set(HOOK_EVENTS) - set(installed))}  # fmt: skip
        if self.rewake:
            armed = [ev for ev in REWAKE_EVENTS if path.exists() and any(
                h.get("asyncRewake") and ours(h.get("command", "")) for e in (data.get("hooks") or {}).get(ev, [])
                for h in e.get("hooks", []))]  # fmt: skip
            res["rewake_missing"] = sorted(set(REWAKE_EVENTS) - set(armed))
        return res

    def push(self, store: Store, session: dict[str, Any], text: str) -> str | None:
        cfg = store.get("adapter.claude-code", {}) or {}
        if not cfg.get("auto_resume") or not session.get("local_id"):
            return None
        if batch_shim(self.binary or "claude"):
            return None  # cmd.exe would re-parse the message text (see oscmd.batch_shim)
        try:
            subprocess.Popen(
                [self.binary or "claude", "--resume", session["local_id"], "-p", text],
                cwd=session.get("cwd") or None, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )  # fmt: skip
        except OSError:
            return None
        return "claude --resume -p"


class Codex(ClaudeCode):
    runtime = "codex"
    binary = "codex"
    rewake = False  # no asyncRewake in codex hooks
    capabilities = {
        "observation": "IMPLEMENTED_NOT_VERIFIED",  # codex hooks need the participant's own hook trust; not exercised
        "notification": "SUPPORTED_AND_TESTED",  # supervisor stores and confirms (real run)
        "injection": "SUPPORTED_AND_TESTED",  # headless `codex exec` turn with the rendered message (real run)
        "resume": "SUPPORTED_AND_TESTED",  # `codex queue --thread` into an open TUI, idle/busy/closed (real run, 0.171)
        "acknowledgement": "PARTIALLY_SUPPORTED",  # exec sandbox has no network: the supervisor relays the answer
        "processing": "SUPPORTED_AND_TESTED",  # final message relayed as reply authored by the agent token (real run)
        "subagents": "UNKNOWN",  # not investigated
        "lifecycle": "PARTIALLY_SUPPORTED",  # headless turn = one server session (registered, ended); interactive sessions need hooks
    }
    notes = {
        "hooks": "Codex runs hooks only after the participant trusts them in Codex itself. Agent Office never bypasses hook "
        "trust. Hooks only deliver when a turn starts; with --auto-resume the supervisor also queues new messages "
        "for an idle session (`codex queue --thread`), so they appear as a prompt in the open terminal.",
        "resume": "`codex queue` needs the thread id the hooks reported, i.e. trusted hooks. Without them use "
        "--headless-delivery (a separate `codex exec` turn, not visible in any terminal).",
        "acknowledgement": "Inside `codex exec` the default sandbox has no network, so the agent cannot call the API itself; "
        "the adapter posts the agent's final message as its reply (authored with the agent's own token).",
    }

    def settings_path(self, scope: str, directory: str | None) -> Path:
        base = (
            Path(directory or os.getcwd()) / ".codex"
            if scope == "project"
            else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        )
        return base / "hooks.json"

    def push(self, store: Store, session: dict[str, Any], text: str) -> str | None:
        """Queue the message on the participant's own Codex thread (opt-in, `auto_resume`).

        `codex queue --thread <id>` hands it to whichever Codex process holds the thread: an open terminal shows it as
        a prompt and the answer streams there, in the same thread the human is watching (busy: after the current
        turn). A separate `codex exec resume` would instead fail with "thread already has an active writer" while the
        terminal is open, and its turn would never be visible there. The agent answers with `agentlink reply` itself.

        Returns None (message stays pending) when disabled or when the queue command fails; after a failure the
        thread is not retried for PUSH_BACKOFF seconds, so a broken install is not hammered every iteration."""
        cfg = store.get("adapter.codex", {}) or {}
        thread = session.get("local_id")
        if not cfg.get("auto_resume") or not thread or batch_shim(self.binary or "codex"):
            return None  # batch shim: cmd.exe would re-parse the message text (see oscmd.batch_shim)
        key = f"push_failed.codex.{thread}"
        if time.time() - float((store.get(key) or {}).get("at") or 0) < PUSH_BACKOFF:
            return None
        try:
            res = subprocess.run(
                [self.binary or "codex", "queue", "--thread", str(thread), "--message", text],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, check=False,
            )  # fmt: skip
            ok, err = res.returncode == 0, (res.stderr or res.stdout or "").strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, err = False, str(e)
        if not ok:
            store.put(key, {"at": time.time(), "error": err[-300:]})
            return None
        store.put(key, None)
        return "codex queue"


class OpenCode(Adapter):
    runtime = "opencode"
    binary = "opencode"
    capabilities = {
        "observation": "IMPLEMENTED_NOT_VERIFIED",  # plugin events (tool.execute.after, session.idle): not exercised
        "notification": "SUPPORTED_AND_TESTED",  # supervisor stores and confirms (real run)
        "injection": "SUPPORTED_AND_TESTED",  # headless `opencode run` turn (real run, opencode-go/deepseek-v4.1-flash)
        "resume": "IMPLEMENTED_NOT_VERIFIED",  # `opencode run --session <id>`: not exercised
        "acknowledgement": "PARTIALLY_SUPPORTED",  # headless mode: the supervisor relays the answer
        "processing": "SUPPORTED_AND_TESTED",  # answer relayed as reply authored by the agent token (real run)
        "subagents": "UNKNOWN",  # not investigated
        "lifecycle": "PARTIALLY_SUPPORTED",  # headless turn = one server session; plugin lifecycle events not exercised
    }
    notes = {
        "plugin": "A JS plugin (installed in ~/.config/opencode/plugins) reports activity to the supervisor spool."
    }

    def plugin_path(self) -> Path:
        return (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "opencode" / "plugins" / "agentoffice.js"
        )

    def install(self, scope: str = "user", directory: str | None = None) -> dict[str, Any]:
        path = self.plugin_path()
        if path.exists() and HOOK_MARK not in path.read_text("utf-8"):
            return {"runtime": self.runtime, "error": f"{path} exists and is not ours; not overwriting"}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(OPENCODE_PLUGIN.replace("__AGENTLINK__", js_array(agentlink_argv())), "utf-8")
        return {"runtime": self.runtime, "file": str(path), "added": ["plugin"]}

    def uninstall(self, scope: str = "user", directory: str | None = None) -> dict[str, Any]:
        path = self.plugin_path()
        if path.exists() and HOOK_MARK in path.read_text("utf-8"):
            path.unlink()
            return {"runtime": self.runtime, "removed": [str(path)]}
        return {"runtime": self.runtime, "removed": []}

    def doctor(self, scope: str = "user", directory: str | None = None) -> dict[str, Any]:
        return {**super().doctor(), "plugin": str(self.plugin_path()), "plugin_installed": self.plugin_path().exists()}

    def push(self, store: Store, session: dict[str, Any], text: str) -> str | None:
        cfg = store.get("adapter.opencode", {}) or {}
        if not cfg.get("auto_resume") or not session.get("local_id"):
            return None
        return "opencode run --session"


class Generic(Adapter):
    """Any CLI agent: the agent (or its wrapper) polls `agentlink inbox --pending` and acks explicitly."""

    runtime = "generic"
    capabilities = {
        "observation": "PARTIALLY_SUPPORTED",  # only what the wrapper reports via `agentlink activity`
        "notification": "SUPPORTED_AND_TESTED",  # local store + `agentlink inbox --pending` (automated tests)
        "injection": "UNSUPPORTED",  # no generic way into an arbitrary model context
        "resume": "UNSUPPORTED",  # no generic mechanism
        "acknowledgement": "SUPPORTED_AND_TESTED",  # explicit `agentlink ack` (automated tests)
        "processing": "SUPPORTED_AND_TESTED",  # `agentlink reply` with evidence (automated tests)
        "subagents": "UNSUPPORTED",  # not observable
        "lifecycle": "PARTIALLY_SUPPORTED",  # `agentlink session start/end` from the wrapper
    }


class Synthetic(Generic):
    runtime = "synthetic"


ADAPTERS: dict[str, Adapter] = {a.runtime: a for a in (ClaudeCode(), Codex(), OpenCode(), Generic(), Synthetic())}


def detect_runtime() -> str:
    for name in ("claude-code", "codex", "opencode"):
        if ADAPTERS[name].detect().get("available"):
            return name
    return "generic"


OPENCODE_PLUGIN = """// agentlink hook: Agent Office plugin for OpenCode (installed by `agentlink install opencode`; removed by uninstall).
// Reports observable activity to the local Agent Office supervisor. It never sends anything to a model provider.
import { spawn } from "node:child_process";

const AGENTLINK = __AGENTLINK__;  // the agentlink command as an argument list: no shell on any OS

const report = (event, sessionID, detail = "") => {
  try {
    const p = spawn(AGENTLINK[0], [...AGENTLINK.slice(1), "activity", "--runtime", "opencode",
      "--local-id", String(sessionID || ""), "--kind", event, "--detail", String(detail).slice(0, 200)],
      { stdio: "ignore", detached: true, windowsHide: true });
    p.on("error", () => {});
    p.unref();
  } catch { /* the supervisor is optional for the runtime: never break the agent */ }
};

export const AgentOffice = async () => ({
  event: async ({ event }) => {
    const sid = event?.properties?.sessionID || event?.properties?.info?.id;
    if (event.type === "session.created") report("session.start", sid);
    else if (event.type === "session.idle") report("idle", sid);
    else if (event.type === "session.deleted") report("session.end", sid);
  },
  "tool.execute.after": async (input) => report("tool", input?.sessionID, input?.tool),
});
"""
