"""`agentlink hook <runtime> <event>`: the runtime-side half of the adapter (Claude Code / Codex hook model).

Runs as a short-lived process per hook event. It must be fast, must never break the agent (fail open:
any error => no output, exit 0), and only talks to the model when there is something addressed to it.

Safe points used for delivery:
    SessionStart       protocol summary + pending messages (also after compaction / resume)
    UserPromptSubmit   pending messages, before the model sees the user's prompt
    PostToolUse        only urgent (P1) messages that are new, after a tool finished (never mid-tool)
    Stop               if new messages are pending, block the stop once so the agent handles them
Lifecycle: SessionStart/SessionEnd and SubagentStart/SubagentStop register and end server sessions.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from .local import Api, ApiError, Store, api_from_config, load_config

MAX_INJECT = 8
BODY_CHARS = 700


def render(msgs: list[dict[str, Any]], project: str | None, session_id: str | None, headless: bool = False) -> str:
    """Compact, deterministic notification (no LLM summarisation). Full detail stays available via the API.

    `headless`: for runtimes that cannot run agentlink themselves; no tool hints, so they never stall on a
    permission prompt nobody can answer."""
    lines = [f"[AgentOffice] {len(msgs)} message(s) addressed to you" + (f" · project {project}" if project else "")]
    if not headless:
        lines.append(
            'Reply: agentlink reply <id> "text"   ·   Acknowledge only: agentlink ack <id>   ·   Details: agentlink show <id>'
        )
    for m in msgs[:MAX_INJECT]:
        body = (m.get("body") or "").strip()
        if len(body) > BODY_CHARS:
            body = body[: BODY_CHARS - 1] + "…"
        data = json.loads(m["data"]) if isinstance(m.get("data"), str) and m.get("data") else (m.get("data") or {})
        tag = f"P{m.get('priority') or 2}" + (
            f" TASK #{data['task_id']}" if isinstance(data, dict) and data.get("task_id") else ""
        )
        lines.append(
            f"#{m['message_id']} {tag} from @{m.get('author')} in #{m.get('channel')} [{m.get('kind')}]: {body}"
        )
    if len(msgs) > MAX_INJECT and not headless:
        lines.append(f"… {len(msgs) - MAX_INJECT} more: agentlink inbox --pending")
    if session_id and not headless:
        lines.append(f"(your Agent Office session: {session_id})")
    return "\n".join(lines)


PROTOCOL = (
    "[AgentOffice] You are connected to Agent Office (coordination for this project). Messages addressed to you are "
    "delivered here automatically; you do not need to poll. Before editing shared files claim the task "
    "(agentlink claim <task_id>) and keep checkpoints (agentlink checkpoint <task_id> --summary ...). If a message asks "
    'something, answer with agentlink reply <id> "...". Never assume you still hold a task after a long pause: '
    "agentlink check-lease <task_id> before pushing."
)


def _ensure_session(
    store: Store, api: Api | None, cfg: dict[str, Any], runtime: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    local_id = str(payload.get("session_id") or "")
    if not local_id:
        return None
    s = store.session(local_id)
    if s and not s["ended_at"]:
        return s
    server_id = ""
    if api is not None:
        try:
            r = api.call("POST", "/api/sessions", {
                "project": cfg.get("project"), "runtime": runtime, "device": cfg.get("device"),
                "specialization": cfg.get("specialization"), "label": (payload.get("cwd") or "")[-120:],
                "model": payload.get("model") or "",
            }, timeout=6)  # fmt: skip
            server_id = r["id"]
        except (ApiError, OSError, ValueError):
            server_id = ""  # the supervisor registers it later; the agent is never blocked
    store.x(
        "INSERT INTO sessions(local_id, runtime, server_id, project, cwd, started_at, last_activity, state, transcript) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'executing', ?) ON CONFLICT(local_id) DO UPDATE SET server_id = excluded.server_id, "
        "ended_at = NULL, end_reason = NULL, started_at = excluded.started_at",
        (
            local_id,
            runtime,
            server_id,
            cfg.get("project"),
            payload.get("cwd"),
            time.time(),
            time.time(),
            payload.get("transcript_path"),
        ),
    )
    return store.session(local_id)


def _inject(store: Store, api: Api | None, s: dict[str, Any] | None, msgs: list[dict[str, Any]], via: str) -> None:
    # Only what render() actually showed the model counts as injected; the rest stays pending for the next safe point.
    ids = [int(m["message_id"]) for m in msgs[:MAX_INJECT]]
    store.mark(ids, "injected_to_runtime", session=(s or {}).get("server_id"), via=via)
    if api is not None and ids:
        try:  # best effort; the supervisor retries anything unreported
            api.call("POST", "/api/deliveries", {"message_ids": ids, "state": "injected_to_runtime",
                     "session_id": (s or {}).get("server_id") or None, "evidence": {"adapter": via, "mechanism": "hook"}}, timeout=5)  # fmt: skip
            store.set_reported(ids, "injected_to_runtime")
        except (ApiError, OSError, ValueError):
            pass


def _ctx(event: str, text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def handle(
    runtime: str, event: str, payload: dict[str, Any], store: Store | None = None, api: Api | None = None
) -> dict[str, Any] | None:
    cfg = load_config()
    store = store or Store()
    if api is None:
        try:
            api = api_from_config(cfg)
        except SystemExit:
            api = None
    s = _ensure_session(store, api, cfg, runtime, payload)
    local_id = (s or {}).get("local_id")
    project = cfg.get("project")
    sid = (s or {}).get("server_id") or None

    if event == "SessionStart":
        if local_id:
            store.activity(local_id, "session.start", str(payload.get("source") or ""), progress=False)
        msgs = store.pending()
        text = PROTOCOL + (f"\nYour session id: {sid}" if sid else "")
        if msgs:
            text += "\n\n" + render(msgs, project, None)
            _inject(store, api, s, msgs, f"{runtime}:SessionStart")
        return _ctx("SessionStart", text)

    if event == "UserPromptSubmit":
        if local_id:
            store.activity(local_id, "prompt", "", progress=True)
        msgs = store.pending(only_new=True)
        if not msgs:
            return None
        _inject(store, api, s, msgs, f"{runtime}:UserPromptSubmit")
        return _ctx("UserPromptSubmit", render(msgs, project, sid))

    if event == "PostToolUse":
        if local_id:
            store.activity(local_id, "tool", str(payload.get("tool_name") or ""), progress=True)
        urgent = store.pending(max_priority=1, only_new=True)
        if not urgent:
            return None
        _inject(store, api, s, urgent, f"{runtime}:PostToolUse")
        return _ctx("PostToolUse", render(urgent, project, sid))

    if event == "Stop":
        if local_id:
            store.activity(local_id, "stop", "", progress=False)
            store.x("UPDATE sessions SET state = 'idle' WHERE local_id = ?", (local_id,))
        msgs = store.pending(only_new=True)
        if not msgs or payload.get("stop_hook_active"):
            return None  # never loop: at most one block per stop chain
        _inject(store, api, s, msgs, f"{runtime}:Stop")
        return {"decision": "block", "reason": render(msgs, project, sid)}

    if event == "SubagentStart":
        agent_id = str(payload.get("agent_id") or "")
        if agent_id and s and api is not None and s.get("server_id"):
            try:
                r = api.call("POST", "/api/sessions", {"parent_session_id": s["server_id"], "runtime": runtime,
                             "specialization": str(payload.get("agent_type") or ""), "label": str(payload.get("agent_type") or "")}, timeout=6)  # fmt: skip
                store.x("INSERT OR REPLACE INTO sessions(local_id, runtime, server_id, parent_local_id, project, started_at, state) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'executing')", (f"{local_id}:{agent_id}", runtime, r["id"], local_id, project, time.time()))  # fmt: skip
            except (ApiError, OSError, ValueError):
                pass
        return None

    if event == "SubagentStop":
        agent_id = str(payload.get("agent_id") or "")
        sub = store.session(f"{local_id}:{agent_id}") if agent_id else None
        if sub and api is not None:
            try:
                api.call("POST", f"/api/sessions/{sub['server_id']}/end", {"reason": "subagent finished"}, timeout=6)
            except (ApiError, OSError, ValueError):
                pass
            store.x(
                "UPDATE sessions SET ended_at = ?, end_reason = 'subagent finished' WHERE local_id = ?",
                (time.time(), sub["local_id"]),
            )
        return None

    if event == "PreCompact":
        if local_id:
            store.activity(local_id, "compact", "", progress=False)
        return None

    if event == "SessionEnd":
        if s and api is not None and s.get("server_id"):
            try:
                api.call(
                    "POST",
                    f"/api/sessions/{s['server_id']}/end",
                    {"reason": str(payload.get("reason") or "session end")},
                    timeout=6,
                )
            except (ApiError, OSError, ValueError):
                pass
        if local_id:
            store.x(
                "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE local_id = ?",
                (time.time(), str(payload.get("reason") or "end"), local_id),
            )
        return None
    return None


def main(runtime: str, event: str) -> None:
    """Entry point. Fail open: a broken coordination layer must never break the agent's own work."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        out = handle(runtime, event, payload)
        if out:
            sys.stdout.write(json.dumps(out))
    except Exception as exc:
        try:
            Store().put(
                "last_hook_error", {"at": time.time(), "event": event, "error": f"{type(exc).__name__}: {exc}"[:300]}
            )
        except Exception:  # noqa: S110
            pass
    sys.exit(0)
