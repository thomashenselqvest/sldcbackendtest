#!/usr/bin/env python3
"""Sync declarative Turnstone config (config/*.yaml, skill files) into a running Turnstone.

Runs as an Argo CD PostSync hook Job on every sync, so every step is idempotent:
objects are upserted by name and existing ones are overwritten with the Git version.

Config is mounted flat from a ConfigMap at CONFIG_DIR. Skill files use "--" for "/" in
their keys, e.g. "skill--turnstone-admin--scripts--tsadmin.py".
"""

from __future__ import annotations

import os
import re
import secrets
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONSOLE = os.environ.get("TURNSTONE_CONSOLE_URL", "http://turnstone-console:8090").rstrip("/")
SERVER = os.environ.get("TURNSTONE_SERVER_URL", "http://turnstone-server:8080").rstrip("/")
NAMESPACE = os.environ.get("POD_NAMESPACE", "turnstone")
SERVER_DEPLOYMENT = os.environ.get("SERVER_DEPLOYMENT", "turnstone-server")
AGENT_TOKEN_SECRET = os.environ.get("AGENT_TOKEN_SECRET", "turnstone-agent-token")
ADMIN_USER = "admin"
AGENT_USER = "turnstone-agent"

_ID_RE = re.compile(r"\b[0-9a-f]{32}\b")


def log(msg: str) -> None:
    print(f"[sync] {msg}", flush=True)


def load(name: str) -> Any:
    path = CONFIG_DIR / name
    return yaml.safe_load(path.read_text()) if path.exists() else None


def expand(value: Any) -> Any:
    """Substitute ${VAR} from the environment in strings, recursively."""
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], value)
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v) for v in value]
    return value


# --------------------------------------------------------------------------- HTTP


class Api:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jwt = ""

    def req(self, method: str, path: str, body: Any = None, ok404: bool = False) -> Any:
        headers = {"Authorization": f"Bearer {self.jwt}"} if self.jwt else {}
        r = httpx.request(method, self.base + path, json=body, headers=headers, timeout=60)
        if ok404 and r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise SystemExit(f"{method} {path} -> HTTP {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else {}

    def login(self, username: str, password: str) -> None:
        r = self.req("POST", "/v1/api/auth/login", {"username": username, "password": password})
        self.jwt = r.get("jwt") or r.get("token") or ""
        if not self.jwt:
            raise SystemExit(f"login as {username} returned no token")


def items(resp: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(resp, list):
        return resp
    for k in keys:
        if isinstance(resp.get(k), list):
            return resp[k]
    return []


def wait_for(url: str, what: str, timeout: int = 600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=5).status_code == 200:
                log(f"{what} is up")
                return
        except httpx.HTTPError:
            pass
        time.sleep(5)
    raise SystemExit(f"timed out waiting for {what} ({url})")


# --------------------------------------------------------------------------- Kubernetes


class Kube:
    SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")

    def __init__(self) -> None:
        self.client = httpx.Client(
            base_url="https://kubernetes.default.svc",
            verify=str(self.SA / "ca.crt"),
            headers={"Authorization": f"Bearer {(self.SA / 'token').read_text().strip()}"},
            timeout=30,
        )

    def get_secret(self, name: str, namespace: str = NAMESPACE) -> dict[str, Any] | None:
        r = self.client.get(f"/api/v1/namespaces/{namespace}/secrets/{name}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def create_secret(self, name: str, data: dict[str, str], namespace: str = NAMESPACE) -> None:
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace},
            "type": "Opaque",
            "stringData": data,
        }
        self.client.post(f"/api/v1/namespaces/{namespace}/secrets", json=body).raise_for_status()

    def restart_deployment(self, name: str) -> None:
        patch = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"turnstone-sync/restartedAt": str(int(time.time()))}}
                }
            }
        }
        self.client.patch(
            f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{name}",
            json=patch,
            headers={"Content-Type": "application/strategic-merge-patch+json"},
        ).raise_for_status()


# --------------------------------------------------------------------------- users


