"""Extract the real route / permission matrix from app.py.

Authorization in this codebase is enforced in TWO places:

1. inside the route handler body, and
2. inside the nested service helper the handler delegates to
   (e.g. ``/pass`` calls ``transition_batch_quality`` which calls
   ``require_admin()``; ``/push`` calls ``push_purchase_order_record``
   which calls ``require_operations()``).

A handler-only scan therefore produces FALSE POSITIVES. This tool builds a
call graph over the nested functions defined inside ``create_app()`` and
resolves guards transitively, so the reported effective permission is the one
the request actually gets.

Usage:
    python tools/extract_routes.py            # summary + deviations
    python tools/extract_routes.py --json     # full machine-readable dump
    python tools/extract_routes.py --markdown # docs/PERMISSION_MATRIX.md table
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from traceability.capabilities import (  # noqa: E402
    ROLE_ADMIN,
    ROLE_OPERATIONS,
    ROLE_WAREHOUSE,
    Capability,
    roles_for,
)

# Routes are registered in more than one module. ``app.py`` holds the bulk;
# ``traceability/auth.py`` registers the authentication and user-management
# routes inside ``initialize_auth()``; the rest live in blueprint modules under
# ``traceability/api/``. Scanning only app.py silently under-counts the API
# surface (and hides those routes from the permission matrix).
#
# The blueprint directory is globbed rather than listed so a new blueprint is
# picked up automatically — a hand-maintained list is exactly the thing that
# goes stale and makes this gate lie.
BLUEPRINT_DIR = ROOT / "traceability" / "api"
SOURCES: tuple[Path, ...] = (
    ROOT / "app.py",
    ROOT / "traceability" / "auth.py",
) + tuple(sorted(BLUEPRINT_DIR.glob("*.py")))

# A route is declared either on the Flask app inside ``create_app()`` (4-space
# indent) or on a blueprint at module level (0 indent). The decorator's receiver
# is therefore not fixed, and neither is the indentation.
ROUTE_RE = re.compile(
    r'^(?P<indent> *)@(?P<receiver>\w+)\.'
    r"(?P<method>get|post|put|delete|patch|route)\((?P<args>.*)\)\s*$"
)


def _def_re(indent: int) -> re.Pattern[str]:
    """Match ``def`` at exactly ``indent`` spaces.

    The call graph is built from the functions declared *alongside* the routes —
    create_app's nested helpers, or a blueprint module's top-level ones. Anything
    deeper is nested inside a route body, and anything shallower is a different
    layer: including app.py's module-level helpers made the service column list
    every helper a route touches (record_audit_event, fetch_records, ...) and
    changed the write detection for unrelated routes. Scoping to the route's own
    indent keeps the graph to the layer that actually carries the guards.
    """
    return re.compile(rf"^ {{{indent}}}def (?P<name>\w+)\(")
CALL_RE = re.compile(r"\b(?P<name>[a-z_][a-z0-9_]*)\s*\(")
CAPABILITY_CALL_RE = re.compile(r"require_capability\(\s*Capability\.(?P<name>\w+)")

# Idempotent write endpoints are split into a thin handler plus a producer:
#
#     @app.post("/api/scan-gun/inbound")
#     def scan_gun_inbound():
#         return run_idempotent("scan-gun.inbound", _impl_scan_gun_inbound)
#
#     def _impl_scan_gun_inbound():
#         require_admin_or_warehouse()
#         ...
#
# The producer is *passed as an argument*, not called, so plain call-graph
# following misses it and every guard would appear to have vanished. Resolve it
# explicitly rather than switching to bare-identifier matching, which would
# over-report guards (the dangerous direction).
IDEMPOTENT_WRAPPER_RE = re.compile(
    r'run_idempotent\(\s*"[^"]*"\s*,\s*(?P<impl>[A-Za-z_]\w*)\s*\)'
)

# Guards live in traceability/auth.py; these are the leaves of the call graph.
# Each maps to the set of roles that pass it.
GUARD_ROLE: dict[str, frozenset[str]] = {
    "require_admin": frozenset({ROLE_ADMIN}),
    "require_admin_or_warehouse": frozenset({ROLE_ADMIN, ROLE_WAREHOUSE}),
    "require_warehouse": frozenset({ROLE_ADMIN, ROLE_WAREHOUSE}),
    "require_operations": frozenset({ROLE_ADMIN, ROLE_OPERATIONS}),
}
GUARD_SCOPE = {
    "require_product_model_access": "scope:product",
    "require_supplier_access": "scope:supplier",
}

ALL_ROLES = frozenset({ROLE_ADMIN, ROLE_WAREHOUSE, ROLE_OPERATIONS})

# Scope guards are no-ops because auth.current_operator_id() returns None.
SCOPE_GUARDS_ARE_NOOP = True

# Paths exempt from the global authentication gate in auth.before_request.
# Requests to these never reach the "must be logged in" check.
GATE_EXEMPT = {"/api/health", "/api/auth/login"}

# Statements that change stored data. ``REPLACE INTO`` and ``executemany`` are
# included because SQLite's upsert form and the bulk form both write without
# matching INSERT/UPDATE; a route that used only those would have been reported
# as a read.
WRITE_RE = re.compile(
    r"BEGIN IMMEDIATE"
    r"|INSERT\s+(?:OR\s+\w+\s+)?INTO"
    r"|REPLACE\s+INTO"
    r"|UPDATE\s+\w+\s+SET"
    r"|DELETE\s+FROM"
    r"|executemany\s*\("
)
READ_METHODS = {"GET"}


def _blocks(lines: list[str], indent: int) -> dict[str, tuple[int, int]]:
    """Map function name -> (start, end) line index for one declaration layer.

    ``indent`` selects the layer: 4 for create_app()'s nested helpers, 0 for a
    blueprint module's top-level functions.
    """
    pattern = _def_re(indent)
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            starts.append((index, match.group("name")))
    blocks: dict[str, tuple[int, int]] = {}
    for position, (start, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        blocks[name] = (start, end)
    return blocks


def _calls(text: str, known: set[str]) -> set[str]:
    return {m.group("name") for m in CALL_RE.finditer(text) if m.group("name") in known}


def _code_only(text: str) -> str:
    """Drop whole-line comments before pattern matching.

    ``traceability/auth.py`` documents the capability guard with the literal
    text ``require_capability(Capability.X)`` in a comment. A line-based scan
    never saw it — the comment sits outside any function body — but once the
    graph crosses module boundaries a whole module's lines are fair game, and
    ``Capability.X`` is not a real capability, so the run aborted.

    Only whole-line comments are removed. Stripping trailing ``#`` would cut
    into string literals that legitimately contain one (``"#fff"``), and the
    cost of missing a trailing comment here is nil: a commented-out guard on
    the same line as code is not a pattern this codebase uses.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


