"""Real-runtime integration test (INSTRUCTIONS §15 Test 14). EXPLICIT OPT-IN: spends real quota.

    RUN_REAL_RUNTIMES=1 python scripts/e2e_runtimes.py [--only claude,codex,opencode,cross] [--keep]

Starts a private Agent Office server on localhost with a throw-away database, creates a project with one
agent per runtime, and records — step by step — how far each message really got:

  claude   synthetic sender -> server -> supervisor -> Claude Code hook injection -> Claude runs
           `agentlink reply` itself -> processed (evidence: reply authored by the Claude agent)
  codex    synthetic sender -> server -> supervisor -> `codex exec` (headless turn) -> answer relayed -> processed
  opencode synthetic sender -> server -> supervisor -> `opencode run` (headless turn) -> answer relayed -> processed
  cross    Claude Code sends a request itself -> Codex answers -> the answer is injected back into the SAME
           Claude session (--resume) -> Claude acknowledges it through the protocol

Evidence (no tokens) is written to docs/evidence/runtime-e2e-<UTC timestamp>.json.
Each step is checked against server state, never against what a model claims.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentlink.adapters import ADAPTERS  # noqa: E402
from agentlink.local import Api, Store  # noqa: E402
from agentlink.supervisor import Supervisor  # noqa: E402

EVIDENCE: dict[str, Any] = {"started": datetime.now(UTC).isoformat(), "steps": [], "results": {}}


def step(name: str, ok: bool, **detail: Any) -> bool:
    EVIDENCE["steps"].append({"step": name, "ok": ok, "at": datetime.now(UTC).isoformat(), **detail})
    print(
        ("PASS " if ok else "FAIL ") + name + (f"  {json.dumps(detail, ensure_ascii=False)[:300]}" if detail else ""),
        flush=True,
    )
    return ok


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Env:
    def __init__(self, keep: bool) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="agentoffice-e2e-"))
        self.keep = keep
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.admin = "al_" + secrets.token_urlsafe(32)
        env = {**os.environ, "AGENTLINK_ADMIN_TOKEN": self.admin, "AGENTLINK_DB_PATH": str(self.dir / "server.db"),
               "AGENTLINK_RECONCILE_INTERVAL": "2", "AGENTLINK_MODE": "testing", "AGENTLINK_WRITE_RATE": "0"}  # fmt: skip
        self.log = open(self.dir / "server.log", "w")  # noqa: SIM115
        self.proc = subprocess.Popen([sys.executable, "-m", "agentlink.serve", "--port", str(self.port), "--env-file",
                                      str(self.dir / "none.env")], cwd=ROOT, env=env, stdout=self.log, stderr=self.log)  # fmt: skip
        self.adm = Api(self.url, self.admin)
        for _ in range(100):
            try:
                self.adm.call("GET", "/api/health")
                break
            except OSError:
                time.sleep(0.1)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        wrapper = self.bin / "agentlink"
        wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} -m agentlink.cli "$@"\n')
        wrapper.chmod(0o755)
        self.adm.call("POST", "/api/projects", {"name": "e2e", "description": "real runtime e2e"})
        self.tokens: dict[str, str] = {}

    def agent(self, name: str, spec: str, owner: str) -> str:
        r = self.adm.call(
            "POST", "/api/admin/agents", {"name": name, "owner": owner, "specialization": spec, "projects": ["e2e"]}
        )
        self.tokens[name] = r["token"]
        return r["token"]

    def channel(self) -> None:
        self.adm.call("POST", "/api/channels", {"name": "e2e-dev", "project": "e2e", "members": list(self.tokens)})

    def participant(self, name: str, runtime: str) -> dict[str, Any]:
        home = self.dir / f"home-{name}"
        home.mkdir()
        ws = self.dir / f"ws-{name}"
        ws.mkdir()
        subprocess.run(["git", "init", "-q", str(ws)], check=True)
        cfg = {"server": self.url, "token": self.tokens[name], "project": "e2e", "device": f"e2e-{name}", "runtime": runtime,
               "agent": name, "channel": "e2e-dev"}  # fmt: skip
        (home / "config.json").write_text(json.dumps(cfg))
        os.chmod(home / "config.json", 0o600)
        env = {
            **os.environ,
            "AGENTLINK_HOME": str(home),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "PYTHONPATH": str(ROOT),
        }
        store = Store(home / "state.db")
        api = Api(self.url, self.tokens[name])
        return {"name": name, "home": home, "ws": ws, "env": env, "store": store, "api": api,
                "sup": Supervisor(api, store, cfg, poll_wait=0)}  # fmt: skip

    def delivery(self, mid: int) -> list[dict[str, Any]]:
        return self.adm.call("GET", f"/api/messages/{mid}/deliveries")["recipients"]

    def messages(self, **q: Any) -> list[dict[str, Any]]:
        return self.adm.call("GET", "/api/channels/e2e-dev/messages", query={"limit": 200, **q})["messages"]

    def close(self) -> None:
        self.proc.terminate()
        self.proc.wait(10)
        self.log.close()
        if not self.keep:
            shutil.rmtree(self.dir, ignore_errors=True)


def run_claude(p: dict[str, Any], prompt: str, resume: str | None = None, turns: int = 8) -> dict[str, Any]:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--max-turns",
        str(turns),
        "--allowedTools",
        "Bash(agentlink:*)",
    ]
    if resume:
        cmd += ["--resume", resume]
    t0 = time.time()
    r = subprocess.run(cmd, cwd=p["ws"], env=p["env"], capture_output=True, text=True, timeout=600, check=False)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = {"raw": r.stdout[-1500:]}
    return {"exit": r.returncode, "seconds": round(time.time() - t0, 1), "session_id": out.get("session_id"),
            "result": str(out.get("result") or out.get("raw") or "")[:1500], "stderr": r.stderr[-600:]}  # fmt: skip


def scenario_claude(env: Env, sender: Api) -> bool:
    p = env.participant("rc-claude", "claude-code")
    inst = ADAPTERS["claude-code"].install("project", str(p["ws"]))
    step("claude: hooks installed in project scope", bool(inst["added"]), added=inst["added"])
    word = "ORCHID-" + secrets.token_hex(2).upper()
    m = sender.call("POST", "/api/channels/e2e-dev/messages",
                    {"body": f"@rc-claude Please confirm you received this by replying with the code {word} (reply via agentlink).",
                     "priority": 1})  # fmt: skip
    step("claude: message persisted on server", any(x["id"] == m["id"] for x in env.messages()), message_id=m["id"])
    new = p["sup"].once(wait=0)["new"]
    step("claude: supervisor received and stored it", m["id"] in new)
    step("claude: server shows delivered_to_supervisor", env.delivery(m["id"])[0]["state"] == "delivered_to_supervisor")
    run = run_claude(
        p,
        "You are working in this repository. Handle any Agent Office messages addressed to you exactly as they ask, then stop.",
    )
    EVIDENCE["results"]["claude_run"] = run
    sess = [s for s in p["store"].q("SELECT * FROM sessions")]
    step(
        "claude: SessionStart hook registered a server session",
        bool(sess and sess[0]["server_id"]),
        local_session=run.get("session_id"),
    )
    replies = [x for x in env.messages() if x.get("reply_to") == m["id"] and x["author"] == "rc-claude"]
    step(
        "claude: agent performed observable action (reply authored by rc-claude)",
        bool(replies),
        reply=replies[0]["body"][:200] if replies else None,
    )
    step("claude: reply contains the requested code", bool(replies) and word in replies[0]["body"])
    st = env.delivery(m["id"])[0]
    step(
        "claude: delivery state processed with evidence",
        st["state"] == "processed" and bool(st.get("evidence")),
        delivery=st,
    )
    ADAPTERS["claude-code"].uninstall("project", str(p["ws"]))
    return bool(replies) and st["state"] == "processed"


def scenario_headless(env: Env, sender: Api, runtime: str, name: str) -> bool:
    p = env.participant(name, runtime)
    if runtime == "opencode" and os.environ.get("AGENTLINK_OPENCODE_MODEL"):
        p["store"].put("adapter.opencode", {"model": os.environ["AGENTLINK_OPENCODE_MODEL"]})
    word = ("CEDAR-" if runtime == "codex" else "MAPLE-") + secrets.token_hex(2).upper()
    m = sender.call(
        "POST",
        "/api/channels/e2e-dev/messages",
        {"body": f"@{name} What is 6 times 7? Include the code {word} in your answer."},
    )
    p["sup"].once(wait=0)
    step(
        f"{runtime}: supervisor received and stored it", env.delivery(m["id"])[0]["state"] == "delivered_to_supervisor"
    )
    # The production path: `agentlink connect --headless-delivery` config, then the supervisor loop decides to run it.
    cfg = dict(p["store"].get(f"adapter.{runtime}", {}) or {})
    p["store"].put(f"adapter.{runtime}", {**cfg, "headless": True, "workdir": str(p["ws"])})
    p["sup"].cfg["runtime"] = runtime
    res = p["sup"].once(wait=0)["headless"] or {}
    EVIDENCE["results"][f"{runtime}_exec"] = res
    EVIDENCE["results"][f"{runtime}_path"] = "Supervisor.once -> headless_if_enabled -> exec_deliver"
    step(
        f"{runtime}: runtime turn executed",
        res.get("exit_code") == 0,
        seconds=res.get("seconds"),
        answer=res.get("answer", "")[:200],
    )
    replies = [x for x in env.messages() if x.get("reply_to") == m["id"] and x["author"] == name]
    ok = bool(replies) and word in replies[0]["body"] and "42" in replies[0]["body"]
    step(f"{runtime}: answer relayed as reply authored by {name} (contains 42 and code)", ok)
    st = env.delivery(m["id"])[0]
    step(f"{runtime}: delivery processed with reply evidence", st["state"] == "processed", delivery=st)
    return ok and st["state"] == "processed"


def scenario_cross(env: Env) -> bool:
    c = env.participant("x-claude", "claude-code")
    x = env.participant("x-codex", "codex")
    ADAPTERS["claude-code"].install("project", str(c["ws"]))
    c["sup"].once(wait=0)
    first = run_claude(
        c,
        'Use the shell command `agentlink post e2e-dev "@x-codex <question>" --kind question` to ask the agent '
        "x-codex: 'Which HTTP status code means Not Found? Answer with the number.' Then stop.",
        turns=6,
    )
    EVIDENCE["results"]["cross_claude_send"] = first
    asks = [m for m in env.messages() if m["author"] == "x-claude" and "x-codex" in (m.get("mentions") or [])]
    if not step("cross: Claude Code itself posted the request", bool(asks), session=first.get("session_id")):
        return False
    q = asks[-1]
    x["sup"].once(wait=0)
    step("cross: Codex supervisor received it", env.delivery(q["id"])[0]["state"] == "delivered_to_supervisor")
    res = x["sup"].exec_deliver("codex", str(x["ws"]))
    EVIDENCE["results"]["cross_codex"] = res
    ans = [m for m in env.messages() if m.get("reply_to") == q["id"] and m["author"] == "x-codex"]
    if not step(
        "cross: Codex answered (relayed reply)",
        bool(ans) and "404" in ans[0]["body"],
        answer=ans[0]["body"][:200] if ans else None,
    ):
        return False
    c["sup"].once(wait=0)
    step(
        "cross: answer delivered to Claude's supervisor",
        env.delivery(ans[0]["id"])[0]["state"] == "delivered_to_supervisor",
    )
    second = run_claude(c, "Continue. If Agent Office delivered an answer to you, acknowledge it with `agentlink ack <id>` and "
                           "tell me the number it contains.", resume=first.get("session_id"), turns=6)  # fmt: skip
    EVIDENCE["results"]["cross_claude_receive"] = second
    st = env.delivery(ans[0]["id"])[0]
    ok = st["state"] in ("acknowledged", "processed")
    step(
        "cross: the same Claude session got the answer injected and acknowledged it",
        ok,
        delivery=st,
        claude_said=second["result"][:200],
    )
    step("cross: Claude's final text mentions 404", "404" in second["result"])
    ADAPTERS["claude-code"].uninstall("project", str(c["ws"]))
    return ok


def main() -> None:
    if os.environ.get("RUN_REAL_RUNTIMES") != "1":
        sys.exit("refusing to run: set RUN_REAL_RUNTIMES=1 (this test uses real runtimes and spends quota)")
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="claude,codex,opencode,cross")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    only = set(a.only.split(","))
    env = Env(a.keep)
    EVIDENCE["runtimes"] = {n: ADAPTERS[n].detect() for n in ("claude-code", "codex", "opencode")}
    try:
        env.agent("synthetic-sender", "coordination", "dev00")
        for name, spec, owner in (("rc-claude", "backend", "dev01"), ("rx-codex", "backend", "dev02"),
                                  ("ro-opencode", "frontend", "dev03"), ("x-claude", "coordination", "dev04"),
                                  ("x-codex", "backend", "dev05")):  # fmt: skip
            env.agent(name, spec, owner)
        env.channel()
        sender = Api(env.url, env.tokens["synthetic-sender"])
        results = {}
        if "claude" in only:
            results["claude-code"] = scenario_claude(env, sender)
        if "codex" in only:
            results["codex"] = scenario_headless(env, sender, "codex", "rx-codex")
        if "opencode" in only:
            results["opencode"] = scenario_headless(env, sender, "opencode", "ro-opencode")
        if "cross" in only:
            results["cross_claude_codex_claude"] = scenario_cross(env)
        EVIDENCE["summary"] = results
        EVIDENCE["events"] = [e["type"] for e in env.adm.call("GET", "/api/events", query={"limit": 1000})["events"]]
    finally:
        EVIDENCE["finished"] = datetime.now(UTC).isoformat()
        out = ROOT / "docs" / "evidence" / f"runtime-e2e-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(EVIDENCE, indent=2, ensure_ascii=False))
        print(f"evidence: {out}")
        if a.keep:
            print(f"kept: {env.dir}")
        env.close()
    sys.exit(0 if all(EVIDENCE.get("summary", {}).values()) and EVIDENCE.get("summary") else 1)


if __name__ == "__main__":
    main()
