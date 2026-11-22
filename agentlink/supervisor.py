"""Local supervisor: a light, model-independent process per participant device.

It keeps the coordination alive without spending a single model token:

* receives messages by long-polling the inbox (AgentLink cursor), **persists them locally first**, then
  confirms ``delivered_to_supervisor`` (at-least-once up to this process; dedup by message id);
* reconciles after a restart from the persisted cursor and the server's pending list;
* reports delivery states reached by the hooks, retrying until the server accepts them;
* sends device/supervisor heartbeats and per-session heartbeats derived from *observed runtime
  activity* (hook events), with ``progress`` only when new tool activity was seen;
* renews task leases only for sessions with recent activity; never on device liveness alone. If a
  renewal is refused (``stale_lease`` / ``no_progress``) it records a local notice that the hooks
  inject at the next safe point (authority lost: stop publishing, re-check the repository);
* optionally pushes messages into idle runtimes that support resume (opt-in per adapter).

`once()` runs a single iteration so the whole loop is unit-testable without threads or sleeps.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .adapters import ADAPTERS
from .local import VERSION, Api, ApiError, Store, load_config

log = logging.getLogger("agentlink.supervisor")

DEVICE_EVERY = 20.0
SESSION_EVERY = 20.0
RENEW_EVERY = 60.0
ACTIVE_WINDOW = 90.0  # runtime activity this recent => the session is executing
DEAD_AFTER = 1800.0  # no runtime activity for this long and no hook => end the local session


class Supervisor:
    def __init__(self, api: Api, store: Store, cfg: dict[str, Any] | None = None, poll_wait: int = 20) -> None:
        self.api = api
        self.store = store
        self.cfg = cfg or load_config()
        self.poll_wait = poll_wait
        self.last_device = 0.0
        self.running = True
        self.status: dict[str, Any] = {"started": time.time(), "errors": 0}
        self.keepalive_every = SESSION_EVERY  # liveness cadence while a long runtime turn blocks the loop

    @contextmanager
    def keepalive(self, *extra_sessions: str) -> Iterator[None]:
        """Keep device/session heartbeats and lease renewals going while the loop is blocked in a runtime turn
        (a turn can take minutes; sessions expire after 90 s). Also beats the turn's own session(s)."""
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.keepalive_every):
                try:
                    self.device_beat(force=True)
                    self.session_beats(force=True)
                    self.renew_leases()
                    for sid in extra_sessions:
                        self.api.call("POST", f"/api/sessions/{sid}/heartbeat", {"state": "executing"})
                except Exception as e:  # keep beating; the main loop reports errors
                    log.warning("keepalive beat failed: %s", e)

        t = threading.Thread(target=beat, name="agentlink-keepalive", daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join(5)

    # -- receive -------------------------------------------------------------------------------------
    def receive(self, wait: int | None = None) -> list[int]:
        after = self.store.cursor("inbox")
        res = self.api.call("GET", "/api/inbox", query={"after": after, "wait": self.poll_wait if wait is None else wait,
                            "limit": 100}, timeout=(wait or self.poll_wait) + 30)  # fmt: skip
        msgs = res.get("messages", [])
        new = self.store.store_messages(msgs)  # 1. persist locally
        if msgs:
            self.store.set_cursor("inbox", int(res["next_after"]))  # 2. only then advance the receive cursor
        self.flush_reports()  # 3. confirm to the server (retried if this fails)
        return new

    def reconcile_pending(self) -> int:
        """After a restart: anything the server still considers pending but we never stored is re-read."""
        pend = self.api.call("GET", "/api/deliveries/pending", query={"limit": 500}).get("pending", [])
        have = {r["message_id"] for r in self.store.q("SELECT message_id FROM inbox")}
        missing = {int(p["message_id"]) for p in pend if int(p["message_id"]) not in have}
        if not missing:
            return 0
        recovered, after, top = 0, min(missing) - 1, max(missing)
        while missing and after < top:  # page through the inbox until every pending gap is filled
            res = self.api.call("GET", "/api/inbox", query={"after": after, "limit": 200})
            msgs = res.get("messages", [])
            if not msgs:
                break
            found = [m for m in msgs if int(m["id"]) in missing]
            recovered += len(self.store.store_messages(found))
            missing -= {int(m["id"]) for m in found}
            after = int(res.get("next_after") or msgs[-1]["id"])
        return recovered

    def flush_reports(self) -> None:
        """Confirm local states to the server. Never raises: one bad receipt must not block the others.

        * ``processed`` without stored evidence (older stores, or a failed `ack`) is confirmed as ``acknowledged``,
          which is the most the server can accept without evidence.
        * A rejected batch (4xx) is retried message by message; a message the server still rejects is given up
          (logged), the rest go through. Transient failures (network, 5xx, 429) stop the flush until the next loop.
        """
        for g in self.store.unreported():
            state, ids, evidence = g["state"], g["ids"], g["evidence"]
            sent = "acknowledged" if state == "processed" and not evidence else state
            body = {"state": sent, "session_id": g["session_id"], "evidence": evidence if sent == "processed" else None}
            try:
                self._post_receipts(ids, body)
            except (ApiError, OSError) as e:
                if isinstance(e, ApiError) and 400 <= e.status < 500 and e.status != 429:
                    for mid in ids:
                        try:
                            self._post_receipts([mid], body)
                            self.store.set_reported([mid], state)
                        except ApiError as e1:
                            if 400 <= e1.status < 500 and e1.status != 429:
                                log.warning("delivery receipt for #%s rejected (%s): giving up", mid, e1.payload)
                                self.store.set_reported([mid], state)
                            else:
                                return
                        except OSError:
                            return
                    continue
                log.warning("delivery receipts not sent (%s); retrying later", e)
                return
            self.store.set_reported(ids, state)

    def _post_receipts(self, ids: list[int], body: dict[str, Any]) -> None:
        try:
            self.api.call("POST", "/api/deliveries", {"message_ids": ids, **body})
        except ApiError as e:
            if e.status == 403 and body.get("session_id"):  # session no longer ours (ended/restored): report without it
                self.api.call("POST", "/api/deliveries", {"message_ids": ids, **body, "session_id": None})
            else:
                raise

    # -- liveness ------------------------------------------------------------------------------------
    def device_beat(self, force: bool = False) -> None:
        if force or time.time() - self.last_device >= DEVICE_EVERY:
            self.api.call("POST", "/api/devices/heartbeat", {"device": self.cfg.get("device"), "supervisor_version": VERSION,
                          "supervisor_ok": True})  # fmt: skip
            self.last_device = time.time()

    def session_beats(self, force: bool = False) -> None:
        now = time.time()
        for s in self.store.live_sessions():
            if not s["server_id"]:
                self._register_late(s)
                continue
            last_act = s["last_activity"] or s["started_at"]
            if now - last_act > DEAD_AFTER:
                self._end(s, "no runtime activity")
                continue
            if not force and s["last_beat"] and now - s["last_beat"] < SESSION_EVERY:
                continue
            progress = bool(s["last_progress"] and (s["last_progress_sent"] or 0) < s["last_progress"])
            state = "executing" if now - last_act < ACTIVE_WINDOW and s["state"] != "idle" else "idle"
            try:
                r = self.api.call(
                    "POST", f"/api/sessions/{s['server_id']}/heartbeat", {"state": state, "progress": progress}
                )
            except ApiError as e:
                if e.reason == "session_ended" or e.status in (403, 404):
                    self.store.x("UPDATE sessions SET ended_at = ?, end_reason = ? WHERE local_id = ?",
                                 (now, f"server: {e.reason or e.status}", s["local_id"]))  # fmt: skip
                    continue
                raise
            self.store.x("UPDATE sessions SET last_beat = ?, last_progress_sent = CASE WHEN ? THEN last_progress ELSE "
                         "last_progress_sent END WHERE local_id = ?", (now, 1 if progress else 0, s["local_id"]))  # fmt: skip
            self._track_held(s, r.get("held_tasks", []))
            coord = r.get("coordinator") or {}
            if coord.get("offer_for_you") and not self.store.get(f"offer:{coord['epoch']}"):
                self.store.put(f"offer:{coord['epoch']}", True)  # the offer itself arrives as an urgent inbox message

    def _register_late(self, s: dict[str, Any]) -> None:
        try:
            r = self.api.call("POST", "/api/sessions", {"project": s["project"] or self.cfg.get("project"), "runtime": s["runtime"],
                              "device": self.cfg.get("device"), "specialization": self.cfg.get("specialization")})  # fmt: skip
            self.store.x("UPDATE sessions SET server_id = ? WHERE local_id = ?", (r["id"], s["local_id"]))
        except ApiError:
            pass

    def _end(self, s: dict[str, Any], reason: str) -> None:
        try:
            self.api.call("POST", f"/api/sessions/{s['server_id']}/end", {"reason": reason})
        except ApiError:
            pass
        self.store.x(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE local_id = ?", (time.time(), reason, s["local_id"])
        )

    # -- task leases ---------------------------------------------------------------------------------
    def _track_held(self, s: dict[str, Any], held: list[dict[str, Any]]) -> None:
        for t in held:
            self.store.x(
                "INSERT INTO held(task_id, server_session, epoch, claimed_at) VALUES (?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE "
                "SET server_session = excluded.server_session, epoch = excluded.epoch, lost_reason = NULL "
                "WHERE held.epoch <> excluded.epoch",
                (t["id"], s["server_id"], t["lease_epoch"], time.time()),
            )

    def renew_leases(self, force: bool = False) -> list[dict[str, Any]]:
        out = []
        now = time.time()
        for h in self.store.q("SELECT * FROM held WHERE lost_reason IS NULL"):
            if not force and h["last_renew"] and now - h["last_renew"] < RENEW_EVERY:
                continue
            s = self.store.q("SELECT * FROM sessions WHERE server_id = ?", (h["server_session"],))
            if not s or s[0]["ended_at"]:
                continue
            active = bool(s[0]["last_progress"] and now - s[0]["last_progress"] < RENEW_EVERY * 2)
            try:
                self.api.call("POST", f"/api/tasks/{h['task_id']}/renew", {"session_id": h["server_session"], "lease_epoch": h["epoch"],
                              "progress": active})  # fmt: skip
                self.store.x("UPDATE held SET last_renew = ? WHERE task_id = ?", (now, h["task_id"]))
                out.append({"task_id": h["task_id"], "renewed": True})
            except ApiError as e:
                if e.reason in ("stale_lease", "no_progress", "lease_required") or e.status in (403, 404):
                    self._lost(h, e.reason or str(e.status))
                    out.append({"task_id": h["task_id"], "renewed": False, "reason": e.reason})
                else:
                    raise
        return out

    def _lost(self, h: dict[str, Any], reason: str) -> None:
        self.store.x("UPDATE held SET lost_reason = ? WHERE task_id = ?", (reason, h["task_id"]))
        notice: dict[str, Any] = {
            "id": -int(h["task_id"]) * 1000 - int(h["epoch"]),  # local-only id space (negative)
            "channel": "local", "author": "agent-office", "kind": "alert", "priority": 1,
            "body": f"TASK #{h['task_id']} | AUTHORITY LOST ({reason}). Your lease epoch {h['epoch']} is no longer valid. "
                    "Do not push or report this task as done. Keep your local changes, run `agentlink check-lease "
                    f"{h['task_id']}` and coordinate before continuing.",
        }  # fmt: skip
        self.store.store_messages([notice])
        self.store.set_reported([notice["id"]], "delivered_to_supervisor")  # local notice: nothing to report upstream
        self.store.x("UPDATE inbox SET reported = state WHERE message_id = ?", (notice["id"],))

    # -- active push (opt-in resume) -----------------------------------------------------------------
    def push_idle(self) -> list[str]:
        used: list[str] = []
        from .hooks import MAX_INJECT

        pending = self.store.pending(only_new=True)[:MAX_INJECT]
        if not pending:
            return used
        for s in self.store.live_sessions():
            ad = ADAPTERS.get(s["runtime"])
            if ad is None or s["state"] != "idle":
                continue
            from .hooks import render

            text = render(pending, s["project"], s["server_id"])
            mech = ad.push(self.store, s, text)
            if mech == "codex exec resume":
                self._codex_resume(s, pending, text)
            elif mech == "opencode run --session":
                self._opencode_run(s, pending, text)
            if mech:
                self.store.mark([int(m["message_id"]) for m in pending], "injected_to_runtime", s["server_id"], mech)
                used.append(mech)
                break
        return used

    def _relay(self, s: dict[str, Any], pending: list[dict[str, Any]], answer: str, mech: str) -> None:
        """The runtime answered but could not call the API itself (sandbox): post its answer as its reply."""
        answer = answer.strip()
        if not answer:
            return
        first = pending[0]
        r = self.api.call("POST", f"/api/channels/{first['channel']}/messages", {"body": answer[:15000], "reply_to": first["message_id"],
                          "mentions": [first["author"]] if first.get("author") else None,
                          "data": {"relayed_by": "agentlink-supervisor", "mechanism": mech, "session_id": s["server_id"]}})  # fmt: skip
        ids = [int(m["message_id"]) for m in pending if int(m["message_id"]) > 0]
        self.api.call("POST", "/api/deliveries", {"message_ids": ids, "state": "processed", "session_id": s["server_id"],
                      "evidence": {"reply_message_id": r["id"], "adapter": s["runtime"], "mechanism": mech}})  # fmt: skip
        self.store.mark(ids, "processed", evidence={"reply_message_id": r["id"]})
        self.store.set_reported(ids, "processed")

    def _codex_resume(self, s: dict[str, Any], pending: list[dict[str, Any]], text: str) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("r", suffix=".txt") as out:
            cmd = ["codex", "exec", "resume", s["local_id"], "-o", out.name, text + "\n\nAnswer the message(s) directly; "
                   "your final message will be posted as your reply."]  # fmt: skip
            with self.keepalive():
                ok = _run(cmd, s["cwd"] or None, 600).returncode == 0
            if ok:
                self._relay(s, pending, open(out.name, encoding="utf-8").read(), "codex exec resume")  # noqa: SIM115

    def _opencode_run(self, s: dict[str, Any], pending: list[dict[str, Any]], text: str) -> None:
        cmd = ["opencode", "run", "--session", s["local_id"], text + "\n\nAnswer the message(s) directly; your final "
               "message will be posted as your reply."]  # fmt: skip
        with self.keepalive():
            res = _run(cmd, s["cwd"] or None, 600)
        if res.returncode == 0:
            self._relay(s, pending, res.stdout, "opencode run --session")

    def exec_deliver(
        self, runtime: str, workdir: str, timeout: int = 600, pending: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Deliver pending messages by starting a headless turn of a runtime that has no trusted hooks.

        Codex: ``codex exec`` (read-only sandbox, no network) and OpenCode: ``opencode run``. The runtime
        answers in its final message; the supervisor posts that answer as the agent's reply (with the agent's
        own token) and records ``processed`` with the reply id as evidence. Nothing is reported as processed
        if the runtime produced no answer.
        """

        from .hooks import MAX_INJECT, render

        # one turn answers at most what it is shown; the rest stays pending for the next turn
        if pending is None:
            pending = [m for m in self.store.pending(only_new=True) if int(m["message_id"]) > 0]
        pending = pending[:MAX_INJECT]
        if not pending:
            return {"delivered": 0}

        sess = self.api.call("POST", "/api/sessions", {"project": self.cfg.get("project"), "runtime": runtime,
                             "device": self.cfg.get("device"), "specialization": self.cfg.get("specialization"),
                             "label": f"{runtime} headless delivery"})  # fmt: skip
        text = render(pending, self.cfg.get("project"), None, headless=True) + (
            "\n\nAnswer the message(s) directly in your final message, without using any tools. "
            "Agent Office posts your final message as your reply."
        )
        ids = [int(m["message_id"]) for m in pending]
        self.store.mark(ids, "injected_to_runtime", sess["id"], f"{runtime} exec")
        self.api.call("POST", "/api/deliveries", {"message_ids": ids, "state": "injected_to_runtime", "session_id": sess["id"],
                      "evidence": {"adapter": runtime, "mechanism": "headless exec"}})  # fmt: skip
        self.store.set_reported(ids, "injected_to_runtime")
        self.api.call("POST", f"/api/sessions/{sess['id']}/heartbeat", {"state": "executing", "progress": True})
        started = time.time()
        with self.keepalive(sess["id"]):
            res, answer = self._headless_turn(runtime, workdir, timeout, text)
        result: dict[str, Any] = {"runtime": runtime, "session_id": sess["id"], "exit_code": res.returncode,
                                  "seconds": round(time.time() - started, 1), "answer": answer.strip()[:2000]}  # fmt: skip
        if answer.strip() and res.returncode == 0:
            self._relay({"server_id": sess["id"], "runtime": runtime}, pending, answer, f"{runtime} exec")
            result["processed"] = ids
            self._retry_forget(ids)
        else:  # stays injected_to_runtime: pending and recoverable, never a false "processed"
            result["stderr_tail"] = (res.stderr or "")[-800:]
            result["stdout_tail"] = (res.stdout or "")[-800:]
            result["retry"] = self._retry_schedule(runtime, ids, res.returncode)
        self.api.call("POST", f"/api/sessions/{sess['id']}/end", {"reason": "headless turn finished"})
        return result

    def _headless_turn(
        self, runtime: str, workdir: str, timeout: int, text: str
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        import tempfile

        if runtime == "codex":
            with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as out:
                path = out.name
            res = _run(["codex", "exec", "--skip-git-repo-check", "-o", path, text], workdir, timeout)
            answer = Path(path).read_text("utf-8") if Path(path).exists() and res.returncode == 0 else ""
            Path(path).unlink(missing_ok=True)
        elif runtime == "opencode":
            model = (self.store.get("adapter.opencode", {}) or {}).get("model") or os.environ.get(
                "AGENTLINK_OPENCODE_MODEL"
            )
            res = _run(
                ["opencode", "run", "--format", "json", *(["-m", model] if model else []), text], workdir, timeout
            )
            answer = _opencode_text(res.stdout)
        else:
            raise ValueError(f"no headless delivery for {runtime}")
        return res, answer

    # -- bounded retry of failed headless turns -------------------------------------------------------
    RETRY_KEY = "headless.retry"

    def _retry_schedule(self, runtime: str, ids: list[int], code: int) -> dict[str, Any]:
        """A turn that failed to run or answered nothing is retried a bounded number of times with growing delays
        (each retry spends quota). A timeout is not retried: the same request would very likely time out again."""
        cfg = self.store.get(f"adapter.{runtime}", {}) or {}
        retries, base = int(cfg.get("retries", 2)), float(cfg.get("retry_after", 300))
        book = self.store.get(self.RETRY_KEY, {}) or {}
        out: dict[str, Any] = {}
        for mid in ids:
            n = int((book.get(str(mid)) or {}).get("n", 0)) + 1
            if code == 124 or n > retries:
                book.pop(str(mid), None)
                out[str(mid)] = "given up" if code != 124 else "timeout: not retried"
                log.warning("headless delivery of #%s abandoned (%s); it stays pending", mid, out[str(mid)])
            else:
                book[str(mid)] = {"n": n, "at": time.time() + base * 2 ** (n - 1)}
                out[str(mid)] = f"retry {n}/{retries}"
        self.store.put(self.RETRY_KEY, book)
        return out

    def _retry_forget(self, ids: list[int]) -> None:
        book = self.store.get(self.RETRY_KEY, {}) or {}
        if any(book.pop(str(i), None) for i in ids):
            self.store.put(self.RETRY_KEY, book)

    def _retry_due(self) -> list[dict[str, Any]]:
        book = self.store.get(self.RETRY_KEY, {}) or {}
        due = [int(k) for k, v in book.items() if float(v.get("at", 0)) <= time.time()]
        if not due:
            return []
        rows = self.store.q(
            f"SELECT * FROM inbox WHERE state = 'injected_to_runtime' AND message_id IN ({','.join('?' * len(due))}) "
            "ORDER BY priority, message_id",
            tuple(due),
        )
        gone = set(due) - {int(r["message_id"]) for r in rows}  # answered meanwhile (e.g. by hand)
        if gone:
            self._retry_forget(sorted(gone))
        return rows

    # -- loop ----------------------------------------------------------------------------------------
    def once(self, wait: int | None = None) -> dict[str, Any]:
        self.device_beat()
        new = self.receive(wait)
        self.session_beats()
        renewed = self.renew_leases()
        pushed = self.push_idle()
        headless = None if pushed else self.headless_if_enabled()
        self.flush_reports()
        self.store.put("supervisor_status", {"at": time.time(), "pid": __import__("os").getpid(), "version": VERSION})
        return {"new": new, "renewed": renewed, "pushed": pushed, "headless": headless}

    def headless_if_enabled(self) -> dict[str, Any] | None:
        """Opt-in autonomous delivery for runtimes without trusted hooks (codex, opencode): when NEW messages are
        pending and no hooked session of that runtime is running, answer them with one headless turn. A failed turn
        leaves them pending (never processed) and is retried at most `retries` times (default 2) after growing delays
        (`retry_after`, default 300 s); timeouts are not retried. Heartbeats keep running during the turn."""
        runtime = self.cfg.get("runtime")
        if runtime not in ("codex", "opencode"):
            return None
        cfg = self.store.get(f"adapter.{runtime}", {}) or {}
        if not cfg.get("headless") or any(s["runtime"] == runtime for s in self.store.live_sessions()):
            return None
        todo = [m for m in self.store.pending(only_new=True) if int(m["message_id"]) > 0] or self._retry_due()
        if not todo:
            return None
        return self.exec_deliver(
            runtime, cfg.get("workdir") or os.getcwd(), timeout=int(cfg.get("timeout") or 300), pending=todo
        )

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        try:
            self.reconcile_pending()
        except (ApiError, OSError) as e:
            log.warning("initial reconciliation failed: %s", e)
        backoff = 1.0
        while self.running:
            try:
                self.once()
                backoff = 1.0
            except (ApiError, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as e:
                self.status["errors"] += 1
                self.store.put("supervisor_last_error", {"at": time.time(), "error": str(e)[:300]})
                log.warning("supervisor iteration failed (%s); retrying in %.0fs", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


def _run(cmd: list[str], cwd: str | None, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a runtime turn; a timeout or a missing binary is a failed turn (returncode != 0), never a supervisor crash."""
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return subprocess.CompletedProcess(cmd, 124, out, f"timed out after {timeout}s")
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def _opencode_text(stdout: str) -> str:
    """Final assistant text from `opencode run --format json` (JSON lines); falls back to raw stdout."""
    texts: list[str] = []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        part = ev.get("part") if isinstance(ev, dict) else None
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            texts.append(str(part["text"]))
    return "\n".join(texts)  # no assistant text => no answer (never relay raw tool/protocol output)
