"""The bundle fingerprint must be able to tell "rebuilt" from "forgotten".

The check exists because the committed front-end bundle can silently fall behind
its source: the Python suite asserts against whatever ``static/dist`` contains,
so nothing notices that the shipped UI is older than the code. A fingerprint that
did not change when a source file changed would be worse than no check at all —
it would report a stale bundle as current.

These tests therefore concentrate on the negative cases: change something and the
fingerprint must move; change only something irrelevant and it must not.

The real repository is checked at the bottom, because that is what CI runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import check_frontend_build as checker  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """A miniature repository with one source file inside the watched roots."""
    source = tmp_path / "frontend" / "apps" / "web" / "src"
    source.mkdir(parents=True)
    (source / "App.vue").write_text("<template><div /></template>\n", encoding="utf-8")
    (source / "main.ts").write_text("export const x = 1\n", encoding="utf-8")

    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(
        checker,
        "SOURCE_ROOTS",
        ("frontend/apps/web/src",),
    )
    return tmp_path


# ---------------------------------------------------------------------------
# The fingerprint has to move when the source moves
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_for_unchanged_source(fake_repo):
    assert checker.source_fingerprint() == checker.source_fingerprint()


def test_fingerprint_changes_when_a_source_file_changes(fake_repo):
    """The exact scenario the check exists for."""
    before = checker.source_fingerprint()
    (fake_repo / "frontend/apps/web/src/App.vue").write_text(
        "<template><div>changed</div></template>\n", encoding="utf-8"
    )
    assert checker.source_fingerprint() != before


def test_fingerprint_changes_when_a_source_file_is_added(fake_repo):
    before = checker.source_fingerprint()
    (fake_repo / "frontend/apps/web/src/extra.ts").write_text("export {}\n", encoding="utf-8")
    assert checker.source_fingerprint() != before


def test_fingerprint_changes_when_a_source_file_is_removed(fake_repo):
    before = checker.source_fingerprint()
    (fake_repo / "frontend/apps/web/src/main.ts").unlink()
    assert checker.source_fingerprint() != before


def test_fingerprint_ignores_files_outside_the_watched_roots(fake_repo):
    """A README in the workspace must not demand a rebuild."""
    before = checker.source_fingerprint()
    (fake_repo / "frontend/apps/web/README.md").write_text("docs\n", encoding="utf-8")
    (fake_repo / "frontend").mkdir(exist_ok=True)
    (fake_repo / "frontend/notes.txt").write_text("notes\n", encoding="utf-8")
    assert checker.source_fingerprint() == before


def test_fingerprint_ignores_line_endings(fake_repo):
    """git checks out CRLF on Windows and LF elsewhere; identical sources must
    fingerprint identically or the check would fail on one platform only."""
    path = fake_repo / "frontend/apps/web/src/App.vue"
    path.write_bytes(b"<template>\n<div />\n</template>\n")
    lf = checker.source_fingerprint()
    path.write_bytes(b"<template>\r\n<div />\r\n</template>\r\n")
    assert checker.source_fingerprint() == lf


def test_fingerprint_ignores_installed_dependencies(fake_repo):
    """node_modules is not an input; touching it must not move the fingerprint."""
    before = checker.source_fingerprint()
    installed = fake_repo / "frontend/apps/web/src/node_modules" / "pkg"
    installed.mkdir(parents=True)
    (installed / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    assert checker.source_fingerprint() == before


def test_fingerprint_ignores_path_order(fake_repo):
    """Two files, added in either order, must produce the same digest."""
    first = checker.source_fingerprint()
    (fake_repo / "frontend/apps/web/src/zzz.ts").write_text("export const z = 1\n", encoding="utf-8")
    (fake_repo / "frontend/apps/web/src/aaa.ts").write_text("export const a = 1\n", encoding="utf-8")
    second = checker.source_fingerprint()
    assert first != second

    # Removing and re-adding in the other order returns to the same digest.
    (fake_repo / "frontend/apps/web/src/zzz.ts").unlink()
    (fake_repo / "frontend/apps/web/src/aaa.ts").unlink()
    (fake_repo / "frontend/apps/web/src/aaa.ts").write_text("export const a = 1\n", encoding="utf-8")
    (fake_repo / "frontend/apps/web/src/zzz.ts").write_text("export const z = 1\n", encoding="utf-8")
    assert checker.source_fingerprint() == second


def test_source_order_is_case_sensitive_and_separator_free(fake_repo):
    """The ordering must not depend on the platform.

    This is not hypothetical: sorting the Path objects made the fingerprint differ
    between a Windows checkout and CI, and the check went red on a tree that was
    in fact consistent. ``PureWindowsPath`` compares case-insensitively, so
    ``api/index.ts`` sorted before ``App.vue`` on Windows and after it on Linux;
    and the absolute prefix carries the native separator, which orders differently
    too. Both are avoided by sorting the relative POSIX form.
    """
    source = fake_repo / "frontend/apps/web/src"
    (source / "api").mkdir()
    (source / "api" / "index.ts").write_text("export {}\n", encoding="utf-8")
    (source / "App.vue").write_text("<template />\n", encoding="utf-8")

    order = [path.relative_to(fake_repo).as_posix() for path in checker._iter_sources()]
    assert order.index("frontend/apps/web/src/App.vue") < order.index(
        "frontend/apps/web/src/api/index.ts"
    ), f"大写字母必须排在小写之前（区分大小写）: {order}"
    assert order == sorted(order), "顺序必须是稳定的字典序"


def test_source_order_does_not_depend_on_the_root_prefix(tmp_path, monkeypatch):
    """The same tree under two different roots must fingerprint identically."""
    def build(root: Path) -> None:
        source = root / "frontend" / "apps" / "web" / "src"
        source.mkdir(parents=True)
        (source / "App.vue").write_text("<template />\n", encoding="utf-8")
        (source / "api").mkdir()
        (source / "api" / "index.ts").write_text("export {}\n", encoding="utf-8")

    first = tmp_path / "a-short"
    second = tmp_path / "b-much-longer-directory-name"
    build(first)
    build(second)

    monkeypatch.setattr(checker, "SOURCE_ROOTS", ("frontend/apps/web/src",))
    monkeypatch.setattr(checker, "ROOT", first)
    first_hash = checker.source_fingerprint()
    monkeypatch.setattr(checker, "ROOT", second)
    assert checker.source_fingerprint() == first_hash


# ---------------------------------------------------------------------------
# The command's exit status, which is what CI reads
# ---------------------------------------------------------------------------


def _fake_bundle(tmp_path: Path, monkeypatch, *, entry: bool = True, info: dict | None = None):
    bundle = tmp_path / "static" / "dist"
    bundle.mkdir(parents=True)
    if entry:
        (bundle / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    if info is not None:
        (bundle / "build-info.json").write_text(json.dumps(info), encoding="utf-8")
    monkeypatch.setattr(checker, "BUNDLE", bundle)
    monkeypatch.setattr(checker, "ENTRY", bundle / "index.html")
    monkeypatch.setattr(checker, "BUILD_INFO", bundle / "build-info.json")


def test_check_passes_when_the_recorded_fingerprint_matches(fake_repo, monkeypatch):
    _fake_bundle(fake_repo, monkeypatch, info={"sourceFingerprint": checker.source_fingerprint()})
    assert checker.main() == 0


def test_check_fails_when_the_source_moved_on(fake_repo, monkeypatch):
    """The whole point: source changed, bundle not rebuilt."""
    _fake_bundle(fake_repo, monkeypatch, info={"sourceFingerprint": "stale-value"})
    assert checker.main() == 1


def test_check_fails_when_the_bundle_has_no_fingerprint(fake_repo, monkeypatch):
    """No recorded fingerprint means it cannot be trusted as current."""
    _fake_bundle(fake_repo, monkeypatch, info=None)
    assert checker.main() == 1


def test_check_fails_when_the_bundle_was_never_built(fake_repo, monkeypatch):
    _fake_bundle(fake_repo, monkeypatch, entry=False, info={"sourceFingerprint": "x"})
    assert checker.main() == 1


def test_write_build_info_records_the_current_fingerprint(fake_repo, monkeypatch):
    _fake_bundle(fake_repo, monkeypatch, info=None)
    written = checker.write_build_info()
    assert written == checker.source_fingerprint()
    assert json.loads(checker.BUILD_INFO.read_text(encoding="utf-8"))[
        "sourceFingerprint"
    ] == written


# ---------------------------------------------------------------------------
# The real repository — this is the part that ships
# ---------------------------------------------------------------------------


def test_the_committed_bundle_matches_the_repository_source():
    assert checker.main() == 0, (
        "已提交的 static/dist 不是由当前源码构建的；"
        "运行 python tools/build_frontend.py 并提交 static/dist"
    )


def test_the_watched_roots_exist():
    """A typo in SOURCE_ROOTS would make the fingerprint cover nothing, and an
    empty fingerprint matches an empty fingerprint — a check that cannot fail."""
    missing = [entry for entry in checker.SOURCE_ROOTS if not (checker.ROOT / entry).exists()]
    assert not missing, f"SOURCE_ROOTS 指向不存在的路径: {missing}"


def test_the_fingerprint_actually_covers_files():
    assert len(checker._iter_sources()) > 100, "指纹覆盖的源文件过少，检查会失效"
