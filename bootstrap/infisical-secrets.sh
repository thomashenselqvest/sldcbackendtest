#!/usr/bin/env bash
# Create the Secrets Infisical needs (never in Git). Idempotent: existing Secrets are kept,
# because regenerating ENCRYPTION_KEY or the DB password would break an existing install.
set -euo pipefail
NS=infisical
SITE_URL="${INFISICAL_SITE_URL:-http://localhost:8881}"
SECRETS_DIR="$(cd "$(dirname "$0")/.." && pwd)/.secrets"
gen() { openssl rand -hex "${1:-24}"; }
exists() { kubectl -n "$NS" get secret "$1" >/dev/null 2>&1; }

kubectl get namespace "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"

if ! exists infisical-secrets; then
  kubectl -n "$NS" create secret generic infisical-secrets \
    --from-literal=ENCRYPTION_KEY="$(openssl rand -hex 16)" \
    --from-literal=AUTH_SECRET="$(openssl rand -base64 32)" \
    --from-literal=SITE_URL="$SITE_URL"
fi
if ! exists infisical-db; then
  pw="$(gen)"
  kubectl -n "$NS" create secret generic infisical-db \
    --from-literal=password="$pw" --from-literal=postgres-password="$(gen)" \
    --from-literal=uri="postgresql://infisical:${pw}@infisical-postgresql:5432/infisicalDB"
fi
if ! exists infisical-redis; then
  pw="$(gen)"
  kubectl -n "$NS" create secret generic infisical-redis \
    --from-literal=redis-password="$pw" \
    --from-literal=url="redis://default:${pw}@infisical-redis-master:6379"
fi
if ! exists infisical-admin; then
  # Login for the Infisical UI; the infisical-config Job bootstraps the instance with it.
  email="${INFISICAL_ADMIN_EMAIL:-admin@sdlc.local}"; pw="$(gen 16)"
  kubectl -n "$NS" create secret generic infisical-admin --from-literal=email="$email" --from-literal=password="$pw"
  mkdir -p "$SECRETS_DIR" && chmod 700 "$SECRETS_DIR"
  ( umask 077; printf 'email=%s\npassword=%s\n' "$email" "$pw" > "$SECRETS_DIR/infisical-admin.txt" )
fi
echo "infisical secrets ready"
