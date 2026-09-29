"""Fail when the docs describe a version of the system that no longer exists.

The problem this solves is specific. `docs/PERMISSION_MATRIX.md` is generated and
therefore cannot drift. Every other document is hand-written, and during the
phase-2 refactor they did drift: the architecture doc still said `user_version`
21 after v22 shipped, `API_CONTRACT.md` still split the route count as
"app.py 100 + auth.py 7" after nine blueprints had moved out, `SECURITY.md`
called the systemd sandbox unenabled when `PrivateTmp` and `NoNewPrivileges` were
already set, and the README advertised Python 3.11+ while CI only ever ran 3.13.

Each of those is cheap to fix and expensive to miss: a reader trusts the doc, and
an untested Python version advertised as supported is a promise nobody verified.
So the check is mechanical, it names the file and the line, and it runs in CI.

What it deliberately does *not* do is restate numbers that change for legitimate
reasons. Route counts per file move with every domain extraction, so only the
total is pinned. Where a document has to repeat a value, the value is read from
the code and compared — never the other way round.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import extract_routes  # noqa: E402  (path set up above)

failures: list[str] = []


def fail(message: str) -> None:
    failures.append(message)


def read(relative: str) -> str:
    path = ROOT / relative
    if not path.is_file():
        fail(f"{relative}: 文件不存在")
        return ""
    return path.read_text(encoding="utf-8")


def check_required_documents() -> None:
    """Every document the project claims to maintain must actually be there."""
    for relative in (
        "README.md",
        "SYSTEM_ARCHITECTURE.md",
        "docs/API_CONTRACT.md",
        "docs/BACKUP_RESTORE.md",
        "docs/DATA_MODEL.md",
        "docs/PERMISSION_MATRIX.md",
        "docs/PRODUCT_RULES.md",
        "docs/SECURITY.md",
        "docs/UPGRADE.md",
        ".github/workflows/ci.yml",
    ):
        if not (ROOT / relative).is_file():
            fail(f"{relative}: 文档缺失")


def check_schema_version() -> None:
    """The migration chain's terminal version, as stated in prose."""
    from traceability.db import SCHEMA_VERSION

    architecture = read("SYSTEM_ARCHITECTURE.md")
    match = re.search(r"`user_version`\s*当前为\s*(\d+)", architecture)
    if not match:
        fail("SYSTEM_ARCHITECTURE.md: 找不到「`user_version` 当前为 N」的表述")
    elif int(match.group(1)) != SCHEMA_VERSION:
        fail(
            f"SYSTEM_ARCHITECTURE.md: 声称 user_version={match.group(1)}，"
            f"代码为 {SCHEMA_VERSION}"
        )

    data_model = read("docs/DATA_MODEL.md")
    match = re.search(r"`user_version\s*=\s*(\d+)`", data_model)
    if not match:
        fail("docs/DATA_MODEL.md: 找不到「`user_version = N`」的表述")
    elif int(match.group(1)) != SCHEMA_VERSION:
        fail(
            f"docs/DATA_MODEL.md: 声称 user_version={match.group(1)}，"
            f"代码为 {SCHEMA_VERSION}"
        )


def check_route_total() -> None:
    """The total is stable; the per-file split is not, so only the total is pinned."""
    routes = extract_routes.extract()
    total = len(routes)

    api_contract = read("docs/API_CONTRACT.md")
    match = re.search(r"共\s*\*\*(\d+)\s*个路由\*\*", api_contract)
    if not match:
        fail("docs/API_CONTRACT.md: 找不到「共 **N 个路由**」的表述")
    elif int(match.group(1)) != total:
        fail(
            f"docs/API_CONTRACT.md: 声称共 {match.group(1)} 个路由，实际 {total} 个"
        )

    matrix = read("docs/PERMISSION_MATRIX.md")
    match = re.search(r"全部\s*(\d+)\s*个路由", matrix)
    if match and int(match.group(1)) != total:
        fail(
            f"docs/PERMISSION_MATRIX.md: 声称全部 {match.group(1)} 个路由，"
            f"实际 {total} 个"
        )

    # A blueprint the extractor does not scan would silently vanish from the
    # permission matrix, so every file in the directory must be in SOURCES.
    sources = {path.resolve() for path in extract_routes.SOURCES}
    for blueprint in sorted(extract_routes.BLUEPRINT_DIR.glob("*.py")):
        if blueprint.name == "__init__.py":
            continue
        if blueprint.resolve() not in sources:
            # Not every blueprint is inside ROOT in every context (a test may
            # point BLUEPRINT_DIR at a temporary directory), so the label must
            # not assume relative_to() will succeed.
            try:
                label = blueprint.relative_to(ROOT)
            except ValueError:
                label = blueprint
            fail(
                f"{label}: 是蓝图但未被 extract_routes.SOURCES 扫描，"
                "其路由会从权限矩阵中消失"
            )


