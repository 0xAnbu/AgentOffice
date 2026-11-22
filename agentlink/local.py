"""Client-side state for the local supervisor and the runtime hooks (standard library only).

Everything lives in ``$AGENTLINK_HOME`` (default ``~/.agentlink``), mode 0700:

    config.json   server URL, the participant's own agent token (file mode 0600), project, device name
    state.db      SQLite: inbox (persisted *before* it is acknowledged to the server), cursors, local
                  sessions (runtime id <-> server session id), runtime activity, held task leases

The token is the participant's own credential on their own machine; the server never ships or
stores client configuration. Two cursors are kept apart on purpose: the *receive* cursor (what was
downloaded) and the per-message *processing* state (what the runtime actually got/handled).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any

from .cli import OPENER, check_server_url

VERSION = "1.0.0"


def home() -> Path:
    return Path(os.environ.get("AGENTLINK_HOME", Path.home() / ".agentlink"))


def _private_dir(p: Path) -> Path:
    """Create (or tighten) a directory only the user can enter: config, tokens and message bodies live here."""
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    if p.stat().st_mode & 0o077:
        p.chmod(0o700)  # mkdir's mode is ignored for existing directories and masked by the umask for new ones
    return p


# -- configuration -----------------------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    f = home() / "config.json"
    try:
        cfg = json.loads(f.read_text("utf-8"))
    except (OSError, ValueError):
        cfg = {}
    # environment wins, so CI / tests / ad-hoc shells can override without touching the file
    for key, env in (("server", "AGENTLINK_URL"), ("token", "AGENTLINK_TOKEN"), ("project", "AGENTLINK_PROJECT")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg.setdefault("device", socket.gethostname())
    return cfg


def save_config(cfg: dict[str, Any]) -> Path:
    d = _private_dir(home())
    f = d / "config.json"
    tmp = d / "config.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, f)
    return f


# -- HTTP --------------------------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, payload: Any) -> None:
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {"message": str(payload)}

    @property
    def reason(self) -> str:
        return str(self.payload.get("reason") or self.payload.get("error") or "")


class Api:
    """Small JSON client that raises instead of exiting (the CLI's Client exits; libraries must not)."""

    def __init__(self, server: str, token: str, timeout: float = 30) -> None:
        self.server = check_server_url(server)
        self.token = token
        self.timeout = timeout

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        query: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        q = {k: v for k, v in (query or {}).items() if v is not None and v != ""}
        url = self.server + path + ("?" + urllib.parse.urlencode(q) if q else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - scheme checked in __init__
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("User-Agent", f"agentlink-supervisor/{VERSION}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with OPENER.open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw or b"null")
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                raise ApiError(e.code, {"error": "redirect", "message": f"server redirected to {e.headers.get('Location')}; "
                               "not followed (it would resend the token). Fix the server URL."}) from None  # fmt: skip
            raw = e.read().decode("utf-8", "replace")
            try:
                payload: Any = json.loads(raw)
            except ValueError:
                payload = raw
            raise ApiError(e.code, payload) from None


def api_from_config(cfg: dict[str, Any] | None = None) -> Api:
    cfg = cfg or load_config()
    if not cfg.get("server") or not cfg.get("token"):
        raise SystemExit(
            "not connected: run `agentlink connect --server URL --token TOKEN` (or set AGENTLINK_URL/AGENTLINK_TOKEN)"
        )
    return Api(cfg["server"], cfg["token"])


# -- local state -------------------------------------------------------------------------------------

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS inbox (
        message_id INTEGER PRIMARY KEY, channel TEXT, author TEXT, kind TEXT, body TEXT, priority INTEGER,
        data TEXT, thread_id INTEGER, received_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'delivered_to_supervisor',
        reported TEXT NOT NULL DEFAULT '', injected_at REAL, injected_session TEXT, injected_via TEXT, evidence TEXT)""",
    "CREATE TABLE IF NOT EXISTS cursors (key TEXT PRIMARY KEY, value INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS sessions (
        local_id TEXT PRIMARY KEY, runtime TEXT NOT NULL, server_id TEXT NOT NULL, parent_local_id TEXT,
        project TEXT, cwd TEXT, started_at REAL NOT NULL, last_activity REAL, last_progress REAL,
        last_beat REAL, last_progress_sent REAL, ended_at REAL, end_reason TEXT, state TEXT NOT NULL DEFAULT 'idle',
        pid INTEGER, transcript TEXT)""",
    """CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, local_id TEXT, at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS held (
        task_id INTEGER PRIMARY KEY, server_session TEXT NOT NULL, epoch INTEGER NOT NULL, claimed_at REAL NOT NULL,
        last_renew REAL, lost_reason TEXT)""",
    "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
]


class Store:
    """Process-safe local store (several hook processes + the supervisor may write concurrently)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (_private_dir(home()) / "state.db")
        with closing(self._conn()) as c:
            for stmt in SCHEMA:
                c.execute(stmt)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(inbox)").fetchall()}
            if "evidence" not in cols:  # stores created by older versions
                c.execute("ALTER TABLE inbox ADD COLUMN evidence TEXT")
        for f in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):  # message bodies: owner only
            if f.exists() and f.stat().st_mode & 0o077:
                f.chmod(0o600)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=15000")
        return c

    def q(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with closing(self._conn()) as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def x(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with closing(self._conn()) as c:
            return c.execute(sql, params).rowcount

    # cursors --------------------------------------------------------------------------------------
    def cursor(self, key: str) -> int:
        rows = self.q("SELECT value FROM cursors WHERE key = ?", (key,))
        return int(rows[0]["value"]) if rows else 0

    def set_cursor(self, key: str, value: int) -> None:
        self.x(
            "INSERT INTO cursors(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = MAX(value, excluded.value)",
            (key, value),
        )

    def get(self, key: str, default: Any = None) -> Any:
        rows = self.q("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def put(self, key: str, value: Any) -> None:
        self.x(
            "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # inbox ----------------------------------------------------------------------------------------
    def store_messages(self, msgs: list[dict[str, Any]]) -> list[int]:
        """Persist downloaded messages. Returns ids that were new (dedup by server message id)."""
        new = []
        now = time.time()
        with closing(self._conn()) as c:
            for m in msgs:
                cur = c.execute(
                    "INSERT OR IGNORE INTO inbox(message_id, channel, author, kind, body, priority, data, thread_id, received_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (m["id"], m.get("channel"), m.get("author"), m.get("kind"), m.get("body"), int(m.get("priority") or 2),
                     json.dumps(m.get("data")) if m.get("data") else None, m.get("thread_id") or m.get("id"), now),
                )  # fmt: skip
                if cur.rowcount:
                    new.append(int(m["id"]))
        return new

    def pending(self, max_priority: int = 3, only_new: bool = False) -> list[dict[str, Any]]:
        """Messages the runtime has not received yet (or not acknowledged when only_new is False)."""
        states = ("delivered_to_supervisor",) if only_new else ("delivered_to_supervisor", "injected_to_runtime")
        ph = ",".join("?" * len(states))
        return self.q(
            f"SELECT * FROM inbox WHERE state IN ({ph}) AND priority <= ? ORDER BY priority, message_id",
            (*states, max_priority),
        )

    def mark(
        self,
        ids: list[int],
        state: str,
        session: str | None = None,
        via: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        """Advance the local state, never backwards. One conditional UPDATE per message, so hooks and the supervisor
        (separate processes) cannot interleave a read and a write and regress a state."""
        order = ["delivered_to_supervisor", "injected_to_runtime", "acknowledged", "processed"]
        rank = "CASE state " + " ".join(f"WHEN '{st}' THEN {i}" for i, st in enumerate(order)) + " ELSE -1 END"
        with closing(self._conn()) as c:
            for mid in ids:
                c.execute(
                    "UPDATE inbox SET state = ?, injected_at = COALESCE(injected_at, CASE WHEN ? = 'injected_to_runtime' "
                    "THEN ? END), injected_session = COALESCE(?, injected_session), injected_via = COALESCE(?, injected_via), "
                    f"evidence = COALESCE(?, evidence) WHERE message_id = ? AND {rank} < ?",
                    (state, state, time.time(), session, via, json.dumps(evidence) if evidence else None, mid,
                     order.index(state)),
                )  # fmt: skip

    def unreported(self) -> list[dict[str, Any]]:
        """States reached locally but not yet confirmed to the server, grouped by (state, session, evidence)."""
        groups: dict[tuple[str, str | None, str | None], list[int]] = {}
        for r in self.q(
            "SELECT message_id, state, injected_session, evidence FROM inbox WHERE state <> reported AND message_id > 0 "
            "ORDER BY message_id"
        ):
            sid = r["injected_session"] if r["state"] != "delivered_to_supervisor" else None
            groups.setdefault((r["state"], sid, r["evidence"]), []).append(int(r["message_id"]))
        return [
            {"state": st, "session_id": sid, "evidence": json.loads(ev) if ev else None, "ids": ids}
            for (st, sid, ev), ids in groups.items()
        ]

    def set_reported(self, ids: list[int], state: str) -> None:
        with closing(self._conn()) as c:
            for mid in ids:
                c.execute("UPDATE inbox SET reported = ? WHERE message_id = ? AND state = ?", (state, mid, state))

    # sessions & activity --------------------------------------------------------------------------
    def session(self, local_id: str) -> dict[str, Any] | None:
        rows = self.q("SELECT * FROM sessions WHERE local_id = ?", (local_id,))
        return rows[0] if rows else None

    def live_sessions(self) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY started_at")

    def activity(self, local_id: str, kind: str, detail: str = "", progress: bool = True) -> None:
        now = time.time()
        self.x(
            "INSERT INTO activity(local_id, at, kind, detail) VALUES (?, ?, ?, ?)", (local_id, now, kind, detail[:300])
        )
        self.x(
            "UPDATE sessions SET last_activity = ?, last_progress = CASE WHEN ? THEN ? ELSE last_progress END WHERE local_id = ?",
            (now, 1 if progress else 0, now, local_id),
        )
        self.x("DELETE FROM activity WHERE id < (SELECT MAX(id) - 2000 FROM activity)")