# ---------------------------------------------------------------------------
# Cross-module call graph (deviation D9)
# ---------------------------------------------------------------------------
#
# The route modules are thin: a handler delegates to a domain function in
# ``traceability/*.py``, and that function is where the INSERT lives. The graph
# above is built per file, so as soon as a write moved into a domain module the
# route's ``Writes`` column went blank — the matrix claimed that POST
# /api/production-orders, which creates a production order, does not write.
#
# This index lets the graph leave a file, following ``from traceability.x import
# y`` into the module that defines ``y``. It only ever follows the project's own
# modules: ``_project_module_name`` returns None for anything that is not a .py
# file under ``traceability/``, so ``flask``, ``sqlite3`` and every other library
# resolve to nothing and the analysis never descends into them. That is the whole
# sandbox — no blocklist to keep current.

DOMAIN_DIR = ROOT / "traceability"

FROM_IMPORT_RE = re.compile(r"^from\s+(?P<module>[\w.]+)\s+import\s+(?P<names>.+?)\s*$")
IMPORTED_NAME_RE = re.compile(r"^(?P<name>\w+)(?:\s+as\s+(?P<alias>\w+))?$")


def _project_module_name(path: Path) -> str | None:
    """Dotted module name for a project file, or None if it is not one."""
    try:
        relative = path.resolve().relative_to(ROOT)
    except ValueError:
        return None
    if relative.suffix != ".py" or not relative.parts or relative.parts[0] != "traceability":
        return None
    return ".".join(relative.with_suffix("").parts)


def _logical_lines(lines: list[str]) -> list[str]:
    """Join parenthesised continuations so a wrapped import reads as one line.

    ``from traceability.purchasing import (`` / ``a,`` / ``b,`` / ``)`` is how
    most of these files are written, and a line-at-a-time parse would miss every
    name in it.
    """
    logical: list[str] = []
    buffer = ""
    depth = 0
    for line in lines:
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped and not buffer:
            continue
        buffer = f"{buffer} {stripped}".strip() if buffer else stripped
        depth += stripped.count("(") - stripped.count(")")
        if depth <= 0:
            logical.append(buffer)
            buffer = ""
            depth = 0
    if buffer:
        logical.append(buffer)
    return logical


