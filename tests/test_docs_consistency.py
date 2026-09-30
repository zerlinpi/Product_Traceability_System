"""The docs-consistency checker has to be able to fail.

A checker that only ever prints "passed" is worse than no checker: it turns a
green CI run into a claim nobody verified. Every test here therefore perturbs a
document and asserts the checker notices, rather than only asserting the happy
path on the real repository.

The real repository is checked too, at the bottom — that is what runs in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import check_docs_consistency as checker  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_failures():
    """Each check appends to a module-level list; keep tests independent."""
    checker.failures.clear()
    yield
    checker.failures.clear()


def _patch_read(monkeypatch, overrides: dict[str, str]) -> None:
    """Serve perturbed text for the named files, real text for everything else."""
    original = checker.read

    def fake_read(relative: str) -> str:
        if relative in overrides:
            return overrides[relative]
        return original(relative)

    monkeypatch.setattr(checker, "read", fake_read)


# --------------------------------------------------------------------------
# Schema version
# --------------------------------------------------------------------------


def test_stale_schema_version_in_architecture_doc_is_caught(monkeypatch):
    """This is the drift that actually happened: doc said 21, code said 22."""
    from traceability.db import SCHEMA_VERSION

    stale = (ROOT / "SYSTEM_ARCHITECTURE.md").read_text(encoding="utf-8")
    stale = stale.replace(
        f"`user_version` 当前为 {SCHEMA_VERSION}",
        f"`user_version` 当前为 {SCHEMA_VERSION - 1}",
        1,
    )
    _patch_read(monkeypatch, {"SYSTEM_ARCHITECTURE.md": stale})

    checker.check_schema_version()

    assert checker.failures, "陈旧版本号未被发现"
    assert any("user_version" in item for item in checker.failures)


def test_stale_schema_version_in_data_model_is_caught(monkeypatch):
    from traceability.db import SCHEMA_VERSION

    stale = (ROOT / "docs/DATA_MODEL.md").read_text(encoding="utf-8")
    stale = stale.replace(
        f"`user_version = {SCHEMA_VERSION}`",
        f"`user_version = {SCHEMA_VERSION - 3}`",
        1,
    )
    _patch_read(monkeypatch, {"docs/DATA_MODEL.md": stale})

    checker.check_schema_version()

    assert checker.failures, "DATA_MODEL 的陈旧版本号未被发现"


# --------------------------------------------------------------------------
# Route total
# --------------------------------------------------------------------------


def test_stale_route_total_is_caught(monkeypatch):
    """API_CONTRACT used to split the count by file, which went stale on the
    ninth blueprint."""
    stale = (ROOT / "docs/API_CONTRACT.md").read_text(encoding="utf-8")
    stale = stale.replace("共 **107 个路由**", "共 **100 个路由**", 1)
    assert stale != (ROOT / "docs/API_CONTRACT.md").read_text(encoding="utf-8"), (
        "测试前提失效：API_CONTRACT.md 中的总数表述已改变"
    )
    _patch_read(monkeypatch, {"docs/API_CONTRACT.md": stale})

    checker.check_route_total()

    assert checker.failures, "陈旧路由总数未被发现"
    assert any("个路由" in item for item in checker.failures)


def test_blueprint_missing_from_sources_is_caught(monkeypatch, tmp_path):
    """A blueprint the extractor does not scan vanishes from the matrix.

    The probe lives in a temporary directory rather than in the real
    ``traceability/api/``: writing there leaked a file into the repository when
    the cleanup did not run, and the next test then failed for the wrong reason.
    A test that mutates the tree it is testing is not isolated.
    """
    probe_dir = tmp_path / "api"
    probe_dir.mkdir()
    (probe_dir / "probe.py").write_text('"""probe"""\n', encoding="utf-8")

    monkeypatch.setattr(checker.extract_routes, "BLUEPRINT_DIR", probe_dir)

    checker.check_route_total()

    assert checker.failures, "未被扫描的蓝图未被发现"
    assert any("probe.py" in item for item in checker.failures)


# --------------------------------------------------------------------------
# Roles and Python versions
# --------------------------------------------------------------------------


def test_role_set_drift_is_caught(monkeypatch):
    stale = (ROOT / "SYSTEM_ARCHITECTURE.md").read_text(encoding="utf-8")
    stale = stale.replace(
        "最终角色集合恰好为 `{ADMIN, WAREHOUSE, OPERATIONS}`",
        "最终角色集合恰好为 `{ADMIN, WAREHOUSE}`",
        1,
    )
    assert stale != (ROOT / "SYSTEM_ARCHITECTURE.md").read_text(encoding="utf-8"), (
        "测试前提失效：角色集合表述已改变"
    )
    _patch_read(monkeypatch, {"SYSTEM_ARCHITECTURE.md": stale})

    checker.check_roles()

    assert checker.failures, "角色集合漂移未被发现"


def test_readme_advertising_an_untested_python_is_caught(monkeypatch):
    """The exact drift this checker was written for: README said 3.11+, CI ran 3.13."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme = readme.replace("要求 Windows 和 **Python 3.13**", "要求 Windows 和 **Python 3.11**", 1)
    assert readme != (ROOT / "README.md").read_text(encoding="utf-8"), (
        "测试前提失效：README 的 Python 版本表述已改变"
    )
    _patch_read(monkeypatch, {"README.md": readme})

    checker.check_python_versions()

    assert checker.failures, "宣称未测试的 Python 版本未被发现"
    assert any("README.md" in item and "3.11" in item for item in checker.failures), (
        f"报告应点名 README.md 与 3.11，实际为 {checker.failures}"
    )


