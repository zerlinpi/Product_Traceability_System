"""The WAL shared-memory holder, and the race it exists to stop.

The holder is not a performance feature, it is a correctness one: without it, the
first two requests to open after the server has been idle race to create SQLite's
``-shm`` file, and on Windows the loser gets a connection whose every subsequent
write fails with ``attempt to write a readonly database``. Nothing in the code can
detect or repair that connection — the only remedy is to close and reopen it, which
is why the file is kept in existence instead.

These tests assert the mechanism rather than the symptom. The symptom needs two
connections to open within about five milliseconds of each other on an idle server,
which is reproducible on the machine the bug was found on and a matter of luck
elsewhere; the mechanism — the file survives, no transaction is held, one handle
per database — is true everywhere or the fix is not there.

Feature: batch-traceability, Property 77: WAL 共享内存句柄
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from traceability.db import (
    _WAL_HOLDERS,
    connect_database,
    hold_wal_shared_memory,
    release_wal_holders,
)


@pytest.fixture()
def database(tmp_path):
    """A WAL database with no connections open, which is the dangerous state."""
    path = tmp_path / "traceability.db"
    # connect_database is autocommit (isolation_level=None), so there is no
    # BEGIN to COMMIT — each statement lands on its own.
    connection = connect_database(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("INSERT INTO items (name) VALUES ('seeded')")
    connection.close()
    # The -shm is gone, exactly as it is between two requests on an idle server.
    assert not (tmp_path / "traceability.db-shm").exists()
    return path


# --------------------------------------------------------------------------
# The mechanism
# --------------------------------------------------------------------------


def test_the_holder_keeps_the_shared_memory_file_alive(database, tmp_path):
    """The whole fix in one assertion: the file outlives the last real connection.

    Delete the holder and this fails immediately, on any machine, because closing
    the writer below is what removes the file.
    """
    hold_wal_shared_memory(database)

    connection = connect_database(database)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO items (name) VALUES ('written')")
    connection.execute("COMMIT")
    connection.close()

    assert (tmp_path / "traceability.db-shm").exists(), (
        "the -shm was deleted when the last connection closed, so the next "
        "connection has to create it and two simultaneous openers will race"
    )


def test_the_holder_holds_no_transaction(database):
    """An idle holder must not pin a read snapshot, or checkpointing stops.

    A connection that kept a read transaction open would freeze the WAL at that
    frame forever: every later commit would append and nothing would ever be
    checkpointed, so the log would grow without bound on a server that runs for
    months.
    """
    hold_wal_shared_memory(database)
    holder = next(iter(_WAL_HOLDERS.values()))

    assert holder.in_transaction is False


def test_writes_still_reach_the_held_file(database):
    """The holder is an extra connection, not an exclusive one."""
    hold_wal_shared_memory(database)

    connection = connect_database(database)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO items (name) VALUES ('after-hold')")
    connection.execute("COMMIT")
    connection.close()

    reader = connect_database(database)
    names = [row["name"] for row in reader.execute("SELECT name FROM items")]
    reader.close()
    assert "after-hold" in names


# --------------------------------------------------------------------------
# Bookkeeping
# --------------------------------------------------------------------------


def test_acquiring_twice_keeps_one_handle(database):
    """Two acquisitions would double the handles for one database."""
    hold_wal_shared_memory(database)
    first = next(iter(_WAL_HOLDERS.values()))

    hold_wal_shared_memory(database)
    hold_wal_shared_memory(str(database))

    assert len(_WAL_HOLDERS) == 1
    assert next(iter(_WAL_HOLDERS.values())) is first


def test_release_closes_the_handle_and_forgets_it(database):
    release_wal_holders()  # anything an earlier test left behind
    hold_wal_shared_memory(database)
    holder = next(iter(_WAL_HOLDERS.values()))

    release_wal_holders()

    assert _WAL_HOLDERS == {}
    with pytest.raises(sqlite3.ProgrammingError):
        holder.execute("SELECT 1")


def test_release_is_safe_with_nothing_held():
    release_wal_holders()  # must not raise on an empty registry
    assert _WAL_HOLDERS == {}


# --------------------------------------------------------------------------
# The race itself
# --------------------------------------------------------------------------


def _open_write_read(path: Path, failures: list[str]) -> None:
    """What a request does: open a connection, read, write, close.

    Exceptions are appended to ``failures`` rather than raised, because a thread
    that dies silently would let the test pass on a broken holder.
    """
    try:
        connection = connect_database(path)
        try:
            connection.execute("SELECT COUNT(*) FROM items").fetchone()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO items (name) VALUES ('concurrent')")
            connection.execute("COMMIT")
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001 — the point is to record what broke
        failures.append(f"{type(error).__name__}: {error}")


def test_simultaneous_openers_do_not_degrade(database):
    """The end-to-end property: a request that opens alongside another still works.

    Each round recreates the dangerous conditions — no other connection is open,
    so the ``-shm`` would have to be created from scratch — and then has two
    threads open at the same moment. With the holder in place both succeed.

    Remove the holder and the second thread gets ``attempt to write a readonly
    database``, which surfaces in the API as a 500. Whether that reproduces on a
    given machine depends on thread-start timing, so this test alone is not the
    guard — ``test_the_holder_keeps_the_shared_memory_file_alive`` is, because it
    asserts the file survives without needing a race to happen at all.
    """
    hold_wal_shared_memory(database)
    rounds = 20

    for _ in range(rounds):
        failures: list[str] = []
        threads = [
            threading.Thread(target=_open_write_read, args=(database, failures))
            for _ in range(2)
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not failures, f"a simultaneous opener was left unable to write: {failures}"