class _Module:
    """One project module: its lines, its top-level functions, its imports."""

    __slots__ = ("name", "lines", "blocks", "imports")

    def __init__(self, name: str, lines: list[str]) -> None:
        self.name = name
        self.lines = lines
        self.blocks = _blocks(lines, 0)
        self.imports: dict[str, tuple[str, str]] = {}
        for line in _logical_lines(lines):
            match = FROM_IMPORT_RE.match(line)
            if not match:
                continue
            target = ROOT / (match.group("module").replace(".", "/") + ".py")
            dotted = _project_module_name(target)
            if dotted is None:
                continue
            names = match.group("names").strip().strip("()")
            for part in names.split(","):
                parsed = IMPORTED_NAME_RE.match(part.strip())
                if parsed:
                    self.imports[parsed.group("alias") or parsed.group("name")] = (
                        dotted,
                        parsed.group("name"),
                    )


class _Index:
    """Lazy cache of project modules, so the graph can cross file boundaries."""

    def __init__(self) -> None:
        self._modules: dict[str, _Module | None] = {}

    def module(self, name: str) -> _Module | None:
        if name not in self._modules:
            path = ROOT / (name.replace(".", "/") + ".py")
            self._modules[name] = (
                _Module(name, path.read_text(encoding="utf-8").splitlines())
                if path.is_file()
                else None
            )
        return self._modules[name]

    def body(self, module_name: str, function: str) -> str | None:
        module = self.module(module_name)
        if module is None or function not in module.blocks:
            return None
        start, end = module.blocks[function]
        return "\n".join(module.lines[start:end])

    def callees(self, module_name: str, body: str) -> set[tuple[str, str]]:
        """(module, function) pairs this body calls, within the project only.

        Local names win over imported ones: a module that defines ``foo`` and
        also imports a ``foo`` means its own definition is what runs.
        """
        module = self.module(module_name)
        if module is None:
            return set()
        found: set[tuple[str, str]] = set()
        for match in CALL_RE.finditer(body):
            name = match.group("name")
            if name in module.blocks:
                found.add((module_name, name))
            elif name in module.imports:
                target_module, original = module.imports[name]
                imported = self.module(target_module)
                if imported is not None and original in imported.blocks:
                    found.add((target_module, original))
        return found



def _resolve_guards(
    name: str,
    module_name: str,
    blocks: dict[str, tuple[int, int]],
    lines: list[str],
    known: set[str],
    index: _Index,
) -> tuple[list[str], set[tuple[str, str]]]:
    """Transitive guards for ``name``. Returns (guards, (module, function) pairs).

    Starts in the route's own declaration layer (``blocks``/``lines``) and then
    crosses into other project modules through ``index``, so a guard reached via
    a domain function is found rather than silently dropped.
    """
    guards: list[str] = []
    seen: set[tuple[str, str]] = set()
    queue: list[tuple[str, str]] = [(module_name, name)]
    while queue:
        current_module, current = queue.pop(0)
        if (current_module, current) in seen:
            continue
        seen.add((current_module, current))
        if current_module == module_name and current in blocks:
            start, end = blocks[current]
            body = _code_only("\n".join(lines[start:end]))
        else:
            raw = index.body(current_module, current)
            if raw is None:
                continue
            body = _code_only(raw)
        for guard in {*GUARD_ROLE, *GUARD_SCOPE}:
            if re.search(rf"\b{guard}\s*\(", body) and guard not in guards:
                guards.append(guard)
        for match in CAPABILITY_CALL_RE.finditer(body):
            token = f"cap:{match.group('name')}"
            if token not in guards:
                guards.append(token)
        for callee in index.callees(current_module, body):
            if callee not in seen:
                queue.append(callee)
        for match in IDEMPOTENT_WRAPPER_RE.finditer(body):
            impl = match.group("impl")
            if impl in known and (module_name, impl) not in seen:
                queue.append((module_name, impl))
    return guards, seen


def extract() -> list[dict[str, object]]:
    routes: list[dict[str, object]] = []
    index = _Index()
    for source in SOURCES:
        routes.extend(_extract_file(source, index))
    return routes


