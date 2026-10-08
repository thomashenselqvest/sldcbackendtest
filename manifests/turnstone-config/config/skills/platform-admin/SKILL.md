---
name: platform-admin
description: Use this skill to set up, bootstrap, inspect or change anything on this platform with full cluster-admin rights — Kubernetes (kubectl), Argo Workflows (argo), Argo CD (argocd), Helm releases (helm) and Turnstone itself (settings, models, MCP servers, skills, personas, policies, users). Trigger phrases: "deploy", "install", "bootstrap", "set up", "create a workflow/template", "sync/rollback an app", "debug the cluster", "what's running", "configure Turnstone".
version: 1.0.0
---

# Platform administration

You have **cluster-admin** on this Kubernetes cluster and admin rights in Turnstone. Every command
runs without human approval, so you are fully responsible for its effects. Act deliberately:
inspect before you change, change only what the task needs, verify after.

## Setup (run once per bash session)

```bash
export KUBECONFIG=${TURNSTONE_SKILL_DIR}/assets/kubeconfig
kubectl auth whoami   # expect system:serviceaccount:turnstone:turnstone-platform-admin
```

The kubeconfig has two contexts: `platform-admin` (current, namespace `default`) and `argocd`
(namespace `argocd`, for Argo CD core mode). All four CLIs are on PATH.

| CLI | Use | Notes |
|---|---|---|
| `kubectl` | anything in the cluster | v1.37, matches the cluster |
| `argo` | Argo Workflows: `submit`, `submit --from workflowtemplate/<t>`, `list`, `get`, `logs --follow`, `watch`, `retry`, `resubmit`, `stop`, `terminate`, `template`/`cron` create/lint | Uses the Kubernetes API directly (no Argo server needed). Default namespace for workflows is `argo` (`-n argo`); AI SDLC agents live in `aisdlc-agents`. Always `argo lint` before `submit`/`template create`. |
| `argocd` | Argo CD apps: `app list/get/diff/sync/history/rollback`, `app set` | Always run in **core mode**: `argocd --core --kube-context argocd ...` (no login or token). |
| `helm` | Helm releases (v4) | Releases you install here are **not** in Git and disappear on a cluster rebuild. |
| `python3 ${TURNSTONE_SKILL_DIR}/scripts/tsadmin.py` | Turnstone admin API | `endpoints <filter>`, `describe METHOD PATH`, `call METHOD PATH --data JSON`, `whoami`. Same tool as the turnstone-admin skill. |

## How this platform is managed (read before changing anything)

- **GitOps.** Argo CD syncs everything from `https://github.com/thomashenselqvest/sldcbackendtest`
  (branch `main`) with **auto-sync and self-heal**. A manual change to a resource Argo CD manages is
  reverted within minutes. Check first: `kubectl get <kind> <name> -n <ns> -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/tracking-id}'`
  — non-empty means Argo CD owns it.
- You cannot push to that Git repo. For an Argo-managed resource, either
  1. tell the user the exact file change to make in the repo (preferred for permanent changes), or
  2. for a temporary change: `argocd --core --kube-context argocd app set <app> --sync-policy none`,
     make the change, and tell the user that auto-sync is now off for that app.
- New things that aren't in Git (your own namespaces, test workflows, Helm releases, Applications
  you create) are fine to create directly — say clearly that they won't survive a rebuild.
- Turnstone config (models, settings, MCP servers, skills, personas, policies) is also synced from
  Git by the `turnstone-config` app; API changes to those objects are overwritten on the next sync.
- Secrets: human-managed keys come from Infisical via External Secrets (`turnstone-bootstrap`,
  `turnstone-auth`, `turnstone-db`). Never print secret values; refer to them by name.

## Namespaces

`argocd` (Argo CD), `argo` (Argo Workflows), `argo-events`, `aisdlc-agents` (webhook agents),
`turnstone`, `argo-mcp` (MCP servers), `rag` (Qdrant), `infisical`, `external-secrets`,
`helm-dashboard`.

## Working rules

1. **Inspect first**: `kubectl get`, `argo list`, `argocd app get`, `helm list -A` before changes.
2. **Dry-run when available**: `kubectl apply --dry-run=server`, `kubectl diff`, `argo lint`,
   `argocd app diff`, `helm template` / `helm upgrade --dry-run`.
3. **Destructive actions** (`delete`, `uninstall`, `terminate`, `drain`, scaling to 0, deleting
   PVCs or namespaces): only when the task explicitly asks for it, and name exactly what will go.
   Never delete the namespaces `kube-system`, `argocd`, `turnstone` or this skill's service account.
4. **Verify**: after a change, show the resulting state (rollout status, workflow phase, app health).
5. **Report**: end with what changed, where, and whether it is in Git or only in the live cluster.
