"""Fail when the committed front-end bundle no longer matches its source.

``GET /`` serves ``static/dist/index.html`` — the output of the Vite build — as a
static file. The bundle is committed because the factory server has no Node
toolchain: ``install.bat`` and ``install-linux.sh`` set up Python only. That
choice buys a deployment with no build step, and it costs exactly one hazard:
somebody edits ``frontend/``, runs the app, sees their change (because they built
locally), and commits only the source. The repository then ships a bundle that
does not contain the fix, and nothing notices — the Python tests assert against
whatever ``static/dist`` currently holds.

So the check is mechanical. CI builds from source and then runs this: if the
working tree's ``static/dist`` differs from the committed one, the build is
stale, and the run fails with the command needed to fix it.

CI does not commit the rebuilt files. A workflow that pushes generated files
turns every build into a possible merge conflict and makes the diff of a
"no-op" run impossible to review. The developer rebuilds and commits; the
pipeline only refuses to let the drift through.

Run it after a build:

    cd frontend && pnpm build && cd ..
    python tools/check_frontend_build.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "static" / "dist"
ENTRY = BUNDLE / "index.html"

REBUILD_HINT = """\
重新构建并提交 bundle：

    cd frontend
    pnpm install --frozen-lockfile
    pnpm build
    cd ..
    git add static/dist
    git commit -m "build: refresh the committed front-end bundle"

CI 不会替你提交这些文件——生成物入库要靠一次有意识的提交，
否则每次构建都可能产生冲突，且「什么都没改」的运行也会留下难以审查的 diff。\
"""


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    if not ENTRY.is_file():
        print(
            f"缺少 {ENTRY.relative_to(ROOT)}：前端尚未构建。\n\n{REBUILD_HINT}",
            file=sys.stderr,
        )
        return 1

    status = _git("status", "--porcelain", "--", "static/dist")
    if status.returncode != 0:
        print(
            "无法读取 git 状态，本检查需要在一个 git 工作区内运行。",
            file=sys.stderr,
        )
        return 2

    changed = [line for line in status.stdout.splitlines() if line.strip()]
    if not changed:
        print("static/dist 与已提交的 bundle 一致。")
        return 0

    # A build that only rewrites hashes with identical output still counts as
    # drift: the committed bytes are what ships, and they would be different.
    modified = [line for line in changed if not line.startswith("??")]
    untracked = [line for line in changed if line.startswith("??")]

    print("static/dist 与源码构建结果不一致。", file=sys.stderr)
    print("", file=sys.stderr)
    if modified:
        print(f"  已跟踪但内容不同（{len(modified)} 个）：", file=sys.stderr)
        for line in modified[:10]:
            print(f"    {line}", file=sys.stderr)
        if len(modified) > 10:
            print(f"    ... 另有 {len(modified) - 10} 个", file=sys.stderr)
    if untracked:
        print(f"  构建新增、尚未提交（{len(untracked)} 个）：", file=sys.stderr)
        for line in untracked[:10]:
            print(f"    {line}", file=sys.stderr)
        if len(untracked) > 10:
            print(f"    ... 另有 {len(untracked) - 10} 个", file=sys.stderr)
    print("", file=sys.stderr)
    print(REBUILD_HINT, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
