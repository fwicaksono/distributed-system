#!/usr/bin/env bash
# Obtain the first Let's Encrypt certificate on the edge node, then switch Nginx to HTTPS.
#
#   sudo ./issue-certificate.sh admin@example.com
#
# Preconditions (the script checks them):
#   1. DNS A record for $DOMAIN points to the Elastic IP of this instance.
#   2. Port 80 is open to 0.0.0.0/0 in sg-edge (Let's Encrypt validates from many IPs).
#   3. Nginx is running with the edge-http template (serves /.well-known/acme-challenge/).
set -euo pipefail

EMAIL=${1:?usage: issue-certificate.sh <email for expiry notices>}
BASE=/opt/taskplatform
# shellcheck disable=SC1091
source "$BASE/shared/node.env"
: "${DOMAIN:?}"
COMPOSE=(docker compose --project-directory "$BASE/current/deploy/compose"
         --env-file "$BASE/shared/node.env" -f "$BASE/current/deploy/compose/edge.yml")
export IMAGE_TAG
IMAGE_TAG=$(basename "$(readlink -f "$BASE/current")")

public_ip=$(curl -fsS --max-time 3 https://checkip.amazonaws.com | tr -d '[:space:]')
dns_ip=$(dig +short A "$DOMAIN" | tail -n1)
echo "public IP: $public_ip | $DOMAIN resolves to: ${dns_ip:-<nothing>}"
[[ "$public_ip" == "$dns_ip" ]] || { echo "DNS does not point to this instance yet (propagation can take minutes)"; exit 1; }

echo "==> requesting certificate (staging first avoids Let's Encrypt rate limits)"
"${COMPOSE[@]}" --profile certbot run --rm certbot certonly \
  --webroot -w /var/www/certbot -d "$DOMAIN" \
  --email "$EMAIL" --agree-tos --no-eff-email --non-interactive --staging

echo "==> staging succeeded; requesting the real certificate"
"${COMPOSE[@]}" --profile certbot run --rm certbot certonly \
  --webroot -w /var/www/certbot -d "$DOMAIN" \
  --email "$EMAIL" --agree-tos --no-eff-email --non-interactive --force-renewal

echo "==> switching Nginx to the HTTPS template"
sed -i 's/^NGINX_SITE_TEMPLATE=.*/NGINX_SITE_TEMPLATE=edge-https/' "$BASE/shared/node.env"
"${COMPOSE[@]}" up -d --no-deps --force-recreate nginx

sleep 3
curl -fsS "https://$DOMAIN/health/live" && echo -e "\nHTTPS is live for $DOMAIN"
