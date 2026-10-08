#!/usr/bin/env python3
"""Admin helper for the turnstone-admin skill.

Reads credentials from the skill's resource files (or env vars), discovers
endpoints from the console/server OpenAPI specs, and calls them.

  tsadmin.py endpoints [FILTER] [--server]     list METHOD PATH summary
  tsadmin.py describe METHOD PATH [--server]   parameters + request body schema
  tsadmin.py call METHOD PATH [--data JSON|@file] [--query k=v ...] [--server]
  tsadmin.py whoami

Python use (typed SDK clients, same credentials):

  import sys; sys.path.insert(0, "<skill dir>/scripts")
  from tsadmin import console, server, call
  with console() as c: print(c.list_settings())
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

_SKILL_DIR = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    p = _SKILL_DIR / "assets" / name
    return p.read_text().strip() if p.exists() else ""


TOKEN = os.environ.get("TURNSTONE_ADMIN_TOKEN") or _read("token")
CONSOLE_URL = (
    os.environ.get("TURNSTONE_CONSOLE_URL") or _read("console_url") or "http://turnstone-console:8090"
).rstrip("/")
SERVER_URL = (
    os.environ.get("TURNSTONE_SERVER_URL") or _read("server_url") or "http://turnstone-server:8080"
).rstrip("/")


def _base(use_server: bool) -> str:
    return SERVER_URL if use_server else CONSOLE_URL


def console():
    """Typed SDK client for the console (admin) API."""
    from turnstone.sdk import TurnstoneConsole

    return TurnstoneConsole(CONSOLE_URL, token=TOKEN)


def server():
    """Typed SDK client for the server (workstream) API."""
    from turnstone.sdk import TurnstoneServer

    return TurnstoneServer(SERVER_URL, token=TOKEN)


def call(
    method: str,
    path: str,
    data: Any = None,
    query: dict[str, str] | None = None,
    use_server: bool = False,
) -> Any:
    """Call any endpoint. Raises SystemExit with the error body on HTTP >= 400."""
    if not TOKEN:
        sys.exit("No admin token: assets/token missing and TURNSTONE_ADMIN_TOKEN unset")
    r = httpx.request(
        method.upper(),
        _base(use_server) + path,
        json=data,
        params=query,
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=60,
    )
    try:
        body = r.json()
    except ValueError:
        body = r.text
    if r.status_code >= 400:
        sys.exit(f"HTTP {r.status_code} {method.upper()} {path}: {json.dumps(body) if not isinstance(body, str) else body}")
    return body


def _spec(use_server: bool) -> dict[str, Any]:
    return httpx.get(_base(use_server) + "/openapi.json", timeout=30).json()


def _resolve(spec: dict[str, Any], node: Any, depth: int = 0) -> Any:
    """Inline $refs a few levels deep so schemas are readable."""
    if depth > 4:
        return node
    if isinstance(node, dict):
        if "$ref" in node:
            target: Any = spec
            for part in node["$ref"].lstrip("#/").split("/"):
                target = target.get(part, {})
            return _resolve(spec, target, depth + 1)
        return {k: _resolve(spec, v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(spec, v, depth + 1) for v in node]
    return node


def cmd_endpoints(args: argparse.Namespace) -> None:
    spec = _spec(args.server)
    needle = (args.filter or "").lower()
    for path, ops in sorted(spec.get("paths", {}).items()):
        for method, op in ops.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            summary = (op.get("summary") or op.get("description") or "").splitlines()[0][:90] if (op.get("summary") or op.get("description")) else ""
            line = f"{method.upper():6} {path}  {summary}"
            if needle in line.lower():
                print(line)


def cmd_describe(args: argparse.Namespace) -> None:
    spec = _spec(args.server)
    op = spec.get("paths", {}).get(args.path, {}).get(args.method.lower())
    if op is None:
        sys.exit(f"No {args.method.upper()} {args.path} in spec; try: tsadmin.py endpoints {args.path.split('/')[-1]}")
    out = {
        "summary": op.get("summary"),
        "description": op.get("description"),
        "parameters": _resolve(spec, op.get("parameters", [])),
        "requestBody": _resolve(spec, op.get("requestBody")),
    }
    print(json.dumps(out, indent=2))


def cmd_call(args: argparse.Namespace) -> None:
    data = None
    if args.data:
        raw = Path(args.data[1:]).read_text() if args.data.startswith("@") else args.data
        data = json.loads(raw)
    query = dict(q.split("=", 1) for q in args.query or [])
    result = call(args.method, args.path, data, query or None, args.server)
    print(json.dumps(result, indent=2) if not isinstance(result, str) else result)


def cmd_whoami(args: argparse.Namespace) -> None:
    print(json.dumps(call("GET", "/v1/api/auth/whoami", use_server=True), indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("endpoints")
    e.add_argument("filter", nargs="?")
    d = sub.add_parser("describe")
    d.add_argument("method")
    d.add_argument("path")
    c = sub.add_parser("call")
    c.add_argument("method")
    c.add_argument("path")
    c.add_argument("--data", help="JSON body, or @file")
    c.add_argument("--query", action="append", help="k=v query parameter (repeatable)")
    sub.add_parser("whoami")
    for sp in (e, d, c):
        sp.add_argument("--server", action="store_true", help="target the server API instead of the console")
    args = p.parse_args()
    {"endpoints": cmd_endpoints, "describe": cmd_describe, "call": cmd_call, "whoami": cmd_whoami}[args.cmd](args)


if __name__ == "__main__":
    main()