def test_ci_matrix_narrower_than_declared_is_caught(monkeypatch):
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    stale = ci.replace('python-version: ["3.13"]', 'python-version: ["3.12"]', 1)
    assert stale != ci, "测试前提失效：CI 的 python-version 矩阵已改变"
    _patch_read(monkeypatch, {".github/workflows/ci.yml": stale})

    checker.check_python_versions()

    assert checker.failures, "CI 矩阵与声明不一致未被发现"


def test_install_bat_checking_an_untested_python_is_caught(monkeypatch):
    """install.bat enforced >=3.11 while CI never ran 3.11."""
    install = (ROOT / "install.bat").read_text(encoding="utf-8")
    stale = install.replace("sys.version_info >= (3, 13)", "sys.version_info >= (3, 11)", 1)
    assert stale != install, "测试前提失效：install.bat 的版本检查已改变"
    _patch_read(monkeypatch, {"install.bat": stale})

    checker.check_python_versions()

    assert checker.failures, "install.bat 接受未测试的 Python 版本未被发现"


# --------------------------------------------------------------------------
# Frontend entry point
# --------------------------------------------------------------------------


def test_doc_naming_the_legacy_template_is_caught(monkeypatch):
    """Two templates that both look current is how a fix lands in a dead file."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    _patch_read(monkeypatch, {"README.md": readme + "\n正式页面是 `index.html`。\n"})

    checker.check_frontend_entry()

    assert checker.failures, "指向遗留模板的表述未被发现"


def test_missing_document_is_caught(monkeypatch):
    _patch_read(monkeypatch, {})
    monkeypatch.setattr(checker, "ROOT", Path("/nonexistent-root-for-test"))

    checker.check_required_documents()

    assert checker.failures, "缺失文档未被发现"


def test_operational_documents_are_required():
    """Removing a doc from the list must be a deliberate act, not an oversight.

    docs/OPERATIONS.md and docs/RELEASE_CHECKLIST.md were listed as 尚未建立 in
    docs/UPGRADE.md for a long time. Now that they exist, the list is what keeps
    them existing — without it, deleting one would leave no gate to notice, which
    is how the gap opened in the first place.
    """
    for expected in (
        "docs/OPERATIONS.md",
        "docs/RELEASE_CHECKLIST.md",
        "docs/BACKUP_RESTORE.md",
        "docs/SECURITY.md",
    ):
        assert expected in checker.REQUIRED_DOCUMENTS, f"{expected} 不在必需文档清单中"


def test_every_required_document_exists_in_the_repository():
    missing = [name for name in checker.REQUIRED_DOCUMENTS if not (checker.ROOT / name).is_file()]
    assert not missing, f"必需文档缺失: {missing}"


# --------------------------------------------------------------------------
# References to files that were deleted
# --------------------------------------------------------------------------


def _fake_git(monkeypatch, tracked: str, deletions: dict[str, str]) -> None:
    """Answer `git ls-files` and `git log --diff-filter=D` without a real clone.

    The check asks git whether a path was ever deleted, which makes it depend on
    how deep the checkout is: under `--depth 1` the answer is always no. A test
    that relied on the real repository's history would therefore pass on a full
    clone and fail on a shallow one, and it would be asserting the environment
    rather than the logic. Faking both calls keeps it deterministic and lets the
    cases below state exactly which paths git claims to have deleted.
    """

    def run(args, **_kwargs):
        if args[:2] == ["git", "ls-files"]:
            return type("Result", (), {"returncode": 0, "stdout": tracked})()
        path = args[-1]
        found = deletions.get(path, "")
        return type("Result", (), {"returncode": 0, "stdout": found})()

    monkeypatch.setattr(checker.subprocess, "run", run)


def test_reference_to_a_deleted_file_is_caught(monkeypatch):
    """`static/app_v2.js` was removed with the vanilla-JS UI.

    This is the shape `design-qa.md` had at the repository root: a document that
    still read as current while every artefact it cited had been deleted.
    """
    _patch_read(monkeypatch, {"README.md": "参见 `static/app_v2.js`。"})
    _fake_git(monkeypatch, "README.md\n", {"static/app_v2.js": "a019e61\n"})

    checker.check_deleted_file_references()

    assert checker.failures, "指向已删除文件的引用未被发现"
    assert "static/app_v2.js" in checker.failures[0]
    assert "a019e61" in checker.failures[0], "应指出是哪个提交删的，否则无从排查"


def test_a_path_that_never_existed_is_not_reported(monkeypatch):
    """Documenting planned work is honest bookkeeping, not drift.

    `docs/UPGRADE.md` lists documents that do not exist yet and says 尚未建立.
    Flagging that would push the project toward deleting the note instead of
    writing the document.
    """
    _patch_read(monkeypatch, {"README.md": "`docs/NEVER_EXISTED.md` 尚未建立。"})
    _fake_git(monkeypatch, "README.md\n", {})

    checker.check_deleted_file_references()

    assert not checker.failures, f"把待办当成了漂移: {checker.failures}"


def test_the_check_is_a_noop_when_history_is_unavailable(monkeypatch):
    """A shallow clone cannot answer the question, so it must not answer wrongly.

    The test job checks out with `fetch-depth: 0` so this does not bite there,
    but the failure mode is worth pinning: with no history the check reports
    nothing rather than guessing, and CI's docs job is where the real answer
    comes from. Silence is the honest outcome; inventing a verdict would not be.
    """
    _patch_read(monkeypatch, {"README.md": "参见 `static/app_v2.js`。"})
    _fake_git(monkeypatch, "README.md\n", {})

    checker.check_deleted_file_references()

    assert not checker.failures, "没有历史时不应凭空报告"


def test_historical_areas_are_exempt(monkeypatch):
    """`.kiro/specs/` and `docs/archive/` are allowed to cite removed files.

    A spec saying "modify static/app_v2.js" was true when it was written. Editing
    it to match today would falsify the record, which is worse than the
    confusion — so the exemption is by location, and the location is the signal
    that a document is a record rather than a description.
    """
    _patch_read(
        monkeypatch,
        {
            ".kiro/specs/example/design.md": "改写 `static/app_v2.js`。",
            "docs/archive/old-report.md": "证据：`tests/ui-dashboard-mobile.png`。",
        },
    )
    _fake_git(
        monkeypatch,
        ".kiro/specs/example/design.md\ndocs/archive/old-report.md\n",
        {"static/app_v2.js": "a019e61\n", "tests/ui-dashboard-mobile.png": "a019e61\n"},
    )

    checker.check_deleted_file_references()

    assert not checker.failures, f"历史区域被误报: {checker.failures}"


def test_the_exemption_prefixes_cover_the_areas_that_need_them():
    """The prefixes are the mechanism; a typo would silently stop protecting."""
    assert ".kiro/" in checker._HISTORICAL_PREFIXES
    assert "docs/archive/" in checker._HISTORICAL_PREFIXES


def test_current_docs_do_not_reference_deleted_files():
    """The repository itself: no current document points at a removed file."""
    checker.check_deleted_file_references()

    assert not checker.failures, "当前文档引用了已删除的文件：\n  " + "\n  ".join(checker.failures)


# --------------------------------------------------------------------------
# The real repository — this is the part CI runs
# --------------------------------------------------------------------------


def test_the_repository_is_consistent():
    """Fails the build when a hand-written doc describes a system that is gone."""
    assert checker.main() == 0, (
        "文档与代码不一致：\n  " + "\n  ".join(checker.failures)
    )
