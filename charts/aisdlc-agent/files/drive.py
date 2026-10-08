#!/usr/bin/env python3
"""Drive one agent run: wait for the ephemeral node, start a pinned workstream with the
agent's persona, wait for its answer, write it to /tmp/result.txt (workflow output)."""

from __future__ import annotations

import os
import sys
import time

import httpx
from turnstone.sdk import TurnstoneConsole

CONSOLE = os.environ["CONSOLE_URL"]
NODE_ID = os.environ["NODE_ID"]
TOKEN = os.environ["TURNSTONE_TOKEN"]
TIMEOUT = int(os.environ.get("TIMEOUT_SECONDS", "900"))


def log(msg: str) -> None:
    print(f"[drive] {msg}", flush=True)


def wait_for_node(console: TurnstoneConsole) -> None:
    deadline = time.time() + 300
    while time.time() < deadline:
        nodes = {n.node_id: n for n in console.nodes(limit=500).nodes}
        node = nodes.get(NODE_ID)
        if node and node.reachable:
            log(f"node {NODE_ID} joined at {node.server_url}")
            return
        time.sleep(5)
    sys.exit(f"node {NODE_ID} never became reachable")


def wait_for_answer(node_url: str, ws_id: str) -> str:
    client = httpx.Client(base_url=node_url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        time.sleep(5)
        states = {w["ws_id"]: w["state"] for w in client.get("/v1/api/workstreams").json()["workstreams"]}
        state = states.get(ws_id, "")
        if state == "error":
            sys.exit(f"workstream {ws_id} ended in error")
        if state == "attention":
            log("workstream is waiting for an approval (auto_approve off?)")
        if state != "idle":
            continue
        history = client.get(f"/v1/api/workstreams/{ws_id}/history").json().get("messages", [])
        # Idle with a final assistant message (no pending tool calls) means the turn is done.
        if history and history[-1].get("role") == "assistant" and not history[-1].get("tool_calls"):
            return str(history[-1].get("content") or "")
    sys.exit(f"workstream {ws_id} did not finish within {TIMEOUT}s")


def main() -> None:
    console = TurnstoneConsole(CONSOLE, token=TOKEN)
    wait_for_node(console)
    message = f"{os.environ.get('INSTRUCTIONS', '')}\n\n{os.environ.get('PAYLOAD', '{}')}".strip()
    resp = console.route_create_workstream(
        name=f"{os.environ['AGENT']} {os.environ['WORKFLOW']}",
        model=os.environ.get("MODEL", ""),
        persona=os.environ["PERSONA"],
        initial_message=message,
        auto_approve=os.environ.get("AUTO_APPROVE", "true").lower() == "true",
        target_node=NODE_ID,
        required_node_id=NODE_ID,
    )
    log(f"workstream {resp.ws_id} on {resp.node_id} ({resp.node_url})")
    answer = wait_for_answer(resp.node_url, resp.ws_id)
    with open("/tmp/result.txt", "w") as f:
        f.write(answer)
    log("answer:\n" + answer)
    console.close()


if __name__ == "__main__":
    main()
