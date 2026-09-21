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

# Routes are registered in more than one module. ``app.py`` holds the bulk;
# ``traceability/auth.py`` registers the authentication and user-management
# routes inside ``initialize_auth()``. Scanning only app.py silently under-counts
# the API surface (and hides the auth routes from the permission matrix).
SOURCES: tuple[Path, ...] = (
    ROOT / "app.py",
    ROOT / "traceability" / "auth.py",
)

ROUTE_RE = re.compile(r'^    @app\.(?P<method>get|post|put|delete|patch|route)\((?P<args>.*)\)\s*$')
DEF_RE = re.compile(r"^    def (?P<name>\w+)\(")
CALL_RE = re.compile(r"\b(?P<name>[a-z_][a-z0-9_]*)\s*\(")

# Guards live in traceability/auth.py; these are the leaves of the call graph.
GUARD_ROLE = {
    "require_admin": "admin",
    "require_admin_or_warehouse": "admin|warehouse",
    "require_warehouse": "admin|warehouse",
    "require_operations": "admin|operations",
}
GUARD_SCOPE = {
    "require_product_model_access": "scope:product",
    "require_supplier_access": "scope:supplier",
}

# Scope guards are no-ops because auth.current_operator_id() returns None.
SCOPE_GUARDS_ARE_NOOP = True

# Paths exempt from the global authentication gate in auth.before_request.
# Requests to these never reach the "must be logged in" check.
GATE_EXEMPT = {"/api/health", "/api/auth/login"}

WRITE_RE = re.compile(r"BEGIN IMMEDIATE|INSERT INTO|UPDATE \w+ SET|DELETE FROM")
READ_METHODS = {"GET"}


def _blocks(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Map nested function name -> (start, end) line index inside create_app()."""
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = DEF_RE.match(line)
        if match:
            starts.append((index, match.group("name")))
    blocks: dict[str, tuple[int, int]] = {}
    for position, (start, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        blocks[name] = (start, end)
    return blocks


def _calls(text: str, known: set[str]) -> set[str]:
    return {m.group("name") for m in CALL_RE.finditer(text) if m.group("name") in known}


def _resolve_guards(
    name: str, blocks: dict[str, tuple[int, int]], lines: list[str], known: set[str]
) -> tuple[list[str], set[str]]:
    """Transitive guards for ``name``. Returns (guards, functions_visited)."""
    guards: list[str] = []
    visited: set[str] = set()
    queue = [name]
    while queue:
        current = queue.pop(0)
        if current in visited or current not in blocks:
            continue
        visited.add(current)
        start, end = blocks[current]
        body = "\n".join(lines[start:end])
        for guard in {*GUARD_ROLE, *GUARD_SCOPE}:
            if re.search(rf"\b{guard}\s*\(", body) and guard not in guards:
                guards.append(guard)
        queue.extend(_calls(body, known) - visited)
    return guards, visited


def extract() -> list[dict[str, object]]:
    routes: list[dict[str, object]] = []
    for source in SOURCES:
        routes.extend(_extract_file(source))
    return routes


def _extract_file(source: Path) -> list[dict[str, object]]:
    lines = source.read_text(encoding="utf-8").splitlines()
    blocks = _blocks(lines)
    known = set(blocks)
    routes: list[dict[str, object]] = []
    for index, line in enumerate(lines):
        route = ROUTE_RE.match(line)
        if not route:
            continue
        args = route.group("args")
        path_match = re.match(r'\s*["\']([^"\']+)["\']', args)
        path = path_match.group(1) if path_match else args.strip()
        definition = DEF_RE.match(lines[index + 1]) if index + 1 < len(lines) else None
        if not definition:
            continue
        handler = definition.group("name")
        guards, visited = _resolve_guards(handler, blocks, lines, known)
        start, end = blocks[handler]
        body = "\n".join(lines[start:end])
        # Write detection follows the same call graph.
        write_body = body
        for callee in visited - {handler}:
            callee_start, callee_end = blocks[callee]
            write_body += "\n" + "\n".join(lines[callee_start:callee_end])
        roles = sorted({GUARD_ROLE[g] for g in guards if g in GUARD_ROLE})
        scopes = sorted({GUARD_SCOPE[g] for g in guards if g in GUARD_SCOPE})
        own_guards = [g for g in guards if re.search(rf"\b{g}\s*\(", body)]
        routes.append(
            {
                "method": route.group("method").upper(),
                "path": path,
                "handler": handler,
                "source": str(source.relative_to(ROOT)).replace("\\", "/"),
                "line": index + 1,
                "roles": roles,
                "scopes": scopes,
                "guard_source": sorted(visited),
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


def effective(route: dict[str, object]) -> str:
    """Human-readable effective access, e.g. 'ADMIN + WAREHOUSE'."""
    roles = route["roles"]  # type: ignore[assignment]
    scopes = route["scopes"]  # type: ignore[assignment]
    names: list[str] = []
    if route.get("public"):
        names.append("public")
    if "admin" in roles:
        names.append("ADMIN")
    for role in roles:
        if role.startswith("admin|"):
            names.append(role.split("|", 1)[1].upper())
    if scopes:
        names.append("+".join(scopes) + ("(NOOP)" if SCOPE_GUARDS_ARE_NOOP else ""))
    if not names:
        return "any authenticated"
    if names == ["public"]:
        return "public (no login)"
    return " + ".join(names)


BEGIN_MARKER = "<!-- BEGIN GENERATED ROUTE TABLE -->"
END_MARKER = "<!-- END GENERATED ROUTE TABLE -->"
MATRIX_DOC = ROOT / "docs" / "PERMISSION_MATRIX.md"


def _markdown_table(routes: list[dict[str, object]]) -> str:
    lines = [
        "| Method | Path | Effective access | Guard location | Writes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for route in sorted(routes, key=lambda r: (str(r["path"]), str(r["method"]))):
        where = (
            "handler"
            if not route["guarded_in_service"]
            else "service: "
            + ", ".join(sorted(set(route["guard_source"]) - {route["handler"]}))[:70]
        )
        lines.append(
            f"| `{route['method']}` | `{route['path']}` | {effective(route)} | "
            f"{where} | {'yes' if route['writes'] else ''} |"
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


def main() -> int:
    routes = extract()

    if "--json" in sys.argv:
        print(json.dumps(routes, ensure_ascii=False, indent=2))
        return 0

    if "--sync" in sys.argv:
        return _sync_doc(routes)

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
