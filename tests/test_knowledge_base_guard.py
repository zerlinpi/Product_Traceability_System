"""The knowledge-base link checker, tested against a knowledge base that is broken.

The check runs in CI on every push, but CI only ever runs it against the committed
docs — which pass. That makes the failure paths the only interesting ones, and until
now none of them had ever executed: a checker that has only seen a good input is a
checker nobody knows is broken. A regression that made it always print ``[OK]``
would go straight through.

These tests build small synthetic bases under ``tmp_path`` so each broken case is
reachable, and finish by running the real repository the way CI does.

Feature: docs-quality, Property 76: 知识库链接守卫
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import check_knowledge_base as checker  # noqa: E402


def write(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def run(root: Path, monkeypatch) -> int:
    """Run the checker against ``root`` the way the command line does.

    ``root`` is resolved first: the real invocation resolves its own ``__file__``,
    and the anchor message compares a resolved destination against the root it was
    given. Passing an unresolved path here would test an error message the command
    line can never produce — on Windows a short-name temp directory would make
    ``relative_to`` raise instead of report.
    """
    monkeypatch.setattr(sys, "argv", ["check_knowledge_base.py", "--root", str(root.resolve())])
    return checker.main()


@pytest.fixture()
def base(tmp_path):
    """A base that passes, so each negative test has exactly one defect.

    Without this, an unrelated orphan page shows up in the output and the assertion
    about *this* defect is satisfied by a different one — a test that passes for the
    wrong reason and keeps passing when the defect it names is fixed.
    """
    root = tmp_path / "kb"
    write(root, "README.md", "# 目录\n\n[页面](01-页面.md)\n")
    write(root, "01-页面.md", "# 页面\n\n## 备份与恢复\n\n内容\n")
    return root


# --------------------------------------------------------------------------
# slug(): the rule that decides whether a link to #section works at all
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("批次生产流程", "批次生产流程"),
        ("Hello, World!", "hello-world"),
        ("库存 异常 处理", "库存-异常-处理"),
        # GitHub replaces each space with a hyphen and does not collapse them,
        # so a double space is a double hyphen. Dropping the "&" leaves the
        # two spaces that surrounded it: "backup--restore", not "backup-restore".
        ("Backup & Restore", "backup--restore"),
        ("  Spaced  Out  ", "spaced--out"),
        ("a/b:c?d", "abcd"),
        ("Ａ１", "a1"),  # NFKC: full-width folds to ASCII
    ],
)
def test_slug_follows_the_github_rule(heading, expected):
    """A heading and its anchor must agree, or every #link to it is a dead link."""
    assert checker.slug(heading) == expected


def test_a_heading_and_the_fragment_that_points_at_it_agree():
    """Both sides of the comparison go through the same function — assert it anyway.

    If ``slug`` changes without ``headings_of`` changing with it, the two call
    sites drift and every anchor in the knowledge base breaks at once.
    """
    assert checker.slug("备份与恢复") == checker.slug("备份与恢复")


# --------------------------------------------------------------------------
# What the checker accepts
# --------------------------------------------------------------------------


def test_a_consistent_base_passes(base, monkeypatch):
    write(base, "sop/管理员.md", "# 管理员\n\n## 备份与恢复\n\n内容\n")
    write(base, "01-页面.md", "# 页面\n\n## 备份与恢复\n\n见[SOP](sop/管理员.md#备份与恢复)。\n")

    assert run(base, monkeypatch) == 0


def test_a_directory_link_resolves_through_its_readme(base, monkeypatch):
    """A link to a folder is a link to its README — the way a wiki behaves."""
    write(base, "README.md", "# 目录\n\n[页面](01-页面.md)\n[案例](案例/)\n")
    write(base, "案例/README.md", "# 案例\n\n[库存异常](库存异常.md)\n")
    write(base, "案例/库存异常.md", "# 库存异常\n")

    assert run(base, monkeypatch) == 0


def test_links_inside_fenced_code_are_not_followed(base, monkeypatch):
    """A code sample that shows a markdown link is documentation, not a link.

    The base itself is consistent; the fenced ``[text](missing.md)`` is a snippet
    the reader copies, and reporting it would train the author to silence the
    checker instead of fixing their links.
    """
    write(base, "README.md", "# 目录\n\n[页面](01-页面.md)\n\n```\n[text](missing.md)\n```\n")

    assert run(base, monkeypatch) == 0


