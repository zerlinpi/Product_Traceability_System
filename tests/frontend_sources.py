"""Read the Vue frontend sources for static tests.

The UI lives in ``frontend/apps/web`` (Vue 3 + TypeScript, built into
``static/dist``). Static tests assert on the *sources* — the same way the
previous tests asserted on ``templates/index_v2.html`` and ``static/app_v2.js``
— so they run in the Python CI job without Node.js.

The route table (``src/router/routes.ts``) is parsed with regular expressions.
It is deliberately written as plain object literals (``PAGES`` and
``MENU_GROUPS``) so this stays reliable; ``test_frontend_route_table_parses``
fails loudly if the file ever stops matching the expected shape.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
APP = FRONTEND / "apps" / "web"
SRC = APP / "src"
VIEWS = SRC / "views"
ROUTES_TS = SRC / "router" / "routes.ts"
DIST = ROOT / "static" / "dist"
DIST_INDEX = DIST / "index.html"

ROLES = ("ADMIN", "WAREHOUSE", "OPERATIONS")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def source_files(*suffixes: str, base: Path = SRC) -> list[Path]:
    """Every source file under ``base`` with one of the suffixes, sorted."""
    wanted = suffixes or (".vue", ".ts", ".tsx", ".css", ".scss")
    return sorted(
        path
        for path in base.rglob("*")
        if path.is_file() and path.suffix in wanted and "node_modules" not in path.parts
    )


def all_source_text(*suffixes: str, base: Path = SRC) -> str:
    return "\n".join(read(path) for path in source_files(*suffixes, base=base))


def template_of(vue_source: str) -> str:
    """The ``<template>`` block of a single-file component (outermost one)."""
    start = vue_source.find("<template>")
    end = vue_source.rfind("</template>")
    assert start != -1 and end != -1, "component has no <template> block"
    return vue_source[start : end + len("</template>")]


def style_blocks(vue_source: str) -> str:
    return "\n".join(re.findall(r"<style\b[^>]*>(.*?)</style>", vue_source, flags=re.S))


@cache
def routes_source() -> str:
    return read(ROUTES_TS)


def _block(source: str, start_marker: str) -> str:
    """The text of a top-level ``const X = { ... }`` object literal."""
    start = source.index(start_marker)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[brace : index + 1]
    raise AssertionError(f"unbalanced braces after {start_marker!r}")


def _string_list(text: str) -> list[str]:
    return re.findall(r"'([^']+)'", text)


@cache
def pages() -> dict[str, dict]:
    """``PAGES`` of routes.ts: key -> {title, roles, views, details}."""
    block = _block(routes_source(), "export const PAGES")
    result: dict[str, dict] = {}
    for match in re.finditer(r"\n  '([a-z][a-z-]*)': \{(.*?)\n  \},", block, flags=re.S):
        key, body = match.group(1), match.group(2)
        roles = re.search(r"\n    roles: \[([^\]]*)\]", body)
        title = re.search(r"\n    title: '([^']+)'", body)
        result[key] = {
            "title": title.group(1) if title else "",
            "roles": _string_list(roles.group(1)) if roles else [],
            "views": re.findall(r"import\('@/views/([^']+)'\)", body),
            "details": re.findall(r"name: '([a-z-]+)'", body),
        }
    return result


@cache
def menu_pages() -> dict[str, list[str]]:
    """``MENU_GROUPS`` of routes.ts: role -> page keys in navigation order."""
    block = _block(routes_source(), "const MENU_GROUPS")
    result: dict[str, list[str]] = {}
    for match in re.finditer(r"\n  (ADMIN|WAREHOUSE|OPERATIONS): \[(.*?)\n  \],", block, flags=re.S):
        role, body = match.group(1), match.group(2)
        keys: list[str] = []
        for group in re.findall(r"pages: \[([^\]]*)\]", body):
            keys.extend(_string_list(group))
        result[role] = keys
    return result


def menu_titles(role: str) -> list[str]:
    block = _block(routes_source(), "const MENU_GROUPS")
    match = re.search(rf"\n  {role}: \[(.*?)\n  \],", block, flags=re.S)
    assert match, f"no MENU_GROUPS entry for {role}"
    return re.findall(r"title: '([^']+)'", match.group(1))


def view_dir(page: str) -> Path:
    """The folder holding a page's view and its local components."""
    views = pages()[page]["views"]
    assert views, f"page {page!r} declares no view component"
    return (VIEWS / views[0]).parent


def page_source(page: str) -> str:
    """All sources (.vue / .ts) that make up one page, including its components."""
    return all_source_text(".vue", ".ts", base=view_dir(page))


def page_templates(page: str) -> str:
    return "\n".join(
        template_of(read(path)) for path in source_files(".vue", base=view_dir(page))
    )


def api_module(name: str) -> str:
    return read(SRC / "api" / "modules" / f"{name}.ts")


def api_calls() -> list[tuple[str, str, str]]:
    """Every backend call the API modules can make: (method, path, module).

    ``api.get/post/put/delete('/api/...')`` calls carry their HTTP method;
    bare ``'/api/...'`` literals are URLs handed to ``<img>`` / ``<a href>``
    and are therefore GETs. Template placeholders become ``1`` and query
    strings are dropped, so the result can be matched against Flask's URL map.
    """
    calls: list[tuple[str, str, str]] = []
    literal = r"['`](/api/[^'`]*)"
    method_call = re.compile(r"api\.(get|post|put|delete)(?:<.*?>)?\(\s*" + literal)
    for path in source_files(".ts", base=SRC / "api" / "modules"):
        text = read(path)
        seen: set[tuple[str, str]] = set()
        for match in method_call.finditer(text):
            seen.add((match.group(1).upper(), _normalise(match.group(2))))
        for match in re.finditer(literal, text):
            url = _normalise(match.group(1))
            if not any(existing_url == url for _method, existing_url in seen):
                seen.add(("GET", url))
        calls.extend((method, url, path.stem) for method, url in sorted(seen))
    return calls


def _normalise(url: str) -> str:
    url = re.sub(r"\$\{[^}]*\}", "1", url)
    url = url.split("${", 1)[0]
    return url.split("?", 1)[0]


def static_ids(template: str) -> list[str]:
    """Static ``id="..."`` attributes in a template (bound ``:id`` excluded)."""
    return re.findall(r"(?<![:\w-])id=\"([^\"]+)\"", template)


def opening_tags(template: str, tag: str) -> list[str]:
    """Opening tags ``<Tag ...>`` of a template, respecting quoted attribute
    values (so ``@click="() => x"`` does not end the tag early)."""
    tags: list[str] = []
    pattern = re.compile(rf"<{tag}\b")
    for match in pattern.finditer(template):
        index = match.end()
        quote = ""
        while index < len(template):
            char = template[index]
            if quote:
                if char == quote:
                    quote = ""
            elif char in "\"'":
                quote = char
            elif char == ">":
                tags.append(template[match.start() : index + 1])
                break
            index += 1
    return tags
