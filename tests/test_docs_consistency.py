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


# --------------------------------------------------------------------------
# The real repository — this is the part CI runs
# --------------------------------------------------------------------------


def test_the_repository_is_consistent():
    """Fails the build when a hand-written doc describes a system that is gone."""
    assert checker.main() == 0, (
        "文档与代码不一致：\n  " + "\n  ".join(checker.failures)
    )
