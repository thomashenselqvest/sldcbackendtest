#!/usr/bin/env python3
"""Configure Infisical for this cluster (Argo CD PostSync hook, idempotent, stdlib only).

1. Bootstrap the instance once (admin user + org + admin machine identity); the admin
   identity token is kept in Secret infisical/infisical-admin-token.
2. Ensure the project and seed its secrets from the existing Kubernetes Secrets. A secret
   that already exists in Infisical is never overwritten: Infisical is the source of truth.
3. Ensure a read-only machine identity for External Secrets Operator (universal auth) and
   store its client id/secret in Secret external-secrets/infisical-universal-auth.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

INFISICAL = os.environ.get("INFISICAL_URL", "http://infisical-infisical-standalone-infisical:8080").rstrip("/")
CONFIG = json.loads(Path(os.environ.get("CONFIG_FILE", "/config/config.json")).read_text())
NS = os.environ.get("POD_NAMESPACE", "infisical")
SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def log(msg: str) -> None:
    print(f"[infisical-sync] {msg}", flush=True)


def http(method: str, url: str, body: Any = None, token: str = "", ctx: ssl.SSLContext | None = None,
         content_type: str = "application/json", ok404: bool = False) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", content_type)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return None
        sys.exit(f"{method} {url} -> HTTP {e.code}: {e.read().decode()[:500]}")


class Kube:
    def __init__(self) -> None:
        self.token = (SA / "token").read_text().strip()
        self.ctx = ssl.create_default_context(cafile=str(SA / "ca.crt"))
        self.base = "https://kubernetes.default.svc"

    def get_secret(self, ns: str, name: str) -> dict[str, str] | None:
        s = http("GET", f"{self.base}/api/v1/namespaces/{ns}/secrets/{name}", token=self.token, ctx=self.ctx, ok404=True)
        if s is None:
            return None
        return {k: base64.b64decode(v).decode() for k, v in (s.get("data") or {}).items()}

    def create_secret(self, ns: str, name: str, data: dict[str, str]) -> None:
        body = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": ns},
                "type": "Opaque", "stringData": data}
        http("POST", f"{self.base}/api/v1/namespaces/{ns}/secrets", body, token=self.token, ctx=self.ctx)


def api(method: str, path: str, body: Any = None, token: str = "", ok404: bool = False) -> Any:
    return http(method, INFISICAL + path, body, token=token, ok404=ok404)


def wait_ready() -> None:
    for _ in range(120):
        try:
            if http("GET", f"{INFISICAL}/api/status").get("message") == "Ok":
                return
        except SystemExit:
            pass
        except Exception:
            pass
        time.sleep(5)
    sys.exit("Infisical never became ready")


def admin_token(kube: Kube) -> tuple[str, str]:
    """Bootstrap once; afterwards reuse the stored admin identity token."""
    stored = kube.get_secret(NS, "infisical-admin-token")
    if stored:
        log("instance: already bootstrapped (using stored admin identity token)")
        return stored["token"], stored["organizationId"]
    if api("GET", "/api/v1/admin/config")["config"]["initialized"]:
        sys.exit("Infisical is initialized but Secret infisical-admin-token is missing; "
                 "create an admin machine identity token manually and store it there")
    res = api("POST", "/api/v1/admin/bootstrap", {
        "email": os.environ["INFISICAL_ADMIN_EMAIL"],
        "password": os.environ["INFISICAL_ADMIN_PASSWORD"],
        "organization": CONFIG["organization"],
    })
    token = res["identity"]["credentials"]["token"]
    org_id = res["organization"]["id"]
    kube.create_secret(NS, "infisical-admin-token", {"token": token, "organizationId": org_id})
    log(f"instance: bootstrapped (admin {os.environ['INFISICAL_ADMIN_EMAIL']}, org {CONFIG['organization']})")
    return token, org_id


def ensure_project(token: str) -> str:
    slug = CONFIG["project"]["slug"]
    for p in api("GET", "/api/v1/projects", token=token)["projects"]:
        if p.get("slug") == slug:
            log(f"project {slug}: exists")
            return p["id"]
    p = api("POST", "/api/v1/projects", {"projectName": CONFIG["project"]["name"], "slug": slug,
                                         "shouldCreateDefaultEnvs": True}, token=token)["project"]
    log(f"project {slug}: created")
    return p["id"]


def seed_secrets(token: str, project_id: str, kube: Kube) -> None:
    env = CONFIG["project"]["environment"]
    q = urllib.parse.urlencode({"projectId": project_id, "environment": env, "secretPath": "/"})
    have = {s["secretKey"] for s in api("GET", f"/api/v4/secrets?{q}", token=token)["secrets"]}
    for s in CONFIG["secrets"]:
        if s["name"] in have:
            log(f"secret {s['name']}: exists in Infisical (kept)")
            continue
        src = kube.get_secret(s["from"]["namespace"], s["from"]["secret"])
        if not src or s["from"]["key"] not in src:
            sys.exit(f"secret {s['name']}: seed source {s['from']} not found")
        api("POST", f"/api/v4/secrets/{s['name']}", {
            "projectId": project_id, "environment": env, "secretPath": "/",
            "secretValue": src[s["from"]["key"]], "secretComment": s.get("comment", ""),
        }, token=token)
        log(f"secret {s['name']}: seeded from {s['from']['namespace']}/{s['from']['secret']}")


def ensure_reader(token: str, org_id: str, project_id: str, kube: Kube) -> None:
    r = CONFIG["reader"]
    ns, name = r["secret"]["namespace"], r["secret"]["name"]
    ids = api("GET", f"/api/v1/identities?orgId={org_id}", token=token)["identities"]
    # Org identity listing returns memberships with the identity nested (or flat in older APIs).
    ident = next((i.get("identity", i) for i in ids if i.get("identity", i).get("name") == r["name"]), None)
    if ident is None:
        ident = api("POST", "/api/v1/identities", {"name": r["name"], "organizationId": org_id,
                                                   "role": "no-access"}, token=token)["identity"]
        log(f"identity {r['name']}: created")
    iid = ident["id"]
    members = api("GET", f"/api/v1/projects/{project_id}/identity-memberships", token=token)
    if not any(m.get("identity", {}).get("id") == iid for m in members.get("identityMemberships", [])):
        api("POST", f"/api/v1/projects/{project_id}/identity-memberships/{iid}", {"role": r["projectRole"]}, token=token)
        log(f"identity {r['name']}: added to project as {r['projectRole']}")
    ua = api("GET", f"/api/v1/auth/universal-auth/identities/{iid}", token=token, ok404=True)
    if ua is None:
        ua = api("POST", f"/api/v1/auth/universal-auth/identities/{iid}", {}, token=token)
        log(f"identity {r['name']}: universal auth enabled")
    client_id = ua["identityUniversalAuth"]["clientId"]
    if kube.get_secret(ns, name):
        log(f"identity {r['name']}: credentials Secret {ns}/{name} exists")
        return
    cs = api("POST", f"/api/v1/auth/universal-auth/identities/{iid}/client-secrets",
             {"description": "external-secrets operator"}, token=token)
    kube.create_secret(ns, name, {"clientId": client_id, "clientSecret": cs["clientSecret"]})
    log(f"identity {r['name']}: client secret stored in Secret {ns}/{name}")


def main() -> None:
    wait_ready()
    kube = Kube()
    token, org_id = admin_token(kube)
    project_id = ensure_project(token)
    seed_secrets(token, project_id, kube)
    ensure_reader(token, org_id, project_id, kube)
    log("done")


if __name__ == "__main__":
    main()
