#!/usr/bin/env bash
# Deploy one release to THIS host, for its role, with health verification and rollback.
#
#   deploy.sh <role> <image-tag>
#     role: edge | app | broker | ops | data | migrate
#
# Layout on every host:
#   /opt/taskplatform/
#   ├── shared/            node.env (static, from bootstrap) + rendered secret env files
#   ├── releases/<tag>/    deployment bundle extracted from the image <tag>
#   ├── current -> releases/<tag>
#   └── previous           tag of the last known-good release (used for rollback)
set -euo pipefail

ROLE=${1:?usage: deploy.sh <role> <image-tag>}
TAG=${2:?usage: deploy.sh <role> <image-tag>}
BASE=/opt/taskplatform
RELEASE="$BASE/releases/$TAG"
SHARED="$BASE/shared"

log() { printf '%s [deploy:%s] %s\n' "$(date -u +%FT%TZ)" "$ROLE" "$*"; }
die() { log "ERROR: $*"; exit 1; }

[[ -d "$RELEASE/deploy" ]] || die "release bundle $RELEASE not found (run remote-deploy.sh)"
# shellcheck disable=SC1091
source "$SHARED/node.env"
ROLE=${1}   # node.env also defines ROLE (the host's role); the CLI argument wins ("migrate")
export IMAGE_TAG="$TAG"

compose() {
  local role_file=$1; shift
  docker compose \
    --project-directory "$RELEASE/deploy/compose" \
    --env-file "$SHARED/node.env" \
    -f "$RELEASE/deploy/compose/$role_file.yml" "$@"
}

http_ok() {  # http_ok <url> [attempts]
  local url=$1 attempts=${2:-30}
  for _ in $(seq "$attempts"); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

verify() {
  case "$ROLE" in
    edge)   http_ok "http://127.0.0.1/nginx-health" && http_ok "http://$PRIVATE_IP:8000/health/ready" ;;
    app)    http_ok "http://$PRIVATE_IP:8000/health/ready" && http_ok "http://$PRIVATE_IP:8001/health/ready" ;;
    broker) http_ok "http://$PRIVATE_IP:9101/metrics" && http_ok "http://$PRIVATE_IP:15692/metrics" ;;
    ops)    http_ok "http://127.0.0.1:9090/-/ready" && http_ok "http://127.0.0.1:3000/api/health" ;;
    data)   compose data exec -T postgres pg_isready -q ;;
  esac
}

rollback() {
  local prev
  prev=$(cat "$BASE/previous" 2>/dev/null || true)
  if [[ -z "$prev" || "$prev" == "$TAG" || ! -d "$BASE/releases/$prev" ]]; then
    die "health check failed and no previous release to roll back to"
  fi
  log "health check FAILED — rolling back to $prev"
  ln -sfn "$BASE/releases/$prev" "$BASE/current"
  "$BASE/releases/$prev/deploy/scripts/deploy.sh" "$ROLE" "$prev" --no-rollback || true
  die "deployment of $TAG failed; rolled back to $prev"
}

NO_ROLLBACK=${3:-}

log "rendering secrets from SSM Parameter Store"
"$RELEASE/deploy/scripts/render-env.sh"   # uses ROLE from node.env (migrate runs on 'app')

case "$ROLE" in
  migrate)
    # Runs on the app node, once per deploy, BEFORE any new code starts.
    # Migrations must be backward compatible (expand/contract): old API containers keep
    # running against the new schema during the rolling update.
    log "running database migrations"
    compose app --profile migrate run --rm migrate
    log "migrations applied"
    exit 0
    ;;
  app)
    # Rolling update: one API container at a time. Nginx's passive health check and
    # proxy_next_upstream route around the container that is restarting.
    for svc in api-2 api-3; do
      log "updating $svc"
      compose app up -d --no-deps --wait --wait-timeout 90 "$svc" || [[ -n "$NO_ROLLBACK" ]] || rollback
    done
    compose app up -d --no-deps node-exporter
    ;;
  edge)
    compose edge up -d --no-deps --wait --wait-timeout 90 api-1 || [[ -n "$NO_ROLLBACK" ]] || rollback
    compose edge up -d --no-deps --wait --wait-timeout 60 nginx node-exporter || [[ -n "$NO_ROLLBACK" ]] || rollback
    ;;
  broker|ops|data)
    install -d -m 755 /data
    compose "$ROLE" up -d --remove-orphans --wait --wait-timeout 180 || [[ -n "$NO_ROLLBACK" ]] || rollback
    ;;
  *) die "unknown role: $ROLE" ;;
esac

if ! verify; then
  [[ -n "$NO_ROLLBACK" ]] && die "verification failed during rollback"
  rollback
fi

# Success: record releases for the next rollback.
if [[ -L "$BASE/current" ]]; then
  current_tag=$(basename "$(readlink "$BASE/current")")
  if [[ "$current_tag" != "$TAG" ]]; then echo "$current_tag" > "$BASE/previous"; fi
fi
ln -sfn "$RELEASE" "$BASE/current"
log "release $TAG is live (previous: $(cat "$BASE/previous" 2>/dev/null || echo none))"

# Housekeeping: keep the 5 newest releases and prune old images (disk-full prevention).
( cd "$BASE/releases" && ls -1t | tail -n +6 | xargs -r rm -rf )
docker image prune -af --filter "until=168h" >/dev/null || true
