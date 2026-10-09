#!/usr/bin/env bash
# Rebuild the local test cluster from scratch and hand it to Argo CD.
#
#   ANTHROPIC_API_KEY=sk-ant-... ./bootstrap/bootstrap.sh
#
# Creates secrets (never stored in Git), installs Argo CD with Helm, then applies the
# root app-of-apps. Argo CD then installs everything else from Git, including itself.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER=turnstone-dev
ARGOCD_CHART_VERSION=10.10.0
SECRETS_DIR="$ROOT/.secrets"

die() { echo "error: $*" >&2; exit 1; }
gen() { openssl rand -hex "${1:-24}"; }

[[ -n "${ANTHROPIC_API_KEY:-}" ]] || die "set ANTHROPIC_API_KEY"
for cmd in kind kubectl helm openssl; do command -v "$cmd" >/dev/null || die "$cmd not found"; done

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "==> deleting existing cluster $CLUSTER"
  kind delete cluster --name "$CLUSTER"
fi
for port in 8080 8090 2746 8880 8881 6333 8882 8883 8884; do
  if ss -ltnH "sport = :$port" | grep -q .; then die "localhost:$port is already in use"; fi
done

echo "==> creating cluster $CLUSTER"
kind create cluster --config "$ROOT/bootstrap/kind-config.yaml"
kubectl config use-context "kind-$CLUSTER" >/dev/null

echo "==> creating secrets"
mkdir -p "$SECRETS_DIR" && chmod 700 "$SECRETS_DIR"
ADMIN_PASSWORD="$(gen 18)"
kubectl create namespace turnstone
kubectl -n turnstone create secret generic turnstone-db \
  --from-literal=password="$(gen)" --from-literal=postgres-password="$(gen)"
kubectl -n turnstone create secret generic turnstone-auth \
  --from-literal=TURNSTONE_JWT_SECRET="$(gen 32)"
kubectl -n turnstone create secret generic turnstone-bootstrap \
  --from-literal=ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
( umask 077; printf 'username=admin\npassword=%s\n' "$ADMIN_PASSWORD" > "$SECRETS_DIR/turnstone-admin.txt" )
"$ROOT/bootstrap/infisical-secrets.sh"
"$ROOT/bootstrap/litellm-secrets.sh"

echo "==> installing Argo CD $ARGOCD_CHART_VERSION"
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm repo update argo >/dev/null
helm install argocd argo/argo-cd --version "$ARGOCD_CHART_VERSION" \
  -n argocd --create-namespace -f "$ROOT/values/argocd.yaml" --wait --timeout 10m
( umask 077; printf 'username=admin\npassword=%s\n' \
  "$(kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)" \
  > "$SECRETS_DIR/argocd-admin.txt" )

echo "==> applying root app"
kubectl apply -f "$ROOT/bootstrap/root-app.yaml"

echo "==> waiting for all platform apps to be Synced/Healthy (agent apps are manual-sync) (up to 20 min)"
deadline=$((SECONDS + 1200))
while :; do
  status="$(kubectl -n argocd get applications -l '!aisdlc.io/agent' -o jsonpath='{range .items[*]}{.metadata.name}={.status.sync.status}/{.status.health.status}{"\n"}{end}' 2>/dev/null || true)"
  total=$(grep -c . <<<"$status" || true)
  ready=$(grep -c '=Synced/Healthy$' <<<"$status" || true)
  echo "    $ready/$total ready: $(grep -v '=Synced/Healthy$' <<<"$status" | tr '\n' ' ')"
  [[ $total -ge 18 && $ready -eq $total ]] && break
  (( SECONDS < deadline )) || die "timed out; check: kubectl -n argocd get applications"
  sleep 15
done

cat <<EOF

Done.
  Argo CD          http://localhost:8880   (login: $SECRETS_DIR/argocd-admin.txt)
  Argo Workflows   http://localhost:2746
  Turnstone        http://localhost:8090   (login: $SECRETS_DIR/turnstone-admin.txt)
  Turnstone server http://localhost:8080
  Infisical        http://localhost:8881   (login: $SECRETS_DIR/infisical-admin.txt)
  Qdrant dashboard http://localhost:6333/dashboard
  Helm Dashboard   http://localhost:8882
  Argo WF MCP UI   http://localhost:8883
  LiteLLM UI       http://localhost:8884/ui   (login: $SECRETS_DIR/litellm-admin.txt)
Config sync log:   kubectl -n turnstone logs job/turnstone-config-sync
EOF