def turnstone_admin(*args: str) -> str:
    out = subprocess.run(["turnstone-admin", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"turnstone-admin {args[0]} failed: {out.stderr.strip()[-500:]}")
    # Structured JSON log lines go to stdout too; keep only the human-readable lines.
    return "\n".join(line for line in out.stdout.splitlines() if not line.startswith("{"))


def ensure_admin(username: str, password: str, name: str) -> str:
    out = turnstone_admin("create-admin", "--username", username, "--name", name, "--password", password)
    match = _ID_RE.search(out)
    if not match:
        raise SystemExit(f"could not parse user id for {username}: {out}")
    return match.group(0)


def ensure_agent_token(kube: Kube) -> str:
    """Agent user + API token. create-token isn't idempotent, so a Secret is the guard."""
    password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(40))
    user_id = ensure_admin(AGENT_USER, password, "Turnstone Agent")
    existing = kube.get_secret(AGENT_TOKEN_SECRET)
    if existing:
        import base64

        log(f"agent token: reusing Secret {AGENT_TOKEN_SECRET}")
        return base64.b64decode(existing["data"]["token"]).decode()
    out = turnstone_admin(
        "create-token", "--user", user_id, "--name", "turnstone-admin-skill", "--scopes", "read,write,approve"
    )
    token = next((line.split("Token:", 1)[1].strip() for line in out.splitlines() if "Token:" in line), "")
    if not token:
        raise SystemExit("create-token printed no token")
    kube.create_secret(AGENT_TOKEN_SECRET, {"token": token, "user_id": user_id})
    log(f"agent token: minted and stored in Secret {AGENT_TOKEN_SECRET}")
    return token


# --------------------------------------------------------------------------- config


def sync_models(api: Api) -> None:
    models = load("models.yaml") or []
    existing = {
        d["alias"]: d
        for d in items(api.req("GET", "/v1/api/admin/model-definitions"), "definitions", "models", "items")
    }
    for m in models:
        body = {k: v for k, v in m.items() if k != "api_key_env"}
        if m.get("api_key_env"):
            body["api_key"] = os.environ[m["api_key_env"]]
        body.setdefault("enabled", True)
        cur = existing.get(m["alias"])
        if cur:
            api.req("PUT", f"/v1/api/admin/model-definitions/{cur['definition_id']}", body)
            log(f"model {m['alias']}: updated")
        else:
            api.req("POST", "/v1/api/admin/model-definitions", body)
            log(f"model {m['alias']}: created")
    result = api.req("POST", "/v1/api/admin/model-definitions/reload", {})
    for node, r in (result.get("results") or {}).items():
        missing = {m["alias"] for m in models} - set(r.get("aliases", []))
        if missing:
            raise SystemExit(f"node {node} did not load models {sorted(missing)}: {r}")
        log(f"models loaded on {node}: {sorted(r.get('aliases', []))}")


def sync_settings(api: Api) -> bool:
    cfg = load("settings.yaml") or {}
    restart_keys = set(cfg.get("restart_keys") or [])
    current = {s["key"]: s.get("value") for s in items(api.req("GET", "/v1/api/admin/settings"), "settings")}
    needs_restart = False
    for key, value in (cfg.get("settings") or {}).items():
        if current.get(key) == value:
            continue
        api.req("PUT", f"/v1/api/admin/settings/{key}", {"value": value})
        log(f"setting {key}: {current.get(key)!r} -> {value!r}")
        needs_restart |= key in restart_keys
    return needs_restart


def sync_mcp(api: Api) -> None:
    servers = expand(load("mcp-servers.yaml") or [])
    existing = {s["name"]: s for s in items(api.req("GET", "/v1/api/admin/mcp-servers"), "servers")}
    for s in servers:
        s.setdefault("enabled", True)
        cur = existing.get(s["name"])
        if cur:
            api.req("PUT", f"/v1/api/admin/mcp-servers/{cur['server_id']}", s)
            log(f"mcp {s['name']}: updated")
        else:
            api.req("POST", "/v1/api/admin/mcp-servers", s)
            log(f"mcp {s['name']}: created")
    api.req("POST", "/v1/api/admin/mcp-servers/reload", {})
    for _ in range(24):
        time.sleep(5)
        live = {s["name"]: s for s in items(api.req("GET", "/v1/api/admin/mcp-servers"), "servers")}
        status = {
            name: [n for n in (live.get(name, {}).get("status") or {}).values()]
            for name in (s["name"] for s in servers)
        }
        if all(nodes and all(n.get("connected") and n.get("tools", 0) > 0 for n in nodes) for nodes in status.values()):
            for name, nodes in status.items():
                log(f"mcp {name}: connected on {len(nodes)} node(s), {nodes[0].get('tools')} tools")
            return
    raise SystemExit(f"MCP servers not connected: {status}")