def _extract_file(source: Path, index: _Index) -> list[dict[str, object]]:
    lines = source.read_text(encoding="utf-8").splitlines()
    module_name = _project_module_name(source) or ""
    # Blocks are per declaration layer, resolved lazily from each route's own
    # indentation, so one file can hold both module-level helpers and nested
    # ones without either leaking into the other's call graph.
    blocks_by_indent: dict[int, dict[str, tuple[int, int]]] = {}
    routes: list[dict[str, object]] = []
    for index_line, line in enumerate(lines):
        route = ROUTE_RE.match(line)
        if not route:
            continue
        indent = len(route.group("indent"))
        blocks = blocks_by_indent.setdefault(indent, _blocks(lines, indent))
        known = set(blocks)
        args = route.group("args")
        path_match = re.match(r'\s*["\']([^"\']+)["\']', args)
        path = path_match.group(1) if path_match else args.strip()
        definition = (
            _def_re(indent).match(lines[index_line + 1]) if index_line + 1 < len(lines) else None
        )
        if not definition:
            continue
        handler = definition.group("name")
        guards, visited = _resolve_guards(handler, module_name, blocks, lines, known, index)
        start, end = blocks[handler]
        body = "\n".join(lines[start:end])
        # Write detection walks the same graph, including the parts of it that
        # live in other modules — that is where the INSERTs are.
        write_body = body
        for visited_module, callee in visited:
            if visited_module == module_name and callee == handler:
                continue
            callee_body = (
                "\n".join(lines[blocks[callee][0] : blocks[callee][1]])
                if visited_module == module_name and callee in blocks
                else index.body(visited_module, callee)
            )
            if callee_body:
                write_body += "\n" + callee_body
        roles = sorted({GUARD_ROLE[g] for g in guards if g in GUARD_ROLE})  # type: ignore[index]
        resolved: set[str] = set()
        for item in roles:  # type: ignore[union-attr]
            resolved |= set(item)
        capabilities = sorted({g[4:] for g in guards if g.startswith("cap:")})
        for name in capabilities:
            try:
                resolved |= set(roles_for(Capability[name]))
            except KeyError as error:  # pragma: no cover - guards against typos
                raise SystemExit(f"unknown capability in app.py: {name}") from error
        roles = sorted(resolved)
        scopes = sorted({GUARD_SCOPE[g] for g in guards if g in GUARD_SCOPE})
        own_guards = [
            g
            for g in guards
            if re.search(rf"\b{g}\s*\(", body) or f"Capability.{g[4:]}" in body
        ]
        routes.append(
            {
                "method": route.group("method").upper(),
                "path": path,
                "handler": handler,
                "source": str(source.relative_to(ROOT)).replace("\\", "/"),
                "line": index_line + 1,
                "roles": roles,
                "capabilities": capabilities,
                "scopes": scopes,
                # Names only: the column is a hint about where a guard lives, and
                # qualified paths would push it past the 70-char cell.
                "guard_source": sorted({callee for _module, callee in visited}),
                "guards_in_handler": sorted(own_guards),
                "guarded_in_service": bool(set(guards) - set(own_guards)),
                # The authentication gate in auth.before_request only inspects
                # paths starting with /api/, so non-API routes are public too.
                "public": path in GATE_EXEMPT or not path.startswith("/api/"),
                "writes": bool(WRITE_RE.search(write_body)),
                "write_in_handler": bool(WRITE_RE.search(body)),
            }
        )
    return routes


def _roles_label(roles: object) -> str:
    role_set = set(roles)  # type: ignore[arg-type]
    if not role_set:
        return "any authenticated"
    if role_set == set(ALL_ROLES):
        return "any authenticated"
    return " + ".join(sorted(role_set))


def effective(route: dict[str, object]) -> str:
    """Human-readable effective access, e.g. 'ADMIN + WAREHOUSE'."""
    if route.get("public"):
        return "public (no login)"
    scopes = route["scopes"]  # type: ignore[assignment]
    parts = [_roles_label(route["roles"])]
    if scopes:
        parts.append("+".join(scopes) + ("(NOOP)" if SCOPE_GUARDS_ARE_NOOP else ""))
    return " ".join(parts)


BEGIN_MARKER = "<!-- BEGIN GENERATED ROUTE TABLE -->"
END_MARKER = "<!-- END GENERATED ROUTE TABLE -->"
MATRIX_DOC = ROOT / "docs" / "PERMISSION_MATRIX.md"


