"""The test-count guard.

A gate that has never been tested is a liability: if it silently passes, it
provides the illusion of protection. These tests exercise the guard itself —
that it reads its baseline, that a shrunken suite fails, and that growing the
suite is allowed.

Feature: batch-traceability, Property 75: CI 测试数量门禁
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import check_test_count  # noqa: E402


@pytest.fixture()
def guard(tmp_path, monkeypatch):
    """Point the guard at a scratch baseline file."""
    baseline = tmp_path / "BASELINE_COUNT"
    monkeypatch.setattr(check_test_count, "BASELINE", baseline)
    return baseline


def test_the_real_baseline_exists_and_parses():
    """The committed baseline must be readable, or CI fails on every run."""
    assert check_test_count.read_baseline() > 0


def test_collect_count_reports_a_plausible_number():
    """The count is parsed out of pytest's summary line, not hardcoded."""
    count = check_test_count.collect_count()
    assert count > 100, f"only {count} tests collected — is the parser broken?"


def test_the_baseline_ignores_comments_and_blank_lines(guard):
    guard.write_text("# a comment\n\n  819  \n# trailing\n", encoding="utf-8")
    assert check_test_count.read_baseline() == 819


def test_a_missing_baseline_is_an_error(guard):
    with pytest.raises(SystemExit) as error:
        check_test_count.read_baseline()
    assert error.value.code == 2


def test_update_writes_the_current_count(guard, monkeypatch):
    monkeypatch.setattr(check_test_count, "collect_count", lambda: 1234)
    assert check_test_count.main.__module__  # module is importable
    monkeypatch.setattr(sys, "argv", ["check_test_count.py", "--update"])
    assert check_test_count.main() == 0
    assert check_test_count.read_baseline() == 1234
    assert "1234" in guard.read_text(encoding="utf-8")


def test_a_shrunken_suite_fails(guard, monkeypatch, capsys):
    guard.write_text("500\n", encoding="utf-8")
    monkeypatch.setattr(check_test_count, "collect_count", lambda: 499)
    monkeypatch.setattr(sys, "argv", ["check_test_count.py"])
    assert check_test_count.main() == 1
    assert "shrank" in capsys.readouterr().err


def test_an_unchanged_suite_passes(guard, monkeypatch, capsys):
    guard.write_text("500\n", encoding="utf-8")
    monkeypatch.setattr(check_test_count, "collect_count", lambda: 500)
    monkeypatch.setattr(sys, "argv", ["check_test_count.py"])
    assert check_test_count.main() == 0
    assert "ok" in capsys.readouterr().out


def test_a_grown_suite_passes_and_nudges(guard, monkeypatch, capsys):
    guard.write_text("500\n", encoding="utf-8")
    monkeypatch.setattr(check_test_count, "collect_count", lambda: 501)
    monkeypatch.setattr(sys, "argv", ["check_test_count.py"])
    assert check_test_count.main() == 0
    assert "--update" in capsys.readouterr().out


def test_the_failure_message_explains_what_to_do(guard, monkeypatch, capsys):
    """The message matters: it is what a developer sees at 2am."""
    guard.write_text("500\n", encoding="utf-8")
    monkeypatch.setattr(check_test_count, "collect_count", lambda: 400)
    monkeypatch.setattr(sys, "argv", ["check_test_count.py"])
    check_test_count.main()
    message = capsys.readouterr().err
    assert "shrank" in message
    assert "baseline" in message


def test_an_unparseable_pytest_output_is_an_error(monkeypatch, capsys):
    """If pytest's summary format changes, fail loudly rather than pass."""
    import subprocess

    class Result:
        stdout = "no summary line here"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Result())
    with pytest.raises(SystemExit) as error:
        check_test_count.collect_count()
    assert error.value.code == 2
    assert "could not determine" in capsys.readouterr().out
