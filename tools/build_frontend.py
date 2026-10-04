"""Build the front end and record what it was built from.

Use this instead of ``pnpm build``. The build itself is unchanged; the extra steps
write ``static/dist/build-info.json`` — the fingerprint that
``tools/check_frontend_build.py`` compares against in CI — and a ``.gz`` sibling
for every compressible asset, which ``traceability/responses.py`` serves in place
of the original.

Both extras have to happen here rather than in the pipeline, because that is the
only thing that tells the repository which source revision the committed bundle
came from. A build that skips the fingerprint leaves the previous one in place and
the bundle would then be trusted for source it does not contain; a build that
skips the compressed copies leaves the previous build's bytes behind, and the
server would send them under ``Content-Encoding: gzip`` where the browser cannot
tell they are wrong. So this is one command rather than three, and
``check_frontend_build.py`` fails loudly when the fingerprint is missing rather
than assuming it is current.
"""

from __future__ import annotations

import gzip
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
from check_frontend_build import BUNDLE, ENTRY, write_build_info  # noqa: E402
from traceability.responses import COMPRESSIBLE_SUFFIXES  # noqa: E402


def write_compressed_copies() -> tuple[int, int, int]:
    """Write a ``.gz`` next to every compressible asset. Returns (count, raw, gz).

    Compression level 9 because it runs once per build, not per request. Stale
    copies are removed first so a renamed asset cannot leave one behind — the
    server checks freshness too, but a build should not depend on that check to
    stay correct.
    """
    removed = 0
    for stale in BUNDLE.rglob("*.gz"):
        stale.unlink()
        removed += 1

    written = 0
    raw_total = 0
    gz_total = 0
    for asset in sorted(BUNDLE.rglob("*")):
        if not asset.is_file() or asset.suffix.lower() not in COMPRESSIBLE_SUFFIXES:
            continue
        raw = asset.read_bytes()
        compressed = gzip.compress(raw, 9)
        # A compressed copy larger than the original is possible for tiny files
        # and is never worth serving.
        if len(compressed) >= len(raw):
            continue
        asset.with_name(asset.name + ".gz").write_bytes(compressed)
        written += 1
        raw_total += len(raw)
        gz_total += len(compressed)

    if removed:
        print(f"  清理了 {removed} 个旧的 .gz")
    return written, raw_total, gz_total


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
    written, raw_total, gz_total = write_compressed_copies()
    if written:
        ratio = gz_total / raw_total * 100
        print(f"已生成 {written} 个 .gz：{raw_total / 1024 / 1024:.2f} MB -> {gz_total / 1024 / 1024:.2f} MB（{ratio:.0f}%）")
        print("  服务端在客户端接受 gzip 时直接发送这些文件（traceability/responses.py）")
    else:
        print("没有可压缩的资源")

    print()
    print("提交前记得一并提交 static/dist：")
    print("    git add static/dist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
