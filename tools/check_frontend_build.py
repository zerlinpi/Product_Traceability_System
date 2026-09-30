"""Fail when the committed front-end bundle was not built from the current source.

``GET /`` serves ``static/dist/index.html``, the output of the Vite build. The
bundle is committed because the factory server has no Node toolchain —
``install.bat`` and ``install-linux.sh`` set up Python only. That buys a
deployment with no build step, and it costs exactly one hazard: somebody edits
``frontend/``, builds locally, sees their change, and commits only the source.
The repository then ships a bundle without the fix, and nothing notices, because
the Python tests assert against whatever ``static/dist`` currently contains.

Why this is a fingerprint and not a byte comparison
---------------------------------------------------
The obvious check — rebuild in CI, diff against what git has — does not work
here, and the reason is worth writing down so nobody tries it again.

The build is not byte-reproducible. Two builds two seconds apart on one machine
produce different chunks, and the same source built on Windows and Linux
produces different chunks again. Two causes:

1. ``lastBuildTime`` was ``dayjs()`` — the wall clock, baked into the bundle.
   That one is fixed in ``vite.config.ts``: it now comes from ``SOURCE_DATE_EPOCH``
   or the git commit time, in UTC. After that change the JavaScript output is
   identical run to run.
2. UnoCSS emits its theme custom properties in a non-deterministic order. Same
   declarations, same bytes, shuffled — two builds differ by two swapped
   ``--fontWeight-*`` / ``--colors-*`` lines inside ``:root``. That is inside the
   library and cannot be pinned from here.

So the bundle is compared by *what it was built from*, not by what it came out
as. The build records a fingerprint of its inputs; this check recomputes the
fingerprint from the working tree and compares. Deterministic across platforms,
because it hashes sources rather than build output.

Usage::

    python tools/build_frontend.py     # build, then record the fingerprint
    python tools/check_frontend_build.py   # CI: is the committed bundle current?
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "static" / "dist"
ENTRY = BUNDLE / "index.html"
BUILD_INFO = BUNDLE / "build-info.json"

# What the bundle is built from. Kept explicit rather than "everything under
# frontend/" so that adding a README to a workspace package does not demand a
# rebuild. Anything that can change the emitted bundle belongs here.
SOURCE_ROOTS = (
    "frontend/apps/web/src",
    "frontend/apps/web/vite",
    "frontend/apps/web/public",
    "frontend/apps/web/index.html",
    "frontend/apps/web/vite.config.ts",
    "frontend/apps/web/postcss.config.js",
    "frontend/apps/web/package.json",
    "frontend/apps/web/tsconfig.json",
    "frontend/apps/web/tsconfig.app.json",
    "frontend/apps/web/tsconfig.node.json",
    "frontend/packages",
    "frontend/uno.config.ts",
    "frontend/tsconfig.json",
    "frontend/pnpm-lock.yaml",
    "frontend/package.json",
)

SKIP_DIRS = {"node_modules", "dist", ".turbo", ".cache"}

REBUILD_HINT = """\
重新构建并提交 bundle：

    python tools/build_frontend.py
    git add static/dist
    git commit -m "build: refresh the committed front-end bundle"

CI 不会替你提交这些文件——生成物入库要靠一次有意识的提交，
否则每次构建都可能产生冲突，且「什么都没改」的运行也会留下难以审查的 diff。\
"""


def _iter_sources() -> list[Path]:
    files: list[Path] = []
    for entry in SOURCE_ROOTS:
        target = ROOT / entry
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            for path in target.rglob("*"):
                if path.is_file() and not any(part in SKIP_DIRS for part in path.parts):
                    files.append(path)
    return sorted(files)


def source_fingerprint() -> str:
    """Hash the build inputs: path + normalised content, order-independent.

    Line endings are normalised because git checks out CRLF on Windows and LF
    elsewhere; without that the fingerprint would differ per platform for
    identical sources, which is the whole failure this replaces.
    """
    digest = hashlib.sha256()
    for path in _iter_sources():
        relative = path.relative_to(ROOT).as_posix()
        content = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def write_build_info() -> str:
    """Record the fingerprint of what the bundle was just built from."""
    fingerprint = source_fingerprint()
    BUILD_INFO.write_text(
        json.dumps(
            {
                "sourceFingerprint": fingerprint,
                "sourceFileCount": len(_iter_sources()),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return fingerprint


def main() -> int:
    if not ENTRY.is_file():
        print(
            f"缺少 {ENTRY.relative_to(ROOT)}：前端尚未构建。\n\n{REBUILD_HINT}",
            file=sys.stderr,
        )
        return 1

    if not BUILD_INFO.is_file():
        print(
            f"缺少 {BUILD_INFO.relative_to(ROOT)}：这份 bundle 没有记录它构建自哪一版源码，\n"
            f"因此无法判断它是否过期。\n\n{REBUILD_HINT}",
            file=sys.stderr,
        )
        return 1

    recorded = json.loads(BUILD_INFO.read_text(encoding="utf-8"))
    expected = recorded.get("sourceFingerprint")
    actual = source_fingerprint()

    if expected == actual:
        print(f"已提交的 bundle 与当前源码一致（{recorded.get('sourceFileCount')} 个源文件）。")
        return 0

    print("已提交的 bundle 不是由当前源码构建的。", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"  bundle 记录的指纹: {expected}", file=sys.stderr)
    print(f"  当前源码的指纹:     {actual}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "源码在 bundle 构建之后又改过。仓库里跑的是旧界面，"
        "而 Python 测试断言的是「当前 static/dist 的内容」，不会发现这一点。",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print(REBUILD_HINT, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
