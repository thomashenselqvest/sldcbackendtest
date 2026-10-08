#!/usr/bin/env python3
"""Create or update this agent's Turnstone persona (Argo CD PostSync hook)."""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

CONSOLE = os.environ["CONSOLE_URL"].rstrip("/")
persona = json.loads(os.environ["PERSONA_JSON"])
client = httpx.Client(
    base_url=CONSOLE, headers={"Authorization": f"Bearer {os.environ['TURNSTONE_TOKEN']}"}, timeout=30
)


def check(r: httpx.Response) -> dict:
    if r.status_code >= 400:
        sys.exit(f"{r.request.method} {r.request.url.path} -> HTTP {r.status_code}: {r.text[:400]}")
    return r.json() if r.content else {}


for _ in range(60):
    try:
        if client.get("/health").status_code == 200:
            break
    except httpx.HTTPError:
        pass
    time.sleep(5)

listing = check(client.get("/v1/api/admin/personas"))
existing = next((p for p in listing.get("personas", listing if isinstance(listing, list) else [])
                 if p.get("name") == persona["name"]), None)
if existing:
    update = {k: v for k, v in persona.items() if k != "name"}
    check(client.patch(f"/v1/api/admin/personas/{existing['persona_id']}", json=update))
    print(f"persona {persona['name']}: updated", flush=True)
else:
    check(client.post("/v1/api/admin/personas", json=persona))
    print(f"persona {persona['name']}: created", flush=True)
