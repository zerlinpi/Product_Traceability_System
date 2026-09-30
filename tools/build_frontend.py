"""Build the front end and record what it was built from.

Use this instead of ``pnpm build``. The build itself is unchanged; the extra step
writes ``static/dist/build-info.json``, the fingerprint that
``tools/check_frontend_build.py`` compares against in CI.

The fingerprint has to be written after every build, because that is the only
thing that tells the repository which source revision the committed bundle came
from. A build that skips it leaves the previous fingerprint in place, and the
bundle would then be trusted for source it does not contain — so this is one
command rather than two, and ``check_frontend_build.py`` fails loudly when the
file is missing rather than assuming it is current.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

sys.path.insert(0, str(ROOT / "tools"))
from check_frontend_build import BUNDLE, ENTRY, write_build_info  # noqa: E402


def main() -> int:
    if not (FRONTEND / "package.json").is_file():
        print(f"找不到 {FRONTEND / 'package.json'}", file=sys.stderr)
        return 1

    print("=== pnpm build ===")
    build = subprocess.run(
        ["pnpm", "build"],
        cwd=FRONTEND,
        shell=(sys.platform == "win32"),
        check=False,
    )
    if build.returncode != 0:
        print("构建失败，未更新指纹。", file=sys.stderr)
        return build.returncode

    if not ENTRY.is_file():
        print(f"构建结束但没有产出 {ENTRY.relative_to(ROOT)}", file=sys.stderr)
        return 1

    fingerprint = write_build_info()
    print()
    print(f"已写入 {BUNDLE.joinpath('build-info.json').relative_to(ROOT)}")
    print(f"  源码指纹: {fingerprint}")
    print()
    print("提交前记得一并提交 static/dist：")
    print("    git add static/dist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