def _markdown_table(routes: list[dict[str, object]]) -> str:
    lines = [
        "| Method | Path | Effective access | Capability | Guard location | Writes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for route in sorted(routes, key=lambda r: (str(r["path"]), str(r["method"]))):
        where = (
            "handler"
            if not route["guarded_in_service"]
            else "service: "
            + ", ".join(sorted(set(route["guard_source"]) - {route["handler"]}))[:70]
        )
        caps = route["capabilities"]  # type: ignore[assignment]
        cap_cell = ", ".join(f"`{item}`" for item in caps) if caps else ""
        lines.append(
            f"| `{route['method']}` | `{route['path']}` | {effective(route)} | "
            f"{cap_cell} | {where} | {'yes' if route['writes'] else ''} |"
        )
    return "\n".join(lines)


def _sync_doc(routes: list[dict[str, object]]) -> int:
    """Rewrite the generated table inside docs/PERMISSION_MATRIX.md in place."""
    if not MATRIX_DOC.exists():
        print(f"missing {MATRIX_DOC}; create it with the markers first", file=sys.stderr)
        return 2
    text = MATRIX_DOC.read_text(encoding="utf-8")
    if BEGIN_MARKER not in text or END_MARKER not in text:
        print(f"markers not found in {MATRIX_DOC}", file=sys.stderr)
        return 2
    head, rest = text.split(BEGIN_MARKER, 1)
    _old, tail = rest.split(END_MARKER, 1)
    updated = f"{head}{BEGIN_MARKER}\n{_markdown_table(routes)}\n{END_MARKER}{tail}"
    MATRIX_DOC.write_text(updated, encoding="utf-8")
    print(f"synced {len(routes)} routes into {MATRIX_DOC.relative_to(ROOT)}")
    return 0


def _check_doc(routes: list[dict[str, object]]) -> int:
    """Fail if the generated table in docs/PERMISSION_MATRIX.md is stale.

    CI runs this so the matrix cannot silently drift from app.py. Without it, a
    route added without re-running ``--sync`` makes the security documentation
    quietly wrong — and the documentation is what a reviewer actually reads.
    """
    if not MATRIX_DOC.exists():
        print(f"missing {MATRIX_DOC}", file=sys.stderr)
        return 2
    text = MATRIX_DOC.read_text(encoding="utf-8")
    if BEGIN_MARKER not in text or END_MARKER not in text:
        print(f"markers not found in {MATRIX_DOC}", file=sys.stderr)
        return 2
    _head, rest = text.split(BEGIN_MARKER, 1)
    current, _tail = rest.split(END_MARKER, 1)
    if current != f"\n{_markdown_table(routes)}\n":
        print(
            f"{MATRIX_DOC.relative_to(ROOT)} is out of date "
            f"({len(routes)} routes in app.py).\n"
            "Run: python tools/extract_routes.py --sync",
            file=sys.stderr,
        )
        return 1
    print(f"{MATRIX_DOC.relative_to(ROOT)} is up to date ({len(routes)} routes)")
    return 0


def main() -> int:
    routes = extract()

    if "--json" in sys.argv:
        print(json.dumps(routes, ensure_ascii=False, indent=2))
        return 0

    if "--sync" in sys.argv:
        return _sync_doc(routes)

    if "--check" in sys.argv:
        return _check_doc(routes)

    if "--markdown" in sys.argv:
        print(_markdown_table(routes))
        return 0

    print(f"total routes: {len(routes)}\n")
    buckets: dict[str, int] = {}
    for route in routes:
        key = effective(route)
        buckets[key] = buckets.get(key, 0) + 1
    print("effective access distribution:")
    for key, count in sorted(buckets.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {key}")

    guarded_in_service = [r for r in routes if r["guarded_in_service"]]
    print(f"\nguard resolved through a service call: {len(guarded_in_service)}")
    for route in guarded_in_service:
        callees = sorted(set(route["guard_source"]) - {route["handler"]})
        print(f"  {route['method']:<6} {route['path']:<48} -> {', '.join(callees)}")

    print("\nroutes with no role guard (authenticated only):")
    for route in routes:
        if not route["roles"]:
            tag = "WRITE" if route["writes"] else "read "
            print(f"  {tag} {route['method']:<6} {route['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