def check_roles() -> None:
    """The role model is a business contract; the prose must name exactly it."""
    from traceability.auth import VALID_ROLES

    architecture = read("SYSTEM_ARCHITECTURE.md")
    match = re.search(r"最终角色集合恰好为\s*`\{([^}]+)\}`", architecture)
    if not match:
        fail("SYSTEM_ARCHITECTURE.md: 找不到角色集合的表述")
        return
    documented = {item.strip() for item in match.group(1).split(",") if item.strip()}
    if documented != set(VALID_ROLES):
        fail(
            f"SYSTEM_ARCHITECTURE.md: 角色集合写作 {sorted(documented)}，"
            f"代码为 {sorted(VALID_ROLES)}"
        )


def check_python_versions() -> None:
    """One version list, agreed on by pyproject, CI, README and install.bat.

    Advertising a version CI never runs is the drift that motivated this file,
    so all four sources have to agree rather than merely coexist.
    """
    config = tomllib.loads(read("pyproject.toml"))
    declared = config.get("tool", {}).get("pts", {}).get("supported-python")
    if not declared:
        fail("pyproject.toml: 缺少 [tool.pts].supported-python")
        return
    declared_set = set(declared)

    ci = read(".github/workflows/ci.yml")
    match = re.search(r"python-version:\s*\[([^\]]+)\]", ci)
    if not match:
        fail(".github/workflows/ci.yml: 找不到测试矩阵的 python-version")
    else:
        tested = {item.strip().strip('"\'') for item in match.group(1).split(",") if item.strip()}
        if tested != declared_set:
            fail(
                f"pyproject.toml 声明支持 {sorted(declared_set)}，"
                f"但 CI 只测 {sorted(tested)}——不得宣称未测试的版本"
            )

    ruff = config.get("tool", {}).get("ruff", {}).get("target-version", "")
    if ruff and ruff != f"py{min(declared_set).replace('.', '')}":
        fail(
            f"pyproject.toml: ruff target-version={ruff} 与 supported-python "
            f"{sorted(declared_set)} 不一致"
        )

    readme = read("README.md")
    match = re.search(r"要求 Windows 和 \*\*Python ([0-9.]+)\*\*", readme)
    if not match:
        fail("README.md: 找不到「要求 Windows 和 **Python X.Y**」的表述")
    elif match.group(1) not in declared_set:
        fail(
            f"README.md: 声称需要 Python {match.group(1)}，"
            f"声明支持的是 {sorted(declared_set)}"
        )

    install = read("install.bat")
    match = re.search(r"sys\.version_info >= \((\d+),\s*(\d+)\)", install)
    if not match:
        fail("install.bat: 找不到 Python 版本检查")
    else:
        wanted = f"{match.group(1)}.{match.group(2)}"
        if wanted not in declared_set:
            fail(
                f"install.bat: 检查 Python >= {wanted}，"
                f"声明支持的是 {sorted(declared_set)}"
            )


#: How GET / may hand out the page: the built SPA entry file, or (historically)
#: a Jinja template. Either way the argument is the path of the file served.
_ENTRY_ROUTE_RE = re.compile(
    r'@app\.get\("/"\)\s*\n\s*def \w+\([^)]*\):\s*\n\s*'
    r'return (?:frontend_entry_response|render_template)\("([^"]+)"\)'
)

#: Any mention of an entry page in the docs, with whatever directory prefix it
#: carries (``static/dist/index.html``, ``templates/index_v2.html``, a bare
#: ``index.html`` …). The whole mention is compared, so a doc cannot point at
#: a different file that merely shares the base name.
_ENTRY_MENTION_RE = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*index(?:_v2)?\.html)")


def check_frontend_entry() -> None:
    """The served entry page must be the one the docs call the live one.

    Two entry pages that both look current is how a fix lands in the file
    nobody loads, so the entry point is pinned to the route that actually
    serves it, and the file must exist in the repository.
    """
    app_source = read("app.py")
    match = _ENTRY_ROUTE_RE.search(app_source)
    if not match:
        fail("app.py: 找不到 GET / 提供的入口页面")
        return
    served = match.group(1)
    if "/" in served and not (ROOT / served).is_file():
        fail(f"app.py: GET / 提供 {served}，但仓库中不存在该文件（前端未构建？）")

    for relative in ("README.md", "SYSTEM_ARCHITECTURE.md"):
        text = read(relative)
        for mentioned in _ENTRY_MENTION_RE.findall(text):
            if mentioned != served:
                fail(
                    f"{relative}: 提到 {mentioned}，但 GET / 实际提供 {served}"
                )


def main() -> int:
    check_required_documents()
    check_schema_version()
    check_route_total()
    check_roles()
    check_python_versions()
    check_frontend_entry()

    if failures:
        print("文档与代码不一致：\n")
        for item in failures:
            print(f"  - {item}")
        print(
            f"\n{len(failures)} 处不一致。文档必须描述当前系统，"
            "不能停留在几个月前的状态。"
        )
        return 1

    print("文档一致性检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
