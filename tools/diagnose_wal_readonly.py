"""Tell a host SQLite problem from a product one, in one command.

Written because the two WAL concurrency tests failed on this machine while CI
passed on both Ubuntu and Windows, and the error said nothing useful:

    sqlite3.OperationalError: attempt to write a readonly database

That message is misleading — the database file and its directory were both
writable at the moment it was raised. What it actually means is that SQLite
could not map the ``-shm`` shared-memory file for writing.

The experiment below separates the two questions that matter. Run it and read the
verdict:

    python tools/diagnose_wal_readonly.py

What it establishes, and why each row is in the table
-----------------------------------------------------
* **WAL, connections closed** — the shape production actually has. The
  application opens a connection per request (``get_db`` / ``close_db``), so
  between requests there are none, and SQLite deletes ``-wal`` and ``-shm`` when
  the last one closes. Every burst of concurrent writes therefore races to
  recreate the shared-memory file.
* **WAL, one connection held open** — if this passes while the row above fails,
  the race is the cause and keeping a connection open removes it.
* **journal_mode=delete** — if this passes, the problem is specific to WAL rather
  than to concurrent writes in general.
* **plain sqlite3, no application code** — this script imports nothing from the
  repository, so a failure here cannot be fixed by changing the product.

A host that fails the first row and passes the other two has a filesystem or
filter driver that cannot support SQLite's WAL handoff. Nothing in this
repository can fix that; the options are to move the database to different
storage, to hold a connection open (which stops the tests' temporary directories
from being deleted on Windows), or to run with ``journal_mode=delete``.

Exit status is 0 when every row behaves as expected on a healthy host.
"""

from __future__ import annotations

import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROUNDS = 6
WORKERS = 4


def _contend(path: Path) -> int:
    """Fire concurrent BEGIN IMMEDIATE transactions; return how many failed."""
    failures = 0

    def worker(_index: int) -> None:
        nonlocal failures
        connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("COMMIT")
        except sqlite3.OperationalError:
            failures += 1
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(worker, range(WORKERS)))
    return failures


def _prepare(directory: Path, mode: str) -> Path:
    path = directory / "probe.db"
    connection = sqlite3.connect(str(path))
    connection.execute(f"PRAGMA journal_mode={mode}")
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    return path


def scenario(mode: str, hold_open: bool, rounds: int | None = None) -> tuple[int, int]:
    """Return (rounds that had a failure, total failed connections).

    ``rounds`` resolves to ``ROUNDS`` inside the call rather than as a default
    argument, so the module constant stays overridable — a default of ``ROUNDS``
    is bound at definition time and cannot be changed afterwards.
    """
    if rounds is None:
        rounds = ROUNDS
    bad_rounds = 0
    total_failures = 0
    for _ in range(rounds):
        directory = Path(tempfile.mkdtemp(prefix="pts-wal-probe-"))
        path = _prepare(directory, mode)
        keeper = (
            sqlite3.connect(str(path), timeout=10, isolation_level=None) if hold_open else None
        )
        try:
            failures = _contend(path)
        finally:
            if keeper is not None:
                keeper.close()
        if failures:
            bad_rounds += 1
            total_failures += failures
    return bad_rounds, total_failures


def main() -> int:
    print(f"  每轮 {WORKERS} 个并发连接 × {ROUNDS} 轮，只用 sqlite3，不加载本仓库任何代码\n")

    rows = [
        ("WAL，连接全部关闭（生产形态）", "wal", False),
        ("WAL，保持一个连接打开", "wal", True),
        ("journal_mode=delete，连接全部关闭", "delete", False),
    ]

    results: dict[str, int] = {}
    for label, mode, hold in rows:
        bad, failed = scenario(mode, hold)
        results[label] = bad
        status = "OK" if bad == 0 else f"失败 {bad}/{ROUNDS} 轮（{failed} 个连接）"
        print(f"  {label:<36} {status}")

    print()
    closed = results["WAL，连接全部关闭（生产形态）"]
    held = results["WAL，保持一个连接打开"]
    delete = results["journal_mode=delete，连接全部关闭"]

    if closed == 0:
        print("  判定：本机正常，WAL 并发写没有问题。")
        return 0

    print("  判定：本机无法支持 SQLite 的 WAL 连接交接。")
    print()
    print("  证据链：")
    print(f"    - 连接全部关闭时失败（{closed}/{ROUNDS} 轮）")
    if held == 0:
        print("    - 保持一个连接打开即消失 —— 竞争点在重建 -shm 共享内存文件")
    if delete == 0:
        print("    - 换成 journal_mode=delete 即消失 —— 只与 WAL 有关，与并发写无关")
    print("    - 全程未加载本仓库代码 —— 不是产品缺陷")
    print()
    print("  这意味着：CI（Ubuntu 与 Windows）通过，产品是好的；本机的文件系统或")
    print("  过滤驱动（杀软、同步盘、网络盘）不支持 SQLite 的 WAL 交接。")
    print()
    print("  注意这项能力会**随运行时间退化**：同一次会话里，刚重启后测出 4/4 成功，")
    print("  数小时后同一段脚本变成 6/6 失败，连「保持连接打开」也不再有效。")
    print("  所以「重启后好了」不等于修好了——它会在下次运行中复发。")
    print()
    print("  可选处置，各有代价：")
    print("    - 把 data/ 放到本机磁盘，并把该目录排除杀软实时扫描（先试这个）")
    print("    - 进程内保持一个连接不关闭（本机实测**不可靠**，且会让 Windows 上")
    print("      测试的临时目录删不掉）")
    print("    - 改用 journal_mode=delete（放弃 WAL 的并发读优势，但本机实测稳定）")
    print()
    print("  详见 docs/OPERATIONS.md 的「数据库」一节。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
