#!/usr/bin/env bash
# Render per-service env files from AWS SSM Parameter Store.
#
#   render-env.sh            (runs on an EC2 host as root; uses the instance IAM role)
#
# Why Parameter Store: secrets are encrypted with KMS at rest, access is controlled and
# audited by IAM/CloudTrail, and no long-lived credentials are needed — the EC2 instance
# role grants ssm:GetParametersByPath on /taskplatform/prod/* only.
#
# Least privilege at the container level: each file contains only what one service needs.
set -euo pipefail

SHARED=/opt/taskplatform/shared
# shellcheck disable=SC1091
source "$SHARED/node.env"
: "${AWS_REGION:?}" "${SSM_PARAMETER_PATH:?}"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Fetch every parameter under the path, decrypted, as NAME=VALUE lines.
aws ssm get-parameters-by-path \
  --region "$AWS_REGION" \
  --path "$SSM_PARAMETER_PATH" \
  --with-decryption \
  --recursive \
  --query 'Parameters[].[Name,Value]' \
  --output text |
while IFS=$'\t' read -r name value; do
  printf '%s=%s\n' "${name##*/}" "$value"
done > "$tmp/all.env"

get() { grep -E "^$1=" "$tmp/all.env" | head -n1 | cut -d= -f2- || true; }

# Validate up front: `exit` inside $(...) would only leave the subshell, not this script.
missing=()
for key in JWT_SECRET_KEY POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD REDIS_PASSWORD \
           RABBITMQ_USER RABBITMQ_PASSWORD GRAFANA_ADMIN_PASSWORD; do
  [[ -n "$(get "$key")" ]] || missing+=("$SSM_PARAMETER_PATH/$key")
done
if (( ${#missing[@]} )); then
  echo "missing SSM parameters: ${missing[*]}" >&2
  exit 1
fi
require() { get "$1"; }

umask 077   # files are created 0600: readable by root only

cat > "$tmp/app.env" <<EOF
JWT_SECRET_KEY=$(require JWT_SECRET_KEY)
POSTGRES_DB=$(require POSTGRES_DB)
POSTGRES_USER=$(require POSTGRES_USER)
POSTGRES_PASSWORD=$(require POSTGRES_PASSWORD)
REDIS_PASSWORD=$(require REDIS_PASSWORD)
RABBITMQ_USER=$(require RABBITMQ_USER)
RABBITMQ_PASSWORD=$(require RABBITMQ_PASSWORD)
CORS_ALLOWED_ORIGINS=$(get CORS_ALLOWED_ORIGINS)
RATE_LIMIT_REQUESTS=$(get RATE_LIMIT_REQUESTS)
EOF
# Drop empty optional lines so pydantic defaults apply.
sed -i -E '/^[A-Z_]+=$/d' "$tmp/app.env"

case "${ROLE:?}" in
  data)
    cat > "$tmp/postgres.env" <<EOF
POSTGRES_DB=$(require POSTGRES_DB)
POSTGRES_USER=$(require POSTGRES_USER)
POSTGRES_PASSWORD=$(require POSTGRES_PASSWORD)
DATA_SOURCE_USER=$(require POSTGRES_USER)
DATA_SOURCE_PASS=$(require POSTGRES_PASSWORD)
EOF
    printf 'REDIS_PASSWORD=%s\n' "$(require REDIS_PASSWORD)" > "$tmp/redis.env"
    ;;
  broker)
    cat > "$tmp/rabbitmq.env" <<EOF
RABBITMQ_DEFAULT_USER=$(require RABBITMQ_USER)
RABBITMQ_DEFAULT_PASS=$(require RABBITMQ_PASSWORD)
EOF
    ;;
  ops)
    printf 'GF_SECURITY_ADMIN_PASSWORD=%s\n' "$(require GRAFANA_ADMIN_PASSWORD)" > "$tmp/grafana.env"
    ;;
esac

# Data services never need the application secrets.
[[ "$ROLE" == "data" ]] && rm -f "$tmp/app.env"

rm -f "$tmp/all.env"
install -d -m 700 "$SHARED"
for f in "$tmp"/*.env; do
  install -m 600 -o root -g root "$f" "$SHARED/$(basename "$f")"
done
echo "rendered: $(cd "$SHARED" && ls -1 ./*.env | grep -v node.env | tr '\n' ' ')"