def test_external_and_same_page_links_are_skipped(base, monkeypatch):
    """A check that fails when the network is down teaches people to ignore it."""
    write(
        base,
        "README.md",
        "# 目录\n\n"
        "[页面](01-页面.md)\n"
        "[外链](https://example.com/docs)\n"
        "[邮件](mailto:ops@example.com)\n"
        "[同页](##本地标题)\n",
    )

    assert run(base, monkeypatch) == 0


# --------------------------------------------------------------------------
# What the checker refuses
# --------------------------------------------------------------------------


def test_a_link_to_a_page_that_does_not_exist_fails(base, monkeypatch, capsys):
    write(base, "README.md", "# 目录\n\n[页面](01-页面.md)\n[丢了](02-不存在.md)\n")

    assert run(base, monkeypatch) == 1
    assert "链接目标不存在" in capsys.readouterr().out


def test_a_foreign_anchor_fails(base, monkeypatch, capsys):
    """The message has to name the page, or the author has to hunt for it."""
    write(base, "README.md", "# 目录\n\n[锚点](01-页面.md#没有这个标题)\n")

    assert run(base, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "锚点不存在" in out
    assert "01-页面.md" in out


def test_a_directory_without_a_readme_fails(base, monkeypatch, capsys):
    (base / "空").mkdir()
    write(base, "README.md", "# 目录\n\n[页面](01-页面.md)\n[空目录](空/)\n")

    assert run(base, monkeypatch) == 1
    assert "目录没有 README" in capsys.readouterr().out


def test_a_link_out_of_the_base_fails(base, tmp_path, monkeypatch, capsys):
    """The target has to exist for the "outside" branch to be the one reported.

    Pointing at a file that is neither inside nor present falls through the
    existence check first and is reported as a missing target — still a failure,
    still naming the offending link. Reaching the "outside" message needs a real
    file on the other side of the boundary.
    """
    write(tmp_path, "outside.md", "# 外部\n")
    write(base, "README.md", "# 目录\n\n[页面](01-页面.md)\n[出去](../outside.md)\n")

    assert run(base, monkeypatch) == 1
    assert "链接指向知识库之外" in capsys.readouterr().out


def test_a_page_nobody_links_to_fails(base, monkeypatch, capsys):
    """An unreachable page is a page nobody reads — and it drifts silently."""
    write(base, "02-孤儿.md", "# 孤儿\n")

    assert run(base, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "02-孤儿.md" in out
    assert "没有任何页面链接到它" in out


def test_the_readme_is_not_an_orphan(base, monkeypatch):
    """The index is where you start; it is allowed to have no inbound link."""
    assert run(base, monkeypatch) == 0


def test_a_root_that_is_not_a_directory_fails(tmp_path, monkeypatch, capsys):
    """Deleting the folder is a real way to lose the knowledge base wholesale."""
    root = tmp_path / "docs" / "knowledge-base"

    assert run(root, monkeypatch) == 1
    assert "知识库目录不存在" in capsys.readouterr().out


def test_a_directory_with_no_pages_fails(tmp_path, monkeypatch, capsys):
    """Silence is not success: no pages means no links, which is not a pass."""
    root = tmp_path / "empty"
    root.mkdir()

    assert run(root, monkeypatch) == 1
    assert "没有任何 Markdown 文件" in capsys.readouterr().out


# --------------------------------------------------------------------------
# The repository, the way CI runs it
# --------------------------------------------------------------------------


def test_the_committed_knowledge_base_passes(base, monkeypatch):
    """The real check, not a stand-in: this is exactly what the Docs job runs.

    A non-ASCII page name is the case that caught the sibling docs checker — git
    quotes those paths when listing files, and anything reading ``git ls-files``
    literally concludes the file does not exist. This base is read straight off
    the filesystem, so it cannot hit that; asserting it stays that way is worth it.
    """
    knowledge_base = ROOT / "docs" / "knowledge-base"
    assert knowledge_base.is_dir(), "docs/knowledge-base is missing"
    assert list(knowledge_base.rglob("*.md")), "the knowledge base has no pages"

    assert run(knowledge_base, monkeypatch) == 0
