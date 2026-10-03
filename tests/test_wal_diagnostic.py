"""The WAL diagnostic must work on any host, including one where WAL is broken.

`tools/diagnose_wal_readonly.py` exists to answer one question on a machine that
is misbehaving: is this the product, or the host? It answers it by running three
controlled sqlite3 scenarios and reading the pattern.

That makes the tool's own correctness load-bearing in a subtle way — it will most
often be run on a machine where something is already wrong, so it has to produce a
verdict there rather than crashing or hanging. These tests assert exactly that,
and deliberately do not assert which verdict this host produces: pinning "WAL
works" would fail on the very machines the tool is for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import diagnose_wal_readonly as diagnostic  # noqa: E402


def test_contend_returns_a_count_within_range(tmp_path):
    """It must count failures, not raise, whatever the host does."""
    path = diagnostic._prepare(tmp_path, "delete")

    failures = diagnostic._contend(path)

    assert 0 <= failures <= diagnostic.WORKERS


def test_scenario_reports_rounds_and_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostic, "ROUNDS", 2)

    bad_rounds, failed_connections = diagnostic.scenario("delete", hold_open=False)

    assert 0 <= bad_rounds <= 2
    assert 0 <= failed_connections <= 2 * diagnostic.WORKERS


def test_a_failure_round_is_counted_once_even_with_several_bad_connections(tmp_path, monkeypatch):
    """One bad round with three failed connections is one bad round.

    The distinction matters for reading the verdict: "3/6 rounds failed" and
    "3 connections failed" are different claims.
    """
    monkeypatch.setattr(diagnostic, "ROUNDS", 1)

    def always_fails(_path):
        return diagnostic.WORKERS

    monkeypatch.setattr(diagnostic, "_contend", always_fails)

    bad_rounds, failed_connections = diagnostic.scenario("delete", hold_open=False)

    assert bad_rounds == 1
    assert failed_connections == diagnostic.WORKERS


def test_prepare_creates_a_usable_database(tmp_path):
    """Both modes must produce a database the contention step can open."""
    import sqlite3

    for mode in ("wal", "delete"):
        directory = tmp_path / mode
        directory.mkdir()
        path = diagnostic._prepare(directory, mode)
        assert path.is_file(), mode

        connection = sqlite3.connect(str(path))
        try:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == mode
        finally:
            connection.close()


@pytest.mark.parametrize("verdict", [0, 1])
def test_main_returns_a_verdict_without_raising(verdict, monkeypatch, capsys):
    """The tool must terminate with a status and a readable report.

    Run once per possible verdict rather than depending on what this host does.
    """
    monkeypatch.setattr(diagnostic, "ROUNDS", 1)
    monkeypatch.setattr(
        diagnostic,
        "scenario",
        lambda *args, **kwargs: (verdict, diagnostic.WORKERS if verdict else 0),
    )

    status = diagnostic.main()

    output = capsys.readouterr().out
    assert status == verdict
    assert "判定：" in output, f"缺少判定行: {output!r}"
    if verdict == 1:
        assert "不是产品缺陷" in output, "本机问题必须明确说不是产品缺陷"
        assert "docs/OPERATIONS.md" in output, "应指向处置文档"


def test_main_always_prints_the_scenario_table(monkeypatch, capsys):
    monkeypatch.setattr(diagnostic, "ROUNDS", 1)
    monkeypatch.setattr(diagnostic, "scenario", lambda *a, **k: (0, 0))

    diagnostic.main()

    output = capsys.readouterr().out
    for label in ("WAL", "journal_mode=delete"):
        assert label in output, f"报告缺少对照项: {label}"
    assert "不加载本仓库任何代码" in output, "应说明结论不依赖产品代码"