def platform_admin_kubeconfig() -> str:
    """kubeconfig for the turnstone-platform-admin service account (mounted token + CA).

    Two contexts: platform-admin (current, namespace default) and argocd (namespace argocd,
    for `argocd --core --kube-context argocd`).
    """
    sa = Path(os.environ.get("PLATFORM_ADMIN_TOKEN_DIR", "/platform-admin"))
    if not (sa / "token").exists():
        return ""
    import base64

    ca = base64.b64encode((sa / "ca.crt").read_bytes()).decode()
    token = (sa / "token").read_text().strip()
    cfg = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "in-cluster", "cluster": {
            "server": "https://kubernetes.default.svc", "certificate-authority-data": ca}}],
        "users": [{"name": "turnstone-platform-admin", "user": {"token": token}}],
        "contexts": [
            {"name": "platform-admin", "context": {"cluster": "in-cluster", "user": "turnstone-platform-admin", "namespace": "default"}},
            {"name": "argocd", "context": {"cluster": "in-cluster", "user": "turnstone-platform-admin", "namespace": "argocd"}},
        ],
        "current-context": "platform-admin",
    }
    return yaml.safe_dump(cfg, sort_keys=False)


def sync_personas(api: Api) -> None:
    personas = load("personas.yaml") or []
    existing = {p["name"]: p for p in items(api.req("GET", "/v1/api/admin/personas"), "personas")}
    for p in personas:
        cur = existing.get(p["name"])
        if cur:
            api.req("PATCH", f"/v1/api/admin/personas/{cur['persona_id']}", {k: v for k, v in p.items() if k != "name"})
            log(f"persona {p['name']}: updated")
        else:
            api.req("POST", "/v1/api/admin/personas", p)
            log(f"persona {p['name']}: created")


def sync_service_users(api: Api, kube: Kube) -> None:
    """Non-admin users with a custom role; token minted once into a Kubernetes Secret."""
    users = load("service-users.yaml") or []
    if not users:
        return
    roles = {r["name"]: r for r in items(api.req("GET", "/v1/api/admin/roles"), "roles")}
    accounts = {u["username"]: u for u in items(api.req("GET", "/v1/api/admin/users"), "users")}
    for su in users:
        role = dict(su["role"])
        role["permissions"] = ",".join(role["permissions"])
        cur = roles.get(role["name"])
        if cur:
            role_id = cur["role_id"]
            api.req("PUT", f"/v1/api/admin/roles/{role_id}", role)
        else:
            role_id = api.req("POST", "/v1/api/admin/roles", role)["role_id"]
        log(f"role {role['name']}: {'updated' if cur else 'created'}")

        user = accounts.get(su["username"])
        if user:
            user_id = user["user_id"]
        else:
            password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(40))
            user_id = api.req(
                "POST",
                "/v1/api/admin/users",
                {"username": su["username"], "display_name": su.get("display_name", ""), "password": password},
            )["user_id"]
            log(f"user {su['username']}: created")
        assigned = {r.get("role_id") for r in items(api.req("GET", f"/v1/api/admin/users/{user_id}/roles"), "roles")}
        if role_id not in assigned:
            api.req("POST", f"/v1/api/admin/users/{user_id}/roles", {"role_id": role_id})
            log(f"user {su['username']}: role {role['name']} assigned")

        tok = su["token"]
        ns, name = tok["secret"]["namespace"], tok["secret"]["name"]
        if kube.get_secret(name, ns):
            log(f"user {su['username']}: token Secret {ns}/{name} exists")
            continue
        raw = api.req(
            "POST", f"/v1/api/admin/users/{user_id}/tokens", {"name": f"{name}", "scopes": tok["scopes"]}
        )
        token = raw.get("token") or raw.get("raw_token") or ""
        if not token:
            raise SystemExit(f"token response for {su['username']} had no token field: {sorted(raw)}")
        kube.create_secret(name, {"token": token, "user_id": user_id}, namespace=ns)
        log(f"user {su['username']}: token minted into Secret {ns}/{name}")


