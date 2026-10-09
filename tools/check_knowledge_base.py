"""Check that every link in the knowledge base points at something real.

Why this exists
---------------
A manual that links to a page that does not exist is worse than one with no links
at all: the reader follows it, finds nothing, and stops trusting the rest. The
knowledge base is a deliverable, so its internal references are checked the same
way the API contract and the permission matrix are.

Checks, for every Markdown file under ``docs/knowledge-base``:

* relative file links resolve to a file that exists;
* anchors (``page.md#section``) resolve to a heading in the target file, matched
  the way GitHub slugs them — Chinese headings keep their characters;
* a file with no inbound link is reported, because an unreachable page is a page
  nobody reads.

External links (``http://``, ``https://``, ``mailto:``) are not fetched: a check
that fails when the network is down teaches people to ignore it.

    python tools/check_knowledge_base.py
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_BASE = ROOT / "docs" / "knowledge-base"

#: ``[text](target)`` — the target stops at whitespace or the closing paren.
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, spaces to hyphens, punctuation dropped."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\u4e00-\u9fff\s-]", "", text, flags=re.UNICODE)
    text = text.replace(" ", "-")
    return unicodedata.normalize("NFKC", text)


def headings_of(path: Path) -> set[str]:
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    return {slug(match.group(2)) for match in HEADING.finditer(text)}


def links_of(path: Path) -> list[str]:
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    return [match.group(1) for match in LINK.finditer(text)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(KNOWLEDGE_BASE), help="knowledge base directory")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"[FAIL] 知识库目录不存在：{root}")
        return 1

    pages = sorted(root.rglob("*.md"))
    if not pages:
        print(f"[FAIL] 知识库没有任何 Markdown 文件：{root}")
        return 1

    known = {page.resolve() for page in pages}
    anchor_cache: dict[Path, set[str]] = {}
    inbound: dict[Path, set[str]] = {page.resolve(): set() for page in pages}
    problems: list[str] = []
    checked = 0

    for page in pages:
        for target in links_of(page):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            checked += 1
            relative, _, fragment = target.partition("#")
            destination = (page.parent / relative).resolve() if relative else page.resolve()

            if not destination.exists():
                problems.append(f"{page.relative_to(root)} → 链接目标不存在：{target}")
                continue
            if destination.is_dir():
                index = destination / "README.md"
                if not index.exists():
                    problems.append(f"{page.relative_to(root)} → 目录没有 README：{target}")
                    continue
                destination = index.resolve()
            if destination not in known:
                problems.append(f"{page.relative_to(root)} → 链接指向知识库之外：{target}")
                continue

            inbound[destination].add(str(page.relative_to(root)))

            if fragment:
                anchors = anchor_cache.setdefault(destination, headings_of(destination))
                if slug(fragment) not in anchors:
                    problems.append(
                        f"{page.relative_to(root)} → 锚点不存在：{target}"
                        f"（{destination.relative_to(root)} 没有对应标题）"
                    )

    orphans = [
        str(page.relative_to(root))
        for page in pages
        if not inbound[page.resolve()] and page.name != "README.md"
    ]

    for orphan in orphans:
        problems.append(f"{orphan} → 没有任何页面链接到它（读者找不到）")

    print(f"检查 {len(pages)} 个页面、{checked} 条内部链接。")
    if problems:
        print(f"[FAIL] 发现 {len(problems)} 个问题：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("[OK] 知识库链接完整，没有孤立页面。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
