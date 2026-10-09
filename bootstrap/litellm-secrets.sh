#!/usr/bin/env bash
# Create the Secrets LiteLLM needs (never in Git). Idempotent: existing Secrets are kept,
# because regenerating the DB password would break an existing install.
# Reads ANTHROPIC_API_KEY from the env, or else from the turnstone-bootstrap Secret.
set -euo pipefail
NS=litellm
SECRETS_DIR="$(cd "$(dirname "$0")/.." && pwd)/.secrets"
gen() { openssl rand -hex "${1:-24}"; }
exists() { kubectl -n "$NS" get secret "$1" >/dev/null 2>&1; }

kubectl get namespace "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"

if ! exists litellm-db; then
  kubectl -n "$NS" create secret generic litellm-db \
    --from-literal=username=litellm --from-literal=password="$(gen)" --from-literal=postgres-password="$(gen)"
fi
if ! exists litellm-masterkey; then
  kubectl -n "$NS" create secret generic litellm-masterkey --from-literal=masterkey="sk-$(gen)"
fi
if ! exists litellm-env; then
  key="${ANTHROPIC_API_KEY:-$(kubectl -n turnstone get secret turnstone-bootstrap -o jsonpath='{.data.ANTHROPIC_API_KEY}' | base64 -d)}"
  pw="$(gen 16)"
  kubectl -n "$NS" create secret generic litellm-env \
    --from-literal=ANTHROPIC_API_KEY="$key" --from-literal=UI_USERNAME=admin --from-literal=UI_PASSWORD="$pw"
  mkdir -p "$SECRETS_DIR" && chmod 700 "$SECRETS_DIR"
  ( umask 077; printf 'username=admin\npassword=%s\nmaster_key=%s\n' "$pw" \
    "$(kubectl -n "$NS" get secret litellm-masterkey -o jsonpath='{.data.masterkey}' | base64 -d)" \
    > "$SECRETS_DIR/litellm-admin.txt" )
fi
echo "litellm secrets ready"
