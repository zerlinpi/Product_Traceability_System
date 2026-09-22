"""Login throttling: per-username and per-IP failure tracking with a short lockout.

Why
---
Without it, the login endpoint is an unlimited oracle: an attacker can try
passwords as fast as the LAN allows. On a factory LAN the realistic threat is not
a remote botnet but a curious insider with a browser, so the bar is "make
guessing impractical and leave a trace", not "resist a nation state".

Design
------
Failures are **events**, not a counter column. A counter has to be reset, and
resetting it races with concurrent failures; a windowed count of recorded events
cannot drift out of sync. Old rows are pruned by the same window, so the table
stays small.

Two dimensions, deliberately:

- **per username** — stops guessing one account's password
- **per IP** — stops guessing many accounts from one workstation

Both windows are checked before the password is verified, and the rejection is
**identical for existing and non-existing usernames**, so the response cannot be
used to enumerate accounts.

The lockout-DoS trade-off
--------------------------
A per-username lockout can be abused: an attacker who keeps failing a known
username keeps that account locked. The lockout is therefore anchored to the
**oldest** failure in the window, which bounds any single burst to
``lockout_seconds`` rather than letting the attacker extend it indefinitely.
That is a mitigation, not a cure — an operator who is locked out mid-shift needs
a way back in, so ``manage.py login-unlock`` exists for exactly that.

Flask-free, like the other ``traceability`` support modules.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

SCOPE_USERNAME = "username"
SCOPE_IP = "ip"

# Defaults chosen for a single-factory LAN. Generous enough that ordinary typos
# and a forgotten password never lock a shift out; tight enough that online
# guessing is impractical.
DEFAULT_WINDOW_SECONDS = 15 * 60
DEFAULT_LOCKOUT_SECONDS = 15 * 60
DEFAULT_MAX_FAILURES_PER_USERNAME = 5
DEFAULT_MAX_FAILURES_PER_IP = 20


@dataclass(frozen=True)
class LoginPolicy:
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    lockout_seconds: int = DEFAULT_LOCKOUT_SECONDS
    max_failures_per_username: int = DEFAULT_MAX_FAILURES_PER_USERNAME
    max_failures_per_ip: int = DEFAULT_MAX_FAILURES_PER_IP

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "LoginPolicy":
        """Read overrides from ``PTS_LOGIN_*``. Unparseable values keep the default."""
        import os

        env = os.environ if environment is None else environment

        def number(name: str, fallback: int) -> int:
            raw = str(env.get(name) or "").strip()
            if not raw:
                return fallback
            try:
                value = int(raw)
            except ValueError:
                return fallback
            return value if value > 0 else fallback

        return cls(
            window_seconds=number("PTS_LOGIN_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS),
            lockout_seconds=number("PTS_LOGIN_LOCKOUT_SECONDS", DEFAULT_LOCKOUT_SECONDS),
            max_failures_per_username=number(
                "PTS_LOGIN_MAX_FAILURES_PER_USERNAME", DEFAULT_MAX_FAILURES_PER_USERNAME
            ),
            max_failures_per_ip=number(
                "PTS_LOGIN_MAX_FAILURES_PER_IP", DEFAULT_MAX_FAILURES_PER_IP
            ),
        )


@dataclass(frozen=True)
class LockoutStatus:
    locked: bool
    retry_after_seconds: int = 0
    reason: str = ""

    def message(self) -> str:
        if not self.locked:
            return ""
        minutes = max(1, (self.retry_after_seconds + 59) // 60)
        return f"登录尝试过于频繁，请在约 {minutes} 分钟后重试"


def to_epoch(value: str | None) -> int:
    """Parse the timestamps this system stores into an epoch second.

    The application writes ISO 8601 with an offset (``NOW_PROVIDER``), so
    ``fromisoformat`` handles it. A naive string is assumed to be UTC rather than
    rejected: a malformed row should not take the login endpoint down.
    """
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def normalize_username(value: object) -> str:
    """Usernames are compared case-insensitively everywhere else; match that."""
    return str(value or "").strip().lower()


def _count_failures(
    connection: sqlite3.Connection, *, scope: str, subject: str, since_epoch: int
) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM login_attempts "
        "WHERE scope = ? AND subject = ? AND succeeded = 0 AND attempted_epoch >= ?",
        (scope, subject, since_epoch),
    ).fetchone()
    return int(row[0])


def _oldest_failure_epoch(
    connection: sqlite3.Connection, *, scope: str, subject: str, since_epoch: int
) -> int:
    row = connection.execute(
        "SELECT MIN(attempted_epoch) FROM login_attempts "
        "WHERE scope = ? AND subject = ? AND succeeded = 0 AND attempted_epoch >= ?",
        (scope, subject, since_epoch),
    ).fetchone()
    return int(row[0] or 0)


def check_lockout(
    connection: sqlite3.Connection,
    *,
    username: object,
    ip: str,
    now_epoch: int,
    policy: LoginPolicy | None = None,
) -> LockoutStatus:
    """Decide whether this login attempt should be refused before verification."""
    active = policy or LoginPolicy()
    window_start = now_epoch - active.window_seconds
    subject = normalize_username(username)

    checks = (
        (SCOPE_USERNAME, subject, active.max_failures_per_username, "账号"),
        (SCOPE_IP, str(ip or ""), active.max_failures_per_ip, "来源地址"),
    )
    for scope, value, threshold, label in checks:
        if not value:
            continue
        if _count_failures(connection, scope=scope, subject=value, since_epoch=window_start) < threshold:
            continue
        # Anchor the lockout to the oldest failure in the window so a sustained
        # attacker cannot extend it indefinitely.
        oldest = _oldest_failure_epoch(
            connection, scope=scope, subject=value, since_epoch=window_start
        )
        retry_after = oldest + active.lockout_seconds - now_epoch
        if retry_after > 0:
            return LockoutStatus(locked=True, retry_after_seconds=retry_after, reason=label)
    return LockoutStatus(locked=False)


def record_attempt(
    connection: sqlite3.Connection,
    *,
    username: object,
    ip: str,
    succeeded: bool,
    occurred_at: str,
    now_epoch: int,
) -> None:
    """Record one attempt under both scopes.

    A success is recorded too, so the audit trail shows the recovery rather than
    only the failures.
    """
    subject = normalize_username(username)
    rows = []
    if subject:
        rows.append((SCOPE_USERNAME, subject))
    if ip:
        rows.append((SCOPE_IP, str(ip)))
    if not rows:
        return
    connection.executemany(
        "INSERT INTO login_attempts(scope, subject, succeeded, attempted_at, "
        "attempted_epoch, username, ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (scope, value, 1 if succeeded else 0, occurred_at, now_epoch, subject, str(ip or ""))
            for scope, value in rows
        ],
    )


def clear_username_failures(connection: sqlite3.Connection, *, username: object) -> int:
    """On success, forget this account's failures.

    Deliberately does **not** clear the IP counter: an attacker holding one valid
    low-privilege account could otherwise use it to reset the per-IP budget and
    keep guessing other accounts from the same workstation. The IP window ages
    out on its own.
    """
    cursor = connection.execute(
        "DELETE FROM login_attempts WHERE scope = ? AND subject = ? AND succeeded = 0",
        (SCOPE_USERNAME, normalize_username(username)),
    )
    return cursor.rowcount


def unlock(connection: sqlite3.Connection, *, username: object = "", ip: str = "") -> int:
    """Operator escape hatch: clear the failure budget for a subject.

    Exists because a per-username lockout can be abused to keep a real operator
    out mid-shift; see the module docstring.
    """
    removed = 0
    if username:
        cursor = connection.execute(
            "DELETE FROM login_attempts WHERE scope = ? AND subject = ?",
            (SCOPE_USERNAME, normalize_username(username)),
        )
        removed += cursor.rowcount
    if ip:
        cursor = connection.execute(
            "DELETE FROM login_attempts WHERE scope = ? AND subject = ?", (SCOPE_IP, str(ip))
        )
        removed += cursor.rowcount
    return removed


def prune_attempts(
    connection: sqlite3.Connection, *, now_epoch: int, policy: LoginPolicy | None = None
) -> int:
    """Drop attempts older than the window. Keeps the table bounded."""
    active = policy or LoginPolicy()
    cursor = connection.execute(
        "DELETE FROM login_attempts WHERE attempted_epoch < ?",
        (now_epoch - active.window_seconds,),
    )
    return cursor.rowcount


def recent_failures(
    connection: sqlite3.Connection, *, policy: LoginPolicy | None = None, now_epoch: int = 0
) -> list[dict]:
    """Current failure budgets, for ``manage.py login-status``."""
    active = policy or LoginPolicy()
    since = now_epoch - active.window_seconds
    rows = connection.execute(
        "SELECT scope, subject, COUNT(*) AS failures, MAX(attempted_at) AS last_at "
        "FROM login_attempts WHERE succeeded = 0 AND attempted_epoch >= ? "
        "GROUP BY scope, subject ORDER BY failures DESC, last_at DESC",
        (since,),
    ).fetchall()
    return [
        {
            "scope": str(row["scope"]),
            "subject": str(row["subject"]),
            "failures": int(row["failures"]),
            "lastAt": str(row["last_at"]),
        }
        for row in rows
    ]
