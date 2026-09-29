"""The route extractor must see writes that live in another module.

Deviation D9 was exactly this: the call graph was built per file, so once a write
moved out of the route module into ``traceability/<domain>.py`` the route's
``Writes`` column went blank. The matrix then claimed that
``POST /api/production-orders`` — which creates a production order — does not
write, and it did so for six routes.

These tests pin the behaviour in both directions. Under-reporting a write is the
failure that happened; over-reporting one is the failure that would make the
column useless, so read routes are checked too.

Most of the file works against a synthetic project built in ``tmp_path``. That
keeps the assertions independent of which domain happens to own an INSERT today —
a test that only asserts on production orders would go quiet the moment those
routes moved again. The last two tests check the real repository, because that is
what CI ships.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import extract_routes  # noqa: E402

AUDIT_MODULE = '''"""Writes an audit row. The INSERT lives one hop further down."""

def record_event(database, event_type):
    database.execute("INSERT INTO audit_events (event_type) VALUES (?)", (event_type,))
    return True
'''

DOMAIN_MODULE = '''"""Domain rules. Imports the write helper from another module."""

from traceability.audit import record_event


def create_thing(database, name):
    database.execute("BEGIN IMMEDIATE")
    database.execute("INSERT INTO things (name) VALUES (?)", (name,))
    record_event(database, "THING_CREATED")
    database.commit()
    return 1


def read_thing(database, thing_id):
    """Pure read: no INSERT, UPDATE, DELETE, REPLACE or executemany."""
    return database.execute("SELECT * FROM things WHERE id = ?", (thing_id,)).fetchone()
'''

BLUEPRINT_MODULE = '''"""HTTP layer."""

from flask import Blueprint, request

from traceability.auth import require_admin
from traceability.db import get_db
from traceability.domain import create_thing, read_thing

things_bp = Blueprint("things", __name__)


@things_bp.post("/api/things")
def make_thing():
    require_admin()
    return create_thing(get_db(), request.json["name"])


@things_bp.get("/api/things/<int:thing_id>")
def show_thing(thing_id):
    require_admin()
    return read_thing(get_db(), thing_id)


@things_bp.get("/api/things/summary")
def summarise_things():
    return {"count": 0}
'''

STUB_MODULES = {
    "auth": "def require_admin():\n    return None\n",
    "db": "def get_db():\n    return None\n",
}


@pytest.fixture
def synthetic_project(tmp_path, monkeypatch):
    """A miniature project whose write lives two modules away from the route."""
    package = tmp_path / "traceability"
    api = package / "api"
    api.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (api / "__init__.py").write_text("", encoding="utf-8")
    (package / "audit.py").write_text(AUDIT_MODULE, encoding="utf-8")
    (package / "domain.py").write_text(DOMAIN_MODULE, encoding="utf-8")
    (api / "things.py").write_text(BLUEPRINT_MODULE, encoding="utf-8")
    for name, source in STUB_MODULES.items():
        (package / f"{name}.py").write_text(source, encoding="utf-8")

    monkeypatch.setattr(extract_routes, "ROOT", tmp_path)
    monkeypatch.setattr(extract_routes, "BLUEPRINT_DIR", api)
    monkeypatch.setattr(extract_routes, "SOURCES", (api / "things.py",))
    return tmp_path


def _route(routes, method, path):
    for route in routes:
        if route["method"] == method and route["path"] == path:
            return route
    raise AssertionError(f"{method} {path} not extracted; got {[r['path'] for r in routes]}")


# ---------------------------------------------------------------------------
# Write detection across module boundaries
# ---------------------------------------------------------------------------


def test_write_two_modules_away_is_detected(synthetic_project):
    """route -> domain.create_thing -> audit.record_event -> INSERT.

    This is the shape that broke: the INSERT is two hops from the handler and in
    a different file, so a per-file graph finds nothing.
    """
    route = _route(extract_routes.extract(), "POST", "/api/things")
    assert route["writes"] is True


def test_write_is_not_reported_as_happening_in_the_handler(synthetic_project):
    """The column is about the route; where the write sits is a separate fact."""
    route = _route(extract_routes.extract(), "POST", "/api/things")
    assert route["write_in_handler"] is False


def test_read_only_route_is_not_flagged(synthetic_project):
    """A GET that only SELECTs must stay blank, or the column means nothing."""
    routes = extract_routes.extract()
    assert _route(routes, "GET", "/api/things/<int:thing_id>")["writes"] is False
    assert _route(routes, "GET", "/api/things/summary")["writes"] is False


def test_guard_found_through_a_domain_module(synthetic_project):
    """The same graph carries the guards, so a cross-module guard is found too."""
    route = _route(extract_routes.extract(), "POST", "/api/things")
    assert route["guards_in_handler"] == ["require_admin"]
    assert "ADMIN" in route["roles"]


# ---------------------------------------------------------------------------
# What the walk must not do
# ---------------------------------------------------------------------------


def test_library_calls_are_not_followed(synthetic_project, monkeypatch):
    """``database.execute`` must not be treated as a project call.

    The module tree only contains traceability/*.py, so an import of flask or
    sqlite3 resolves to nothing and the walk stops. If it did not, every route
    that touches a connection would look like a write.
    """
    index = extract_routes._Index()
    module = index.module("traceability.domain")
    assert module is not None
    assert "execute" not in module.imports
    assert index.body("sqlite3", "connect") is None
    assert index.body("flask", "Blueprint") is None


def test_project_module_name_rejects_non_project_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_routes, "ROOT", tmp_path)
    assert extract_routes._project_module_name(tmp_path / "traceability" / "db.py") == (
        "traceability.db"
    )
    assert extract_routes._project_module_name(tmp_path / "app.py") is None
    assert extract_routes._project_module_name(Path("/usr/lib/python3/sqlite3/__init__.py")) is None


def test_comment_only_guard_mention_does_not_abort(synthetic_project, monkeypatch):
    """A commented-out guard is not a guard, and must not crash the run.

    traceability/auth.py documents the capability guard as
    ``require_capability(Capability.X)`` inside a comment. Once whole modules are
    in scope that text is matched, and X is not a real capability, so the run
    aborted with "unknown capability".
    """
    package = synthetic_project / "traceability"
    (package / "domain.py").write_text(
        DOMAIN_MODULE + "\n# require_capability(Capability.X) is the documented form.\n",
        encoding="utf-8",
    )
    assert _route(extract_routes.extract(), "POST", "/api/things")["writes"] is True


def test_circular_imports_terminate(synthetic_project):
    """Two modules calling each other must not loop forever."""
    package = synthetic_project / "traceability"
    (package / "domain.py").write_text(
        'from traceability.audit import record_event\n'
        "\n"
        "def create_thing(database, name):\n"
        "    return record_event(database, name)\n"
        "\n"
        "def read_thing(database, thing_id):\n"
        "    return None\n",
        encoding="utf-8",
    )
    (package / "audit.py").write_text(
        'from traceability.domain import read_thing\n'
        "\n"
        "def record_event(database, event_type):\n"
        "    read_thing(database, 1)\n"
        '    database.execute("INSERT INTO audit_events (event_type) VALUES (?)", (event_type,))\n'
        "    return True\n",
        encoding="utf-8",
    )
    assert _route(extract_routes.extract(), "POST", "/api/things")["writes"] is True


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO t (a) VALUES (?)",
        "INSERT OR REPLACE INTO t (a) VALUES (?)",
        "REPLACE INTO t (a) VALUES (?)",
        "UPDATE t SET a = ?",
        "DELETE FROM t WHERE a = ?",
        "BEGIN IMMEDIATE",
        "executemany(sql, rows)",
    ],
)
def test_every_write_statement_shape_is_recognised(statement):
    """Each form writes; none of them may fall through the pattern."""
    assert extract_routes.WRITE_RE.search(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM things",
        "SELECT count(*) FROM things WHERE a = ?",
        "BEGIN",
        "COMMIT",
    ],
)
def test_read_statements_are_not_matched(statement):
    assert not extract_routes.WRITE_RE.search(statement)


# ---------------------------------------------------------------------------
# The real repository — this is the part that ships
# ---------------------------------------------------------------------------


def test_routes_whose_writes_live_in_a_domain_module_are_reported():
    """D9's concrete cases, pinned against the real tree.

    Each of these writes in a module other than the route's, which is what the
    per-file graph used to miss. They are listed together so that a regression
    shows up as a name rather than as an unexplained column change.
    """
    routes = extract_routes.extract()
    expected_writing = [
        ("POST", "/api/production-orders"),
        ("POST", "/api/production-orders/batch"),
        ("POST", "/api/purchase-orders/<int:purchase_order_id>/push"),
        ("POST", "/api/auth/logout"),
        ("PUT", "/api/part-types/<int:part_type_id>"),
        ("PUT", "/api/suppliers/<int:supplier_id>"),
    ]
    missing = [
        f"{method} {path}"
        for method, path in expected_writing
        if not _route(routes, method, path)["writes"]
    ]
    assert not missing, f"写操作未被识别（跨模块调用图回归）: {missing}"


def test_no_get_route_is_reported_as_a_write():
    """A GET that mutates would be a bug in the API, not in the analysis.

    Pinning it here means the write detection cannot drift into flagging reads,
    which would quietly drain the column of meaning.
    """
    offenders = [
        f"{route['method']} {route['path']}"
        for route in extract_routes.extract()
        if route["method"] == "GET" and route["writes"]
    ]
    assert not offenders, f"GET 路由被误判为写操作: {offenders}"
