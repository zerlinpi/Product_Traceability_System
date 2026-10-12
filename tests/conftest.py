"""Shared fixtures for the backend suite.

Only one thing genuinely belongs here. Every test file already puts the repository
root on ``sys.path`` itself before importing the application — that has to stay
per-file, because the modules are imported as top-level names under pytest's
default import mode and a conftest that imported them first would change the order
in which ``sys.path`` is set up for the suite.

The fixture below is the lifecycle counterpart to ``traceability.db`` holding one
idle connection open per database file (``hold_wal_shared_memory``). That holder
exists to stop a WAL database's shared-memory file from being deleted between
requests, which is what makes two requests that open at the same moment race to
create it — and on Windows the loser of that race gets a connection whose writes
fail with ``attempt to write a readonly database`` for the rest of its life.

In production the server holds the connection until the process exits. Here the
suite builds a fresh app, and therefore a fresh database file, for each test;
without this fixture it would leave one open SQLite handle behind per test and
never let the temporary directory be removed. Releasing after each test keeps the
behaviour identical to a long-lived server within the test, and leaves nothing
behind after it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from traceability.db import release_wal_holders  # noqa: E402


@pytest.fixture(autouse=True)
def _release_database_holders():
    """Close the WAL holder this test's app opened, and any it left behind."""
    yield
    release_wal_holders()
