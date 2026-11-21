"""Runtime configuration, read once from environment variables.

Every knob has a safe default so `make dev` works with zero configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    admin_token: str = ""
    database_url: str = ""  # postgres://... ; empty => SQLite
    sqlite_path: str = "data/agentlink.db"
    github_webhook_secret: str = ""
    max_wait_seconds: int = 25  # long-poll ceiling (keep < platform timeout)
    poll_interval: float = 1.0  # DB re-check period during long-poll
    write_rate_per_min: int = 240  # per token, all mutating calls
    read_rate_per_min: int = 1200  # per token per instance, best effort
    max_body_bytes: int = 256 * 1024
    allowed_origins: tuple[str, ...] = field(default_factory=tuple)
    on_vercel: bool = False
    # -- Agent Office recovery policy (seconds; all judged on the server clock) --------------------
    mode: str = "development"  # development | testing | production (labels only: same code paths)
    session_ttl: int = 90  # no session heartbeat for this long => session expired
    stall_after: int = 600  # executing without progress for this long => session stalled
    task_lease_default: int = 300  # task lease TTL when the claimer does not ask for one
    task_lease_max: int = 3600
    progress_window: int = 900  # a lease is only renewed if progress was reported this recently
    coordinator_lease: int = 120  # renewed by the coordinator session's heartbeats
    offer_timeout: int = 180  # a provisional coordinator must accept within this window
    orphan_hold: int = 30  # orphaned tasks return to 'open' after this (they are claimable meanwhile)
    wait_timeout: int = 3600  # blocked/waiting tasks raise an alert after this long
    delivery_overdue: int = 900  # high-priority mention not acknowledged after this long => alert
    reconcile_interval: float = 5.0  # background reconciler period; 0 disables the loop (tests)
    office_config: str = ""  # path to the building/floors JSON; empty => built-in default

    @classmethod
    def from_env(cls) -> Settings:
        db_url = (
            os.environ.get("AGENTLINK_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
            or os.environ.get("POSTGRES_URL")
            or ""
        ).strip()
        origins = tuple(
            o.strip().rstrip("/") for o in os.environ.get("AGENTLINK_ALLOWED_ORIGINS", "").split(",") if o.strip()
        )
        return cls(
            admin_token=os.environ.get("AGENTLINK_ADMIN_TOKEN", "").strip(),
            database_url=db_url,
            sqlite_path=os.environ.get("AGENTLINK_DB_PATH", "data/agentlink.db"),
            github_webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip(),
            max_wait_seconds=_int("AGENTLINK_MAX_WAIT", 25),
            poll_interval=_float("AGENTLINK_POLL_INTERVAL", 2.0 if db_url else 1.0),
            write_rate_per_min=_int("AGENTLINK_WRITE_RATE", 240),
            read_rate_per_min=_int("AGENTLINK_READ_RATE", 1200),
            max_body_bytes=_int("AGENTLINK_MAX_BODY", 256 * 1024),
            allowed_origins=origins,
            on_vercel=bool(os.environ.get("VERCEL")),
            mode=os.environ.get("AGENTLINK_MODE", "development").strip() or "development",
            session_ttl=_int("AGENTLINK_SESSION_TTL", 90),
            stall_after=_int("AGENTLINK_STALL_AFTER", 600),
            task_lease_default=_int("AGENTLINK_TASK_LEASE", 300),
            task_lease_max=_int("AGENTLINK_TASK_LEASE_MAX", 3600),
            progress_window=_int("AGENTLINK_PROGRESS_WINDOW", 900),
            coordinator_lease=_int("AGENTLINK_COORDINATOR_LEASE", 120),
            offer_timeout=_int("AGENTLINK_OFFER_TIMEOUT", 180),
            orphan_hold=_int("AGENTLINK_ORPHAN_HOLD", 30),
            wait_timeout=_int("AGENTLINK_WAIT_TIMEOUT", 3600),
            delivery_overdue=_int("AGENTLINK_DELIVERY_OVERDUE", 900),
            reconcile_interval=_float("AGENTLINK_RECONCILE_INTERVAL", 5.0),
            office_config=os.environ.get("AGENTLINK_OFFICE_CONFIG", "").strip(),
        )
