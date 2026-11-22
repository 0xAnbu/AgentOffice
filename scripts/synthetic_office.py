"""Five synthetic participants with several sessions and sub-agents, driven through the REAL API (no LLMs).

    python scripts/synthetic_office.py --server http://127.0.0.1:8080 --admin-token $AGENTLINK_ADMIN_TOKEN [--seconds 120]

TESTING / DEMO ONLY. It refuses to run against a server whose /api/health?deep=1 reports mode=production unless
--i-know-this-is-production is given, and every synthetic session registers with runtime "synthetic" so the dashboard
labels it as such. Nothing is injected into the dashboard directly: everything it shows comes from these API calls.

Deterministic (seeded): the same run produces the same sequence of actions. Prints the monitor token to log in.
"""

from __future__ import annotations

import argparse
import contextlib
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentlink.local import Api, ApiError

TEAM = [  # (agent, owner, specialization, runtime label, coordinator priority)
    ("ana-claude", "dev01", "coordination", "claude-code", 10),
    ("ana-codex", "dev01", "backend", "codex", 0),
    ("ben-claude", "dev02", "backend", "claude-code", 0),
    ("ben-opencode", "dev02", "frontend", "opencode", 0),
    ("cris-claude", "dev03", "frontend", "claude-code", 0),
    ("dani-codex", "dev04", "devops", "codex", 3),
    ("eva-claude", "dev05", "qa", "claude-code", 5),
    ("eva-opencode", "dev05", "security", "opencode", 0),
]
SUBAGENTS = {"ana-claude": ["qa", "frontend"], "ben-claude": ["backend"], "eva-claude": ["security"]}
QUESTIONS = ["¿Cuál es el contrato del endpoint /login?", "¿Puedes revisar la migración 0004?", "El build de CI falla en main",
             "¿Quién tiene el lease de src/auth/?", "Necesito la variable de entorno del bucket", "Revisión del PR #12, por favor"]  # fmt: skip
TASKS = ["API de autenticación", "Pantalla de login", "Pipeline de despliegue", "Pruebas de regresión", "Auditoría de dependencias",
         "Migración de esquema", "Componente de tabla", "Backups SQLite"]  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8080")
    ap.add_argument("--admin-token", required=True)
    ap.add_argument("--project", default="demo")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--i-know-this-is-production", action="store_true")
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    adm = Api(a.server, a.admin_token)
    mode = adm.call("GET", "/api/health", query={"deep": "1"})
    if mode.get("mode") == "production" and not a.i_know_this_is_production:
        sys.exit("refusing: synthetic activity must never be presented as real in production")
    try:
        adm.call("POST", "/api/projects", {"name": a.project, "description": "synthetic test office"})
    except ApiError as e:
        if e.status != 409:
            raise
    tokens = {}
    for name, owner, spec, _rt, prio in TEAM:
        try:
            r = adm.call("POST", "/api/admin/agents", {"name": name, "owner": owner, "specialization": spec, "projects": [a.project],
                                                       "coord_priority": prio, "description": "synthetic"})  # fmt: skip
            tokens[name] = r["token"]
        except ApiError as e:
            if e.status != 409:
                raise
            tokens[name] = adm.call("POST", f"/api/admin/agents/{name}/tokens", {"label": "synthetic"})["token"]
    ch = f"{a.project}-dev"
    try:
        adm.call("POST", "/api/channels", {"name": ch, "project": a.project, "members": list(tokens)})
    except ApiError as e:
        if e.status != 409:
            raise
    try:
        mon = adm.call(
            "POST", "/api/admin/agents", {"name": f"{a.project}-monitor", "role": "monitor", "description": "dashboard"}
        )["token"]
    except ApiError:
        mon = adm.call("POST", f"/api/admin/agents/{a.project}-monitor/tokens", {"label": "dashboard"})["token"]
    print(f"monitor token (paste it in the dashboard): {mon}", flush=True)

    apis = {n: Api(a.server, t) for n, t in tokens.items()}
    sessions: dict[str, str] = {}
    for name, _owner, spec, _rt, _p in TEAM:
        sessions[name] = apis[name].call("POST", "/api/sessions", {"project": a.project, "runtime": "synthetic", "specialization": spec,
                                                                    "label": "synthetic", "device": f"{name}-box"})["id"]  # fmt: skip
        for sub in SUBAGENTS.get(name, []):
            sid = apis[name].call(
                "POST",
                "/api/sessions",
                {"parent_session_id": sessions[name], "runtime": "synthetic", "specialization": sub},
            )["id"]
            sessions[f"{name}/{sub}"] = sid
    st = adm.call("GET", f"/api/projects/{a.project}/coordinator")
    if st["status"] == "none":
        st = adm.call(
            "POST", f"/api/projects/{a.project}/coordinator/designate", {"session_id": sessions["ana-claude"]}
        )
    if st["status"] == "offered" and st.get("offered_session") == sessions["ana-claude"]:
        apis["ana-claude"].call(
            "POST",
            f"/api/projects/{a.project}/coordinator/accept",
            {"session_id": sessions["ana-claude"], "epoch": st["epoch"]},
        )
    task_ids = [
        apis["ana-claude"].call("POST", f"/api/channels/{ch}/tasks", {"title": t, "priority": rnd.randint(1, 3)})["id"]
        for t in TASKS[:5]
    ]
    held: dict[str, tuple[int, int]] = {}
    names = [t[0] for t in TEAM]
    end = time.time() + a.seconds
    tick = 0
    while time.time() < end:
        tick += 1
        for key, sid in sessions.items():
            agent = key.split("/")[0]
            state = rnd.choice(["executing", "executing", "idle", "waiting"]) if tick % 3 == 1 else None
            body = {"progress": rnd.random() < 0.7}
            if state:
                body["state"] = state
            with contextlib.suppress(ApiError):  # an expired synthetic session just stops beating
                apis[agent].call("POST", f"/api/sessions/{sid}/heartbeat", body)
        if tick % 2 == 0:
            frm, to = rnd.sample(names, 2)
            apis[frm].call(
                "POST",
                f"/api/channels/{ch}/messages",
                {"body": f"@{to} {rnd.choice(QUESTIONS)}", "kind": rnd.choice(["question", "chat", "review"])},
            )
        if tick % 3 == 0 and task_ids:
            who = rnd.choice(names)
            tid = rnd.choice(task_ids)
            try:
                t = apis[who].call("POST", f"/api/tasks/{tid}/claim", {"session_id": sessions[who], "ttl_seconds": 120})
                held[who] = (tid, t["lease_epoch"])
            except ApiError:
                pass
        if tick % 4 == 0 and held:
            who, (tid, ep) = rnd.choice(sorted(held.items()))
            status = rnd.choice(["in_progress", "review", "done", "blocked"])
            try:
                apis[who].call(
                    "PATCH",
                    f"/api/tasks/{tid}",
                    {"status": status, "session_id": sessions[who], "lease_epoch": ep, "note": "synthetic"},
                )
                if status == "done":
                    held.pop(who)
                    task_ids.remove(tid)
                    task_ids.append(
                        apis["ana-claude"].call("POST", f"/api/channels/{ch}/tasks", {"title": rnd.choice(TASKS)})["id"]
                    )
            except ApiError:
                held.pop(who, None)
        time.sleep(2)
    print("synthetic run finished; sessions will expire on their own (no more heartbeats)")


if __name__ == "__main__":
    main()