def sync_policies(api: Api) -> None:
    existing = {p["name"]: p for p in items(api.req("GET", "/v1/api/admin/policies"), "policies", "items")}
    for p in load("policies.yaml") or []:
        cur = existing.get(p["name"])
        if cur:
            pid = cur.get("policy_id") or cur.get("id")
            api.req("PUT", f"/v1/api/admin/policies/{pid}", p)
            log(f"policy {p['name']}: updated")
        else:
            api.req("POST", "/v1/api/admin/policies", p)
            log(f"policy {p['name']}: created")


def skill_files(name: str) -> dict[str, str]:
    prefix = f"skill--{name}--"
    return {
        f.name[len(prefix):].replace("--", "/"): f.read_text()
        for f in CONFIG_DIR.iterdir()
        if f.name.startswith(prefix)
    }


def sync_skills(api: Api, generated: dict[str, str]) -> None:
    existing = {s["name"]: s for s in items(api.req("GET", "/v1/api/admin/skills"), "skills")}
    for sk in load("skills.yaml") or []:
        files = skill_files(sk["name"])
        parsed = api.req("POST", "/v1/api/admin/skills/parse", {"raw": files.pop("SKILL.md")})
        body = {k: v for k, v in parsed.items() if v not in (None, "", [], {})}
        body.setdefault("category", "custom")
        body["activation"] = sk.get("activation", "named")
        cur = existing.get(sk["name"])
        if cur:
            sid = cur["template_id"]
            api.req("PUT", f"/v1/api/admin/skills/{sid}", body)
            log(f"skill {sk['name']}: updated")
        else:
            sid = api.req("POST", "/v1/api/admin/skills", body)["template_id"]
            log(f"skill {sk['name']}: created")
        for path, source in (sk.get("generated_assets") or {}).items():
            files[path] = generated.get(source, source)
        have = {
            r["path"]: r
            for r in items(api.req("GET", f"/v1/api/admin/skills/{sid}/resources"), "resources")
        }
        for path, content in files.items():
            cur_res = have.pop(path, None)
            if cur_res is not None and cur_res.get("content") == content:
                continue
            if cur_res is not None:
                api.req("DELETE", f"/v1/api/admin/skills/{sid}/resources/{path}")
            api.req("POST", f"/v1/api/admin/skills/{sid}/resources", {"path": path, "content": content})
            log(f"skill {sk['name']}: resource {path} {'replaced' if cur_res else 'added'}")
        for path in have:  # files removed from Git
            api.req("DELETE", f"/v1/api/admin/skills/{sid}/resources/{path}")
            log(f"skill {sk['name']}: resource {path} removed")


# --------------------------------------------------------------------------- main


def main() -> None:
    wait_for(f"{SERVER}/health", "turnstone server")
    wait_for(f"{CONSOLE}/health", "turnstone console")
    kube = Kube()

    ensure_admin(ADMIN_USER, os.environ["ADMIN_PASSWORD"], "Admin")
    log(f"user {ADMIN_USER}: ok")
    api = Api(CONSOLE)
    api.login(ADMIN_USER, os.environ["ADMIN_PASSWORD"])

    sync_models(api)
    needs_restart = sync_settings(api)
    sync_mcp(api)
    agent_token = ensure_agent_token(kube)
    sync_skills(api, {"agent_token": agent_token, "platform_admin_kubeconfig": platform_admin_kubeconfig()})
    sync_personas(api)
    sync_policies(api)
    sync_service_users(api, kube)

    if needs_restart:
        kube.restart_deployment(SERVER_DEPLOYMENT)
        log(f"restarted deployment/{SERVER_DEPLOYMENT} for startup-only settings")
    log("done")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None):
            print(f"[sync] FAILED: {e.code}", file=sys.stderr, flush=True)
        raise
