"""Upgrade a database from an older release against a *copy*, and check it works.

    python tools/verify_upgrade.py [--database data/traceability.db]

Why a copy, and why this exists
-------------------------------
Upgrading is the highest-risk thing an operator does to this system: migrations
rewrite tables in place, and the deployment database is usually several versions
behind the code. The migrations are covered by tests, but those start from a
synthetic v11 — none of them tell you that *this* database, holding *this* data,
comes out the other side serving requests.

So: copy the file, migrate the copy, exercise what is most likely to be wrong
afterwards. The original is never opened for writing, and the whole copy is one
directory that can be deleted afterwards.

What is checked
---------------
- the version reaches the current one, and integrity_check is clean
- the list endpoints answer with their window headers and honour limit / search
- every product carries ``productionBatchCount``
- the idempotency purge runs at startup: a stale completed key goes, a fresh one
  stays — a purge that ate fresh keys would break retries, which is the entire
  point of the mechanism

Exit code is 0 only when every check passed.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from traceability.db import connect_database  # noqa: E402
from traceability.idempotency import claim, complete, lookup  # noqa: E402

#: A fixed clock, so the retention window behaves the same on every run.
NOW = "2026-10-17T13:00:00+08:00"

#: The schema version the code ships. Kept beside the check so the two cannot
#: drift apart silently.
CURRENT_VERSION = 24

#: Row counts per table, read from the original before anything touches it. A
#: migration that widened a CHECK but dropped rows would otherwise pass every
#: check here, and the empty tables in a sparse deployment database make that
#: easy to miss.
#: Tables the v23/v24 migrations rebuild, and what makes each worth checking.
REBUILT_TABLES = ("purchase_orders", "inbound_receipts", "product_stock_sync")


def _app_for(path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(path),
            "AUTH_DISABLED": True,
            "NOW_PROVIDER": lambda: NOW,
        }
    )


class Results:
    """Collects checks so the run ends with one honest summary."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.rows.append((label, ok, detail))
        print(f"  {'✓' if ok else '✗'} {label}" + (f"  — {detail}" if detail else ""))

    def ok(self) -> bool:
        return all(ok for _label, ok, _detail in self.rows)


def run_checks(dst: Path, before: int, before_rows: dict[str, int]) -> Results:
    """Migrate the copy and check it, having read its *original* version first.

    Two ordering traps, both hit while writing this:

    - ``connect_database`` runs the schema script but not the migrations, and it
      *creates* a database when the path has none. So calling it on the copy
      before the app would overwrite the operator's data with an empty schema —
      and reading the version that way reports what the file already had, after
      which ``create_app`` finds nothing left to migrate. The copy is therefore
      touched only by the app, and the original version is read with plain
      sqlite3 in ``main`` and passed in here.
    """
    results = Results()

    # Starting the app is the upgrade: create_app runs initialize_database, which
    # is what applies the migrations this tool exists to verify.
    client = _app_for(dst).test_client()

    probe = connect_database(str(dst))
    version = probe.execute("PRAGMA user_version").fetchone()[0]
    integrity = probe.execute("PRAGMA integrity_check").fetchone()[0]
    results.check("升级到当前版本", version == CURRENT_VERSION, f"{before} → {version}")
    results.check("integrity_check 干净", integrity == "ok")

    # The migrations rebuild tables to widen a CHECK constraint. Assert the
    # constraint actually widened and the rows came with it — without this the
    # tool passes even when the rebuild is skipped, which is exactly what an
    # ablation of _rebuild_product_stock_sync_v24 showed: 7/7 on a database whose
    # CHECK was still three-valued, because nothing here looked at a CHECK.
    #
    # Read before the connection closes, and while it is still the migrated one.
    for table in REBUILT_TABLES:
        row = probe.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        widened = bool(row) and "NEEDS_REVIEW" in row[0]
        results.check(
            f"{table} 的 CHECK 已放宽为四值",
            widened,
            "" if widened else "仍为三值——重建没有发生",
        )
        if before_rows.get(table):
            after = probe.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            results.check(
                f"{table} 的 {before_rows[table]} 行数据随之保留",
                after == before_rows[table],
                f"升级前 {before_rows[table]} 行，升级后 {after} 行",
            )
    probe.close()

    client = _app_for(dst).test_client()

    listing = client.get("/api/production-batches")
    results.check(
        "批次端点带窗口响应头",
        listing.headers.get("X-Total-Count") is not None
        and listing.headers.get("X-List-Limit") == "500",
        f"total={listing.headers.get('X-Total-Count')} limit={listing.headers.get('X-List-Limit')}",
    )
    paged = client.get("/api/production-batches?limit=1").get_json()["data"]
    results.check("limit 参数生效", len(paged) <= 1)

    miss = client.get("/api/production-batches?search=nothing-matches-this")
    results.check(
        "搜索参数可用且不误伤",
        miss.status_code == 200 and miss.get_json()["data"] == [],
        f"total={miss.headers.get('X-Total-Count')}",
    )

    products = client.get("/api/products").get_json()["data"]
    results.check(
        "每个产品都带 productionBatchCount",
        all("productionBatchCount" in row for row in products) if products else True,
        f"{len(products)} 个产品",
    )

    # Seed one stale and one fresh completed key, then start again: the purge that
    # runs at startup has to separate them.
    connection = connect_database(str(dst))
    for key, when in (
        ("stale-on-real-db", "2020-01-01T00:00:00+08:00"),
        ("fresh-on-real-db", NOW),
    ):
        claim(
            connection,
            key=key,
            scope="verify-upgrade",
            request_fingerprint="x",
            actor_user_id=None,
            started_at=when,
        )
        complete(
            connection,
            key=key,
            scope="verify-upgrade",
            status_code=200,
            body={"ok": True, "data": None},
            completed_at=when,
        )
    connection.close()

    _app_for(dst)

    connection = connect_database(str(dst))
    stale_gone = lookup(connection, "stale-on-real-db", "verify-upgrade") is None
    fresh_kept = lookup(connection, "fresh-on-real-db", "verify-upgrade") is not None
    connection.close()
    results.check("过期幂等键被清、新键保留", stale_gone and fresh_kept)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default="data/traceability.db",
        help="要升级的库（只读取它；所有改动都发生在副本上）",
    )
    args = parser.parse_args()
    source = Path(args.database)
    if not source.exists():
        print(f"找不到数据库：{source}", file=sys.stderr)
        return 1

    work = Path(tempfile.mkdtemp(prefix="verify-upgrade-"))
    dst = work / source.name
    shutil.copy2(source, dst)

    # Opened through sqlite3 directly, and read-only: connect_database() creates
    # and migrates, which is the one thing this must never do to the operator's
    # live database.
    import sqlite3

    original = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    before = original.execute("PRAGMA user_version").fetchone()[0]
    before_rows = {
        table: original.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in REBUILT_TABLES
    }
    original.close()
    print(f"源库：{source}")
    print(f"  升级前 user_version = {before}")
    print(f"  迁移目标表现有行数：{before_rows}")
    print(f"副本：{dst}\n")

    results = run_checks(dst, before, before_rows)

    passed = sum(1 for _label, ok, _detail in results.rows if ok)
    print(f"\n{'=' * 46}")
    print(f"结果：{passed}/{len(results.rows)} 通过")
    print(f"原库未被修改；副本可整个目录删除：{work}")
    return 0 if results.ok() else 1


if __name__ == "__main__":
    raise SystemExit(main())
