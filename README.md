# sldcbackendtest — local GitOps test cluster

A single-node kind cluster where Argo CD installs and manages, from this repo:

| App | What | Source |
|---|---|---|
| `argocd` | Argo CD itself (self-managed) | `argo/argo-cd` 10.10.0 + `values/argocd.yaml` |
| `argo-workflows` | Argo Workflows (no artifact store) | `argo/argo-workflows` 2.0.11 + `values/argo-workflows.yaml` |
| `argo-events` | Argo Events | `argo/argo-events` 2.4.27 + `values/argo-events.yaml` |
| `argo-config` | EventBus, sensor RBAC, webhook example | `manifests/argo` |
| `turnstone` | Turnstone 1.8.5 + Postgres | `charts/turnstone` (vendored, see `PATCHES.md`) + `manifests/turnstone` |
| `argo-mcp` | Kubernetes MCP server for Turnstone agents | `ghcr.io/containers/charts/kubernetes-mcp-server` 0.1.0 + `manifests/argo-mcp` |
| `turnstone-config` | Models, settings, MCP registration, admin agent + skill, policies | `manifests/turnstone-config` |

## Bootstrap

```bash
ANTHROPIC_API_KEY=sk-ant-... ./bootstrap/bootstrap.sh
```

Deletes and recreates the `turnstone-dev` kind cluster, creates the secrets (never in Git; generated
passwords land in `.secrets/`), installs Argo CD and applies `bootstrap/root-app.yaml`.

| UI | URL |
|---|---|
| Argo CD | http://localhost:8880 |
| Argo Workflows | http://localhost:2746 |
| Turnstone console | http://localhost:8090 |
| Turnstone server | http://localhost:8080 |

## Changing Turnstone config

Edit files under `manifests/turnstone-config/config/` and push. The ConfigMap hash changes, Argo CD
syncs, and the `turnstone-config-sync` PostSync Job (`sync.py`) upserts everything by name.

## The admin agent

- **MCP server `argo`**: Turnstone calls it with the `turnstone-mcp` service account's token
  (`cluster-admin`). The MCP pod itself has no permissions and rejects requests without a token.
- **Skill `turnstone-admin`**: lets an agent change Turnstone itself as the `turnstone-agent` admin
  user. Its API token is minted once by the sync Job and kept in Secret `turnstone-agent-token`.

This is a test setup: agents get full control of the cluster and of Turnstone.
