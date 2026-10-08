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
| `external-secrets` | External Secrets Operator | `external-secrets` 2.12.0 |
| `infisical` | Infisical secret manager UI (+ its Postgres/Redis) | `infisical-standalone` 1.11.0 + Bitnami `postgresql`/`redis` + `values/infisical*.yaml` |
| `qdrant` | Qdrant vector DB + dashboard, two Qdrant MCP servers, `rag-ingest` workflow | `qdrant` 1.19.2 + `manifests/rag` |
| `helm-dashboard` | Helm Dashboard (Komodor) UI | `helm-dashboard` 2.0.7 + `manifests/helm-dashboard` |
| `argo-workflows-mcp` | Heapy/argo-workflows-mcp (Argo-specific MCP tools + web UI) behind a supergateway SSE->streamable-HTTP sidecar | `manifests/argo-workflows-mcp` |
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
| Qdrant dashboard | http://localhost:6333/dashboard |
| Argo Workflows MCP UI | http://localhost:8883 (connections, permission settings, audit log) |
| Helm Dashboard | http://localhost:8882 (no login; cluster-wide write access) |
| Infisical | http://localhost:8881 (login: `.secrets/infisical-admin.txt`) |

## Changing Turnstone config

Edit files under `manifests/turnstone-config/config/` and push. The ConfigMap hash changes, Argo CD
syncs, and the `turnstone-config-sync` PostSync Job (`sync.py`) upserts everything by name.

## The admin agent

- **MCP server `argo`**: Turnstone calls it with the `turnstone-mcp` service account's token
  (`cluster-admin`). The MCP pod itself has no permissions and rejects requests without a token.
- **Skill `turnstone-admin`**: lets an agent change Turnstone itself as the `turnstone-agent` admin
  user. Its API token is minted once by the sync Job and kept in Secret `turnstone-agent-token`.

This is a test setup: agents get full control of the cluster and of Turnstone.

## AI SDLC agents

An **agent** is one Argo CD Application in the `aisdlc-agents` project, deployed into the
`aisdlc-agents` namespace, with every part labelled `aisdlc.io/agent=<name>`. It consists of:

1. **Webhook receiver**: an Argo Events EventSource at
   `http://<name>-eventsource-svc.aisdlc-agents:12000/<name>`, plus a Sensor.
2. **Per-event workflow** (WorkflowTemplate `<name>`):
   - starts an **ephemeral Turnstone node** (pod `eph-<workflow>` in the `turnstone` namespace),
     which joins the cluster through the shared database;
   - creates a workstream **pinned to that node** with the agent's persona and the webhook body
     as the first message;
   - waits for the answer (the workflow output `result`);
   - deletes the node.
3. **Persona** `<name>` in Turnstone, created/updated by a PostSync hook on every sync.

Agents talk to Turnstone as the non-admin `aisdlc-runner` user (custom role, token in Secret
`aisdlc-agents/turnstone-runner-token`, minted by the central config sync).

**Add an agent**: create `agents/<name>/values.yaml` (see `agents/issue-triage/`) and push. The
ApplicationSet creates `agent-<name>`; deploy it by clicking **Sync** in Argo CD (project
`aisdlc-agents`). Values reference: `charts/aisdlc-agent/values.yaml`.

## Secrets in Infisical

Human-managed keys live in Infisical (project **SDLC Platform**, environment **dev**) and External
Secrets Operator syncs them into Kubernetes every minute:

| Infisical secret | Kubernetes Secret (key) |
|---|---|
| `ANTHROPIC_API_KEY` | `turnstone/turnstone-bootstrap` (`ANTHROPIC_API_KEY`) |
| `TURNSTONE_ADMIN_PASSWORD` | `turnstone/turnstone-bootstrap` (`ADMIN_PASSWORD`) |
| `TURNSTONE_JWT_SECRET` | `turnstone/turnstone-auth` |
| `TURNSTONE_DB_PASSWORD`, `TURNSTONE_DB_POSTGRES_PASSWORD` | `turnstone/turnstone-db` |

`infisical-config` (PostSync Job) bootstraps Infisical once, seeds these from the Secrets
`bootstrap.sh` created (only if missing in Infisical), and creates the read-only `external-secrets`
machine identity used by the `infisical` ClusterSecretStore. The mapping is in
`manifests/external-secrets-config/turnstone.yaml`.

- **Rotating the Anthropic key**: change it in Infisical; within a minute the Secret updates. Then
  sync `turnstone-config` in Argo CD so the models pick it up.
- **Don't rotate the DB passwords in Infisical**: Postgres keeps the password it was initialised with.
- Generated machine tokens (agent/runner tokens, MCP service-account token) stay in Kubernetes only.

## Knowledge base (RAG)

Qdrant holds the `knowledge` collection; browse and search it in the dashboard
(http://localhost:6333/dashboard). Turnstone agents reach it through two MCP servers
(`qdrant/mcp-server-qdrant` 0.8.1, local FastEmbed `all-MiniLM-L6-v2` embeddings):

| Turnstone MCP server | Tools | For |
|---|---|---|
| `rag` | `qdrant-find` | every agent (read-only) |
| `rag-writer` | `qdrant-find`, `qdrant-store` | curator/ingestion agents |

**Ingest documents**: Argo Workflows UI -> Workflow Templates -> `rag-ingest` -> Submit, with a
public GitHub repo (`owner/name`), branch and optional path prefix. Markdown files are split at
headings and stored with metadata (`source`, `path`, `heading`). Re-running without `reset=true`
adds duplicates.
